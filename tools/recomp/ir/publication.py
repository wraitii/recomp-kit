"""Conservative must-analysis for already-published CPU fields.

The default retains every memory observation. Optional access_fields restricts
publication at guest loads and stores under an explicitly selected performance
policy. A field store is redundant only when
all incoming paths prove its current value already resides in the CPU. Facts
are relative to block entry/exit state, so a loop phi's previous iteration can
never be mistaken for its newly assigned value. Helpers invalidate facts.
"""
from .simplify import EFFECTS


def plan(s, successors, fields, *, access_fields=None, unpublished=frozenset(),
         boundary_skip=frozenset()):
    """Return required register-lane keys for each effect and return snapshot.

`fields` groups register lanes by runtime field. LOAD/STORE have read-only CPU
observers on normal continuation in the current accessor contract: the store
watchpoint reads only the address and value, and null-check builds, whose fault
dispatch exposes the CPU, compile the strict form. access_fields (the locals
policy) therefore names the only fields published at accesses. DIV32 may
return through an error handler; it therefore invalidates all publication facts.
At joins a field is known only if every predecessor published its exit value.
Starting with no facts gives a conservative least fixed point for loops.

`unpublished` is a corpus-only, UNPROVEN ceiling relaxation (`ceiling.py`): the
named effect opcodes publish nothing and leave the known-field facts untouched.
It defaults to empty and is never supplied by production selection.

`boundary_skip` names register lanes that CALL, CALLIND and RETURN do not
publish: the MSVC-convention arithmetic flags (`ir_ssa_msvc_convention`).
Like a skipped access snapshot, skipping creates no publication fact.
"""
    groups = [tuple(key for key in keys if key in s.inputs) for keys in fields]
    groups = [keys for keys in groups if keys]
    access_fields = None if access_fields is None else frozenset(access_fields)
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
                if v.opc in unpublished:
                    publications[v.id] = ()
                    continue
                state = b.exit if v.opc == "RETURN" else b.snapshots[v.id]
                access = v.opc in ("LOAD", "STORE", "X87_MEM")
                boundary = v.opc in ("CALL", "CALLIND", "RETURN")
                required = []
                for n, keys in enumerate(groups):
                    if boundary and all(key in boundary_skip for key in keys):
                        continue
                    if access and access_fields is not None and not any(key in access_fields for key in keys):
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
