from collections import defaultdict, deque

from .values import MAX_FIELD_OFFSET

CONFLICTING = (("pointer", "scalar_write"), ("vtable", "scalar_write"), ("vtable", "pointer"))
IGNORED_KINDS = frozenset(("null", "stack"))


def _new_field():
    return {"kinds": {}, "labels": {}, "sizes": set(), "pointees": set(), "refs": set(), "tables": set()}


def _initial(structs, labels):
    shapes = defaultdict(dict)
    for (node, offset), label, evidence in labels:
        shapes[node].setdefault(offset, _new_field())["labels"].setdefault(label, (node, offset, evidence))
    for struct in structs:
        node = struct["node"]
        for report in struct["fields"]:
            kinds = set(report["kinds"]) - IGNORED_KINDS
            if any(entry["access"] == "write" for entry in report["evidence"].get("scalar", [])):
                kinds.add("scalar_write")
            if not kinds and not report["pointee"]:
                continue
            field = shapes[node].setdefault(report["offset"], _new_field())
            field["kinds"].update((kind, (node, report["offset"], None)) for kind in kinds)
            field["sizes"] |= set(report["sizes"])
            field["pointees"] |= set(report["pointee"])
            field["tables"] |= set(report.get("tables", ()))
    return shapes


class _Solver:
    def __init__(self, shapes):
        self.shapes = shapes
        self.reasons = {}
        self.dependents = defaultdict(list)
        self.queue, self.pending = deque(), {}

    def require(self, requirer, provider, reason):
        key = (requirer, provider)
        if requirer == provider or key in self.reasons:
            return
        self.reasons[key] = reason
        self.dependents[provider[0]].append(key)
        self._enqueue(key, None)

    def _enqueue(self, key, offsets):
        if key not in self.pending:
            self.queue.append(key)
            self.pending[key] = offsets
        elif self.pending[key] is not None:
            self.pending[key] = None if offsets is None else self.pending[key] | offsets

    def run(self):
        while self.queue:
            key = self.queue.popleft()
            changed = self._apply(key, self.pending.pop(key))
            if changed:
                for other in self.dependents[key[0][0]]:
                    self._enqueue(other, changed)

    def _apply(self, key, offsets):
        (required_node, required_at), (provided_node, provided_at) = key
        reason = self.reasons[key]
        shift = required_at - provided_at
        required = self.shapes[required_node]
        provided = self.shapes.get(provided_node, {})
        changed = set()
        for offset in list(provided) if offsets is None else offsets & provided.keys():
            given, at = provided[offset], offset + shift
            if abs(at) >= MAX_FIELD_OFFSET:
                continue
            field = required.get(at)
            if field is None:
                field = required[at] = _new_field()
                changed.add(at)
            for name in ("kinds", "labels"):
                for kind, (origin, origin_at, _) in given[name].items():
                    if kind not in field[name]:
                        field[name][kind] = (origin, origin_at, reason)
                        changed.add(at)
            for name in ("sizes", "tables"):
                if not given[name] <= field[name]:
                    field[name] |= given[name]
                    changed.add(at)
            targets = given["pointees"] | given["refs"]
            if field["pointees"]:
                for pointee in field["pointees"]:
                    for target in targets:
                        self.require(pointee, target, ("pointee", required_node, at))
            elif not targets <= field["refs"]:
                field["refs"] |= targets
                changed.add(at)
        return changed


def _same_field(shapes):
    for node, fields in list(shapes.items()):
        for offset, field in fields.items():
            first, *rest = sorted(field["pointees"]) or [None]
            for other in rest:
                reason = ("field", node, offset)
                yield first, other, reason
                yield other, first, reason


def propagate(structs, labels, requirements):
    solver = _Solver(_initial(structs, labels))
    for requirer, provider, reason in [*requirements, *_same_field(solver.shapes)]:
        solver.require(requirer, provider, reason)
    solver.run()
    return solver.shapes, conflicts(solver.shapes), len(solver.reasons)


def conflicts(shapes):
    found = []
    for node, fields in shapes.items():
        for offset, field in fields.items():
            kinds = field["kinds"]
            if any(first in kinds and second in kinds for first, second in CONFLICTING):
                found.append({"node": node, "offset": offset, "reason": "kinds",
                              "origins": {kind: kinds[kind] for kind in sorted(kinds)}})
        overlap = _overlapping(fields)
        if overlap:
            found.append({"node": node, "offsets": overlap, "reason": "overlap"})
    return found


def _overlapping(fields):
    extents = sorted({(offset, size) for offset, field in fields.items() for size in field["sizes"]})
    offsets = set()
    for k, (first, first_size) in enumerate(extents):
        for second, second_size in extents[k + 1:]:
            if second >= first + first_size:
                break
            if first < second and first + first_size < second + second_size:
                offsets.update((first, second))
    return sorted(offsets)
