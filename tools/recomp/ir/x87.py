"""Byte-backed x87 codegen corrections as ordered runtime-semantic effects.

Raw SLEIGH FLOAT operations are not admitted: its rounding, status and tag
semantics differ from runtime/x86.h. Original instruction bytes select operands;
the existing helpers retain PC/RC, NaNs, exceptions, tags, exact integer metadata
and popped-slot residue. These are effect nodes, not scalar floating SSA yet.
No game addresses or inferred dead FPU fields appear in this adapter.

`statements` is the audited ordered lowering the scalar tracker (`x87_scalar.py`)
delegates to and validates against.
"""
import capstone
from capstone import x86_const as X

from .lift import Op
from .ssa import SSAError

ARITH = {"FADD": "+", "FSUB": "-", "FSUBR": "-", "FMUL": "*",
         "FDIV": "/", "FDIVR": "/"}
INTEGER_ARITH = {"FI" + name[1:]: operator for name, operator in ARITH.items()}
CONSTANTS = {"FLD1": "1.0", "FLDZ": "0.0", "FLDPI": "3.14159265358979323846",
             "FLDLN2": "0.69314718055994530942", "FLDL2E": "1.44269504088896340736",
             "FLDLG2": "0.30102999566398119521", "FLDL2T": "3.32192809488736234787"}
UNARY = {"FABS": "fabs(%s)", "FCHS": "-(%s)", "FSQRT": "fx87(c, sqrt(%s))",
         "FRNDINT": "fx87_exact(c, fround_cw(c, %s))",
         # Transcendentals keep the register's full precision (PC applies only
         # to arithmetic and FSQRT); F2XM1 uses expm1 to avoid cancellation.
         "FSIN": "fx87_exact(c, sin(%s))", "FCOS": "fx87_exact(c, cos(%s))",
         "F2XM1": "fx87_exact(c, expm1(%s * M_LN2))"}
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
    if mnem not in SUPPORTED:
        raise SSAError("%08x: unsupported x87 instruction %s" % (ins.addr, mnem))
    expected = ins.mnem.upper().removeprefix("WAIT ").replace("FN", "F", 1)
    if mnem.replace("FN", "F", 1) != expected:
        raise SSAError("%08x: x87 mnemonic disagrees with original bytes" % ins.addr)
    operands, ops, args = [], [], []
    for operand in decoded.operands:
        if operand.type == X.X86_OP_REG:
            name = decoded.reg_name(operand.reg).upper()
            if name.startswith("ST(") and name.endswith(")"):
                operands.append(("st", int(name[3:-1])))
            elif name == "AX" and mnem == "FNSTSW":
                operands.append(("ax", 0))
            else:
                raise SSAError("%08x: unsupported x87 register %s" % (ins.addr, name))
        elif operand.type == X.X86_OP_MEM:
            if args or decoded.addr_size != 4 or operand.mem.segment in (X.X86_REG_FS, X.X86_REG_GS):
                raise SSAError("%08x: unsupported x87 addressing" % ins.addr)
            address = lifter.fresh_unique(4)
            ops.append(Op("COPY", address, [("const", operand.mem.disp & 0xffffffff, 4)]))
            for register, scale in ((operand.mem.base, 1), (operand.mem.index, operand.mem.scale)):
                if not register:
                    continue
                value = lifter.register(decoded.reg_name(register).upper())
                if value[2] != 4:
                    raise SSAError("%08x: x87 address register is not 32-bit" % ins.addr)
                if scale != 1:
                    scaled = lifter.fresh_unique(4)
                    ops.append(Op("INT_MULT", scaled, [value, ("const", scale, 4)]))
                    value = scaled
                added = lifter.fresh_unique(4)
                ops.append(Op("INT_ADD", added, [address, value]))
                address = added
            args.append(address)
            operands.append(("mem", operand.size))
        else:
            raise SSAError("%08x: unsupported x87 operand" % ins.addr)
    out = lifter.fresh_unique(2) if operands == [("ax", 0)] else None
    effect = Op("X87_MEM" if args else "X87_REG", out, args,
                {"mnem": mnem, "operands": tuple(operands)})
    # Validate every shape before emitting any function, including dead paths.
    try:
        statements(effect.data, "address", "result")
    except SSAError as error:
        raise SSAError("%08x: %s" % (ins.addr, error)) from error
    ops.append(effect)
    if out:
        ops.append(Op("COPY", lifter.register("AX"), [out]))
    return ops


def statements(data, address=None, result=None):
    """Lower a validated effect using the comparison runtime's exact recipes."""
    m, operands = data["mnem"], data["operands"]
    memory = [size for kind, size in operands if kind == "mem"]
    slots = [index for kind, index in operands if kind == "st"]
    bits = memory[0] * 8 if memory else 0
    st = lambda n: "ST(c, %d)" % n
    lines = []

    def read(index):
        return st(index)

    def fail():
        raise SSAError("unsupported x87 operand shape %s %r" % (m, operands))

    def mem_value(integer=False):
        if integer:
            if bits not in (16, 32, 64):
                fail()
            return "(int%d_t)rd%d((uint32_t)%s)" % (bits, bits, address)
        if bits not in (32, 64, 80):
            fail()
        return "rdf%d((uint32_t)%s)" % (bits, address)

    def set_slot(index, value):
        lines.append("fset(c, %d, %s);" % (index, value))

    if m in CONSTANTS and not operands:
        lines.append("fpush(c, %s);" % CONSTANTS[m])
    elif m == "FLD" and len(operands) == 1:
        if slots:
            lines.append("fpush_st(c, %d);" % slots[0])
        else:
            lines.append("fpush(c, %s);" % mem_value())
    elif m == "FILD" and len(memory) == 1 and len(operands) == 1:
        # The helper sets exact-integer metadata from the promoted value, so
        # bind nothing here: the slot value is only later read through `c`.
        lines.append("fpush_int(c, %s);" % mem_value(True))
    elif m in ("FST", "FSTP") and len(operands) == 1:
        if slots:
            if slots[0]:
                lines.append("fcopy(c, %d, 0);" % slots[0])
        elif bits in (32, 64, 80):
            value = ("fto_float(c, %s)" % read(0)) if bits == 32 else read(0)
            lines.append("wrf%d((uint32_t)%s, %s);" % (bits, address, value))
        else:
            fail()
        if m == "FSTP":
            lines.append("fdrop(c);")
    elif m in ("FIST", "FISTP") and len(operands) == 1 and bits in (16, 32, 64):
        lines.append("wr%d((uint32_t)%s, (uint%d_t)fist_i%d(c));" % (bits, address, bits, bits))
        if m == "FISTP":
            lines.append("fdrop(c);")
    elif m in ARITH or m in INTEGER_ARITH or (m.endswith("P") and m[:-1] in ARITH):
        pop = m.endswith("P")
        base = m[:-1] if pop else m
        integer = base in INTEGER_ARITH
        operator = INTEGER_ARITH[base] if integer else ARITH[base]
        if memory and len(operands) == 1 and not pop:
            lines.append("double operand = %s;" % mem_value(integer))
            dst, lhs, rhs = 0, read(0), "operand"
        elif slots and not memory and len(slots) == len(operands) and not integer:
            dst = slots[0] if len(slots) == 2 else (slots[0] if pop else 0)
            src = slots[1] if len(slots) == 2 else (0 if pop else slots[0])
            lhs, rhs = read(dst), read(src)
        elif not operands and pop:
            dst, lhs, rhs = 1, read(1), read(0)
        else:
            fail()
        if base.endswith("R"):
            lhs, rhs = rhs, lhs
        value = "fdivz(c, %s, %s)" % (lhs, rhs) if operator == "/" else "%s %s %s" % (lhs, operator, rhs)
        set_slot(dst, "fx87(c, %s)" % value)
        if pop:
            lines.append("fdrop(c);")
    elif m in ("FCOM", "FCOMP", "FUCOM", "FUCOMP", "FICOM", "FICOMP", "FCOMPP", "FUCOMPP"):
        if memory and len(operands) == 1 and not m.endswith("PP"):
            lines.append("double operand = %s;" % mem_value(m.startswith("FI")))
            other = "operand"
        elif not memory and len(slots) == len(operands) and len(slots) <= 2:
            other = read(slots[-1] if slots else 1)
        else:
            fail()
        lines.append("%s(c, %s, %s);" % ("fucom" if m.startswith("FU") else "fcom", read(0), other))
        for _ in range(2 if m.endswith("PP") else int(m.endswith("P"))):
            lines.append("fdrop(c);")
    elif m in UNARY and not operands:
        set_slot(0, UNARY[m] % read(0))
    elif m in ("FPREM", "FPREM1") and not operands:
        set_slot(0, "fprem_common(c, %s, %s, %d)" % (read(0), read(1), int(m == "FPREM1")))
    elif m == "FPTAN" and not operands:
        lines.extend(["double v_ = %s;" % read(0), "fset(c, 0, fx87_exact(c, tan(v_)));", "fpush(c, 1.0);"])
    elif m == "FSINCOS" and not operands:
        lines.extend(["double v_ = %s;" % read(0), "fset(c, 0, fx87_exact(c, sin(v_)));",
                      "fpush(c, fx87_exact(c, cos(v_)));"])
    elif m == "FSCALE" and not operands:
        set_slot(0, "fx87_exact(c, fscale(%s, %s))" % (read(0), read(1)))
    elif m == "FPATAN" and not operands:
        set_slot(1, "fx87_exact(c, atan2(%s, %s))" % (read(1), read(0)))
        lines.append("fdrop(c);")
    elif m == "FYL2X" and not operands:
        set_slot(1, "fx87_exact(c, %s * log2(%s))" % (read(1), read(0)))
        lines.append("fdrop(c);")
    elif m == "FYL2XP1" and not operands:
        # log1p keeps the bits log2(x+1) loses for small x.
        set_slot(1, "fx87_exact(c, %s * log1p(%s) / M_LN2)" % (read(1), read(0)))
        lines.append("fdrop(c);")
    elif m == "FXCH" and not memory and len(slots) == len(operands) and len(slots) <= 2:
        other = slots[-1] if slots else 1
        lines.append("fxch(c, %d);" % other)
    elif m == "FTST" and not operands:
        lines.append("fcom(c, %s, 0.0);" % read(0))
    elif m == "FXAM" and not operands:
        # fxam reads c->st[0] itself, so a dirty ST(0) must be materialised.
        lines.append("fxam(c);")
    elif m in ("FNCLEX", "FCLEX") and not operands:
        lines.append("c->fpu_sw &= (uint16_t)~0x80ffu;")
    elif m == "FNSTSW" and operands == (("ax", 0),):
        lines.append("%s = fstsw(c);" % result)
    elif m in ("FNSTSW", "FNSTCW", "FLDCW") and operands == (("mem", 2),):
        if m == "FLDCW":
            lines.append("x87_set_cw(c, rd16((uint32_t)%s));" % address)
        else:
            lines.append("wr16((uint32_t)%s, %s);" % (address, "fstsw(c)" if m == "FNSTSW" else "c->fpu_cw"))
    elif m in ("FNINIT", "FINIT") and not operands:
        lines.append("x87_finit(c);")
    elif m in ("FDECSTP", "FINCSTP") and not operands:
        # TOP moves without touching a value slot: old ST(i-1) becomes ST(i)
        # for FDECSTP, old ST(i+1) becomes ST(i) for FINCSTP.
        lines.extend(["c->fpu_top = (c->fpu_top %s 1u) & 7u;" % ("-" if m == "FDECSTP" else "+"),
                      "c->fpu_sw &= (uint16_t)~0x0200u;"])
    elif m == "FFREE" and len(operands) == 1 and len(slots) == 1:
        lines.append("ftag_put(c, (c->fpu_top + %du) & 7u, FTAG_EMPTY);" % slots[0])
    elif m == "FNOP" and not operands:
        lines.append(";")
    else:
        fail()
    return lines
