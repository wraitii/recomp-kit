"""Evidence parsing refuses incomplete checks and measurements."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from corpus.run import parse_results, symbol_sizes


def test_linked_spans_include_padding_and_keep_helpers_separate():
    assembly = '''00001000 <_combined_fn_00100000>:
    1000: ldr x0, [sp, #8]
    1004: bl 0x1010 <_helper>
    1008: ret
    100c: nop
00001010 <_helper>:
    1010: fmadd s0, s1, s2, s3
    1014: ret
00001018 <_sentinel>:
'''
    sizes = symbol_sizes(assembly)
    assert sizes['combined_fn_00100000'] == dict(span_bytes=16, instructions=4,
                                                static_sp_accesses=1, fused_multiply_adds=0)
    assert sizes['helper']['span_bytes'] == 8
    assert sizes['helper']['fused_multiply_adds'] == 1


def test_check_only_still_requires_every_function():
    assert parse_results('CHECK 0 16\nCHECK 1 16\n', 2, 16, 0, 3) == {}
    with pytest.raises(ValueError, match='incomplete correctness'):
        parse_results('CHECK 0 16\n', 2, 16, 0, 3)
    with pytest.raises(ValueError, match='duplicate corpus'):
        parse_results('CHECK 0 16\nCHECK 0 16\n', 1, 16, 0, 3)


def test_repeated_local_helper_names_are_not_lost():
    assembly = '''00001000 <_OUTLINED_FUNCTION_1>:
    1000: ret
00001004 <_OUTLINED_FUNCTION_1>:
    1004: fmla v0.4s, v1.4s, v2.4s
    1008: ret
0000100c <_sentinel>:
'''
    sizes = symbol_sizes(assembly)
    assert sizes['OUTLINED_FUNCTION_1']['span_bytes'] == 4
    assert sizes['OUTLINED_FUNCTION_1@00001004']['span_bytes'] == 8
    assert sizes['OUTLINED_FUNCTION_1@00001004']['fused_multiply_adds'] == 1


def test_timings_require_all_rotating_trials_and_positive_values():
    output = 'CHECK 0 16\n' + ''.join(f'TIME 0 {m} {t} {m+t+1}.0\n'
                                      for m in range(6) for t in range(3))
    assert len(parse_results(output, 1, 16, 100, 3)) == 18
    with pytest.raises(ValueError, match='incomplete benchmark'):
        parse_results(output.rsplit('TIME', 1)[0], 1, 16, 100, 3)
    with pytest.raises(ValueError, match='duplicate or invalid'):
        parse_results(output.replace('TIME 0 0 0 1.0', 'TIME 0 0 0 0.0'), 1, 16, 100, 3)


def test_fixture_mismatch_fails_before_compilation(tmp_path):
    # Hash checks occur on original bytes before any selected code is compiled;
    # integration coverage lives in the game corpus, which requires private data.
    import json
    from corpus.run import run_corpus
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'contract': 'not-approved', 'functions': []}))
    (tmp_path / 'build').mkdir()
    with pytest.raises(ValueError, match='explicit mapped-native'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1)
