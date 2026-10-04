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

from experiments.x87_llvm.function import emit_function
import translate as T


def function_insns(lines):
    return T.parse_listing_text('\n'.join(f'{0x1000+i:08x}  {s}' for i, s in enumerate(lines)))


def test_function_frontend_emits_cfg_without_deciding_x87_flow():
    insns = function_insns(['FLD float ptr [ESP + 4]', 'FNSTSW AX', 'TEST AH,0x1',
                           'JE 0x1005', 'FCHS', 'FCOMP double ptr [ECX]', 'RET'])
    text = emit_function('test', insns, True)
    assert 'br i1' in text and 'label %b1005' in text
    assert 'fneg double' in text and '@rk_load64' in text
    assert '@rk_fnstsw' in text and '@rk_ret' in text
    assert 'phi ' not in text and '@rk_slot' not in text


@pytest.mark.parametrize('lines', [
    ['FLDCW word ptr [ESI]', 'RET'], ['CALL 0x2000', 'RET'],
    ['JZ 0x2000', 'RET'], ['FLD ST0', 'RET'],
    ['FLD float ptr FS:[0x0]', 'RET'], ['TEST AL,0x1', 'RET'],
    ['RET 0x4'], ['FLD float ptr [ESI]'], ['MOV AX,0x1', 'RET'],
])
def test_function_frontend_rejects_unsupported_forms(lines):
    with pytest.raises(ValueError):
        emit_function('test', function_insns(lines), True)


def test_function_frontend_leaves_incoming_dependency_for_analysis():
    # The frontend faithfully emits it; recomp-x87-analyze rejects it.
    text = emit_function('test', function_insns(['FCHS', 'RET']), True)
    assert 'call double @rk_read' in text

@pytest.mark.parametrize('operation', ['FADD', 'FSUB', 'FMUL'])
def test_function_arithmetic_retains_round_at_each_machine_operation(operation):
    text = emit_function('test', function_insns([
        'FLD float ptr [ESP + 4]', f'{operation} float ptr [ESP + 8]',
        'FLD float ptr [ESP + 12]', 'FADDP ST1', 'RET',
    ]), True)
    assert text.count('@rk_round(') == 2
    assert text.count('@rk_pop(') == 1
    assert {'FADD': 'fadd', 'FSUB': 'fsub', 'FMUL': 'fmul'}[operation] + ' double' in text
    assert ' fast ' not in text


@pytest.mark.parametrize('instruction', ['FADDP ST0', 'FADDP ST2', 'FADDP ST1,ST0',
                                          'FMUL ST1', 'FADD dword ptr FS:[0x0]'])
def test_function_arithmetic_scope_is_explicit(instruction):
    with pytest.raises(ValueError):
        emit_function('test', function_insns([instruction, 'RET']), True)


def test_access_instrumentation_preserves_store_conversion_and_pop_order():
    from experiments.x87_llvm.instrument import instrument_memory
    text = instrument_memory('wrf32(a, fto_float(c, ST(c, 0)));\nfdrop(c);')
    assert text == 'rk_access_store32(c, a, fto_float(c, ST(c, 0)));\nfdrop(c);'
    with pytest.raises(ValueError):
        instrument_memory('wrf80(a, ST(c, 0));')
