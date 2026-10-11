"""Byte-backed x87 codegen corrections as ordered runtime-semantic effects.

Raw SLEIGH FLOAT operations are not admitted: its rounding, status and tag
semantics differ from runtime/x86.h. Original instruction bytes select operands;
the existing helpers retain PC/RC, NaNs, exceptions, tags, exact integer metadata
and popped-slot residue. These are effect nodes, not scalar floating SSA yet.
No game addresses or inferred dead FPU fields appear in this adapter.

`x87_transition.py` validates operand recipes independently of either emitter.
"""
import capstone
from capstone import x86_const as X

from .lift import Op
from .ssa import SSAError

from .x87_transition import ARITH, INTEGER_ARITH, CONSTANTS, UNARY, recipe

SUPPORTED = frozenset(ARITH) | frozenset(INTEGER_ARITH) | frozenset(CONSTANTS) | frozenset(UNARY) | {
    *(name + "P" for name in ARITH), "FLD", "FILD", "FST", "FSTP", "FIST", "FISTP",
    "FCOM", "FCOMP", "FCOMPP", "FUCOM", "FUCOMP", "FUCOMPP", "FICOM", "FICOMP",
    "FTST", "FXAM", "FXCH", "FPREM", "FPREM1", "FNCLEX", "FCLEX",
    "FNSTSW", "FSTSW", "FNSTCW", "FSTCW", "FLDCW", "FNINIT", "FINIT",
    "FDECSTP", "FINCSTP", "FFREE", "FNOP", "FPTAN", "FSINCOS", "FSCALE", "FPATAN", "FYL2X", "FYL2XP1",
}


def lower(ins, lifter):
    """Validate original operands and produce an ordered X87_MEM/REG node.

Only one x87 instruction, optionally preceded by WAIT, is allowed. Addresses
are explicit 32-bit integer p-code; segmented/16-bit addressing is rejected.
The descriptor carries validated operand roles rather than generated C text.
"""
    if ins.raw is None:
        raise SSAError("%08x: x87 requires original bytes" % ins.addr)
    record = getattr(ins, "decode", None)
    if (record is not None and record.address == ins.addr
            and record.source_bytes == ins.raw and record.length == ins.length):
        mnem = record.raw_mnemonic.upper()
        address_size = record.address_size
        decoded_operands = [
            (operand.kind, operand.reg, operand.width // 8, operand.base,
             operand.index, operand.scale, operand.displacement, operand.segment)
            for operand in record.operands]
    else:
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        decoder.detail = True
        decoded = list(decoder.disasm(ins.raw, ins.addr))
        if sum(i.size for i in decoded) != ins.length:
            raise SSAError("%08x: x87 operand decode disagrees with boundary" % ins.addr)
        meaningful = [i for i in decoded if i.mnemonic.upper() != "WAIT"]
        if len(meaningful) != 1:
            raise SSAError("%08x: expected one x87 instruction" % ins.addr)
        decoded = meaningful[0]
        mnem = decoded.mnemonic.upper()
        address_size = decoded.addr_size
        decoded_operands = []
        for operand in decoded.operands:
            if operand.type == X.X86_OP_REG:
                name = decoded.reg_name(operand.reg).upper()
                if name.startswith("ST(") and name.endswith(")"):
                    name = "ST" + name[3:-1]
                decoded_operands.append(("reg", name, operand.size, None, None, 1, 0, None))
            elif operand.type == X.X86_OP_MEM:
                mem = operand.mem
                decoded_operands.append((
                    "mem", None, operand.size,
                    decoded.reg_name(mem.base).upper() if mem.base else None,
                    decoded.reg_name(mem.index).upper() if mem.index else None,
                    mem.scale, mem.disp,
                    decoded.reg_name(mem.segment).upper() if mem.segment else None))
            else:
                decoded_operands.append(("unsupported", None, operand.size, None, None,
                                         1, 0, None))
    if mnem not in SUPPORTED:
        raise SSAError("%08x: unsupported x87 instruction %s" % (ins.addr, mnem))
    expected = ins.mnem.upper().removeprefix("WAIT ").replace("FN", "F", 1)
    if mnem.replace("FN", "F", 1) != expected:
        raise SSAError("%08x: x87 mnemonic disagrees with original bytes" % ins.addr)
    operands, ops, args = [], [], []
    for kind, name, size, base, index, scale, displacement, segment in decoded_operands:
        if kind in ("reg", "st"):
            if name.startswith("ST"):
                operands.append(("st", int(name[2:])))
            elif name == "AX" and mnem == "FNSTSW":
                operands.append(("ax", 0))
            else:
                raise SSAError("%08x: unsupported x87 register %s" % (ins.addr, name))
        elif kind == "mem":
            if args or address_size != 4 or segment in ("FS", "GS"):
                raise SSAError("%08x: unsupported x87 addressing" % ins.addr)
            address = lifter.fresh_unique(4)
            ops.append(Op("COPY", address, [("const", displacement & 0xffffffff, 4)]))
            for register, factor in ((base, 1), (index, scale)):
                if not register:
                    continue
                value = lifter.register(register.upper())
                if value[2] != 4:
                    raise SSAError("%08x: x87 address register is not 32-bit" % ins.addr)
                if factor != 1:
                    scaled = lifter.fresh_unique(4)
                    ops.append(Op("INT_MULT", scaled, [value, ("const", factor, 4)]))
                    value = scaled
                added = lifter.fresh_unique(4)
                ops.append(Op("INT_ADD", added, [address, value]))
                address = added
            args.append(address)
            operands.append(("mem", size))
        else:
            raise SSAError("%08x: unsupported x87 operand" % ins.addr)
    out = lifter.fresh_unique(2) if operands == [("ax", 0)] else None
    effect = Op("X87_MEM" if args else "X87_REG", out, args,
                {"mnem": mnem, "operands": tuple(operands)})
    # Validate every shape before emitting any function, including dead paths.
    try:
        recipe(effect.data)
    except SSAError as error:
        raise SSAError("%08x: %s" % (ins.addr, error)) from error
    ops.append(effect)
    if out:
        ops.append(Op("COPY", lifter.register("AX"), [out]))
    return ops
