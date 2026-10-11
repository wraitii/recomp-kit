"""Production SSA bodies inside the existing dispatch ABI.

Calls publish/reload complete tracked state and use the production entry thunk,
including replacement, hook, profiling and frame-watch policy. Indirect calls
are admitted only through the explicit `recomp_call` opt-in; complex host-frame
ownership and unsupported shapes keep the decoded body whole.
"""
from dataclasses import dataclass

from frontend import function_ir
from .emit_c import prepare as prepare_body, emit_prepared
from .normalize import codegen_ir
from .invocation import EntryBinding
from .lift import LiftError
from .ssa import SSAError
from .call_contracts import FIELDS, prepare, summarize

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


def prepare_ssa(tr, lifter, fn, entries, common, known, contracts, checked_returns=(), contract_calls=(), report_calls=False,
             contract_masks=None, x87_mode_sites=None, x87_entry_verify=None):
    """Prepare the SSA body of `fn`, including checked/private call obligations.

    Raises SSAError or LiftError for a shape that stays decoded."""
    calls = {}
    for ins in fn.insns:
        if ins.mnem == "CALL":
            target = tr.branch_target(ins)
            if target in known:
                calls[target] = EntryBinding(target, target in checked_returns)
    fir = function_ir(tr, lifter, fn)
    fir.entries = external_entries(tr, fn, entries)
    options = dict(common)
    options["call_symbols"] = calls
    options["tail_symbols"] = {t: EntryBinding(t, False) for targets in fir.exits.values()
                               for t in targets if t in tr.func_addrs}
    if contracts:
        options["call_contracts"] = {t: contracts[t] for t in calls if t in contracts}
    checked = fn.addr in checked_returns
    options["checked_return"] = checked
    options["checked_calls"] = set(calls) & set(checked_returns)
    options["contract_calls"] = set(calls) & set(contract_calls) if not fir.entries and not fir.seh else ()
    options["contract_masks"] = contract_masks
    options["x87_mode_sites"] = x87_mode_sites
    options["x87_entry_verify"] = x87_entry_verify
    symbol = ("body_checked_%08x" if checked else "body_%08x" if fir.entries else "fn_%08x") % fn.addr
    try:
        return prepare_body(fir, symbol, lifter=lifter,
                            call_decisions=[] if report_calls else None, **options)
    except RecursionError:
        raise SSAError("SSA graph recursion budget")


def emit_ssa_prepared(prepared):
    source = emit_prepared(prepared)
    addr, entries = prepared.ir.addr, prepared.ir.entries
    checked, symbol = prepared.options["checked_return"], prepared.options["symbol"]
    lines = source.splitlines()
    if checked:
        lines.append("void fn_%08x(X86 *c) { %s(c, 0u, 0); }" % (addr, symbol))
        lines.append("void fn_checked_%08x(X86 *c, uint32_t expected) { %s(c, expected, 1); }"
                     % (addr, symbol))
    if entries:
        lines.append("void fn_%08x(X86 *c) { body_%08x(c, 0u); }" % (addr, addr))
        lines += ["void fn_%08x(X86 *c) { body_%08x(c, 0x%xu); }" % (e, addr, e)
                  for e in entries]
    return lines, entries, prepared.bound_requests, prepared.decision_rows()


@dataclass
class PrivatePlan:
    addr: int
    mask: int
    body: object | None


def prepare_private(tr, lifter, fn, common, mask, known_mode=False):
    fields = frozenset(f for n, f in enumerate(FIELDS) if mask & (1 << n))
    body = prepare_body(function_ir(tr, lifter, fn), "body_contract_%08x_%04x" % (fn.addr, mask),
                        lifter=lifter, checked_return=True, return_fields=fields,
                        x87_known_mode=known_mode, **common)
    return PrivatePlan(fn.addr, mask, None if not known_mode and fields == frozenset(FIELDS) else body)


def emit_private_prepared(plan):
    if plan.body is None:
        return ["void fn_contract_%08x_%04x(X86 *c, uint32_t expected) { fn_checked_%08x(c, expected); }"
                % (plan.addr, plan.mask, plan.addr)]
    lines = emit_prepared(plan.body).splitlines()
    lines.append("void fn_contract_%08x_%04x(X86 *c, uint32_t expected) { %s(c, expected); }"
                 % (plan.addr, plan.mask, plan.body.options["symbol"]))
    return lines


def emit_ssa(*args, **options):
    return emit_ssa_prepared(prepare_ssa(*args, **options))


def emit_private(*args, **options):
    return emit_private_prepared(prepare_private(*args, **options))


def private_contract(tr, lifter, fn):
    return summarize(prepare(codegen_ir(function_ir(tr, lifter, fn), lifter)), lambda _: None)
