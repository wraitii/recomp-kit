"""Production body preparation and wrappers for the existing dispatch ABI."""
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .body import BodyRequest, BodyVariant, EntryForm, ReturnForm, EmissionPolicy, FrozenMap
from . import c_boundaries, c_symbols, flag_region
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
    ecx_private_targets: frozenset = frozenset()

    def __post_init__(self):
        for name in ("contracts", "mode_sites"):
            object.__setattr__(self, name, FrozenMap.of(getattr(self, name)))
        for name in ("known", "checked_returns", "contract_calls", "ecx_private_targets"):
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
                       ecx_private_targets=request.ecx_private_targets,
                       mode_sites=request.mode_sites, entry_verify=request.entry_verify,
                       report_calls=request.report_calls)
    try:
        return prepare_body(fir, body, lifter=lifter, normalized=artifacts.normalized(fir, lifter, tails))
    except RecursionError:
        raise SSAError("SSA graph recursion budget")


def _entry_action(prepared):
    if not prepared.options.request.policy.lazy_flags:
        return []
    return c_boundaries.cc_action_lines(prepared.cc_entry)


def _wrapped(action, call):
    prefix = " ".join(action)
    return "%s %s" % (prefix, call) if prefix else call


def emit_ssa_prepared(prepared):
    source = emit_prepared(prepared, external_entry=prepared.options.checked_return)
    addr, entries = prepared.ir.addr, prepared.ir.entries
    checked, symbol = prepared.options.checked_return, prepared.options.symbol
    action = _entry_action(prepared) if checked else []
    lines = source.splitlines()
    ordinary = c_symbols.entry_signature(BodyVariant(), c_symbols.symbol("fn", addr))
    if checked:
        lines.append("%s { %s; }" % (ordinary, _wrapped(action, "%s(c, 0u, 0)" % symbol)))
        entry = c_symbols.entry_signature(prepared.options.request.variant, c_symbols.symbol("fn_checked", addr))
        lines.append("%s { %s; }" % (entry, _wrapped(action, "%s(c, expected, 1)" % symbol)))
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


def prepare_private(tr, lifter, fn, policy, layout, known_mode=False, artifacts=None, finalize=False):
    artifacts = artifacts if artifacts is not None else ArtifactStore()
    variant = BodyVariant(EntryForm.PRIVATE, ReturnForm.PRIVATE,
                          Guarded(CW_FAST, "entry") if known_mode else Dynamic())
    fir = artifacts.lifted(tr, lifter, fn)
    request = BodyRequest(c_symbols.body_symbol(variant, fn.addr, layout), variant, policy, layout=layout)
    body = prepare_body(fir, request, lifter=lifter, normalized=artifacts.normalized(fir, lifter))
    if finalize:
        body.finalize({})
    complete = layout.outputs == tuple(FIELDS) and not layout.ecx_argument
    if not known_mode and not complete and not layout.ecx_argument:
        complete = private_contract(tr, lifter, fn, artifacts).writes <= set(layout.outputs)
    return PrivatePlan(fn.addr, layout, variant,
                       None if not known_mode and complete else body)


def emit_private_prepared(plan):
    name = c_symbols.symbol("fn_contract", plan.addr, plan.layout.output_mask)
    signature = c_symbols.entry_signature(plan.variant, name, plan.layout.ecx_argument)
    if plan.body is None:
        return ["%s { %s(c, expected); }" % (signature, c_symbols.symbol("fn_checked", plan.addr))]
    if plan.body.options.request.layout != plan.layout or plan.body.options.request.variant != plan.variant:
        raise SSAError("private body disagrees with its entry layout or variant")
    lines = emit_prepared(plan.body, external_entry=True).splitlines()
    body_arguments = "c, ecx_, expected" if plan.layout.ecx_argument else "c, expected"
    if (plan.layout.ecx_argument and plan.body.options.request.policy.lazy_flags
            and plan.body.cc_entry == flag_region.SETTLE):
        body_prototype = lines[0].removesuffix(" {")
        lines[:0] = ["#if defined(__GNUC__) || defined(__clang__)",
                     "%s __attribute__((noinline));" % body_prototype,
                     "#endif"]
        slow_name = name + "_settle"
        slow_signature = c_symbols.entry_signature(plan.variant, slow_name, True)
        lines.append("RECOMP_COLD %s { x86_cc_settle(c); %s(%s); }" % (
            slow_signature, plan.body.options.symbol, body_arguments))
        lines.append("%s { if (RECOMP_UNLIKELY(c->cc_op != X86_CC_NONE)) %s(%s); else %s(%s); }" % (
            signature, slow_name, body_arguments, plan.body.options.symbol, body_arguments))
        return lines
    wrapped = _wrapped(_entry_action(plan.body), "%s(%s)" % (plan.body.options.symbol, body_arguments))
    lines.append("%s { %s; }" % (signature, wrapped))
    return lines


def private_contract(tr, lifter, fn, artifacts=None):
    artifacts = artifacts if artifacts is not None else ArtifactStore()
    return summarize(prepare(artifacts.normalized(artifacts.lifted(tr, lifter, fn), lifter)), lambda _: None)
