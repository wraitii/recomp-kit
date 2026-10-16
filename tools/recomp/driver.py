"""Translate a game from its Ghidra code map: program model, SLEIGH lifting, SSA emission.

The map names every function, entry and jump table; nothing here discovers
code. Workers lift and emit one function at a time from the image bytes. A body
SSA cannot emit, and the wrappers of bodies whose entries are all switch cases,
come from the decoded emitter, fed the map's own instruction boundaries.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import re
from copy import copy
import sys
import time
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import bootstrap_map
import code_map
import game_config
import decoded
import output
import emission_cache
from settings import Settings
from frontend import external_exits
from ir import call_contracts, call_values, contract_variants, production, x87_mode, x87_facts, x87_report, prepared_store
from ir import c_symbols
from ir import indirect_targets as indirect_target_facts
from ir.c_symbols import Declaration
from ir.artifacts import ArtifactStore
from ir.body import FrozenMap
from ir.selection import Eligibility
from phases import (ScanTask, EscapeTask, ProgramAnalysis, Selection, PreparationTask,
                    FunctionPlan, EmissionTask, EmissionResult, CandidateTask, CandidateResult,
                    PrivatePreparationTask, PrivateEmissionResult, FinalFunctionPlan, FinalEmissionTask)
from ir.lift import Lifter, LiftError
from ir.ssa import SSAError
from program import FunctionDescription as Row, Program, ScanRow, TranslateError

_CTX = None
_CLOCK = [time.monotonic()]


def phase(name):
    now = time.monotonic()
    print("  [phase] %-18s %6.1fs" % (name, now - _CLOCK[0]), file=sys.stderr, flush=True)
    _CLOCK[0] = now


def discovered_rows(settings):
    if settings.discovered is None and settings.discover:
        settings.discovered = bootstrap_map.discover(settings.exe, settings.base, settings.code_map).code_map_rows()
    return settings.discovered


class Context(object):
    """What one process needs to read, lift and emit functions."""

    def __init__(self, settings, artifacts=None):
        self.settings = settings
        self.image = decoded.Image(settings.exe, settings.base)
        self.program = Program(settings, self.image, discovered_rows(settings))
        program = self.program
        self.decoded_only = program.metadata.get("emitter") == "decoded"
        if program.provisional and not settings.allow_unmodelled:
            raise TranslateError("%d discovered functions are provisional and require --allow-unmodelled with an explicit reason"
                                 % len(program.provisional))
        self.trap_instructions = {int(at, 16) for at in program.metadata.get("trap_instructions", "").split(",") if at}
        if self.decoded_only:
            settings.call_contracts = False
            self.image.allow_unmodelled = settings.allow_unmodelled
        self.entries = program.entries()
        self.interior = program.interior_entries()
        self.tr = decoded.Translator(self.image, set(self.entries), settings.allow_unmodelled)
        self.tr.provisional = program.provisional
        self.tr.noreturn_callees = set(program.noreturn)
        self.tr.noreturn_sites = set(program.noreturn_calls)
        self.tr.jumptables = dict(program.tables)
        self.lifter = Lifter()
        self.known = frozenset(program.functions)
        self.artifacts = ArtifactStore(artifacts)

    def load(self, addr):
        """The decoded function, indexed, with the entries the map gives it."""
        image = self.image
        insns = []
        for start, size, lengths in self.program.functions[addr].spans:
            lengths = lengths or code_map.linear_lengths(self.lifter, image, start, size)
            if lengths is None:
                raise TranslateError("%08x: decoder cannot tile the mapped span at %08x" % (addr, start))
            insns.extend(code_map.decode_span(image, start, lengths))
        runs = self.program.overlapping_runs(addr, {ins.addr for ins in insns})
        for start, lengths in runs:
            insns.extend(code_map.decode_span(image, start, "".join("%x" % n for n in lengths)))
        if runs:
            insns.sort(key=lambda ins: ins.addr)
        for ins in insns:
            if ins.addr in self.trap_instructions:
                ins.raw = "%08x  UNMODELLED direct transfer: %s" % (ins.addr, ins.raw.split("  ", 1)[1])
                ins.mnem, ins.rep, ins.ops = "UNMODELLED", None, []
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


def _init(settings, artifacts=None):
    global _CTX
    _CTX = Context(settings, artifacts)


def _scan(task):
    """Per-function facts the whole-program steps need, with the SSA contract inputs."""
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set()
    rows = []
    for addr in task.addresses:
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
                    if not ctx.dispatchable(target) and addr not in ctx.program.provisional:
                        dangling.add(target)
                if target in ctx.entries and target not in ctx.known and ctx.program.containing(target) != addr:
                    refs.add(target)
        external_targets = set()
        for targets in external_exits(tr, fn).values():
            external_targets.update(targets)
            if addr not in ctx.program.provisional:
                dangling.update(t for t in targets if not ctx.dispatchable(t))
        reason = "decoded-only code map" if ctx.decoded_only else production.exclusion(fn)
        escapes = bool(tr.seh_escaping_returns(fn))
        site = (bool(fn.seh_sites) or any(fn.insns[i].mnem == "POP" for i in fn.seh_restores)
                or tr.image.msvc_seh_epilog(addr) or any(map(tr.seh_except_entry, fn.insns)))
        prepared, mode_prepared = None, None
        if not (escapes or fn.pushed_continuations or fn.return_jumps
                or fn.dead_addrs or addr in tr.noreturn_callees):
            try:
                fir = ctx.artifacts.lifted(tr, ctx.lifter, fn)
                prepared = call_contracts.prepare(fir, image=ctx.image) if not (entries or site) else None
                fir.entries = tuple(entries)
                mode_prepared = x87_mode.prepare(fir, ctx.image)
            except (LiftError, RecursionError):
                prepared = None
            else:
                prepared = None if site else prepared or False
        x87_count, writers = 0, []
        for ins in fn.insns:
            mnem = ins.mnem.upper().removeprefix("WAIT ")
            x87_count += mnem.startswith("F") and mnem != "FEMMS"
            if x87_mode.writer_effect(mnem) is not None:
                writers.append({"site": ins.addr, "mnemonic": mnem})
        rows.append(ScanRow(addr, Row(addr, fn.name, len(fn.insns), fn.insns[0].addr, fn.insns[-1].addr),
                     calls, refs, sorted(dangling), returns, reason, escapes, prepared, mode_prepared, x87_count,
                     writers, sorted(external_targets)))
    ctx.artifacts.flush_counts()
    return rows


def _escapes(task):
    addrs, helpers = task.addresses, task.helpers
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    found = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        if tr.seh_escaping_returns(fn):
            found.append(addr)
    return found


def scan_program(settings, jobs):
    ctx = Context(settings)
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(settings,)) as pool:
        scanned = {row.addr: row for batch in pool.map(_scan,
                   [ScanTask(tuple(part)) for part in chunked(sorted(ctx.program.functions), jobs * 24)]) for row in batch}
        helpers = seh_helpers(pool, chunked(sorted(scanned), jobs * 24),
                              {addr: row.seh_escapes for addr, row in scanned.items()},
                              {addr: row.calls for addr, row in scanned.items()})
    return scanned, helpers


def _prepare(task):
    analysis, selection = task.analysis, task.selection
    helpers, internal, contracts = analysis.helpers, analysis.internal, analysis.contracts
    private_targets, checked_returns = selection.private_targets, selection.checked_returns
    selected_callers, mode_sites = selection.callers, analysis.mode_sites
    entry_verification, store = analysis.entry_verification, task.store
    addrs = task.addresses
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    tr.internal_entries = internal
    plans = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        reason = "decoded-only code map" if ctx.decoded_only else production.exclusion(fn)
        path, requests, input_sites = None, set(), ()
        if reason is None:
            try:
                selected = addr in selected_callers
                targets = {tr.branch_target(ins) for ins in fn.insns if ins.mnem == "CALL"}
                callee_contracts = {t: contracts[t] for t in targets if t in contracts}
                if selected:
                    callee_contracts.update(private_targets)
                body = production.prepare_ssa(
                    tr, ctx.lifter, fn, entries,
                    production.ProductionRequest(ctx.settings.emit, ctx.known, callee_contracts,
                        checked_returns, frozenset(private_targets) if selected else frozenset(), selected,
                        mode_sites.get(addr), entry_verification.get(addr),
                        selection.input_arguments), ctx.artifacts)
            except (SSAError, LiftError) as error:
                reason = str(error)
            else:
                path = Path(store) / ("%08x.pickle" % addr)
                prepared_store.write(path, body)
                requests = body.requests
                groups = dict(body.state_groups)
                sites = []
                if ctx.settings.private_input_arguments:
                    for block in body.ssa.blocks.values():
                        for event in block.ops:
                            if event.opc != "CALL" or event.data not in body.options.request.contract_calls:
                                continue
                            call = body.transfers.calls[event.id]
                            publication = body.transfers.publications[event.id]
                            inputs = tuple(field for field in call.state.inputs if field in call_contracts.GPRS)
                            stored = tuple(field for field in inputs
                                           if set(groups[field]) & set(publication.required))
                            preserved_live = []
                            for field in call_contracts.GPRS:
                                if field not in call.state.preserved:
                                    continue
                                value = call_values.field_value(body.ssa, field, groups[field], block.reloads[event.id])
                                live = ((value.whole is not None and value.whole.id in body.live)
                                        or any(lane is not None and lane.id in body.live for lane in value.lanes))
                                if live:
                                    preserved_live.append(field)
                            sites.append((event.data, inputs, stored, tuple(preserved_live)))
                input_sites = tuple(sites)
        if reason is not None and addr in checked_returns:
            raise TranslateError("%08x: checked-return body failed preparation: %s" % (addr, reason))
        plans.append(FunctionPlan(addr, path, reason, frozenset(requests), input_sites))
    ctx.artifacts.flush_counts()
    return plans


def _finalize(task):
    plans = []
    for plan in task.plans:
        body = prepared_store.read(plan.path) if plan.path is not None else None
        if body is not None:
            body.finalize(task.layouts)
        plans.append(FinalFunctionPlan(plan.addr, body, plan.reason))
    return FinalEmissionTask(tuple(plans), task.analysis, task.layouts)


def _finalize_and_emit(task):
    return _emit(_finalize(task))


def _emit(task):
    plans, helpers, internal, layouts = task.plans, task.analysis.helpers, task.analysis.internal, task.layouts
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    tr.internal_entries = internal
    out = []
    for plan in plans:
        addr = plan.addr
        tr.unmodelled.clear()
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        reason, lines, requests, decisions, deps = plan.reason, None, set(), [], set()
        if plan.body is not None:
            body = plan.body
            if body.final is None or body.final.layouts != layouts:
                raise TranslateError("%08x: emission requires finalized shared layouts" % addr)
            lines, external, requests, decisions = production.emit_ssa_prepared(body)
            deps = set(c_symbols.prepared_declarations(body))
            if body.ir.import_tails:
                fallback = copy(fn)
                fallback.dead_addrs = set()
                symbol = c_symbols.symbol('body_import_fallback', addr)
                lines = tr.translate(fallback, fallback.addrs, body_symbol=symbol) + lines
                deps |= {Declaration("entry", t) for t in tr.call_targets}
            if entries and not external:
                wrappers = tr.translate(fn, entries)
                own = "void fn_%08x(X86 *c) { region_%08x(c, %s); }" % (addr, addr, decoded.hexlit(addr))
                kept = [line for line in wrappers if line != own]
                if len(kept) != len(wrappers) - 1:
                    raise TranslateError("%08x: prepared entry wrapper layout changed" % addr)
                lines = kept + lines
                deps |= {Declaration("entry", t) for t in tr.call_targets}
        region = None
        if lines is None:
            lines = tr.translate(fn, entries)
            deps = {Declaration("entry", t) for t in tr.call_targets}
            live = [ins.addr for ins in fn.insns if ins.addr not in fn.dead_addrs]
            region = (min(live), max(live) + 1, "region_%08x" % addr)
        out.append(EmissionResult(addr, lines, re.sub(r"^[0-9a-f]{8}: ", "", reason) if reason else None,
                    tuple(tr.unmodelled), frozenset(requests), tuple(decisions), frozenset(deps), region))
    return out


def _private_candidate(task):
    addr, helpers, known_mode = task.addr, task.helpers, task.known_mode
    ctx = _CTX
    ctx.tr.seh_helpers = set(helpers)
    try:
        fn, entries = ctx.load(addr)
        ctx.tr.analyze(fn, entries)
        ctx.artifacts.reuse_normalized(addr)
        register_uses = (call_contracts.substantive_register_uses(
            call_contracts.prepare(ctx.artifacts.lifted(ctx.tr, ctx.lifter, fn)), fn.insns)
            if task.input_argument_heuristic else {})
        contract = production.private_contract(ctx.tr, ctx.lifter, fn, ctx.artifacts)
        production.prepare_private(ctx.tr, ctx.lifter, fn, ctx.settings.emit,
                                   contract_variants.PrivateLayout.from_mask(0x3fff, contract.reads), known_mode, ctx.artifacts,
                                   finalize=True)
        production.prepare_ssa(ctx.tr, ctx.lifter, fn, entries,
                               production.ProductionRequest(ctx.settings.emit, ctx.known, checked_returns=frozenset({addr})),
                               ctx.artifacts)
        ctx.artifacts.flush_counts()
        return CandidateResult(addr, contract, None, FrozenMap.of(register_uses))
    except (SSAError, LiftError, RecursionError) as error:
        ctx.artifacts.flush_counts()
        return CandidateResult(addr, None, str(error))


def _prepare_private(task):
    addr, layout, known_mode, store = task.addr, task.layout, task.known_mode, task.store
    ctx = _CTX
    fn, entries = ctx.load(addr)
    ctx.tr.analyze(fn, entries)
    plan = production.prepare_private(ctx.tr, ctx.lifter, fn, ctx.settings.emit, layout, known_mode, ctx.artifacts)
    if plan.body is not None:
        path = Path(store) / ("private-%08x-%04x.pickle" % (addr, layout.output_mask))
        prepared_store.write(path, plan.body)
        plan = replace(plan, body=path)
    ctx.artifacts.flush_counts()
    return plan


def _private(plan):
    if plan.body is not None:
        plan = replace(plan, body=prepared_store.read(plan.body))
        plan.body.finalize({})
    lines = production.emit_private_prepared(plan)
    return PrivateEmissionResult(plan.addr, tuple(lines))


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
        for part in pool.map(_escapes, [EscapeTask(tuple(p), frozenset(helpers)) for p in chunked(pending, len(parts))]):
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
    ap.add_argument("--emission-cache", type=Path)
    ap.add_argument("--regen-function", type=lambda v: int(v, 16), action="append", default=[])
    ap.add_argument("--module", help="translate one [modules.aux] image")
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
    settings = Settings(game_config.load(args.game), args.allow_unmodelled, jobs_for(args.jobs), args.module)
    discovered_rows(settings)
    ctx = Context(settings)
    settings, program = ctx.settings, ctx.program
    phase("load")
    if args.regen_function:
        return translate_selected(args, settings, ctx, log, started)
    addrs = sorted(program.functions)
    parts = chunked(addrs, settings.jobs * 24)
    with TemporaryDirectory(prefix="recomp-prepared-") as temporary:
        store = Path(temporary)
        artifacts = store / "artifacts"
        artifacts.mkdir()
        return translate(args, settings, ctx, log, started, store, artifacts, parts, addrs)


def translate(args, settings, ctx, log, started, store, artifacts, parts, addrs):
    program = ctx.program
    with ProcessPoolExecutor(max_workers=settings.jobs, initializer=_init, initargs=(settings, artifacts)) as pool:
        scanned = {}
        for part in pool.map(_scan, [ScanTask(tuple(part)) for part in parts]):
            for row in part:
                scanned[row.addr] = row
        phase("scan")

        rows = {addr: row.description for addr, row in scanned.items()}
        calls = {addr: row.calls for addr, row in scanned.items()}
        escapes = {addr: row.seh_escapes for addr, row in scanned.items()}
        helpers = seh_helpers(pool, parts, escapes, calls)
        phase("seh helpers")

        entry_model = program.classify_entries(scanned)
        internal = entry_model.internal

        dangling = sorted((addr, t) for addr, row in scanned.items() for t in row.dangling)
        if dangling:
            for addr, t in dangling:
                log("  fn_%08x dispatches to %08x, which is not an entry point" % (addr, t))
            raise TranslateError(
                "%d literal dispatch targets are not entry points; add them in Ghidra "
                "(function or referenced entry), re-export the code map and regenerate" % len(dangling))

        conservative = native_replaced(settings)
        mode_prepared = entry_model.cw_inputs(scanned)
        mode_opaque = conservative | helpers | {addr for addr, row in scanned.items() if row.exclusion is not None}
        x87_facts_loaded = x87_facts.load(settings.x87_facts, ctx, mode_prepared, mode_opaque)
        indirect_targets = indirect_target_facts.load(settings, ctx.program)
        mode_sites = x87_facts_loaded.mode_sites()
        entry_verification = {row["function"]: x87_facts.EntryContract.from_row(row)
                              for row in settings.x87_entry_contracts if row.get("verify")}
        mode_summaries = x87_mode.summarize(mode_prepared, mode_opaque, indirect=indirect_targets,
                                            effects=settings.call_effects)
        x87_counts = {addr: row.x87_count for addr, row in scanned.items()}
        mode_exposure = x87_mode.exposure_report(mode_prepared, calls, mode_opaque, x87_counts,
                                                {addr: row.writers for addr, row in scanned.items()})
        eligibility = Eligibility(settings, scanned, mode_opaque)
        if (settings.private_input_arguments or settings.private_input_registers) and not eligibility.enabled:
            raise TranslateError("private register arguments require call contracts, relaxed local state and a game translation")
        for addr in settings.checked_returns:
            if not eligibility.checked(addr):
                raise TranslateError("%08x: checked return requires an unreplaced, closed single-entry body without SEH" % addr)
        for addr in settings.private_input_registers:
            if not eligibility.checked(addr):
                raise TranslateError("%08x: private input register requires an unreplaced, closed single-entry body without SEH" % addr)
        contracts = {}
        if settings.call_contracts:
            prepared = {addr: row.register_facts or None for addr, row in scanned.items()}
            roots = [addr for addr, row in scanned.items() if row.exclusion is None]
            contracts = call_contracts.analyze(
                calls, prepared,
                analyzable=lambda a: bool(scanned.get(a) and scanned[a].register_facts is not None
                                          and a not in helpers and a not in conservative),
                roots=roots, indirect=indirect_targets, effects=settings.call_effects)
        phase("contracts")
        private_targets, private_rejections = {}, {}
        register_uses = {}
        checked_returns = set(settings.checked_returns)
        candidates = eligibility.candidates()
        known_modes = eligibility.known_modes(candidates, mode_prepared)
        for candidate in pool.map(_private_candidate,
                [CandidateTask(a, frozenset(helpers), a in known_modes, settings.private_input_arguments)
                 for a in sorted(candidates)]):
            addr, contract, reason = candidate.addr, candidate.contract, candidate.reason
            if reason is None:
                private_targets[addr] = contract
                register_uses[addr] = candidate.register_uses
            elif addr in settings.checked_returns or addr in settings.private_input_registers:
                raise TranslateError("%08x: configured private body failed: %s" % (addr, reason))
            else:
                private_rejections[addr] = reason
        known_modes &= private_targets.keys()
        checked_returns |= private_targets.keys()
        selected_callers = eligibility.callers(private_targets)
        phase("private candidates")

        analysis = ProgramAnalysis(frozenset(helpers), FrozenMap.of(internal), FrozenMap.of(contracts),
                                   FrozenMap.of(mode_sites), FrozenMap.of(entry_verification))
        input_arguments = dict(settings.private_input_registers)
        selection = Selection(FrozenMap.of(private_targets), frozenset(checked_returns),
                              frozenset(selected_callers), frozenset(known_modes), FrozenMap.of(private_rejections),
                              FrozenMap.of(input_arguments))
        tasks = [PreparationTask(tuple(part), analysis.for_addresses(part, calls), selection, store) for part in parts]
        plans = {plan.addr: plan for batch in pool.map(_prepare, tasks) for plan in batch}
        phase("prepare callers")
        argument_report = {}
        if settings.private_input_arguments:
            automatic_arguments, argument_report = contract_variants.select_input_arguments(
                private_targets, register_uses,
                {addr: plan.input_sites for addr, plan in plans.items()},
                {addr: scanned[addr].description.count for addr in private_targets})
            input_arguments = {**automatic_arguments, **settings.private_input_registers}
            for addr, register in settings.private_input_registers.items():
                row = argument_report.setdefault("%08x" % addr, {})
                row["selected"] = register
                row["configured"] = True
            selection = replace(selection, input_arguments=FrozenMap.of(input_arguments))
            phase("input selection %d" % len(input_arguments))
            affected = {plan.addr for plan in plans.values()
                        if any(input_arguments.get(site[0]) != settings.private_input_registers.get(site[0])
                               and input_arguments.get(site[0]) is not None for site in plan.input_sites)}
            refresh_tasks = [PreparationTask(tuple(part), analysis.for_addresses(part, calls), selection, store)
                             for part in chunked(affected, settings.jobs * 24)]
            for batch in pool.map(_prepare, refresh_tasks):
                plans.update({plan.addr: plan for plan in batch})
        else:
            argument_report = {"%08x" % addr: {"selected": register, "configured": True}
                               for addr, register in settings.private_input_registers.items()}
        specialization = contract_variants.SpecializationPlan.build(
            {addr: plan.requests for addr, plan in plans.items()}, private_targets,
            selection.input_arguments)
        shared_layouts = specialization.layouts
        private_plans = list(pool.map(_prepare_private,
                             [PrivatePreparationTask(addr, layout, addr in known_modes, store)
                              for addr, layouts in sorted(shared_layouts.items()) for layout in layouts]))
        phase("prepare and plan")
        emit_tasks = [EmissionTask(tuple(plans[a] for a in part), analysis.for_addresses(part), shared_layouts) for part in parts]
        results = {row.addr: row for batch in pool.map(_finalize_and_emit, emit_tasks) for row in batch}
        phase("final bind / emit")
        private_masks = specialization.private_masks
        private_requests = specialization.caller_requests
        for addr, row in results.items():
            if row.requests != private_requests.get(addr, set()):
                raise TranslateError("%08x: emission differs from its specialization plan" % addr)
        for row in pool.map(_private, private_plans):
            results[row.addr].lines.extend(row.lines)
        phase("private entries")

    workload = Counter()
    for path in artifacts.glob("counts-*.json"):
        workload.update(json.loads(path.read_text()))
    log("  frontend work: " + json.dumps(dict(workload), sort_keys=True))
    bodies = {addr: results[addr].lines for addr in addrs}
    entries_by_fn = {addr: set(found) for addr, found in ctx.interior.items()}
    entry_names = sorted(ctx.entries)
    call_returns = sorted({r for row in scanned.values() for r in row.returns})
    functions = [rows[a] for a in addrs]
    dependencies = {addr: results[addr].dependencies for addr in addrs}
    os.makedirs(args.out, exist_ok=True)
    output.write_funcs_header(args.out, entry_names)
    output.emit_body_chunks(args.out, functions, bodies, entries_by_fn, dependencies, known_modes)
    output.emit_entry_chunks(args.out, entry_names, settings.module, checked_returns, private_masks,
                             selection.input_arguments)
    symbols = symbol_rows(ctx, entry_names, rows)
    if settings.module:
        output.write_module_table(args.out, entry_names, symbols, call_returns, settings.module)
    else:
        output.write_table(args.out, entry_names, symbols, call_returns, private_masks, known_modes,
                           [row.region for row in results.values() if row.region])
    output.write_symbols(
        args.out, str(settings.exe), ctx.image.base, symbols, settings.curated)
    output.write_call_facts(args.out, {ctx.image.insn_end(site, "CALL"): targets
                                       for site, (kind, targets) in indirect_targets.items() if kind == "call"})
    decisions = [decision for addr in sorted(results) if addr in selected_callers
                 for decision in results[addr].decisions]
    decisions.sort(key=lambda item: (item.get("site", ""), item.get("target", ""),
                                     json.dumps(item, sort_keys=True, separators=(",", ":"))))
    contract_report = {
        "frontend_work": dict(workload),
        "configured_callers": ["%08x" % a for a in sorted(settings.state_contract_callers)],
        "selected_callers": ["%08x" % a for a in sorted(selected_callers)],
        "checked_targets": ["%08x" % a for a in sorted(checked_returns)],
        "private_selection": {"automatic": settings.private_leaf_contracts,
                              "heuristic": "at least two direct call sites; at most 64 instructions per site",
                              "rejections": {"%08x" % a: r for a, r in sorted(private_rejections.items())}},
        "private_input_arguments": {"enabled": settings.private_input_arguments,
                                    "overrides": {"%08x" % a: r for a, r in sorted(settings.private_input_registers.items())},
                                    "selection": argument_report},
        "private_variants": [
            {"target": "%08x" % addr, "mask": "%04x" % mask,
             "fields": [field for i, field in enumerate(call_contracts.FIELDS) if mask & (1 << i)]}
            for addr in sorted(private_masks) for mask in sorted(private_masks[addr])],
        "ssa_fallbacks": [
            {"function": "%08x" % addr, "reason": results[addr].reason}
            for addr in sorted(results) if results[addr].reason is not None],
        "call_decisions": decisions,
        "x87_compiler_facts": x87_facts_loaded.report(),
        "x87_mode": x87_report.mode_report(settings, mode_prepared, mode_summaries, mode_opaque,
                                known_modes & private_masks.keys(), x87_counts, mode_exposure,
                                selected_callers | checked_returns),
    }
    with open(os.path.join(args.out, "contract-report.json"), "w") as fh:
        json.dump(contract_report, fh, sort_keys=True, indent=1)
        fh.write("\n")
    if args.emission_cache:
        emission_cache.save(args.emission_cache, settings, args.out, plans, private_plans,
                            analysis, functions, entries_by_fn, results, contract_report, known_modes)
    phase("write")

    emitted = sum(1 for a in addrs if results[a].reason is None)
    reasons = Counter(results[a].reason for a in addrs if results[a].reason is not None)
    unmodelled = [item for a in addrs for item in results[a].unmodelled]
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
                "frontend_work": dict(workload),
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


def translate_selected(args, settings, ctx, log, started):
    if not args.emission_cache:
        raise TranslateError("--regen-function needs --emission-cache")
    state = emission_cache.load(args.emission_cache, settings, args.out)
    selected = set(args.regen_function)
    unknown = selected - state['plans'].keys()
    if unknown:
        raise TranslateError("selected regeneration has unmapped functions: " + " ".join("%08x" % addr for addr in sorted(unknown)))
    analysis = state['analysis']
    layouts = FrozenMap.of({plan.addr: plan.layout for plan in state['private_plans']})
    plans = [replace(state['plans'][addr], path=Path(args.emission_cache) / state['plans'][addr].path
                     if state['plans'][addr].path is not None else None) for addr in sorted(selected)]
    tasks = [EmissionTask(tuple(part), analysis, layouts) for part in
             (plans[index::settings.jobs] for index in range(settings.jobs)) if part]
    results = state['results']
    with ProcessPoolExecutor(max_workers=settings.jobs, initializer=_init, initargs=(settings,)) as pool:
        for batch in pool.map(_finalize_and_emit, tasks):
            for result in batch:
                if result.requests != results[result.addr].requests:
                    raise TranslateError("selected regeneration changed shared private requests; run a full --regenerate")
                results[result.addr] = result
        private = [replace(plan, body=Path(args.emission_cache) / plan.body if plan.body is not None else None)
                   for plan in state['private_plans'] if plan.addr in selected]
        for row in pool.map(_private, private):
            results[row.addr].lines.extend(row.lines)
    phase("selected emission")
    functions = state['functions']
    bodies = {addr: result.lines for addr, result in results.items()}
    dependencies = {addr: result.dependencies for addr, result in results.items()}
    paths = output.emit_body_chunks(args.out, functions, bodies, state['entries_by_fn'], dependencies,
                                    state['known_modes'])
    written = {Path(path).name for path in paths}
    for path in Path(args.out).glob('chunk_*.c'):
        if not path.name.startswith('chunk_entries_') and path.name not in written:
            path.unlink()
    report = dict(state['report'])
    report['call_decisions'] = sorted([decision for result in results.values() for decision in result.decisions],
                                    key=lambda item: (item.get('site', ''), item.get('target', '')))
    report['partial_regeneration'] = ['%08x' % addr for addr in sorted(selected)]
    Path(args.out, 'contract-report.json').write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
    emission_cache.update(args.emission_cache, state, args.out, selected)
    if args.report:
        Path(args.report).write_text(json.dumps({'partial_regeneration': report['partial_regeneration'],
            'seconds': time.monotonic() - started, 'prepared_from_cache': len(selected)}, indent=1) + "\n")
    phase("selected write")
    log("  regenerated %d functions from cached whole-image preparation" % len(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
