"""Codegen-only corrections to integer SLEIGH semantics.

Raw lifting remains the census input. These adapters retain the comparison
runtime's AF and shift-OF recipes and its checked unsigned division helper.
Unsupported shapes fail rather than becoming unchecked C arithmetic.
"""
from .lift import Op
from .ssa import SSAError


def read_modify_write(ins, lifter, correct):
    """Correct SLEIGH's repeated reads for one memory RMW instruction.

    The baseline reads the operand once and performs the guest store before
    publishing flags. Capture the value once, replace SLEIGH's repeated operand
    reloads, apply `correct(ins, operand)` and defer flag writes until after the
    store. No inter-instruction forwarding or memory-alias inference is involved.
    """
    stores = [op for op in ins.ops if op.opc == "STORE"]
    if not stores:
        return correct(ins, None)
    loads = [op for op in ins.ops if op.opc == "LOAD"]
    if len(stores) != 1 or not loads or any(op.ins != loads[0].ins or op.out != loads[0].out for op in loads):
        raise SSAError("%08x: unsupported read-modify-write memory operand shape" % ins.addr)
    store = stores[0]
    if store.ins[0] != loads[0].ins[0] or store.ins[1][2] != loads[0].out[2]:
        raise SSAError("%08x: read-modify-write memory addresses disagree" % ins.addr)
    old = lifter.fresh_unique(loads[0].out[2])
    stored = lifter.fresh_unique(store.ins[1][2])
    ops, seen_load, seen_store = [], False, False
    for op in ins.ops:
        if op.opc == "LOAD":
            if not seen_load:
                ops.extend([op, Op("COPY", old, [op.out])])
                seen_load = True
            else:
                ops.append(Op("COPY", op.out, [stored if seen_store else old]))
        elif op.opc == "STORE":
            ops.extend([Op("COPY", stored, [op.ins[1]]), op])
            seen_store = True
        else:
            ops.append(op)
    from .lift import Insn
    corrected = Insn(ins.addr, ins.length, ins.mnem, ops, 0, False, False, [], ins.raw)
    ops = correct(corrected, loads[0].out)
    flags = {lifter.register(name) for name in ("CF", "OF", "SF", "ZF", "PF", "AF")}
    names, deferred, result = {}, {}, []
    for op in ops:
        args = [names.get(v, v) for v in op.ins]
        out = op.out
        if out in flags:
            out = lifter.fresh_unique(op.out[2])
            names[op.out] = out
            deferred[op.out] = out
        result.append(Op(op.opc, out, args, op.data))
    result.extend(Op("COPY", register, [value]) for register, value in deferred.items())
    return result


def arithmetic(ins, lifter, result_node=None):
    """Restore AF, capturing original operands before the destination changes."""
    ops, mnem = list(ins.ops), ins.mnem.upper()
    if mnem in ("SBB", "ADC"):
        opcode = "INT_SUB" if mnem == "SBB" else "INT_ADD"
        results = [op for op in ops if op.opc == opcode and op.out[0] == "register"]
        if len(results) != 1:
            raise SSAError("%08x: C emitter: unsupported %s shape" % (ins.addr, mnem))
        result = results[0].out
        first_cf = next((op for op in ops if op.out == lifter.register("CF")), None)
        if first_cf is not None and first_cf.opc == ("INT_LESS" if mnem == "SBB" else "INT_CARRY"):
            operands = first_cf.ins
        elif first_cf is not None and first_cf.opc == "COPY" and first_cf.ins == (("const", 0, 1),):
            # The lifter folds the first subtraction of SBB reg,reg; only the
            # carry-dependent subtraction remains, but AF still needs reg's bits.
            operands = (result, result)
        else:
            raise SSAError("%08x: C emitter: unsupported %s carry recipe" % (ins.addr, mnem))
        a, b = [lifter.fresh_unique(result[2]) for _ in range(2)]
        at = ops.index(first_cf)
        ops[at:at] = [Op("COPY", a, [operands[0]]), Op("COPY", b, [operands[1]])]
        results[0].data = {"cc_operands": (a, b, results[0].ins[1])}
        insert = len(ops)
    else:
        opcode = "INT_ADD" if mnem in ("ADD", "INC") else "INT_SUB"
        candidates = [(n, op) for n, op in enumerate(ops) if op.opc == opcode
                      and (op.out == result_node if result_node else (mnem == "CMP" or op.out[0] == "register"))]
        folded_compare = mnem == "CMP" and any(
            op.opc == "COPY" and op.out == lifter.register("ZF")
            and op.ins == (("const", 1, 1),) for op in ops)
        folded_subtract = mnem == "SUB" and any(
            op.opc == "COPY" and op.out[0] == "register"
            and op.out[2] in (1, 2, 4) and op.ins == (("const", 0, op.out[2]),)
            and op.out not in (lifter.register("CF"), lifter.register("OF")) for op in ops)
        if not candidates and (folded_compare or folded_subtract):
            return ops + [Op("COPY", lifter.register("AF"), [("const", 0, 1)])]
        if len(candidates) != 1:
            raise SSAError("%08x: C emitter: unsupported arithmetic shape" % ins.addr)
        n, op = candidates[0]
        result = op.out
        a, b = [lifter.fresh_unique(result[2]) for _ in range(2)]
        ops[n:n] = [Op("COPY", a, [op.ins[0]]), Op("COPY", b, [op.ins[1]])]
        insert = n + 3
    x, y, z = [lifter.fresh_unique(result[2]) for _ in range(3)]
    ops[insert:insert] = [Op("INT_XOR", x, [a, b]), Op("INT_XOR", y, [x, result]),
                        Op("INT_RIGHT", z, [y, ("const", 4, result[2])]),
                        Op("INT_AND", lifter.register("AF"), [z, ("const", 1, result[2])])]
    return ops


def shift(ins, lifter, operand=None):
    """Retain runtime OF for nonzero SHL/SHR counts, including counts > 1.

    DIVERGENCE(original): OF for counts greater than one is architecturally
    undefined. This uses the existing port's deterministic shl/shr*_f recipe;
    it introduces no new undefined-flag policy. AF and zero-count flags remain.
    """
    ops = list(ins.ops)
    opcode = "INT_LEFT" if ins.mnem.upper() == "SHL" else "INT_RIGHT"
    shifts = [op for op in ops if op.opc == opcode
              and (op.out[0] == "register" if operand is None else op.out == operand)]
    if len(shifts) != 1 or shifts[0].out[2] not in (1, 2, 4):
        raise SSAError("%08x: C emitter: unsupported shift shape" % ins.addr)
    op = shifts[0]
    original, old_of = lifter.fresh_unique(op.out[2]), lifter.fresh_unique(1)
    ops.insert(ops.index(op), Op("COPY", original, [op.ins[0]]))
    ops[0:0] = [Op("COPY", old_of, [lifter.register("OF")])]
    nonzero, zero, sign, changed, kept, updated = [lifter.fresh_unique(1) for _ in range(6)]
    cnt = op.ins[1]  # SLEIGH has already masked variable/immediate counts to 31.
    source = op.out if opcode == "INT_LEFT" else original
    ops.extend([Op("INT_NOTEQUAL", nonzero, [cnt, ("const", 0, cnt[2])]),
                Op("BOOL_NEGATE", zero, [nonzero]),
                Op("INT_SLESS", sign, [source, ("const", 0, source[2])])])
    if opcode == "INT_LEFT":
        recipe = lifter.fresh_unique(1)
        ops.append(Op("INT_XOR", recipe, [sign, lifter.register("CF")]))
    else:
        recipe = sign
    ops.extend([Op("INT_AND", changed, [nonzero, recipe]), Op("INT_AND", kept, [zero, old_of]),
                Op("INT_OR", updated, [changed, kept]), Op("COPY", lifter.register("OF"), [updated])])
    return ops


def divide(ins, lifter):
    """Replace only the audited 64/32 unsigned DIV shape with checked div32.

    The helper handles both zero divisors and quotient overflow before any
    register write, reports the original instruction address and preserves
    flags. Its packed result returns quotient in the low dword and remainder
    in the high dword. Narrow and signed divisions remain unsupported.
    """
    ops = list(ins.ops)
    divisions = [(n, op) for n, op in enumerate(ops) if op.opc == "INT_DIV"]
    if len(divisions) != 1:
        raise SSAError("%08x: C emitter: unsupported DIV shape" % ins.addr)
    n, op = divisions[0]
    remainder = [other for other in ops if other.opc == "INT_REM"]
    if (op.out[2] != 8 or any(v[2] != 8 for v in op.ins) or len(remainder) != 1
            or remainder[0].ins != op.ins or len(ops[n:]) != 4
            or ops[n+1].out != lifter.register("EAX") or ops[n+1].opc != "SUBPIECE"
            or ops[n+1].ins != (op.out, ("const", 0, 4))
            or ops[n+2] is not remainder[0] or ops[n+3].out != lifter.register("EDX")
            or ops[n+3].opc != "SUBPIECE" or ops[n+3].ins != (remainder[0].out, ("const", 0, 4))):
        raise SSAError("%08x: C emitter: only unsigned DIV32 is supported" % ins.addr)
    result = lifter.fresh_unique(8)
    return ops[:n] + [Op("DIV32", result, op.ins + (("const", ins.addr, 4),)),
                     Op("SUBPIECE", lifter.register("EAX"), [result, ("const", 0, 4)]),
                     Op("SUBPIECE", lifter.register("EDX"), [result, ("const", 4, 4)])]
