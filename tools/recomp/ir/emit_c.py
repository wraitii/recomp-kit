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
from .lift import Lifter, Insn, Op
from .integer import read_modify_write, arithmetic, shift, divide
from .integer_extra import EXTRA_MNEMONICS, correct as correct_extra
from .ssa import SSAError, MEMORY, build
from .cfg import FunctionIR
from .simplify import canonicalize, simplify, state_roots, whole, EFFECTS, EXITS
from .publication import plan
from . import flag_region
from . import x87


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
CONTRACT_GPRS = ("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")
CONTRACT_FLAGS = ("CF", "PF", "AF", "ZF", "SF", "OF")
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


#: Minimum PC/RC-sensitive x87 operations for a fast CW clone (x87_cw_clone).
CW_CLONE_MIN_OPS = 4
#: ...and at least one per this many instructions of the body.
CW_CLONE_DENSITY = 8
#: ...and at most this many guarded tracker activations: a leaf FP body that
#: is one tracker window.
CW_CLONE_MAX_GUARDS = 1


def emit(fir, symbol, *, call_symbols=None, x87_scalar_strict=False, local_state=True,
         msvc_convention=True, lazy_flags=False, lifter=None, indirect_call_symbol=None,
         call_contracts=None, x87_cw_clone=True, tail_symbols=None):
    """Return a complete C function or raise SSAError for whole-function fallback.

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

    `x87_cw_clone` (the `x87_cw_clone` setting) emits a performance body with
    at least `CW_CLONE_MIN_OPS` precision/rounding-sensitive x87 operations,
    and one per `CW_CLONE_DENSITY` instructions, whose fast clone guards at
    most `CW_CLONE_MAX_GUARDS` tracker activations,
    twice in one C function: a fast clone whose tracker activations guard
    PC = RC = 0 (the D3D8 single-precision, round-to-nearest control word)
    and fold CW to that constant, then the general clone it falls back to at
    the same activation. It is exact: the guard covers every other CW. Strict
    x87 never clones.
    """
    arguments = dict(locals())
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
    if indirect_call_symbol is not None and (
            not isinstance(indirect_call_symbol, str)
            or not indirect_call_symbol.isidentifier()
            or not indirect_call_symbol.isascii()):
        raise SSAError("invalid indirect call symbol %r" % (indirect_call_symbol,))
    from .x87_scalar import X87Scalar
    # DIVERGENCE(original): [ssa-x87-convention] FINCSTP/FDECSTP leave a tagged
    # register above TOP, so those functions keep the exact x87 flush.
    x87_convention = msvc_convention and not any(
        ins.mnem.upper().removeprefix("WAIT ") in ("FINCSTP", "FDECSTP") for ins in fir.insns)
    # Lazy NaN needs per-op deferral, so it is off for the exact flush (which
    # resets at edges) and strict x87.
    defer_ie = not x87_scalar_strict and msvc_convention
    scalar = X87Scalar(observe_loads=x87_scalar_strict, convention=x87_convention,
                       lazy_nan=defer_ie)

    def flush_x87():
        return scalar.flush()

    def lower_x87(data, address, result):
        return scalar.statements(data, address, result)

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
    publications = plan(s, fir.succ, groups, access_fields=access_fields,
                        reloaded=set(mapping) - flag_keys, kept=kept_calls)
    # Cross-function contracts: a direct CALL may omit publication of a field
    # the callee neither reads nor preserves.  A field the callee preserves is
    # not droppable even when this body does not read it back: the field's CPU
    # value can still flow out to this body's own caller, and the publication
    # plan's must-facts may not republish it at RET.
    contract_skip, contract_guard, contract_roots = {}, {}, []
    if call_contracts:
        for b in s.blocks.values():
            for v in b.ops:
                if v.opc != "CALL":
                    continue
                contract = call_contracts.get(v.data)
                if contract is None:
                    continue
                reads, kills = contract.reads, contract.kills
                skipped = set()
                for field in field_lanes:
                    if field in CONTRACT_NEVER_SKIP or field in reads or field not in kills:
                        continue
                    skipped.add(field)
                kept = tuple(key for key in kept_calls.get(v.id, ()) if key in s.inputs)
                if not skipped and not kept:
                    continue
                if skipped:
                    contract_skip[v.id] = skipped
                # A mod hook installed on the callee at runtime observes and may
                # rewrite the full CPU, so dropped and kept fields stay
                # publishable behind recomp_hooks_ever, and a kept field is
                # reread there after the call; keep their values live.
                guarded = tuple(key for key in publications[v.id]
                                if lane_field.get(key) in skipped) + kept
                contract_guard[v.id] = guarded
                state = b.snapshots[v.id]
                contract_roots.extend(state_roots(s, state, guarded, groups))
                publications[v.id] = tuple(
                    key for key in publications[v.id] if lane_field.get(key) not in skipped)
    # Decide which seams defer their flags as a descriptor before dead-value
    # elimination, so the descriptor's operands can be kept live as roots.
    cc_plan, cc_roots = {}, []
    live_publications = dict(publications)
    if lazy_flags:
        for b in s.blocks.values():
            for v in b.ops:
                if v.opc not in EFFECTS and v.opc not in EXITS:
                    continue
                record = b.cc_exit if v.opc in ("RETURN", "TAIL") else b.cc_snapshots.get(v.id)
                if record is None:
                    continue
                state = b.exit if v.opc in EXITS else b.snapshots[v.id]
                required = publications[v.id]
                defines = set(record.flags) if record.kind == "join" else set(CC_DEFINES[record.kind])
                mask = 0
                for name, key in flag_first_key.items():
                    if name not in defines or key not in required:
                        continue
                    rec = record.flags.get(name)
                    cur = state.get(key)
                    if rec is None or cur is None or s.resolve(cur) is not s.resolve(rec):
                        mask = 0
                        break
                    mask |= CC_BIT_VALUES[name]
                if not mask or v.opc in CC_EAGER_EVENTS:
                    continue
                cc_plan[v.id] = (record, mask)
                covered = {flag_first_key[name] for name, bit in CC_BIT_VALUES.items() if mask & bit}
                live_publications[v.id] = tuple(key for key in required if key not in covered)
                cc_roots.extend(x for x in (record.op, record.a, record.b, record.carry, record.res)
                                if x is not None)
    # A callee-state read with no use is dropped; that also confines post-call
    # settles to reloads whose value is needed.
    live = simplify(s, live_publications, canonical=False, extra_roots=cc_roots + contract_roots,
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

    def publish(state, event):
        lines = []
        required = publications[event.id]
        cc = cc_plan.get(event.id)
        record = None
        mask = 0
        covered = set()
        if cc is not None:
            record, mask = cc
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
        if lazy_flags and event.id in contract_skip \
                and (contract_skip[event.id] & set(CONTRACT_FLAGS)) and record is None:
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

    # Fast CW clone: worth its size only when enough operations read PC/RC.
    cw_sensitive = 0
    if x87_cw_clone and not scalar.observe_loads:
        for v in s.values:
            if v.opc not in ("X87_REG", "X87_MEM") or v.id not in live:
                continue
            m = v.data["mnem"]
            if (m.rstrip("P") in x87.ARITH or m.rstrip("P") in x87.INTEGER_ARITH
                    or m in ("FSQRT", "FRNDINT", "FIST", "FISTP")
                    or (v.opc == "X87_MEM" and m in ("FST", "FSTP"))):
                cw_sensitive += 1
    # The clone duplicates the integer code too; a mostly-integer body
    # (ProjectionMeshBuilder::Build: 25 x87 of ~300 instructions) doubled in
    # size for a 3% corpus gain, so also require a minimum x87 density.
    clones = (("fast", "general")
              if cw_sensitive >= CW_CLONE_MIN_OPS
              and cw_sensitive * CW_CLONE_DENSITY >= len(fir.insns)
              else (None,))
    block_label = "F" if clones[0] == "fast" else "B"

    lines = ["static void %s(X86 *c, uint32_t entry_) {" % symbol if fir.entries
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
    indices = {b.insn.addr: i for i, b in s.blocks.items()}
    predecessors = {i: set() for i in s.blocks}
    for e in [s.entry] + s.alternates:
        predecessors[e].add(-1)
    for i in s.blocks:
        for j in set(fir.succ[i]):
            predecessors[j].add(i)
    scalar_binary32 = set()
    if not scalar.observe_loads:
        # A one-operation read/modify/store run does not amortize the precision
        # selector and its extra live representations. Keep its double recipe.
        # Longer linear arithmetic runs retain the proven binary32 path. This
        # is a codegen-cost heuristic, never a relaxation of operand provenance.
        arithmetic = []

        def finish_scalar_run():
            if len(arithmetic) >= 2:
                scalar_binary32.update(arithmetic)
            arithmetic.clear()

        prev = None
        for i, b in s.blocks.items():
            if prev is not None and (set(fir.succ[prev]) != {i} or predecessors[i] != {prev}):
                finish_scalar_run()
            for v in b.ops:
                if v.opc in ("X87_REG", "X87_MEM"):
                    m = v.data["mnem"]
                    if m.rstrip("P") in ("FADD", "FSUB", "FSUBR", "FMUL"):
                        arithmetic.append(v.id)
                    if m in ("FXAM", "FIST", "FISTP", "FLDCW", "FNINIT", "FINIT") or (
                            v.opc == "X87_MEM" and m in ("FST", "FSTP", "FNSTSW", "FNSTCW")):
                        finish_scalar_run()
                elif v.opc in ("STORE", "DIV32", "IDIV32", "CALL", "CALLIND", "STRINGOP",
                               "RETURN", "TAIL", "TRAP", "BRANCH", "CBRANCH", "BRANCHIND"):
                    finish_scalar_run()
            prev = i
        finish_scalar_run()
    # Exact-flush functions (msvc_convention=False or FINCSTP/FDECSTP) keep the
    # per-edge flush. Carry only under the MSVC convention, where popped residue
    # may be relaxed; x87_scalar.snapshot() carries all popped parts otherwise.
    carry_mode = not scalar.observe_loads and scalar.convention
    entry_shape = None
    if carry_mode:
        from .x87_carry import analyze as analyze_carry, assert_covers
        from .x87_scalar import PART_TYPES, UNSAFE, carry_var, ordered_parts

        def carry_factory():
            return X87Scalar(observe_loads=False, convention=x87_convention)

        # The fixed point is needed because a loop header's shape depends on
        # its own backedge; see x87_carry.py.
        entry_shape, _exit_shape = analyze_carry(s, fir, carry_factory, scalar_binary32)
        declared = set()
        for i, b in s.blocks.items():
            shape = entry_shape.get(i)
            if shape is None or shape is UNSAFE or not shape.active:
                continue
            for position, slot in shape.slots:
                for part in ordered_parts(slot.parts):
                    if part not in dict(slot.const):
                        declared.add((position, part))
        lines[scalar_declarations:scalar_declarations] = [
            "%s %s;" % (PART_TYPES[part], carry_var(position, part))
            for position, part in sorted(declared)]

        # Only non-linear edges cut the tracker. A plain fallthrough from the
        # previous instruction keeps the live scalar locals and needs neither
        # canonical assignment nor entry reload.
        linear_prev = {}
        for i in s.blocks:
            preds = predecessors[i]
            if len(preds) != 1:
                continue
            prev = next(iter(preds))
            if prev == -1:
                continue
            shape = entry_shape.get(i)
            if shape is None or shape is UNSAFE:
                # The successor must reset, so this edge is a real cut and
                # must publish before it even though it looks like a
                # fallthrough.
                continue
            if (i == prev + 1 and set(fir.succ[prev]) == {i}
                    and not any(op.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP")
                                for op in s.blocks[prev].ops)):
                linear_prev[i] = prev

        def seed_block(shape):
            if shape is None or shape is UNSAFE or not shape.active:
                scalar.reset()
                return
            exprs = {}
            for position, slot in shape.slots:
                agreements = dict(slot.const)
                for part in ordered_parts(slot.parts):
                    if part not in agreements:
                        exprs[(position, part)] = scalar._temp(
                            carry_var(position, part), lines, PART_TYPES[part])
            scalar.seed(shape, exprs)

        def transition(source, targets):
            active = []
            fallback = False
            # Deferred NaNs never cross a non-linear internal edge: fold IE and
            # canonicalise the carried value so the planned shape is unchanged.
            scalar.fold_pending(lines)
            live = scalar.snapshot()
            for target in targets:
                shape = entry_shape.get(target)
                if shape is None or shape is UNSAFE:
                    fallback = True
                elif shape.active:
                    active.append((target, shape))
                else:
                    assert_covers(shape, live, "B%d->B%d" % (source, target))
            if active:
                scalar.normalize(lines)
                # Check every target against the edge state before any
                # materialization read: reading a slot another predecessor
                # dirtied raises the tracker's `high` mark, which is bookkeeping
                # for this edge, not state the other target inherits.
                live = scalar.snapshot()
                for target, shape in active:
                    assert_covers(shape, live, "B%d->B%d" % (source, target))
                # Both CBRANCH targets read the same tracker, so one canonical
                # assignment per (position, part) serves every carried target.
                assignments = {}
                for target, shape in active:
                    for position, slot in shape.slots:
                        agreements = dict(slot.const)
                        for part in ordered_parts(slot.parts):
                            name = carry_var(position, part)
                            if part not in agreements and name not in assignments:
                                assignments[name] = scalar._read(position, part, lines)
                for name, expr in assignments.items():
                    lines.append("%s = %s;" % (name, expr))
            if fallback:
                lines.extend(flush_x87())

    activations = []
    for clone in clones:
        if clone is not None:
            # Each clone replays the tracker from a clean state, so their
            # activation sequences line up; see X87Scalar._activation.
            block_label = "F" if clone == "fast" else "B"
            scalar.reset()
            scalar.clone, scalar.activations = clone, 0
        previous = None
        for i, b in s.blocks.items():
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
                    lines.extend(publish(b.snapshots[v.id], v))
                    if contract_guard.get(v.id):
                        guarded = store_fields(b.snapshots[v.id], contract_guard[v.id])
                        if lazy_flags and any(key in flag_keys for key in kept_calls.get(v.id, ())):
                            guarded.append("c->cc_op = X86_CC_NONE;")
                        lines.append("if (RECOMP_UNLIKELY(recomp_hooks_ever)) {")
                        lines.extend(guarded)
                        lines.append("}")
                    name = call_symbols.get(v.data)
                    if name is None:
                        raise SSAError("%08x: no C symbol bound for call target %08x"
                                       % (b.insn.addr, v.data))
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
                    lines.extend(publish(b.exit, v))
                    lines.extend(["recomp_return(c);", "return;"])
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
        if clone is not None:
            activations.append(scalar.activations)
    if len(set(activations)) > 1:
        raise SSAError("x87 CW clones activate at different points: %r" % activations)
    if activations and activations[0] > CW_CLONE_MAX_GUARDS:
        # Several tracker windows (calls, opaque recipes, reset edges): the
        # shared locals stay live across every guard into the general clone,
        # and the extra register pressure outweighed the folded selects
        # (RayTestTriangles 323 -> 331 ns with 13 guards). Emit one body.
        arguments["x87_cw_clone"] = False
        return emit(**arguments)
    lines.append("}")
    lines[scalar_declarations:scalar_declarations] = scalar.temps
    return "\n".join(lines)
