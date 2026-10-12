"""One shared ordered state layout per specialized callee."""
from dataclasses import dataclass

from .body import FrozenMap
from .call_contracts import FIELDS


def output_mask(fields):
    return sum(1 << i for i, field in enumerate(FIELDS) if field in fields)


@dataclass(frozen=True)
class PrivateLayout:
    inputs: tuple
    outputs: tuple
    resident_opaque: tuple = ("EIP", "FS_OFFSET", "DF", "x87", "runtime")

    def __post_init__(self):
        for names in (self.inputs, self.outputs):
            if not isinstance(names, tuple) or names != tuple(f for f in FIELDS if f in names):
                raise ValueError("private fields must be unique and in guest contract order")
        if not isinstance(self.resident_opaque, tuple) or len(set(self.resident_opaque)) != len(self.resident_opaque):
            raise ValueError("resident opaque state must be an ordered unique tuple")

    @classmethod
    def from_mask(cls, mask, inputs=FIELDS):
        if mask < 0 or mask & ~((1 << len(FIELDS)) - 1):
            raise ValueError("invalid private output mask")
        return cls(tuple(f for f in FIELDS if f in inputs),
                   tuple(f for i, f in enumerate(FIELDS) if mask & (1 << i)))

    @property
    def output_mask(self):
        return output_mask(self.outputs)


@dataclass(frozen=True)
class ProvisionalBindings:
    invocations: FrozenMap


@dataclass(frozen=True)
class FinalBindings:
    invocations: FrozenMap
    layouts: FrozenMap
    values: FrozenMap


@dataclass(frozen=True)
class SpecializationPlan:
    layouts: FrozenMap
    caller_requests: FrozenMap

    @classmethod
    def build(cls, requests, contracts, value_transport=False):
        masks = {}
        for wanted in requests.values():
            for target, mask in wanted:
                masks[target] = masks.get(target, 0) | mask
        layouts = {}
        for addr, mask in masks.items():
            inputs = contracts[addr].reads | frozenset(("ESP", "EBP"))
            if value_transport:
                inputs |= frozenset(f for i, f in enumerate(FIELDS)
                                    if mask & (1 << i) and f not in contracts[addr].kills)
            layouts[addr] = PrivateLayout.from_mask(mask, inputs)
        callers = {addr: frozenset((target, layouts[target].output_mask) for target, _ in wanted)
                   for addr, wanted in requests.items() if wanted}
        return cls(FrozenMap.of(layouts), FrozenMap.of(callers))

    @property
    def private_masks(self):
        return {addr: {layout.output_mask} for addr, layout in self.layouts.items()}


def eligible_leaves(scanned, opaque):
    uses = {}
    for addr, row in scanned.items():
        if addr in opaque or not row.register_facts or not row.cw_facts:
            continue
        for target in row.calls:
            uses[target] = uses.get(target, 0) + 1
    return {addr for addr, row in scanned.items()
            if addr not in opaque and row.register_facts and row.cw_facts and row.cw_facts.returns
            and not row.cw_facts.calls and uses.get(addr, 0) >= 2
            and row.description.count <= 64 * uses[addr]}
