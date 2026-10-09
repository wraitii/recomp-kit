"""Decode public instruction-location metadata using the owner's private PE.

A map holds function spans (start and byte count) and, only where a linear
decode disagrees with the analysis boundaries, explicit instruction lengths,
plus recovered jump tables, interior entries and non-returning functions.
Addresses only: no names, bytes or instruction text.

    code_map.py --pack V1_DIR --exe GAME.EXE --out MAP_DIR
"""
import argparse
from pathlib import Path
import hashlib
import sys

MAP_FILES = ('metadata.txt', 'spans.tsv', 'tables.tsv', 'entries.tsv', 'noreturn.tsv')
FORMAT = 'recomp-code-map-v3'
EXPORT_FORMAT = 'recomp-code-map-v3-export'
SPANS_HEADER = 'function\tstart\tbytes\tlengths'
TABLES_HEADER = 'function\tsite\ttargets'
ENTRIES_HEADER = 'address\towner\tkind'
NORETURN_HEADER = 'address\tkind'
NORETURN_KINDS = ('function', 'call')
ENTRY_KINDS = ('branch', 'data', 'table')


def read_map(root):
    """Returns metadata and {function: [(start, bytes, lengths or '')]}."""
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
        raise ValueError('code-map function count mismatch')
    return metadata, functions


def read_rows(root, name, header):
    rows = (Path(root) / name).read_text().splitlines()
    if not rows or rows[0] != header:
        raise ValueError('invalid code-map header in ' + name)
    return [row.split('\t') for row in rows[1:]]


def read_program(root):
    """Everything a map says about control flow, beyond the spans.

    Returns tables {(function, site): [targets]}, entries {address: (owner, kind)}
    the non-returning function entries and the CALL sites Ghidra ends flow at."""
    tables = {}
    for function, site, targets in read_rows(root, 'tables.tsv', TABLES_HEADER):
        tables[int(function, 16), int(site, 16)] = [int(t, 16) for t in targets.split(',')]
    entries = {}
    for address, owner, kind in read_rows(root, 'entries.tsv', ENTRIES_HEADER):
        if kind not in ENTRY_KINDS:
            raise ValueError('invalid code-map entry kind: ' + kind)
        entries[int(address, 16)] = (int(owner, 16), kind)
    noreturn = {kind: set() for kind in NORETURN_KINDS}
    for address, kind in read_rows(root, 'noreturn.tsv', NORETURN_HEADER):
        noreturn[kind].add(int(address, 16))
    return tables, entries, noreturn['function'], noreturn['call']


def linear_lengths(lifter, image, start, size):
    """SLEIGH's instruction lengths across a span, or None where it cannot tile it exactly."""
    from ir.lift import LiftError
    try:
        lengths = lifter.lengths(start, bytes(image.data[start - image.base:start - image.base + size]))
    except LiftError:
        return None
    if any(n > 15 for n in lengths):
        return None
    return ''.join('%x' % n for n in lengths)


def pack(export_root, executable, out):
    """Convert an export (names, sizes and every instruction length) into a v3 map.

    Lengths stay explicit wherever SLEIGH's linear decode differs from them."""
    sys.path.insert(0, str(Path(__file__).parent))
    from program import PE
    from ir.lift import Lifter
    export_root, out = Path(export_root), Path(out)
    metadata = dict(line.split('=', 1) for line in (export_root / 'metadata.txt').read_text().splitlines())
    if metadata.get('format') != EXPORT_FORMAT or metadata.get('status') != 'complete':
        raise ValueError('expected a complete %s export' % EXPORT_FORMAT)
    image = PE(Path(executable))
    if hashlib.sha256(Path(executable).read_bytes()).hexdigest() != metadata['executable_sha256']:
        raise ValueError('executable does not match the export')
    lifter = Lifter()
    rows = [SPANS_HEADER]
    explicit = 0
    for row in (export_root / 'instruction_map.tsv').read_text().splitlines()[1:]:
        function, start, lengths = row.split('\t')
        addr = int(start, 16)
        size = sum(int(c, 16) for c in lengths)
        keep = '' if linear_lengths(lifter, image, addr, size) == lengths else lengths
        explicit += bool(keep)
        rows.append('%s\t%s\t%d\t%s' % ('' if function == start else function, start, size, keep))
    out.mkdir(parents=True, exist_ok=True)
    metadata['format'] = FORMAT
    (out / 'metadata.txt').write_text(''.join('%s=%s\n' % item for item in metadata.items()))
    (out / 'spans.tsv').write_text('\n'.join(rows) + '\n')
    for name, header in (('tables.tsv', TABLES_HEADER), ('entries.tsv', ENTRIES_HEADER),
                         ('noreturn.tsv', NORETURN_HEADER)):
        body = (export_root / name).read_text().splitlines()
        if body[0] != header:
            raise ValueError('invalid export header in ' + name)
        (out / name).write_text('\n'.join(body) + '\n')
    print('Packed %d spans (%d with explicit lengths) into %s' % (len(rows) - 1, explicit, out))


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pack', type=Path, required=True, metavar='EXPORT_DIR')
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    pack(args.pack, args.exe, args.out)


if __name__ == '__main__':
    main()
