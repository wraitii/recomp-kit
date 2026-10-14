import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ir.body import BodyRequest, EmissionPolicy
from ir.call_contracts import ARITH_FLAGS, Contract, FIELDS
from ir.cfg import FunctionIR
from ir.emit_c import emit_prepared
from ir.invocation import EntryBinding
from ir.lift import Lifter
from ir.normalize import codegen_ir
from ir.prepare import live_flag_reload_calls, prepare


class LazyFlagTests(unittest.TestCase):
    def body(self, encoded, exits=None, tails=None, **options):
        lifter = Lifter()
        raw = bytes.fromhex(encoded)
        instructions, position = [], 0
        for size in lifter.lengths(0x1000, raw):
            instructions.append(lifter.lift(0x1000 + position, raw[position:position + size]))
            position += size
        successors = [[i + 1] if i + 1 < len(instructions) else [] for i in range(len(instructions))]
        tails = tails or {}
        request = BodyRequest('fixture', policy=EmissionPolicy(lazy_flags=True, indirect_call_symbol='indirect'),
                              tail_symbols=tails, **options)
        fir = FunctionIR(0x1000, instructions, successors, {}, exits=exits)
        normalized = codegen_ir(fir, lifter, tails) if tails else None
        return prepare(fir, request, lifter=lifter, normalized=normalized)

    def test_imul_does_not_read_incoming_carry(self):
        body = self.body('e8 fb 0f 00 00 69 c9 fd 43 03 00 83 c1 01 c3',
                         call_symbols={0x2000: 'callee'})
        self.assertEqual(body.cc_calls[0], 'drop')

    def test_sbb_still_reads_incoming_carry(self):
        body = self.body('e8 fb 0f 00 00 19 c0 c3', call_symbols={0x2000: 'callee'})
        self.assertEqual(body.cc_calls[0], ('x86_cc_settle_or_drop', frozenset(('cf',))))

    def test_inc_entry_preserves_only_carry(self):
        body = self.body('40 c3')
        self.assertEqual(body.cc_entry, ('x86_cc_settle_or_drop', frozenset(('cf',))))

    def test_resolved_tail_forwards_pending_descriptor(self):
        # jmp 0x2000: a resolved tail is a normal boundary, so the descriptor
        # passes through to the target's entry instead of settling here.
        body = self.body('e9 fb 0f 00 00', exits={0: [0x2000]}, tails={0x2000: 'target'})
        self.assertEqual(body.cc_entry, 'remove')

    def test_tail_after_full_overwrite_drops(self):
        # xor eax, eax; jmp 0x2000: every flag is overwritten before the tail.
        body = self.body('31 c0 e9 f9 0f 00 00', exits={1: [0x2000]}, tails={0x2000: 'target'})
        self.assertEqual(body.cc_entry, 'drop')

    def test_tail_still_keeps_incoming_read(self):
        # adc eax, eax; jmp 0x2000: the incoming carry is a real read.
        body = self.body('11 c0 e9 f9 0f 00 00', exits={1: [0x2000]}, tails={0x2000: 'target'})
        self.assertEqual(body.cc_entry, ('x86_cc_settle_or_drop', frozenset(('cf',))))

    def test_inc_zero_reader_only_keeps_result_predicate(self):
        body = self.body('40 0f 94 c1 c3')
        operations = {v.opc for v in body.ssa.values if v.id in body.live and body.ssa.resolve(v) is v}
        self.assertIn('INT_EQUAL', operations)
        self.assertNotIn('POPCOUNT', operations)
        self.assertNotIn('INT_SCARRY', operations)

    def test_indirect_call_return_forwards_flags(self):
        body = self.body('ff d0 c3')
        self.assertEqual(body.cc_calls[0], 'remove')
        self.assertFalse(live_flag_reload_calls(body.ssa, body.live,
                                               {key[1]: name.lower() for name, keys in body.state_groups
                                                if name in ARITH_FLAGS for key in keys}))
        returns = [v for block in body.ssa.blocks.values() for v in block.ops if v.opc == 'RETURN']
        self.assertTrue(returns)
        for event in returns:
            self.assertFalse(set(body.transfers.publications[event.id].required) & body.flag_keys)

    def test_specialized_call_retains_flag_outputs(self):
        flags = frozenset(ARITH_FLAGS)
        body = self.body('e8 fb 0f 00 00 c3',
                         call_symbols={0x2000: EntryBinding(0x2000, True)},
                         checked_calls={0x2000}, contract_calls={0x2000},
                         call_contracts={0x2000: Contract(frozenset(), flags, flags)})
        calls = list(body.transfers.calls.values())
        self.assertEqual(len(calls), 1)
        self.assertTrue(flags <= calls[0].required_outputs)

    def test_preserved_flag_hook_fallback_settles_inside_guard(self):
        body = self.body('e8 fb 0f 00 00 31 c0 c3', call_symbols={0x2000: 'callee'},
                         call_contracts={0x2000: Contract(frozenset(), frozenset(), frozenset(),
                                                        frozenset(FIELDS))})
        self.assertEqual(body.cc_entry, 'remove')
        self.assertFalse(any(v.id in body.live and v.opc == 'INPUT' and v.data in body.flag_keys
                             for v in body.ssa.values))
        self.assertTrue(next(iter(body.transfers.calls.values())).settle_guard)
        body.finalize({})
        source = emit_prepared(body)
        self.assertLess(source.index('if (RECOMP_UNLIKELY(recomp_hooks_ever))'),
                        source.index('x86_cc_settle(c);'))


if __name__ == '__main__':
    unittest.main()
