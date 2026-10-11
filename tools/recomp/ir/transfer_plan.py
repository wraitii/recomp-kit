from dataclasses import dataclass

from .simplify import state_roots, EFFECTS, EXITS
from .call_contracts import continuation_outputs
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
        return tuple(key for key in self.required if self.flags is None or key not in self.flags.covered)


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
    outputs: frozenset | None = None
    output_mask: int | None = None
    mode_proven: bool = False


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


def calls(s, publications, known_before, contracts, kept, lane_field, never_skip, private, flag_keys):
    result = {}
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc != "CALL":
                continue
            required = publications[v.id]
            contract = contracts.get(v.data)
            skipped = frozenset(field for field in lane_field.values()
                                if contract is not None and field not in never_skip
                                and field not in contract.reads and field in contract.kills)
            guard = tuple(key for key in required if lane_field.get(key) in skipped)
            guard += tuple(key for key in kept.get(v.id, ()) if key in s.inputs)
            fallback = ()
            if v.data in private:
                known = set(known_before[v.id]) - flag_keys
                fallback = tuple(key for key in b.snapshots[v.id]
                                 if key != MEMORY and key in s.inputs and key not in known)
            result[v.id] = CallPlan(Publication(tuple(key for key in required if lane_field.get(key) not in skipped)),
                                    guard, skipped, Publication(fallback))
    return result


def publication_roots(s, state, required, groups, flags=None):
    keys = tuple(key for key in required if flags is None or key not in flags.covered)
    return state_roots(s, state, keys, groups) + (list(flags.roots) if flags else [])


def build(s, groups, facts, contracts, kept, lane_field, never_skip,
          private, return_fields, flag_keys, first_keys, defines, bits, lazy_flags, eager_events):
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
                publication = publications[v.id]
                publication.required = tuple(key for key in publication.required
                                             if key not in lane_field or lane_field[key] in required)
                returns[v.id] = ReturnPlan(publication, Publication(fallback))
    call_plans = calls(s, {key: value.required for key, value in publications.items()},
                       facts.known_before, contracts, kept, lane_field, never_skip, private, flag_keys)
    roots = []
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc == "CALL":
                call = call_plans[v.id]
                publications[v.id] = call.publication
                if v.data not in private:
                    roots.extend(state_roots(s, b.snapshots[v.id], call.guard, groups))
            if v.opc not in EFFECTS and v.opc not in EXITS:
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
    if private:
        outputs = continuation_outputs(s, result.live_publications, groups, lane_field, roots,
                                       {target: contract.reads for target, contract in contracts.items()})
        for key, fields in outputs.items():
            call_plans[key].outputs = fields | never_skip
    return result
