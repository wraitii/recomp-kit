"""Per-function object facts for whole-program dispatch bounds.

Each function is lifted once, in parallel, against a register-preservation
oracle taken from the build's call contracts, and reduced to the values the
dispatch proof needs: its entry `this`, resolved writes, direct and tail calls
with register views, return-register identities and indirect call shapes.
Nodes are SSA value ids local to the function.
"""
import hashlib
import json
import os
import pickle
import sys
from tempfile import TemporaryDirectory
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import ModuleType

import driver
import scan
from ir import call_contracts, indirect_targets
from ir.artifacts import ArtifactStore
from program import Program

from .core import CALLEE_SAVED, Functions, _exits, _forms, _views, this_node, vtable_slots
from .values import (ADDITIVE, FRAME_REGISTERS, GPRS, Frame, _address_form, _leaves, _stored, _whole,
                     absolute_call_targets, call_register_lanes, direct_call_sites, frame_ids,
                     indirect_call_sites, loaded_field, memory_accesses, return_register_lanes, whole_lane_value)

GlobalRead = namedtuple("GlobalRead", "site addr size node")
GlobalWrite = namedtuple("GlobalWrite", "site addr size value")
FieldWrite = namedtuple("FieldWrite", "site base offset value size")
Facts = namedtuple("Facts", "addr entry writes calls exits sites error reloads global_reads global_writes "
                            "results call_values call_arguments field_writes tainted pops",
                   defaults=(frozenset(), (), (), {}, {}, {}, (), frozenset(), None))
LINK_REGISTERS = tuple(name for name in GPRS if name not in FRAME_REGISTERS)
RESULT_REGISTERS = ("EAX", "ECX", "EDX")
UNTRACKED_RELOADS = frozenset(CALLEE_SAVED | {"ESP"})
VERSION = "dispatch-facts-v12"
MAX_ARGUMENTS = 32

_WORKER = {}


def contracts(settings, jobs):
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
    return {addr: (contract.preserves | frozenset(GPRS).difference(contract.writes), contract.kills)
            for addr, contract in found.items() if contract is not None}


def oracle(preserved, abi=True):
    def preserves(target, name):
        kept, killed = preserved.get(target, (frozenset(), frozenset())) if target is not None else ((), ())
        return name in kept or abi and name in CALLEE_SAVED and name not in killed
    return preserves


def _init(settings, preserved, abi):
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
    return Program(settings).functions


def cached(path, key, build):
    path = Path(path)
    if path.exists():
        try:
            stored_key, value = pickle.loads(path.read_bytes())
        except Exception:
            pass
        else:
            if stored_key == key:
                return value
    value = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps((key, value), protocol=5))
    return value


def source_key(settings, *extra):
    digest = hashlib.sha256(VERSION.encode())
    digest.update(settings.sha256.encode())
    key = {name: settings.cfg.get(name) for name in ("game", "translate", "modules", "hooks")}
    key["curated"] = settings.curated
    digest.update(json.dumps(key, sort_keys=True, default=str).encode())
    files = set(_module_files())
    if settings.code_map.is_file():
        files.add(settings.code_map.resolve())
    else:
        files.update(p.resolve() for p in settings.code_map.rglob("*")
                     if p.is_file() and p.suffix in (".tsv", ".txt", ".toml")
                     and "__pycache__" not in p.parts)
    for path in (settings.indirect_targets,
                 settings.game_dir / settings.native_header if settings.native_header else None):
        if path is not None and path.is_file():
            files.add(path.resolve())
    for path in sorted(files):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    for value in extra:
        digest.update(repr(value).encode())
    return digest.hexdigest()


def _module_files():
    tools = Path(__file__).resolve().parents[2]
    seen, pending = set(), [sys.modules[__name__]]
    while pending:
        module = pending.pop()
        if module.__name__ in seen:
            continue
        seen.add(module.__name__)
        for value in vars(module).values():
            if isinstance(value, ModuleType):
                dependency = value
            else:
                name = getattr(value, "__module__", None)
                dependency = sys.modules.get(name) if name else None
            if dependency is not None and dependency.__name__ not in seen:
                pending.append(dependency)
    found = set()
    for name in seen:
        path = getattr(sys.modules.get(name), "__file__", None)
        if not path:
            continue
        path = Path(path).resolve()
        try:
            path.relative_to(tools)
        except ValueError:
            continue
        if path.suffix == ".py":
            found.add(path)
    return found


def _stack_states(fv, frames):
    ssa, memo = fv.ssa, {}
    def state(token):
        pending, current = [], token
        while True:
            current = ssa.resolve(current)
            if current.id in memo:
                break
            if current.opc != "MEMORY":
                memo[current.id] = {}
                break
            op = ssa.resolve(current.args[0])
            if op.opc not in ("STORE", "LOAD") or not op.args:
                memo[current.id] = {}
                break
            pending.append((current, op))
            current = op.args[0]
        for node, op in reversed(pending):
            previous = memo.get(ssa.resolve(op.args[0]).id, {})
            if op.opc == "LOAD":
                memo[node.id] = previous
                continue
            current_state = dict(previous)
            form = _address_form(ssa, op.args[1])
            if form is not None and form[0].id in frames and ssa.resolve(op.args[2]).size == 4:
                current_state[(form[0].id, form[1])] = _stored(ssa, op.args[2])
            memo[node.id] = current_state
        return memo.get(ssa.resolve(token).id, {})
    return state


def _arguments(fv, record, count, states):
    ssa = fv.ssa
    esp = whole_lane_value(ssa, record.before["ESP"])
    form = None if esp is None else _address_form(ssa, esp)
    if form is None:
        return (None,) * count
    root, at = form
    try:
        found = states(record.value.args[0])
    except (IndexError, AttributeError):
        return (None,) * count
    return tuple(found.get((root.id, at + 4 + 4 * k)) for k in range(count))


def function_facts(functions, addr, preserves):
    functions.lift([addr])
    fv = functions.values.get(addr)
    if fv is None:
        return Facts(addr, None, (), (), {}, (), functions.fallbacks.get(addr) or functions.errors.get(addr)
                     or "not lifted")
    entry = this_node(addr, fv)
    program = functions.program
    registers = {record.site_addr: record for record in call_register_lanes(fv)}
    direct = {call.site_addr: call.target for call in direct_call_sites(fv)}
    absolute = absolute_call_targets(fv)
    imports = {site: program.pe.iat_keys[target] for site, target in absolute.items()
               if target in program.pe.iat_keys}
    accesses = memory_accesses(fv)
    stack = {}
    frames = frame_ids(fv)
    offsets = Frame(fv, preserves, functions.pops)
    stack["frame"], stack["offsets"] = frames, offsets
    stack["written"] = {at for access in accesses if access.access == "write"
                        if (form := _address_form(fv.ssa, access.address)) is not None
                        and form[0].id in frames and (at := offsets.of(access.address)) is not None}
    writes, reloads, global_reads, global_writes, field_writes = [], set(), [], [], []
    for access in accesses:
        absolute_addr = _absolute(fv, access.address)
        if access.access == "read":
            if absolute_addr is not None:
                global_reads.append(GlobalRead(access.site_addr, absolute_addr, access.size, access.value.id))
            continue
        value_id = _value_identity(fv, access.value, registers, preserves, functions.pops, stack, imports)
        if absolute_addr is not None:
            global_writes.append(GlobalWrite(access.site_addr, absolute_addr, access.size, value_id))
        for base, constant in _forms(fv, access.base, access.constant, preserves):
            if base is None:
                continue
            if base.id not in frames:
                base_id = _value_identity(fv, base, registers, preserves, functions.pops, stack, imports)
                field_writes.append(FieldWrite(access.site_addr, base_id, constant, value_id, access.size))
            value = access.value
            table = (value.data if value.opc == "CONST" and access.size == 4
                     and vtable_slots(program, value.data) else None)
            writes.append((access.site_addr, base.id, constant, table))
            reload = fv.reloads.get(base.id)
            if table is not None and reload is not None and reload.name in UNTRACKED_RELOADS:
                reloads.add(base.id)
    call_values, call_arguments = {}, {}
    states = _stack_states(fv, frames)
    for site, record in registers.items():
        call_values[site] = {
            name: _value_identities(fv, value, registers, preserves, functions.pops, stack, imports)
            for name in LINK_REGISTERS if (value := _lane_whole(fv, record.before[name])) is not None}
        target = direct.get(site, absolute.get(site))
        pops = functions.pops(target) if target is not None else None
        count = MAX_ARGUMENTS if pops is None or pops < 0 else pops // 4
        arguments = _arguments(fv, record, count, states)
        call_arguments[site] = {
            k: _value_identities(fv, value, registers, preserves, functions.pops, stack, imports)
            for k, value in enumerate(arguments) if value is not None}
    calls = []
    for call in direct_call_sites(fv):
        record = registers.get(call.site_addr)
        if record is None:
            continue
        before = {name: _ids(_views(addr, fv, record.before[name], preserves)) for name in LINK_REGISTERS}
        after = {name: _ids(_views(addr, fv, record.after[name], preserves)) for name in RESULT_REGISTERS}
        calls.append((call.site_addr, call.target, before, after))
    results = {}
    for ret in return_register_lanes(fv):
        for name in RESULT_REGISTERS:
            value = whole_lane_value(fv.ssa, ret.lanes[name])
            if value is not None:
                results.setdefault(name, set()).update(
                    _value_identities(fv, value, registers, preserves, functions.pops, stack, imports))
    exits = {name: frozenset((source, offset) for kind, source, offset in
                             (view for view in views if view[0] == "through"))
             for name, views in _exits(addr, fv, preserves).items()}
    sites, frame = [], None
    for record in indirect_call_sites(fv):
        fields, kinds, global_addr = set(), set(), None
        for leaf, at in _forms(fv, record.base_value, 0, preserves):
            holder = loaded_field(fv, leaf) if leaf is not None and at == 0 else None
            if holder is None:
                fields.add((None, None))
                kinds.add("vtable not loaded from the receiver")
                continue
            for root, offset in _forms(fv, holder[0], holder[1], preserves):
                fields.add((None if root is None else root.id, offset))
                if global_addr is None and root is not None and root.opc == "LOAD":
                    global_addr = _absolute(fv, root.args[1])
                if root is not None and (entry is None or root.id != entry[1]):
                    frame = frames
                    kinds.add(_root_kind(fv, root, frame))
        before = registers.get(record.site_addr)
        ecx = frozenset() if before is None else _ids(_views(addr, fv, before.before["ECX"], preserves))
        receiver = _value_identities(fv, record.base_value, registers, preserves, functions.pops, stack, imports)
        sites.append((record.site_addr, record.displacement, frozenset(fields), ecx, frozenset(kinds), global_addr,
                      receiver))
    tainted = {_root_identity(write.value) for write in global_writes}
    tainted.update(_root_identity(write.value) for write in field_writes)
    tainted.update(_root_identity(value) for values in call_values.values() for value in values.get("ECX", ()))
    tainted.update(_root_identity(value) for values in call_arguments.values() for found in values.values()
                   for value in found)
    return Facts(addr, None if entry is None else entry[1], tuple(writes), tuple(calls),
                 {name: found for name, found in exits.items() if found}, tuple(sites), None,
                 frozenset(reloads), tuple(global_reads), tuple(global_writes),
                 {name: frozenset(values) for name, values in results.items()}, call_values,
                 call_arguments, tuple(field_writes), frozenset(tainted), functions.pops(addr))


def _root_identity(value):
    while value[0] == "offset":
        value = value[1]
    return value


def _absolute(fv, value):
    whole = _whole(fv.ssa, value)
    if whole.opc == "CONST":
        return whole.data
    if whole.opc in ADDITIVE:
        left, right = (_whole(fv.ssa, arg) for arg in whole.args)
        if right.opc == "CONST":
            base = _absolute(fv, left)
            if base is not None:
                return (base + (right.data if whole.opc == "INT_ADD" else -right.data)) & 0xffffffff
        elif whole.opc == "INT_ADD" and left.opc == "CONST":
            base = _absolute(fv, right)
            if base is not None:
                return (base + left.data) & 0xffffffff
    return None


def _lane_whole(fv, lanes):
    whole = whole_lane_value(fv.ssa, lanes)
    if whole is not None:
        return whole
    resolved = [fv.ssa.resolve(lane) for lane in lanes if lane is not None]
    if len(resolved) == len(lanes) and resolved and all(v.opc == "CONST" and v.data == 0 for v in resolved):
        return resolved[0]
    return None


def _value_identities(fv, value, registers, preserves, pops, stack, imports):
    leaves, complete = _leaves(fv, value, preserves)
    if not complete:
        return (("other", "incomplete"),)
    return tuple(sorted({_value_identity(fv, leaf, registers, preserves, pops, stack, imports) for leaf in leaves}))


def _value_identity(fv, value, registers, preserves, pops, stack, imports):
    memo = stack.setdefault("identities", {})
    key = _whole(fv.ssa, value).id
    if key in memo:
        return memo[key] or ("other", "cycle")
    memo[key] = None
    memo[key] = found = _identity(fv, value, registers, preserves, pops, stack, imports)
    return found


def _identity(fv, value, registers, preserves, pops, stack, imports):
    whole, seen = _whole(fv.ssa, value), set()
    while True:
        if whole.id in seen:
            return ("other", "cycle")
        seen.add(whole.id)
        reload = fv.reloads.get(whole.id)
        if reload is None or reload.target is None or not preserves(reload.target, reload.name):
            break
        if reload.before is not None:
            whole = _whole(fv.ssa, reload.before)
            continue
        record = registers.get(reload.site_addr)
        lane = _lane_whole(fv, record.before[reload.name]) if record is not None else None
        if lane is None:
            break
        whole = lane
    if whole.opc == "PHI":
        found = set()
        for arg in whole.args:
            if arg is not None:
                nested = _value_identity(fv, arg, registers, preserves, pops, stack, imports)
                found.update(nested[1] if nested[0] == "phi" else (nested,))
        return ("phi", tuple(sorted(found)))
    if whole.opc == "CONST":
        return ("null",) if whole.data == 0 else ("const", whole.data)
    reload = fv.reloads.get(whole.id)
    if reload is not None:
        if reload.target is None and reload.site_addr in imports:
            return ("import", reload.site_addr, imports[reload.site_addr], reload.name)
        return ("call", reload.site_addr, reload.target, reload.name)
    if whole.opc in ADDITIVE:
        left, right = (_whole(fv.ssa, arg) for arg in whole.args)
        if right.opc == "CONST":
            inner = left
            delta = right.data if whole.opc == "INT_ADD" else -right.data
        elif whole.opc == "INT_ADD" and left.opc == "CONST":
            inner, delta = right, left.data
        else:
            inner = None
        if inner is not None:
            base = _value_identity(fv, inner, registers, preserves, pops, stack, imports)
            return ("offset", base, delta)
    if whole.opc == "LOAD":
        absolute = _absolute(fv, whole.args[1])
        if absolute is not None:
            return ("global", absolute)
        form = _address_form(fv.ssa, whole.args[1])
        if form is not None:
            base, at = form
            if "frame" not in stack:
                stack["frame"] = frame_ids(fv)
            if base.id in stack["frame"]:
                if "offsets" not in stack:
                    stack["offsets"] = Frame(fv, preserves, pops)
                offset = stack["offsets"].of(whole.args[1])
                if offset is None:
                    return ("stack", at)
                if offset >= 4 and (offset - 4) % 4 == 0:
                    k = (offset - 4) // 4
                    return ("arg_modified", k) if offset in stack.get("written", ()) else ("arg", k)
                return ("stack", offset)
            return ("field", _value_identity(fv, base, registers, preserves, pops, stack, imports), at)
    for name in GPRS:
        entry = whole_lane_value(fv.ssa, fv.entry[name])
        if entry is not None and entry.id == whole.id:
            return ("reg", name)
    return ("other", whole.opc)


def _root_kind(fv, root, frame):
    for name in GPRS:
        value = whole_lane_value(fv.ssa, fv.entry[name])
        if value is not None and value.id == root.id:
            return "entry %s" % name
    site = fv.reloads.get(root.id)
    if site is not None:
        return "%s after %s call" % (site.name, "a direct" if site.target else "an indirect")
    if root.opc == "LOAD":
        form = _address_form(fv.ssa, root.args[1])
        if form is None:
            return "global"
        return "stack slot" if form[0].id in frame else "loaded pointer"
    return {"PHI": "merge", "INT_ADD": "indexed"}.get(root.opc, root.opc.lower())


def _ids(views):
    return frozenset((node[1], offset) for node, offset in views)


def default_jobs():
    return os.cpu_count() or 1


def collect_addrs(settings, preserved, jobs, addrs, abi=True):
    addrs = sorted(set(addrs))
    if not addrs:
        return {}
    parts = [addrs[k:k + 64] for k in range(0, len(addrs), 64)]
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(settings, preserved, abi)) as pool:
        return {facts.addr: facts for batch in pool.map(_batch, parts) for facts in batch}


def scoped(settings, roots, depth, observe=None, jobs=None, abi=True):
    from .dispatch import Image
    from .observed import ObservedEdges, load_observed

    jobs = default_jobs() if jobs is None else jobs
    program = Program(settings)
    image = Image(program)
    observed = load_observed(observe, program) if observe else ObservedEdges()
    roots = {program.containing(addr) or addr for addr in roots}
    scope, facts = set(roots), {}
    literals = {}

    def literal_functions(addr):
        if addr not in literals:
            found = set()
            for at, code in image.literals(addr):
                if code:
                    owner = program.containing(at)
                    if owner is not None:
                        found.add(owner)
            literals[addr] = found
        return literals[addr]

    for _ in range(depth + 1):
        frontier = scope - facts.keys()
        facts.update(collect_addrs(settings, {}, jobs, frontier, abi))
        added = set()
        for addr in frontier:
            found = facts.get(addr)
            if found is None:
                continue
            for site, target, _, _ in found.calls:
                added.add(target)
            for site, _, _, _, _, _, _ in found.sites:
                added |= observed.targets_at(site)
            for caller, _ in image.callers(addr):
                added.add(caller)
            for read in found.global_reads:
                added |= literal_functions(read.addr)
            for write in found.global_writes:
                added |= literal_functions(write.addr)
        scope |= {addr for addr in added if addr in program.functions}
    return facts, scope, observed


FieldAccess = namedtuple("FieldAccess", "site base offset size kind")


def function_field_accesses(functions, addr, preserves):
    functions.lift([addr])
    fv = functions.values.get(addr)
    if fv is None:
        return ()
    registers = {record.site_addr: record for record in call_register_lanes(fv)}
    absolute = absolute_call_targets(fv)
    imports = {site: functions.program.pe.iat_keys[target] for site, target in absolute.items()
               if target in functions.program.pe.iat_keys}
    accesses = memory_accesses(fv)
    frames = frame_ids(fv)
    offsets = Frame(fv, preserves, functions.pops)
    stack = {"frame": frames, "offsets": offsets,
             "written": {at for access in accesses if access.access == "write"
                         if (form := _address_form(fv.ssa, access.address)) is not None
                         and form[0].id in frames and (at := offsets.of(access.address)) is not None}}
    found = []
    for access in accesses:
        for base, constant in _forms(fv, access.base, access.constant, preserves):
            if base is None or base.id in frames:
                continue
            found.append(FieldAccess(access.site_addr,
                                     _value_identity(fv, base, registers, preserves, functions.pops, stack, imports),
                                     constant, access.size, access.access))
    return tuple(found)
