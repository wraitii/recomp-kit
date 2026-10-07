"""Build a byte-verified game corpus and report normal-exit checks and codegen.

Game-owned manifests, typed native references and input/observation contracts
stay outside the kit. Outputs contain private instruction contents and image
bytes, and must stay in the game's ignored build directory.

Two contracts are supported:

``mapped-native-corpus-v1`` pairs every reviewed translation with a typed native
reference. ``mapped-comparison-corpus-v2`` additionally admits explicit
``comparison: "translation-only"`` rows that have no native kernel or adapter,
declares modeled call boundaries (fixture stubs and indirect dispatch) and
per-row guest memory ranges. Translation-only rows never fabricate native
results.
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
CEILING_MODE = 'ceiling'
# Fields of one parsed `CEILING` harness record (see harness.c).
CEILING_FIELDS = ('checked', 'skipped', 'observation', 'memory', 'eax', 'st0', 'boundary', 'first_input')


def corpus_modes(ceiling=False):
    """Mode names in harness order: the experimental ceiling precedes native.

    The ceiling column is appended only when requested, so the default order,
    indices and generated tables are unchanged.
    """
    return MODES[:-1] + ((CEILING_MODE,) if ceiling else ()) + MODES[-1:]
NATIVE_REFERENCE = 'native-reference'
TRANSLATION_ONLY = 'translation-only'
COMPARISONS = (NATIVE_REFERENCE, TRANSLATION_ONLY)
CONTRACTS = ('mapped-native-corpus-v1', 'mapped-comparison-corpus-v2')


def validate_code_map_metadata(metadata, cfg):
    """Refuse a code map that does not pin the configured executable and base."""
    if metadata.get('executable_sha256') != cfg['game']['sha256']:
        raise ValueError('code-map executable hash mismatch')
    if int(metadata['image_base'], 16) != cfg['game']['image_base']:
        raise ValueError('code-map image base mismatch')


def row_comparison(row):
    """Return the declared comparison contract for a row."""
    comparison = row.get('comparison', NATIVE_REFERENCE)
    if comparison not in COMPARISONS:
        raise ValueError(f"{row['address']}: comparison must be native-reference or translation-only")
    return comparison


def _check_hex(value, what):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{8}', value):
        raise ValueError(f'{what} must be an eight-digit lowercase hex address')


def _check_symbol(value, what):
    if (not isinstance(value, str) or not value.isascii()
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value)):
        raise ValueError(f'{what} must be an ASCII C identifier')


def _continuation(insns, index, sizes, address):
    """Return the canonical call continuation or fail.

    The pushed return address must be exactly the next decoded byte, not merely
    the next entry in a possibly-uncoalesced list. The runner supplies decoded
    per-instruction sizes; a missing size is never accepted as proof.
    """
    if index + 1 >= len(insns):
        raise ValueError(f'{address}: call without mapped continuation')
    size = sizes[index] if sizes is not None else getattr(insns[index], 'size', None)
    if type(size) is not int or size <= 0:
        raise ValueError(f'{address}: call instruction size unavailable')
    if insns[index + 1].addr != insns[index].addr + size:
        raise ValueError(f'{address}: non-canonical call continuation at {insns[index].addr:08x}')
    return insns[index + 1].addr


def ssa_call_symbols(mode, boundary, direct, callees):
    """Map direct-call targets to wrapper or same-mode callee symbols.

    Boundary rows bind every direct target to a wrapper that calls
    ``corpus_boundary`` first. Non-boundary rows (including all v1 rows) bind
    the legacy ``callees`` list directly; an empty ``direct`` mapping from the
    v1 path must not silently drop those calls.
    """
    if boundary:
        return {int(a, 16): f'{mode}_fnwrap_{a}' for a in direct}
    return {int(a, 16): f'{mode}_fn_{a}' for a in callees}


def reviewed_calls(row, addresses, insns, sizes=None):
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
        returns.append(_continuation(insns, index, sizes, row['address']))
        targets.add(f'{target:08x}')
    if targets != set(declared):
        raise ValueError(f"{row['address']}: declared callees differ from decoded calls")
    return returns


def review_boundaries(row, addresses, insns, sizes=None):
    """Validate a v2 row's direct and indirect call declarations.

    Returns ``(direct, indirect_sites, indirect_targets, returns)``. ``direct``
    maps an original direct-call target to ``('callee', None)`` or
    ``('stub', fixture_symbol)``. Boundary fields must exactly cover decoded
    calls; no broad unbound call is accepted.
    """
    address = row['address']
    callees = row.get('callees', [])
    if (not isinstance(callees, list) or any(not isinstance(a, str) for a in callees)
            or len(set(callees)) != len(callees) or not set(callees) <= set(addresses)):
        raise ValueError(f"{address}: callees must name unique reviewed corpus rows")
    stubs = row.get('boundary_stubs', {})
    if (not isinstance(stubs, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in stubs.items())):
        raise ValueError(f"{address}: boundary_stubs must map addresses to fixture symbols")
    for target, symbol in stubs.items():
        _check_hex(target, f'{address}: boundary stub target')
        _check_symbol(symbol, f'{address}: boundary stub symbol')
    sites = row.get('indirect_calls', [])
    if (not isinstance(sites, list) or any(not isinstance(s, str) for s in sites)
            or len(set(sites)) != len(sites)):
        raise ValueError(f"{address}: indirect_calls must be unique instruction addresses")
    for site in sites:
        _check_hex(site, f'{address}: indirect call site')
    indirect_targets = row.get('indirect_targets', {})
    if (not isinstance(indirect_targets, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or not v
                   for k, v in indirect_targets.items())):
        raise ValueError(f"{address}: indirect_targets must map guest targets to fixture symbols")
    for token, symbol in indirect_targets.items():
        _check_hex(token, f'{address}: indirect target token')
        _check_symbol(symbol, f'{address}: indirect target symbol')

    direct_insns, indirect_insns = [], []
    for index, ins in enumerate(insns):
        if ins.mnem != 'CALL':
            continue
        target = T.Translator.branch_target(ins)
        if target is None:
            indirect_insns.append((index, ins))
        else:
            direct_insns.append((index, ins, f'{target:08x}'))
    actual_direct = {target for _, _, target in direct_insns}
    if actual_direct != set(callees) | set(stubs) or set(callees) & set(stubs):
        raise ValueError(f"{address}: boundary stubs and callees must exactly partition decoded direct calls")
    actual_sites = {f'{ins.addr:08x}' for _, ins in indirect_insns}
    if set(sites) != actual_sites:
        raise ValueError(f"{address}: indirect_calls differ from decoded indirect sites")
    if actual_sites and not indirect_targets:
        raise ValueError(f"{address}: indirect_targets required when indirect calls are declared")
    if indirect_targets and not actual_sites:
        raise ValueError(f"{address}: indirect_targets declared without indirect calls")

    returns = set()
    direct = {}
    for index, ins, target in direct_insns:
        returns.add(_continuation(insns, index, sizes, address))
        direct[target] = ('stub', stubs[target]) if target in stubs else ('callee', None)
    for index, ins in indirect_insns:
        # The translator pushes the canonical next instruction; a boundary
        # row may not invent a different continuation.
        returns.add(_continuation(insns, index, sizes, address))
    return direct, sorted(actual_sites), indirect_targets, returns


def bind_reviewed_calls(body, callees, mode):
    """Bind explicit calls without accepting other emitted dispatch paths."""
    targets = set(re.findall(r'\bCALL_FN\(([0-9a-f]{8})\)', body))
    if targets != set(callees) or re.search(r'\brecomp_(?:call|jump|setjmp|seh|unknown_call)\b', body):
        raise ValueError('undeclared or unsupported emitted call/transfer')
    return re.sub(r'\bCALL_FN\(([0-9a-f]{8})\)', lambda m: f'{mode}_fn_{m[1]}(c)', body)


def bind_boundary_calls(body, mode, row_addr, direct, indirect_sites):
    """Bind a boundary row to declared wrappers and one explicit dispatch."""
    found = set(re.findall(r'\bCALL_FN\(([0-9a-f]{8})\)', body))
    if found != set(direct):
        raise ValueError('boundary direct calls differ from decoded/declared set')
    if re.search(r'\brecomp_(?:jump|setjmp|seh|unknown_call)\b', body):
        raise ValueError('unsupported emitted transfer in boundary row')
    emitted = len(re.findall(r'\brecomp_call\s*\(', body))
    if emitted != len(indirect_sites):
        raise ValueError('boundary indirect call sites differ from declared calls')
    body = re.sub(r'\bCALL_FN\(([0-9a-f]{8})\)', lambda m: f'{mode}_fnwrap_{m[1]}(c)', body)
    body = re.sub(r'\brecomp_call\(c, t_\);', f'{mode}_indirect_{row_addr}(c, t_);', body)
    if re.search(r'\brecomp_call\b', body):
        raise ValueError('unbound indirect call remains in boundary row')
    return body


def wrap_string_helpers(body):
    """Route movsd/rep_movsd through the fixture hook, keyed by instruction site.

    The decoded translator emits a per-instruction comment carrying the
    original EIP. The generated unit defines CORPUS_MOVSD/CORPUS_REP_MOVSD
    after x86.h; with boundary hooks these call
    ``corpus_movs_site(c, rep, site)`` without changing ``*c``. Without hooks
    the macros fall back to the ordinary helpers and the site is ignored. A
    helper that cannot be attributed to an instruction is an error, never a
    silently unwrapped call.
    """
    site = None
    lines = []
    for line in body.splitlines():
        found = re.search(r'/\*\s*([0-9a-f]{8})\b', line)
        if found:
            site = found.group(1)
        if re.search(r'\b(?:rep_)?movsd\(c\)', line):
            if site is None:
                raise ValueError('string helper without an instruction site')
            line = re.sub(r'\brep_movsd\(c\)', f'CORPUS_REP_MOVSD(c, 0x{site}u)', line)
            line = re.sub(r'(?<![_0-9A-Za-z])movsd\(c\)', f'CORPUS_MOVSD(c, 0x{site}u)', line)
        lines.append(line)
    return '\n'.join(lines)


def wrap_string_helpers_ssa(body, insns):
    """Annotate IR SSA ``B<index>`` blocks with the original instruction EIP.

    ``emit`` numbers its blocks by index into ``fir.insns``, which is built
    one-to-one from the decoded ``fn.insns``. The SSA emitter has no per-
    instruction comments, so the block label supplies the helper site. """
    site = None
    lines = []
    for line in body.splitlines():
        found = re.match(r'\s*B(\d+):', line)
        if found:
            index = int(found.group(1))
            if index >= len(insns):
                raise ValueError('IR SSA block index outside decoded instructions')
            site = f'{insns[index].addr:08x}'
        if re.search(r'\b(?:rep_)?movsd\(c\)', line):
            if site is None:
                raise ValueError('IR SSA string helper without an instruction site')
            line = re.sub(r'\brep_movsd\(c\)', f'CORPUS_REP_MOVSD(c, 0x{site}u)', line)
            line = re.sub(r'(?<![_0-9A-Za-z])movsd\(c\)', f'CORPUS_MOVSD(c, 0x{site}u)', line)
        lines.append(line)
    return '\n'.join(lines)


def boundary_wrappers(mode, row_addr, direct, indirect_sites, indirect_targets):
    """Emit per-mode direct wrappers and the single declared indirect dispatch."""
    if not direct and not indirect_sites:
        return ''
    lines = []
    for target in sorted(direct):
        kind, symbol = direct[target]
        lines.append(f'static void {mode}_fnwrap_{target}(X86 *c) {{')
        lines.append(f'    corpus_boundary(c, 0x{target}u);')
        lines.append(f'    {mode}_fn_{target}(c);' if kind == 'callee' else f'    {symbol}(c);')
        lines.append('}')
    if indirect_sites:
        lines.append(f'static void {mode}_indirect_{row_addr}(X86 *c, uint32_t target) {{')
        lines.append('    switch (target) {')
        for token in sorted(indirect_targets):
            lines.append(f'    case 0x{token}u: corpus_boundary(c, target); {indirect_targets[token]}(c); return;')
        lines.append('    default:')
        lines.append(f'        fprintf(stderr, "corpus: unbound indirect target %08x in {row_addr}\\n", target);')
        lines.append('        abort();')
        lines.append('    }')
        lines.append('}')
    return '\n'.join(lines) + '\n'


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


def parse_calls(output, rows, trial_ms):
    """Per-row calibrated call counts; required for every row of a timed run."""
    counts = {}
    for line in output.splitlines():
        parts = line.split()
        if parts[:1] == ['CALLS']:
            row, count = map(int, parts[1:])
            if row in counts or not count > 0:
                raise ValueError('duplicate or invalid calibrated call count')
            counts[row] = count
    if set(counts) != (set(range(rows)) if trial_ms else set()):
        raise ValueError('incomplete calibrated call counts')
    return counts


def parse_results(output, rows, checks, trial_ms, trials, row_modes=None, row_has_native=None):
    """Refuse missing checks/trials rather than presenting partial runs as passes.

    ``row_modes`` gives the number of translated-plus-adapter modes per row
    (four for translation-only rows, five otherwise). ``row_has_native`` says
    whether the row's kernel mode index ``row_modes[r]`` is present. The
    defaults preserve the original uniform five-mode behavior.
    """
    if row_modes is None:
        row_modes = [len(MODES)] * rows
    if row_has_native is None:
        row_has_native = [True] * rows
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
                raise ValueError('duplicate or invalid timing trial; increase the trial budget')
            times[key] = value
    if checked != dict.fromkeys(range(rows), checks):
        raise ValueError('incomplete correctness checks')
    expected = set()
    if trial_ms:
        for r in range(rows):
            for m in range(row_modes[r]):
                for t in range(trials):
                    expected.add((r, m, t))
            if row_has_native[r]:
                for t in range(trials):
                    expected.add((r, row_modes[r], t))
    if set(times) != expected:
        raise ValueError('incomplete benchmark trials')
    return times


def parse_coverage(output, fixture_ids, required_rows):
    """Parse one exact integer-count coverage object for required rows.

    Native-reference rows may omit coverage; translation-only rows must emit it
    exactly once. Unknown fixture ids, duplicates, malformed payloads and
    negative or non-integer counts are rejected.
    """
    rows_by_fixture = {}
    for index, fixture in enumerate(fixture_ids):
        if fixture in rows_by_fixture:
            raise ValueError('duplicate fixture id in corpus manifest')
        rows_by_fixture[fixture] = index
    coverage = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if not parts or parts[0] != 'COVERAGE':
            continue
        if len(parts) != 3:
            raise ValueError('malformed coverage line')
        try:
            fixture = int(parts[1])
        except ValueError:
            raise ValueError('malformed coverage fixture id')
        if fixture not in rows_by_fixture:
            raise ValueError(f'coverage for unknown fixture {fixture}')
        row = rows_by_fixture[fixture]
        if row in coverage:
            raise ValueError('duplicate coverage record')
        try:
            record = json.loads(parts[2])
        except json.JSONDecodeError:
            raise ValueError('coverage payload is not JSON')
        if (not isinstance(record, dict)
                or any(not isinstance(k, str) or type(v) is not int or v < 0
                       for k, v in record.items())):
            raise ValueError('coverage must map names to nonnegative integer counts')
        coverage[row] = record
    for row in required_rows:
        if row not in coverage:
            raise ValueError('missing coverage for translation-only row')
    return coverage


def parse_ceiling(output, rows, required=True):
    """Parse one exact `CEILING row ...` record per row (experimental column).

    Fields: checked skipped observation memory eax st0 boundary first_input,
    followed by a free-text first-mismatch reason. Counts are nonnegative
    integers; ``first_input`` is -1 when there was no mismatch. A missing or
    duplicate record is an error, never a silent pass.
    """
    records = {}
    for line in output.splitlines():
        parts = line.split(None, 2 + len(CEILING_FIELDS))
        if parts[:1] != ['CEILING']:
            continue
        if len(parts) < 2 + len(CEILING_FIELDS):
            raise ValueError('malformed ceiling record')
        try:
            row = int(parts[1])
            values = [int(v) for v in parts[2:2 + len(CEILING_FIELDS)]]
        except ValueError:
            raise ValueError('malformed ceiling record')
        if not 0 <= row < rows or row in records:
            raise ValueError('ceiling record for unknown or duplicate row')
        if any(v < 0 for v in values[:-1]) or values[-1] < -1:
            raise ValueError('negative ceiling count')
        record = dict(zip(CEILING_FIELDS, values))
        record['reason'] = parts[2 + len(CEILING_FIELDS)].strip() if len(parts) > 2 + len(CEILING_FIELDS) else ''
        records[row] = record
    if required and set(records) != set(range(rows)):
        raise ValueError('missing ceiling record')
    return records


def parse_ceiling_bench(output, rows):
    """Rows whose post-timing sanity check failed in the ceiling variant."""
    seen, invalid = set(), set()
    for line in output.splitlines():
        parts = line.split()
        if parts[:1] != ['CEILING_BENCH']:
            continue
        row, valid = int(parts[1]), int(parts[2])
        if not 0 <= row < rows or row in seen or valid not in (0, 1):
            raise ValueError('malformed ceiling bench record')
        seen.add(row)
        if not valid:
            invalid.add(row)
    if seen and seen != set(range(rows)):
        raise ValueError('incomplete ceiling bench records')
    return invalid


def ceiling_mismatches(record):
    """Total mismatching observations in one parsed ceiling record."""
    return sum(record[k] for k in ('observation', 'memory', 'eax', 'st0', 'boundary'))


def ceiling_note(row):
    """One-line CSV/Markdown summary of a row's experimental ceiling status."""
    info = row.get('ceiling')
    if not info:
        return ''
    obs = info['observation']
    status = 'emitted' if info['emitted'] else f"fallback:{info['fallback']}"
    return (f"relax={','.join(info['relaxations'])}; {status}; observation={obs['status']} "
            f"(checked {obs['checked']}, skipped {obs['skipped']}, mismatches {obs['mismatches']})")


def markdown(report):
    lines = ['# Function corpus report', '',
             f"Host: {report['host']}. Compiler: {report['compiler']}.", '',
             f"Contract: {report['contract']}.",
             f"Decoded x87 dataflow: {'enabled' if report.get('x87_dataflow') else 'disabled'}.",
             f"Guest-stack forwarding: {'enabled' if report.get('x87_stack_forwarding') else 'disabled'}.",
             f"Decoded integer dataflow: {'enabled' if report.get('decoded_dataflow') else 'disabled'}.", '',
             f"IR SSA in combined mode: {sum(r.get('ir_ssa', {}).get('emitted', False) for r in report['functions'])} functions emitted; x87 comparison mode: {report.get('ir_ssa_x87', 'effects')}; per-function fallbacks are in JSON.", '',
             f"IR SSA CPU publication policy: {report.get('ir_ssa_state', 'strict')}.", '',
             'Native is reviewed C plus its ABI adapter; kernel text is also shown separately.',
             'Translation-only rows have no native reference and omit adapter/kernel results entirely.',
             'Translated/native-adapter times include entry reset and indirect-call overhead.',
             'Native-kernel times use the typed host ABI, without guest state/reset. No LTO, FMA or fast-math.',
             'Text spans include alignment and exclude out-of-line helper bodies; see JSON helper sizes.',
             'Declared direct callees use the same translation mode; per-function spans exclude callee bodies.',
             'Original x86 bytes and host text sizes describe different architectures.', '',
             (f"Timing: {report['trial_ms']:g} ms budget per eager trial, {report['trials']} rotating trials; "
              "each row's call count is calibrated once and shared by every variant and trial of that row."
              if report.get('trial_ms') else 'Timing: not run.'), '',
             '| Function | Contract | Original bytes / x87 instructions | Eager bytes | Combined bytes | Native adapter / kernel bytes | Eager / combined / native kernel ns per call | Calls per trial |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in report['functions']:
        def timing(mode):
            values = row['variants'].get(mode, {}).get('timing_ns')
            return f"{values['median']:.2f}" if values else '—'
        variants = row['variants']
        native = variants.get('native')
        native_bytes = f"{native['span_bytes']}" if native else '—'
        kernel = row.get('native_kernel')
        kernel_bytes = f"{kernel['span_bytes']}" if kernel else '—'
        kernel_ns = f"{kernel['timing_ns']['median']:.2f}" if kernel and kernel.get('timing_ns') else '—'
        lines.append(f"| {row['address']} {row['name']} | {row.get('comparison', NATIVE_REFERENCE)} | "
                     f"{row['original_bytes']} / {row['x87_instructions']} | "
                     f"{variants['eager']['span_bytes']} | {variants['combined']['span_bytes']} | "
                     f"{native_bytes} / {kernel_bytes} | {timing('eager')} / {timing('combined')} / {kernel_ns} | "
                     f"{row.get('calls_per_trial', '—')} |")
    info = report.get('ir_ssa_ceiling', {})
    if info.get('enabled'):
        lines += ['', f"## {info['label']} (UNPROVEN, corpus-only)", '',
                  'Relaxations A-E are unproven measurement ceilings, not the agreed performance-mode contract; '
                  'they never apply to production. Full-state equality with eager C is not expected. '
                  'Native rows are checked against declared native observations; translation-only rows against '
                  'eager guest memory ranges (excluding stack residue below the final ESP), EAX and ST0. '
                  'Mismatches are counted, not fatal; inputs outside PC=00/RC=nearest/masked are skipped when E is enabled.', '',
                  '| Function | SSA scalar/locals ns | Ceiling ns | Native adapter ns | SSA bytes | Ceiling bytes | Status | Observation check |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |']
        for row in report['functions']:
            variants = row['variants']

            def t(mode):
                values = variants.get(mode, {}).get('timing_ns')
                return f"{values['median']:.2f}" if values else '—'
            c = row['ceiling']
            o = c['observation']
            status = 'emitted' if c['emitted'] else f"fallback ({c['fallback']}): {c['reason']}"
            if o['mismatches']:
                check = (f"MISMATCH {o['mismatches']} (obs {o['observation']}, mem {o['memory']}, eax {o['eax']}, "
                         f"st0 {o['st0']}, boundary {o['boundary']}) of {o['checked']}; first input {o['first_input']}: {o['reason']}")
            else:
                check = f"{o['status']} ({o['checked']} checked, {o['skipped']} skipped)"
            if c.get('timing_sanity') == 'FAILED':
                check += '; timed-workload sanity check FAILED'
            lines.append(f"| {row['address']} {row['name']} | {t('combined')} | {t('ceiling')} | {t('native')} | "
                         f"{variants['combined']['span_bytes']} | {variants['ceiling']['span_bytes']} | {status} | {check} |")
    boundary_rows = [row for row in report['functions']
                     if row.get('boundary_stubs') or row.get('indirect_calls')]
    if boundary_rows:
        lines += ['', '## Modeled boundaries', '',
                  '| Function | Contract | Real callees | Boundary stubs | Indirect sites | Indirect targets |',
                  '| --- | --- | --- | --- | --- | --- |']
        for row in boundary_rows:
            callees = ', '.join(row.get('callees', [])) or '—'
            stubs = ', '.join(f"{a}->{b}" for a, b in sorted(row.get('boundary_stubs', {}).items())) or '—'
            sites = ', '.join(row.get('indirect_calls', [])) or '—'
            targets = ', '.join(f"{a}->{b}" for a, b in sorted(row.get('indirect_targets', {}).items())) or '—'
            lines.append(f"| {row['address']} | {row.get('comparison')} | {callees} | {stubs} | {sites} | {targets} |")
    coverage_rows = [row for row in report['functions'] if 'coverage' in row]
    if coverage_rows:
        lines += ['', '## Validation coverage', '',
                  'Counts are defined by the game fixtures; their assertions run before report generation.', '',
                  '| Function | Observed coverage |', '| --- | --- |']
        for row in coverage_rows:
            counts = ', '.join(f'{key}={value}' for key, value in row['coverage'].items())
            lines.append(f"| {row['address']} {row['name']} | {counts} |")
    ceiling_note_text = (' (eager, CPU locals, x87 locals and combined; the experimental ceiling column is judged '
                         'separately above)' if info.get('enabled') else '')
    lines += ['', f"All {report['checks_per_function']} inputs/function passed full-state translation checks"
              + ceiling_note_text + ("; native-reference rows also matched their declared observations." if
                 any(r.get('comparison', NATIVE_REFERENCE) == NATIVE_REFERENCE for r in report['functions'])
                 else "."),
              '', 'Native contracts are restricted to the inputs/settings documented by the game fixtures. '
              'This is a practical native target, not a proven lower bound or original-x86 equivalence. '
              'These hot microbenchmarks do not establish game frame-time improvements.', '']
    return '\n'.join(lines)


def run_corpus(manifest, game_dir, out, cmake, jobs, checks=4096, trial_ms=10.0, trials=9,
               x87_dataflow=False, x87_stack_forwarding=False, decoded_dataflow=False,
               ir_ssa=False, ir_ssa_x87="effects", ir_ssa_state="strict", ir_ssa_ceiling=None):
    """Decode the selected instructions, build isolated variants, validate, report.

    ``ir_ssa_ceiling`` (e.g. ``"A,B"``/``"all"``) adds the UNPROVEN, corpus-only
    "SSA ceiling" column; see ``ir/ceiling.py``. It is judged against declared
    observations rather than full-state equality and requires scalar x87 and
    local-state SSA, which also supply its per-function fallback body.
    """
    from ir.ceiling import parse_relaxations, label as ceiling_label
    ceiling = parse_relaxations(ir_ssa_ceiling)
    if ceiling and not (ir_ssa and ir_ssa_x87 == "scalar" and ir_ssa_state == "locals"):
        raise ValueError("SSA ceiling requires --ir-ssa with scalar x87 and locals state")
    modes = corpus_modes(bool(ceiling))
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
    if checks < 1 or trial_ms < 0 or trials < 3:
        raise ValueError('checks must be positive, trial budget nonnegative, trials at least three')
    game_dir, manifest, out = Path(game_dir).resolve(), Path(manifest).resolve(), Path(out).resolve()
    build = game_config.build_root_for(game_dir).resolve() if hasattr(game_config, 'build_root_for') else game_dir / 'build'
    if build not in out.parents:
        raise ValueError('private corpus outputs must stay under the game build directory')
    spec = json.loads(manifest.read_text())
    if spec.get('contract') not in CONTRACTS:
        raise ValueError('explicit mapped-native-corpus-v1 or mapped-comparison-corpus-v2 contract required')
    contract = spec['contract']
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
    metadata, functions = read_map(cfg['code_map_path'])
    validate_code_map_metadata(metadata, cfg)
    T.configure(cfg)
    image = T.Image(T.BINARY)
    if image.base != cfg['game']['image_base']:
        raise ValueError('decoded executable image base mismatch')
    out.mkdir(parents=True, exist_ok=True)
    # Do not leave an earlier successful report looking current after a failure.
    for filename in ('report.json', 'report.md', 'report.csv'):
        (out / filename).unlink(missing_ok=True)
    sources = []
    provenance = []
    call_returns = set()
    row_modes = []
    row_has_native = []
    row_boundary = []
    started = time.monotonic()
    for row in rows:
        addr = int(row['address'], 16)
        comparison = row_comparison(row)
        translation_only = comparison == TRANSLATION_ONLY
        if translation_only:
            if 'kernel' in row:
                raise ValueError(f'{row["address"]}: translation-only rows must not declare a native kernel')
        elif not re.fullmatch(r'clean_[a-zA-Z0-9_]+', row.get('kernel', '')):
            raise ValueError('invalid native kernel symbol')
        memory_ranges = row.get('memory_ranges')
        if memory_ranges is not None and (
                not isinstance(memory_ranges, list) or not memory_ranges
                or any(not isinstance(item, list) or len(item) != 2
                       or type(item[0]) is not int or type(item[1]) is not int
                       or item[0] < 0 or item[1] <= 0
                       or item[0] > 0xffffffff or item[1] > 0xffffffff
                       or sum(item) > 0x100000000 for item in memory_ranges)):
            raise ValueError(f'{row["address"]}: memory_ranges must be nonempty [start,size] integer pairs')
        name, size, spans = functions[addr]
        insns, ins_sizes = [], []
        for start, lengths in spans:
            for ins, digit in zip(decode_span(image, start, lengths), lengths):
                insns.append(ins)
                ins_sizes.append(int(digit, 16))
        raw = b''.join(bytes(image.data[start-image.base:start-image.base+sum(int(c, 16) for c in lengths)])
                       for start, lengths in spans)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row['instructions_sha256']:
            raise ValueError(f'{addr:08x}: reviewed instruction hash mismatch')
        if contract == 'mapped-native-corpus-v1' and (
                'comparison' in row or 'boundary_stubs' in row
                or 'indirect_calls' in row or 'indirect_targets' in row
                or 'memory_ranges' in row):
            raise ValueError(f'{addr:08x}: v1 corpus rejects comparison/boundary declarations')
        if contract == 'mapped-comparison-corpus-v2':
            direct, indirect_sites, indirect_targets, returns = review_boundaries(
                row, addresses, insns, ins_sizes)
        else:
            direct, indirect_sites, indirect_targets = {}, [], {}
            returns = reviewed_calls(row, addresses, insns, ins_sizes)
        boundary = bool(row.get('boundary_stubs')) or bool(row.get('indirect_calls'))
        call_returns.update(returns)
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
        ceiling_result = None
        for mode in modes[:-1]:
            options = SimpleNamespace(eager_flags=mode == "eager", cpu_locals=mode in ('cpu', 'combined', CEILING_MODE),
                                      x87_locals=mode in ('x87', 'combined', CEILING_MODE),
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
            if boundary:
                body = bind_boundary_calls(body, mode, row['address'], direct, indirect_sites)
            else:
                body = bind_reviewed_calls(body, row.get('callees', []), mode)
            wrapped = False
            if ir_ssa and mode in ('combined', CEILING_MODE):
                from ir.lift import Lifter, LiftError
                from ir.census import function_ir
                from ir.ssa import SSAError
                from ir.emit_c import emit
                # Only byte-verified, declared direct callees may be bound; an
                # undeclared or indirect call stays a whole-function fallback.
                call_symbols = ssa_call_symbols(mode, boundary, direct, row.get('callees', []))
                indirect_symbol = (f'{mode}_indirect_{row["address"]}'
                                   if boundary and indirect_sites else None)

                def ssa_emit(relax):
                    return emit(fir, f'{mode}_fn_{addr:08x}', call_symbols=call_symbols,
                                indirect_call_symbol=indirect_symbol,
                                x87_values=(ir_ssa_x87 == 'values'),
                                x87_region=(ir_ssa_x87 == 'region'),
                                x87_scalar=ir_ssa_x87 in ('scalar', 'scalar-strict'),
                                x87_scalar_strict=(ir_ssa_x87 == 'scalar-strict'),
                                local_state=(ir_ssa_state == 'locals'),
                                resumable_stacks=getattr(T, 'RESUMABLE_STACKS', False),
                                _ceiling=relax)
                try:
                    lifter = Lifter()
                    fir = function_ir(tr, lifter, fn)
                    if mode == CEILING_MODE:
                        # UNPROVEN ceiling: any unsupported shape keeps the ordinary
                        # SSA scalar/locals body for the whole function.
                        try:
                            body = ssa_emit(ceiling)
                            ceiling_result = {'emitted': True, 'reason': None, 'fallback': None}
                        except SSAError as error:
                            body = ssa_emit(frozenset())
                            ceiling_result = {'emitted': False, 'reason': str(error),
                                              'fallback': 'ssa-scalar-locals'}
                    else:
                        body = ssa_emit(frozenset())
                    if contract == 'mapped-comparison-corpus-v2':
                        body = wrap_string_helpers_ssa(body, insns)
                        wrapped = True
                    if mode == 'combined':
                        ir_result = {'emitted': True, 'reason': None}
                except (SSAError, LiftError) as error:
                    if mode == CEILING_MODE:
                        ceiling_result = {'emitted': False, 'reason': str(error), 'fallback': 'decoded'}
                    else:
                        ir_result = {'emitted': False, 'reason': str(error)}
            if not wrapped and contract == 'mapped-comparison-corpus-v2':
                body = wrap_string_helpers(body)
            prototypes = ''.join(f'void {mode}_fn_{a}(X86 *);\n' for a in row.get('callees', []))
            include = ['#include "x86.h"']
            if contract == 'mapped-comparison-corpus-v2':
                include.append(f'#include {json.dumps(spec["header"])}')
                if boundary and indirect_sites:
                    include += ['#include <stdio.h>', '#include <stdlib.h>']
                include += ['#if defined(CORPUS_BOUNDARY_HOOKS)',
                            'void corpus_movs_site(X86 *, int, uint32_t);',
                            '#define CORPUS_MOVSD(c, site) corpus_movs_site((c), 0, (uint32_t)(site))',
                            '#define CORPUS_REP_MOVSD(c, site) corpus_movs_site((c), 1, (uint32_t)(site))',
                            '#else',
                            '#define CORPUS_MOVSD(c, site) movsd(c)',
                            '#define CORPUS_REP_MOVSD(c, site) rep_movsd(c)',
                            '#endif']
                fixture_symbols = sorted({symbol for _kind, symbol in direct.values() if symbol}
                                         | set(indirect_targets.values()))
                include += [f'void {symbol}(X86 *);' for symbol in fixture_symbols]
            wrappers = (boundary_wrappers(mode, row['address'], direct, indirect_sites, indirect_targets)
                        if boundary else '')
            path = directory / (mode + '.c')
            write_input(path, '\n'.join(include) + '\n' + prototypes + wrappers + body + '\n')
            sources.append(path)
        provenance.append({**row, 'comparison': comparison, 'analysis_name': name,
                           'original_bytes': len(raw), 'ir_ssa': ir_result,
                           **({'ir_ssa_ceiling': ceiling_result} if ceiling else {}),
                           'original_instructions': len(insns),
                           'x87_instructions': sum(i.mnem.startswith('F') for i in insns),
                           'spans': spans})
        row_modes.append(len(modes) - 1 if translation_only else len(modes))
        row_has_native.append(0 if translation_only else 1)
        row_boundary.append(1 if boundary else 0)
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
    declarations = [f'#include {quote(header)}', f'#define CORPUS_COUNT {len(rows)}',
                    f'#define CORPUS_MODES {len(modes)}',
                    f'#define CORPUS_MODE_NATIVE {len(modes) - 1}']
    if ceiling:
        declarations.append(f'#define CORPUS_MODE_CEILING {modes.index(CEILING_MODE)}')
        if 'E' in ceiling:
            # Ceiling E assumes PC=00/nearest/masked; only such inputs are compared.
            declarations.append('#define CORPUS_CEILING_E_DOMAIN 1')
    declarations.append('static const uint32_t corpus_call_returns[] = {CORPUS_RETURN' +
                        ''.join(f',0x{a:08x}u' for a in sorted(call_returns)) + '};')
    for row in provenance:
        for mode in modes:
            if row['comparison'] == TRANSLATION_ONLY and mode == 'native':
                continue
            symbol = f'{mode}_fn_{row["address"]}' if mode != 'native' else f'native_{row["address"]}'
            declarations.append(f'void {symbol}(X86 *);')
    declarations.append('static const char *corpus_names[] = {' + ','.join(quote(r['name']) for r in rows) + '};')
    declarations.append('static const unsigned corpus_fixture_ids[] = {' + ','.join(map(str, fixture_ids)) + '};')
    declarations.append('struct corpus_range { uint32_t start, size; };')
    needs_default = False
    explicit_totals = []
    range_symbols = []
    range_counts = []
    for row in provenance:
        symbol = 'corpus_ranges_' + row['address']
        ranges = row.get('memory_ranges')
        if ranges is None:
            needs_default = True
            declarations.append(f'static const struct corpus_range {symbol}[] = '
                                '{{CORPUS_SCRATCH, CORPUS_SCRATCH_SIZE}};')
            counts = 1
        else:
            explicit_totals.append(sum(item[1] for item in ranges))
            body = ','.join(f'{{0x{a:08x}u,0x{s:08x}u}}' for a, s in ranges)
            declarations.append(f'static const struct corpus_range {symbol}[] = {{ {body} }};')
            counts = len(ranges)
        range_symbols.append(symbol)
        range_counts.append(counts)
    max_explicit = max(explicit_totals) if explicit_totals else 1
    if needs_default:
        declarations.append(f'#define CORPUS_MAX_SNAPSHOT (CORPUS_SCRATCH_SIZE > {max_explicit}u '
                            f'? CORPUS_SCRATCH_SIZE : {max_explicit}u)')
    else:
        declarations.append(f'#define CORPUS_MAX_SNAPSHOT {max_explicit}u')
    declarations.append('static const struct corpus_range *corpus_memory_ranges[] = {'
                        + ','.join(range_symbols) + '};')
    declarations.append('static const unsigned corpus_memory_range_counts[] = {'
                        + ','.join(map(str, range_counts)) + '};')
    declarations.append('static const unsigned corpus_row_modes[] = {' + ','.join(map(str, row_modes)) + '};')
    declarations.append('static const unsigned corpus_has_native[] = {' + ','.join(map(str, row_has_native)) + '};')
    declarations.append('static const unsigned corpus_is_boundary[] = {' + ','.join(map(str, row_boundary)) + '};')
    declarations.append(f'static void (*corpus_functions[][{len(modes)}])(X86 *) = {{' + ','.join(
        '{' + ','.join(
            (f'{m}_fn_{r["address"]}' if m != 'native' else f'native_{r["address"]}')
            if not (r['comparison'] == TRANSLATION_ONLY and m == 'native') else '0'
            for m in modes) + '}' for r in provenance) + '};')
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
    completed = subprocess.run([str(executable), str(out / 'image.bin'), str(image.size), str(checks), repr(float(trial_ms)), str(trials)],
                               capture_output=True, text=True)
    (out / 'results.txt').write_text(completed.stdout + completed.stderr)
    if completed.returncode:
        raise ValueError(f'corpus failed; see {out / "results.txt"}: {completed.stderr.strip()}')
    times = parse_results(completed.stdout, len(rows), checks, trial_ms, trials, row_modes, row_has_native)
    row_calls = parse_calls(completed.stdout, len(rows), trial_ms)
    required_coverage = [r for r, row in enumerate(provenance) if row['comparison'] == TRANSLATION_ONLY]
    coverage = parse_coverage(completed.stdout, fixture_ids, required_coverage)
    ceiling_records = parse_ceiling(completed.stdout, len(rows)) if ceiling else {}
    ceiling_bench_invalid = parse_ceiling_bench(completed.stdout, len(rows)) if ceiling and trial_ms else set()
    report_rows = []
    for r, row in enumerate(provenance):
        translation_only = row['comparison'] == TRANSLATION_ONLY
        variants = {}
        for m, mode in enumerate(modes):
            if translation_only and mode == 'native':
                continue
            symbol = f'{mode}_fn_{row["address"]}' if mode != 'native' else f'native_{row["address"]}'
            variants[mode] = {**sizes[symbol], 'symbol': symbol}
            if mode != 'native':
                source = out / row['address'] / (mode + '.c')
                variants[mode]['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
            if trial_ms:
                values = [times[r, m, t] for t in range(trials)]
                variants[mode]['timing_ns'] = {'median': statistics.median(values), 'min': min(values),
                                             'max': max(values), 'trials': values}
        entry = {**row, 'variants': variants}
        if trial_ms:
            entry['calls_per_trial'] = row_calls[r]
        if not translation_only:
            kernel = dict(sizes[row['kernel']])
            if trial_ms:
                values = [times[r, len(modes), t] for t in range(trials)]
                kernel['timing_ns'] = {'median': statistics.median(values), 'min': min(values),
                                       'max': max(values), 'trials': values}
            entry['native_kernel'] = kernel
        if r in coverage:
            entry['coverage'] = coverage[r]
        if ceiling:
            record = ceiling_records[r]
            entry['ceiling'] = {
                **row['ir_ssa_ceiling'], 'relaxations': sorted(ceiling),
                'method': 'translation-only: guest ranges (minus stack residue), EAX, ST0 vs eager'
                          if translation_only else 'declared native observations (object window, EAX/AL/ST0)',
                'timing_sanity': 'FAILED' if r in ceiling_bench_invalid else ('pass' if trial_ms else 'not run'),
                'observation': {**record, 'mismatches': ceiling_mismatches(record),
                                'status': ('no in-domain inputs' if not record['checked'] else
                                           'pass' if not ceiling_mismatches(record) else 'mismatch')}}
        report_rows.append(entry)
    import shlex
    compiler = compiler_rows[0].get('arguments', []) or shlex.split(compiler_rows[0]['command'])
    report = {'contract': contract, 'host': platform.platform(),
              'x87_dataflow': x87_dataflow, 'x87_stack_forwarding': x87_stack_forwarding, 'decoded_dataflow': decoded_dataflow,
              'ir_ssa': ir_ssa,
              'ir_ssa_x87': ir_ssa_x87, 'ir_ssa_state': ir_ssa_state,
              'ir_ssa_ceiling': {'enabled': bool(ceiling), 'relaxations': sorted(ceiling),
                                 'label': ceiling_label(ceiling) if ceiling else None,
                                 'unproven': 'corpus-only experiment, not the agreed performance-mode contract'},
              'compiler': subprocess.check_output([compiler[0], '--version'], text=True).splitlines()[0],
              'compile_commands': compiler_rows, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
              'executable_sha256': cfg['game']['sha256'],
              'fixture_sha256': {str(p.relative_to(game_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in fixture_dir.rglob('*') if p.is_file() and
                                 p.suffix in ('.c', '.h', '.cpp', '.hpp') and game_dir in p.parents},
              'runtime_sha256': hashlib.sha256((KIT / 'runtime/x86.h').read_bytes()).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'generation_seconds': generation_seconds, 'build_seconds': build_seconds,
              'checks_per_function': checks, 'trial_ms': trial_ms, 'trials': trials,
              'row_modes': row_modes, 'row_has_native': row_has_native,
              'benchmark': spec.get('benchmark', {}),
              'native_executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'helper_text': {k: v for k, v in sizes.items() if not re.match(r'(?:eager|cpu|x87|combined|ceiling|native|clean)_', k)},
              'functions': report_rows}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    (out / 'report.md').write_text(markdown(report))
    with (out / 'report.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['address', 'name', 'comparison', 'variant', 'original_bytes',
                                              'x87_instructions', 'span_bytes', 'instructions', 'calls_per_trial',
                                              'median_ns', 'min_ns', 'max_ns', 'note'])
        writer.writeheader()
        for row in report_rows:
            sources = dict(row['variants'])
            if 'native_kernel' in row:
                sources['native_kernel'] = row['native_kernel']
            for mode, v in sources.items():
                writer.writerow({'address': row['address'], 'name': row['name'],
                                 'comparison': row['comparison'], 'variant': mode,
                                 'original_bytes': row['original_bytes'], 'x87_instructions': row['x87_instructions'],
                                 'span_bytes': v['span_bytes'], 'instructions': v['instructions'],
                                 'calls_per_trial': row.get('calls_per_trial', ''),
                                 **{key+'_ns': v.get('timing_ns', {}).get(key, '') for key in ('median', 'min', 'max')},
                                 'note': ceiling_note(row) if mode == CEILING_MODE else ''})
    print((out / 'report.md').read_text())
    print('Artifacts:', out)
