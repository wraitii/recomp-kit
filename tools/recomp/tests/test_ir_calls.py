"""Direct-call effect model: explicit binding, complete reload, rejection.

These are structural/interpreter regressions over real instruction bytes. The
native call fixtures in ``ir/native_checks.py`` supply the executed full-state
comparison; this file keeps the binding and invalidation rules cheap to check.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter, Insn, Op
from ir.summary import FunctionIR, default_successors
from ir.ssa import SSAError, MEMORY, build
from ir.emit_c import codegen_ir, emit
from ir.publication import plan
from ir.simplify import canonicalize, simplify

LIFTER = Lifter()
TARGET = 0x2000
CALLER = 0x1000


def rel32(call_addr, target):
    value = (target - (call_addr + 5)) & 0xffffffff
    return "e8" + "".join("%02x" % ((value >> (8 * n)) & 0xff) for n in range(4))


def function(*hexes, entry=CALLER):
    """Lift a byte sequence at consecutive addresses with default successors."""
    at, insns = entry, []
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return FunctionIR(entry, insns, default_successors(insns))


def caller(*body):
    """Body bytes with a CALL to TARGET placed after them, then RET."""
    at = CALLER + sum(len(bytes.fromhex(h)) for h in body)
    return function(*body, rel32(at, TARGET), "c3")


def lane(name, n=0):
    # Register-lane keys are 2-tuples: ("register", byte-offset).
    _, off, _ = LIFTER.register(name)
    return ("register", off + n)


def reload_value(s, key):
    """The last SSA value that wrote `key` after the call, or None."""
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc == "CALL_RELOAD" and v.data == key:
                return v
    return None


def test_default_no_calls_rejects_direct_call_with_named_diagnostic():
    with pytest.raises(SSAError, match="direct call target is not bound"):
        build(caller("bb01000000"))
    with pytest.raises(SSAError, match="direct call target is not bound"):
        emit(caller("bb01000000"), "test_fn")


def test_reused_lifter_keeps_identical_codegen_across_bodies_and_policies():
    for fir in (function("40", "8903", "c3"), caller("bb01000000"),
                function("d906", "d80e", "d806", "d91b", "c3")):
        options = dict(call_symbols={TARGET: "callee"}, local_state=True)
        assert emit(fir, "test_fn", lifter=LIFTER, **options) == emit(fir, "test_fn", **options)


def test_indirect_and_unsupported_transfers_still_require_fallback():
    with pytest.raises(SSAError):
        build(function("ffd0", "c3"))  # call eax
    with pytest.raises(SSAError):
        build(function("ffe0", "c3"))  # jmp eax


@pytest.mark.parametrize("bad", ["", "1bad", "has space", "semi;colon", "call.callee"])
def test_binding_must_be_a_plain_c_identifier(bad):
    with pytest.raises(SSAError, match="invalid call symbol"):
        emit(caller("bb01000000"), "test_fn", call_symbols={TARGET: bad})


@pytest.mark.parametrize("bad", ["0x2000", TARGET + 0.5, True, -1, 0x1_0000_0000, None])
def test_binding_keys_are_exact_32_bit_integers(bad):
    with pytest.raises(SSAError, match="invalid direct-call target"):
        emit(caller("bb01000000"), "test_fn", call_symbols={bad: "callee_fn"})


def test_binding_target_map_rejects_non_mapping_input():
    with pytest.raises(SSAError, match="invalid direct-call target map"):
        emit(caller("bb01000000"), "test_fn", call_symbols=[TARGET, "callee_fn"])


def test_multiple_direct_calls_in_one_instruction_are_rejected():
    ret = LIFTER.lift(0x1005, bytes.fromhex("c3"))
    ins = Insn(0x1000, 1, "CALL", [Op("CALL", None, [("ram", TARGET, 4)]),
               Op("CALL", None, [("ram", TARGET, 4)])], 0, False, False, [])
    with pytest.raises(SSAError, match="multiple direct calls"):
        build(FunctionIR(0x1000, [ins, ret], [[1], []]), call_targets={TARGET})


def test_call_without_canonical_fallthrough_is_rejected():
    f = caller("bb01000000")
    call_index = next(i for i, ins in enumerate(f.insns) if ins.mnem.upper() == "CALL")
    for successor in ([0], []):  # retarget away, then remove the fallthrough
        f.succ[call_index] = list(successor)
        with pytest.raises(SSAError, match="canonical fallthrough"):
            build(f, call_targets={TARGET})


def test_bound_call_emits_symbol_snapshot_and_complete_reload():
    body = emit(caller("bb01000000", "b9cc000000"), "test_fn",
                call_symbols={TARGET: "callee_fn"}, _guard_null_checks=False)
    assert "callee_fn(c);" in body
    assert body.count("callee_fn(c);") == 1


def test_call_is_an_ordered_effect_with_a_publication_snapshot():
    s = build(caller("bb01000000"), call_targets={TARGET})
    call = next(v for b in s.blocks.values() for v in b.ops if v.opc == "CALL")
    block = next(b for b in s.blocks.values() if call in b.ops)
    assert block.snapshots[call.id], "call must capture pre-call CPU state"
    assert s.resolve(call.args[0]).opc == "MEMORY"


def test_every_tracked_register_flag_and_eip_is_reloaded_after_the_call():
    s = build(caller("bb01000000", "31f6", "89d8"), call_targets={TARGET})
    for key in s.inputs:
        if key != MEMORY:
            assert reload_value(s, key) is not None, key


def test_tracked_register_written_before_call_is_reloaded_not_stale():
    s = build(caller("bb01000000"), call_targets={TARGET})
    reloads = [v for b in s.blocks.values() for v in b.ops
               if v.opc == "CALL_RELOAD" and v.data == lane("EBX")]
    assert reloads
    assert set(reloads[0].args) == {next(v for b in s.blocks.values() for v in b.ops
                                          if v.opc == "CALL")}


def test_loop_backedge_uses_post_call_reload_not_pre_call_value():
    # bb 01000000   mov ebx,1
    # 83 c3 01      add ebx,1
    # e8 ..         call target
    # 83 fb 03      cmp ebx,3
    # 72 ..         jb entry
    # c3            ret
    call = CALLER + 5 + 3
    jb_target = CALLER
    jb_addr = call + 5 + 3
    rel = (jb_target - (jb_addr + 2)) & 0xff
    f = function("bb01000000", "83c301", rel32(call, TARGET), "83fb03",
                 "72%02x" % rel, "c3")
    s = build(f, call_targets={TARGET})
    assert sum(v.opc == "CALL" for b in s.blocks.values() for v in b.ops) == 1
    reloads = {v.id for b in s.blocks.values() for v in b.ops if v.opc == "CALL_RELOAD"}
    entry = s.blocks[s.entry]
    phi = next(phi for phi in entry.phis if phi.data[0] == lane("EBX"))

    def depends(value, seen=()):
        value = s.resolve(value)
        if value.id in seen:
            return False
        if value.id in reloads:
            return True
        return any(depends(arg, seen + (value.id,)) for arg in value.args)

    assert any(depends(arg) for arg in phi.args), "loop phi ignored the post-call reload"


def test_publication_facts_do_not_survive_a_call():
    # mov ebx,1; mov ecx,0xcc; call; mov eax,[ecx]; ret
    call = CALLER + 5 + 5
    f = function("bb01000000", "b9cc000000", rel32(call, TARGET), "8b01", "c3")
    s = build(codegen_ir(f, LIFTER), call_targets={TARGET})
    canonicalize(s)
    pubs = plan(s, f.succ, [[lane("EBX")], [lane("ECX")]])
    call_value = next(v for b in s.blocks.values() for v in b.ops if v.opc == "CALL")
    load = next(v for b in s.blocks.values() if b.insn.mnem.upper() == "MOV"
                for v in b.ops if v.opc == "LOAD")
    assert call_value.id in pubs
    # Facts are cleared at the call, so the later load republishes both fields.
    assert {lane("EBX"), lane("ECX")} <= set(pubs[load.id])


def test_call_keeps_effect_order_and_does_not_drop_return_address_store():
    s = build(caller("bb01000000"), call_targets={TARGET})
    block = next(b for b in s.blocks.values()
                 if any(v.opc == "CALL" for v in b.ops))
    opcodes = [v.opc for v in block.ops]
    assert opcodes.index("STORE") < opcodes.index("CALL")


def test_resumable_continuation_check_is_emitted_only_when_requested():
    plain = emit(caller("bb01000000"), "test_fn", call_symbols={TARGET: "callee_fn"})
    resumable = emit(caller("bb01000000"), "test_fn", call_symbols={TARGET: "callee_fn"},
                     resumable_stacks=True)
    assert "if (c->eip !=" not in plain
    assert "if (c->eip !=" in resumable


def absolute_effect(s, opcode, address=0x8dd4dc):
    return [v for b in s.blocks.values() for v in b.ops
            if v.opc == opcode and v.args[1].opc == "CONST" and v.args[1].data == address]


def test_absolute_load_is_a_guest_memory_effect_not_a_target_constant():
    # mov eax,[0x8dd4dc]; test eax,eax; ret -- SLEIGH folds the direct read into
    # a data-position ram operand, which codegen must turn into a LOAD.
    s = build(codegen_ir(function("a1dcd48d00", "85c0", "c3"), LIFTER))
    assert absolute_effect(s, "LOAD")
    assert not any(v.opc == "TARGET" for b in s.blocks.values() for v in b.ops)


def test_absolute_store_is_a_guest_memory_effect():
    # mov [0x8dd4dc],eax; ret -- SLEIGH folds the direct write into a ram output.
    s = build(codegen_ir(function("a3dcd48d00", "c3"), LIFTER))
    assert absolute_effect(s, "STORE")


@pytest.mark.parametrize("encoding", [
    "0305dcd48d00",      # ADD EAX,[abs]
    "1305dcd48d00",      # ADC EAX,[abs]
    "1b05dcd48d00",      # SBB EAX,[abs]
    "2305dcd48d00",      # AND EAX,[abs]
    "0b05dcd48d00",      # OR  EAX,[abs]
    "3305dcd48d00",      # XOR EAX,[abs]
    "8505dcd48d00",      # TEST [abs],EAX
    "3905dcd48d00",      # CMP [abs],EAX
    "0fb605dcd48d00",    # MOVZX EAX,byte [abs]
    "0fbe05dcd48d00",    # MOVSX EAX,byte [abs]
    "0faf15dcd48d00",    # IMUL EDX,[abs]
])
def test_absolute_source_forms_capture_exactly_one_read(encoding):
    # SLEIGH repeats the direct `ram` operand across result and flag p-code; it
    # must become one captured LOAD shared by every consumer, never a TARGET.
    s = build(codegen_ir(function(encoding, "c3"), LIFTER))
    assert len(absolute_effect(s, "LOAD")) == 1
    assert not any(v.opc == "TARGET" for b in s.blocks.values() for v in b.ops)


@pytest.mark.parametrize("encoding", [
    "0105dcd48d00",      # ADD [abs],EAX
    "2905dcd48d00",      # SUB [abs],EAX
    "2105dcd48d00",      # AND [abs],EAX
    "0905dcd48d00",      # OR  [abs],EAX
    "3105dcd48d00",      # XOR [abs],EAX
    "ff05dcd48d00",      # INC [abs]
    "ff0ddcd48d00",      # DEC [abs]
    "8705dcd48d00",      # XCHG [abs],EAX
])
def test_direct_ram_destination_rmw_is_rejected_not_approximated(encoding):
    # A destination RMW reads memory, stores, then re-reads it for flags (or is
    # locked XCHG). SLEIGH's repeated `ram` operand must not be approximated.
    with pytest.raises(SSAError, match="read-modify-write"):
        codegen_ir(function(encoding, "c3"), LIFTER)


def test_absolute_not_read_modify_write_lowers_to_ordered_load_compute_store():
    # NOT [abs] is a single-op RMW with no flag definitions: one captured load,
    # one store, in guest order.
    s = build(codegen_ir(function("f715dcd48d00", "c3"), LIFTER))
    loads, stores = absolute_effect(s, "LOAD"), absolute_effect(s, "STORE")
    assert len(loads) == 1 and len(stores) == 1
    order = [v.opc for b in s.blocks.values() for v in b.ops if v in (loads[0], stores[0])]
    assert order == ["LOAD", "STORE"]


def test_ssa_rejects_a_raw_data_ram_operand():
    with pytest.raises(SSAError, match="requires normalization"):
        build(function("a1dcd48d00", "c3"))


def test_simplify_keeps_call_snapshot_and_reloads_live():
    f = caller("bb01000000", "89d8")
    s = build(codegen_ir(f, LIFTER), call_targets={TARGET})
    call = next(v for b in s.blocks.values() for v in b.ops if v.opc == "CALL")
    used = next(v for b in s.blocks.values() for v in b.ops
                if v.opc == "CALL_RELOAD" and v.data == lane("EBX"))
    simplify(s)
    remaining = {v.id for b in s.blocks.values() for v in b.ops}
    assert call.id in remaining
    assert used.id in remaining
