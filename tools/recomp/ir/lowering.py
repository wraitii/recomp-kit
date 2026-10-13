"""Finalize semantic boundary operations before selecting transport."""
from dataclasses import dataclass

from . import flag_region
from .body import FrozenMap
from .call_contracts import ARITH_FLAGS
from .call_values import field_value
from .operations import EXITS
from .ssa import SSAError, Value
from .invocation import Invocation
from .simplify import whole
from .transfer_plan import Publication


@dataclass(frozen=True)
class FlagRecipe:
    kind: str
    size: int
    op: Value | None
    a: Value | None
    b: Value | None
    carry: Value | None
    res: Value


@dataclass(frozen=True)
class PublishedFlags:
    record: FlagRecipe
    mask: int


@dataclass(frozen=True)
class Publish:
    values: tuple
    flags: PublishedFlags | None = None
    clear_flags: int = 0


@dataclass(frozen=True)
class Invoke:
    invocation: Invocation
    ecx_argument: object | None = None


@dataclass(frozen=True)
class IndirectInvoke:
    helper: str
    target: Value


@dataclass(frozen=True)
class SettleFlags:
    action: object


@dataclass(frozen=True)
class Guard:
    kind: str
    operand: object
    taken: tuple
    otherwise: tuple = ()


@dataclass(frozen=True)
class Reload:
    result: Value
    field: str
    lane: int


@dataclass(frozen=True)
class Preserve:
    result: Value
    field: str
    lane: int
    values: tuple
    whole: Value | None
    settle_flags: bool


@dataclass(frozen=True)
class Return:
    dispatch: str


@dataclass(frozen=True)
class EdgeCopies:
    target: int
    copies: tuple


@dataclass(frozen=True)
class BoundaryPlan:
    publications: FrozenMap
    events: FrozenMap
    edges: FrozenMap
    entry: tuple


@dataclass(frozen=True)
class CPUFieldTransport:
    boundaries: BoundaryPlan
    calls: FrozenMap


def select_cpu_fields(boundaries, calls):
    for values in calls.values():
        if not values.fallback_inputs or not values.fallback_outputs:
            raise SSAError("CPU-field transport requires full fallback bindings")
    return CPUFieldTransport(boundaries, calls)


def plan(prepared, invocations):
    s, groups = prepared.ssa, prepared.state_groups
    lazy = prepared.options.request.policy.lazy_flags
    flag_fields = frozenset(ARITH_FLAGS)
    lane_fields = {key: (field, lane) for field, keys in groups for lane, key in enumerate(keys)}

    def publish(state, publication, direct=False, clear=False):
        flags = publication.flags
        covered = flags.covered if flags is not None else ()
        values = tuple(field_value(s, field, keys, state) for field, keys in groups
                       if any(key in publication.required for key in keys)
                       and not any(key in covered for key in keys))
        clear = int(clear) + int(lazy and not direct and flags is None and any(v.field in flag_fields for v in values))
        recipe = None
        if flags is not None:
            record = flags.record
            operands = tuple(s.resolve(v) if v is not None else None
                             for v in (record.op, record.a, record.b, record.carry, record.res))
            recipe = PublishedFlags(FlagRecipe(record.kind, record.size, *operands), flags.mask)
        result = Publish(values, recipe, clear)
        if flags is not None and flags.record.guarded:
            return Guard("hooks", None,
                         (publish(state, Publication(publication.required)),), (result,))
        return result

    publications, events = {}, {}
    for block in s.blocks.values():
        for event in block.ops:
            if event.id in prepared.transfers.publications:
                snapshot = block.exit if event.opc in EXITS else block.snapshots[event.id]
                call = prepared.transfers.calls.get(event.id)
                publication = prepared.transfers.publications[event.id]
                invocation = invocations.get(event.id)
                if invocation is not None and invocation.form == "private" and invocation.ecx_argument:
                    ecx_keys = set(dict(groups)["ECX"])
                    publication = Publication(tuple(key for key in publication.required if key not in ecx_keys),
                                              publication.flags)
                clear = lazy and call is not None and bool(call.skipped & flag_fields) and publication.flags is None
                publications[event.id] = publish(snapshot, publication, clear=clear)
            if event.opc == "CALL":
                invocation, call = invocations[event.id], prepared.transfers.calls[event.id]
                publication = publications[event.id]
                primary = (publication,)
                if invocation.form == "private":
                    fallback = publish(block.snapshots[event.id], call.fallback)
                    ecx = (field_value(s, "ECX", dict(groups)["ECX"], block.snapshots[event.id])
                           if invocation.ecx_argument else None)
                    body = (Guard("eligible", invocation.preparation,
                                  (*primary, Invoke(invocation, ecx)),
                                  (fallback, Invoke(invocation.fallback))),)
                else:
                    if call.guard:
                        guarded = publish(block.snapshots[event.id],
                                          Publication(call.guard), direct=True,
                                          clear=lazy and any(key in prepared.flag_keys for key in prepared.kept_calls.get(event.id, ())))
                        primary += (Guard("hooks", None, (guarded,)),)
                    body = (*primary, Invoke(invocation))
                events[event.id] = body + ((SettleFlags(prepared.cc_calls.get(block.index)),) if lazy else ())
            elif event.opc in ("CALL_RELOAD", "CALL_KEEP"):
                field, lane = lane_fields[event.data]
                if event.opc == "CALL_RELOAD":
                    events[event.id] = (Reload(event, field, lane),)
                else:
                    values = event.args[1:]
                    source = whole(s, values) if len(values) > 1 else None
                    events[event.id] = (Preserve(event, field, lane, tuple(s.resolve(v) for v in values),
                                                 s.resolve(source) if source is not None else None,
                                                 lazy and field in flag_fields),)
            elif event.opc == "CALLIND":
                events[event.id] = (publications[event.id], IndirectInvoke(event.data, s.resolve(event.args[1])))
                if lazy:
                    events[event.id] += (SettleFlags(prepared.cc_calls.get(block.index)),)
            elif event.opc == "RETURN":
                body = ()
                if prepared.options.return_fields is not None:
                    popped = field_value(s, "EIP", dict(groups)["EIP"], block.exit)
                    fallback = publish(block.exit, prepared.transfers.returns[event.id].fallback)
                    body = (Guard("return_mismatch", popped, (fallback, Return("always"))),)
                dispatch = ("none" if prepared.options.return_fields is not None else
                            "checked" if prepared.options.checked_return else "always")
                events[event.id] = (*body, publications[event.id], Return(dispatch))
            elif event.opc == "TAIL":
                events[event.id] = (publications[event.id], Invoke(invocations[event.id]), Return("none"))
    predecessors = {target: set() for target in s.blocks}
    for source in s.blocks:
        for target in prepared.ir.succ[source]:
            predecessors[target].add(source)
    for target in (s.entry, *s.alternates):
        predecessors[target].add(-1)
    edges = {}
    for target, block in s.blocks.items():
        phis = [v for v in block.phis if v.size and s.resolve(v) is v]
        for source in predecessors[target]:
            copies = tuple((phi, s.resolve(phi.args[phi.data[1].index(source)])) for phi in phis)
            edges[source, target] = EdgeCopies(target, copies)
    return BoundaryPlan(FrozenMap.of(publications), FrozenMap.of(events), FrozenMap.of(edges),
                        (SettleFlags(prepared.cc_entry),) if lazy else ())
