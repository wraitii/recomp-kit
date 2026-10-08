"""Native SSA/eager full-state checks over integer and x87 instruction bytes."""
from pathlib import Path
from types import SimpleNamespace
import re
import subprocess

import capstone
import translate as T
from code_map import decode_span
from .lift import Lifter
from .cfg import FunctionIR, default_successors
from .emit_c import emit
from .integer_extra_checks import CASES as INTEGER_EXTRA_CASES
from .x87_register_checks import CASES as X87_REGISTER_CASES

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[2]
ENTRY = 0x100000  # Synthetic guest address, never a game entry point.

CASES = {
    "inc_dec": ("fec0", "6640", "fecc", "4a", "8903", "c3"),
    "sub": ("29c8", "29d2", "8903", "c3"),
    "sbb": ("19c8", "8903", "c3"),
    "sbb_same": ("19c0", "8903", "c3"),
    "sbb_byte": ("18c8", "8903", "c3"),
    "sbb_word": ("6619c8", "8903", "c3"),
    "shl_byte_cl": ("d2e0", "8903", "c3"),
    "shr_word_cl": ("66d3e8", "8903", "c3"),
    "shl_dword_cl": ("d3e0", "8903", "c3"),
    "shr_dword_cl": ("d3e8", "8903", "c3"),
    "shift_zero": ("c1e800", "8903", "c3"),
    "shift_mask_zero": ("c1e020", "8903", "c3"),
    "shift_mask_one": ("c1e821", "8903", "c3"),
    "shift_wide": ("c0e808", "8903", "c3"),
    "partial": ("8ac4", "b47f", "00c8", "8903", "c3"),
    "loop_phi": ("b900000000", "83c101", "83f904", "72f8", "890b", "c3"),
    "alias": ("8b06", "01c8", "8903", "8b16", "c3"),
    "div_valid": ("83e203", "b937000000", "f7f1", "8903", "c3"),
    "div_zero": ("31c9", "f7f1", "8903", "c3"),
    "div_overflow": ("b901000000", "ba01000000", "f7f1", "8903", "c3"),
    "publication_join": ("a901000000", "7405", "ba11223344", "8903", "c3"),
    "publication_loop": ("b904000000", "890b", "49", "75fb", "c3"),
    "partial_word_loop": ("b904000000", "8903", "6640", "49", "75f9", "c3"),
    "adc_register": ("11c8", "8903", "c3"),
    "adc_memory_source": ("1306", "8903", "c3"),
    "sbb_memory_source": ("1b06", "8903", "c3"),
    "add_memory": ("0103", "c3"),
    "sub_memory": ("2903", "c3"),
    "inc_memory": ("ff03", "c3"),
    "dec_memory_word": ("66ff0b", "c3"),
    "cmp_memory": ("3903", "c3"),
    "extend_alias": ("0fbec4", "0fb7d0", "8903", "c3"),
    "xchg_register": ("92", "8903", "c3"),
    "not_memory": ("f713", "c3"),
    "leave": ("55", "89e5", "50", "c9", "c3"),
    "x87_load_store": ("d906", "d903", "d9c1", "d9c9", "d95b04", "ddd9", "c3"),
    # Narrow (proven binary32) FSTP m32 skips the redundant fto_float rounding
    # step under PC=00 but must still quiet an sNaN payload. The harness sweeps
    # every PC/RC combination and the special-input table includes sNaN/qNaN.
    "x87_narrow_store": ("d906", "d91b", "c3"),
    "x87_narrow_arith_store": ("d906", "d84604", "d84604", "d91b", "c3"),
    "x87_double_store": ("dd06", "dd13", "dd5b08", "c3"),
    "x87_extended_store": ("db2e", "db3b", "c3"),
    "x87_arithmetic": ("d906", "d94604", "d80e", "d85e08", "d806", "d9c9", "dee1", "d91b", "c3"),
    "x87_divide": ("d906", "d84604", "d87e08", "d87604", "d95b04", "c3"),
    "x87_register_direction": ("d906", "d94604", "dce1", "d8e9", "dce9", "dcf1", "dcf9", "def1", "d95b04", "c3"),
    "x87_comparison_status": ("d906", "d85604", "dfe0", "8903", "d9e4", "d85e08", "dfe0", "c3"),
    "x87_quiet_compare": ("d906", "d94604", "dde1", "dde9", "dfe0", "8903", "c3"),
    "x87_two_pops": ("d906", "d94604", "ded9", "dfe0", "8903", "c3"),
    "x87_round_control": ("dd06", "d9fc", "dd1b", "c3"),
    "x87_integer_store": ("d906", "df13", "db5304", "df7b08", "c3"),
    "x87_exact_integer": ("df2e", "d9c0", "df3b", "d9c0", "db5b08", "dddb", "c3"),
    # fild qword [ebx]; ret -- a live exact-integer return keeps its shadow.
    "x87_fild_return": ("df2b", "c3"),
    # fild qword [ebx]; fstp st0; fld1; fistp qword [ebx+8]; ret -- the
    # register reused after a pop must not keep the popped exact shadow.
    "x87_exact_reuse": ("df2b", "ddd8", "d9e8", "df7b08", "c3"),
    "x87_integer_arithmetic": ("df06", "da06", "da2e", "da36", "da3e", "da16", "da1e", "c3"),
    "x87_partial_remainder": ("d94604", "d906", "d9f8", "dfe0", "8903", "d9f5", "d91b", "c3"),
    "x87_clear_status": ("dbe2", "dfe0", "8903", "c3"),
    "x87_classify": ("d9e5", "dfe0", "8903", "d906", "d9e5", "dfe0", "c3"),
    "x87_control_word": ("d93b", "d92b", "d97b02", "c3"),
    "x87_rotate_top": ("d9f6", "d9f7", "d9e8", "d9ee", "d9e1", "d9e0", "d95b04", "c3"),
    # Direct absolute memory: watched single read/write and a source-only
    # arithmetic read that must not be duplicated across flag expressions.
    "absolute_read": ("a180000100", "8903", "c3"),
    "absolute_write": ("b844332211", "a380000100", "c3"),
    "absolute_add_source": ("b807000000", "030580000100", "8903", "c3"),
    # Other register-destination forms read the absolute operand once and share
    # the captured load across result and flag expressions.
    "absolute_and_source": ("b8ffffffff", "230580000100", "8903", "c3"),
    "absolute_or_source": ("b800000000", "0b0580000100", "8903", "c3"),
    "absolute_xor_source": ("b8f0f0f0f0", "330580000100", "8903", "c3"),
    "absolute_adc_source": ("b801000000", "130580000100", "8903", "c3"),
    "absolute_sbb_source": ("b801000000", "1b0580000100", "8903", "c3"),
    "absolute_cmp_source": ("b801000000", "3b0580000100", "8903", "c3"),
    "absolute_test_source": ("b8f0f0f0f0", "850580000100", "8903", "c3"),
    "absolute_imul_source": ("b802000000", "0faf0580000100", "8903", "c3"),
    "absolute_movzx_source": ("b8ffffffff", "0fb60580000100", "8903", "c3"),
    "absolute_movsx_source": ("b8ffffffff", "0fbe0580000100", "8903", "c3"),
    "absolute_word_read": ("66a180000100", "8903", "c3"),
    "absolute_word_write": ("66a380000100", "c3"),
    # Post-helper reload coverage: a returning divide-error handler mutates
    # EBX/ESI/flags. Every variant must re-read them from the helper's result
    # state, not only the EAX/EDX division registers.
    "div_handler_mutates_siblings": ("bb44332211", "31c9", "f7f1", "89d8", "c3"),
    # Byte-audited REP MOVSD: zero count, both DF directions and an overlapping
    # forward copy that exposes the runtime helper's access-then-advance order.
    "rep_movsd_zero": ("31c9", "f3a5", "c3"),
    "rep_movsd_df0": ("fc", "b903000000", "f3a5", "c3"),
    "rep_movsd_df1": ("be20000100", "bf60000100", "fd", "b903000000", "f3a5", "c3"),
    "rep_movsd_overlap": ("be00000100", "bf02000100", "b904000000", "fc", "f3a5", "c3"),
    # Remaining byte-audited string forms and bare WAIT/SAHF/MUL. Flags after
    # REPNE SCASB / REPE CMPSB depend on where the scan stops.
    "rep_movsb_df0": ("be00000100", "bf20000100", "fc", "b907000000", "f3a4", "c3"),
    "rep_movsb_df1": ("be20000100", "bf60000100", "fd", "b905000000", "f3a4", "c3"),
    "rep_movsw": ("be00000100", "bf20000100", "fc", "b903000000", "66f3a5", "c3"),
    "movsb_single": ("be00000100", "bf20000100", "a4", "c3"),
    "rep_stosd": ("bf00000100", "fc", "b904000000", "f3ab", "c3"),
    "rep_stosb_df1": ("bf20000100", "fd", "b905000000", "f3aa", "c3"),
    "stosw_single": ("bf00000100", "66ab", "c3"),
    "repne_scasb": ("bf00000100", "fc", "b908000000", "f2ae", "c3"),
    "repne_scasb_zero_count": ("bf00000100", "31c9", "f2ae", "c3"),
    "repe_scasb": ("bf00000100", "fc", "b908000000", "f3ae", "c3"),
    "scasd_single": ("bf00000100", "af", "c3"),
    "repe_cmpsb": ("be00000100", "bf20000100", "fc", "b908000000", "f3a6", "c3"),
    "repne_cmpsd": ("be00000100", "bf20000100", "fc", "b904000000", "f2a7", "c3"),
    # FS:[0] chain-head reads and the MSVC epilogue unlink store (no SEH hook:
    # the decoded emitter attaches none to a body without establishing sites).
    "fs_chain_read": ("64a100000000", "8903", "c3"),
    "fs_chain_restore": ("8b0b", "64890d00000000", "c3"),
    "fs_chain_push_pop": ("64ff3500000000", "5a", "8913", "c3"),
    "wait_nop": ("9b", "c3"),
    "sahf": ("9e", "c3"),
    "sahf_then_branch": ("9e", "7502", "ffc0", "c3"),
    "mul8": ("f6e1", "c3"),
    "mul16": ("66f7e1", "c3"),
    "mul32": ("f7e1", "c3"),
    "mul32_mem": ("f723", "c3"),
}

# Signed-integer (CDQ/IMUL/IDIV/NEG/SAR/SETcc) and x87 register-run cases kept
# in their own modules. They are plain byte tuples and merge directly here so the
# five-column eager/raw/scalar/strict/local harness covers them.
CASES = {**CASES, **INTEGER_EXTRA_CASES, **X87_REGISTER_CASES}


def _table_case():
    """A bounded jump table in the image: `cmp eax,3; ja end; jmp [eax*4+table]`.

    Cases 0 and 2 share a block, so the SSA switch groups two labels on one
    edge. The table is image data after the RET, never decoded as code.
    Layout: cmp 0, ja 3, jmp 5, block0 12 (mov+jmp), block1 19 (mov+jmp),
    block2 26 (inc), ret 28, table 29.
    """
    code = ("83f803", "7717", "ff2485" + (ENTRY + 29).to_bytes(4, "little").hex(),
            "b901000000", "eb09", "b902000000", "eb02", "ffc3", "c3")
    targets = [ENTRY + 12, ENTRY + 19, ENTRY + 12, ENTRY + 26]
    return code, b"".join(t.to_bytes(4, "little") for t in targets)


TABLE_CASE, TABLE_DATA_BYTES = _table_case()
#: Cases whose image carries data after the code (name -> bytes).
TABLE_DATA = {"jump_table_switch": TABLE_DATA_BYTES}
CASES["jump_table_switch"] = TABLE_CASE


CALLEE = 0x200000  # Synthetic callee address, never a game entry point.


def _call_bytes(addr, target):
    rel = (target - (addr + 5)) & 0xffffffff
    return "e8" + "".join("%02x" % ((rel >> (8 * n)) & 0xff) for n in range(4))


def _caller(prefix, suffix, callee=CALLEE):
    addr = ENTRY + sum(len(bytes.fromhex(h)) for h in prefix)
    return prefix + [_call_bytes(addr, callee)] + suffix


def _loop_caller():
    """Loop with a call inside the body; the backedge targets the call itself."""
    call_addr = ENTRY + 5
    call = _call_bytes(call_addr, CALLEE)
    jb_addr = call_addr + 5 + 3 + 3
    rel = (call_addr - (jb_addr + 2)) & 0xff
    # mov ecx,0; loop: call; add ecx,1; cmp ecx,3; jb loop; mov [ebx],ecx; ret
    return ["b900000000", call, "83c101", "83f903", "72%02x" % rel, "890b", "c3"]


# Byte encodings for the flag-producing shapes the lazy descriptor covers, at
# byte/word/dword width. CMP/SUB lower to X86_CC_SUB; TEST lowers to
# X86_CC_LOGIC, the same descriptor an AND/OR/XOR would use.
FLAG_ABI_ENCODINGS = {
    "cmp": {"8": "38d8", "16": "6639d8", "32": "39d8"},
    "sub": {"8": "28d8", "16": "6629d8", "32": "29d8"},
    "add": {"8": "00d8", "16": "6601d8", "32": "01d8"},
    "test": {"8": "84d8", "16": "6685d8", "32": "85d8"},
    "inc": {"8": "fec0", "16": "6640", "32": "40"},
    "dec": {"8": "fec8", "16": "6648", "32": "48"},
}
#: `setz al; mov [ebx],al` makes ZF observable after a seam. Every producer in
#: FLAG_ABI_ENCODINGS defines ZF.
FLAG_CONSUMER = ["0f94c0", "8803", "c3"]


def _flag_abi_cases():
    """CMP/SUB/ADD/TEST/INC/DEC at 8/16/32-bit across CALL and RET seams.

    The `_call` rows put the producer in the caller and observe the flags after
    a callee that touches no flags, so a descriptor written at the call seam
    must survive the call. The `_ret` rows put the producer in a byte-backed
    SSA callee and observe the flags it returns, so the callee's RET descriptor
    must materialise for the caller's consumer.
    """
    cases = {}
    for op, widths in FLAG_ABI_ENCODINGS.items():
        for width, code in widths.items():
            cases["flags_%s%s_call" % (op, width)] = {
                "hexes": _caller([code], FLAG_CONSUMER),
                "callee_hexes": ["8b442404", "c3"],  # mov eax,[esp+4]; ret
                "ssa_callee": True,
                "resumable": False,
            }
            cases["flags_%s%s_ret" % (op, width)] = {
                "hexes": _caller([], FLAG_CONSUMER),
                "callee_hexes": [code, "c3"],
                "ssa_callee": True,
                "resumable": False,
            }
    return cases


# Byte-backed callers with explicit host callees. The callee models a guest
# function: mutate state, pop the return address (RET or RET n) and set EIP.
# The raw/scalar/strict/local IR callers are compared against the eager caller for
# the same bytes under the same full-state obligations.
CALL_CASES = {
    "call_cdecl_mutate": {
        # mov ebx,1; mov eax,0x44332211; push ecx; call; add esp,4; add eax,ebx; ret
        "hexes": _caller(["bb01000000", "b811223344", "51"],
                         ["83c404", "01d8", "c3"]),
        "callee": """static void call_cdecl_mutate_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0x0badf00du;
    c->r[R_ECX] = 0x12345678u;
    c->r[R_EDX] = 0xdeadbeefu;
    c->eflags_cf = 1; c->eflags_zf = 0; c->eflags_sf = 1;
    c->eflags_of = 0; c->eflags_pf = 1; c->eflags_af = 0;
    fpush(c, 2.5);
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "call_ret4": {
        # mov eax,0; push 0x11223344; call; mov ebx,0x99; ret  (callee RET 4)
        "hexes": _caller(["b800000000", "6844332211"],
                         ["bb99000000", "c3"]),
        "callee": """static void call_ret4_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 8; /* RET 4 */
    c->r[R_EAX] = 0xfeedfaceu;
    c->eflags_cf = 0; c->eflags_zf = 1;
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "call_x87_live": {
        # mov ecx,0x10600; fld1; call; fadd st0,st1; fstp [ecx]; mov eax,[ecx]; ret
        "hexes": _caller(["b900060100", "d9e8"],
                         ["d8c1", "d919", "8b01", "c3"]),
        "callee": """static void call_x87_live_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    fpush(c, 4.0);
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "call_x87_carry_join": {
        # mov ecx,0x10600; fld1; test eax,1; jz +5 (over the call); call;
        # fadd st0,st1; fstp [ecx]; mov eax,[ecx]; ret. The called path resets
        # the scalar tracker and pushes, the other carries a dirty FLD1, so the
        # join merges an inactive and an active predecessor at different TOPs.
        "hexes": _caller(["b900060100", "d9e8", "a901000000", "7405"],
                         ["d8c1", "d919", "8b01", "c3"]),
        "callee": """static void call_x87_carry_join_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    fpush(c, 4.0);
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "call_resumable": {
        # mov ebx,1; call; add ebx,1; ret  (callee resumes normally)
        "hexes": _caller(["bb01000000"], ["83c301", "c3"]),
        "callee": """static void call_resumable_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0xabcd1234u;
    c->eip = ret;
}
""",
        "resumable": True,
    },
    "call_resumable_divert": {
        # Same caller, but the callee leaves EIP elsewhere: resumable callers
        # must bail out instead of continuing at the fallthrough.
        "hexes": _caller(["bb01000000"], ["83c301", "c3"]),
        "callee": """static void call_resumable_divert_callee(X86 *c) {
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0xabcd1234u;
    c->eip = 0xdeadbeefu;
}
""",
        "resumable": True,
    },
}


# Callers whose callee is a real byte-translated function rather than a host
# helper. The eager translation of the callee is shared by all four caller
# modes, so any caller-side publication/reload error stays isolated. The
# callee's RET goes through recomp_return and is accepted by the wrapper's
# ir_accept_call_return registration.
BYTE_CALL_CASES = {
    "call_flags_return_consumed": {
        # A CRT-style classifier returns ZF as well as EAX. The caller turns
        # ZF into guest memory, so canonicalizing dead return flags cannot
        # hide a lost flag result. Exercise a production SSA callee too.
        "hexes": _caller(["31c0", "680000e03f", "6a00"],
                         ["0f94c1", "0fb6c9", "894b04", "83c408", "c3"]),
        "callee_hexes": ["8b442408", "250000f07f", "3d0000f07f", "7401", "c3",
                          "8b442408", "c3"],
        "ssa_callee": True,
        "resumable": False,
    },
    "call_translated_callee": {
        # mov eax,0x11111111; push 0x22; call; pop ecx; mov [ebx],eax; ret
        # callee: mov eax,[esp+4]; add eax,0x33; fld1; ret
        "hexes": _caller(["b811111111", "6a22"], ["59", "8903", "c3"]),
        "callee_hexes": ["8b442404", "83c033", "d9e8", "c3"],
        "resumable": False,
    },
    "call_translated_ret4": {
        # mov eax,0; push 0x11223344; call; mov ebx,0x99; ret
        # callee: mov eax,[esp+4]; ret 4
        "hexes": _caller(["b800000000", "6844332211"], ["bb99000000", "c3"]),
        "callee_hexes": ["8b442404", "c20400"],
        "resumable": False,
    },
    "call_translated_loop": {
        # ecx=0; loop { call; ecx++; cmp ecx,3; jb loop } mov [ebx],ecx; ret
        # callee: inc ecx; ret
        "hexes": _loop_caller(),
        "callee_hexes": ["41", "c3"],
        "resumable": False,
    },
    "call_x87_empty_fxam": {
        # fld1; fld1; faddp; fstp [ebx+4]; call; mov [ebx+8],eax; ret
        # callee: fxam; fnstsw ax; ret -- under the MSVC convention the
        # caller's pushed-and-popped registers must still read as empty.
        "hexes": _caller(["d9e8", "d9e8", "dec1", "d95b04"], ["894308", "c3"]),
        "callee_hexes": ["d9e5", "dfe0", "c3"],
        "resumable": False,
    },
    "call_x87_join": {
        # fld1; call; test eax,eax; jz skip; fadd st0,st1; skip: fstp [ebx+4]; ret
        # callee: fld1; ret -- the caller's scalar x87 state must be
        # flushed by the call and reset again at the conditional join.
        "hexes": _caller(["d9e8"], ["85c0", "7402", "d8c1", "d95b04", "c3"]),
        "callee_hexes": ["d9e8", "c3"],
        "resumable": False,
    },
    "call_flags_inc_preserves_cf": {
        # cmp eax,ebx; call; setc al; mov [ebx],al; ret
        # callee: inc ecx; ret -- INC leaves CF, so the caller's CMP carry must
        # survive the callee's INC descriptor. A callee that overwrote the
        # caller's pending CMP descriptor would lose it.
        "hexes": _caller(["39d8"], ["0f92c0", "8803", "c3"]),
        "callee_hexes": ["41", "c3"],
        "ssa_callee": True,
        "resumable": False,
    },
    "call_flags_noflag_passthrough": {
        # cmp eax,ebx; call; setc al; mov [ebx],al; ret
        # callee: mov eax,[esp+4]; ret -- touches no flags at all, so the
        # caller's pending descriptor passes through the callee unsettled and
        # the caller settles it after the return.
        "hexes": _caller(["39d8"], ["0f92c0", "8803", "c3"]),
        "callee_hexes": ["8b442404", "c3"],
        "ssa_callee": True,
        "resumable": False,
    },
    "call_flags_return_jz": {
        # call; jz +5; mov eax,1; mov [ebx],eax; ret
        # The 006ff798 CRT classifier returns ZF; a lazy callee leaves a
        # descriptor at RET and the caller's JZ must materialise it instead of
        # reading a stale field.
        "hexes": _caller([], ["7405", "b801000000", "8903", "c3"]),
        "callee_hexes": ["8b442408", "250000f07f", "3d0000f07f", "7401", "c3",
                          "8b442408", "c3"],
        "ssa_callee": True,
        "resumable": False,
    },
}

# Generated op x width x seam matrix; kept out of the literal for readability.
BYTE_CALL_CASES.update(_flag_abi_cases())


#: Distinct synthetic targets let one generated dispatcher serve every
#: indirect fixture without cross-case callee collisions.
INDIRECT_CASES = {
    "callind_register": {
        # fld1; mov eax,0x200100; call eax; fadd st0,st1; fstp qword ptr [0x10800];
        # add eax,ebx; ret  -- live x87 spans the indirect call so the opaque
        # barrier must flush before and invalidate after the helper.
        "hexes": ["d9e8", "b800012000", "ffd0", "d8c1", "d91d00080100", "01d8", "c3"],
        "target": 0x200100,
        "callee": """static void callind_register_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0x0badf00du;
    c->r[R_EBX] ^= 0x55aa55aau;
    c->r[R_ECX] = 0x99887766u;
    c->r[R_EDX] = 0xdeadbeefu;
    c->eflags_cf = 1; c->eflags_zf = 0; c->eflags_sf = 1;
    c->eflags_of = 0; c->eflags_pf = 1; c->eflags_af = 0;
    fpush(c, 2.5);
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "callind_esp_relative": {
        # push 0x200200; call dword ptr [esp]; add esp,4; add eax,ebx; ret
        # The target load is before the call's return-address store.
        "hexes": ["6800022000", "ff1424", "83c404", "01d8", "c3"],
        "target": 0x200200,
        "callee": """static void callind_esp_relative_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0x11112222u;
    c->r[R_ESI] ^= 0x0f0f0f0fu;
    c->r[R_EDI] = 0x33334444u;
    c->eflags_zf = 1; c->eflags_cf = 0; c->eflags_sf = 0;
    fpush(c, 7.25);
    c->eip = ret;
}
""",
        "resumable": False,
    },
    "callind_resumable_normal": {
        # A resumable body whose callee resumes at the call fallthrough must
        # continue normally.
        "hexes": ["6800042000", "ff1424", "83c404", "01d8", "c3"],
        "target": 0x200400,
        "callee": """static void callind_resumable_normal_callee(X86 *c) {
    uint32_t ret = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0x55667788u;
    c->eflags_cf = 1; c->eflags_zf = 0;
    c->eip = ret;
}
""",
        "resumable": True,
    },
    "callind_resumable_divert": {
        # The callee leaves EIP elsewhere: a resumable body must bail out
        # instead of continuing at the call fallthrough.
        "hexes": ["6800032000", "ff1424", "83c404", "01d8", "c3"],
        "target": 0x200300,
        "callee": """static void callind_resumable_divert_callee(X86 *c) {
    c->r[R_ESP] += 4;
    c->r[R_EAX] = 0xabcd1234u;
    c->eip = 0xdeadbeefu;
}
""",
        "resumable": True,
    },
}


def call_sources(name, hexes, callee_addr, resumable=False, indirect=False):
    """Eager caller plus raw/scalar/strict/local IR callers and CALL fallthroughs.

    `indirect` selects the explicit production-style `recomp_call` opt-in for
    indirect CALL effects; without it those effects stay a fallback.
    """
    chunks = [bytes.fromhex(h) for h in hexes]
    image = T.Image.__new__(T.Image)
    image.base, image.data = ENTRY, b"".join(chunks)
    image.end = ENTRY + len(image.data)
    image.is_exec = lambda addr: ENTRY <= addr < image.end
    image.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    decoded = list(decode_span(image, ENTRY, "".join("%x" % len(raw) for raw in chunks)))
    previous = T.RESUMABLE_STACKS
    T.RESUMABLE_STACKS = resumable
    try:
        tr = T.Translator(image, {ENTRY, callee_addr}, SimpleNamespace(eager_flags=True))
        fn = T.Function(ENTRY, name, len(image.data), decoded)
        tr.prepare(fn, strict=True)
        eager = re.sub(r"\b(fn|body)_([0-9a-f]{8})\b", lambda m: name + "_eager_" + m[0],
                       "\n".join(tr.translate(fn)))
    finally:
        T.RESUMABLE_STACKS = previous
    eager = re.sub(r"CALL_FN\(%08x\)" % callee_addr, name + "_callee(c)", eager)
    lifter, addr, lifted = Lifter(), ENTRY, []
    for raw, ins in zip(chunks, decoded):
        lifted.append(lifter.lift(addr, raw, ins.mnem))
        addr += len(raw)
    fir = FunctionIR(ENTRY, lifted, default_successors(lifted))
    symbols = {callee_addr: name + "_callee"}
    indirect_symbol = "recomp_call" if indirect else None
    options = dict(call_symbols=symbols, indirect_call_symbol=indirect_symbol,
                   resumable_stacks=resumable)
    raw = emit(fir, name + "_ir_raw", optimize=False, **options)
    fallthroughs = [ins.addr + ins.length for ins in lifted if ins.mnem.upper() == "CALL"]
    scalar = emit(fir, name + "_ir_scalar", local_state=False, msvc_convention=False, **options)
    strict = emit(fir, name + "_ir_strict", x87_scalar_strict=True, local_state=False,
                  msvc_convention=False, **options)
    local = emit(fir, name + "_ir_local", **options)
    lazy = emit(fir, name + "_ir_lazy", lazy_nan=True, lazy_flags=True, **options)
    return eager, raw, scalar, strict, local, lazy, fallthroughs


def _eager_callee(symbol, base, hexes):
    """Translate a real byte-backed callee for the synthetic call fixtures.

    The callee is the eager comparison body (not hand-written), so its RET
    goes through ``recomp_return`` and must see a registered continuation.
    """
    chunks = [bytes.fromhex(h) for h in hexes]
    image = T.Image.__new__(T.Image)
    image.base, image.data = base, b"".join(chunks)
    image.end = base + len(image.data)
    image.is_exec = lambda addr: base <= addr < image.end
    image.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    decoded = list(decode_span(image, base, "".join("%x" % len(raw) for raw in chunks)))
    tr = T.Translator(image, {base}, SimpleNamespace(eager_flags=True))
    fn = T.Function(base, symbol, len(image.data), decoded)
    tr.prepare(fn, strict=True)
    body = "\n".join(tr.translate(fn))
    return re.sub(r"\b(fn|body)_%08x\b" % base, symbol, body)


def sources(name, hexes):
    """Decode and lift the exact same byte boundaries for all six consumers."""
    chunks = [bytes.fromhex(h) for h in hexes]
    image = T.Image.__new__(T.Image)
    image.base, image.data = ENTRY, b"".join(chunks) + TABLE_DATA.get(name, b"")
    image.end = ENTRY + len(image.data)
    image.is_exec = lambda addr: ENTRY <= addr < image.end
    image.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    decoded = list(decode_span(image, ENTRY, "".join("%x" % len(raw) for raw in chunks)))
    tr = T.Translator(image, {ENTRY}, SimpleNamespace(eager_flags=True))
    fn = T.Function(ENTRY, name, len(b"".join(chunks)), decoded)
    tr.prepare(fn, strict=True)
    eager = re.sub(r"\b(fn|body)_([0-9a-f]{8})\b", lambda m: name + "_eager_" + m[0],
                   "\n".join(tr.translate(fn)))
    lifter, addr, lifted = Lifter(), ENTRY, []
    for raw, ins in zip(chunks, decoded):
        lifted.append(lifter.lift(addr, raw, ins.mnem))
        addr += len(raw)
    if name in TABLE_DATA:
        from .cfg import table_jumps
        assert tr.jumptables, "fixture table was not decoded"
        fir = FunctionIR(ENTRY, lifted, [tr.successors(fn, i) for i in range(len(decoded))],
                         table_jumps(tr, fn))
        assert fir.tables, "fixture table jump was not admitted"
    else:
        fir = FunctionIR(ENTRY, lifted, default_successors(lifted))
    raw = emit(fir, name + "_ir_raw", optimize=False)
    scalar = emit(fir, name + "_ir_scalar", local_state=False, msvc_convention=False)
    strict = emit(fir, name + "_ir_strict", x87_scalar_strict=True, local_state=False,
                  msvc_convention=False)
    local = emit(fir, name + "_ir_local")
    lazy = emit(fir, name + "_ir_lazy", lazy_nan=True, lazy_flags=True)
    return eager, raw, scalar, strict, local, lazy


def _checked_wrapper(symbol, fallthroughs):
    """Wrap one fixture with the full-state observer and accepted returns."""
    register = "".join("ir_accept_call_return(0x%x); " % addr for addr in fallthroughs)
    return ("void %s_checked(X86 *c) { ir_observer_begin(c); %s%s(c); ir_observer_end(); }"
            % (symbol, register, symbol))


def run_checks(out, cmake, jobs):
    """Build via tools/build.py, then compare every CPU field and scratch byte.

    The five harness columns compare eager, raw (unoptimized ordered effects),
    scalar, scalar-strict and scalar/local-state with the same full-state
    obligations. The last is the production policy, including the MSVC x87
    convention, and compares with only its dead x87 fields cleared.
    """
    out.mkdir(parents=True, exist_ok=True)
    code, rows, declarations = ['#include "x86.h"',
        'void ir_observer_begin(X86 *);', 'void ir_observer_end(void);',
        'void ir_accept_call_return(uint32_t);'], [], []

    def add_case(name, eager, variants, extra=(), fallthroughs=()):
        code.extend(extra)
        code.extend([eager, *variants])
        symbols = [name + "_eager_fn_%08x" % ENTRY, name + "_ir_raw",
                   name + "_ir_scalar", name + "_ir_strict", name + "_ir_local",
                   name + "_ir_lazy"]
        for symbol in symbols:
            code.append(_checked_wrapper(symbol, fallthroughs))
        checked = [symbol + "_checked" for symbol in symbols]
        declarations.extend("void %s(X86 *);" % symbol for symbol in checked)
        rows.append("{" + ",".join(checked) + "}")

    for name, hexes in CASES.items():
        eager, raw, scalar, strict, local, lazy = sources(name, hexes)
        add_case(name, eager, [raw, scalar, strict, local, lazy])
    for name, spec in CALL_CASES.items():
        eager, raw, scalar, strict, local, lazy, returns = call_sources(
            name, spec["hexes"], CALLEE, spec["resumable"])
        add_case(name, eager, [raw, scalar, strict, local, lazy], extra=[spec["callee"]],
                 fallthroughs=returns)
    for name, spec in BYTE_CALL_CASES.items():
        eager, raw, scalar, strict, local, lazy, returns = call_sources(
            name, spec["hexes"], CALLEE, spec["resumable"])
        callee = _eager_callee(name + "_callee", CALLEE, spec["callee_hexes"])
        extra = [callee]
        if spec.get("ssa_callee"):
            lifter, addr, lifted = Lifter(), CALLEE, []
            for raw_bytes in map(bytes.fromhex, spec["callee_hexes"]):
                lifted.append(lifter.lift(addr, raw_bytes))
                addr += len(raw_bytes)
            fir = FunctionIR(CALLEE, lifted, default_successors(lifted))
            extra.append(emit(fir, name + "_callee_local"))
            # The lazy caller needs a lazy callee to exercise a descriptor the
            # callee leaves pending at its return; the local column stays eager.
            extra.append(emit(fir, name + "_callee_lazy", lazy_flags=True))
            local = local.replace(name + "_callee(c)", name + "_callee_local(c)")
            lazy = lazy.replace(name + "_callee(c)", name + "_callee_lazy(c)")
        add_case(name, eager, [raw, scalar, strict, local, lazy], extra=extra,
                 fallthroughs=returns)
    dispatch = []
    for name, spec in INDIRECT_CASES.items():
        eager, raw, scalar, strict, local, lazy, returns = call_sources(
            name, spec["hexes"], spec["target"], spec["resumable"], indirect=True)
        add_case(name, eager, [raw, scalar, strict, local, lazy],
                 extra=[spec["callee"]], fallthroughs=returns)
        dispatch.append((spec["target"], name + "_callee"))
    code.extend(['void ir_unexpected_call(uint32_t);',
                 'void ir_indirect_dispatch(X86 *c, uint32_t target) {',
                 '    switch (target) {']
                + ['    case 0x%x: %s(c); return;' % (t, s) for t, s in dispatch]
                + ['    default: ir_unexpected_call(target); }',
                   '}'])
    declarations.extend([
        'static const char *mode_names[] = {"eager", "raw", "scalar", "strict", "local", "lazy"};',
        'static const unsigned normalize_empty_mask = 0, required_match_mask = 62;',
        # The production local column uses ir_ssa_msvc_convention; the lazy
        # column adds ir_ssa_x87_lazy_nan on top of it.
        '#define FIXTURE_CONVENTION_MASK 48u',
        '#define FIXTURE_SCRATCH_SIZE 2048',
        '#define FIXTURE_CUSTOM_INPUTS 1',
        '#define FIXTURE_SETUP(c, n) do { (c)->r[R_ESP] = 0x10100; (c)->fs_base = 0x10600; '
        'wr32(0x10100, GUEST_RETURN_SENTINEL); '
        '(c)->r[R_ECX] = ((c)->r[R_ECX] & 0xffffff00u) | ((n) & 255u); '
        '(c)->eflags_cf = ((n) >> 8) & 1; (c)->eflags_zf = ((n) >> 9) & 1; '
        '(c)->eflags_sf = ((n) >> 10) & 1; (c)->eflags_of = ((n) >> 11) & 1; '
        '(c)->eflags_pf = ((n) >> 12) & 1; (c)->eflags_af = ((n) >> 13) & 1; } while (0)',
        '#define FIXTURE_RESET(c) ((void)0)', '#define FIXTURE_BEFORE(mode) ((void)0)',
        '#define FIXTURE_AFTER(mode) ((void)0)',
        'void ir_observe_store(uint32_t, uint32_t, uint64_t);',
        'void ir_observer_compare(unsigned, int);',
        '#define FIXTURE_WATCH_HIT(a, n, v) ir_observe_store(a, n, v)',
        # Performance-mode scalar x87 (scalar, local and lazy columns) publishes
        # no x87 state at guest stores, and local state defers GPRs/flags except
        # ESP/EBP/EIP there; strict and raw keep complete snapshots.
        '#define FIXTURE_AFTER_STATE(mode, c) ir_observer_compare(mode, ((mode) == 4 || (mode) == 5) ? 2 : (mode) == 2)',
        'static const char *case_names[] = {'
        + ','.join('"%s"' % name
                   for name in list(CASES) + list(CALL_CASES) + list(BYTE_CALL_CASES)
                   + list(INDIRECT_CASES))
        + '};',
        'static void (*functions[][6])(X86 *) = {' + ','.join(rows) + '};',
    ])
    (out / "generated.c").write_text("\n".join(code) + "\n")
    (out / "fixtures.h").write_text("\n".join(declarations) + "\n")
    subprocess.run([cmake, "-S", str(HERE / "checks"), "-B", str(out),
                    "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                    "-DKIT_RUNTIME=%s" % (KIT / "runtime")], check=True)
    subprocess.run([cmake, "--build", str(out), "--parallel", str(jobs)], check=True)
    for target in ("ir_ssa_checks", "ir_ssa_null_checks"):
        result = subprocess.run([str(out / target)], capture_output=True, text=True)
        (out / ("results.txt" if target == "ir_ssa_checks" else "null-results.txt")).write_text(result.stdout + result.stderr)
        result.check_returncode()
        print(target + ":")
        print(result.stdout, end="")
    print("Artifacts:", out)
