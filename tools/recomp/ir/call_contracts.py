"""Per-function field contracts for direct call publication.

A contract is two sets over the runtime CPU fields the emitter publishes at a
direct call: the eight GPRs and the six arithmetic flags.

``reads``
    Fields whose *entry* value can be observed before being overwritten on some
    path, including transitive callee reads (a backward may-liveness over the
    lifted CFG).

``kills``
    Fields overwritten on *every* path to a return (a forward must-definite
    analysis).  A GPR is killed only when all four byte lanes are written; a
    flag is killed when its field is written.  ``kills`` is about the value at
    return, so a push/pop save-restore does not count as a kill even though the
    register is written: the push also reads it, which keeps it in ``reads``.

At a direct CALL in an SSA body the caller may skip publishing field ``F`` iff
``F not in callee.reads and F in callee.kills``.  A field the callee preserves
is never dropped: its CPU value can still flow out to this body's caller even
when this body does not read it back.

The analysis is conservative.  Indirect calls, CALLOTHER, unbound targets,
failed lifts, SEH/alternate-entry/rewritten bodies, tail transfers and targets
whose dispatch can be swapped all contribute ``reads=all, kills=none``.  Raw
lifted instructions are the evidence; no emission is inspected.
"""
from collections import namedtuple

from .lift import Lifter, LiftError
from .cfg import FunctionIR, call_graph, function_ir

GPRS = ("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")
ARITH_FLAGS = ("CF", "PF", "AF", "ZF", "SF", "OF")
FIELDS = GPRS + ARITH_FLAGS

#: (reads, kills) field sets.  Plain data so it can cross a process boundary.
Contract = namedtuple("Contract", ("reads", "kills"))

#: reads everything, kills nothing.
CONSERVATIVE = Contract(frozenset(FIELDS), frozenset())


class Mapping(object):
    """Register byte offsets to contract cells.

    A cell is ``("g", name, lane)`` for one GPR byte or ``("f", name)`` for one
    arithmetic flag.  EIP, FS, DF and the x87/SSE registers are not part of the
    contract and are ignored.
    """

    __slots__ = ("gpr_lane", "flag_cell", "all_cells", "_field_cells")

    def __init__(self, lifter):
        self.gpr_lane = {}
        for name in GPRS:
            _, off, size = lifter.register(name)
            for lane in range(size):
                self.gpr_lane[off + lane] = (name, lane)
        self.flag_cell = {}
        for name in ARITH_FLAGS:
            _, off, size = lifter.register(name)
            for n in range(size):
                self.flag_cell[off + n] = name
        self.all_cells = frozenset(
            [("g", n, l) for (n, l) in self.gpr_lane.values()] + [("f", n) for n in self.flag_cell.values()])
        self._field_cells = {}
        for name in GPRS:
            self._field_cells[name] = frozenset(("g", name, lane) for lane in range(4))
        for name in ARITH_FLAGS:
            self._field_cells[name] = frozenset((("f", name),))

    def cells(self, varnode):
        """Cells named by a p-code ``(space, offset, size)`` varnode."""
        space, off, size = varnode
        if space != "register":
            return ()
        out = []
        for n in range(size):
            lane = self.gpr_lane.get(off + n)
            if lane is not None:
                out.append(("g", lane[0], lane[1]))
                continue
            flag = self.flag_cell.get(off + n)
            if flag is not None:
                out.append(("f", flag))
        return out

    def field_cells(self, fields):
        out = set()
        for field in fields:
            out |= self._field_cells[field]
        return out


def _scc_cap(size, cell_count):
    """Safety cap on fixed-point iterations for one recursive component.

    A sound analysis must converge; the cap only bounds a pathological input.
    Tests monkeypatch this to force the non-convergence fallback.
    """
    return max(size + 4, 4 * (cell_count + 1) * size)


_MAPPING = None


def mapping():
    """The process-wide x86 cell mapping (offsets are language constants)."""
    global _MAPPING
    if _MAPPING is None:
        _MAPPING = Mapping(Lifter())
    return _MAPPING


def contract_from_cells(reads_cells, killed_cells, cells=None):
    """Project cell sets onto the 14 runtime fields."""
    cells = cells or mapping()
    reads = frozenset(
        field for field in GPRS if cells.field_cells((field,)) & set(reads_cells))
    reads |= frozenset(
        field for field in ARITH_FLAGS if cells.field_cells((field,)) & set(reads_cells))
    kills = frozenset()
    for name in GPRS:
        if all(("g", name, lane) in killed_cells for lane in range(4)):
            kills |= {name}
    for name in ARITH_FLAGS:
        if ("f", name) in killed_cells:
            kills |= {name}
    return Contract(reads, kills)


def _is_callable_target(ins):
    for op in ins.ops:
        if op.opc == "CALL" and op.ins:
            return op.ins[0][1] if op.ins[0][0] == "ram" else None
    return None


class _Bits(object):
    """Integer bit masks over the contract cells (GPR lanes, then flags)."""

    __slots__ = ("bit", "all", "field_bits", "cache")

    def __init__(self, cells):
        order = sorted(cells.all_cells)
        self.bit = {cell: 1 << n for n, cell in enumerate(order)}
        self.cache = {}
        self.all = (1 << len(order)) - 1
        self.field_bits = {name: sum(self.bit[c] for c in cells.field_cells((name,)))
                           for name in FIELDS}

    def varnode(self, cells, varnode):
        """Mask of a p-code varnode's cells, cached per varnode."""
        m = self.cache.get(varnode)
        if m is None:
            m = self.cache[varnode] = self.mask(cells.cells(varnode))
        return m

    def mask(self, cells_):
        m = 0
        for cell in cells_:
            m |= self.bit[cell]
        return m

    def fields(self, names):
        m = 0
        for name in names:
            m |= self.field_bits[name]
        return m


_BITS = {}


def _bits(cells):
    b = _BITS.get(id(cells))
    if b is None:
        b = _BITS[id(cells)] = _Bits(cells)
    return b


class _Prepared(object):
    """One body's callee-independent facts, computed once per function.

    Per reachable instruction: the cells it reads before writing and writes
    itself, its direct call target (None when not a direct CALL) and whether
    it is opaque (indirect call, CALLOTHER, unbound CALL).
    """

    __slots__ = ("entry", "order", "succ", "preds", "use", "defs", "call", "returns")

    def __init__(self, fir, cells, bits):
        indices = {ins.addr: i for i, ins in enumerate(fir.insns)}
        self.entry = indices.get(fir.addr)
        self.order, self.succ, self.preds = [], {}, {}
        self.use, self.defs, self.call, self.returns = {}, {}, {}, []
        if self.entry is None:
            return
        n = len(fir.insns)
        seen, todo = set(), [self.entry]
        while todo:
            i = todo.pop()
            if i in seen or not 0 <= i < n:
                continue
            seen.add(i)
            self.order.append(i)
            todo.extend(fir.succ[i])
        for i in self.order:
            self.succ[i] = [j for j in fir.succ[i] if j in seen]
            self.preds.setdefault(i, [])
        for i in self.order:
            for j in self.succ[i]:
                self.preds[j].append(i)
            ins = fir.insns[i]
            use = defs = local = 0
            unknown = any(op.opc == "CALLIND" for op in ins.ops)
            for op in ins.ops:
                if op.opc == "CALLOTHER":
                    unknown = True
                    continue
                # A register read after a write in the same instruction is not
                # an entry read: `xor eax,eax` only reads the new value.
                for v in op.ins:
                    use |= bits.varnode(cells, v) & ~local
                if op.out is not None:
                    produced = bits.varnode(cells, op.out)
                    local |= produced
                    defs |= produced
            target = None
            if ins.mnem.upper() == "CALL":
                target = _is_callable_target(ins)
                if target is None:
                    unknown = True
            if unknown:
                use = bits.all
                target = None
            self.use[i], self.defs[i], self.call[i] = use, defs, target
            if _is_return(ins):
                self.returns.append(i)


def _is_callable_target(ins):
    for op in ins.ops:
        if op.opc == "CALL" and op.ins:
            return op.ins[0][1] if op.ins[0][0] == "ram" else None
    return None


def _is_return(ins):
    return ins.mnem.upper() == "RET" or any(op.opc == "RETURN" for op in ins.ops)


def summarize(fir, callee_lookup, cells=None, prepared=None):
    """Compute one function's contract from its lifted CFG.

    ``callee_lookup(target)`` returns a Contract or None for no summary.  The
    result is conservative when the body is empty, missing its entry or has no
    path to a return.  ``prepared`` reuses this body's callee-independent facts
    across fixed-point iterations.
    """
    cells = cells or mapping()
    bits = _bits(cells)
    if not fir.insns:
        return CONSERVATIVE
    p = prepared or _Prepared(fir, cells, bits)
    if p.entry is None:
        return CONSERVATIVE
    use, defs = dict(p.use), dict(p.defs)
    for i, target in p.call.items():
        if target is None:
            continue
        contract = callee_lookup(target)
        if contract is None:
            use[i] = bits.all
        else:
            use[i] |= bits.fields(contract.reads)
            defs[i] |= bits.fields(contract.kills)

    # Backward may-liveness to the least fixed point, with a worklist.
    live_in = dict.fromkeys(p.order, 0)
    work, queued = list(p.order), set(p.order)
    while work:
        i = work.pop()
        queued.discard(i)
        out = 0
        for j in p.succ[i]:
            out |= live_in[j]
        new = use[i] | (out & ~defs[i])
        if new != live_in[i]:
            live_in[i] = new
            for q in p.preds[i]:
                if q not in queued:
                    queued.add(q)
                    work.append(q)

    # Forward must-definite to the greatest fixed point: non-entry points
    # start at the top.
    must_out = dict.fromkeys(p.order, bits.all)
    work, queued = list(reversed(p.order)), set(p.order)
    while work:
        i = work.pop()
        queued.discard(i)
        if i == p.entry:
            new_in = 0
        else:
            new_in = bits.all
            for q in p.preds[i]:
                new_in &= must_out[q]
        new = new_in | defs[i]
        if new != must_out[i]:
            must_out[i] = new
            for j in p.succ[i]:
                if j not in queued:
                    queued.add(j)
                    work.append(j)

    killed = 0
    if p.returns:
        killed = bits.all
        for i in p.returns:
            killed &= must_out[i]
    reads = frozenset(f for f in FIELDS if live_in[p.entry] & bits.field_bits[f])
    kills = frozenset(f for f in FIELDS
                      if killed & bits.field_bits[f] == bits.field_bits[f])
    return Contract(reads, kills)


def cfg_is_closed(fir):
    """False for a body whose control can leave without a modeled edge.

    Tail jumps, unresolved computed jumps and calls with no fallthrough are
    conservatively outside this model.
    """
    for i, ins in enumerate(fir.insns):
        mnem = ins.mnem.upper()
        succ = fir.succ[i]
        if i in fir.exits:
            return False
        if mnem == "RET":
            if succ:
                return False
        elif mnem == "JMP":
            if not succ:
                return False
        elif mnem == "CALL":
            if not succ:
                return False
        elif mnem == "BRANCHIND" and i not in fir.tables:
            return False
        elif not succ and not _is_return(ins):
            return False
    return True


def direct_targets(tr, fn):
    """Direct CALL targets of a decoded function, in instruction order."""
    out = []
    for ins in fn.insns:
        if ins.mnem == "CALL":
            target = tr.branch_target(ins)
            if target is not None:
                out.append(target)
    return out


def analyze(tr, functions, *, analyzable, roots=None, progress=None, lifted=None):
    """Contracts for every analyzable body reachable from `roots`.

    `analyzable(addr)` decides which functions have liftable, trustworthy
    bodies (no SEH, alternate entries, rewrites, hooks or replacements).
    Functions outside the reachable set or an analyzable closure are not in the
    result; a call-site lookup that misses them must treat them as
    ``CONSERVATIVE``.  ``lifted``, when a dict, receives every body lifted here
    so the caller can reuse it instead of lifting again.  Direct callees are analyzed before callers; recursive
    components iterate to a fixed point.
    """
    by_addr = {fn.addr: fn for fn in functions}
    targets = {addr: direct_targets(tr, fn) for addr, fn in by_addr.items()}
    root_list = sorted(roots) if roots is not None else sorted(by_addr)
    needed, stack = set(), []
    for addr in root_list:
        if analyzable(addr) and addr not in needed:
            needed.add(addr)
            stack.append(addr)
        for target in targets.get(addr, ()):
            if analyzable(target) and target not in needed:
                needed.add(target)
                stack.append(target)
    while stack:
        addr = stack.pop()
        for target in targets.get(addr, ()):
            if analyzable(target) and target not in needed:
                needed.add(target)
                stack.append(target)

    lifter = Lifter()
    firs, dropped = {}, set()
    for addr in sorted(needed):
        fn = by_addr.get(addr)
        if fn is None:
            dropped.add(addr)
            continue
        try:
            fir = function_ir(tr, lifter, fn)
        except (LiftError, RecursionError):
            dropped.add(addr)
            continue
        if lifted is not None:
            lifted[addr] = fir
        if not cfg_is_closed(fir):
            dropped.add(addr)
            continue
        firs[addr] = fir
    needed -= dropped
    cells = mapping()
    contracts = {}
    components = call_graph(sorted(needed),
                            lambda a: [t for t in targets.get(a, ()) if t in firs])
    done = 0
    for comp in components:
        # Sound bottom for both analyses: the may-liveness least fixed point and
        # the must-definite least fixed point (the strongest sound under-
        # approximation, which only makes the caller publish more).
        live = {a: Contract(frozenset(), frozenset()) for a in comp if a in firs}

        def lookup(target, live=live):
            if target in contracts:
                return contracts[target]
            return live.get(target)

        # The lattice chain is bounded by |comp| x cell count for each of reads
        # and kills.  If the safety cap is hit the result is not a fixed point,
        # and a non-converged may-analysis would under-approximate reads, so
        # fall back to the conservative contract for the whole component.
        prepared = {a: _Prepared(firs[a], cells, _bits(cells)) for a in comp if a in firs}
        recursive = len(comp) > 1 or any(
            t == comp[0] for t in targets.get(comp[0], ()))
        if not recursive:
            # No call into the component: one pass is the fixed point.
            for addr in comp:
                if addr in firs:
                    contracts[addr] = summarize(firs[addr], lookup, cells, prepared[addr])
                    done += 1
                    if progress is not None:
                        progress(done, len(needed))
            continue
        converged = False
        for _ in range(_scc_cap(len(comp), len(cells.all_cells))):
            changed = False
            for addr in comp:
                if addr not in firs:
                    continue
                new = summarize(firs[addr], lookup, cells, prepared[addr])
                current = live.get(addr)
                if current is None or new.reads != current.reads or new.kills != current.kills:
                    live[addr] = new
                    changed = True
            if not changed:
                converged = True
                break
        for addr in comp:
            if addr in firs:
                contracts[addr] = live[addr] if converged else CONSERVATIVE
                done += 1
                if progress is not None:
                    progress(done, len(needed))
    return contracts
