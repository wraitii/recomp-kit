"""Production SSA selection over byte-backed decoded functions and entry calls."""
from pathlib import Path
import json
import struct
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T
from code_map import decode_span
from ir.production import apply, MAX_INSTRUCTIONS
from test_translate_driver import synthetic_image

ENTRY, CALLEE = 0x00401000, 0x00401100
SETTINGS = {"fault_state": "relaxed", "msvc_x87_convention": True}


def fixture(code):
    image = synthetic_image(code)
    tr = T.Translator(image, set(code), SimpleNamespace(eager_flags=True))
    functions, bodies = [], {}
    for addr, raw in code.items():
        insns = list(decode_span(image, addr, "".join(
            "%x" % ins.size for ins in image.md.disasm(raw, addr))))
        fn = T.Function(addr, "fixture_%08x" % addr, len(raw), insns)
        fn.measure(image)
        tr.prepare(fn, strict=True)
        bodies[addr] = tr.translate(fn)
        functions.append(fn)
    return tr, functions, bodies


def test_mixed_callees_use_stable_thunks_and_chunk_declarations(tmp_path):
    # Caller: mov eax,1; call; add eax,ebx; fld1; fstp [ebx]; ret.
    # Callee: RDTSC is unsupported SSA, so it remains decoded C.
    raw = b"\xb8\x01\x00\x00\x00\xe8" + struct.pack("<i", CALLEE - ENTRY - 10)
    raw += b"\x01\xd8\xd9\xe8\xd9\x1b\xc3"
    tr, functions, bodies = fixture({ENTRY: raw, CALLEE: b"\x0f\x31\xc3"})
    original = bodies[CALLEE][:]
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["emitted"] == report["fallback"] == 1
    assert report["emitted_percent"] == report["fallback_percent"] == 50
    # Per-site lazy-flag settle census is part of the report; the emitted
    # caller's entry region is call-only, so its entry settle is removed.
    assert report["ssa_settles"]["entry"]["remove"] >= 1
    assert set(report["ssa_settles"]["postcall"]) == {"remove", "drop", "settle"}
    source = "\n".join(bodies[ENTRY])
    assert "CALL_FN(%08x);" % CALLEE in source
    assert "fn_%08x(c);" % CALLEE not in source
    assert "#if defined(RECOMP_NULL_CHECKS)" in source
    assert bodies[CALLEE] == original
    paths = T.emit_body_chunks(tmp_path, functions, bodies, {})
    assert any("void entry_%08x(X86 *c);" % CALLEE in Path(p).read_text() for p in paths)


@pytest.mark.parametrize("contract,reason", [
    ("alternate", "alternate entries"), ("seh", "SEH frame ownership"),
    ("helper", "SEH frame ownership"), ("continuation", "guest continuations"),
    ("patch", "audited instruction rewrite"), ("redirect", "audited instruction rewrite"),
    ("volatile", "audited instruction rewrite"), ("budget", "SSA instruction budget"),
    ("module", "auxiliary module"), ("intrinsic", "runtime intrinsic"),
])
def test_host_contracts_keep_whole_decoded_body(contract, reason):
    tr, functions, bodies = fixture({ENTRY: b"\x40\xc3"})
    fn = functions[0]
    original = bodies[ENTRY][:]
    entries, policies = {}, {}
    if contract == "alternate":
        entries[ENTRY] = {ENTRY + 1}
    elif contract == "seh":
        fn.seh_sites = {0: ENTRY}
    elif contract == "helper":
        tr.seh_helpers.add(ENTRY)
    elif contract == "continuation":
        fn.pushed_continuations = {ENTRY + 1}
    elif contract == "budget":
        fn.insns = fn.insns * (MAX_INSTRUCTIONS + 1)
    elif contract in ("patch", "redirect", "volatile", "intrinsic"):
        policies[{"patch": "instruction_patches", "redirect": "operand_redirects",
                  "volatile": "volatile_reads", "intrinsic": "intrinsic_bodies"}[contract]] = {ENTRY}
    report = apply(tr, functions, bodies, entries, SETTINGS, policies=policies,
                   module=contract == "module", quiet=True)
    assert report["fallback_reasons"] == {reason: 1}
    assert report["per_function"]["%08x" % ENTRY]["reason"] == reason
    assert bodies[ENTRY] == original


@pytest.mark.parametrize("raw,reason", [
    (b"\xff\xe0\xc3", "opaque effect BRANCHIND"),
])
def test_unsupported_effects_fallback_without_losing_diagnostics(raw, reason):
    tr, functions, bodies = fixture({ENTRY: raw})
    original = bodies[ENTRY][:]
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["fallback"] == 1
    actual = report["per_function"]["%08x" % ENTRY]["reason"]
    assert reason in actual
    assert bodies[ENTRY] == original


def test_division_and_indirect_call_emit_with_production_contract():
    # div ecx; ret: checked DIV32 and the full post-division state reload.
    tr, functions, bodies = fixture({ENTRY: b"\xf7\xf1\xc3"})
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["emitted"] == 1, report["per_function"]
    assert "div32(c," in "\n".join(bodies[ENTRY])
    # call eax; ret: production opts into recomp_call and reloads tracked state.
    tr, functions, bodies = fixture({ENTRY: b"\xff\xd0\xc3"})
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["emitted"] == 1, report["per_function"]
    assert "recomp_call(c, (uint32_t)" in "\n".join(bodies[ENTRY])


def test_direct_seh_helper_call_is_not_bound():
    raw = b"\xe8" + struct.pack("<i", CALLEE - ENTRY - 5) + b"\xc3"
    tr, functions, bodies = fixture({ENTRY: raw, CALLEE: b"\xc3"})
    tr.seh_helpers.add(CALLEE)
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["emitted"] == 0
    assert "not bound" in report["per_function"]["%08x" % ENTRY]["reason"]


def test_production_driver_selects_ssa_and_reports_final_bodies(tmp_path, monkeypatch):
    image = synthetic_image({ENTRY: b"\x40\xc3"})
    image.plausible_immediate_target = lambda addr: False
    image.code_pointers = lambda *args, **kwargs: (set(), set())
    listings = tmp_path / "functions"
    listings.mkdir()
    (listings / ("%08x.asm" % ENTRY)).write_text(
        "%08x  INC EAX\n%08x  RET\n" % (ENTRY, ENTRY + 1))
    table = tmp_path / "functions.tsv"
    table.write_text("address\tname\tsize\n%08x\tfixture\t2\n" % ENTRY)
    binary = tmp_path / "image"
    binary.write_bytes(image.data)
    curated = tmp_path / "globals.toml"
    curated.write_text("")
    monkeypatch.setattr(T, "configure", lambda cfg: None)
    monkeypatch.setattr(T.game_config, "load", lambda path: {
        "translate": {"ir_ssa": True, **SETTINGS}})
    for key, value in (("LISTINGS", listings), ("FUNCS_TSV", table),
                       ("BINARY", binary), ("CURATED", curated)):
        monkeypatch.setattr(T, key, str(value))
    monkeypatch.setattr(T, "Image", lambda path: image)
    monkeypatch.setattr(T, "AUX_MODULE", None)
    out, report = tmp_path / "gen", tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["translate.py", "--game", str(tmp_path),
                                     "--out", str(out), "--report", str(report), "--quiet"])
    assert T.main() == 0, report.read_text()
    coverage = json.loads(report.read_text())["ir_ssa"]
    assert coverage["functions"] == coverage["emitted"] == 1
    assert coverage["fallback"] == 0
    assert any("B0:;" in p.read_text() for p in out.glob("chunk_*.c"))


def test_mov_chain_restore_without_sites_is_an_ordinary_store():
    # mov ecx,[esp+4]; mov fs:[0],ecx; ret. The decoded emitter attaches no SEH
    # hook to a MOV-form unlink in a body with no establishing site, so the SSA
    # body is the same plain FS:[0] store and must be admitted.
    tr, functions, bodies = fixture({ENTRY: b"\x8b\x4c\x24\x04\x64\x89\x0d\x00\x00\x00\x00\xc3"})
    assert functions[0].seh_restores and not functions[0].seh_sites
    assert "recomp_seh" not in "\n".join(bodies[ENTRY])
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["emitted"] == 1, report["per_function"]
    assert "recomp_seh" not in "\n".join(bodies[ENTRY])


def test_pop_chain_restore_keeps_its_runtime_hook():
    # pop dword ptr fs:[0]; ret: the decoded emitter calls recomp_seh_frame_leave.
    tr, functions, bodies = fixture({ENTRY: b"\x64\x8f\x05\x00\x00\x00\x00\xc3"})
    assert "recomp_seh_frame_leave" in "\n".join(bodies[ENTRY])
    report = apply(tr, functions, bodies, {}, SETTINGS, quiet=True)
    assert report["fallback_reasons"] == {"SEH frame ownership": 1}


def test_internal_switch_entries_keep_decoded_wrappers_and_get_ssa_main_entry():
    # cmp eax,3; ja end; jmp [eax*4+table]; two case blocks; ret; table (data).
    code = bytes.fromhex("83f803" "7717" "ff2485") + struct.pack("<I", ENTRY + 29)
    code += bytes.fromhex("b901000000" "eb09" "b902000000" "eb02" "ffc3" "c3")
    code += b"".join(struct.pack("<I", ENTRY + o) for o in (12, 19, 12, 26))
    image = synthetic_image({ENTRY: code})
    tr = T.Translator(image, {ENTRY}, SimpleNamespace(eager_flags=True))
    insns = list(decode_span(image, ENTRY, "327525221"))
    fn = T.Function(ENTRY, "fixture_%08x" % ENTRY, 29, insns)
    fn.measure(image)
    tr.prepare(fn, strict=True)
    cases = {ENTRY + 12, ENTRY + 19, ENTRY + 26}
    tr.internal_entries = {ENTRY: set(cases)}
    bodies = {ENTRY: tr.translate(fn, cases)}
    assert any(line.startswith("void fn_%08x(X86 *c) { body_" % ENTRY) for line in bodies[ENTRY])
    report = apply(tr, [fn], bodies, {ENTRY: set(cases)}, SETTINGS, quiet=True)
    assert report["emitted"] == 1, report["per_function"]
    text = "\n".join(bodies[ENTRY])
    assert "static void body_%08x(X86 *c, uint32_t entry_)" % ENTRY in text
    assert "void fn_%08x(X86 *c) { body_" % ENTRY not in text
    # The SSA body, once per RECOMP_NULL_CHECKS arm.
    assert text.count("void fn_%08x(X86 *c) {" % ENTRY) == 2
    for case in cases:
        assert "void fn_%08x(X86 *c) { body_%08x(c, " % (case, ENTRY) in text
