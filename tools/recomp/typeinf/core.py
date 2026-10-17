import functools
import time
from collections import defaultdict, namedtuple

import numpy as np

from .fields import struct_reports
from .interfaces import import_signature
from .shapes import propagate
from .values import (FRAME_REGISTERS, GPRS, absolute_call_targets, call_register_lanes, direct_call_sites,
                     frame_ids, function_values, indirect_call_sites, lane_views, loaded_field, memory_accesses,
                     preserved_registers, return_register_lanes, stack_arguments, stack_pops, value_views,
                     whole_lane_value)

CallRecord = namedtuple("CallRecord", "caller kind target imported registers")
CALLEE_SAVED = frozenset(("EBX", "ESI", "EDI", "EBP"))
MAX_SLOTS = 1024
MAX_VISITS = 16


class Functions:
    def __init__(self, program):
        self.program = program
        self.values, self.fallbacks, self.errors, self.stack_pops = {}, {}, {}, {}
        self.unbalanced = set()
        self.locations = None

    def lift(self, addrs, deadline=float("inf")):
        lifted, skipped = {}, []
        for addr in addrs:
            if addr not in self.values and addr not in self.fallbacks and addr not in self.errors:
                if time.monotonic() >= deadline:
                    skipped.append(addr)
                    continue
                self._lift(addr)
            if addr in self.values:
                lifted[addr] = self.values[addr]
        return lifted, skipped

    def _lift(self, addr):
        try:
            fv = function_values(self.program, addr)
        except Exception as error:
            self.errors[addr] = str(error)
            return
        if fv.ssa is None:
            self.fallbacks[addr] = fv.fallback
        else:
            self.values[addr] = fv

    def pops(self, addr):
        if addr in self.values:
            return self.values[addr].pops if addr not in self.unbalanced else None
        if addr not in self.program.functions:
            return None
        if addr not in self.stack_pops:
            try:
                body = self.program.body(addr)
            except Exception:
                self.stack_pops[addr] = None
            else:
                fir = body.ir
                tail = bool(fir.import_tails or body.exits) or any(op.opc == "TAIL" for ins in fir.insns for op in ins.ops)
                self.stack_pops[addr] = stack_pops(fir, tail)
        return self.stack_pops[addr]

    def stored_at(self, target):
        if self.locations is None:
            pe = self.program.pe
            words = np.frombuffer(pe.data[:len(pe.data) // 4 * 4], dtype="<u4")
            hits = np.flatnonzero(np.isin(words, np.fromiter(self.program.functions, dtype=np.uint32)))
            self.locations = defaultdict(list)
            for index in hits.tolist():
                at = pe.base + 4 * index
                if not pe.is_exec(at):
                    self.locations[int(words[index])].append(at)
        return self.locations.get(target, ())

    def tables(self, targets, displacement):
        found = set()
        for target in targets:
            for at in self.stored_at(target):
                table = at - displacement
                if self._table_prefix(table, displacement):
                    found.add(table)
        return found

    def _table_prefix(self, table, displacement):
        pe = self.program.pe
        if pe.is_exec(table) or pe.rd32(table) not in self.program.functions:
            return False
        return all(pe.is_exec(pe.rd32(table + slot) or 0) for slot in range(4, displacement + 4, 4))

    def slot_methods(self, table, displacement):
        methods = [self.program.pe.rd32(table + slot) for slot in range(0, displacement + 4, 4)]
        return [method for method in methods if method in self.program.functions]

    def callees(self, fv, observed):
        found = {call.target for call in direct_call_sites(fv)}
        for record in indirect_call_sites(fv):
            targets = observed.targets_at(record.site_addr)
            found |= targets
            tables = self.tables(targets, record.displacement) if targets else ()
            if len(tables) == 1:
                found.update(self.slot_methods(next(iter(tables)), record.displacement))
        return found & self.program.functions.keys()


def call_closure(functions, roots, observed, depth, deadline=float("inf")):
    seeds, frontier = set(roots), set(roots)
    for _ in range(depth):
        lifted, _ = functions.lift(sorted(frontier), deadline)
        new = set()
        for fv in lifted.values():
            new |= functions.callees(fv, observed)
        frontier = new - seeds
        seeds |= new
    return sorted(seeds)


def _view(addr, root, offset):
    return ((addr, root.id), offset)


def _views(addr, fv, lanes, preserves):
    return [_view(addr, root, offset) for root, offset in lane_views(fv, lanes, preserves)]


def _entry_values(fv):
    found = {}
    for name in GPRS:
        value = whole_lane_value(fv.ssa, fv.entry[name])
        if value is not None:
            found[value.id] = name
    return found


def _preserved(functions, lifted, abi):
    callers = defaultdict(set)
    for addr, fv in lifted.items():
        for call in direct_call_sites(fv):
            callers[call.target].add(addr)
    preserved, assumed = {addr: frozenset() for addr in lifted}, {}

    def preserves(target, name):
        if target is None or target not in lifted:
            return abi and name in CALLEE_SAVED
        return name in preserved.get(target, ())

    pending, visits = set(lifted), defaultdict(int)
    while pending:
        addr = pending.pop()
        found, assumed[addr], balanced = preserved_registers(lifted[addr], preserves, functions.pops)
        visits[addr] += 1
        if (not balanced) != (addr in functions.unbalanced) and visits[addr] <= MAX_VISITS:
            functions.unbalanced.symmetric_difference_update({addr})
            pending |= callers[addr] & lifted.keys()
        if found != preserved[addr]:
            preserved[addr] = found
            pending |= callers[addr] & lifted.keys()
    return preserved, {addr: names for addr, names in assumed.items() if names}, preserves


def _call_records(lifted, observed, program):
    records = []
    for addr, fv in lifted.items():
        direct = {call.site_addr: call.target for call in direct_call_sites(fv)}
        absolute = absolute_call_targets(fv)
        for registers in call_register_lanes(fv):
            site = registers.site_addr
            imported = program.pe.iat_keys.get(absolute.get(site))
            if site in direct:
                records.append(CallRecord(addr, "call", direct[site], None, registers))
            elif imported is not None:
                records.append(CallRecord(addr, "import", None, tuple(imported.split("!", 1)), registers))
            for target in sorted(observed.targets_at(site)):
                records.append(CallRecord(addr, "observed", target, None, registers))
    return records


def _exits(callee, fv, preserves):
    entries = _entry_values(fv)
    exits = {}
    for ret in return_register_lanes(fv):
        for name in GPRS:
            if name in FRAME_REGISTERS:
                continue
            for root, offset in lane_views(fv, ret.lanes[name], preserves):
                through = entries.get(root.id)
                exits.setdefault(name, set()).add(("through", through, offset) if through
                                                  else ("local", _view(callee, root, offset)))
    return exits


def _link_calls(lifted, calls, preserves):
    requirements = []
    exits = {}
    for call in calls:
        if call.target not in lifted:
            continue
        callee, caller = lifted[call.target], lifted[call.caller]
        if call.target not in exits:
            exits[call.target] = _exits(call.target, callee, preserves)
        site = call.registers.site_addr
        before = {name: _views(call.caller, caller, call.registers.before[name], preserves) for name in GPRS}
        for name in GPRS:
            if name in FRAME_REGISTERS:
                continue
            entry = whole_lane_value(callee.ssa, callee.entry[name])
            if entry is not None:
                requirements.extend((view, _view(call.target, entry, 0), (call.kind, site, name))
                                    for view in before[name])
            posts = _views(call.caller, caller, call.registers.after[name], preserves)
            for exit_view in exits[call.target].get(name, ()):
                if exit_view[0] == "local":
                    requirements.extend((post, exit_view[1], ("return", site, name)) for post in posts)
                    continue
                _, source, offset = exit_view
                for (pre_node, pre_at) in before[source]:
                    for post in posts:
                        reason = ("through", site, name, source)
                        requirements.append((post, (pre_node, pre_at + offset), reason))
                        requirements.append(((pre_node, pre_at + offset), post, reason))
    return requirements


def _import_labels(lifted, calls, preserves):
    labels, sites = [], []
    for call in calls:
        if call.kind != "import":
            continue
        signature = import_signature(*call.imported)
        if signature is None:
            continue
        fv = lifted[call.caller]
        site = call.registers.site_addr
        evidence = ("import", site, "!".join(call.imported))
        found = []
        if signature.returns:
            for view in _views(call.caller, fv, call.registers.after["EAX"], preserves):
                found.append((view, signature.returns, evidence))
        count = max(list(signature.inputs) + list(signature.outputs), default=-1) + 1
        arguments = stack_arguments(fv, call.registers, count)
        for k, interface in signature.inputs.items():
            if arguments[k] is not None:
                found.extend((_view(call.caller, root, offset), interface, evidence)
                             for root, offset in value_views(fv, arguments[k], preserves))
        for k, interface in signature.outputs.items():
            if arguments[k] is not None:
                for root, offset in value_views(fv, arguments[k], preserves):
                    found.append(((("out", site, k), 0), interface, evidence))
                    sites.append(((call.caller, root.id), offset, ("out", site, k)))
        labels.extend(found)
    return labels, sites


def _forms(fv, root, offset, preserves):
    if root is None:
        return [(None, None)]
    return [(leaf, at + offset) for leaf, at in value_views(fv, root, preserves)] or [(root, offset)]


def _resolved(fv, access, preserves):
    values = _forms(fv, access.value_base, access.value_offset or 0, preserves)
    return [access._replace(base=base, constant=constant, value_base=value_base,
                            value_offset=None if value_base is None else value_offset)
            for base, constant in _forms(fv, access.base, access.constant, preserves)
            for value_base, value_offset in values]


def _resolved_calls(fv, record, preserves):
    return [record._replace(base_value=base, displacement=displacement)
            for base, displacement in _forms(fv, record.base_value, record.displacement, preserves)]


@functools.cache
def vtable_slots(program, addr):
    if addr is None or program.pe.is_exec(addr) or program.pe.rd32(addr) not in program.functions:
        return None
    slots = []
    while len(slots) < MAX_SLOTS:
        word = program.pe.rd32(addr + 4 * len(slots))
        if word is None or not program.pe.is_exec(word):
            break
        slots.append(4 * len(slots))
    return tuple(slots)


def _vtable_facts(functions, lifted, calls, indirect, observed, preserves):
    registers = {(call.caller, call.registers.site_addr): call.registers for call in calls}
    requirements, extra, candidates = [], [], {}
    for addr, record in indirect:
        targets = observed.targets_at(record.site_addr)
        if not targets:
            continue
        tables = functions.tables(targets, record.displacement)
        candidates[record.site_addr] = sorted(tables)
        if len(tables) != 1:
            continue
        table = next(iter(tables))
        fv = lifted[addr]
        holder = loaded_field(fv, record.base_value)
        if holder is not None:
            for root, offset in _forms(fv, holder[0], holder[1], preserves):
                extra.append(((addr, root.id), offset, table))
        before = registers.get((addr, record.site_addr))
        receivers = [] if before is None else _views(addr, fv, before.before["ECX"], preserves)
        for method in functions.slot_methods(table, record.displacement):
            node = this_node(method, lifted[method]) if method in lifted else None
            if node is not None:
                requirements.extend((view, (node, 0), ("vtable", record.site_addr, table)) for view in receivers)
    return requirements, extra, candidates


def _out_structs(structs, sites, tables):
    extra = defaultdict(list)
    for node, offset, pointee in sites:
        extra[node].append({"offset": offset, "sizes": [4], "kinds": [], "evidence": {}, "pointee": [(pointee, 0)]})
    for node, offset, table in tables:
        extra[node].append({"offset": offset, "sizes": [4], "kinds": ["vtable"], "evidence": {}, "pointee": [],
                            "tables": [table]})
    return structs + [{"node": node, "fields": fields} for node, fields in extra.items()]


def this_node(addr, fv):
    ecx = whole_lane_value(fv.ssa, fv.entry["ECX"])
    return None if ecx is None else (addr, ecx.id)


def _merged(shapes, nodes):
    merged = {}
    for node in nodes:
        for offset, field in shapes.get(node, {}).items():
            into = merged.setdefault(offset, {"kinds": {}, "labels": {}, "sizes": set(), "tables": set(),
                                              "sources": set()})
            for kind in field["kinds"]:
                into["kinds"].setdefault(kind, node[0])
            for label in field["labels"]:
                into["labels"].setdefault(label, node[0])
            into["sizes"] |= field["sizes"]
            into["tables"] |= field["tables"]
            into["sources"].add(node[0])
    return dict(sorted(merged.items()))


def _classes(program, lifted, shapes):
    constructors = defaultdict(set)
    for addr, fv in lifted.items():
        node = this_node(addr, fv)
        field = shapes.get(node, {}).get(0) if node is not None else None
        if field and field["kinds"].get("vtable", (None,))[0] == node:
            for table in field["tables"]:
                constructors[table].add(addr)
    classes = []
    for table, found in sorted(constructors.items()):
        methods = [(slot, program.pe.rd32(table + slot)) for slot in vtable_slots(program, table) or ()]
        members = sorted(found | {target for _, target in methods if target in lifted})
        nodes = [node for addr in members if (node := this_node(addr, lifted[addr])) is not None]
        classes.append({"vtable": table, "constructors": sorted(found), "methods": methods,
                        "shape": _merged(shapes, nodes)})
    return classes


def analyze(functions, seeds, observed, deadline=float("inf"), abi=True):
    program = functions.program
    seeds = list(dict.fromkeys(seeds))
    lifted, skipped = functions.lift(seeds, deadline)
    preserved, assumed, preserves = _preserved(functions, lifted, abi)
    calls = _call_records(lifted, observed, program)
    requirements = _link_calls(lifted, calls, preserves)
    labels, out_sites = _import_labels(lifted, calls, preserves)
    indirect = [(addr, record) for addr, fv in lifted.items() for record in indirect_call_sites(fv)]
    frames = {addr: frame_ids(fv) for addr, fv in lifted.items()}
    accesses = [(addr, resolved) for addr, fv in lifted.items() for access in memory_accesses(fv)
                for resolved in _resolved(fv, access, preserves)]
    indirect = [(addr, resolved) for addr, record in indirect
                for resolved in _resolved_calls(lifted[addr], record, preserves)]
    structs, access_stats = struct_reports(accesses, indirect, frames, lambda addr: vtable_slots(program, addr),
                                           lambda data: program.pe.rd32(data) is not None)
    vtable_requirements, vtable_holders, candidates = _vtable_facts(functions, lifted, calls, indirect, observed,
                                                                    preserves)
    shapes, conflicts, linked = propagate(_out_structs(structs, out_sites, vtable_holders), labels,
                                          requirements + vtable_requirements)
    witnesses = {record.site_addr: sorted(observed.targets_at(record.site_addr))
                 for _, record in indirect if observed.targets_at(record.site_addr)}
    return {
        "lifted": lifted,
        "functions": {
            "seeded": len(seeds),
            "lifted": len(lifted),
            "fallback": {addr: functions.fallbacks[addr] for addr in seeds if addr in functions.fallbacks},
            "errored": {addr: functions.errors[addr] for addr in seeds if addr in functions.errors},
            "skipped": skipped,
        },
        "preserved": preserved,
        "preserved_by_saved_slot": assumed,
        "shapes": shapes,
        "conflicts": conflicts,
        "classes": _classes(program, lifted, shapes),
        "labels": labels,
        "observed_targets": witnesses,
        "observed_tables": candidates,
        "observed_rejected": sorted(observed.rejected),
        "stats": {
            "requirements": len(requirements),
            "requirements_linked": linked,
            "labels": len(labels),
            "conflicts": len(conflicts),
            "preserving_functions": sum(1 for names in preserved.values() if names),
            "saved_slot_functions": len(assumed),
            "unbalanced_functions": sorted(functions.unbalanced & lifted.keys()),
            "abi_indirect_preservation": abi,
            "indirect_calls": len(indirect),
            "observed_witnessed_sites": len(witnesses),
            "observed_unique_tables": sum(1 for tables in candidates.values() if len(tables) == 1),
            "vtable_requirements": len(vtable_requirements),
            **access_stats,
        },
    }
