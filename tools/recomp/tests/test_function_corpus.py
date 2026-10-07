"""Evidence parsing refuses incomplete checks and measurements."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from corpus.run import (parse_results, parse_coverage, symbol_sizes, reviewed_calls,
                        bind_reviewed_calls, review_boundaries, bind_boundary_calls,
                        wrap_string_helpers, wrap_string_helpers_ssa, boundary_wrappers,
                        ssa_call_symbols, validate_code_map_metadata, markdown,
                        corpus_modes, parse_ceiling, parse_ceiling_bench, ceiling_note, MODES)
from types import SimpleNamespace


def test_direct_calls_require_reviewed_rows_and_mapped_returns():
    row = {'address': '00100000', 'callees': ['00200000']}
    insns = [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['0x200000']),
             SimpleNamespace(addr=0x100005, mnem='RET', ops=[])]
    sizes = [5, 1]
    assert reviewed_calls(row, ['00100000', '00200000'], insns, sizes) == [0x100005]
    with pytest.raises(ValueError, match='reviewed corpus rows'):
        reviewed_calls(row, ['00100000'], insns, sizes)
    with pytest.raises(ValueError, match='undeclared/indirect'):
        reviewed_calls({'address': '00100000'}, ['00100000', '00200000'], insns, sizes)
    insns[0].ops = ['EAX']
    with pytest.raises(ValueError, match='undeclared/indirect'):
        reviewed_calls(row, ['00100000', '00200000'], insns, sizes)
    insns[0].ops = ['0x200000']
    with pytest.raises(ValueError, match='mapped continuation'):
        reviewed_calls(row, ['00100000', '00200000'], insns[:1], sizes[:1])
    with pytest.raises(ValueError, match='size unavailable'):
        reviewed_calls(row, ['00100000', '00200000'], insns)
    noncanonical = [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['0x200000']),
                    SimpleNamespace(addr=0x100006, mnem='RET', ops=[])]
    with pytest.raises(ValueError, match='non-canonical'):
        reviewed_calls(row, ['00100000', '00200000'], noncanonical, [5, 1])
    with pytest.raises(ValueError, match='differ from decoded'):
        reviewed_calls(row, ['00100000', '00200000'], insns[1:], sizes[1:])


def test_call_binding_preserves_mode_and_rejects_dispatch_or_extra_targets():
    assert bind_reviewed_calls('CALL_FN(00200000);', ['00200000'], 'combined') == \
        'combined_fn_00200000(c);'
    for body in ('CALL_FN(00300000);', 'recomp_call(c, target);',
                 'CALL_FN(00200000); recomp_jump(c, target);', ''):
        with pytest.raises(ValueError, match='unsupported emitted'):
            bind_reviewed_calls(body, ['00200000'], 'eager')


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


def test_code_map_metadata_must_pin_executable_and_image_base():
    cfg = {'game': {'sha256': 'a' * 64, 'image_base': 0x400000}}
    validate_code_map_metadata({'executable_sha256': 'a' * 64, 'image_base': '00400000'}, cfg)
    with pytest.raises(ValueError, match='executable hash'):
        validate_code_map_metadata({'executable_sha256': 'b' * 64, 'image_base': '00400000'}, cfg)
    with pytest.raises(ValueError, match='image base'):
        validate_code_map_metadata({'executable_sha256': 'a' * 64, 'image_base': '00401000'}, cfg)


def test_boundary_partition_requires_exact_decoded_calls_and_declared_sites():
    row = {'address': '00100000', 'callees': ['00200000'],
           'boundary_stubs': {'00300000': 'fixture_stub'},
           'indirect_calls': ['00100010'],
           'indirect_targets': {'00400000': 'fixture_indirect'}}
    insns = [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['0x200000']),
             SimpleNamespace(addr=0x100005, mnem='RET', ops=[]),
             SimpleNamespace(addr=0x10000a, mnem='CALL', ops=['0x300000']),
             SimpleNamespace(addr=0x10000f, mnem='NOP', ops=[]),
             SimpleNamespace(addr=0x100010, mnem='CALL', ops=['EAX']),
             SimpleNamespace(addr=0x100015, mnem='RET', ops=[])]
    sizes = [5, 1, 5, 1, 5, 1]
    direct, sites, targets, returns = review_boundaries(row, ['00100000', '00200000'], insns, sizes)
    assert direct == {'00200000': ('callee', None), '00300000': ('stub', 'fixture_stub')}
    assert sites == ['00100010']
    assert targets == {'00400000': 'fixture_indirect'}
    assert returns == {0x100005, 0x10000f, 0x100015}


def test_boundary_validation_rejects_bad_targets_sites_and_overlap():
    insns = [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['0x200000']),
             SimpleNamespace(addr=0x100005, mnem='RET', ops=[])]
    sizes = [5, 1]
    good = {'address': '00100000', 'callees': ['00200000']}
    assert review_boundaries(good, ['00100000', '00200000'], insns, sizes)[0] == {
        '00200000': ('callee', None)}
    bad_stub = {**good, 'boundary_stubs': {'00999999': 'stub'}}
    with pytest.raises(ValueError, match='partition decoded direct calls'):
        review_boundaries(bad_stub, ['00100000', '00200000'], insns, sizes)
    overlap = {**good, 'boundary_stubs': {'00200000': 'stub'}}
    with pytest.raises(ValueError, match='partition decoded direct calls'):
        review_boundaries(overlap, ['00100000', '00200000'], insns, sizes)
    bad_site = {**good, 'indirect_calls': ['00100009']}
    with pytest.raises(ValueError, match='indirect_calls differ'):
        review_boundaries(bad_site, ['00100000', '00200000'], insns, sizes)
    no_targets = {'address': '00100000', 'callees': [], 'indirect_calls': ['00100000']}
    with pytest.raises(ValueError, match='indirect_targets required'):
        review_boundaries(no_targets, ['00100000'],
                          [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['EAX']),
                           SimpleNamespace(addr=0x100005, mnem='RET', ops=[])], sizes)
    orphan_targets = {**good, 'indirect_targets': {'00400000': 'fixture_indirect'}}
    with pytest.raises(ValueError, match='without indirect calls'):
        review_boundaries(orphan_targets, ['00100000', '00200000'], insns, sizes)
    bad_symbol = {**good, 'boundary_stubs': {'00300000': '1bad'}}
    with pytest.raises(ValueError, match='ASCII C identifier'):
        review_boundaries(bad_symbol, ['00100000', '00200000'],
                          [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['0x200000']),
                           SimpleNamespace(addr=0x100005, mnem='RET', ops=[]),
                           SimpleNamespace(addr=0x10000a, mnem='CALL', ops=['0x300000']),
                           SimpleNamespace(addr=0x10000f, mnem='RET', ops=[])],
                          [5, 1, 5, 1])
    bad_indirect_symbol = {**good, 'indirect_calls': ['00100000'],
                           'indirect_targets': {'00400000': 'bad symbol'}}
    with pytest.raises(ValueError, match='ASCII C identifier'):
        review_boundaries(bad_indirect_symbol, ['00100000', '00200000'],
                          [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['EAX']),
                           SimpleNamespace(addr=0x100005, mnem='RET', ops=[])], sizes)
    noncanonical = {**good, 'indirect_calls': ['00100000'],
                    'indirect_targets': {'00400000': 'fixture_indirect'}}
    with pytest.raises(ValueError, match='non-canonical'):
        review_boundaries(noncanonical, ['00100000', '00200000'],
                          [SimpleNamespace(addr=0x100000, mnem='CALL', ops=['EAX']),
                           SimpleNamespace(addr=0x100006, mnem='CALL', ops=['0x200000']),
                           SimpleNamespace(addr=0x10000b, mnem='RET', ops=[])],
                          [5, 5, 1])


def test_ssa_call_symbols_keep_v1_callees_and_use_boundary_wrappers():
    assert ssa_call_symbols('combined', False, {}, ['00200000']) == {
        0x200000: 'combined_fn_00200000'}
    assert ssa_call_symbols('eager', True,
                            {'00200000': ('callee', None), '00300000': ('stub', 'stub')},
                            ['00200000']) == {
        0x200000: 'eager_fnwrap_00200000', 0x300000: 'eager_fnwrap_00300000'}


def test_boundary_binding_uses_wrappers_and_one_dispatch():
    direct = {'00200000': ('callee', None), '00300000': ('stub', 'fixture_stub')}
    body = ('CALL_FN(00200000); CALL_FN(00300000); uint32_t t_ = c->r[0]; '
            'c->r[4] -= 4; wr32(c->r[4], 0x00100015u); recomp_call(c, t_);')
    bound = bind_boundary_calls(body, 'eager', '00100000', direct, ['00100010'])
    assert 'eager_fnwrap_00200000(c)' in bound
    assert 'eager_fnwrap_00300000(c)' in bound
    assert 'eager_indirect_00100000(c, t_)' in bound
    assert 'recomp_call' not in bound
    with pytest.raises(ValueError, match='sites differ'):
        bind_boundary_calls('CALL_FN(00200000); CALL_FN(00300000);', 'eager', '00100000',
                            direct, ['00100010'])
    with pytest.raises(ValueError, match='direct calls differ'):
        bind_boundary_calls('CALL_FN(00200000);', 'eager', '00100000', direct, [])
    with pytest.raises(ValueError, match='unsupported emitted transfer'):
        bind_boundary_calls('CALL_FN(00200000); CALL_FN(00300000); recomp_jump(c, t_);',
                            'eager', '00100000', direct, ['00100010'])


def test_boundary_wrappers_call_corpus_boundary_and_fail_named():
    direct = {'00200000': ('callee', None), '00300000': ('stub', 'fixture_stub')}
    source = boundary_wrappers('combined', '00100000', direct, ['00100010'],
                               {'00400000': 'fixture_indirect'})
    assert 'corpus_boundary(c, 0x00200000u);' in source
    assert 'combined_fn_00200000(c);' in source
    assert 'fixture_stub(c);' in source
    assert 'combined_indirect_00100000' in source
    assert 'unbound indirect target' in source


def test_string_helpers_wrap_by_instruction_site_and_never_go_unattributed():
    body = ('movsd(c);  /* 00123456 MOVSD */\n'
            'rep_movsd(c);  /* 00123460 REP MOVSD */')
    wrapped = wrap_string_helpers(body)
    assert 'CORPUS_MOVSD(c, 0x00123456u)' in wrapped
    assert 'CORPUS_REP_MOVSD(c, 0x00123460u)' in wrapped
    assert 'rep_CORPUS' not in wrapped
    with pytest.raises(ValueError, match='without an instruction site'):
        wrap_string_helpers('movsd(c);')


def test_ssa_string_helpers_wrap_from_block_index():
    insns = [SimpleNamespace(addr=0x100000 + 5 * i, mnem='NOP', ops=[]) for i in range(5)]
    body = 'B3:;\n{ movsd(c); }\nB4:;\n{ rep_movsd(c); }'
    wrapped = wrap_string_helpers_ssa(body, insns)
    assert 'CORPUS_MOVSD(c, 0x0010000fu)' in wrapped
    assert 'CORPUS_REP_MOVSD(c, 0x00100014u)' in wrapped
    with pytest.raises(ValueError, match='without an instruction site'):
        wrap_string_helpers_ssa('movsd(c);', insns)
    with pytest.raises(ValueError, match='outside decoded instructions'):
        wrap_string_helpers_ssa('B9:;\nmovsd(c);', insns)


def test_coverage_requires_exactly_one_valid_object_for_required_rows():
    output = 'COVERAGE 7 {"paths":2,"indirect":1}\n'
    assert parse_coverage(output, [7, 8], [0]) == {0: {'paths': 2, 'indirect': 1}}
    with pytest.raises(ValueError, match='missing coverage'):
        parse_coverage(output, [7, 8], [0, 1])
    with pytest.raises(ValueError, match='duplicate coverage'):
        parse_coverage(output + output, [7], [0])
    with pytest.raises(ValueError, match='unknown fixture'):
        parse_coverage('COVERAGE 9 {"paths":1}\n', [7], [])
    with pytest.raises(ValueError, match='nonnegative integer'):
        parse_coverage('COVERAGE 7 {"paths":-1}\n', [7], [0])
    with pytest.raises(ValueError, match='nonnegative integer'):
        parse_coverage('COVERAGE 7 {"paths":true}\n', [7], [0])


def test_variable_mode_trials_reject_incomplete_rows_but_keep_defaults():
    output = 'CHECK 0 16\n' + ''.join(f'TIME 0 {m} {t} {m+t+1}.0\n'
                                      for m in range(4) for t in range(3))
    assert len(parse_results(output, 1, 16, 100, 3, row_modes=[4], row_has_native=[False])) == 12
    with pytest.raises(ValueError, match='incomplete benchmark'):
        parse_results(output.rsplit('TIME', 1)[0], 1, 16, 100, 3,
                      row_modes=[4], row_has_native=[False])
    mixed = ('CHECK 0 16\nCHECK 1 16\n'
             + ''.join(f'TIME 0 {m} {t} {m+t+1}.0\n' for m in range(4) for t in range(3))
             + ''.join(f'TIME 1 {m} {t} {m+t+1}.5\n' for m in range(6) for t in range(3)))
    times = parse_results(mixed, 2, 16, 100, 3,
                          row_modes=[4, 5], row_has_native=[False, True])
    assert len(times) == 30
    with pytest.raises(ValueError, match='incomplete benchmark'):
        parse_results(mixed, 2, 16, 100, 3, row_modes=[4, 5], row_has_native=[False, False])


def test_translation_report_exposes_control_timings_boundaries_and_coverage():
    row = {'address': '00100000', 'name': 'Workload', 'comparison': 'translation-only',
           'original_bytes': 16, 'x87_instructions': 2,
           'variants': {'eager': {'span_bytes': 64, 'timing_ns': {'median': 123.0}},
                        'combined': {'span_bytes': 32, 'timing_ns': {'median': 45.0}}},
           'boundary_stubs': {'00200000': 'fixture_stub'}, 'coverage': {'copies': 7}}
    result = markdown({'host': 'test', 'compiler': 'test', 'contract': 'mapped-comparison-corpus-v2',
                       'checks_per_function': 16, 'functions': [row]})
    assert '| 123.00 / 45.00 / — |' in result
    assert '| — / — |' in result
    assert '00200000->fixture_stub' in result
    assert 'copies=7' in result
    assert 'native-reference rows also matched' not in result


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


def test_ir_ssa_x87_mode_is_validated_before_any_game_work(tmp_path):
    import json
    from corpus.run import run_corpus
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'contract': 'mapped-native-corpus-v1', 'functions': []}))
    (tmp_path / 'build').mkdir()
    with pytest.raises(ValueError, match='x87 mode must be'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1,
                   ir_ssa=True, ir_ssa_x87='bogus')
    with pytest.raises(ValueError, match='requires IR SSA'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1,
                   ir_ssa=False, ir_ssa_x87='values')
    with pytest.raises(ValueError, match='requires IR SSA'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1,
                   ir_ssa=False, ir_ssa_state='locals')
    with pytest.raises(ValueError, match='state policy must be'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1,
                   ir_ssa=True, ir_ssa_state='bogus')


def test_ceiling_mode_precedes_native_and_default_order_is_unchanged():
    assert corpus_modes() == MODES == ('eager', 'cpu', 'x87', 'combined', 'native')
    assert corpus_modes(True) == ('eager', 'cpu', 'x87', 'combined', 'ceiling', 'native')


def test_ceiling_option_is_validated_before_any_game_work(tmp_path):
    import json
    from corpus.run import run_corpus
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'contract': 'mapped-native-corpus-v1', 'functions': []}))
    (tmp_path / 'build').mkdir()
    for kwargs in ({}, {'ir_ssa': True}, {'ir_ssa': True, 'ir_ssa_x87': 'scalar'},
                   {'ir_ssa': True, 'ir_ssa_x87': 'scalar', 'ir_ssa_state': 'strict'}):
        with pytest.raises(ValueError, match='ceiling requires --ir-ssa with scalar x87 and locals'):
            run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1, ir_ssa_ceiling='A', **kwargs)
    with pytest.raises(ValueError, match='unknown ceiling relaxation Q'):
        run_corpus(p, tmp_path, tmp_path / 'build/corpus', 'unused', 1, ir_ssa=True,
                   ir_ssa_x87='scalar', ir_ssa_state='locals', ir_ssa_ceiling='A,Q')


def test_ceiling_records_require_one_exact_row_each():
    good = ('CEILING 0 4096 0 0 0 0 0 0 -1 \n'
            'CEILING 1 256 3840 0 2 0 0 5 17 EAX differs\n')
    records = parse_ceiling(good, 2)
    assert records[0]['checked'] == 4096 and records[0]['first_input'] == -1 and records[0]['reason'] == ''
    assert records[1]['skipped'] == 3840 and records[1]['memory'] == 2 and records[1]['reason'] == 'EAX differs'
    for bad, match in (('CEILING 0 1 0 0 0 0 0 0 -1\n', 'missing'),
                       (good + 'CEILING 1 1 0 0 0 0 0 0 -1\n', 'duplicate'),
                       ('CEILING 2 1 0 0 0 0 0 0 -1\nCEILING 0 1 0 0 0 0 0 0 -1\n', 'unknown'),
                       ('CEILING 0 1 0 0 0 0 0\n', 'malformed'),
                       ('CEILING 0 x 0 0 0 0 0 0 -1\n', 'malformed'),
                       ('CEILING 0 -1 0 0 0 0 0 0 0\nCEILING 1 1 0 0 0 0 0 0 -1\n', 'negative')):
        with pytest.raises(ValueError, match=match):
            parse_ceiling(bad, 2)


def test_ceiling_bench_records_name_failed_timing_sanity():
    assert parse_ceiling_bench('CEILING_BENCH 0 1\nCEILING_BENCH 1 0\n', 2) == {1}
    assert parse_ceiling_bench('', 2) == set()
    with pytest.raises(ValueError, match='incomplete'):
        parse_ceiling_bench('CEILING_BENCH 0 1\n', 2)


def ceiling_row(address, mismatches=0, emitted=True):
    observation = {'checked': 256, 'skipped': 3840, 'observation': 0, 'memory': mismatches,
                   'eax': 0, 'st0': 0, 'boundary': 0, 'first_input': 3 if mismatches else -1,
                   'reason': 'guest memory differs' if mismatches else '',
                   'mismatches': mismatches, 'status': 'mismatch' if mismatches else 'pass'}
    return {'address': address, 'name': 'Workload ' + address, 'comparison': 'translation-only',
            'original_bytes': 16, 'x87_instructions': 2,
            'variants': {'eager': {'span_bytes': 64, 'timing_ns': {'median': 123.0}},
                         'combined': {'span_bytes': 32, 'timing_ns': {'median': 45.0}},
                         'ceiling': {'span_bytes': 20, 'timing_ns': {'median': 30.0}}},
            'ceiling': {'emitted': emitted, 'reason': None if emitted else 'ceiling x87: unsupported FLDCW',
                        'fallback': None if emitted else 'ssa-scalar-locals', 'relaxations': ['A', 'B'],
                        'timing_sanity': 'pass', 'observation': observation}}


def test_report_lists_relaxations_status_and_observation_outcome():
    rows = [ceiling_row('00100000'), ceiling_row('00100100', mismatches=2, emitted=False)]
    result = markdown({'host': 'test', 'compiler': 'test', 'contract': 'mapped-comparison-corpus-v2',
                       'checks_per_function': 16, 'functions': rows,
                       'ir_ssa_ceiling': {'enabled': True, 'relaxations': ['A', 'B'],
                                          'label': 'SSA ceiling [A,B]'}})
    assert '## SSA ceiling [A,B] (UNPROVEN, corpus-only)' in result
    assert '| 45.00 | 30.00 | — | 32 | 20 | emitted | pass (256 checked, 3840 skipped) |' in result
    assert 'fallback (ssa-scalar-locals): ceiling x87: unsupported FLDCW' in result
    assert 'MISMATCH 2' in result and 'first input 3: guest memory differs' in result
    assert 'experimental ceiling column is judged separately' in result
    assert 'relax=A,B; emitted; observation=pass (checked 256, skipped 3840, mismatches 0)' == ceiling_note(rows[0])
    assert ceiling_note({'variants': {}}) == ''
