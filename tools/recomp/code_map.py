"""Decode public instruction-location metadata using the owner's private PE.

A map holds function spans (start and byte count) and, only where a linear
Capstone decode disagrees with the analysis boundaries, explicit instruction
lengths. The resulting assembly listings are local build inputs, never
publication artifacts.

    code_map.py --pack V1_DIR --exe GAME.EXE --out MAP_DIR
"""
import argparse
from pathlib import Path
import hashlib
import json
import shutil
import sys
import tempfile

MAP_FILES = ('metadata.txt', 'spans.tsv')
FORMAT = 'recomp-code-map-v2'
SPANS_HEADER = 'function\tstart\tbytes\tlengths'


def read_map(root):
    """Validate a map before touching its generated listing directory.

    Returns metadata and {function: [(start, bytes, lengths or '')]}."""
    root = Path(root)
    metadata = dict(line.split('=', 1) for line in (root / 'metadata.txt').read_text().splitlines())
    if metadata.get('format') != FORMAT or metadata.get('status') != 'complete':
        raise ValueError('unsupported or incomplete code map')
    rows = (root / 'spans.tsv').read_text().splitlines()
    if not rows or rows[0] != SPANS_HEADER:
        raise ValueError('invalid code-map span header')
    functions = {}
    ends = {}
    for row in rows[1:]:
        function, start, size, lengths = row.split('\t')
        addr, size = int(start, 16), int(size)
        owner = int(function, 16) if function else addr
        if size <= 0 or not 0 <= addr or addr + size > 0x100000000:
            raise ValueError('invalid code-map span at %08x' % addr)
        if any(c not in '123456789abcdef' for c in lengths) or (
                lengths and sum(int(c, 16) for c in lengths) != size):
            raise ValueError('invalid code-map lengths at %08x' % addr)
        if addr < ends.get(owner, 0):
            raise ValueError('overlapping or unordered code-map spans')
        ends[owner] = addr + size
        functions.setdefault(owner, []).append((addr, size, lengths))
    if int(metadata['functions']) != len(functions):
        raise ValueError('code-map census mismatch')
    return metadata, functions


def linear_lengths(image, start, size):
    """Capstone's instruction lengths across a span, or None where it cannot tile it exactly."""
    raw = image.data[start - image.base:start - image.base + size]
    lengths = [insn.size for insn in image.md.disasm(raw, start)]
    if sum(lengths) != size or any(n > 15 for n in lengths):
        return None
    return ''.join('%x' % n for n in lengths)


def pack(v1_root, executable, out):
    """Convert a v1 export (names, sizes and every instruction length) into a v2 map."""
    sys.path.insert(0, str(Path(__file__).parent))
    from translate import Image
    v1_root, out = Path(v1_root), Path(out)
    metadata = dict(line.split('=', 1) for line in (v1_root / 'metadata.txt').read_text().splitlines())
    if metadata.get('format') != 'recomp-code-map-v1' or metadata.get('status') != 'complete':
        raise ValueError('expected a complete recomp-code-map-v1 export')
    image = Image(Path(executable))
    if hashlib.sha256(Path(executable).read_bytes()).hexdigest() != metadata['executable_sha256']:
        raise ValueError('executable does not match the export')
    rows = [SPANS_HEADER]
    explicit = 0
    for row in (v1_root / 'instruction_map.tsv').read_text().splitlines()[1:]:
        function, start, lengths = row.split('\t')
        addr = int(start, 16)
        size = sum(int(c, 16) for c in lengths)
        keep = '' if linear_lengths(image, addr, size) == lengths else lengths
        explicit += bool(keep)
        rows.append('%s\t%s\t%d\t%s' % ('' if function == start else function, start, size, keep))
    out.mkdir(parents=True, exist_ok=True)
    metadata['format'] = FORMAT
    (out / 'metadata.txt').write_text(''.join('%s=%s\n' % item for item in metadata.items()))
    (out / 'spans.tsv').write_text('\n'.join(rows) + '\n')
    print('Packed %d spans (%d with explicit lengths) into %s' % (len(rows) - 1, explicit, out))


def expand(image, functions):
    """{function: (name, bytes, [(start, lengths)])} with every span's lengths filled in."""
    expanded = {}
    for owner, spans in functions.items():
        filled = []
        for start, size, lengths in spans:
            if not lengths:
                if not image.base <= start < start + size <= image.end:
                    raise ValueError('code-map span outside executable image: %08x' % start)
                lengths = linear_lengths(image, start, size)
                if lengths is None:
                    raise ValueError('decoder cannot tile code-map span at %08x' % start)
            filled.append((start, lengths))
        expanded[owner] = ('FUN_%08x' % owner, sum(span[1] for span in spans), filled)
    return expanded


def decode_span(image, start, lengths):
    """Decode exactly the mapped boundaries, including Ghidra's folded WAIT."""
    if image.md is None:
        raise ValueError('code-map decoding requires capstone')
    detail = image.md.detail
    image.md.detail = True
    try:
        addr = start
        for digit in lengths:
            size = int(digit, 16)
            if not image.base <= addr < addr + size <= image.end:
                raise ValueError('code-map instruction outside executable image: %08x' % addr)
            raw = image.data[addr - image.base:addr - image.base + size]
            decoded = list(image.md.disasm(raw, addr))
            if len(decoded) == 1 and decoded[0].size == size:
                insn = image.to_insn(decoded[0])
            elif (len(decoded) == 2 and decoded[0].mnemonic in ('wait', 'fwait')
                  and decoded[1].mnemonic.startswith('fn')
                  and sum(ci.size for ci in decoded) == size):
                insn = image.to_insn(decoded[1])
                insn.addr = addr
                insn.mnem = 'F' + insn.mnem[2:]
                insn.raw = '%08x  %s%s' % (addr, insn.mnem,
                                         ' ' + ','.join(insn.ops) if insn.ops else '')
            else:
                raise ValueError('decoder disagrees with code-map boundary at %08x (length %d)' % (addr, size))
            # Listing spellings are an adapter concern: keep discovery's existing
            # acceptance rules unchanged while replacing exported instruction text.
            if insn.mnem == 'XLATB':
                insn.mnem = 'XLAT'
            # The short XCHG opcode has an implicit accumulator. Ghidra prints
            # that operand first; Capstone prints it second. Canonicalize it so
            # the emitted C stays byte-for-byte identical apart from comments.
            if insn.mnem == 'XCHG' and 0x90 <= raw[0] <= 0x97 and len(insn.ops) == 2:
                insn.ops.reverse()
            insn.raw = '%08x  %s%s%s' % (addr, insn.mnem,
                '.' + insn.rep if insn.rep else '',
                ' ' + ','.join(insn.ops) if insn.ops else '')
            yield insn
            addr += size
    finally:
        image.md.detail = detail


def ensure_listings(cfg):
    """Verify the private executable and atomically materialize cached listings.

    Refuse to overwrite a directory not owned by this cache. A failed decode
    leaves the previous complete cache intact. Hashes detect edited/deleted files.
    """
    root = cfg.get('code_map_path')
    if root is None:
        return
    root, destination = Path(root), Path(cfg['listings_path'])
    if root == destination or root in destination.parents or destination in root.parents:
        raise ValueError('code map and generated listings must be separate directories')
    metadata, functions = read_map(root)
    # A game may temporarily keep functions the translator cannot lower yet
    # ([translate] skip_functions). They are dropped from the generated listings
    # and so from translation; reaching one at run time is a fault until the
    # corresponding lowering lands. Addresses are guest VAs.
    skip = frozenset(int(a) for a in cfg.get('translate', {}).get('skip_functions', ()))
    if skip:
        functions = {addr: value for addr, value in functions.items() if addr not in skip}
    executable = Path(cfg['developer_exe_path'])
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    if sha != cfg['game']['sha256'] or sha != metadata['executable_sha256']:
        raise ValueError('code-map executable SHA-256 mismatch')
    if int(metadata['image_base'], 16) != cfg['game']['image_base']:
        raise ValueError('code-map image base mismatch')
    import capstone
    digest = hashlib.sha256()
    digest.update(sha.encode() + capstone.__version__.encode())
    for name in MAP_FILES:
        digest.update(name.encode() + (root / name).read_bytes())
    for name in ('code_map.py', 'translate.py'):
        digest.update(Path(__file__).with_name(name).read_bytes())
    key = digest.hexdigest()
    stamp = destination / '.code-map-cache.json'
    if stamp.is_file():
        try:
            cache = json.loads(stamp.read_text())
            expected_files = {'functions.tsv'} | {'functions/%08x.asm' % addr for addr in functions}
            if cache['key'] == key and set(cache['files']) == expected_files and all(
                    (destination / name).is_file() and
                    hashlib.sha256((destination / name).read_bytes()).hexdigest() == expected
                    for name, expected in cache['files'].items()):
                return
        except (ValueError, KeyError, TypeError):
            pass
    elif destination.exists() and any(destination.iterdir()):
        raise ValueError('refusing to overwrite non-cache listings: %s' % destination)
    from translate import Image
    image = Image(executable)
    if image.base != cfg['game']['image_base']:
        raise ValueError('executable image base differs from code map')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.code-map-', dir=destination.parent))
    try:
        (stage / 'functions').mkdir()
        functions = expand(image, functions)
        (stage / 'functions.tsv').write_text('address\tname\tbytes\n' + ''.join(
            '%08x\t%s\t%d\n' % (addr, name, size) for addr, (name, size, _) in sorted(functions.items())))
        hashes = {'functions.tsv': hashlib.sha256((stage / 'functions.tsv').read_bytes()).hexdigest()}
        count = 0
        for addr, (_, _, spans) in functions.items():
            lines = [insn.raw for start, lengths in spans for insn in decode_span(image, start, lengths)]
            count += len(lines)
            name = 'functions/%08x.asm' % addr
            data = ('\n'.join(lines) + ('\n' if lines else '')).encode()
            (stage / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        if not skip and count != int(metadata['instructions']):
            raise ValueError('code-map census mismatch: decoded %d instructions, map says %s'
                             % (count, metadata['instructions']))
        (stage / stamp.name).write_text(json.dumps({'key': key, 'files': hashes}) + '\n')
        # Complete staging first. Rename the previous cache aside so interrupted
        # publication cannot mix old and new per-function listings.
        backup = destination.with_name(destination.name + '.previous')
        if backup.exists():
            raise ValueError('previous code-map cache needs recovery: %s' % backup)
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        print('Decoded code map: %d functions into %s' % (len(functions), destination))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pack', type=Path, required=True, metavar='V1_DIR')
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    pack(args.pack, args.exe, args.out)


if __name__ == '__main__':
    main()
