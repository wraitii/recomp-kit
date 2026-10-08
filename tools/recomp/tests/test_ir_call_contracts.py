"""Cross-function field contracts: summary rules and emission integration.

`call_contracts.summarize` is a byte-backed may-read/must-kill analysis over the
lifted CFG.  `emit(..., call_contracts=...)` drops dead fields at a direct call.
The executed full-state comparison and the poison build live in
``ir/native_checks.py``; these are structural checks.
"""
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir import call_contracts as CC
from ir.lift import Lifter
from ir.cfg import FunctionIR, default_successors
from ir.emit_c import emit

LIFTER = Lifter()
ENTRY = 0x1000
CALLEE = 0x2000


def function_at(base, *hexes):
    at, insns = base, []
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return FunctionIR(base, insns, default_successors(insns))


def rel_call(addr, target):
    rel = (target - (addr + 5)) & 0xffffffff
    return "e8" + "".join("%02x" % ((rel >> (8 * n)) & 0xff) for n in range(4))


def summarize(*hexes, base=ENTRY, lookup=lambda target: None):
    return CC.summarize(function_at(base, *hexes), lookup)


def fast(src):
    return src.split("#else", 1)[1] if "#else" in src else src


# -- summary rules ----------------------------------------------------------

def test_reads_entry_and_kills_written_register():
    contract = summarize("89d8", "c3")  # mov eax,ebx; ret
    assert "EBX" in contract.reads and "EAX" not in contract.reads
    assert "EAX" in contract.kills


def test_inc_preserves_carry():
    contract = summarize("40", "c3")  # inc eax
    assert "EAX" in contract.kills
    assert "CF" not in contract.kills
    # SLEIGH omits AF, so the contract stays conservative on it.
    assert {"ZF", "SF", "OF", "PF"} <= contract.kills


def test_logic_defines_all_except_af():
    contract = summarize("31c0", "c3")  # xor eax,eax
    assert "EAX" in contract.kills
    assert "AF" not in contract.kills
    assert {"CF", "OF", "ZF", "SF", "PF"} <= contract.kills


def test_same_operand_zero_does_not_read_register():
    # The lifter folds xor eax,eax to a constant, so the old EAX is not read.
    assert "EAX" not in summarize("31c0", "c3").reads


def test_partial_write_does_not_kill_the_gpr():
    contract = summarize("8ac3", "c3")  # mov al,bl
    assert "EAX" not in contract.kills
    assert "EBX" in contract.reads


def test_push_counts_as_a_read_of_a_saved_register():
    contract = summarize("53", "5b", "c3")  # push ebx; pop ebx
    assert "EBX" in contract.reads
    assert "EBX" in contract.kills


def test_flag_consumer_reads_its_flag():
    contract = summarize("7401", "c3")  # jz +1
    assert "ZF" in contract.reads


def test_unknown_direct_call_is_conservative():
    body = function_at(ENTRY, rel_call(ENTRY, CALLEE), "c3")
    contract = CC.summarize(body, lambda target: None)
    assert contract.reads == frozenset(CC.FIELDS)
    # No CPU field is definitely overwritten by the unknown callee; the CALL's
    # own ESP p-code is the only write the body can prove.
    assert contract.kills <= frozenset(("ESP",))


def test_indirect_call_is_conservative():
    body = function_at(ENTRY, "ffd0", "c3")  # call eax
    contract = CC.summarize(body, lambda target: None)
    assert contract.reads == frozenset(CC.FIELDS)
    assert contract.kills <= frozenset(("ESP",))


def test_callee_reads_and_kills_reach_the_caller():
    callee = summarize("31c0", "c3")  # kills EAX, writes flags
    caller = function_at(ENTRY, "b844332211", rel_call(ENTRY + 5, CALLEE), "c3")
    contract = CC.summarize(caller, lambda target: callee if target == CALLEE else None)
    assert "EAX" in contract.kills


def test_tail_jump_body_is_not_closed():
    # jmp past the body: the CFG has no modeled successor.
    body = function_at(ENTRY, "e900000000")
    assert not CC.cfg_is_closed(body)


# -- recursive fixed point --------------------------------------------------

class _FakeTr:
    def __init__(self, targets):
        self._targets = targets

    def branch_target(self, ins):
        return self._targets.get(ins.addr)


def test_nonconverged_scc_falls_back_to_conservative(monkeypatch):
    # A self-recursive call site with the iteration cap forced to one pass must
    # not publish a possibly under-approximated reads set.
    body = function_at(ENTRY, rel_call(ENTRY, ENTRY), "c3")
    monkeypatch.setattr(CC, "function_ir", lambda tr, lifter, fn: body)
    monkeypatch.setattr(CC, "_scc_cap", lambda size, cells: 1)
    tr = _FakeTr({ENTRY: ENTRY})
    fn = type("Fn", (), {"addr": ENTRY, "insns": body.insns})()
    contracts = CC.analyze(tr, [fn], analyzable=lambda addr: True, roots=[ENTRY])
    assert contracts[ENTRY] == CC.CONSERVATIVE


def test_recursive_scc_converges(monkeypatch):
    body = function_at(ENTRY, rel_call(ENTRY, ENTRY), "c3")
    monkeypatch.setattr(CC, "function_ir", lambda tr, lifter, fn: body)
    tr = _FakeTr({ENTRY: ENTRY})
    fn = type("Fn", (), {"addr": ENTRY, "insns": body.insns})()
    contracts = CC.analyze(tr, [fn], analyzable=lambda addr: True, roots=[ENTRY])
    # The self-call observes only what the body itself reads before the call;
    # the least fixed point is the call's ESP use, not a conservative set.
    assert contracts[ENTRY].reads == frozenset(("ESP",))
    assert "EAX" not in contracts[ENTRY].kills


# -- emission integration ---------------------------------------------------

def test_emitter_drops_killed_field_and_poisons_it():
    contract = summarize("31c0", "c3")  # callee kills EAX and flags
    caller = function_at(ENTRY, "b844332211", rel_call(ENTRY + 5, CALLEE), "c3")
    facts = {}
    src = emit(caller, "test_fn", call_symbols={CALLEE: "callee"},
               call_contracts={CALLEE: contract}, lazy_flags=True, facts=facts)
    body = fast(src)
    assert facts["call_contract_calls"] == 1
    assert facts["call_contract_fields_skipped"] > 0
    assert "RECOMP_CONTRACT_POISON_CALL" in body
    # The caller's pre-call EAX is not published to the guest field.
    # The dropped EAX store survives only behind the mod-hook guard.
    tail = body
    guard = tail.index("if (RECOMP_UNLIKELY(recomp_hooks_ever)) {")
    store = tail.index("c->r[0] = (uint32_t)((0x44ull")
    assert tail.count("c->r[0] = (uint32_t)((0x44ull") == 1
    assert guard < store < tail.index("}", guard)


def test_emitter_keeps_a_field_the_callee_reads():
    # setz al reads ZF; inc ecx then overwrites the flags.  A wrong summary
    # would drop ZF, but the correct one must publish it.
    contract = summarize("0f94c0", "41", "c3")
    assert "ZF" in contract.reads
    caller = function_at(ENTRY, "39d8", rel_call(ENTRY + 2, CALLEE), "c3")
    facts = {}
    src = emit(caller, "test_fn", call_symbols={CALLEE: "callee"},
               call_contracts={CALLEE: contract}, lazy_flags=True, facts=facts)
    body = fast(src)
    masks = [int(m, 16) for m in
             re.findall(r"RECOMP_CONTRACT_POISON_CALL\(c, 0x([0-9a-f]+)u\)", body)]
    assert all(mask & (1 << 11) == 0 for mask in masks)  # ZF bit is 8+3


def test_emitter_keeps_a_preserved_field():
    # The callee does not touch EBX, so EBX is preserved.  Even though this
    # caller never reads EBX again, its value must be published before the call
    # so it can flow out to the caller's caller.
    contract = summarize("31c0", "c3")  # kills EAX/flags, preserves EBX
    assert "EBX" not in contract.kills
    caller = function_at(ENTRY, "bb44332211", rel_call(ENTRY + 5, CALLEE), "c3")
    src = emit(caller, "test_fn", call_symbols={CALLEE: "callee"},
               call_contracts={CALLEE: contract}, lazy_flags=True)
    body = fast(src)
    assert "c->r[3] = (uint32_t)((0x44ull << 0)" in body


def test_emitter_without_contracts_publishes_everything():
    caller = function_at(ENTRY, "b844332211", rel_call(ENTRY + 5, CALLEE), "c3")
    src = emit(caller, "test_fn", call_symbols={CALLEE: "callee"}, lazy_flags=True)
    assert "RECOMP_CONTRACT_POISON_CALL" not in fast(src)
