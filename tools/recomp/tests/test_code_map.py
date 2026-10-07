"""Public code maps decode private bytes without requiring Ghidra."""
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import capstone
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import code_map as M
import translate as T

IMAGE_CLASS = T.Image


def image(raw):
    result = IMAGE_CLASS.__new__(IMAGE_CLASS)
    result.base = 0x401000
    result.end = result.base + len(raw)
    result.data = raw
    result.exec_ranges = [(result.base, result.end, '.text')]
    result.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    return result


def fixture(tmp_path, raw=b'\x90\xc3', lengths='11'):
    root = tmp_path / 'map'
    root.mkdir()
    exe = tmp_path / 'private.exe'
    exe.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (root / 'metadata.txt').write_text(
        f'format={M.FORMAT}\nstatus=complete\nexecutable_sha256={digest}\n'
        f'image_base=00401000\nfunctions=1\ninstructions={len(lengths)}\n')
    (root / 'functions.tsv').write_text('address\tname\tbytes\n00401000\tTest\t2\n')
    (root / 'instruction_map.tsv').write_text('function\tstart\tlengths\n00401000\t00401000\t' + lengths + '\n')
    return {'code_map_path': root, 'listings_path': tmp_path / 'listings',
            'developer_exe_path': exe, 'game': {'sha256': digest, 'image_base': 0x401000}}


def test_disconnected_ranges_do_not_decode_embedded_data():
    img = image(b'\x90\xff\xff\xff\xc3')
    assert [i.mnem for i in M.decode_span(img, 0x401000, '1')] == ['NOP']
    assert [i.mnem for i in M.decode_span(img, 0x401004, '1')] == ['RET']


def test_folded_wait_and_structured_x87_operands():
    img = image(bytes.fromhex('9bdd7108d930d920'))
    instructions = list(M.decode_span(img, img.base, '422'))
    assert [(i.addr, i.mnem, i.ops) for i in instructions] == [
        (img.base, 'FSAVE', ['[ECX + 0x8]']),
        (img.base + 4, 'FNSTENV', ['[EAX]']),
        (img.base + 6, 'FLDENV', ['[EAX]'])]
    assert img.md.detail is False


def test_standalone_wait_is_preserved():
    assert [i.mnem for i in M.decode_span(image(bytes.fromhex('9b90')), 0x401000, '11')] == ['WAIT', 'NOP']


def test_listing_aliases_preserve_existing_discovery_rules():
    img = image(bytes.fromhex('d791'))
    mapped = list(M.decode_span(img, img.base, '11'))
    assert [(i.mnem, i.ops) for i in mapped] == [('XLAT', []), ('XCHG', ['EAX', 'ECX'])]
    # The migration does not change speculative recovery's existing acceptance.
    assert img.instruction_at(img.base).mnem == 'XLATB'


def test_instruction_crossing_executable_section_fails():
    img = image(bytes.fromhex('b801000000'))
    img.exec_ranges = [(img.base, img.base + 2, '.text')]
    with pytest.raises(ValueError, match='outside executable'):
        list(M.decode_span(img, img.base, '5'))


@pytest.mark.parametrize('raw,lengths', [(b'\x90\xc3', '2'), (b'\xe8', '1'), (b'\x90', '2')])
def test_decoder_boundary_disagreement_fails(raw, lengths):
    img = image(raw)
    with pytest.raises(ValueError):
        list(M.decode_span(img, img.base, lengths))
    assert img.md.detail is False


@pytest.mark.parametrize('row', [
    '00401000\t00401000\t0', '00401000\t00401000\tg',
    '00402000\t00401000\t11',
    '00401000\t00401000\t1\n00401000\t00401000\t1'])
def test_invalid_map_spans_fail(tmp_path, row):
    cfg = fixture(tmp_path)
    (cfg['code_map_path'] / 'instruction_map.tsv').write_text('function\tstart\tlengths\n' + row + '\n')
    with pytest.raises(ValueError):
        M.read_map(cfg['code_map_path'])


def test_hash_rejection_leaves_existing_inputs_intact(tmp_path):
    cfg = fixture(tmp_path)
    cfg['developer_exe_path'].write_bytes(b'wrong version')
    cfg['listings_path'].mkdir()
    sentinel = cfg['listings_path'] / 'keep'
    sentinel.write_text('original')
    with pytest.raises(ValueError, match='SHA-256'):
        M.ensure_listings(cfg)
    assert sentinel.read_text() == 'original'


def test_decode_failure_does_not_publish_partial_listings(tmp_path, monkeypatch):
    cfg = fixture(tmp_path, b'\xe8', '1')
    monkeypatch.setattr(T, 'Image', lambda _: image(b'\xe8'))
    with pytest.raises(ValueError, match='boundary'):
        M.ensure_listings(cfg)
    assert not cfg['listings_path'].exists()
    assert not list(tmp_path.glob('.code-map-*'))


def test_cache_reuse_and_repair_of_deleted_or_modified_listings(tmp_path, monkeypatch):
    cfg = fixture(tmp_path)
    reads = []
    def load(_):
        reads.append(1)
        return image(b'\x90\xc3')
    monkeypatch.setattr(T, 'Image', load)
    M.ensure_listings(cfg)
    listing = cfg['listings_path'] / 'functions/00401000.asm'
    original = listing.read_text()
    assert original == '00401000  NOP\n00401001  RET\n'
    M.ensure_listings(cfg)
    assert len(reads) == 1
    listing.write_text('corrupted')
    M.ensure_listings(cfg)
    assert listing.read_text() == original
    listing.unlink()
    M.ensure_listings(cfg)
    assert listing.read_text() == original
    assert len(reads) == 3


def test_non_cache_export_is_never_overwritten(tmp_path):
    cfg = fixture(tmp_path)
    cfg['listings_path'].mkdir()
    (cfg['listings_path'] / 'functions.tsv').write_text('research export')
    with pytest.raises(ValueError, match='non-cache'):
        M.ensure_listings(cfg)


def test_legacy_games_need_no_map_or_executable():
    M.ensure_listings({})


@pytest.mark.parametrize('raw,mnem', [('dff1', 'FCOMIP'), ('dfe9', 'FUCOMIP')])
def test_popping_compare_aliases_reach_existing_x87_lowering(raw, mnem):
    img = image(bytes.fromhex(raw))
    ins = list(M.decode_span(img, img.base, '2'))[0]
    assert (ins.mnem, ins.ops) == (mnem, ['ST1'])
    # Speculative recovery and mapped decoding share the same adapter.
    assert img.instruction_at(img.base).mnem == mnem
    tr = T.Translator(img, set(), SimpleNamespace())
    emitted = tr.emit_x87(None, ins, ins.mnem,
                          [T.parse_operand(op) for op in ins.ops])
    assert any(('fucomi(' if mnem == 'FUCOMIP' else 'fcomi(') in line
               for line in emitted)
    assert 'fdrop(c);' in ' '.join(emitted)
