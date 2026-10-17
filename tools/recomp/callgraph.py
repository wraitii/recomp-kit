"""Whole-program entry graph: modeled callers and unmodeled entry paths."""
from collections import defaultdict
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Edge:
    source: int | None
    site: int | None
    target: int
    function: int
    provenance: str


class CallGraph:
    def __init__(self, edges, unknown, joins):
        self._edges = {function: tuple(sorted(found, key=_edge_key))
                       for function, found in edges.items()}
        self._unknown = {function: tuple(sorted(set(found)))
                         for function, found in unknown.items()}
        self.join_edges = {source: frozenset(targets) for source, targets in joins.items()}

    def callers(self, function):
        return self._edges.get(function, ())

    def unmodeled(self, function):
        return self._unknown.get(function, ())

    def can_enter_unmodeled(self, function):
        return bool(self._unknown.get(function))


def _edge_key(edge):
    return (edge.source if edge.source is not None else -1,
            edge.site if edge.site is not None else -1,
            edge.target, edge.provenance)


def _address_literals(program, targets):
    pe = program.pe
    data = pe.data
    if len(data) < 6 or not targets:
        return []
    values = np.ndarray((len(data) - 3,), dtype="<u4", buffer=data, strides=(1,))
    wanted = np.fromiter(sorted(targets), dtype=np.uint32)
    hits = np.flatnonzero(np.isin(values, wanted))
    if not hits.size:
        return []
    raw = np.frombuffer(data, dtype=np.uint8)
    starts = np.fromiter(program.owners(), dtype=np.int64)
    offsets = starts - pe.base
    offsets = offsets[(offsets >= 0) & (offsets + 5 < len(data))]
    first, second = raw[offsets], raw[offsets + 1]
    near = (first == 0xE8) | (first == 0xE9)
    conditional = (first == 0x0F) & ((second & 0xF0) == 0x80)
    branch = np.zeros(len(data), dtype=bool)
    for mask, delta in ((near, 1), (conditional, 2)):
        at = offsets[mask]
        for step in range(4):
            branch[at + delta + step] = True
    hits = hits[~branch[hits]]
    return [(int(values[index]), pe.base + int(index), pe.is_exec(pe.base + int(index)))
            for index in hits.tolist()]


def build(seed, program, settings, indirect):
    functions = set(program.functions)
    edges, unknown, joins = defaultdict(list), defaultdict(list), defaultdict(set)

    def function_of(target):
        return target if target in functions else program.containing(target)

    def add(source, site, target, provenance, join=False, reason=None):
        function = function_of(target)
        if function is None:
            return
        edges[function].append(Edge(source, site, target, function, provenance))
        if join and source is not None:
            joins[source].add(function)
        if reason is not None:
            unknown[function].append(reason)

    for addr, row in seed.scanned.items():
        for target in row.calls:
            add(addr, None, target, "direct_call", join=True)
        external = set(row.external_targets)
        jumps = set(row.jumps)
        for target in external:
            add(addr, None, target, "tail_jump" if target in jumps else "fallthrough", join=True)
        for target in set(row.references) - set(row.calls) - external - jumps:
            add(addr, None, target, "alternate_entry_reference",
                reason="unmodeled external reference from %08x" % addr)

    for (owner, site), targets in program.tables.items():
        for target in targets:
            function = function_of(target)
            if function is None:
                continue
            add(owner, site, target, "jump_table", join=function != owner)
            if target not in seed.internal.get(owner, ()):
                unknown[function].append("external jump-table entry at %08x" % target)

    for target, site in program.pe.relocated_pointers().items():
        add(None, site, target, "relocated_pointer",
            reason="address taken by relocation at %08x" % site)

    for target in settings.configured_entries | {settings.cfg["game"]["entry_point"]}:
        add(None, None, target, "configured_entry",
            reason="configured or process entry at %08x" % target)

    for site, (kind, targets) in indirect.items():
        owner = program.containing(site)
        for target in targets:
            add(owner, site, target, "indirect_target",
                reason="indirect %s target at %08x from %08x" % (kind, target, site))

    for target, site, executable in _address_literals(program, functions):
        add(None, site, target, "address_taken_operand" if executable else "address_taken_data",
            reason="address taken at %08x" % site)

    return CallGraph(edges, unknown, joins)
