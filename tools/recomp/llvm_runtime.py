"""Build and activate only inspected, fixture-backed LLVM functions.

The three-function comparison runs first using the opaque-boundary ABI. Runtime
objects have unique exported names and no harness definitions. Existing native
overrides remain the fallback; explicitly declared callers can route through
their retained C body to avoid bypassing the selected LLVM entries.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

from llvm_compare_build import cmake_quote, run_comparison
from experiments.x87_llvm.run import llvm_config

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[1]


def write_changed(path, text):
    """Keep the game's table/native sources untouched when activation is unchanged."""
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)


def wrappers(functions, callers, native_header):
    """Generate dispatch-only redirects; translated chunks retain their bodies.

    Runtime selection and counters run under the ordinary guest scheduler baton.
    Disabled activation delegates to the prior native override if one exists.
    Callers use retained C only while activation is enabled. No game constants
    are embedded in this reusable generator.
    """
    addresses = sorted(set(functions) | set(callers))
    if len(addresses) != len(functions) + len(callers):
        raise ValueError('LLVM functions and translated callers must be disjoint')
    header = ['#pragma once', '#include "x86.h"']
    source = ['#include "x86.h"', '#include <stdio.h>', '#include <stdlib.h>']
    if native_header:
        source.append('#include ' + json.dumps(str(native_header)))
    source += [f'static unsigned long long calls[{len(addresses)}];',
               'static int selected = -1, stats;',
               'static void llvm_report(void) {']
    source += [f'  fprintf(stderr, "llvm-runtime: {a} {"llvm" if a in functions else "translated-caller"} calls=%llu\\n", calls[{i}]);'
               for i, a in enumerate(addresses)]
    source += ['}', 'static int llvm_enabled(void) {', '  if (selected < 0) {',
               '    const char *v = getenv("RECOMP_LLVM_ORIGINAL");',
               "    selected = !(v && v[0] == '1' && v[1] == '\\0');",
               '    v = getenv("RECOMP_LLVM_STATS");',
               "    stats = v && v[0] == '1' && v[1] == '\\0';",
               '    if (stats) {', '      atexit(llvm_report);',
               '      fprintf(stderr, "llvm-runtime: %s; synchronous access boundaries\\n", selected ? "enabled" : "original overrides");',
               '    }', '  }', '  return selected;', '}']
    for i, addr in enumerate(addresses):
        symbol = f'recomp_llvm_{addr}' if addr in functions else f'fn_{addr}'
        source += [f'void fn_{addr}(X86 *);', f'void {symbol}(X86 *);',
                   f'#ifndef FN_{addr}', f'#define FN_{addr} fn_{addr}', '#endif',
                   f'void recomp_llvm_dispatch_{addr}(X86 *c) {{',
                   f'  if (!llvm_enabled()) {{ FN_{addr}(c); return; }}',
                   f'  if (stats && ++calls[{i}] == 1) fprintf(stderr, "llvm-runtime: entered {addr} {"llvm" if addr in functions else "translated-caller"}\\n");',
                   f'  {symbol}(c);', '}']
        header += [f'void recomp_llvm_dispatch_{addr}(X86 *);', f'#undef FN_{addr}',
                   f'#define FN_{addr} recomp_llvm_dispatch_{addr}']
    return '\n'.join(header) + '\n', '\n'.join(source) + '\n'


def prepare_runtime(manifest, game, out, database, cmake, jobs, cfg):
    """Replay the exact boundary ABI, then build standalone production objects."""
    manifest = Path(manifest).resolve()
    spec = json.loads(manifest.read_text())
    if spec.get('activation_contract') != 'synchronous-access-boundaries-v1':
        raise ValueError('LLVM activation requires the synchronous access-boundary contract')
    callers = {}
    exe = cfg['developer_exe_path']
    executable_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
    import translate as T
    T.configure(cfg)
    image = T.Image(str(exe))
    for path in spec.get('translated_callers', []):
        p = json.loads((manifest.parent / path).read_text())
        if (manifest.parent / path).parent.joinpath(p['exe']).resolve() != exe.resolve() or p['sha256'] != executable_hash:
            raise ValueError('translated caller executable mismatch')
        addr, size = int(p['entry'], 0), p['size']
        listed = T.load_functions({f'{addr:08x}'})
        if len(listed) != 1 or listed[0][2] != size:
            raise ValueError('translated caller is not an existing census function')
        data = image.data[addr-image.base:addr-image.base+size]
        if hashlib.sha256(data).hexdigest() != p['function_sha256']:
            raise ValueError('translated caller bytes differ from inspected evidence')
        if f'{addr:08x}' in callers:
            raise ValueError('duplicate translated caller')
        callers[f'{addr:08x}'] = p
    comparison = out / 'comparison'
    run_comparison(manifest, game, comparison, database, cmake, jobs, boundary_access=True, benchmark=False)
    report = json.loads((comparison / 'build-settings.json').read_text())
    functions = report['translation']['functions']
    native = cfg['translate'].get('native', {})
    native_header = game / native['header'] if native else None
    header, source = wrappers(functions, callers, native_header)
    write_changed(out / 'activation.h', header)
    write_changed(out / 'dispatch.c', source)
    for addr in functions:
        text = (comparison / addr / 'optimized.ll').read_text().replace('@compare_lifted', f'@recomp_llvm_{addr}')
        (out / addr).mkdir(exist_ok=True)
        write_changed(out / addr / 'runtime.ll', text)
    config = llvm_config()
    bindir = Path(subprocess.check_output([config, '--bindir'], text=True).strip())
    cmakedir = subprocess.check_output([config, '--cmakedir'], text=True).strip()
    write_changed(out / 'settings.cmake', 'set(LEAVES ' + ' '.join(functions) + ')\n' +
                  f'set(COMPARISON {cmake_quote(comparison)})\n')
    subprocess.run([cmake, '-S', str(HERE / 'llvm_runtime_native'), '-B', str(out),
                    f'-DLLVM_DIR={cmakedir}', f'-DCMAKE_C_COMPILER={bindir / "clang"}'], check=True)
    subprocess.run([cmake, '--build', str(out), '--parallel', str(jobs)], check=True)
    # Record activation separately from the comparison's harness-dependent replay.
    report['activation'] = {'contract': spec['activation_contract'], 'enabled_by_default': True,
                            'functions': list(functions), 'translated_callers': list(callers),
                            'fallback': 'RECOMP_LLVM_ORIGINAL=1 retains prior native/C dispatch'}
    (out / 'activation.json').write_text(json.dumps(report, indent=2) + '\n')
    objects = [out / addr / 'runtime.o' for addr in functions]
    write_changed(out / 'integration.cmake',
                  'set(RECOMP_LLVM_OBJECTS ' + ' '.join(map(cmake_quote, objects)) + ')\n' +
                  'set(RECOMP_LLVM_DISPATCH ' + cmake_quote(out / 'dispatch.c') + ')\n' +
                  'set(RECOMP_LLVM_HEADER ' + cmake_quote(out / 'activation.h') + ')\n')
