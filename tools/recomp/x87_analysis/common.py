"""Shared analysis inputs, contracts and reports."""
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import pickle

import driver
import game_config
from settings import Settings
from ir import x87, x87_mode

MASK = 0x0f00
ScanRow = namedtuple("ScanRow", "addr description calls references dangling returns exclusion seh_escapes register_facts cw_facts x87_count writers external_targets")


def settings_for(args):
    game_config.resolve_game_args(args, default=None)
    settings = Settings(game_config.load(args.game_dir, args.build_root), allow_unmodelled="offline CW analysis")
    if hashlib.sha256(Path(settings.exe).read_bytes()).hexdigest() != settings.sha256.lower():
        raise ValueError("original executable SHA-256 does not match game configuration")
    return settings


def scan_fingerprint(ctx):
    fingerprint = hashlib.sha256()
    kit = Path(__file__).resolve().parents[3]
    inputs = list((kit / "tools").rglob("*.py"))
    inputs += list(ctx.settings.game_dir.rglob("*.toml"))
    inputs += list(ctx.settings.code_map.rglob("*"))
    if ctx.settings.x87_mode_metadata:
        inputs.append(ctx.settings.x87_mode_metadata)
    for path in sorted(set(p.resolve() for p in inputs if p.is_file())):
        fingerprint.update(str(path).encode())
        fingerprint.update(path.read_bytes())
    fingerprint.update(ctx.image.data)
    fingerprint.update(repr(sorted(ctx.program.configured_entries)).encode())
    return fingerprint.hexdigest()


def read_scan(path, ctx):
    stamp, scanned, helpers = pickle.loads(path.read_bytes())
    if stamp != scan_fingerprint(ctx):
        raise ValueError("stale scan cache; refresh with propagation --scan-cache")
    return {addr: ScanRow(*row) for addr, row in scanned.items()}, helpers


def scan(ctx, jobs, cache=None):
    if cache and cache.exists():
        try:
            return read_scan(cache, ctx)
        except ValueError:
            pass
    scanned, helpers = driver.scan_program(ctx.settings, jobs)
    scanned = {addr: ScanRow(*row) for addr, row in scanned.items()}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps((scan_fingerprint(ctx), scanned, helpers)))
    return scanned, helpers


def apply_contracts(prepared, boundaries, image, rows):
    prepared = dict(prepared)
    boundaries = {**boundaries, "calls": dict(boundaries["calls"])}
    for row in rows:
        addr, site = int(row["function"], 16), int(row["site"], 16)
        if not row.get("evidence") or not row.get("assumption"):
            raise ValueError("analysis contracts require evidence and an explicit assumption")
        raw = bytes.fromhex(row["bytes"])
        if image.bytes_at(site, len(raw)) != raw:
            raise ValueError("%08x: analysis contract disagrees with original bytes" % site)
        if row["kind"] == "terminate_function":
            if site != addr:
                raise ValueError("termination contract bytes must identify the function entry")
            sites = [{**row, "kind": "terminate_call", "function": "%08x" % owner,
                      "site": "%08x" % call_site,
                      "bytes": image.bytes_at(call_site, 5).hex()}
                     for owner, body in prepared.items() if body
                     for _, call_site, target in body.calls if target == addr]
            prepared, boundaries = apply_contracts(prepared, boundaries, image, sites)
            continue
        body = prepared[addr]
        if body is None or site not in body.sites:
            raise ValueError("%08x: analysis contract has no prepared instruction" % site)
        index = body.sites.index(site)
        if row["kind"] == "preserve_call":
            if site not in {s for _, s, _ in body.calls}:
                raise ValueError("%08x: expected a prepared call" % site)
            boundaries["calls"][site] = x87_mode.ReturnSummary(MASK, 0, 0)
            continue
        succ = list(body.succ)
        if row["kind"] == "terminate_call":
            if site not in {s for _, s, _ in body.calls}:
                raise ValueError("%08x: expected a prepared call" % site)
            succ[index] = ()
        elif row["kind"] == "select_edge":
            target = body.sites.index(int(row["target"], 16))
            if target not in succ[index]:
                raise ValueError("%08x: selected edge is absent" % site)
            succ[index] = (target,)
        else:
            raise ValueError("unknown analysis contract kind: " + row["kind"])
        seen, todo = set(), list(body.entries)
        while todo:
            i = todo.pop()
            if i not in seen:
                seen.add(i)
                todo.extend(succ[i])
        succ = tuple(tuple(j for j in targets if j in seen) if i in seen else ()
                     for i, targets in enumerate(succ))
        preds = [[] for _ in body.sites]
        for i, targets in enumerate(succ):
            for j in targets:
                preds[j].append(i)
        prepared[addr] = body._replace(
            order=tuple(i for i in body.order if i in seen), succ=succ,
            preds=tuple(tuple(p) for p in preds),
            effects=tuple(e for e in body.effects if e[0] in seen),
            calls=tuple(c for c in body.calls if c[0] in seen
                        and not (row["kind"] == "terminate_call" and c[0] == index)),
            returns=tuple(i for i in body.returns if i in seen
                          and not (row["kind"] == "terminate_call" and i == index)),
            word_relations={i: r for i, r in body.word_relations.items() if i in seen})
    return prepared, boundaries


def preserving(summary):
    return summary.preserved_mask & MASK == MASK


def coverage(results, mnemonics):
    arithmetic = []
    known_functions = set()
    sites = 0
    for addr, result in results.items():
        for site, fact in result.sites.items():
            if not fact.known_mask & MASK:
                continue
            sites += 1
            known_functions.add(addr)
            if mnemonics.get(site) in x87.ARITH:
                arithmetic.append({"function": "%08x" % addr, "site": "%08x" % site,
                                   **x87_mode.fact_dict(fact)})
    return {"functions_with_known_sites": len(known_functions), "known_sites": sites,
            "arithmetic_sites": len(arithmetic),
            "arithmetic_functions": len({row["function"] for row in arithmetic}),
            "arithmetic": arithmetic}



def context_contracts(path, ctx):
    contracts = json.loads(path.read_text()) if path else []
    for row in contracts:
        site = int(row.get('site', row['function']), 16)
        raw = bytes.fromhex(row['bytes'])
        if not raw or ctx.image.bytes_at(site, len(raw)) != raw:
            raise ValueError('contract bytes disagree at %08x' % site)
        if row['kind'] not in ('frame_call', 'terminate_function', 'terminate_call') or not row.get('assumption') or not row.get('evidence'):
            raise ValueError('contract must name its kind, evidence and assumption')
        if row['kind'] == 'frame_call' and (row.get('stack_pop', -1) < 0
                or row.get('foreign_eax') and row.get('foreign_size', 0) <= 0):
            raise ValueError('frame contract requires stack cleanup and a bounded foreign object')
        if row.get('require_imports_preserve_pc_rc') and not ctx.settings.x87_imports_preserve_pc_rc:
            raise ValueError('frame contract requires the configured import preservation contract')
        if row['kind'] == 'terminate_call' and ctx.program.containing(site) != int(row['function'], 16):
            raise ValueError('terminating call does not belong to its stated owner')
    return contracts
