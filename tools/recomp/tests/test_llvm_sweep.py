"""Coverage must account for omissions and refusals without inventing execution."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
import llvm_sweep as sweep
from test_translate_driver import synthetic_image


def inputs(tmp_path, monkeypatch, code):
    addr = 0x400100
    image = synthetic_image({addr: code})
    image.md.detail = True
    insns = [image.to_insn(i) for i in image.md.disasm(code, addr)]
    monkeypatch.setattr(T, 'LISTINGS', str(tmp_path))
    (tmp_path / f'{addr:08x}.asm').write_text('\n'.join(i.raw for i in insns))
    return image, addr, SimpleNamespace(eager_flags=False)


def test_missing_rows_are_counted_and_duplicates_fail(tmp_path, monkeypatch):
    path = tmp_path / 'functions.tsv'
    monkeypatch.setattr(T, 'FUNCS_TSV', str(path))
    path.write_text('address\tname\tbytes\n00400100\tmissing\t1\n')
    assert sweep.census(T) == [(0x400100, 'missing', 1)]
    monkeypatch.setattr(T, 'LISTINGS', str(tmp_path))
    row, module, body = sweep.candidate(T, None, None, 0x400100, 'missing', 1, None)
    assert row['stage'] == 'listing' and row['reason'] == 'missing or empty listing'
    assert module is body is None
    path.write_text(path.read_text() + '00400100\tduplicate\t1\n')
    with pytest.raises(ValueError, match='duplicate'):
        sweep.census(T)


@pytest.mark.parametrize('code,stage,reason', [
    ('90c3', 'emission', 'unsupported function instruction'), # NOP: no quiet dropping
    ('e8fb000000c3', 'contract', 'exact explicit list'), # no guessed callee contract
    ('d9e0c3', 'lifting', 'byte-verified'), # emission succeeds; incoming read fails in LLVM
])
def test_first_refusal_has_stage_and_evidence(tmp_path, monkeypatch, code, stage, reason):
    image, addr, args = inputs(tmp_path, monkeypatch, bytes.fromhex(code))
    row, module, body = sweep.candidate(T, image, args, addr, 'test', len(bytes.fromhex(code)), None)
    assert row['stage'] == stage and reason in row['reason']
    assert bool(module) == (stage == 'lifting')


def test_listing_disagreement_and_rewrites_are_not_supported(tmp_path, monkeypatch):
    image, addr, args = inputs(tmp_path, monkeypatch, bytes.fromhex('b801000000c3'))
    listing = tmp_path / f'{addr:08x}.asm'
    original = listing.read_text()
    listing.write_text(original.replace('0x1', '0x2'))
    row, module, _ = sweep.candidate(T, image, args, addr, 'test', 6, None)
    assert row['stage'] == 'listing' and 'disagree' in row['reason']
    listing.write_text(original)
    monkeypatch.setattr(T, 'OPERAND_REDIRECTS', {addr: (1, 2)})
    row, module, _ = sweep.candidate(T, image, args, addr, 'test', 6, None)
    assert row['stage'] == 'contract' and 'rewrites' in row['reason']


@pytest.mark.parametrize('exitcode,stderr,expected', [
    (1, 'error: x87 incoming stack dependency\n', 'rejected'),
    (-6, 'crash', None),
    (1, 'missing plugin', None),
])
def test_tool_failures_are_not_refusals(tmp_path, monkeypatch, exitcode, stderr, expected):
    report = {'execution': 'not run', 'functions': [
        {'address': '00400100', 'status': 'emitted', 'stage': 'lifting', 'reason': 'emitted'}]}
    (tmp_path / '00400100').mkdir()
    sweep.write_report(tmp_path, report)
    monkeypatch.setattr(sweep.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=exitcode, stderr=stderr))
    monkeypatch.setattr(sweep.subprocess, 'check_output', lambda *a, **kw: 'LLVM version 22\n')
    if expected:
        sweep.lift_candidates(tmp_path, 'opt', 'plugin')
        result = json.loads((tmp_path / 'coverage.json').read_text())
        assert result['summary'] == {'rejected': 1}
        assert result['execution'] == 'not run'
        assert result['functions'][0]['reason'] == stderr.strip()
    else:
        with pytest.raises(RuntimeError, match='unexpected opt failure'):
            sweep.lift_candidates(tmp_path, 'opt', 'plugin')


def test_publication_guard_precedes_output(tmp_path):
    out = tmp_path / 'build/recomp/gen/subdir'
    args = SimpleNamespace(out=out, game=tmp_path)
    with pytest.raises(ValueError, match='separate'):
        sweep.emit_sweep(T, None, args)
    assert not out.exists()


def test_successful_lifting_checks_body_without_rejecting_declarations(tmp_path, monkeypatch):
    report = {'execution': 'not run', 'functions': [
        {'address': '00400100', 'status': 'emitted', 'stage': 'lifting', 'reason': 'emitted',
         'x87_instructions': 1}]}
    directory = tmp_path / '00400100'
    directory.mkdir()
    (directory / 'lifted.ll').write_text('define void @sweep(ptr %cpu) {\n  ret void\n}\n'
                                        'declare void @rk_snapshot(ptr)\n')
    sweep.write_report(tmp_path, report)
    monkeypatch.setattr(sweep.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stderr=''))
    monkeypatch.setattr(sweep.subprocess, 'check_output', lambda *a, **kw: 'LLVM version 22\n')
    sweep.lift_candidates(tmp_path, 'opt', 'plugin')
    result = json.loads((tmp_path / 'coverage.json').read_text())
    assert result['summary'] == result['x87_summary'] == {'supported': 1}
    assert result['execution'] == 'not run'


def test_object_spans_handle_aliases_last_symbol_and_separate_data():
    symbols = '0000 T _compare_raw\n0000 t ltmp0\n0008 T _compare_lifted\n'
    disasm = ('  0 __text 00000010 00000000 TEXT\n'
              '  1 __literal8 00000008 00000010 DATA\n'
              '0000 <ltmp0>:\n 0: ret\n 4: nop\n'
              '0008 <_compare_lifted>:\n 8: fmadd\n c: ret\n')
    sizes = sweep.object_sizes(symbols, disasm)
    assert sizes['compare_raw'] == {'span_bytes': 8, 'instructions': 2, 'fused_multiply_adds': 0}
    assert sizes['compare_lifted'] == {'span_bytes': 8, 'instructions': 2, 'fused_multiply_adds': 1}
    with pytest.raises(ValueError, match='one text section'):
        sweep.object_sizes(symbols, disasm + ' 2 .text.cold 00000004 00000000 TEXT\n')
