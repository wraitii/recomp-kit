"""Native full-state CASES for the signed integer SSA cluster.

Integration (applied by the reviewer, not committed here): merge these into
``native_checks.CASES`` before ``run_checks`` builds its fixtures, for example

    from .integer_extra_checks import CASES as EXTRA_CASES
    CASES = {**CASES, **EXTRA_CASES}

The runner decodes these exact byte boundaries for both the eager translator and
the patched IR emitter, then compares every CPU field and 2 KiB of scratch over
24576 randomized inputs. The fixtures set their own divisor where a random
EDX:EAX would almost always fault, so valid signed quotient/remainder paths get
real coverage; the ``idiv_*_fault`` fixtures force the checked error seam and
compare its CPU/scratch snapshot. This file imports nothing and has no side
effects.
"""

CASES = {
    # CDQ and the IMUL forms only need admission; SLEIGH p-code is already
    # expressible. All widths and register/memory sources are exercised.
    "cdq": ("99", "c3"),
    "imul_two_register": ("0fafd0", "c3"),          # imul edx,eax
    "imul_two_memory": ("0faf5304", "c3"),           # imul edx,[ebx+4]
    "imul_three_immediate": ("6bc00a", "c3"),        # imul eax,eax,0xa
    "imul_one_register": ("f7e9", "c3"),             # imul ecx
    "imul_one_byte": ("f6e9", "c3"),                 # imul cl
    # NEG defines AF.
    "neg_register": ("f7d8", "c3"),
    "neg_byte": ("f6d9", "c3"),
    "neg_word": ("66f7d8", "c3"),
    # Signed IDIV through the checked idiv32 helper. CDQ makes EDX the sign
    # extension of the random EAX, and the divisor is fixed, so the quotient
    # and remainder are valid and cover both operand signs.
    "idiv_cdq_pos": ("b903000000", "99", "f7f9", "c3"),      # /3
    "idiv_cdq_neg": ("b9f9ffffff", "99", "f7f9", "c3"),      # /-7
    "idiv_cdq_one": ("b901000000", "99", "f7f9", "c3"),      # /1
    "idiv_cdq_neg_one": ("b9ffffffff", "99", "f7f9", "c3"),  # /-1 (faults only at INT32_MIN)
    "idiv_cdq_pow2": ("b908000000", "99", "f7f9", "c3"),      # /8
    "idiv_cdq_min": ("b900000080", "99", "f7f9", "c3"),       # /INT32_MIN
    "idiv_cdq_memory": ("c70307000000", "99", "f73b", "c3"),  # [ebx]=7; /[ebx]
    "idiv_zero": ("31c9", "f7f9", "c3"),                     # xor ecx,ecx
    "idiv_min_over_neg_one": ("b800000000", "ba00000080", "b9ffffffff",
                              "f7f9", "c3"),
    "idiv_quotient_overflow": ("b800000000", "ba00000001", "b901000000",
                               "f7f9", "c3"),
}

# SAR count edges for each destination width: zero, one, width-1, width and the
# largest masked count, plus CL. These are the cases where the port's sar*_f
# clamp and OF recipe diverge from a naive shift.
for _name, _bytes in (
    ("sar_byte_0", "c0f800"), ("sar_byte_1", "c0f801"), ("sar_byte_7", "c0f807"),
    ("sar_byte_8", "c0f808"), ("sar_byte_31", "c0f81f"), ("sar_byte_cl", "d2f8"),
    ("sar_word_0", "66c1f800"), ("sar_word_1", "66c1f801"), ("sar_word_15", "66c1f80f"),
    ("sar_word_16", "66c1f810"), ("sar_word_31", "66c1f81f"), ("sar_word_cl", "66d3f8"),
    ("sar_dword_0", "c1f800"), ("sar_dword_1", "d1f8"), ("sar_dword_31", "c1f81f"),
    ("sar_dword_cl", "d3f8"),
):
    CASES[_name] = (_bytes, "c3")

# The full SETcc condition set (0f 90+cc /0 AL, byte destination).
for _cc, _opcode in (
    ("seto", 0x90), ("setno", 0x91), ("setb", 0x92), ("setae", 0x93),
    ("sete", 0x94), ("setne", 0x95), ("setbe", 0x96), ("seta", 0x97),
    ("sets", 0x98), ("setns", 0x99), ("setp", 0x9a), ("setnp", 0x9b),
    ("setl", 0x9c), ("setge", 0x9d), ("setle", 0x9e), ("setg", 0x9f),
):
    CASES[_cc + "_register"] = ("0f%02xc0" % _opcode, "c3")

# A memory destination for the most common conditions, including a store that
# must be observed.
CASES["sete_memory"] = ("0f9413", "c3")
CASES["setne_memory"] = ("0f9513", "c3")
