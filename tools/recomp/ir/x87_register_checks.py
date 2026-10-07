"""Byte-backed x87 register-run cases for the native full-state checks.

``native_checks.py`` merges ``CASES`` into its fixture map. Each tuple is the
exact instruction stream at synthetic guest base ``0x100000``; the fixture
supplies ``ESI=0x10000``, ``EDI=0x10040`` and an output address in ``EBX``, so
the scratch region and every CPU field, including empty-slot residue, are
compared with eager C. They stress the scalar tracker's deferred state:
popped dirty residue, ``FXCH``/``FLD`` outside the recent slots, physical
wraparound, a watched integer store between deferred arithmetic, and dirty
state at a forward join or loop backedge.

Encodings are the ordinary 32-bit forms: ``d9e8`` FLD1, ``dec1`` FADDP
ST(1),ST(0), ``d9cb`` FXCH ST(3), ``d9c7`` FLD ST(7), ``d95b04`` FSTP dword
[EBX+4], ``8903`` MOV [EBX],EAX, ``a901000000`` TEST EAX,1, ``7404`` JZ +4,
``b903000000`` MOV ECX,3, ``49`` DEC ECX, ``75f7`` JNZ -9, ``c3`` RET.
"""

CASES = {
    # Eight pushes overwrite every incoming physical residue, then nine pops
    # revisit the wrapped slots. No stale scalar/metadata alias may survive.
    "x87_scalar_full_wrap": (
        *(["d9e8"] * 8), *(["ddd8"] * 9), "8903", "c3",
    ),
    # A qword FILD above 2^53 retains bits through copies, exchanges and pops.
    "x87_scalar_exact_copy": (
        "df2e", "d9c0", "d9e8", "d9ca", "ddda", "df3b", "8903", "c3",
    ),
    # Noncommutative register arithmetic and a memory read after dirty state.
    "x87_scalar_reverse_load": (
        "d906", "d9e8", "def1", "d806", "d9e0", "8903", "d95b04", "c3",
    ),
    # Rounded float store, then a status read and a full-CPU integer watcher.
    "x87_scalar_store_status": (
        "dd06", "d95b04", "dfe0", "8903", "c3",
    ),
    # A CW change cuts the local environment; subsequent precision must reload.
    "x87_scalar_control_cut": (
        "d9e8", "d9e8", "dec1", "d92f", "d9e8", "dec1", "8903", "c3",
    ),
    # Unconsumed flags at reads are deferred, but the final watcher sees them.
    "ssa_local_read_store": (
        "83c007", "8b16", "83c001", "8b4e04", "83c002", "8903", "c3",
    ),
    # Deferred arithmetic result popped: the residue must match eager.
    "x87_run_dirty_pop_residue": (
        "d9e8",    # FLD1
        "d9e8",    # FLD1
        "dec1",    # FADDP ST(1), ST(0)  -> deferred result
        "dec1",    # FADDP ST(1), ST(0)  -> pops the dirty residue
        "c3",      # RET
    ),
    # FXCH ST(3) reaches a slot below the recent window.
    "x87_run_outside_fxch": (
        "d9e8",    # FLD1
        "d9e8",    # FLD1
        "d9cb",    # FXCH ST(3)
        "d9e8",    # FLD1
        "d95b04",  # FSTP dword [EBX+4]
        "c3",
    ),
    # FLD ST(7) wraps the physical register file.
    "x87_run_st7_wrap": (
        "d9e8",    # FLD1
        "d9e8",    # FLD1
        "d9c7",    # FLD ST(7)
        "d9e8",    # FLD1
        "d95b04",  # FSTP dword [EBX+4]
        "c3",
    ),
    # Watched integer store between deferred arithmetic.
    "x87_run_integer_watched_store": (
        "d9e8",    # FLD1
        "d9e8",    # FLD1
        "dec1",    # FADDP ST(1), ST(0)  -> dirty
        "8903",    # MOV [EBX], EAX      -> full-CPU store observation
        "d9e8",    # FLD1
        "dec1",    # FADDP ST(1), ST(0)
        "d95b04",  # FSTP dword [EBX+4]
        "c3",
    ),
    # Conditional branch with dirty state and a forward join.
    "x87_run_branch_join": (
        "d9e8",        # FLD1
        "d9e8",        # FLD1
        "a901000000",  # TEST EAX, 1
        "7404",        # JZ +4: skip both popping adds
        "dec1",        # FADDP ST(1), ST(0)  -> dirty
        "dec1",        # FADDP ST(1), ST(0)  -> pops dirty residue
        "d95b04",      # FSTP dword [EBX+4] (join)
        "c3",
    ),
    # Loop backedge; the header is a join.
    "x87_run_backedge": (
        "b903000000",  # MOV ECX, 3
        "d9e8",        # FLD1                <- loop top at +5
        "d9e8",        # FLD1
        "dec1",        # FADDP ST(1), ST(0)  -> dirty
        "49",          # DEC ECX
        "75f7",        # JNZ -9 -> loop top
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
    # Carried value across a forward join: path A loads and adds a second
    # value while path B leaves a single register. The join must carry the
    # union of both live-slot sets.
    "x87_carry_forward_join": (
        "d9e8",        # FLD1
        "a901000000",  # TEST EAX, 1
        "7404",        # JZ +4 -> FSTP
        "d906",        # FLD dword [ESI]
        "d8c1",        # FADD ST(0), ST(1)
        "d95b04",      # FSTP dword [EBX+4] (join)
        "c3",
    ),
    # Different dirty parts per predecessor: path A creates an exact-integer
    # slot while path B only has plain values; the join must union exact/bits
    # with the value-only slot.
    "x87_carry_dirty_parts": (
        "d9e8",        # FLD1
        "d9e8",        # FLD1
        "a901000000",  # TEST EAX, 1
        "7402",        # JZ +2 -> FXCH
        "df06",        # FILD dword [ESI]
        "d9c9",        # FXCH ST(1) (join)
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
    # FXCH inside a loop body: the parallel-copy shape must survive the
    # backedge and the two registers must not alias during materialization.
    "x87_carry_fxch_loop": (
        "b902000000",  # MOV ECX, 2
        "d9e8",        # FLD1                <- loop top
        "d9e8",        # FLD1
        "d9c9",        # FXCH ST(1)
        "49",          # DEC ECX
        "75f7",        # JNZ -9 -> loop top
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
    # Live value and popped residue carried on a loop backedge.
    "x87_carry_backedge_live": (
        "b903000000",  # MOV ECX, 3
        "d9e8",        # FLD1                <- loop top
        "d9e8",        # FLD1
        "dec1",        # FADDP ST(1), ST(0)
        "49",          # DEC ECX
        "75f7",        # JNZ -9
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
    # Cull-style FCOMP / FNSTSW AX / TEST AH,1 / JE with a live value still on
    # the stack at the branch.
    "x87_carry_fcomp_branch": (
        "d9e8",        # FLD1
        "d906",        # FLD dword [ESI]
        "d81e",        # FCOMP dword [ESI]
        "dfe0",        # FNSTSW AX
        "f6c401",      # TEST AH, 1
        "7402",        # JE +2 -> FSTP
        "dec1",        # FADDP ST(1), ST(0)
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
    # Popped dirty registers must still be retagged empty after a carried
    # join; path A pushes again while path B leaves the residue.
    "x87_carry_pop_tag": (
        "d9e8",        # FLD1
        "d9e8",        # FLD1
        "d9e8",        # FLD1
        "dec1",        # FADDP ST(1), ST(0)  -> result, one residue
        "dec1",        # FADDP ST(1), ST(0)  -> result, two residues
        "a901000000",  # TEST EAX, 1
        "7402",        # JZ +2 -> FSTP
        "d9e8",        # FLD1 (path A overwrites the popped slot)
        "d95b04",      # FSTP dword [EBX+4] (join)
        "c3",
    ),
    # A branch whose join target needs a slot only another predecessor
    # dirtied (FXCH reaches the entry ST0). Materializing it for the join must
    # not disturb the check of the other, fallthrough target (CRT __nan2 shape).
    "x87_carry_join_reads_other_slot": (
        "d95b08",      # FSTP dword [EBX+8] (pop, then reload at the same depth)
        "d906",        # FLD dword [ESI]
        "a901000000",  # TEST EAX, 1
        "740b",        # JZ +11 -> FSTP (join)
        "d9c9",        # FXCH ST(1)
        "a902000000",  # TEST EAX, 2
        "7402",        # JZ +2 -> FSTP (join)
        "d9e0",        # FCHS
        "d95b04",      # FSTP dword [EBX+4] (join)
        "c3",
    ),
    # A loop that pushes every iteration: the join window must overflow and
    # fall back to the exact flush/reset instead of growing forever.
    "x87_carry_unbalanced_loop": (
        "b903000000",  # MOV ECX, 3
        "d9e8",        # FLD1                <- loop top
        "49",          # DEC ECX
        "75fb",        # JNZ -5 -> loop top
        "d95b04",      # FSTP dword [EBX+4]
        "c3",
    ),
}
