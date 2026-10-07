"""UNPROVEN SSA ceiling relaxations: option plumbing and emitted shapes.

These corpus-only experiments (ir/ceiling.py) are not the agreed performance
mode contract. The tests pin which state each relaxation removes and that
production selection cannot reach them. Execution evidence comes from the game
corpus, which judges the column on declared observations rather than full state.
"""
from pathlib import Path
import inspect
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir import ceiling, production
from ir.emit_c import emit
from ir.lift import Lifter
from ir.publication import plan
from ir.simplify import canonicalize
from ir.ssa import SSAError, build
from ir.summary import FunctionIR, default_successors


def function(*hexes):
    lifter, at, insns = Lifter(), 0x1000, []
    for raw in map(bytes.fromhex, hexes):
        insns.append(lifter.lift(at, raw))
        at += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


def ceil(f, relax, **kw):
    return emit(f, "t", x87_scalar=True, local_state=True, _ceiling=frozenset(relax), **kw)


def base(f, **kw):
    return emit(f, "t", x87_scalar=True, local_state=True, _guard_null_checks=False, **kw)


# mov eax,[esi]; add eax,1; mov [edi],eax; ret
INT_STORE = function("8b06", "83c001", "8907", "c3")
# fld1; fld1; faddp; fstp dword [edi]; ret
X87_ADD = function("d9e8", "d9e8", "dec1", "d91f", "c3")
# fld dword [esi]; fmul dword [esi+4]; fstp dword [edi]; ret
X87_MUL = function("d906", "d84e04", "d91f", "c3")
# fld dword [esi]; fcomp dword [edi]; fnstsw ax; ret
X87_CMP = function("d906", "d81f", "dfe0", "c3")


def test_parse_relaxations_and_labels():
    assert ceiling.parse_relaxations("all") == frozenset("ABCDE")
    assert ceiling.parse_relaxations("a, c") == frozenset("AC")
    assert ceiling.parse_relaxations("BDE") == frozenset("BDE")
    assert ceiling.parse_relaxations(None) == frozenset() == ceiling.parse_relaxations("")
    assert ceiling.label(frozenset("EA")) == "SSA ceiling [A,E]"
    with pytest.raises(ValueError, match="unknown ceiling relaxation F"):
        ceiling.parse_relaxations("A,F")


def test_ceiling_requires_scalar_locals_and_known_letters():
    with pytest.raises(SSAError, match="require optimized scalar x87"):
        emit(INT_STORE, "t", _ceiling=frozenset("A"))
    with pytest.raises(SSAError, match="require optimized scalar x87"):
        emit(INT_STORE, "t", x87_scalar=True, _ceiling=frozenset("A"))
    with pytest.raises(SSAError, match="unknown ceiling relaxation Z"):
        ceil(INT_STORE, "Z")


def test_empty_ceiling_is_byte_identical_and_unreachable_from_production():
    for f in (INT_STORE, X87_ADD, X87_MUL, X87_CMP):
        plain = emit(f, "t", x87_scalar=True, local_state=True)
        assert plain == emit(f, "t", x87_scalar=True, local_state=True, _ceiling=frozenset())
        assert "__builtin_nan" not in plain
    # Production never names the experiment, and the emit argument is private.
    assert "ceiling" not in inspect.getsource(production).lower()
    assert "_ceiling" in inspect.signature(emit).parameters
    assert "ceiling" not in inspect.signature(production.apply).parameters


def stores_then_publish(text):
    """Return True when no CPU GPR assignment precedes the final guest store."""
    last_store = text.rindex("wr32(")
    return not re.search(r"c->r\[\d\] = ", text[:last_store])


def test_a_removes_store_snapshots_but_keeps_return_state():
    strict = base(INT_STORE)
    relaxed = ceil(INT_STORE, "A")
    assert not stores_then_publish(strict)
    assert stores_then_publish(relaxed)
    assert "c->eflags_zf =" in strict.split("wr32(")[0]
    assert "c->eflags_zf =" not in relaxed.split("wr32(")[0]
    # Return still publishes ESP/EIP/EAX.
    tail = relaxed[relaxed.rindex("wr32("):]
    assert "c->r[0] = " in tail and "recomp_return(c);" in tail


def test_a_skips_x87_flush_before_stores():
    strict = base(X87_ADD)
    relaxed = ceil(X87_ADD, "A")
    assert strict.index("c->st[") < strict.index("wrf32(")
    assert relaxed.index("wrf32(") < relaxed.index("c->fpu_top =")
    assert "c->st[" not in relaxed.split("wrf32(")[0]


def test_a_plan_leaves_known_facts_for_unpublished_effects():
    f = INT_STORE
    s = build(f)
    canonicalize(s)
    groups = [[key] for key in s.inputs if key[0] == "register"]
    pubs = plan(s, f.succ, groups, unpublished=frozenset(("STORE",)))
    store = next(v for b in s.blocks.values() for v in b.ops if v.opc == "STORE")
    assert pubs[store.id] == ()
    assert plan(s, f.succ, groups)[store.id] != ()


def test_b_flags_are_not_live_in_or_live_out():
    # jz +1; nop; ret consumes the incoming ZF; cmp/ret leaves flags live-out.
    consumer = function("7401", "90", "c3")
    assert "c->eflags_zf" in base(consumer)
    assert "c->eflags_zf" not in ceil(consumer, "B")
    producer = function("39d8", "c3")  # cmp eax,ebx; ret
    assert "c->eflags_zf =" in base(producer)
    assert "c->eflags_" not in ceil(producer, "B").replace("c->eflags_df", "")


def test_b_flags_consumed_inside_the_function_stay_exact():
    # cmp eax,ebx; jz skip; inc eax; skip: ret
    f = function("39d8", "7401", "40", "c3")
    text = ceil(f, "B")
    assert "0x1ull" in text and "if (" in text  # a branch condition computed from live ALU flags
    assert "c->eflags_" not in text


def test_b_flags_are_neither_published_nor_reloaded_around_calls():
    # call 0x2000; cmp eax,ebx; ret
    f = function("e8fb0f0000", "39d8", "c3")
    strict = base(f, call_symbols={0x2000: "callee"})
    relaxed = ceil(f, "B", call_symbols={0x2000: "callee"})
    assert "c->eflags_zf" in strict
    assert "c->eflags_" not in relaxed
    assert "callee(c);" in relaxed


def test_c_drops_tags_integer_shadows_and_residue():
    strict = base(X87_ADD)
    lite = ceil(X87_ADD, "C")
    for name in ("st_bits", "st_exact", "fpu_tag", "FTAG_EMPTY", "ftag_classify"):
        assert name in strict
        assert name not in lite
    assert "c->fpu_top =" in lite  # TOP still published at return


def test_c_does_not_publish_popped_values_but_publishes_returned_st0():
    # fld1; fld1; faddp; (ST0 returned): only the surviving slot reaches the CPU.
    f = function("d9e8", "d9e8", "dec1", "c3")
    text = ceil(f, "C")
    assert text.count("c->st[") == 1
    popped = function("d9e8", "ddd8", "c3")  # fld1; fstp st0 leaves nothing live
    assert "c->st[" not in ceil(popped, "C")


def test_d_has_no_sticky_status_or_per_operation_canonicalization():
    strict = base(X87_MUL)
    relaxed = ceil(X87_MUL, "D")
    assert "fx87(" in strict and "fx87(" not in relaxed
    assert "fpu_sw |=" not in relaxed
    # NaN canonicalization moves to the guest store.
    assert '-__builtin_nanf("")' in relaxed


def test_d_keeps_exact_condition_bits_for_compares():
    strict = base(X87_CMP)
    relaxed = ceil(X87_CMP, "D")
    assert "fcom(" in strict and "fcom(" not in relaxed
    for bits in ("0x4700u", "0x4500u", "0x0100u", "0x4000u"):
        assert bits in relaxed
    assert "fpu_sw" in relaxed and "fstsw" not in relaxed
    assert "(x87_top_" in relaxed  # FNSTSW AX still injects TOP


def test_e_has_no_control_word_selector_and_uses_float_slots():
    assert "fx87(" in base(X87_MUL)
    text = ceil(X87_MUL, "E")
    assert "x87_env_.fpu_cw" not in text.replace("x87_env_.fpu_cw = c->fpu_cw;", "")
    assert "double x87s" not in text and "float x87s" in text
    assert "fx87(" not in text and "fto_float" not in text
    # D without E keeps the precision selector, E removes it.
    selector = ceil(X87_MUL, "D")
    assert "(x87_env_.fpu_cw >> 8) & 3u" in selector
    assert "(x87_env_.fpu_cw >> 8) & 3u" not in ceil(X87_MUL, "DE")


def test_e_plain_float_arithmetic_has_no_double_widening():
    text = ceil(X87_MUL, "DE")
    assert re.search(r"x87s\d+ = x87s\d+ \* x87s\d+;", text)
    assert "(float)rdf32(" in text


def test_x87_shapes_outside_the_ceiling_model_raise_named_fallbacks():
    for raw, name in (("d92e", "FLDCW"), ("db1f", "FISTP"), ("d9fc", "FRNDINT")):
        f = function(raw, "c3")
        for relax in ("C", "D", "E"):
            with pytest.raises(SSAError, match="ceiling x87: unsupported %s" % name):
                ceil(f, relax)
        # A and B alone retain the exact general x87 recipes for these shapes.
        assert ceil(f, "AB")


def test_each_relaxation_alone_emits_valid_shape_for_every_sample():
    samples = (INT_STORE, X87_ADD, X87_MUL, X87_CMP, function("39d8", "7401", "40", "c3"))
    for f in samples:
        for relax in "ABCDE":
            text = ceil(f, relax)
            assert text.startswith("void t(X86 *c) {") and text.rstrip().endswith("}")
            assert "RECOMP_NULL_CHECKS" not in text  # ceiling skips the dual null-check body
