"""Experimental C emission from integer SSA.

The production adapter admits a restricted subset through existing entry thunks;
this emitter alone does not implement hooks, SEH or alternate entries. Original
interior fault equivalence has not been validated. A direct CALL is emitted
only when the caller supplies an explicit target-to-symbol binding; otherwise
it is a whole-function fallback. Optimized bodies lower audited x87 effects
through the scalar tracker (`x87_scalar.py`); the raw `optimize=False` path uses
the comparison runtime's ordered helpers directly. Memory uses the
existing guest accessors, in instruction order, with explicit state
publication. No inferred convention permits discarding guest state.
"""
from .lift import Lifter, Insn, Op
from .integer import memory_arithmetic, shift, divide
from .integer_extra import EXTRA_MNEMONICS, correct as correct_extra
from .ssa import SSAError, MEMORY, build
from .cfg import FunctionIR
from .simplify import canonicalize, simplify, EFFECTS
from .publication import plan
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

# Call-contract fields.  The mask order is the poison helper's (see x86.h).
CONTRACT_GPRS = ("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")
CONTRACT_FLAGS = ("CF", "PF", "AF", "ZF", "SF", "OF")
CONTRACT_FIELDS = frozenset(CONTRACT_GPRS) | frozenset(CONTRACT_FLAGS)
#: The guest frame pointer and stack pointer are call-helper state even when a
#: contract says the callee overwrites them; never drop their publication.
CONTRACT_NEVER_SKIP = frozenset(("ESP", "EBP"))


def contract_poison_mask(fields):
    """Bitmask for `RECOMP_CONTRACT_POISON_CALL`: GPRs low, flags high."""
    mask = 0
    for index, name in enumerate(CONTRACT_GPRS):
        if name in fields:
            mask |= 1 << index
    for index, name in enumerate(CONTRACT_FLAGS):
        if name in fields:
            mask |= 1 << (8 + index)
    return mask

#: Lazy-flag producers recognised at a seam: (kind, primary p-code opcode).
#: The primary op must write a non-flag destination.  SUB/CMP, ADD, logic/TEST
#: and INC/DEC are the initial set; everything else stays eager.
CC_PRIMARY = {
    "ADD": ("add", "INT_ADD"),
    "SUB": ("sub", "INT_SUB"),
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
    for op in ops:
        if op.opc != opc or op.out is None:
            continue
        space, off, size = op.out
        if space not in ("register", "unique"):
            continue
        if space == "register" and any((off + n) in flag_offsets for n in range(size)):
            continue
        return {"kind": kind, "opc": opc, "size": size, "result": op.out}
    return None


#: Which arithmetic flags a recognised producer defines.  Flags absent are
#: preserved; the emitter stores them directly at the seam.
CC_DEFINES = {
    "add": ("cf", "pf", "af", "zf", "sf", "of"),
    "sub": ("cf", "pf", "af", "zf", "sf", "of"),
    "cmp": ("cf", "pf", "af", "zf", "sf", "of"),
    "logic": ("cf", "of", "zf", "sf", "pf"),
    "inc": ("pf", "af", "zf", "sf", "of"),
    "dec": ("pf", "af", "zf", "sf", "of"),
}
CC_OP_CONST = {"add": "X86_CC_ADD", "sub": "X86_CC_SUB", "cmp": "X86_CC_SUB",
               "logic": "X86_CC_LOGIC", "inc": "X86_CC_INC", "dec": "X86_CC_DEC"}
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
    * a self-contained single-op read-modify-write (e.g. NOT [abs]) becomes
      LOAD + compute + STORE;
    * a direct-ram destination that also computes flags (ADD/XOR/... [abs],reg)
      is rejected rather than approximated. Control-flow ram is untouched.
    """
    ops = list(ins.ops)
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
        if len(touched) != 1:
            raise SSAError("%08x: unsupported direct-ram read-modify-write" % ins.addr)
        op = touched[0]
        index = ops.index(op)
        src, dst = lifter.fresh_unique(size), lifter.fresh_unique(size)
        inputs = tuple(src if (v is not None and v[0] == "ram") else v for v in op.ins)
        ops[index:index + 1] = [Op("LOAD", src, [address]),
                                Op(op.opc, dst, inputs, op.data),
                                Op("STORE", None, [address, dst])]
        return ops
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


def codegen_ir(fir, lifter):
    """Apply audited integer/x87 corrections without changing raw census input."""
    flag_offsets = set()
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF"):
        _, off, size = lifter.register(name)
        flag_offsets.update(range(off, off + size))
    insns = []
    for ins in fir.insns:
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
        if mnem in EXTRA_MNEMONICS:
            ops = correct_extra(normalized, lifter)
        elif mnem in ("ADD", "SUB", "CMP", "INC", "DEC", "SBB", "ADC"):
            ops = memory_arithmetic(normalized, lifter)
        elif mnem in ("SHL", "SHR"):
            ops = shift(normalized, lifter)
        elif mnem == "DIV":
            ops = divide(normalized, lifter)
        else:
            ops = normalized.ops
        result = Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                      ins.x87, ins.internal_flow, ins.userops, ins.raw)
        result.cc = _flag_producer(mnem, ops, flag_offsets)
        insns.append(result)
    return FunctionIR(fir.addr, insns, fir.succ, fir.tables)


def emit(fir, symbol, *, optimize=True, publish_changed=True, wide_registers=True,
         call_symbols=None, x87_scalar_strict=False, local_state=True, msvc_convention=True,
         lazy_nan=False, lazy_flags=False, resumable_stacks=False, lifter=None,
         indirect_call_symbol=None, _guard_null_checks=True, facts=None,
         call_contracts=None):
    """Return a complete C function or raise SSAError for whole-function fallback.

    `call_symbols` maps an allowed direct-call target address to the C symbol
    that implements it. With no mapping, a direct CALL is rejected rather than
    dispatched or approximated. `indirect_call_symbol` is the explicit opt-in
    for indirect CALL effects: when set, CALLIND lowers to that runtime
    dispatch and reloads all tracked state; when None, indirect calls stay a
    whole-function fallback. The defaults are the production policy (scalar x87,
    local CPU state). `x87_scalar_strict=True` keeps pre-load x87 observations and
    `local_state=False` keeps every pre-access GPR/flag snapshot; null-check
    builds compile both strict forms, and either can be requested explicitly.

    `msvc_convention` (the `ir_ssa_msvc_convention` setting) assumes the MSVC
    x87 stack convention at calls and returns: flushes skip popped residue under
    the empty-above-TOP invariant (`x87_scalar.py`). False restores the
    conservative publication; null-check builds compile it False.

    `lazy_nan` (the `ir_ssa_x87_lazy_nan` setting) defers the per-arithmetic
    NaN check and indefinite canonicalisation to sinks and internal CFG edges,
    folding IE before any status read or publication. It is a representation
    change: with it off the emitted body is byte-identical to the eager
    `fx87`/`fx87_exact` forms. It is ignored by strict x87, the exact flush
    and `optimize=False`.

    `call_contracts` maps a direct-call target address to a contract
    (``reads``/``kills`` field sets, e.g. from ``call_contracts.py``). When
    given, a direct CALL drops publication of a field the callee neither reads
    nor preserves (``F not in reads and F in kills``). A preserved field is
    never dropped: its CPU value can still flow out to this body's caller even
    when this body does not read it back. The mapping is plain data so it
    survives the emission process pool; a missing target keeps the conservative
    full publication. ESP/EBP are never dropped.

    `facts`, when a dict, receives census facts about the performance body:
    whether an arithmetic flag is read from the CPU at entry or after a call,
    and whether the x87 flush kept
    the exact form. They describe the code; they do not gate emission.
    """
    if not symbol.isidentifier() or not symbol.isascii():
        raise SSAError("invalid C symbol")
    # The register model and operand corrections are immutable across bodies.
    # Reuse the image's SLEIGH context instead of reparsing its specification
    # for every function and both null-check policy variants.
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
    x87_statements = x87.statements
    if optimize and _guard_null_checks and (
            local_state or not x87_scalar_strict or msvc_convention):
        # Null-fault dispatch can expose CPU state to guest exception handlers.
        # Compile the strict observation path whenever that facility is enabled.
        options = dict(optimize=optimize, publish_changed=publish_changed, wide_registers=wide_registers,
                       call_symbols=call_symbols, resumable_stacks=resumable_stacks,
                       lifter=lifter, indirect_call_symbol=indirect_call_symbol,
                       lazy_nan=lazy_nan, call_contracts=call_contracts,
                       _guard_null_checks=False)
        strict = emit(fir, symbol, x87_scalar_strict=True, local_state=False, msvc_convention=False,
                      lazy_flags=False, **options)
        fast = emit(fir, symbol, x87_scalar_strict=x87_scalar_strict, local_state=local_state,
                    msvc_convention=msvc_convention, lazy_flags=lazy_flags, facts=facts, **options)
        return "#if defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS\n%s\n#else\n%s\n#endif" % (strict, fast)
    from .x87_scalar import X87Scalar
    msvc_convention = msvc_convention and optimize
    # DIVERGENCE(original): [ssa-x87-convention] FINCSTP/FDECSTP leave a tagged
    # register above TOP, so those functions keep the exact x87 flush.
    x87_convention = msvc_convention and not any(
        ins.mnem.upper().removeprefix("WAIT ") in ("FINCSTP", "FDECSTP") for ins in fir.insns)
    # Lazy NaN needs per-op deferral, so it is off for the exact flush (which
    # resets at edges), strict x87 and raw emission.
    defer_ie = lazy_nan and not x87_scalar_strict and msvc_convention
    scalar = X87Scalar(observe_loads=x87_scalar_strict, convention=x87_convention,
                       lazy_nan=defer_ie) if optimize else None

    def flush_x87():
        return scalar.flush() if scalar is not None else []

    def lower_x87(data, address, result):
        if scalar is not None:
            return scalar.statements(data, address, result)
        return x87_statements(data, address, result)

    # Keep a raw-SSA comparison path for measuring the passes independently.
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
    s = build(codegen_ir(fir, lifter),
              register_groups=groups if optimize and wide_registers else (),
              call_targets=call_symbols, indirect_call_symbol=indirect_call_symbol,
              flag_off_name=flag_off_name)
    if any(key != MEMORY and key not in mapping for key in s.inputs):
        raise SSAError("unmapped runtime register")
    publications = None
    if optimize:
        canonicalize(s)
        if publish_changed:
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
            publications = plan(s, fir.succ, groups, access_fields=access_fields)
    # Cross-function contracts: a direct CALL may omit publication of a field
    # the callee neither reads nor preserves.  A field the callee preserves is
    # not droppable even when this body does not read it back: the field's CPU
    # value can still flow out to this body's own caller, and the publication
    # plan's must-facts may not republish it at RET.
    contract_skip, contract_guard, contract_roots, contract_stats = {}, {}, [], [0, 0]
    if optimize and publications is not None and call_contracts:
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
                if not skipped:
                    continue
                contract_skip[v.id] = skipped
                contract_stats[0] += 1
                contract_stats[1] += len(skipped)
                # A mod hook installed on the callee at runtime observes the
                # full CPU, so the dropped fields stay publishable behind
                # recomp_hooks_ever; keep their values live for that path.
                guarded = tuple(key for key in publications[v.id]
                                if lane_field.get(key) in skipped)
                contract_guard[v.id] = guarded
                state = b.snapshots[v.id]
                contract_roots.extend(state[key] for key in guarded if key in state)
                publications[v.id] = tuple(
                    key for key in publications[v.id] if lane_field.get(key) not in skipped)
    if facts is not None:
        facts["call_contract_calls"] = contract_stats[0]
        facts["call_contract_fields_skipped"] = contract_stats[1]
    # Decide which seams defer their flags as a descriptor before dead-value
    # elimination, so the descriptor's operands can be kept live as roots.
    cc_plan, cc_roots = {}, []
    if lazy_flags and optimize and publications is not None:
        for b in s.blocks.values():
            for v in b.ops:
                if v.opc not in EFFECTS and v.opc not in ("RETURN", "BRANCHIND"):
                    continue
                record = b.cc_exit if v.opc == "RETURN" else b.cc_snapshots.get(v.id)
                if record is None:
                    continue
                state = b.exit if v.opc in ("RETURN", "BRANCHIND") else b.snapshots[v.id]
                required = publications[v.id]
                defines = set(CC_DEFINES[record.kind])
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
                cc_roots.extend(x for x in (record.a, record.b, record.res) if x is not None)
    if optimize:
        if publications is not None:
            live = simplify(s, publications, canonical=False, extra_roots=cc_roots + contract_roots)
        else:
            live = simplify(s, canonical=False)
    else:
        live = {v.id for v in s.values}
    reads_entry_flags = any(
        v.opc == "INPUT" and v.data in flag_keys and v.id in live and s.resolve(v) is v
        for v in s.values)
    reads_after_call = any(
        v.opc == "CALL_RELOAD" and v.data in flag_keys and v.id in live and s.resolve(v) is v
        for v in s.values)
    if facts is not None:
        facts["flags_read_at_entry"] = reads_entry_flags
        facts["flags_read_after_call"] = reads_after_call
        facts["x87_exact_flush"] = msvc_convention and not x87_convention
        facts["lazy_flags"] = bool(cc_plan)

    def ref(v):
        v = s.resolve(v)
        if v.opc in ("CONST", "TARGET"):
            return "0x%xull" % v.data
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

    # Whether this body writes any flag state.  Such a body must settle a
    # descriptor left by its caller or a callee before writing, or the fields
    # a newer descriptor does not cover would be lost (INC after a caller's
    # CMP keeps CF).  A body that never touches flags passes it through.
    cc_touch = [reads_entry_flags or reads_after_call]
    CC_SETTLE = "/*cc-settle*/"

    def store_fields(state, required):
        """Direct field stores for the required keys (no descriptor)."""
        lines = []
        for (_, off, size), field in fields:
            keys = [("register", off + n) for n in range(size)]
            if not any(key in required for key in keys):
                continue
            parts = ["(%s << %d)" % (ref(state[key]), n * 8)
                     if key in state else "(%s & 0x%xu)" % (field, 255 << (n * 8))
                     for n, key in enumerate(keys)]
            lines.append("%s = (uint32_t)(%s);" % (field, " | ".join(parts)))
        return lines

    def publish(state, event):
        lines = []
        required = state if publications is None else publications[event.id]
        if lazy_flags and getattr(event, "opc", None) in CC_EAGER_EVENTS:
            # These helpers may write flag fields directly.
            cc_touch[0] = True
        cc = cc_plan.get(event.id)
        record = None
        mask = 0
        covered = set()
        if cc is not None:
            record, mask = cc
            covered = {"c->eflags_%s" % name for name in CC_DEFINES[record.kind]}
        stored_flag = False
        for (_, off, size), field in fields:
            keys = [("register", off + n) for n in range(size)]
            if not any(key in required for key in keys):
                continue
            if field in covered:
                continue
            parts = ["(%s << %d)" % (ref(state[key]), n * 8)
                     if key in state else "(%s & 0x%xu)" % (field, 255 << (n * 8))
                     for n, key in enumerate(keys)]
            lines.append("%s = (uint32_t)(%s);" % (field, " | ".join(parts)))
            if field in FLAG_FIELDS:
                stored_flag = True
        if record is not None or stored_flag:
            cc_touch[0] = True
        if record is not None:
            lines.append("c->cc_op = %s;" % CC_OP_CONST[record.kind])
            lines.append("c->cc_size = %du;" % record.size)
            lines.append("c->cc_mask = 0x%xu;" % mask)
            lines.append("c->cc_a = (uint32_t)%s;" % (
                ref(record.a) if record.a is not None else "0"))
            lines.append("c->cc_b = (uint32_t)%s;" % (
                ref(record.b) if record.b is not None else "0"))
            lines.append("c->cc_res = (uint32_t)%s;" % ref(record.res))
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
        lines.extend(["goto B%d;" % target, "}"])
        return lines

    lines = ["void %s(X86 *c) {" % symbol]
    for v in s.values:
        if v.id in live and v.size and v.opc not in ("CONST", "TARGET") and s.resolve(v) is v:
            lines.append("uint64_t v%d;" % v.id)
    scalar_declarations = len(lines)
    if scalar is not None:
        lines.extend(scalar.declarations())
    if lazy_flags:
        lines.append(CC_SETTLE)
    for v in s.values:
        if v.opc != "INPUT" or not v.size or v.id not in live or s.resolve(v) is not v:
            continue
        field, lane = mapping[v.data]
        lines.append("v%d = (%s >> %d) & %s;" % (v.id, field, lane * 8, mask(v.size)))
    for v in getattr(s, "entry_ops", ()):
        if v.id in live and s.resolve(v) is v:
            lines.append("v%d = (%s) & %s;" % (v.id, expression(v), mask(v.size)))
    lines.extend(edge(-1, s.entry))
    indices = {b.insn.addr: i for i, b in s.blocks.items()}
    predecessors = {i: set() for i in s.blocks}
    predecessors[s.entry].add(-1)
    for i in s.blocks:
        for j in set(fir.succ[i]):
            predecessors[j].add(i)
    scalar_binary32 = set()
    if scalar is not None and not scalar.observe_loads:
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
                               "RETURN", "BRANCH", "CBRANCH", "BRANCHIND"):
                    finish_scalar_run()
            prev = i
        finish_scalar_run()
    # Exact-flush functions (msvc_convention=False or FINCSTP/FDECSTP) keep the
    # per-edge flush. Carry only under the MSVC convention, where popped residue
    # may be relaxed; x87_scalar.snapshot() carries all popped parts otherwise.
    carry_mode = scalar is not None and not scalar.observe_loads and scalar.convention
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
                    and not any(op.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND")
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

    previous = None
    for i, b in s.blocks.items():
        lines.append("B%d:;" % i)
        if carry_mode:
            # Seeding must run on every entry, including a branch that jumps
            # straight to this label, so it follows the label.
            if i not in linear_prev:
                seed_block(entry_shape.get(i))
        elif scalar is not None and (previous is None or set(fir.succ[previous]) != {i}
                                     or predecessors[i] != {previous}):
            scalar.reset()
        for v in b.ops:
            if v.opc == "MEMORY":
                continue
            if v.opc in ("X87_REG", "X87_MEM"):
                if scalar is not None:
                    scalar.binary32 = v.id in scalar_binary32
                if v.opc == "X87_MEM":
                    lines.extend(publish(b.snapshots[v.id], v))
                lines.append("{")
                lines.extend(lower_x87(v.data, ref(v.args[1]) if v.opc == "X87_MEM" else None,
                                       "v%d" % v.id if v.size else None))
                lines.append("}")
            elif v.opc == "CALL":
                lines.extend(flush_x87())
                if scalar is not None:
                    scalar.reset()
                # Publish every required field before the callee runs, then
                # invoke only the explicitly bound symbol. The callee receives
                # the guest return address already stored by the preceding
                # CALL push, so its own RET owns ESP/EIP restoration.
                lines.extend(publish(b.snapshots[v.id], v))
                if v.id in contract_skip:
                    # Validation builds poison the killed fields this contract
                    # dropped; production builds compile the macro to a no-op.
                    lines.append("RECOMP_CONTRACT_POISON_CALL(c, 0x%xu);"
                                 % contract_poison_mask(contract_skip[v.id]))
                # After poison, so a hooked run sees real values.
                if contract_guard.get(v.id):
                    guarded = store_fields(b.snapshots[v.id], contract_guard[v.id])
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
                    lines.append(CC_SETTLE)
                if resumable_stacks:
                    # Match the eager emitter's resumable-stack contract: a
                    # callee that diverted EIP did not resume the continuation.
                    lines.append("if (c->eip != 0x%x) return;" % (
                        b.insn.addr + b.insn.length))
            elif v.opc == "CALL_RELOAD":
                # Complete, callee-agnostic reload of one mapped state field.
                field, lane = mapping[v.data]
                lines.append("v%d = (%s >> %d) & %s;" % (v.id, field, lane * 8, mask(v.size)))
            elif v.opc == "CALLIND":
                lines.extend(flush_x87())
                if scalar is not None:
                    scalar.reset()
                # Publish the required pre-call state, including the target,
                # then dispatch through the runtime. The return address was
                # already stored by the preceding lifted CALL sequence, and the
                # SSA builder reloads every tracked lane/flag afterward.
                lines.extend(publish(b.snapshots[v.id], v))
                lines.append("%s(c, (uint32_t)%s);" % (v.data, ref(v.args[1])))
                if lazy_flags:
                    lines.append(CC_SETTLE)
                if resumable_stacks:
                    lines.append("if (c->eip != 0x%x) return;" % (
                        b.insn.addr + b.insn.length))
            elif v.opc == "STRINGOP":
                lines.extend(flush_x87())
                if scalar is not None:
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
                if scalar is not None and (scalar.observe_loads or v.opc not in ("LOAD", "STORE")):
                    lines.extend(flush_x87())
                lines.extend(publish(b.snapshots[v.id], v))
                if v.opc in ("DIV32", "IDIV32"):
                    if scalar is not None:
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
        if not any(v.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND") for v in b.ops):
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
    lines.append("}")
    if lazy_flags:
        settle = "x86_cc_settle(c);" if cc_touch[0] else None
        lines = [settle if line == CC_SETTLE else line for line in lines
                 if line != CC_SETTLE or settle]
    if scalar is not None:
        lines[scalar_declarations:scalar_declarations] = scalar.temps
    return "\n".join(lines)
