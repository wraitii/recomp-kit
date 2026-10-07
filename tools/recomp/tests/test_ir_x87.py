"""Byte-backed x87 admission, corrected semantic recipes and operand checks."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter
from ir.summary import FunctionIR, default_successors
from ir.ssa import build, SSAError
from ir.emit_c import emit, codegen_ir


def function(*hexes):
    lifter, addr, insns = Lifter(), 0x1000, []
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(lifter.lift(addr, raw))
        addr += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


@pytest.mark.parametrize("hexes,helper", [
    (("d906", "df13", "c3"), "fist_i16(c)"),
    (("d906", "db1b", "c3"), "fist_i32(c)"),
    (("df2e", "df3b", "c3"), "fpush_int(c,"),
    (("dd06", "d9fc", "c3"), "fround_cw(c,"),
    (("d906", "d94604", "d9f8", "c3"), "fprem_common(c,"),
    (("d906", "d85604", "c3"), "fcom(c,"),
    (("dbe2", "c3"), "~0x80ffu"),
    (("d9e5", "c3"), "fxam(c)"),
    (("db2e", "db3b", "c3"), "wrf80("),
    (("9bdfe0", "c3"), "fstsw(c)"),
])
def test_known_sleigh_gaps_use_runtime_semantics_before_admission(hexes, helper):
    f = function(*hexes)
    assert helper in emit(f, "test_fn", optimize=False)
    corrected = codegen_ir(f, Lifter())
    assert not any(op.opc.startswith("FLOAT_") for ins in corrected.insns for op in ins.ops)
    s = build(corrected)
    effects = [v for b in s.blocks.values() for v in b.ops
               if v.opc in ("LOAD", "STORE", "X87_MEM", "X87_REG")]
    for earlier, later in zip(effects, effects[1:]):
        token = s.resolve(later.args[0])
        assert token.opc == "MEMORY" and token.args == (earlier,)


@pytest.mark.parametrize("hexcode,expected", [
    ("d8e1", "ST(c, 0) - ST(c, 1)"),
    ("dce1", "ST(c, 0) - ST(c, 1)"),
    ("d8e9", "ST(c, 1) - ST(c, 0)"),
    ("dce9", "ST(c, 1) - ST(c, 0)"),
    ("dee1", "ST(c, 0) - ST(c, 1)"),
    ("dee9", "ST(c, 1) - ST(c, 0)"),
])
def test_register_operand_direction_comes_from_bytes(hexcode, expected):
    body = emit(function(hexcode, "c3"), "test_fn", optimize=False)
    assert expected in body
    assert ("fset(c, 1," in body) == hexcode.startswith(("dc", "de"))


@pytest.mark.parametrize("hexcode,reason", [
    ("64d900", "unsupported x87 addressing"),
    ("67d900", "unsupported x87 addressing"),
    ("d9fb", "unsupported x87 instruction FSINCOS"),
    ("d933", "unsupported x87 instruction FNSTENV"),
    ("dbe9", "unsupported x87 instruction FUCOMI"),
])
def test_unmodeled_x87_effects_retain_named_fallback(hexcode, reason):
    with pytest.raises(SSAError, match=reason):
        emit(function(hexcode, "c3"), "test_fn", optimize=False)


def test_original_bytes_are_required_for_x87_operand_corrections():
    f = function("d906", "c3")
    f.insns[0].raw = None
    with pytest.raises(SSAError, match="x87 requires original bytes"):
        emit(f, "test_fn", optimize=False)


def test_mismatched_original_boundary_is_rejected():
    f = function("d906", "c3")
    f.insns[0].length = 3
    with pytest.raises(SSAError, match="operand decode disagrees with boundary"):
        emit(f, "test_fn", optimize=False)
