"""Stable translation units and the dependency boundary around hook policy."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T


def function(addr):
    return SimpleNamespace(addr=addr, name="fixture_%08x" % addr,
                           insns=[SimpleNamespace(addr=addr)])


def assignment(functions, bodies, budget=100):
    return {fn.addr: name for name, group in T.partition_chunks(functions, bodies, budget)
            for fn in group}


def test_partition_is_deterministic_and_bounded():
    functions = [function(0x10000 + i * 32) for i in range(50)]
    bodies = {fn.addr: ["x" * (i + 1)] for i, fn in enumerate(functions)}
    expected = assignment(functions, bodies)
    assert assignment(list(reversed(functions)), bodies) == expected
    assert len(expected) == len(functions)
    for _, group in T.partition_chunks(functions, bodies, 100):
        assert sum(sum(len(s) + 1 for s in bodies[f.addr]) for f in group) <= 100


def test_giant_function_is_alone_and_neighbours_do_not_follow_its_growth():
    functions = [function(0x10000), function(0x10020), function(0x10040)]
    bodies = {f.addr: ["x" * (150 if i == 1 else 20)] for i, f in enumerate(functions)}
    first = assignment(functions, bodies)
    giant = first[0x10020]
    assert list(first.values()).count(giant) == 1
    bodies[0x10020] = ["x" * 2000]
    assert assignment(functions, bodies) == first


def test_insertion_and_removal_cannot_repack_other_address_buckets():
    functions = [function(0x10000), function(0x10020), function(0x20000)]
    bodies = {f.addr: ["x" * 30] for f in functions}
    before = assignment(functions, bodies)
    added = function(0x10010)
    bodies[added.addr] = ["x" * 60]
    after = assignment(functions + [added], bodies)
    assert after[0x20000] == before[0x20000]
    assert assignment(functions, bodies) == before


def test_body_output_is_independent_of_dispatch_index_and_unrelated_entries(tmp_path):
    functions = [function(0x20000), function(0x30000)]
    bodies = {0x20000: ["void fn_00020000(X86 *c) { CALL_FN(00030000); }"],
              0x30000: ["void fn_00030000(X86 *c) { c->r[0]++; }"]}
    paths = T.emit_body_chunks(tmp_path, functions, bodies, {})
    before = {Path(p).name: Path(p).read_bytes() for p in paths}
    T.emit_entry_chunks(tmp_path, [0x20000, 0x30000], "recomp_")
    entries = (tmp_path / "chunk_entries_00030000.c").read_bytes()
    added = function(0x10000)
    bodies[added.addr] = ["void fn_00010000(X86 *c) { c->r[1]++; }"]
    T.emit_body_chunks(tmp_path, functions + [added], bodies, {})
    T.emit_entry_chunks(tmp_path, [0x10000, 0x20000, 0x30000], "recomp_")
    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())
    assert (tmp_path / "chunk_entries_00030000.c").read_bytes() != entries
    caller = next(data.decode() for data in before.values() if "CALL_FN" in data.decode())
    assert "void entry_00030000(X86 *c);" in caller
    assert "entry_00010000" not in caller
    assert "funcs.h" not in caller
    assert "RECOMP_OVERRIDE_HEADER" not in T.BODY_HEADER
    assert "hooked" not in T.BODY_HEADER


def test_auxiliary_entry_thunks_use_their_own_table(tmp_path):
    paths = T.emit_entry_chunks(tmp_path, [0x10001000], "recomp_fixture_")
    text = Path(paths[0]).read_text()
    assert "recomp_fixture_enter(c, 0u)" in text
    dispatch = T.emit_entry_dispatch("recomp_fixture_")
    assert "recomp_fixture_hooked[i]" in dispatch
    assert "recomp_fixture_base_ptrs[i](c)" in dispatch
    assert "recomp_call(c, recomp_fixture_func_addrs[i])" in dispatch
