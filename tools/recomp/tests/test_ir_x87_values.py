"""Bounded write-through x87 value reuse: stack model and recipe integration.

These tests exercise `ir.x87_values.X87Values` directly and through
`ir.x87.statements`. They check that read reuse follows the logical x87 stack,
that every audited helper call survives in order, and that the disabled path is
unchanged.

No original guest bytes are needed: the effect descriptors below are the same
validated shapes `x87.lower` produces.
"""
import re
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir import x87
from ir.x87_values import X87Values, X87Region


# -- tracker model ---------------------------------------------------------

def test_declarations_cover_the_window():
    values = X87Values(depth=3)
    assert values.declarations() == ["double x87v0;", "double x87v1;", "double x87v2;"]


def test_zero_depth_is_rejected():
    with pytest.raises(ValueError):
        X87Values(depth=0)


def test_depth_cannot_exceed_the_register_file():
    assert X87Values(depth=8).depth == 8
    with pytest.raises(ValueError):
        X87Values(depth=9)


def test_push_assigns_a_variable_and_shifts_older_values():
    values = X87Values()
    lines = []
    first = values.push("A", lines)
    assert lines == ["%s = A;" % first]
    assert values.read(0, "ST0") == first
    assert values.read(1, "ST1") == "ST1"

    second = values.push("B", lines)
    assert second != first
    assert values.read(0, "ST0") == second
    assert values.read(1, "ST1") == first
    assert values.read(2, "ST2") == "ST2"


def test_push_copy_aliases_without_emitting_an_assignment():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    lines.clear()
    values.push_copy(0)
    assert lines == []  # the physical FLD ST(0) helper is emitted by statements
    assert values.read(0, "ST0") == a
    assert values.read(1, "ST1") == a
    assert values.read(2, "ST2") == "ST2"


def test_push_copy_of_an_untracked_slot_is_unknown():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    values.push_copy(2)  # ST(2) was never tracked
    assert values.read(0, "ST0") == "ST0"
    # Old ST(0) moved to ST(1); the pushed copy is unknown.
    assert values.read(1, "ST1") == a


def test_drop_moves_the_window_up():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    b = values.push("B", lines)
    values.drop()
    # After B, ST(1) was A; the pop brings A back to ST(0) and drops B.
    assert values.read(0, "ST0") == a
    assert values.read(1, "ST1") == "ST1"
    assert values.read(2, "ST2") == "ST2"


def test_write_reuses_a_dead_slot_variable():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    assert values.write(0, "A2", lines) == a
    assert lines[-1] == "%s = A2;" % a


def test_copy_shares_a_variable_between_slots():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    values.copy(1, 0)
    assert values.read(0, "ST0") == a
    assert values.read(1, "ST1") == a


def test_swap_with_an_out_of_window_slot_forgets_st0():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    b = values.push("B", lines)
    # FXCH ST(3) with depth 3: ST(0) leaves the window, ST(3) was unknown.
    values.swap(3)
    assert values.read(0, "ST0") == "ST0"
    assert values.read(1, "ST1") == a  # untouched below the swapped top
    assert values.read(2, "ST2") == "ST2"
    assert values.known(0) is None
    # The released variable is reusable again.
    assert values.write(0, "C", lines) in values._names


def test_swap_with_an_out_of_window_slot_of_an_empty_window():
    values = X87Values()
    values.swap(5)
    assert values.known(0) is None


def test_swap_with_slot_zero_is_a_noop():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    values.swap(0)
    assert values.read(0, "ST0") == a


def test_push_at_full_depth_drops_the_oldest_slot():
    values = X87Values(depth=8)
    lines = []
    names = [values.push("V%d" % i, lines) for i in range(8)]
    values.push("V8", lines)
    # V0 (held by names[0]) has fallen out and its variable is recycled for
    # the new top; the remaining seven shift up one slot.
    assert values.read(7, "ST7") == names[1]
    assert values.read(6, "ST6") == names[2]
    assert lines[-1] == "%s = V8;" % names[0]
    assert values.read(0, "ST0") != names[-1]


def test_swap_within_window_then_write_keeps_alias():
    values = X87Values()
    lines = []
    a = values.push("A", lines)
    b = values.push("B", lines)
    values.copy(0, 1)  # ST(0) aliases ST(1)
    values.write(1, "C", lines)
    assert values.read(0, "ST0") == a  # still the old ST(1) value
    assert values.read(1, "ST1") != a


def test_invalidate_forgets_every_value():
    values = X87Values()
    lines = []
    values.push("A", lines)
    values.push("B", lines)
    values.invalidate()
    assert [values.read(i, "S%d" % i) for i in range(3)] == ["S0", "S1", "S2"]
    assert values.declarations() == ["double x87v0;", "double x87v1;", "double x87v2;"]


def test_variable_pool_never_exhausts_under_long_rotation():
    values = X87Values()
    lines = []
    for step in range(200):
        values.push("V%d" % step, lines)
        values.push_copy(step % 3)
        if step % 2:
            values.drop()
        values.write(step % 3, "W%d" % step, lines)
        for line in lines:
            name = line.split(" = ")[0]
            assert name in values._names
    # Every tracked slot names a declared, non-freed variable.
    for slot in values._slot:
        if slot is not None:
            assert slot in values._names


# -- recipe integration ----------------------------------------------------

HELPER = re.compile(
    r"\b(fset|fpush_st|fpush_int|fpush|fcopy|fdrop|fxch|fucom|fcom|"
    r"fprem_common|fxam|fstsw|x87_finit|x87_set_cw)\b")


def helpers(lines):
    """Ordered helper names, ignoring the tracker assignments between them."""
    return [match.group(1) for line in lines for match in HELPER.finditer(line)]


def assigned(lines):
    """Assignments in order, as (variable, expression) pairs."""
    out = []
    for line in lines:
        match = re.fullmatch(r"(x87v\d+) = (.*);", line)
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def reads(lines):
    """Every variable read outside an assignment line."""
    out = []
    for line in lines:
        if re.fullmatch(r"x87v\d+ = .*;", line):
            continue
        out.extend(re.findall(r"x87v\d+", line))
    return out


def run(data, values=None, address=None, result="res"):
    # A memory operand needs an address expression; register effects must pass
    # None so the region knows no guest access is happening.
    if address is None and any(kind == "mem" for kind, _ in data["operands"]):
        address = "addr"
    return x87.statements(data, address, result, values)


@pytest.mark.parametrize("data", [
    {"mnem": "FLD1", "operands": ()},
    {"mnem": "FLD", "operands": (("mem", 4),)},
    {"mnem": "FLD", "operands": (("st", 0),)},
    {"mnem": "FILD", "operands": (("mem", 4),)},
    {"mnem": "FADD", "operands": (("st", 0), ("st", 1))},
    {"mnem": "FSUBR", "operands": (("st", 0), ("st", 1))},
    {"mnem": "FADDP", "operands": (("st", 1),)},
    {"mnem": "FMUL", "operands": (("mem", 4),)},
    {"mnem": "FDIV", "operands": (("mem", 8),)},
    {"mnem": "FSQRT", "operands": ()},
    {"mnem": "FRNDINT", "operands": ()},
    {"mnem": "FPREM", "operands": ()},
    {"mnem": "FXCH", "operands": (("st", 1),)},
    {"mnem": "FSTP", "operands": (("mem", 4),)},
    {"mnem": "FSTP", "operands": (("mem", 10),)},
    {"mnem": "FISTP", "operands": (("mem", 4),)},
    {"mnem": "FCOM", "operands": ()},
    {"mnem": "FCOMPP", "operands": ()},
    {"mnem": "FTST", "operands": ()},
    {"mnem": "FXAM", "operands": ()},
    {"mnem": "FNCLEX", "operands": ()},
    {"mnem": "FNSTSW", "operands": (("ax", 0),)},
    {"mnem": "FNINIT", "operands": ()},
    {"mnem": "FDECSTP", "operands": ()},
    {"mnem": "FINCSTP", "operands": ()},
    {"mnem": "FNOP", "operands": ()},
])
def test_helper_calls_survive(data):
    baseline = run(data, values=None)
    tracked = run(data, values=X87Values())
    # Binding a value to a variable hoists a nested helper out of its argument,
    # so compare the multiset rather than the textual order.
    assert sorted(helpers(tracked)) == sorted(helpers(baseline))


@pytest.mark.parametrize("data", [
    {"mnem": "FLD", "operands": (("mem", 4),)},
    {"mnem": "FILD", "operands": (("mem", 4),)},
    {"mnem": "FADD", "operands": (("st", 0), ("st", 1))},
    {"mnem": "FADDP", "operands": (("st", 1),)},
    {"mnem": "FSTP", "operands": (("mem", 4),)},
    {"mnem": "FISTP", "operands": (("mem", 4),)},
    {"mnem": "FCOMPP", "operands": ()},
    {"mnem": "FXCH", "operands": (("st", 1),)},
    {"mnem": "FNINIT", "operands": ()},
])
def test_knowledge_matches_the_helper_shape(data):
    values = X87Values()
    tracked = run(data, values=values)
    # Every tracked slot names a declared variable, and every variable read
    # elsewhere was either assigned on an earlier line or is a c->st read.
    for slot in values._slot:
        assert slot is None or slot in values._names
    defined = {name for name, _ in assigned(tracked)}
    for line in tracked:
        for variable in reads([line]):
            assert variable in values._names
            assert variable in defined


def test_arithmetic_reuses_a_preceding_produced_value():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    lines = run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=values)
    # Both operands of the add are the value that FLD1 pushed: one variable
    # is assigned and the helper reads it instead of c->st.
    assert assigned(lines)
    variable = assigned(lines)[0][0]
    assert variable in lines[-1]
    assert "ST(c, 0)" not in lines[-1]


def test_stacked_add_reuses_both_operands():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    run({"mnem": "FLDZ", "operands": ()}, values=values)
    add = run({"mnem": "FADD", "operands": (("st", 0), ("st", 1))}, values=values)
    # Both adds read tracked values, so the result assignment names two variables.
    expression = assigned(add)[0][1]
    assert expression.count("x87v") == 2
    assert "ST(c, 0)" not in expression
    assert "ST(c, 1)" not in expression


def test_fild_does_not_track_the_exact_integer():
    values = X87Values()
    lines = run({"mnem": "FILD", "operands": (("mem", 4),)}, values=values)
    assert helpers(lines) == ["fpush_int"]
    assert assigned(lines) == []
    assert values.known(0) is None  # the promoted value is not trusted as exact


def test_finit_invalidates_the_window():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    assert values.known(0) is not None
    run({"mnem": "FINIT", "operands": ()}, values=values)
    assert values.known(0) is None


def test_fdecstp_and_fincstp_rotate_the_window():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    a = values.read(0, "ST0")
    run({"mnem": "FDECSTP", "operands": ()}, values=values)
    assert values.read(1, "ST1") == a
    assert values.read(0, "ST0") == "ST0"
    run({"mnem": "FINCSTP", "operands": ()}, values=values)
    assert values.read(0, "ST0") == a


def test_disabled_recipes_do_not_name_tracker_variables():
    for data in (
        {"mnem": "FLD1", "operands": ()},
        {"mnem": "FADD", "operands": (("st", 0), ("st", 1))},
        {"mnem": "FADDP", "operands": (("st", 1),)},
        {"mnem": "FSTP", "operands": (("mem", 4),)},
    ):
        assert "x87v" not in "\n".join(run(data, values=None))


def test_invalidated_reads_fall_back_to_c_st():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    values.invalidate()
    lines = run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=values)
    assert helpers(lines) == ["fset"]
    # The inputs fall back to c->st even though the produced value is tracked.
    expression = assigned(lines)[0][1]
    assert expression.count("ST(c, 0)") == 2


def test_produced_value_is_read_back_through_the_variable():
    values = X87Values()
    push = run({"mnem": "FLD", "operands": (("mem", 4),)}, values=values)
    assert assigned(push)
    variable = assigned(push)[0][0]
    store = run({"mnem": "FSTP", "operands": (("mem", 4),)}, values=values)
    assert variable in store[-2]  # the wrf32 line
    assert "ST(c, 0)" not in store[-2]


def test_float_store_with_a_tracked_local_still_calls_fto_float():
    # fto_float owns the signalling-NaN quieting; the tracked value must still
    # be narrowed through it rather than through a local C cast.
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    lines = run({"mnem": "FSTP", "operands": (("mem", 4),)}, values=values)
    store = lines[-2]
    assert "fto_float(c, x87v" in store


def test_push_copy_then_fmul_reuses_the_copied_value():
    values = X87Values()
    run({"mnem": "FLD1", "operands": ()}, values=values)
    run({"mnem": "FLD", "operands": (("st", 0),)}, values=values)
    lines = run({"mnem": "FMUL", "operands": (("st", 0), ("st", 1))}, values=values)
    expression = assigned(lines)[0][1]
    # The copy aliases one variable into both operand slots.
    variables = re.findall(r"x87v\d+", expression)
    assert len(variables) == 2 and variables[0] == variables[1]


# -- observation-aware region ----------------------------------------------

from types import SimpleNamespace


def fake_ssa(blocks):
    """blocks: id -> (predecessors, [opc...]); entry is block 0."""
    return SimpleNamespace(entry=0, blocks={
        i: SimpleNamespace(phis=[SimpleNamespace(data=(None, list(preds)))],
                           ops=[SimpleNamespace(opc=o) for o in ops])
        for i, (preds, ops) in blocks.items()})


def test_region_defers_arithmetic_and_records_dirty():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    lines = run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    assert helpers(lines) == []          # no eager fset
    assert assigned(lines)               # value lives in a double local
    assert region._dirty == {0: "0"}


def test_region_flushes_before_a_guest_memory_access():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    lines = run({"mnem": "FSTP", "operands": (("mem", 4),)},
                values=region, address="addr")
    tag = next(i for i, l in enumerate(lines) if "ftag_put" in l)
    store = next(i for i, l in enumerate(lines) if "wrf32" in l)
    assert tag < store
    assert region._dirty == {}


def test_region_materialises_a_dirty_popped_residue_before_fdrop():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADDP", "operands": (("st", 1),)}, values=region)
    # The second add pops the dirty first result, so its residue must be
    # written into c->st immediately before the eager fdrop.
    lines = run({"mnem": "FADDP", "operands": (("st", 1),)}, values=region)
    assert lines[-1] == "fdrop(c);"
    assert any(l.startswith("c->st[") for l in lines[:-1])
    assert region._dirty == {0: "0"}


def test_region_fpush_st_materialises_a_dirty_source():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    lines = run({"mnem": "FLD", "operands": (("st", 0),)}, values=region)
    assert any("ftag_put" in l for l in lines)
    assert lines[-1] == "fpush_st(c, 0);"
    # The flushed source is now eager in c, so the copy is tracked but clean.
    assert region._dirty == {}
    assert region.values.known(0) is not None


def test_region_fxch_materialises_both_slots_and_clears_dirty():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    lines = run({"mnem": "FXCH", "operands": (("st", 1),)}, values=region)
    assert any("ftag_put" in l for l in lines)
    assert region._dirty == {}


def test_region_eviction_flushes_before_dropping_state():
    region = X87Region(depth=2)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    assert set(region._dirty) == {0, 1}
    lines = run({"mnem": "FLD1", "operands": ()}, values=region)
    assert any("ftag_put" in l for l in lines)
    assert 0 not in region._dirty  # flushed, then the new push shifted the rest


def test_region_flush_materialises_every_dirty_slot_for_an_observation():
    # The emitter calls this before an interleaved integer store/load, a call,
    # a division error seam and a return, so the CPU snapshot sees full state.
    region = X87Region(depth=2)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    assert region._dirty
    lines = region.flush()
    assert region._dirty == {}
    assert sum(1 for l in lines if "c->st[" in l) == 2
    assert sum(1 for l in lines if "ftag_put" in l) == 2


def test_region_finit_materialises_residue_then_resets():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    lines = run({"mnem": "FINIT", "operands": ()}, values=region)
    assert any("ftag_put" in l for l in lines)
    assert lines[-1] == "x87_finit(c);"
    assert region._dirty == {} and region.values.known(0) is None


def test_region_reads_a_deferred_value_without_reloading_c_st():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    lines = run({"mnem": "FADDP", "operands": (("st", 1),)}, values=region)
    result = next(l for l in lines if l.startswith("x87v") and "fx87" in l)
    assert result.count("x87v") >= 2
    assert "ST(c, 0)" not in result


def test_region_edge_flush_then_join_reset_is_sound():
    # Emitter sequence at a join: every incoming branch edge flushes first, so
    # the header can drop the read cache without losing physical FPU state.
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    materialised = region.flush()
    assert any("ftag_put" in line for line in materialised)
    region.reset()
    assert region._dirty == {}
    assert region.values.known(0) is None


def test_region_backedge_flushes_then_loop_header_resets():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    # The loop-body CBRANCH flushes before its backedge.
    assert any("ftag_put" in line for line in region.flush())
    # The loop header is a join (entry predecessor and backedge), so its cache
    # resets; the block 0 entry is not the header in this shape.
    loop = fake_ssa({0: ([-1], []), 1: ([0, 2], []), 2: ([1], [])})
    region.at_block(0, loop)
    region.at_block(1, loop)
    assert region.values.known(0) is None and region._dirty == {}


def test_region_fallthrough_single_pred_keeps_the_read_cache():
    region = X87Region()
    run({"mnem": "FLD1", "operands": ()}, values=region)
    run({"mnem": "FADD", "operands": (("st", 0), ("st", 0))}, values=region)
    region.flush()  # an observation in the predecessor block
    chain = fake_ssa({0: ([-1], []), 1: ([0], []), 2: ([1], [])})
    region.at_block(0, chain)
    region.at_block(1, chain)
    region.at_block(2, chain)
    assert region.values.known(0) is not None
    assert region._dirty == {}


def test_region_at_block_keeps_a_single_predecessor_run():
    region = X87Region()
    lines = []
    region.values.push("A", lines)
    ssa = fake_ssa({0: ([-1], []), 1: ([0], []), 2: ([1], [])})
    region.at_block(0, ssa)
    region.at_block(1, ssa)
    region.at_block(2, ssa)
    assert region.values.known(0) is not None


def test_region_at_block_resets_at_a_join():
    region = X87Region()
    lines = []
    region.values.push("A", lines)
    ssa = fake_ssa({0: ([-1], []), 1: ([0, 2], []), 2: ([1], [])})
    region.at_block(0, ssa)
    region.at_block(1, ssa)
    assert region.values.known(0) is None


def test_region_at_block_resets_after_an_opaque_predecessor():
    region = X87Region()
    lines = []
    region.values.push("A", lines)
    ssa = fake_ssa({0: ([-1], ["DIV32"]), 1: ([0], [])})
    region.at_block(0, ssa)
    region.at_block(1, ssa)
    assert region.values.known(0) is None
