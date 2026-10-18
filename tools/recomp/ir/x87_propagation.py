"""CFG propagation of control-word facts over prepared byte-backed bodies."""
from collections import namedtuple

from .x87_cw import (CW_MASK, CW_NEUTRAL, CW_NEUTRAL_USER_OPS, CW_PC_RC, ENTRY, Fact,
                     ReturnSummary, UNKNOWN, UNKNOWN_SUMMARY, fact_dict,
                     instruction_effect, instruction_mnemonic, invalidate, join,
                     join_summaries, overwrite)
from . import call_sites
from .cfg import call_graph
from .call_contracts import cfg_is_closed
from .import_sites import CALLBACK_IMPORTS, EMPTY_EFFECTS

Prepared = namedtuple("Prepared", ("addr", "entry", "sites", "order", "succ", "preds",
                                   "effects", "calls", "returns", "x87_count", "word_relations", "entries",
                                   "call_sites"))
Analysis = namedtuple("Analysis", ("calls", "returns", "losses", "sites", "gaps"), defaults=((),))


def prepare(fir, image=None, sites=None):
    """Prepare closed byte-backed IR; secondary entries contribute unknown facts.

    SEH bodies qualify only when every frame they establish is unwind-only
    (`FunctionIR.seh_unwind_only`): an exception then never resumes here, so
    normal returns stay sound.
    A call that never returns ends its path without reaching a return.
    """
    if not fir.insns:
        return None
    rows = sites if sites is not None else call_sites.build(fir, image)
    tails = {i: row.targets[0].address if row.targets else None
             for i, row in rows.items() if row.kind == "tail"}
    if any(i not in tails for i in fir.exits):
        return None
    if any(not ins.raw for ins in fir.insns):
        return None
    if fir.seh and (any(set(kinds) - {"enter", "leave"} for kinds in fir.seh.values())
                    or not all(i in fir.seh_unwind_only for i, kinds in fir.seh.items() if "enter" in kinds)):
        return None
    if not tails and not cfg_is_closed(fir, fir.noreturn):
        return None
    for i, ins in enumerate(fir.insns):
        if not fir.succ[i] and i not in tails and i not in fir.noreturn and instruction_mnemonic(ins) != "RET":
            return None
    entry = next((i for i, ins in enumerate(fir.insns) if ins.addr == fir.addr), None)
    if entry is None:
        return None
    roots = (entry,) + tuple(i for i, ins in enumerate(fir.insns) if ins.addr in fir.entries)
    seen, todo = set(), list(roots)
    while todo:
        i = todo.pop()
        if i in seen or not 0 <= i < len(fir.insns):
            continue
        seen.add(i)
        todo.extend(fir.succ[i])
    order = tuple(i for i in range(len(fir.insns)) if i in seen)
    succ = tuple(tuple(j for j in fir.succ[i] if j in seen) if i in seen else ()
                 for i in range(len(fir.insns)))
    preds = [[] for _ in fir.insns]
    effects, calls, returns = [], [], []
    sites = tuple(ins.addr for ins in fir.insns)
    for i in order:
        ins = fir.insns[i]
        mnem = instruction_mnemonic(ins)
        if mnem is None:
            return None
        effect = instruction_effect(mnem)
        if (effect[0] == "keep" and mnem not in CW_NEUTRAL | CW_NEUTRAL_USER_OPS
                and any(op.opc == "CALLOTHER" for op in ins.ops)):
            effect = ("unknown", 0, 0, "unmodeled user operation")
        effects.append((i, effect))
        if i in tails:
            calls.append((i, ins.addr, tails[i]))
        elif any(op.opc in ("CALL", "CALLIND") for op in ins.ops):
            calls.append((i, ins.addr, rows[i].targets[0].address if rows[i].targets else None))
        if i in tails or mnem == "RET" or any(op.opc == "RETURN" for op in ins.ops):
            returns.append(i)
        for target in succ[i]:
            preds[target].append(i)
    from .x87_provenance import relations
    word_relations = relations(fir) if any(instruction_mnemonic(fir.insns[i]) == "FLDCW" for i in order) else {}
    return Prepared(fir.addr, entry, sites, order, succ,
                    tuple(tuple(row) for row in preds), tuple(effects),
                    tuple(calls), tuple(returns),
                    sum(1 for i in order if instruction_mnemonic(fir.insns[i]).startswith("F")
                        and instruction_mnemonic(fir.insns[i]) != "FEMMS"), word_relations or {}, roots,
                    rows)


def _apply_summary(fact, summary):
    if summary is None:
        return UNKNOWN
    preserved = summary.preserved_mask
    known = (fact.known_mask & preserved) | summary.known_mask
    value = (fact.known_value & preserved) | summary.known_value
    entry = fact.entry_mask & preserved & ~known
    return Fact(known & CW_MASK, value & known & CW_MASK, entry & CW_MASK)


def _lost_mask(before, after):
    same_known = before.known_mask & after.known_mask & ~(before.known_value ^ after.known_value)
    same_entry = before.entry_mask & after.entry_mask
    represented = same_known | same_entry
    return (before.known_mask | before.entry_mask) & ~represented & CW_MASK


def _call_summary(target, summaries, opaque_targets, recursive):
    if target is None or target in recursive:
        return None
    if target in opaque_targets and summaries.get(target, UNKNOWN_SUMMARY) == UNKNOWN_SUMMARY:
        return None
    return summaries.get(target)


def _record_loss(losses, seen, site, reason, target, lost, predecessor=None):
    if not lost:
        return
    row = {"site": "%08x" % site, "reason": reason,
           "target": None if target is None else "%08x" % target,
           "lost_mask": "%04x" % lost,
           "kind": "unresolved_preservation" if ("unresolved" in reason or "opaque" in reason or "unmodeled" in reason or "recursive" in reason) else "join" if "join" in reason else "possible_mode_change"}
    if predecessor is not None:
        row["predecessor"] = "%08x" % predecessor
    key = (row["site"], row["reason"], row["target"], row["lost_mask"], predecessor)
    if key not in seen:
        seen.add(key)
        losses.append(row)


def _configured_summary(value):
    if value == "preserve":
        return ReturnSummary(CW_MASK, 0, 0)
    if value == "unknown":
        return None
    return ReturnSummary(value["preserved"], value["known"], value["value"])


def _domain(summaries, opaque_targets, recursive, call_boundaries, call_effects):
    def boundary(row):
        return call_boundaries.get(row.site, call_sites.MISSING)

    def override(row):
        value = call_effects["sites"].get(row.site, {}).get("x87")
        return call_sites.MISSING if value is None else _configured_summary(value)

    def indirect(row, facts):
        fact = facts.get(row.site)
        return call_sites.MISSING if fact is None else fact[1]

    def import_value(row):
        if row.import_key is None:
            return call_sites.MISSING
        default = "unknown" if row.import_key in CALLBACK_IMPORTS else "preserve"
        return _configured_summary(call_effects["imports"].get(row.import_key, {}).get("x87", default))

    def lookup(target):
        return _call_summary(target, summaries, opaque_targets, recursive)

    return call_sites.Domain(lookup=lookup, join=join_summaries, override=override,
                             import_value=import_value, boundary=boundary, indirect=indirect,
                             indirect_with_direct=False)


def _run(prepared, summaries, opaque_targets, recursive=frozenset(),
         initial=ENTRY, boundaries=None, indirect=None, call_effects=None, roots=None):
    roots = prepared.entries if roots is None else roots
    boundaries = boundaries or {}
    indirect = indirect or {}
    call_effects = call_effects or EMPTY_EFFECTS
    call_boundaries = boundaries.get("calls", {})
    edge_boundaries = boundaries.get("edges", {})

    domain = _domain(summaries, opaque_targets, recursive, call_boundaries, call_effects)

    def call_summary(i):
        return call_sites.resolve(prepared.call_sites[i], domain, indirect)

    def edge_fact(p, i):
        fact = outgoing[p]
        effect = edge_boundaries.get((prepared.sites[p], prepared.sites[i]))
        return _apply_summary(fact, effect) if effect else fact
    effects = dict(prepared.effects)
    calls = {i: target for i, _site, target in prepared.calls}
    site_of = {i: site for i, site, _target in prepared.calls}
    incoming, outgoing = {}, {}
    call_facts, losses, loss_keys, gaps = {}, [], set(), []
    work = list(roots)
    queued = set(work)
    while work:
        i = work.pop()
        queued.discard(i)
        predecessors = [edge_fact(p, i) for p in prepared.preds[i] if p in outgoing]
        if i == roots[0]:
            predecessors.append(initial)
        elif i in roots:
            predecessors.append(UNKNOWN)
        state = join(predecessors)
        if incoming.get(i) == state and i in outgoing:
            continue
        incoming[i] = state
        if i in calls:
            state = _apply_summary(state, call_summary(i))
        kind, mask, value, reason = effects[i]
        if i in prepared.word_relations:
            relation = prepared.word_relations[i]
            state = _apply_summary(initial, _summary(relation))
        elif kind == "set":
            state = overwrite(state, mask, value)
        elif kind == "unknown":
            state = invalidate()
        if outgoing.get(i) == state:
            continue
        outgoing[i] = state
        for target in prepared.succ[i]:
            if target not in queued:
                queued.add(target)
                work.append(target)

    for i in prepared.order:
        if i not in incoming:
            continue
        state = incoming[i]
        for p in prepared.preds[i]:
            if p in outgoing:
                before = edge_fact(p, i)
                _record_loss(losses, loss_keys, prepared.sites[i],
                             "control-flow join loses predecessor facts", None, _lost_mask(before, state), prepared.sites[p])
        if i in calls:
            target = calls[i]
            site = site_of[i]
            call_facts[site] = state
            summary = call_summary(i)
            unresolved = CW_PC_RC & ~(summary.preserved_mask | summary.known_mask) if summary else CW_PC_RC
            if not prepared.succ[i] and i not in prepared.returns:
                unresolved, summary = 0, ReturnSummary(CW_MASK, 0, 0)
            if unresolved:
                gaps.append({"site": "%08x" % site,
                             "target": None if target is None else "%08x" % target,
                             "kind": "unresolved_preservation",
                             "unresolved_mask": "%04x" % unresolved,
                             "reason": "indirect or opaque call" if summary is None else "callee return relation lacks a preservation proof"})
            after = _apply_summary(state, summary)
            lost = _lost_mask(state, after)
            if summary is None:
                reason = "indirect or opaque call" if target is None or target in opaque_targets else "recursive or unmodeled callee"
            elif lost:
                changed = state.known_mask & summary.known_mask & (state.known_value ^ summary.known_value)
                reason = "callee proves changed control-word bits" if changed else "unresolved callee preservation proof"
            else:
                reason = None
            if reason:
                _record_loss(losses, loss_keys, site, reason, target, lost)
            state = after
        kind, mask, value, reason = effects[i]
        if i in prepared.word_relations:
            relation = prepared.word_relations[i]
            after = _apply_summary(initial, _summary(relation))
        elif kind == "set":
            after = overwrite(state, mask, value)
        elif kind == "unknown":
            after = invalidate()
        else:
            after = state
        lost = _lost_mask(state, after)
        if i in prepared.word_relations:
            loss_reason = "proven local control-word load changes entry bits" if lost else None
        elif kind == "unknown":
            loss_reason = reason
        elif lost:
            loss_reason = reason or "instruction changes control-word facts"
        else:
            loss_reason = None
        if kind == "unknown" and i not in prepared.word_relations:
            gaps.append({"site": "%08x" % prepared.sites[i], "target": None,
                         "kind": "possible_mode_change" if "restor" in reason or "loaded" in reason else "unresolved_instruction",
                         "unresolved_mask": "%04x" % CW_PC_RC, "reason": reason})
        if loss_reason:
            _record_loss(losses, loss_keys, prepared.sites[i], loss_reason, None, lost)
    returns = join(outgoing[i] for i in prepared.returns if i in outgoing)
    return Analysis(call_facts, returns, tuple(losses),
                    {prepared.sites[i]: fact for i, fact in incoming.items()}, tuple(gaps))


def _summary(fact):
    return ReturnSummary(fact.entry_mask, fact.known_mask, fact.known_value)


def summarize(prepared_by_addr, opaque_targets=(), boundaries=None, indirect=None, effects=None, noreturn=()):
    """Prove return relations, including induction over finite recursive returns.

    A call enters at one entry, so each entry's relation follows only the paths
    from it; calls to a secondary entry use that entry's own relation. An entry
    with no reachable return, or a function the code map marks noreturn and
    whose body is not analyzed, never hands control back, so it preserves vacuously.
    """
    opaque_targets = frozenset(opaque_targets)
    indirect = indirect or {}
    summaries = {addr: UNKNOWN_SUMMARY for addr, body in prepared_by_addr.items()
                 if body is None or addr in opaque_targets}
    analyzable = {addr: body for addr, body in prepared_by_addr.items()
                  if body is not None and addr not in opaque_targets}
    summaries.update((addr, ReturnSummary(CW_MASK, 0, 0)) for addr in noreturn if addr not in analyzable)

    owners = {body.sites[i]: addr for addr, body in analyzable.items() for i in body.entries
              if i != body.entry and body.sites[i] not in prepared_by_addr}

    def callees(body):
        for _, site, target in body.calls:
            if target is not None:
                yield owners.get(target, target)
            elif site in indirect:
                yield from (owners.get(t, t) for t in indirect[site][1])
    targets = {addr: list(callees(p)) for addr, p in analyzable.items()}
    components = call_graph(sorted(analyzable), lambda addr: targets.get(addr, ()))
    for component in components:
        recursive = len(component) > 1 or any(addr in targets.get(addr, ()) for addr in component)
        entries = [(addr, i) for addr in component for i in analyzable[addr].entries
                   if i == analyzable[addr].entry or analyzable[addr].sites[i] in owners]
        if recursive:
            for addr, i in entries:
                summaries[analyzable[addr].sites[i]] = ReturnSummary(CW_MASK, 0, 0)
        changed = True
        while changed:
            changed = False
            for addr, i in entries:
                result = _run(analyzable[addr], summaries, opaque_targets, boundaries=boundaries,
                              indirect=indirect, call_effects=effects, roots=(i,))
                body = analyzable[addr]
                returned = any(body.sites[r] in result.sites for r in body.returns)
                summary = _summary(result.returns) if returned else ReturnSummary(CW_MASK, 0, 0)
                if summaries.get(analyzable[addr].sites[i]) != summary:
                    summaries[analyzable[addr].sites[i]] = summary
                    changed = True
            if not recursive:
                break
    return summaries


def analyze(prepared, summaries, opaque_targets=(), initial=ENTRY, boundaries=None,
            indirect=None, effects=None):
    """Propagate mode facts through one function and report calls/returns/losses."""
    if prepared is None:
        return Analysis({}, UNKNOWN, (), {})
    return _run(prepared, summaries, frozenset(opaque_targets), initial=initial, boundaries=boundaries,
                indirect=indirect, call_effects=effects)


def _call_edges(prepared, indirect):
    for _, site, target in prepared.calls:
        targets = (target,) if target is not None else (indirect or {}).get(site, (None, ()))[1]
        for target in targets:
            yield site, target


def mode_preserving_leaf(prepared):
    """True when every reachable instruction preserves CW and the body makes no calls."""
    return bool(prepared and prepared.returns and not prepared.calls
                and all(effect[1][0] == "keep" for effect in prepared.effects))


def propagate(prepared, summaries, roots, opaque_targets=(), boundaries=None, indirect=None, effects=None):
    """Contextual call facts; roots are explicit assumptions, not entry proofs."""
    entries = dict(roots)
    results, work = {}, list(roots)
    edges = {}
    while work:
        addr = work.pop()
        body = prepared.get(addr)
        if body is None or addr in opaque_targets:
            results[addr] = Analysis({}, UNKNOWN, (), {})
            continue
        result = analyze(body, summaries, opaque_targets, entries[addr], boundaries, indirect, effects)
        results[addr] = result
        for site, target in _call_edges(body, indirect):
            if target not in prepared or site not in result.calls:
                continue
            edges.setdefault(target, {})[(addr, site)] = result.calls[site]
            incoming = list(edges[target].values())
            fact = roots[target] if target in roots else join(incoming)
            if entries.get(target) != fact:
                entries[target] = fact
                if target not in work:
                    work.append(target)
    return entries, results


def ordinary_entries(prepared, summaries, contracts, callgraph,
                     opaque_targets=(), boundaries=None, indirect=None, effects=None):
    """Join every static caller and unknown entry before folding a shared body.

    Contracts constrain every invocation at their named primary entry. All
    other entries start unknown; facts grow only from constants or contracts.
    Opaque callers contribute unknown even when their outgoing edge is unlifted.
    """
    from collections import deque

    opaque = frozenset(opaque_targets)
    roots = {row["function"]: Fact(row["mask"], row["value"], 0) for row in contracts}
    for addr in roots:
        if addr not in prepared or prepared[addr] is None or addr in opaque:
            raise ValueError("%08x: CW contract requires a prepared unreplaced body" % addr)
    callers, edges = {}, {}
    residual = {(edge.source, target) for target in prepared for edge in callgraph.callers(target)
                if edge.provenance in ("tail_jump", "fallthrough", "jump_table")}
    for addr, targets in callgraph.join_edges.items():
        for target in targets:
            callers.setdefault(target, set()).add(addr)
            edges.setdefault(target, {})[(addr, None)] = UNKNOWN
    for addr, body in prepared.items():
        if body is None or addr in opaque:
            continue
        for site, target in _call_edges(body, indirect):
            callers.setdefault(target, set()).add(addr)
            edges.setdefault(target, {})
            if (addr, target) not in residual:
                edges[target].pop((addr, None), None)
            edges[target][(addr, site)] = UNKNOWN
    entries = {addr: roots.get(addr, UNKNOWN) for addr in prepared}
    results = {}
    work = deque(prepared)
    queued = set(work)
    while work:
        addr = work.popleft()
        queued.discard(addr)
        body = prepared[addr]
        if body is None or addr in opaque:
            continue
        result = analyze(body, summaries, opaque, entries[addr], boundaries, indirect, effects)
        results[addr] = result
        changed = set()
        for site, target in _call_edges(body, indirect):
            if target not in prepared:
                continue
            incoming = result.calls.get(site, UNKNOWN)
            if edges[target][(addr, site)] != incoming:
                edges[target][(addr, site)] = incoming
                changed.add(target)
        for target in changed:
            incoming = list(edges[target].values())
            if callgraph.can_enter_unmodeled(target) or not incoming:
                incoming.append(UNKNOWN)
            fact = roots.get(target, join(incoming))
            if fact != entries[target]:
                entries[target] = fact
                if target not in queued:
                    work.append(target)
                    queued.add(target)
    gaps = []
    for addr in prepared:
        if addr in roots:
            continue
        for reason in callgraph.unmodeled(addr):
            gaps.append({"function": "%08x" % addr, "kind": "unknown_entry", "reason": reason})
        if not callers.get(addr):
            gaps.append({"function": "%08x" % addr, "kind": "unknown_entry",
                         "reason": "no proven direct caller; includes external or unreachable entry"})
        for (caller, site), fact in edges.get(addr, {}).items():
            lost = fact.known_mask & ~entries[addr].known_mask
            if lost:
                gaps.append({"function": "%08x" % addr, "kind": "caller_join",
                             "caller": "%08x" % caller,
                             "site": None if site is None else "%08x" % site,
                             "lost_mask": "%04x" % lost,
                             "reason": "other caller or unknown entry disagrees",
                             "other_inputs": [{"caller": "%08x" % a,
                                "site": None if s is None else "%08x" % s,
                                **fact_dict(f)} for (a, s), f in sorted(edges[addr].items(), key=lambda item: (item[0][0], item[0][1] or 0))]})
    return entries, results, gaps
