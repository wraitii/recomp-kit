"""Opt-in SSA bodies inside the existing production dispatch ABI.

Run only after decoded discovery and its boundary/dispatch checks have completed.
Unsupported bodies retain decoded C whole; no summary changes the guest ABI.
Calls publish/reload complete tracked state and use the production entry thunk,
including replacement, hook, profiling and frame-watch policy. Indirect calls
are admitted only through the explicit `recomp_call` opt-in; complex host-frame
ownership and unsupported division shapes remain on the decoded path.
"""
from collections import Counter
import re
import time

from .cfg import function_ir
from .emit_c import emit
from .lift import Lifter, LiftError
from .ssa import SSAError

# Bound Python graph/phi construction before it can consume excessive resources.
# This is a code-generation budget, never a guest execution limit.
MAX_INSTRUCTIONS = 2048


def exclusion(tr, fn, entries, policies):
    """Reject production contracts the corpus emitter does not implement."""
    if entries:
        return "alternate entries"
    if fn.addr in policies.get("intrinsic_bodies", {}):
        return "runtime intrinsic"
    if (fn.seh_sites or fn.seh_restores or fn.seh_escapes
            or fn.addr in tr.seh_helpers):
        return "SEH frame ownership"
    if fn.pushed_continuations or fn.return_jumps:
        return "guest continuations"
    if fn.dead_addrs or fn.addr in tr.noreturn_callees:
        return "nonreturning control flow"
    if len(fn.insns) > MAX_INSTRUCTIONS:
        return "SSA instruction budget"
    rewritten = (set(policies.get("instruction_patches", ()))
                 | set(policies.get("operand_redirects", ()))
                 | set(policies.get("volatile_reads", ())))
    if fn.addrs & rewritten:
        return "audited instruction rewrite"
    return None


def apply(tr, functions, bodies, entries_by_fn, settings, *, policies=None,
          module=False, quiet=False):
    """Replace admitted final bodies and return exact final-image coverage."""
    policies = policies or {}
    started = time.monotonic()
    lifter = Lifter()
    known = {fn.addr for fn in functions}
    forbidden = (set(tr.seh_helpers) | set(tr.noreturn_callees)
                 | set(policies.get("intrinsic_bodies", {})))
    mode = settings.get("ir_ssa_x87", "scalar")
    state = settings.get("ir_ssa_state", "locals")
    convention = settings.get("ir_ssa_msvc_convention", True)
    results, reasons, census = {}, Counter(), Counter()
    for index, fn in enumerate(functions, 1):
        reason = "auxiliary module" if module else exclusion(
            tr, fn, entries_by_fn.get(fn.addr, ()), policies)
        source = None
        if reason is None:
            calls = {}
            for ins in fn.insns:
                if ins.mnem == "CALL":
                    target = tr.branch_target(ins)
                    if target in known and target not in forbidden:
                        calls[target] = "entry_%08x" % target
            facts = {}
            try:
                fir = function_ir(tr, lifter, fn)
                source = emit(fir, "fn_%08x" % fn.addr, call_symbols=calls,
                              x87_scalar_strict=mode == "scalar-strict",
                              local_state=state == "locals",
                              msvc_convention=convention,
                              resumable_stacks=policies.get("resumable_stacks", False),
                              indirect_call_symbol="recomp_call",
                              lifter=lifter, facts=facts)
            except (SSAError, LiftError) as error:
                reason = str(error)
            except RecursionError:
                reason = "SSA graph recursion budget"
        if reason is None:
            # Keep the production CALL_FN spelling: local declarations, module
            # retargeting and dispatch checks already understand this seam.
            source = re.sub(r"\bentry_([0-9a-f]{8})\(c\);", r"CALL_FN(\1);", source)
            bodies[fn.addr] = source.splitlines()
            census.update(key for key, value in facts.items() if value)
        else:
            reasons[re.sub(r"^[0-9a-f]{8}: ", "", reason)] += 1
        results["%08x" % fn.addr] = {
            "name": fn.name, "emitted": reason is None, "reason": reason,
            "instructions": len(fn.insns),
        }
        if not quiet and (index % 1000 == 0 or index == len(functions)):
            print("  ir SSA: %d/%d final bodies (%.1fs)" % (
                index, len(functions), time.monotonic() - started), flush=True)
    total = len(results)
    emitted = sum(row["emitted"] for row in results.values())
    report = {
        "enabled": True, "x87": mode, "state": state, "msvc_convention": convention,
        "functions": total, "emitted": emitted, "fallback": total - emitted,
        "emitted_percent": 100 * emitted / total if total else 0,
        "fallback_percent": 100 * (total - emitted) / total if total else 0,
        "denominator": "Final emitted function bodies, including recovered bodies; alternate entries are not separate functions.",
        "fallback_reasons": dict(reasons.most_common()),
        # Emitted bodies that read flags at an entry/call boundary, or
        # kept the exact x87 flush. A sanity census, not an admission gate.
        "convention_census": {key: census[key] for key in (
            "flags_read_at_entry", "flags_read_after_call", "x87_exact_flush")},
        "seconds": round(time.monotonic() - started, 3), "per_function": results,
    }
    if not quiet:
        print("  ir SSA: %d emitted (%.2f%%), %d decoded fallback (%.2f%%)" % (
            emitted, report["emitted_percent"], total - emitted, report["fallback_percent"]), flush=True)
    return report
