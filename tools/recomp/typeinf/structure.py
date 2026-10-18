"""Receiver and interface-prefix bounds for indirect calls."""
from collections import defaultdict

from .dispatch import ProofError


class Structure:
    def __init__(self, facts, image, observed, aliases=None):
        self.facts = facts
        self.image = image
        self.observed = observed
        self.aliases = aliases
        self._objects = {}
        self.installs = defaultdict(list)
        self.anomalies = []
        table_writes = set()
        for addr, found in facts.items():
            for site, base, offset, table in found.writes:
                if table is not None:
                    table_writes.add((addr, base, offset))
                    self.installs[table].append((addr, site, base, offset))
        for addr, found in facts.items():
            for write in found.field_writes:
                if write.offset < 0 or write.value[0] == "null":
                    continue
                if (addr, write.base, write.offset) in table_writes:
                    if not (write.value[0] == "const" and self.image.program.pe.rd32(write.value[1]) is not None):
                        self.anomalies.append((addr, write.site, write.base, write.offset))
        self._extents = {}
        self._cleanups = {}
        self._profiles = {}
        self._methods = None
        self._site_cache = {}
        self._req_cache = {}
        self._compatible = {}
        self._rejected = {site for site, _ in observed.rejected}

    def extent(self, table):
        if table not in self._extents:
            try:
                self._extents[table] = self.image.extent(table)
            except ProofError:
                self._extents[table] = ()
        return self._extents[table]

    def _cleanup(self, target, seen=frozenset()):
        if target in self._cleanups:
            return self._cleanups[target]
        if target in seen or target in self.image.program.noreturn:
            return None
        fact = self.facts.get(target)
        if fact is None or fact.error is not None:
            return None
        result = fact.pops
        if result is None:
            instructions = self.image.program.instructions(target)
            if 1 <= len(instructions) <= 2:
                pe = self.image.program.pe
                records = [pe.decode_span_record(at, pe.bytes_at(at, size)) for at, size in instructions]
                if all(record is not None for record in records):
                    tail = records[-1]
                    adjusts = all(record.opcode in ("ADD", "SUB") and len(record.operands) == 2
                                  and record.operands[0].kind == "reg" and record.operands[0].reg == "ECX"
                                  and record.operands[1].kind == "imm" for record in records[:-1])
                    if (adjusts and tail.opcode == "JMP" and len(tail.operands) == 1
                            and tail.operands[0].kind == "imm"):
                        result = self._cleanup(tail.operands[0].immediate, seen | {target})
        self._cleanups[target] = result
        return result

    def _profile(self, table):
        if table not in self._profiles:
            self._profiles[table] = tuple(self._cleanup(self.image.program.pe.rd32(table + slot))
                                          for slot in self.extent(table))
        return self._profiles[table]

    def _anchors(self, slot, target):
        if self._methods is None:
            self._methods = defaultdict(set)
            for table in self.installs:
                for offset in self.extent(table):
                    self._methods[(offset, self.image.program.pe.rd32(table + offset))].add(table)
        return self._methods.get((slot, target), ())

    @staticmethod
    def _object(identity):
        if identity[0] != "field":
            return None
        return ("offset", identity[1], identity[2]) if identity[2] else identity[1]

    @staticmethod
    def _stable(identity):
        if identity[0] == "offset":
            return Structure._stable(identity[1])
        return identity[0] in ("reg", "arg", "call", "import", "const")

    def _site_requirements(self, site, slot):
        if site in self._site_cache:
            return self._site_cache[site]
        groups, reasons = set(), set()
        if slot < 0 or slot % 4:
            reasons.add("virtual slot is not a nonnegative pointer-sized offset")
        elif site in self._rejected:
            reasons.add("observed target is unlifted")
        else:
            for target in self.observed.targets_at(site):
                if target in self.image.program.noreturn:
                    continue
                fact = self.facts.get(target)
                if fact is None or fact.error is not None:
                    reasons.add("observed target is unknown or unlifted")
                    continue
                anchors = self._anchors(slot, target)
                if not anchors:
                    reasons.add("observed target has no installed table at the called slot")
                    continue
                groups.add(frozenset(self._profile(table)[:slot // 4 + 1] for table in anchors))
        result = {slot: groups}, reasons
        self._site_cache[site] = result
        return result

    @staticmethod
    def _merge(into, other):
        slots, reasons = other
        for slot, groups in slots.items():
            into[0].setdefault(slot, set()).update(groups)
        into[1].update(reasons)

    def _requirements(self, function, identity, seen=frozenset()):
        key = (function, identity)
        if key in self._req_cache:
            return self._req_cache[key]
        if key in seen:
            return {}, set()
        fact = self.facts.get(function)
        if fact is None:
            return {}, {"outside scope: no facts for 0x%08x" % function}
        slots, reasons = {}, set()
        for site, slot, _, _, _, _, receiver in fact.sites:
            if {self._object(item) for item in receiver} == {identity}:
                self._merge((slots, reasons), self._site_requirements(site, slot))
        for site, target, _, _ in fact.calls:
            for name, sources in (fact.call_values.get(site, {}) or {}).items():
                if identity in sources:
                    self._merge((slots, reasons), self._requirements(target, ("reg", name), seen | {key}))
            for k, sources in (fact.call_arguments.get(site, {}) or {}).items():
                if identity in sources:
                    self._merge((slots, reasons), self._requirements(target, ("arg", k), seen | {key}))
        result = slots, reasons
        if not reasons:
            self._req_cache[key] = result
        return result

    def _tables(self, slot, groups):
        key = slot, frozenset(groups)
        if key not in self._compatible:
            tables = set()
            for table in self.installs:
                if slot not in self.extent(table):
                    continue
                profile = self._profile(table)[:slot // 4 + 1] if groups else ()
                if all(any(all(a is None or b is None or a == b for a, b in zip(profile, anchor))
                           for anchor in alternatives) for alternatives in groups):
                    tables.add(table)
            self._compatible[key] = frozenset(tables)
        return self._compatible[key]

    def _receiver(self, caller, site):
        if self.aliases is None:
            return None
        if caller not in self._objects:
            paths = defaultdict(set)
            for found in self.facts[caller].sites:
                for identity in found[6]:
                    path = []
                    while identity[0] == "field":
                        path.append(identity[2])
                        identity = identity[1]
                    if len(path) > 1:
                        paths[tuple(path)].add(found[1])
            objects = self.aliases.function(caller)["objects"] if any(len(slots) > 1 for slots in paths.values()) else ()
            self._objects[caller] = {use["site"]: obj for obj in objects for use in obj["uses"]}
        return self._objects[caller].get(site)

    def bound(self, caller, site):
        address, slot, vptr_fields, _, _, _, receiver = site
        objects = {self._object(item) for item in receiver}
        observed = sorted(self.observed.targets_at(address))
        result = {"open": None, "tables": [], "targets": [], "observed": observed}
        if not objects or None in objects:
            return {**result, "open": "receiver is not an identified vptr load"}
        slots, reasons = {}, set()
        self._merge((slots, reasons), self._site_requirements(address, slot))
        receiver_type = self._receiver(caller, address)
        if receiver_type is not None:
            required = next(use["requirements"] for use in receiver_type["uses"] if use["site"] == address)
            for use in receiver_type["uses"]:
                if use["site"] in required:
                    self._merge((slots, reasons), self._site_requirements(use["site"], use["slot"]))
        if len(objects) == 1:
            identity = next(iter(objects))
            if self._stable(identity):
                self._merge((slots, reasons), self._requirements(caller, identity))
        if reasons:
            return {**result, "open": sorted(reasons)[0]}
        if not vptr_fields or any(node is None or at is None for node, at in vptr_fields):
            return {**result, "open": "receiver field form is not exact"}
        if self.anomalies:
            return {**result, "open": "a vptr store writes a non-table value at 0x%08x" % self.anomalies[0][1]}
        tables = set(self.installs)
        for offset, groups in slots.items():
            tables.intersection_update(self._tables(offset, groups))
        targets = {self.image.program.pe.rd32(table + slot) for table in tables}
        for target in targets:
            fact = self.facts.get(target)
            if target not in self.image.program.functions or fact is None or fact.error is not None:
                return {**result, "open": "candidate target 0x%08x is not a lifted function" % (target or 0)}
        violations = sorted(set(observed) - targets)
        if violations:
            return {**result, "open": "structural bound excludes observed targets", "violations": violations}
        terminal = targets & self.image.program.noreturn
        own = self._site_requirements(address, slot)[0]
        unified = None if receiver_type is None else {
            **receiver_type, "kind": "structural-nominal", "at": address,
            "methods": [{"slot": offset, "targets": sorted({self.image.program.pe.rd32(table + offset)
                                                             for table in tables})}
                        for offset in sorted(slots)]}
        return {**result, "tables": sorted(tables), "targets": sorted(targets), "receiver_type": unified,
                "returning_targets": sorted(targets - terminal), "terminal_targets": sorted(terminal),
                "prefix_slots": max(slots) // 4 + 1,
                "shared_receiver": slots != own,
                "basis": "structure+observed" if observed else "structure"}
