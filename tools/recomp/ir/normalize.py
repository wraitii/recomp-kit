"""Normalize lifted instructions into the operation set consumed by SSA."""

from .lift import Insn, Op
from .integer import read_modify_write, arithmetic, shift, divide
from .integer_extra import EXTRA_MNEMONICS, correct as correct_extra
from .ssa import SSAError
from .cfg import FunctionIR
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


#: Lazy-flag producers recognised at a seam: (kind, primary p-code opcode).
#: The primary op must write a non-flag destination. SUB/CMP, ADD, logic/TEST
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
    """Append SEH runtime effects; an orphan precedes the return it guards."""
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


def codegen_ir(fir, lifter, tails=()):
    """Apply audited integer/x87 corrections without changing raw census input."""
    if fir.language != lifter.language:
        raise SSAError("function language %s disagrees with lifter %s" % (fir.language, lifter.language))
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
                              False, False, [], ins.raw, ins.decode))
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
                              False, False, [], ins.raw, ins.decode))
            continue
        if mnem == "WAIT":
            # A bare WAIT only synchronises pending x87 exceptions; the port's
            # eager emitter treats it as a no-op, and no exception is delivered.
            insns.append(Insn(ins.addr, ins.length, ins.mnem, [], 0,
                              False, False, [], ins.raw, ins.decode))
            continue
        if mnem not in SUPPORTED_MNEMONICS:
            raise SSAError("%08x: C emitter: unsupported instruction %s" % (ins.addr, mnem))
        ops = normalize_direct_ram(ins, lifter)
        normalized = Insn(ins.addr, ins.length, ins.mnem, ops, ins.x87_delta,
                          ins.x87, ins.internal_flow, ins.userops, ins.raw, ins.decode)
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
                      ins.x87, ins.internal_flow, ins.userops, ins.raw, ins.decode)
        result.cc = _flag_producer(mnem, ops, flag_offsets)
        insns.append(result)
    for original, normalized in zip(fir.insns, insns):
        normalized.text = original.text
    succ = add_exit_stubs(insns, fir.succ, fir.exits, tails)
    return FunctionIR(fir.addr, insns, succ, fir.tables, entries=fir.entries, language=fir.language,
                      import_tails=fir.import_tails)
