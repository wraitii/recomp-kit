"""SSA regressions over instruction bytes and independently expected results."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ir.lift import Lifter, Insn, Op
from ir.summary import FunctionIR, default_successors
from ir.ssa import SSAError, MEMORY, build
from ir.emit_c import codegen_ir, emit
from ir.simplify import canonicalize, simplify
from ir.publication import plan

LIFTER = Lifter()


def function(*hexes):
    at, insns = 0x1000, []
    for h in hexes:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(at, raw))
        at += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


def execute(s, registers, memory=None, publications=None, read_fields=None):
    """Small test interpreter; expected machine results are asserted separately."""
    values, memory = {}, dict(memory or {})

    def read(v):
        v = s.resolve(v)
        return v.data if v.opc in ("CONST", "TARGET") else values[v.id]

    for v in s.values:
        if v.opc == "INPUT":
            values[v.id] = sum(registers.get(v.data[1] + n, 0) << (8 * n)
                               for n in range(v.size)) if v.size else 0
    for v in getattr(s, "entry_ops", ()):
        if s.resolve(v) is v:
            values[v.id] = (read(v.args[0]) >> (8 * v.data)) & 255
    published = dict(registers)

    def observe(v, state):
        if publications is None:
            return
        for key in publications[v.id]:
            published[key[1]] = read(state[key])
        # Every field must match, even if the plan omitted its assignment.
        for key, value in state.items():
            if v.opc == "LOAD" and read_fields is not None and key not in read_fields:
                continue
            if key != MEMORY:
                assert published.get(key[1], 0) == read(value), (v.id, key)
    block, previous = s.entry, -1
    for _ in range(1000):
        b = s.blocks[block]
        pending = {v.id: read(v.args[v.data[1].index(previous)]) for v in b.phis
                   if s.resolve(v) is v}
        values.update(pending)
        target = None
        for v in b.ops:
            a = [read(arg) for arg in v.args]
            opc = v.opc
            if opc in ("LOAD", "STORE"):
                observe(v, b.snapshots[v.id])
            if opc == "PACK":
                r = sum(value << (n * 8) for n, value in enumerate(a))
            elif opc == "BYTE":
                r = a[0] >> (v.data * 8)
            elif opc in ("COPY", "INT_ZEXT"):
                r = a[0]
            elif opc == "INT_SEXT":
                bits = v.args[0].size * 8
                r = a[0] - (1 << bits) if a[0] & (1 << (bits - 1)) else a[0]
            elif opc == "INT_ADD":
                r = a[0] + a[1]
            elif opc == "INT_SUB":
                r = a[0] - a[1]
            elif opc == "INT_XOR":
                r = a[0] ^ a[1]
            elif opc == "INT_AND":
                r = a[0] & a[1]
            elif opc == "INT_OR":
                r = a[0] | a[1]
            elif opc == "INT_MULT":
                r = a[0] * a[1]
            elif opc == "BOOL_OR":
                r = bool(a[0] or a[1])
            elif opc == "BOOL_XOR":
                r = bool(a[0]) != bool(a[1])
            elif opc == "BOOL_NEGATE":
                r = not a[0]
            elif opc == "INT_RIGHT":
                r = 0 if a[1] >= v.args[0].size * 8 else a[0] >> a[1]
            elif opc == "INT_LEFT":
                r = 0 if a[1] >= v.args[0].size * 8 else a[0] << a[1]
            elif opc in ("INT_EQUAL", "INT_NOTEQUAL", "INT_LESS"):
                r = {"INT_EQUAL": a[0] == a[1], "INT_NOTEQUAL": a[0] != a[1],
                     "INT_LESS": a[0] < a[1]}[opc]
            elif opc in ("INT_SLESS", "INT_SCARRY", "INT_SBORROW", "INT_CARRY"):
                bits = v.args[0].size * 8
                sign = 1 << (bits - 1)
                signed = lambda x: x - (1 << bits) if x & sign else x
                if opc == "INT_SLESS":
                    r = signed(a[0]) < signed(a[1])
                elif opc == "INT_CARRY":
                    r = a[0] + a[1] >= 1 << bits
                else:
                    result = signed(a[0]) + signed(a[1]) if opc == "INT_SCARRY" else signed(a[0]) - signed(a[1])
                    r = not -sign <= result < sign
            elif opc == "POPCOUNT":
                r = bin(a[0]).count("1")
            elif opc == "LOAD":
                r = sum(memory.get(a[1] + n, 0) << (n * 8) for n in range(v.size))
            elif opc == "STORE":
                for n in range(v.args[2].size):
                    memory[a[1] + n] = (a[2] >> (n * 8)) & 255
                r = 0
            elif opc == "MEMORY":
                r = 0
            elif opc == "CBRANCH":
                addr = a[0] if a[1] else b.insn.addr + b.insn.length
                target = next(i for i, other in s.blocks.items() if other.insn.addr == addr)
                r = 0
            elif opc == "BRANCH":
                target = next(i for i, other in s.blocks.items() if other.insn.addr == a[0])
                r = 0
            elif opc == "RETURN":
                observe(v, b.exit)
                return {k: read(v) for k, v in b.exit.items()}, memory
            else:
                raise AssertionError(opc)
            values[v.id] = r & ((1 << (v.size * 8)) - 1) if v.size else 0
        if target is None:
            target = next(i for i, other in s.blocks.items()
                          if other.insn.addr == b.insn.addr + b.insn.length)
        previous, block = block, target
    raise AssertionError("interpreter did not terminate")


def initial(**regs):
    result = {}
    for name, value in regs.items():
        _, off, size = LIFTER.register(name)
        result.update({off + n: (value >> (8 * n)) & 255 for n in range(size)})
    return result


def reg(state, name):
    _, off, size = LIFTER.register(name)
    return sum(state[("register", off + n)] << (n * 8) for n in range(size))


def test_partial_register_writes_keep_other_lanes_and_read_old_source():
    # mov al,ah; mov ah,7f; mov edx,eax; ret
    s = build(function("8ac4", "b47f", "8bd0", "c3"))
    out, _ = execute(s, initial(EAX=0x1234abcd, ESP=0x8000))
    assert reg(out, "EAX") == reg(out, "EDX") == 0x12347fab


def test_loop_to_entry_retains_virtual_entry_inputs_and_parallel_phi_values():
    # xchg eax,edx (SLEIGH copies); add ecx,1; cmp ecx,4; jb entry; ret
    f = function("92", "83c101", "83f904", "72f7", "c3")
    s = build(f)
    out, _ = execute(s, initial(EAX=11, EDX=22, ECX=1, ESP=0x8000))
    assert reg(out, "EAX") == 22 and reg(out, "EDX") == 11
    assert reg(out, "ECX") == 4
    assert any(v.opc == "PHI" and s.resolve(v) is v and -1 in v.data[1]
               for v in s.blocks[s.entry].phis)


@pytest.mark.parametrize("start,expected,af,cf,of", [
    (0xf, 0x10, 1, 0, 0), (0xffffffff, 0, 1, 1, 0),
    (0x7fffffff, 0x80000000, 1, 0, 1), (0, 1, 0, 0, 0)])
def test_codegen_add_captures_old_operands_and_preserves_wrapping_flags(start, expected, af, cf, of):
    f = codegen_ir(function("83c001", "c3"), LIFTER)
    out, _ = execute(build(f), initial(EAX=start, ESP=0x8000))
    assert reg(out, "EAX") == expected
    assert (reg(out, "AF"), reg(out, "CF"), reg(out, "OF")) == (af, cf, of)


def test_folded_same_operand_cmp_still_defines_af():
    out, _ = execute(build(codegen_ir(function("39c0", "c3"), LIFTER)),
                     initial(EAX=0xff, AF=1, ESP=0x8000))
    assert reg(out, "AF") == 0 and reg(out, "ZF") == 1


def test_memory_token_orders_aliasing_store_load_and_return_fetch():
    f = function("8901", "8b11", "c3")  # mov [ecx],eax; mov edx,[ecx]; ret
    s = build(f)
    effects = [v for b in s.blocks.values() for v in b.ops if v.opc in ("LOAD", "STORE")]
    assert len(effects) == 3
    for earlier, later in zip(effects, effects[1:]):
        token = s.resolve(later.args[0])
        assert token.opc == "MEMORY" and token.args == (earlier,)
    out, memory = execute(s, initial(EAX=0xfedcba98, ECX=0x9000, ESP=0x8000))
    assert reg(out, "EDX") == 0xfedcba98
    assert [memory[0x9000 + n] for n in range(4)] == [0x98, 0xba, 0xdc, 0xfe]


def test_unique_storage_aliases_bytes_but_never_leaks_between_instructions():
    u, sub, eax = ("unique", 0x20, 4), ("unique", 0x21, 1), LIFTER.register("EAX")
    ins = Insn(0x1000, 1, "synthetic", [Op("COPY", u, [("const", 0x12345678, 4)]),
               Op("COPY", sub, [("const", 0xab, 1)]), Op("COPY", eax, [u])], 0, False, False, [])
    ret = LIFTER.lift(0x1001, bytes.fromhex("c3"))
    out, _ = execute(build(FunctionIR(0x1000, [ins, ret], [[1], []])), initial(ESP=0x8000))
    assert reg(out, "EAX") == 0x1234ab78
    ret.ops.insert(0, Op("COPY", eax, [u]))
    with pytest.raises(SSAError, match="read before definition"):
        build(FunctionIR(0x1000, [ins, ret], [[1], []]))


@pytest.mark.parametrize("hexes", [("d901", "c3"), ("e8fb0f0000", "c3"),
                                   ("ffd0", "c3"), ("f3a4", "c3"), ("ffe0",)])
def test_unknown_effects_require_whole_function_fallback(hexes):
    with pytest.raises(SSAError):
        build(function(*hexes))


def test_branch_cfg_mismatch_and_external_tail_are_rejected():
    f = function("7401", "90", "c3")
    f.succ[0] = [1]
    with pytest.raises(SSAError, match="disagrees"):
        build(f)
    with pytest.raises(SSAError, match="external branch"):
        build(function("e9fb0f0000"))


def test_emitter_stages_phi_edge_copies_and_rejects_unreviewed_arithmetic():
    # add ecx,1; cmp ecx,4; jb entry; ret
    body = emit(function("83c101", "83f904", "72f8", "c3"), "test_fn")
    assert "uint64_t p" in body and "recomp_return(c);" in body
    with pytest.raises(SSAError, match="only signed IDIV32 is supported"):
        emit(function("66f7f9", "c3"), "test_fn")


@pytest.mark.parametrize("hexcode,name,start,expected,af,of", [
    ("40", "EAX", 0xf, 0x10, 1, 0),
    ("40", "EAX", 0xffffffff, 0, 1, 0),
    ("40", "EAX", 0x7fffffff, 0x80000000, 1, 1),
    ("48", "EAX", 0, 0xffffffff, 1, 0),
    ("48", "EAX", 0x80000000, 0x7fffffff, 1, 1),
    ("fec0", "AL", 0xff, 0, 1, 0),
    ("fecc", "AH", 0x80, 0x7f, 1, 1),
    ("6640", "AX", 0x7fff, 0x8000, 1, 1),
])
@pytest.mark.parametrize("cf", [0, 1])
def test_inc_dec_supply_af_without_changing_carry(hexcode, name, start, expected, af, of, cf):
    f = codegen_ir(function(hexcode, "c3"), LIFTER)
    out, _ = execute(build(f), initial(**{name: start, "CF": cf, "ESP": 0x8000}))
    assert (reg(out, name), reg(out, "AF"), reg(out, "OF")) == (expected, af, of)
    # An unchanged CF may never enter this function's SSA state at all.
    cf_key = ("register", LIFTER.register("CF")[1])
    assert cf_key not in out or out[cf_key] == cf


@pytest.mark.parametrize("carry", [0, 1])
@pytest.mark.parametrize("hexcode,regname", [("19c0", "EAX"), ("18c0", "AL"), ("6619c0", "AX")])
def test_same_operand_sbb_af_includes_the_incoming_borrow(carry, hexcode, regname):
    f = codegen_ir(function(hexcode, "c3"), LIFTER)
    out, _ = execute(build(f), initial(EAX=0x12345678, CF=carry, ESP=0x8000))
    assert reg(out, regname) == ((1 << (LIFTER.register(regname)[2] * 8)) - 1 if carry else 0)
    assert reg(out, "AF") == reg(out, "CF") == carry


@pytest.mark.parametrize("count", [0, 1, 16, 31, 32, 33, 255])
def test_shr_masks_count_preserves_af_and_uses_runtime_of(count):
    f = codegen_ir(function("c1e8%02x" % count, "c3"), LIFTER)
    out, _ = execute(build(f), initial(EAX=0x80000000, AF=1, OF=0, CF=1, ESP=0x8000))
    assert reg(out, "EAX") == 0x80000000 >> (count & 31)
    assert not any(op.out == LIFTER.register("AF") for ins in f.insns for op in ins.ops)
    assert reg(out, "OF") == (1 if count & 31 else 0)


def test_unsigned_division_uses_checked_runtime_seam_and_rejects_narrow_forms():
    f = codegen_ir(function("f7f1", "c3"), LIFTER)
    opcodes = [op.opc for op in f.insns[0].ops]
    assert "DIV32" in opcodes and "INT_DIV" not in opcodes and "INT_REM" not in opcodes
    body = emit(function("f7f1", "c3"), "test_fn")
    assert "div32(c," in body and "0x1000ull" in body
    for h in ("f6f1", "66f7f1"):
        with pytest.raises(SSAError, match="only unsigned DIV32"):
            emit(function(h, "c3"), "test_fn")


@pytest.mark.parametrize("hexes", [
    ("8ac4", "b47f", "8bd0", "c3"),
    ("92", "83c101", "83f904", "72f7", "c3"),
    ("8901", "8b11", "c3"),
    ("b8ffffffff", "83c001", "c3"),
])
def test_simplification_preserves_expected_partial_loop_memory_and_wrapping_results(hexes):
    raw = function(*hexes)
    f = raw if raw.insns[0].mnem.upper() == "XCHG" else codegen_ir(raw, LIFTER)
    original, optimized = build(f), build(f)
    before = sum(len(b.ops) + len(b.phis) for b in optimized.blocks.values())
    live = simplify(optimized)
    assert sum(len(b.ops) + len(b.phis) for b in optimized.blocks.values()) < before
    for eax in (0, 0x1234abcd, 0xffffffff):
        inputs = initial(EAX=eax, EDX=22, ECX=1, ESP=0x8000)
        assert execute(optimized, inputs) == execute(original, inputs)
    # Re-running the passes must not create new values or resurrect dead ops.
    count = len(optimized.values)
    assert simplify(optimized) == live
    assert len(optimized.values) == count


def test_dead_load_result_keeps_effect_token_and_pre_fault_snapshot():
    # mov eax,[ecx]; mov eax,7; ret: loaded value is otherwise unused.
    s = build(codegen_ir(function("8b01", "b807000000", "c3"), LIFTER))
    loads = [v for b in s.blocks.values() for v in b.ops if v.opc == "LOAD"]
    assert len(loads) == 2
    live = simplify(s)
    assert all(v.id in live for v in loads)
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc == "LOAD":
                assert all(s.resolve(x).id in live for x in b.snapshots[v.id].values())
    # EAX at the first load must still be its incoming value, not constant 7.
    first = next(b for b in s.blocks.values() if loads[0] in b.ops)
    key = ("register", LIFTER.register("EAX")[1])
    assert s.resolve(first.snapshots[loads[0].id][key]) is s.inputs[key]


def test_canonicalization_is_width_aware_and_preserves_overlapping_uniques():
    from ir.ssa import SSA
    s = SSA()
    source = s.value("INPUT", 4)
    lanes = [s.value("BYTE", 1, [source], n) for n in range(4)]
    full = s.value("PACK", 4, lanes)
    partial = s.value("PACK", 2, lanes[:2])
    picked = s.value("BYTE", 1, [partial], 1)
    narrow = s.value("COPY", 1, [source])
    canonicalize(s)
    assert s.resolve(full) is source
    assert s.resolve(partial) is partial
    assert s.resolve(picked) is lanes[1]
    assert s.resolve(narrow) is narrow
    u, sub, eax = ("unique", 0x20, 4), ("unique", 0x21, 1), LIFTER.register("EAX")
    ins = Insn(0x1000, 1, "synthetic", [Op("COPY", u, [("const", 0x12345678, 4)]),
               Op("COPY", sub, [("const", 0xab, 1)]), Op("COPY", eax, [u])], 0, False, False, [])
    ret = LIFTER.lift(0x1001, bytes.fromhex("c3"))
    graph = build(FunctionIR(0x1000, [ins, ret], [[1], []]))
    simplify(graph)
    out, _ = execute(graph, initial(ESP=0x8000))
    assert reg(out, "EAX") == 0x1234ab78


def test_constant_shifts_and_arithmetic_use_guest_widths():
    from ir.ssa import SSA
    s = SSA()
    x = s.value("CONST", 1, data=255)
    one = s.value("CONST", 1, data=1)
    eight = s.value("CONST", 4, data=8)
    add = s.value("INT_ADD", 1, [x, one])
    shift = s.value("INT_LEFT", 1, [x, eight])
    byte = s.value("BYTE", 1, [s.value("CONST", 4, data=0x12345678)], 2)
    canonicalize(s)
    assert s.resolve(add).data == s.resolve(shift).data == 0
    assert s.resolve(byte).data == 0x34


def test_dead_unknown_operation_still_requires_consumer_diagnostic():
    f = function("90", "c3")
    f.insns[0].ops.append(Op("UNMODELED_EFFECT", None, []))
    with pytest.raises(SSAError, match="unsupported integer operation UNMODELED_EFFECT"):
        emit(f, "test_fn")


def test_raw_ssa_comparison_path_retains_unsimplified_emission():
    f = function("b8ffffffff", "83c001", "c3")
    assert len(emit(f, "test_fn")) < len(emit(f, "test_fn", optimize=False))


def runtime_groups():
    return [[("register", off + n) for n in range(size)]
            for _, off, size in [LIFTER.register(name) for name in (
                "EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI", "EIP",
                "CF", "PF", "AF", "ZF", "SF", "OF", "DF")]]


@pytest.mark.parametrize("wide", [False, True])
@pytest.mark.parametrize("hexes", [
    ("8ac4", "b47f", "8bd0", "c3"),
    ("92", "83c101", "83f904", "72f7", "c3"),
    ("8901", "8b11", "c3"),
    # Branch with a write on only one incoming path, then a load at the join.
    ("85c0", "7405", "ba11223344", "8b01", "c3"),
    # Loop: publish ECX, then increment it before the backedge. Old phi values
    # must never be treated as the already-published value of the next iteration.
    ("8b01", "83c101", "83f904", "72f6", "c3"),
])
def test_publication_and_wide_phis_preserve_every_observed_field(hexes, wide):
    raw = function(*hexes)
    f = raw if raw.insns[0].mnem.upper() == "XCHG" else codegen_ir(raw, LIFTER)
    s = build(f, register_groups=runtime_groups() if wide else ())
    canonicalize(s)
    publications = plan(s, f.succ, runtime_groups())
    # Keep all state roots so the interpreter can validate omitted stores too.
    simplify(s)
    for eax in (0, 0x1234abcd, 0xffffffff):
        inputs = initial(EAX=eax, EDX=22, ECX=1, ESP=0x8000)
        assert execute(s, inputs, publications=publications) == execute(build(f), inputs)


@pytest.mark.parametrize("hexes", [
    ("83c007", "8b11", "8901", "c3"),
    ("85c0", "7405", "ba11223344", "8b01", "8901", "c3"),
    ("8b01", "83c101", "83f904", "72f6", "8901", "c3"),
])
def test_deferred_reads_still_publish_exact_stores_and_returns(hexes):
    f = codegen_ir(function(*hexes), LIFTER)
    s = build(f, register_groups=runtime_groups())
    canonicalize(s)
    read_fields = {key for group in runtime_groups()[4:6] + runtime_groups()[8:9] for key in group}
    pubs = plan(s, f.succ, runtime_groups(), read_fields=read_fields)
    simplify(s)  # Retain snapshots for independent interpreter assertions.
    for eax in (0, 0x1234abcd, 0xffffffff):
        inputs = initial(EAX=eax, EDX=22, ECX=1, ESP=0x8000)
        assert execute(s, inputs, publications=pubs, read_fields=read_fields) == execute(build(f), inputs)


def test_publication_after_division_invalidates_all_field_facts():
    f = codegen_ir(function("f7f1", "8903", "c3"), LIFTER)
    s = build(f, register_groups=runtime_groups())
    canonicalize(s)
    publications = plan(s, f.succ, runtime_groups())
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc == "STORE":
                assert set(publications[v.id]) == set(s.inputs) - {MEMORY}


def test_partial_write_at_join_keeps_unwritten_upper_lanes_in_wide_value():
    f = codegen_ir(function("85c9", "7402", "b4ab", "8903", "c3"), LIFTER)
    s = build(f, register_groups=runtime_groups())
    canonicalize(s)
    publications = plan(s, f.succ, runtime_groups())
    simplify(s)
    assert any(v.size == 4 and s.resolve(v) is v
               for b in s.blocks.values() for v in b.phis)
    for eax in (0, 0x12345678, 0xffffffff):
        for ecx in (0, 1):
            inputs = initial(EAX=eax, ECX=ecx, EBX=0x9000, ESP=0x8000)
            out, memory = execute(s, inputs, publications=publications)
            expected = eax if not ecx else (eax & 0xffff00ff) | 0xab00
            assert reg(out, "EAX") == expected
            assert sum(memory[0x9000 + n] << (8 * n) for n in range(4)) == expected


@pytest.mark.parametrize("eax", [0, 0x7f, 0x80, 0xff, 0x1234abcd, 0xffffffff])
def test_signed_and_unsigned_extension_preserve_guest_widths(eax):
    f = codegen_ir(function("0fbec0", "0fb7d0", "c3"), LIFTER)
    out, _ = execute(build(f), initial(EAX=eax, ESP=0x8000))
    low = eax & 255
    expected = (low - 256 if low & 128 else low) & 0xffffffff
    assert reg(out, "EAX") == expected
    assert reg(out, "EDX") == expected & 0xffff


@pytest.mark.parametrize("carry", [0, 1])
@pytest.mark.parametrize("eax,ecx", [(0xffffffff, 0), (0xf, 0), (0x7fffffff, 0), (0x1234, 0x56)])
def test_adc_includes_carry_in_result_and_auxiliary_carry(eax, ecx, carry):
    f = codegen_ir(function("11c8", "c3"), LIFTER)
    out, _ = execute(build(f), initial(EAX=eax, ECX=ecx, CF=carry, ESP=0x8000))
    result = (eax + ecx + carry) & 0xffffffff
    assert reg(out, "EAX") == result
    assert reg(out, "AF") == int((eax & 15) + (ecx & 15) + carry > 15)


def test_memory_rmw_reads_once_and_keeps_old_flags_at_store_observation():
    f = codegen_ir(function("0103", "c3"), LIFTER)
    s = build(f)
    assert sum(v.opc == "LOAD" for b in s.blocks.values() for v in b.ops) == 2
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc == "STORE":
                for name in ("CF", "OF", "SF", "ZF", "PF", "AF"):
                    key = ("register", LIFTER.register(name)[1])
                    assert s.resolve(b.snapshots[v.id][key]) is s.inputs[key]
    inputs = initial(EAX=1, EBX=0x9000, ESP=0x8000, CF=0, AF=0)
    out, memory = execute(s, inputs, {0x9000 + n: 255 for n in range(4)})
    assert reg(out, "CF") == reg(out, "AF") == reg(out, "ZF") == 1
    assert [memory[0x9000 + n] for n in range(4)] == [0, 0, 0, 0]


def test_implicit_atomic_memory_exchange_requires_whole_function_fallback():
    with pytest.raises(SSAError, match="opaque effect CALLOTHER"):
        emit(function("8703", "c3"), "test_fn")


def named_function(pairs):
    """Like `function`, but with the translator's capstone mnemonic supplied."""
    at, insns = 0x1000, []
    for h, mnem in pairs:
        raw = bytes.fromhex(h)
        insns.append(LIFTER.lift(at, raw, mnem=mnem))
        at += len(raw)
    return FunctionIR(0x1000, insns, default_successors(insns))


def test_rep_movsd_lowers_to_audited_runtime_helper_only():
    # The production decoder names the string form MOVSD; the bare pypcode
    # lifter names it MOVSD.REP. Both must resolve to the same audited bytes.
    for mnem in ("MOVSD", "MOVSD.REP"):
        rep = named_function([("f3a5", mnem), ("c3", "RET")])
        body = emit(rep, "test_fn")
        assert "rep_movsd(c);" in body
        assert any(op.opc == "MOVS32" and op.data == {"rep": True}
                   for ins in codegen_ir(rep, LIFTER).insns for op in ins.ops)
    bare = named_function([("a5", "MOVSD"), ("c3", "RET")])
    assert "movsd(c);" in emit(bare, "test_fn")
    # SSE MOVSD, other string widths and address-size/other prefixes stay
    # whole-function fallbacks rather than becoming an unaudited helper call.
    for h, mnem in (("f20f10c1", "MOVSD"), ("f3a4", "MOVSB"),
                    ("66a5", "MOVSW"), ("67a5", "MOVSD")):
        with pytest.raises(SSAError, match="unsupported instruction"):
            emit(named_function([(h, mnem), ("c3", "RET")]), "test_fn")


def test_rep_movsd_reloads_indices_count_and_flags_after_helper():
    # ECX is written locally before the move, so it must be published before the
    # helper and reloaded after it along with every other tracked lane/flag.
    f = codegen_ir(named_function([("b909000000", "MOV"), ("f3a5", "MOVSD"),
                                   ("c3", "RET")]), LIFTER)
    s = build(f, register_groups=runtime_groups())
    moves = [v for b in s.blocks.values() for v in b.ops if v.opc == "MOVS32"]
    assert len(moves) == 1
    block = next(b for b in s.blocks.values() if moves[0] in b.ops)
    after = block.ops[block.ops.index(moves[0]) + 1:]
    reloaded = {v.data for v in after if v.opc == "CALL_RELOAD"}
    assert {key for key in s.inputs if key != MEMORY} <= reloaded


def test_indirect_call_requires_explicit_symbol_and_canonical_fallthrough():
    with pytest.raises(SSAError, match="opaque effect CALLIND"):
        emit(function("ffd0", "c3"), "test_fn")
    body = emit(function("ffd0", "c3"), "test_fn", indirect_call_symbol="recomp_call")
    assert "recomp_call(c, (uint32_t)" in body
    with pytest.raises(SSAError, match="invalid indirect call symbol"):
        emit(function("ffd0", "c3"), "test_fn", indirect_call_symbol="not a symbol")
    # A non-canonical successor set is a tail/noreturn shape, not a call.
    f = function("ffd0", "c3")
    f.succ[0] = []
    with pytest.raises(SSAError, match="indirect call lacks its canonical fallthrough"):
        build(f, indirect_call_symbol="recomp_call")


def test_indirect_call_reads_memory_target_before_return_push():
    # call dword ptr [esp+4]: the target load must be emitted before the return
    # address store, matching the original instruction's access order.
    import re
    body = emit(function("ff542404", "c3"), "test_fn", indirect_call_symbol="recomp_call")
    lines = body.splitlines()
    call = next(l for l in lines if "recomp_call(c," in l)
    target = re.search(r"recomp_call\(c, \(uint32_t\)(v\d+)\);", call).group(1)
    assigned = next(i for i, l in enumerate(lines) if l.strip().startswith(target + " ="))
    pushed = next(i for i, l in enumerate(lines) if "wr32(" in l)
    assert assigned < pushed


def test_division_reloads_every_tracked_field_after_helper():
    # div ecx; mov [ebx],eax: after DIV32 every tracked lane/flag is re-read from
    # the helper's result state, so a returning divide-error handler that mutates
    # EBX/ECX or flags cannot leave stale SSA values behind.
    f = codegen_ir(function("f7f1", "8903", "c3"), LIFTER)
    s = build(f, register_groups=runtime_groups())
    division = next(v for b in s.blocks.values() for v in b.ops if v.opc == "DIV32")
    block = next(b for b in s.blocks.values() if division in b.ops)
    after = block.ops[block.ops.index(division) + 1:]
    reloaded = {v.data for v in after if v.opc == "CALL_RELOAD"}
    assert {key for key in s.inputs if key != MEMORY} <= reloaded
