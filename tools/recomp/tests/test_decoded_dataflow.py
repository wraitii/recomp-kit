"""Decoded flag effects, loops, observers and exact exits."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
from decoded_dataflow import decode, flag_liveness


def function(lines):
    insns = T.parse_listing_text('\n'.join(f'{0x100000+i:08x}  {s}' for i, s in enumerate(lines)))
    fn = T.Function(0x100000, 'decoded', len(insns), insns)
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=False, cpu_locals=True, x87_locals=True, x87_dataflow=True,
        decoded_dataflow=True))
    tr.prepare(fn)
    return tr, fn


def test_loads_and_audited_x87_are_transparent_but_callbacks_and_unknown_helpers_observe():
    for instruction in ('MOV EAX,dword ptr [ESI]', 'FLD float ptr [ESI]', 'FNSTSW AX'):
        e = decode(T.parse_listing_text('00100000  ' + instruction)[0])
        assert not e.observer and not e.flag_uses
    for instruction in ('CALL 0x00200000', 'MOV dword ptr [ESI],EAX',
                        'MOV EAX,FS:[ESI]', 'FLDCW word ptr [ESI]', 'DIV EAX', 'FXAM'):
        e = decode(T.parse_listing_text('00100000  ' + instruction)[0])
        assert e.observer and e.flag_uses == T.ALL_FLAGS


def test_runtime_af_survives_test_and_logic_and_reaches_return():
    tr, fn = function(['ADD EAX,ECX', 'TEST EDX,EDX', 'RET'])
    live = flag_liveness(tr, fn)
    assert live[0] == frozenset({'af'})
    assert live[1] == T.ALL_FLAGS
    # Conservative comparison liveness must retain the same preserved bit.
    assert 'af' in tr.liveness(fn)[0]


def test_overwritten_flags_cross_loads_and_x87_without_becoming_observers():
    tr, fn = function(['CMP EAX,ECX', 'MOV EDX,dword ptr [ESI]',
                       'FLD float ptr [EDI]', 'FSTP float ptr [EBX]',
                       'CMP EAX,EDX', 'RET'])
    live = flag_liveness(tr, fn)
    assert not live[0] and live[4] == T.ALL_FLAGS
    assert tr.liveness(fn)[0] == T.ALL_FLAGS


def test_calls_keep_complete_inputs_and_loops_keep_consumed_flags():
    tr, fn = function(['MOV EDX,3', 'CMP EAX,ECX', 'JZ 0x00100006',
                       'MOV ECX,dword ptr [ESI]', 'DEC EDX', 'JNZ 0x00100001',
                       'CALL 0x00200000', 'RET'])
    live = flag_liveness(tr, fn)
    assert live[1] == T.ALL_FLAGS  # early exit observes the complete CMP result
    assert 'zf' in live[4]


def test_decoded_emission_has_eager_null_flag_fallback():
    tr, fn = function(['ADD EAX,ECX', 'MOV EDX,dword ptr [ESI]', 'TEST EDX,EDX', 'RET'])
    body = '\n'.join(tr.translate(fn))
    assert '#if defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS' in body
    assert tr.stats['_decoded_dataflow_functions'] == 1
    from cpu_locals import after_initialization
    lines = after_initialization([line for line in body.splitlines()[1:] if line.strip()])
    assert lines[0].strip() == 'L_00100000: ;'


def test_analysis_budget_retains_conservative_fallback():
    from decoded_dataflow import MAX_INSTRUCTIONS
    tr, fn = function(['NOP'] * (MAX_INSTRUCTIONS + 1))
    assert flag_liveness(tr, fn) is None


def test_decoded_settings_require_existing_stages(tmp_path):
    text = (Path(__file__).resolve().parents[3] / 'games/stub/game.toml').read_text()
    for value, message in [('"yes"', 'must be a boolean'), ('true', 'requires x87_dataflow and cpu_locals')]:
        (tmp_path / 'game.toml').write_text(text.replace('[translate]', '[translate]\ndecoded_dataflow = ' + value))
        with pytest.raises(ValueError, match=message):
            T.game_config.load(tmp_path)
