"""CFG propagation of control-word facts over prepared byte-backed bodies."""
from collections import namedtuple

from .x87_cw import (CW_MASK, CW_NEUTRAL, CW_NEUTRAL_USER_OPS, CW_PC_RC, ENTRY, Fact,
                     ReturnSummary, UNKNOWN, UNKNOWN_SUMMARY, fact_dict,
                     instruction_effect, instruction_mnemonic, invalidate, join,
                     overwrite)
from .cfg import call_graph
from .call_contracts import cfg_is_closed

Prepared = namedtuple("Prepared", ("addr", "entry", "sites", "order", "succ", "preds",
                                   "effects", "calls", "returns", "x87_count", "word_relations", "entries"))
Analysis = namedtuple("Analysis", ("calls", "returns", "losses", "sites", "gaps"), defaults=((),))


def direct_call_target(ins):
    for op in ins.ops:
        if op.opc == "CALL" and op.ins:
            space, target, _size = op.ins[0]
            return target if space == "ram" else None
    return None


def prepare(fir):
    """Prepare closed byte-backed IR; secondary entries contribute unknown facts."""
    tails = {}
    for i, targets in fir.exits.items():
        ins = fir.insns[i]
        literal = [op.ins[0][1] for op in ins.ops
                   if op.opc == "BRANCH" and op.ins[0][0] == "ram"]
        if (instruction_mnemonic(ins) != "JMP" or fir.succ[i] or len(targets) != 1
                or literal != list(targets)):
            return None
        tails[i] = literal[0]
    if not fir.insns or fir.seh or any(not ins.raw for ins in fir.insns):
        return None
    if not tails and not cfg_is_closed(fir):
        return None
    for i, ins in enumerate(fir.insns):
        if not fir.succ[i] and i not in tails and instruction_mnemonic(ins) != "RET":
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
            calls.append((i, ins.addr, direct_call_target(ins)))
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
                        and instruction_mnemonic(fir.insns[i]) != "FEMMS"), word_relations or {}, roots)


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
    if target is None or target in opaque_targets or target in recursive:
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


def _run(prepared, summaries, opaque_targets, recursive=frozenset(),
         initial=ENTRY, boundaries=None):
    boundaries = boundaries or {}
    call_boundaries = boundaries.get("calls", {})
    edge_boundaries = boundaries.get("edges", {})

    def call_summary(i, target):
        return call_boundaries.get(prepared.sites[i],
               _call_summary(target, summaries, opaque_targets, recursive))

    def edge_fact(p, i):
        fact = outgoing[p]
        effect = edge_boundaries.get((prepared.sites[p], prepared.sites[i]))
        return _apply_summary(fact, effect) if effect else fact
    effects = dict(prepared.effects)
    calls = {i: target for i, _site, target in prepared.calls}
    call_sites = {i: site for i, site, _target in prepared.calls}
    incoming, outgoing = {}, {}
    call_facts, losses, loss_keys, gaps = {}, [], set(), []
    work = list(prepared.entries)
    queued = set(work)
    while work:
        i = work.pop()
        queued.discard(i)
        predecessors = [edge_fact(p, i) for p in prepared.preds[i] if p in outgoing]
        if i == prepared.entry:
            predecessors.append(initial)
        elif i in prepared.entries:
            predecessors.append(UNKNOWN)
        state = join(predecessors)
        if incoming.get(i) == state and i in outgoing:
            continue
        incoming[i] = state
        if i in calls:
            target = calls[i]
            state = _apply_summary(state, call_summary(i, target))
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
            site = call_sites[i]
            call_facts[site] = state
            summary = call_summary(i, target)
            unresolved = CW_PC_RC & ~(summary.preserved_mask | summary.known_mask) if summary else CW_PC_RC
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


def summarize(prepared_by_addr, opaque_targets=(), boundaries=None):
    """Prove return relations, including induction over finite recursive returns."""
    opaque_targets = frozenset(opaque_targets)
    summaries = {addr: UNKNOWN_SUMMARY for addr, body in prepared_by_addr.items()
                 if body is None or addr in opaque_targets}
    analyzable = {addr: body for addr, body in prepared_by_addr.items()
                  if body is not None and addr not in opaque_targets}
    targets = {addr: [target for _, _site, target in p.calls if target is not None]
               for addr, p in analyzable.items()}
    components = call_graph(sorted(analyzable), lambda addr: targets.get(addr, ()))
    for component in components:
        recursive = len(component) > 1 or any(addr in targets.get(addr, ()) for addr in component)
        if recursive:
            for addr in component:
                summaries[addr] = ReturnSummary(CW_MASK, 0, 0)
        changed = True
        while changed:
            changed = False
            for addr in component:
                result = _run(analyzable[addr], summaries, opaque_targets, boundaries=boundaries)
                summary = _summary(result.returns)
                if summaries.get(addr) != summary:
                    summaries[addr] = summary
                    changed = True
            if not recursive:
                break
    return summaries


def analyze(prepared, summaries, opaque_targets=(), initial=ENTRY, boundaries=None):
    """Propagate mode facts through one function and report calls/returns/losses."""
    if prepared is None:
        return Analysis({}, UNKNOWN, (), {})
    return _run(prepared, summaries, frozenset(opaque_targets), initial=initial, boundaries=boundaries)


def mode_preserving_leaf(prepared):
    """True when every reachable instruction preserves CW and the body makes no calls."""
    return bool(prepared and prepared.returns and not prepared.calls
                and all(effect[1][0] == "keep" for effect in prepared.effects))


def propagate(prepared, summaries, roots, opaque_targets=(), boundaries=None):
    """Contextual direct-call facts; roots are explicit assumptions, not entry proofs."""
    entries = dict(roots)
    results, work = {}, list(roots)
    edges = {}
    while work:
        addr = work.pop()
        body = prepared.get(addr)
        if body is None or addr in opaque_targets:
            results[addr] = Analysis({}, UNKNOWN, (), {})
            continue
        result = analyze(body, summaries, opaque_targets, entries[addr], boundaries)
        results[addr] = result
        for _, site, target in body.calls:
            if target is None or site not in result.calls:
                continue
            edges.setdefault(target, {})[(addr, site)] = result.calls[site]
            incoming = list(edges[target].values())
            fact = roots[target] if target in roots else join(incoming)
            if entries.get(target) != fact:
                entries[target] = fact
                if target not in work:
                    work.append(target)
    return entries, results


def ordinary_entries(prepared, summaries, contracts, direct_calls, unknown_entries,
                     opaque_targets=(), boundaries=None):
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
    for addr, targets in direct_calls.items():
        for target in targets:
            callers.setdefault(target, set()).add(addr)
            edges.setdefault(target, {})[(addr, None)] = UNKNOWN
    for addr, body in prepared.items():
        if body is None or addr in opaque:
            continue
        for _, site, target in body.calls:
            if target is not None:
                edges.setdefault(target, {}).pop((addr, None), None)
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
        result = analyze(body, summaries, opaque, entries[addr], boundaries)
        results[addr] = result
        for _, site, target in body.calls:
            if target is None or target not in prepared:
                continue
            edges[target][(addr, site)] = result.calls.get(site, UNKNOWN)
            incoming = list(edges[target].values())
            if target in unknown_entries or not incoming:
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
        for reason in unknown_entries.get(addr, ()):
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
