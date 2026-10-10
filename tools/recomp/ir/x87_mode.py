"""Conservative control-word facts derived from original instruction bytes.

Summaries describe translated bodies only. Callers must mark imports,
replacements and hookable targets opaque before using them as proof.
"""
from collections import namedtuple

import capstone

from . import x87
from .cfg import call_graph
from .call_contracts import cfg_is_closed

CW_MASK = 0xffff
CW_RESET = 0x037f

Fact = namedtuple("Fact", ("known_mask", "known_value", "entry_mask"))
ReturnSummary = namedtuple("ReturnSummary", ("preserved_mask", "known_mask", "known_value"))
Prepared = namedtuple("Prepared", ("addr", "entry", "sites", "order", "succ", "preds",
                                   "effects", "calls", "returns", "x87_count", "word_relations"))
Analysis = namedtuple("Analysis", ("calls", "returns", "losses"))

UNKNOWN = Fact(0, 0, 0)
ENTRY = Fact(0, 0, CW_MASK)
UNKNOWN_SUMMARY = ReturnSummary(0, 0, 0)

_CW_NEUTRAL = x87.SUPPORTED | frozenset((
    "FCOMI", "FCOMIP", "FUCOMI", "FUCOMIP", "FEMMS", "WAIT",
))
_DECODER = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)


def join(facts):
    """Keep only equal known bits and entry bits common to every path."""
    facts = tuple(facts)
    if not facts:
        return UNKNOWN
    known = facts[0].known_mask
    value = facts[0].known_value
    entry = facts[0].entry_mask
    for fact in facts[1:]:
        equal = ~(value ^ fact.known_value) & CW_MASK
        known &= fact.known_mask & equal
        entry &= fact.entry_mask
    return Fact(known, value & known, entry & ~known & CW_MASK)


def overwrite(fact, mask, value):
    """Set selected bits to a constant while retaining other facts."""
    mask &= CW_MASK
    known = (fact.known_mask & ~mask) | mask
    known_value = (fact.known_value & ~mask) | (value & mask)
    entry = fact.entry_mask & ~mask
    return Fact(known & CW_MASK, known_value & known & CW_MASK, entry & CW_MASK)


def invalidate():
    return UNKNOWN


def fact_dict(fact):
    return {"known_mask": "%04x" % fact.known_mask,
            "known_value": "%04x" % fact.known_value,
            "entry_mask": "%04x" % fact.entry_mask}


def summary_dict(summary):
    return {"preserved_mask": "%04x" % summary.preserved_mask,
            "known_mask": "%04x" % summary.known_mask,
            "known_value": "%04x" % summary.known_value}


def _mnemonic(ins):
    """Decode and validate the byte-backed instruction name used by effects."""
    if not ins.raw:
        return None
    decoded = list(_DECODER.disasm(ins.raw, ins.addr))
    if sum(item.size for item in decoded) != ins.length:
        return None
    meaningful = [item.mnemonic.upper() for item in decoded if item.mnemonic.lower() != "wait"]
    if not meaningful and decoded:
        return "WAIT"
    if len(meaningful) != 1:
        return None
    return meaningful[0]


def _effect(mnem):
    if mnem in ("FINIT", "FNINIT", "FSAVE", "FNSAVE"):
        return ("set", CW_MASK, CW_RESET, "reset control word")
    if mnem == "FNSTENV" or mnem == "FSTENV":
        return ("set", 0x003f, 0x003f, "environment save masks exceptions")
    if mnem in ("FLDCW", "FLDENV", "FRSTOR"):
        return ("unknown", 0, 0, "control word loaded from guest memory")
    if mnem in ("FXRSTOR", "XRSTOR", "XRSTORS"):
        return ("unknown", 0, 0, "extended state restored from guest memory")
    if mnem.startswith("F") and mnem not in _CW_NEUTRAL:
        return ("unknown", 0, 0, "unclassified x87 instruction %s" % mnem)
    return ("keep", 0, 0, None)


_CW_WRITERS = frozenset(("FINIT", "FNINIT", "FSAVE", "FNSAVE", "FNSTENV", "FSTENV",
                         "FLDCW", "FLDENV", "FRSTOR", "FXRSTOR", "XRSTOR", "XRSTORS"))


def writer_effect(mnem):
    """Return the audited CW effect for a concrete writer mnemonic, else None.

    Callers must supply a mnemonic decoded from the original instruction bytes.
    Unknown x87 instructions are reported separately as possible boundaries,
    never promoted to definite writer sites by this helper.
    """
    mnem = str(mnem).upper()
    return _effect(mnem) if mnem in _CW_WRITERS else None


def _call_target(ins):
    for op in ins.ops:
        if op.opc == "CALL" and op.ins:
            space, target, _size = op.ins[0]
            return target if space == "ram" else None
    return None


def prepare(fir):
    """Prepare closed lifted IR; reject bodies without byte-backed boundaries."""
    if (not fir.insns or not cfg_is_closed(fir) or fir.entries or fir.seh
            or any(not ins.raw for ins in fir.insns)):
        return None
    entry = next((i for i, ins in enumerate(fir.insns) if ins.addr == fir.addr), None)
    if entry is None:
        return None
    seen, todo = set(), [entry]
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
        mnem = _mnemonic(ins)
        if mnem is None:
            return None
        effect = _effect(mnem)
        if (effect[0] == "keep" and mnem not in ("FEMMS",)
                and any(op.opc == "CALLOTHER" for op in ins.ops)):
            effect = ("unknown", 0, 0, "unmodeled user operation")
        effects.append((i, effect))
        if any(op.opc in ("CALL", "CALLIND") for op in ins.ops):
            calls.append((i, ins.addr, _call_target(ins)))
        if mnem == "RET" or any(op.opc == "RETURN" for op in ins.ops):
            returns.append(i)
        for target in succ[i]:
            preds[target].append(i)
    from .x87_provenance import relations
    word_relations = relations(fir) if any(_mnemonic(fir.insns[i]) == "FLDCW" for i in order) else {}
    return Prepared(fir.addr, entry, sites, order, succ,
                    tuple(tuple(row) for row in preds), tuple(effects),
                    tuple(calls), tuple(returns),
                    sum(1 for i in order if _mnemonic(fir.insns[i]).startswith("F")
                        and _mnemonic(fir.insns[i]) != "FEMMS"), word_relations or {})


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


def _record_loss(losses, seen, site, reason, target, lost):
    if not lost:
        return
    row = {"site": "%08x" % site, "reason": reason,
           "target": None if target is None else "%08x" % target,
           "lost_mask": "%04x" % lost}
    key = (row["site"], row["reason"], row["target"], row["lost_mask"])
    if key not in seen:
        seen.add(key)
        losses.append(row)


def _run(prepared, summaries, opaque_targets, recursive=frozenset()):
    effects = dict(prepared.effects)
    calls = {i: target for i, _site, target in prepared.calls}
    call_sites = {i: site for i, site, _target in prepared.calls}
    incoming, outgoing = {}, {}
    call_facts, losses, loss_keys = {}, [], set()
    work = [prepared.entry]
    queued = {prepared.entry}
    initial = ENTRY
    while work:
        i = work.pop()
        queued.discard(i)
        predecessors = [outgoing[p] for p in prepared.preds[i] if p in outgoing]
        if i == prepared.entry:
            predecessors.append(initial)
        state = join(predecessors)
        if incoming.get(i) == state and i in outgoing:
            continue
        incoming[i] = state
        if i in calls:
            target = calls[i]
            state = _apply_summary(state, _call_summary(target, summaries, opaque_targets, recursive))
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
        state = incoming[i]
        if i in calls:
            target = calls[i]
            site = call_sites[i]
            call_facts[site] = state
            summary = _call_summary(target, summaries, opaque_targets, recursive)
            after = _apply_summary(state, summary)
            lost = _lost_mask(state, after)
            if summary is None:
                reason = "indirect or opaque call" if target is None or target in opaque_targets else "recursive or unmodeled callee"
            elif lost:
                reason = "callee return relation changes or loses control-word bits"
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
        if loss_reason:
            _record_loss(losses, loss_keys, prepared.sites[i], loss_reason, None, lost)
    returns = join(outgoing[i] for i in prepared.returns if i in outgoing)
    return Analysis(call_facts, returns, tuple(losses))


def _summary(fact):
    return ReturnSummary(fact.entry_mask, fact.known_mask, fact.known_value)


def summarize(prepared_by_addr, opaque_targets=()):
    """Summarize translated direct-call return relations; recursive edges are opaque."""
    opaque_targets = frozenset(opaque_targets)
    summaries = {addr: UNKNOWN_SUMMARY for addr, body in prepared_by_addr.items()
                 if body is None or addr in opaque_targets}
    analyzable = {addr: body for addr, body in prepared_by_addr.items()
                  if body is not None and addr not in opaque_targets}
    targets = {addr: [target for _, _site, target in p.calls if target is not None]
               for addr, p in analyzable.items()}
    components = call_graph(sorted(analyzable), lambda addr: targets.get(addr, ()))
    for component in components:
        members = frozenset(component)
        recursive = len(component) > 1 or any(addr in targets.get(addr, ()) for addr in component)
        for addr in component:
            result = _run(analyzable[addr], summaries, opaque_targets,
                          members if recursive else frozenset())
            summaries[addr] = _summary(result.returns)
    return summaries


def analyze(prepared, summaries, opaque_targets=()):
    """Propagate mode facts through one function and report calls/returns/losses."""
    if prepared is None:
        return Analysis({}, UNKNOWN, ())
    return _run(prepared, summaries, frozenset(opaque_targets))


def mode_preserving_leaf(prepared):
    """True when every reachable instruction preserves CW and the body makes no calls."""
    return bool(prepared and prepared.returns and not prepared.calls
                and all(effect[1][0] == "keep" for effect in prepared.effects))


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
    recomputed with :func:`writer_effect`; optional supplied effect details are
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
