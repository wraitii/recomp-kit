"""Construct raw function IR from decoded boundaries and structural flow facts."""
from ir.cfg import FunctionIR
from ir.lift import LiftError


def function_ir(tr, lifter, fn):
    """Lift `fn`'s listed instructions from the image bytes with the
    translator's successors."""
    image = tr.image
    if fn.index is None:
        tr.prepare(fn)
    insns = []
    for i, ins in enumerate(fn.insns):
        end = fn.fallthrough[i] if fn.fallthrough[i] is not None else image.insn_end(
            ins.addr, ins.mnem)
        if end is None or end <= ins.addr:
            raise LiftError("%08x: instruction length unknown" % ins.addr)
        lo = ins.addr - image.base
        insns.append(lifter.lift(ins.addr, bytes(image.data[lo:lo + end - ins.addr]),
                                 mnem=ins.mnem, decode=ins.decode))
        insns[-1].text = ' '.join(filter(None, (ins.rep, ins.mnem, ', '.join(ins.ops))))
    succ = [tr.successors(fn, i) for i in range(len(fn.insns))]
    noreturn = {}
    for i, ins in enumerate(fn.insns):
        if tr.never_returns(ins):
            noreturn[i] = insns[i].addr + insns[i].length
            succ[i] = []
    return FunctionIR(fn.addr, insns, succ, table_jumps(tr, fn), external_exits(tr, fn),
                      noreturn=noreturn, seh=tr.seh_effects(fn), language=lifter.language,
                      import_tails={i: slot for i, ins in enumerate(fn.insns)
                                    if (slot := tr.import_tail_slot(ins)) is not None})


def external_exits(tr, fn):
    """Transfers that leave the body, by instruction index.

    Mirrors `goto_target` and `emit_indirect_jump`: direct jumps, conditional
    jumps and decoded table cases outside the body, and a last instruction
    that is not a terminator falling out of the span. A computed jump without
    a decoded table may leave for any address and maps to an empty tuple.
    """
    exits = {}
    for i, ins in enumerate(fn.insns):
        m = ins.mnem
        if (m == "INT3" or tr.push_ret_target(fn, i) is not None or tr.never_returns(ins)
                or ins.addr in fn.dead_addrs):
            continue
        out = []
        if m == "JMP":
            t = tr.branch_target(ins)
            if t is not None:
                out.append(t)
            elif i not in fn.return_jumps:
                table = tr.jumptables.get((fn.addr, ins.addr))
                if not table:
                    exits[i] = ()
                    continue
                out.extend(table)
        elif m.startswith("J"):
            out.append(tr.branch_target(ins))
        if m not in ("RET", "JMP") and not (i + 1 < len(fn.insns) and fn.contiguous[i]):
            out.append(fn.fallthrough[i] or fn.end)
        out = sorted({t for t in out if t is not None and t not in fn.index})
        if out:
            exits[i] = tuple(out)
    return exits


def table_jumps(tr, fn):
    """Indices of table jumps the decoded emitter lowers to a pure in-body switch.

    Mirrors `emit_indirect_jump`: a JMP that is not a proven return jump. Without
    a decoded table every instruction of the body is a case. Cases outside this
    body are `external_exits`.
    """
    result = set()
    return_jumps = fn.return_jumps
    for i, ins in enumerate(fn.insns):
        if ins.mnem != "JMP" or not ins.ops or ins.ops[0].startswith("0x") or i in return_jumps:
            continue
        result.add(i)
    return result
