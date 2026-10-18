"""Bounded shared state layouts for specialized callees."""
from dataclasses import dataclass

from .body import FrozenMap
from .call_contracts import FIELDS, ARITH_FLAGS, closed
from .call_contracts import GPRS

INPUT_ARGUMENT_REGISTERS = tuple(field for field in GPRS if field not in ("ESP", "EBP"))


def output_mask(fields):
    return sum(1 << i for i, field in enumerate(FIELDS) if field in fields)


@dataclass(frozen=True)
class PrivateLayout:
    inputs: tuple
    outputs: tuple
    resident_opaque: tuple = ("EIP", "FS_OFFSET", "DF", "x87", "runtime")
    input_argument: str | None = None

    def __post_init__(self):
        for names in (self.inputs, self.outputs):
            if not isinstance(names, tuple) or names != tuple(f for f in FIELDS if f in names):
                raise ValueError("private fields must be unique and in guest contract order")
        if not isinstance(self.resident_opaque, tuple) or len(set(self.resident_opaque)) != len(self.resident_opaque):
            raise ValueError("resident opaque state must be an ordered unique tuple")
        if self.input_argument is not None and (self.input_argument not in INPUT_ARGUMENT_REGISTERS
                                                or self.input_argument in self.inputs):
            raise ValueError("private register input must use the explicit argument")

    @classmethod
    def from_mask(cls, mask, inputs=FIELDS, input_argument=None):
        if mask < 0 or mask & ~((1 << len(FIELDS)) - 1):
            raise ValueError("invalid private output mask")
        return cls(tuple(f for f in FIELDS if f in inputs and f != input_argument),
                   tuple(f for i, f in enumerate(FIELDS) if mask & (1 << i)),
                   input_argument=input_argument)

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
    def build(cls, requests, contracts, input_arguments=()):
        input_arguments = dict(input_arguments)
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
                                                input_arguments.get(addr)),)
                   for addr, mask in masks.items()}
        for addr, mask in flagless.items():
            if users[addr] >= 16 and masks[addr] & flags:
                layouts[addr] += (PrivateLayout.from_mask(mask, layouts[addr][0].inputs,
                                                          input_arguments.get(addr)),)
        callers = {addr: frozenset((target, select_layout(layouts, target, mask).output_mask)
                                  for target, mask in wanted)
                   for addr, wanted in requests.items() if wanted}
        return cls(FrozenMap.of(layouts), FrozenMap.of(callers))

    @property
    def private_masks(self):
        return {addr: {layout.output_mask for layout in variants}
                for addr, variants in self.layouts.items()}


def select_input_arguments(contracts, register_uses, input_sites, instruction_counts):
    sites = [site for rows in input_sites.values() for site in rows]
    chosen, report = {}, {}
    for target, contract in sorted(contracts.items()):
        uses = register_uses.get(target, {})
        rows = [site for site in sites if site[0] == target]
        evidence = {}
        if instruction_counts.get(target, 0) > 64:
            report["%08x" % target] = {"excluded": "body exceeds 64 instructions",
                                       "instructions": instruction_counts[target]}
            continue
        ranked = []
        for order, field in enumerate(INPUT_ARGUMENT_REGISTERS):
            if field not in contract.reads or field not in uses:
                continue
            input_sites_count = sum(field in site[1] for site in rows)
            stores = sum(field in site[2] for site in rows)
            preserved = sum(field in site[3] for site in rows)
            instructions = tuple(uses[field])
            score = (stores, preserved, input_sites_count, len(instructions),
                     -min(instructions), -order)
            evidence[field] = {"input_sites": input_sites_count, "saved_stores": stores,
                               "preserved_continuations": preserved,
                               "substantive_reads": len(instructions),
                               "first_read_index": min(instructions)}
            if input_sites_count:
                ranked.append((score, field))
        if ranked:
            score, field = max(ranked)
            chosen[target] = field
            evidence["selected"] = field
            evidence["score"] = list(score)
        else:
            evidence["excluded"] = "no substantive register input at private call sites"
        report["%08x" % target] = evidence
    return chosen, report

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
        if addr in opaque or not closed(row.register_facts) or not row.cw_facts:
            continue
        for target in row.calls:
            uses[target] = uses.get(target, 0) + 1
    return {addr for addr, row in scanned.items()
            if addr not in opaque and closed(row.register_facts) and row.cw_facts and row.cw_facts.returns
            and not row.cw_facts.calls and uses.get(addr, 0) >= 2
            and row.description.count <= 64 * uses[addr]}
