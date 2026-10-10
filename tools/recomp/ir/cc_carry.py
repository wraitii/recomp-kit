"""Carry lazy flag recipes through resolved CFG edges and equal-width joins.

Unresolved cycles and entry edges keep eager flags. Each merged flag must still
match its predecessor's recipe; uncovered flags retain their ordinary SSA phi.
"""
from collections import deque

from .ssa import CcRecord


def carry(s, predecessors, flag_off_name):
    flag_keys = {name: ("register", off) for off, name in flag_off_name.items()}
    exits = {i: b.cc_exit for i, b in s.blocks.items() if b.cc_exit is not b.cc_entry}
    successors = {i: set() for i in s.blocks}
    for i, preds in predecessors.items():
        for p in preds:
            if p != -1:
                successors[p].add(i)
    pending = set(s.blocks)
    todo = deque(sorted(pending))

    def merge(i):
        preds, block = predecessors[i], s.blocks[i]
        if not preds or -1 in preds:
            return None
        records = [exits[p] for p in preds]
        if any(r is None for r in records) or len({r.size for r in records}) != 1:
            return None
        if all(r is records[0] for r in records):
            return records[0]
        flags = set.intersection(*(set(r.flags) for r in records))
        flags = {name for name in flags if all(
            s.resolve(r.flags[name]) is s.resolve(s.blocks[p].exit[flag_keys[name]])
            for p, r in zip(preds, records))}
        if not flags:
            return None
        result = CcRecord("join", records[0].size)

        def phi(part, size, values):
            value = s.value("PHI", size, values, (("cc", i, part), preds))
            block.phis.append(value)
            return value

        result.op = phi("op", 1, [r.op if r.op is not None else
                                 s.value("CC_KIND", 1, data=r.kind) for r in records])
        for part in ("a", "b", "res", "carry"):
            values = [getattr(r, part) for r in records]
            size = result.size
            values = [v if v is not None else s.value("CONST", size, data=0) for v in values]
            if len({v.size for v in values}) != 1:
                return None
            setattr(result, part, phi(part, values[0].size, values))
        result.flags = {name: block.state[flag_keys[name]] for name in flags}
        return result

    while todo:
        i = todo.popleft()
        if i not in pending:
            continue
        preds = predecessors[i]
        if -1 not in preds and any(p not in exits for p in preds):
            continue
        pending.remove(i)
        block = s.blocks[i]
        incoming = merge(i)
        block.cc_snapshots = {ident: incoming if r is block.cc_entry else r
                              for ident, r in block.cc_snapshots.items()}
        if block.cc_exit is block.cc_entry:
            block.cc_exit = incoming
            exits[i] = incoming
            todo.extend(sorted(successors[i]))
    for i in pending:
        block = s.blocks[i]
        block.cc_snapshots = {ident: None if r is block.cc_entry else r
                              for ident, r in block.cc_snapshots.items()}
        if block.cc_exit is block.cc_entry:
            block.cc_exit = None
