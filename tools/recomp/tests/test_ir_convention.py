"""`ir_ssa_msvc_convention`: flags dead across CALL/RET and x87 residue skipped.

Shape tests for the emitted publication. Execution evidence comes from
`tools/build.py --ir-ssa-checks` (the production column compares with the
convention's dead fields cleared) and the game function corpus.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir import production
from ir.emit_c import emit
from ir.lift import Lifter
from ir.summary import FunctionIR, default_successors


def function(*hexes):
    lifter, at, insns = Lifter(), 0x1000, []
    for raw in map(bytes.fromhex, hexes):
        insns.append(lifter.lift(at, raw))
        at += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


def fast(f, **kw):
    return emit(f, "t", _guard_null_checks=False, **kw)


def exact(f, **kw):
    return fast(f, msvc_convention=False, **kw)


def arithmetic_flags(text):
    return [line for line in text.splitlines()
            if "c->eflags_" in line and "c->eflags_df" not in line]


def test_flags_are_not_published_at_return():
    producer = function("39d8", "c3")  # cmp eax,ebx; ret
    assert "c->eflags_zf =" in exact(producer)
    assert not arithmetic_flags(fast(producer))


def test_flags_consumed_inside_the_function_stay_exact():
    # cmp eax,ebx; jz skip; inc eax; skip: ret
    text = fast(function("39d8", "7401", "40", "c3"))
    assert "if (" in text and not arithmetic_flags(text)


def test_incoming_flags_are_still_read_when_consumed():
    # jz +1; nop; ret reads ZF before any definition: the CPU value is used.
    facts = {}
    text = emit(function("7401", "90", "c3"), "t", facts=facts)
    assert "c->eflags_zf" in text
    assert facts["flags_read_at_entry"] and not facts["flags_read_after_call"]


def test_flags_are_not_published_before_calls_but_df_is():
    # std; cmp eax,ebx; call 0x2000; ret
    f = function("fd", "39d8", "e8f80f0000", "c3")
    calls = {0x2000: "callee"}
    assert "c->eflags_zf =" in exact(f, call_symbols=calls)
    text = fast(f, call_symbols=calls)
    assert not arithmetic_flags(text.split("callee(c);")[0])
    assert "c->eflags_df =" in text


def test_flag_read_after_call_is_reported():
    # call 0x2000; jz +1; nop; ret
    facts = {}
    emit(function("e8fb0f0000", "7401", "90", "c3"), "t",
         call_symbols={0x2000: "callee"}, facts=facts)
    assert facts["flags_read_after_call"]


def test_division_seams_keep_complete_flags():
    # cmp eax,ebx; div ecx; ret -- the divide-error handler sees every flag.
    text = fast(function("39d8", "f7f1", "c3"))
    before = text.split("div32(")[0]
    assert "c->eflags_zf =" in before


def test_balanced_x87_publishes_no_residue_or_top():
    # fld1; fld1; faddp; fstp dword [edi]; ret
    f = function("d9e8", "d9e8", "dec1", "d91f", "c3")
    conservative = exact(f)
    for name in ("c->st[", "c->st_bits[", "c->st_exact[", "c->fpu_tag =", "c->fpu_top ="):
        assert name in conservative
        assert name not in fast(f)
    assert "c->fpu_sw =" in fast(f)


def test_returned_st0_publishes_value_tag_and_exact_flag_only():
    # fld1; fld1; faddp; ret -- one live register, one popped residue.
    text = fast(function("d9e8", "d9e8", "dec1", "c3"))
    assert text.count("c->st[") == 1 and text.count("c->st_exact[") == 1
    assert "c->st_bits[" not in text  # read only when st_exact is set
    assert text.count("c->fpu_tag =") == 1 and "c->fpu_top =" in text


def test_exact_integer_return_keeps_bits():
    text = fast(function("df2b", "c3"))  # fild qword [ebx]; ret
    assert "c->st_bits[" in text and "c->st_exact[" in text


def test_popping_an_entry_register_retags_it_empty():
    # fstp dword [edi]; ret pops a register that was live at entry.
    text = fast(function("d91f", "c3"))
    assert "FTAG_EMPTY" in text and "c->fpu_top =" in text
    stores = [line for line in text.splitlines() if line.startswith(("c->st[", "c->st_exact["))]
    assert not stores


def test_fincstp_keeps_the_exact_flush():
    # fld1; fincstp; ret leaves a tagged register above TOP.
    f = function("d9e8", "d9f7", "c3")
    facts = {}
    assert emit(f, "t", _guard_null_checks=False, facts=facts) == exact(f)
    assert facts["x87_exact_flush"]


def test_null_check_builds_compile_the_conservative_form():
    f = function("39d8", "c3")
    text = emit(f, "t")
    strict, relaxed = text.split("#else")
    assert "c->eflags_zf =" in strict
    assert "c->eflags_zf =" not in relaxed


def test_production_passes_the_setting_and_reports_it():
    source = Path(production.__file__).read_text()
    assert 'settings.get("ir_ssa_msvc_convention", True)' in source
    assert "msvc_convention=convention" in source and '"convention_census"' in source
