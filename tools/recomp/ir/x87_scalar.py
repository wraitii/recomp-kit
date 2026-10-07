"""Scalar x87 regions with exact state at the existing observation boundaries.

Slots are named relative to a captured entry TOP. Push/pop/copy change this
compile-time mapping, not the physical CPU array. Values, exact-integer shadows,
tags and popped residue are written back before calls, division seams, opaque
effects and exits. In performance mode the tracker also carries unpublished
state across internal CFG edges as a fixed-point join shape (`x87_carry.py`);
strict mode keeps publishing at every edge. Only CW/SW
helpers use a private nonescaping X86 context; no helper receives it unless its
recipe accesses those two fields exclusively.

This is an instruction-derived representation change, not dead-state removal,
memory forwarding, a native call ABI or a floating-point approximation.

Guest loads and stores do not observe x87 state in the kit runtime: the
watchpoint and DirectDraw dirty tracking read only the address and value, and
null-check builds, whose fault dispatch exposes the CPU, compile the strict
form. This is the decoded `x87_locals.py` contract, which also leaves x87 state
unpublished at arena accesses. A runtime that exposes x87 state at accesses
must use the strict form.

DIVERGENCE(original): [ssa-x87-scalar] performance mode publishes at calls,
division seams, opaque effects and exits, and carries unpublished x87 state
across internal CFG edges instead of publishing there; loads and stores still
do not publish. Interior access faults and store watch callbacks may see the
preceding published x87 state, within the agreed performance-mode contract.
observe_loads=True (strict) keeps publication before every guest access and
the per-edge flush.

DIVERGENCE(original): [ssa-x87-binary32] common PC=00 arithmetic with proven
binary32 operands uses the documented float exponent-range policy. Other
precision settings and unproven operands retain the audited double helpers.
No reassociation, contraction or division approximation is introduced.

DIVERGENCE(original): [ssa-x87-convention] with `convention=True` (the
`ir_ssa_msvc_convention` setting) a flush assumes the MSVC invariant at region
start: every register above TOP is tagged empty. Popped registers publish no
value, `st_bits` or `st_exact` (residue: every push and `fset` rewrites all
three), and a register pushed and popped within the region keeps its
untouched, already-empty tag. A register popped from the region-start stack is
retagged empty. Live registers publish `st_bits` only when `st_exact` may be
set, the only case a runtime helper reads it, and TOP only when it moved. The
tag word, TOP and live registers stay exact; only popped residue differs.
Functions containing FINCSTP/FDECSTP, which break the invariant inside a
region, keep the exact flush.
"""
from dataclasses import dataclass, field
from typing import Optional

from . import x87


@dataclass
class Slot:
    value: Optional[str] = None
    bits: Optional[str] = None
    exact: Optional[str] = None
    tag: Optional[str] = None
    narrow: bool = False  # Proven binary32 when PC=00, not an incoming-state guess.
    dirty: set = field(default_factory=set)
    pending: bool = False  # Arithmetic NaN whose IE check/canonicalisation is deferred.


#: Slot components carried across an internal CFG edge.
PARTS = ("value", "bits", "exact", "tag")
PART_TYPES = {"value": "double", "bits": "uint64_t",
              "exact": "uint8_t", "tag": "unsigned"}

#: Canonical iteration order for the parts of a carried slot. The emitter used
#: to walk ``SlotShape.parts`` (a frozenset), whose order follows the process
#: hash seed; that made the generated temp numbering and edge assignments vary
#: between runs. This order is exactly what the reference PYTHONHASHSEED=0 run
#: produced, now made explicit so every process emits identical text. Any part
#: not named here sorts after the known four.
PART_ORDER = ("bits", "exact", "value", "tag")


def ordered_parts(parts):
    """Return ``parts`` in a hash-seed-independent code-generation order."""
    known = [part for part in PART_ORDER if part in parts]
    return known + sorted(part for part in parts if part not in PART_ORDER)
#: Literal expressions whose agreement survives a join (the convention flush
#: tests `slot.exact != "0"`, so `0`/`FTAG_EMPTY`/`1` must stay constants).
CARRY_LITERALS = frozenset(("0", "1", "FTAG_EMPTY"))


@dataclass(frozen=True)
class SlotShape:
    """Unpublished parts of one physical slot, keyed by relative position."""
    parts: frozenset = frozenset()          # dirty parts that must be carried
    narrow: bool = False
    const: tuple = ()                       # sorted (part, literal) agreements


@dataclass(frozen=True)
class CarryShape:
    """Canonical x87 state at a block entry, relative to the runtime TOP.

    Slots are keyed by signed relative position `p` (0 = ST(0), negative =
    popped residue). Only parts that still need publishing are carried; clean
    parts are reloaded from `c` on demand. Counters are relative to the same
    reference and merge conservatively."""
    active: bool
    slots: tuple = ()                       # sorted ((p, SlotShape), ...)
    base: int = 0
    low: int = 0
    high: int = 0
    # Predecessors disagree on the last published TOP relative to this one,
    # so the next flush must publish TOP even if `position == base`.
    top_unknown: bool = False
    status_dirty: bool = False


#: No predecessor carries x87 state (all inactive/reset).
INACTIVE = CarryShape(False)
#: A merged join that exceeds the eight-slot window; callers must reset.
UNSAFE = object()


def carry_var(position, part):
    """Stable function-scope C name for one carried slot component."""
    tag = "m%d" % -position if position < 0 else "%d" % position
    return "x87c%s_%s" % (tag, part)


def merge_shapes(shapes):
    """Merge predecessor edge shapes into a block entry shape.

    Known parts and dirty flags union, `narrow` intersects, counters take the
    conservative extremes (min base/low, max high) and activity/dirty flags
    OR. Returns ``None`` when a predecessor is still unknown and ``UNSAFE``
    when the merged window cannot be represented by eight physical slots."""
    known = [s for s in shapes if s is not None]
    if len(known) != len(shapes) or not known:
        return None
    if not any(s.active for s in known):
        return INACTIVE
    slots = {}
    for shape in known:
        if not shape.active:
            continue
        for p, slot in shape.slots:
            slots.setdefault(p, []).append(slot)
    merged = []
    for p in sorted(slots):
        entries = slots[p]
        parts = frozenset().union(*(e.parts for e in entries))
        narrow = all(e.narrow for e in entries)
        const = []
        for part in sorted(parts):
            literals = set()
            ok = True
            for e in entries:
                agreement = dict(e.const)
                if part not in e.parts or part not in agreement:
                    ok = False
                    break
                literals.add(agreement[part])
            if ok and len(literals) == 1:
                const.append((part, literals.pop()))
        merged.append((p, SlotShape(parts, narrow, tuple(const))))
    positions = [p for p, _ in merged]
    # An inactive predecessor activates at the edge with everything published
    # at its current TOP, i.e. relative base/low/high of zero.
    low = min(s.low if s.active else 0 for s in known)
    high = max(s.high if s.active else 0 for s in known)
    base = min(s.base if s.active else 0 for s in known)
    if (high - low >= 8 or (positions and max(positions) - min(positions) >= 8)):
        return UNSAFE
    # An inactive predecessor has published its current TOP (relative base 0).
    # The min base only widens popped-tag publication; TOP itself needs an
    # explicit flag when the predecessors' published TOPs differ.
    published = {s.base if s.active else 0 for s in known}
    top_unknown = len(published) > 1 or any(s.top_unknown for s in known)
    return CarryShape(True, tuple(merged), base, low, high, top_unknown,
                      any(s.status_dirty for s in known))


class X87Scalar:
    """Track all eight physical residues within a single-entry linear region."""

    def __init__(self, observe_loads=False, convention=False, lazy_nan=False):
        self.serial = 0
        self.temps = []
        self.observe_loads = observe_loads
        self.convention = convention
        self.lazy_nan = lazy_nan
        self.binary32 = True
        self.reset()

    def declarations(self):
        return ["X86 x87_env_;", "uint32_t x87_top_;"]

    def reset(self):
        self.active = False
        self.top = 0
        self.top_dirty = False
        self.top_unknown = False  # see CarryShape.top_unknown
        # Unbounded stack positions relative to the region's captured TOP
        # (negative = pushed). `base` is the last published TOP, `low` the
        # deepest push since then and `high` the highest position accessed.
        self.position = self.base = self.low = self.high = 0
        self.status_dirty = False
        self.slots = {}

    def _activate(self, lines):
        if not self.active:
            lines.extend(["x87_top_ = c->fpu_top;",
                          "x87_env_.fpu_cw = c->fpu_cw;",
                          "x87_env_.fpu_sw = c->fpu_sw;"])
            self.active = True

    def _temp(self, expr, lines, ctype="double"):
        name = "x87s%d" % self.serial
        self.serial += 1
        self.temps.append("%s %s;" % (ctype, name))
        lines.append("%s = %s;" % (name, expr))
        return name

    def _phys(self, offset):
        return "(x87_top_ + %du) & 7u" % offset

    def _slot(self, logical=0):
        self.high = max(self.high, self.position + logical)
        offset = (self.top + logical) & 7
        return offset, self.slots.setdefault(offset, Slot())

    def _read(self, logical, part, lines):
        offset, slot = self._slot(logical)
        if getattr(slot, part) is None:
            phys = self._phys(offset)
            expr = ("ftag_of(c, %s)" % phys if part == "tag" else
                    "c->%s[%s]" % ({"value": "st", "bits": "st_bits",
                                    "exact": "st_exact"}[part], phys))
            ctype = {"value": "double", "bits": "uint64_t",
                     "exact": "uint8_t", "tag": "unsigned"}[part]
            setattr(slot, part, self._temp(expr, lines, ctype))
        return getattr(slot, part)

    def _assign(self, logical, narrow=None, pending=None, **parts):
        _, slot = self._slot(logical)
        if narrow is not None:
            slot.narrow = narrow
        for part, expr in parts.items():
            setattr(slot, part, expr)
            slot.dirty.add(part)
        if pending is not None:
            slot.pending = pending

    def _fold_slot(self, slot, lines, emit_ie=True, canonical=True):
        """Fold one deferred arithmetic NaN: raise IE and canonicalise its bits.

        A deferred value is always bound to a simple local by `_set`/`_read`;
        the guard below keeps a future caller from duplicating a compound
        expression in the `v != v ? indef : v` ternary.
        """
        if not slot.pending:
            return slot.value
        value = slot.value
        if value is None:
            slot.pending = False
            return None
        if not value.isidentifier():
            value = self._temp("(%s)" % value, lines, "double")
        if emit_ie:
            # IE is bit 0, so a branchless boolean OR raises it exactly when
            # the value is NaN, mirroring `fx87`'s NaN branch.
            lines.append("x87_env_.fpu_sw |= (uint16_t)(%s != %s);" % (value, value))
            self.status_dirty = True
        if canonical:
            slot.value = self._temp(
                "(%s != %s ? x87_indefinite() : %s)" % (value, value, value),
                lines, "double")
            slot.dirty.add("value")
        slot.pending = False
        return slot.value

    def _fold(self, logical, lines, emit_ie=True, canonical=True):
        _, slot = self._slot(logical)
        return self._fold_slot(slot, lines, emit_ie, canonical)

    def _fold_all(self, lines, emit_ie=True, canonical=True):
        for _, slot in sorted(self.slots.items()):
            if slot.pending:
                self._fold_slot(slot, lines, emit_ie, canonical)

    def fold_pending(self, lines):
        """Fold every deferred NaN before an internal CFG edge or publication."""
        if self.active:
            self._fold_all(lines)

    def _release(self, logical, lines, subsumed=False):
        """Discard the slot at `logical`; fold IE unless the new value subsumes it."""
        _, slot = self._slot(logical)
        if not slot.pending:
            return
        if subsumed:
            slot.pending = False
        else:
            self._fold_slot(slot, lines)

    def _set(self, logical, expr, lines, bits="0", exact="0", tag=None, narrow=False,
             pending=False, subsumed=False):
        self._release(logical, lines, subsumed=subsumed)
        value = self._temp(expr, lines)
        self._assign(logical, value=value, bits=bits, exact=exact,
                     tag=tag or "ftag_classify(%s)" % value, narrow=narrow,
                     pending=pending)

    def _move_top(self, delta):
        self.top = (self.top + delta) & 7
        self.top_dirty = True
        self.position += delta
        self.low = min(self.low, self.position)

    def _drop(self, lines=None, subsumed=False):
        # Preserve both value and integer shadow in the popped physical slot.
        if lines is not None:
            self._release(0, lines, subsumed=subsumed)
        self._assign(0, exact="0", tag="FTAG_EMPTY")
        self._move_top(1)

    def _convention_flush(self):
        """Publish under the MSVC stack invariant, or None when ambiguous.

        Every tracked physical slot maps to one position in [low, low + 8)
        unless the region touched a position eight or more above its deepest
        push (only possible on a stack overflow); then the exact flush runs.
        """
        if self.high >= self.low + 8 or self.position >= self.low + 8:
            return None
        lines, tags = [], []
        for offset, slot in sorted(self.slots.items()):
            phys = self._phys(offset)
            position = self.low + ((offset - self.low) & 7)
            if position < self.position:
                # Popped residue. Only a register that was on the stack when
                # the region began needs its (dirty) empty tag.
                if position >= self.base and "tag" in slot.dirty:
                    tags.append((phys, slot.tag))
            else:
                if "value" in slot.dirty:
                    lines.append("c->st[%s] = %s;" % (phys, slot.value))
                if "exact" in slot.dirty:
                    lines.append("c->st_exact[%s] = %s;" % (phys, slot.exact))
                if "bits" in slot.dirty and slot.exact != "0":
                    lines.append("c->st_bits[%s] = %s;" % (phys, slot.bits))
                if "tag" in slot.dirty:
                    tags.append((phys, slot.tag))
            slot.dirty.clear()
        if tags:
            masks = " | ".join("(3u << (2u * (%s)))" % phys for phys, _ in tags)
            values = " | ".join("((%s) << (2u * (%s)))" % (tag, phys) for phys, tag in tags)
            lines.append("c->fpu_tag = (uint16_t)((c->fpu_tag & ~(%s)) | %s);" % (masks, values))
        if self.position != self.base or self.top_unknown:
            lines.append("c->fpu_top = (%s);" % self._phys(self.top))
        self.top_dirty = self.top_unknown = False
        self.base = self.low = self.high = self.position
        if self.status_dirty:
            lines.append("c->fpu_sw = x87_env_.fpu_sw;")
            self.status_dirty = False
        return lines

    def flush(self):
        """Publish only changed components; retain scalar knowledge afterward."""
        if not self.active:
            return []
        folded = []
        self._fold_all(folded)
        if self.convention:
            lines = self._convention_flush()
            if lines is not None:
                return folded + lines
        lines, tags = [], []
        for offset, slot in sorted(self.slots.items()):
            phys = self._phys(offset)
            for part, array in (("value", "st"), ("bits", "st_bits"),
                                ("exact", "st_exact")):
                if part in slot.dirty:
                    lines.append("c->%s[%s] = %s;" % (array, phys, getattr(slot, part)))
            if "tag" in slot.dirty:
                tags.append((phys, slot.tag))
            slot.dirty.clear()
        if tags:
            masks = " | ".join("(3u << (2u * (%s)))" % phys for phys, _ in tags)
            values = " | ".join("((%s) << (2u * (%s)))" % (tag, phys) for phys, tag in tags)
            lines.append("c->fpu_tag = (uint16_t)((c->fpu_tag & ~(%s)) | %s);" % (masks, values))
        if self.top_dirty or self.top_unknown:
            lines.append("c->fpu_top = (%s);" % self._phys(self.top))
            self.top_dirty = self.top_unknown = False
        self.base = self.low = self.high = self.position
        if self.status_dirty:
            lines.append("c->fpu_sw = x87_env_.fpu_sw;")
            self.status_dirty = False
        return folded + lines

    def snapshot(self):
        """Current unpublished state as a carry shape relative to the live TOP.

        Under the MSVC convention a popped register only needs its empty tag
        retagged; its value, bits and exact shadow are relaxed and dropped so a
        join does not copy dead residue. Live slots carry every dirty part."""
        if not self.active:
            return INACTIVE
        slots = []
        for offset, slot in self.slots.items():
            if not slot.dirty:
                continue
            position = self.low + ((offset - self.low) & 7) - self.position
            parts = frozenset(slot.dirty)
            if position < 0 and self.convention:
                # Only the MSVC convention relaxes popped residue. With an exact
                # flush (msvc_convention=False or FINCSTP/FDECSTP) the successor
                # must be able to republish the popped value, bits and shadow.
                parts &= frozenset(("tag",))
                if not parts:
                    continue
            const = tuple((part, getattr(slot, part)) for part in sorted(parts)
                          if getattr(slot, part) in CARRY_LITERALS)
            slots.append((position, SlotShape(parts, slot.narrow, const)))
        slots.sort()
        return CarryShape(True, tuple(slots), self.base - self.position,
                          self.low - self.position, self.high - self.position,
                          self.top_unknown, self.status_dirty)

    def seed(self, shape, exprs=None):
        """Replace this tracker with a carried shape from a block entry.

        `exprs` maps (position, part) to the fresh C local holding the carried
        value; const parts come from the shape. With no `exprs` (fixed-point
        replay) placeholder expressions stand in for the locals."""
        self.reset()
        if not shape.active:
            return
        self.active = True
        self.top = 0
        self.position = 0
        self.base, self.low, self.high = shape.base, shape.low, shape.high
        self.top_unknown = shape.top_unknown
        self.status_dirty = shape.status_dirty
        for position, slot_shape in shape.slots:
            slot = Slot(narrow=slot_shape.narrow, dirty=set(slot_shape.parts))
            agreements = dict(slot_shape.const)
            for part in slot_shape.parts:
                if part in agreements:
                    expr = agreements[part]
                elif exprs is not None:
                    expr = exprs[(position, part)]
                else:
                    expr = "_"
                setattr(slot, part, expr)
            self.slots[position & 7] = slot

    def normalize(self, lines):
        """Activate or re-anchor the tracker to the current physical TOP.

        Afterward `top == position == 0` and slot keys are relative positions,
        so an edge can publish `x87_top_` and index `slots` consistently even
        when predecessors reached the block from different activations."""
        if not self.active:
            lines.extend(["x87_top_ = c->fpu_top;",
                          "x87_env_.fpu_cw = c->fpu_cw;",
                          "x87_env_.fpu_sw = c->fpu_sw;"])
            self.active = True
            return
        if self.top != 0:
            lines.append("x87_top_ = (x87_top_ + %du) & 7u;" % self.top)
            self.slots = {((offset - self.top) & 7): slot
                          for offset, slot in self.slots.items()}
        delta = self.position
        self.top = 0
        self.position -= delta
        self.base -= delta
        self.low -= delta
        self.high -= delta

    def statements(self, data, address=None, result=None):
        """Lower common audited effects; materialize before other runtime recipes."""
        m, operands = data["mnem"], data["operands"]
        memory = [size for kind, size in operands if kind == "mem"]
        slots = [index for kind, index in operands if kind == "st"]
        bits = memory[0] * 8 if memory else 0
        lines = []
        self._activate(lines)
        # Strict mode keeps every pre-access snapshot; performance mode defers
        # across loads and stores, which do not observe x87 state.
        if address is not None and self.observe_loads:
            lines.extend(self.flush())

        def read(index):
            return self._read(index, "value", lines)

        def mem(integer=False):
            expr = ("(int%d_t)rd%d((uint32_t)%s)" % (bits, bits, address) if integer else
                    "rdf%d((uint32_t)%s)" % (bits, address))
            return self._temp(expr, lines, "int64_t" if integer else "double")

        if m in x87.CONSTANTS and not operands:
            self._move_top(-1)
            self._set(0, x87.CONSTANTS[m], lines, narrow=m in ("FLD1", "FLDZ"))
        elif m == "FLD":
            if slots:
                # Capture all source components before TOP moves (FLD ST7).
                parts = {part: self._read(slots[0], part, lines)
                         for part in ("value", "bits", "exact", "tag")}
                narrow = self._slot(slots[0])[1].narrow
                pending = self._slot(slots[0])[1].pending if self.lazy_nan else False
                self._move_top(-1)
                if self.lazy_nan:
                    self._release(0, lines)
                    self._assign(0, narrow=narrow, pending=pending, **parts)
                else:
                    self._assign(0, narrow=narrow, **parts)
            else:
                value = mem()
                self._move_top(-1)
                self._set(0, value, lines, narrow=bits == 32)
        elif m == "FILD":
            value = mem(True)
            self._move_top(-1)
            self._set(0, "(double)%s" % value, lines, bits="(uint64_t)%s" % value, exact="1")
        elif m in ("FST", "FSTP"):
            if slots:
                if slots[0]:
                    parts = {part: self._read(0, part, lines)
                             for part in ("value", "bits", "exact", "tag")}
                    narrow = self._slot(0)[1].narrow
                    if self.lazy_nan:
                        # The copy carries any deferred NaN; fold the value the
                        # destination is about to lose (not an operand).
                        pending = self._slot(0)[1].pending
                        self._release(slots[0], lines)
                        self._assign(slots[0], narrow=narrow, pending=pending, **parts)
                    else:
                        self._assign(slots[0], narrow=narrow, **parts)
            else:
                if self.lazy_nan:
                    # Store to memory is a sink: canonicalise the double before
                    # any narrowing conversion, matching eager's fx87 result.
                    self._fold(0, lines)
                value = read(0)
                if bits == 32:
                    # A narrow slot is exactly a binary32 when PC=00 (every
                    # FLD m32, FLD1/FLDZ and PC=00 arithmetic result has been
                    # widened from a float), so (float)v is exact and the RC
                    # stepper in fto_float is dead. Keep the NaN arm: fto_float
                    # quiets an sNaN payload that clang could otherwise retain
                    # by cancelling the widening/narrowing casts. The runtime
                    # PC test keeps the general double path for PC!=0.
                    narrow = self._slot(0)[1].narrow
                    if narrow and not self.observe_loads:
                        if not value.isidentifier():
                            value = self._temp("(%s)" % value, lines, "double")
                        value = ("((x87_env_.fpu_cw & 0x300u) == 0u && %s == %s) ? "
                                 "(float)(%s) : fto_float(&x87_env_, %s)" %
                                 (value, value, value, value))
                    else:
                        value = "fto_float(&x87_env_, %s)" % value
                lines.append("wrf%d((uint32_t)%s, %s);" % (bits, address, value))
            if m == "FSTP":
                # A register copy already carried the value to its destination;
                # a plain FSTP ST(0) discards it and must fold.
                self._drop(lines, subsumed=self.lazy_nan and bool(slots and slots[0]))
        elif m in x87.ARITH or m in x87.INTEGER_ARITH or (m.endswith("P") and m[:-1] in x87.ARITH):
            pop = m.endswith("P")
            base = m[:-1] if pop else m
            integer = base in x87.INTEGER_ARITH
            operator = (x87.INTEGER_ARITH if integer else x87.ARITH)[base]
            if memory:
                rhs = mem(integer)
                dst, lhs = 0, read(0)
                narrow = self._slot(0)[1].narrow and not integer and bits == 32
            elif slots:
                dst = slots[0] if len(slots) == 2 or pop else 0
                src = slots[1] if len(slots) == 2 else (0 if pop else slots[0])
                lhs, rhs = read(dst), read(src)
                narrow = self._slot(dst)[1].narrow and self._slot(src)[1].narrow
            else:
                dst, lhs, rhs = 1, read(1), read(0)
                narrow = self._slot(1)[1].narrow and self._slot(0)[1].narrow
            if base.endswith("R"):
                lhs, rhs = rhs, lhs
            if self.lazy_nan:
                # NaN-propagating: keep the full-precision result, apply PC
                # rounding per op, and defer the IE check/canonicalisation.
                # The destination slot was an operand, so the pending result
                # subsumes its old IE, and so does a popped operand.
                raw = ("fdivz(&x87_env_, %s, %s)" % (lhs, rhs) if operator == "/" else
                       "%s %s %s" % (lhs, operator, rhs))
                if narrow and operator in ("+", "-", "*") and not self.observe_loads and self.binary32:
                    value = ("((x87_env_.fpu_cw & 0x300u) == 0u ? "
                             "(double)((float)(%s) %s (float)(%s)) : %s)" %
                             (lhs, operator, rhs, raw))
                else:
                    value = ("((x87_env_.fpu_cw & 0x300u) == 0u ? "
                             "(double)(float)(%s) : (%s))" % (raw, raw))
                self._set(dst, value, lines, narrow=True, pending=True, subsumed=True)
            else:
                value = ("fdivz(&x87_env_, %s, %s)" % (lhs, rhs) if operator == "/" else
                         "%s %s %s" % (lhs, operator, rhs))
                value = "fx87(&x87_env_, %s)" % value
                if narrow and operator in ("+", "-", "*") and not self.observe_loads and self.binary32:
                    value = ("((x87_env_.fpu_cw & 0x300u) == 0u ? "
                             "fx87_exact(&x87_env_, (double)((float)(%s) %s (float)(%s))) : %s)" %
                             (lhs, operator, rhs, value))
                self._set(dst, value, lines, narrow=True)
            self.status_dirty = True
            if pop:
                self._drop(lines, subsumed=True)
        elif m in ("FCOM", "FCOMP", "FUCOM", "FUCOMP", "FICOM", "FICOMP", "FCOMPP", "FUCOMPP", "FTST"):
            if self.lazy_nan:
                # Compare is a sink: FUCOM would not raise IE for a quiet
                # arithmetic NaN, so fold both operands first.
                self._fold(0, lines)
                a = read(0)
                if memory:
                    other = mem(m.startswith("FI"))
                elif m == "FTST":
                    other = "0.0"
                else:
                    logical = slots[-1] if slots else 1
                    self._fold(logical, lines)
                    other = read(logical)
                lines.append("%s(&x87_env_, %s, %s);" %
                             ("fucom" if m.startswith("FU") else "fcom", a, other))
            else:
                other = (mem(m.startswith("FI")) if memory else
                         "0.0" if m == "FTST" else read(slots[-1] if slots else 1))
                lines.append("%s(&x87_env_, %s, %s);" %
                             ("fucom" if m.startswith("FU") else "fcom", read(0), other))
            self.status_dirty = True
            for _ in range(2 if m.endswith("PP") else int(m.endswith("P"))):
                self._drop(lines)
        elif m in x87.UNARY:
            narrow = self._slot(0)[1].narrow if m in ("FABS", "FCHS") else m == "FSQRT"
            if self.lazy_nan and m in ("FABS", "FCHS"):
                # Sign/payload-sensitive: eager acts on the canonical indefinite.
                self._fold(0, lines)
            self._set(0, (x87.UNARY[m] % read(0)).replace("(c,", "(&x87_env_,"), lines,
                      narrow=narrow, subsumed=self.lazy_nan and m in ("FSQRT", "FRNDINT"))
            self.status_dirty = True
        elif m in ("FPREM", "FPREM1"):
            self._set(0, "fprem_common(&x87_env_, %s, %s, %d)" %
                      (read(0), read(1), int(m == "FPREM1")), lines)
            self.status_dirty = True
        elif m == "FXCH":
            other = slots[-1] if slots else 1
            a = {part: self._read(0, part, lines) for part in ("value", "bits", "exact", "tag")}
            b = {part: self._read(other, part, lines) for part in a}
            a_narrow, b_narrow = self._slot(0)[1].narrow, self._slot(other)[1].narrow
            if self.lazy_nan:
                # A pure swap moves the deferred NaN with its value.
                a_pending, b_pending = self._slot(0)[1].pending, self._slot(other)[1].pending
                self._assign(0, narrow=b_narrow, pending=b_pending, **b)
                self._assign(other, narrow=a_narrow, pending=a_pending, **a)
            else:
                self._assign(0, narrow=b_narrow, **b)
                self._assign(other, narrow=a_narrow, **a)
        elif m == "FNSTSW" and operands == (("ax", 0),):
            if self.lazy_nan:
                self._fold_all(lines)
            lines.append("%s = (uint16_t)((x87_env_.fpu_sw & (uint16_t)~0x3800u) | ((%s) << 11));" %
                         (result, self._phys(self.top)))
        elif m in ("FNCLEX", "FCLEX"):
            if self.lazy_nan:
                # Eager canonicalised before clearing; suppress the IE it cleared.
                self._fold_all(lines, emit_ie=False)
            lines.append("x87_env_.fpu_sw &= (uint16_t)~0x80ffu;")
            self.status_dirty = True
        elif m in ("FDECSTP", "FINCSTP"):
            self._move_top(-1 if m == "FDECSTP" else 1)
            lines.append("x87_env_.fpu_sw &= (uint16_t)~0x0200u;")
            self.status_dirty = True
        elif m == "FNOP":
            pass
        else:
            # State-reading helpers (FXAM/FIST/FINIT/CW changes) remain audited
            # runtime effects. Afterward no scalar or environment fact survives.
            lines.extend(self.flush())
            lines.extend(x87.statements(data, address, result))
            self.reset()
        return lines
