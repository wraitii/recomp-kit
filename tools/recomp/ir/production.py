"""Production body preparation and wrappers for the existing dispatch ABI."""
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .body import BodyRequest, BodyVariant, EntryForm, ReturnForm, EmissionPolicy, FrozenMap
from . import c_symbols
from .prepare import prepare as prepare_body
from .emit_c import emit_prepared
from .invocation import EntryBinding
from .ssa import SSAError
from .call_contracts import FIELDS, prepare, summarize
from .contract_variants import PrivateLayout
from .x87_cw import CW_FAST, Dynamic, Guarded

MAX_INSTRUCTIONS = 16384


def external_entries(tr, fn, entries):
    entries = sorted(e for e in entries if e in fn.index)
    internal = tr.internal_entries.get(fn.addr, set())
    return entries if any(e not in internal for e in entries) else []


def exclusion(fn):
    if fn.pushed_continuations or fn.return_jumps:
        return "guest continuations"
    if len(fn.insns) > MAX_INSTRUCTIONS:
        return "SSA instruction budget"
    return None


@dataclass(frozen=True)
class ProductionRequest:
    policy: EmissionPolicy
    known: frozenset
    contracts: FrozenMap = FrozenMap()
    checked_returns: frozenset = frozenset()
    contract_calls: frozenset = frozenset()
    report_calls: bool = False
    mode_sites: FrozenMap = FrozenMap()
    entry_verify: object | None = None

    def __post_init__(self):
        for name in ("contracts", "mode_sites"):
            object.__setattr__(self, name, FrozenMap.of(getattr(self, name)))
        for name in ("known", "checked_returns", "contract_calls"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))


def prepare_ssa(tr, lifter, fn, entries, request, artifacts=None):
    artifacts = artifacts if artifacts is not None else ArtifactStore()
    calls = {}
    for ins in fn.insns:
        if ins.mnem == "CALL":
            target = tr.branch_target(ins)
            if target in request.known:
                calls[target] = EntryBinding(target, target in request.checked_returns)
    fir = artifacts.lifted(tr, lifter, fn)
    fir.entries = tuple(external_entries(tr, fn, entries))
    checked = fn.addr in request.checked_returns
    variant = BodyVariant(EntryForm.CHECKED if checked else EntryForm.MULTIPLE if fir.entries else EntryForm.ORDINARY,
                          ReturnForm.CHECKED if checked else ReturnForm.ORDINARY)
    tails = {t: EntryBinding(t, False) for targets in fir.exits.values() for t in targets if t in tr.func_addrs}
    body = BodyRequest(c_symbols.body_symbol(variant, fn.addr), variant, request.policy,
                       call_symbols=calls, tail_symbols=tails,
                       call_contracts={t: request.contracts[t] for t in calls if t in request.contracts},
                       checked_calls=set(calls) & request.checked_returns,
                       contract_calls=set(calls) & request.contract_calls if not fir.entries and not fir.seh else (),
                       mode_sites=request.mode_sites, entry_verify=request.entry_verify,
                       report_calls=request.report_calls)
    try:
        return prepare_body(fir, body, lifter=lifter, normalized=artifacts.normalized(fir, lifter, tails))
    except RecursionError:
        raise SSAError("SSA graph recursion budget")


def emit_ssa_prepared(prepared, emitter=emit_prepared):
    source = emitter(prepared)
    addr, entries = prepared.ir.addr, prepared.ir.entries
    checked, symbol = prepared.options.checked_return, prepared.options.symbol
    lines = source.splitlines()
    ordinary = c_symbols.entry_signature(BodyVariant(), c_symbols.symbol("fn", addr))
    if checked:
        lines.append("%s { %s(c, 0u, 0); }" % (ordinary, symbol))
        entry = c_symbols.entry_signature(prepared.options.request.variant, c_symbols.symbol("fn_checked", addr))
        lines.append("%s { %s(c, expected, 1); }" % (entry, symbol))
    if entries:
        lines.append("%s { %s(c, 0u); }" % (ordinary, symbol))
        lines += ["%s { %s(c, 0x%xu); }" % (
            c_symbols.entry_signature(BodyVariant(), c_symbols.symbol("fn", e)), symbol, e) for e in entries]
    return lines, entries, prepared.bound_requests, prepared.decision_rows()


@dataclass(frozen=True)
class PrivatePlan:
    addr: int
    layout: PrivateLayout
    variant: BodyVariant
    body: object | None


def prepare_private(tr, lifter, fn, policy, layout, known_mode=False, artifacts=None):
    artifacts = artifacts if artifacts is not None else ArtifactStore()
    variant = BodyVariant(EntryForm.PRIVATE, ReturnForm.PRIVATE,
                          Guarded(CW_FAST, "entry") if known_mode else Dynamic())
    fir = artifacts.lifted(tr, lifter, fn)
    request = BodyRequest(c_symbols.body_symbol(variant, fn.addr, layout), variant, policy, layout=layout)
    body = prepare_body(fir, request, lifter=lifter, normalized=artifacts.normalized(fir, lifter))
    body.finalize({})
    return PrivatePlan(fn.addr, layout, variant,
                       None if not policy.private_value_transport and not known_mode and layout.outputs == tuple(FIELDS) else body)


def emit_private_prepared(plan, emitter=emit_prepared):
    name = c_symbols.symbol("fn_contract", plan.addr, plan.layout.output_mask)
    signature = c_symbols.entry_signature(plan.variant, name)
    if plan.body is None:
        return ["%s { %s(c, expected); }" % (signature, c_symbols.symbol("fn_checked", plan.addr))]
    if plan.body.options.request.layout != plan.layout or plan.body.options.request.variant != plan.variant:
        raise SSAError("private body disagrees with its entry layout or variant")
    lines = emitter(plan.body).splitlines()
    if plan.body.options.request.policy.private_value_transport:
        name = c_symbols.symbol("fn_values", plan.addr, plan.layout.output_mask)
        signature = c_symbols.value_signature(name, plan.layout, plan.addr)
        lines.append("%s { %s(%s); }" % (signature, plan.body.options.symbol,
                                         c_symbols.value_arguments(plan.layout)))
        return lines
    lines.append("%s { %s(c, expected); }" % (signature, plan.body.options.symbol))
    return lines


def private_contract(tr, lifter, fn, artifacts=None):
    artifacts = artifacts if artifacts is not None else ArtifactStore()
    return summarize(prepare(artifacts.normalized(artifacts.lifted(tr, lifter, fn), lifter)), lambda _: None)
