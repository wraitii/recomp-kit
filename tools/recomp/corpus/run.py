"""Build a byte-verified game corpus and report normal-exit checks and codegen.

Game-owned manifests, typed native references and input/observation contracts
stay outside the kit. Outputs contain private instruction contents and image
bytes, and must stay in the game's ignored build directory.
"""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import csv
import json
import platform
import re
import shutil
import statistics
import subprocess
import time

import game_config
import translate as T
from code_map import read_map, decode_span

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[2]
MODES = ('eager', 'cpu', 'x87', 'combined', 'native')


def write_input(path, contents):
    """Preserve timestamps when emission is unchanged for incremental CMake."""
    data = contents.encode() if isinstance(contents, str) else contents
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)


def symbol_sizes(disassembly):
    """Linked text spans include alignment; helper bodies are measured separately."""
    labels = list(re.finditer(r'^([0-9a-fA-F]+) <([^>]+)>:$', disassembly, re.M))
    result = {}
    for left, right in zip(labels, labels[1:]):
        name = left[2].lstrip('_')
        body = disassembly[left.end():right.start()]
        ops = re.findall(r'^\s*[0-9a-fA-F]+:\s+([a-z][a-z0-9.]*)\b', body, re.M)
        # Mach-O can retain multiple local outlined helpers with the same name.
        # Preserve each span instead of silently overwriting earlier helpers.
        key = name if name not in result else name + '@' + left[1]
        result[key] = {'span_bytes': int(right[1], 16) - int(left[1], 16),
                        'instructions': len(ops),
                        'static_sp_accesses': len(re.findall(r'\[sp(?:,|\])', body)),
                        'fused_multiply_adds': sum(op in ('fmadd', 'fmsub', 'fnmadd', 'fnmsub', 'fmla', 'fmls')
                                                  or op.startswith(('vfmadd', 'vfmsub', 'vfnmadd', 'vfnmsub')) for op in ops)}
    return result


def parse_results(output, rows, checks, calls, trials):
    """Refuse missing checks/trials rather than presenting partial runs as passes."""
    checked = {}
    times = {}
    for line in output.splitlines():
        parts = line.split()
        if parts[:1] == ['CHECK']:
            row, count = map(int, parts[1:])
            if row in checked:
                raise ValueError('duplicate corpus check')
            checked[row] = count
        elif parts[:1] == ['TIME']:
            row, mode, trial = map(int, parts[1:4])
            key = (row, mode, trial)
            value = float(parts[4])
            if key in times or not value > 0:
                raise ValueError('duplicate or invalid timing trial; increase call count')
            times[key] = value
    if checked != dict.fromkeys(range(rows), checks):
        raise ValueError('incomplete correctness checks')
    expected = {(r, m, t) for r in range(rows) for m in range(len(MODES)+1)
                for t in range(trials)} if calls else set()
    if set(times) != expected:
        raise ValueError('incomplete benchmark trials')
    return times


def markdown(report):
    lines = ['# Function corpus report', '',
             f"Host: {report['host']}. Compiler: {report['compiler']}.", '',
             'Native is reviewed C plus its ABI adapter; kernel text is also shown separately.',
             'Translated/native-adapter times include entry reset and indirect-call overhead.',
             'Native-kernel times use the typed host ABI, without guest state/reset. No LTO, FMA or fast-math.',
             'Text spans include alignment and exclude out-of-line helper bodies; see JSON helper sizes.',
             'Original x86 bytes and host text sizes describe different architectures.', '',
             '| Function | Original bytes / x87 instructions | Eager bytes | Combined bytes | Native adapter / kernel bytes | Combined / native kernel ns per call |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for row in report['functions']:
        def timing(mode):
            values = row['variants'][mode].get('timing_ns')
            return f"{values['median']:.2f}" if values else '—'
        variants = row['variants']
        lines.append(f"| {row['address']} {row['name']} | {row['original_bytes']} / {row['x87_instructions']} | "
                     f"{variants['eager']['span_bytes']} | {variants['combined']['span_bytes']} | "
                     f"{variants['native']['span_bytes']} / {row['native_kernel']['span_bytes']} | "
                     f"{timing('combined')} / " + (f"{row['native_kernel']['timing_ns']['median']:.2f}" if 'timing_ns' in row['native_kernel'] else '—') + ' |')
    lines += ['', f"All {report['checks_per_function']} inputs/function passed full-state translation checks and declared native observations.",
              '', 'Native contracts are restricted to the inputs/settings documented by the game fixtures. '
              'This is a practical native target, not a proven lower bound or original-x86 equivalence. '
              'These hot microbenchmarks do not establish game frame-time improvements.', '']
    return '\n'.join(lines)


def run_corpus(manifest, game_dir, out, cmake, jobs, checks=4096, calls=100000, trials=9):
    """Decode the selected instructions, build isolated variants, validate, report."""
    if checks < 1 or calls < 0 or trials < 3:
        raise ValueError('checks must be positive, calls nonnegative, trials at least three')
    game_dir, manifest, out = Path(game_dir).resolve(), Path(manifest).resolve(), Path(out).resolve()
    build = game_config.build_root_for(game_dir).resolve() if hasattr(game_config, 'build_root_for') else game_dir / 'build'
    if build not in out.parents:
        raise ValueError('private corpus outputs must stay under the game build directory')
    spec = json.loads(manifest.read_text())
    if spec.get('contract') != 'mapped-native-corpus-v1':
        raise ValueError('explicit mapped-native-corpus-v1 contract required')
    rows = spec['functions']
    addresses = [row['address'] for row in rows]
    if not rows or len(set(addresses)) != len(rows) or any(not re.fullmatch('[0-9a-f]{8}', a) for a in addresses):
        raise ValueError('unique eight-digit function addresses required')
    fixture_ids = [row['fixture_id'] for row in rows]
    if any(type(i) is not int or i < 0 for i in fixture_ids) or len(set(fixture_ids)) != len(rows):
        raise ValueError('unique nonnegative fixture ids required')
    cfg = game_config.load(game_dir)
    if hashlib.sha256(cfg['developer_exe_path'].read_bytes()).hexdigest() != cfg['game']['sha256']:
        raise ValueError('original executable hash mismatch')
    if not cfg.get('code_map_path'):
        raise ValueError('corpus requires a public instruction code map')
    _, functions = read_map(cfg['code_map_path'])
    T.configure(cfg)
    image = T.Image(T.BINARY)
    out.mkdir(parents=True, exist_ok=True)
    # Do not leave an earlier successful report looking current after a failure.
    for filename in ('report.json', 'report.md', 'report.csv'):
        (out / filename).unlink(missing_ok=True)
    sources = []
    provenance = []
    started = time.monotonic()
    for row in rows:
        addr = int(row['address'], 16)
        name, size, spans = functions[addr]
        insns = [ins for start, lengths in spans for ins in decode_span(image, start, lengths)]
        raw = b''.join(bytes(image.data[start-image.base:start-image.base+sum(int(c, 16) for c in lengths)])
                       for start, lengths in spans)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row['instructions_sha256']:
            raise ValueError(f'{addr:08x}: reviewed instruction hash mismatch')
        if not re.fullmatch(r'clean_[a-zA-Z0-9_]+', row['kernel']):
            raise ValueError('invalid native kernel symbol')
        if any(i.mnem == 'CALL' for i in insns):
            raise ValueError(f'{addr:08x}: calls require a future explicit callee fixture')
        instruction_addresses = {i.addr for i in insns}
        if any(T.Translator.branch_target(i) not in instruction_addresses
               for i in insns if i.mnem in T.JCC or i.mnem == 'JMP'):
            raise ValueError(f'{addr:08x}: indirect/outward branches require an explicit fixture')
        if any(i.addr in T.INSTRUCTION_PATCHES or i.addr in T.OPERAND_REDIRECTS for i in insns):
            raise ValueError('corpus refuses configured instruction rewrites')
        directory = out / row['address']
        directory.mkdir(exist_ok=True)
        write_input(directory / 'original.asm', '\n'.join(i.raw for i in insns) + '\n')
        write_input(directory / 'original.bin', raw)
        for mode in MODES[:-1]:
            options = SimpleNamespace(eager_flags=False, cpu_locals=mode in ('cpu', 'combined'),
                                      x87_locals=mode in ('x87', 'combined'))
            tr = T.Translator(image, set(functions), options)
            fn = T.Function(addr, name, size, insns)
            fn.measure(image)
            tr.prepare(fn, strict=True)
            if fn.seh_sites or fn.pushed_continuations:
                raise ValueError(f'{addr:08x}: SEH/continuation entries require an explicit fixture')
            body = '\n'.join(tr.translate(fn))
            if re.search(r'\bCALL_FN\(', body):
                raise ValueError(f'{addr:08x}: undeclared outward transfer')
            body = re.sub(r'\b(fn|body|entry)_([0-9a-f]{8})\b', lambda m: mode + '_' + m[0], body)
            path = directory / (mode + '.c')
            write_input(path, '#include "x86.h"\n' + body + '\n')
            sources.append(path)
        provenance.append({**row, 'analysis_name': name, 'original_bytes': len(raw),
                           'original_instructions': len(insns),
                           'x87_instructions': sum(i.mnem.startswith('F') for i in insns),
                           'spans': spans})
    generation_seconds = time.monotonic() - started
    fixture_dir = manifest.parent
    for source in spec['sources']:
        path = (fixture_dir / source).resolve()
        if fixture_dir not in path.parents or not path.is_file():
            raise ValueError('fixture sources must be inside the manifest directory')
        sources.append(path)
    header = (fixture_dir / spec['header']).resolve()
    if fixture_dir not in header.parents or not header.is_file():
        raise ValueError('fixture header must be inside the manifest directory')
    quote = lambda p: json.dumps(str(p))
    write_input(out / 'sources.cmake', 'set(CORPUS_SOURCES\n' + '\n'.join(map(quote, sources)) + '\n)\n')
    declarations = [f'#include {quote(header)}', f'#define CORPUS_COUNT {len(rows)}', '#define CORPUS_MODES 5']
    for addr in addresses:
        for mode in MODES:
            symbol = f'{mode}_fn_{addr}' if mode != 'native' else f'native_{addr}'
            declarations.append(f'void {symbol}(X86 *);')
    declarations.append('static const char *corpus_names[] = {' + ','.join(quote(r['name']) for r in rows) + '};')
    declarations.append('static const unsigned corpus_fixture_ids[] = {' + ','.join(map(str, fixture_ids)) + '};')
    declarations.append('static void (*corpus_functions[][5])(X86 *) = {' + ','.join(
        '{' + ','.join(f'{m}_fn_{a}' if m != 'native' else f'native_{a}' for m in MODES) + '}' for a in addresses) + '};')
    write_input(out / 'corpus-config.h', '\n'.join(declarations) + '\n')
    write_input(out / 'image.bin', image.data)
    started = time.monotonic()
    subprocess.run([cmake, '-S', str(HERE), '-B', str(out), '-DCMAKE_BUILD_TYPE=Release',
                    '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', f'-DKIT_RUNTIME={KIT / "runtime"}',
                    f'-DCORPUS_FIXTURES={fixture_dir}'], check=True)
    subprocess.run([cmake, '--build', str(out), '--parallel', str(jobs)], check=True)
    build_seconds = time.monotonic() - started
    compiler_rows = json.loads((out / 'compile_commands.json').read_text())
    objdump = shutil.which('llvm-objdump')
    if not objdump:
        for candidate in (Path('/opt/homebrew/opt/llvm/bin/llvm-objdump'), Path('/usr/local/opt/llvm/bin/llvm-objdump')):
            if candidate.is_file():
                objdump = str(candidate)
                break
    if not objdump:
        raise ValueError('llvm-objdump required for linked native size reporting')
    executable = out / ('function_corpus.exe' if platform.system() == 'Windows' else 'function_corpus')
    assembly = subprocess.check_output([objdump, '--disassemble', '--no-show-raw-insn', str(executable)], text=True)
    (out / 'native.asm').write_text(assembly)
    sizes = symbol_sizes(assembly)
    completed = subprocess.run([str(executable), str(out / 'image.bin'), str(image.size), str(checks), str(calls), str(trials)],
                               capture_output=True, text=True)
    (out / 'results.txt').write_text(completed.stdout + completed.stderr)
    if completed.returncode:
        raise ValueError(f'corpus failed; see {out / "results.txt"}: {completed.stderr.strip()}')
    times = parse_results(completed.stdout, len(rows), checks, calls, trials)
    report_rows = []
    for r, row in enumerate(provenance):
        variants = {}
        for m, mode in enumerate(MODES):
            symbol = f'{mode}_fn_{row["address"]}' if mode != 'native' else f'native_{row["address"]}'
            variants[mode] = {**sizes[symbol], 'symbol': symbol}
            if mode != 'native':
                source = out / row['address'] / (mode + '.c')
                variants[mode]['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
            if calls:
                values = [times[r, m, t] for t in range(trials)]
                variants[mode]['timing_ns'] = {'median': statistics.median(values), 'min': min(values),
                                             'max': max(values), 'trials': values}
        kernel = dict(sizes[row['kernel']])
        if calls:
            values = [times[r, len(MODES), t] for t in range(trials)]
            kernel['timing_ns'] = {'median': statistics.median(values), 'min': min(values),
                                   'max': max(values), 'trials': values}
        report_rows.append({**row, 'variants': variants, 'native_kernel': kernel})
    import shlex
    compiler = compiler_rows[0].get('arguments', []) or shlex.split(compiler_rows[0]['command'])
    report = {'contract': spec['contract'], 'host': platform.platform(),
              'compiler': subprocess.check_output([compiler[0], '--version'], text=True).splitlines()[0],
              'compile_commands': compiler_rows, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
              'executable_sha256': cfg['game']['sha256'],
              'fixture_sha256': {str(p.relative_to(game_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in fixture_dir.rglob('*') if p.is_file() and
                                 p.suffix in ('.c', '.h', '.cpp', '.hpp') and game_dir in p.parents},
              'runtime_sha256': hashlib.sha256((KIT / 'runtime/x86.h').read_bytes()).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'generation_seconds': generation_seconds, 'build_seconds': build_seconds,
              'checks_per_function': checks, 'calls_per_trial': calls, 'trials': trials,
              'benchmark': spec.get('benchmark', {}),
              'native_executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'helper_text': {k: v for k, v in sizes.items() if not re.match(r'(?:eager|cpu|x87|combined|native|clean)_', k)},
              'functions': report_rows}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    (out / 'report.md').write_text(markdown(report))
    with (out / 'report.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['address', 'name', 'variant', 'original_bytes',
                                              'x87_instructions', 'span_bytes', 'instructions',
                                              'median_ns', 'min_ns', 'max_ns'])
        writer.writeheader()
        for row in report_rows:
            for mode, v in {**row['variants'], 'native_kernel': row['native_kernel']}.items():
                writer.writerow({'address': row['address'], 'name': row['name'], 'variant': mode,
                                 'original_bytes': row['original_bytes'], 'x87_instructions': row['x87_instructions'],
                                 'span_bytes': v['span_bytes'], 'instructions': v['instructions'],
                                 **{key+'_ns': v.get('timing_ns', {}).get(key, '') for key in ('median', 'min', 'max')}})
    print((out / 'report.md').read_text())
    print('Artifacts:', out)
