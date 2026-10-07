"""Scalar-region boundaries and full-state publication policy regressions.

Execution coverage lives in native_checks, where byte-backed wrap/copy/status/
control fixtures compare every CPU field and store snapshot against eager C.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.emit_c import emit
from ir.lift import Lifter
from ir.ssa import SSAError, build
from ir.simplify import canonicalize, simplify
from ir.publication import plan
from ir.summary import FunctionIR, default_successors
from ir.x87_scalar import INACTIVE, CarryShape, SlotShape, UNSAFE, X87Scalar, merge_shapes


def function(*hexes):
    lifter, at, insns = Lifter(), 0x1000, []
    for raw in map(bytes.fromhex, hexes):
        insns.append(lifter.lift(at, raw))
        at += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


def test_scalar_load_boundary_retains_a_strict_comparison():
    f = function("d9e8", "d906", "dec1", "8903", "c3")
    fast = emit(f, "fast").split("\n#else\n", 1)[1]
    strict = emit(f, "strict", x87_scalar_strict=True)
    # After FLD1, strict publishes before FLD [esi]. Guest accesses do not
    # observe x87 state, so fast defers past the integer store to RET.
    assert fast.index("rdf32(") < fast.index("wr32(") < fast.index("c->st[")
    assert strict.index("c->st[") < strict.index("rdf32(")


def test_scalar_pop_keeps_residue_without_physical_stack_helpers():
    text = emit(function("d9e8", "d9e8", "dec1", "ddd8", "c3"),
                "scalar")
    assert "fpush(" not in text and "fdrop(" not in text and "fset(" not in text
    assert "FTAG_EMPTY" in text and "c->st_bits[" in text
    assert "c->fpu_sw = x87_env_.fpu_sw;" in text


def test_scalar_environment_is_invalidated_after_control_word_change():
    text = emit(function("d9e8", "d92e", "d9e8", "dec1", "c3"),
                "scalar").split("\n#else\n", 1)[1]
    assert text.count("x87_env_.fpu_cw = c->fpu_cw;") == 2
    assert text.index("c->st[") < text.index("x87_set_cw(c,")


def test_deferred_accesses_do_not_claim_unpublished_cpu_fields():
    f = function("83c007", "8b16", "8903", "c3")
    s = build(f)
    canonicalize(s)
    pubs = plan(s, f.succ, [[key] for key in s.inputs if key[0] == "register"],
                access_fields=())
    load = next(v for b in s.blocks.values() for v in b.ops if v.opc == "LOAD")
    store = next(v for b in s.blocks.values() for v in b.ops if v.opc == "STORE")
    assert not pubs[load.id] and not pubs[store.id]
    # Neither access claimed EAX, so the return still publishes it.
    ret = next(b for b in s.blocks.values() if any(v.opc == "RETURN" for v in b.ops))
    eax = Lifter().register("EAX")
    assert any(key[1] == eax[1] for key in plan(s, f.succ, [[key] for key in s.inputs if key[0] == "register"],
                                                 access_fields=()).get(next(v.id for v in ret.ops if v.opc == "RETURN"), ()))
    simplify(s, pubs)
    assert any(load in b.ops for b in s.blocks.values())


def test_binary32_requires_proven_operands_and_keeps_a_general_precision_path():
    proven = emit(function("d906", "d906", "d8c8", "dec1", "c3"), "proven")
    incoming = emit(function("d806", "c3"), "incoming")
    assert "(float)(" in proven and "fpu_cw & 0x300u" in proven
    assert "fx87_exact(&x87_env_," in proven and "fx87(&x87_env_," in proven
    assert "(float)(" not in incoming


def test_narrow_fstp_m32_skips_the_redundant_rounding_step():
    # fld dword [esi]; fstp dword [ebx]; ret
    narrow = function("d906", "d91b", "c3")
    for lazy in (False, True):
        text = emit(narrow, "t", lazy_nan=lazy, _guard_null_checks=False)
        # Proven binary32 under PC=00: plain cast, but the NaN arm still calls
        # the helper so an sNaN payload is quieted.
        assert "(x87_env_.fpu_cw & 0x300u) == 0u && x87s" in text
        assert "? (float)(x87s" in text and ": fto_float(&x87_env_, x87s" in text
        assert text.count("fto_float(&x87_env_,") == 1
    # Arithmetic -> FSTP m32 keeps the same fast arm.
    arith = emit(function("d906", "d84604", "d84604", "d91b", "c3"), "t",
                 lazy_nan=True, _guard_null_checks=False)
    assert "(x87_env_.fpu_cw & 0x300u) == 0u && x87s" in arith
    # A double load is not proven binary32: keep the general helper.
    wide = emit(function("dd06", "d91b", "c3"), "t", _guard_null_checks=False)
    assert "== 0u && " not in wide
    assert "fto_float(&x87_env_," in wide
    # Strict x87 keeps the pre-access observation form, so no fast arm.
    strict = emit(narrow, "t", x87_scalar_strict=True, _guard_null_checks=False)
    assert "== 0u && " not in strict and "fto_float(&x87_env_," in strict


def test_isolated_arithmetic_keeps_general_recipe_to_bound_selector_cost():
    text = emit(function("d906", "d806", "d91b", "c3"), "single")
    assert "fx87_exact(&x87_env_, (double)((float)" not in text


def test_raw_emission_ignores_scalar_and_local_state_options():
    f = function("d9e8", "ddd8", "c3")
    assert emit(f, "raw", optimize=False) == emit(
        f, "raw", optimize=False, local_state=True)


def test_null_checks_select_strict_state_and_x87_publication():
    f = function("d9e8", "83c001", "8b16", "c3")
    text = emit(f, "guarded", local_state=True)
    strict, fast = text.split("\n#else\n", 1)
    assert strict.startswith("#if defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS")
    assert strict.index("c->st[") < strict.index("rd32(")
    assert strict.index("c->r[0] =") < strict.index("rd32(")
    assert fast.index("rd32(") < fast.index("c->st[")


def test_carry_shape_keeps_popped_parts_only_under_the_convention():
    # Regression: snapshot() drops popped value/bits/exact under the MSVC
    # convention, but the exact flush (msvc_convention=False or FINCSTP/
    # FDECSTP) must carry and republish them.
    for convention, expected in ((False, {"value", "bits", "exact", "tag"}),
                                 (True, {"tag"})):
        scalar = X87Scalar(convention=convention)
        scalar.statements({"mnem": "FLD1", "operands": ()}, None, None)
        scalar.statements({"mnem": "FADDP", "operands": ()}, None, None)
        popped = {part for position, slot in scalar.snapshot().slots
                  if position < 0 for part in slot.parts}
        assert popped == expected, (convention, popped)


def test_merge_shapes_takes_conservative_extremes():
    left = CarryShape(True, ((-1, SlotShape(frozenset(("tag",)), True,
                                            (("tag", "FTAG_EMPTY"),))),
                             (0, SlotShape(frozenset(("value", "exact")), False,
                                           (("exact", "0"),)))),
                      base=-1, low=-1, high=0, status_dirty=False)
    right = CarryShape(True, ((0, SlotShape(frozenset(("value", "exact")), True,
                                             (("exact", "0"),))),),
                       base=0, low=0, high=1, status_dirty=True)
    merged = merge_shapes([left, right])
    slots = dict(merged.slots)
    assert set(slots) == {-1, 0}
    assert slots[-1].parts == frozenset(("tag",))
    assert slots[0].parts == frozenset(("value", "exact"))
    # `narrow` intersects; agreement on `exact` survives, while `value` (not
    # carried by every predecessor) is a runtime variable.
    assert slots[0].narrow is False
    assert dict(slots[0].const) == {"exact": "0"}
    assert (merged.base, merged.low, merged.high) == (-1, -1, 1)
    # The predecessors published different TOPs, so TOP must be republished.
    assert merged.top_unknown and merged.status_dirty


def test_merge_shapes_forces_top_only_when_published_tops_disagree():
    moved = CarryShape(True, (), base=1, low=0, high=1)
    same = CarryShape(True, (), base=1, low=0, high=0)
    assert not merge_shapes([moved, same]).top_unknown
    # An inactive predecessor published its current TOP (relative base 0).
    assert merge_shapes([moved, INACTIVE]).top_unknown
    assert not merge_shapes([CarryShape(True, ()), INACTIVE]).top_unknown


def test_merge_shapes_rejects_an_overfull_window():
    wide = CarryShape(True, ((0, SlotShape(frozenset(("value",)))),
                             (7, SlotShape(frozenset(("value",))))),
                      base=0, low=0, high=7)
    assert merge_shapes([wide]) is not UNSAFE
    over = CarryShape(True, ((8, SlotShape(frozenset(("value",)))),),
                      base=0, low=0, high=8)
    assert merge_shapes([over]) is UNSAFE
    assert merge_shapes([wide, over]) is UNSAFE


def _arith_fnstsw():
    # fld1; fld1; faddp; fld1; faddp; fnstsw ax; mov [ebx],eax; ret
    return function("d9e8", "d9e8", "dec1", "d9e8", "dec1", "dfe0",
                    "8903", "c3")


def test_lazy_nan_off_is_the_eager_fx87_emission():
    f = _arith_fnstsw()
    eager = emit(f, "t", _guard_null_checks=False)
    lazy = emit(f, "t", lazy_nan=True, _guard_null_checks=False)
    assert eager != lazy
    # Off keeps the per-op helper; on defers the only IE check to FNSTSW.
    assert eager.count("fx87(&x87_env_,") >= 2
    assert "fx87(&x87_env_," not in lazy
    assert lazy.count("x87_env_.fpu_sw |= (uint16_t)(") == 1


def test_lazy_nan_fold_uses_a_bound_local():
    import re
    lazy = emit(_arith_fnstsw(), "t", lazy_nan=True, _guard_null_checks=False)
    assert "x87_indefinite()" in lazy
    # The ternary repeats only the bound local, never an arithmetic expression.
    assert re.search(r"\w+ = \((\w+) != \1 \? x87_indefinite\(\) : \1\);", lazy)


def test_lazy_nan_disabled_for_raw_strict_exact():
    f = _arith_fnstsw()
    assert (emit(f, "t", optimize=False, lazy_nan=True)
            == emit(f, "t", optimize=False, lazy_nan=False))
    assert (emit(f, "t", x87_scalar_strict=True, local_state=False, msvc_convention=False,
                 lazy_nan=True, _guard_null_checks=False)
            == emit(f, "t", x87_scalar_strict=True, local_state=False, msvc_convention=False,
                    lazy_nan=False, _guard_null_checks=False))
    assert (emit(f, "t", msvc_convention=False, lazy_nan=True, _guard_null_checks=False)
            == emit(f, "t", msvc_convention=False, lazy_nan=False, _guard_null_checks=False))


def test_lazy_nan_fchs_folds_before_negation():
    # fld [esi]; fadd st0,st0; fchs; fstp [ebx+4]; ret
    lazy = emit(function("d906", "d8c0", "d9e0", "d95b04", "c3"), "t",
                lazy_nan=True, _guard_null_checks=False)
    assert lazy.index("x87_indefinite()") < lazy.index("= -(")


def test_lazy_nan_fclex_canonicalises_without_ie():
    # fld [esi]; fadd st0,st0; fnclex; fnstsw ax; mov [ebx],eax; ret
    lazy = emit(function("d906", "d8c0", "dbe2", "dfe0", "8903", "c3"), "t",
                lazy_nan=True, _guard_null_checks=False)
    assert lazy.count("x87_env_.fpu_sw |= (uint16_t)(") == 0
    assert lazy.count("x87_indefinite()") == 1


def test_lazy_nan_dropped_value_folds_ie():
    # fld [esi]; fld st0; fsubp; fstp st0; fnstsw ax; mov [ebx],eax; ret
    lazy = emit(function("d906", "d9c0", "dee9", "ddd8", "dfe0", "8903", "c3"), "t",
                lazy_nan=True, _guard_null_checks=False)
    assert lazy.count("x87_env_.fpu_sw |= (uint16_t)(") == 1
