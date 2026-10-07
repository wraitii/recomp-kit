"""Infer each function's actual calling convention from lifted p-code.

Nothing here assumes a compiler ABI for code it can see. For one function it
computes, from the instructions alone:

* inputs: registers and flags whose entry value is used (strong backward
  liveness: a pure operation makes its inputs live only if its result is);
* x87_inputs: entry x87 slots used;
* stack_args: bytes above the return address the function reads;
* preserved: general registers whose entry value is restored at every return
  (forward value tracking through pushes, pops and frame-pointer moves);
* purge: bytes the function removes above its return address (RET n);
* x87_delta: net x87 stack depth change (+1 for a float returned in ST0).

Known stack slots (entry-ESP-relative addresses the forward pass resolves)
take part in liveness, so a register that is only saved and restored, or
pushed merely to reserve a local (`push ecx`), is not an input.

Calls use the callee's inferred summary, so analysis runs bottom-up over the
direct call graph; recursive components iterate to a fixed point. Calls whose
target is not visible (indirect calls, imports, tail jumps through registers)
use the standard Win32 convention: EBX/ESI/EDI/EBP preserved, EAX/ECX/EDX and
flags clobbered. Runtime import metadata supplies cleanup when available;
otherwise stack purge is inferred from pushes and nearby caller cleanup,
including arguments constructed around a getter call, and checked afterwards: a function whose stack depth does not
agree at every join and return is reported, never silently accepted.

Assumptions beyond the instructions, all visible in the census:

* A callee, or a store through an unknown pointer, does not overwrite the
  caller's saved-register slots.
* Loads through unknown pointers and unknown callees read the caller's stack
  slots only when some stack address escapes the function; saved-register
  slots are never read that way.
* An unknown callee consumes no register inputs (it may be passed ECX/EDX;
  code generation must still pass their current values).
* A float returned by an unknown callee is consumed by the next x87
  instruction, which reveals the +1 depth change.
"""
from collections import defaultdict

from .lift import LiftError
from .cfg import FunctionIR, default_successors, call_graph  # compatibility exports

GPRS = ("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")
CALLEE_SAVED = frozenset(("EBX", "EBP", "ESI", "EDI"))
FLAGS = ("CF", "PF", "AF", "ZF", "SF", "DF", "OF")
ARITH_FLAGS = frozenset(FLAGS) - {"DF"}
FLAG_OFFSETS = {0x200: "CF", 0x202: "PF", 0x204: "AF", 0x206: "ZF", 0x207: "SF",
                0x20a: "DF", 0x20b: "OF"}
ESP_VN = ("register", 0x10, 4)
MASK = 0xffffffff
#: Operations whose only effect is their output; dead outputs make inputs dead.
EFFECTFUL = frozenset(("STORE", "CALL", "CALLIND", "BRANCH", "CBRANCH", "BRANCHIND",
                       "RETURN", "CALLOTHER"))


def signed(v):
    v &= MASK
    return v - 0x100000000 if v & 0x80000000 else v


def slot_parts(off):
    """Stack coordinate: entry-relative integer, or (anchor, alignment, offset).

    An aligned base is (entry ESP + anchor) rounded down, never a guessed
    entry offset. Coordinates on different bases may alias; stores invalidate
    every tracked slot whose possible byte range overlaps.
    """
    return (None, off) if isinstance(off, int) else (off[:2], off[2])


def slot_shift(off, delta):
    base, pos = slot_parts(off)
    return pos + delta if base is None else base + (pos + delta,)


def slot_distance(a, b):
    ba, pa = slot_parts(a)
    bb, pb = slot_parts(b)
    return pa - pb if ba == bb else None


def slot_bounds(off):
    base, pos = slot_parts(off)
    return (pos, pos) if base is None else (base[0] + pos - base[1] + 1, base[0] + pos)


def slot_before(a, b):
    distance = slot_distance(a, b)
    return distance < 0 if distance is not None else slot_bounds(a)[1] < slot_bounds(b)[0]


def slot_align(off):
    base, pos = slot_parts(off)
    return pos & ~3 if base is None else base + (pos & ~3,)


def dwords(off, size):
    """Aligned stack-slot keys covering bytes [off, off + size)."""
    base, pos = slot_parts(off)
    return {("s", k if base is None else base + (k,))
            for k in range((pos // 4) * 4, pos + size, 4)}


def lanes(reg):
    """Liveness keys for a whole general register: one per byte lane."""
    return {("L", reg, b) for b in range(4)}


def name_keys(names):
    """Liveness keys for summary input names (GPRs by lane, flags by name)."""
    out = set()
    for n in names:
        if n in GPRS:
            out |= lanes(n)
        else:
            out.add(n)
    return out


def reg_key(v):
    """('r', name, full) for a GPR varnode, ('f', name) for a flag, else None."""
    space, off, size = v
    if space != "register":
        return None
    if off < 0x20:
        return ("r", GPRS[off // 4], off % 4 == 0 and size == 4)
    name = FLAG_OFFSETS.get(off)
    if name is not None and size == 1:
        return ("f", name)
    return None


class Summary(object):
    """What a caller needs to know about a callee."""
    __slots__ = ("addr", "inputs", "x87_inputs", "stack_args", "preserved", "purge",
                 "x87_delta", "ok", "reasons", "returns", "notes", "exit_regs", "exit_stack")

    def __init__(self, addr, inputs=frozenset(), x87_inputs=0, stack_args=0,
                 preserved=CALLEE_SAVED, purge=None, x87_delta=0, ok=True, reasons=(),
                 returns=True, notes=(), exit_regs=None, exit_stack=None):
        self.addr = addr
        self.inputs = frozenset(inputs)        # GPR and flag names
        self.x87_inputs = x87_inputs           # number of entry slots read
        self.stack_args = stack_args           # bytes read above the return address
        self.preserved = frozenset(preserved)  # GPR names, excluding ESP
        self.purge = purge                     # bytes, or None when unknown
        self.x87_delta = x87_delta             # int, or None when unknown
        self.ok = ok
        self.reasons = tuple(reasons)
        self.returns = returns
        self.notes = tuple(notes)
        #: Known exit values of registers that are not simply preserved, in
        #: the callee's entry terms: ('e', reg, k) or ('c', k). _EH_prolog
        #: leaves EBP = entry ESP, for example.
        self.exit_regs = dict(exit_regs or {})
        #: Stack slots, relative to the callee's entry ESP, that hold a known
        #: value at return and remain allocated for the caller.
        self.exit_stack = dict(exit_stack or {})

    def key(self):
        return (self.inputs, self.x87_inputs, self.stack_args, self.preserved, self.purge,
                self.x87_delta, self.ok, self.returns, tuple(sorted(self.exit_regs.items())),
                tuple(sorted(self.exit_stack.items())))

    def failed(self, reason):
        return Summary(self.addr, self.inputs, self.x87_inputs, self.stack_args,
                       self.preserved, self.purge, self.x87_delta, False,
                       self.reasons + (reason,), self.returns, self.notes, self.exit_regs,
                       self.exit_stack)

    def standard(self):
        """Matches the Win32 conventions (cdecl/stdcall/thiscall/fastcall)."""
        return (self.ok and CALLEE_SAVED <= self.preserved
                and not (self.inputs - {"ECX", "EDX", "DF"})
                and self.purge is not None and self.purge >= 0
                and self.x87_inputs == 0 and self.x87_delta in (0, 1))

    def as_json(self):
        return {"inputs": sorted(self.inputs), "x87_inputs": self.x87_inputs,
                "stack_args": self.stack_args, "preserved": sorted(self.preserved),
                "purge": self.purge, "x87_delta": self.x87_delta, "ok": self.ok,
                "returns": self.returns, "standard": self.standard(),
                "reasons": list(self.reasons), "notes": list(self.notes),
                "exit_regs": {r: list(v) for r, v in sorted(self.exit_regs.items())},
                "exit_stack": {str(k): list(v) for k, v in sorted(self.exit_stack.items())}}


def unknown_callee(addr=None):
    return Summary(addr, purge=None, x87_delta=None, notes=("unknown",))


class State(object):
    """Forward abstract state before an instruction.

    Values are ('e', reg, offset) for an entry register plus a constant,
    ('c', k) for a constant, ('m', addr) for a load from an absolute address
    (only used to name import calls), ('a', (anchor, alignment), offset) for
    (entry ESP + anchor) rounded down plus offset, or None for unknown.
    """
    __slots__ = ("regs", "stack", "depth", "run", "pending", "saved")

    def __init__(self, regs, stack, depth, run, pending, saved=()):
        self.saved = set(saved)  # slots written by register saves, including frame addresses
        self.regs = regs        # dict GPR -> value
        self.stack = stack      # dict entry-relative offset -> value (4-byte slots)
        self.depth = depth      # x87 depth relative to entry, or None
        self.run = run          # bytes pushed since the last reset, for purge inference
        self.pending = pending  # x87 depth at an unknown call not yet resolved, or None

    def copy(self):
        return State(dict(self.regs), dict(self.stack), self.depth, self.run, self.pending, self.saved)

    def key(self):
        return (tuple(self.regs[r] for r in GPRS),
                tuple(sorted(self.stack.items(), key=lambda item: repr(item[0]))),
                self.depth, self.run, self.pending, frozenset(self.saved))


def entry_state():
    return State({r: ("e", r, 0) for r in GPRS}, {}, 0, 0, None)


def join(a, b, problems):
    regs = {r: (a.regs[r] if a.regs[r] == b.regs[r] else None) for r in GPRS}
    if a.regs["ESP"] is not None and b.regs["ESP"] is not None \
            and a.regs["ESP"] != b.regs["ESP"]:
        problems.add("stack depth differs at a join")
    stack = {k: v for k, v in a.stack.items() if b.stack.get(k) == v}
    depth = a.depth
    if a.depth != b.depth:
        if a.depth is not None and b.depth is not None:
            problems.add("x87 depth differs at a join")
        depth = None
    run = a.run if a.run == b.run else 0
    pending = a.pending if a.pending == b.pending else None
    return State(regs, stack, depth, run, pending, a.saved & b.saved)


class Facts(object):
    """Per-function results of the forward pass, consumed by liveness."""

    def __init__(self, n):
        self.states = [None] * n
        self.depth = [None] * n     # x87 depth before each instruction
        self.slots = {}             # (insn, op index) -> entry-relative stack offset
        self.calls = {}             # insn -> (Summary, depth, esp offset or None, arg bytes)
        self.saves = set()          # stack offsets holding a saved callee-saved register
        self.escapes = False        # a stack address reaches a register or memory
        self.returns = []           # ("ret"|"tail", state[, summary])
        self.tails = {}             # insn -> Summary of the tail-jump target
        self.results = {}           # return insn -> result keys live at that return
        self.return_loads = set()     # implicit return-address fetches by RET
        self.ret_states = {}        # return insn -> state at RETURN
        self.problems = set()
        self.notes = set()


class Analyzer(object):
    """Infer summaries for functions, given a way to summarize callees.

    `import_purge(iat_addr)` gives cleanup bytes from runtime metadata.
    `callee_summary(addr)` returns the Summary for a direct call target or
    None when it is not a known function. `import_name(addr)` names an IAT
    slot or returns None.
    """

    def __init__(self, callee_summary, import_name=lambda a: None, ret_purge=lambda a: None,
                 import_purge=lambda name: None):
        self._callee_summary = callee_summary
        self.import_name = import_name
        self.import_purge = import_purge
        #: Syntactic purge of a direct target from its RET n instructions (all
        #: agreeing), or None. A fact even when the target's analysis failed.
        self.ret_purge = ret_purge

    def callee_summary(self, addr):
        """A usable summary for a direct target, else None. A callee whose own
        analysis failed or has unknown effects is treated like an unknown
        callee rather than propagating its unreliable facts."""
        s = self._callee_summary(addr)
        return s if (s is not None and s.ok and s.purge is not None
                     and s.x87_delta is not None) else None

    # -- forward pass ------------------------------------------------------

    def forward(self, fir):
        n = len(fir.insns)
        facts = Facts(n)
        states = facts.states
        states[0] = entry_state()
        work = [0]
        visits = [0] * n
        while work:
            i = work.pop()
            visits[i] += 1
            if visits[i] > 64:
                facts.problems.add("forward analysis did not converge")
                break
            out = self.transfer(fir, i, states[i].copy(), facts, record=False)
            if out is None:
                continue
            for s in fir.succ[i]:
                if states[s] is None:
                    states[s] = out
                    work.append(s)
                else:
                    merged = join(states[s], out, facts.problems)
                    if merged.key() != states[s].key():
                        states[s] = merged
                        work.append(s)
        # Replay every reachable instruction once on its final state to record
        # what liveness needs: resolved stack slots, call sites and returns.
        for i in range(n):
            if states[i] is not None:
                self.transfer(fir, i, states[i].copy(), facts, record=True)
        offsets = set(facts.slots.values())
        for i, st in facts.ret_states.items():
            facts.results[i] = self.result_keys(st, offsets)
        return facts

    def value(self, v, st, temps):
        space, off, size = v
        if space == "const":
            return ("c", off & MASK)
        if space == "ram" and size == 4:
            return ("m", off)  # SLEIGH also represents absolute loads as ram inputs
        if space == "unique":
            return temps.get(v)
        k = reg_key(v)
        if k is not None and k[0] == "r" and k[2]:
            return st.regs[k[1]]
        return None

    @staticmethod
    def add(a, b):
        if a is None or b is None:
            return None
        if a[0] == "c" and b[0] == "c":
            return ("c", (a[1] + b[1]) & MASK)
        if a[0] in ("e", "a") and b[0] == "c":
            return (a[0], a[1], signed(a[2] + b[1]))
        if a[0] == "c" and b[0] in ("e", "a"):
            return (b[0], b[1], signed(b[2] + a[1]))
        return None

    @staticmethod
    def stack_addr(a):
        if a is not None and a[0] == "e" and a[1] == "ESP":
            return a[2]
        if a is not None and a[0] == "a":
            return a[1] + (a[2],)
        return None

    def resolve_pending(self, ins, st, facts):
        """An unknown call left a float in ST0 if the next x87 instruction
        reads below the depth the call was made at."""
        if st.pending is None or not ins.x87 or st.depth is None:
            return
        reads = [v[1] for op in ins.ops for v in op.ins if v[0] == "stin"]
        if reads and max(reads) >= st.depth - st.pending:
            st.depth += 1
            facts.notes.add("unknown call returned ST0")
        st.pending = None

    def transfer(self, fir, i, st, facts, record):
        """Apply instruction i to st in place; return st, or None if no flow.

        With `record`, also store this instruction's facts (the state passed
        in must then be the converged one)."""
        ins = fir.insns[i]
        self.resolve_pending(ins, st, facts)
        if record:
            facts.depth[i] = st.depth
        temps = {}
        written = set()
        flows = True
        esp0 = st.regs["ESP"]
        for n, op in enumerate(ins.ops):
            opc = op.opc
            if opc in ("CALL", "CALLIND"):
                self.apply_call(fir, i, op, st, temps, facts, record)
                written.update(r for r in GPRS if st.regs[r] is None)
                continue
            if opc == "RETURN":
                if record:
                    facts.returns.append(("ret", st.copy()))
                    facts.ret_states[i] = st.copy()
                flows = False
                continue
            if opc in ("BRANCH", "BRANCHIND"):
                if not fir.succ[i] and not ins.internal_flow:
                    self.tail_jump(i, op, st, temps, facts, record)
                    flows = False
                continue
            if opc == "CBRANCH":
                continue
            args = op.ins
            res = None
            if opc == "COPY":
                res = self.value(args[0], st, temps)
            elif opc == "INT_ADD":
                res = self.add(self.value(args[0], st, temps), self.value(args[1], st, temps))
            elif opc == "INT_SUB":
                b = self.value(args[1], st, temps)
                if b is not None and b[0] == "c":
                    res = self.add(self.value(args[0], st, temps), ("c", (-b[1]) & MASK))
            elif opc == "INT_AND":
                a, b = (self.value(v, st, temps) for v in args)
                if a is not None and b is not None and b[0] == "c":
                    mask = b[1]
                    alignment = ((~mask) & MASK) + 1
                    if a[0] == "c":
                        res = ("c", a[1] & mask)
                    elif (a[0] == "e" and a[1] == "ESP" and 4 <= alignment <= 0x80000000
                          and alignment & (alignment - 1) == 0):
                        res = ("a", (a[2], alignment), 0)
            elif opc == "LOAD":
                a = self.value(args[0], st, temps)
                off = self.stack_addr(a)
                if off is not None:
                    if record:
                        facts.slots[(i, n)] = off
                        if ins.mnem in ("RET", "RET NEAR"):
                            facts.return_loads.add((i, n))
                    if op.out[2] == 4:
                        res = st.stack.get(off)
                elif a is not None and a[0] == "c" and op.out[2] == 4:
                    res = ("m", a[1])
            elif opc == "STORE":
                a = self.value(args[0], st, temps)
                v = self.value(args[1], st, temps)
                off = self.stack_addr(a)
                if record:
                    if off is not None:
                        facts.slots[(i, n)] = off
                        if v is not None and v[0] == "e" and v[1] in CALLEE_SAVED \
                                and v[2] == 0 and args[1][2] == 4:
                            facts.saves.add(off)
                    if self.stack_addr(v) is not None:
                        facts.escapes = True
                self.store(st, off, v, args[1][2])
                saved_reg = reg_key(ins.ops[0].ins[0]) if (ins.mnem.startswith("PUSH")
                            and ins.ops[0].opc == "COPY") else reg_key(args[1])
                if off is not None and args[1][2] == 4 and (
                        (saved_reg is not None and saved_reg[1] in CALLEE_SAVED
                         and ins.mnem.startswith("PUSH"))
                        or (v is not None and v[0] == "e" and v[1] in CALLEE_SAVED
                            and v[2] == 0)):
                    st.saved.add(off)
                    if record:
                        facts.saves.add(off)
                continue
            out = op.out
            if out is None:
                continue
            if out[0] == "unique":
                temps[out] = res
                continue
            k = reg_key(out)
            if k is not None and k[0] == "r":
                st.regs[k[1]] = res if k[2] else None
                written.add(k[1])
                if record and k[1] not in ("ESP", "EBP") and self.stack_addr(res) is not None:
                    facts.escapes = True
        if ins.internal_flow:
            # Straight-line evaluation above ignored the internal branches;
            # anything the instruction wrote may hold either value.
            for r in written:
                if r != "ESP":
                    st.regs[r] = None
            if st.regs["ESP"] != esp0:
                facts.problems.add("ESP changes inside a REP or other internal loop")
        self.track_run(ins, st, esp0)
        if st.depth is not None:
            st.depth += ins.x87_delta
        return st if flows else None

    def result_keys(self, st, offsets):
        """What a return may hand back: EAX/EDX unless they still hold their
        entry value (a pass-through is not a result), every x87 slot the
        function leaves pushed, and stack slots still allocated for the caller
        whose value is not a known constant or entry value (those the summary
        exports, and callers model as stores)."""
        keys = {("res", r, b) for r in ("EAX", "EDX") if st.regs[r] != ("e", r, 0)
                for b in range(4)}
        if st.depth is not None and st.depth > 0:
            keys |= {("x", d) for d in range(st.depth)}
        esp = self.stack_addr(st.regs["ESP"])
        if esp is not None:
            for k in offsets:
                v = st.stack.get(k)
                if not slot_before(k, esp) and (v is None or v[0] not in ("c", "e")):
                    keys |= dwords(k, 1)
        return keys

    @staticmethod
    def store(st, off, val, size):
        if off is None:
            return  # assumption: not the saved-register area
        lo, hi = slot_bounds(off)
        st.saved.discard(off)
        for k in list(st.stack):
            distance = slot_distance(k, off)
            klo, khi = slot_bounds(k)
            overlaps = (-3 <= distance < size if distance is not None
                        else klo <= hi + size - 1 and khi + 3 >= lo)
            if overlaps:
                del st.stack[k]
                st.saved.discard(k)
        if size == 4:
            st.stack[off] = val
        else:
            st.stack.pop(off, None)

    @staticmethod
    def track_run(ins, st, esp0):
        """Bytes of argument pushes since the last reset point."""
        esp1 = st.regs["ESP"]
        if esp0 is None or esp1 is None or esp0[:2] != esp1[:2] \
                or esp0[0] not in ("e", "a"):
            st.run = 0
            return
        moved = esp1[2] - esp0[2]
        if moved > 0:
            st.run = 0
        elif moved < 0:
            pushed = st.stack.get(Analyzer.stack_addr(esp1))
            frame_save = (ins.ops and ins.ops[0].opc == "COPY"
                          and ins.ops[0].ins[0] == ("register", 0x14, 4)
                          and esp1[0] == "a"
                          and pushed == ("e", "ESP", esp1[1][0]))
            entry_save = (pushed is not None and pushed[0] == "e" and pushed[2] == 0
                          and pushed[1] in CALLEE_SAVED)
            if ins.mnem.startswith("PUSH") and moved == -4 and (entry_save or frame_save):
                st.run = 0  # saving a callee-saved register ends the prologue
            else:
                st.run -= moved

    def call_target(self, op, st, temps):
        t = op.ins[0]
        if op.opc == "CALL":
            return ("direct", t[1]) if t[0] == "ram" else ("unknown", None)
        v = self.value(t, st, temps)
        if v is not None and v[0] == "m":
            name = self.import_name(v[1])
            if name is not None:
                return ("import", v[1])
        if v is not None and v[0] == "c":
            return ("direct", v[1])
        return ("indirect", None)

    def apply_call(self, fir, i, op, st, temps, facts, record):
        kind, t = self.call_target(op, st, temps)
        summary = self.callee_summary(t) if kind == "direct" else None
        inferred = summary is not None
        if summary is None:
            summary = unknown_callee(t)
            if kind == "direct" and self.ret_purge(t) is not None:
                summary.purge = self.ret_purge(t)
            elif kind == "import":
                summary.purge = self.import_purge(t)
                if summary.purge is not None:
                    summary.stack_args = summary.purge
            if record:
                facts.notes.add("calls %s" % ("unknown direct target" if kind == "direct"
                                              else kind))
        esp = self.stack_addr(st.regs["ESP"])
        purge = summary.purge
        if purge is None:
            purge = self.guess_purge(fir, i, st)
        args = (summary.stack_args if inferred or (kind == "import" and summary.purge)
                else max(purge, st.run))
        if record:
            facts.calls[i] = (summary, st.depth, esp, args)
            if not summary.returns:
                facts.notes.add("calls a non-returning function")
        before = dict(st.regs)

        def sub(v):
            """A callee-entry value in this caller's terms."""
            if v is None or v[0] != "e":
                return v
            if v[1] == "ESP":
                return self.add(before["ESP"], ("c", v[2] & MASK))
            return self.add(before[v[1]], ("c", v[2] & MASK))

        for r in GPRS:
            if r != "ESP" and r not in summary.preserved:
                st.regs[r] = sub(summary.exit_regs.get(r))
        if esp is not None:
            st.regs["ESP"] = self.add(before["ESP"], ("c", (4 + purge) & MASK))
            # The callee pops its return address and purge: those slots die.
            for k in [k for k in st.stack if slot_before(k, slot_shift(esp, 4 + purge))]:
                del st.stack[k]
                st.saved.discard(k)
        elif st.regs["ESP"] is not None:
            st.regs["ESP"] = None
        # Callee-owned argument slots may be rewritten; keep only saved registers.
        for k in [k for k, v in st.stack.items()
                  if k not in st.saved]:
            del st.stack[k]
            st.saved.discard(k)
        if esp is not None:
            for off, v in summary.exit_stack.items():
                if off >= 4 + purge:
                    st.stack[slot_shift(esp, off)] = sub(v)
                    val = sub(v)
                    if val is not None and val[0] == "e" and val[1] in CALLEE_SAVED \
                            and val[2] == 0:
                        st.saved.add(slot_shift(esp, off))
                        if record:
                            facts.saves.add(slot_shift(esp, off))
        if summary.x87_delta is None:
            st.pending = st.depth
        elif st.depth is not None:
            st.depth += summary.x87_delta
        st.run = 0

    def guess_purge(self, fir, i, st):
        """Unknown callee: cdecl when the caller cleans up right after the
        call (`ADD ESP,n`, or VC's one-argument `POP ECX`), otherwise
        callee-pops every argument pushed before it."""
        nxt = fir.succ[i][0] if fir.succ[i] else None
        if nxt is not None:
            after = fir.insns[nxt]
            if after.mnem == "ADD" and any(
                    op.out == ESP_VN and op.opc == "INT_ADD" and op.ins[0] == ESP_VN
                    and op.ins[1][0] == "const" for op in after.ops):
                return 0
            # VC cleans small cdecl calls with POP ECX (or EDX), once per dword.
            popped, k = 0, nxt
            while k is not None and fir.insns[k].mnem == "POP" and any(
                    op.out in (("register", 4, 4), ("register", 8, 4))
                    for op in fir.insns[k].ops):
                popped += 4
                k = fir.succ[k][0] if len(fir.succ[k]) == 1 else None
            if popped and popped == st.run:
                return 0
        # Arguments may be constructed around a call: push the trailing
        # arguments, call a getter for the first argument, then push its
        # result and invoke a known import. The getter cannot also consume
        # those pushes when the import needs exactly the whole group.
        added, k = 0, nxt
        for _ in range(8):
            if k is None:
                break
            ins = fir.insns[k]
            if any(op.opc in ("CALL", "CALLIND") for op in ins.ops):
                purge = self.literal_import_purge(ins)
                if st.run and purge == st.run + added:
                    return 0
                break
            if ins.mnem.startswith("PUSH"):
                # Only ordinary dword pushes; PUSHAD/PUSHFW need other rules.
                updates = [op for op in ins.ops if op.out == ESP_VN]
                if (len(updates) != 1 or updates[0].opc != "INT_SUB"
                        or updates[0].ins != (ESP_VN, ("const", 4, 4))):
                    break
                added += 4
            elif any(op.out == ESP_VN for op in ins.ops):
                break
            if any(op.opc in ("BRANCH", "BRANCHIND", "CBRANCH", "RETURN") for op in ins.ops):
                break
            k = fir.succ[k][0] if len(fir.succ[k]) == 1 else None
        return st.run

    def literal_import_purge(self, ins):
        """Cleanup for an instruction calling a literal IAT slot, else None.

        Follow COPY/LOAD temporaries without assuming any register values;
        unrelated IAT references cannot identify the call's target.
        """
        st = entry_state()
        st.regs = {r: None for r in GPRS}
        temps = {}
        for op in ins.ops:
            if op.opc in ("CALL", "CALLIND"):
                kind, target = self.call_target(op, st, temps)
                return self.import_purge(target) if kind == "import" else None
            val = None
            if op.opc == "COPY":
                val = self.value(op.ins[0], st, temps)
            elif op.opc == "LOAD":
                addr = self.value(op.ins[0], st, temps)
                if addr is not None and addr[0] == "c" and op.out[2] == 4:
                    val = ("m", addr[1])
            if op.out is not None and op.out[0] == "unique":
                temps[op.out] = val
        return None

    def tail_jump(self, i, op, st, temps, facts, record):
        t = op.ins[0]
        if op.opc == "BRANCH" and t[0] == "ram":
            summary = self.callee_summary(t[1])
            if summary is None:
                if record:
                    target = self._callee_summary(t[1])
                    facts.notes.add("jumps outside the function to a non-function" if target is None
                                    else "tail-jumps to a function whose analysis failed" if not target.ok
                                    else "tail-jumps to a function with unknown effects")
                summary = unknown_callee(t[1])
                summary.purge = self.ret_purge(t[1])
        else:
            v = self.value(t, st, temps)
            name = self.import_name(v[1]) if v is not None and v[0] == "m" else None
            if record:
                facts.notes.add("tail-jumps to an import" if name else "indirect tail jump")
            summary = unknown_callee()
            if name:
                summary.purge = self.import_purge(v[1])
                summary.stack_args = summary.purge or 0
        if not record:
            return
        st2 = st.copy()
        esp = self.stack_addr(st2.regs["ESP"])
        purge = summary.purge
        if purge is None:
            facts.notes.add("tail-jump purge unknown")
        for r in GPRS:
            if r != "ESP" and r not in summary.preserved:
                st2.regs[r] = None
        st2.regs["ESP"] = (self.add(st.regs["ESP"], ("c", (4 + purge) & MASK))
                           if purge is not None else None)
        if summary.x87_delta is not None and st2.depth is not None:
            st2.depth += summary.x87_delta
        else:
            st2.depth = None
        facts.returns.append(("tail", st2, summary, esp))
        facts.tails[i] = (summary, esp)

    # -- backward liveness -------------------------------------------------

    def backward(self, fir, facts):
        """Strong liveness with stack slots; returns the keys live at entry.

        Keys are GPR and flag names, ('x', slot) for an x87 slot at an
        absolute depth (negative slots belong to the caller), ('s', offset)
        for an entry-relative stack slot, and unique varnodes inside one
        instruction.
        """
        n = len(fir.insns)
        preds = defaultdict(list)
        for i, ss in enumerate(fir.succ):
            if facts.states[i] is None:
                continue
            for s in ss:
                preds[s].append(i)
        saves = {slot_align(k) for k in facts.saves}
        escaped = (frozenset(("s", slot_align(k)) for k in facts.slots.values()
                             if slot_bounds(k)[0] < 0 and slot_align(k) not in saves)
                   if facts.escapes else frozenset())
        live_in = [frozenset()] * n
        work = [i for i in range(n) if facts.states[i] is not None]
        budget = 64 * n + 1000
        while work and budget:
            budget -= 1
            i = work.pop()
            out = set()
            for s in fir.succ[i]:
                out |= live_in[s]
            new = frozenset(self.insn_live(fir, i, out, facts, escaped))
            if new != live_in[i]:
                live_in[i] = new
                work.extend(preds[i])
        if not budget:
            facts.problems.add("liveness did not converge")
        return live_in[0] if n else frozenset()

    def insn_live(self, fir, i, live, facts, escaped):
        ins = fir.insns[i]
        depth = facts.depth[i]
        d_after = depth + ins.x87_delta if depth is not None else None
        live = set(live) | facts.results.get(i, set())
        if i in facts.tails:
            summary, esp = facts.tails[i]
            live |= name_keys(summary.inputs)
            live |= self.x87_keys(summary.x87_inputs, depth)
            live |= self.arg_keys(esp, summary.stack_args)
        kill = not ins.internal_flow
        ops = ins.ops
        if ins.internal_flow:
            # Ops inside an internal loop may repeat: no kills, every input live.
            for n, op in enumerate(ops):
                live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
            return live
        for n in range(len(ops) - 1, -1, -1):
            op = ops[n]
            opc = op.opc
            if opc in ("CALL", "CALLIND"):
                summary, cdepth, esp, args = facts.calls[i]
                if esp is not None:
                    # Slots the callee leaves holding an entry register are
                    # stores of that register here: live only if read later.
                    for off, v in summary.exit_stack.items():
                        key = ("s", slot_shift(esp, off))
                        if key in live:
                            live.discard(key)
                            if v[0] == "e" and v[1] != "ESP":
                                live |= lanes(v[1])
                clobbered = {r for r in GPRS if r != "ESP" and r not in summary.preserved}
                live -= name_keys(clobbered)
                live -= {("res", r, b) for r in clobbered for b in range(4)}
                live -= ARITH_FLAGS
                live |= name_keys(summary.inputs)
                live |= self.x87_keys(summary.x87_inputs, cdepth)
                live |= self.arg_keys(esp, args)
                live |= escaped
                live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
                continue
            if opc == "STORE":
                off = facts.slots.get((i, n))
                if off is not None:
                    touched = dwords(off, op.ins[1][2])
                    if touched & live:
                        if op.ins[1][2] == 4 and slot_parts(off)[1] % 4 == 0:
                            live -= touched  # a full aligned store kills its slot
                        live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
                    else:
                        live |= self.vn_keys(op.ins[0], depth, d_after)
                    continue
                live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
                continue
            if opc in EFFECTFUL:
                live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
                continue
            out = op.out
            okeys = self.vn_keys(out, depth, d_after, writing=True) if out is not None else None
            if okeys and any(k[0] == "g" for k in okeys if isinstance(k, tuple)):
                okeys = None  # global machine state: an effect, never dead
            if okeys:
                rkeys = {("res", k[1], k[2]) for k in okeys
                         if k[0] == "L" and k[1] in ("EAX", "EDX")}
                if not (okeys & live) and not (rkeys & live):
                    continue  # dead pure operation
                if kill:
                    live -= okeys
                    live -= rkeys
            live |= self.op_inputs(i, n, op, depth, d_after, facts, escaped)
        return live

    @staticmethod
    def vn_keys(v, depth, d_after, writing=False):
        """Liveness keys a varnode names. Sub-registers name their byte lanes,
        so `MOV BL,..` kills only lane 0 of EBX. Writes to state other than
        GPRs, flags and x87 slots (FPU control/status, segments) name a key
        that is never killed elsewhere, keeping those writes live."""
        space = v[0]
        if space == "unique":
            return {v}
        if space == "stin":
            return {("x", depth - 1 - v[1])} if depth is not None else set()
        if space == "stout":
            return {("x", d_after - 1 - v[1])} if d_after is not None else set()
        k = reg_key(v)
        if k is None:
            return {("g",) + v} if writing and space == "register" else set()
        if k[0] == "f":
            return {k[1]}
        if k[1] == "ESP":
            return {("g", "ESP")} if writing else set()
        lo = v[1] % 4
        return {("L", k[1], b) for b in range(lo, min(4, lo + v[2]))}

    def op_inputs(self, i, n, op, depth, d_after, facts, escaped):
        keys = set()
        for v in op.ins:
            keys |= self.vn_keys(v, depth, d_after)
        if op.opc == "LOAD":
            off = facts.slots.get((i, n))
            if off is not None:
                if (i, n) not in facts.return_loads:
                    keys |= dwords(off, op.out[2])
            else:
                keys |= escaped
        return keys

    @staticmethod
    def x87_keys(count, depth):
        if depth is None or not count:
            return set()
        return {("x", depth - 1 - k) for k in range(count)}

    @staticmethod
    def arg_keys(esp, nbytes):
        if esp is None or not nbytes:
            return set()
        return {("s", slot_shift(esp, 4 + k)) for k in range(0, nbytes, 4)}

    # -- summary -----------------------------------------------------------

    def summarize(self, fir):
        facts = self.forward(fir)
        live = self.backward(fir, facts)
        reasons = set(facts.problems)
        notes = set(facts.notes)
        # A result key still live at entry means that register's entry value
        # can reach a return unchanged: a pass-through, not an input.
        passthrough = {k[1] for k in live if isinstance(k, tuple) and k[0] == "res"}
        if passthrough:
            notes.add("returns entry %s on some path" % "/".join(sorted(passthrough)))
        inputs = {k for k in live if isinstance(k, str) and k in FLAGS}
        inputs |= {k[1] for k in live if isinstance(k, tuple) and k[0] == "L"}
        x87_slots = [k[1] for k in live if isinstance(k, tuple) and k[0] == "x" and k[1] < 0]
        x87_inputs = -min(x87_slots) if x87_slots else 0
        stack = [k[1] for k in live if isinstance(k, tuple) and k[0] == "s"]
        args = [k for k in stack if isinstance(k, int) and k >= 4]
        stack_args = max(args) if args else 0  # keys are aligned: offset 4 is one dword
        if any(slot_bounds(k)[0] < 0 for k in stack):
            notes.add("reads uninitialized stack")
        if 0 in stack:
            notes.add("reads its return address")
        if any(facts.depth[i] is None for i in range(len(fir.insns))
               if facts.states[i] is not None and fir.insns[i].x87):
            reasons.add("x87 depth unknown at an x87 instruction")
        has_x87 = any(ins.x87 for ins in fir.insns)
        preserved = set(GPRS) - {"ESP"}
        purge = delta = None
        unknown_delta = False
        unknown_purge = False
        for entry in facts.returns:
            st = entry[1]
            for r in list(preserved):
                if st.regs[r] != ("e", r, 0):
                    preserved.discard(r)
            esp = self.stack_addr(st.regs["ESP"])
            if not isinstance(esp, int):
                if (entry[0] == "tail" and entry[2].purge is None
                        and isinstance(entry[3], int)):
                    unknown_purge = True
                else:
                    reasons.add("stack pointer unknown at return")
            elif purge is None:
                purge = esp - 4
            elif purge != esp - 4:
                reasons.add("returns disagree on stack purge")
            d = st.depth
            if d is None and not has_x87 and (entry[0] == "ret" or entry[2].x87_delta == 0):
                d = 0
            if d is None:
                if entry[0] == "tail" and not has_x87:
                    unknown_delta = True
                    notes.add("tail-jump x87 effect unknown")
                else:
                    reasons.add("x87 depth unknown at return")
            elif delta is None:
                delta = d
            elif delta != d:
                reasons.add("returns disagree on x87 depth")
        exit_regs, exit_stack = {}, None
        for r in GPRS:
            if r == "ESP" or r in preserved or not facts.returns:
                continue
            vals = {entry[1].regs[r] for entry in facts.returns}
            if len(vals) == 1:
                v = vals.pop()
                if v is not None and v[0] in ("c", "e"):
                    exit_regs[r] = v
        for entry in facts.returns:
            st = entry[1]
            esp = self.stack_addr(st.regs["ESP"])
            known = {k: v for k, v in st.stack.items()
                     if isinstance(esp, int) and isinstance(k, int) and k >= esp
                     and v is not None and v[0] in ("c", "e")}
            exit_stack = known if exit_stack is None else {
                k: v for k, v in exit_stack.items() if known.get(k) == v}
        if not facts.returns:
            preserved = set(GPRS) - {"ESP"}
        return Summary(fir.addr, inputs=inputs, x87_inputs=x87_inputs, stack_args=stack_args,
                       preserved=preserved,
                       purge=None if unknown_purge else (purge if purge is not None else 0),
                       x87_delta=None if unknown_delta else (delta if delta is not None else 0),
                       ok=not reasons,
                       reasons=sorted(reasons), returns=bool(facts.returns),
                       notes=sorted(notes), exit_regs=exit_regs, exit_stack=exit_stack)


def summarize_all(functions, build_ir, direct_targets, import_name=lambda a: None,
                  max_rounds=8, progress=None, ret_purge=lambda a: None,
                  import_purge=lambda a: None):
    """Summaries for every function address in `functions`.

    `build_ir(addr)` returns a FunctionIR (or raises LiftError);
    `direct_targets(addr)` lists direct call and tail-jump targets cheaply,
    without lifting, to order the analysis.
    """
    summaries = {}
    fset = set(functions)

    def callee(addr):
        return summaries.get(addr) if addr in fset else None

    analyzer = Analyzer(callee, import_name, ret_purge, import_purge)
    done = 0
    for comp in call_graph(sorted(fset), direct_targets):
        irs = {}
        for a in comp:
            try:
                irs[a] = build_ir(a)
            except LiftError as e:
                summaries[a] = Summary(a, ok=False, reasons=("lift: %s" % e,),
                                       purge=None, x87_delta=None)
        recursive = len(comp) > 1 or comp[0] in direct_targets(comp[0])
        for a in irs:
            summaries.setdefault(a, Summary(a, purge=None, x87_delta=None,
                                            notes=("recursion seed",)))
        for _ in range(max_rounds if recursive else 1):
            changed = False
            for a, fir in irs.items():
                s = analyzer.summarize(fir)
                if summaries[a].key() != s.key():
                    changed = True
                summaries[a] = s
            if not changed:
                break
        else:
            if recursive:
                for a in irs:
                    summaries[a] = summaries[a].failed("recursion did not converge")
        done += len(comp)
        if progress:
            progress(done, len(fset))
    return summaries
