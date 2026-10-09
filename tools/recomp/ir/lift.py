"""Lift x86 instructions to raw p-code with TOP-relative x87 slots.

SLEIGH describes each instruction as a short sequence of p-code operations
over varnodes (space, offset, size). Every effect is explicit, including each
arithmetic flag, so later analyses need no per-mnemonic tables.

Varnodes are plain tuples ``(space, offset, size)``. Spaces are SLEIGH's
``register``, ``unique``, ``const`` and ``ram``, plus two introduced here:

``stin``  ST(i) as it was before the instruction.
``stout`` ST(i) as it is after the instruction.

SLEIGH models ST0..ST7 as eight fixed registers and physically shifts all of
them on every push and pop. That is faithful but hides the stack discipline.
`normalize_x87` symbolically executes each x87 instruction's shifts and keeps
only real computations, so an instruction becomes "reads some pre-instruction
slots, writes some post-instruction slots, moves TOP by `x87_delta`". Analyses
that know the stack depth at an instruction can then name every slot
statically, independent of the absolute TOP value.

Known SLEIGH gaps relevant to code generation (not to convention inference),
to be replaced by kit semantics before any emitter relies on them: FIST/FISTP
and FRNDINT ignore the control word's rounding mode; FPREM does not truncate
its quotient; FCOM-family and FNCLEX writes replace the whole status word
(losing TOP and exception bits); FXAM does not classify; tags are absent.
"""
import pypcode

ST_BASE = 0x1100
ST_STRIDE = 0x10
ST_SIZE = 10

#: Diagnostic FPU state with no consumer in translated code. The agreed
#: performance mode lets diagnostic snapshots differ, so writes are dropped.
DIAGNOSTIC_REGISTERS = frozenset(("FPUInstructionPointer", "FPUDataPointer",
                                  "FPULastInstructionOpcode", "FPUPointerSelector",
                                  "FPUDataSelector"))

#: Binary operations whose result is a constant when both inputs are the
#: same varnode: `XOR EBX,EBX`, `SUB EAX,EAX` and `SBB EAX,EAX` then read
#: nothing but the carry.
SAME_OPERAND_ZERO = frozenset(("INT_XOR", "INT_SUB", "INT_LESS", "INT_SLESS", "INT_NOTEQUAL",
                               "INT_SBORROW", "BOOL_XOR"))
SAME_OPERAND_ONE = frozenset(("INT_EQUAL", "INT_LESSEQUAL", "INT_SLESSEQUAL"))

BRANCHES = frozenset(("BRANCH", "CBRANCH", "BRANCHIND", "CALL", "CALLIND", "RETURN"))


class LiftError(Exception):
    """An instruction the lifter cannot represent faithfully."""


class Op(object):
    """One p-code operation. `out` is a varnode or None; `ins` a tuple."""
    __slots__ = ("opc", "out", "ins", "data")

    def __init__(self, opc, out, ins, data=None):
        self.opc = opc
        self.out = out
        self.ins = tuple(ins)
        self.data = data

    def __repr__(self):
        lhs = "%s = " % (fmt_var(self.out),) if self.out else ""
        return "%s%s %s" % (lhs, self.opc, ", ".join(fmt_var(v) for v in self.ins))


class Insn(object):
    """A lifted instruction.

    `ops` are p-code operations in execution order. Branch inputs naming
    ``ram`` are guest addresses; a ``const`` branch target is a relative index
    into `ops` (SLEIGH's intra-instruction control flow, e.g. REP prefixes),
    and `internal_flow` records whether any exists. `x87_delta` is the net
    TOP movement in slots (+1 per push).
    """
    __slots__ = ("addr", "length", "mnem", "ops", "x87_delta", "x87", "internal_flow",
                 "userops", "raw", "cc")

    def __init__(self, addr, length, mnem, ops, x87_delta, x87, internal_flow, userops, raw=None):
        self.addr = addr
        self.length = length
        self.mnem = mnem
        self.ops = ops
        self.x87_delta = x87_delta
        self.x87 = x87
        self.internal_flow = internal_flow
        self.userops = userops
        self.raw = raw
        # Optional lazy-flag producer metadata: kind, size and the primary
        # p-code result varnode, set by the C emitter's codegen corrections.
        self.cc = None

    def __repr__(self):
        return "%08x %s" % (self.addr, self.mnem)


def fmt_var(v):
    if v is None:
        return "_"
    space, off, size = v
    if space == "register":
        return REGISTER_NAMES.get((off, size), "reg[%x:%d]" % (off, size))
    if space == "const":
        return "0x%x" % off
    if space in ("stin", "stout"):
        return "%s(%d)" % (space, off)
    return "%s[%x:%d]" % (space, off, size)


REGISTER_NAMES = {}


def st_index(v):
    """ST(i) index for a SLEIGH ST register varnode, else None."""
    if v is None or v[0] != "register" or v[2] != ST_SIZE:
        return None
    rel = v[1] - ST_BASE
    if 0 <= rel < 8 * ST_STRIDE and rel % ST_STRIDE == 0:
        return rel // ST_STRIDE
    return None


class Lifter(object):
    """Lift instruction bytes to `Insn` objects through one SLEIGH context."""

    def __init__(self, language="x86:LE:32:default"):
        self.ctx = pypcode.Context(language)
        regs = self.ctx.registers
        if not REGISTER_NAMES:
            for name, vn in regs.items():
                REGISTER_NAMES.setdefault((vn.offset, vn.size), name)
        self.diagnostic = {(regs[n].offset, regs[n].size) for n in DIAGNOSTIC_REGISTERS
                           if n in regs}
        self._unique = 0

    def register(self, name):
        vn = self.ctx.registers[name]
        return ("register", vn.offset, vn.size)

    def lengths(self, addr, data):
        """Instruction lengths SLEIGH decodes across `data`, which it must tile exactly."""
        try:
            marks = [op.inputs[0].size for op in self.ctx.translate(data, addr).ops
                     if op.opcode == pypcode.OpCode.IMARK]
        except Exception as e:
            raise LiftError("%08x: SLEIGH decode failed: %s" % (addr, e))
        if sum(marks) != len(data):
            raise LiftError("%08x: SLEIGH decodes %d of %d bytes" % (addr, sum(marks), len(data)))
        return marks

    def lift(self, addr, data, mnem=None):
        """Lift exactly `data` (one listed instruction) at guest `addr`.

        A listing boundary can hold more than one SLEIGH instruction: Ghidra
        folds WAIT into a following FNSTSW/FNSTCW. Those are lifted in order
        and concatenated.
        """
        ops, mnems, delta, x87, flow, userops = [], [], 0, False, False, []
        pos = 0
        while pos < len(data):
            try:
                tx = self.ctx.translate(data[pos:], addr + pos, max_instructions=1)
            except Exception as e:  # pypcode raises its own decode error types
                raise LiftError("%08x: SLEIGH decode failed: %s" % (addr + pos, e))
            raw = tx.ops
            if not raw or raw[0].opcode != pypcode.OpCode.IMARK:
                raise LiftError("%08x: no instruction mark" % (addr + pos))
            size = raw[0].inputs[0].size
            if size <= 0 or pos + size > len(data):
                raise LiftError("%08x: SLEIGH length %d overruns listed length %d"
                                % (addr + pos, size, len(data)))
            if mnem is None:
                mnems.append(self.ctx.disassemble(data[pos:pos + size], addr + pos,
                                                  max_instructions=1).instructions[0].mnem)
            sub = [self.convert(op, userops) for op in raw[1:]]
            sub = [op for op in sub if op is not None]
            # REP prefixes loop back to their own address and exit to the next
            # one through ram targets; both are flow inside this instruction.
            sub_flow = any(op.opc in ("BRANCH", "CBRANCH") and (
                op.ins[0][0] == "const"
                or (op.ins[0][0] == "ram" and addr <= op.ins[0][1] <= addr + len(data)))
                for op in sub)
            if any(st_index(v) is not None for op in sub for v in (op.out,) + op.ins):
                if sub_flow:
                    raise LiftError("%08x: x87 instruction with internal control flow"
                                    % (addr + pos))
                sub, d = normalize_x87(self.rename_uniques(sub), self.fresh_unique)
                delta += d
                x87 = True
            if sub_flow and ops:
                raise LiftError("%08x: internal control flow after a folded prefix" % addr)
            flow = flow or sub_flow
            ops.extend(sub)
            pos += size
        return Insn(addr, len(data), mnem or " ".join(mnems), ops, delta, x87, flow, userops,
                    bytes(data))

    def convert(self, op, userops):
        opc = op.opcode.name
        out = op.output
        out = (out.space.name, out.offset, out.size) if out is not None else None
        if out is not None and out[0] == "register" and (out[1], out[2]) in self.diagnostic:
            return None
        ins = [(v.space.name, v.offset, v.size) for v in op.inputs]
        if opc in ("LOAD", "STORE"):
            ins = ins[1:]  # drop the address-space id; x86-32 has only ram
        elif opc == "CALLOTHER":
            userops.append(op.inputs[0].getUserDefinedOpName())
        elif len(ins) == 2 and ins[0] == ins[1] and ins[0][0] != "const":
            if opc in SAME_OPERAND_ZERO:
                return Op("COPY", out, [("const", 0, out[2])])
            if opc in SAME_OPERAND_ONE:
                return Op("COPY", out, [("const", 1, out[2])])
        return Op(opc, out, ins)

    def fresh_unique(self, size):
        self._unique += 1
        return ("unique", 0x10000000 + self._unique * 0x10, size)

    def rename_uniques(self, ops):
        """Give every unique definition a fresh name (straight-line code only),
        so symbolic slot values cannot be clobbered by SLEIGH reusing a unique."""
        names = {}
        out = []
        for op in ops:
            ins = tuple(names.get(v, v) if v[0] == "unique" else v for v in op.ins)
            dst = op.out
            if dst is not None and dst[0] == "unique":
                names[dst] = self.fresh_unique(dst[2])
                dst = names[dst]
            out.append(Op(op.opc, dst, ins))
        return out


def normalize_x87(ops, fresh):
    """Replace SLEIGH's physical ST shifts with TOP-relative slot accesses.

    Returns (ops, delta). Reads of ST(j) whose value is still the
    pre-instruction ST(j) become ``stin(j)``; computed values go to fresh
    uniques; at the end, every post-instruction slot whose value is not simply
    the shifted pre-instruction slot is written as ``stout(i)``. Post slots
    exposed by a pop are dropped: in a TOP model they are popped residue.
    """
    st = [("pre", i) for i in range(8)]
    alias = {}  # unique -> ("pre", j) when the unique is a plain copy of a slot
    out = []

    def value(v):
        k = st_index(v)
        if k is not None:
            s = st[k]
            return ("stin", s[1], ST_SIZE) if s[0] == "pre" else s[1]
        return v

    for op in ops:
        ins = tuple(value(v) for v in op.ins)
        k = st_index(op.out)
        if op.opc == "COPY" and op.out is not None and op.out[0] == "unique" \
                and ins[0][0] == "stin":
            alias[op.out] = ("pre", ins[0][1])
            continue
        if k is None:
            out.append(Op(op.opc, op.out, ins))
            continue
        if op.opc == "COPY":
            src = ins[0]
            if src[0] == "stin":
                st[k] = ("pre", src[1])
                continue
            if src in alias:
                st[k] = alias[src]
                continue
            if src[0] in ("unique", "const"):
                st[k] = ("tmp", src)
                continue
        t = fresh(ST_SIZE)
        out.append(Op(op.opc, t, ins))
        st[k] = ("tmp", t)
    # Any surviving read of an aliased unique must see the slot it copied.
    for n, op in enumerate(out):
        if any(v in alias for v in op.ins):
            out[n] = Op(op.opc, op.out, tuple(
                ("stin", alias[v][1], ST_SIZE) if v in alias else v for v in op.ins))
    best, delta = -1, 0
    for d in (0, -1, 1, -2):
        score = sum(1 for i in range(8) if st[i] == ("pre", i - d))
        if score > best:
            best, delta = score, d
    for i in range(8):
        if delta < 0 and i >= 8 + delta:
            continue
        s = st[i]
        if s == ("pre", i - delta):
            continue
        src = ("stin", s[1], ST_SIZE) if s[0] == "pre" else s[1]
        out.append(Op("COPY", ("stout", i, ST_SIZE), (src,)))
    return out, delta
