"""Scope boundaries for the opt-in fragment probe (native checks run via build.py)."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.x87_locals.run import emit, CASES, MODES


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lines", [
    ["CALL 0x12345678"],
    ["FLDCW word ptr [ESI]"],
    ["FLD ST0"],
    ["FLD double ptr [ESI]"],
    ["FLD float ptr FS:[ESI]"],
    ["FMUL float ptr [ESI]"],
    ["FADDP"],
    ["FLD float ptr [ESI]", "FADDP ST1,ST0"],
    ["FLD float ptr [ESI]"] * 9,
])
def test_unsupported_regions_fail(lines, mode):
    with pytest.raises(ValueError):
        emit(lines, mode)


@pytest.mark.parametrize("name", CASES)
def test_preserved_rounding_points(name):
    """Local lifting must retain one runtime rounding per arithmetic instruction."""
    expected = sum(line.split()[0] in {"FMUL", "FADD", "FSUB", "FADDP"} for line in CASES[name])
    for mode in ("baseline", "full", "live"):
        assert emit(CASES[name], mode).count("fx87(c,") == expected
    assert "fx87(c," not in emit(CASES[name], "relaxed")
