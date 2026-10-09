"""Shared instruction CFG and byte-backed lifting, independent of analyses.

Production supplies resolved successors. The default constructor is only for
standalone byte fixtures; it does not discover jump tables or external exits.
"""
from .lift import LiftError


class FunctionIR(object):
    """A function's lifted instructions and CFG.

    `succ[i]` lists successor instruction indices inside the function. The
    driver supplies it from the translator's own control-flow resolution
    (jump tables, noreturn calls, pushed continuations).
    """

    def __init__(self, addr, insns, succ, tables=None, exits=None, entries=(),
                 noreturn=None, seh=None):
        self.addr = addr
        #: Other instruction addresses generated code can enter at, with the
        #: CPU state at the normal entry.
        self.entries = tuple(entries)
        self.insns = insns
        self.succ = succ
        #: Instruction index -> addresses outside this body it can transfer to:
        #: tail jumps, table targets in other functions, a fallthrough out of
        #: the span. Not part of `succ`.
        self.exits = exits or {}
        #: Direct calls that never return -> the address after them.
        self.noreturn = noreturn or {}
        #: Instruction index -> ordered SEH runtime effects (`Translator.seh_effects`).
        self.seh = seh or {}
        #: Indices of computed jumps whose complete target set is `succ[i]`:
        #: decoded jump tables with every case inside this body. Any other
        #: BRANCHIND stays an opaque effect.
        self.tables = frozenset(tables or ())


def default_successors(insns):
    """Successors from p-code alone, for tests and standalone use."""
    index = {ins.addr: k for k, ins in enumerate(insns)}
    succ = []
    for k, ins in enumerate(insns):
        out, falls = [], True
        for op in ins.ops:
            if ins.internal_flow:
                continue
            if op.opc in ("BRANCH", "CBRANCH") and op.ins[0][0] == "ram":
                t = index.get(op.ins[0][1])
                if t is not None:
                    out.append(t)
                if op.opc == "BRANCH":
                    falls = False
            elif op.opc in ("RETURN", "BRANCHIND"):
                falls = False
        if falls and k + 1 < len(insns):
            out.append(k + 1)
        succ.append(sorted(set(out)))
    return succ


def function_ir(tr, lifter, fn):
    """Lift `fn`'s listed instructions from the image bytes with the
    translator's successors."""
    image = tr.image
    if not hasattr(fn, "index"):
        tr.prepare(fn)
    insns = []
    for i, ins in enumerate(fn.insns):
        end = fn.fallthrough[i] if fn.fallthrough[i] is not None else image.insn_end(
            ins.addr, ins.mnem)
        if end is None or end <= ins.addr:
            raise LiftError("%08x: instruction length unknown" % ins.addr)
        lo = ins.addr - image.base
        insns.append(lifter.lift(ins.addr, bytes(image.data[lo:lo + end - ins.addr]),
                                 mnem=ins.mnem))
    succ = [tr.successors(fn, i) for i in range(len(fn.insns))]
    noreturn = {}
    for i, ins in enumerate(fn.insns):
        if tr.never_returns(ins):
            noreturn[i] = insns[i].addr + insns[i].length
            succ[i] = []
    return FunctionIR(fn.addr, insns, succ, table_jumps(tr, fn), external_exits(tr, fn),
                      noreturn=noreturn, seh=tr.seh_effects(fn))


def external_exits(tr, fn):
    """Transfers that leave the body, by instruction index.

    Mirrors `goto_target` and `emit_indirect_jump`: direct jumps, conditional
    jumps and decoded table cases outside the body, and a last instruction
    that is not a terminator falling out of the span.
    """
    exits = {}
    for i, ins in enumerate(fn.insns):
        m = ins.mnem
        if (m == "INT3" or tr.push_ret_target(fn, i) is not None or tr.never_returns(ins)
                or ins.addr in getattr(fn, "dead_addrs", ())):
            continue
        out = []
        if m == "JMP":
            t = tr.branch_target(ins)
            if t is not None:
                out.append(t)
            elif i not in getattr(fn, "return_jumps", ()):
                out.extend(tr.jumptables.get((fn.addr, ins.addr)) or ())
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
    return_jumps = getattr(fn, "return_jumps", ())
    for i, ins in enumerate(fn.insns):
        if ins.mnem != "JMP" or not ins.ops or ins.ops[0].startswith("0x") or i in return_jumps:
            continue
        result.add(i)
    return result


def call_graph(functions, direct_targets):
    """Tarjan SCCs of the direct call graph, callees before callers."""
    fset = set(functions)
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in functions:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        work = [(root, iter(direct_targets(root)))]
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in fset:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(direct_targets(w))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out
