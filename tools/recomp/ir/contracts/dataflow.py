"""Worklist analyses independent of x86 lifting, runtime policy and emission."""
from collections import deque

from ..cfg import call_graph
from .model import Effects, Mask


def reachable(successors, entries):
    """Validate the entire CFG, then find nodes reachable from all entries."""
    entries = tuple(entries)
    count = len(successors)
    if not entries:
        raise ValueError("at least one region entry required")
    if any(type(i) is not int or not 0 <= i < count
           for i in tuple(entries) + tuple(j for row in successors for j in row)):
        raise ValueError("CFG index outside instruction table")
    seen, todo = set(), list(entries)
    while todo:
        i = todo.pop()
        if i not in seen:
            seen.add(i)
            todo.extend(successors[i])
    return frozenset(seen)


def backward_demands(nodes, successors, entries, exits):
    """Propagate observer demands through joins and loops to a fixed point.

    exits maps each externally continuing node to its required CPU mask. Every
    reachable terminal must be declared. Internal observer reads are roots;
    possible observer writes do not kill demand without a definite-write proof.
    Returns (before, after) maps for reachable nodes. Memory liveness and fault
    state reconstruction deliberately remain separate future analyses.
    """
    if len(nodes) != len(successors):
        raise ValueError("node/successor count mismatch")
    active = reachable(successors, entries)
    if any(i not in active for i in exits):
        raise ValueError("exit demand names an unreachable node")
    if any(not successors[i] and i not in exits for i in active):
        raise ValueError("terminal node lacks an exit contract")
    predecessors = {i: set() for i in active}
    for i in active:
        for j in successors[i]:
            predecessors[j].add(i)
    before, after = ({i: Mask() for i in active} for _ in range(2))
    work, queued = deque(sorted(active, reverse=True)), set(active)
    while work:
        i = work.popleft()
        queued.remove(i)
        out = exits.get(i, Mask())
        for j in successors[i]:
            out = out.union(before[j])
        # At present boundaries are before the instruction. Keep observer
        # reads outside the kill so call/fault demands cannot disappear.
        need = out.without(nodes[i].defs).union(nodes[i].effects.cpu_reads)
        for boundary in nodes[i].boundaries:
            need = need.union(boundary.observer.effects.cpu_reads)
        after[i] = out
        if need != before[i]:
            before[i] = need
            for p in sorted(predecessors[i]):
                if p not in queued:
                    queued.add(p)
                    work.append(p)
    return before, after


def region_effects(nodes, successors, entries):
    """Union reachable may-effects; never interpret the union as an event trace."""
    if len(nodes) != len(successors):
        raise ValueError("node/successor count mismatch")
    active = reachable(successors, entries)
    effect = Effects()
    for i in sorted(active):
        effect = effect.union(nodes[i].effects)
        for boundary in nodes[i].boundaries:
            effect = effect.union(boundary.observer.effects)
    # A cycle is not proof of divergence, but termination must not be assumed.
    degrees = {i: 0 for i in active}
    for i in active:
        for j in successors[i]:
            degrees[j] += 1
    work = deque(i for i in sorted(active) if not degrees[i])
    visited = 0
    while work:
        i = work.popleft()
        visited += 1
        for j in successors[i]:
            degrees[j] -= 1
            if not degrees[j]:
                work.append(j)
    if visited != len(active):
        effect = effect.union(Effects(events=frozenset({'nonreturn'})))
    return effect


def program_effects(functions):
    """Compose supplied function may-effects, including recursive components.

    Missing callees contribute top, never a Win32 ABI guess. The finite union
    domain converges without an arbitrary 'eight rounds means success' limit.
    This does not model private call interfaces or prove observer coverage.
    """
    functions = tuple(functions)
    table = {f.address: f for f in functions}
    if len(table) != len(functions):
        raise ValueError("duplicate function address")
    local = {a: f.local for a, f in table.items()}
    for component in call_graph(sorted(table), lambda a: sorted(table[a].callees)):
        if len(component) > 1 or component[0] in table[component[0]].callees:
            for a in component:
                local[a] = local[a].union(Effects(events=frozenset({'nonreturn'})))
    out = dict(local)
    callers = {a: set() for a in table}
    for a, f in table.items():
        for callee in f.callees:
            if callee in callers:
                callers[callee].add(a)
    work, queued = deque(sorted(table)), set(table)
    while work:
        a = work.popleft()
        queued.remove(a)
        merged = local[a]
        for callee in sorted(table[a].callees):
            merged = merged.union(out[callee] if callee in out else
                                  Effects.unknown("unresolved callee %08x" % callee))
        if merged != out[a]:
            out[a] = merged
            for caller in sorted(callers[a]):
                if caller not in queued:
                    queued.add(caller)
                    work.append(caller)
    return out
