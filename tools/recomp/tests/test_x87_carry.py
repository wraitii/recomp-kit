import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ir import x87_carry
from ir.ssa import SSAError
from ir.x87_scalar import X87Scalar
from ir.x87_transition import Type, literal


def _op(opc, mnem=None):
    return SimpleNamespace(id=1, opc=opc,
                           data={"mnem": mnem, "operands": ()} if mnem else None,
                           args=[], size=0)


def _blocks(branch_at_one=False):
    first = [_op("X87_REG", "FLD1"), _op("X87_REG", "FLD1"),
             _op("X87_REG", "FADDP")]
    middle = [_op("BRANCH")] if branch_at_one else []
    return SimpleNamespace(entry=0, alternates=[], blocks={
        0: SimpleNamespace(ops=first),
        1: SimpleNamespace(ops=middle),
        2: SimpleNamespace(ops=[]),
    })


def _scalar_factory():
    return X87Scalar(convention=True, lazy_nan=True)


class X87CarryTests(unittest.TestCase):
    def test_pending_nan_crosses_linear_edge_and_folds_at_cut(self):
        s = _blocks(branch_at_one=True)
        _, exits = x87_carry.analyze(
            s, SimpleNamespace(succ={0: [1], 1: [2], 2: []}),
            _scalar_factory, set())

        carried = next(slot for _, slot in exits[0].slots if slot.pending)
        self.assertFalse(carried.quiet)
        cut = next(slot for _, slot in exits[1].slots if slot.quiet)
        self.assertFalse(cut.pending)

    def test_fxch_transports_quiet_and_pending_with_value(self):
        scalar = _scalar_factory()
        scalar._assign(0, value=literal(1.0, Type.F64), quiet=True)
        scalar._assign(1, value=literal(2.0, Type.F64), pending=True)

        scalar.apply({"mnem": "FXCH", "operands": (("st", 1),)})

        self.assertFalse(scalar._slot(0)[1].quiet)
        self.assertTrue(scalar._slot(0)[1].pending)
        self.assertTrue(scalar._slot(1)[1].quiet)
        self.assertFalse(scalar._slot(1)[1].pending)

    def test_deferred_nan_at_multi_predecessor_join_fails_closed(self):
        s = _blocks()
        transfer = x87_carry._transfer

        def leave_pending(block, entry, scalar_factory, binary32, fold_pending):
            return transfer(block, entry, scalar_factory, binary32, False)

        with patch.object(x87_carry, "_transfer", leave_pending):
            with self.assertRaisesRegex(SSAError, "deferred x87 NaN reached a carried join"):
                x87_carry.analyze(
                    s, SimpleNamespace(succ={0: [1, 2], 1: [2], 2: []}),
                    _scalar_factory, set())


if __name__ == "__main__":
    unittest.main()
