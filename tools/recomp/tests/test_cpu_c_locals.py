"""Whole-function CPU locals and conservative observation boundaries."""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
from cpu_locals import transparent, after_initialization, INITIALIZATION_BEGIN, INITIALIZATION_END


def translate(lines, enabled=True, entries=(), x87=False):
    insns = T.parse_listing_text("\n".join(f"{0x100000+i:08x}  {s}" for i, s in enumerate(lines)))
    fn = T.Function(0x100000, "synthetic", len(insns), insns)
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=True, cpu_locals=enabled, x87_locals=x87))
    tr.prepare(fn)
    return "\n".join(tr.translate(fn, entries)), tr


ARITH = ["MOV EAX,ECX", "ADD EAX,1", "ADD ECX,EAX", "TEST EAX,ECX"]


def test_cfg_values_and_fault_diagnostics():
    body, tr = translate(ARITH + ["JNZ 0x00100001", "RET"])
    assert "uint32_t cpu_r0_value_" in body
    assert "(*cpu_r0_ptr_) +=" not in body  # baseline uses wrapping r_ temporaries
    assert "goto L_00100001" in body
    assert "cpu_r4" not in body and "cpu_r5" not in body
    assert "RECOMP_NULL_CHECKS" in body
    assert body.index("c->r[0] = (*cpu_r0_ptr_);") < body.index("00100005 RET")
    assert tr.stats["_cpu_local_functions"] == 1
    assert "cpu_r0" not in translate(ARITH + ["RET"], enabled=False)[0]


def test_entry_validation_skips_only_marked_host_initialization():
    body, _ = translate(ARITH + ["RET"])
    lines = [line for line in body.splitlines()[1:] if line.strip()]
    assert after_initialization(lines)[0].strip() == "L_00100000: ;"
    bad = [INITIALIZATION_BEGIN, "guest_operation();"]
    assert after_initialization(bad) == bad
    ordinary = ["/* unrelated comment */", INITIALIZATION_END, "guest_operation();"]
    assert after_initialization(ordinary) == ordinary


def test_calls_publish_and_reload_without_abi_clobber_assumptions():
    body, _ = translate(ARITH + ["JZ 0x00100005", "CALL 0x00200000", *ARITH, "RET"])
    call = body.index("CALL_FN(00200000)")
    assert body.rfind("c->r[0] = (*cpu_r0_ptr_);", 0, call) >= 0
    assert body.index("(*cpu_r0_ptr_) = c->r[0];", call) > call
    assert body.rfind("c->eflags_zf = (*cpu_eflags_zf_ptr_);", 0, call) >= 0


def test_partial_registers_keep_original_masks():
    body, _ = translate(["MOV EAX,ECX", "MOV AH,BL", "MOV AL,DL", "ADD AX,CX", "RET"])
    assert "(*cpu_r0_ptr_) & 0xffff00ffu" in body
    assert "(*cpu_r0_ptr_) & 0xffffff00u" in body
    assert "(*cpu_r0_ptr_) & 0xffff0000u" in body


def test_alternate_entry_initializes_before_dispatch():
    body, _ = translate(ARITH + ["RET"], entries=(0x100002,))
    assert body.index("uint32_t cpu_r0_value_") < body.index("switch (entry_)")
    assert body.count("uint32_t cpu_r0_value_") == 1


def test_x87_scopes_do_not_cut_cpu_values():
    body, tr = translate(["MOV EAX,ECX", *["ADD EAX,1"] * 9, "FLD float ptr [ESI]",
                         "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                         "FSTP float ptr [EBX]", "ADD EAX,ECX", "RET"], x87=True)
    assert tr.stats["_cpu_local_functions"] == 1
    start, end = body.index("local x87"), body.index("0010000e ADD")
    assert "c->r[0] = (*cpu_r0_ptr_);" not in body[start:end]


def test_opaque_helpers_and_escapes_are_not_guessed():
    assert transparent(["shl32_f(c, c->r[0], 1);"])
    assert not transparent(["mul32(c, c->r[0]);"])
    assert not transparent(["opaque(&c->r[0]);"])
    assert not transparent(["c->r[i] = 1;"])
    assert not transparent(["CALL_FN(00200000);"])
    assert not transparent(["recomp_jump(c, 0x200000);"])
    assert transparent(["fcom(c, ST(c, 0), rdf32(c->r[0]));"])
    assert transparent(["fdivz(c, ST(c, 0), rdf32(c->r[0]));"])


def test_unconsumed_flags_do_not_extend_native_live_ranges():
    body, _ = translate(ARITH + ["RET"])
    assert "cpu_eflags_of_ptr_" not in body
    assert "c->eflags_of =" in body


def test_float_dominated_leaf_keeps_comparison_shape():
    body, tr = translate(["MOV EAX,ECX", "FNSTSW AX", "FLD float ptr [ESI]",
                          *["FMUL float ptr [EDI]"] * 20, "FSTP float ptr [EBX]", "RET"], x87=True)
    assert tr.stats["_cpu_local_functions"] == 0
    assert "cpu_r0_ptr_" not in body


def test_gap_and_final_fallthrough_publish():
    body, _ = translate(ARITH)
    assert body.index("c->r[0] = (*cpu_r0_ptr_);") < body.index("recomp_jump")
    insns = T.parse_listing_text("\n".join(
        f"{addr:08x}  {s}" for addr, s in zip(
            (0x100000, 0x100001, 0x100002, 0x100010), ARITH)))
    fn = T.Function(0x100000, "gap", 0x11, insns)
    tr = T.Translator(None, {fn.addr}, SimpleNamespace(
        eager_flags=True, cpu_locals=True, x87_locals=False))
    tr.prepare(fn)
    body = "\n".join(tr.translate(fn))
    assert body.index("c->r[0] = (*cpu_r0_ptr_);") < body.index("recomp_jump")
