"""Build-only LLVM emission through the production decoder and C translator.

No dispatch table, override, or production chunk is written. This deliberately
accepts only verified, contiguous, single-entry leaves in the existing LLVM
subset. The caller must select the mapped-normal-exit contract explicitly.
"""
from pathlib import Path
import hashlib
import json

from experiments.x87_llvm.direct import direct_ir
from experiments.x87_llvm.function import DECLARATIONS as FUNCTION_DECLS, emit_function
from experiments.x87_llvm.run import DECLARATIONS

CONTRACT = 'mapped-normal-exit-v1'


def read_manifest(path):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get('contract') != CONTRACT:
        raise ValueError(f'LLVM comparison requires explicit contract {CONTRACT}')
    profiles = manifest.get('functions')
    if not isinstance(profiles, list) or not profiles or not all(isinstance(p, str) for p in profiles):
        raise ValueError('LLVM comparison requires a nonempty function-profile list')
    return [(path.parent / p).resolve() for p in profiles]


def validate_listing(T, image, fn, profile):
    """Bytes establish instructions; the listing must describe exactly that body.

    Compare normalized operand structure, not mnemonic aliases or hex spelling.
    Neither listing edits nor translator instruction patches may alter semantics.
    """
    start, size = int(profile['entry'], 0), profile['size']
    if fn.addr != start or fn.size != size or size <= 0:
        raise ValueError('profile and listed function extent differ')
    data = image.data[start-image.base:start-image.base+size]
    if hashlib.sha256(data).hexdigest() != profile['function_sha256']:
        raise ValueError('function bytes differ from inspected evidence')
    image.md.detail = True
    decoded = list(image.md.disasm(data, start))
    if not decoded or sum(i.size for i in decoded) != size:
        raise ValueError('incomplete byte decode')
    canonical = [image.to_insn(i) for i in decoded]

    def key(ins):
        m = {'JE': 'JZ', 'JNE': 'JNZ'}.get(ins.mnem, ins.mnem)
        operands = [] if m == 'FADDP' and ins.ops == ['ST1'] else ins.ops
        return ins.addr, m, ins.rep, [tuple(getattr(T.parse_operand(op), field) for field in T.Op.__slots__) for op in operands]

    if [key(i) for i in fn.insns] != [key(i) for i in canonical]:
        raise ValueError('listing instructions disagree with executable bytes')
    fn.measure(image)
    if not all(fn.contiguous) or fn.fallthrough[-1] != start + size:
        raise ValueError('noncontiguous function is unsupported')


def emit_comparison(T, image, args):
    profiles = read_manifest(args.llvm_compare)
    out = Path(args.out).resolve()
    # Never place even a partial comparison in the generated-code source tree.
    production = Path(args.game).resolve() / 'build/recomp/gen'
    if out == production or production in out.parents or any((p / 'table.c').exists() for p in (out, *out.parents)):
        raise ValueError('LLVM comparison output must be separate from production generation')
    executable_hash = hashlib.sha256(Path(T.BINARY).read_bytes()).hexdigest()
    rows, prepared, seen = {}, [], set()
    for profile_path in profiles:
        p = json.loads(profile_path.read_text())
        start = int(p['entry'], 0)
        if start in seen:
            raise ValueError('duplicate LLVM comparison function')
        seen.add(start)
        if (profile_path.parent / p['exe']).resolve() != Path(T.BINARY).resolve() or p['sha256'] != executable_hash:
            raise ValueError('profile is not for the configured executable')
        listed = T.load_functions({f'{start:08x}'})
        if len(listed) != 1:
            raise ValueError(f'expected one existing listing for {start:08x}')
        addr, name, size, listing = listed[0]
        fn = T.Function(addr, name, size, T.parse_listing(listing))
        validate_listing(T, image, fn, p)
        if fn.addrs & (set(T.OPERAND_REDIRECTS) | set(T.VISUAL_ANIMATION_READS)):
            raise ValueError('configured instruction rewrites are unsupported by LLVM comparison')
        if addr in T.INTRINSIC_BODY:
            raise ValueError('runtime intrinsic is not an LLVM leaf comparison')
        # LLVM frontend rejects calls, stores, alternate transfers and unsupported
        # operands before any output is published. Both emitters use fn.insns.
        module = direct_ir(DECLARATIONS + FUNCTION_DECLS)
        module += direct_ir(emit_function('compare_raw', fn.insns, False))
        module += direct_ir(emit_function('compare_lifted', fn.insns, True), effects=True)
        tr = T.Translator(image, {addr}, args)
        tr.prepare(fn, strict=True)
        body = '\n'.join(tr.translate(fn))
        if (fn.seh_sites or fn.seh_escapes or fn.pushed_continuations or fn.dead_addrs
                or tr.jumptables or 'static void body_' in body):
            raise ValueError('LLVM comparison requires an ordinary single-entry leaf')
        fixture = profile_path.parent / p['codegen_fixture']
        prepared.append((f'{addr:08x}', body, module, fixture.read_text()))
        rows[f'{addr:08x}'] = {'name': name, 'profile': str(profile_path),
                             'size': size, 'instructions': len(fn.insns),
                             'function_sha256': p['function_sha256'],
                             'listing': str(listing), 'body_sha256': hashlib.sha256(body.encode()).hexdigest()}
    out.mkdir(parents=True, exist_ok=True)
    for addr, body, module, fixture in prepared:
        directory = out / addr
        directory.mkdir(exist_ok=True)
        (directory / 'body.h').write_text(T.BODY_HEADER)
        (directory / 'body.txt').write_text(body)
        (directory / 'baseline.c').write_text(
            f'#include "body.h"\n#define fn_{addr} COMPARE_SYMBOL\n{body}\n')
        (directory / 'input.ll').write_text(module)
        (directory / 'fixtures.h').write_text(fixture)
    report = {'contract': CONTRACT, 'executable_sha256': executable_hash,
              'dispatch_enabled': False, 'functions': rows}
    (out / 'translation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'LLVM comparison emitted {len(rows)} leaves into {out}; production dispatch unchanged')
