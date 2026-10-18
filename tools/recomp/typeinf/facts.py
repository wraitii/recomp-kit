"""Per-function object facts for whole-program dispatch bounds.

Each function is lifted once, in parallel, against a register-preservation
oracle taken from the build's call contracts, and reduced to the values the
dispatch proof needs: its entry `this`, resolved writes, direct and tail calls
with register views, return-register identities and indirect call shapes.
Nodes are SSA value ids local to the function.
"""
import hashlib
import os
import pickle
from tempfile import TemporaryDirectory
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .core import CALLEE_SAVED, Functions, _exits, _forms, _views, this_node, vtable_slots
from .values import (FRAME_REGISTERS, GPRS, call_register_lanes, direct_call_sites, indirect_call_sites,
                     loaded_field, memory_accesses)

Facts = namedtuple("Facts", "addr entry writes calls exits sites error")
LINK_REGISTERS = tuple(name for name in GPRS if name not in FRAME_REGISTERS)
RESULT_REGISTERS = ("EAX", "ECX", "EDX")
VERSION = "dispatch-facts-v1"

_WORKER = {}


def contracts(settings, jobs):
    import driver
    import scan
    from ir import call_contracts
    from ir import indirect_targets
    from ir.artifacts import ArtifactStore
    driver.discovered_rows(settings)
    ctx = driver.Context(settings)
    with TemporaryDirectory(prefix="typeinf-scan-") as temporary:
        found_scan = scan.build(ctx, jobs, temporary)
        scanned, helpers = found_scan.scanned, found_scan.helpers
        store = ArtifactStore(temporary)

        def contract_body(addr):
            fn, entries = ctx.load(ctx.program.containing(addr) or addr)
            fir = store.lifted(ctx.tr, ctx.lifter, fn)
            fir.entries = tuple(entries)
            return fir

        found = call_contracts.analyze(
            {addr: row.calls for addr, row in scanned.items()},
            {addr: row.register_facts or None for addr, row in scanned.items()},
            analyzable=lambda a: bool(scanned.get(a) and scanned[a].register_facts is not None
                                      and a not in helpers and a not in found_scan.native_replaced),
            roots=[addr for addr, row in scanned.items() if row.exclusion is None],
            indirect=indirect_targets.load(settings, ctx.program), effects=settings.call_effects,
            noreturn=ctx.program.noreturn, bodies=contract_body)
    return {addr: frozenset(contract.preserves) for addr, contract in found.items() if contract is not None}


def oracle(preserved, abi=True):
    def preserves(target, name):
        found = preserved.get(target) if target is not None else None
        if found is None:
            return abi and name in CALLEE_SAVED
        return name in found
    return preserves


def _init(settings, preserved, abi):
    from program import Program
    program = Program(settings)
    _WORKER["functions"] = Functions(program)
    _WORKER["preserves"] = oracle(preserved, abi)


def _batch(addrs):
    functions, found = _WORKER["functions"], []
    for addr in addrs:
        found.append(function_facts(functions, addr, _WORKER["preserves"]))
        functions.values.pop(addr, None)
    return found


def collect(settings, preserved, jobs, abi=True):
    program_addrs = sorted(_program_functions(settings))
    parts = [program_addrs[k:k + 64] for k in range(0, len(program_addrs), 64)]
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(settings, preserved, abi)) as pool:
        return {facts.addr: facts for batch in pool.map(_batch, parts) for facts in batch}


def _program_functions(settings):
    from program import Program
    return Program(settings).functions


def cached(path, key, build):
    path = Path(path)
    if path.exists():
        stored_key, value = pickle.loads(path.read_bytes())
        if stored_key == key:
            return value
    value = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps((key, value), protocol=5))
    return value


def source_key(settings, *extra):
    digest = hashlib.sha256(VERSION.encode())
    digest.update(settings.sha256.encode())
    tools = Path(__file__).resolve().parents[1]
    roots = [settings.code_map, settings.game_dir / "game.toml", tools]
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*")
                                                     if p.is_file() and p.suffix in (".py", ".tsv", ".txt", ".toml")
                                                     and "__pycache__" not in p.parts
                                                     and p.name != "dispatch.py")
        for path in files:
            digest.update(str(path).encode())
            digest.update(path.read_bytes())
    for value in extra:
        digest.update(repr(value).encode())
    return digest.hexdigest()


def function_facts(functions, addr, preserves):
    functions.lift([addr])
    fv = functions.values.get(addr)
    if fv is None:
        return Facts(addr, None, (), (), {}, (), functions.fallbacks.get(addr) or functions.errors.get(addr)
                     or "not lifted")
    entry = this_node(addr, fv)
    program = functions.program
    writes = []
    for access in memory_accesses(fv):
        if access.access != "write":
            continue
        for base, constant in _forms(fv, access.base, access.constant, preserves):
            if base is None:
                continue
            value = access.value
            table = (value.data if value.opc == "CONST" and access.size == 4
                     and vtable_slots(program, value.data) else None)
            writes.append((access.site_addr, base.id, constant, table))
    registers = {record.site_addr: record for record in call_register_lanes(fv)}
    calls = []
    for call in direct_call_sites(fv):
        record = registers.get(call.site_addr)
        if record is None:
            continue
        before = {name: _ids(_views(addr, fv, record.before[name], preserves)) for name in LINK_REGISTERS}
        after = {name: _ids(_views(addr, fv, record.after[name], preserves)) for name in RESULT_REGISTERS}
        calls.append((call.site_addr, call.target, before, after))
    exits = {name: frozenset((source, offset) for kind, source, offset in
                             (view for view in views if view[0] == "through"))
             for name, views in _exits(addr, fv, preserves).items()}
    sites = []
    for record in indirect_call_sites(fv):
        fields = set()
        for leaf, at in _forms(fv, record.base_value, 0, preserves):
            holder = loaded_field(fv, leaf) if leaf is not None and at == 0 else None
            fields |= {(None, None)} if holder is None else {
                (None if root is None else root.id, offset)
                for root, offset in _forms(fv, holder[0], holder[1], preserves)}
        before = registers.get(record.site_addr)
        ecx = frozenset() if before is None else _ids(_views(addr, fv, before.before["ECX"], preserves))
        sites.append((record.site_addr, record.displacement, frozenset(fields), ecx))
    return Facts(addr, None if entry is None else entry[1], tuple(writes), tuple(calls),
                 {name: found for name, found in exits.items() if found}, tuple(sites), None)


def _ids(views):
    return frozenset((node[1], offset) for node, offset in views)


def default_jobs():
    return os.cpu_count() or 1
