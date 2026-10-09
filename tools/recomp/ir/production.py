"""Production SSA bodies inside the existing dispatch ABI.

Calls publish/reload complete tracked state and use the production entry thunk,
including replacement, hook, profiling and frame-watch policy. Indirect calls
are admitted only through the explicit `recomp_call` opt-in; complex host-frame
ownership and unsupported shapes keep the decoded body whole.
"""
import re

from .cfg import function_ir
from .emit_c import emit
from .lift import LiftError
from .ssa import SSAError

# Bound Python graph/phi construction before it can consume excessive resources.
# This is a code-generation budget, never a guest execution limit.
MAX_INSTRUCTIONS = 16384


def seh_hooks(tr, fn):
    """Does the decoded emitter attach SEH runtime hooks to this body?

    Mirrors translate.py: `recomp_seh_frame_enter`/`_leave`/`_orphan`/`_adopt`
    are emitted for establishing sites, escaping returns, helper calls and
    POP-form chain restores. A MOV-form chain restore emits a hook only in a
    body that also has establishing sites, so with none it is a plain FS:[0]
    store with no runtime effect, and the SSA path models it as one.
    """
    if fn.seh_sites or fn.seh_escapes or fn.addr in tr.seh_helpers:
        return True
    # Calls to helpers (`recomp_seh_frame_adopt`) are rejected later as unbound
    # callees, since `apply` forbids every helper as a direct-call target.
    return any(fn.insns[i].mnem == "POP" for i in fn.seh_restores)


def external_entries(tr, fn, entries):
    """Entries other code can reach, not only switch cases of this body.

    With any, the SSA body takes every entry and the wrappers follow; with
    none, `apply` keeps the decoded body for the switch-case wrappers."""
    entries = sorted(e for e in entries if e in fn.index)
    internal = tr.internal_entries.get(fn.addr, set())
    return entries if any(e not in internal for e in entries) else []


def exclusion(tr, fn, entries, policies):
    """Reject production contracts the corpus emitter does not implement."""
    if fn.addr in policies.get("intrinsic_bodies", {}):
        return "runtime intrinsic"
    if fn.pushed_continuations or fn.return_jumps:
        return "guest continuations"
    if len(fn.insns) > MAX_INSTRUCTIONS:
        return "SSA instruction budget"
    rewritten = (set(policies.get("instruction_patches", ()))
                 | set(policies.get("operand_redirects", ()))
                 | set(policies.get("volatile_reads", ())))
    if fn.addrs & rewritten:
        return "audited instruction rewrite"
    return None


def options(settings, policies):
    relaxed = settings.get("fault_state", "relaxed") == "relaxed"
    return {
        "x87_scalar_strict": not relaxed,
        "local_state": relaxed,
        "msvc_convention": settings.get("msvc_x87_convention", True),
        "lazy_nan": True,
        "lazy_flags": relaxed,
        "resumable_stacks": policies.get("resumable_stacks", False),
        "indirect_call_symbol": "recomp_call",
        "x87_cw_clone": settings.get("x87_cw_clone", True),
    }


def emit_ssa(tr, lifter, fn, entries, common, known, forbidden, contracts, facts):
    """The SSA body of `fn` as C lines, with its wrappers when other code can enter it.

    Raises SSAError or LiftError for a shape that stays decoded."""
    calls = {}
    for ins in fn.insns:
        if ins.mnem == "CALL":
            target = tr.branch_target(ins)
            if target in known and target not in forbidden:
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
        source = emit(fir, symbol, facts=facts, lifter=lifter, **options)
    except RecursionError:
        raise SSAError("SSA graph recursion budget")
    lines = re.sub(r"\bentry_([0-9a-f]{8})\(c\);", r"CALL_FN(\1);", source).splitlines()
    if fir.entries:
        lines.append("void fn_%08x(X86 *c) { body_%08x(c, 0u); }" % (fn.addr, fn.addr))
        lines += ["void fn_%08x(X86 *c) { body_%08x(c, 0x%xu); }" % (e, fn.addr, e)
                  for e in fir.entries]
    return lines, fir.entries
