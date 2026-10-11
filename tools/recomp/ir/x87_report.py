"""Read-only reporting for control-word facts, exposure and private selection."""
from .x87_cw import CW_FAST, CW_PC_RC, UNKNOWN_SUMMARY, fact_dict, summary_dict, writer_effect
from .x87_propagation import analyze


def _addr(value):
    if isinstance(value, str):
        return int(value, 16)
    return int(value)


def _addr_hex(value):
    return "%08x" % _addr(value)


def _closure(seeds, edges):
    seen, todo = set(seeds), list(seeds)
    while todo:
        node = todo.pop()
        for target in edges.get(node, ()):
            if target not in seen:
                seen.add(target)
                todo.append(target)
    return seen


def exposure_report(prepared_by_addr, direct_calls_by_addr, opaque_targets=(),
                    x87_counts=None, local_writers=None):
    """Inventory byte-backed CW writers and conservative direct-call exposure.

    ``local_writers`` may include byte-decoded sites from functions that could
    not be prepared. Each row has ``site`` and ``mnemonic``. Known effects are
    recomputed with :func:`x87_cw.writer_effect`; optional supplied effect details are
    ignored. Direct-call descendants and callers are exposure overapproximations
    because this inventory does not reason about call-site ordering or paths.
    """
    prepared_by_addr = {_addr(addr): body for addr, body in prepared_by_addr.items()}
    graph = {}
    for addr, targets in direct_calls_by_addr.items():
        graph.setdefault(_addr(addr), set()).update(_addr(t) for t in targets if t is not None)
    for addr, body in prepared_by_addr.items():
        if body:
            graph.setdefault(addr, set()).update(target for _, _, target in body.calls
                                                  if target is not None)
    graph = {addr: tuple(sorted(targets)) for addr, targets in graph.items()}

    if isinstance(opaque_targets, dict):
        opaque = {_addr(addr): str(reason) for addr, reason in opaque_targets.items()}
    else:
        opaque = {_addr(addr): "opaque replacement, import, or dispatch target"
                  for addr in opaque_targets}

    counts = {_addr(addr): int(count) for addr, count in (x87_counts or {}).items()}
    for addr, body in prepared_by_addr.items():
        if body is not None:
            counts.setdefault(addr, int(body.x87_count))
    x87_functions = {addr for addr, count in counts.items() if count > 0}

    rows = {}

    def add_writer(function, site, mnemonic, effect, evidence):
        kind, mask, value, reason = effect
        classification = "known_set" if kind == "set" else "memory_load"
        row = {"function": _addr_hex(function), "site": _addr_hex(site),
               "mnemonic": mnemonic, "classification": classification,
               "mask": "%04x" % mask if kind == "set" else None,
               "value": "%04x" % value if kind == "set" else None,
               "reason": reason, "evidence": evidence}
        key = (_addr(function), _addr(site), classification)
        current = rows.get(key)
        if current is None or (current["mnemonic"] is None and mnemonic is not None):
            rows[key] = row

    for function, body in prepared_by_addr.items():
        if body is None:
            continue
        for i, effect in body.effects:
            kind, mask, value, reason = effect
            if kind == "set":
                classification = "known_set"
            elif (kind == "unknown" and reason in (
                    "control word loaded from guest memory",
                    "extended state restored from guest memory")):
                classification = "memory_load"
            else:
                continue
            row = {"function": _addr_hex(function), "site": _addr_hex(body.sites[i]),
                   "mnemonic": None, "classification": classification,
                   "mask": "%04x" % mask if kind == "set" else None,
                   "value": "%04x" % value if kind == "set" else None,
                   "reason": reason, "evidence": "prepared byte-backed FIR"}
            key = (_addr(function), _addr(body.sites[i]), classification)
            rows.setdefault(key, row)

    for function, sites in (local_writers or {}).items():
        function = _addr(function)
        for site_row in sites:
            mnemonic = str(site_row["mnemonic"]).upper()
            effect = writer_effect(mnemonic)
            if effect is None:
                continue
            site = _addr(site_row["site"])
            add_writer(function, site, mnemonic, effect,
                       site_row.get("evidence", "original decoded bytes"))

    writer_rows = sorted(rows.values(), key=lambda row: (row["function"], row["site"],
                                                          row["mnemonic"] or ""))
    writer_funcs = {_addr(row["function"]) for row in writer_rows}

    unknown = []
    unknown_seen = set()

    def add_unknown(function, site, reason, mnemonic=None):
        row = {"function": _addr_hex(function),
               "site": None if site is None else _addr_hex(site),
               "mnemonic": mnemonic, "reason": reason}
        key = (row["function"], row["site"], row["mnemonic"], row["reason"])
        if key not in unknown_seen:
            unknown_seen.add(key)
            unknown.append(row)

    for function, body in prepared_by_addr.items():
        if body is None:
            add_unknown(function, None, "function body could not be prepared")
            continue
        for i, effect in body.effects:
            kind, _mask, _value, reason = effect
            if kind == "unknown" and reason not in (
                    "control word loaded from guest memory",
                    "extended state restored from guest memory"):
                add_unknown(function, body.sites[i], reason or "unknown instruction effect")
        for _i, site, target in body.calls:
            if target is None:
                add_unknown(function, site, "indirect call target is unresolved")
    for function, reason in opaque.items():
        add_unknown(function, None, reason)
    for caller, targets in graph.items():
        for target in targets:
            if target not in prepared_by_addr and target not in opaque:
                add_unknown(target, None, "direct call target has no prepared body (called by %s)"
                            % _addr_hex(caller))

    unknown_funcs = {_addr(row["function"]) for row in unknown}
    possible_sources = writer_funcs | unknown_funcs
    forward = _closure(writer_funcs, graph) - writer_funcs
    possible_descendants = _closure(possible_sources, graph) - possible_sources
    reverse = {}
    for caller, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(caller)
    reverse = {addr: tuple(sorted(callers)) for addr, callers in reverse.items()}
    callers = _closure(possible_sources, reverse) - possible_sources

    excluded = possible_sources | possible_descendants | callers
    candidates = x87_functions - excluded
    return {
        "scope": "static translated/direct-call exposure inventory; closures overapproximate and are not preservation proofs",
        "writers": writer_rows,
        "unknown_boundaries": sorted(unknown, key=lambda row: (row["function"], row["site"] or "",
                                                                 row["reason"])),
        "writer_functions": sorted(_addr_hex(addr) for addr in writer_funcs),
        "possible_effect_sources": sorted(_addr_hex(addr) for addr in possible_sources),
        "writer_descendants": sorted(_addr_hex(addr) for addr in forward),
        "possible_effect_descendants": sorted(_addr_hex(addr) for addr in possible_descendants),
        "reverse_callers": sorted(_addr_hex(addr) for addr in callers),
        "opaque_gaps": [{"function": _addr_hex(addr), "reason": opaque[addr]}
                        for addr in sorted(opaque)],
        "candidate_functions": sorted(_addr_hex(addr) for addr in candidates),
        "counts": {
            "x87_only_functions": len(x87_functions),
            "direct_writer_functions": len(writer_funcs),
            "direct_writer_sites": len(writer_rows),
            "x87_writer_or_descendant_functions": len((writer_funcs | forward) & x87_functions),
            "x87_outside_direct_writer_cone": len(x87_functions - writer_funcs - forward),
            "writer_descendant_functions": len(forward),
            "possible_effect_descendant_functions": len(possible_descendants),
            "reverse_caller_functions": len(callers),
            "unknown_boundary_functions": len(unknown_funcs),
            "x87_unknown_boundary_functions": len(unknown_funcs & x87_functions),
            "x87_reverse_caller_functions": len(callers & x87_functions),
            "remaining_candidates": len(candidates),
        },
    }


def mode_report(settings, prepared, summaries, opaque, known_modes, x87_counts, exposure, selected=()):
    functions = []
    for addr in sorted(set(selected) | settings.state_contract_callers | settings.checked_returns):
        body = prepared.get(addr)
        row = {"function": "%08x" % addr,
               "x87_instructions": x87_counts.get(addr, 0),
               "return_relation": summary_dict(summaries.get(addr, UNKNOWN_SUMMARY))}
        if body is None or addr in opaque:
            row["reason"] = "opaque function"
        else:
            analysis = analyze(body, summaries, opaque)
            targets = {site: target for _, site, target in body.calls}
            row["calls"] = [{"site": "%08x" % site,
                             "target": "%08x" % targets[site] if targets[site] is not None else None,
                             "incoming": fact_dict(fact)}
                            for site, fact in sorted(analysis.calls.items())]
            row["return_fact"] = fact_dict(analysis.returns)
            row["losses"] = analysis.losses
        functions.append(row)
    x87_functions = {addr for addr, count in x87_counts.items() if count}
    covered = {addr for addr in x87_functions
               if summaries.get(addr, UNKNOWN_SUMMARY).preserved_mask & CW_PC_RC == CW_PC_RC}
    return {"scope": "translated paths without hooks or dispatch overrides",
            "exposure": exposure,
            "coverage": {"x87_functions": len(x87_functions),
                         "mode_preserving": len(covered),
                         "without_mode_preservation_proof": len(x87_functions - covered),
                         "opaque_or_unprepared": sum(prepared.get(addr) is None or addr in opaque
                                                     for addr in x87_functions)},
            "guarded_private_targets": ["%08x" % addr for addr in sorted(known_modes)],
            "private_entry_guard": {"mask": "%04x" % CW_FAST.mask, "value": "%04x" % CW_FAST.value},
            "entry_fact": "symbolic incoming control word; no startup-mode assumption",
            "functions": functions,
            "return_relations": [{"function": "%08x" % addr, **summary_dict(summary)}
                                 for addr, summary in sorted(summaries.items())]}
