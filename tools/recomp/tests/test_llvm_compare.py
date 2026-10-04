"""Focused refusal and provenance checks for build-only translator integration."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
from llvm_compare import read_manifest, validate_listing, emit_comparison
from llvm_compare_build import compile_settings, cmake_quote, function_sizes
from test_translate_driver import synthetic_image


@pytest.mark.parametrize('calls,synchronize,resumable,intrinsic,valid', [
    ([], True, False, False, False),
    (['0x2000'], False, False, False, False),
    (['0x2000'], True, True, False, False),
    (['0x2000'], True, False, True, False),
    (['0x2000', '0x3000'], True, False, False, False),
    (['0x2000'], True, False, False, True),
])
def test_calls_require_exact_declared_ordinary_boundaries(monkeypatch, calls, synchronize, resumable, intrinsic, valid):
    from llvm_compare import validate_calls
    fn = SimpleNamespace(insns=T.parse_listing_text('00001000  CALL 0x2000'))
    monkeypatch.setattr(T, 'RESUMABLE_STACKS', resumable)
    monkeypatch.setattr(T, 'INTRINSIC_BODY', {0x2000: ''} if intrinsic else {})
    p = {'call_targets': calls, 'synchronize_cfg': synchronize}
    if valid:
        assert validate_calls(T, fn, p) == ({0x2000}, True)
    else:
        with pytest.raises(ValueError):
            validate_calls(T, fn, p)


def fixture_function():
    # Handwritten x86 FLD [ESP+4]; FNSTSW AX; RET (no game bytes).
    code = bytes.fromhex('d9442404dfe0c3')
    image = synthetic_image({0x400100: code})
    image.md.detail = True
    insns = [image.to_insn(i) for i in image.md.disasm(code, 0x400100)]
    fn = T.Function(0x400100, 'test', len(code), insns)
    p = {'entry': '0x400100', 'size': len(code),
         'function_sha256': hashlib.sha256(code).hexdigest()}
    return image, fn, p


def test_listing_must_match_bytes_including_operands():
    image, fn, p = fixture_function()
    validate_listing(T, image, fn, p)
    fn.insns[0].ops = ['float ptr [ESP + 8]']
    with pytest.raises(ValueError, match='disagree'):
        validate_listing(T, image, fn, p)


def test_profile_extent_and_bytes_are_checked():
    image, fn, p = fixture_function()
    with pytest.raises(ValueError, match='extent'):
        validate_listing(T, image, fn, dict(p, size=8))
    with pytest.raises(ValueError, match='bytes differ'):
        validate_listing(T, image, fn, dict(p, function_sha256='0' * 64))


@pytest.mark.parametrize('manifest', [{}, {'contract': 'unknown', 'functions': ['a.json']},
                                      {'contract': 'mapped-normal-exit-v1', 'functions': []}])
def test_contract_is_explicit(tmp_path, manifest):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_manifest(path)


def test_no_output_below_production_tree(tmp_path):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'contract': 'mapped-normal-exit-v1', 'functions': ['leaf.json']}))
    gen = tmp_path / 'custom/gen'
    gen.mkdir(parents=True)
    (gen / 'table.c').write_text('original')
    with pytest.raises(ValueError, match='separate'):
        emit_comparison(T, None, SimpleNamespace(llvm_compare=manifest, game=tmp_path,
                                                 out=gen / 'comparison'))
    assert not (gen / 'comparison').exists()
    assert (gen / 'table.c').read_text() == 'original'


def command(extra=()):
    return {'file': '/src/body.c', 'arguments': ['/usr/bin/cc', '-I/runtime', '-O2', *extra,
                                                '-o', '/build/body.o', '-c', '/src/body.c']}


def test_production_flags_keep_order_and_drop_only_io():
    cc, flags = compile_settings(command(['-g', '-std=c11', '-DGUEST_SIZE=0x1000']))
    assert cc == '/usr/bin/cc'
    assert flags == ['-I/runtime', '-O2', '-g', '-std=c11', '-DGUEST_SIZE=0x1000']


@pytest.mark.parametrize('flag', ['-ffast-math', '-Ofast', '-O0', '-O3', '-flto',
                                  '-fsanitize=address', '@response', '-Irelative'])
def test_unsupported_build_settings_fail_instead_of_being_dropped(flag):
    with pytest.raises(ValueError):
        compile_settings(command([flag]))


def test_cross_architecture_is_rejected():
    with pytest.raises(ValueError, match='cross'):
        compile_settings(command(['-arch', 'unrecognized']))


def test_cmake_arguments_do_not_expand_variables_or_split_lists():
    assert cmake_quote('/space path/${name}') == '[=[/space path/${name}]=]'
    with pytest.raises(ValueError):
        cmake_quote('-Dvalue=a;b')


def test_macho_sizes_use_boundaries_not_zero_nm_sizes():
    asm = '\n'.join(f'{0x1000+i*8:x} <_{name}>:\n{0x1000+i*8:x}: ret\n'
                    for i, name in enumerate(('compare_c_native', 'compare_c_llvm',
                                              'compare_raw', 'compare_lifted', 'next')))
    result = function_sizes(asm)
    assert result['compare_lifted'] == {'span_bytes': 8, 'instructions': 1, 'fused_multiply_adds': 0}


@pytest.mark.parametrize('stale_body', [True, False])
def test_stale_production_source_or_runtime_is_refused(tmp_path, stale_body):
    from llvm_compare_build import production_settings
    out, runtime, gen = (tmp_path / name for name in ('compare', 'runtime', 'gen'))
    for directory in (out / '00400100', runtime, gen):
        directory.mkdir(parents=True)
    (out / '00400100/body.txt').write_text('void fn_00400100(X86 *c) {}')
    (runtime / 'x86.h').write_text('current runtime')
    (gen / 'x86.h').write_text('current runtime' if stale_body else 'stale runtime')
    chunk = gen / 'chunk_test.c'
    chunk.write_text('wrong body' if stale_body else 'void fn_00400100(X86 *c) {}')
    database = tmp_path / 'compile_commands.json'
    database.write_text(json.dumps([{
        'file': str(chunk), 'directory': str(tmp_path),
        'arguments': ['/usr/bin/cc', '-O2', '-c', str(chunk),
                      '-o', 'CMakeFiles/recomp_gen.dir/chunk_test.c.o']}]))
    with pytest.raises(ValueError, match='match production chunks|runtime header is stale'):
        production_settings(database, out, {'functions': {'00400100': {}}}, runtime)
    assert not (out / '00400100/settings.cmake').exists()
