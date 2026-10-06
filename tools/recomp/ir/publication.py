"""Conservative must-analysis for already-published CPU fields.

The default retains every memory observation. Optional read_fields restricts
ordinary-read publication under an explicitly selected performance policy. A field store is redundant only when
all incoming paths prove its current value already resides in the CPU. Facts
are relative to block entry/exit state, so a loop phi's previous iteration can
never be mistaken for its newly assigned value. Helpers invalidate facts.
"""
from .simplify import EFFECTS


def plan(s, successors, fields, *, read_fields=None):
    """Return required register-lane keys for each effect and return snapshot.

`fields` groups register lanes by runtime field. LOAD/STORE have read-only CPU
observers on normal continuation in the current accessor contract. DIV32 may
return through an error handler; it therefore invalidates all publication facts.
At joins a field is known only if every predecessor published its exit value.
Starting with no facts gives a conservative least fixed point for loops.
"""
    groups = [tuple(key for key in keys if key in s.inputs) for keys in fields]
    groups = [keys for keys in groups if keys]
    read_fields = None if read_fields is None else frozenset(read_fields)
    predecessors = {i: [] for i in s.blocks}
    predecessors[s.entry].append(-1)
    for i in s.blocks:
        for j in set(successors[i]):
            predecessors[j].append(i)

    def values(state, keys):
        return tuple(s.resolve(state[key]) for key in keys)

    known_exit = {i: set() for i in s.blocks}
    publications = {}
    changed = True
    while changed:
        changed = False
        for i, b in s.blocks.items():
            known = {n: values(b.state, keys) for n, keys in enumerate(groups)
                     if all(p == -1 or n in known_exit[p] for p in predecessors[i])}
            for v in b.ops:
                if v.opc not in EFFECTS and v.opc != "RETURN":
                    continue
                state = b.exit if v.opc == "RETURN" else b.snapshots[v.id]
                read_only = v.opc == "LOAD" or (v.opc == "X87_MEM" and
                    v.data["mnem"] not in ("FST", "FSTP", "FIST", "FISTP", "FNSTSW", "FNSTCW"))
                required = []
                for n, keys in enumerate(groups):
                    if read_only and read_fields is not None and not any(key in read_fields for key in keys):
                        # A skipped snapshot does not make a publication fact.
                        # The older CPU value stays known until a real observer.
                        continue
                    current = values(state, keys)
                    if known.get(n) != current:
                        required.extend(keys)
                    known[n] = current
                publications[v.id] = tuple(required)
                if v.opc in ("DIV32", "IDIV32", "CALL", "CALLIND", "MOVS32"):
                    # A division error handler, an opaque callee and the string
                    # helper's fault path may leave arbitrary CPU state behind,
                    # so no must-fact survives.
                    known.clear()
            outgoing = {n for n, keys in enumerate(groups)
                        if known.get(n) == values(b.exit, keys)}
            if outgoing != known_exit[i]:
                known_exit[i] = outgoing
                changed = True
    return publications
