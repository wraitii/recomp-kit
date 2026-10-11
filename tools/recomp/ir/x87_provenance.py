"""Conservative linear-leaf provenance for x87 control-word save/restore.

The analysis follows only original byte-backed, lifted p-code. It tracks
control-word bits, register aliases and local stack bytes; all other values
remain unknown. It proves no environment/FX state restore or memory alias.
"""
from .lift import Lifter, BRANCHES
from .x87_cw import Fact, CW_MASK, writer_effect, instruction_mnemonic, instruction_effect

ENTRY_CW = "entry-cw"
STACK_ADDR = "stack-address"
ENV_RESTORES = frozenset(("FLDENV", "FRSTOR", "FXRSTOR", "XRSTOR", "XRSTORS"))
SETTERS = frozenset(("FINIT", "FNINIT", "FSAVE", "FNSAVE"))
ENV_MASKERS = frozenset(("FSTENV", "FNSTENV"))
_LIFTER = None


def _lifter():
    global _LIFTER
    if _LIFTER is None:
        _LIFTER = Lifter()
    return _LIFTER


def const_bits(value, width):
    value &= (1 << width) - 1
    return tuple((value >> bit) & 1 for bit in range(width))


def unknown(width):
    return (None,) * width


def affine_bits(delta, width):
    return tuple((STACK_ADDR, delta, bit) for bit in range(width))


def affine(bits):
    if len(bits) != 32 or not bits:
        return None
    first = bits[0]
    if not (isinstance(first, tuple) and len(first) == 3 and first[0] == STACK_ADDR):
        return None
    delta = first[1]
    return delta if all(bit == (STACK_ADDR, delta, i) for i, bit in enumerate(bits)) else None


def constant(bits):
    if all(bit in (0, 1) for bit in bits):
        return sum(bit << i for i, bit in enumerate(bits))
    return None


def signed(value, width):
    sign = 1 << (width - 1)
    mask = (1 << width) - 1
    value &= mask
    return value - (1 << width) if value & sign else value


def bitwise(opc, a, b):
    out = []
    for left, right in zip(a, b):
        if opc == "INT_OR":
            if left == 1 or right == 1:
                out.append(1)
            elif left == 0:
                out.append(right)
            elif right == 0 or left == right:
                out.append(left)
            else:
                out.append(None)
        elif opc == "INT_AND":
            if left == 0 or right == 0:
                out.append(0)
            elif left == 1:
                out.append(right)
            elif right == 1 or left == right:
                out.append(left)
            else:
                out.append(None)
        else:
            if left == 0:
                out.append(right)
            elif right == 0:
                out.append(left)
            elif left in (0, 1) and right in (0, 1):
                out.append(left ^ right)
            elif left is not None and left == right:
                out.append(0)
            else:
                out.append(None)
    return tuple(out)


def fact(bits):
    known_mask = known_value = entry_mask = 0
    for i, bit in enumerate(bits):
        if bit in (0, 1):
            known_mask |= 1 << i
            known_value |= bit << i
        elif bit == (ENTRY_CW, i):
            entry_mask |= 1 << i
    return Fact(known_mask & CW_MASK, known_value & CW_MASK, entry_mask & CW_MASK)


def _linear_leaf(fir):
    if (not fir or not fir.insns or fir.entries or fir.seh or fir.exits or fir.tables
            or fir.noreturn or len(fir.succ) != len(fir.insns)
            or fir.addr != fir.insns[0].addr):
        return False
    for i, ins in enumerate(fir.insns):
        if (not ins.raw or ins.internal_flow
                or any(op.opc in BRANCHES and op.opc != "RETURN" for op in ins.ops)):
            return False
        if any(op.opc == "CALLOTHER" for op in ins.ops):
            return False
        is_return = any(op.opc == "RETURN" for op in ins.ops)
        if is_return:
            if i != len(fir.insns) - 1 or fir.succ[i]:
                return False
        elif fir.succ[i] != [i + 1] and tuple(fir.succ[i]) != (i + 1,):
            return False
    return any(op.opc == "RETURN" for op in fir.insns[-1].ops)


def relations(fir, lifter=None):
    """Return proven FLDCW instruction-index facts for a closed linear leaf, else None."""
    if not _linear_leaf(fir):
        return None
    try:
        lifter = lifter or _lifter()
        cw_var = lifter.register("FPUControlWord")
        esp_var = lifter.register("ESP")
    except Exception:
        return None

    cells = {}
    memory = {}
    write_bits(cells, cw_var, tuple((ENTRY_CW, i) for i in range(16)))
    write_bits(cells, esp_var, affine_bits(0, 32))

    def read(var):
        space, off, size = var
        width = size * 8
        if space == "const":
            return const_bits(off, width)
        if space not in ("register", "unique"):
            return unknown(width)
        return tuple(cells.get((space, off + byte, bit), None)
                     for byte in range(size) for bit in range(8))

    def load(address, size):
        delta = affine(address)
        if delta is None:
            return unknown(size * 8)
        return tuple(bit for byte in range(size)
                     for bit in memory.get((delta + byte) & 0xffffffff, unknown(8)))

    def store(address, value):
        delta = affine(address)
        if delta is None:
            if any(isinstance(bit, tuple) and bit[0] == STACK_ADDR for bit in value):
                return False
            memory.clear()
            return True
        size = len(value) // 8
        for byte in range(size):
            memory[(delta + byte) & 0xffffffff] = tuple(value[byte * 8:(byte + 1) * 8])
        return True

    def write(var, value):
        write_bits(cells, var, value)

    relations = {}
    for i, ins in enumerate(fir.insns):
        for key in tuple(cells):
            if key[0] == "unique":
                del cells[key]
        mnemonic = instruction_mnemonic(ins)
        if mnemonic is None:
            return None
        cw_written = False
        for op in ins.ops:
            opc = op.opc
            if opc == "STORE":
                if len(op.ins) != 2 or not store(read(op.ins[0]), read(op.ins[1])):
                    return None
                continue
            if opc == "RETURN":
                continue
            if opc in BRANCHES:
                return None
            if op.out is None:
                continue
            if op.out[0] not in ("register", "unique", "stin", "stout"):
                return None
            if (op.out[0] == "register" and op.out[1] < cw_var[1] + cw_var[2]
                    and op.out[1] + op.out[2] > cw_var[1]):
                cw_written = True
            width = op.out[2] * 8
            value = unknown(width)
            if opc == "COPY" and op.ins:
                value = read(op.ins[0])
            elif opc == "LOAD" and op.ins:
                value = load(read(op.ins[-1]), op.out[2])
            elif opc in ("INT_OR", "INT_AND", "INT_XOR") and len(op.ins) == 2:
                value = bitwise(opc, read(op.ins[0]), read(op.ins[1]))
            elif opc in ("INT_ADD", "INT_SUB") and len(op.ins) == 2:
                left, right = read(op.ins[0]), read(op.ins[1])
                la, ra = affine(left), affine(right)
                lc, rc = constant(left), constant(right)
                delta = None
                if la is not None and rc is not None:
                    offset = signed(rc, len(right))
                    delta = la + (offset if opc == "INT_ADD" else -offset)
                elif opc == "INT_ADD" and ra is not None and lc is not None:
                    delta = ra + signed(lc, len(left))
                if delta is not None and -(1 << 31) <= delta < (1 << 31):
                    value = affine_bits(delta, width)
                elif lc is not None and rc is not None:
                    result = lc + rc if opc == "INT_ADD" else lc - rc
                    value = const_bits(result, width)
            elif opc in ("INT_ZEXT", "INT_SEXT") and op.ins:
                source = read(op.ins[0])
                if len(source) <= width:
                    fill = 0 if opc == "INT_ZEXT" else source[-1]
                    value = source + (fill,) * (width - len(source))
            elif opc == "SUBPIECE" and len(op.ins) == 2:
                source = read(op.ins[0])
                byteoff = constant(read(op.ins[1]))
                if byteoff is not None:
                    value = tuple(source[byteoff * 8:byteoff * 8 + width])
                    if len(value) != width:
                        value = unknown(width)
            elif opc == "PIECE" and len(op.ins) == 2:
                value = read(op.ins[1]) + read(op.ins[0])
                if len(value) != width:
                    value = unknown(width)
            write(op.out, value)

        effect = writer_effect(mnemonic)
        if mnemonic in SETTERS:
            write_bits(cells, cw_var, const_bits(0x037f, 16))
        elif mnemonic in ENV_MASKERS:
            old = read(cw_var)
            write_bits(cells, cw_var, tuple(1 if i < 6 else old[i] for i in range(16)))
        elif mnemonic in ENV_RESTORES:
            write_bits(cells, cw_var, unknown(16))
        elif mnemonic == "FLDCW" and not cw_written:
            write_bits(cells, cw_var, unknown(16))
        elif effect is None and instruction_effect(mnemonic)[0] == "unknown":
            write_bits(cells, cw_var, unknown(16))
        if mnemonic == "FLDCW":
            relation = fact(read(cw_var))
            if relation.known_mask or relation.entry_mask:
                relations[i] = relation
    return relations


def write_bits(cells, var, value):
    if var is None or var[0] not in ("register", "unique"):
        return
    space, off, size = var
    width = size * 8
    value = tuple(value[:width]) + (None,) * max(0, width - len(value))
    for byte in range(size):
        for bit in range(8):
            cells[(space, off + byte, bit)] = value[byte * 8 + bit]
