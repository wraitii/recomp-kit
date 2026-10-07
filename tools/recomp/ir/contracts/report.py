"""Deterministic, diagnostic reports; no emitter consumes these as ABI facts."""
from .dataflow import backward_demands, program_effects, reachable, region_effects
from .lifted import inventory
from .model import FunctionFacts, Mask


def analyze(functions):
    """Inventory resolved original CFGs and compose conservative may-effects.

    Public terminal states remain complete. Missing callees, unsupported effects
    and fault observers are explicit. No result is optimization authorization.
    """
    facts, rows = [], {}
    for fir in functions:
        if fir.addr in rows:
            raise ValueError("duplicate function address")
        if len(fir.insns) != len(fir.succ):
            raise ValueError("instruction/successor count mismatch")
        indices = {ins.addr: i for i, ins in enumerate(fir.insns)}
        if len(indices) != len(fir.insns) or fir.addr not in indices:
            raise ValueError("duplicate instruction addresses or missing entry")
        entry = indices[fir.addr]
        active = reachable(fir.succ, (entry,))
        nodes = inventory(fir)
        exits = {i: Mask.unknown() for i in active if not fir.succ[i]}
        before, after = backward_demands(nodes, fir.succ, (entry,), exits)
        local = region_effects(nodes, fir.succ, (entry,))
        callees = frozenset(boundary.target for i in active for boundary in nodes[i].boundaries
                            if boundary.kind == "call" and boundary.target is not None)
        facts.append(FunctionFacts(fir.addr, local, callees))
        rows[fir.addr] = {
            "entry_demand": before[entry].as_json(),
            "local_effects": local.as_json(),
            "callees": ["%08x" % a for a in sorted(callees)],
            "nodes": [{"site": "%08x" % nodes[i].site,
                       "successors": ["%08x" % nodes[j].site for j in sorted(fir.succ[i])],
                       "before": before[i].as_json(), "after": after[i].as_json(),
                       "terminal": i in exits,
                       "boundaries": [{"kind": b.kind,
                                       "target": None if b.target is None else "%08x" % b.target,
                                       "effects": b.observer.effects.as_json(),
                                       "evidence": list(b.observer.evidence)}
                                      for b in nodes[i].boundaries]}
                      for i in sorted(active)],
        }
    summaries = program_effects(facts)
    for a in rows:
        rows[a]["transitive_effects"] = summaries[a].as_json()
    return {
        "schema": "observable-contract-inventory-v1",
        "purpose": "diagnostic; not optimization authorization",
        "optimization_authorized": False,
        "policy": "complete public exits and unreviewed call/fault observers",
        "limitations": ["raw integer flags and x87 need corrected effect models",
                        "memory footprints are unknown; no alias/escape analysis",
                        "no exceptional reconstruction or termination proof",
                        "event sets are may-effects, not ordered execution traces",
                        "no private ABI, region selection or code-generation changes"],
        "functions": {"%08x" % a: rows[a] for a in sorted(rows)},
    }
