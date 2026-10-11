"""Translate a game from its Ghidra code map: program model, SLEIGH lifting, SSA emission.

The map names every function, entry and jump table; nothing here discovers
code. Workers lift and emit one function at a time from the image bytes. A body
SSA cannot emit, and the wrappers of bodies whose entries are all switch cases,
come from the decoded emitter, fed the map's own instruction boundaries.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import code_map
import game_config
import decoded
import output
from settings import Settings
from decoded import external_exits
from ir import call_contracts, contract_variants, production, x87_mode, x87_facts, prepared_store
from ir.invocation import Declaration
from ir.lift import Lifter, LiftError
from ir.ssa import SSAError
from program import FunctionDescription as Row, Program, ScanRow, TranslateError

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
        self.image = decoded.Image(settings.exe, settings.base)
        self.program = Program(settings)
        program = self.program
        self.decoded_only = program.metadata.get("emitter") == "decoded"
        if program.metadata.get("provenance") == "provisional-sweep" and not settings.allow_unmodelled:
            raise TranslateError("provisional code maps require --allow-unmodelled with an explicit reason")
        self.trap_instructions = {int(at, 16) for at in program.metadata.get("trap_instructions", "").split(",") if at}
        if self.decoded_only:
            settings.call_contracts = False
            self.image.allow_unmodelled = settings.allow_unmodelled
        self.entries = program.entries()
        self.interior = program.interior_entries()
        self.tr = decoded.Translator(self.image, set(self.entries), settings.allow_unmodelled)
        self.tr.provisional = program.metadata.get("provenance") == "provisional-sweep"
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
        external_targets = set()
        for targets in external_exits(tr, fn).values():
            external_targets.update(targets)
            if not ctx.tr.provisional:
                dangling.update(t for t in targets if not ctx.dispatchable(t))
        reason = "decoded-only code map" if ctx.decoded_only else production.exclusion(fn)
        escapes = bool(tr.seh_escaping_returns(fn))
        site = (bool(fn.seh_sites) or any(fn.insns[i].mnem == "POP" for i in fn.seh_restores)
                or tr.image.msvc_seh_epilog(addr) or any(map(tr.seh_except_entry, fn.insns)))
        prepared, mode_prepared = None, None
        if not (escapes or site or fn.pushed_continuations or fn.return_jumps
                or fn.dead_addrs or addr in tr.noreturn_callees):
            try:
                fir = decoded.function_ir(tr, ctx.lifter, fn)
                prepared = call_contracts.prepare(fir) if not entries else None
                fir.entries = tuple(entries)
                mode_prepared = x87_mode.prepare(fir)
            except (LiftError, RecursionError):
                prepared = None
            else:
                prepared = prepared or False
        x87_count, writers = 0, []
        for ins in fn.insns:
            mnem = ins.mnem.upper().removeprefix("WAIT ")
            x87_count += mnem.startswith("F") and mnem != "FEMMS"
            if x87_mode.writer_effect(mnem) is not None:
                writers.append({"site": ins.addr, "mnemonic": mnem})
        rows.append(ScanRow(addr, Row(addr, fn.name, len(fn.insns), fn.insns[0].addr, fn.insns[-1].addr),
                     calls, refs, sorted(dangling), returns, reason, escapes, prepared, mode_prepared, x87_count,
                     writers, sorted(external_targets)))
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


def scan_program(settings, jobs):
    ctx = Context(settings)
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(settings,)) as pool:
        scanned = {row.addr: row for batch in pool.map(_scan,
                   chunked(sorted(ctx.program.functions), jobs * 24)) for row in batch}
        helpers = seh_helpers(pool, chunked(sorted(scanned), jobs * 24),
                              {addr: row.seh_escapes for addr, row in scanned.items()},
                              {addr: row.calls for addr, row in scanned.items()})
    return scanned, helpers


@dataclass
class FunctionPlan:
    addr: int
    path: Path | None
    reason: str | None
    requests: set


def _prepare(task):
    addrs, helpers, internal, contracts, private_targets, checked_returns, selected_callers, mode_sites, entry_verification, store = task
    ctx, tr = _CTX, _CTX.tr
    tr.seh_helpers = set(helpers)
    tr.internal_entries = internal
    plans = []
    for addr in addrs:
        fn, entries = ctx.load(addr)
        tr.analyze(fn, entries)
        reason = "decoded-only code map" if ctx.decoded_only else production.exclusion(fn)
        path, requests = None, set()
        if reason is None:
            try:
                selected = addr in selected_callers
                callee_contracts = dict(contracts.get(addr) or {})
                if selected:
                    callee_contracts.update(private_targets)
                body = production.prepare_ssa(
                    tr, ctx.lifter, fn, entries, ctx.settings.emit, ctx.known, callee_contracts,
                    checked_returns, private_targets if selected else (), report_calls=selected,
                    x87_mode_sites=mode_sites.get(addr), x87_entry_verify=entry_verification.get(addr))
            except (SSAError, LiftError) as error:
                reason = str(error)
            else:
                path = Path(store) / ("%08x.pickle" % addr)
                prepared_store.write(path, body)
                requests = body.requests
        if reason is not None and addr in checked_returns:
            raise TranslateError("%08x: checked-return body failed preparation: %s" % (addr, reason))
        plans.append(FunctionPlan(addr, path, reason, requests))
    return plans


def _emit(task):
    plans, helpers, internal, contract_masks = task
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
        if plan.path is not None:
            body = prepared_store.read(plan.path)
            body.finalize(contract_masks)
            lines, external, requests, decisions = production.emit_ssa_prepared(body)
            deps = set(body.declarations())
            if entries and not external:
                wrappers = tr.translate(fn, entries)
                head = "void fn_%08x(X86 *c) {" % addr
                kept = [line for line in wrappers if not line.startswith(head)]
                if len(kept) != len(wrappers) - 1:
                    raise TranslateError("%08x: prepared entry wrapper layout changed" % addr)
                lines = kept + lines
                deps |= {Declaration("entry", t) for t in tr.call_targets}
        if lines is None:
            lines = tr.translate(fn, entries)
            deps = {Declaration("entry", t) for t in tr.call_targets}
        out.append((addr, lines, re.sub(r"^[0-9a-f]{8}: ", "", reason) if reason else None,
                    list(tr.unmodelled), requests, decisions, frozenset(deps)))
    return out


def _private_candidate(task):
    addr, helpers, known_mode = task
    ctx = _CTX
    ctx.tr.seh_helpers = set(helpers)
    try:
        fn, entries = ctx.load(addr)
        ctx.tr.analyze(fn, entries)
        contract = production.private_contract(ctx.tr, ctx.lifter, fn)
        production.prepare_private(ctx.tr, ctx.lifter, fn, ctx.settings.emit, 0x3fff, known_mode)
        production.prepare_ssa(ctx.tr, ctx.lifter, fn, entries, ctx.settings.emit,
                            ctx.known, {}, {addr})
        return addr, contract, None
    except (SSAError, LiftError, RecursionError) as error:
        return addr, None, str(error)


def _prepare_private(task):
    addr, mask, known_mode, store = task
    ctx = _CTX
    fn, entries = ctx.load(addr)
    ctx.tr.analyze(fn, entries)
    plan = production.prepare_private(ctx.tr, ctx.lifter, fn, ctx.settings.emit, mask, known_mode)
    if plan.body is not None:
        path = Path(store) / ("private-%08x.pickle" % addr)
        prepared_store.write(path, plan.body)
        plan.body = path
    return plan


def _private(plan):
    if plan.body is not None:
        plan.body = prepared_store.read(plan.body)
    return plan.addr, production.emit_private_prepared(plan)


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
    ap.add_argument("--module", help="translate one [modules.aux] image")
    ap.add_argument("--report", default=None, help="write a JSON stats file")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--jobs", type=int, default=None, help="worker processes (default: all cores)")
    ap.add_argument("--allow-unmodelled", metavar="REASON", default=None,
                    help="Translate instructions this translator cannot model into a trap at "
                         "their own address instead of refusing the image; reaching one at run "
                         "time is still fatal.")
    return ap.parse_args(argv)


def mode_report(settings, prepared, summaries, opaque, known_modes, x87_counts, exposure, selected=()):
    functions = []
    for addr in sorted(set(selected) | settings.state_contract_callers | settings.checked_returns):
        body = prepared.get(addr)
        row = {"function": "%08x" % addr,
               "x87_instructions": x87_counts.get(addr, 0),
               "return_relation": x87_mode.summary_dict(summaries.get(addr, x87_mode.UNKNOWN_SUMMARY))}
        if body is None or addr in opaque:
            row["reason"] = "opaque function"
        else:
            analysis = x87_mode.analyze(body, summaries, opaque)
            targets = {site: target for _, site, target in body.calls}
            row["calls"] = [{"site": "%08x" % site,
                             "target": "%08x" % targets[site] if targets[site] is not None else None,
                             "incoming": x87_mode.fact_dict(fact)}
                            for site, fact in sorted(analysis.calls.items())]
            row["return_fact"] = x87_mode.fact_dict(analysis.returns)
            row["losses"] = analysis.losses
        functions.append(row)
    x87_functions = {addr for addr, count in x87_counts.items() if count}
    covered = {addr for addr in x87_functions
               if summaries.get(addr, x87_mode.UNKNOWN_SUMMARY).preserved_mask & 0x0f00 == 0x0f00}
    return {"scope": "translated paths without hooks or dispatch overrides",
            "exposure": exposure,
            "coverage": {"x87_functions": len(x87_functions),
                         "mode_preserving": len(covered),
                         "without_mode_preservation_proof": len(x87_functions - covered),
                         "opaque_or_unprepared": sum(prepared.get(addr) is None or addr in opaque
                                                     for addr in x87_functions)},
            "guarded_private_targets": ["%08x" % addr for addr in sorted(known_modes)],
            "private_entry_guard": {"mask": "0f00", "value": "0000"},
            "entry_fact": "symbolic incoming control word; no startup-mode assumption",
            "functions": functions,
            "return_relations": [{"function": "%08x" % addr, **x87_mode.summary_dict(summary)}
                                 for addr, summary in sorted(summaries.items())]}


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr, flush=True))
    settings = Settings(game_config.load(args.game), args.allow_unmodelled, jobs_for(args.jobs), args.module)
    ctx = Context(settings)
    settings, program = ctx.settings, ctx.program
    phase("load")
    addrs = sorted(program.functions)
    parts = chunked(addrs, settings.jobs * 24)
    with TemporaryDirectory(prefix="recomp-prepared-") as store, ProcessPoolExecutor(
            max_workers=settings.jobs, initializer=_init, initargs=(settings,)) as pool:
        scanned = {}
        for part in pool.map(_scan, parts):
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
        mode_sites = x87_facts_loaded.mode_sites()
        entry_verification = {row["function"]: (row["mask"], row["value"])
                              for row in settings.x87_entry_contracts if row.get("verify")}
        mode_summaries = x87_mode.summarize(mode_prepared, mode_opaque)
        x87_counts = {addr: row.x87_count for addr, row in scanned.items()}
        mode_exposure = x87_mode.exposure_report(mode_prepared, calls, mode_opaque, x87_counts,
                                                {addr: row.writers for addr, row in scanned.items()})
        for addr in settings.checked_returns:
            row = scanned.get(addr)
            if not row or not row.register_facts or row.exclusion is not None or addr in helpers or addr in conservative:
                raise TranslateError("%08x: checked return requires an unreplaced, closed single-entry body without SEH" % addr)
        contracts = {}
        if settings.call_contracts:
            prepared = {addr: row.register_facts or None for addr, row in scanned.items()}
            roots = [addr for addr, row in scanned.items() if row.exclusion is None]
            contracts = call_contracts.analyze(
                calls, prepared,
                analyzable=lambda a: bool(scanned.get(a) and scanned[a].register_facts is not None
                                          and a not in helpers and a not in conservative),
                roots=roots)
        phase("contracts")
        private_targets, private_rejections = {}, {}
        checked_returns = set(settings.checked_returns)
        selected_callers = set(settings.state_contract_callers)
        enabled = settings.call_contracts and settings.emit["local_state"] and not settings.module
        candidates = {addr for addr in checked_returns if not calls[addr]} if enabled and selected_callers else set()
        if enabled and settings.private_leaf_contracts:
            candidates |= contract_variants.eligible_leaves(scanned, mode_opaque)
        known_modes = {addr for addr in candidates
                       if settings.emit["x87_cw_clone"] and not settings.emit["x87_scalar_strict"]
                       and x87_counts[addr] and addr not in mode_opaque
                       and x87_mode.mode_preserving_leaf(mode_prepared.get(addr))}
        for addr, contract, reason in pool.map(_private_candidate,
                [(a, helpers, a in known_modes) for a in sorted(candidates)]):
            if reason is None:
                private_targets[addr] = contract
            elif addr in settings.checked_returns:
                raise TranslateError("%08x: configured private body failed: %s" % (addr, reason))
            else:
                private_rejections[addr] = reason
        known_modes &= private_targets.keys()
        checked_returns |= private_targets.keys()
        if enabled and settings.private_leaf_contracts:
            selected_callers |= {addr for addr, row in scanned.items()
                                 if addr not in mode_opaque and row.register_facts and row.cw_facts
                                 and any(t in private_targets for t in calls[addr])}
        if not enabled:
            selected_callers.clear()
        phase("private candidates")

        tasks = [(part, helpers, {a: internal[a] for a in part if a in internal},
                  {a: {t: contracts[t] for t in calls[a] if t in contracts}
                   for a in part if contracts}, private_targets, checked_returns,
                  selected_callers, mode_sites, entry_verification, store) for part in parts]
        plans = {plan.addr: plan for batch in pool.map(_prepare, tasks) for plan in batch}
        specialization = contract_variants.SpecializationPlan.build(
            {addr: plan.requests for addr, plan in plans.items()})
        shared_masks = specialization.layouts
        private_plans = list(pool.map(_prepare_private,
                             [(addr, mask, addr in known_modes, store)
                              for addr, mask in sorted(shared_masks.items())]))
        phase("prepare and plan")
        emit_tasks = [([plans[a] for a in part], helpers,
                       {a: internal[a] for a in part if a in internal}, shared_masks) for part in parts]
        results = {row[0]: row for batch in pool.map(_emit, emit_tasks) for row in batch}
        phase("emit")
        private_masks = specialization.private_masks
        private_requests = specialization.caller_requests
        for addr, row in results.items():
            if row[4] != private_requests.get(addr, set()):
                raise TranslateError("%08x: emission differs from its specialization plan" % addr)
        for addr, lines in pool.map(_private, private_plans):
            results[addr][1].extend(lines)
        phase("private entries")

    bodies = {addr: results[addr][1] for addr in addrs}
    entries_by_fn = {addr: set(found) for addr, found in ctx.interior.items()}
    entry_names = sorted(ctx.entries)
    call_returns = sorted({r for row in scanned.values() for r in row.returns})
    functions = [rows[a] for a in addrs]
    dependencies = {addr: results[addr][6] for addr in addrs}
    os.makedirs(args.out, exist_ok=True)
    output.write_funcs_header(args.out, entry_names)
    output.emit_body_chunks(args.out, functions, bodies, entries_by_fn, dependencies)
    output.emit_entry_chunks(args.out, entry_names, settings.module, checked_returns, private_masks)
    symbols = symbol_rows(ctx, entry_names, rows)
    if settings.module:
        output.write_module_table(args.out, entry_names, symbols, call_returns, settings.module)
    else:
        output.write_table(args.out, entry_names, symbols, call_returns, private_masks, known_modes)
    output.write_symbols(
        args.out, str(settings.exe), ctx.image.base, symbols, settings.curated)
    decisions = [decision for addr in sorted(results) if addr in selected_callers
                 for decision in results[addr][5]]
    decisions.sort(key=lambda item: (item.get("site", ""), item.get("target", ""),
                                     json.dumps(item, sort_keys=True, separators=(",", ":"))))
    contract_report = {
        "configured_callers": ["%08x" % a for a in sorted(settings.state_contract_callers)],
        "selected_callers": ["%08x" % a for a in sorted(selected_callers)],
        "checked_targets": ["%08x" % a for a in sorted(checked_returns)],
        "private_selection": {"automatic": settings.private_leaf_contracts,
                              "heuristic": "at least two direct call sites; at most 64 instructions per site",
                              "rejections": {"%08x" % a: r for a, r in sorted(private_rejections.items())}},
        "private_variants": [
            {"target": "%08x" % addr, "mask": "%04x" % mask,
             "fields": [field for i, field in enumerate(call_contracts.FIELDS) if mask & (1 << i)]}
            for addr in sorted(private_masks) for mask in sorted(private_masks[addr])],
        "ssa_fallbacks": [
            {"function": "%08x" % addr, "reason": results[addr][2]}
            for addr in sorted(results) if results[addr][2] is not None],
        "call_decisions": decisions,
        "x87_compiler_facts": x87_facts_loaded.report(),
        "x87_mode": mode_report(settings, mode_prepared, mode_summaries, mode_opaque,
                                known_modes & private_masks.keys(), x87_counts, mode_exposure,
                                selected_callers | checked_returns),
    }
    with open(os.path.join(args.out, "contract-report.json"), "w") as fh:
        json.dump(contract_report, fh, sort_keys=True, indent=1)
        fh.write("\n")
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
