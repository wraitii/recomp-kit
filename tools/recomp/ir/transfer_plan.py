from dataclasses import dataclass, replace

from .operations import EXITS, OBSERVES
from .simplify import state_roots
from .call_contracts import continuation_outputs, FIELDS, ARITH_FLAGS
from .call_state import CallState, OPAQUE_STATE
from .ssa import MEMORY


@dataclass
class FlagPublication:
    record: object
    mask: int
    covered: frozenset
    roots: tuple


@dataclass
class Publication:
    required: tuple
    flags: FlagPublication | None = None

    def roots(self, s, state, groups):
        return publication_roots(s, state, self.required, groups, self.flags)

    @property
    def live_keys(self):
        return tuple(key for key in self.required
                     if self.flags is None or self.flags.record.guarded or key not in self.flags.covered)


@dataclass
class ReturnPlan:
    publication: Publication
    fallback: Publication


@dataclass
class TransferPlan:
    publications: dict
    calls: dict
    returns: dict
    roots: list

    @property
    def live_publications(self):
        return {key: value.live_keys for key, value in self.publications.items()}


@dataclass
class CallPlan:
    publication: Publication
    guard: tuple
    skipped: frozenset
    fallback: Publication
    state: CallState | None = None
    settle_guard: bool = False

    @property
    def required_outputs(self):
        if self.state is None or self.state.required_outputs is None:
            return None
        return self.state.required_outputs.fields


def flag_publication(s, state, required, record, first_keys, defines, bits):
    if record is None:
        return None
    names = set(record.flags) if record.kind == "join" else set(defines[record.kind])
    mask = 0
    for name, key in first_keys.items():
        if name not in names or key not in required:
            continue
        producer, current = record.flags.get(name), state.get(key)
        if producer is None or current is None or s.resolve(current) is not s.resolve(producer):
            return None
        mask |= bits[name]
    if not mask:
        return None
    covered = frozenset(first_keys[name] for name, bit in bits.items() if mask & bit)
    roots = tuple(v for v in (record.op, record.a, record.b, record.carry, record.res)
                  if v is not None)
    return FlagPublication(record, mask, covered, roots)


def calls(s, publications, known_before, resident_before, contracts, kept, lane_field, never_skip,
          private, flag_keys, indirect_contracts):
    result = {}
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc not in ("CALL", "CALLIND"):
                continue
            required = publications[v.id]
            contract = (contracts.get(v.data) if v.opc == "CALL"
                        else indirect_contracts.get(v.data[1]))
            skipped = frozenset(field for field in lane_field.values()
                                if contract is not None and field not in never_skip
                                and field not in contract.reads and field in contract.kills)
            guard = tuple(key for key in required if lane_field.get(key) in skipped)
            guard += tuple(key for key in kept.get(v.id, ()) if key in s.inputs)
            resident_flags = frozenset(guard) & flag_keys & set(resident_before[v.id])
            settle_guard = bool(resident_flags) and not (skipped & set(ARITH_FLAGS)) and not (set(required) & flag_keys)
            if settle_guard:
                guard = tuple(key for key in guard if key not in resident_flags)
            fallback = ()
            if v.data in private:
                known = set(known_before[v.id]) - flag_keys
                fallback = tuple(key for key in b.snapshots[v.id]
                                 if key != MEMORY and key in s.inputs and key not in known)
            result[v.id] = CallPlan(Publication(tuple(key for key in required if lane_field.get(key) not in skipped)),
                                    guard, skipped, Publication(fallback), settle_guard=settle_guard)
    return result


def publication_roots(s, state, required, groups, flags=None):
    keys = tuple(key for key in required if flags is None or flags.record.guarded or key not in flags.covered)
    return state_roots(s, state, keys, groups) + (list(flags.roots) if flags else [])


def build(s, groups, facts, contracts, kept, lane_field, never_skip,
          private, return_fields, flag_keys, first_keys, defines, bits, lazy_flags, eager_events,
          explicit_input_argument=None, call_input_arguments=(), indirect_contracts=()):
    input_keys = tuple(key for key, field in lane_field.items() if field == explicit_input_argument)
    call_input_arguments = dict(call_input_arguments)
    publications = {key: Publication(tuple(value)) for key, value in facts.required.items()}
    returns = {}
    if return_fields is not None:
        required = set(return_fields) | set(never_skip)
        for b in s.blocks.values():
            for v in b.ops:
                if v.opc != "RETURN":
                    continue
                fallback = tuple(key for group in groups
                                 if any(s.resolve(b.exit[key]) is not s.resolve(s.inputs[key])
                                        for key in group if key in b.exit)
                                 for key in group if key in b.exit)
                if explicit_input_argument:
                    fallback = tuple(dict.fromkeys((*fallback, *input_keys)))
                publication = publications[v.id]
                publication.required = tuple(key for key in publication.required
                                             if key not in lane_field or lane_field[key] in required)
                if explicit_input_argument and explicit_input_argument in required:
                    publication.required = tuple(dict.fromkeys((*publication.required, *input_keys)))
                returns[v.id] = ReturnPlan(publication, Publication(fallback))
    call_plans = calls(s, {key: value.required for key, value in publications.items()},
                       facts.known_before, facts.resident_before, contracts, kept, lane_field,
                       never_skip, private, flag_keys, indirect_contracts)
    roots = []
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc in ("CALL", "CALLIND"):
                call = call_plans[v.id]
                publications[v.id] = call.publication
                if v.opc == "CALL":
                    input_argument = call_input_arguments.get(v.data)
                    if v.data in private and input_argument:
                        keys = tuple(key for key, field in lane_field.items() if field == input_argument)
                        roots.extend(state_roots(s, b.snapshots[v.id], keys, groups))
                if v.opc == "CALLIND" or v.data not in private:
                    roots.extend(state_roots(s, b.snapshots[v.id], call.guard, groups))
            if v.opc not in OBSERVES:
                continue
            state = b.exit if v.opc in EXITS else b.snapshots[v.id]
            record = b.cc_exit if v.opc in ("RETURN", "TAIL") else b.cc_snapshots.get(v.id)
            publication = publications[v.id]
            if lazy_flags and v.opc not in eager_events:
                publication.flags = flag_publication(s, state, publication.required, record,
                                                     first_keys, defines, bits)
                if publication.flags:
                    roots.extend(publication.flags.roots)
            fallback = None
            if v.opc == "CALL" and v.data in private:
                fallback = call_plans[v.id].fallback
            elif v.opc == "RETURN" and v.id in returns:
                fallback = returns[v.id].fallback
            if fallback is not None:
                if lazy_flags:
                    fallback.flags = flag_publication(s, state, fallback.required, record,
                                                      first_keys, defines, bits)
                roots.extend(fallback.roots(s, state, groups))
    result = TransferPlan(publications, call_plans, returns, roots)
    continuations = {}
    if private:
        continuations = continuation_outputs(
            s, result.live_publications, groups, lane_field, roots,
            {target: contract.reads for target, contract in contracts.items()}, contracts)
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc != "CALL":
                continue
            call = call_plans[v.id]
            continuation = continuations.get(v.id)
            if continuation is not None and never_skip:
                continuation = replace(continuation, fields=continuation.fields | never_skip)
            contract = contracts.get(v.data)
            call.state = CallState(
                inputs=frozenset(contract.reads if contract is not None else FIELDS) | never_skip,
                required_outputs=continuation if v.data in private else None,
                preserved=frozenset(lane_field[key] for key in kept.get(v.id, ()) if key in lane_field),
                resident=frozenset(lane_field[key] for key in facts.known_before.get(v.id, ())
                                   if key in lane_field),
                opaque=OPAQUE_STATE)
    return result
