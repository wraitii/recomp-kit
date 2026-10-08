"""Opt-in SSA bodies inside the existing production dispatch ABI.

Run only after decoded discovery and its boundary/dispatch checks have completed.
Unsupported bodies retain decoded C whole; no summary changes the guest ABI.
Calls publish/reload complete tracked state and use the production entry thunk,
including replacement, hook, profiling and frame-watch policy. Indirect calls
are admitted only through the explicit `recomp_call` opt-in; complex host-frame
ownership and unsupported division shapes remain on the decoded path.
"""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import os
import re
import time

from .cfg import function_ir
from .emit_c import emit
from .lift import Lifter, LiftError
from .ssa import SSAError

# Bound Python graph/phi construction before it can consume excessive resources.
# This is a code-generation budget, never a guest execution limit.
MAX_INSTRUCTIONS = 2048

#: Bodies lifted by the parent and handed to workers per batch. Bounds the
#: parent's copy of lifted IR while keeping each pool submission large enough
#: to amortize IPC.
_EMIT_BATCH = 2048

#: Below this many functions the pool startup cost outweighs the parallelism,
#: so small corpora and unit fixtures stay in-process.
_PARALLEL_MIN = 64

#: Per-process lifter, built once by the pool initializer. `emit` only reads it
#: to name registers, so it is safe to share across the batch a worker runs.
_WORKER_LIFTER = None


def _worker_init():
    global _WORKER_LIFTER
    _WORKER_LIFTER = Lifter()


def _emit_one(task, lifter):
    """Run one production emit; return (index, source|None, reason|None, facts)."""
    index, symbol, fir, options = task
    options = dict(options)
    options["lifter"] = lifter
    facts = {}
    try:
        source = emit(fir, symbol, facts=facts, **options)
    except (SSAError, LiftError) as error:
        return index, None, str(error), {}
    except RecursionError:
        return index, None, "SSA graph recursion budget", {}
    return index, source, None, facts


def _worker_emit(task):
    return _emit_one(task, _WORKER_LIFTER)


def _jobs(count):
    """Worker count: all cores unless RECOMP_SSA_JOBS overrides it."""
    override = os.environ.get("RECOMP_SSA_JOBS")
    if override is not None:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return max(1, min(count, os.cpu_count() or 1))


def exclusion(tr, fn, entries, policies):
    """Reject production contracts the corpus emitter does not implement."""
    external = set(entries) - tr.internal_entries.get(fn.addr, set())
    if external:
        return "alternate entries"
    if entries:
        # Only switch-case blocks: not an entry problem. The SSA builder decides
        # whether it can lower the table jump (today it names BRANCHIND).
        return "jump table"
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
    relaxed = settings.get("fault_state", "relaxed") == "relaxed"
    mode, state = ("scalar", "locals") if relaxed else ("scalar-strict", "strict")
    convention = settings.get("msvc_x87_convention", True)
    common = {
        "x87_scalar_strict": not relaxed,
        "local_state": relaxed,
        "msvc_convention": convention,
        "lazy_nan": True,
        "resumable_stacks": policies.get("resumable_stacks", False),
        "indirect_call_symbol": "recomp_call",
    }
    results, reasons, census = {}, Counter(), Counter()
    # Lifting needs the parent's image and SLEIGH context; emission does not.
    # Lift here, then emit each batch in workers. Tasks carry their function
    # index so ordered combination is exact, and the emitted text is
    # hash-seed-independent (see x87_scalar.PART_ORDER).
    jobs = _jobs(len(functions))
    use_pool = jobs > 1 and len(functions) >= _PARALLEL_MIN
    pool = ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init) if use_pool else None
    batch = []
    reasons_by_index = {}
    outcomes = {}

    def flush():
        if not batch:
            return
        tasks = [(index, "fn_%08x" % fn.addr, fir, options)
                 for index, fn, fir, options in batch]
        if pool is not None:
            for index, source, reason, facts in pool.map(_worker_emit, tasks, chunksize=8):
                outcomes[index] = (source, reason, facts)
        else:
            for task in tasks:
                index, source, reason, facts = _emit_one(task, lifter)
                outcomes[index] = (source, reason, facts)
        batch.clear()

    try:
        for index, fn in enumerate(functions, 1):
            reason = "auxiliary module" if module else exclusion(
                tr, fn, entries_by_fn.get(fn.addr, ()), policies)
            if reason is None:
                calls = {}
                for ins in fn.insns:
                    if ins.mnem == "CALL":
                        target = tr.branch_target(ins)
                        if target in known and target not in forbidden:
                            calls[target] = "entry_%08x" % target
                try:
                    fir = function_ir(tr, lifter, fn)
                except (SSAError, LiftError) as error:
                    reason = str(error)
                except RecursionError:
                    reason = "SSA graph recursion budget"
                else:
                    options = dict(common)
                    options["call_symbols"] = calls
                    batch.append((index, fn, fir, options))
                    if len(batch) >= _EMIT_BATCH * jobs:
                        flush()
            if reason is not None:
                reasons_by_index[index] = reason
            if not quiet and (index % 1000 == 0 or index == len(functions)):
                print("  ir SSA: %d/%d final bodies (%.1fs)" % (
                    index, len(functions), time.monotonic() - started), flush=True)
        flush()
    finally:
        if pool is not None:
            pool.shutdown()

    for index, fn in enumerate(functions, 1):
        if index in outcomes:
            source, reason, facts = outcomes[index]
        else:
            source, reason, facts = None, reasons_by_index[index], {}
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
    total = len(results)
    emitted = sum(row["emitted"] for row in results.values())
    report = {
        "enabled": True, "x87": mode, "state": state, "msvc_convention": convention,
        "lazy_nan": True,
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
