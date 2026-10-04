"""Decode public instruction-location metadata using the owner's private PE.

Maps contain addresses, instruction lengths and analysis names only. The resulting
assembly listings are local build inputs, never publication artifacts.
"""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile

MAP_FILES = ('metadata.txt', 'functions.tsv', 'instruction_map.tsv')
FORMAT = 'recomp-code-map-v1'


def read_map(root):
    """Validate a map before touching its generated listing directory."""
    root = Path(root)
    metadata = dict(line.split('=', 1) for line in (root / 'metadata.txt').read_text().splitlines())
    if metadata.get('format') != FORMAT or metadata.get('status') != 'complete':
        raise ValueError('unsupported or incomplete code map')
    rows = (root / 'functions.tsv').read_text().splitlines()
    if not rows or rows[0] != 'address\tname\tbytes':
        raise ValueError('invalid code-map function header')
    functions = {}
    for row in rows[1:]:
        address, name, size = row.split('\t')
        addr, size = int(address, 16), int(size)
        if addr in functions or not 0 <= addr <= 0xffffffff or size < 0:
            raise ValueError('invalid or duplicate code-map function')
        functions[addr] = (name, size, [])
    rows = (root / 'instruction_map.tsv').read_text().splitlines()
    if not rows or rows[0] != 'function\tstart\tlengths':
        raise ValueError('invalid code-map instruction header')
    count = 0
    ends = {}
    for row in rows[1:]:
        function, start, lengths = row.split('\t')
        owner, addr = int(function, 16), int(start, 16)
        if owner not in functions or not lengths or any(c not in '123456789abcdef' for c in lengths):
            raise ValueError('invalid code-map instruction span')
        if not 0 <= addr <= 0xffffffff or addr < ends.get(owner, 0):
            raise ValueError('overlapping or unordered code-map spans')
        end = addr + sum(int(c, 16) for c in lengths)
        if end > 0x100000000:
            raise ValueError('code-map span exceeds guest address space')
        ends[owner] = end
        functions[owner][2].append((addr, lengths))
        count += len(lengths)
    if int(metadata['functions']) != len(functions) or int(metadata['instructions']) != count:
        raise ValueError('code-map census mismatch')
    return metadata, functions


def decode_span(image, start, lengths):
    """Decode exactly the exported boundaries, including Ghidra's folded WAIT."""
    if image.md is None:
        raise ValueError('code-map decoding requires capstone')
    detail = image.md.detail
    image.md.detail = True
    try:
        addr = start
        for digit in lengths:
            size = int(digit, 16)
            if (not image.is_exec(addr) or not image.is_exec(addr + size - 1)
                    or not image.base <= addr < addr + size <= image.end):
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
        shutil.copyfile(root / 'functions.tsv', stage / 'functions.tsv')
        hashes = {'functions.tsv': hashlib.sha256((stage / 'functions.tsv').read_bytes()).hexdigest()}
        for addr, (_, _, spans) in functions.items():
            lines = [insn.raw for start, lengths in spans for insn in decode_span(image, start, lengths)]
            name = 'functions/%08x.asm' % addr
            data = ('\n'.join(lines) + ('\n' if lines else '')).encode()
            (stage / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
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
