"""Closed-world bounds on dispatch through a method's own `this` fields.

For every `CALL [[this+d]+s]` in a method that is entered only through vtable
slots, the bound is every constant table its objects' construction chains
write at `this+d`. The image has no relocations, so every use of a method or
table address is its literal bytes and the scan finds all of them. Each table
holding the method must be written only by recognized constant stores; from
those stores the chain descends into direct callees that install tables on
their own `this`, and climbs from installers to their direct callers. Any
write the chain cannot account for, or any link it cannot follow, fails the
site with a named reason.

Premises from the C++ object model, not checked: a call through `[[X]+s]`
passes X as `this`, and vtable pointers are written only by constant stores
in constructor and destructor chains that pass the object as `this`.
"""
import argparse
import json
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import numpy as np

KIT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(KIT / "tools"), str(KIT / "tools" / "recomp")]

from .core import vtable_slots  # noqa: E402
from .facts import cached, collect, contracts, default_jobs, source_key  # noqa: E402

MAX_POSITIONS = 32


class ProofError(Exception):
    pass


class Image:
    def __init__(self, program):
        self.program = program
        pe = program.pe
        self.raw = np.frombuffer(pe.data, dtype=np.uint8)
        wide = self.raw.astype(np.uint32)
        self.words = wide[:-3] | wide[1:-2] << 8 | wide[2:-1] << 16 | wide[3:] << 24
        self.executable = np.zeros(len(self.words), dtype=bool)
        for lo, hi in pe.exec_ranges:
            self.executable[max(0, lo - pe.base):max(0, hi - pe.base)] = True
        self.starts = {}
        self._literals = {}
        self._callers = None
        hits = np.flatnonzero(np.isin(self.words, np.fromiter(program.functions, dtype=np.uint32)))
        self._function_literals = defaultdict(list)
        for at, value in zip(hits.tolist(), self.words[hits].tolist()):
            self._function_literals[value].append(at)
        code = np.flatnonzero(self.executable)
        values = self.words[code]
        values = values[(values >= pe.base) & (values < pe.end)]
        offsets = (values - pe.base).astype(np.int64)
        self.code_values = set(np.unique(values[~self.executable[offsets]]).tolist())

    def literals(self, value):
        if value not in self._literals:
            positions = (self._function_literals[value] if value in self.program.functions
                         else np.flatnonzero(self.words == value).tolist())
            found = []
            for at in positions:
                addr, code = self.program.pe.base + at, bool(self.executable[at])
                if not code or self._operand(addr, value):
                    found.append((addr, code))
            self._literals[value] = found
        return self._literals[value]

    def code_literal(self, value):
        return value in self.code_values and any(code for _, code in self.literals(value))

    def _operand(self, addr, value):
        owner, start = self.instruction(addr)
        if start is None:
            return True
        pe = self.program.pe
        raw = pe.bytes_at(start, next(n for at, n in self.starts[owner] if at == start))
        ins = self.program.lifter.lift(start, raw, decode=pe.decode_span_record(start, raw))
        return any(v is not None and v[0] in ("const", "ram") and v[1] == value
                   for op in ins.ops for v in (op.out, *op.ins))

    def instruction(self, addr):
        owner = self.program.containing(addr)
        if owner is None:
            return None, None
        if owner not in self.starts:
            self.starts[owner] = self.program.instructions(owner)
        rows = self.starts[owner]
        k = bisect_right(rows, (addr, 1 << 32)) - 1
        return (owner, rows[k][0]) if k >= 0 and addr < rows[k][0] + rows[k][1] else (owner, None)

    def callers(self, target):
        if self._callers is None:
            n = len(self.words)
            at = np.arange(n - 6, dtype=np.int64)
            raw, words = self.raw[:n - 6], self.words
            near = at[self.executable[:n - 6] & ((raw == 0xE8) | (raw == 0xE9))]
            cond = at[self.executable[:n - 6] & (raw == 0x0F) & ((self.raw[1:n - 5] & 0xF0) == 0x80)]
            short = at[self.executable[:n - 6] & ((raw == 0xEB) | ((raw & 0xF0) == 0x70) | ((raw & 0xFC) == 0xE0))]
            sites = np.concatenate([near, cond, short]) + self.program.pe.base
            targets = np.concatenate([near + 5 + words[near + 1].view(np.int32),
                                      cond + 6 + words[cond + 2].view(np.int32),
                                      short + 2 + self.raw[short + 1].view(np.int8)]) + self.program.pe.base
            keep = np.isin(targets, np.fromiter(self.program.functions, dtype=np.int64))
            self._callers = defaultdict(list)
            for site, found in zip(sites[keep].tolist(), targets[keep].tolist()):
                self._callers[found].append(site)
        found = []
        for site in self._callers.get(target, ()):
            owner, start = self.instruction(site)
            if owner is None:
                raise ProofError("unmapped branch %08x to %08x" % (site, target))
            if start == site:
                found.append((owner, site))
        return found

    def table_of(self, slot):
        pe, functions = self.program.pe, self.program.functions
        at = slot
        while pe.rd32(at) in functions:
            if self.code_literal(at):
                return at
            at -= 4
        raise ProofError("slot %08x is in no table addressed from code" % slot)

    def extent(self, table):
        slots = vtable_slots(self.program, table) or ()
        for slot in slots[1:]:
            if self.code_literal(table + slot):
                return slots[:slots.index(slot)]
        return slots


class Bounds:
    def __init__(self, image, facts):
        self.image, self.facts = image, facts
        self.installs = defaultdict(list)
        self.untracked, self.untracked_in = {}, {}
        self.writes = defaultdict(lambda: defaultdict(set))
        for addr, found in facts.items():
            for site, node, offset, table in found.writes:
                self.writes[(addr, node)][offset].add(table)
                if table is not None:
                    self.installs[table].append((addr, site, node, offset))
                    if node in found.reloads:
                        self.untracked.setdefault(table, site)
                        self.untracked_in.setdefault(addr, site)
        self.installing = self._installing()
        self.edges, self.unlifted, self.linked = self._edges()
        self._objects, self._closures, self._incomplete = {}, {}, {}

    def _installing(self):
        own, calls = set(), defaultdict(set)
        for addr, found in self.facts.items():
            if found.entry is None:
                continue
            if any(node == found.entry and table is not None for _, node, _, table in found.writes):
                own.add(addr)
            for _, target, before, _ in found.calls:
                if any(node == found.entry for node, _ in before["ECX"]):
                    calls[addr].add(target)
        installing, grew = set(own), True
        while grew:
            grew = False
            for addr, targets in calls.items():
                if addr not in installing and targets & installing:
                    installing.add(addr)
                    grew = True
        return installing

    def _edges(self):
        edges, unlifted, linked = defaultdict(list), defaultdict(set), set()
        for addr, found in self.facts.items():
            for site, target, before, after in found.calls:
                callee = self.facts.get(target)
                if before["ECX"]:
                    linked.add((addr, site))
                if callee is None or callee.error is not None:
                    for node, _ in before["ECX"]:
                        unlifted[(addr, node)].add(target)
                    continue
                for name, sources in callee.exits.items():
                    for source, offset in sources:
                        for pre, at in before.get(source, ()):
                            for post, post_at in after.get(name, ()):
                                edges[(addr, post)].append(((addr, pre), post_at - at - offset, None))
                                edges[(addr, pre)].append(((addr, post), at + offset - post_at, None))
                if target in self.installing and callee.entry is not None:
                    entry = (target, callee.entry)
                    for node, at in before["ECX"]:
                        edges[(addr, node)].append((entry, at, False))
                        edges[entry].append(((addr, node), -at, True))
        return edges, unlifted, linked

    def dispatch_tables(self, method):
        tables = {}
        for at, code in self.image.literals(method):
            if code:
                raise ProofError("%08x is addressed from code at %08x" % (method, at))
            table = self.image.table_of(at)
            if at - table not in self.image.extent(table):
                raise ProofError("%08x slot %08x is outside table %08x" % (method, at, table))
            tables[table] = at - table
        return tables

    def objects(self, method, offset=0, progress=None):
        progress = {} if progress is None else progress
        if method in self._objects:
            found = self._objects[method]
            if isinstance(found, ProofError):
                raise found
            return found, False
        if method in progress:
            if progress[method] != offset:
                raise ProofError("%08x reaches itself at another offset" % method)
            return {}, True
        progress[method] = offset
        cyclic = False
        try:
            found = self.facts.get(method)
            if found is None or found.entry is None:
                raise ProofError("%08x has no entry this" % method)
            tables = self.dispatch_tables(method)
            callers = self.image.callers(method)
            if not tables and not callers:
                raise ProofError("%08x is in no table and has no direct callers" % method)
            positions = defaultdict(set)
            for node, pos in (self.closure(tables).items() if tables else ()):
                positions[node] |= pos
            for caller, site in callers:
                facts = self.facts.get(caller)
                views = next((before["ECX"] for at, _, before, _ in facts.calls if at == site), None) if facts else None
                if not views or any(node != facts.entry for node, _ in views):
                    raise ProofError("%08x calls %08x without passing its own this" % (site, method))
                for _, at in views:
                    outer, looped = self.objects(caller, offset + at, progress)
                    cyclic |= looped
                    for node, pos in outer.items():
                        positions[node] |= {p - at for p in pos}
                        positions[(caller, facts.entry)] |= {-at}
            for node, pos in self._walk([((method, found.entry), 0)]).items():
                positions[node] |= pos
            if any(len(pos) > MAX_POSITIONS for pos in positions.values()):
                raise ProofError("%08x objects take too many positions" % method)
        except ProofError as error:
            self._objects[method] = error
            raise
        finally:
            del progress[method]
        if not cyclic:
            self._objects[method] = dict(positions)
        return dict(positions), cyclic

    def seeds(self, table):
        if table in self.untracked:
            raise ProofError("table %08x is installed at %08x through a register reloaded after a call" % (
                table, self.untracked[table]))
        installs = self.installs.get(table, ())
        sites = {site for _, site, _, _ in installs}
        for at, code in self.image.literals(table):
            _, start = self.image.instruction(at) if code else (None, None)
            if start not in sites:
                raise ProofError("table %08x is used at %08x outside a recognized store" % (table, at))
        return [((addr, node), -offset) for addr, _, node, offset in installs]

    def closure(self, tables):
        key = frozenset(tables)
        if key not in self._closures:
            try:
                starts = [start for table in sorted(key) for start in self.seeds(table)]
                self._closures[key] = self._walk(starts)
            except ProofError as error:
                self._closures[key] = error
        found = self._closures[key]
        if isinstance(found, ProofError):
            raise found
        return found

    def _walk(self, starts):
        positions = defaultdict(set)
        seen = {(node, pos, True) for node, pos in starts}
        pending = list(seen)
        while pending:
            node, pos, climbing = pending.pop()
            positions[node].add(pos)
            if len(positions[node]) > MAX_POSITIONS:
                raise ProofError("%08x:%d takes too many positions" % node)
            if self.unlifted.get(node):
                raise ProofError("%08x passes the object to unlifted %s" % (
                    node[0], ",".join("%08x" % t for t in sorted(self.unlifted[node]))))
            if climbing and node[0] in self.installing and self.facts[node[0]].entry == node[1]:
                self._complete(node[0])
            for other, delta, up in self.edges.get(node, ()):
                if up and not climbing:
                    continue
                state = (other, pos + delta, climbing if up is None else up)
                if state not in seen:
                    seen.add(state)
                    pending.append(state)
        return positions

    def _complete(self, addr):
        if addr not in self._incomplete:
            self._incomplete[addr] = next(("%08x calls %08x without a linked this" % (site, addr)
                                           for caller, site in self.image.callers(addr)
                                           if (caller, site) not in self.linked), None)
        if self._incomplete[addr]:
            raise ProofError(self._incomplete[addr])

    def written(self, positions, field):
        tables = set()
        for node, found in positions.items():
            if node[0] in self.untracked_in:
                raise ProofError("%08x installs a table at %08x through a register reloaded after a call" % (
                    node[0], self.untracked_in[node[0]]))
            offsets = self.writes.get(node)
            if not offsets:
                continue
            for pos in found:
                for table in offsets.get(field - pos, ()):
                    if table is None:
                        raise ProofError("%08x writes a non-table value at the field" % node[0])
                    tables.add(table)
        return tables

    def method(self, method):
        found = self.facts[method]
        own = [site for site in found.sites if found.entry is not None
               and any(root == found.entry for root, _ in site[2])]
        if not found.sites:
            return None
        result = {"method": method, "sites": {}}
        for site, slot, _, _, kinds in found.sites:
            if all(site != row[0] for row in own):
                result["sites"][site] = {"slot": slot, "failure": "receiver is not this: " + ", ".join(sorted(kinds))}
        if not own:
            return result
        try:
            positions, _ = self.objects(method)
            dispatch = self.dispatch_tables(method)
            result["dispatch"] = dispatch
        except ProofError as error:
            result["failure"] = str(error)
            for site, slot, _, _, _ in own:
                result["sites"][site] = {"slot": slot, "failure": str(error)}
            return result
        for site, slot, fields, ecx, _ in own:
            row = result["sites"][site] = {"slot": slot, "fields": sorted(offset for _, offset in fields
                                                                          if offset is not None)}
            try:
                if len(fields) != 1 or ecx != fields:
                    raise ProofError("receiver is not the field whose table is read")
                field = next(iter(fields))[1]
                tables = self.written(positions, field)
                if field == 0 and dispatch and not self.written(self._walk([((method, found.entry), 0)]), 0):
                    tables = set(dispatch) if not self.image.callers(method) else tables
                tables = {table for table in tables if slot in self.image.extent(table)}
                if not tables:
                    raise ProofError("no table with slot %#x is written at this%+#x" % (slot, field))
                targets = {self.image.program.pe.rd32(table + slot) for table in tables}
                if not targets <= self.image.program.functions.keys():
                    raise ProofError("a slot target is not a function entry")
                row["tables"], row["targets"] = sorted(tables), sorted(targets)
            except ProofError as error:
                row["failure"] = str(error)
        return result


def main():
    import game_config
    from program import Program
    from settings import Settings
    parser = argparse.ArgumentParser(description="Bound vtable dispatch through methods' own fields")
    game_config.add_game_args(parser)
    parser.add_argument("--jobs", type=int, default=default_jobs())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--targets", type=Path, help="write proven sites as an indirect-targets file")
    parser.add_argument("--explain", type=lambda v: int(v, 16), action="append", default=[])
    parser.add_argument("--no-abi", action="store_true",
                        help="do not assume callees preserve EBX/ESI/EDI/EBP unless their contract kills it")
    args = parser.parse_args()
    game_config.resolve_game_args(args)
    settings = Settings(game_config.load(args.game_dir, args.build_root), None, args.jobs, None)
    started = time.monotonic()
    cache = Path(args.build_root) / "typeinf"
    key = source_key(settings)
    preserved = cached(cache / "contracts.pickle", key, lambda: contracts(settings, args.jobs))
    print("contracts %d (%.0fs)" % (len(preserved), time.monotonic() - started), flush=True)
    facts = cached(cache / "dispatch-facts.pickle", (key, not args.no_abi),
                   lambda: collect(settings, preserved, args.jobs, abi=not args.no_abi))
    print("facts %d (%.0fs)" % (len(facts), time.monotonic() - started), flush=True)
    bounds = Bounds(Image(Program(settings)), facts)
    print("indexes (%.0fs)" % (time.monotonic() - started), flush=True)
    methods = [result for addr in sorted(facts) if (result := bounds.method(addr)) is not None]
    sites = {site: row for result in methods for site, row in result["sites"].items()}
    proven = {site: row for site, row in sites.items() if "targets" in row}
    failures = defaultdict(int)
    for row in sites.values():
        if "failure" in row:
            failures[_reason(row["failure"])] += 1
    stats = {"functions": len(facts), "lift_errors": sum(1 for f in facts.values() if f.error),
             "installing": len(bounds.installing), "methods": len(methods), "sites": len(sites),
             "proven": len(proven), "monomorphic": sum(1 for row in proven.values() if len(row["targets"]) == 1),
             "failures": dict(sorted(failures.items(), key=lambda item: -item[1])),
             "seconds": round(time.monotonic() - started, 1)}
    args.out.write_text(json.dumps({"stats": stats, "methods": methods}, indent=1, default=_plain))
    if args.targets:
        lines = ["recomp-indirect-targets-v1\t%s" % settings.sha256.lower(), "site\tkind\ttargets"]
        lines += ["%08x\tcall\t%s" % (site, ",".join("%08x" % t for t in row["targets"]))
                  for site, row in sorted(proven.items())]
        args.targets.write_text("\n".join(lines) + "\n")
    for addr in args.explain:
        print(json.dumps(bounds.method(addr), indent=1, default=_plain))
    print(json.dumps(stats, indent=1))


def _reason(text):
    return " ".join(word for word in text.split() if not all(c in "0123456789abcdef:+-x," for c in word.lower()))


def _plain(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value))


if __name__ == "__main__":
    main()
