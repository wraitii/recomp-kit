"""Forward proven register saves without removing guest stores or stack motion.

Every incoming memory path must reach the same PUSH value. Unknown aliases,
calls and opaque effects stop forwarding. Aligned stack locals
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
        result = (value, 0)
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
        base = root
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
            pending, visited, saved_value = [load.args[0]], set(), None
            while pending:
                token = pending.pop()
                token = s.resolve(token)
                if token.id in visited:
                    continue
                if len(visited) >= 512:
                    saved_value = None
                    break
                visited.add(token.id)
                if token.opc == "PHI" and token.data[0] == MEMORY:
                    if not token.args:
                        saved_value = None
                        break
                    pending.extend(token.args)
                    continue
                elif token.opc == "MEMORY":
                    effect = token.args[0]
                    if effect.opc == "STORE":
                        source, value = effect.args[1:]
                        origin = owners[effect.id].insn.raw or b""
                        if (address(source) == address(target) and value.size == 4
                                and origin == bytes([0x50 + register])):
                            source_value = s.resolve(value)
                            if saved_value is not None and source_value is not saved_value:
                                saved_value = None
                                break
                            saved_value = source_value
                            continue
                        elif disjoint(target, 4, source, value.size):
                            pending.append(effect.args[0])
                            continue
                    elif effect.opc in ("LOAD", "X87_REG"):
                        pending.append(effect.args[0])
                        continue
                    elif effect.opc == "X87_MEM":
                        mnem = effect.data["mnem"]
                        writes = mnem.startswith(("FST", "FNST", "FIST"))
                        size = effect.data["operands"][0][1]
                        if not writes or disjoint(target, 4, effect.args[1], size):
                            pending.append(effect.args[0])
                            continue
                saved_value = None
                break
            if saved_value is not None:
                s.aliases[load.id] = saved_value
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
