"""Integer p-code SSA with byte-lane aliases and an ordered memory token.

Each listed instruction is initially a block. Register joins include a virtual
entry predecessor, so a backedge to the function entry cannot erase its inputs.
SLEIGH unique storage is instruction-local but byte-addressed like registers.
No memory forwarding, dead-store removal or call-convention assumptions occur.
Unsupported effects fail the entire build; consumers must retain a fallback.
"""
from .lift import BRANCHES
from .simplify import ORDERED


class SSAError(Exception):
    """The lifted function is outside the currently supported SSA subset."""


class Value:
    def __init__(self, ident, opc, size, args=(), data=None):
        self.id, self.opc, self.size = ident, opc, size
        self.args, self.data = tuple(args), data


class Block:
    def __init__(self, index, insn):
        self.index, self.insn = index, insn
        self.phis, self.ops, self.snapshots = [], [], {}
        self.state, self.exit = {}, {}


class SSA:
    def __init__(self):
        self.values, self.blocks, self.inputs, self.aliases = [], {}, {}, {}

    def value(self, opc, size, args=(), data=None):
        v = Value(len(self.values), opc, size, args, data)
        self.values.append(v)
        return v

    def resolve(self, v):
        path = []
        while v.id in self.aliases:
            path.append(v.id)
            v = self.aliases[v.id]
        for ident in path:
            self.aliases[ident] = v
        return v

    def simplify_phis(self):
        """Remove trivial joins, including loop-carried copies of an input."""
        changed = True
        while changed:
            changed = False
            for b in self.blocks.values():
                for phi in b.phis:
                    if phi.id in self.aliases:
                        continue
                    args = {self.resolve(a) for a in phi.args}
                    args.discard(phi)
                    if len(args) == 1:
                        self.aliases[phi.id] = args.pop()
                        changed = True


MEMORY = ("memory", 0)


def build(fir, *, register_groups=(), call_targets=(), indirect_call_symbol=None):
    """Build SSA for reachable integer instructions using the supplied CFG.

    A direct CALL is admitted only when its literal target appears in
    `call_targets`; the caller that binds the target to a C symbol owns that
    mapping. Indirect calls are admitted only when `indirect_call_symbol` names
    the runtime dispatch used for them, and must have a canonical fallthrough;
    the caller/user is responsible for the target value's 32-bit width. Raw
    x87 and intra-instruction branches still need additional state/CFG models
    and are deliberately rejected. A ram varnode is a control target only;
    ordinary guest memory remains behind LOAD/STORE and the memory token.
    """
    call_targets = frozenset(call_targets)
    if not fir.insns or len(fir.succ) != len(fir.insns):
        raise SSAError("empty function or invalid successor table")
    indices = {ins.addr: i for i, ins in enumerate(fir.insns)}
    if len(indices) != len(fir.insns) or fir.addr not in indices:
        raise SSAError("duplicate addresses or missing entry")
    entry = indices[fir.addr]
    reachable, todo = set(), [entry]
    while todo:
        i = todo.pop()
        if i in reachable:
            continue
        if not 0 <= i < len(fir.insns):
            raise SSAError("successor outside instruction table")
        reachable.add(i)
        todo.extend(fir.succ[i])
    keys = {MEMORY}
    for i in sorted(reachable):
        ins = fir.insns[i]
        if ins.x87 or ins.internal_flow:
            raise SSAError("%08x: x87 or intra-instruction control flow" % ins.addr)
        call_ops = [op for op in ins.ops if op.opc == "CALL"]
        if call_ops:
            if len(call_ops) > 1:
                raise SSAError("%08x: multiple direct calls in one instruction" % ins.addr)
            target = call_ops[0].ins
            if len(target) != 1 or target[0][0] != "ram" or target[0][1] not in call_targets:
                raise SSAError("%08x: direct call target is not bound" % ins.addr)
            # The call must continue exactly at its own fallthrough. A CFG that
            # resumes elsewhere is a noreturn/tail/alternate-entry shape this
            # effect model does not cover, so it stays a whole-function fallback.
            fall_index = indices.get(ins.addr + ins.length)
            if fall_index is None or set(fir.succ[i]) != {fall_index}:
                raise SSAError("%08x: direct call lacks its canonical fallthrough" % ins.addr)
        indirect_ops = [op for op in ins.ops if op.opc == "CALLIND"]
        if indirect_ops:
            if indirect_call_symbol is None:
                raise SSAError("%08x: opaque effect CALLIND" % ins.addr)
            if len(indirect_ops) > 1:
                raise SSAError("%08x: multiple indirect calls in one instruction" % ins.addr)
            target = indirect_ops[0].ins
            if (len(target) != 1 or target[0][0] == "ram" or target[0][2] != 4):
                raise SSAError("%08x: indirect call target is not a 32-bit value" % ins.addr)
            # Indirect calls must also continue exactly at their own
            # fallthrough; noreturn/tail/alternate-entry shapes stay fallback.
            fall_index = indices.get(ins.addr + ins.length)
            if fall_index is None or set(fir.succ[i]) != {fall_index}:
                raise SSAError("%08x: indirect call lacks its canonical fallthrough" % ins.addr)
        for op in ins.ops:
            if (op.opc == "BRANCHIND" and i in fir.tables and len(ins.ops) > 0
                    and len(op.ins) == 1 and op.ins[0][0] != "ram" and op.ins[0][2] == 4):
                continue
            if op.opc in ("BRANCHIND", "CALLOTHER") or (
                    op.opc == "CALLIND" and indirect_call_symbol is None):
                raise SSAError("%08x: opaque effect %s" % (ins.addr, op.opc))
            control_target = op.ins[0] if op.opc in ("BRANCH", "CBRANCH", "CALL") and op.ins else None
            for v in (op.out,) + op.ins:
                if v is None:
                    continue
                space, off, size = v
                if not 1 <= size <= 8 or space not in ("register", "unique", "const", "ram"):
                    raise SSAError("%08x: unsupported varnode %r" % (ins.addr, v))
                if space == "ram" and v != control_target:
                    # A data-position ram varnode is SLEIGH's direct absolute
                    # memory operand. `codegen_ir` must turn it into explicit
                    # LOAD/STORE uniques, so a raw one here is a bug, not a value.
                    raise SSAError("%08x: direct ram operand requires normalization" % ins.addr)
                if space == "register":
                    keys.update((space, off + n) for n in range(size))
    keys = sorted(keys)
    s = SSA()
    s.entry = entry
    for key in keys:
        s.inputs[key] = s.value("INPUT", 0 if key == MEMORY else 1, data=key)
    predecessors = {i: [] for i in reachable}
    predecessors[entry].append(-1)
    for i in sorted(reachable):
        for j in sorted(set(fir.succ[i])):
            predecessors[j].append(i)
    for i in sorted(reachable):
        b = Block(i, fir.insns[i])
        s.blocks[i] = b
        for key in keys:
            phi = s.value("PHI", s.inputs[key].size, data=(key, predecessors[i]))
            b.phis.append(phi)
            b.state[key] = phi
    for i, b in s.blocks.items():
        state, temps = dict(b.state), {}

        def emit(opc, size, args=(), data=None):
            v = s.value(opc, size, args, data)
            b.ops.append(v)
            return v

        def read(v):
            space, off, size = v
            if space == "const":
                return s.value("CONST", size, data=off & ((1 << (size * 8)) - 1))
            if space == "ram":
                return s.value("TARGET", size, data=off)
            storage = state if space == "register" else temps
            try:
                lanes = [storage[(space, off + n)] for n in range(size)]
            except KeyError:
                raise SSAError("%08x: unique read before definition %r" % (b.insn.addr, v))
            return lanes[0] if size == 1 else emit("PACK", size, lanes)

        def write(dst, value):
            space, off, size = dst
            if space not in ("register", "unique"):
                raise SSAError("unsupported output space %s" % space)
            storage = state if space == "register" else temps
            for n in range(size):
                storage[(space, off + n)] = value if size == 1 else emit("BYTE", 1, [value], n)

        for op in b.insn.ops:
            if op.opc == "CALL":
                # An opaque direct call: publish the pre-call CPU, invoke the
                # bound callee, then replace every tracked register lane, flag
                # and the memory token with a fresh read of the callee's result
                # state. No caller-side clobber inference occurs.
                before = dict(state)
                value = emit("CALL", 0, [state[MEMORY]], data=op.ins[0][1])
                b.snapshots[value.id] = before
                state[MEMORY] = emit("MEMORY", 0, [value])
                for key in keys:
                    if key != MEMORY:
                        # Pure read of the callee's result state; the call value
                        # keeps it ordered after the call and lets simplify drop
                        # a reload that no later observation uses.
                        state[key] = emit("CALL_RELOAD", s.inputs[key].size, [value], data=key)
                continue
            if op.opc == "CALLIND":
                # An opaque indirect call: publish the pre-call CPU (including
                # the target), dispatch through the runtime, then reload every
                # tracked register lane, flag and the memory token. The target
                # expression was read before the return-address store that the
                # lifted CALL sequence already emitted, so ESP-relative reads
                # keep their instruction order.
                target = read(op.ins[0])
                before = dict(state)
                value = emit("CALLIND", 0, [state[MEMORY], target], data=indirect_call_symbol)
                b.snapshots[value.id] = before
                state[MEMORY] = emit("MEMORY", 0, [value])
                for key in keys:
                    if key != MEMORY:
                        state[key] = emit("CALL_RELOAD", s.inputs[key].size, [value], data=key)
                continue
            args = [read(v) for v in op.ins]
            if op.opc in ORDERED:
                before = dict(state)
                value = emit(op.opc, op.out[2] if op.out else 0,
                             [state[MEMORY]] + args, op.data)
                b.snapshots[value.id] = before
                state[MEMORY] = emit("MEMORY", 0, [value])
            else:
                value = emit(op.opc, op.out[2] if op.out else 0, args, op.data)
            if op.out is not None:
                write(op.out, value)
            if op.opc in ("DIV32", "IDIV32", "STRINGOP"):
                # A returning divide-error handler and the string helper's
                # fault path may leave arbitrary CPU state behind. Reload every
                # tracked lane and flag from the helper's result state, exactly
                # as the eager emitter publishes and reloads live locals.
                for key in keys:
                    if key != MEMORY:
                        state[key] = emit("CALL_RELOAD", s.inputs[key].size, [value], data=key)
        b.exit = state
        branches = [op for op in b.insn.ops if op.opc in BRANCHES]
        if len(branches) > 1:
            raise SSAError("multiple instruction terminators")
        if branches and branches[0].opc in ("BRANCH", "CBRANCH"):
            target = branches[0].ins[0]
            if target[0] != "ram" or target[1] not in indices:
                raise SSAError("external branch requires a tail-call model")
            wanted = {indices[target[1]]}
            if branches[0].opc == "CBRANCH":
                if b.insn.addr + b.insn.length not in indices:
                    raise SSAError("missing conditional fallthrough")
                wanted.add(indices[b.insn.addr + b.insn.length])
            if set(fir.succ[i]) != wanted:
                raise SSAError("branch disagrees with supplied CFG")
        elif branches and branches[0].opc == "RETURN":
            if fir.succ[i]:
                raise SSAError("return has successors")
        elif branches and branches[0].opc == "BRANCHIND":
            if i not in fir.tables or not fir.succ[i]:
                raise SSAError("computed jump without a decoded table")
        elif len(set(fir.succ[i])) != 1:
            raise SSAError("instruction requires one fallthrough successor")
    for i, b in s.blocks.items():
        for phi in b.phis:
            key, preds = phi.data
            phi.args = tuple(s.inputs[key] if p == -1 else s.blocks[p].exit[key] for p in preds)
    if register_groups:
        from .coalesce import registers
        registers(s, register_groups)
    s.simplify_phis()
    return s
