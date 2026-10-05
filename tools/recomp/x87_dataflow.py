"""Decoded x87 width effects and bounded must-provenance over an admitted CFG.

Facts describe physical slots relative to the region's entry TOP, conditional
on PC=00. They say nothing about tags, exact integers, aliases or other precision
settings. Entry values are unknown; a fact survives a join only on every edge.
The caller admits instructions and proves consistent TOP before using this pass.
"""
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class WidthEffect:
    delta: int = 0
    writes: frozenset = frozenset()
    binary32: frozenset = frozenset()

    def transfer(self, facts):
        return (facts - self.writes) | self.binary32


def width_effect(ins, top, parse_operand):
    """Describe admitted instructions without matching emitted C expressions.

    Pops retain slot contents. Stores do not narrow the source register. A
    PC=00 arithmetic result is rounded by the existing runtime even when its
    inputs are wide. One-operand ST arithmetic has byte-dependent direction;
    forget all widths instead of interpreting that ambiguous listing spelling.
    Register copies remain region barriers in the caller.
    """
    m = ins.mnem
    ops = [parse_operand(o) for o in ins.ops]
    if m in ("FLD", "FLD1", "FLDZ"):
        if m == "FLD" and (len(ops) != 1 or ops[0].kind != "mem"):
            return None
        slot = (top - 1) & 7
        narrow = m != "FLD" or ops[0].size == 32
        return WidthEffect(-1, frozenset((slot,)),
                           frozenset((slot,)) if narrow else frozenset())
    base = m[:-1] if m.endswith("P") else m
    if base in ("FADD", "FSUB", "FSUBR", "FMUL"):
        popping = m.endswith("P")
        if popping:
            dest = ops[0].sti if ops else 1
        elif len(ops) == 2 and all(o.kind == "st" for o in ops):
            dest = ops[0].sti
        elif len(ops) == 1 and ops[0].kind == "mem":
            dest = 0
        elif len(ops) == 1 and ops[0].kind == "st":
            return WidthEffect(writes=frozenset(range(8)))
        else:
            return None
        slot = (top + dest) & 7
        return WidthEffect(int(popping), frozenset((slot,)), frozenset((slot,)))
    if m in ("FST", "FSTP"):
        if len(ops) != 1 or ops[0].kind != "mem":
            return None
        return WidthEffect(int(m == "FSTP"))
    if m in ("FCOMP", "FUCOMP"):
        return WidthEffect(1)
    if m in ("FCOMPP", "FUCOMPP"):
        return WidthEffect(2)
    if m in ("FCOM", "FUCOM", "FNSTSW") or not m.startswith("F"):
        return WidthEffect()
    return None


def binary32_inputs(effects, successors, entry, max_nodes=8192, max_edges=32768):
    """Compute greatest fixed-point must facts, or refuse an oversized CFG.

    Internal nodes start at the lattice top; entry's synthetic incoming edge
    has no facts. Descending intersection avoids both self-justifying loop
    facts and losing a proven loop-carried value merely because of a backedge.
    Each node loses at most eight facts, bounding work by nodes and edges.
    """
    if entry not in effects or len(effects) > max_nodes:
        return None
    edges = {i: tuple(j for j in successors[i] if j in effects) for i in effects}
    if sum(map(len, edges.values())) > max_edges:
        return None
    reachable = {entry}
    todo = [entry]
    while todo:
        for j in edges[todo.pop()]:
            if j not in reachable:
                reachable.add(j)
                todo.append(j)
    if len(reachable) != len(effects):
        return None
    facts = {i: frozenset(range(8)) for i in effects}
    facts[entry] = frozenset()
    queue = deque(effects)
    pending = set(effects)
    while queue:
        i = queue.popleft()
        pending.remove(i)
        out = effects[i].transfer(facts[i])
        for j in edges[i]:
            incoming = facts[j] & out
            if incoming != facts[j]:
                facts[j] = incoming
                if j not in pending:
                    queue.append(j)
                    pending.add(j)
    return facts
