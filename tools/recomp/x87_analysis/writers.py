import argparse
from collections import Counter, deque
import hashlib
import json
from pathlib import Path

import driver
import game_config
import scan
from ir import x87, x87_boundaries, x87_mode
from .common import MASK, settings_for, apply_contracts, preserving, coverage
from .core import CW_NEUTRAL_TRANSCENDENTALS, internal_pcode_flow, tail_region, cfg_relations, context_graph

mode = x87_mode


def reload_envelopes(prepared, summaries, scanned):
    records = []
    reload_sites = {w['site'] for row in scanned.values() for w in row.writers
                    if w['mnemonic'] in ('FLDCW', 'FLDENV', 'FRSTOR', 'FXRSTOR', 'XRSTOR', 'XRSTORS')}
    for addr, body in sorted(prepared.items()):
        if body is None:
            continue
        calls = {i: (site, target) for i, site, target in body.calls}
        writers = {i for i in body.order if body.sites[i] in reload_sites}
        for origin, site, target in body.calls:
            summary = summaries.get(target, mode.UNKNOWN_SUMMARY)
            if not summary.known_mask & MASK:
                continue
            seen, todo = set(), list(body.succ[origin])
            reloads, exposures, returns = set(), set(), set()
            while todo:
                i = todo.pop()
                if i in seen:
                    continue
                seen.add(i)
                if i in writers:
                    reloads.add(body.sites[i])
                    continue
                if i in calls:
                    exposures.add(body.sites[i])
                if i in body.returns:
                    returns.add(body.sites[i])
                todo.extend(body.succ[i])
            records.append({'function': '%08x' % addr, 'call': '%08x' % site,
                            'modifier': '%08x' % target,
                            'classification': 'reload_before_calls_and_returns' if reloads and not exposures and not returns
                                else 'call_or_return_before_reload',
                            'reloads': ['%08x' % s for s in sorted(reloads)],
                            'calls_before_reload': ['%08x' % s for s in sorted(exposures)],
                            'returns_before_reload': ['%08x' % s for s in sorted(returns)],
                            'scope': 'CFG structure only; a reload is not proof that the saved entry CW survives'})
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scan-cache', type=Path, required=True,
                        help='trusted local scan pickle produced by x87_audit.py propagation')
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--root', type=lambda value: int(value, 16), action='append', default=[],
                        help='also analyze this mapped entry with its own saved-frame context')
    game_config.add_game_args(parser)
    args = parser.parse_args(argv)
    settings = settings_for(args)
    args.out = args.out or args.build_root / "recomp/x87-audit/writers.json"
    ctx = driver.Context(settings)
    seed = scan.read_cache(ctx, args.scan_cache)
    scanned = seed.scanned
    prepared = {a: r.cw_facts for a, r in scanned.items()}
    roots = set(args.root)
    if roots - scanned.keys():
        raise ValueError('analysis roots must be mapped function entries')
    enriched, provenance = dict(prepared), {}
    opaque = seed.opaque()
    lifted, tail_records = {}, {}
    for addr, row in sorted(scanned.items()):
        if not row.writers and prepared[addr] is not None and addr not in roots:
            continue
        try:
            fir, transfers = tail_region(ctx, addr, lifted, opaque)
        except ValueError as error:
            tail_records[addr] = {'error': str(error)}
            continue
        body = mode.prepare(fir)
        tail_records[addr] = {'transfers': transfers, 'instructions': len(fir.insns),
                              'prepared': body is not None}
        if body is None:
            continue
        body = body._replace(effects=tuple(
            (i, ('keep', 0, 0, None) if mode.instruction_mnemonic(fir.insns[i]) in CW_NEUTRAL_TRANSCENDENTALS
             else effect) for i, effect in body.effects))
        tail_records[addr]['provenance_obstacles'] = (
            [{'kind': 'alternate_entry', 'site': '%08x' % site} for site in fir.entries]
            + [{'kind': 'internal_pcode_flow', 'site': '%08x' % ins.addr,
                'mnemonic': mode.instruction_mnemonic(ins)} for ins in fir.insns if internal_pcode_flow(ins)]
            + [{'kind': 'unmodeled_user_operation', 'site': '%08x' % ins.addr,
                'mnemonic': mode.instruction_mnemonic(ins)} for ins in fir.insns
               if any(op.opc == 'CALLOTHER' for op in ins.ops)
               and mode.instruction_mnemonic(ins) not in CW_NEUTRAL_TRANSCENDENTALS])
        relations = cfg_relations(fir, ctx.lifter)
        provenance[addr] = relations
        enriched[addr] = body._replace(word_relations=relations or {})
    summaries = mode.summarize(enriched, opaque)
    rows = []
    for addr, row in sorted(scanned.items()):
        if not row.writers and addr not in roots:
            continue
        body = enriched[addr]
        record = {'function': '%08x' % addr, 'writers': [dict(w, site='%08x' % w['site']) for w in row.writers],
                  'prepared': body is not None, 'cfg_provenance': provenance.get(addr) is not None,
                  'tail_region': tail_records.get(addr),
                  'return_relation': mode.summary_dict(summaries[addr])}
        record['known_return_override_mask'] = '%04x' % (summaries[addr].known_mask & MASK)
        if body is None or addr in opaque:
            fn, entries = ctx.load(addr)
            ctx.tr.analyze(fn, entries)
            reasons = [name for name, value in (
                ('SEH escaping returns', fn.seh_escapes), ('SEH sites', fn.seh_sites),
                ('pushed continuations', fn.pushed_continuations), ('return jumps', fn.return_jumps),
                ('dead instruction addresses', fn.dead_addrs), ('declared nonreturning', addr in ctx.program.noreturn),
                ('opaque replacement/helper', addr in opaque)) if value]
            record.update(classification='unresolved_body', reason=row.exclusion,
                          preparation_obstacles=reasons or ['no closed byte-backed mode CFG'])
        else:
            result = mode.analyze(body, summaries, opaque)
            exposed = [{'site': '%08x' % site, 'target': None if target is None else '%08x' % target,
                        **mode.fact_dict(result.calls[site]),
                        'scope': 'entry PC/RC equality is unproven here; this alone does not prove a mode change'}
                       for _, site, target in body.calls
                       if site in result.calls and result.calls[site].entry_mask & MASK != MASK]
            restored = summaries[addr].preserved_mask & MASK == MASK
            known_override = bool(summaries[addr].known_mask & MASK)
            record.update(classification='restored_with_call_exposure' if restored and exposed else
                          'contained' if restored else 'known_return_override' if known_override else 'unresolved',
                          call_exposure=exposed,
                          load_relations=[{'site': '%08x' % body.sites[i], **mode.fact_dict(f)}
                                          for i, f in sorted(body.word_relations.items())],
                          gaps=list(result.gaps))
        rows.append(record)
    report = {'scope': 'mechanical normal CFG paths only; faults, asynchronous callbacks and unwind containment are not proven',
              'assumptions': 'no added preservation or entry contracts; cached scan must match current translation inputs',
              'counts': dict(Counter(r['classification'] for r in rows if r['writers'])),
              'additional_root_counts': dict(Counter(r['classification'] for r in rows if not r['writers'])),
              'functions': rows,
              'modifier_caller_envelopes': reload_envelopes(enriched, summaries, scanned),
              'unprepared_modifier_callers': [
                  {'function': '%08x' % a, 'modifier': '%08x' % target}
                  for a, row in sorted(scanned.items()) if enriched[a] is None
                  for target in sorted(set(row.calls))
                  if summaries.get(target, mode.UNKNOWN_SUMMARY).known_mask & MASK]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + '\n')
    print(report['counts'])
    for r in rows:
        print(r['function'], r['classification'], r['return_relation'],
              'exposed calls',len(r.get('call_exposure', [])))
    print(args.out)


if __name__ == '__main__':
    main()
