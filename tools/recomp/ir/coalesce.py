"""Carry complete register groups through CFG edges as one wider SSA value.

Byte lanes remain the aliasing interface for p-code and snapshots. Wider inputs
and phis replace lane tuples at block boundaries; explicit BYTE operations bridge
partial writes. Canonicalization removes complete extraction/reassembly pairs.
This changes representation only, without inferring dead upper register bits.
"""


def registers(s, groups):
    """Coalesce complete groups before the builder simplifies their lane phis."""
    s.entry_ops = []
    for keys in groups:
        keys = tuple(keys)
        if len(keys) <= 1 or not all(key in s.inputs for key in keys):
            continue
        source = s.value("INPUT", len(keys), data=keys[0])
        for n, key in enumerate(keys):
            byte = s.value("BYTE", 1, [source], n)
            s.entry_ops.append(byte)
            s.aliases[s.inputs[key].id] = byte
        phis = {}
        for i, b in s.blocks.items():
            lanes = [b.state[key] for key in keys]
            predecessors = lanes[0].data[1]
            phi = s.value("PHI", len(keys), data=(keys, predecessors))
            phis[i] = phi
            b.phis.append(phi)
            bytes_ = [s.value("BYTE", 1, [phi], n) for n in range(len(keys))]
            b.ops[:0] = bytes_
            for lane, byte in zip(lanes, bytes_):
                s.aliases[lane.id] = byte
        for i, phi in phis.items():
            args = []
            for p in phi.data[1]:
                if p == -1:
                    args.append(source)
                    continue
                b = s.blocks[p]
                packed = s.value("PACK", len(keys), [b.exit[key] for key in keys])
                # Keep the terminator last. These pure copies do not move or
                # change any access or its captured state.
                at = next((n for n, v in enumerate(b.ops)
                           if v.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP")), len(b.ops))
                b.ops.insert(at, packed)
                args.append(packed)
            phi.args = tuple(args)
