"""Conservative linear-leaf provenance for x87 control-word save/restore.

The analysis follows only original byte-backed, lifted p-code. It tracks
control-word bits, register aliases and local stack bytes; all other values
remain unknown. It proves no environment/FX state restore or memory alias.
"""
from .lift import Lifter, BRANCHES
from .x87_mode import Fact, CW_MASK, writer_effect, _mnemonic, _effect

_ENTRY_CW = "entry-cw"
_STACK_ADDR = "stack-address"
_ENV_RESTORES = frozenset(("FLDENV", "FRSTOR", "FXRSTOR", "XRSTOR", "XRSTORS"))
_SETTERS = frozenset(("FINIT", "FNINIT", "FSAVE", "FNSAVE"))
_ENV_MASKERS = frozenset(("FSTENV", "FNSTENV"))
_LIFTER = None


def _lifter():
    global _LIFTER
    if _LIFTER is None:
        _LIFTER = Lifter()
    return _LIFTER


def _const_bits(value, width):
    value &= (1 << width) - 1
    return tuple((value >> bit) & 1 for bit in range(width))


def _unknown(width):
    return (None,) * width


def _affine_bits(delta, width):
    return tuple((_STACK_ADDR, delta, bit) for bit in range(width))


def _affine(bits):
    if len(bits) != 32 or not bits:
        return None
    first = bits[0]
    if not (isinstance(first, tuple) and len(first) == 3 and first[0] == _STACK_ADDR):
        return None
    delta = first[1]
    return delta if all(bit == (_STACK_ADDR, delta, i) for i, bit in enumerate(bits)) else None


def _constant(bits):
    if all(bit in (0, 1) for bit in bits):
        return sum(bit << i for i, bit in enumerate(bits))
    return None


def _signed(value, width):
    sign = 1 << (width - 1)
    mask = (1 << width) - 1
    value &= mask
    return value - (1 << width) if value & sign else value


def _bitwise(opc, a, b):
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


def _fact(bits):
    known_mask = known_value = entry_mask = 0
    for i, bit in enumerate(bits):
        if bit in (0, 1):
            known_mask |= 1 << i
            known_value |= bit << i
        elif bit == (_ENTRY_CW, i):
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
    _write(cells, cw_var, tuple((_ENTRY_CW, i) for i in range(16)))
    _write(cells, esp_var, _affine_bits(0, 32))

    def read(var):
        space, off, size = var
        width = size * 8
        if space == "const":
            return _const_bits(off, width)
        if space not in ("register", "unique"):
            return _unknown(width)
        return tuple(cells.get((space, off + byte, bit), None)
                     for byte in range(size) for bit in range(8))

    def load(address, size):
        delta = _affine(address)
        if delta is None:
            return _unknown(size * 8)
        return tuple(bit for byte in range(size)
                     for bit in memory.get((delta + byte) & 0xffffffff, _unknown(8)))

    def store(address, value):
        delta = _affine(address)
        if delta is None:
            if any(isinstance(bit, tuple) and bit[0] == _STACK_ADDR for bit in value):
                return False
            memory.clear()
            return True
        size = len(value) // 8
        for byte in range(size):
            memory[(delta + byte) & 0xffffffff] = tuple(value[byte * 8:(byte + 1) * 8])
        return True

    def write(var, value):
        _write(cells, var, value)

    relations = {}
    for i, ins in enumerate(fir.insns):
        for key in tuple(cells):
            if key[0] == "unique":
                del cells[key]
        mnemonic = _mnemonic(ins)
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
            value = _unknown(width)
            if opc == "COPY" and op.ins:
                value = read(op.ins[0])
            elif opc == "LOAD" and op.ins:
                value = load(read(op.ins[-1]), op.out[2])
            elif opc in ("INT_OR", "INT_AND", "INT_XOR") and len(op.ins) == 2:
                value = _bitwise(opc, read(op.ins[0]), read(op.ins[1]))
            elif opc in ("INT_ADD", "INT_SUB") and len(op.ins) == 2:
                left, right = read(op.ins[0]), read(op.ins[1])
                la, ra = _affine(left), _affine(right)
                lc, rc = _constant(left), _constant(right)
                delta = None
                if la is not None and rc is not None:
                    offset = _signed(rc, len(right))
                    delta = la + (offset if opc == "INT_ADD" else -offset)
                elif opc == "INT_ADD" and ra is not None and lc is not None:
                    delta = ra + _signed(lc, len(left))
                if delta is not None and -(1 << 31) <= delta < (1 << 31):
                    value = _affine_bits(delta, width)
                elif lc is not None and rc is not None:
                    result = lc + rc if opc == "INT_ADD" else lc - rc
                    value = _const_bits(result, width)
            elif opc in ("INT_ZEXT", "INT_SEXT") and op.ins:
                source = read(op.ins[0])
                if len(source) <= width:
                    fill = 0 if opc == "INT_ZEXT" else source[-1]
                    value = source + (fill,) * (width - len(source))
            elif opc == "SUBPIECE" and len(op.ins) == 2:
                source = read(op.ins[0])
                byteoff = _constant(read(op.ins[1]))
                if byteoff is not None:
                    value = tuple(source[byteoff * 8:byteoff * 8 + width])
                    if len(value) != width:
                        value = _unknown(width)
            elif opc == "PIECE" and len(op.ins) == 2:
                value = read(op.ins[1]) + read(op.ins[0])
                if len(value) != width:
                    value = _unknown(width)
            write(op.out, value)

        effect = writer_effect(mnemonic)
        if mnemonic in _SETTERS:
            _write(cells, cw_var, _const_bits(0x037f, 16))
        elif mnemonic in _ENV_MASKERS:
            old = read(cw_var)
            _write(cells, cw_var, tuple(1 if i < 6 else old[i] for i in range(16)))
        elif mnemonic in _ENV_RESTORES:
            _write(cells, cw_var, _unknown(16))
        elif mnemonic == "FLDCW" and not cw_written:
            _write(cells, cw_var, _unknown(16))
        elif effect is None and _effect(mnemonic)[0] == "unknown":
            _write(cells, cw_var, _unknown(16))
        if mnemonic == "FLDCW":
            relation = _fact(read(cw_var))
            if relation.known_mask or relation.entry_mask:
                relations[i] = relation
    return relations


def _write(cells, var, value):
    if var is None or var[0] not in ("register", "unique"):
        return
    space, off, size = var
    width = size * 8
    value = tuple(value[:width]) + (None,) * max(0, width - len(value))
    for byte in range(size):
        for bit in range(8):
            cells[(space, off + byte, bit)] = value[byte * 8 + bit]
