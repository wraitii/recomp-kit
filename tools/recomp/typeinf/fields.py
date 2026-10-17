from collections import defaultdict

VTABLE, POINTER, SCALAR, NULL, STACK, UNKNOWN = "vtable", "pointer", "scalar", "null", "stack", "unknown"


class _Field:
    def __init__(self):
        self.evidence = defaultdict(list)
        self.slots = set()
        self.tables = set()
        self.pointees = set()

    def add(self, kind, entry):
        self.evidence[kind].append(entry)

    def sizes(self):
        return sorted({entry["size"] for entries in self.evidence.values() for entry in entries})


def _node(addr, value):
    return (addr, value.id)


def struct_reports(accesses, calls, frames, vtable_slots, addressable):
    fields = defaultdict(dict)
    attributable, stats = _split_accesses(accesses, frames)
    objects = {_node(addr, access.base) for addr, access in attributable}
    slots = _call_slots(calls, frames)
    for addr, access in attributable:
        field = fields[_node(addr, access.base)].setdefault(access.constant, _Field())
        _add_access(field, objects, addr, access, slots, frames, vtable_slots, addressable)
    structs = [{"node": root, "fields": [_field_report(offset, field) for offset, field in sorted(by_offset.items())]}
               for root, by_offset in fields.items()]
    return sorted(structs, key=lambda struct: struct["node"]), stats


def _split_accesses(accesses, frames):
    kept, unattributed, on_stack = [], 0, 0
    for addr, access in accesses:
        if access.base is None:
            unattributed += 1
        elif access.base.id in frames[addr]:
            on_stack += 1
        else:
            kept.append((addr, access))
    return kept, {"unattributed_accesses": unattributed, "frame_accesses": on_stack}


def _call_slots(calls, frames):
    slots = defaultdict(set)
    for addr, call in calls:
        if call.base_value.id not in frames[addr]:
            slots[_node(addr, call.base_value)].add(call.displacement)
    return slots


def _opaque(entry):
    return SCALAR if entry["size"] < 4 else UNKNOWN


def _add_access(field, objects, addr, access, slots, frames, vtable_slots, addressable):
    entry = {"function": addr, "site": access.site_addr, "access": access.access, "size": access.size}
    value = access.value
    if value.id in frames[addr]:
        field.add(STACK, entry)
    elif value.opc == "CONST":
        _add_constant(field, value.data, entry, vtable_slots, addressable)
    elif access.value_base is None or entry["size"] != 4:
        field.add(_opaque(entry), entry)
    else:
        _add_value(field, objects, addr, access, entry, slots)


def _add_constant(field, data, entry, vtable_slots, addressable):
    if data == 0:
        field.add(NULL, entry)
        return
    found = vtable_slots(data) if entry["size"] == 4 else None
    if found:
        field.add(VTABLE, dict(entry, vtable=data, slot_count=len(found)))
        field.slots.update(found)
        field.tables.add(data)
    elif addressable(data):
        field.add(UNKNOWN, entry)
    else:
        field.add(SCALAR, entry)


def _add_value(field, objects, addr, access, entry, slots):
    root = _node(addr, access.value_base)
    field.pointees.add((root, access.value_offset))
    if root in slots and access.value_offset == 0:
        field.add(VTABLE, entry)
        field.slots.update(slots[root])
    elif root in objects:
        field.add(POINTER, entry)
    else:
        field.add(UNKNOWN, entry)


def _field_report(offset, field):
    report = {
        "offset": offset,
        "sizes": field.sizes(),
        "kinds": sorted(field.evidence),
        "evidence": dict(field.evidence),
        "pointee": sorted(field.pointees),
    }
    if VTABLE in field.evidence:
        report["slots"] = sorted(field.slots)
        report["tables"] = sorted(field.tables)
    return report
