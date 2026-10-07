"""Codegen-only corrections for the signed dword integer cluster.

CDQ, the two/three-operand IMUL forms and the 8/16/32-bit one-operand IMUL
already lift to p-code the emitter expresses without correction, so they are
admitted unchanged. SETcc lifts to a byte condition/store and is admitted in
full. IDIV needs the checked runtime ``idiv32`` helper, NEG needs the AF
definition SLEIGH omits, and SAR needs the port's deterministic OF recipe.

Every other shape is rejected with a named diagnostic so the whole function
keeps the eager emitted original.

DIVERGENCE(original): OF for SAR counts greater than one is architecturally
undefined. This follows the port's ``sar*_f`` runtime recipe (OF=0 for every
nonzero count, preserved for a zero count) rather than SLEIGH's preserve-old
choice, so the SSA path matches the eager comparison contract. IDIV leaves
EFLAGS unchanged on a successful divide, exactly like the eager ``idiv32``
call. AF is not defined by SAR or IMUL/IDIV and stays untouched.
"""
from .lift import Op
from .ssa import SSAError

#: CDQ, IMUL and the sixteen SETcc conditions already lift to expressible
#: p-code. IDIV/NEG/SAR pass through correction below.
EXTRA_MNEMONICS = frozenset((
    "CDQ", "IMUL", "IDIV", "NEG", "SAR",
    "SETA", "SETAE", "SETB", "SETBE", "SETC", "SETE", "SETG", "SETGE",
    "SETL", "SETLE", "SETNA", "SETNAE", "SETNB", "SETNBE", "SETNC",
    "SETNE", "SETNG", "SETNGE", "SETNL", "SETNLE", "SETNO", "SETNP",
    "SETNS", "SETNZ", "SETO", "SETP", "SETPE", "SETPO", "SETS", "SETZ",
))


def correct(ins, lifter):
    """Return corrected operations for one admitted extra mnemonic."""
    mnem = ins.mnem.upper()
    if mnem == "IDIV":
        return divide_signed(ins, lifter)
    if mnem == "NEG":
        return negate(ins, lifter)
    if mnem == "SAR":
        return shift_arithmetic(ins, lifter)
    # CDQ, IMUL and SETcc: SLEIGH semantics are already expressible.
    return list(ins.ops)


def divide_signed(ins, lifter):
    """Replace the audited 64/32 signed IDIV shape with checked ``idiv32``.

    The helper handles divide-by-zero and the out-of-range quotient (including
    MIN/-1) before any register write, reports the original instruction address
    and leaves flags unchanged. Its packed result returns the quotient in the
    low dword and the remainder in the high dword. 8/16-bit IDIV and any
    unrecognized shape remain unsupported.
    """
    ops = list(ins.ops)
    divisions = [(n, op) for n, op in enumerate(ops) if op.opc == "INT_SDIV"]
    if len(divisions) != 1:
        raise SSAError("%08x: C emitter: unsupported IDIV shape" % ins.addr)
    n, op = divisions[0]
    remainders = [other for other in ops if other.opc == "INT_SREM"]
    if (op.out[2] != 8 or any(v[2] != 8 for v in op.ins) or len(remainders) != 1
            or remainders[0].ins != op.ins or len(ops[n:]) != 4
            or ops[n+1].out != lifter.register("EAX") or ops[n+1].opc != "SUBPIECE"
            or ops[n+1].ins != (op.out, ("const", 0, 4))
            or ops[n+2] is not remainders[0] or ops[n+3].out != lifter.register("EDX")
            or ops[n+3].opc != "SUBPIECE"
            or ops[n+3].ins != (remainders[0].out, ("const", 0, 4))):
        raise SSAError("%08x: C emitter: only signed IDIV32 is supported" % ins.addr)
    result = lifter.fresh_unique(8)
    return ops[:n] + [Op("IDIV32", result, op.ins + (("const", ins.addr, 4),)),
                      Op("SUBPIECE", lifter.register("EAX"), [result, ("const", 0, 4)]),
                      Op("SUBPIECE", lifter.register("EDX"), [result, ("const", 4, 4)])]


def negate(ins, lifter):
    """Add the NEG AF definition that SLEIGH omits.

    NEG defines AF as a borrow out of bit 3, which for ``0 - x`` is exactly the
    low nibble of the wrapped result being nonzero. The remaining flags come
    from SLEIGH unchanged. Memory destinations stay unsupported.
    """
    ops = list(ins.ops)
    results = [op for op in ops if op.opc == "INT_2COMP" and op.out is not None
               and op.out[0] == "register"]
    if (len(results) != 1 or any(op.opc in ("LOAD", "STORE") for op in ops)
            or any(op.out == lifter.register("AF") for op in ops)):
        raise SSAError("%08x: C emitter: unsupported NEG shape" % ins.addr)
    result = results[0].out
    low = lifter.fresh_unique(result[2])
    af = lifter.fresh_unique(1)
    ops.extend([Op("INT_AND", low, [result, ("const", 0xf, result[2])]),
                Op("INT_NOTEQUAL", af, [low, ("const", 0, result[2])]),
                Op("COPY", lifter.register("AF"), [af])])
    return ops


def shift_arithmetic(ins, lifter):
    """Match the runtime ``sar*_f`` OF recipe and keep SLEIGH's other flags.

    ``sar*_f`` clears OF for every nonzero count and preserves every flag for a
    zero count. SLEIGH clears OF only for count one and preserves it otherwise,
    which is the documented undefined-flag divergence above. Memory destinations
    stay unsupported.
    """
    ops = list(ins.ops)
    shifts = [op for op in ops if op.opc == "INT_SRIGHT" and op.out is not None
              and op.out[0] == "register"]
    if (len(shifts) != 1 or shifts[0].out[2] not in (1, 2, 4)
            or any(op.opc in ("LOAD", "STORE") for op in ops)):
        raise SSAError("%08x: C emitter: unsupported SAR shape" % ins.addr)
    op = shifts[0]
    cnt = op.ins[1]
    old_of = lifter.fresh_unique(1)
    nonzero = lifter.fresh_unique(1)
    zero = lifter.fresh_unique(1)
    updated = lifter.fresh_unique(1)
    ops[0:0] = [Op("COPY", old_of, [lifter.register("OF")])]
    ops.extend([Op("INT_NOTEQUAL", nonzero, [cnt, ("const", 0, cnt[2])]),
                Op("BOOL_NEGATE", zero, [nonzero]),
                Op("INT_AND", updated, [zero, old_of]),
                Op("COPY", lifter.register("OF"), [updated])])
    return ops
