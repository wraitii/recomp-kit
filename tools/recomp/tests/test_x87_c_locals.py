"""Production C region boundaries; native full-state checks use the fragment probe."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T


DOT = ["FLD float ptr [ESI]", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]"]


def translate(lines, enabled=True, entries=()):
    insns = T.parse_listing_text("\n".join(f"{0x100000+i:08x}  {s}" for i, s in enumerate(lines)))
    fn = T.Function(0x100000, "synthetic", len(insns), insns)
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(eager_flags=False, x87_locals=enabled))
    tr.prepare(fn)
    return "\n".join(tr.translate(fn, entries)), tr


def test_full_state_and_rounding_not_dead_slot_normalization():
    body, tr = translate(DOT + ["FSTP float ptr [EBX]", "RET"])
    assert body.count("fx87_sw(&x87_sw_, x87_cw_,") == 2
    assert "fpush(c," not in body and "fdrop(c);" not in body
    assert "st_bits[" in body and "st_exact[" in body
    assert "FTAG_EMPTY" in body and "c->st[" in body
    assert tr.stats["_x87_local_regions"] == 1
    assert "/* 00100000 local x87" in body
    assert "/* 00100001 FMUL float ptr [EDI] */" in body
    assert "local x87" not in translate(DOT + ["RET"], enabled=False)[0]


@pytest.mark.parametrize("boundary", [
    "CALL 0x00200000", "MOV EAX,dword ptr FS:[ESI]",
    "PUSH EAX", "FLDCW word ptr [ESI]", "FILD qword ptr [ESI]", "FXAM",
])
def test_observers_materialize_before_original_instruction(boundary):
    body, _ = translate(DOT + [boundary, "RET"])
    commit = body.index("c->fpu_top = (x87_top_")
    assert commit < body.index(f"00100003 {boundary}")


def test_integer_accesses_keep_order_inside_region():
    body, _ = translate(DOT + ["MOV EAX,dword ptr [ESI]", "MOV dword ptr [ESI],EAX",
                               "FSTP float ptr [EBX]", "RET"])
    assert body.count("local x87") == 1
    assert body.index("00100003 MOV") < body.index("00100004 MOV") < body.index("00100005 FSTP")
    assert body.index("00100005 FSTP") < body.index("c->st[")


def test_binary32_arithmetic_requires_operand_provenance():
    body, _ = translate(["FLD double ptr [ESI]", "FMUL float ptr [EDI]",
                         "FADD float ptr [EDI + 4]", "FSTP float ptr [EBX]", "RET"])
    # The first arithmetic consumes the wide input. Its PC=00 result then
    # provides a proven binary32 operand for the second operation.
    assert body.count("fx87_exact_sw(&x87_sw_,") == 1
    assert "(x87_cw_ & 0x300u) == 0u" in body


def test_binary32_provenance_does_not_cross_a_join():
    body, _ = translate([*DOT, "TEST EAX,EAX", "JZ 0x00100007",
                         "FSTP float ptr [EBX]", "FLD double ptr [ESI]",
                         "FMUL float ptr [EDI]", "FSTP float ptr [EBX]", "RET"])
    # The join can receive the original narrow value or the replacement double.
    assert body.count("fx87_exact_sw(&x87_sw_,") == 2


def test_diamond_join_uses_scalar_slots_and_publishes_before_return():
    body, _ = translate(DOT + ["TEST EAX,EAX", "JZ 0x00100007", "FMUL float ptr [ESI]",
                               "JMP 0x00100008", "FADD float ptr [EDI]",
                               "FSTP float ptr [EBX]", "RET"])
    assert "local x87 CFG" in body
    assert "goto L_x87_00100007" in body and "goto L_x87_00100008" in body
    assert body.index("c->st[") < body.index("00100009 RET")
    assert "x87_tag7_ == 4" in body


def test_loop_backedge_skips_initialization():
    body, _ = translate(["MOV EAX,2", *DOT, "FSTP float ptr [EBX]", "DEC EAX",
                         "JNZ 0x00100001", "RET"])
    assert "local x87 CFG" in body
    assert body.index("const unsigned x87_top_") < body.index("L_x87_00100001:")
    assert "goto L_x87_00100001;" in body


def test_disagreeing_top_join_and_alternate_entry_refuse_crossing():
    lines = DOT + ["JZ 0x00100005", "FLD1", "FSTP float ptr [EBX]", "RET"]
    assert "local x87 CFG" not in translate(lines)[0]
    lines = DOT + ["JZ 0x00100005", "NOP", "FSTP float ptr [EBX]", "RET"]
    body, _ = translate(lines, entries=(0x100005,))
    assert "goto L_x87_00100005" not in body


def test_alternate_entry_and_branch_targets_start_new_regions():
    body, _ = translate(DOT + ["FSTP float ptr [EBX]", "RET"], entries=(0x100002,))
    # The alternate entry consumes an incoming value: it must retain eager C.
    assert "local x87" not in body
    body, _ = translate(DOT + ["JMP 0x00100005", "NOP", *DOT, "RET"])
    assert body.count("local x87") == 2
    assert body.index("c->fpu_top = (x87_top_") < body.index("00100003 JMP")


def test_virtual_top_for_status_and_eager_comparison():
    body, _ = translate(DOT + ["FNSTSW AX", "FCOMP float ptr [EDI]", "RET"])
    assert "fstsw(c)" not in body
    assert "x87_sw_ & (uint16_t)~0x3800u" in body
    assert "fcom_sw(&x87_sw_, x87_v2_" in body
    assert "FTAG_EMPTY" in body


def test_incoming_integer_state_is_not_assumed_float():
    body, _ = translate(["FILD qword ptr [ESI]", "FMUL float ptr [EDI]", "FSTP float ptr [EBX]", "RET"])
    assert "local x87" not in body
    assert "fpush_int(c," in body


def test_local_register_copies_and_pop_keep_one_region():
    body, tr = translate(["FLD float ptr [ESI]", "FLD ST0", "FADD float ptr [EDI]",
                          "FST ST1", "FSTP ST0", "FSTP float ptr [EBX]", "RET"])
    assert tr.stats["_x87_local_regions"] == 1
    assert "fpush_st(c," not in body and "fcopy(c," not in body
    assert "fdrop(c);" not in body
    assert "fto_float_cw(x87_cw_," in body


def test_incoming_register_copy_and_cfg_metadata_remain_eager():
    body, _ = translate(["FILD qword ptr [ESI]", "FLD ST0", *DOT, "RET"])
    assert "fpush_st(c, 0);" in body
    body, _ = translate(["FLD float ptr [ESI]", *DOT, "TEST EAX,EAX",
                         "JZ 0x00100007", "FST ST1", "NOP", "RET"])
    assert "local x87 CFG" in body
    assert "fcopy(c, 1, 0);" in body
    assert body.index("c->st[") < body.index("fcopy(c, 1, 0);")


def test_listing_gap_cuts_even_without_a_label():
    from x87_locals import lower_regions
    insns = T.parse_listing_text("\n".join(f"{0x100000+i:08x}  {s}" for i, s in enumerate(DOT * 2)))
    fn = T.Function(0x100000, "synthetic", len(insns), insns)
    fn.contiguous[2] = False
    tr = T.Translator(None, {fn.addr}, SimpleNamespace(eager_flags=True))
    bodies = {i: tr.emit(fn, i, T.ALL_FLAGS) for i in range(len(insns))}
    _, count, _ = lower_regions(fn, bodies, set(), set(), T.parse_operand)
    assert count == 2


def test_config_is_opt_in_and_boolean(tmp_path):
    cfg = T.game_config.load(Path(__file__).resolve().parents[3] / "games/stub")
    assert cfg["translate"]["fault_state"] == "relaxed"
    text = (cfg["dir"] / "game.toml").read_text().replace("[translate]", '[translate]\nfault_state = "strict"')
    (tmp_path / "game.toml").write_text(text)
    with pytest.raises(ValueError, match="fault_state must be"):
        T.game_config.load(tmp_path)
