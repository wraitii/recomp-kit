"""Translate a game from its Ghidra code map: program model, SLEIGH lifting, SSA emission.

The map names every function, entry and jump table; nothing here discovers
code. Workers lift and emit one function at a time from the image bytes. A body
SSA cannot emit, and the wrappers of bodies whose entries are all switch cases,
come from the decoded emitter, fed the map's own instruction boundaries.
"""
import argparse
from collections import Counter, namedtuple
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import code_map
import game_config
import decoded
import output
from settings import Settings
from decoded import external_exits
from ir import call_contracts, production
from ir.lift import Lifter, LiftError
from ir.ssa import SSAError
from program import Program, TranslateError

Row = namedtuple("Row", "addr name count first last")

_CTX = None
_CLOCK = [time.monotonic()]


def phase(name):
    now = time.monotonic()
    print("  [phase] %-18s %6.1fs" % (name, now - _CLOCK[0]), file=sys.stderr, flush=True)
    _CLOCK[0] = now


class Context(object):
    """What one process needs to read, lift and emit functions."""

    def __init__(self, settings):
        self.settings = settings
        self.image = decoded.Image(settings.exe)
        self.program = Program(settings)
        program = self.program
        self.entries = program.entries()
        self.interior = program.interior_entries()
        self.tr = decoded.Translator(self.image, set(self.entries), settings.allow_unmodelled)
        self.tr.noreturn_callees = set(program.noreturn)
        self.tr.noreturn_sites = set(program.noreturn_calls)
        self.tr.jumptables = dict(program.tables)
        self.lifter = Lifter()
        self.known = frozenset(program.functions)

    def load(self, addr):
        """The decoded function, indexed, with the entries the map gives it."""
        image = self.image
        insns = []
        for start, size, lengths in self.program.functions[addr].spans:
            lengths = lengths or code_map.linear_lengths(self.lifter, image, start, size)
            if lengths is None:
                raise TranslateError("%08x: decoder cannot tile the mapped span at %08x" % (addr, start))
            insns.extend(code_map.decode_span(image, start, lengths))
        size = sum(span[1] for span in self.program.functions[addr].spans)
        fn = decoded.Function(addr, "FUN_%08x" % addr, size, insns)
        fn.measure(image)
        self.tr.prepare(fn)
        if addr not in fn.index:
            raise TranslateError("fn_%08x does not contain its own entry" % addr)
        entries = self.interior.get(addr, [])
        stale = [e for e in entries if e not in fn.index]
        if stale:
            raise TranslateError("%08x: entries %s are not instruction boundaries; fix Ghidra and re-export"
                                 % (addr, " ".join("%08x" % e for e in stale)))
        return fn, entries

    def dispatchable(self, target):
        return target in self.entries or decoded.GUEST_SHIM_BASE <= target < decoded.GUEST_SHIM_END


def _init(settings):
    global _CTX
    _CTX = Context(settings)


def _scan(addrs):
    """Per-function facts the whole-program steps need, with the SSA contract inputs."""
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set()
    rows = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        calls, refs, dangling, returns = [], set(), set(), []
        for i, ins in enumerate(fn.insns):
            if ins.mnem == "CALL":
                returns.append(fn.fallthrough[i] or (fn.insns[i + 1].addr if i + 1 < len(fn.insns) else fn.end))
            if ins.mnem == "CALL" or ins.mnem == "JMP" or ins.mnem in decoded.JCC:
                target = tr.branch_target(ins)
                if target is None:
                    continue
                if ins.mnem == "CALL":
                    calls.append(target)
                    if not ctx.dispatchable(target):
                        dangling.add(target)
                if target in ctx.entries and target not in ctx.known and ctx.program.containing(target) != addr:
                    refs.add(target)
        for targets in external_exits(tr, fn).values():
            dangling.update(t for t in targets if not ctx.dispatchable(t))
        reason = production.exclusion(fn)
        escapes = bool(tr.seh_escaping_returns(fn))
        site = bool(fn.seh_sites) or any(fn.insns[i].mnem == "POP" for i in fn.seh_restores)
        prepared = None
        if not (escapes or site or entries or fn.pushed_continuations or fn.return_jumps
                or fn.dead_addrs or addr in tr.noreturn_callees):
            try:
                prepared = call_contracts.prepare(decoded.function_ir(tr, ctx.lifter, fn))
            except (LiftError, RecursionError):
                prepared = None
            else:
                prepared = prepared or False
        rows.append((addr, Row(addr, fn.name, len(fn.insns), fn.insns[0].addr, fn.insns[-1].addr),
                     calls, refs, sorted(dangling), returns, reason, escapes, prepared))
    return rows


def _escapes(task):
    addrs, helpers = task
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    found = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        if tr.seh_escaping_returns(fn):
            found.append(addr)
    return found


def _emit(task):
    """Emit one chunk of functions: SSA where admitted, the decoded body otherwise."""
    addrs, helpers, internal, contracts = task
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    tr.internal_entries = internal
    out = []
    for addr in addrs:
        tr.unmodelled.clear()
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        lines, wrappers = None, False
        reason = production.exclusion(fn)
        if reason is None:
            try:
                lines, external = production.emit_ssa(
                    tr, ctx.lifter, fn, entries, ctx.settings.emit, ctx.known, contracts.get(addr))
            except (SSAError, LiftError) as error:
                reason = str(error)
            else:
                wrappers = bool(entries) and not external
        if wrappers:
            decoded = tr.translate(fn, entries)
            head = "void fn_%08x(X86 *c) {" % addr
            kept = [line for line in decoded if not line.startswith(head)]
            if len(kept) != len(decoded) - 1:
                reason, lines = "entry wrapper layout", None
            else:
                lines = kept + lines
        if lines is None:
            lines = tr.translate(fn, entries)
        out.append((addr, lines, re.sub(r"^[0-9a-f]{8}: ", "", reason) if reason else None,
                    list(tr.unmodelled)))
    return out


def chunked(addrs, count):
    addrs = sorted(addrs)
    return [part for part in (addrs[i::count] for i in range(count)) if part]


def jobs_for(override):
    if override is not None:
        return max(1, override)
    env = os.environ.get("RECOMP_SSA_JOBS")
    if env is not None:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(1, os.cpu_count() or 1)


def native_replaced(settings):
    """Addresses a native replacement header names; their call sites keep full publication."""
    header = settings.native_header
    named, seen = set(), set()
    stack = [(settings.game_dir / header).resolve()] if header else []
    while stack:
        path = stack.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        text = path.read_text(errors="replace")
        named.update(int(m, 16) for m in re.findall(r"FN_([0-9a-fA-F]{8})", text))
        stack.extend((path.parent / inc).resolve() for inc in re.findall(r'#\s*include\s+"([^"]+)"', text))
    return named


def seh_helpers(pool, parts, escapes, calls):
    """Fixpoint of functions that return with an established SEH frame."""
    helpers = {addr for addr, esc in escapes.items() if esc}
    while True:
        pending = sorted(addr for addr, targets in calls.items()
                         if addr not in helpers and helpers.intersection(targets))
        if not pending:
            return helpers
        found = set()
        for part in pool.map(_escapes, [(p, helpers) for p in chunked(pending, len(parts))]):
            found.update(part)
        if not found:
            return helpers
        helpers |= found


def symbol_rows(ctx, entry_names, rows):
    program = ctx.program
    kinds = program.entries()
    curated = ctx.settings.curated
    relocated = ctx.image.relocated_pointers()
    evidence = {}
    for name, addr in curated.get("alternates", {}).items():
        evidence.setdefault(addr, set()).add("curated")
    for name, addr in curated.get("entries", {}).items():
        evidence.setdefault(addr, set()).add("curated_entry")
    for addr in ctx.settings.alternate_entries:
        evidence.setdefault(addr, set()).add("curated")
    alt_owner = {}
    for owner, addrs in ctx.interior.items():
        for addr in addrs:
            alt_owner[addr] = owner
            if addr in relocated:
                evidence.setdefault(addr, set()).add("reloc")
    listed = {addr: rows[addr].name for addr in program.functions}
    provenance = {addr: kind for addr, kind in kinds.items() if addr not in program.functions}
    functions = []
    for i, addr in enumerate(entry_names):
        kind, hookable = output.hook_kind(addr, listed, alt_owner, provenance, evidence)
        if addr in listed:
            name = listed[addr]
        else:
            owner = alt_owner.get(addr)
            name = "%s.%s_%08x" % (listed.get(owner, "sub_%08x" % (owner or addr)),
                                   "alt" if kind == "alternate" else "blk", addr)
        functions.append({"addr": "%08x" % addr, "index": i, "name": name, "kind": kind,
                          "provenance": provenance.get(addr, "listed"),
                          "evidence": sorted(evidence.get(addr, ())),
                          "hookable": hookable, "aliases": []})
    by_addr = {int(f["addr"], 16): f for f in functions}
    for alias, addr in sorted({**curated.get("entries", {}), **curated.get("aliases", {})}.items()):
        if addr in by_addr:
            by_addr[addr]["aliases"].append(alias)
    return functions


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Translate a game from its Ghidra code map.")
    ap.add_argument("--out", required=True, help="where the generated sources go (build/recomp/gen)")
    ap.add_argument("--game", required=True, help="the directory holding game.toml")
    ap.add_argument("--report", default=None, help="write a JSON stats file")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--jobs", type=int, default=None, help="worker processes (default: all cores)")
    ap.add_argument("--allow-unmodelled", metavar="REASON", default=None,
                    help="Translate instructions this translator cannot model into a trap at "
                         "their own address instead of refusing the image; reaching one at run "
                         "time is still fatal.")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr, flush=True))
    settings = Settings(game_config.load(args.game), args.allow_unmodelled, jobs_for(args.jobs))
    ctx = Context(settings)
    settings, program = ctx.settings, ctx.program
    phase("load")
    addrs = sorted(program.functions)
    parts = chunked(addrs, settings.jobs * 24)
    with ProcessPoolExecutor(max_workers=settings.jobs, initializer=_init, initargs=(settings,)) as pool:
        scanned = {}
        for part in pool.map(_scan, parts):
            for row in part:
                scanned[row[0]] = row
        phase("scan")

        rows = {addr: row[1] for addr, row in scanned.items()}
        calls = {addr: row[2] for addr, row in scanned.items()}
        escapes = {addr: row[7] for addr, row in scanned.items()}
        helpers = seh_helpers(pool, parts, escapes, calls)
        phase("seh helpers")

        outside = set()
        for row in scanned.values():
            outside |= row[3]
        relocated = ctx.image.relocated_pointers()
        kinds = program.entries()
        internal = {}
        for (owner, _site), targets in program.tables.items():
            for t in targets:
                if (kinds.get(t) == "table" and program.containing(t) == owner and t != owner
                        and t not in program.configured_entries and t not in relocated
                        and t not in outside):
                    internal.setdefault(owner, set()).add(t)

        dangling = sorted((addr, t) for addr, row in scanned.items() for t in row[4])
        if dangling:
            for addr, t in dangling[:20]:
                log("  fn_%08x dispatches to %08x, which is not an entry point" % (addr, t))
            raise TranslateError(
                "%d literal dispatch targets are not entry points; add them in Ghidra "
                "(function or referenced entry), re-export the code map and regenerate" % len(dangling))

        conservative = native_replaced(settings)
        contracts = {}
        if settings.call_contracts:
            prepared = {addr: row[8] or None for addr, row in scanned.items()}
            roots = [addr for addr, row in scanned.items() if row[6] is None]
            contracts = call_contracts.analyze(
                calls, prepared,
                analyzable=lambda a: bool(scanned.get(a) and scanned[a][8] is not None
                                          and a not in helpers and a not in conservative),
                roots=roots)
        phase("contracts")

        tasks = []
        for part in parts:
            tasks.append((part, helpers, {a: internal[a] for a in part if a in internal},
                          {a: {t: contracts[t] for t in calls[a] if t in contracts}
                           for a in part if contracts}))
        results = {}
        for part in pool.map(_emit, tasks):
            for row in part:
                results[row[0]] = row
        phase("emit")

    bodies = {addr: results[addr][1] for addr in addrs}
    entries_by_fn = {addr: set(found) for addr, found in ctx.interior.items()}
    entry_names = sorted(ctx.entries)
    call_returns = sorted({r for row in scanned.values() for r in row[5]})
    functions = [rows[a] for a in addrs]
    os.makedirs(args.out, exist_ok=True)
    output.write_funcs_header(args.out, entry_names)
    output.emit_body_chunks(args.out, functions, bodies, entries_by_fn)
    output.emit_entry_chunks(args.out, entry_names)
    symbols = symbol_rows(ctx, entry_names, rows)
    output.write_table(args.out, entry_names, symbols, call_returns)
    output.write_symbols(
        args.out, str(settings.exe), ctx.image.base, symbols, settings.curated)
    phase("write")

    emitted = sum(1 for a in addrs if results[a][2] is None)
    reasons = Counter(results[a][2] for a in addrs if results[a][2] is not None)
    unmodelled = [item for a in addrs for item in results[a][3]]
    kinds_count = Counter(f["kind"] for f in symbols)
    log("  %d functions, %d entries (%s), %d emitted by SSA (%.2f%%), %d decoded"
        % (len(addrs), len(entry_names), ", ".join("%d %s" % (n, k) for k, n in sorted(kinds_count.items())),
           emitted, 100.0 * emitted / len(addrs), len(addrs) - emitted))
    if unmodelled:
        log("%d instructions could not be modelled and trap if reached:" % len(unmodelled))
        for a, why in unmodelled[:20]:
            log("  %08x  %s" % (a, why))
    if args.report:
        with open(args.report, "w") as fh:
            json.dump({
                "functions": len(addrs),
                "entry_points": len(entry_names),
                "entry_kinds": dict(kinds_count),
                "ssa_emitted": emitted,
                "decoded": len(addrs) - emitted,
                "decoded_reasons": dict(reasons.most_common()),
                "unmodelled": [["%08x" % a, w] for a, w in unmodelled],
                "seconds": time.monotonic() - started,
            }, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
