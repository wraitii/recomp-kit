"""LLVM frontend contract checks; the compiled pass is checked by the build probe."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.x87_llvm.run import emit_llvm, CASES


@pytest.mark.parametrize('name', CASES)
def test_frontend_retains_machine_operations_for_the_pass(name):
    text = emit_llvm('test', CASES[name], True)
    assert '"recomp.x87.region"' in text
    assert text.count('call void @rk_push(') == sum(s.startswith('FLD ') for s in CASES[name])
    assert text.count('call double @rk_round(') == sum(
        s.split()[0] in {'FMUL', 'FADD', 'FSUB', 'FADDP'} for s in CASES[name])
    assert '@rk_slot' not in text  # State materialization belongs to the LLVM pass.
    assert ' fast ' not in text and ' nsw ' not in text and ' nuw ' not in text


def test_guest_address_wraps_before_memory_access():
    text = emit_llvm('test', ['FLD float ptr [ESI + EDI*0x4 + -0x4]'], True)
    assert 'mul i32' in text
    assert '4294967292' in text
    assert 'add i32' in text
    assert 'getelementptr' not in text


@pytest.mark.parametrize('lines', [
    ['CALL 0x12345678'], ['FLD ST0'], ['FLD double ptr [ESI]'],
    ['FLDCW word ptr [ESI]'], ['FADDP'], ['FLD float ptr [ESI]'] * 9,
])
def test_unsupported_frontend_input_is_not_approximated(lines):
    with pytest.raises(ValueError):
        emit_llvm('test', lines, True)
