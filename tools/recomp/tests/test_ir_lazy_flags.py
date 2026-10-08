"""Lazy arithmetic flag descriptors: emitted C shape and the seam touch rule.

`emit(..., lazy_flags=True)` publishes a pending ADD/SUB/LOGIC/INC/DEC
descriptor at call/return seams instead of writing the flag fields. A body that
writes any flag state must settle a descriptor left by its caller or callee at
entry and after calls; a body that never touches flags passes the descriptor
through untouched. These are structural checks over byte-backed bodies; the
executed full-state comparison lives in ``ir/native_checks.py``.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter
from ir.summary import FunctionIR, default_successors
from ir.emit_c import emit

LIFTER = Lifter()
ENTRY = 0x1000
TARGET = 0x2000


def function(*hexes, entry=ENTRY):
    at, insns = entry, []
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return FunctionIR(entry, insns, default_successors(insns))


def rel32(addr, target=TARGET):
    value = (target - (addr + 5)) & 0xffffffff
    return "e8" + "".join("%02x" % ((value >> (8 * n)) & 0xff) for n in range(4))


def caller(*body):
    """Body bytes with a CALL to TARGET placed after them, then RET."""
    at = ENTRY + sum(len(bytes.fromhex(h)) for h in body)
    return function(*body, rel32(at), "c3")


def fast(fir, **options):
    out = emit(fir, "test_fn", lazy_flags=True, call_symbols={TARGET: "callee"}, **options)
    return out.split("#else", 1)[1] if "#else" in out else out


#: (hex, expected cc_op, expected cc_size, expected cc_mask)
RETURN_SEAMS = [
    ("38d8", "X86_CC_SUB", 1, 0x3F),    # cmp al,bl
    ("6639d8", "X86_CC_SUB", 2, 0x3F),  # cmp ax,bx
    ("39d8", "X86_CC_SUB", 4, 0x3F),    # cmp eax,ebx
    ("28d8", "X86_CC_SUB", 1, 0x3F),    # sub al,bl
    ("6629d8", "X86_CC_SUB", 2, 0x3F),  # sub ax,bx
    ("29d8", "X86_CC_SUB", 4, 0x3F),    # sub eax,ebx
    ("00d8", "X86_CC_ADD", 1, 0x3F),    # add al,bl
    ("6601d8", "X86_CC_ADD", 2, 0x3F),  # add ax,bx
    ("01d8", "X86_CC_ADD", 4, 0x3F),    # add eax,ebx
    ("84d8", "X86_CC_LOGIC", 1, 0x3B),  # test al,bl
    ("6685d8", "X86_CC_LOGIC", 2, 0x3B),  # test ax,bx
    ("85d8", "X86_CC_LOGIC", 4, 0x3B),  # test eax,ebx
    ("fec0", "X86_CC_INC", 1, 0x3E),    # inc al
    ("6640", "X86_CC_INC", 2, 0x3E),    # inc ax
    ("40", "X86_CC_INC", 4, 0x3E),      # inc eax
    ("fec8", "X86_CC_DEC", 1, 0x3E),    # dec al
    ("6648", "X86_CC_DEC", 2, 0x3E),    # dec ax
    ("48", "X86_CC_DEC", 4, 0x3E),      # dec eax
]


@pytest.mark.parametrize("code,op,size,mask", RETURN_SEAMS)
def test_return_seam_writes_descriptor(code, op, size, mask):
    body = fast(function(code, "c3"))
    assert "c->cc_op = %s;" % op in body
    assert "c->cc_size = %du;" % size in body
    assert "c->cc_mask = 0x%xu;" % mask in body
    # The covered fields are left to x86_cc_settle, not written inline.
    assert "c->eflags_" not in body


def test_flags_dead_body_has_no_descriptor():
    # mov eax,ebx; ret -- no flag writer, so nothing is deferred.
    body = fast(function("89d8", "c3"))
    assert "c->cc_op" not in body
    assert "x86_cc_settle" not in body


def test_touch_rule_settles_at_entry_and_after_call():
    # cmp eax,ebx; setz al; mov [ebx],al; call; ret
    body = fast(caller("39d8", "0f94c0", "8803"))
    # The CMP kills all six flags before the call, so the entry settle is a
    # drop; the callee's flags are reloaded and published after the call, so
    # the post-call settle stays.
    assert body.count("x86_cc_drop(c);") == 1
    assert body.count("x86_cc_settle(c);") == 1
    # The drop precedes the first body statement; the settle follows callee.
    assert body.index("x86_cc_drop(c);") < body.index("c->cc_op = X86_CC_SUB;")
    assert "callee(c);\nx86_cc_settle(c);" in body
    # The producer's descriptor is published before the call.
    assert body.index("c->cc_op = X86_CC_SUB;") < body.index("callee(c);")


def test_nonflag_body_passes_descriptor_through():
    # call; ret -- no flag write and no flag read, so no settle and no
    # descriptor; a caller's pending descriptor is left untouched.
    body = fast(caller())
    assert "x86_cc_settle" not in body
    assert "c->cc_op" not in body


def test_direct_field_write_invalidates_pending_descriptor():
    # After a call the caller reloads the callee's flags and publishes them as
    # fields at RET; a direct field write must clear any older pending
    # descriptor rather than let it overwrite the reloaded values.
    body = fast(caller("39d8", "0f94c0", "8803"))
    assert "c->eflags_cf = (uint32_t)" in body
    assert body.index("c->eflags_cf = (uint32_t)") < body.index("c->cc_op = X86_CC_NONE;")


def test_lazy_flags_flag_off_is_the_eager_emission():
    fir = function("39d8", "c3")
    eager = emit(fir, "test_fn")
    lazy = emit(fir, "test_fn", lazy_flags=True)
    assert eager != lazy
    assert "c->cc_op" not in eager
    assert "c->cc_op" in lazy
