import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import driver
import game_config
import scan
from ir.x87_cw import UNKNOWN, fact_dict, instruction_mnemonic, join
from ir.call_sites import direct_target
from .common import MASK, settings_for, context_contracts
from .core import CW_NEUTRAL_TRANSCENDENTALS, internal_pcode_flow, cfg_relations, context_graph


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=lambda value: int(value, 16), action='append', required=True)
    parser.add_argument('--call-depth', type=int, default=3)
    parser.add_argument('--max-instructions', type=int, default=20000)
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--metadata-out', type=Path,
                        help='write a compact hash-pinned candidate return-summary file')
    parser.add_argument('--scan-cache', type=Path,
                        help='trusted current scan for caller reach inventories, not coverage proofs')
    parser.add_argument('--contracts', type=Path, help='byte-checked analysis-only frame and termination contracts')
    parser.add_argument('--entry-cw-mask', type=lambda value: int(value, 16), default=0)
    parser.add_argument('--entry-cw-value', type=lambda value: int(value, 16), default=0)
    parser.add_argument('--stack-residue', type=int, choices=range(16),
                        help='analyze entry ESP modulo 16; all 16 cases cover arbitrary alignment')
    parser.add_argument('--all-stack-alignments', action='store_true',
                        help='prove every entry ESP modulo 16 without an alignment assumption')
    game_config.add_game_args(parser)
    args = parser.parse_args(argv)
    if args.call_depth < 0 or args.max_instructions < 1:
        parser.error('depth must be nonnegative and instruction limit positive')
    if args.entry_cw_mask & ~0xffff or args.entry_cw_value & ~args.entry_cw_mask:
        parser.error('entry CW value must fit its 16-bit mask')
    if args.all_stack_alignments and args.stack_residue is not None:
        parser.error('select one residue or all alignments')
    settings = settings_for(args)
    args.out = args.out or args.build_root / "recomp/x87-audit/contexts.json"
    ctx = driver.Context(settings)
    executable_hash = settings.sha256.lower()
    contracts = context_contracts(args.contracts, ctx)
    scanned, reverse = {}, {}
    seed = scan.read_cache(ctx, args.scan_cache) if args.scan_cache else None
    opaque = seed.opaque() if seed else scan.native_replaced(settings)
    if seed:
        scanned = seed.scanned
        for owner, row in scanned.items():
            for target in set(row.calls) | set(row.external_targets):
                target = ctx.program.containing(target)
                if target in scanned:
                    reverse.setdefault(target, set()).add(owner)

    def reach_inventory(roots):
        seen, pending = set(roots), list(roots)
        while pending:
            for caller in reverse.get(pending.pop(), ()):
                if caller not in seen:
                    seen.add(caller)
                    pending.append(caller)
        return {'functions': len(seen), 'x87_functions': sum(bool(scanned[a].x87_count) for a in seen if a in scanned),
                'scope': 'direct-call and mapped-tail ancestor inventory; not preservation or constant-mode coverage'}

    records = []
    for root in args.root:
        record = {'root': '%08x' % root}
        if scanned:
            record['caller_inventory'] = reach_inventory([root])
        try:
            fir, keys, inlined, returns, frontier, transfers, boundaries, terminating = context_graph(
                ctx, root, args.call_depth, args.max_instructions, opaque, contracts)
            cases = []
            residues = range(16) if args.all_stack_alignments else [args.stack_residue]
            for residue in residues:
                facts = {}
                relation = cfg_relations(fir, ctx.lifter, inlined, returns, facts,
                    track_escaped_stack=True, call_boundaries=boundaries, terminating=terminating,
                    entry_cw_mask=args.entry_cw_mask, entry_cw_value=args.entry_cw_value,
                    stack_residue=residue)
                cases.append((residue, relation, facts))
            details = dict(cases[0][2])
            for field in ('outgoing', 'calls'):
                indices = set().union(*(facts.get(field, {}) for _, _, facts in cases))
                details[field] = {i: join(facts[field][i] for _, _, facts in cases
                                                    if i in facts.get(field, {})) for i in indices}
            for field in ('reachable', 'unmatched_returns'):
                details[field] = sorted(set().union(*(facts.get(field, []) for _, _, facts in cases)))
            relations = None if any(relation is None for _, relation, _ in cases) else {
                i: join(relation[i] for _, relation, _ in cases if i in relation)
                for i in set().union(*(relation for _, relation, _ in cases))}
            terminal = [i for i, successors in enumerate(fir.succ) if not successors
                        and i not in terminating and i in details.get('outgoing', {})]
            return_fact = join(details.get('outgoing', {}).get(i, UNKNOWN)
                               for i in terminal)
            exposures = [{'node': i, 'site': '%08x' % fir.insns[i].addr,
                          'target': None if direct_target(fir.insns[i]) is None else
                              '%08x' % direct_target(fir.insns[i]),
                          **fact_dict(fact)}
                         for i, fact in details.get('calls', {}).items()
                         if fact.entry_mask & MASK != MASK]
            obstacles = [{'node': i, 'site': '%08x' % ins.addr, 'kind': 'internal_pcode_flow'}
                         for i, ins in enumerate(fir.insns) if internal_pcode_flow(ins)]
            obstacles += [{'node': i, 'site': '%08x' % ins.addr, 'kind': 'unmodeled_user_operation'}
                          for i, ins in enumerate(fir.insns)
                          if any(op.opc == 'CALLOTHER' for op in ins.ops)
                          and instruction_mnemonic(ins) not in CW_NEUTRAL_TRANSCENDENTALS]
            restored = relations is not None and return_fact.entry_mask & MASK == MASK
            graph = [[ins.addr, [[ret, call] for ret, call in key[1]], list(fir.succ[i])]
                     for i, (key, ins) in enumerate(zip(keys, fir.insns))]
            record.update(classification='contained' if restored and not exposures else
                          'restored_with_call_exposure' if restored else 'unresolved',
                          instructions=len(fir.insns), unique_instructions=len(set(ins.addr for ins in fir.insns)),
                          owners=sorted('%08x' % a for a in {ctx.program.containing(ins.addr) for ins in fir.insns}),
                          inlined_calls=len(inlined), nested_returns=len(returns),
                          contracted_calls=len(boundaries), terminating_paths=len(terminating),
                          used_contracts=sorted({row['function'] for row in boundaries.values()}
                                                | {'%08x' % fir.insns[i].addr for i in terminating}),
                          reachable_instructions=len(details.get('reachable', [])),
                          reached_frontier=[i for i in details.get('calls', {}) if i not in inlined and i not in boundaries],
                          proven_branch_choices=details.get('branch_choices', {}),
                          memory_invalidations=[{'node': i, 'site': '%08x' % fir.insns[i].addr,
                                                 'constant_address': address}
                                                for i, address in details.get('memory_invalidations', {}).items()],
                          call_argument_cw_bits=[{'node': i, 'site': '%08x' % fir.insns[i].addr,
                                                 'arguments': [fact_dict(fact) for fact in facts]}
                                                for i, facts in details.get('call_arguments', {}).items()],
                          tail_sites=['%08x' % site for site in sorted(transfers)],
                          cfg_provenance=relations is not None,
                          alignment_cases=[{'residue': residue, 'checked': relation is not None,
                                            'unmatched_returns': len(facts.get('unmatched_returns', [])),
                                            'return_fact': fact_dict(join(
                                                facts.get('outgoing', {}).get(i, UNKNOWN)
                                                for i, successors in enumerate(fir.succ)
                                                if not successors and i not in terminating
                                                and i in facts.get('outgoing', {})))}
                                           for residue, relation, facts in cases],
                          provenance_failure=details.get('failure'),
                          return_fact=fact_dict(return_fact),
                          unmatched_returns=[{'node': i, 'site': '%08x' % fir.insns[i].addr}
                                             for i in details.get('unmatched_returns', [])],
                          obstacles=obstacles, call_exposure=exposures,
                          load_relations=[{'node': i, 'site': '%08x' % fir.insns[i].addr,
                                           **fact_dict(fact)} for i, fact in (relations or {}).items()],
                          frontier=[{'site': '%08x' % key[0], 'stack': list(key[1]),
                                     'target': None if target is None else '%08x' % target, 'reason': reason}
                                    for key, (target, reason) in sorted(frontier.items())],
                          graph=graph,
                          byte_digest=hashlib.sha256(b''.join(ins.raw for ins in fir.insns)).hexdigest())
        except ValueError as error:
            record.update(classification='unresolved_graph', error=str(error))
        records.append(record)
        print('%08x' % root, record['classification'], record.get('instructions'),
              'inline calls', record.get('inlined_calls'), 'frontier', len(record.get('frontier', [])))
    report = {'input_fingerprint': scan.fingerprint(ctx.settings),
              'executable_sha256': settings.sha256,
              'scope': 'requested normal entry contexts; bounded direct calls; terminating/unwind paths excluded',
              'contracts': contracts,
              'entry_cw_precondition': {'mask': '%04x' % args.entry_cw_mask, 'value': '%04x' % args.entry_cw_value},
              'entry_esp_mod16': args.stack_residue,
              'all_stack_alignments': args.all_stack_alignments,
              'call_depth': args.call_depth, 'counts': dict(Counter(r['classification'] for r in records)),
              'roots': records}
    if scanned:
        report['caller_inventory_union'] = reach_inventory(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, separators=(',', ':')) + '\n')
    if args.metadata_out:
        metadata = {'format': 'x87-cw-context-return-candidate-v1',
                    'executable_sha256': executable_hash, 'assumptions': contracts,
                    'entry_cw_precondition': report['entry_cw_precondition'],
                    'entry_esp_mod16': args.stack_residue,
                    'all_stack_alignments': args.all_stack_alignments,
                    'scope': 'normal returns from listed entries; not constant entry modes',
                    'proofs': [{'entry': r['root'], 'preserved_mask': '%04x' % MASK,
                                'members': r['owners'], 'byte_digest': r['byte_digest'],
                                'call_mode_exposure': r['call_exposure']}
                               for r in records if r['classification'] in ('contained', 'restored_with_call_exposure')]}
        args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_out.write_text(json.dumps(metadata, separators=(',', ':')) + '\n')
        print('candidate metadata', args.metadata_out.stat().st_size, 'bytes', args.metadata_out)
    print(args.out)


if __name__ == '__main__':
    main()
