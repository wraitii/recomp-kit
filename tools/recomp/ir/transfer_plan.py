from dataclasses import dataclass

from .simplify import state_roots
from .ssa import MEMORY


@dataclass
class FlagPublication:
    record: object
    mask: int
    covered: frozenset
    roots: tuple


@dataclass
class CallPlan:
    publication: tuple
    guard: tuple
    skipped: frozenset
    fallback: tuple
    outputs: frozenset | None = None


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
            result[v.id] = CallPlan(tuple(key for key in required if lane_field.get(key) not in skipped),
                                    guard, skipped, fallback)
    return result


def publication_roots(s, state, required, groups, flags=None):
    keys = tuple(key for key in required if flags is None or key not in flags.covered)
    return state_roots(s, state, keys, groups) + (list(flags.roots) if flags else [])
