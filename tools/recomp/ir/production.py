"""Production SSA bodies inside the existing dispatch ABI.

Calls publish/reload complete tracked state and use the production entry thunk,
including replacement, hook, profiling and frame-watch policy. Indirect calls
are admitted only through the explicit `recomp_call` opt-in; complex host-frame
ownership and unsupported shapes keep the decoded body whole.
"""
import re

from decoded import function_ir
from .emit_c import emit
from .lift import LiftError
from .ssa import SSAError

# Bound Python graph/phi construction before it can consume excessive resources.
# This is a code-generation budget, never a guest execution limit.
MAX_INSTRUCTIONS = 16384


def external_entries(tr, fn, entries):
    """Entries other code can reach, not only switch cases of this body.

    With any, the SSA body takes every entry and the wrappers follow; with
    none, `apply` keeps the decoded body for the switch-case wrappers."""
    entries = sorted(e for e in entries if e in fn.index)
    internal = tr.internal_entries.get(fn.addr, set())
    return entries if any(e not in internal for e in entries) else []


def exclusion(fn):
    if fn.pushed_continuations or fn.return_jumps:
        return "guest continuations"
    if len(fn.insns) > MAX_INSTRUCTIONS:
        return "SSA instruction budget"
    return None


def emit_ssa(tr, lifter, fn, entries, common, known, contracts):
    """The SSA body of `fn` as C lines, with its wrappers when other code can enter it.

    Raises SSAError or LiftError for a shape that stays decoded."""
    calls = {}
    for ins in fn.insns:
        if ins.mnem == "CALL":
            target = tr.branch_target(ins)
            if target in known:
                calls[target] = "entry_%08x" % target
    fir = function_ir(tr, lifter, fn)
    fir.entries = external_entries(tr, fn, entries)
    options = dict(common)
    options["call_symbols"] = calls
    options["tail_symbols"] = {t: "entry_%08x" % t for targets in fir.exits.values()
                               for t in targets if t in tr.func_addrs}
    if contracts:
        options["call_contracts"] = {t: contracts[t] for t in calls if t in contracts}
    symbol = ("body_%08x" if fir.entries else "fn_%08x") % fn.addr
    try:
        source = emit(fir, symbol, lifter=lifter, **options)
    except RecursionError:
        raise SSAError("SSA graph recursion budget")
    lines = re.sub(r"\bentry_([0-9a-f]{8})\(c\);", r"CALL_FN(\1);", source).splitlines()
    if fir.entries:
        lines.append("void fn_%08x(X86 *c) { body_%08x(c, 0u); }" % (fn.addr, fn.addr))
        lines += ["void fn_%08x(X86 *c) { body_%08x(c, 0x%xu); }" % (e, fn.addr, e)
                  for e in fir.entries]
    return lines, fir.entries
