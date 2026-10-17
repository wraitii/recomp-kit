"""SSA values of one function, shaped for the type-inference core.

A function is built from `Program.body` once. When `ir.ssa.build` admits the
body, `FunctionValues.ssa` holds the SSA and `entry` maps each general-purpose
register to its entry lane values (INPUT values). When the body is rejected,
`ssa` is None, `fallback` names the reason, and `entry` maps each register to
its lane keys `("register", offset)` over the raw per-instruction p-code in
`insns`; the SSA queries refuse rather than guess.
"""
import weakref
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field

from ir.guest import X86_32
from ir.lift import LiftError
from ir.normalize import codegen_ir
from ir.preservation import forward
from ir.simplify import canonicalize
from ir.ssa import SSAError, build
from program import TranslateError


GPRS = X86_32.names("gpr")
INDIRECT_SYMBOL = "typeinf"
ADDITIVE = ("INT_ADD", "INT_SUB")
FRAME_REGISTERS = ("ESP", "EBP")
FRAME_OPS = ("COPY", "PACK", "BYTE", "INT_ADD", "INT_SUB", "PHI")
MAX_ADDRESS_DEPTH = 32
MAX_FIELD_OFFSET = 1 << 16
MAX_PHI_LEAVES = 256
MAX_STORE_CHAIN = 64
MAX_MEMORY_WALK = 4096

DirectCall = namedtuple("DirectCall", "site_addr target")
IndirectCall = namedtuple("IndirectCall", "site_addr base_value displacement")
CallRegisters = namedtuple("CallRegisters", "site_addr opc before after value")
ReturnRegisters = namedtuple("ReturnRegisters", "site_addr lanes")
MemoryAccess = namedtuple("MemoryAccess", "site_addr access size address value base constant value_base value_offset")
Reload = namedtuple("Reload", "target name before")

_WHOLE = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class FunctionValues:
    addr: int
    insns: tuple
    index: dict
    registers: dict
    entry: dict
    ssa: object = None
    fallback: str = None
    reloads: dict = field(default_factory=dict)
    tail_exit: bool = False
    pops: int = None


def function_values(program, addr):
    if addr not in program.functions:
        raise TranslateError("%08x: not a function in the code map" % addr)
    try:
        body = program.body(addr)
    except (TranslateError, LiftError) as error:
        raise TranslateError("%08x: body cannot be built: %s" % (addr, error)) from error
    fir = body.ir
    for k, target in body.exits:
        fir.exits.setdefault(k, []).append(target)
    lanes = _register_lanes(program.lifter)
    index = {ins.addr: k for k, ins in enumerate(fir.insns)}
    tail_exit = bool(fir.import_tails or fir.exits) or any(op.opc == "TAIL" for ins in fir.insns for op in ins.ops)
    pops = stack_pops(fir, tail_exit)
    try:
        ssa = _build(fir, program.lifter, lanes)
        canonicalize(ssa)
        forward(ssa)
    except SSAError as error:
        return FunctionValues(addr, tuple(fir.insns), index, lanes, lanes, fallback=str(error), tail_exit=tail_exit,
                              pops=pops)
    entry = {name: tuple(ssa.inputs.get(key) for key in keys) for name, keys in lanes.items()}
    return FunctionValues(addr, tuple(fir.insns), index, lanes, entry, ssa=ssa, reloads=_reload_sites(ssa, lanes),
                          tail_exit=tail_exit, pops=pops)


def stack_pops(fir, tail_exit):
    found = set()
    for ins in fir.insns:
        raw = bytes(ins.raw or b"").lstrip(b"\xf3")
        if raw[:1] == b"\xc3":
            found.add(0)
        elif raw[:1] == b"\xc2" and len(raw) == 3:
            found.add(int.from_bytes(raw[1:], "little"))
    return found.pop() if len(found) == 1 and not tail_exit else None


def indirect_call_sites(fv):
    ssa = _require_ssa(fv)
    records = []
    for site, value in _calls(ssa):
        if value.opc == "CALLIND":
            found = loaded_field(fv, value.args[1])
            if found is not None:
                records.append(IndirectCall(site, *found))
    return sorted(records, key=lambda record: record.site_addr)


def absolute_call_targets(fv):
    ssa = _require_ssa(fv)
    found = {}
    for site, value in _calls(ssa):
        load = _whole(ssa, value.args[1]) if value.opc == "CALLIND" else None
        if load is not None and load.opc == "LOAD":
            address = _whole(ssa, load.args[1])
            if address.opc == "CONST":
                found[site] = address.data
    return found


def direct_call_sites(fv):
    ssa = _require_ssa(fv)
    calls = [DirectCall(site, value.data) for site, value in _calls(ssa) if value.opc == "CALL"]
    calls += [DirectCall(source.insn.addr, _whole(ssa, value.args[0]).data) for source, value in _tails(ssa)]
    return sorted(calls, key=lambda record: record.site_addr)


def memory_accesses(fv):
    ssa = _require_ssa(fv)
    records = [_memory_access(ssa, block.insn.addr, op)
               for block in ssa.blocks.values() for op in block.ops if op.opc in ("LOAD", "STORE")]
    return sorted(records, key=lambda record: record.site_addr)


def frame_ids(fv):
    ssa = _require_ssa(fv)
    users = _users(ssa)
    frame = set()
    for name in FRAME_REGISTERS:
        frame |= _register_ids(ssa, fv.entry[name])
    grew = True
    while grew:
        frame = _close(frame, users)
        grew = _add_call_reloads(fv, ssa, frame)
    return frame


def _users(ssa):
    users = defaultdict(list)
    for value in ssa.values:
        resolved = ssa.resolve(value)
        for arg in resolved.args:
            users[ssa.resolve(arg).id].append(resolved)
    return users


def _register_ids(ssa, lanes):
    values = [ssa.resolve(lane) for lane in lanes if lane is not None]
    ids = {value.id for value in values}
    if values and len(values) == len(lanes):
        whole = _whole_lanes(ssa, values)
        if whole is not None:
            ids.add(whole.id)
    return ids


def _close(frame, users):
    closed, pending = set(frame), list(frame)
    while pending:
        for user in users[pending.pop()]:
            if user.id not in closed and user.opc in FRAME_OPS:
                closed.add(user.id)
                pending.append(user.id)
    return closed


def _add_call_reloads(fv, ssa, frame):
    added = False
    for block, value in _call_ops(ssa):
        for name in FRAME_REGISTERS:
            before = _register_ids(ssa, [block.snapshots[value.id].get(key) for key in fv.registers[name]])
            after = _register_ids(ssa, [block.reloads[value.id].get(key) for key in fv.registers[name]])
            if before & frame and not after <= frame:
                frame |= after
                added = True
    return added


def call_register_lanes(fv):
    ssa = _require_ssa(fv)
    records = [CallRegisters(block.insn.addr, value.opc, _gpr_lanes(fv, block.snapshots[value.id]),
                             _gpr_lanes(fv, block.reloads[value.id]), value)
               for block, value in _call_ops(ssa)]
    records += [CallRegisters(source.insn.addr, value.opc, _gpr_lanes(fv, source.exit), _gpr_lanes(fv, {}), value)
                for source, value in _tails(ssa)]
    return sorted(records, key=lambda record: record.site_addr)


def stack_arguments(fv, record, count):
    ssa, call = _require_ssa(fv), record.value
    esp = whole_lane_value(ssa, record.before["ESP"])
    form = None if esp is None else _address_form(ssa, esp)
    if form is None:
        return (None,) * count
    root, at = form
    wanted = {(root.id, at + 4 + 4 * k): k for k in range(count)}
    found = [None] * count
    token = call.args[0]
    for _ in range(MAX_STORE_CHAIN):
        token = ssa.resolve(token)
        if token.opc != "MEMORY":
            break
        op = ssa.resolve(token.args[0])
        if op.opc not in ("STORE", "LOAD"):
            break
        if op.opc == "STORE":
            stored = _address_form(ssa, op.args[1])
            k = None if stored is None else wanted.pop((stored[0].id, stored[1]), None)
            if k is not None and ssa.resolve(op.args[2]).size == 4:
                found[k] = _stored(ssa, op.args[2])
        token = op.args[0]
    return tuple(found)


def return_register_lanes(fv):
    ssa = _require_ssa(fv)
    records = [ReturnRegisters(block.insn.addr, _gpr_lanes(fv, block.exit))
               for block in ssa.blocks.values() if _returns(block)]
    return sorted(records, key=lambda record: record.site_addr)


def _register_lanes(lifter):
    lanes = {}
    for name in GPRS:
        _, offset, size = lifter.register(name)
        lanes[name] = tuple(("register", offset + n) for n in range(size))
    return lanes


def _build(fir, lifter, lanes):
    cgi = codegen_ir(fir, lifter, {target for targets in fir.exits.values() for target in targets})
    targets = {op.ins[0][1] for ins in cgi.insns for op in ins.ops
               if op.opc == "CALL" and op.ins[0][0] == "ram"}
    return build(cgi, register_groups=list(lanes.values()), call_targets=targets,
                 indirect_call_symbol=INDIRECT_SYMBOL)


def _require_ssa(fv):
    if fv.ssa is None:
        raise ValueError("%08x: no SSA for this function (%s)" % (fv.addr, fv.fallback))
    return fv.ssa


def _call_ops(ssa):
    for block in ssa.blocks.values():
        for value in block.ops:
            if value.opc in ("CALL", "CALLIND"):
                yield block, value


def _tails(ssa):
    for block in ssa.blocks.values():
        for value in block.ops:
            if value.opc == "TAIL":
                target = block.insn.addr
                sources = [source for source in ssa.blocks.values() if source is not block and any(
                    op.opc in ("BRANCH", "CBRANCH") and op.ins and op.ins[0][:2] == ("ram", target)
                    for op in source.insn.ops)]
                for source in sources or [block]:
                    yield source, value


def _calls(ssa):
    for block, value in _call_ops(ssa):
        yield block.insn.addr, value


def _gpr_lanes(fv, state):
    return {name: tuple(state.get(key) for key in keys) for name, keys in fv.registers.items()}


def _returns(block):
    return any(op.opc == "RETURN" for op in block.insn.ops)


def whole_lane_value(ssa, lanes):
    if not lanes or any(lane is None for lane in lanes):
        return None
    whole = _whole_lanes(ssa, lanes)
    return None if whole is None else _whole(ssa, whole)


def _whole(ssa, value):
    value = ssa.resolve(value)
    cache = _WHOLE.setdefault(ssa, {})
    if value.id not in cache:
        cache[value.id] = _whole_uncached(ssa, value)
    return cache[value.id]


def _whole_uncached(ssa, value):
    if value.opc == "COPY":
        return _whole(ssa, value.args[0])
    if value.opc == "PACK":
        inner = _whole_lanes(ssa, value.args)
        if inner is not None:
            return _whole(ssa, inner)
    return value


def _whole_lanes(ssa, lanes):
    lanes = [ssa.resolve(lane) for lane in lanes]
    first = lanes[0]
    if first.opc == "BYTE" and all(lane.opc == "BYTE" and lane.data == n for n, lane in enumerate(lanes)):
        sources = {_whole(ssa, lane.args[0]).id for lane in lanes}
        if len(sources) == 1:
            return ssa.resolve(first.args[0])
    return None


def preserved_registers(fv, preserves, pops):
    ssa = _require_ssa(fv)
    returns = return_register_lanes(fv)
    offsets = Frame(fv, preserves, pops)
    if not returns or fv.tail_exit:
        return frozenset(), frozenset(), offsets.balanced
    kept, assumed = set(), set()
    for name in GPRS:
        if name == "ESP":
            continue
        if all(lane is None for lane in fv.entry[name]):
            if all(preserves(value.data if value.opc == "CALL" else None, name) for _, value in _call_ops(ssa)):
                kept.add(name)
            continue
        entry = whole_lane_value(ssa, fv.entry[name])
        if entry is None:
            continue
        results = [_restores(fv, ret.lanes[name], entry, preserves, offsets) for ret in returns]
        if all(results):
            kept.add(name)
            if "slot" in results:
                assumed.add(name)
    return frozenset(kept), frozenset(assumed), offsets.balanced


def _restores(fv, lanes, entry, preserves, offsets):
    value = whole_lane_value(fv.ssa, lanes)
    if value is None:
        return False
    leaves, complete = _leaves(fv, value, preserves)
    if not complete:
        return False
    result = True
    for leaf in leaves:
        if leaf.id == entry.id:
            continue
        if leaf.opc == "LOAD" and leaf.size == 4 and _slot_holds(fv, leaf, offsets, entry):
            result = "slot"
            continue
        return False
    return result


def _slot_holds(fv, load, offsets, entry):
    ssa = fv.ssa
    slot = offsets.of(load.args[1])
    if slot is None:
        return False
    pending, seen = [load.args[0]], set()
    while pending:
        token = ssa.resolve(pending.pop())
        if token.id in seen:
            continue
        seen.add(token.id)
        if len(seen) > MAX_MEMORY_WALK:
            return False
        if token.opc == "PHI":
            pending.extend(arg for arg in token.args if arg is not None)
            continue
        if token.opc != "MEMORY":
            return False
        op = ssa.resolve(token.args[0])
        if op.opc == "STORE":
            at = offsets.of(op.args[1])
            size = ssa.resolve(op.args[2]).size
            if at is not None and at < slot + 4 and slot < at + size:
                if at != slot or size != 4 or _whole(ssa, op.args[2]).id != entry.id:
                    return False
                continue
        if not op.args:
            return False
        pending.append(op.args[0])
    return True


class Frame:
    ZERO = "entry"

    def __init__(self, fv, preserves, pops):
        ssa = fv.ssa
        self.ssa, self.parent, self.inconsistent, self.esp_reloads = ssa, {}, 0, set()
        esp = whole_lane_value(ssa, fv.entry["ESP"])
        if esp is not None:
            self._union(esp.id, self.ZERO, 0)
        for block, value in _call_ops(ssa):
            target = value.data if value.opc == "CALL" else None
            for name in FRAME_REGISTERS:
                keys = fv.registers[name]
                reload = whole_lane_value(ssa, [block.reloads[value.id].get(key) for key in keys])
                before = whole_lane_value(ssa, [block.snapshots[value.id].get(key) for key in keys])
                form = None if reload is None or before is None else _address_form(ssa, before)
                if form is None:
                    continue
                if name == "ESP":
                    self.esp_reloads.add(reload.id)
                if name == "ESP" and target is not None and pops(target) is not None:
                    self._union(reload.id, form[0].id, form[1] + 4 + pops(target))
                elif name == "EBP" and preserves(target, "EBP"):
                    self._union(reload.id, form[0].id, form[1])
        for value in ssa.values:
            if value.opc == "PHI" and value.size == 4 and ssa.resolve(value) is value:
                for arg in value.args:
                    form = None if arg is None else _address_form(ssa, arg)
                    if form is not None:
                        self._union(value.id, form[0].id, form[1])
        self.balanced = fv.pops is not None and not fv.tail_exit
        if self.balanced:
            for ret in return_register_lanes(fv):
                exit_esp = whole_lane_value(ssa, ret.lanes["ESP"])
                form = None if exit_esp is None else _address_form(ssa, exit_esp)
                if form is None or not self._stack_class(form[0].id):
                    self.balanced = False
                    continue
                before = self.inconsistent
                self._union(form[0].id, self.ZERO, 4 + fv.pops - form[1])
                self.balanced &= self.inconsistent == before

    def _stack_class(self, node):
        root = self._find(node)[0]
        return root == self.ZERO or any(self._find(reload)[0] == root for reload in self.esp_reloads)

    def _find(self, node):
        delta = 0
        path = []
        while node in self.parent:
            path.append((node, delta))
            parent, step = self.parent[node]
            delta += step
            node = parent
        for child, before in path:
            self.parent[child] = (node, delta - before)
        return node, delta

    def _union(self, first, second, delta):
        first_root, first_delta = self._find(first)
        second_root, second_delta = self._find(second)
        if first_root == second_root:
            self.inconsistent += first_delta != second_delta + delta
        elif first_root == self.ZERO:
            self.parent[second_root] = (first_root, first_delta - second_delta - delta)
        else:
            self.parent[first_root] = (second_root, second_delta + delta - first_delta)

    def of(self, value):
        form = _address_form(self.ssa, value)
        if form is None:
            return None
        root, delta = self._find(form[0].id)
        return delta + form[1] if root == self.ZERO else None


def _reload_sites(ssa, registers):
    sites = {}
    for block, value in _call_ops(ssa):
        for name, keys in registers.items():
            reload = whole_lane_value(ssa, [block.reloads[value.id].get(key) for key in keys])
            if reload is None or reload.opc != "CALL_RELOAD":
                continue
            before = whole_lane_value(ssa, [block.snapshots[value.id].get(key) for key in keys])
            target = value.data if value.opc == "CALL" else None
            sites[reload.id] = Reload(target, name, before)
    return sites


def _leaves(fv, value, preserves):
    ssa = fv.ssa
    leaves, stack, seen = [], [value], set()
    while stack:
        if len(seen) >= MAX_PHI_LEAVES:
            return leaves, False
        current = _whole(ssa, stack.pop())
        if current.id in seen:
            continue
        seen.add(current.id)
        if current.opc == "PHI":
            stack.extend(arg for arg in current.args if arg is not None)
            continue
        site = fv.reloads.get(current.id)
        if site is not None and site.before is not None and preserves(site.target, site.name):
            stack.append(site.before)
        else:
            leaves.append(current)
    return leaves, True


def value_views(fv, value, preserves):
    _require_ssa(fv)
    forms = (_address_form(fv.ssa, leaf) for leaf in _leaves(fv, value, preserves)[0])
    return [form for form in forms if form is not None]


def lane_views(fv, lanes, preserves):
    value = whole_lane_value(fv.ssa, lanes)
    return [] if value is None else value_views(fv, value, preserves)


def loaded_field(fv, value):
    ssa = _require_ssa(fv)
    load = _whole(ssa, value)
    if load.opc != "LOAD":
        return None
    return _address_form(ssa, load.args[1])


def _memory_access(ssa, site, op):
    address = _whole(ssa, op.args[1])
    base, constant = _address_form(ssa, address) or (None, None)
    if op.opc == "LOAD":
        value_base, value_offset = _address_form(ssa, op) or (None, None)
        return MemoryAccess(site, "read", op.size, address, op, base, constant, value_base, value_offset)
    data = op.args[2]
    stored = _stored(ssa, data)
    value_base, value_offset = _address_form(ssa, stored) or (None, None)
    return MemoryAccess(site, "write", ssa.resolve(data).size, address, stored, base, constant, value_base, value_offset)


def _address_form(ssa, value, depth=0):
    value = _whole(ssa, value)
    if value.opc in ADDITIVE:
        return _additive_form(ssa, value, depth) if depth < MAX_ADDRESS_DEPTH else None
    if value.opc == "CONST":
        return None
    return value, 0


def _additive_form(ssa, op, depth):
    left, right = (_whole(ssa, arg) for arg in op.args)
    if right.opc == "CONST":
        sign, constant, inner = (-1 if op.opc == "INT_SUB" else 1), right, left
    elif left.opc == "CONST" and op.opc == "INT_ADD":
        sign, constant, inner = 1, left, right
    else:
        return None
    if abs(_signed32(constant.data)) >= MAX_FIELD_OFFSET:
        return None
    form = _address_form(ssa, inner, depth + 1)
    if form is None:
        return None
    return form[0], form[1] + sign * _signed32(constant.data)


def _signed32(data):
    return data - (1 << 32) if data >= 1 << 31 else data


def _stored(ssa, data):
    whole = _whole(ssa, data)
    return whole if whole.size == ssa.resolve(data).size else ssa.resolve(data)


def describe(fv, value, depth=0):
    ssa = fv.ssa
    value = _whole(ssa, value)
    if depth > 6:
        return "..."
    for name in GPRS:
        entry = whole_lane_value(ssa, fv.entry[name])
        if entry is not None and entry.id == value.id:
            return name
    form = _address_form(ssa, value)
    if form is not None and form[0].id != value.id:
        return "%s%+#x" % (describe(fv, form[0], depth + 1), form[1])
    if value.opc == "LOAD":
        return "[%s]" % describe(fv, value.args[1], depth + 1)
    site = fv.reloads.get(value.id)
    if site is not None:
        return "%s@after %s" % (site.name, "%08x" % site.target if site.target else "indirect")
    if value.opc == "PHI":
        return "phi(%s)" % ", ".join(describe(fv, arg, depth + 1) for arg in value.args[:3] if arg is not None)
    if value.opc == "CONST":
        return "%#x" % value.data
    return "%s#%d" % (value.opc, value.id)
