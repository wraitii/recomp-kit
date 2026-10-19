"""Carry complete register groups through CFG edges as one wider SSA value.

Byte lanes remain the aliasing interface for p-code and snapshots. Wider inputs
and phis replace lane tuples at block boundaries; explicit BYTE operations bridge
partial writes. Canonicalization removes complete extraction/reassembly pairs.
This changes representation only, without inferring dead upper register bits.
"""
from .operations import TERMINATORS


def initialize(s, groups, predecessors, inherited=()):
    """Create wide inputs and joins with byte lanes for instruction state."""
    s.entry_ops = []
    joins = []
    for keys in groups:
        keys = tuple(keys)
        if len(keys) <= 1 or not all(key in s.inputs for key in keys):
            continue
        start = len(s.values)
        source = s.value("INPUT", len(keys), data=keys[0])
        for n, key in enumerate(keys):
            byte = s.value("BYTE", 1, [source], n)
            s.entry_ops.append(byte)
            s.aliases[s.inputs[key].id] = byte
        phis = {}
        for i, b in s.blocks.items():
            if i in inherited:
                continue
            phi = s.value("PHI", len(keys), data=(keys, predecessors[i]))
            phis[i] = phi
            b.phis.append(phi)
            bytes_ = [s.value("BYTE", 1, [phi], n) for n in range(len(keys))]
            b.ops[:0] = bytes_
            b.state.update(zip(keys, bytes_))
        joins.append((keys, source, phis, start, len(s.values)))
    return joins


def connect(s, joins):
    if not joins:
        return
    values = s.values[:joins[0][3]] + s.values[joins[-1][4]:]
    positions = {i: next((n for n, v in enumerate(b.ops) if v.opc in TERMINATORS), len(b.ops))
                 for i, b in s.blocks.items()}
    for keys, source, phis, start, end in joins:
        values.extend(s.values[start:end])
        packed_start = len(s.values)
        for i, phi in phis.items():
            args = []
            for p in phi.data[1]:
                if p == -1:
                    args.append(source)
                    continue
                b = s.blocks[p]
                packed = s.value("PACK", len(keys), [b.exit[key] for key in keys])
                b.ops.insert(positions[p], packed)
                positions[p] += 1
                args.append(packed)
            phi.args = tuple(args)
        values.extend(s.values[packed_start:])
    s.values = values
