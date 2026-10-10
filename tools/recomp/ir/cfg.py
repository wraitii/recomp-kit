"""The function CFG the SSA emitter consumes, and the direct call graph."""
class FunctionIR(object):
    """A function's lifted instructions and CFG.

    `succ[i]` lists successor instruction indices inside the function. The
    driver supplies it from the program model's control-flow resolution
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
        #: the span; empty for a computed jump with no decoded table. Not part
        #: of `succ`.
        self.exits = exits or {}
        #: Direct calls that never return -> the address after them.
        self.noreturn = noreturn or {}
        #: Instruction index -> ordered SEH runtime effects (`Translator.seh_effects`).
        self.seh = seh or {}
        #: Indices of computed jumps whose complete target set is `succ[i]`:
        #: decoded jump tables with every case inside this body. Any other
        #: BRANCHIND stays an opaque effect.
        self.tables = frozenset(tables or ())


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
