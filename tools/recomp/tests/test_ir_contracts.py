"""Conservative region contracts: lattice, CFG, call composition and byte facts."""
from dataclasses import FrozenInstanceError
from itertools import product
from pathlib import Path
import json
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.cfg import FunctionIR, default_successors
from ir.contracts.dataflow import backward_demands, program_effects, reachable, region_effects
from ir.contracts.lifted import inventory
from ir.contracts.model import Boundary, Effects, FunctionFacts, Mask, Node, Observer
from ir.contracts.report import analyze
from ir.lift import Lifter


def finite(*atoms):
    return Mask(frozenset(atoms))


def test_mask_complement_algebra_matches_concrete_sets():
    universe = {'a', 'b', 'c', 'd'}
    masks = [Mask(atoms, all_) for atoms in (set(), {'a'}, {'b', 'c'})
             for all_ in (False, True)]

    def concrete(mask):
        return universe - mask.atoms if mask.all else mask.atoms

    for a, b in product(masks, repeat=2):
        assert concrete(a.union(b)) == concrete(a) | concrete(b)
        assert a.union(b) == b.union(a)
        assert a.union(a) == a
        for c in masks:
            assert a.union(b).union(c) == a.union(b.union(c))
    for mask in masks:
        assert concrete(mask.without({'a', 'd'})) == concrete(mask) - {'a', 'd'}
    assert Mask() != Mask.unknown()


def test_immutable_facts_copy_mutable_inputs():
    source = {'eax:0'}
    mask = Mask(source)
    node = Node(0x1000, defs=source)
    source.add('eax:1')
    assert mask.atoms == node.defs == {'eax:0'}
    with pytest.raises(FrozenInstanceError):
        node.site = 0x2000
    with pytest.raises(ValueError, match='32-bit guest'):
        Boundary(0x1000, 'call', 1 << 32)


def test_join_kills_only_definitely_written_lanes():
    # One arm defines EAX low byte, another preserves it. Both preserve AH.
    nodes = [Node(0x1000), Node(0x1001, defs={'eax:0'}), Node(0x1002), Node(0x1003)]
    before, _ = backward_demands(nodes, [[1, 2], [3], [3], []], (0,),
                                 {3: finite('eax:0', 'eax:1')})
    assert before[1] == finite('eax:1')
    assert before[0] == finite('eax:0', 'eax:1')


def test_loop_demand_reaches_fixed_point_and_unreachable_effects_are_excluded():
    nodes = [Node(0x1000), Node(0x1001, Effects(cpu_reads=finite('zf'))), Node(0x1002),
             Node(0x1003, Effects.unknown('unreachable'))]
    succ = [[1], [0, 2], [], []]
    before, _ = backward_demands(nodes, succ, (0,), {2: finite('eax')})
    assert before[0] == before[1] == finite('eax', 'zf')
    assert 3 not in before
    assert not region_effects(nodes, succ, (0,)).unresolved


def test_alternate_entries_are_included():
    nodes = [Node(0x1000), Node(0x1001, Effects(cpu_reads=finite('ecx')))]
    before, _ = backward_demands(nodes, [[], []], (0, 1), {0: Mask(), 1: Mask()})
    assert before[1] == finite('ecx')


@pytest.mark.parametrize('succ,entries,exits', [
    ([[1]], (0,), {}), ([[]], (1,), {}), ([[]], (), {}),
    ([[]], (0,), {}), ([[], []], (0,), {0: Mask(), 1: Mask()}),
])
def test_invalid_cfg_or_missing_exit_is_rejected(succ, entries, exits):
    with pytest.raises(ValueError):
        backward_demands([Node(0x1000 + i) for i in range(len(succ))],
                         succ, entries, exits)


def test_observer_read_cannot_be_killed_by_same_instruction_definition():
    observer = Observer(Effects(cpu_reads=finite('zf'), cpu_writes=finite('eax'),
                                events={'callback', 'yield'}), evidence=('reviewed test observer',))
    nodes = [Node(0x1000, defs={'zf'}, boundaries=(Boundary(0x1000, 'call', observer=observer),))]
    before, _ = backward_demands(nodes, [[]], (0,), {0: finite('eax')})
    # May-clobber EAX is not a proof it overwrites the old value on every path.
    assert before[0] == finite('eax', 'zf')
    assert region_effects(nodes, [[]], (0,)).events == {'callback', 'yield'}


def test_unreviewed_observer_demands_and_clobbers_all_state_and_memory():
    boundary = Boundary(0x1000, 'indirect')
    effect = boundary.observer.effects
    assert effect.cpu_reads == effect.cpu_writes == Mask.unknown()
    assert effect.memory_reads == effect.memory_writes == Mask.unknown()
    assert {'callback', 'yield', 'fault', 'nonreturn'} <= effect.events
    before, _ = backward_demands([Node(0x1000, boundaries=(boundary,))], [[]], (0,), {0: Mask()})
    assert before[0] == Mask.unknown()


def test_recursive_call_effects_converge_independent_of_input_order():
    a = FunctionFacts(0x1000, Effects(memory_reads=finite('object')), {0x2000})
    b = FunctionFacts(0x2000, Effects(cpu_writes=finite('zf')), {0x3000})
    c = FunctionFacts(0x3000, Effects(events={'yield'}), {0x1000})
    result = program_effects([a, b, c])
    assert result == program_effects([c, b, a])
    for effects in result.values():
        assert effects.memory_reads == finite('object')
        assert effects.cpu_writes == finite('zf')
        assert effects.events == {'yield', 'nonreturn'}


def test_missing_callee_is_top_and_propagates_to_ancestors():
    result = program_effects([FunctionFacts(0x1000, Effects(), {0x2000}),
                              FunctionFacts(0x2000, Effects(), {0x3000})])
    assert result[0x1000].memory_writes == Mask.unknown()
    assert 'unresolved callee 00003000' in result[0x1000].unresolved


def test_deep_call_chain_does_not_depend_on_python_recursion_or_round_limit():
    functions = [FunctionFacts(i, Effects(), {i + 1}) for i in range(1000)]
    functions.append(FunctionFacts(1000, Effects(cpu_reads=finite('zf'))))
    assert program_effects(functions)[0].cpu_reads == finite('zf')


def test_cycles_may_diverge_without_claiming_they_do():
    nodes = [Node(0x1000), Node(0x1001)]
    assert 'nonreturn' in region_effects(nodes, [[0, 1], []], (0,)).events
    assert 'nonreturn' not in region_effects(nodes, [[1], []], (0,)).events
    recursive = FunctionFacts(0x1000, Effects(), {0x1000})
    assert 'nonreturn' in program_effects([recursive])[0x1000].events


def test_indirect_tail_transfer_keeps_a_complete_boundary():
    node = inventory(function(0x1000, 'ffe0'))[0]  # JMP EAX
    assert any(b.kind == 'external-transfer' and b.target is None for b in node.boundaries)


LIFTER = Lifter()


def function(address, *hexes):
    insns, at = [], address
    for text in hexes:
        raw = bytes.fromhex(text)
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return FunctionIR(address, insns, default_successors(insns))


def test_byte_backed_flag_dependency_is_explicit():
    fir = function(0x1000, '7401', '90', 'c3')  # JZ, NOP, RET
    nodes = inventory(fir)
    assert 'register:206' in nodes[0].effects.cpu_reads.atoms
    assert not nodes[0].effects.cpu_reads.all


def test_raw_arithmetic_and_x87_do_not_claim_complete_semantic_facts():
    add = inventory(function(0x1000, '01c8', 'c3'))[0]
    assert add.effects.cpu_writes.all and not add.defs
    assert 'raw arithmetic flags require corrected semantics' in add.effects.unresolved
    fld = inventory(function(0x1000, 'd901', 'c3'))[0]
    assert fld.effects.cpu_reads.all and fld.effects.memory_writes.all
    assert any(b.kind == 'fault' for b in fld.boundaries)


def test_load_and_indirect_call_retain_fault_and_callback_boundaries():
    nodes = inventory(function(0x1000, '8b01', 'ffd0', 'c3'))
    assert nodes[0].effects.memory_reads.all
    assert any(b.kind == 'fault' for b in nodes[0].boundaries)
    call = next(b for b in nodes[1].boundaries if b.kind == 'call')
    assert call.target is None and call.observer.effects.cpu_reads.all


def test_report_does_not_internalize_a_known_callee_or_claim_an_optimized_abi():
    caller = function(0x1000, 'e8fb0f0000', 'c3')
    callee = function(0x2000, 'b801000000', 'c3')
    report = analyze([caller, callee])
    assert report == analyze([callee, caller])
    assert 'not optimization authorization' in report['purpose']
    row = report['functions']['00001000']
    call = next(b for n in row['nodes'] for b in n['boundaries'] if b['kind'] == 'call')
    assert call['target'] == '00002000' and call['effects']['cpu_reads']['all']
    assert row['entry_demand']['all']
    assert json.loads(json.dumps(report)) == report


def test_cfg_compatibility_exports_are_identical():
    from ir import census, cfg, summary
    assert summary.FunctionIR is cfg.FunctionIR
    assert summary.default_successors is cfg.default_successors
    assert summary.call_graph is cfg.call_graph
    assert census.function_ir is cfg.function_ir


@pytest.fixture
def pinned_inputs(tmp_path, monkeypatch):
    import analyze_contracts as driver
    from test_translate_driver import synthetic_image
    entry, raw = 0x00401000, bytes.fromhex('b801000000c3')
    binary = tmp_path / 'original.exe'
    binary.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    cfg = {'developer_exe_path': binary, 'code_map_path': tmp_path / 'map',
           'game': {'sha256': digest, 'image_base': 0x00400000}}
    metadata = {'executable_sha256': digest, 'image_base': '00400000'}
    manifest = tmp_path / 'manifest.json'
    row = {'address': '%08x' % entry, 'instructions_sha256': digest}
    manifest.write_text(json.dumps({'contract': 'mapped-native-corpus-v1', 'functions': [row]}))
    image = synthetic_image({entry: raw})
    monkeypatch.setattr(driver.game_config, 'load', lambda _: cfg)
    monkeypatch.setattr(driver, 'read_map', lambda _: (metadata, {entry: ('leaf', len(raw), [(entry, '51')])}))
    monkeypatch.setattr(driver.T, 'configure', lambda _: None)
    monkeypatch.setattr(driver.T, 'Image', lambda _: image)
    monkeypatch.setattr(driver.T, 'INSTRUCTION_PATCHES', {})
    monkeypatch.setattr(driver.T, 'OPERAND_REDIRECTS', {})
    return driver, cfg, metadata, manifest, row


def test_driver_lifts_verified_original_bytes_with_production_cfg(pinned_inputs):
    driver, cfg, _, manifest, _ = pinned_inputs
    _, digest, functions, provenance = driver.load_inputs(manifest.parent, manifest)
    assert digest == cfg['game']['sha256']
    assert functions[0].insns[0].raw == bytes.fromhex('b801000000')
    assert functions[0].succ == [[1], []]
    assert provenance['00401000']['instructions_sha256'] == digest


@pytest.mark.parametrize('failure', ['image', 'map', 'instructions', 'rewrite'])
def test_driver_rejects_unpinned_or_rewritten_inputs(pinned_inputs, failure, monkeypatch):
    driver, cfg, metadata, manifest, row = pinned_inputs
    if failure == 'image':
        cfg['game']['sha256'] = '0' * 64
    elif failure == 'map':
        metadata['image_base'] = '00500000'
    elif failure == 'instructions':
        row['instructions_sha256'] = '0' * 64
        manifest.write_text(json.dumps({'contract': 'mapped-native-corpus-v1', 'functions': [row]}))
    else:
        monkeypatch.setattr(driver.T, 'INSTRUCTION_PATCHES', {0x00401000: 'NOP'})
    with pytest.raises(ValueError):
        driver.load_inputs(manifest.parent, manifest)
