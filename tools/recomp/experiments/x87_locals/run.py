"""Isolated straight-line x87 lifting probe; never used by game translation.

Only self-contained FLD/F{ADD,SUB,MUL}/FADDP/FSTP float-memory regions are
accepted. Full mode preserves final runtime state, including popped contents;
it does NOT reconstruct intermediate state at faults or asynchronous observers.
"""
from pathlib import Path
from types import SimpleNamespace
import json
import re
import subprocess

import translate as T

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[3]
MODES = ("baseline", "full", "live", "relaxed")
# Synthetic fixtures, not addresses or recovered routines from any game.
CASES = {
    "dot_store": ["FLD float ptr [ESI + 0x4]", "FMUL float ptr [EDI + 0x4]",
                  "FLD float ptr [ESI + 0x8]", "FMUL float ptr [EDI + 0x8]",
                  "FADDP", "FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                  "FADDP", "FADD float ptr [EDI + 0xc]", "FSTP float ptr [EBX]"],
    "dot_live": ["FLD float ptr [ESI + 0x4]", "FMUL float ptr [EDI + 0x4]",
                 "FLD float ptr [ESI + 0x8]", "FMUL float ptr [EDI + 0x8]",
                 "FADDP", "FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                 "FADDP", "FADD float ptr [EDI + 0xc]"],
    "store_reload": ["FLD float ptr [ESI]", "FSUB float ptr [EDI]",
                     "FSTP float ptr [EBX]", "FLD float ptr [ESI]",
                     "FMUL float ptr [EDI + 0x4]", "FSTP float ptr [EBX + 0x4]"],
}
PRODUCTION_CASES = {
    "compare_status": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                       "FNSTSW AX", "FCOMP float ptr [EDI]"],
    "integer_boundary": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                         "FILD qword ptr [ESI]", "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]"],
    "register_direction": ["FLD float ptr [ESI]", "FLD float ptr [EDI]", "FMUL ST1,ST0",
                           "FSUBR ST0,ST1", "FSUBP ST1,ST0", "FSTP float ptr [EBX]"],
    "integer_alias": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                      "MOV EAX,dword ptr [ESI + 4]", "MOV dword ptr [ESI],EAX",
                      "FADD float ptr [ESI]", "FSTP float ptr [EBX]"],
    "diamond": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                "TEST EBX,1", "JZ 0x00100007", "FSUB float ptr [ESI]",
                "JMP 0x00100008", "FMUL float ptr [EDI]", "FSTP float ptr [EBX]"],
    "loop": ["MOV EAX,3", "FLD float ptr [ESI]", "FMUL float ptr [EDI]",
             "FADD float ptr [EDI + 4]", "FSTP float ptr [EBX]", "DEC EAX",
             "JNZ 0x00100001", "NOP"],
    "incoming_metadata": ["TEST EBX,1", "JZ 0x00100007", "FLD float ptr [ESI]",
                          "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                          "FSTP float ptr [EBX]", "JMP 0x00100008", "NOP", "NOP"],
}


def emit_production(lines, enabled):
    """Use the C driver's instruction output and its actual region lowering."""
    from x87_locals import lower_regions
    insns = T.parse_listing_text("\n".join(f"{0x100000 + i:08x}  {s}" for i, s in enumerate(lines)))
    tr = T.Translator(None, set(), SimpleNamespace(eager_flags=True))
    fn = T.Function(0x100000, "fragment", len(insns), insns)
    tr.prepare(fn)
    fn.index = {ins.addr: i for i, ins in enumerate(insns)}
    fn.pushed_continuations = set()
    labels = {tr.branch_target(ins) for ins in insns if ins.mnem in T.JCC or ins.mnem == "JMP"}
    labels.discard(None)
    bodies = {i: tr.emit(fn, i, T.ALL_FLAGS) for i in range(len(insns))}
    if enabled:
        bodies, _, _ = lower_regions(fn, bodies, labels, set(), T.parse_operand,
                                     cfg=(tr.successors, tr.branch_target, T.JCC, ()))
    return "\n".join(line for i, ins in enumerate(insns)
                     for line in ([f"L_{ins.addr:08x}: ;"] if ins.addr in labels else []) + bodies[i])


def emit(lines, mode):
    """Map logical stack positions to SSA-like C temporaries, then commit once."""
    if mode not in MODES:
        raise ValueError(mode)
    insns = T.parse_listing_text("\n".join(f"{0x100000 + i:08x}  {s}" for i, s in enumerate(lines)))
    tr = T.Translator(None, set(), SimpleNamespace(eager_flags=False))
    if mode == "full":
        # Exercise the reusable production C pass, retaining the eager emitter
        # as this experiment's baseline. Scope checks remain shared below.
        emit(lines, "baseline")
        return emit_production(lines, True)
    out = ["const unsigned top = c->fpu_top;"] if mode != "baseline" else []
    stack, last, serial = [], {}, 0

    def value(expr):
        nonlocal serial
        name = f"v{serial}"
        serial += 1
        out.append(f"double {name} = {expr};")
        return name

    for ins in insns:
        ops = [T.parse_operand(o) for o in ins.ops]
        m = ins.mnem
        if m not in {"FLD", "FMUL", "FADD", "FSUB", "FADDP", "FSTP"}:
            raise ValueError(f"unsupported instruction: {ins}")
        if m == "FADDP":
            if ops or len(stack) < 2:
                raise ValueError("FADDP requires two locally defined values and implicit operands")
        elif len(ops) != 1 or ops[0].kind != "mem" or ops[0].size != 32 or ops[0].seg:
            raise ValueError(f"only ordinary binary32 memory operands supported: {ins}")
        if m not in {"FLD", "FADDP"} and not stack:
            raise ValueError("region consumes an incoming x87 value")
        if m == "FLD" and len(stack) == 8:
            raise ValueError("region overflows x87 stack")
        # Track even baseline so every variant has the same supported domain.
        out.append(f"/* {ins.raw.split('  ', 1)[1]} */")
        if mode == "baseline":
            out.extend(["{"] + tr.emit_x87(None, ins, m, ops) + ["}"])
        if m == "FLD":
            v = value(tr.x87_mem_value(ops[0])) if mode != "baseline" else "unused"
            stack.append(v)
            last[-len(stack)] = v
        elif m == "FSTP":
            if mode != "baseline":
                out.append(f"wrf32({T.addr_expr(ops[0])}, fto_float(c, {stack[-1]}));")
            stack.pop()
        else:
            if m == "FADDP":
                rhs, lhs, op = stack.pop(), stack[-1], "+"
            else:
                lhs, op = stack[-1], {"FMUL": "*", "FADD": "+", "FSUB": "-"}[m]
                rhs = value(tr.x87_mem_value(ops[0])) if mode != "baseline" else "unused"
            expr = f"{lhs} {op} {rhs}"
            if mode != "relaxed":
                expr = f"fx87(c, {expr})"
            v = value(expr) if mode != "baseline" else "unused"
            stack[-1] = v
            last[-len(stack)] = v
    if mode != "baseline":
        for offset, v in sorted(last.items()):
            phys = f"((top + ({offset})) & 7u)"
            live = -offset <= len(stack)
            if mode == "full" or live:
                out.extend([f"c->st[{phys}] = {v};", f"c->st_bits[{phys}] = 0;"])
            out.append(f"c->st_exact[{phys}] = 0;")
            tag = f"ftag_classify({v})" if live else "FTAG_EMPTY"
            out.append(f"ftag_put(c, {phys}, {tag});")
        out.append(f"c->fpu_top = (top - {len(stack)}u) & 7u;")
    return "\n".join(out)


def run_experiment(out, cmake, jobs):
    """Generate, build through CMake, execute comparisons and save raw evidence."""
    out.mkdir(parents=True, exist_ok=True)
    code = ['#include "x86.h"']
    for name, lines in CASES.items():
        for mode in MODES:
            code.append(f"void {name}_{mode}(X86 *c) {{\n{emit(lines, mode)}\n}}")
    for name, lines in PRODUCTION_CASES.items():
        for mode in MODES:
            # The live/relaxed columns are eager controls for these additional
            # production-pass regressions, not relaxed-contract experiments.
            code.append(f"void {name}_{mode}(X86 *c) {{\n{emit_production(lines, mode == 'full')}\n}}")
    (out / "generated.c").write_text("\n".join(code) + "\n")
    declarations = ['static const char *mode_names[] = {"baseline", "full", "live", "relaxed"};',
                    "static const unsigned normalize_empty_mask = 12, required_match_mask = 6;"]
    for name in {**CASES, **PRODUCTION_CASES}:
        for mode in MODES:
            declarations.append(f"void {name}_{mode}(X86 *);")
    cases = {**CASES, **PRODUCTION_CASES}
    declarations += ["static const char *case_names[] = {" + ','.join(f'"{n}"' for n in cases) + "};",
                     "static void (*functions[][4])(X86 *) = {" +
                     ','.join('{' + ','.join(f"{n}_{m}" for m in MODES) + '}' for n in cases) + "};"]
    (out / "fixtures.h").write_text("\n".join(declarations))
    subprocess.run([cmake, "-S", str(HERE), "-B", str(out),
                    f"-DKIT_RUNTIME={KIT / 'runtime'}"], check=True)
    subprocess.run([cmake, "--build", str(out), "--parallel", str(jobs)], check=True)
    result = subprocess.run([str(out / "x87_locals")], check=True, capture_output=True, text=True)
    (out / "results.txt").write_text(result.stdout)
    print(result.stdout, end="")
    # Clang save-temps retains the actual optimized assembly used in the build.
    counts = {}
    for path in out.rglob("generated*.s"):
        current = None
        for line in path.read_text().splitlines():
            match = re.match(r"_?((?:dot_store|dot_live|store_reload)_(?:baseline|full|live|relaxed)):", line)
            if match:
                current = match[1]
                counts[current] = 0
            elif line.startswith("\t.cfi_endproc"):
                current = None
            elif current and re.match(r"\t[a-z][a-z0-9.]*\s", line):
                counts[current] += 1
    (out / "assembly-counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    print("Static host instruction counts:", json.dumps(counts, sort_keys=True))
    print("Artifacts:", out)
