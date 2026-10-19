"""Whole-program scan: lifted per-function facts, SEH helpers and entry model."""
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import pickle
from pathlib import Path
import re

import decoded
from frontend import external_exits
from ir import call_contracts, call_sites, production, x87_facts
from ir.x87_cw import writer_effect
from ir.x87_propagation import prepare as prepare_x87
from ir.lift import LiftError
from phases import ScanTask, EscapeTask
from program import FunctionDescription as Row, EntryModel

ScanRow = namedtuple("ScanRow", "addr description calls jumps references returns exclusion seh_escapes register_facts cw_facts x87_count writers external_targets stack_pops")
CACHE_VERSION = "recomp-scan-v4"
_WORKER = {}


def _init(settings, artifacts=None):
    import driver
    import emission_cache
    _WORKER["ctx"] = driver.Context(settings, artifacts)
    _WORKER['cache'] = (emission_cache.AnalysisCache(*settings.preparation_cache, readonly=True)
                        if getattr(settings, 'preparation_cache', None) else None)


def _scan(task):
    """Per-function facts the whole-program steps need, with the SSA contract inputs."""
    ctx, tr = _WORKER["ctx"], _WORKER["ctx"].tr
    tr.seh_helpers = set()
    rows = []
    for addr in task.addresses:
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        cache, key = _WORKER['cache'], None
        if cache is not None:
            targets = {target for ins in fn.insns
                       if ins.mnem in ('CALL', 'JMP') or ins.mnem in decoded.JCC
                       if (target := tr.branch_target(ins)) is not None}
            bindings = [(target, target in ctx.entries, target in ctx.known, ctx.program.containing(target))
                        for target in sorted(targets)]
            try:
                key = cache.key(None, ('scan', ctx.artifacts.input_key(tr, ctx.lifter, fn), entries,
                                      bindings, addr in tr.noreturn_callees, ctx.decoded_only))
            except (LiftError, RecursionError):
                pass
            if key is not None:
                cached = cache.get(key, 'scan')
                if cached is not None:
                    rows.append((key, cached, True))
                    continue
        calls, jumps, refs, returns = [], set(), set(), []
        for i, ins in enumerate(fn.insns):
            if ins.mnem == "CALL":
                returns.append(fn.fallthrough[i] or (fn.insns[i + 1].addr if i + 1 < len(fn.insns) else fn.end))
            if ins.mnem == "CALL" or ins.mnem == "JMP" or ins.mnem in decoded.JCC:
                target = tr.branch_target(ins)
                if target is None:
                    continue
                if ins.mnem == "CALL":
                    calls.append(target)
                else:
                    jumps.add(target)
                if target in ctx.entries and target not in ctx.known and ctx.program.containing(target) != addr:
                    refs.add(target)
        external_targets = set()
        for targets in external_exits(tr, fn).values():
            external_targets.update(targets)
        reason = "decoded-only code map" if ctx.decoded_only else production.exclusion(fn)
        escapes = bool(tr.seh_escaping_returns(fn))
        site = (bool(fn.seh_sites) or any(fn.insns[i].mnem == "POP" for i in fn.seh_restores)
                or tr.image.msvc_seh_epilog(addr) or any(map(tr.seh_except_entry, fn.insns)))
        prepared, mode_prepared = None, None
        if not (escapes or fn.pushed_continuations or fn.return_jumps or addr in tr.noreturn_callees):
            try:
                fir = ctx.artifacts.lifted(tr, ctx.lifter, fn)
                fir.entries = tuple(entries)
                sites = call_sites.build(fir, ctx.image)
                prepared = (call_contracts.prepare(fir, image=ctx.image, sites=sites, frontend=key)
                            if not (entries or site or fn.dead_addrs) else None)
                mode_prepared = prepare_x87(fir, ctx.image, sites=sites)
            except (LiftError, RecursionError):
                prepared = None
            else:
                prepared = None if site or fn.dead_addrs else prepared or False
        x87_count, writers = 0, []
        for ins in fn.insns:
            mnem = ins.mnem.upper().removeprefix("WAIT ")
            x87_count += mnem.startswith("F") and mnem != "FEMMS"
            if writer_effect(mnem) is not None:
                writers.append({"site": ins.addr, "mnemonic": mnem})
        row = ScanRow(addr, Row(addr, fn.name, len(fn.insns), fn.insns[0].addr, fn.insns[-1].addr),
                     calls, sorted(jumps), refs, returns, reason, escapes, prepared, mode_prepared, x87_count,
                     writers, sorted(external_targets), call_contracts.stack_pops_from_decoded(fn.insns))
        rows.append((key, row, False))
    ctx.artifacts.flush_counts()
    return rows


def _escapes(task):
    addrs, helpers = task.addresses, task.helpers
    ctx, tr = _WORKER["ctx"], _WORKER["ctx"].tr
    tr.seh_helpers = set(helpers)
    found = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        if tr.seh_escaping_returns(fn):
            found.append(addr)
    return found


def seh_helpers(pool, parts, escapes, calls):
    """Fixpoint of functions that return with an established SEH frame."""
    helpers = {addr for addr, esc in escapes.items() if esc}
    while True:
        pending = sorted(addr for addr, targets in calls.items()
                         if addr not in helpers and helpers.intersection(targets))
        if not pending:
            return helpers
        found = set()
        for part in pool.map(_escapes, [EscapeTask(tuple(p), frozenset(helpers)) for p in chunked(pending, len(parts))]):
            found.update(part)
        if not found:
            return helpers
        helpers |= found


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


def chunked(addrs, count):
    addrs = sorted(addrs)
    return [part for part in (addrs[i::count] for i in range(count)) if part]


@dataclass(frozen=True)
class ScanResult:
    scanned: dict
    helpers: frozenset
    entry_model: EntryModel
    native_replaced: frozenset

    @property
    def internal(self):
        return self.entry_model.internal

    def cw_inputs(self):
        return self.entry_model.cw_inputs(self.scanned)

    def opaque(self, exclusions=False):
        found = set(self.native_replaced) | set(self.helpers)
        if exclusions:
            found.update(addr for addr, row in self.scanned.items() if row.exclusion is not None)
        return found


def build(ctx, jobs, artifacts=None, cache=None):
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(ctx.settings, artifacts)) as pool:
        scanned = {}
        for batch in pool.map(_scan,
                [ScanTask(tuple(part)) for part in chunked(sorted(ctx.program.functions), jobs * 24)]):
            for key, row, hit in batch:
                scanned[row.addr] = row
                if cache is not None and key is not None:
                    counter = 'scan_hits' if hit else 'scan_misses'
                    cache.counts[counter] = cache.counts.get(counter, 0) + 1
                    if hit:
                        cache.retain(key)
                    else:
                        cache.put(key, row)
        helpers = seh_helpers(pool, chunked(sorted(scanned), jobs * 24),
                              {addr: row.seh_escapes for addr, row in scanned.items()},
                              {addr: row.calls for addr, row in scanned.items()})
    return ScanResult(scanned, frozenset(helpers), ctx.program.classify_entries(scanned),
                      frozenset(native_replaced(ctx.settings)))


def fingerprint(settings):
    identity = x87_facts.identity(settings, indirect=False)
    return x87_facts.digest({"format": CACHE_VERSION, **identity})


def read_cache(ctx, path):
    version, stamp, rows, helpers = pickle.loads(Path(path).read_bytes())
    if version != CACHE_VERSION or stamp != fingerprint(ctx.settings):
        raise ValueError("stale scan cache; refresh with propagation --scan-cache")
    scanned = {addr: ScanRow(*row) for addr, row in rows.items()}
    return ScanResult(scanned, frozenset(helpers), ctx.program.classify_entries(scanned),
                      frozenset(native_replaced(ctx.settings)))


def load(ctx, jobs, cache=None):
    if cache and Path(cache).exists():
        try:
            return read_cache(ctx, cache)
        except (OSError, ValueError, TypeError, EOFError, pickle.UnpicklingError):
            pass
    result = build(ctx, jobs)
    if cache:
        path = Path(cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = {addr: tuple(row) for addr, row in result.scanned.items()}
        path.write_bytes(pickle.dumps((CACHE_VERSION, fingerprint(ctx.settings), rows, result.helpers)))
    return result
