"""Drive the isolated translator comparison using recorded production commands.

All native compilation runs through the comparison CMake project. The compile
DB is required, and each newly emitted body must match its production chunk.
Unsupported flags/targets fail rather than silently becoming a different test.
"""
from pathlib import Path
import hashlib
import json
import platform
import re
import shlex
import subprocess
import sys

from experiments.x87_llvm.run import llvm_config

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[1]
CMAKE = HERE / 'llvm_compare_native'


def compile_settings(row):
    argv = row.get('arguments') or shlex.split(row['command'])
    if not argv or Path(argv[0]).name not in {'cc', 'clang', 'clang-22'}:
        raise ValueError('comparison requires a plain native Clang compile command')
    flags, i = [], 1
    while i < len(argv):
        a = argv[i]
        if a == '-o':
            i += 2
            continue
        if a == '-c' or a == row['file']:
            i += 1
            continue
        if a in ('-arch', '-isysroot', '-I'):
            if i + 1 == len(argv):
                raise ValueError('incomplete production compiler option')
            value = argv[i + 1]
            if a == '-arch' and value != platform.machine():
                raise ValueError('cross/universal builds are unsupported by the comparison')
            if a != '-arch' and not Path(value).is_absolute():
                raise ValueError('comparison requires absolute include/sysroot paths')
            flags.extend((a, value))
            i += 2
            continue
        if a.startswith('-I') and not Path(a[2:]).is_absolute():
            raise ValueError('comparison requires absolute include paths')
        if not (a in ('-g', '-O2', '-O3', '-Wall', '-Wextra', '-Wno-unused', '-std=c11', '-DNDEBUG')
                or a.startswith(('-I', '-DGUEST_', '-mmacosx-version-min='))):
            raise ValueError(f'unsupported production compiler option: {a}')
        flags.append(a)
        i += 1
    if [f for f in flags if f in ('-O2', '-O3')][-1:] != ['-O2']:
        raise ValueError('bounded comparison requires the production O2 configuration')
    if flags.count('-arch') > 1:
        raise ValueError('universal builds are unsupported')
    return argv[0], flags


def cmake_quote(value):
    value = str(value)
    if ';' in value or '\n' in value:
        raise ValueError('multiline/list-valued compiler arguments are unsupported')
    delimiter = '='
    while f']{delimiter}]' in value:
        delimiter += '='
    return f'[{delimiter}[{value}]{delimiter}]'


def production_settings(database, out, metadata, runtime):
    rows = json.loads(Path(database).read_text())
    candidates = [row for row in rows if Path(row['file']).name.startswith('chunk_')
                  and 'recomp_gen.dir' in (row.get('command') or ' '.join(row['arguments']))]
    chunks = {row['file']: Path(row['file']).read_text() for row in candidates}
    settings = {}
    for addr in metadata['functions']:
        body = (out / addr / 'body.txt').read_text()
        matches = [row for row in candidates if body in chunks[row['file']]]
        if len(matches) != 1:
            raise ValueError(f'{addr}: emitted body does not uniquely match production chunks; regenerate C first')
        row = matches[0]
        # The production build deliberately includes a copied runtime header.
        # Refuse stale copies rather than comparing two different runtimes.
        header = Path(row['file']).parent / 'x86.h'
        if header.read_bytes() != (runtime / 'x86.h').read_bytes():
            raise ValueError('production runtime header is stale; refresh the normal build first')
        compiler, flags = compile_settings(row)
        version = subprocess.check_output([compiler, '--version'], text=True).splitlines()[0]
        if 'clang' not in version.lower():
            raise ValueError('production compiler must be Clang')
        record = {'compiler': compiler, 'version': version, 'flags': flags,
                  'directory': row['directory'], 'chunk': row['file'],
                  'original_command': row.get('command', row.get('arguments')),
                  'body_matches_production': True}
        settings[addr] = record
        (out / addr / 'settings.cmake').write_text(
            'set(PRODUCTION_CC ' + cmake_quote(compiler) + ')\n' +
            'set(PRODUCTION_CWD ' + cmake_quote(row['directory']) + ')\n' +
            'set(PRODUCTION_FLAGS ' + ' '.join(map(cmake_quote, flags)) + ')\n')
    return settings


def function_sizes(disassembly):
    """Decode spans; Mach-O nm does not provide symbol sizes.

    Byte spans include alignment to the next symbol. Instruction counts include
    padding decoded inside that span and all cold/dispatcher paths.
    """
    labels = list(re.finditer(r'^([0-9a-f]+) <([^>]+)>:$', disassembly, re.M))
    sizes = {}
    for left, right in zip(labels, labels[1:]):
        name = left[2].lstrip('_')
        if name not in ('compare_c_native', 'compare_c_llvm', 'compare_raw', 'compare_lifted'):
            continue
        body = disassembly[left.end():right.start()]
        instructions = re.findall(r'^\s*[0-9a-f]+:\s+([a-z][a-z0-9.]*)\b', body, re.M)
        sizes[name] = {'span_bytes': int(right[1], 16) - int(left[1], 16),
                       'instructions': len(instructions),
                       'fused_multiply_adds': sum(op in ('fmadd', 'fmsub', 'fnmadd', 'fnmsub') for op in instructions)}
    if len(sizes) != 4:
        raise ValueError('could not identify all four comparison functions in disassembly')
    return sizes


def run_comparison(manifest, game_dir, out, database, cmake, jobs):
    config = llvm_config()
    bindir = Path(subprocess.check_output([config, '--bindir'], text=True).strip())
    cmakedir = subprocess.check_output([config, '--cmakedir'], text=True).strip()
    out = Path(out).resolve()
    if not Path(database).is_file():
        raise ValueError('production compile_commands.json required; configure the normal build first')
    subprocess.run([sys.executable, str(HERE / 'translate.py'), '--game', str(game_dir),
                    '--out', str(out), '--llvm-compare', str(Path(manifest).resolve())], check=True)
    metadata = json.loads((out / 'translation.json').read_text())
    settings = production_settings(database, out, metadata, KIT / 'runtime')
    (out / 'settings.cmake').write_text('set(LEAVES ' + ' '.join(settings) + ')\n')
    report = {'translation': metadata, 'production': settings,
              'compile_database_sha256': hashlib.sha256(Path(database).read_bytes()).hexdigest(),
              'llvm_version': subprocess.check_output([str(bindir / 'clang'), '--version'], text=True).splitlines()[0],
              'fp_policy': 'production flags unchanged; semantic IR has no contraction/fast-math flags',
              'execution_contract': 'complete normal-exit state; ordinary return supplied by harness'}
    (out / 'build-settings.json').write_text(json.dumps(report, indent=2) + '\n')
    subprocess.run([cmake, '-S', str(CMAKE), '-B', str(out),
                    f'-DLLVM_DIR={cmakedir}', f'-DCMAKE_C_COMPILER={bindir / "clang"}',
                    f'-DCMAKE_CXX_COMPILER={bindir / "clang++"}', f'-DPython3_EXECUTABLE={sys.executable}'], check=True)
    subprocess.run([cmake, '--build', str(out), '--parallel', str(jobs)], check=True)
    for addr in settings:
        directory = out / addr
        lifted = (directory / 'lifted.ll').read_text().split('define void @compare_lifted(', 1)[1].split('\n}', 1)[0]
        raw = (directory / 'input.ll').read_text().split('define void @compare_raw(', 1)[1].split('\n}', 1)[0]
        assert not any(f'@rk_{op}(' in lifted for op in ('push', 'pop', 'read', 'set', 'snapshot'))
        assert lifted.count('@rk_round(') == raw.count('@rk_round(')
        run = subprocess.run([str(directory / 'replay')], capture_output=True, text=True, check=True)
        (directory / 'results.txt').write_text(run.stdout)
        print(addr + ':\n' + run.stdout, end='')
        disassembly = subprocess.check_output([str(bindir / 'llvm-objdump'), '--disassemble',
                                              '--no-show-raw-insn', str(directory / 'replay')], text=True)
        (directory / 'disassembly.txt').write_text(disassembly)
        sizes = function_sizes(disassembly)
        (directory / 'codegen.json').write_text(json.dumps(sizes, indent=2) + '\n')
        print('Native function spans (include alignment padding):', sizes)
    print('Translator comparison artifacts:', out)
