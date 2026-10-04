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
    assert body.count("fx87(c,") == 2
    assert "fpush(c," not in body and "fdrop(c);" not in body
    assert "st_bits[" in body and "st_exact[" in body
    assert "FTAG_EMPTY" in body and "c->st[" in body
    assert tr.stats["_x87_local_regions"] == 1
    assert "/* 00100000 local x87" in body
    assert "/* 00100001 FMUL float ptr [EDI] */" in body
    assert "local x87" not in translate(DOT + ["RET"], enabled=False)[0]


@pytest.mark.parametrize("boundary", [
    "CALL 0x00200000", "MOV EAX,dword ptr [ESI]", "MOV dword ptr [ESI],EAX",
    "PUSH EAX", "FLDCW word ptr [ESI]", "FILD qword ptr [ESI]", "FXAM",
])
def test_observers_materialize_before_original_instruction(boundary):
    body, _ = translate(DOT + [boundary, "RET"])
    commit = body.index("c->fpu_top = (x87_top_")
    assert commit < body.index(f"00100003 {boundary}")


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
    assert "c->fpu_sw & (uint16_t)~0x3800u" in body
    assert "fcom(c, x87_v2_" in body
    assert "FTAG_EMPTY" in body


def test_incoming_integer_state_is_not_assumed_float():
    body, _ = translate(["FILD qword ptr [ESI]", "FMUL float ptr [EDI]", "FSTP float ptr [EBX]", "RET"])
    assert "local x87" not in body
    assert "fpush_int(c," in body


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
    assert cfg["translate"]["x87_locals"] is False
    text = (cfg["dir"] / "game.toml").read_text().replace("[translate]", '[translate]\nx87_locals = "yes"')
    (tmp_path / "game.toml").write_text(text)
    with pytest.raises(ValueError, match="x87_locals must be a boolean"):
        T.game_config.load(tmp_path)
