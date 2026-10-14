"""Conservative must-analysis for already-published CPU fields.

The default retains every memory observation. Optional access_fields restricts
publication at guest loads and stores under an explicitly selected performance
policy. A field store is redundant only when
all incoming paths prove its current value already resides in the CPU. Facts
are relative to block entry/exit state, so a loop phi's previous iteration can
never be mistaken for its newly assigned value. Helpers invalidate facts.
"""
from dataclasses import dataclass
from .operations import EXITS, OBSERVES, OPAQUE

#: Policy: observers whose contract permits the exact/relaxed field policy.
RELAXABLE_ACCESS = OBSERVES - OPAQUE - EXITS


@dataclass
class PublicationPlan:
    required: dict
    known_before: dict
    resident_before: dict


def plan(s, successors, fields, *, access_fields=None, reloaded=(), kept=None,
         lazy_fields=(), preserved=None, eager_events=()):
    return analyze(s, successors, fields, access_fields=access_fields,
                   reloaded=reloaded, kept=kept, lazy_fields=lazy_fields,
                   preserved=preserved, eager_events=eager_events).required


def analyze(s, successors, fields, *, access_fields=None, reloaded=(), kept=None,
            lazy_fields=(), preserved=None, eager_events=(), returned_lazy=()):
    """Return publication requirements and facts already known before each event.

`fields` groups register lanes by runtime field. LOAD/STORE have read-only CPU
observers on normal continuation in the current accessor contract: the store
watchpoint reads only the address and value, and null-check builds, whose fault
dispatch exposes the CPU, translate with fault_state = "exact". access_fields (the locals
policy) therefore names the only fields published at accesses. DIV32 may
return through an error handler; it therefore invalidates all publication facts.
A field in `reloaded` is known after a helper: its reload reads the CPU.
`kept` maps a CALL to the fields its callee neither reads nor writes: the call
does not publish them, and they stay known across it only if already known.
`lazy_fields` may reside in a descriptor at non-eager observers. Preserved
flags retain that residency across calls, but eager observers require fields.
`returned_lazy` identifies full-output calls whose returned descriptors can
stay resident without reloading fields; specialized calls can omit outputs.
At joins each fact requires every predecessor to carry its exit value.
Starting with no facts gives a conservative least fixed point for loops.

"""
    groups = [tuple(key for key in keys if key in s.inputs) for keys in fields]
    groups = [keys for keys in groups if keys]
    access_fields = None if access_fields is None else frozenset(access_fields)
    predecessors = {i: [] for i in s.blocks}
    for e in [s.entry] + s.alternates:
        predecessors[e].append(-1)
    for i in s.blocks:
        for j in set(successors[i]):
            predecessors[j].append(i)

    resolved = {}

    def values(state, keys):
        cache_key = id(state), keys
        current = resolved.get(cache_key)
        if current is None:
            current = tuple(s.resolve(state[key]) for key in keys)
            resolved[cache_key] = current
        return current

    lazy_fields = frozenset(lazy_fields)
    eager_events = frozenset(eager_events)
    known_exit = {i: set() for i in s.blocks}
    resident_exit = {i: set() for i in s.blocks}
    publications = {}
    known_before = {}
    resident_before = {}
    returned_lazy = frozenset(returned_lazy)
    changed = True
    while changed:
        changed = False
        for i, b in s.blocks.items():
            known = {n: values(b.state, keys) for n, keys in enumerate(groups)
                     if all((p == -1 and not any(key in lazy_fields for key in keys))
                            or (p != -1 and n in known_exit[p]) for p in predecessors[i])}
            resident = {n: values(b.state, keys) for n, keys in enumerate(groups)
                        if all(p == -1 or n in resident_exit[p] for p in predecessors[i])}
            for v in b.ops:
                if v.opc not in OBSERVES:
                    continue
                state = b.exit if v.opc in EXITS else b.snapshots[v.id]
                access = v.opc in RELAXABLE_ACCESS
                held = kept.get(v.id, ()) if kept else ()
                saved = preserved.get(v.id, ()) if preserved else ()
                kept_groups, kept_known, kept_resident = set(), set(), set()
                required = []
                already_known = []
                already_resident = []
                for n, keys in enumerate(groups):
                    current = values(state, keys)
                    if known.get(n) == current:
                        already_known.extend(keys)
                    if resident.get(n) == current:
                        already_resident.extend(keys)
                    if access and access_fields is not None and not any(key in access_fields for key in keys):
                        # A skipped snapshot does not make a publication fact.
                        # The older CPU value stays known until a real observer.
                        continue
                    if held and all(key in held for key in keys):
                        kept_groups.add(n)
                        if known.get(n) == current:
                            kept_known.add(n)
                        if resident.get(n) == current:
                            kept_resident.add(n)
                        continue
                    lazy = all(key in lazy_fields for key in keys)
                    available = resident if lazy and v.opc not in eager_events else known
                    if available.get(n) != current:
                        required.extend(keys)
                    if v.opc != "BRANCHIND":
                        # Only a table jump's default arm publishes; its case
                        # edges leave the CPU as it was.
                        resident[n] = current
                        if lazy and v.opc not in eager_events:
                            known.pop(n, None)
                        else:
                            known[n] = current
                if v.opc not in eager_events and any(key in lazy_fields for key in required):
                    required.extend(key for keys in groups for key in keys
                                    if key in lazy_fields and key not in required)
                publications[v.id] = tuple(required)
                known_before[v.id] = tuple(already_known)
                resident_before[v.id] = tuple(already_resident)
                if v.opc in OPAQUE:
                    # A division error handler, an opaque callee and the string
                    # helper's fault path may leave arbitrary CPU state behind,
                    # so no must-fact survives.
                    saved_resident = dict(resident)
                    known.clear()
                    resident.clear()
                    reloads = b.reloads.get(v.id, {})
                    for n, keys in enumerate(groups):
                        if n in kept_groups:
                            if n in kept_known and not any(key in lazy_fields for key in keys):
                                known[n] = values(reloads, keys)
                            if n in kept_resident:
                                resident[n] = values(reloads, keys)
                        elif all(key in saved and key in lazy_fields and key in reloads for key in keys):
                            current = values(state, keys)
                            if saved_resident.get(n) == current:
                                resident[n] = values(reloads, keys)
                        elif all(key in reloaded and key in reloads for key in keys):
                            known[n] = values(reloads, keys)
                            resident[n] = known[n]
                        elif v.id in returned_lazy and all(key in lazy_fields and key in reloads for key in keys):
                            resident[n] = values(reloads, keys)
            outgoing = {n for n, keys in enumerate(groups)
                        if known.get(n) == values(b.exit, keys)}
            resident_outgoing = {n for n, keys in enumerate(groups)
                                 if resident.get(n) == values(b.exit, keys)}
            if resident_outgoing != resident_exit[i]:
                resident_exit[i] = resident_outgoing
                changed = True
            if outgoing != known_exit[i]:
                known_exit[i] = outgoing
                changed = True
    return PublicationPlan(publications, known_before, resident_before)
