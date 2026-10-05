"""Opt-in whole-function C scalarization of GPRs and arithmetic flags.

Keep ESP/EBP/EIP eager for fatal host diagnostics. Decoded instructions and
the driver's control transfers determine observation boundaries; a body with
an unknown CPU helper stays eager. Calls publish all locally written fields
and reload every cached field, without assuming an ABI clobber set. Scalar C
lvalues let the native compiler perform value/phi and dead-store analysis over
the existing CFG, including across x87 region scopes and alternate entries.

CPU objects must live outside the guest arena. The kit's ordinary arena loads,
dirty tracking and backtrace-only watchpoints cannot mutate or inspect these
fields. RECOMP_NULL_CHECKS builds instead point the lvalues at CPU fields, so
guest null-fault/SEH dispatch retains eager state inside each instruction.

DIVERGENCE(original): [cpu-locals] interior fatal faults may expose preceding
published scratch registers/flags. ESP/EBP/EIP remain eager; calls, returns and
opaque helpers retain the comparison emitter's state. No outgoing residue is
discarded and no arithmetic or guest-memory ordering is changed.
"""
import re
from collections import Counter


FIELD = re.compile(r"c->(?:r\[([012367])\]|eflags_(cf|zf|sf|of|pf|af)\b)")
COMMENTS = re.compile(r"/\*.*?\*/", re.S)
ADDRESS_OF = re.compile(r"&\s*(?:\(\s*)*c->")
# A bare CPU field is a value operand before '&', never a cast or keyword.
# Recognize only this bounded emitter shape; other apparent escapes stay eager.
BINARY_AND = re.compile(
    r"(c->(?:r\[[0-7]\]|eflags_(?:cf|zf|sf|of|pf|af)\b))\s*&\s*(?=(?:\(\s*)*c->)")
INITIALIZATION_BEGIN = "/* local CPU values; eager lvalues with guest null checks */"
INITIALIZATION_END = "/* end local CPU initialization */"
# Only these helpers' CPU arguments are known to access exclusively x87 state.
# All GPR/flag helpers remain barriers, even if their mnemonic is supported.
X87_HELPER = re.compile(
    r"\b(?:ST|fpush|fpush_int|fpush_st|fset|fcopy|fdrop|ftag_of|ftag_put|fstsw|"
    r"fist_i(?:16|32|64)|"
    r"fx87|fx87_exact|fdivz|fcom|fucom|fto_float)\(c(?=\s*[,\)])")
FLAG_HELPER = re.compile(
    r"\b((?:shl|shr|sar|rol|ror|rcl|rcr)(?:8|16|32)_f|"
    r"sh[lr]d32_f|imul2_(?:16|32)_f)\(c(?=\s*,)")


def helper_flags(name):
    """Audited x86.h helpers touch flags only; all original expressions stay."""
    if name.startswith(("rol", "ror", "rcl", "rcr", "imul2_")):
        return {"eflags_cf", "eflags_of"}
    return {"eflags_cf", "eflags_of", "eflags_zf", "eflags_sf", "eflags_pf"}


def field_name(match):
    return "r" + match[1] if match[1] is not None else "eflags_" + match[2]


def cpu_field(name):
    return f"c->r[{name[1]}]" if name.startswith("r") else "c->" + name


def local(name):
    return f"(*cpu_{name}_ptr_)"


def transparent(body):
    """Refuse escapes, dispatch macros and unmodeled CPU-pointer helpers.

    This is an effect check on the ordinary emitter's bounded C expressions,
    not an attempt to infer arbitrary C semantics. Refusal retains the exact
    baseline body. A dynamic register index or a symbolic index likewise
    requires eager state rather than guessing which cached fields it uses.
    """
    code = COMMENTS.sub("", "\n".join(body))
    escape_code = BINARY_AND.sub(r"\1 bitwise_and ", code).replace("&&", " logical_and ")
    if ("recomp_" in code or "CALL_FN" in code or
            ADDRESS_OF.search(escape_code)):
        return False
    code = FIELD.sub("field_", code)
    if "c->r[" in code:
        # ESP/EBP are intentionally eager, but other unresolved indices are not.
        code = re.sub(r"c->r\[[45]\]", "eager_", code)
        if "c->r[" in code:
            return False
    code = X87_HELPER.sub("x87_helper_(cpu_", code)
    code = FLAG_HELPER.sub("flag_helper_(cpu_", code)
    code = re.sub(r"c->\w+", "eager_", code)
    return re.search(r"\bc\b", code) is None


def lower_function(bodies):
    """Return rewritten bodies, entry declarations and exit publications.

    Every label belongs to the existing function scope; initialize before the
    entry dispatch so alternate entries and backedges never skip initialization.
    Publication before an opaque body also covers conditional outward transfers
    in that body. Their returning/fall-through path reloads after the body.
    Driver-added listing-gap/final transfers must use the returned publication.
    Select reused fields with a bounded live set: at most six fields in integer
    code and one or two alongside floating-point lowering. The source-shape
    heuristic is a register-pressure guard, never an execution-speed claim.
    """
    safe = {i for i, body in bodies.items() if transparent(body)}
    uses = Counter()
    reads = Counter()
    writes = Counter()
    written = set()
    for i in safe:
        code = COMMENTS.sub("", "\n".join(bodies[i]))
        for match in FLAG_HELPER.finditer(code):
            for name in helper_flags(match[1]):
                written.add(name)
                uses[name] += 1
        for match in FIELD.finditer(code):
            name = field_name(match)
            uses[name] += 1
            if re.match(r"\s*(?:=(?!=)|[+\-*/&|^]=|\+\+|--)", code[match.end():]):
                written.add(name)
                writes[name] += 1
            else:
                reads[name] += 1
    # Outgoing flags remain exact, but caching flags with no local consumer
    # only prolongs their native live ranges through x87 arithmetic. Keep them
    # eager. Bound the remaining live set instead of caching every CPU field.
    candidates = [name for name, count in uses.items() if count >= 3 and
                  (name.startswith("r") or reads[name])]
    floating = sum(len(re.findall(r"\b(?:fx87|fx87_exact|fcom|fucom|fto_float|fdivz)\(",
                                 COMMENTS.sub("", "\n".join(body)))) for body in bodies.values())
    # Existing x87 locals can already carry several value/tag/exact tuples.
    # Spend fewer GPR/flag registers alongside them, and leave float-dominated
    # leaf arithmetic eager when scalar CPU traffic is too small to justify it.
    budget = (1 if floating > 128 else 2) if floating else 6
    names = sorted(sorted(candidates, key=lambda name: (-reads[name], -uses[name], name))[:budget])
    # Eager writes to other GPRs do not pay for the cached field's live range.
    # Count only selected GPR writes: newly transparent float regions must not
    # activate a sparse cache merely because they contain many unrelated moves.
    gp_writes = sum(writes[name] for name in names if name.startswith("r"))
    if floating and (floating > 8 * gp_writes or (gp_writes <= 8 and floating > gp_writes)):
        return bodies, [], [], 0
    if not names or not (written & set(names)):
        return bodies, [], [], 0
    selected = set(names)
    declarations = [INITIALIZATION_BEGIN]
    for name in names:
        declarations += [f"uint32_t cpu_{name}_value_ = {cpu_field(name)};",
                         f"uint32_t *cpu_{name}_ptr_ = &{cpu_field(name)};"]
    declarations.append("#if !defined(RECOMP_NULL_CHECKS) || !RECOMP_NULL_CHECKS")
    declarations += [f"cpu_{name}_ptr_ = &cpu_{name}_value_;" for name in names]
    declarations.append("#else")
    declarations += [f"(void)cpu_{name}_value_;" for name in names]
    declarations.append("#endif")
    declarations.append(INITIALIZATION_END)
    publish = [f"{cpu_field(name)} = {local(name)};" for name in names if name in written]
    reload = [f"{local(name)} = {cpu_field(name)};" for name in names]

    def replace(match):
        name = field_name(match)
        return local(name) if name in selected else match[0]

    result = {}
    for i, body in bodies.items():
        if i in safe:
            result[i] = []
            for line in body:
                touched = set().union(*(helper_flags(m[1]) for m in
                                      FLAG_HELPER.finditer(COMMENTS.sub("", line)))) & selected
                # A known flag helper reads/writes the real CPU flags, so only
                # its affected cached flags need publication and invalidation.
                # GPR arguments/results still use locals. Unknown helpers below
                # retain full publication/reload without an assumed ABI.
                result[i] += [f"{cpu_field(name)} = {local(name)};" for name in sorted(touched)]
                result[i].append(FIELD.sub(replace, line))
                result[i] += [f"{local(name)} = {cpu_field(name)};" for name in sorted(touched)]
        else:
            result[i] = publish + body + reload
    return result, declarations, publish, len(names)


def after_initialization(lines):
    """Strip only this pass's marked host initialization for entry validation."""
    if lines and lines[0].strip() == INITIALIZATION_BEGIN:
        end = next((i for i, line in enumerate(lines)
                    if line.strip() == INITIALIZATION_END), None)
        if end is not None:
            return lines[end + 1:]
    return lines
