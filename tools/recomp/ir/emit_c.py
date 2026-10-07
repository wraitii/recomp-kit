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
from .summary import FunctionIR
from .simplify import canonicalize, simplify
from .publication import plan
from . import x87


SUPPORTED_MNEMONICS = frozenset((
    "MOV", "MOVSX", "MOVZX", "XCHG", "NOT", "LEAVE", "LEA", "PUSH", "POP", "RET", "NOP", "ADD", "SUB", "CMP", "INC", "DEC",
    "ADC", "SBB", "SHL", "SHR", "DIV",
    "CLD", "STD",
    "TEST", "AND", "OR", "XOR", "JMP", "JZ", "JNZ", "JE", "JNE",
    "JA", "JAE", "JB", "JBE", "JC", "JNC", "JG", "JGE", "JL", "JLE", "JS", "JNS",
    "JO", "JNO", "JP", "JNP", "JPE", "JPO", "CALL",
)) | EXTRA_MNEMONICS


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
    insns = []
    for ins in fir.insns:
        mnem = ins.mnem.upper()
        if mnem.startswith("F") or ins.x87 or mnem.startswith("WAIT "):
            ops = x87.lower(ins, lifter)
            insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, 0,
                              False, False, [], ins.raw))
            continue
        # String MOVSD is byte-audited to the runtime helper rather than the
        # raw SLEIGH loop: SLEIGH advances ESI/EDI and decrements ECX before
        # the access, while the helper accesses first and then advances. Only
        # dword MOVSD (bare A5 and REP F3 A5) is admitted; SSE MOVSD and
        # address-size or other prefixes stay unsupported fallbacks.
        if mnem in ("MOVSD", "MOVSD.REP") and ins.raw in (b"\xa5", b"\xf3\xa5"):
            ops = [Op("MOVS32", None, [], {"rep": ins.raw.startswith(b"\xf3")})]
            insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, 0,
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
        insns.append(Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                          ins.x87, ins.internal_flow, ins.userops, ins.raw))
    return FunctionIR(fir.addr, insns, fir.succ)


def emit(fir, symbol, *, optimize=True, publish_changed=True, wide_registers=True,
         call_symbols=None, x87_scalar_strict=False, local_state=True, resumable_stacks=False,
         lifter=None, indirect_call_symbol=None, _guard_null_checks=True, _ceiling=frozenset()):
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

    `_ceiling` is private to the function corpus: a set of UNPROVEN relaxation
    letters from `ceiling.py` (A-E). It is not part of the agreed performance
    mode contract, requires optimized non-strict x87 and local-state SSA, and production
    selection never passes it.
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
    from .ceiling import RELAXATIONS, X87_RELAXATIONS, FLAG_FIELDS
    _ceiling = frozenset(_ceiling or ())
    if _ceiling:
        if not _ceiling <= frozenset(RELAXATIONS):
            raise SSAError("unknown ceiling relaxation %s" % ",".join(sorted(_ceiling - frozenset(RELAXATIONS))))
        if not (optimize and not x87_scalar_strict and local_state):
            raise SSAError("ceiling relaxations require optimized scalar x87 and local-state SSA")
    if optimize and _guard_null_checks and not _ceiling and (local_state or not x87_scalar_strict):
        # Null-fault dispatch can expose CPU state to guest exception handlers.
        # Compile the strict observation path whenever that facility is enabled.
        options = dict(optimize=optimize, publish_changed=publish_changed, wide_registers=wide_registers,
                       call_symbols=call_symbols, resumable_stacks=resumable_stacks,
                       lifter=lifter, indirect_call_symbol=indirect_call_symbol,
                       _guard_null_checks=False)
        strict = emit(fir, symbol, x87_scalar_strict=True, local_state=False, **options)
        fast = emit(fir, symbol, x87_scalar_strict=x87_scalar_strict, local_state=local_state, **options)
        return "#if defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS\n%s\n#else\n%s\n#endif" % (strict, fast)
    from .x87_scalar import X87Scalar
    scalar = X87Scalar(observe_loads=x87_scalar_strict) if optimize else None
    if scalar is not None and _ceiling:
        if _ceiling & X87_RELAXATIONS:
            from .x87_ceiling import X87Ceiling  # UNPROVEN ceiling C/D/E lowering.
            scalar = X87Ceiling(_ceiling)

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
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF", "DF"):
        fields.append((lifter.register(name), "c->eflags_%s" % name.lower()))
    mapping = {}
    for (_, off, size), field in fields:
        for n in range(size):
            mapping[("register", off + n)] = (field, n)
    groups = [[("register", off + n) for n in range(size)]
              for (_, off, size), _ in fields]
    # UNPROVEN ceiling B (corpus-only): flag lanes are dead across entry, calls
    # and return. Never populated by production selection.
    dead_flags = frozenset(key for key, (field, _) in mapping.items()
                           if field in FLAG_FIELDS) if "B" in _ceiling else frozenset()
    s = build(codegen_ir(fir, lifter),
              register_groups=groups if optimize and wide_registers else (),
              call_targets=call_symbols, indirect_call_symbol=indirect_call_symbol,
              dead_flag_keys=dead_flags)
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
            plan_groups = [g for g, (_, field) in zip(groups, fields)
                           if not ("B" in _ceiling and field in FLAG_FIELDS)]
            # UNPROVEN ceiling A: guest memory accesses publish nothing.
            unpublished = (frozenset(("LOAD", "STORE", "X87_MEM"))
                           if "A" in _ceiling else frozenset())
            publications = plan(s, fir.succ, plan_groups, access_fields=access_fields,
                                unpublished=unpublished)
        live = simplify(s, publications)
    else:
        live = {v.id for v in s.values}

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

    def publish(state, event):
        lines = []
        required = state if publications is None else publications[event.id]
        for (_, off, size), field in fields:
            keys = [("register", off + n) for n in range(size)]
            if not any(key in required for key in keys):
                continue
            parts = ["(%s << %d)" % (ref(state[key]), n * 8)
                     if key in state else "(%s & 0x%xu)" % (field, 255 << (n * 8))
                     for n, key in enumerate(keys)]
            lines.append("%s = (uint32_t)(%s);" % (field, " | ".join(parts)))
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
                elif v.opc in ("STORE", "DIV32", "IDIV32", "CALL", "CALLIND", "MOVS32",
                               "RETURN", "BRANCH", "CBRANCH"):
                    finish_scalar_run()
            prev = i
        finish_scalar_run()
    previous = None
    for i, b in s.blocks.items():
        if scalar is not None and (previous is None or set(fir.succ[previous]) != {i}
                                  or predecessors[i] != {previous}):
            scalar.reset()
        lines.append("B%d:;" % i)
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
                name = call_symbols.get(v.data)
                if name is None:
                    raise SSAError("%08x: no C symbol bound for call target %08x"
                                   % (b.insn.addr, v.data))
                lines.append("%s(c);" % name)
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
                if resumable_stacks:
                    lines.append("if (c->eip != 0x%x) return;" % (
                        b.insn.addr + b.insn.length))
            elif v.opc == "MOVS32":
                lines.extend(flush_x87())
                if scalar is not None:
                    scalar.reset()
                # Byte-audited dword string move. Publication before the helper
                # and reloads afterward keep guest memory observers and fault
                # snapshots in access-then-advance order; the helper owns
                # EDI/ESI/ECX/DF exactly as the eager emitter's rep_movsd does.
                lines.extend(publish(b.snapshots[v.id], v))
                lines.append("rep_movsd(c);" if v.data.get("rep") else "movsd(c);")
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
            elif v.opc == "BRANCH":
                lines.extend(flush_x87())
                lines.extend(edge(i, indices[v.args[0].data]))
            elif v.opc == "CBRANCH":
                lines.extend(flush_x87())
                lines.append("if (%s)" % ref(v.args[1]))
                lines.extend(edge(i, indices[v.args[0].data]))
                lines.extend(edge(i, indices[b.insn.addr + b.insn.length]))
            else:
                lines.append("v%d = (%s) & %s;" % (v.id, expression(v), mask(v.size)))
        if not any(v.opc in ("BRANCH", "CBRANCH", "RETURN") for v in b.ops):
            target = fir.succ[i][0]
            # A non-linear edge must publish before its goto. Never emit a
            # predecessor-specific flush at a shared destination label.
            if predecessors[target] != {i} or target != i + 1:
                lines.extend(flush_x87())
            lines.extend(edge(i, target))
        previous = i
    lines.append("}")
    if scalar is not None:
        lines[scalar_declarations:scalar_declarations] = scalar.temps
    return "\n".join(lines)
