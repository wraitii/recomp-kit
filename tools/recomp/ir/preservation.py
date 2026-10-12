"""Forward proven register saves without removing guest stores or stack motion.

Every incoming memory path must reach the same PUSH value. Unknown aliases,
calls, opaque effects and cyclic proofs stop forwarding. Aligned stack locals
use modular address intervals to prove disjointness. A surviving store to the
same four bytes establishes that the removed restore load cannot fault.
"""
from .ssa import MEMORY
from .simplify import canonicalize


def forward(s):
    owners = {v.id: b for b in s.blocks.values() for v in b.ops}
    shapes, bounds = {}, {}

    def address(value):
        value = s.resolve(value)
        if value.id in shapes:
            return shapes[value.id]
        result = (value.id, 0)
        if value.opc in ("INT_ADD", "INT_SUB") and value.size == 4:
            a, b = map(s.resolve, value.args)
            if b.opc == "CONST":
                root, offset = address(a)
                result = (root, (offset + (b.data if value.opc == "INT_ADD" else -b.data)) & 0xffffffff)
            elif a.opc == "CONST" and value.opc == "INT_ADD":
                root, offset = address(b)
                result = (root, (offset + a.data) & 0xffffffff)
        shapes[value.id] = result
        return result

    def interval(value):
        value = s.resolve(value)
        if value.id in bounds:
            return bounds[value.id]
        root, offset = address(value)
        base = s.values[root]
        result = (root, offset, offset)
        if base.opc == "INT_AND" and base.size == 4:
            a, b = map(s.resolve, base.args)
            if a.opc == "CONST":
                a, b = b, a
            if b.opc == "CONST":
                cleared = (~b.data) & 0xffffffff
                if cleared < 256 and not cleared & (cleared + 1):
                    origin, low, high = interval(a)
                    result = (origin, low + offset - cleared, high + offset)
        bounds[value.id] = result
        return result

    def disjoint(a, size_a, b, size_b):
        ra, la, ha = interval(a)
        rb, lb, hb = interval(b)
        if ra != rb:
            return False
        low, high = (la - hb) & 0xffffffff, (ha - lb) & 0xffffffff
        return size_b <= low <= high <= 0x100000000 - size_a

    changed = False
    for block in s.blocks.values():
        raw = block.insn.raw or b""
        if len(raw) == 1 and 0x58 <= raw[0] <= 0x5f:
            register = raw[0] - 0x58
        elif raw == b"\xc9":
            register = 5
        else:
            continue
        for load in block.ops:
            if load.opc != "LOAD" or load.size != 4:
                continue
            target = load.args[1]
            memo, active = {}, set()

            def saved(token):
                token = s.resolve(token)
                if token.id in memo:
                    return memo[token.id]
                if token.id in active or len(memo) + len(active) >= 512:
                    return None
                active.add(token.id)
                result = None
                if token.opc == "PHI" and token.data[0] == MEMORY:
                    values = [saved(arg) for arg in token.args]
                    if values and values[0] is not None and all(v is values[0] for v in values):
                        result = values[0]
                elif token.opc == "MEMORY":
                    effect = token.args[0]
                    if effect.opc == "STORE":
                        source, value = effect.args[1:]
                        origin = owners[effect.id].insn.raw or b""
                        if (address(source) == address(target) and value.size == 4
                                and origin == bytes([0x50 + register])):
                            result = s.resolve(value)
                        elif disjoint(target, 4, source, value.size):
                            result = saved(effect.args[0])
                    elif effect.opc in ("LOAD", "X87_REG"):
                        result = saved(effect.args[0])
                    elif effect.opc == "X87_MEM":
                        mnem = effect.data["mnem"]
                        writes = mnem.startswith(("FST", "FNST", "FIST"))
                        size = effect.data["operands"][0][1]
                        if not writes or disjoint(target, 4, effect.args[1], size):
                            result = saved(effect.args[0])
                active.remove(token.id)
                memo[token.id] = result
                return result

            value = saved(load.args[0])
            if value is not None:
                s.aliases[load.id] = value
                changed = True
    if changed:
        for block in s.blocks.values():
            for effect in block.ops:
                if (effect.opc == "MEMORY" and effect.args[0].opc == "LOAD"
                        and effect.args[0].id in s.aliases):
                    s.aliases[effect.id] = effect.args[0].args[0]
            block.ops = [effect for effect in block.ops
                         if effect.opc != "LOAD" or effect.id not in s.aliases]
        canonicalize(s)
    return changed
