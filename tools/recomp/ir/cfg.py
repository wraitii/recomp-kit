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

    def __init__(self, addr, insns, succ, tables=None):
        self.addr = addr
        self.insns = insns
        self.succ = succ
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
    return FunctionIR(fn.addr, insns, succ, table_jumps(tr, fn))


def table_jumps(tr, fn):
    """Indices of table jumps the decoded emitter lowers to a pure in-body switch.

    Mirrors `emit_indirect_jump`: a JMP that is not a proven return jump and has
    a decoded table. A table naming a target outside this body is excluded, since
    the decoded emitter tail-calls those (`CALL_FN`) and the SSA CFG has no such
    edge.
    """
    result = set()
    return_jumps = getattr(fn, "return_jumps", ())
    for i, ins in enumerate(fn.insns):
        if ins.mnem != "JMP" or not ins.ops or ins.ops[0].startswith("0x") or i in return_jumps:
            continue
        targets = tr.jumptables.get((fn.addr, ins.addr))
        if targets and all(t in fn.index for t in targets):
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
