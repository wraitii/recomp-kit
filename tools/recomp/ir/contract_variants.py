"""Bounded shared state layouts for specialized callees."""
from dataclasses import dataclass

from .body import FrozenMap
from .call_contracts import FIELDS, ARITH_FLAGS


def output_mask(fields):
    return sum(1 << i for i, field in enumerate(FIELDS) if field in fields)


@dataclass(frozen=True)
class PrivateLayout:
    inputs: tuple
    outputs: tuple
    resident_opaque: tuple = ("EIP", "FS_OFFSET", "DF", "x87", "runtime")
    ecx_argument: bool = False

    def __post_init__(self):
        for names in (self.inputs, self.outputs):
            if not isinstance(names, tuple) or names != tuple(f for f in FIELDS if f in names):
                raise ValueError("private fields must be unique and in guest contract order")
        if not isinstance(self.resident_opaque, tuple) or len(set(self.resident_opaque)) != len(self.resident_opaque):
            raise ValueError("resident opaque state must be an ordered unique tuple")
        if type(self.ecx_argument) is not bool or (self.ecx_argument and "ECX" in self.inputs):
            raise ValueError("private ECX input must use the explicit argument")

    @classmethod
    def from_mask(cls, mask, inputs=FIELDS, ecx_argument=False):
        if mask < 0 or mask & ~((1 << len(FIELDS)) - 1):
            raise ValueError("invalid private output mask")
        return cls(tuple(f for f in FIELDS if f in inputs and not (ecx_argument and f == "ECX")),
                   tuple(f for i, f in enumerate(FIELDS) if mask & (1 << i)),
                   ecx_argument=ecx_argument)

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
    def build(cls, requests, contracts, ecx_targets=()):
        ecx_targets = frozenset(ecx_targets)
        masks, flagless, users = {}, {}, {}
        flags = output_mask(ARITH_FLAGS)
        for wanted in requests.values():
            flagless_targets = set()
            for target, mask in wanted:
                masks[target] = masks.get(target, 0) | mask
                if not mask & flags:
                    flagless[target] = flagless.get(target, 0) | mask
                    flagless_targets.add(target)
            for target in flagless_targets:
                users[target] = users.get(target, 0) + 1
        layouts = {addr: (PrivateLayout.from_mask(mask, contracts[addr].reads | frozenset(("ESP", "EBP")),
                                                addr in ecx_targets),)
                   for addr, mask in masks.items()}
        for addr, mask in flagless.items():
            if users[addr] >= 16 and masks[addr] & flags:
                layouts[addr] += (PrivateLayout.from_mask(mask, layouts[addr][0].inputs,
                                                          addr in ecx_targets),)
        callers = {addr: frozenset((target, select_layout(layouts, target, mask).output_mask)
                                  for target, mask in wanted)
                   for addr, wanted in requests.items() if wanted}
        return cls(FrozenMap.of(layouts), FrozenMap.of(callers))

    @property
    def private_masks(self):
        return {addr: {layout.output_mask for layout in variants}
                for addr, variants in self.layouts.items()}


def select_layout(layouts, target, mask):
    variants = layouts[target]
    if isinstance(variants, PrivateLayout):
        variants = (variants,)
    compatible = [layout for layout in variants if not mask & ~layout.output_mask]
    if not compatible:
        raise ValueError("private outputs do not cover the call continuation")
    return min(compatible, key=lambda layout: (layout.output_mask.bit_count(), layout.output_mask))


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
