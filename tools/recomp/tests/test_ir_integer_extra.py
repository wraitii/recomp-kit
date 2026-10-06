"""Independent semantics tests for the signed integer SSA corrections.

Every fixture is decoded from real instruction bytes. The interpreter here is
deliberately separate from the emitter and from the other SSA interpreter so an
expected quotient, remainder, flag or rejection is asserted independently.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter, Insn
from ir.summary import FunctionIR, default_successors
from ir.ssa import SSAError, build
from ir.integer_extra import EXTRA_MNEMONICS, correct, divide_signed, negate, shift_arithmetic

LIFTER = Lifter()
ENTRY = 0x1000


def function(*hexes, at=ENTRY):
    insns, addr = [], at
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(addr, raw))
        addr += len(raw)
    return FunctionIR(at, insns, default_successors(insns))


def corrected(fir):
    insns = []
    for ins in fir.insns:
        if ins.mnem.upper() in EXTRA_MNEMONICS:
            ops = correct(ins, LIFTER)
            insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                              ins.x87, ins.internal_flow, ins.userops, ins.raw))
        else:
            insns.append(ins)
    return FunctionIR(fir.addr, insns, fir.succ)


class Fault(Exception):
    pass


def signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def run(fir, **regs):
    """Evaluate a straight-line corrected fixture and return the exit state.

    `regs` names x86 registers; their byte lanes are the SSA inputs. The model
    is intentionally simple: it checks values and flags, not memory ordering.
    """
    register_bytes = {}
    for name, value in regs.items():
        _, off, size = LIFTER.register(name)
        for n in range(size):
            register_bytes[("register", off + n)] = (value >> (8 * n)) & 0xff
    s = build(corrected(fir))
    values = {}

    def read(v):
        v = s.resolve(v)
        return v.data if v.opc in ("CONST", "TARGET") else values[v.id]

    for v in s.values:
        if v.opc == "INPUT":
            values[v.id] = register_bytes.get(v.data, 0) & 0xff if v.size else 0
    b = s.blocks[s.entry]
    for phi in b.phis:
        values[phi.id] = read(phi.args[0])

    def merged_state():
        state = dict(register_bytes)
        for key, value in b.exit.items():
            state[key] = read(value)
        return state

    for v in b.ops:
        a = [read(arg) for arg in v.args]
        opc, size = v.opc, v.size
        if opc == "PACK":
            r = sum(value << (8 * n) for n, value in enumerate(a))
        elif opc == "BYTE":
            r = a[0] >> (8 * v.data)
        elif opc in ("COPY", "INT_ZEXT"):
            r = a[0]
        elif opc == "INT_SEXT":
            r = signed(a[0], v.args[0].size * 8)
        elif opc == "INT_ADD":
            r = a[0] + a[1]
        elif opc == "INT_SUB":
            r = a[0] - a[1]
        elif opc == "INT_MULT":
            r = a[0] * a[1]
        elif opc == "INT_AND":
            r = a[0] & a[1]
        elif opc == "INT_OR":
            r = a[0] | a[1]
        elif opc == "INT_XOR":
            r = a[0] ^ a[1]
        elif opc == "INT_EQUAL":
            r = a[0] == a[1]
        elif opc == "INT_NOTEQUAL":
            r = a[0] != a[1]
        elif opc == "INT_LESS":
            r = a[0] < a[1]
        elif opc == "INT_LESSEQUAL":
            r = a[0] <= a[1]
        elif opc in ("INT_SLESS", "INT_SLESSEQUAL", "INT_SCARRY", "INT_SBORROW", "INT_CARRY"):
            bits = v.args[0].size * 8
            if opc == "INT_SLESS":
                r = signed(a[0], bits) < signed(a[1], bits)
            elif opc == "INT_SLESSEQUAL":
                r = signed(a[0], bits) <= signed(a[1], bits)
            elif opc == "INT_CARRY":
                r = a[0] + a[1] >= 1 << bits
            elif opc == "INT_SCARRY":
                r = not -(1 << (bits - 1)) <= signed(a[0], bits) + signed(a[1], bits) < (1 << (bits - 1))
            else:
                r = not -(1 << (bits - 1)) <= signed(a[0], bits) - signed(a[1], bits) < (1 << (bits - 1))
        elif opc == "BOOL_AND":
            r = bool(a[0] and a[1])
        elif opc == "BOOL_OR":
            r = bool(a[0] or a[1])
        elif opc == "BOOL_XOR":
            r = bool(a[0]) != bool(a[1])
        elif opc == "BOOL_NEGATE":
            r = not a[0]
        elif opc == "INT_NEGATE":
            r = ~a[0]
        elif opc == "INT_2COMP":
            r = -a[0]
        elif opc == "POPCOUNT":
            r = bin(a[0]).count("1")
        elif opc == "INT_RIGHT":
            r = 0 if a[1] >= v.args[0].size * 8 else a[0] >> a[1]
        elif opc == "INT_LEFT":
            r = 0 if a[1] >= v.args[0].size * 8 else a[0] << a[1]
        elif opc == "INT_SRIGHT":
            bits = v.args[0].size * 8
            r = signed(a[0], bits) >> min(a[1] & 31, bits - 1)
        elif opc == "SUBPIECE":
            r = a[0] >> (8 * a[1]) if a[1] < v.args[0].size else 0
        elif opc == "PIECE":
            r = (a[0] << (v.args[1].size * 8)) | a[1]
        elif opc == "IDIV32":
            base = 1 if len(v.args) == 4 else 0  # ordered effects prepend MEMORY
            numerator = signed(a[base], 64)
            divisor = signed(a[base + 1] & 0xffffffff, 32)
            if divisor == 0 or (divisor == -1 and numerator == -(1 << 63)):
                raise Fault()
            q = abs(numerator) // abs(divisor)
            if (numerator < 0) != (divisor < 0):
                q = -q
            if q < -(1 << 31) or q > (1 << 31) - 1:
                raise Fault()
            r = (q & 0xffffffff) | (((numerator - q * divisor) & 0xffffffff) << 32)
        elif opc == "MEMORY":
            r = 0
        elif opc == "RETURN":
            return merged_state()
        else:
            raise AssertionError("interpreter does not model %s" % opc)
        values[v.id] = r & ((1 << (8 * size)) - 1) if size else 0
    return merged_state()


def reg(state, name):
    _, off, size = LIFTER.register(name)
    return sum(state[("register", off + n)] << (8 * n) for n in range(size))


# ------------------------------------------------------------------ lifting --

def test_cdq_sign_extends_eax_into_edx():
    out = run(function("99", "c3"), EAX=0x80000001)
    assert reg(out, "EDX") == 0xffffffff
    out = run(function("99", "c3"), EAX=0x7fffffff)
    assert reg(out, "EDX") == 0


def test_imul_two_operand_signed_overflow_flag():
    out = run(function("0fafd0", "c3"), EDX=0x00010000, EAX=0x00010000)
    assert reg(out, "EDX") == 0
    assert reg(out, "CF") == 1 and reg(out, "OF") == 1


def test_imul_three_operand_immediate():
    out = run(function("6bc00a", "c3"), EAX=7)
    assert reg(out, "EAX") == 70


def test_setne_uses_zf_without_touching_other_bits():
    out = run(function("0f95c0", "c3"), EAX=0x12340000, ZF=1)
    assert reg(out, "EAX") == 0x12340000
    out = run(function("0f95c0", "c3"), EAX=0x12340000, ZF=0)
    assert reg(out, "EAX") == 0x12340001


def test_neg_defines_af_from_result_low_nibble():
    out = run(function("f7d8", "c3"), EAX=0x10)
    assert reg(out, "EAX") == 0xfffffff0
    assert reg(out, "AF") == 0
    out = run(function("f7d8", "c3"), EAX=0x11)
    assert reg(out, "EAX") == 0xffffffef
    assert reg(out, "AF") == 1


def test_sar_matches_runtime_of_recipe():
    out = run(function("c1f905", "c3"), ECX=0x80000000, OF=1)
    assert reg(out, "ECX") == 0xfc000000
    assert reg(out, "CF") == 0 and reg(out, "OF") == 0
    out = run(function("c1f900", "c3"), ECX=0x12345678, OF=1, CF=1)
    assert reg(out, "ECX") == 0x12345678
    assert reg(out, "OF") == 1 and reg(out, "CF") == 1


def test_idiv_valid_signed_division_truncates_toward_zero():
    # EDX:EAX = -7, ECX = 2 -> quotient -3, remainder -1.
    out = run(function("f7f9", "c3"), EDX=0xffffffff, EAX=0xfffffff9, ECX=2)
    assert reg(out, "EAX") == 0xfffffffd
    assert reg(out, "EDX") == 0xffffffff


def test_idiv_divide_by_zero_is_a_fault():
    with pytest.raises(Fault):
        run(function("f7f9", "c3"), EAX=1, EDX=0, ECX=0)


def test_idiv_min_over_negative_one_is_a_fault():
    with pytest.raises(Fault):
        run(function("f7f9", "c3"), EAX=0, EDX=0x80000000, ECX=0xffffffff)


def test_rejects_memory_neg_sar_and_narrow_idiv():
    with pytest.raises(SSAError):
        negate(LIFTER.lift(ENTRY, bytes.fromhex("f71b"), "NEG"), LIFTER)
    with pytest.raises(SSAError):
        shift_arithmetic(LIFTER.lift(ENTRY, bytes.fromhex("c13b03"), "SAR"), LIFTER)
    narrow = LIFTER.lift(ENTRY, bytes.fromhex("66f7f9"), "IDIV")
    assert any(op.opc == "INT_SDIV" for op in narrow.ops)
    with pytest.raises(SSAError):
        divide_signed(narrow, LIFTER)


def test_admits_real_memory_idiv_and_narrow_sar_forms():
    # idiv dword ptr [edi+0x18] from the original 0x0057a0d0 sequence.
    idiv = LIFTER.lift(ENTRY, bytes.fromhex("f77f18"), "IDIV")
    assert any(op.opc == "IDIV32" for op in divide_signed(idiv, LIFTER))
    for h in ("c0f803", "66c1f803", "d2f8"):
        ins = LIFTER.lift(ENTRY, bytes.fromhex(h), "SAR")
        assert any(op.opc == "INT_SRIGHT" for op in shift_arithmetic(ins, LIFTER))


SETCC_CONDITIONS = (
    # (capstone mnemonic, opcode, expected 0/1 from CF, PF, ZF, SF, OF)
    ("seto", 0x90, lambda f: f["OF"]),
    ("setno", 0x91, lambda f: not f["OF"]),
    ("setb", 0x92, lambda f: f["CF"]),
    ("setae", 0x93, lambda f: not f["CF"]),
    ("sete", 0x94, lambda f: f["ZF"]),
    ("setne", 0x95, lambda f: not f["ZF"]),
    ("setbe", 0x96, lambda f: f["CF"] or f["ZF"]),
    ("seta", 0x97, lambda f: not f["CF"] and not f["ZF"]),
    ("sets", 0x98, lambda f: f["SF"]),
    ("setns", 0x99, lambda f: not f["SF"]),
    ("setp", 0x9a, lambda f: f["PF"]),
    ("setnp", 0x9b, lambda f: not f["PF"]),
    ("setl", 0x9c, lambda f: f["SF"] != f["OF"]),
    ("setge", 0x9d, lambda f: f["SF"] == f["OF"]),
    ("setle", 0x9e, lambda f: f["ZF"] or f["SF"] != f["OF"]),
    ("setg", 0x9f, lambda f: not f["ZF"] and f["SF"] == f["OF"]),
)


@pytest.mark.parametrize("mnem,opcode,condition", SETCC_CONDITIONS)
@pytest.mark.parametrize("flags", [
    {"CF": 1, "PF": 0, "ZF": 0, "SF": 1, "OF": 0},
    {"CF": 0, "PF": 1, "ZF": 1, "SF": 0, "OF": 1},
])
def test_all_setcc_conditions_match_their_boolean(mnem, opcode, condition, flags):
    out = run(function("0f%02xc0" % opcode, "c3"), EAX=0x12345600, **flags)
    assert reg(out, "EAX") == 0x12345600 | int(condition(flags))


@pytest.mark.parametrize("hexes,value,count,width", [
    ("c0f800", 0x80, 0, 8), ("c0f801", 0x80, 1, 8), ("c0f807", 0x80, 7, 8),
    ("c0f808", 0x80, 8, 8), ("c0f81f", 0x80, 31, 8),
    ("66c1f800", 0x8000, 0, 16), ("66c1f801", 0x8000, 1, 16),
    ("66c1f80f", 0x8000, 15, 16), ("66c1f810", 0x8000, 16, 16),
    ("66c1f81f", 0x8000, 31, 16),
    ("c1f800", 0x80000000, 0, 32), ("d1f8", 0x80000000, 1, 32),
    ("c1f81f", 0x80000000, 31, 32),
])
def test_sar_count_edges_use_the_runtime_clamp(hexes, value, count, width):
    # sar eax/al/ax, imm; the destination is always the accumulator here.
    name = "EAX" if width == 32 else ("AX" if width == 16 else "AL")
    out = run(function(hexes, "c3"), EAX=value, OF=1, CF=1)
    if count == 0:
        expected = value & ((1 << width) - 1)
        assert reg(out, name) == expected
        assert reg(out, "OF") == 1 and reg(out, "CF") == 1
        return
    k = min(count, width - 1)
    r = signed(value & ((1 << width) - 1), width) >> k
    assert reg(out, name) == r & ((1 << width) - 1)
    assert reg(out, "OF") == 0
    assert reg(out, "CF") == ((value >> (min(count, width) - 1)) & 1)
