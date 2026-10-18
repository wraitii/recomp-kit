import argparse
from collections import deque
import hashlib
import json
from pathlib import Path

import callgraph
import driver
import game_config
import scan
from ir import x87_facts
from ir import indirect_targets
from ir.x87_cw import Fact, ReturnSummary, UNKNOWN_SUMMARY, fact_dict, summary_dict
from ir.x87_propagation import analyze, ordinary_entries, propagate, summarize
from .common import MASK, settings_for, apply_contracts, preserving, coverage, load_boundaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--facts-out", type=Path, help="emit validated compiler facts for the selected production policy")
    parser.add_argument("--root", action="append", metavar="NAME=HEX_ADDRESS",
                        help="analyze these hypothetical entry contracts instead of the default roots")
    parser.add_argument("--bundle", action="store_true", help="also analyze all selected entry contracts together")
    parser.add_argument("--analysis-contracts", type=Path,
                        help="byte-checked conditional call/edge contracts; never applied to production")
    parser.add_argument("--return-metadata", type=Path,
                        help="hash-pinned completed normal-return context proofs; analysis only")
    parser.add_argument("--scan-cache", type=Path,
                        help="reuse a trusted local scan pickle, validated against translation inputs")
    game_config.add_game_args(parser)
    args = parser.parse_args(argv)
    roots = dict(item.split("=", 1) for item in args.root) if args.root else {}
    roots = {name: int(addr, 16) if isinstance(addr, str) else addr for name, addr in roots.items()}
    settings = settings_for(args)
    args.out = args.out or args.build_root / "recomp/x87-audit/propagation.json"
    ctx = driver.Context(settings)
    fact_identity = x87_facts.identity(settings)
    seed = scan.load(ctx, args.jobs, args.scan_cache)
    scanned = seed.scanned
    prepared = seed.cw_inputs()
    opaque = seed.opaque()
    boundaries = load_boundaries(settings.x87_mode_metadata, prepared, ctx.image,
                                 settings.x87_imports_preserve_pc_rc)
    base_boundaries = boundaries
    return_metadata = None
    if args.return_metadata:
        return_metadata = json.loads(args.return_metadata.read_text())
        if (return_metadata.get('format') != 'x87-cw-context-return-candidate-v1'
                or return_metadata.get('executable_sha256') != hashlib.sha256(Path(settings.exe).read_bytes()).hexdigest()
                or int(return_metadata.get('entry_cw_precondition', {}).get('mask', '0'), 16)
                or return_metadata.get('entry_esp_mod16') is not None
                or not return_metadata.get('all_stack_alignments')):
            raise ValueError('return metadata identity, format or entry preconditions are incompatible')
        if any(row.get('require_imports_preserve_pc_rc')
               for row in return_metadata.get('assumptions', ())) and not settings.x87_imports_preserve_pc_rc:
            raise ValueError('return metadata requires the imports-preserve-PC/RC contract')
        if any(int(row['preserved_mask'], 16) & ~0xffff for row in return_metadata['proofs']):
            raise ValueError('return metadata preservation mask exceeds the control word')
        relations = {int(row['entry'], 16): ReturnSummary(int(row['preserved_mask'], 16), 0, 0)
                     for row in return_metadata['proofs']}
        boundaries = {**boundaries, 'calls': dict(boundaries['calls'])}
        for body in prepared.values():
            if body:
                for _, site, target in body.calls:
                    if target in relations:
                        boundaries['calls'][site] = relations[target]
    indirect = indirect_targets.load(settings, ctx.program)
    call_effects = settings.call_effects
    intrinsic = summarize(prepared, opaque, indirect=indirect, effects=call_effects)
    summaries = summarize(prepared, opaque, boundaries, indirect, call_effects)
    base_summaries = summarize(prepared, opaque, base_boundaries, indirect, call_effects) if return_metadata else summaries
    unconditional = {"intrinsic": sum(preserving(s) for s in intrinsic.values()),
                     "with_boundary_contracts": sum(preserving(s) for s in base_summaries.values()),
                     "x87_intrinsic": sum(preserving(intrinsic[a]) for a, r in scanned.items() if r.x87_count),
                     "x87_with_boundary_contracts": sum(preserving(base_summaries[a]) for a, r in scanned.items() if r.x87_count)}
    contract_rows = json.loads(args.analysis_contracts.read_text()) if args.analysis_contracts else []
    sensitivity = []
    if contract_rows:
        for count in range(1, len(contract_rows) + 1):
            bodies, effects = apply_contracts(prepared, boundaries, ctx.image, contract_rows[:count])
            relations = summarize(bodies, opaque, effects, indirect, call_effects)
            sensitivity.append({"contracts": [r["name"] for r in contract_rows[:count]],
                                "preserving_functions": sum(preserving(s) for s in relations.values()),
                                "preserving_x87_functions": sum(preserving(relations[a]) for a, r in scanned.items() if r.x87_count),
                                "newly_preserving": ["%08x" % a for a in sorted(relations)
                                    if preserving(relations[a]) and not preserving(summaries[a])]})
        prepared, boundaries = bodies, effects
        summaries = relations
    analyses = {addr: analyze(body, summaries, opaque, boundaries=boundaries)
                for addr, body in prepared.items() if body and addr not in opaque}
    graph = callgraph.build(seed, ctx.program, settings, indirect)
    mnemonics = {}
    for addr in scanned:
        fn, _ = ctx.load(addr)
        mnemonics.update((ins.addr, ins.mnem.upper().removeprefix("WAIT ")) for ins in fn.insns)

    def ordinary(contracts):
        return ordinary_entries(prepared, summaries, contracts, graph,
                                opaque, boundaries)

    baseline_entries, baseline, baseline_gaps = ordinary(settings.x87_entry_contracts)
    if args.facts_out:
        if args.analysis_contracts or args.return_metadata:
            parser.error("compiler facts cannot depend on exploratory contracts or candidate metadata")
        if x87_facts.identity(settings) != fact_identity:
            raise ValueError("analysis inputs changed during proof discovery")
        production_opaque = opaque | {a for a, r in scanned.items() if r.exclusion is not None}
        facts = x87_facts.build(ctx, prepared, summaries, baseline, baseline_entries, production_opaque)
        args.facts_out.parent.mkdir(parents=True, exist_ok=True)
        args.facts_out.write_text(facts.to_json() + "\n")
        x87_facts.load(args.facts_out, ctx, prepared, opaque)
    report = {"scope": "analysis only; hypothetical contracts never change game configuration",
              "return_metadata": return_metadata,
              "analysis_contracts": contract_rows, "contract_sensitivity": sensitivity,
              "unconditional_preservation": unconditional,
              "preservation_scope": "with_boundary_contracts additionally depends on analysis_contracts and return_metadata"
                  if contract_rows or return_metadata else "existing production boundary contracts only",
              "functions": len(prepared), "boundary_evidence": boundaries["metadata"],
              "preservation": {"intrinsic": sum(preserving(s) for s in intrinsic.values()),
                               "with_boundary_contracts": sum(preserving(s) for s in summaries.values()),
                               "x87_intrinsic": sum(preserving(intrinsic[a]) for a, r in scanned.items() if r.x87_count),
                               "x87_with_boundary_contracts": sum(preserving(summaries[a]) for a, r in scanned.items() if r.x87_count)},
              "ordinary": coverage(baseline, mnemonics), "ordinary_entry_gaps": baseline_gaps,
              "instruction_boundaries": [{"function": "%08x" % addr,
                  "site": "%08x" % body.sites[index], "effect": effect[0],
                  "mask": "%04x" % effect[1], "value": "%04x" % effect[2], "reason": effect[3],
                  "load_relation": fact_dict(body.word_relations[index])
                      if index in body.word_relations else None}
                  for addr, body in prepared.items() if body
                  for index, effect in body.effects if effect[0] != "keep"],
              "relations": [{"function": "%08x" % addr,
                             "intrinsic": summary_dict(intrinsic[addr]),
                             "with_boundary_contracts": summary_dict(summaries[addr])}
                            for addr in sorted(prepared)],
              "roots": {}}

    def frontier(root):
        queue, seen, blockers = deque([(root, [])]), set(), []
        while queue:
            addr, path = queue.popleft()
            if addr in seen or preserving(summaries.get(addr, UNKNOWN_SUMMARY)):
                continue
            seen.add(addr)
            if addr not in analyses:
                blockers.append({"function": "%08x" % addr, "kind": "unresolved_body",
                                 "path": path, "reason": "opaque or unprepared body",
                                 "preparation_exclusion": scanned[addr].exclusion if addr in scanned else None})
                continue
            body, result = prepared[addr], analyses[addr]
            gap_sites = {gap["site"] for gap in result.gaps}
            for loss in result.losses:
                if loss["kind"] == "possible_mode_change" and loss["target"] is None and loss["site"] not in gap_sites:
                    blockers.append({"function": "%08x" % addr, **loss, "path": path})
            for index in body.entries:
                if index != body.entry:
                    blockers.append({"function": "%08x" % addr, "site": "%08x" % body.sites[index],
                                     "kind": "unknown_entry", "path": path})
            for gap in result.gaps:
                target = int(gap["target"], 16) if gap["target"] else None
                step = {"function": "%08x" % addr, **gap}
                if target in prepared and target not in opaque:
                    queue.append((target, path + [step]))
                else:
                    blockers.append({**step, "path": path})
        return blockers

    for name, addr in roots.items():
        contract = {"function": addr, "mask": MASK, "value": 0}
        contracts = [row for row in settings.x87_entry_contracts if row["function"] != addr] + [contract]
        entries, results, gaps = ordinary(contracts)
        contextual_entries, contextual = propagate(
            prepared, summaries, {addr: Fact(MASK, 0, 0)}, opaque, boundaries)
        report["roots"][name] = {"function": "%08x" % addr,
            "assumption": "hypothetical PC=RC=0 at every primary-entry invocation",
            "ordinary": coverage(results, mnemonics),
            "ordinary_known_entries": ["%08x" % a for a, fact in entries.items() if fact.known_mask & MASK == MASK],
            "caller_joins": [gap for gap in gaps if gap["kind"] == "caller_join"],
            "contextual": coverage(contextual, mnemonics),
            "contextual_known_entries": ["%08x" % a for a, fact in contextual_entries.items() if fact.known_mask & MASK == MASK],
            "root_losses": contextual[addr].losses,
            "preservation_frontier": frontier(addr)}
        row = report["roots"][name]
        print(name, "ordinary", row["ordinary"]["arithmetic_functions"], "functions /",
              row["ordinary"]["arithmetic_sites"], "arithmetic sites; frontier",
              len(row["preservation_frontier"]), flush=True)
    if args.bundle:
        contracts = [row for row in settings.x87_entry_contracts if row["function"] not in roots.values()]
        contracts += [{"function": addr, "mask": MASK, "value": 0} for addr in roots.values()]
        _, results, gaps = ordinary(contracts)
        report["bundle"] = {"assumptions": roots, "ordinary": coverage(results, mnemonics),
                            "caller_joins": [gap for gap in gaps if gap["kind"] == "caller_join"]}
        print("bundle", report["bundle"]["ordinary"]["arithmetic_functions"], "arithmetic functions /",
              report["bundle"]["ordinary"]["arithmetic_sites"], "sites", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + "\n")
    print(report["preservation"], flush=True)
    print(args.out)


if __name__ == "__main__":
    main()
