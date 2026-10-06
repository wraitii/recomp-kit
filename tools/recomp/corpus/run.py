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


def reviewed_calls(row, addresses, insns):
    """Allow only declared direct calls to independently byte-verified rows.

    Callees use the caller's translation mode in separate translation units.
    No host stub, replacement, indirect dispatch or tail transfer is implied.
    """
    declared = row.get('callees', [])
    if (not isinstance(declared, list) or any(not isinstance(a, str) for a in declared)
            or len(set(declared)) != len(declared) or not set(declared) <= set(addresses)):
        raise ValueError(f"{row['address']}: callees must name unique reviewed corpus rows")
    targets, returns = set(), []
    for index, ins in enumerate(insns):
        if ins.mnem != 'CALL':
            continue
        target = T.Translator.branch_target(ins)
        if target is None or f'{target:08x}' not in declared:
            raise ValueError(f"{row['address']}: undeclared/indirect call at {ins.addr:08x}")
        if index + 1 == len(insns):
            raise ValueError(f"{row['address']}: call without mapped continuation")
        targets.add(f'{target:08x}')
        returns.append(insns[index + 1].addr)
    if targets != set(declared):
        raise ValueError(f"{row['address']}: declared callees differ from decoded calls")
    return returns


def bind_reviewed_calls(body, callees, mode):
    """Bind explicit calls without accepting other emitted dispatch paths."""
    targets = set(re.findall(r'\bCALL_FN\(([0-9a-f]{8})\)', body))
    if targets != set(callees) or re.search(r'\brecomp_(?:call|jump|setjmp|seh|unknown_call)\b', body):
        raise ValueError('undeclared or unsupported emitted call/transfer')
    return re.sub(r'\bCALL_FN\(([0-9a-f]{8})\)', lambda m: f'{mode}_fn_{m[1]}(c)', body)


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
             f"Decoded x87 dataflow: {'enabled' if report.get('x87_dataflow') else 'disabled'}.",
             f"Guest-stack forwarding: {'enabled' if report.get('x87_stack_forwarding') else 'disabled'}.",
             f"Decoded integer dataflow: {'enabled' if report.get('decoded_dataflow') else 'disabled'}.", '',
             f"IR SSA in combined mode: {sum(r.get('ir_ssa', {}).get('emitted', False) for r in report['functions'])} functions emitted; x87 comparison mode: {report.get('ir_ssa_x87', 'effects')}; per-function fallbacks are in JSON.", '',
             f"IR SSA CPU publication policy: {report.get('ir_ssa_state', 'strict')}.", '',
             'Native is reviewed C plus its ABI adapter; kernel text is also shown separately.',
             'Translated/native-adapter times include entry reset and indirect-call overhead.',
             'Native-kernel times use the typed host ABI, without guest state/reset. No LTO, FMA or fast-math.',
             'Text spans include alignment and exclude out-of-line helper bodies; see JSON helper sizes.',
             'Declared direct callees use the same translation mode; per-function spans exclude callee bodies.',
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


def run_corpus(manifest, game_dir, out, cmake, jobs, checks=4096, calls=100000, trials=9,
               x87_dataflow=False, x87_stack_forwarding=False, decoded_dataflow=False,
               ir_ssa=False, ir_ssa_x87="effects", ir_ssa_state="strict"):
    """Decode the selected instructions, build isolated variants, validate, report."""
    if ir_ssa_state not in ("strict", "locals"):
        raise ValueError("IR SSA state policy must be strict or locals")
    if ir_ssa_state != "strict" and not ir_ssa:
        raise ValueError("IR SSA state policy requires IR SSA")
    if ir_ssa_x87 not in ("effects", "values", "region", "scalar", "scalar-strict"):
        raise ValueError("IR SSA x87 mode must be effects, values, region, scalar or scalar-strict")
    if ir_ssa_x87 != "effects" and not ir_ssa:
        raise ValueError("IR SSA x87 comparison mode requires IR SSA")
    if decoded_dataflow and not x87_dataflow:
        raise ValueError('decoded dataflow requires decoded x87 dataflow')
    if ir_ssa and (x87_dataflow or x87_stack_forwarding or decoded_dataflow):
        raise ValueError('IR SSA and decoded-dataflow corpus modes must run separately')
    if x87_stack_forwarding and not x87_dataflow:
        raise ValueError('guest-stack forwarding requires decoded x87 dataflow')
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
    call_returns = set()
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
        call_returns.update(reviewed_calls(row, addresses, insns))
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
        ir_result = {'emitted': False, 'reason': 'disabled'}
        for mode in MODES[:-1]:
            options = SimpleNamespace(eager_flags=mode == "eager", cpu_locals=mode in ('cpu', 'combined'),
                                      x87_locals=mode in ('x87', 'combined'),
                                      x87_dataflow=x87_dataflow and mode in ('x87', 'combined'),
                                      x87_stack_forwarding=x87_stack_forwarding and mode in ('x87', 'combined'),
                                      decoded_dataflow=decoded_dataflow and mode == 'combined')
            tr = T.Translator(image, set(functions), options)
            fn = T.Function(addr, name, size, insns)
            fn.measure(image)
            tr.prepare(fn, strict=True)
            if fn.seh_sites or fn.pushed_continuations:
                raise ValueError(f'{addr:08x}: SEH/continuation entries require an explicit fixture')
            body = '\n'.join(tr.translate(fn))
            body = re.sub(r'\b(fn|body|entry)_([0-9a-f]{8})\b', lambda m: mode + '_' + m[0], body)
            body = bind_reviewed_calls(body, row.get('callees', []), mode)
            if ir_ssa and mode == 'combined':
                from ir.lift import Lifter, LiftError
                from ir.census import function_ir
                from ir.ssa import SSAError
                from ir.emit_c import emit
                # Only byte-verified, declared direct callees may be bound; an
                # undeclared or indirect call stays a whole-function fallback.
                call_symbols = {int(a, 16): f'{mode}_fn_{a}'
                                for a in row.get('callees', [])}
                try:
                    lifter = Lifter()
                    fir = function_ir(tr, lifter, fn)
                    body = emit(fir, f'{mode}_fn_{addr:08x}', call_symbols=call_symbols,
                                x87_values=(ir_ssa_x87 == 'values'),
                                x87_region=(ir_ssa_x87 == 'region'),
                                x87_scalar=ir_ssa_x87 in ('scalar', 'scalar-strict'),
                                x87_scalar_strict=(ir_ssa_x87 == 'scalar-strict'),
                                local_state=(ir_ssa_state == 'locals'),
                                resumable_stacks=getattr(T, 'RESUMABLE_STACKS', False))
                    ir_result = {'emitted': True, 'reason': None}
                except (SSAError, LiftError) as error:
                    ir_result = {'emitted': False, 'reason': str(error)}
            prototypes = ''.join(f'void {mode}_fn_{a}(X86 *);\n' for a in row.get('callees', []))
            path = directory / (mode + '.c')
            write_input(path, '#include "x86.h"\n' + prototypes + body + '\n')
            sources.append(path)
        provenance.append({**row, 'analysis_name': name, 'original_bytes': len(raw),
                           'ir_ssa': ir_result,
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
    declarations.append('static const uint32_t corpus_call_returns[] = {CORPUS_RETURN' +
                        ''.join(f',0x{a:08x}u' for a in sorted(call_returns)) + '};')
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
              'x87_dataflow': x87_dataflow, 'x87_stack_forwarding': x87_stack_forwarding, 'decoded_dataflow': decoded_dataflow,
              'ir_ssa': ir_ssa,
              'ir_ssa_x87': ir_ssa_x87, 'ir_ssa_state': ir_ssa_state,
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
