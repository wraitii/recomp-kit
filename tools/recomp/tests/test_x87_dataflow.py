"""Decoded TOP/width joins and publication boundaries for the opt-in stage."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
from x87_dataflow import Effect, effect, solve, width_transfer


def translate(lines, enabled=True, entries=(), gap=None):
    insns = T.parse_listing_text('\n'.join(f'{0x100000+i:08x}  {s}' for i, s in enumerate(lines)))
    fn = T.Function(0x100000, 'synthetic', len(insns), insns)
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=True, cpu_locals=False, x87_locals=True, x87_dataflow=enabled))
    tr.prepare(fn)
    if gap is not None:
        fn.contiguous[gap] = False
        fn.fallthrough[gap] = fn.insns[gap + 1].addr
    return '\n'.join(tr.translate(fn, entries)), tr


CHAIN = ['FLD float ptr [ESI]', 'FDIV float ptr [EDI]', 'FLD ST0',
         'FMUL float ptr [ESI]', 'FSTP float ptr [EBX]', 'FSTP ST0']


def test_division_chain_materializes_once_and_keeps_double_status_helper():
    body, tr = translate([*CHAIN, 'RET'])
    assert tr.stats['_x87_dataflow_regions'] == 1
    assert 'fdivz(&x87_env_,' in body
    assert 'fpush(c,' not in body and 'fpush_st(c,' not in body and 'fdrop(c);' not in body
    assert body.index('c->st[') < body.index('00100006 RET')
    old, _ = translate([*CHAIN, 'RET'], enabled=False)
    assert 'decoded x87 dataflow' not in old and 'fdivz(c,' in old


def test_incoming_register_copy_keeps_all_metadata_and_exchange():
    body, _ = translate(['FLD ST0', 'FXCH ST2', 'FST ST3', 'FSTP ST0', 'RET'])
    assert 'decoded x87 dataflow' in body
    assert 'c->st_bits[' in body and 'c->st_exact[' in body and 'ftag_of(c,' in body
    assert 'swap_bits_' in body and 'swap_tag_' in body
    assert 'fpush_st(c,' not in body and 'fxch(c,' not in body


def test_listing_gap_and_bounded_analysis_retain_fallbacks():
    body, _ = translate([*CHAIN, 'RET'], gap=1)
    # The gap instruction must publish/execute before any following local
    # region rather than being swallowed into a cross-gap region.
    assert '/* 00100001 FDIV float ptr [EDI] */' in body
    assert 'fdivz(c,' in body
    body, _ = translate([*CHAIN, *(['NOP'] * 512), 'RET'])
    assert 'decoded x87 dataflow' not in body


@pytest.mark.parametrize('boundary', ['CALL 0x00200000', 'FLDCW word ptr [ESI]',
                                     'FILD qword ptr [ESI]', 'FXAM', 'FNSTENV [ESI]'])
def test_opaque_observers_receive_materialized_state(boundary):
    body, _ = translate([*CHAIN, boundary, 'RET'])
    assert body.index('c->st[') < body.index('00100006 ' + boundary)


def test_external_entry_and_inconsistent_top_do_not_cross():
    body, _ = translate([*CHAIN, 'RET'], entries=(0x100001,))
    assert 'goto L_x87_00100001' not in body
    body, _ = translate(['FLD float ptr [ESI]', 'FDIV float ptr [EDI]',
                         'TEST EAX,EAX', 'JZ 0x00100005', 'FLD ST0',
                         'FMUL float ptr [EDI]', 'FSTP ST0', 'RET'])
    assert 'decoded x87 dataflow' not in body


def test_decoded_effects_and_wrapping_simultaneous_width_transfers():
    ins = T.parse_listing_text('00100000  FDIVR ST2,ST0')[0]
    assert effect(ins, T.parse_operand) == Effect('arithmetic', reads=(2, 0), writes=(2,))
    assert width_transfer(Effect('copy', reads=(7,), writes=(-1,), delta=-1),
                          0, frozenset({7})) == frozenset({7})
    assert width_transfer(Effect('swap', reads=(0, 1), writes=(0, 1)),
                          7, frozenset({7})) == frozenset({0})


def test_width_meet_does_not_assume_pc00_means_incoming_binary32():
    effects = {0: Effect('branch'), 1: Effect('push', writes=(-1,), delta=-1, narrow=True),
               2: Effect('push', writes=(-1,), delta=-1), 3: Effect('arithmetic', reads=(0,), writes=(0,))}
    plan = solve(0, 4, effects, [[1, 2], [3], [3], []])
    assert plan.widths[3] == frozenset()
    effects[2] = Effect('push', writes=(-1,), delta=-1, narrow=True)
    assert solve(0, 4, effects, [[1, 2], [3], [3], []]).widths[3] == frozenset({7})


def test_loop_widths_include_the_external_entry():
    effects = {0: Effect('arithmetic', reads=(0,), writes=(0,)), 1: Effect('branch')}
    plan = solve(0, 2, effects, [[1], [0]])
    assert plan.widths[0] == frozenset()
    assert plan.widths[1] == frozenset({0})


def test_publication_uses_writes_reaching_each_exit_and_loop_backedge():
    effects = {0: Effect('branch'), 1: Effect('push', writes=(-1,), delta=-1, narrow=True),
               2: Effect('store', reads=(0,), delta=1), 3: Effect('branch')}
    plan = solve(0, 4, effects, [[1, 3], [2], [3], []])
    assert plan.modified[0] == frozenset()
    assert plan.modified[1] == frozenset()
    assert plan.modified[2] == plan.modified[3] == frozenset({7})
    # An early exit from the head of a later loop iteration must publish the
    # writes made by previous iterations, even before this iteration writes.
    plan = solve(0, 4, effects, [[1, 3], [2], [0], []])
    assert plan.modified[0] == plan.modified[3] == frozenset({7})


def test_early_dispatch_exit_does_not_publish_unwritten_slots():
    body, _ = translate(['TEST EAX,EAX', 'JZ 0x00100008', *CHAIN, 'RET'])
    branch = next(line for line in body.splitlines() if '00100001 JZ' in line)
    assert 'c->st[' not in branch and 'c->st_bits[' not in branch
    assert 'c->fpu_sw = x87_env_.fpu_sw;' in branch


def test_config_is_boolean_and_requires_existing_local_mode(tmp_path):
    cfg = T.game_config.load(Path(__file__).resolve().parents[3] / 'games/stub')
    text = (cfg['dir'] / 'game.toml').read_text()
    for setting, message in [('"yes"', 'must be a boolean'), ('true', 'requires x87_locals')]:
        (tmp_path / 'game.toml').write_text(text.replace('[translate]', '[translate]\nx87_dataflow = ' + setting))
        with pytest.raises(ValueError, match=message):
            T.game_config.load(tmp_path)


def forward(lines, entries=(), gap=None):
    insns = T.parse_listing_text('\n'.join(f'{0x100000+i:08x}  {s}' for i, s in enumerate(lines)))
    fn = T.Function(0x100000, 'forward', len(insns), insns)
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=True, cpu_locals=False, x87_locals=True, x87_dataflow=True,
        x87_stack_forwarding=True))
    tr.prepare(fn)
    if gap is not None:
        fn.contiguous[gap] = False
        fn.fallthrough[gap] = fn.insns[gap + 1].addr
    return '\n'.join(tr.translate(fn, entries))


SPILL = ['FLD double ptr [ESI]', 'FST float ptr [ESP + 12]']
RELOAD = ['FLD float ptr [ESP + 12]', 'FMUL float ptr [EDI]',
          'FSTP float ptr [EBX]', 'FSTP ST0', 'RET']


def test_stack_forwarding_keeps_rounding_store_and_reloads_rounded_value():
    body = forward([*SPILL, 'FMUL float ptr [EDI]', *RELOAD])
    assert 'stack_float_1_ = fto_float(&x87_env_,' in body
    assert 'wrf32((c->r[4] + 0xcu), (stack_float_1_' in body
    assert '(double)stack_float_1_' in body
    assert 'rdf32((c->r[4] + 0xcu))' not in body


@pytest.mark.parametrize('barrier', ['MOV byte ptr [EDI],AL', 'FST float ptr [EDI]',
    'ADD ESP,4', 'MOV SP,AX', 'PUSH EAX', 'POP EDX', 'CALL 0x00200000',
    'FLDCW word ptr [EDI]', 'FNSTSW word ptr [EDI]'])
def test_stack_forwarding_refuses_alias_writes_stack_mutations_and_observers(barrier):
    assert 'stack_float_' not in forward([*SPILL, barrier, *RELOAD])


def test_stack_forwarding_refuses_external_entries_gaps_and_proof_budget():
    lines = [*SPILL, *RELOAD]
    assert 'stack_float_' not in forward(lines, entries=(0x100002,))
    assert 'stack_float_' not in forward(lines, gap=1)
    assert 'stack_float_' not in forward([*SPILL, *(['NOP'] * 33), *RELOAD])
    # A branch into the reload skips the spill; no cross-join proof.
    assert 'stack_float_' not in forward(['TEST EAX,EAX', 'JZ 0x00100004', *SPILL, *RELOAD])


def test_stack_forwarding_config_requires_dataflow(tmp_path):
    text = (Path(__file__).resolve().parents[3] / 'games/stub/game.toml').read_text()
    for value, message in [('"yes"', 'must be a boolean'), ('true', 'requires x87_dataflow')]:
        (tmp_path / 'game.toml').write_text(text.replace('[translate]', '[translate]\nx87_stack_forwarding = ' + value))
        with pytest.raises(ValueError, match=message):
            T.game_config.load(tmp_path)
