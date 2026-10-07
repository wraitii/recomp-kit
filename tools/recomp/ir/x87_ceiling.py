"""UNPROVEN x87 ceiling lowering (relaxations C, D, E); corpus-only.

This is not part of the agreed performance-mode contract and has no production
entry point; see ``ceiling.py``. It subclasses the exact scalar tracker and
overrides only what each enabled relaxation discards:

C  slots track values only: no st_bits/st_exact/tags, popped values are never
   published, flushes write only live values and TOP.
D  arithmetic does not set IE/ZE and does not canonicalize NaNs; stores to
   guest memory substitute the x87 indefinite for any NaN. FCOM keeps exact
   C0/C2/C3 through an inline condition-bit update.
E  every slot is a C ``float``; arithmetic is plain float math with no control
   word selector (PC=00, RC=nearest assumed everywhere).

The lowering models only the instruction forms found in the corpus (loads,
stores, basic/integer-memory arithmetic, comparisons, FCHS/FABS/FSQRT, FXCH and
FNSTSW AX). Anything else (FLDCW, FIST*, FPREM, FRNDINT, FXAM, ...) raises a
named ``SSAError`` so the caller keeps the ordinary SSA body for that function.
"""
from . import x87
from .ssa import SSAError
from .x87_scalar import X87Scalar

# Constant NaN with the x87 "real indefinite" sign (0xffc00000 / 0xfff8...).
_INDEF32 = '-__builtin_nanf("")'
_INDEF64 = '-__builtin_nan("")'


class X87Ceiling(X87Scalar):
    def __init__(self, relax, convention=False):
        super().__init__(observe_loads=False, convention=convention)
        self.lite = "C" in relax
        self.nosticky = "D" in relax
        self.f32 = "E" in relax
        self.vtype = "float" if self.f32 else "double"

    def _parts(self):
        return ("value",) if self.lite else ("value", "bits", "exact", "tag")

    def _read(self, logical, part, lines):
        offset, slot = self._slot(logical)
        if part == "value" and slot.value is None:
            slot.value = self._temp("(%s)c->st[%s]" % (self.vtype, self._phys(offset)),
                                    lines, self.vtype)
        return super()._read(logical, part, lines)

    def _set(self, logical, expr, lines, bits="0", exact="0", tag=None, narrow=False):
        value = self._temp(expr, lines, self.vtype)
        if self.lite:
            self._assign(logical, value=value, narrow=narrow)
        else:
            self._assign(logical, value=value, bits=bits, exact=exact,
                         tag=tag or "ftag_classify(%s)" % value, narrow=narrow)

    def _drop(self):
        if self.lite:
            # Ceiling C: popped residue is neither kept nor published.
            _, slot = self._slot(0)
            slot.dirty.clear()
            self._move_top(1)
        else:
            super()._drop()

    def flush(self):
        if not self.lite:
            return super().flush()
        if not self.active:
            return []
        lines = []
        for offset, slot in sorted(self.slots.items()):
            if "value" in slot.dirty:
                lines.append("c->st[%s] = %s;" % (self._phys(offset), slot.value))
            slot.dirty.clear()
        if self.top_dirty:
            lines.append("c->fpu_top = (%s);" % self._phys(self.top))
            self.top_dirty = False
        if self.status_dirty:
            lines.append("c->fpu_sw = x87_env_.fpu_sw;")
            self.status_dirty = False
        return lines

    def _result(self, raw, lines):
        """Wrap a PC-controlled arithmetic result for the enabled relaxations."""
        if self.f32:
            if self.nosticky:
                return raw
            t = self._temp(raw, lines, "float")
            return "(%s != %s ? (x87_env_.fpu_sw |= 0x0001u, %s) : %s)" % (t, t, _INDEF32, t)
        if self.nosticky:
            return "(((x87_env_.fpu_cw >> 8) & 3u) == 0u ? (double)(float)(%s) : (%s))" % (raw, raw)
        return "fx87(&x87_env_, %s)" % raw

    def _store_value(self, value, bits):
        """Value written to guest memory by FST/FSTP (D canonicalizes NaNs here)."""
        if bits == 32:
            plain = value if self.f32 else "fto_float(&x87_env_, %s)" % value
            return "(%s != %s ? %s : %s)" % (value, value, _INDEF32, plain) if self.nosticky else plain
        plain = "(double)%s" % value if self.f32 else value
        return "(%s != %s ? %s : %s)" % (value, value, _INDEF64, plain) if self.nosticky else plain

    def statements(self, data, address=None, result=None):
        m, operands = data["mnem"], data["operands"]
        memory = [size for kind, size in operands if kind == "mem"]
        slots = [index for kind, index in operands if kind == "st"]
        bits = memory[0] * 8 if memory else 0
        lines = []
        self._activate(lines)

        def fail():
            raise SSAError("ceiling x87: unsupported %s %r" % (m, operands))

        def read(index):
            return self._read(index, "value", lines)

        def mem(integer=False):
            if integer:
                if bits not in (16, 32, 64):
                    fail()
                return self._temp("(int%d_t)rd%d((uint32_t)%s)" % (bits, bits, address), lines, "int64_t")
            if bits not in (32, 64):
                fail()
            return self._temp("(%s)rdf%d((uint32_t)%s)" % (self.vtype, bits, address), lines, self.vtype)

        def copy(src):
            parts = {part: self._read(src, part, lines) for part in self._parts()}
            narrow = self._slot(src)[1].narrow
            return parts, narrow

        if m in x87.CONSTANTS and not operands:
            self._move_top(-1)
            self._set(0, "(%s)%s" % (self.vtype, x87.CONSTANTS[m]), lines, narrow=m in ("FLD1", "FLDZ"))
        elif m == "FLD" and len(operands) == 1:
            if slots:
                parts, narrow = copy(slots[0])
                self._move_top(-1)
                self._assign(0, narrow=narrow, **parts)
            else:
                value = mem()
                self._move_top(-1)
                self._set(0, value, lines, narrow=bits == 32)
        elif m == "FILD" and len(memory) == 1 and len(operands) == 1:
            value = mem(True)
            self._move_top(-1)
            self._set(0, "(%s)%s" % (self.vtype, value), lines, bits="(uint64_t)%s" % value, exact="1")
        elif m in ("FST", "FSTP") and len(operands) == 1:
            if slots:
                if slots[0]:
                    parts, narrow = copy(0)
                    self._assign(slots[0], narrow=narrow, **parts)
            elif bits in (32, 64):
                lines.append("wrf%d((uint32_t)%s, %s);" % (bits, address, self._store_value(read(0), bits)))
            else:
                fail()
            if m == "FSTP":
                self._drop()
        elif m in x87.ARITH or m in x87.INTEGER_ARITH or (m.endswith("P") and m[:-1] in x87.ARITH):
            pop = m.endswith("P")
            base = m[:-1] if pop else m
            integer = base in x87.INTEGER_ARITH
            operator = (x87.INTEGER_ARITH if integer else x87.ARITH)[base]
            if memory and len(operands) == 1 and not pop:
                rhs = mem(integer)
                if integer:
                    rhs = "(%s)%s" % (self.vtype, rhs)
                dst, lhs = 0, read(0)
            elif slots and not memory and len(slots) == len(operands) and not integer:
                dst = slots[0] if len(slots) == 2 or pop else 0
                src = slots[1] if len(slots) == 2 else (0 if pop else slots[0])
                lhs, rhs = read(dst), read(src)
            elif not operands and pop:
                dst, lhs, rhs = 1, read(1), read(0)
            else:
                fail()
            if base.endswith("R"):
                lhs, rhs = rhs, lhs
            if operator == "/":
                # Sticky ZE (not D) keeps the runtime's fdivz; D divides plainly.
                raw = ("%s / %s" % (lhs, rhs) if self.nosticky else
                       "fdivz(&x87_env_, (double)%s, (double)%s)" % (lhs, rhs))
                if self.f32 and not self.nosticky:
                    raw = "(float)" + raw
            else:
                raw = "%s %s %s" % (lhs, operator, rhs)
            value = self._result(raw, lines)
            self._set(dst, value, lines, narrow=True)
            if not self.nosticky:
                self.status_dirty = True
            if pop:
                self._drop()
        elif m in ("FCOM", "FCOMP", "FUCOM", "FUCOMP", "FICOM", "FICOMP", "FCOMPP", "FUCOMPP", "FTST"):
            other = (mem(m.startswith("FI")) if memory else
                     ("(%s)0.0" % self.vtype) if m == "FTST" else read(slots[-1] if slots else 1))
            if memory and m.startswith("FI"):
                other = "(%s)%s" % (self.vtype, other)
            a = read(0)
            if self.nosticky:
                # Exact C3/C2/C0 only (ceiling D); IE is never raised.
                lines.append(
                    "x87_env_.fpu_sw = (uint16_t)((x87_env_.fpu_sw & (uint16_t)~0x4700u) | "
                    "((%s != %s || %s != %s) ? 0x4500u : (%s < %s) ? 0x0100u : "
                    "(%s == %s) ? 0x4000u : 0u));" % (a, a, other, other, a, other, a, other))
                self.status_dirty = True
            else:
                lines.append("%s(&x87_env_, (double)%s, (double)%s);" %
                             ("fucom" if m.startswith("FU") else "fcom", a, other))
                self.status_dirty = True
            for _ in range(2 if m.endswith("PP") else int(m.endswith("P"))):
                self._drop()
        elif m == "FCHS" and not operands:
            self._set(0, "-(%s)" % read(0), lines, narrow=self._slot(0)[1].narrow)
        elif m == "FABS" and not operands:
            self._set(0, ("__builtin_fabsf(%s)" if self.f32 else "fabs(%s)") % read(0), lines,
                      narrow=self._slot(0)[1].narrow)
        elif m == "FSQRT" and not operands:
            raw = ("__builtin_sqrtf(%s)" if self.f32 else "sqrt(%s)") % read(0)
            self._set(0, self._result(raw, lines), lines, narrow=True)
            if not self.nosticky:
                self.status_dirty = True
        elif m == "FXCH" and not memory and len(slots) == len(operands) and len(slots) <= 2:
            other = slots[-1] if slots else 1
            a = {part: self._read(0, part, lines) for part in self._parts()}
            b = {part: self._read(other, part, lines) for part in a}
            a_narrow, b_narrow = self._slot(0)[1].narrow, self._slot(other)[1].narrow
            self._assign(0, narrow=b_narrow, **b)
            self._assign(other, narrow=a_narrow, **a)
        elif m == "FNSTSW" and operands == (("ax", 0),):
            lines.append("%s = (uint16_t)((x87_env_.fpu_sw & (uint16_t)~0x3800u) | ((%s) << 11));" %
                         (result, self._phys(self.top)))
        elif m == "FNOP" and not operands:
            pass
        else:
            fail()
        return lines
