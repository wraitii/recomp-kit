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
