"""Receiver identities across proven register and memory preservation."""
from collections import OrderedDict, defaultdict

from .core import Functions
from .facts import oracle
from .values import (Frame, _address_form, _whole, call_register_lanes, indirect_call_sites,
                     loaded_field, preserved_registers, whole_lane_value)


def _memory_write(mnemonic):
    return mnemonic.startswith(("FST", "FNST", "FIST", "FSAVE", "FNSAVE", "FXSAVE", "XSAVE"))


class _Stack:
    def __init__(self, aliases, fv):
        self.aliases, self.fv = aliases, fv
        self.frame = Frame(fv, aliases.oracle, aliases.functions.pops, aliases.returning_targets)
        states = [fv.entry["ESP"], *(r.after["ESP"] for r in call_register_lanes(fv))]
        self.roots = {form[0].id for state in states
                      if (value := whole_lane_value(fv.ssa, state)) is not None
                      and (form := _address_form(fv.ssa, value)) is not None}

    @property
    def inconsistent(self):
        return self.frame.inconsistent

    def is_stack(self, value, seen=frozenset()):
        form = _address_form(self.fv.ssa, value)
        if form is None or form[0].id in seen:
            return False
        root = _whole(self.fv.ssa, form[0])
        if root.id in self.roots:
            return True
        if root.opc == "PHI":
            args = [arg for arg in root.args if arg is not None and _whole(self.fv.ssa, arg).id != root.id]
            return bool(args) and all(self.is_stack(arg, seen | {root.id}) for arg in args)
        reload = self.fv.reloads.get(root.id)
        if reload is not None and reload.target is not None and reload.before is not None:
            return (self.aliases.oracle(reload.target, reload.name)
                    and self.is_stack(reload.before, seen | {root.id}))
        return False

    def of(self, value):
        return self.frame.of(value) if self.is_stack(value) else None


class Aliases:
    def __init__(self, program, preserved, indirect, abi=True, settings=None):
        self.program = program
        self.functions = Functions(program)
        self.oracle = oracle(preserved, abi)
        self.indirect = indirect
        self.abi = abi
        self.registers = {}
        self.memory = {}
        self.results = {}
        self.recent = OrderedDict()
        self.settings, self.context = settings, None
        self.exceptions = {}

    def closed_exceptions(self, addr, fv):
        if addr not in self.exceptions:
            if fv.dynamic or self.settings is None:
                self.exceptions[addr] = False
            else:
                from driver import Context
                self.context = self.context or Context(self.settings)
                fn, entries = self.context.load(addr)
                self.context.tr.analyze(fn, entries)
                fir = self.context.artifacts.lifted(self.context.tr, self.context.lifter, fn)
                self.exceptions[addr] = not fir.seh or (
                    all(not set(kinds) - {"enter", "leave"} for kinds in fir.seh.values())
                    and all(i in fir.seh_unwind_only for i, kinds in fir.seh.items() if "enter" in kinds))
                self.context.artifacts.local.clear()
        return self.exceptions[addr]

    def values(self, addr):
        self.functions.lift([addr])
        value = self.functions.values.get(addr)
        self.recent[addr] = None
        self.recent.move_to_end(addr)
        while len(self.recent) > 64:
            self.functions.values.pop(self.recent.popitem(last=False)[0], None)
        return value if value is not None and value.ssa is not None else None

    def targets(self, site, operation):
        if operation.opc == "CALL":
            return (operation.data,)
        found = self.indirect.get(site)
        return found[1] if found is not None and found[0] == "call" else ()

    def returning_targets(self, site, operation):
        return tuple(t for t in self.targets(site, operation) if t not in self.program.noreturn)

    def kept(self, target):
        if target in self.program.noreturn:
            return frozenset()
        if target not in self.registers:
            self.registers[target] = frozenset()
            fv = self.values(target)
            if fv is not None and self.closed_exceptions(target, fv):
                kept, assumed, _ = preserved_registers(fv, self.oracle, self.functions.pops)
                self.registers[target] = kept - assumed
        return self.registers[target]

    def readonly(self, target, seen=frozenset()):
        if target in self.program.noreturn:
            return True
        if target in self.memory:
            return self.memory[target][0]
        if target in seen or len(seen) >= 32:
            return False
        fv = self.values(target)
        if fv is None or fv.tail_exit or not self.closed_exceptions(target, fv):
            self.memory[target] = False, "body is unavailable or has an external exit"
            return False
        frame = _Stack(self, fv)
        for block in fv.ssa.blocks.values():
            for operation in block.ops:
                if operation.opc in ("CALL", "CALLIND"):
                    targets = self.targets(block.insn.addr, operation)
                    if not targets or not all(self.readonly(t, seen | {target}) for t in targets):
                        self.memory[target] = False, "call at 0x%08x may write memory" % block.insn.addr
                        return False
                elif operation.opc == "STORE":
                    offset = frame.of(operation.args[1])
                    size = fv.ssa.resolve(operation.args[2]).size
                    if not (self.abi and not frame.inconsistent and offset is not None
                            and -0x100000 < offset and offset + size <= 0):
                        self.memory[target] = False, "store at 0x%08x is not private stack memory" % block.insn.addr
                        return False
                elif operation.opc == "X87_MEM":
                    if _memory_write(operation.data["mnem"]):
                        self.memory[target] = False, "x87 store at 0x%08x" % block.insn.addr
                        return False
                elif operation.opc in ("STRINGOP", "CALLOTHER", "X87_OPAQUE", "SEH"):
                    self.memory[target] = False, "opaque memory effect at 0x%08x" % block.insn.addr
                    return False
        self.memory[target] = True, "writes only private stack memory" if self.abi else "no memory writes"
        return True

    def function(self, addr):
        if addr not in self.results:
            fv = self.values(addr)
            if fv is None or not self.closed_exceptions(addr, fv):
                self.results[addr] = {"objects": [], "barriers": [{"reason": "body or exception flow is open"}]}
            else:
                self.results[addr] = _Function(self, fv).run()
        return self.results[addr]


class _Function:
    def __init__(self, aliases, fv):
        self.aliases, self.fv, self.ssa = aliases, fv, fv.ssa
        self.calls = {r.site_addr: r for r in call_register_lanes(fv)}
        self.locations = {operation.id: block.insn for block in self.ssa.blocks.values() for operation in block.ops}
        self.frame = _Stack(aliases, fv)
        self.canonical = {}
        self.active = set()
        self.barriers = {}
        self.proofs = {}
        self.objects = set()

    def value(self, value):
        value = _whole(self.ssa, value)
        if value.id in self.canonical:
            return self.canonical[value.id]
        if value.id in self.active:
            return value
        self.active.add(value.id)
        result = value
        reload = self.fv.reloads.get(value.id)
        if reload is not None:
            call = self.calls[reload.site_addr]
            targets = self.aliases.targets(reload.site_addr, call.value)
            returning = [t for t in targets if t not in self.aliases.program.noreturn]
            if targets and all(reload.name in self.aliases.kept(t) for t in returning) and reload.before is not None:
                result = self.value(reload.before)
        elif value.opc == "PHI":
            alternatives = {self.value(arg).id: self.value(arg) for arg in value.args
                            if arg is not None and _whole(self.ssa, arg).id != value.id}
            alternatives.pop(value.id, None)
            if len(alternatives) == 1:
                result = next(iter(alternatives.values()))
        elif value.opc == "LOAD":
            previous = self.previous(value)
            if previous is not None:
                result = self.value(previous)
        self.active.remove(value.id)
        self.canonical[value.id] = result
        return result

    def address(self, value):
        form = _address_form(self.ssa, value)
        if form is None:
            return None
        base, offset = form
        return self.value(base).id, offset & 0xffffffff

    def disjoint(self, first, size_first, second, size_second):
        a, b = self.address(first), self.address(second)
        if a is None or b is None or a[0] != b[0]:
            return False
        delta = (a[1] - b[1]) & 0xffffffff
        return size_second <= delta <= 0x100000000 - size_first

    def private_push(self, operation, load):
        if not self.aliases.abi or self.frame.inconsistent:
            return False
        instruction = self.locations[operation.id]
        offset = self.frame.of(operation.args[1])
        size = self.ssa.resolve(operation.args[2]).size
        queried = self.frame.of(load.args[1])
        if queried is not None:
            return offset is not None and (queried + load.size <= offset or offset + size <= queried)
        form = _address_form(self.ssa, load.args[1])
        return (form is not None and not self.frame.is_stack(load.args[1])
                and self.value(form[0]).id in self.objects
                and instruction.mnem in ("PUSH", "CALL") and offset is not None and offset + size <= 0)

    def previous(self, load):
        wanted = self.address(load.args[1])
        if wanted is None:
            return None
        pending, seen, anchors, calls, pushes = [load.args[0]], set(), {}, set(), set()
        while pending:
            token = self.ssa.resolve(pending.pop())
            if token.id in seen:
                continue
            seen.add(token.id)
            if len(seen) > 1024:
                self.barriers[load.id] = {"reason": "memory walk budget"}
                return None
            if token.opc == "PHI":
                pending.extend(arg for arg in token.args if arg is not None)
                continue
            if token.opc != "MEMORY":
                return None
            operation = self.ssa.resolve(token.args[0])
            instruction = self.locations.get(operation.id)
            site = instruction.addr if instruction else 0
            if operation.opc == "LOAD":
                if operation.id != load.id and operation.size == load.size and self.address(operation.args[1]) == wanted:
                    anchor = self.value(operation)
                    anchors[anchor.id] = anchor
                    continue
            elif operation.opc == "STORE":
                size = self.ssa.resolve(operation.args[2]).size
                if not self.disjoint(load.args[1], load.size, operation.args[1], size):
                    if not self.private_push(operation, load):
                        self.barriers[load.id] = {"site": site, "reason": "store may alias the loaded field"}
                        return None
                    pushes.add(site)
            elif operation.opc in ("CALL", "CALLIND"):
                targets = self.aliases.targets(site, operation)
                if not targets or not all(self.aliases.readonly(t) for t in targets):
                    self.barriers[load.id] = {"site": site, "reason": "call may write the loaded field",
                                              "targets": list(targets)}
                    return None
                calls.add(site)
            elif operation.opc == "X87_MEM":
                if _memory_write(operation.data["mnem"]):
                    self.barriers[load.id] = {"site": site, "reason": "x87 store may alias the loaded field"}
                    return None
            elif operation.opc != "X87_REG":
                self.barriers[load.id] = {"site": site, "reason": "opaque memory effect"}
                return None
            if not operation.args:
                return None
            pending.append(operation.args[0])
        if len(anchors) == 1:
            anchor = next(iter(anchors.values()))
            self.proofs[load.id] = {"value": load.id, "same_as": anchor.id,
                                   "read": self.locations[load.id].addr,
                                   "previous_read": self.locations[anchor.id].addr,
                                   "readonly_calls": sorted(calls), "private_stack_pushes": sorted(pushes)}
            return anchor
        return None

    def run(self):
        groups = defaultdict(list)
        sites = indirect_call_sites(self.fv)
        for site in sites:
            pointer = _whole(self.ssa, site.base_value)
            seen = set()
            while pointer.opc == "LOAD" and pointer.id not in seen:
                seen.add(pointer.id)
                form = _address_form(self.ssa, pointer.args[1])
                if form is None:
                    break
                pointer = _whole(self.ssa, form[0])
                self.objects.add(pointer.id)
            if pointer.opc != "LOAD":
                self.objects.add(self.value(pointer).id)
        for site in sites:
            receiver = loaded_field(self.fv, site.base_value)
            if receiver is None:
                continue
            pointer, offset = receiver
            nominal, vptr = self.value(pointer), self.value(site.base_value)
            self.objects.add(nominal.id)
            key = nominal.id, vptr.id, offset
            groups[key].append({"site": site.site_addr, "slot": site.displacement})
        body = self.aliases.program.body(self.fv.addr).ir
        successors = {ins.addr: [body.insns[j].addr for j in body.succ[i]]
                      for i, ins in enumerate(body.insns)}
        for uses in groups.values():
            for use in uses:
                use["requirements"] = []
                for other in uses:
                    pending, seen = [self.fv.addr], set()
                    while pending:
                        at = pending.pop()
                        if at in seen or at == other["site"]:
                            continue
                        seen.add(at)
                        pending.extend(successors.get(at, ()))
                    if use["site"] not in seen:
                        use["requirements"].append(other["site"])
        objects = [{"nominal": [self.fv.addr, nominal], "vptr": vptr, "vptr_offset": offset,
                    "uses": sorted(uses, key=lambda row: row["site"]),
                    "abi": self.aliases.abi}
                   for (nominal, vptr, offset), uses in groups.items()]
        return {"objects": objects, "proofs": list(self.proofs.values()),
                "barriers": [{"value": value, "read": self.locations[value].addr, **reason}
                             for value, reason in self.barriers.items()],
                "stack_frame_isolation": self.aliases.abi,
                "stack_conflicts": self.frame.inconsistent}
