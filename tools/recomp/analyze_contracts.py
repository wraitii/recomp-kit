#!/usr/bin/env python3
"""Inventory observable contracts for byte-pinned corpus functions; no build.

Uses original PE bytes and the production CFG resolver. Fixture stubs and native
comparison masks are never imported as production observer contracts. Reports
remain under the game's ignored build directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import translate as T  # also installs the kit tools path
import game_config
from code_map import decode_span, read_map
from ir.cfg import function_ir
from ir.contracts.report import analyze
from ir.lift import Lifter


def load_inputs(game_dir, manifest):
    """Verify the image, map and per-row bytes before resolving any CFG."""
    cfg = game_config.load(game_dir)
    image_hash = hashlib.sha256(cfg['developer_exe_path'].read_bytes()).hexdigest()
    if image_hash != cfg['game']['sha256']:
        raise ValueError('original executable hash mismatch')
    if not cfg.get('code_map_path'):
        raise ValueError('contract inventory requires a public code map')
    metadata, mapped = read_map(cfg['code_map_path'])
    if (metadata.get('executable_sha256') != image_hash
            or int(metadata['image_base'], 16) != cfg['game']['image_base']):
        raise ValueError('code-map image identity mismatch')
    spec = json.loads(Path(manifest).read_text())
    if spec.get('contract') not in ('mapped-native-corpus-v1', 'mapped-comparison-corpus-v2'):
        raise ValueError('a byte-pinned corpus manifest is required')
    rows = spec['functions']
    addresses = [int(row['address'], 16) for row in rows]
    if not rows or len(set(addresses)) != len(addresses):
        raise ValueError('nonempty unique function addresses required')
    T.configure(cfg)
    image = T.Image(T.BINARY)
    if image.base != cfg['game']['image_base']:
        raise ValueError('image base mismatch')
    tr = T.Translator(image, set(mapped), SimpleNamespace(eager_flags=True))
    lifter, functions, provenance = Lifter(), [], {}
    for row, address in zip(rows, addresses):
        name, size, spans = mapped[address]
        insns, original = [], bytearray()
        for start, lengths in spans:
            insns.extend(decode_span(image, start, lengths))
            lo, count = start - image.base, sum(int(c, 16) for c in lengths)
            original.extend(image.data[lo:lo + count])
        digest = hashlib.sha256(original).hexdigest()
        if digest != row['instructions_sha256']:
            raise ValueError('%08x: reviewed instruction hash mismatch' % address)
        sites = {ins.addr for ins in insns}
        if sites & (set(T.INSTRUCTION_PATCHES) | set(T.OPERAND_REDIRECTS)):
            raise ValueError('contract inventory rejects configured instruction rewrites')
        fn = T.Function(address, name, size, insns)
        fn.measure(image)
        tr.prepare(fn, strict=True)
        functions.append(function_ir(tr, lifter, fn))
        provenance['%08x' % address] = {'name': name, 'instructions_sha256': digest}
    return cfg, image_hash, functions, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    cfg = game_config.load(args.game_dir)
    build = (game_config.build_root_for(args.game_dir) if hasattr(game_config, 'build_root_for')
             else args.game_dir / 'build').resolve()
    output = args.out.resolve()
    if build not in output.parents:
        parser.error('private reports must stay under the game build directory')
    # Do not leave an earlier successful report current after a failed rerun.
    output.unlink(missing_ok=True)
    _, image_hash, functions, provenance = load_inputs(args.game_dir, args.manifest)
    report = analyze(functions)
    report['executable_sha256'] = image_hash
    for address, evidence in provenance.items():
        report['functions'][address]['provenance'] = evidence
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print('Inventoried %d byte-verified functions: %s' % (len(functions), output))
    print('Diagnostic facts only; production emission and dispatch are unchanged.')


if __name__ == '__main__':
    main()
