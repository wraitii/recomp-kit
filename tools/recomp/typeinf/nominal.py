"""Indirect-call bounds from nominal writers and receiver/interface structure."""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

KIT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(KIT / "tools"), str(KIT / "tools" / "recomp")]

from .facts import cached, collect, contracts, default_jobs, scoped, source_key  # noqa: E402
from .dispatch import Bounds, Image, ProofError  # noqa: E402
from .globals import FRESH_IMPORTS, census  # noqa: E402
from .observed import ObservedEdges, load_observed  # noqa: E402
from .structure import Structure  # noqa: E402
from .alias import Aliases  # noqa: E402


class Layer:
    def __init__(self, facts, report, image, allocators=(), known_sites=None):
        self.facts = facts
        self.image = image
        self.known_sites = known_sites or {}
        self.base = {row["addr"]: row for row in report}
        self.allocators = _allocators(facts, allocators)
        self.node_tables = {}
        self.table_offsets = {}
        self.origins = defaultdict(lambda: {"tables": defaultdict(set)})
        self.globals = {addr: set() for addr in self.base}
        argument_sites = {(addr, write["site"]) for addr, row in self.base.items() for write in row["writes"]
                          if write["value"][0] in ("arg", "arg_modified")}
        self.open = {addr: {reason for reason in row["reasons"] if not _argument_reason(reason, addr, argument_sites)}
                     for addr, row in self.base.items()}
        self.values = {}
        self.dependents = defaultdict(set)
        self.write_nodes = defaultdict(list)
        self.callers = defaultdict(set)
        self.unresolved_slots = defaultdict(set)
        self.site_results = {}
        self.field_values = defaultdict(set)
        for addr, row in self.base.items():
            for write in row["writes"]:
                if write["value"][0] != "null":
                    self.write_nodes[addr].append((write["site"], (write["writer"], write["value"])))
        self.site_owner = {site[0]: caller for caller, found in self.facts.items() for site in found.sites}
        for caller, found in self.facts.items():
            for site, target, _, _ in found.calls:
                self.callers[target].add((caller, site))
            for write in found.field_writes:
                self.field_values[(write.base, write.offset)].add((caller, write.value))
        for site, targets in self.known_sites.items():
            caller = self.site_owner.get(site)
            if caller is not None:
                for target in targets:
                    self.callers[target].add((caller, site))

    def run(self):
        while True:
            self._seed()
            self._run_values()
            self.changed = False
            self._read_tables()
            edges = self._resolve_sites()
            if not edges and not self.changed:
                break
            self.values.clear()
        self._open_read_chains()
        self._potential_arguments()

    @staticmethod
    def _global_key(addr):
        return ("global", addr)

    def _seed(self):
        self.pending = {write[1] for writes in self.write_nodes.values() for write in writes}
        self.pending.update(self._global_key(addr) for addr in self.write_nodes)
        for caller, found in self.facts.items():
            for site in found.sites:
                self.pending.update((caller, identity) for identity in site[6])

    def _run_values(self):
        while self.pending:
            key = self.pending.pop()
            old = self.values.get(key)
            new = self._compute(key)
            if new == old:
                continue
            self.values[key] = new
            self.pending.update(self.dependents.get(key, ()))

    def _depend(self, source, key):
        if source != key:
            self.dependents[source].add(key)
            if source not in self.values:
                self.pending.add(source)

    def _reaches(self, start, target):
        pending, seen = [start], set()
        while pending:
            key = pending.pop()
            if key == target:
                return True
            if key in seen:
                continue
            seen.add(key)
            pending.extend(self.dependents.get(key, ()))
        return False

    def _compute(self, key):
        if key[0] == "global":
            return self._global_value(key[1])
        return self._value(*key)

    def _global_value(self, addr):
        key, found, reasons = self._global_key(addr), set(), set()
        for site, write in self.write_nodes.get(addr, ()):
            self._depend(write, key)
            write_found, write_reasons = self.values.get(write, (frozenset(), frozenset()))
            found |= write_found
            reasons |= {"write at 0x%08x: %s" % (site, reason) for reason in write_reasons}
        self.globals[addr] |= found
        self.open[addr] |= reasons
        return frozenset(found), frozenset(reasons)

    def _value(self, addr, value):
        key = (addr, value)
        kind = value[0]
        if kind == "null":
            return frozenset(), frozenset()
        if kind == "import":
            return ((frozenset({(value[1], 0)}), frozenset()) if _fresh_import(value[2])
                    else (frozenset(), frozenset({_describe(value)})))
        if kind == "global":
            source = self._global_key(value[1])
            self._depend(source, key)
            reasons = frozenset({"depends on open global 0x%08x" % value[1]}) if self.open.get(value[1]) else frozenset()
            return frozenset(self.globals.get(value[1], ())), reasons
        if kind == "reg":
            return self._linked(addr, key, lambda found: found.call_values)
        if kind == "arg":
            return self._linked(addr, key, lambda found: found.call_arguments, value[1])
        if kind == "phi":
            found, reasons = set(), set()
            for item in value[1]:
                source = (addr, item)
                self._depend(source, key)
                nested, nested_reasons = self.values.get(source, (frozenset(), frozenset()))
                found |= nested
                reasons |= nested_reasons
            return frozenset(found), frozenset(reasons)
        if kind == "field":
            source = (addr, value[1])
            self._depend(source, key)
            base, base_reasons = self.values.get(source, (frozenset(), frozenset()))
            found, reasons = set(), set(base_reasons)
            if base and not base_reasons:
                sources = self.field_values.get((value[1], value[2]), ())
                if not sources:
                    reasons.add("no value is written to field [%s+%#x]" % (_describe(value[1]), value[2]))
                for writer, stored in sources:
                    source_key = (writer, stored)
                    self._depend(source_key, key)
                    nested, nested_reasons = self.values.get(source_key, (frozenset(), frozenset()))
                    found |= nested
                    reasons |= nested_reasons
            elif not base and not base_reasons:
                reasons.add("field base has no nominal type")
            return frozenset(found), frozenset(reasons)
        if kind == "offset":
            if value[1][0] not in ("call", "global", "offset", "import", "reg", "arg", "field"):
                return frozenset(), frozenset({_describe(value)})
            source = (addr, value[1])
            if self._reaches(key, source):
                return frozenset(), frozenset({_describe(value) + " cycles through an offset"})
            self._depend(source, key)
            found, reasons = self.values.get(source, (frozenset(), frozenset()))
            return frozenset((origin, offset + value[2]) for origin, offset in found), reasons
        if kind == "call":
            return self._call_value(addr, value, key)
        return frozenset(), frozenset({_describe(value)})

    def _linked(self, addr, key, summary, argument=None):
        found, reasons = set(), set()
        callers = self.callers.get(addr, ())
        for caller, site in callers:
            facts = self.facts.get(caller)
            if facts is None:
                continue
            values = summary(facts).get(site, {})
            sources = values.get(argument if argument is not None else key[1][1], ())
            if not sources:
                if argument is None:
                    reasons.add("call site 0x%08x has no %s input summary" % (site, key[1][1]))
                else:
                    reasons.add("call site 0x%08x has no argument %d summary" % (site, argument))
            for source in sources:
                source_key = (caller, source)
                self._depend(source_key, key)
                nested, nested_reasons = self.values.get(source_key, (frozenset(), frozenset()))
                found |= nested
                reasons |= nested_reasons
        return frozenset(found), frozenset(reasons)

    def _call_value(self, addr, value, key):
        _, site, target, register = value
        targets = ({target} if target is not None else (
            self.site_results.get((addr, site), (frozenset(),))[0] | self.known_sites.get(site, frozenset())))
        if not targets:
            return frozenset(), frozenset({"indirect call at 0x%08x has no typed target" % site})
        found, reasons = set(), set()
        caller = self.facts.get(addr)
        for target in sorted(targets):
            if target in self.allocators:
                found.add((site, 0))
                continue
            callee = self.facts.get(target)
            if callee is None:
                reasons.add("callee 0x%08x has no facts" % target)
                continue
            results = callee.results.get(register)
            if not results:
                reasons.add("callee 0x%08x has no %s result summary" % (target, register))
                continue
            for result in results:
                if result[0] == "reg":
                    inputs = (caller.call_values.get(site, {}) if caller else {}).get(result[1], ())
                    if not inputs:
                        reasons.add("call site 0x%08x has no %s input summary" % (site, result[1]))
                    for source in inputs:
                        source_key = (addr, source)
                        self._depend(source_key, key)
                        nested, nested_reasons = self.values.get(source_key, (frozenset(), frozenset()))
                        if result[1] == "ECX":
                            tables = self._tables(target)
                            for origin, offset in nested:
                                self._apply(origin, offset, tables)
                        found |= nested
                        reasons |= nested_reasons
                elif result[0] == "global":
                    source = self._global_key(result[1])
                    self._depend(source, key)
                    found |= self.globals.get(result[1], ())
                    if self.open.get(result[1]):
                        reasons.add("depends on open global 0x%08x" % result[1])
                elif result[0] == "null":
                    continue
                else:
                    reasons.add("callee 0x%08x returns unlinked %s" % (target, _describe(result)))
        return frozenset(found), frozenset(reasons)

    def _read_tables(self):
        for reader, found in self.facts.items():
            for read in found.global_reads:
                if read.addr not in self.globals:
                    continue
                tables = self._node_tables(reader, read.node)
                for origin, offset in self.globals[read.addr]:
                    self._apply(origin, offset, tables)

    def _resolve_sites(self):
        edges = False
        self.unresolved_slots = defaultdict(set)
        for caller, found in self.facts.items():
            for site, slot, fields, ecx, kinds, global_addr, receiver in found.sites:
                types, reasons = set(), set()
                for identity in receiver:
                    key = (caller, identity)
                    nested, nested_reasons = self.values.get(key, (frozenset(), frozenset()))
                    types |= nested
                    reasons |= nested_reasons
                offsets = {at for node, at in fields if node is not None and at is not None}
                targets = set()
                if types and offsets:
                    for origin, offset in sorted(types):
                        for at in sorted(offsets):
                            tables = self.origins[origin]["tables"].get(offset + at)
                            if not tables:
                                reasons.add("no table at type offset %#x" % (offset + at))
                                continue
                            for table in sorted(tables):
                                if slot not in self.image.extent(table):
                                    reasons.add("slot %#x outside table 0x%08x" % (slot, table))
                                    continue
                                target = self.image.program.pe.rd32(table + slot)
                                callee = self.facts.get(target)
                                if target not in self.image.program.functions or callee is None or callee.error is not None:
                                    reasons.add("target 0x%08x is unknown or unlifted" % (target or 0))
                                    continue
                                targets.add(target)
                                if callee.entry is not None:
                                    self._apply(origin, offset, self._node_tables(target, callee.entry))
                elif types:
                    reasons.add("receiver field form is not exact")
                targets |= self.known_sites.get(site, frozenset())
                if reasons and site not in self.known_sites:
                    self.unresolved_slots[slot].add((caller, site))
                    for identity in receiver:
                        if identity[0] == "global":
                            for reason in reasons:
                                self.open[identity[1]].add(
                                    "read at 0x%08x passed as this to indirect call 0x%08x: %s" % (
                                        next((read.site for read in found.global_reads
                                              if read.node in {node for node, _ in ecx}), site), site, reason))
                self.site_results[(caller, site)] = (frozenset(targets), frozenset(reasons), frozenset(types))
                for target in targets:
                    edge = (caller, site)
                    if edge not in self.callers[target]:
                        self.callers[target].add(edge)
                        edges = True
        return edges

    def _open_read_chains(self):
        for reader, found in self.facts.items():
            for read in found.global_reads:
                if read.addr not in self.open:
                    continue
                for site, target, before, _ in found.calls:
                    callee = self.facts.get(target)
                    if (callee is None or callee.error is not None) and any(
                            node == read.node for node, _ in before.get("ECX", ())):
                        self.open[read.addr].add(
                            "read at 0x%08x passed as this to unlifted callee 0x%08x" % (read.site, target))

    def _potential_arguments(self):
        for addr, row in self.base.items():
            for write in row["writes"]:
                value = write["value"]
                if value[0] == "arg_modified":
                    self.open[addr].add("write at 0x%08x takes argument %d after a store to its slot" % (
                        write["site"], value[1]))
                if value[0] != "arg":
                    continue
                hits = [(table, offset) for table, offset in self._table_offsets(write["writer"])
                        if self.unresolved_slots.get(offset)]
                if hits:
                    sites = sorted({site for table, offset in hits for _, site in self.unresolved_slots[offset]})
                    self.open[addr].add("write at 0x%08x takes argument %d through unresolved slot%s at %s" % (
                        write["site"], value[1], "" if len(hits) == 1 else "s",
                        ", ".join("0x%08x+%#x" % hit for hit in hits)))

    def _table_offsets(self, addr):
        if addr not in self.table_offsets:
            found = set()
            for at, code in self.image.literals(addr):
                if code:
                    continue
                try:
                    table = self.image.table_of(at)
                except ProofError:
                    continue
                if at - table in self.image.extent(table):
                    found.add((table, at - table))
            self.table_offsets[addr] = found
        return self.table_offsets[addr]

    def _apply(self, origin, offset, tables):
        if tables:
            for at, values in tables.items():
                stored = self.origins[origin]["tables"][offset + at]
                if not values <= stored:
                    stored |= values
                    self.changed = True

    def _tables(self, target):
        found = self.facts.get(target)
        return self._node_tables(target, found.entry) if found is not None and found.entry is not None else {}

    def _node_tables(self, addr, node):
        key = (addr, node)
        if key not in self.node_tables:
            tables, work, seen = defaultdict(set), [(addr, node, 0)], set()
            while work:
                function, base, offset = work.pop()
                if (function, base, offset) in seen:
                    continue
                seen.add((function, base, offset))
                found = self.facts.get(function)
                if found is None or found.entry is None:
                    continue
                for _, written, at, table in found.writes:
                    if table is not None and written == base:
                        tables[offset + at].add(table)
                for _, target, before, _ in found.calls:
                    callee = self.facts.get(target)
                    if callee is None or callee.entry is None:
                        continue
                    for root, at in before.get("ECX", ()):
                        if root == base:
                            work.append((target, callee.entry, offset + at))
            self.node_tables[key] = dict(tables)
        return self.node_tables[key]

    def result(self, addr):
        row = self.base[addr]
        by_origin = defaultdict(set)
        for origin, offset in self.globals[addr]:
            by_origin[origin].add(offset)
        return {
            "global": "0x%08x" % addr,
            "types": [{"origin": "0x%08x" % origin,
                       "offsets": ["0x%x" % offset for offset in sorted(by_origin[origin])],
                       "tables": [{"offset": "0x%x" % at, "values": ["0x%08x" % table for table in sorted(values)]}
                                  for at, values in sorted(self.origins[origin]["tables"].items())]}
                      for origin in sorted(by_origin)],
            "null_faults": ["0x%08x" % write["site"] for write in row["writes"] if write["value"][0] == "null"],
            "open": bool(self.open[addr]),
            "reasons": sorted(self.open[addr]),
            "table_evidence": "tables from known this-chains",
        }


def _convention(facts, caller, site, target, functions):
    callee = facts.get(target)
    if callee is None or target not in functions or callee.error is not None:
        return "unknown"
    if callee.pops is None or callee.pops % 4:
        return "unknown"
    caller_facts = facts.get(caller)
    if caller_facts is None:
        return "unknown"
    arguments = caller_facts.call_arguments.get(site, {})
    if any(k not in arguments for k in range(callee.pops // 4)):
        return "unknown"
    if callee.entry is not None and not caller_facts.call_values.get(site, {}).get("ECX"):
        return "unknown"
    return "ok"


def _nominal_targets(layer, facts, functions):
    found = {}
    for (caller, site), (targets, reasons, types) in layer.site_results.items():
        if not types or reasons or not targets:
            continue
        accepted, unknown = set(), False
        for target in targets:
            state = _convention(facts, caller, site, target, functions)
            if state == "unknown":
                unknown = True
                break
            accepted.add(target)
        if unknown or not accepted:
            continue
        found[site] = accepted
    return found


def _argument_reason(reason, addr, argument_sites):
    if not reason.startswith("write at 0x") or "argument" not in reason:
        return False
    try:
        site = int(reason.split()[2].rstrip(":"), 16)
    except ValueError:
        return False
    return (addr, site) in argument_sites


def _allocators(facts, declared=()):
    def state(value, found):
        kind = value[0]
        if kind == "null":
            return "null"
        if kind == "offset":
            return state(value[1], found)
        if kind == "import":
            return "fresh" if _fresh_import(value[2]) else None
        if kind == "call" and value[2] in found:
            return "fresh"
        return None

    found = set(declared)
    while True:
        grew = False
        for addr, found_facts in facts.items():
            exits = found_facts.results.get("EAX")
            if addr in found or not exits:
                continue
            states = [state(value, found) for value in exits]
            tainted = {_root_identity(value) for value in exits} & found_facts.tainted
            if not tainted and all(state is not None for state in states) and "fresh" in states:
                found.add(addr)
                grew = True
        if not grew:
            return found


def _root_identity(value):
    while value[0] == "offset":
        value = value[1]
    return value


def _fresh_import(name):
    return name.rsplit("!", 1)[-1] in FRESH_IMPORTS


def _describe(value):
    kind = value[0]
    if kind == "import":
        return "import %s (%s)" % (value[2], value[3])
    if kind == "call":
        return "call 0x%08x -> 0x%08x (%s)" % (value[1], value[2] or 0, value[3])
    if kind == "reg":
        return "entry %s" % value[1]
    if kind == "offset":
        return "offset of %s%+#x" % (_describe(value[1]), value[2])
    if kind == "global":
        return "global 0x%08x" % value[1]
    if kind == "field":
        return "field [%s+%#x]" % (_describe(value[1]), value[2])
    if kind == "phi":
        return "phi(%s)" % ", ".join(_describe(item) for item in value[1])
    if kind == "stack":
        return "stack %#x" % value[1]
    if kind == "const":
        return "constant 0x%08x" % value[1]
    if kind in ("arg", "arg_modified"):
        return "argument %d%s" % (value[1], " (modified)" if kind == "arg_modified" else "")
    return "unknown %s" % (value[1] if len(value) > 1 else value[0])


def _outside_scope(layer, image, scope):
    for addr in layer.base:
        for at, code in image.literals(addr):
            if not code:
                continue
            owner = image.program.containing(at)
            if owner not in scope:
                layer.open[addr].add("outside scope: literal at 0x%08x in 0x%08x" % (at, owner or 0))


def main():
    import game_config
    from program import Program
    from settings import Settings
    parser = argparse.ArgumentParser(description="Infer indirect-call bounds from receiver structure and nominal writers")
    game_config.add_game_args(parser)
    parser.add_argument("--jobs", type=int, default=default_jobs())
    parser.add_argument("--out", type=Path)
    parser.add_argument("--explain", type=lambda value: int(value, 16), action="append", default=[])
    parser.add_argument("--explain-site", type=lambda value: int(value, 16), action="append", default=[])
    parser.add_argument("--explain-function", type=lambda value: int(value, 16), action="append", default=[],
                        help="show receiver identities and alias barriers")
    parser.add_argument("--structure-only", action="store_true", help="skip global census and nominal writer inference")
    parser.add_argument("--targets", type=Path, help="write structural and nominal targets merged with proven dispatch sites")
    parser.add_argument("--basis", type=Path, help="write each target row's basis")
    parser.add_argument("--observe", type=Path, help="observed indirect-call edges")
    parser.add_argument("--scope", type=lambda value: int(value, 16), action="append", default=[],
                        help="compute facts only for the closure of these functions or addresses")
    parser.add_argument("--scope-depth", type=int, default=2)
    parser.add_argument("--escape-window", type=lambda value: int(value, 0), default=0x1000)
    parser.add_argument("--no-abi", action="store_true")
    args = parser.parse_args()
    game_config.resolve_game_args(args)
    settings = Settings(game_config.load(args.game_dir, args.build_root), jobs=args.jobs)
    started = time.monotonic()
    cache = Path(args.build_root) / "typeinf"
    key = source_key(settings)
    preserved = {}
    if args.scope:
        facts, scope, observed = scoped(settings, args.scope, args.scope_depth, args.observe, args.jobs,
                                        abi=not args.no_abi)
    else:
        preserved = cached(cache / "contracts.pickle", key, lambda: contracts(settings, args.jobs))
        facts = cached(cache / "dispatch-facts.pickle", (key, not args.no_abi),
                       lambda: collect(settings, preserved, args.jobs, abi=not args.no_abi))
        scope = None
        observed = None
    facts_done = time.monotonic()
    print("facts %d (%.0fs)" % (len(facts), facts_done - started), flush=True)
    program = Program(settings)
    image = Image(program)
    if observed is None:
        observed = load_observed(args.observe, program) if args.observe else ObservedEdges()
    image_done = time.monotonic()
    report = [] if args.structure_only else census(image, facts, args.escape_window)
    census_done = time.monotonic()
    bounds = Bounds(image, facts)
    known = {}
    for addr in sorted(facts):
        result = bounds.method(addr)
        if result is None:
            continue
        for site, row in result["sites"].items():
            if "targets" in row:
                known[site] = frozenset(row["targets"])
    layer = Layer(facts, report, image, settings.allocators, known)
    if not args.structure_only:
        layer.run()
    if scope is not None:
        _outside_scope(layer, image, scope)
    fixpoint_done = time.monotonic()
    from ir.indirect_targets import instruction_kind, load as load_targets
    aliases = Aliases(program, preserved, load_targets(settings, program), abi=not args.no_abi, settings=settings)
    structure = Structure(facts, image, observed, aliases)
    print("image %.1fs, census %.1fs, fixpoint %.1fs" % (
        image_done - facts_done, census_done - image_done, fixpoint_done - census_done), flush=True)
    rows = [layer.result(row["addr"]) for row in report]
    structural = {}
    for addr in sorted(facts):
        found = facts[addr]
        for site in found.sites:
            structural[site[0]] = structure.bound(addr, site)
    structural_found = {site: row for site, row in structural.items()
                        if row.get("open") is None and row.get("targets")}
    emitted = structural_found if scope is None else {}
    if args.targets:
        merged = {site: set(targets) for site, targets in known.items()}
        dispatch = set(merged)
        nominal = _nominal_targets(layer, facts, image.program.functions)
        for site, targets in nominal.items():
            merged.setdefault(site, set()).update(targets)
        added = set()
        for site, row in emitted.items():
            if site in merged:
                continue
            merged[site] = set(row["targets"])
            added.add(site)
        lines = ["recomp-indirect-targets-v1\t%s" % settings.sha256.lower(), "site\tkind\ttargets"]
        lines += ["%08x\t%s\t%s" % (site, instruction_kind(program.pe, site),
                                   ",".join("%08x" % target for target in sorted(targets)))
                  for site, targets in sorted(merged.items())]
        args.targets.write_text("\n".join(lines) + "\n")
        print("targets %d (dispatch %d, nominal %d, structure %d, added %d)" % (
            len(merged), len(dispatch), len(nominal), len(added), len(set(merged) - dispatch)), flush=True)
    if args.basis:
        lines = ["site\tbasis\ttargets"]
        nominal = _nominal_targets(layer, facts, image.program.functions)
        for site in sorted(set(known) | set(nominal) | set(structural_found)):
            if site in known:
                basis, targets = "proof", known[site]
            elif site in nominal:
                basis, targets = "nominal", nominal[site]
            else:
                basis, targets = structural_found[site]["basis"], structural_found[site]["targets"]
                if scope is not None:
                    basis += " (scoped, not emitted)"
            lines.append("%08x\t%s\t%s" % (site, basis, ",".join("%08x" % t for t in sorted(targets))))
        args.basis.write_text("\n".join(lines) + "\n")
    distribution = Counter(len(row["targets"]) for row in structural_found.values())
    pure = sum(1 for row in structural_found.values() if row["basis"] == "structure")
    observed_rows = sum(1 for row in structural_found.values() if row["basis"] == "structure+observed")
    print("structure %d (pure %d, observed %d, open %d, sizes %s)" % (
        len(structural_found), pure, observed_rows, len(structural) - len(structural_found),
        dict(sorted(distribution.items()))), flush=True)
    origins = {origin for found in layer.globals.values() for origin, _ in found}
    all_sites = [(caller, site) for caller, found in facts.items() for site in found.sites]
    def typed_sites(found):
        return [(caller, site) for caller, site in found if layer.site_results.get((caller, site[0]), ((), (), ()))[2]]
    def closed_sites(found):
        return [(caller, site) for caller, site in found
                if layer.site_results.get((caller, site[0]), ((), frozenset(), ()))[2]
                and not layer.site_results[(caller, site[0])][1]]
    stats = {
        "globals": len(rows),
        "nonempty": sum(1 for row in rows if row["types"]),
        "closed_nonempty": sum(1 for row in rows if row["types"] and not row["open"]),
        "types": len(origins),
        "allocators": len(layer.allocators),
        "allocator_functions": ["0x%08x" % addr for addr in sorted(layer.allocators)],
        "indirect_sites": len(all_sites),
        "typed_sites": len(typed_sites(all_sites)),
        "closed_receiver_sites": len(closed_sites(all_sites)),
        "structural_sites": len(structural_found),
        "structural_pure": sum(1 for row in structural_found.values() if row["basis"] == "structure"),
        "structural_observed": sum(1 for row in structural_found.values() if row["basis"] == "structure+observed"),
        "structural_open": len(structural) - len(structural_found),
        "structural_sizes": dict(sorted(Counter(len(row["targets"]) for row in structural_found.values()).items())),
        "structural_returning_sizes": dict(sorted(Counter(len(row["returning_targets"])
                                                          for row in structural_found.values()).items())),
        "structural_single_returning": sum(len(row["returning_targets"]) == 1
                                           for row in structural_found.values()),
        "structural_shared_receiver": sum(row["shared_receiver"] for row in structural_found.values()),
        "receiver_alias_functions": len(aliases.results),
        "receiver_alias_types": sum(len(row["uses"]) > 1 for result in aliases.results.values()
                                    for row in result["objects"]),
        "structure_only": args.structure_only,
        "structural_soundness_violations": ["%08x" % site for site, row in structural.items() if row.get("violations")],
        "facts_seconds": round(facts_done - started, 1),
        "image_seconds": round(image_done - facts_done, 1),
        "census_seconds": round(census_done - image_done, 1),
        "fixpoint_seconds": round(fixpoint_done - census_done, 1),
        "seconds": round(time.monotonic() - started, 1),
    }
    receivers = {}
    for row in structural_found.values():
        receiver = row.get("receiver_type")
        if receiver is not None:
            key = (*receiver["nominal"], receiver["vptr"])
            receivers.setdefault(key, {name: value for name, value in receiver.items() if name not in ("at", "methods")})
            receivers[key].setdefault("bounds", []).append({"site": receiver["at"], "methods": receiver["methods"]})
    output = {"stats": stats, "globals": rows, "receiver_types": list(receivers.values()),
              "receiver_identity": {"%08x" % addr: aliases.function(addr) for addr in args.explain_function}}
    if args.out:
        args.out.write_text(json.dumps(output, indent=1))
    for addr in args.explain:
        row = next((row for row in rows if row["global"] == "0x%08x" % addr), None)
        print(json.dumps(row, indent=1) if row else "no global 0x%08x" % addr)
    for site in args.explain_site:
        row = structural.get(site)
        print(json.dumps(row, indent=1) if row else "no indirect site 0x%08x" % site)
    for addr in args.explain_function:
        print(json.dumps(aliases.function(addr), indent=1))
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
