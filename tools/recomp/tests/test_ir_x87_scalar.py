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
    # After FLD1, strict publishes before FLD [esi]; fast publishes at the
    # integer store, still before its observer runs.
    assert fast.index("rdf32(") < fast.index("c->st[") < fast.index("wr32(")
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


def test_deferred_reads_do_not_claim_unpublished_cpu_fields():
    f = function("83c007", "8b16", "8903", "c3")
    s = build(f)
    canonicalize(s)
    pubs = plan(s, f.succ, [[key] for key in s.inputs if key[0] == "register"],
                read_fields=())
    load = next(v for b in s.blocks.values() for v in b.ops if v.opc == "LOAD")
    store = next(v for b in s.blocks.values() for v in b.ops if v.opc == "STORE")
    assert not pubs[load.id]
    eax = Lifter().register("EAX")
    assert ("register", eax[1]) in pubs[store.id]
    simplify(s, pubs)
    assert any(load in b.ops for b in s.blocks.values())


def test_binary32_requires_proven_operands_and_keeps_a_general_precision_path():
    proven = emit(function("d906", "d906", "d8c8", "dec1", "c3"), "proven")
    incoming = emit(function("d806", "c3"), "incoming")
    assert "(float)(" in proven and "fpu_cw & 0x300u" in proven
    assert "fx87_exact(&x87_env_," in proven and "fx87(&x87_env_," in proven
    assert "(float)(" not in incoming


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
