"""Experimental C emission from integer SSA.

The production adapter admits a restricted subset through existing entry thunks;
this emitter alone does not implement hooks, SEH or alternate entries. Original
interior fault equivalence has not been validated. A direct CALL is emitted
only when the caller supplies an explicit target-to-symbol binding; otherwise
it is a whole-function fallback. Optimized bodies lower audited x87 effects
through the scalar tracker (`x87_scalar.py`). Memory uses the
existing guest accessors, in instruction order, with explicit state
publication. No inferred convention permits discarding guest state.
"""
from dataclasses import dataclass

from .lift import Lifter, Insn, Op
from .integer import read_modify_write, arithmetic, shift, divide
from .integer_extra import EXTRA_MNEMONICS, correct as correct_extra
from .ssa import SSAError, MEMORY, build
from .cfg import FunctionIR
from .simplify import canonicalize, simplify, state_roots, whole, EFFECTS, EXITS
from .publication import analyze as publication_analysis
from . import transfer_plan, x87_plan
from . import flag_region
from . import x87
from .call_contracts import GPRS as CONTRACT_GPRS, ARITH_FLAGS as CONTRACT_FLAGS
from .contract_variants import output_mask as contract_output_mask


SUPPORTED_MNEMONICS = frozenset((
    "MOV", "MOVSX", "MOVZX", "XCHG", "NOT", "LEAVE", "LEA", "PUSH", "POP", "RET", "NOP", "ADD", "SUB", "CMP", "INC", "DEC",
    "ADC", "SBB", "SHL", "SHR", "DIV",
    "CLD", "STD", "SAHF", "MUL",
    "TEST", "AND", "OR", "XOR", "JMP", "JZ", "JNZ", "JE", "JNE",
    "JA", "JAE", "JB", "JBE", "JC", "JNC", "JG", "JGE", "JL", "JLE", "JS", "JNS",
    "JO", "JNO", "JP", "JNP", "JPE", "JPO", "CALL",
)) | EXTRA_MNEMONICS


def _string_helpers():
    """Map the audited string-instruction encodings to their runtime helpers."""
    table = {}
    for opcode, name in ((0xa4, "movs"), (0xa5, "movs"), (0xaa, "stos"), (0xab, "stos"),
                         (0xa6, "cmps"), (0xa7, "cmps"), (0xae, "scas"), (0xaf, "scas")):
        for size_prefix, wide in ((b"", "d"), (b"\x66", "w")):
            if opcode & 1 == 0:
                if size_prefix:
                    continue
                suffix = "b"
            else:
                suffix = wide
            single = bytes([opcode])
            table[size_prefix + single] = name + suffix
            if name in ("movs", "stos"):
                table[size_prefix + b"\xf3" + single] = "rep_" + name + suffix
            else:
                table[b"\xf3" + size_prefix + single] = "repe_" + name + suffix
                table[b"\xf2" + size_prefix + single] = "repne_" + name + suffix
    return table


#: Raw encodings (bare, REP, REPE, REPNE; byte, word, dword) -> runtime helper.
STRING_HELPERS = _string_helpers()

# Arithmetic flags tracked by the calling-convention census.
FLAG_FIELDS = frozenset("c->eflags_" + n for n in ("cf", "pf", "af", "zf", "sf", "of"))

# Call-contract fields.
CONTRACT_FIELDS = frozenset(CONTRACT_GPRS) | frozenset(CONTRACT_FLAGS)
#: The guest frame pointer and stack pointer are call-helper state even when a
#: contract says the callee overwrites them; never drop their publication.
CONTRACT_NEVER_SKIP = frozenset(("ESP", "EBP"))


#: Lazy-flag producers recognised at a seam: (kind, primary p-code opcode).
#: The primary op must write a non-flag destination.  SUB/CMP, ADD, logic/TEST
#: INC/DEC and carry-aware ADC/SBB are recognised; everything else stays eager.
CC_PRIMARY = {
    "ADD": ("add", "INT_ADD"),
    "SUB": ("sub", "INT_SUB"),
    "ADC": ("adc", "INT_ADD"),
    "SBB": ("sbb", "INT_SUB"),
    "CMP": ("cmp", "INT_SUB"),
    "INC": ("inc", "INT_ADD"),
    "DEC": ("dec", "INT_SUB"),
    "AND": ("logic", "INT_AND"),
    "OR": ("logic", "INT_OR"),
    "XOR": ("logic", "INT_XOR"),
    "TEST": ("logic", "INT_AND"),
}


def _flag_producer(mnem, ops, flag_offsets):
    """Return the primary op metadata for a recognised lazy-flag producer."""
    info = CC_PRIMARY.get(mnem)
    if info is None:
        return None
    kind, opc = info
    stored = {op.ins[1] for op in ops if op.opc == "STORE"}
    found = None
    for op in ops:
        if stored and op.out not in stored:
            continue
        if op.out is None:
            continue
        space, off, size = op.out
        if space == "register" and any((off + n) in flag_offsets for n in range(size)):
            if op.opc == "INT_EQUAL" and op.ins[1] == ("const", 0, op.ins[0][2]):
                break
            continue
        if op.opc == opc and space in ("register", "unique"):
            if kind in ("adc", "sbb"):
                if not isinstance(op.data, dict) or "cc_operands" not in op.data:
                    continue
            found = {"kind": kind, "opc": opc, "size": size, "result": op.out}
            if kind in ("adc", "sbb"):
                found["operands"] = op.data["cc_operands"]
            if kind in ("adc", "sbb"):
                break
    return found


#: Which arithmetic flags a recognised producer defines.  Flags absent are
#: preserved; the emitter stores them directly at the seam.
CC_DEFINES = {
    "add": ("cf", "pf", "af", "zf", "sf", "of"),
    "sub": ("cf", "pf", "af", "zf", "sf", "of"),
    "adc": ("cf", "pf", "af", "zf", "sf", "of"),
    "sbb": ("cf", "pf", "af", "zf", "sf", "of"),
    "cmp": ("cf", "pf", "af", "zf", "sf", "of"),
    "logic": ("cf", "of", "zf", "sf", "pf"),
    "inc": ("pf", "af", "zf", "sf", "of"),
    "dec": ("pf", "af", "zf", "sf", "of"),
}
CC_OP_CONST = {"add": "X86_CC_ADD", "sub": "X86_CC_SUB", "cmp": "X86_CC_SUB",
               "logic": "X86_CC_LOGIC", "inc": "X86_CC_INC", "dec": "X86_CC_DEC",
               "adc": "X86_CC_ADC", "sbb": "X86_CC_SBB"}
CC_BIT_VALUES = {"cf": 1, "pf": 2, "af": 4, "zf": 8, "sf": 0x10, "of": 0x20}
#: Seams whose observer may read the guest's fields directly, so flags stay eager.
CC_EAGER_EVENTS = frozenset(("DIV32", "IDIV32", "STRINGOP", "BRANCHIND"))

_RAM_CONTROL = frozenset(("BRANCH", "CBRANCH", "CALL", "CALLIND", "BRANCHIND", "CALLOTHER"))


def normalize_direct_ram(ins, lifter):
    """Codegen-only lowering of SLEIGH's direct absolute memory operands.

    SLEIGH folds a direct access into a data-position ``ram`` varnode
    (`MOV EAX,[0x...]` is `EAX = COPY ram[...]`). Raw lifting keeps those for
    the census; code generation must make them explicit memory effects so flag
    order and single-read behavior match the eager emitter:

    * a source-only access is captured once into a fresh unique shared by every
      consumer, so arithmetic flags cannot re-read memory;
    * a pure store becomes an explicit STORE;
    * a read-modify-write becomes SLEIGH's register-addressed memory form,
      a LOAD before each read and a STORE after the write, which the
      mnemonic's read-modify-write correction reduces to one read.
    Control-flow ram is untouched.
    """
    ops = list(ins.ops)
    for index, op in enumerate(ops):
        if op.opc == "BRANCHIND" and op.ins[0][0] == "ram":
            _, offset, size = op.ins[0]
            target = lifter.fresh_unique(size)
            ops[index:index + 1] = [Op("LOAD", target, [("const", offset, 4)]),
                                    Op("BRANCHIND", None, [target])]
            break
    touched = [op for op in ops if op.opc not in _RAM_CONTROL
               and any(v is not None and v[0] == "ram" for v in (op.out,) + op.ins)]
    if not touched:
        return ops
    operands = {(v[1], v[2]) for op in touched for v in (op.out,) + op.ins
                if v is not None and v[0] == "ram"}
    if len(operands) != 1:
        raise SSAError("%08x: multiple direct-ram operands" % ins.addr)
    offset, size = operands.pop()
    address = ("const", offset, 4)
    has_write = any(op.out is not None and op.out[0] == "ram" for op in touched)
    has_read = any(any(v is not None and v[0] == "ram" for v in op.ins) for op in touched)
    if has_write and has_read:
        if sum(op.out is not None and op.out[0] == "ram" for op in touched) != 1:
            raise SSAError("%08x: unsupported direct-ram read-modify-write" % ins.addr)
        operand = lifter.fresh_unique(size)
        rewritten = []
        for op in ops:
            if op.opc in _RAM_CONTROL:
                rewritten.append(op)
                continue
            inputs = tuple(operand if (v is not None and v[0] == "ram") else v for v in op.ins)
            if inputs != op.ins:
                rewritten.append(Op("LOAD", operand, [address]))
            if op.out is not None and op.out[0] == "ram":
                rewritten.extend([Op(op.opc, operand, inputs, op.data),
                                  Op("STORE", None, [address, operand])])
            else:
                rewritten.append(Op(op.opc, op.out, inputs, op.data))
        return rewritten
    if has_write:
        for index, op in enumerate(ops):
            if op.out is not None and op.out[0] == "ram":
                dst = lifter.fresh_unique(size)
                ops[index] = Op(op.opc, dst, op.ins, op.data)
                ops.insert(index + 1, Op("STORE", None, [address, dst]))
                break
        return ops
    # Source-only: exactly one captured read, inserted where the first direct
    # read appeared and shared by every consumer.
    src = lifter.fresh_unique(size)
    first = ops.index(touched[0])
    rewritten = []
    for op in ops:
        if op.opc in _RAM_CONTROL:
            rewritten.append(op)
            continue
        inputs = tuple(src if (v is not None and v[0] == "ram") else v for v in op.ins)
        rewritten.append(Op(op.opc, op.out, inputs, op.data))
    rewritten.insert(first, Op("LOAD", src, [address]))
    return rewritten


def add_exit_stubs(insns, succ, exits, tails):
    """Give each out-of-body transfer target one TAIL block, shared by its sources."""
    index = {ins.addr: k for k, ins in enumerate(insns)}
    succ = [list(row) for row in succ]
    for i, targets in sorted(exits.items()):
        for target in targets:
            if target not in tails:
                raise SSAError("%08x: tail target %08x is not bound" % (insns[i].addr, target))
            if target not in index:
                index[target] = len(insns)
                insns.append(Insn(target, 0, "TAIL", [Op("TAIL", None, [("ram", target, 4)])],
                                  0, False, False, [], None))
                succ.append([])
            succ[i].append(index[target])
    return succ


def seh_ops(ops, kinds, addr):
    """Append the SEH runtime effects; an orphan precedes the return it guards."""
    ops = list(ops)
    for kind in kinds:
        op = Op("SEH", None, [], {"kind": kind, "eip": addr})
        if kind == "landing":
            ops.insert(0, op)
            continue
        if kind != "orphan":
            ops.append(op)
            continue
        at = next((n for n, o in enumerate(ops) if o.opc == "RETURN"), None)
        if at is None:
            raise SSAError("%08x: SEH orphan without a return" % addr)
        ops.insert(at, op)
    return ops


def seh_lines(kind, addr):
    eip = "c->eip = 0x%xu;" % addr
    if kind == "enter":
        return [eip, "{ jmp_buf *b_ = recomp_seh_frame_enter(c); "
                     "if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } }"]
    if kind == "adopt":
        return [eip, "{ jmp_buf *b_ = recomp_seh_frame_adopt(c); "
                     "if (b_) { if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } } }"]
    if kind == "leave":
        return [eip, "recomp_seh_frame_leave(c);"]
    if kind == "landing":
        return ["if (recomp_seh_pending_target()) recomp_seh_intercept(c, 0x%xu);" % addr]
    return ["recomp_seh_frame_orphan(c, seh_mark_);"]


def codegen_ir(fir, lifter, tails=()):
    """Apply audited integer/x87 corrections without changing raw census input."""
    flag_offsets = set()
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF"):
        _, off, size = lifter.register(name)
        flag_offsets.update(range(off, off + size))
    insns = []
    for i, ins in enumerate(fir.insns):
        mnem = ins.mnem.upper()
        if mnem.startswith("F") or ins.x87 or mnem.startswith("WAIT "):
            ops = x87.lower(ins, lifter)
            insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, 0,
                              False, False, [], ins.raw))
            continue
        # String instructions are byte-audited to the runtime helpers rather
        # than the raw SLEIGH loops: SLEIGH advances ESI/EDI and decrements ECX
        # before the access, while the helpers access first and then advance.
        # Only the exact byte sequences in STRING_HELPERS are admitted; SSE
        # MOVSD, address-size and other prefixes stay unsupported fallbacks.
        helper = STRING_HELPERS.get(ins.raw)
        if helper is not None:
            ops = [Op("STRINGOP", None, [], {"helper": helper})]
            insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, 0,
                              False, False, [], ins.raw))
            continue
        if mnem == "WAIT":
            # A bare WAIT only synchronises pending x87 exceptions; the port's
            # eager emitter treats it as a no-op, and no exception is delivered.
            insns.append(Insn(ins.addr, ins.length, ins.mnem, [], 0,
                              False, False, [], ins.raw))
            continue
        if mnem not in SUPPORTED_MNEMONICS:
            raise SSAError("%08x: C emitter: unsupported instruction %s" % (ins.addr, mnem))
        ops = normalize_direct_ram(ins, lifter)
        normalized = Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                          ins.x87, ins.internal_flow, ins.userops, ins.raw)
        if mnem == "SAR":
            ops = read_modify_write(normalized, lifter,
                                    lambda insn, operand: correct_extra(insn, lifter, operand))
        elif mnem in EXTRA_MNEMONICS:
            ops = correct_extra(normalized, lifter)
        elif mnem in ("ADD", "SUB", "CMP", "INC", "DEC", "SBB", "ADC"):
            ops = read_modify_write(normalized, lifter,
                                    lambda insn, operand: arithmetic(insn, lifter, operand))
        elif mnem in ("SHL", "SHR"):
            ops = read_modify_write(normalized, lifter,
                                    lambda insn, operand: shift(insn, lifter, operand))
        elif mnem == "DIV":
            ops = divide(normalized, lifter)
        elif mnem in ("AND", "OR", "XOR", "NOT"):
            ops = read_modify_write(normalized, lifter, lambda insn, operand: list(insn.ops))
        else:
            ops = normalized.ops
        if i in fir.seh:
            ops = seh_ops(ops, fir.seh[i], ins.addr)
        if i in fir.noreturn:
            ops = ops + [Op("TRAP", None, [], fir.noreturn[i])]
        result = Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                      ins.x87, ins.internal_flow, ins.userops, ins.raw)
        result.cc = _flag_producer(mnem, ops, flag_offsets)
        insns.append(result)
    succ = add_exit_stubs(insns, fir.succ, fir.exits, tails)
    return FunctionIR(fir.addr, insns, succ, fir.tables, entries=fir.entries)


#: Codegen ops whose seams materialise flags eagerly before or after running.
#: They end the region as observers, so the settle is kept.
_SSA_EAGER_OPS = frozenset(("DIV32", "IDIV32", "STRINGOP", "BRANCHIND", "CALLOTHER"))


def cc_action_lines(action):
    """The C statement for one lazy-flag region decision."""
    if action == flag_region.DROP:
        return ["x86_cc_drop(c);"]
    if action == flag_region.REMOVE:
        return []
    if isinstance(action, tuple):
        helper, flags = action
        bits = " | ".join("X86_CCF_%s" % f.upper() for f in sorted(flags))
        return ["%s(c, %s);" % (helper, bits)]
    return ["x86_cc_settle(c);"]


def call_cc_action(summary, live):
    """The post-call action for a region summary and the flags read from the fields.

    A descriptor whose `cc_mask` misses every needed flag leaves those fields
    current: with no write in the region it stays pending, and once every
    flag it defines is overwritten before the next boundary it is dropped.
    """
    if summary is None:
        return flag_region.SETTLE
    reads, writes, must = summary
    need = reads | live if not writes else reads | live | (flag_region.ALL_FLAGS - must)
    if not need:
        return flag_region.REMOVE if not writes else flag_region.DROP
    if need == flag_region.ALL_FLAGS:
        return flag_region.SETTLE
    return ("x86_cc_settle_mask" if not writes else "x86_cc_settle_or_drop", need)


def ssa_settle_plan(cgi, flag_off_name):
    """Per-site lazy-flag decisions for one SSA body.

    Returns ``(entry, calls)``: the decision at the body entry and a mapping
    from each call instruction index to the post-call region summary
    (`flag_region.summarize`), which `call_cc_action` turns into a decision.
    The classification is over the codegen ops, so it matches what the
    emitter actually materialises: an eager seam is an observer, a call is a
    normal region boundary, and unknown forms settle.
    """
    n = len(cgi.insns)

    def access(i):
        ins = cgi.insns[i]
        if ins.mnem.upper() in ("CALL", "RET"):
            return (frozenset(), frozenset())
        if any(op.opc in _SSA_EAGER_OPS for op in ins.ops):
            return None
        reads, writes = set(), set()
        for op in ins.ops:
            if op.out is not None and op.out[0] == "register":
                for k in range(op.out[2]):
                    name = flag_off_name.get(op.out[1] + k)
                    if name:
                        writes.add(name)
            for v in op.ins:
                if v is not None and v[0] == "register":
                    for k in range(v[2]):
                        name = flag_off_name.get(v[1] + k)
                        if name:
                            reads.add(name)
        return (frozenset(reads), frozenset(writes))

    def successors(i):
        # Out-of-body targets stay in the list: the analysis treats them as
        # observers rather than silently ending the path.
        return list(cgi.succ[i])

    def is_end(i):
        return cgi.insns[i].mnem.upper() in ("CALL", "RET")

    def summarize(start):
        return flag_region.summarize(start, successors, access, is_end,
                                     lambda i: False, n)

    indices = {ins.addr: k for k, ins in enumerate(cgi.insns)}
    start = indices.get(cgi.addr)
    entry = flag_region.SETTLE
    if start is not None:
        found = summarize(start)
        entry = flag_region.SETTLE if found is None else flag_region.decision(*found)
    calls = {}
    for i, ins in enumerate(cgi.insns):
        if ins.mnem.upper() != "CALL":
            continue
        after = successors(i)
        calls[i] = summarize(after[0]) if after else None
    return entry, calls


def live_flag_reload_calls(s, live, flag_off_name):
    """Block index -> flags whose post-call reload is live.

    The SSA builder replaces every tracked flag with a fresh ``CALL_RELOAD``
    of the callee's fields after a call.  ``simplify`` keeps only the live,
    un-forwarded reloads, and the emitter lowers each surviving one to a
    direct read of the flag fields right after the site's settle.  A reload
    can be live because a guest instruction later reads it, because a RET
    publishes it, or because a following call's publication snapshot (a
    callee that reads or preserves flags) roots it.  Only the call that owns
    such a reload needs its flags materialised; its neighbours keep their
    region decision.
    """
    calls = {}
    for index, block in s.blocks.items():
        if not any(v.opc in ("CALL", "CALLIND") for v in block.ops):
            continue
        for v in block.ops:
            if (v.opc == "CALL_RELOAD" and v.data[0] == "register" and v.data[1] in flag_off_name
                    and v.id in live and s.resolve(v) is v):
                calls.setdefault(index, set()).add(flag_off_name[v.data[1]])
    return calls


@dataclass
class PreparedFunction:
    options: dict
    ir: FunctionIR
    ssa: object
    live: set
    fields: list
    mapping: dict
    flag_keys: set
    kept_calls: dict
    transfers: transfer_plan.TransferPlan
    cc_entry: object
    cc_calls: dict
    seh_mark: bool
    arithmetic: x87_plan.X87Plan
    decisions: list

    @property
    def requests(self):
        return {(v.data, self.transfers.calls[v.id].output_mask)
                for b in self.ssa.blocks.values() for v in b.ops
                if v.opc == "CALL" and v.data in self.options["contract_calls"]}

    def bind_layouts(self, masks):
        for b in self.ssa.blocks.values():
            for v in b.ops:
                if v.opc != "CALL" or v.data not in self.options["contract_calls"]:
                    continue
                call = self.transfers.calls[v.id]
                if call.outputs is None:
                    continue
                needed = contract_output_mask(call.outputs)
                layout = masks.get(v.data, needed)
                if needed & ~layout or layout & ~((1 << len(CONTRACT_GPRS + CONTRACT_FLAGS)) - 1):
                    raise SSAError("shared contract outputs do not cover the call continuation")
                call.output_mask = layout
                call.mode_proven = self.options["x87_mode_sites"].get(b.insn.addr) == (0x0f00, 0)
        for row in self.decisions:
            if row["specialization"] == "private":
                target = int(row["target"], 16)
                row["entry_output_mask"] = "%04x" % masks.get(target, int(row["entry_output_mask"], 16))


def validate_operations(s):
    supported = frozenset((
        "MEMORY", "X87_REG", "X87_MEM", "CALL", "CALL_RELOAD", "CALL_KEEP", "CALLIND",
        "STRINGOP", "LOAD", "STORE", "DIV32", "IDIV32", "RETURN", "SEH", "TRAP", "TAIL",
        "BRANCHIND", "BRANCH", "CBRANCH", "PACK", "BYTE", "COPY", "INT_ZEXT", "INT_SEXT",
        "INT_ADD", "INT_SUB", "INT_MULT", "INT_AND", "INT_OR", "INT_XOR", "INT_EQUAL",
        "INT_NOTEQUAL", "INT_LESS", "INT_LESSEQUAL", "BOOL_AND", "BOOL_OR", "BOOL_XOR",
        "INT_SLESS", "INT_SLESSEQUAL", "INT_LEFT", "INT_RIGHT", "INT_SRIGHT", "INT_CARRY",
        "INT_SCARRY", "INT_SBORROW", "BOOL_NEGATE", "INT_NEGATE", "INT_2COMP", "POPCOUNT",
        "SUBPIECE", "PIECE"))
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc not in supported:
                raise SSAError("unsupported integer operation %s" % v.opc)


def prepare(fir, symbol, *, call_symbols=None, x87_scalar_strict=False, local_state=True,
         msvc_convention=True, lazy_flags=False, lifter=None, indirect_call_symbol=None,
         call_contracts=None, x87_cw_clone=True, tail_symbols=None,
         checked_return=False, checked_calls=(), contract_calls=(), return_fields=None,
         variant_requests=None, call_decisions=None, x87_known_mode=False, contract_masks=None, x87_mode_sites=None, x87_entry_verify=None):
    """Prepare a complete body plan or raise SSAError for whole-function fallback.

    `call_symbols` maps an allowed direct-call target address to the C symbol
    that implements it. With no mapping, a direct CALL is rejected rather than
    dispatched or approximated. `indirect_call_symbol` is the explicit opt-in
    for indirect CALL effects: when set, CALLIND lowers to that runtime
    dispatch and reloads all tracked state; when None, indirect calls stay a
    whole-function fallback. The defaults are the production policy (scalar x87,
    local CPU state). `x87_scalar_strict=True` keeps pre-load x87 observations and
    `local_state=False` keeps every pre-access GPR/flag snapshot.

    `msvc_convention` (the `msvc_x87_convention` setting) assumes the MSVC
    x87 stack convention at calls and returns: flushes skip popped residue under
    the empty-above-TOP invariant (`x87_scalar.py`). False restores the
    conservative publication.

    `call_contracts` maps a direct-call target address to a contract
    (``reads``/``kills``/``writes`` field sets, e.g. from ``call_contracts.py``).
    When given, a direct CALL drops publication of a field the callee neither
    reads nor preserves (``F not in reads and F in kills``). A field the callee
    neither reads nor writes is kept: it is not published, and the caller's
    value continues past the call, so it is published later wherever the plan
    still requires it. Flags are kept only all six together, so a pending
    descriptor passes through untouched. Bodies with SEH effects keep nothing.
    The mapping is plain data so it survives the emission process pool; a
    missing target keeps the conservative full publication. ESP/EBP are never
    dropped or kept.

    `checked_calls` selects bound symbols that take the decoded guest
    continuation as a second argument. `checked_return` emits a shared body
    with explicit expected-continuation and enable arguments; its ordinary
    wrapper disables the comparison, preserving general return handling.

    `contract_calls` selects checked leaf edges for continuation-output
    specialization. `return_fields` emits a private leaf's normal-return
    publication, with full changed state retained for unexpected returns.

    `x87_mode_sites` supplies instruction facts from the separate CW analysis.
    General emission folds proven bits; hooks use the general recipe.

    The shared body pass selects arithmetic and state-transfer capabilities
    before lowering. `x87_cw_clone` retains guarded D3D8 arithmetic for ordinary
    entries within the operation, density and activation budgets. Private x87
    entries use that lowering with an entry CW guard and reduced publication.

    `x87_known_mode` emits only the fast private body. Its entry preparation
    must prove PC = RC = 0, and byte-backed analysis must prove the leaf has
    no calls or control-word effects. Full guest x87 publication is retained.
    """
    options = {name: value for name, value in locals().items()
               if name in ("symbol", "checked_return", "return_fields", "lazy_flags",
                           "call_symbols", "tail_symbols", "checked_calls", "contract_calls",
                           "x87_mode_sites", "x87_entry_verify")}
    x87_mode_sites = (x87_mode_sites or {}) if not x87_scalar_strict else {}
    mode_boundaries = {ins.addr for ins in fir.insns}
    if any(site not in mode_boundaries or known & ~0x0f00 or value & ~known
           for site, (known, value) in x87_mode_sites.items()):
        raise SSAError("invalid instruction control-word mode metadata")
    if x87_known_mode:
        from . import x87_mode
        if (return_fields is None or x87_scalar_strict or not x87_cw_clone
                or not x87_mode.mode_preserving_leaf(x87_mode.prepare(fir))):
            raise SSAError("known x87 mode requires a guarded private control-word-preserving leaf")
    report_calls = call_decisions is not None
    call_decisions = []
    checked_calls = frozenset(checked_calls)
    contract_calls = frozenset(contract_calls)
    contract_masks = contract_masks or {}
    if not contract_calls <= checked_calls:
        raise SSAError("state contracts require checked calls")
    if return_fields is not None and not x87_known_mode and any(
            op.opc in ("X87_REG", "X87_MEM", "X87_OPAQUE")
            for ins in codegen_ir(fir, lifter or Lifter()).insns for op in ins.ops):
        raise SSAError("private x87 entry requires guarded D3D8 arithmetic")
    if return_fields is not None and (not checked_return or any(
            op.opc in ("CALL", "CALLIND", "DIV32", "IDIV32", "STRINGOP", "SEH")
            for ins in codegen_ir(fir, lifter or Lifter()).insns for op in ins.ops)):
        raise SSAError("private state entry requires a checked leaf without opaque observers")
    if checked_return and (fir.entries or fir.seh or fir.exits):
        raise SSAError("checked return requires a single-entry closed body without SEH")
    if not symbol.isidentifier() or not symbol.isascii():
        raise SSAError("invalid C symbol")
    # The register model and operand corrections are immutable across bodies.
    # Reuse the image's SLEIGH context instead of reparsing its specification
    # for every function.
    lifter = lifter if lifter is not None else Lifter()
    try:
        entries = list(dict(call_symbols or {}).items())
    except (TypeError, ValueError):
        raise SSAError("invalid direct-call target map")
    call_symbols = {}
    for target, name in entries:
        # An exact 32-bit guest address, never a bool, float or numeric string
        # that int() would silently round or reinterpret.
        if type(target) is not int or not 0 <= target <= 0xffffffff:
            raise SSAError("invalid direct-call target %r" % (target,))
        if target in call_symbols:
            raise SSAError("duplicate direct-call target %08x" % target)
        if not isinstance(name, str) or not name.isidentifier() or not name.isascii():
            raise SSAError("invalid call symbol %r for target %08x" % (name, target))
        call_symbols[target] = name
    if not checked_calls <= call_symbols.keys():
        raise SSAError("checked calls require bound direct-call symbols")
    if indirect_call_symbol is not None and (
            not isinstance(indirect_call_symbol, str)
            or not indirect_call_symbol.isidentifier()
            or not indirect_call_symbol.isascii()):
        raise SSAError("invalid indirect call symbol %r" % (indirect_call_symbol,))
    # DIVERGENCE(original): [ssa-x87-convention] FINCSTP/FDECSTP leave a tagged
    # register above TOP, so those functions keep the exact x87 flush.
    x87_convention = msvc_convention and not any(
        ins.mnem.upper().removeprefix("WAIT ") in ("FINCSTP", "FDECSTP") for ins in fir.insns)
    # Lazy NaN needs per-op deferral, so it is off for the exact flush (which
    # resets at edges) and strict x87.
    defer_ie = not x87_scalar_strict and msvc_convention
    # Each SLEIGH register byte must map to a known runtime field.
    fields = []
    for index, name in enumerate(("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")):
        fields.append((lifter.register(name), "c->r[%d]" % index))
    fields.append((lifter.register("EIP"), "c->eip"))
    # FS-relative accesses (the SEH chain head, TLS reads) lift to FS_OFFSET plus
    # the displacement. It is read-only guest state: no instruction here writes it.
    fields.append((lifter.register("FS_OFFSET"), "c->fs_base"))
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF", "DF"):
        fields.append((lifter.register(name), "c->eflags_%s" % name.lower()))
    mapping = {}
    for (_, off, size), field in fields:
        for n in range(size):
            mapping[("register", off + n)] = (field, n)
    flag_off_name = {}
    flag_first_key = {}
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF"):
        _, off, size = lifter.register(name)
        flag_first_key[name.lower()] = ("register", off)
        for n in range(size):
            flag_off_name[off + n] = name.lower()
    flag_keys = {key for key, (field, _) in mapping.items() if field in FLAG_FIELDS}
    groups = [[("register", off + n) for n in range(size)]
              for (_, off, size), _ in fields]
    lane_field, field_lanes = {}, {}
    for name in CONTRACT_GPRS:
        _, off, size = lifter.register(name)
        for lane in range(size):
            key = ("register", off + lane)
            lane_field[key] = name
            field_lanes.setdefault(name, set()).add(key)
    for name in CONTRACT_FLAGS:
        _, off, size = lifter.register(name)
        for n in range(size):
            key = ("register", off + n)
            lane_field[key] = name
            field_lanes.setdefault(name, set()).add(key)
    kept_fields = {}
    if call_contracts and not fir.seh:
        for target, contract in call_contracts.items():
            fields_ = {field for field in CONTRACT_FIELDS
                       if field not in CONTRACT_NEVER_SKIP and field not in contract.reads
                       and field not in contract.writes}
            if not set(CONTRACT_FLAGS) <= fields_:
                fields_ -= set(CONTRACT_FLAGS)
            if fields_:
                kept_fields[target] = frozenset(
                    key for field in fields_ for key in field_lanes[field])
    tail_symbols = dict(tail_symbols or {})
    seh_mark = any("orphan" in kinds for kinds in fir.seh.values())
    cgi = codegen_ir(fir, lifter, tail_symbols)
    fir = cgi
    # Per-site lazy-flag settle decisions over the same codegen ops the
    # emitter lowers, so an eager seam or a call boundary is classified the
    # way the generated body actually behaves.
    cc_entry, cc_calls = flag_region.SETTLE, {}
    if lazy_flags:
        cc_entry, cc_calls = ssa_settle_plan(cgi, flag_off_name)
    s = build(cgi,
              register_groups=groups,
              call_targets=call_symbols, indirect_call_symbol=indirect_call_symbol,
              flag_off_name=flag_off_name, kept_calls=kept_fields)
    if any(key != MEMORY and key not in mapping for key in s.inputs):
        raise SSAError("unmapped runtime register")
    canonicalize(s)
    # DIVERGENCE(original): [ssa-state-locals] guest load/store faults
    # and store watch callbacks may observe earlier GPR/flag values
    # under the agreed performance-mode policy. Keep diagnostic
    # EIP/ESP/EBP eager; division, string helpers, calls and returns
    # still publish every required field. The strict path keeps all
    # pre-access snapshots.
    access_fields = {key for key, (field, _) in mapping.items()
                     if field in ("c->eip", "c->r[4]", "c->r[5]")} if local_state else None
    # Compiler conventions do not cover every binary boundary: CRT
    # assembly helpers can return flags or consume incoming flags.
    # Keep flag publication at calls/returns until actual call
    # summaries prove which fields a boundary does not observe.
    kept_calls = {v.id: kept_fields[v.data] for b in s.blocks.values() for v in b.ops
                  if v.opc == "CALL" and v.data in kept_fields}
    publication_facts = publication_analysis(s, fir.succ, groups, access_fields=access_fields,
                                             reloaded=set(mapping) - flag_keys, kept=kept_calls)
    transfers = transfer_plan.build(s, groups, publication_facts,
                                    call_contracts or {}, kept_calls, lane_field, CONTRACT_NEVER_SKIP,
                                    contract_calls, return_fields, flag_keys, flag_first_key,
                                    CC_DEFINES, CC_BIT_VALUES, lazy_flags, CC_EAGER_EVENTS)
    if report_calls:
        def field_names(keys):
            return sorted({lane_field.get(key, "EIP" if mapping[key][0] == "c->eip" else mapping[key][0]) for key in keys if key in mapping})

        for b in s.blocks.values():
            for v in b.ops:
                if v.opc != "CALL":
                    continue
                call = transfers.calls[v.id]
                private = v.data in contract_calls
                reason = None if private else ("caller has alternate entries or SEH" if fir.entries or fir.seh
                                                else "target is not an admitted checked leaf")
                contract = (call_contracts or {}).get(v.data)
                call_decisions.append({
                    "caller": "%08x" % fir.addr, "site": "%08x" % b.insn.addr,
                    "target": "%08x" % v.data,
                    "specialization": "private" if private else "checked" if v.data in checked_calls else "ordinary",
                    "callee_reads": sorted(contract.reads) if contract is not None else None,
                    "fast_publication": field_names(call.publication.required),
                    "outputs": sorted(call.outputs) if private else None,
                    "entry_output_mask": "%04x" % contract_masks.get(v.data, contract_output_mask(call.outputs)) if private else None,
                    "ordinary_fallback": field_names(call.fallback.required) if private else None,
                    "fallback_flag_mask": call.fallback.flags.mask if call.fallback.flags else None,
                    "reason": reason,
                })
    live = simplify(s, transfers.live_publications, canonical=False, extra_roots=transfers.roots,
                    removable=frozenset(("CALL_RELOAD", "CALL_KEEP")), groups=groups)
    reads_entry_flags = any(
        v.opc == "INPUT" and v.data in flag_keys and v.id in live and s.resolve(v) is v
        for v in s.values)
    if lazy_flags:
        # A post-call reload reads the flag fields directly, right after the
        # site's action, so the action materialises a descriptor that defines
        # one of them. A reload can be live only as a following call's
        # publication root: a callee that reads or preserves flags forces the
        # caller to publish the fields before that call, so the earlier reload
        # must not read a stale field. A call whose flag reload is dead
        # (shadowed by a later call or overwritten) keeps its region decision.
        reloads = live_flag_reload_calls(s, live, flag_off_name)
        cc_calls = {i: call_cc_action(summary, frozenset(reloads.get(i, ())))
                    for i, summary in cc_calls.items()}
    if reads_entry_flags or fir.entries:
        # The SSA builder loads live entry flag INPUTs straight from the fields,
        # immediately after the entry settle.  Those loads are not codegen ops,
        # so the region walk cannot see them: it may have classified the entry
        # region as REMOVE or DROP while a caller descriptor is still pending,
        # and the later guest read would use a stale field.  Settle first.
        cc_entry = flag_region.SETTLE
    arithmetic_plan = x87_plan.prepare(s, fir, live, strict=x87_scalar_strict,
                                       convention=x87_convention, defer_ie=defer_ie,
                                       known_mode=x87_known_mode, cw_clone=x87_cw_clone,
                                       mode_sites=x87_mode_sites)
    options.update(call_symbols=call_symbols, tail_symbols=tail_symbols,
                   x87_mode_sites=x87_mode_sites, checked_calls=checked_calls,
                   contract_calls=contract_calls)
    prepared = PreparedFunction(options, fir, s, live, fields, mapping, flag_keys,
                                kept_calls, transfers, cc_entry, cc_calls, seh_mark,
                                arithmetic_plan, call_decisions)
    prepared.bind_layouts(contract_masks)
    validate_operations(s)
    return prepared


def emit_prepared(prepared):
    options = prepared.options
    symbol, checked_return = options["symbol"], options["checked_return"]
    return_fields, lazy_flags = options["return_fields"], options["lazy_flags"]
    call_symbols, tail_symbols = options["call_symbols"], options["tail_symbols"]
    checked_calls, contract_calls = options["checked_calls"], options["contract_calls"]
    x87_mode_sites, x87_entry_verify = options["x87_mode_sites"], options["x87_entry_verify"]
    fir, s, live = prepared.ir, prepared.ssa, prepared.live
    fields, mapping, flag_keys = prepared.fields, prepared.mapping, prepared.flag_keys
    kept_calls, transfers = prepared.kept_calls, prepared.transfers
    cc_entry, cc_calls, seh_mark = prepared.cc_entry, prepared.cc_calls, prepared.seh_mark
    arithmetic_plan = prepared.arithmetic
    scalar = arithmetic_plan.scalar()

    def flush_x87():
        return scalar.flush()

    def lower_x87(data, address, result):
        return scalar.statements(data, address, result)

    def ref(v):
        v = s.resolve(v)
        if v.opc in ("CONST", "TARGET"):
            return "0x%xull" % v.data
        if v.opc == "CC_KIND":
            return CC_OP_CONST[v.data]
        return "v%d" % v.id

    def mask(size):
        return "0x%xull" % ((1 << (size * 8)) - 1)

    def expression(v):
        a = [ref(arg) for arg in v.args]
        opc = v.opc
        if opc == "PACK":
            return " | ".join("(%s << %d)" % (arg, n * 8) for n, arg in enumerate(a))
        if opc == "BYTE":
            return "%s >> %d" % (a[0], v.data * 8)
        if opc in ("COPY", "INT_ZEXT"):
            return a[0]
        if opc == "INT_SEXT":
            sign = "0x%xull" % (1 << (v.args[0].size * 8 - 1))
            return "(%s ^ %s) - %s" % (a[0], sign, sign)
        binary = {"INT_ADD": "+", "INT_SUB": "-", "INT_MULT": "*", "INT_AND": "&", "INT_OR": "|",
                  "INT_XOR": "^", "INT_EQUAL": "==", "INT_NOTEQUAL": "!=",
                  "INT_LESS": "<", "INT_LESSEQUAL": "<=", "BOOL_AND": "&&",
                  "BOOL_OR": "||", "BOOL_XOR": "!=", "PIECE": None}
        if opc in binary and binary[opc]:
            return "%s %s %s" % (a[0], binary[opc], a[1])
        bits = v.args[0].size * 8 if v.args else 0
        if opc in ("INT_SLESS", "INT_SLESSEQUAL"):
            # Sign-bit bias compares two's-complement values without a signed
            # overflow or an implementation-defined unsigned-to-signed cast.
            sign = "0x%xull" % (1 << (bits - 1))
            return "(%s ^ %s) %s (%s ^ %s)" % (a[0], sign,
                    "<" if opc == "INT_SLESS" else "<=", a[1], sign)
        if opc in ("INT_LEFT", "INT_RIGHT"):
            return "%s >= %d ? 0ull : (%s %s %s)" % (a[1], bits, a[0],
                    "<<" if opc == "INT_LEFT" else ">>", a[1])
        if opc == "INT_SRIGHT":
            # P-code counts saturate, unlike the x86 helper's modulo-32 count.
            # Biasing the sign bit expresses sign extension with unsigned C.
            sign = "0x%xull" % (1 << (bits - 1))
            return "%s >= %d ? (0ull - (%s >> %d)) : (((%s ^ %s) >> %s) - (%s >> %s))" % (
                a[1], bits, a[0], bits - 1, a[0], sign, a[1], sign, a[1])
        if opc == "INT_CARRY":
            return "%s > (%s - %s)" % (a[0], mask(v.args[0].size), a[1])
        if opc in ("INT_SCARRY", "INT_SBORROW"):
            result = "(%s %s %s)" % (a[0], "+" if opc == "INT_SCARRY" else "-", a[1])
            return "((%s(%s ^ %s) & (%s ^ %s)) >> %d) & 1ull" % (
                "~" if opc == "INT_SCARRY" else "", a[0], a[1], a[0], result, bits - 1)
        if opc == "BOOL_NEGATE":
            return "!%s" % a[0]
        if opc == "INT_NEGATE":
            return "~%s" % a[0]
        if opc == "INT_2COMP":
            return "0ull - %s" % a[0]
        if opc == "POPCOUNT":
            return "__builtin_popcountll(%s)" % a[0]
        if opc == "SUBPIECE":
            return "%s >= 8 ? 0ull : (%s >> (%s * 8))" % (a[1], a[0], a[1])
        if opc == "PIECE":
            return "(%s << %d) | %s" % (a[0], v.args[1].size * 8, a[1])
        raise SSAError("unsupported integer operation %s" % opc)

    def field_value(state, field, keys):
        if all(key in state for key in keys):
            source = whole(s, [state[key] for key in keys])
            if source is not None:
                return ref(source)
        return " | ".join("(%s << %d)" % (ref(state[key]), n * 8) if key in state
                          else "(%s & 0x%xu)" % (field, 255 << (n * 8))
                          for n, key in enumerate(keys))

    def store_fields(state, required):
        """Direct field stores for the required keys (no descriptor)."""
        lines = []
        for (_, off, size), field in fields:
            keys = [("register", off + n) for n in range(size)]
            if any(key in required for key in keys):
                lines.append("%s = (uint32_t)(%s);" % (field, field_value(state, field, keys)))
        return lines

    def publish_fields(state, required, cc=None):
        lines = []
        record = None
        mask = 0
        covered = set()
        if cc is not None:
            record, mask = cc.record, cc.mask
            covered = {"c->eflags_%s" % name for name, bit in CC_BIT_VALUES.items() if mask & bit}
        stored_flag = False
        for (_, off, size), field in fields:
            keys = [("register", off + n) for n in range(size)]
            if not any(key in required for key in keys):
                continue
            if field in covered:
                continue
            lines.append("%s = (uint32_t)(%s);" % (field, field_value(state, field, keys)))
            if field in FLAG_FIELDS:
                stored_flag = True
        if record is not None:
            lines.append("c->cc_op = %s;" % (ref(record.op) if record.op is not None
                                             else CC_OP_CONST[record.kind]))
            lines.append("c->cc_size = %du;" % record.size)
            lines.append("c->cc_mask = 0x%xu;" % mask)
            lines.append("c->cc_a = (uint32_t)%s;" % (
                ref(record.a) if record.a is not None else "0"))
            lines.append("c->cc_b = (uint32_t)%s;" % (
                ref(record.b) if record.b is not None else "0"))
            lines.append("c->cc_res = (uint32_t)%s;" % ref(record.res))
            if record.carry is not None:
                lines.append("c->cc_carry = (uint8_t)%s;" % ref(record.carry))
        elif lazy_flags and stored_flag:
            # A direct field write must not leave an older descriptor pending.
            lines.append("c->cc_op = X86_CC_NONE;")
        return lines

    def publish(state, event):
        publication = transfers.publications[event.id]
        lines = publish_fields(state, publication.required, publication.flags)
        call = transfers.calls.get(event.id)
        if lazy_flags and call is not None and (call.skipped & set(CONTRACT_FLAGS)) and publication.flags is None:
            # The dropped flags have no descriptor at this seam, so an older
            # pending descriptor must not materialise into them after the call.
            lines.append("c->cc_op = X86_CC_NONE;")
        return lines

    def edge(source, target):
        # Phi assignments are parallel copies: stage every source before any
        # destination changes, including swaps on loop backedges.
        phis = [v for v in s.blocks[target].phis if v.size and s.resolve(v) is v]
        lines = ["{"]
        for v in phis:
            arg = v.args[v.data[1].index(source)]
            lines.append("uint64_t p%d = %s;" % (v.id, ref(arg)))
        lines.extend("v%d = p%d;" % (v.id, v.id) for v in phis)
        lines.extend(["goto %s%d;" % (block_label, target), "}"])
        return lines

    clones = arithmetic_plan.clones
    block_label = "F" if clones[0] == "fast" else "B"

    lines = ["static void %s(X86 *c, uint32_t expected_return_) {" % symbol if return_fields is not None
             else "static void %s(X86 *c, uint32_t expected_return_, int checked_return_) {" % symbol if checked_return
             else "static void %s(X86 *c, uint32_t entry_) {" % symbol if fir.entries
             else "void %s(X86 *c) {" % symbol]
    for v in s.values:
        if v.id in live and v.size and v.opc not in ("CONST", "TARGET", "CC_KIND") and s.resolve(v) is v:
            lines.append("uint64_t v%d;" % v.id)
    scalar_declarations = len(lines)
    lines.extend(scalar.declarations())
    if lazy_flags:
        lines.extend(cc_action_lines(cc_entry))
    if seh_mark:
        lines.append("uint64_t seh_mark_ = recomp_seh_frame_mark(c);")
    for v in s.values:
        if v.opc != "INPUT" or not v.size or v.id not in live or s.resolve(v) is not v:
            continue
        field, lane = mapping[v.data]
        lines.append("v%d = (%s >> %d) & %s;" % (v.id, field, lane * 8, mask(v.size)))
    for v in getattr(s, "entry_ops", ()):
        if v.id in live and s.resolve(v) is v:
            lines.append("v%d = (%s) & %s;" % (v.id, expression(v), mask(v.size)))
    if x87_entry_verify:
        known, value = x87_entry_verify
        lines.append('if (RECOMP_UNLIKELY(!recomp_hooks_ever && (c->fpu_cw & 0x%xu) != 0x%xu)) recomp_x87_contract_violation(c, 0x%08xu, 0x%xu, 0x%xu);' % (known, value, fir.addr, known, value))
    if fir.entries:
        lines.append("switch (entry_) {")
        for e, block in zip(fir.entries, s.alternates):
            lines.append("case 0x%xu:" % e)
            lines.extend(edge(-1, block))
        lines.append("default:")
        lines.extend(edge(-1, s.entry))
        lines.append("}")
    else:
        lines.extend(edge(-1, s.entry))
    indices, predecessors = arithmetic_plan.indices, arithmetic_plan.predecessors
    scalar_binary32 = arithmetic_plan.binary32
    carry_mode = arithmetic_plan.carry_mode
    entry_shape, linear_prev = arithmetic_plan.entry_shape, arithmetic_plan.linear_prev
    lines[scalar_declarations:scalar_declarations] = arithmetic_plan.declarations()

    def seed_block(shape):
        arithmetic_plan.seed_block(scalar, shape, lines)

    def transition(source, targets):
        arithmetic_plan.transition(scalar, source, targets, lines)

    activations = []
    for clone in clones:
        scalar.reset()
        scalar.clone, scalar.activations = clone, 0
        block_label = "F" if clone == "fast" else "B"
        previous = None
        for i, b in s.blocks.items():
            scalar.proven_cw = x87_mode_sites.get(b.insn.addr)
            lines.append("%s%d:;" % (block_label, i))
            if carry_mode:
                # Seeding must run on every entry, including a branch that jumps
                # straight to this label, so it follows the label.
                if i not in linear_prev:
                    seed_block(entry_shape.get(i))
            elif previous is None or set(fir.succ[previous]) != {i} or predecessors[i] != {previous}:
                scalar.reset()
            for v in b.ops:
                if v.opc == "MEMORY":
                    continue
                if v.opc in ("X87_REG", "X87_MEM"):
                    scalar.binary32 = v.id in scalar_binary32
                    if v.opc == "X87_MEM":
                        lines.extend(publish(b.snapshots[v.id], v))
                    lines.append("{")
                    lines.extend(lower_x87(v.data, ref(v.args[1]) if v.opc == "X87_MEM" else None,
                                           "v%d" % v.id if v.size else None))
                    lines.append("}")
                elif v.opc == "CALL":
                    lines.extend(flush_x87())
                    scalar.reset()
                    # Publish every required field before the callee runs, then
                    # invoke only the explicitly bound symbol. The callee receives
                    # the guest return address already stored by the preceding
                    # CALL push, so its own RET owns ESP/EIP restoration.
                    outputs = transfers.calls[v.id].outputs if v.data in contract_calls else None
                    if outputs is not None:
                        call = transfers.calls[v.id]
                        output_mask, mode_proven = call.output_mask, call.mode_proven
                        lines.append("if (entry_contract_prepare_%08x(c, %du)) {" % (v.data, mode_proven))
                    lines.extend(publish(b.snapshots[v.id], v))
                    if outputs is None and transfers.calls[v.id].guard:
                        guarded = store_fields(b.snapshots[v.id], transfers.calls[v.id].guard)
                        if lazy_flags and any(key in flag_keys for key in kept_calls.get(v.id, ())):
                            guarded.append("c->cc_op = X86_CC_NONE;")
                        lines.append("if (RECOMP_UNLIKELY(recomp_hooks_ever)) {")
                        lines.extend(guarded)
                        lines.append("}")
                    name = call_symbols.get(v.data)
                    if name is None:
                        raise SSAError("%08x: no C symbol bound for call target %08x"
                                       % (b.insn.addr, v.data))
                    if outputs is not None:
                        lines.append("entry_contract_%08x_%04x(c, 0x%xu);" % (
                            v.data, output_mask, b.insn.addr + b.insn.length))
                        lines.append("} else {")
                        lines.extend(publish_fields(b.snapshots[v.id], transfers.calls[v.id].fallback.required,
                                                    transfers.calls[v.id].fallback.flags))
                        lines.append("%s(c, 0x%xu);" % (name, b.insn.addr + b.insn.length))
                        lines.append("}")
                    elif v.data in checked_calls:
                        lines.append("%s(c, 0x%xu);" % (name, b.insn.addr + b.insn.length))
                    else:
                        lines.append("%s(c);" % name)
                    if lazy_flags:
                        # A callee (SSA or otherwise) may leave a pending descriptor.
                        lines.extend(cc_action_lines(cc_calls.get(b.index)))
                elif v.opc == "CALL_RELOAD":
                    # Complete, callee-agnostic reload of one mapped state field.
                    field, lane = mapping[v.data]
                    lines.append("v%d = (%s >> %d) & %s;" % (v.id, field, lane * 8, mask(v.size)))
                elif v.opc == "CALL_KEEP":
                    field, lane = mapping[v.data]
                    lanes = v.args[1:]
                    source = whole(s, lanes) if len(lanes) > 1 else None
                    kept = ref(source) if source is not None else " | ".join(
                        "(%s << %d)" % (ref(x), n * 8) for n, x in enumerate(lanes))
                    settle = "x86_cc_settle(c), " if lazy_flags and field in FLAG_FIELDS else ""
                    lines.append("v%d = RECOMP_UNLIKELY(recomp_hooks_ever) ? (%s(%s >> %d) & %s) : (%s);"
                                 % (v.id, settle, field, lane * 8, mask(v.size), kept))
                elif v.opc == "CALLIND":
                    lines.extend(flush_x87())
                    scalar.reset()
                    # Publish the required pre-call state, including the target,
                    # then dispatch through the runtime. The return address was
                    # already stored by the preceding lifted CALL sequence, and the
                    # SSA builder reloads every tracked lane/flag afterward.
                    lines.extend(publish(b.snapshots[v.id], v))
                    lines.append("%s(c, (uint32_t)%s);" % (v.data, ref(v.args[1])))
                    if lazy_flags:
                        lines.extend(cc_action_lines(cc_calls.get(b.index)))
                elif v.opc == "STRINGOP":
                    lines.extend(flush_x87())
                    scalar.reset()
                    # Byte-audited string instruction. Publication before the helper
                    # and reloads afterward keep guest memory observers and fault
                    # snapshots in access-then-advance order; the helper owns
                    # EDI/ESI/ECX/DF exactly as the eager emitter's string calls do.
                    lines.extend(publish(b.snapshots[v.id], v))
                    lines.append("%s(c);" % v.data["helper"])
                elif v.opc in ("LOAD", "STORE", "DIV32", "IDIV32"):
                    # Guest accesses do not observe x87 state (see x87_scalar.py);
                    # only strict mode publishes before them. The division seam
                    # always sees the complete CPU.
                    if (scalar.observe_loads or v.opc not in ("LOAD", "STORE")):
                        lines.extend(flush_x87())
                    lines.extend(publish(b.snapshots[v.id], v))
                    if v.opc in ("DIV32", "IDIV32"):
                        scalar.reset()
                        helper = "div32" if v.opc == "DIV32" else "idiv32"
                        lines.append("%s(c, (uint32_t)%s, (uint32_t)%s);"
                                     % (helper, ref(v.args[2]), ref(v.args[3])))
                        lines.append("v%d = c->r[R_EAX] | ((uint64_t)c->r[R_EDX] << 32);" % v.id)
                    elif v.opc == "LOAD":
                        lines.append("v%d = rd%d((uint32_t)%s);" % (v.id, v.size * 8, ref(v.args[1])))
                    else:
                        lines.append("wr%d((uint32_t)%s, (uint%d_t)%s);" % (
                            v.args[2].size * 8, ref(v.args[1]), v.args[2].size * 8, ref(v.args[2])))
                elif v.opc == "RETURN":
                    lines.extend(flush_x87())
                    if return_fields is not None:
                        eip_keys = [key for key, (field, _) in mapping.items() if field == "c->eip"]
                        popped = field_value(b.exit, "c->eip", eip_keys)
                        lines.append("if (RECOMP_UNLIKELY(%s != expected_return_)) {" % popped)
                        lines.extend(publish_fields(b.exit, transfers.returns[v.id].fallback.required,
                                                    transfers.returns[v.id].fallback.flags))
                        lines.extend(["recomp_return(c);", "return;", "}"])
                    lines.extend(publish(b.exit, v))
                    if return_fields is not None:
                        pass
                    elif checked_return:
                        lines.append("if (!checked_return_ || RECOMP_UNLIKELY(c->eip != expected_return_)) recomp_return(c);")
                    else:
                        lines.append("recomp_return(c);")
                    lines.append("return;")
                elif v.opc == "SEH":
                    lines.extend(publish(b.snapshots[v.id], v))
                    lines.extend(seh_lines(v.data["kind"], v.data["eip"]))
                elif v.opc == "TRAP":
                    lines.append("recomp_unknown_call(c, 0x%xu); return;" % v.data)
                elif v.opc == "TAIL":
                    lines.extend(flush_x87())
                    lines.extend(publish(b.exit, v))
                    lines.append("%s(c); return;" % tail_symbols[v.args[0].data])
                elif v.opc == "BRANCHIND":
                    # A decoded jump table: the target address selects a case, any
                    # other value is a runtime jump exactly like the decoded
                    # emitter's default arm (publish everything, set EIP to the
                    # jump, `recomp_jump`). Case edges are ordinary internal edges.
                    grouped = {}
                    for j in sorted(set(fir.succ[i])):
                        grouped.setdefault(j, fir.insns[j].addr)
                    targets = list(grouped)
                    if carry_mode:
                        transition(i, targets)
                    else:
                        lines.extend(flush_x87())
                    lines.append("switch ((uint32_t)%s) {" % ref(v.args[0]))
                    for j, addr in grouped.items():
                        lines.append("case 0x%xu:" % addr)
                        lines.extend(edge(i, j))
                    lines.append("default:")
                    lines.extend(flush_x87() if carry_mode else [])
                    lines.extend(publish(b.exit, v))
                    lines.append("c->eip = 0x%xu; recomp_jump(c, (uint32_t)%s); return;" % (
                        b.insn.addr, ref(v.args[0])))
                    lines.append("}")
                elif v.opc == "BRANCH":
                    if carry_mode:
                        target = indices[v.args[0].data]
                        transition(i, [target])
                        lines.extend(edge(i, target))
                    else:
                        lines.extend(flush_x87())
                        lines.extend(edge(i, indices[v.args[0].data]))
                elif v.opc == "CBRANCH":
                    if carry_mode:
                        taken = indices[v.args[0].data]
                        fallthrough = indices[b.insn.addr + b.insn.length]
                        transition(i, [taken, fallthrough])
                        lines.append("if (%s)" % ref(v.args[1]))
                        lines.extend(edge(i, taken))
                        lines.extend(edge(i, fallthrough))
                    else:
                        lines.extend(flush_x87())
                        lines.append("if (%s)" % ref(v.args[1]))
                        lines.extend(edge(i, indices[v.args[0].data]))
                        lines.extend(edge(i, indices[b.insn.addr + b.insn.length]))
                else:
                    lines.append("v%d = (%s) & %s;" % (v.id, expression(v), mask(v.size)))
            if not any(v.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP") for v in b.ops):
                target = fir.succ[i][0]
                if carry_mode:
                    if linear_prev.get(target) != i:
                        transition(i, [target])
                    lines.extend(edge(i, target))
                else:
                    # A non-linear edge must publish before its goto. Never emit a
                    # predecessor-specific flush at a shared destination label.
                    if predecessors[target] != {i} or target != i + 1:
                        lines.extend(flush_x87())
                    lines.extend(edge(i, target))
            previous = i
        activations.append(scalar.activations)
    if tuple(activations) != arithmetic_plan.activations:
        raise SSAError("x87 emission differs from its activation plan")
    lines.append("}")
    lines[scalar_declarations:scalar_declarations] = scalar.temps
    return "\n".join(lines)


def emit(fir, symbol, **options):
    requests = options.get("variant_requests")
    decisions = options.get("call_decisions")
    prepared = prepare(fir, symbol, **options)
    source = emit_prepared(prepared)
    if requests is not None:
        requests.update(prepared.requests)
    if decisions is not None:
        decisions.extend(prepared.decisions)
    return source
