"""Bounded write-through value reuse for lowered x87 effects.

`x87.statements` currently hands every helper a fresh ``ST(c, i)`` read, so a
value that a preceding effect just wrote is loaded back out of ``c->st``.  This
module records the scalar ``double`` a bounded window of logical stack slots
holds so a later effect can pass that value straight through.

Only reads are reused.  Every physical helper call still runs, so precision
control, NaN canonicalisation, exception status, tags, exact-integer metadata
and popped-slot residue remain exactly as the audited recipes left them.  This
is a straight-line optimisation: callers must invalidate at control-flow joins,
opaque guests and any effect the tracker does not model.

The tracker is deliberately small.  Each tracked logical slot ``ST(0) ..
ST(depth-1)`` owns at most one of ``depth`` function-scope ``double`` variables
(slots may share a variable after an ``FLD ST(i)`` copy).  Slot rotation is a
relabel, not a data copy, so push/pop/FXCH add no generated instructions beyond
the value assignments that the reuse itself requires.

No game addresses, no memory forwarding and no floating-point approximation
appear here.
"""

# Logical slots tracked from the top of the x87 stack.  Three covers the
# common `x = a*b`, `y = ...`, `z = ...` chains without keeping a wide window
# live across every effect.
DEFAULT_DEPTH = 3


class X87Values:
    """Known ``double`` values for a bounded window of logical x87 slots.

    ``slot[i]`` names the variable that currently holds ``ST(i)``, or ``None``
    when the value is unknown.  Reference counts let several slots share one
    variable (``FLD ST(i)`` copies without touching ``c->st``) while still
    recycling variables when the last reference is overwritten or dropped.
    """

    def __init__(self, depth=DEFAULT_DEPTH):
        if depth < 1:
            raise ValueError("x87 value depth must be positive")
        if depth > 8:
            # The x87 register file is exactly eight slots.  A wider window
            # cannot be indexed without wrapping the logical stack.
            raise ValueError("x87 value depth cannot exceed 8")
        self.depth = depth
        self._names = ["x87v%d" % i for i in range(depth)]
        self._slot = [None] * depth
        self._refs = {name: 0 for name in self._names}
        self._free = list(self._names)

    # -- declarations ---------------------------------------------------
    def declarations(self):
        """Function-scope declarations the caller must emit once."""
        return ["double %s;" % name for name in self._names]

    # -- introspection (tests and diagnostics) --------------------------
    def known(self, index):
        """Variable name for ``ST(index)`` if its value is known, else None."""
        if 0 <= index < self.depth:
            return self._slot[index]
        return None

    def read(self, index, default):
        """Expression for ``ST(index)``: the tracked variable or ``default``."""
        name = self.known(index)
        return name if name is not None else default

    def __repr__(self):  # pragma: no cover - diagnostic only
        return "X87Values(%r)" % (tuple(self._slot),)

    # -- refcount bookkeeping -------------------------------------------
    def _alloc(self):
        name = self._free.pop()
        self._refs[name] = 1
        return name

    def _retain(self, name):
        if self._refs[name] == 0:
            self._free.remove(name)
        self._refs[name] += 1

    def _release(self, name):
        self._refs[name] -= 1
        if self._refs[name] == 0:
            self._free.append(name)

    # -- stack model ----------------------------------------------------
    def _shift_down(self):
        """Old ST(i) becomes ST(i+1); the window's last slot falls out."""
        dropped = self._slot[self.depth - 1]
        for i in range(self.depth - 1, 0, -1):
            self._slot[i] = self._slot[i - 1]
        self._slot[0] = None
        if dropped is not None:
            self._release(dropped)

    def invalidate(self, lines=None):
        """Forget every tracked value; keep the variable pool."""
        self._slot = [None] * self.depth
        self._refs = {name: 0 for name in self._names}
        self._free = list(self._names)

    def push(self, expr, lines=None):
        """Model a push whose new ST(0) is ``expr`` (or unknown if None).

        Returns the expression the caller should pass to the helper: a tracked
        variable when one was assigned, otherwise ``expr`` unchanged.
        """
        self._shift_down()
        if expr is None:
            return expr
        name = self._alloc()
        if lines is not None:
            lines.append("%s = %s;" % (name, expr))
        self._slot[0] = name
        return name

    def push_copy(self, source, lines=None):
        """Model ``FLD ST(source)``: new ST(0) aliases the old source slot."""
        source_name = self.known(source)
        if source_name is not None:
            self._retain(source_name)
        self._shift_down()
        self._slot[0] = source_name

    def drop(self, lines=None):
        """Model a pop: old ST(i+1) becomes ST(i)."""
        dropped = self._slot[0]
        for i in range(self.depth - 1):
            self._slot[i] = self._slot[i + 1]
        self._slot[self.depth - 1] = None
        if dropped is not None:
            self._release(dropped)

    def write(self, index, expr, lines):
        """Record that ST(index) now holds ``expr``.

        Returns the expression the caller should pass to the helper: a tracked
        variable when the value fits the window, otherwise ``expr``.
        """
        if index < 0 or index >= self.depth:
            return expr
        old = self._slot[index]
        if old is not None and self._refs[old] == 1:
            name = old
        else:
            name = self._alloc()
            if old is not None:
                self._release(old)
        self._slot[index] = name
        lines.append("%s = %s;" % (name, expr))
        return name

    def copy(self, dest, source, lines=None):
        """Model a register-to-register copy inside the tracked window."""
        if dest < 0 or dest >= self.depth:
            return
        source_name = self.known(source)
        if source_name is not None:
            self._retain(source_name)
        old = self._slot[dest]
        self._slot[dest] = source_name
        if old is not None:
            self._release(old)

    def swap(self, other, lines=None):
        """Model ``FXCH ST(other)``: ST(0) and ST(other) trade places.

When ``other`` is outside the tracked window, its value is unknown, so the
new ST(0) becomes unknown and the old ST(0) leaves the window entirely.
Leaving the cache untouched would be wrong: ST(0) no longer holds the value
it held before the exchange.
"""
        if other < 0:
            return
        if other >= self.depth:
            old = self._slot[0]
            self._slot[0] = None
            if old is not None:
                self._release(old)
            return
        self._slot[0], self._slot[other] = self._slot[other], self._slot[0]


class X87Region:
    """Observation-aware register-run cache over an ``X87Values`` read cache.

`X87Values` reuses reads but still calls ``fset`` for every arithmetic result,
so each chain reclassifies the tag and rewrites ``st_bits``/``st_exact``.  This
region defers that bookkeeping: a register-arithmetic result is written to a
``double`` local and recorded as a *dirty* logical slot, while every structural
helper (``fpush``/``fpush_st``/``fcopy``/``fxch``/``fdrop``) and the eager
``c->fpu_top`` update still run.

Nothing is left stale.  A dirty slot is materialised (value, bits, exact and
``ftag_classify``) before any helper that reads its ``c->st`` value, before a
guest memory access, and before a branch/call/exit boundary.  A pop is special:
the eager ``fdrop`` only clears ``st_exact`` and the tag, so the residue stays
in ``c->st`` and ``c->st_bits``; the region therefore writes the dirty value
and its bits into the physical slot immediately before the ``fdrop``.

The arithmetic-only scope keeps the live state to ``depth`` doubles and no
separate metadata locals.  If the window cannot hold a dirty slot (push
eviction or a slot outside the window) the region flushes first rather than
dropping state.
"""

    def __init__(self, depth=DEFAULT_DEPTH):
        self.depth = depth
        self.values = X87Values(depth)
        # Logical offset -> C expression for the preserved ``st_bits`` shadow.
        # Arithmetic clears exact and writes zero bits, but keeping the field
        # lets a future deferred copy preserve nonzero FILD bits.
        self._dirty = {}
        self._previous = None

    # -- emitter seam ---------------------------------------------------
    def declarations(self):
        return self.values.declarations()

    def read(self, index, default):
        return self.values.read(index, default)

    def reset(self):
        """Forget everything without emitting; used at flushed joins."""
        self.values.invalidate()
        self._dirty.clear()

    def _flush_slot(self, offset, lines):
        var = self.values.known(offset)
        if var is None or offset not in self._dirty:
            self._dirty.pop(offset, None)
            return
        bits = self._dirty.pop(offset)
        phys = "(c->fpu_top + %d) & 7u" % offset
        lines.append("c->st[%s] = %s;" % (phys, var))
        lines.append("c->st_bits[%s] = %s;" % (phys, bits))
        lines.append("c->st_exact[%s] = 0;" % phys)
        lines.append("ftag_put(c, %s, ftag_classify(%s));" % (phys, var))

    def flush(self, lines=None):
        """Materialise every dirty slot and return the emitted lines."""
        if lines is None:
            lines = []
        for offset in sorted(self._dirty):
            self._flush_slot(offset, lines)
        self._dirty.clear()
        return lines

    # -- the tracker interface statements.py calls ----------------------
    def push(self, expr, lines=None):
        # A push shifts the window down; the bottom dirty slot would fall out
        # of the tracked window, so materialise it first.
        if (self.depth - 1) in self._dirty:
            self.flush(lines)
        result = self.values.push(expr, lines)
        self._shift_dirty_down()
        return result

    def _shift_dirty_down(self):
        self._dirty = {d + 1: bits for d, bits in self._dirty.items() if d + 1 < self.depth}

    def push_copy(self, source, lines=None):
        # FLD ST(source) reads c->st[source] and its metadata, so a dirty
        # source must be materialised first; the evicted bottom too.
        for offset in (source, self.depth - 1):
            if 0 <= offset < self.depth:
                self._flush_slot(offset, lines)
        self.values.push_copy(source, lines)
        self._shift_dirty_down()

    def drop(self, lines=None):
        # The popped slot's residue must be in c before the eager fdrop.
        self._flush_slot(0, lines)
        self.values.drop(lines)
        self._dirty = {d - 1: bits for d, bits in self._dirty.items() if d > 0}

    def store(self, index, value, lines=None):
        """Defer an arithmetic fset unless it falls outside the window.

        Returns ``(expression, emit_fset)``: the caller emits ``fset`` only when
        the second element is true.
        """
        if index < 0 or index >= self.depth:
            return value, True
        name = self.values.write(index, value, lines)
        self._dirty[index] = "0"
        return name, False

    def copy(self, dest, source, lines=None):
        # The eager fcopy reads source and writes dest.
        self._flush_slot(source, lines)
        self.values.copy(dest, source, lines)
        self._dirty.pop(dest, None)

    def swap(self, other, lines=None):
        # The eager fxch reads and writes both slots.
        self._flush_slot(0, lines)
        self._flush_slot(other, lines)
        self.values.swap(other, lines)
        self._dirty.pop(0, None)
        self._dirty.pop(other, None)

    def invalidate(self, lines=None):
        # FINIT leaves the register values as residue, so materialise first.
        self.flush(lines)
        self.reset()

    def at_block(self, block_id, ssa, fir=None):
        """Invalidate the read cache at a flushed join or opaque predecessor."""
        if block_id == getattr(ssa, "entry", None):
            self._previous = block_id
            return
        block = ssa.blocks[block_id]
        preds = set(block.phis[0].data[1]) if block.phis else set()
        previous = getattr(self, "_previous", None)
        if preds != {previous} or (previous is not None and any(
                op.opc in ("CALL", "DIV32") for op in ssa.blocks[previous].ops)):
            self.reset()
        self._previous = block_id
