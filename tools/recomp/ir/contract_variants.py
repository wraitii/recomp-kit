"""One shared return layout per specialized callee."""
from .call_contracts import FIELDS


def output_mask(fields):
    return sum(1 << i for i, field in enumerate(FIELDS) if field in fields)


def shared_masks(requests):
    masks = {}
    for variants in requests:
        for target, mask in variants:
            masks[target] = masks.get(target, 0) | mask
    return masks


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
