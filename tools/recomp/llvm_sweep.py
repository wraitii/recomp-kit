"""Full listing-census coverage for the existing bounded LLVM experiment.

This is emission/build evidence only. Call contracts come from inspected profiles;
the sweep never invents callees, entry states, boundaries, or replay fixtures.
"""
from collections import Counter
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
from translate import TranslateError

from llvm_compare import CONTRACT, read_manifest, validate_calls, validate_listing
from experiments.x87_llvm.direct import direct_ir
from experiments.x87_llvm.function import DECLARATIONS as FUNCTION_DECLS, emit_function
from experiments.x87_llvm.run import DECLARATIONS, llvm_config

HERE = Path(__file__).resolve().parent


def census(T):
    """Include missing/empty listings rather than silently dropping census rows."""
    rows = []
    for line in Path(T.FUNCS_TSV).read_text().splitlines()[1:]:
        address, name, size = line.split('\t')
        rows.append((int(address, 16), name, int(size)))
    if len({r[0] for r in rows}) != len(rows):
        raise ValueError('duplicate address in function census')
    return sorted(rows)


def candidate(T, image, args, addr, name, size, profile):
    """Return byte-verified IR and ordinary production C, or a scoped refusal."""
    listing = Path(T.LISTINGS) / f'{addr:08x}.asm'
    row = {'address': f'{addr:08x}', 'name': name, 'size': size,
           'listing': str(listing), 'status': 'rejected', 'stage': 'listing'}
    try:
        if not listing.is_file() or not listing.stat().st_size:
            raise ValueError('missing or empty listing')
        fn = T.Function(addr, name, size, T.parse_listing(listing))
        row['instructions'] = len(fn.insns)
        row['x87_instructions'] = sum(i.mnem.startswith('F') for i in fn.insns)
        data = image.data[addr-image.base:addr-image.base+size]
        digest = hashlib.sha256(data).hexdigest()
        row['function_sha256'] = digest
        # The census extent is checked, never repaired by trimming or recovery.
        validate_listing(T, image, fn, {'entry': hex(addr), 'size': size,
                                       'function_sha256': digest})
        if profile and (profile['size'] != size or profile['function_sha256'] != digest):
            raise ValueError('declared profile differs from census bytes/extent')
        row['stage'] = 'contract'
        if fn.addrs & (set(T.OPERAND_REDIRECTS) | set(T.VISUAL_ANIMATION_READS)
                       | set(T.INSTRUCTION_PATCHES)):
            raise ValueError('configured instruction rewrites are unsupported')
        if addr in T.INTRINSIC_BODY:
            raise ValueError('runtime intrinsic is not a bounded LLVM function')
        # Synchronization only widens the analysis scope; calls still need evidence.
        p = profile or {'synchronize_cfg': True, 'call_targets': []}
        row['listed_call_targets'] = [T.Translator.branch_target(i) for i in fn.insns if i.mnem == 'CALL']
        calls, sync = validate_calls(T, fn, p)
        row['call_targets'] = sorted(calls)
        row['synchronize_cfg'] = sync
        row['stage'] = 'emission'
        module = direct_ir(DECLARATIONS + FUNCTION_DECLS)
        module += direct_ir(emit_function(f'sweep_{addr:08x}', fn.insns, True, sync), effects=True)
        row['stage'] = 'production-preparation'
        tr = T.Translator(image, {addr} | calls, args)
        tr.prepare(fn, strict=True)
        body = '\n'.join(tr.translate(fn))
        if (fn.seh_sites or fn.seh_escapes or fn.pushed_continuations or fn.dead_addrs
                or tr.jumptables or 'static void body_' in body):
            raise ValueError('requires an ordinary single-entry function')
        row.update(status='emitted', stage='lifting', reason='byte-verified bounded frontend')
        return row, module, body
    except (ValueError, T.TranslateError, TranslateError) as error:
        row['reason'] = str(error)
        return row, None, None


def write_report(out, report):
    """Keep per-row first refusals as well as stage/reason totals; no execution counts."""
    report['summary'] = dict(Counter(r['status'] for r in report['functions']))
    report['x87_summary'] = dict(Counter(r['status'] for r in report['functions']
                                        if r.get('x87_instructions')))
    # Preserve full diagnostic/address per row; aggregate the first refusal kind.
    reasons = Counter((r['stage'], r['reason'].removeprefix('error: ').split(':', 1)[0])
                      for r in report['functions'])
    report['reasons'] = [{'stage': s, 'reason': reason, 'functions': n}
                         for (s, reason), n in sorted(reasons.items())]
    (out / 'coverage.json').write_text(json.dumps(report, indent=2) + '\n')


def emit_sweep(T, image, args):
    """Verify all census rows; publish candidates only outside production generation."""
    out = Path(args.out).resolve()
    production = Path(args.game).resolve() / 'build/recomp/gen'
    if out == production or production in out.parents or any((p / 'table.c').exists() for p in (out, *out.parents)):
        raise ValueError('LLVM sweep output must be separate from production generation')
    executable_hash = hashlib.sha256(Path(T.BINARY).read_bytes()).hexdigest()
    profiles = {}
    for path in read_manifest(args.llvm_sweep):
        p = json.loads(path.read_text())
        addr = int(p['entry'], 0)
        if addr in profiles:
            raise ValueError('duplicate sweep contract profile')
        if (path.parent / p['exe']).resolve() != Path(T.BINARY).resolve() or p['sha256'] != executable_hash:
            raise ValueError('sweep profile executable mismatch')
        profiles[addr] = p
    rows = census(T)
    if profiles.keys() - {r[0] for r in rows}:
        raise ValueError('declared profile absent from census')
    out.mkdir(parents=True, exist_ok=True)
    report = {'contract': CONTRACT, 'scope': 'exported function census; no recovered blocks',
              'executable_sha256': executable_hash,
              'census_sha256': hashlib.sha256(Path(T.FUNCS_TSV).read_bytes()).hexdigest(),
              'manifest_sha256': hashlib.sha256(Path(args.llvm_sweep).read_bytes()).hexdigest(),
              'source_sha256': {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (HERE / 'llvm_sweep.py', HERE / 'llvm_compare.py',
                                          HERE / 'translate.py',
                                          HERE / 'experiments/x87_llvm/function.py',
                                          HERE / 'experiments/x87_llvm/stack_pass.cpp')},
              'dispatch_enabled': False, 'execution': 'not run', 'functions': []}
    for addr, name, size in rows:
        row, module, body = candidate(T, image, args, addr, name, size, profiles.get(addr))
        report['functions'].append(row)
        if module:
            directory = out / row['address']
            directory.mkdir(exist_ok=True)
            (directory / 'input.ll').write_text(module)
            (directory / 'body.txt').write_text(body)
    write_report(out, report)
    print(f'LLVM sweep: {len(rows)} census rows, {report["summary"]}; no execution')


def lift_candidates(out, opt, plugin):
    """Use the real passes per function so one refusal cannot hide later coverage.

    LLVM's error exit is a refusal; crashes/tool failures stop the survey instead
    of being mislabeled unsupported. No equivalent Python stack model is used.
    """
    report = json.loads((out / 'coverage.json').read_text())
    for row in report['functions']:
        if row['status'] != 'emitted':
            continue
        directory = out / row['address']
        result = subprocess.run([str(opt), f'-load-pass-plugin={plugin}',
                                 '-passes=recomp-x87-stack', '-verify-each', '-S',
                                 str(directory / 'input.ll'), '-o', str(directory / 'lifted.ll')],
                                capture_output=True, text=True)
        (directory / 'lifting.txt').write_text(result.stderr)
        if result.returncode:
            if result.returncode != 1 or not result.stderr.startswith('error: '):
                raise RuntimeError(f'{row["address"]}: unexpected opt failure: {result.stderr}')
            row.update(status='rejected', reason=result.stderr.strip())
        else:
            lifted = (directory / 'lifted.ll').read_text()
            # Ignore declarations: the transformed body must have no stack helpers.
            body = lifted.split('define void @', 1)[1].split('\n}', 1)[0]
            if any(f'@rk_{op}(' in body for op in ('push', 'pop', 'read', 'set', 'snapshot')):
                raise RuntimeError('lifting postcondition failed')
            row.update(status='supported', reason='verified emission and complete lifting')
    report['llvm_version'] = subprocess.check_output([str(opt), '--version'], text=True).splitlines()[0]
    write_report(out, report)
    print('LLVM lifting coverage:', report['summary'], '; execution not run')


def object_sizes(symbols, disassembly):
    """Measure relocatable text spans, excluding other helpers and data sections.

    These are object spans through the next symbol/text end, not linked executable
    sizes. Alignment and cold paths within the span count, like the replay metric.
    Refuse split text sections instead of guessing across different address spaces.
    """
    sections = re.findall(r'^\s*\d+\s+\S+\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+TEXT\s*$', disassembly, re.M)
    if len(sections) != 1:
        raise ValueError('object span measurement requires one text section')
    size, base = (int(s, 16) for s in sections[0])
    entries = [(int(a, 16), n.lstrip('_')) for a, n in
               re.findall(r'^([0-9a-fA-F]+)\s+[tT]\s+(\S+)$', symbols, re.M)]
    result = {}
    instructions = [(int(a, 16), op) for a, op in
                    re.findall(r'^\s*([0-9a-fA-F]+):\s+([a-z][a-z0-9.]*)\b', disassembly, re.M)]
    for start, name in entries:
        if name not in {'compare_c_native', 'compare_c_llvm', 'compare_raw', 'compare_lifted'}:
            continue
        end = min([a for a, _ in entries if a > start] + [base + size])
        ops = [op for a, op in instructions if start <= a < end]
        result[name] = {'span_bytes': end - start, 'instructions': len(ops),
                        'fused_multiply_adds': sum(op in {'fmadd', 'fmsub', 'fnmadd', 'fnmsub'} for op in ops)}
    return result


def codegen_candidates(out, database, cmake, jobs, bindir, cmakedir):
    """Compile eligible C/raw/lifted bodies with production settings; never execute."""
    from llvm_compare_build import production_settings
    import translate as T
    report = json.loads((out / 'coverage.json').read_text())
    supported = {r['address']: r for r in report['functions'] if r['status'] == 'supported'}
    for addr, row in supported.items():
        directory = out / addr
        body = (directory / 'body.txt').read_text()
        (directory / 'body.h').write_text(T.BODY_HEADER)
        calls = row['call_targets']
        prototypes = ''.join(f'void entry_{a:08x}(X86 *);\n' for a in calls)
        (directory / 'baseline.c').write_text(
            f'#include "body.h"\n{prototypes}#define fn_{addr} COMPARE_SYMBOL\n{body}\n')
        (directory / 'call-dispatch.h').write_text('#include "x86.h"\n' + prototypes +
            'static inline __attribute__((always_inline)) void rk_compare_dispatch(X86 *c, uint32_t target) {\n' +
            ''.join(f'  if (target == 0x{a:08x}u) {{ entry_{a:08x}(c); return; }}\n' for a in calls) +
            '  recomp_call(c, target);\n}\n#define RK_CALL_TARGET(c, target) rk_compare_dispatch(c, target)\n')
        original = (directory / 'input.ll').read_text()
        function = original.split('define void @', 1)[1]
        raw = ('define void @' + function).replace(f'sweep_{addr}', 'compare_raw').replace(
            ' "recomp.x87.region"', '').replace(' "recomp.x87.effects"', '')
        lifted = original.replace(f'sweep_{addr}', 'compare_lifted')
        (directory / 'input.ll').write_text(lifted + raw)
    refusals = {}
    settings = production_settings(database, out, {'functions': supported}, HERE.parents[1] / 'runtime', refusals)
    for addr, reason in refusals.items():
        supported[addr]['codegen'] = {'status': 'rejected', 'reason': reason}
    report['codegen'] = {'execution': 'not run', 'production': settings,
                         'compile_database_sha256': hashlib.sha256(Path(database).read_bytes()).hexdigest(),
                         'span_kind': 'relocatable objects; includes alignment; excludes other symbols'}
    write_report(out, report)
    (out / 'settings.cmake').write_text('set(LEAVES ' + ' '.join(settings) + ')\n')
    subprocess.run([cmake, '-S', str(HERE / 'llvm_compare_native'), '-B', str(out),
                    f'-DLLVM_DIR={cmakedir}', f'-DCMAKE_C_COMPILER={bindir / "clang"}',
                    f'-DCMAKE_CXX_COMPILER={bindir / "clang++"}', f'-DPython3_EXECUTABLE={sys.executable}',
                    '-DBUILD_REPLAY=OFF'], check=True)
    subprocess.run([cmake, '--build', str(out), '--parallel', str(jobs)], check=True)
    for addr in settings:
        sizes = {}
        for variant in ('c_native', 'c_llvm', 'llvm'):
            obj = out / addr / f'{variant}.o'
            symbols = subprocess.check_output([str(bindir / 'llvm-nm'), '-n', str(obj)], text=True)
            disasm = subprocess.check_output([str(bindir / 'llvm-objdump'), '--section-headers',
                                              '--disassemble', '--no-show-raw-insn', str(obj)], text=True)
            (out / addr / f'{variant}-disassembly.txt').write_text(disasm)
            sizes.update(object_sizes(symbols, disasm))
        if len(sizes) != 4:
            raise RuntimeError(f'{addr}: cannot identify all four object function spans')
        supported[addr]['codegen'] = {'status': 'compiled', 'sizes': sizes}
        (out / addr / 'codegen.json').write_text(json.dumps(sizes, indent=2) + '\n')
    report['codegen']['summary'] = dict(Counter(r['codegen']['status'] for r in supported.values()))
    write_report(out, report)
    print('LLVM object codegen coverage:', report['codegen']['summary'], '; execution not run')


def run_sweep(manifest, game, out, cmake, jobs, database):
    """Run through the build wrapper; build only the reusable pass plugin."""
    config = llvm_config()
    if not Path(database).is_file():
        raise ValueError('LLVM corpus codegen requires the production compile database')
    bindir = Path(subprocess.check_output([config, '--bindir'], text=True).strip())
    cmakedir = subprocess.check_output([config, '--cmakedir'], text=True).strip()
    subprocess.run([sys.executable, str(HERE / 'translate.py'), '--game', str(game),
                    '--out', str(out), '--llvm-sweep', str(Path(manifest).resolve())], check=True)
    plugin_dir = out / 'plugin'
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / 'settings.cmake').write_text('set(LEAVES)\n')
    subprocess.run([cmake, '-S', str(HERE / 'llvm_compare_native'), '-B', str(plugin_dir),
                    f'-DLLVM_DIR={cmakedir}', f'-DCMAKE_C_COMPILER={bindir / "clang"}',
                    f'-DCMAKE_CXX_COMPILER={bindir / "clang++"}'], check=True)
    subprocess.run([cmake, '--build', str(plugin_dir), '--target', 'RecompX87',
                    '--parallel', str(jobs)], check=True)
    plugin = next(p for p in plugin_dir.glob('RecompX87.*') if p.suffix in {'.so', '.dylib', '.dll'})
    lift_candidates(out, bindir / 'opt', plugin)
    codegen_candidates(out, database, cmake, jobs, bindir, cmakedir)
