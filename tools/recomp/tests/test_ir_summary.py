"""Calling-convention inference over SLEIGH p-code (tools/recomp/ir)."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter  # noqa: E402
from ir import summary as S  # noqa: E402

LIFTER = Lifter()


def function(addr, *hexes):
    insns, at = [], addr
    for h in hexes:
        raw = bytes.fromhex(h.replace(" ", ""))
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return S.FunctionIR(addr, insns, S.default_successors(insns))


def summarize(*funcs, imports=None):
    table = {f.addr: f for f in funcs}

    def targets(a):
        out = []
        for ins in table[a].insns:
            for op in ins.ops:
                if op.opc in ("CALL", "BRANCH") and op.ins[0][0] == "ram":
                    out.append(op.ins[0][1])
        return out

    return S.summarize_all(list(table), table.__getitem__, targets,
                           (imports or {}).get)


def one(*hexes):
    f = function(0x1000, *hexes)
    return summarize(f)[0x1000]


def test_thiscall_leaf_reads_ecx_only():
    s = one("b8ffff7f7f", "894104", "c3")  # mov eax,imm; mov [ecx+4],eax; ret
    assert s.ok and s.inputs == {"ECX"} and s.purge == 0 and s.standard()
    assert S.CALLEE_SAVED <= s.preserved and "EAX" not in s.preserved


def test_saved_register_and_stdcall_purge():
    # push esi; mov esi,[esp+8]; mov eax,[esi]; pop esi; ret 4
    s = one("56", "8b742408", "8b06", "5e", "c20400")
    assert s.ok and s.inputs == frozenset() and s.purge == 4
    assert "ESI" in s.preserved and s.standard()


def test_frame_pointer_epilogue_restores_saved_registers():
    # push ebp; mov ebp,esp; sub esp,8; push ebx; mov ebx,[ebp+8]; mov eax,ebx;
    # pop ebx; mov esp,ebp; pop ebp; ret
    s = one("55", "8bec", "83ec08", "53", "8b5d08", "8bc3", "5b", "8be5", "5d", "c3")
    assert s.ok and {"EBX", "EBP"} <= s.preserved and s.purge == 0 and s.standard()


def test_eax_input_is_nonstandard():
    s = one("01c8", "c3")  # add eax,ecx; ret
    assert s.inputs == {"EAX", "ECX"} and not s.standard()


def test_unrestored_callee_saved_register_is_nonstandard():
    s = one("be01000000", "c3")  # mov esi,1; ret
    assert s.ok and "ESI" not in s.preserved and not s.standard()


def test_flag_read_at_entry_is_an_input():
    s = one("7401", "90", "c3")  # jz +1; nop; ret
    assert "ZF" in s.inputs and not s.standard()


def test_float_return_and_float_input():
    push = one("d901", "c3")  # fld dword [ecx]; ret
    assert push.ok and push.x87_delta == 1 and push.x87_inputs == 0 and push.standard()
    pop = one("d919", "c3")  # fstp dword [ecx]; ret
    assert pop.x87_delta == -1 and pop.x87_inputs == 1 and not pop.standard()


def test_direct_callee_purge_and_inputs_propagate():
    g = function(0x2000, "8bc1", "c20400")         # mov eax,ecx; ret 4
    f = function(0x1000, "6a01", "e8f90f0000", "c3")  # push 1; call g; ret
    out = summarize(f, g)
    assert out[0x2000].purge == 4 and out[0x2000].inputs == {"ECX"}
    assert out[0x1000].ok and out[0x1000].purge == 0
    assert out[0x1000].inputs == {"ECX"}  # f passes its own ECX through to g


def test_indirect_stdcall_purge_is_inferred_from_pushes():
    # push esi; mov esi,ecx; push 2; push 1; mov eax,[esi]; call [eax+8]; pop esi; ret
    s = one("56", "8bf1", "6a02", "6a01", "8b06", "ff5008", "5e", "c3")
    assert s.ok and "ESI" in s.preserved and s.purge == 0 and s.inputs == {"ECX"}


def test_indirect_cdecl_cleanup_after_call():
    # mov eax,[ecx]; push 1; call [eax]; add esp,4; ret
    s = one("8b01", "6a01", "ff10", "83c404", "c3")
    assert s.ok and s.purge == 0 and s.inputs == {"ECX"}


def test_wrong_purge_guess_is_reported_not_accepted():
    # push 1; call [eax] (callee assumed to pop 4); ret 4 -> ESP at RET is
    # entry+4, so the function looks like `ret 4` with purge 8: inconsistent
    # only if the guess was wrong. Here the guess is consistent by design;
    # make the caller then pop again so the stack no longer balances.
    s = one("6a01", "ff10", "59", "c3")  # push 1; call [eax]; pop ecx; ret
    # pop ecx right after the call marks cdecl cleanup, so this balances.
    assert s.ok and s.purge == 0


def test_unknown_call_float_result_is_detected():
    # mov eax,[ecx]; call [eax]; fstp dword [esi]; ret
    s = one("8b01", "ff10", "d91e", "c3")
    assert s.ok and s.x87_inputs == 0 and s.x87_delta == 0
    assert "unknown call returned ST0" in s.notes


def test_recursive_function_converges():
    # f: test ecx,ecx; jz done; dec ecx; call f; done: ret
    f = function(0x1000, "85c9", "7406", "49", "e8f7ffffff", "90", "c3")
    s = summarize(f)[0x1000]
    assert s.ok and s.inputs == {"ECX"} and s.standard()


def test_eh_prolog_frame_helper_is_substituted_into_callers():
    # VC6 _EH_prolog: builds the caller's EBP frame and returns 16 bytes lower.
    helper = function(0x2000, "6aff", "50", "64a100000000", "50", "8b44240c",
                      "64892500000000", "896c240c", "8d6c240c", "50", "c3")
    # mov eax,handler; call _EH_prolog; push ecx; push ebx; xor ebx,ebx;
    # call another function; pop ebx;
    # mov ecx,[ebp-0xc]; mov fs:[0],ecx; leave; ret
    other = function(0x3000, "b801000000", "c3")
    caller = function(0x1000, "b878563412", "e8f60f0000", "51", "53", "33db",
                      "e8ed1f0000", "5b",
                      "8b4df4", "64890d00000000", "c9", "c3")
    out = summarize(caller, helper, other)
    h = out[0x2000]
    assert h.ok and h.inputs == {"EAX"} and h.purge == -16
    assert h.exit_regs["EBP"] == ("e", "ESP", 0) and h.exit_stack[0] == ("e", "EBP", 0)
    c = out[0x1000]
    assert c.ok and c.standard() and c.purge == 0 and {"EBX", "EBP"} <= c.preserved
    assert c.inputs == frozenset()
    assert "calls unknown direct target" not in c.notes


@pytest.mark.parametrize("mask", ["f8", "f0", "e0"])
def test_aligned_frame_restores_frame_pointer_across_call(mask):
    # Frame pointer is saved twice: entry EBP, then the pre-alignment ESP.
    # The working EBP is reused and must be recovered from the aligned frame.
    helper = function(0x2000, "b801000000", "c3")
    prologue = ["55", "8bec", "83e4" + mask, "83ec10", "53", "55",
                "56", "57", "bd12345678"]
    next_addr = 0x1000 + sum(len(h) // 2 for h in prologue) + 5
    call = "e8" + (helper.addr - next_addr).to_bytes(4, "little", signed=True).hex()
    caller = function(0x1000, *prologue, call, "5f", "5e", "5d", "5b", "8be5", "5d", "c3")
    s = summarize(caller, helper)[caller.addr]
    assert s.ok and s.standard() and s.purge == 0
    assert S.CALLEE_SAVED <= s.preserved
    assert "EBP" not in s.inputs


def test_alignment_without_restoring_entry_stack_is_unknown():
    s = one("83e4f8", "c3")
    assert not s.ok and "stack pointer unknown at return" in s.reasons


def test_alignment_does_not_hide_overwritten_saved_register():
    # [aligned ESP] can overlap the EBP saved at entry ESP-4.
    s = one("55", "8bec", "83e4f8", "c7042400000000", "8be5", "5d", "c3")
    assert "EBP" not in s.preserved


def test_ret_load_is_not_an_explicit_return_address_read():
    assert "reads its return address" not in one("c3").notes
    assert "reads its return address" not in one("c20800").notes
    assert "reads its return address" in one("8b0424", "c3").notes
    assert "reads its return address" in one("58", "50", "c3").notes


def test_failed_tail_target_keeps_ret_purge_and_unknown_x87():
    thunk = function(0x1000, "e9fb0f0000")
    bad = S.Summary(0x2000, ok=False, reasons=("unsupported",))
    analyzer = S.Analyzer(lambda a: bad, ret_purge=lambda a: 8)
    s = analyzer.summarize(thunk)
    assert s.ok and s.purge == 8 and s.x87_delta is None
    assert not s.standard()
    assert "tail-jumps to a function whose analysis failed" in s.notes
    assert "jumps outside the function to a non-function" not in s.notes


def test_missing_tail_target_is_distinct_from_failed_analysis():
    s = S.Analyzer(lambda a: None).summarize(function(0x1000, "e9fb0f0000"))
    assert "jumps outside the function to a non-function" in s.notes


@pytest.mark.parametrize("purge", [0, 4, 8])
def test_import_metadata_separates_stack_cleanup_from_push_run(purge):
    # Two pushes even when the import pops zero/one; cleanup balances the rest.
    f = function(0x1000, "6a02", "6a01", "ff1500300000",
                 *(["83c4%02x" % (8 - purge)] if purge != 8 else []), "c3")
    analyzer = S.Analyzer(lambda a: None, lambda a: "known" if a == 0x3000 else None,
                          import_purge=lambda a: purge)
    facts = analyzer.forward(f)
    call = next(iter(facts.calls.values()))
    assert call[0].purge == purge
    assert call[3] == (purge or 8)  # cdecl argument count is not supplied by argc_stdcall
    s = analyzer.summarize(f)
    assert s.ok and s.purge == 0


def test_import_tail_uses_runtime_cleanup_metadata():
    thunk = function(0x1000, "ff2500300000")
    s = S.Analyzer(lambda a: None, lambda a: "known", import_purge=lambda a: 12).summarize(thunk)
    assert s.ok and s.purge == 12 and s.stack_args == 12 and s.x87_delta is None


def test_import_tables_only_accept_supported_shapes_and_reject_conflicts(tmp_path):
    from ir.imports import read_import_cleanup
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "fixture.cpp").write_text('''
    // {"a.dll", "ignored", 9, fn}
    const Other unrelated[] = {{"a.dll", "also_ignored", 7, fn}};
    #define G(n, a, f) { "a.dll", n, a, f }
    const ImportShim shims[] = {
        {"A.dll", "zero", 0, fn}, {"a.dll", "two", 2, fn},
        {"b.dll", "two", 3, fn}, {"a.dll", "cdecl", ARGC_CDECL, fn},
        {"a.dll", "unknown", ARGC_UNKNOWN, fn},
        {"a.dll", "conflict", 1, fn}, {"a.dll", "conflict", 2, fn},
        {"a.dll", "expression", COUNT + 1, fn}, G("macro", 4, fn)
    };
    ''')
    counts = read_import_cleanup(tmp_path)
    assert counts == {("a.dll", "zero"): 0, ("a.dll", "two"): 8,
                      ("b.dll", "two"): 12, ("a.dll", "cdecl"): 0,
                      ("a.dll", "unknown"): None, ("a.dll", "conflict"): None,
                      ("a.dll", "macro"): 16}


def test_live_runtime_import_table_metadata():
    from ir.imports import read_import_cleanup
    counts = read_import_cleanup()
    assert counts[("kernel32.dll", "GetTickCount")] == 0
    assert counts[("user32.dll", "MessageBoxA")] == 16
    assert counts[("user32.dll", "wsprintfA")] == 0
    assert counts[("gdi32.dll", "BitBlt")] == 36


def test_pushing_defined_callee_saved_register_as_argument_counts_for_cleanup():
    # Saving entry EBX ends the prologue. The later push of an assigned EBX
    # is an argument, even though the operand's register name is callee-saved.
    s = one("53", "bb01000000", "53", "ff10", "5b", "c3")
    assert s.ok and s.purge == 0 and "EBX" in s.preserved


def test_unknown_tail_purge_does_not_hide_a_lost_stack_pointer():
    f = function(0x1000, "83e4f8", "e9f80f0000")
    s = S.Analyzer(lambda a: None).summarize(f)
    assert not s.ok and "stack pointer unknown at return" in s.reasons


def test_partial_summary_is_not_used_as_a_complete_call_abi():
    # A tail fallback can leave a partial wrapper summary with a negative
    # apparent purge. A caller must use the target's actual RET n evidence,
    # rather than substitute the partial wrapper's frame/exit facts.
    partial = S.Summary(0x2000, purge=-36, x87_delta=None, preserved={"EDI"},
                        exit_regs={"EBP": ("e", "ESP", -4)})
    f = function(0x1000, "6a01", "e8f90f0000", "c3")
    s = S.Analyzer(lambda a: partial, ret_purge=lambda a: 4).summarize(f)
    assert s.ok and s.purge == 0 and S.CALLEE_SAVED <= s.preserved
    assert "calls unknown direct target" in s.notes


def test_arguments_built_around_a_getter_for_a_known_import():
    # Push lParam/wParam/Msg; call a virtual getter with no arguments;
    # push returned HWND; PostMessageA pops all four arguments.
    f = function(0x1000, "6801000100", "6a1b", "6802010000", "ff5010", "50",
                 "ff1500300000", "c3")
    analyzer = S.Analyzer(lambda a: None, lambda a: "PostMessageA" if a == 0x3000 else None,
                          import_purge=lambda a: 16 if a == 0x3000 else None)
    facts = analyzer.forward(f)
    calls = list(facts.calls.values())
    assert calls[0][2] == -16 and calls[1][2] == -20
    s = analyzer.summarize(f)
    assert s.ok and s.purge == 0 and "reads its return address" not in s.notes


def test_future_import_does_not_determine_cleanup_across_a_branch():
    f = function(0x1000, "6a01", "ff10", "7401", "50", "ff1500300000", "c3")
    analyzer = S.Analyzer(lambda a: None, lambda a: "known", import_purge=lambda a: 8)
    # A branch changes the available argument group; retain conservative inference.
    st = S.entry_state()
    st.run = 4
    assert analyzer.guess_purge(f, 1, st) == 4


def test_frame_address_in_ebp_can_be_an_ordinary_argument():
    # EBP is reused as a pointer to an incoming argument, then passed to an
    # indirect callee. This is not the second save of an aligned-frame prologue.
    s = one("55", "8d6c2408", "55", "ff10", "5d", "c3")
    assert s.ok and s.purge == 0 and "EBP" in s.preserved


def test_aligned_stack_depth_difference_is_reported_even_if_epilogue_restores_esp():
    # The old unknown ESP after AND hid this differing-depth join. Tracking
    # the aligned base exposes it; current analysis conservatively rejects it.
    s = one("55", "8bec", "83e4f8", "7401", "50", "8be5", "5d", "c3")
    assert not s.ok and "stack depth differs at a join" in s.reasons
    assert s.purge == 0 and "EBP" in s.preserved
