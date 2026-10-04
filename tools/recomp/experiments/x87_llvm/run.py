"""Direct LLVM emission and a bounded stack-to-SSA pass; no parallel custom IR."""
from pathlib import Path
import os
import shutil
import subprocess

import translate as T
from experiments.x87_locals.run import CASES as BASE_CASES, emit as emit_c

CASES = dict(BASE_CASES, deep_stack=["FLD float ptr [ESI]"] * 8 + ["FADDP"] * 7 + ["FSTP float ptr [EBX]"])

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[3]
DECLARATIONS = '''
declare i32 @rk_reg(ptr, i32)
declare double @rk_load(ptr, i32)
declare void @rk_store(ptr, i32, double)
declare double @rk_round(ptr, double)
declare void @rk_push(ptr, double)
declare void @rk_pop(ptr)
declare double @rk_read(ptr, i32)
declare void @rk_set(ptr, i32, double)
'''


def emit_llvm(name, lines, lift):
    """Lower the inspected subset instruction-by-instruction into semantic calls.

    The frontend deliberately does not track stack values: the LLVM pass does.
    Guest address expressions stay wrapping i32, without nsw/nuw or inbounds.
    """
    emit_c(lines, "baseline")  # Reuse the experiment's explicit scope checks.
    out, serial = [], 0

    def value(instruction):
        nonlocal serial
        result = f"%v{serial}"
        serial += 1
        out.append(f"  {result} = {instruction}")
        return result

    def read(index):
        return value(f"call double @rk_read(ptr %cpu, i32 {index})")

    def address(op):
        terms = []
        if op.base is not None:
            terms.append(value(f"call i32 @rk_reg(ptr %cpu, i32 {op.base})"))
        if op.index is not None:
            index = value(f"call i32 @rk_reg(ptr %cpu, i32 {op.index})")
            terms.append(value(f"mul i32 {index}, {op.scale}"))
        if op.disp or not terms:
            terms.append(str(op.disp & 0xffffffff))
        result = terms[0]
        for term in terms[1:]:
            result = value(f"add i32 {result}, {term}")
        return result

    insns = T.parse_listing_text('\n'.join(f'{0x100000+i:08x}  {s}' for i, s in enumerate(lines)))
    for ins in insns:
        m = ins.mnem
        out.append(f"  ; {ins.raw.split('  ', 1)[1]}")
        if m == "FADDP":
            lhs, rhs = read(1), read(0)
            result = value(f"fadd double {lhs}, {rhs}")
            rounded = value(f"call double @rk_round(ptr %cpu, double {result})")
            out.extend([f"  call void @rk_set(ptr %cpu, i32 1, double {rounded})",
                        "  call void @rk_pop(ptr %cpu)"])
            continue
        a = address(T.parse_operand(ins.ops[0]))
        if m == "FSTP":
            result = read(0)
            out.extend([f"  call void @rk_store(ptr %cpu, i32 {a}, double {result})",
                        "  call void @rk_pop(ptr %cpu)"])
            continue
        operand = value(f"call double @rk_load(ptr %cpu, i32 {a})")
        if m == "FLD":
            out.append(f"  call void @rk_push(ptr %cpu, double {operand})")
        else:
            lhs = read(0)
            opcode = {"FADD": "fadd", "FSUB": "fsub", "FMUL": "fmul"}[m]
            result = value(f"{opcode} double {lhs}, {operand}")
            rounded = value(f"call double @rk_round(ptr %cpu, double {result})")
            out.append(f"  call void @rk_set(ptr %cpu, i32 0, double {rounded})")
    attribute = ' "recomp.x87.region"' if lift else ''
    return f"define void @{name}(ptr %cpu){attribute} {{\n" + '\n'.join(out) + '\n  ret void\n}\n'


def llvm_config():
    configured = os.environ.get("LLVM_CONFIG")
    found = configured or shutil.which("llvm-config")
    if not found:
        candidate = Path("/opt/homebrew/opt/llvm/bin/llvm-config")
        if candidate.is_file():
            found = str(candidate)
    if not found:
        raise SystemExit("LLVM 22 development tools required; set LLVM_CONFIG to llvm-config")
    version = subprocess.check_output([found, "--version"], text=True).strip()
    if version.split('.')[0] != "22":
        raise SystemExit(f"This bounded prototype targets LLVM 22, found {version}")
    return found


def run_experiment(out, cmake, jobs, function_profile=None):
    """Build through CMake with one consistent LLVM toolchain, retaining every stage."""
    config = llvm_config()
    bindir = Path(subprocess.check_output([config, "--bindir"], text=True).strip())
    cmakedir = subprocess.check_output([config, "--cmakedir"], text=True).strip()
    out.mkdir(parents=True, exist_ok=True)
    from experiments.x87_llvm.function import DECLARATIONS as FUNCTION_DECLS
    from experiments.x87_llvm.fixtures import BODY, BASELINE
    module, baseline, declarations = [DECLARATIONS, FUNCTION_DECLS], ['#include "x86.h"'], []
    modes = ("baseline", "llvm_raw", "llvm_lifted", "full")
    declarations += ['static const char *mode_names[] = {"baseline", "llvm_raw", "llvm_lifted", "full"};',
                     "static const unsigned normalize_empty_mask = 0, required_match_mask = 14;"]
    for name, lines in CASES.items():
        for mode in modes:
            symbol = f"{name}_{mode}"
            declarations.append(f"void {symbol}(X86 *);")
            if mode.startswith("llvm_"):
                module.append(emit_llvm(symbol, lines, mode == "llvm_lifted"))
            else:
                baseline.append(f"void {symbol}(X86 *c) {{\n{emit_c(lines, mode)}\n}}")
    # A CFG regression independent of any game: PHIs for a live slot AND a
    # popped slot, followed by an observer and an aliasing memory store.
    names = [*CASES, "cfg_join"]
    for mode in modes:
        symbol = f"cfg_join_{mode}"
        declarations.append(f"void {symbol}(X86 *);")
        if mode.startswith("llvm_"):
            attr = ' "recomp.x87.region"' if mode == "llvm_lifted" else ''
            module.append(f"define void @{symbol}(ptr %cpu){attr} {{\n{BODY}\n}}")
        else:
            baseline.append(f"void {symbol}(X86 *c) {{\n{BASELINE}\n}}")
    if function_profile:
        from experiments.x87_llvm.function import prepare
        module.append(prepare(function_profile, out))
    (out / "input.ll").write_text('\n'.join(module))
    (out / "baseline.c").write_text('\n'.join(baseline))
    declarations += ["static const char *case_names[] = {" + ','.join(f'"{n}"' for n in names) + "};",
                     "static void (*functions[][4])(X86 *) = {" +
                     ','.join('{' + ','.join(f"{n}_{m}" for m in modes) + '}' for n in names) + "};"]
    (out / "fixtures.h").write_text('\n'.join(declarations))
    subprocess.run([cmake, "-S", str(HERE), "-B", str(out),
                    f"-DFUNCTION_TEST={'ON' if function_profile else 'OFF'}",
                    f"-DLLVM_DIR={cmakedir}", f"-DKIT_RUNTIME={KIT / 'runtime'}",
                    f"-DCMAKE_C_COMPILER={bindir / 'clang'}",
                    f"-DCMAKE_CXX_COMPILER={bindir / 'clang++'}"], check=True)
    subprocess.run([cmake, "--build", str(out), "--parallel", str(jobs)], check=True)
    from experiments.x87_llvm.checks import check_pass
    plugins = list(out.glob("RecompX87.*"))
    plugin = next(p for p in plugins if p.suffix in {'.so', '.dylib', '.dll'})
    check_pass(bindir / 'opt', plugin, out)
    result = subprocess.run([str(out / "x87_llvm")], check=True, capture_output=True, text=True)
    (out / "results.txt").write_text(result.stdout)
    print(result.stdout, end="")
    if function_profile:
        result = subprocess.run([str(out / "x87_function")], check=True, capture_output=True, text=True)
        (out / "function-results.txt").write_text(result.stdout)
        print(result.stdout, end="")
    # Structural postcondition, in addition to opt's verifier: the pass must
    # remove every stack operation from each requested region, retaining rounds.
    lifted = (out / "lifted.ll").read_text()
    for name, lines in CASES.items():
        body = lifted.split(f"define void @{name}_llvm_lifted(", 1)[1].split('\n}', 1)[0]
        assert not any(f"@rk_{op}(" in body for op in ("push", "pop", "read", "set"))
        assert body.count("@rk_round(") == sum(s.split()[0] in {"FMUL", "FSUB", "FADD", "FADDP"} for s in lines)
    cfg_body = lifted.split("define void @cfg_join_llvm_lifted(", 1)[1].split('\n}', 1)[0]
    assert cfg_body.count("phi double") == 2  # live AND popped value
    assert "@rk_snapshot(" not in cfg_body
    if function_profile:
        body = lifted.split("define void @real_lifted(", 1)[1].split('\n}', 1)[0]
        raw_body = lifted.split("define void @real_raw(", 1)[1].split('\n}', 1)[0]
        for observer in ("fnstsw", "ret"):
            assert body.count(f"@rk_{observer}(") == raw_body.count(f"@rk_{observer}(")
        assert not any(f"@rk_{op}(" in body for op in ("push", "pop", "read", "set", "snapshot"))
    print("Verified LLVM stack elimination and retained arithmetic rounding calls.")
    print("Artifacts:", out)
