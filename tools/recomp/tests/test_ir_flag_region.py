"""Per-site lazy-flag region decisions (ir/flag_region.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ir import flag_region as fr  # noqa: E402

ALL = fr.ALL_FLAGS
NONE = frozenset()


def run(program, succ):
    """program: list of (kind, reads, writes); kind is 'op', 'end' or 'unknown'."""
    def access(i):
        kind, r, w = program[i]
        return None if kind == "unknown" else (frozenset(r), frozenset(w))

    return fr.analyze(0, lambda i: succ.get(i, [i + 1]), access,
                      lambda i: program[i][0] == "end", lambda i: False, len(program))


def test_pass_through_removes():
    assert run([("op", NONE, NONE), ("end", NONE, NONE)], {}) == fr.REMOVE


def test_kill_all_drops():
    assert run([("op", NONE, ALL), ("end", NONE, NONE)], {}) == fr.DROP


def test_partial_kill_settles():
    assert run([("op", NONE, {"zf"}), ("end", NONE, NONE)], {}) == fr.SETTLE


def test_read_first_settles():
    assert run([("op", {"zf"}, NONE), ("end", NONE, NONE)], {}) == fr.SETTLE


def test_unknown_settles():
    assert run([("unknown", NONE, NONE), ("end", NONE, NONE)], {}) == fr.SETTLE


def test_path_without_boundary_is_an_observer():
    # 0 branches to a clean return (1) or a flag write that leaves the body
    # without a CALL/RET (2, e.g. a tail jump).  The write must not vanish
    # from the summary and turn the site into a pass-through.
    program = [("op", NONE, NONE), ("end", NONE, NONE), ("op", NONE, {"zf"})]
    assert run(program, {0: [1, 2], 2: []}) == fr.SETTLE


def test_out_of_body_successor_is_an_observer():
    program = [("op", NONE, NONE), ("end", NONE, NONE)]
    assert run(program, {0: [1, 7]}) == fr.SETTLE
