"""Scalar x87 regions with exact state at the existing observation boundaries.

Slots are named relative to a captured entry TOP. Push/pop/copy change this
compile-time mapping, not the physical CPU array. Values, exact-integer shadows,
tags and popped residue are written back before calls, division seams, opaque
effects and exits. In performance mode the tracker also carries unpublished
state across internal CFG edges as a fixed-point join shape (`x87_carry.py`);
strict mode keeps publishing at every edge. CW and SW live
in two scalar locals (`x87_cw_`, `x87_sw_`); helpers take the control word by
value and the status word by address through always-inlined `_sw` forms, so
neither local escapes and both stay SSA values instead of stack slots.

This is an instruction-derived representation change, not dead-state removal,
memory forwarding, a native call ABI or a floating-point approximation.

Guest loads and stores do not observe x87 state in the kit runtime: the
watchpoint and DirectDraw dirty tracking read only the address and value, and
null-check builds, whose fault dispatch exposes the CPU, translate with
fault_state = "exact". This is the former decoded x87-locals contract, which also left x87 state
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

from . import x87_transition as t
from .ssa import SSAError
from .x87_transition import Expr, Type, Assign, Evaluate, Store, PublishTags, Activation, Declare
from .x87_cw import CW_FAST, Dynamic, Guarded, condition_of, proven_fact


@dataclass
class Slot:
    value: Expr | None = None
    bits: Expr | None = None
    exact: Expr | None = None
    tag: Expr | None = None
    narrow: bool = False  # Proven binary32 when PC=00, not an incoming-state guess.
    dirty: set = field(default_factory=set)
    quiet: bool = False
    pending: bool = False  # Arithmetic NaN whose IE check/canonicalisation is deferred.


#: Slot components carried across an internal CFG edge.
PARTS = ("value", "bits", "exact", "tag")
PART_TYPES = t.PART_TYPES

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
#: Typed literal agreements retained across carry joins.
CARRY_LITERALS = frozenset((t.ZERO, t.ONE, t.EMPTY, t.literal(0, Type.U64)))


@dataclass(frozen=True)
class SlotShape:
    """Unpublished parts of one physical slot, keyed by relative position."""
    parts: frozenset = frozenset()
    narrow: bool = False
    const: tuple = ()                       # sorted (part, literal) agreements
    dirty: frozenset = frozenset()
    quiet: bool = False
    pending: bool = False


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


def merge_shapes(shapes):
    """Merge predecessor edge shapes into a block entry shape.

    Dirty parts union, cached clean parts and proofs intersect, counters take the
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
        dirty = frozenset().union(*(e.dirty for e in entries))
        common = (frozenset.intersection(*(e.parts for e in entries))
                  if len(entries) == len(known) else frozenset())
        parts = dirty | common
        if not parts:
            continue
        narrow = len(entries) == len(known) and all(e.narrow for e in entries)
        quiet = len(entries) == len(known) and all(e.quiet for e in entries)
        pending = len(entries) == len(known) and all(e.pending for e in entries)
        const = []
        for part in sorted(parts):
            literals = set()
            ok = len(entries) == len(known)
            for e in entries:
                agreement = dict(e.const)
                if part not in e.parts or part not in agreement:
                    ok = False
                    break
                literals.add(agreement[part])
            if ok and len(literals) == 1:
                const.append((part, literals.pop()))
        merged.append((p, SlotShape(parts, narrow, tuple(const), dirty, quiet, pending)))
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
    """Interpret typed recipes while tracking physical slot state and publication."""

    def __init__(self, observe_loads=False, convention=False, lazy_nan=False):
        self.clone = None
        self.activations = 0
        self.choice = Dynamic()
        self.proven_cw = None
        self.mode_hook_guard = False
        self.serial = 0
        self.temps = []
        self.observe_loads = observe_loads
        self.convention = convention
        self.lazy_nan = lazy_nan
        self.binary32 = True
        self.reset()

    @property
    def known_mode(self):
        return isinstance(self.choice, Guarded) and self.choice.boundary == 'entry'

    def reset(self):
        self.active = False
        self.top = 0
        self.top_dirty = self.top_unknown = False
        self.position = self.base = self.low = self.high = 0
        self.status_dirty = False
        self.slots = {}

    def _activation(self):
        operation = Activation(self.activations, self.clone, self.choice, self.known_mode)
        self.activations += 1
        return [operation]

    def _activate(self, operations):
        if not self.active:
            operations.extend(self._activation())
            self.active = True

    def _temp(self, value, operations, type=Type.F64):
        result = t.local(self.serial, type)
        self.serial += 1
        self.temps.append(Declare(result))
        operations.append(Assign(result, value))
        return result

    def _phys(self, offset):
        return Expr('physical', Type.U32, (t.environment('top'),), offset)

    def _slot(self, logical=0):
        self.high = max(self.high, self.position + logical)
        offset = (self.top + logical) & 7
        return offset, self.slots.setdefault(offset, Slot())

    def _read(self, logical, part, operations):
        offset, slot = self._slot(logical)
        if getattr(slot, part) is None:
            setattr(slot, part, self._temp(t.slot(part, self._phys(offset)), operations, PART_TYPES[part]))
        return getattr(slot, part)

    def _assign(self, logical, narrow=None, pending=None, quiet=None, **parts):
        _, slot = self._slot(logical)
        if narrow is not None:
            slot.narrow = narrow
        for part, value in parts.items():
            setattr(slot, part, value)
            slot.dirty.add(part)
        if quiet is not None:
            slot.quiet = quiet
        if pending is not None:
            slot.pending = pending

    def _fold_slot(self, slot, operations, emit_ie=True, canonical=True):
        if not slot.pending:
            return slot.value
        value = slot.value
        if value is None:
            slot.pending = False
            return None
        if not value.atomic:
            value = self._temp(t.group(value), operations)
        if emit_ie:
            operations.append(Assign(t.environment('sw'), Expr('nan_test', Type.U16, (value,)), '|='))
            self.status_dirty = True
        if canonical:
            slot.value = self._temp(Expr('nan_fold', Type.F64, (value,)), operations)
            slot.dirty.add('value')
            slot.quiet = True
        slot.pending = False
        return slot.value

    def _fold(self, logical, operations, emit_ie=True, canonical=True):
        return self._fold_slot(self._slot(logical)[1], operations, emit_ie, canonical)

    def _fold_all(self, operations, emit_ie=True, canonical=True):
        for _, slot in sorted(self.slots.items()):
            if slot.pending:
                self._fold_slot(slot, operations, emit_ie, canonical)

    def fold_pending(self, operations):
        if self.active:
            self._fold_all(operations)

    def _release(self, logical, operations, subsumed=False):
        slot = self._slot(logical)[1]
        if not slot.pending:
            return
        if subsumed:
            slot.pending = False
        else:
            self._fold_slot(slot, operations)

    def _set(self, logical, value, operations, bits=None, exact=None, tag=None, narrow=False,
             pending=False, subsumed=False):
        self._release(logical, operations, subsumed=subsumed)
        value = self._temp(value, operations)
        self._assign(logical, value=value, bits=bits if bits is not None else t.literal(0, Type.U64),
                     exact=exact if exact is not None else t.ZERO,
                     tag=tag if tag is not None else t.helper('ftag_classify', Type.TAG, value),
                     narrow=narrow, pending=pending, quiet=False)

    def _move_top(self, delta):
        self.top = (self.top + delta) & 7
        self.top_dirty = True
        self.position += delta
        self.low = min(self.low, self.position)

    def _drop(self, operations=None, subsumed=False):
        if operations is not None:
            self._release(0, operations, subsumed=subsumed)
        self._assign(0, exact=t.ZERO, tag=t.EMPTY)
        self._move_top(1)

    def _publish(self, convention):
        operations, tags = [], []
        for offset, slot in sorted(self.slots.items()):
            physical = self._phys(offset)
            position = self.low + ((offset - self.low) & 7)
            if convention and position < self.position:
                if position >= self.base and 'tag' in slot.dirty:
                    tags.append((physical, slot.tag))
            else:
                parts = ('value', 'exact', 'bits') if convention else ('value', 'bits', 'exact')
                for part in parts:
                    if part in slot.dirty and not (convention and part == 'bits' and t.zero(slot.exact)):
                        operations.append(Assign(t.slot(part, physical), getattr(slot, part)))
                if 'tag' in slot.dirty:
                    tags.append((physical, slot.tag))
            slot.dirty.clear()
        if tags:
            operations.append(PublishTags(tuple(tags)))
        if (self.position != self.base if convention else self.top_dirty) or self.top_unknown:
            operations.append(Assign(t.environment('top', True), t.group(self._phys(self.top))))
        self.top_dirty = self.top_unknown = False
        self.base = self.low = self.high = self.position
        if self.status_dirty:
            operations.append(Assign(t.environment('sw', True), t.environment('sw')))
            self.status_dirty = False
        return operations

    def flush(self):
        if not self.active:
            return []
        operations = []
        self._fold_all(operations)
        convention = self.convention and self.high < self.low + 8 and self.position < self.low + 8
        operations.extend(self._publish(convention))
        return operations

    def snapshot(self):
        if not self.active:
            return INACTIVE
        slots = []
        for offset, slot in self.slots.items():
            position = self.low + ((offset - self.low) & 7) - self.position
            dirty = frozenset(slot.dirty)
            parts = frozenset(part for part in PARTS if getattr(slot, part) is not None)
            if position < 0 and self.convention:
                parts = dirty & frozenset(('tag',))
            dirty &= parts
            if not parts:
                continue
            const = tuple((part, getattr(slot, part)) for part in sorted(parts)
                          if getattr(slot, part) in CARRY_LITERALS)
            slots.append((position, SlotShape(parts, slot.narrow, const, dirty, slot.quiet,
                                              slot.pending)))
        slots.sort()
        return CarryShape(True, tuple(slots), self.base - self.position,
                          self.low - self.position, self.high - self.position,
                          self.top_unknown, self.status_dirty)

    def seed(self, shape, values=None):
        self.reset()
        if not shape.active:
            return
        self.active = True
        self.top = self.position = 0
        self.base, self.low, self.high = shape.base, shape.low, shape.high
        self.top_unknown, self.status_dirty = shape.top_unknown, shape.status_dirty
        for position, slot_shape in shape.slots:
            slot = Slot(narrow=slot_shape.narrow, dirty=set(slot_shape.dirty),
                        quiet=slot_shape.quiet, pending=slot_shape.pending)
            agreements = dict(slot_shape.const)
            for part in slot_shape.parts:
                value = agreements.get(part)
                if value is None:
                    value = values[position, part] if values is not None else t.carry(position, part)
                setattr(slot, part, value)
            self.slots[position & 7] = slot

    def normalize(self, operations):
        if not self.active:
            operations.extend(self._activation())
            self.active = True
            return
        if self.top != 0:
            operations.append(Assign(t.environment('top'), self._phys(self.top)))
            self.slots = {((offset - self.top) & 7): slot for offset, slot in self.slots.items()}
        delta = self.position
        self.top = 0
        self.position -= delta
        self.base -= delta
        self.low -= delta
        self.high -= delta

    def site_choice(self):
        if self.clone == 'fast':
            assert isinstance(self.choice, Guarded), 'fast clone requires a guarded body choice'
            assert self.choice.condition == CW_FAST, 'fast clone supports only CW_FAST'
            return Guarded(self.choice.condition, self.choice.boundary)
        if self.proven_cw is not None:
            record = proven_fact(self.proven_cw, condition_of(self.proven_cw))
            if record is not None:
                return record
        return Dynamic()

    def apply(self, recipe, address=None, result=None):
        extended = isinstance(recipe, dict) and recipe.get('mnem') in ('FCOMI', 'FCOMIP', 'FUCOMI', 'FUCOMIP')
        r = t.recipe(recipe, extended=extended) if isinstance(recipe, dict) else recipe
        m, bits, slots = r.mnem, r.bits, r.slots
        memory = bool(bits)
        operations = []
        self._activate(operations)
        if address is not None and self.observe_loads:
            operations.extend(self.flush())
        sw, cw = t.environment('sw'), t.control(self.site_choice(), self.mode_hook_guard)
        def read(index):
            return self._read(index, 'value', operations)
        def mem(integer=False):
            return self._temp(t.load(bits, address, integer), operations, Type.I64 if integer else Type.F64)
        if r.action == 'constant':
            self._move_top(-1)
            self._set(0, t.CONSTANTS[m], operations, narrow=m in ('FLD1', 'FLDZ'))
        elif r.action == 'push':
            if slots:
                parts = {part: self._read(slots[0], part, operations) for part in ('value', 'bits', 'exact', 'tag')}
                slot = self._slot(slots[0])[1]
                quiet, narrow, pending = slot.quiet, slot.narrow, slot.pending if self.lazy_nan else False
                self._move_top(-1)
                if self.lazy_nan:
                    self._release(0, operations)
                self._assign(0, narrow=narrow, pending=pending if self.lazy_nan else None, quiet=quiet, **parts)
            else:
                value = mem()
                self._move_top(-1)
                self._set(0, value, operations, narrow=bits == 32)
        elif r.action == 'free':
            self._assign(r.src, tag=t.EMPTY)
        elif r.action == 'push_integer':
            value = mem(True)
            self._move_top(-1)
            self._set(0, t.cast(Type.F64, value), operations, bits=t.cast(Type.U64, value), exact=t.ONE)
        elif r.action == 'store':
            if slots:
                if slots[0]:
                    parts = {part: self._read(0, part, operations) for part in ('value', 'bits', 'exact', 'tag')}
                    slot = self._slot(0)[1]
                    quiet, narrow, pending = slot.quiet, slot.narrow, slot.pending
                    if self.lazy_nan:
                        self._release(slots[0], operations)
                    self._assign(slots[0], narrow=narrow, pending=pending if self.lazy_nan else None, quiet=quiet, **parts)
            else:
                if self.lazy_nan:
                    self._fold(0, operations)
                value = read(0)
                if bits == 32:
                    slot = self._slot(0)[1]
                    if slot.narrow and slot.quiet and self.known_mode and not self.observe_loads:
                        value = t.cast(Type.F32, t.group(value))
                    elif slot.narrow and not self.observe_loads:
                        if not value.atomic:
                            value = self._temp(t.group(value), operations)
                        value = Expr('float_store', Type.F32, (cw, value))
                    else:
                        value = t.helper('fto_float', Type.F32, cw, value)
                operations.append(Store(bits, address, value, True))
            if r.pops:
                self._drop(operations, subsumed=self.lazy_nan and bool(slots and slots[0]))
        elif r.action == 'store_integer' and not self.observe_loads:
            if self.lazy_nan:
                self._fold(0, operations)
            value, exact = read(0), self._read(0, 'exact', operations)
            raw = t.literal(0, Type.U64) if t.zero(exact) else self._read(0, 'bits', operations)
            conversion = t.helper('fist', Type.I64, sw, cw, value, exact, raw)
            value = Expr('integer_convert', Type.I64, conversion.args, bits)
            operations.append(Store(bits, address, t.cast({16: Type.U16, 32: Type.U32, 64: Type.U64}[bits], value)))
            self.status_dirty = True
            if r.pops:
                self._drop(operations)
        elif r.action == 'arithmetic':
            if memory:
                rhs, dst = mem(r.integer), 0
                lhs = read(0)
                narrow = self._slot(0)[1].narrow and not r.integer and bits == 32
            else:
                dst = r.dst
                lhs, rhs = read(dst), read(r.src)
                narrow = self._slot(dst)[1].narrow and self._slot(r.src)[1].narrow
            if r.reverse:
                lhs, rhs = rhs, lhs
            raw = t.arithmetic(r.operator, lhs, rhs, sw)
            use_float = narrow and r.operator in ('+', '-', '*') and not self.observe_loads and self.binary32
            if self.lazy_nan:
                value = (Expr('float_arithmetic', Type.F64, (cw, lhs, rhs, raw), r.operator) if use_float else
                         Expr('pc_round', Type.F64, (cw, raw)))
                self._set(dst, value, operations, narrow=True, pending=True, subsumed=True)
            else:
                value = t.helper('fx87', Type.F64, sw, cw, raw)
                if use_float:
                    value = Expr('float_arithmetic', Type.F64, (cw, lhs, rhs, value, sw), r.operator)
                self._set(dst, value, operations, narrow=True)
            self.status_dirty = True
            if r.pops:
                self._drop(operations, subsumed=True)
        elif r.action == 'compare':
            if self.lazy_nan:
                self._fold(0, operations)
                a = read(0)
                if memory:
                    other = mem(r.integer)
                elif m == 'FTST':
                    other = t.literal(0.0, Type.F64)
                else:
                    self._fold(r.src, operations)
                    other = read(r.src)
            else:
                other = mem(r.integer) if memory else t.literal(0.0, Type.F64) if m == 'FTST' else read(r.src)
                a = read(0)
            operations.append(Evaluate(t.helper('fucom' if m.startswith('FU') else 'fcom', Type.VOID, sw, a, other)))
            self.status_dirty = True
            for _ in range(r.pops):
                self._drop(operations)
        elif r.action == 'compare_flags':
            if result is None:
                raise SSAError('x87 flag comparison has no SSA result')
            if self.lazy_nan:
                self._fold(0, operations)
                self._fold(r.src, operations)
            value = t.helper('fcomi_flags', Type.U16, sw, read(0), read(r.src),
                             t.literal(int(m.startswith('FU'))))
            operations.append(Assign(result, value))
            self.status_dirty = True
            for _ in range(r.pops):
                self._drop(operations)
        elif r.action == 'unary':
            narrow = self._slot(0)[1].narrow if m in ('FABS', 'FCHS') else m == 'FSQRT'
            if self.lazy_nan and m in ('FABS', 'FCHS'):
                self._fold(0, operations)
            self._set(0, t.unary(m, read(0), sw, cw), operations, narrow=narrow,
                      subsumed=self.lazy_nan and m in ('FSQRT', 'FRNDINT', 'FSIN', 'FCOS', 'F2XM1'))
            self.status_dirty = True
        elif r.action == 'remainder':
            self._set(0, t.helper('fprem', Type.F64, sw, read(0), read(1), t.literal(int(m == 'FPREM1'))), operations)
            self.status_dirty = True
        elif r.action == 'exchange':
            other = r.src
            a = {part: self._read(0, part, operations) for part in ('value', 'bits', 'exact', 'tag')}
            b = {part: self._read(other, part, operations) for part in a}
            a_slot, b_slot = self._slot(0)[1], self._slot(other)[1]
            a_narrow, b_narrow = a_slot.narrow, b_slot.narrow
            a_quiet, b_quiet = a_slot.quiet, b_slot.quiet
            a_pending, b_pending = a_slot.pending, b_slot.pending
            self._assign(0, narrow=b_narrow, quiet=b_quiet,
                         pending=b_pending if self.lazy_nan else None, **b)
            self._assign(other, narrow=a_narrow, quiet=a_quiet,
                         pending=a_pending if self.lazy_nan else None, **a)
        elif r.action == 'status':
            if self.lazy_nan:
                self._fold_all(operations)
            operations.append(Assign(result, Expr('status_word', Type.U16, (sw, self._phys(self.top)))))
        elif r.action == 'clear':
            if self.lazy_nan:
                self._fold_all(operations, emit_ie=False)
            operations.append(Assign(sw, Expr('clear_mask', Type.U16, data=0x80ff), '&='))
            self.status_dirty = True
        elif r.action == 'rotate':
            self._move_top(-1 if m == 'FDECSTP' else 1)
            operations.append(Assign(sw, Expr('clear_mask', Type.U16, data=0x200), '&='))
            self.status_dirty = True
        elif r.action != 'nop':
            operations.extend(self.flush())
            operations.extend(t.eager(r, address, result))
            operations.append(t.Reset())
            self.reset()
        return tuple(operations)
