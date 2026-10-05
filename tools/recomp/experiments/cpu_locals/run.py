"""Native full-state checks of production CPU/x87 C lowering; no timing runs."""
from pathlib import Path
from types import SimpleNamespace
import re
import subprocess

import translate as T

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[3]
MODES = ((False, False), (True, False), (True, True), (False, True))
# Synthetic instruction identifiers, never game entry points.
CASES = {
    "loop": ["MOV EAX,ECX", "MOV EDX,3", "ADD EAX,ECX", "XOR ECX,EAX",
             "DEC EDX", "JNZ 0x00100002", "MOV dword ptr [EBX],EAX", "RET"],
    "partial": ["MOV EAX,ECX", "MOV AH,DL", "ADD AL,CL", "ADC AX,DX",
                "MOV CH,AL", "XOR EAX,ECX", "MOV dword ptr [EBX],EAX", "RET"],
    "diamond": ["MOV EAX,ECX", "TEST EDX,1", "JZ 0x00100006", "ADD EAX,ECX",
                "INC ECX", "JMP 0x00100008", "SUB EAX,ECX", "DEC ECX",
                "ADC EAX,ECX", "MOV dword ptr [EBX],EAX", "RET"],
    "alias": ["MOV EAX,dword ptr [ESI]", "ADD EAX,ECX", "MOV dword ptr [EBX],EAX",
              "MOV ECX,dword ptr [ESI]", "XOR EAX,ECX", "MOV dword ptr [EBX + 4],EAX", "RET"],
    "helper": ["MOV EAX,ECX", "ADD EAX,EDX", "SHL EAX,CL", "ADC EAX,EDX",
               "PUSHFD", "POPFD", "XOR EAX,ECX", "RET"],
    "shift_rotate": ["MOV EAX,EDX", "ADD EAX,ECX", "SHL AL,CL", "SHR AX,CL",
                     "SAR EAX,CL", "RCL AX,CL", "RCR AL,CL", "ROL EAX,CL", "ROR AX,CL",
                     "SHLD EAX,EDX,CL", "SHRD EAX,EDX,CL", "ADC EAX,ECX", "RET"],
    "imul_flags": ["MOV EAX,EDX", "ADD EAX,ECX", "IMUL EAX,ECX", "ADC EAX,EDX",
                   "IMUL AX,DX", "ADC EAX,EDX", "MOV dword ptr [EBX],EAX", "RET"],
    "x87_scopes": ["MOV EAX,ECX", *["ADD EAX,EDX"] * 9, "FLD float ptr [ESI]",
                   "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                   "FSTP float ptr [EBX]", "INC EAX", "FILD qword ptr [ESI]",
                   "FSTP double ptr [EBX + 8]", "ADC EAX,ECX", "RET"],
    "call": ["MOV EAX,ECX", "ADD EAX,EDX", "XOR ECX,EAX", "CALL 0x00200000",
             "ADC EAX,ECX", "ADD EDX,EAX", "MOV dword ptr [EBX],EDX", "RET"],
    "indirect_call": ["MOV EAX,ECX", "ADD EAX,EDX", "MOV EDX,0x00200000", "CALL EDX",
                      "ADC EAX,ECX", "ADD EDX,EAX", "MOV dword ptr [EBX],EDX", "RET"],
    "x87_call": ["MOV EAX,ECX", *["ADD EAX,EDX"] * 9, "FLD float ptr [ESI]",
                 "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                 "CALL 0x00200000", "FMUL float ptr [ESI]", "FADD float ptr [EDI]",
                 "FSTP float ptr [EBX]", "ADC EAX,ECX", "RET"],
    "alternate": ["ADD EAX,ECX", "XOR ECX,EAX", "MOV EDX,EAX", "ADD EAX,EDX",
                  "TEST EAX,ECX", "MOV dword ptr [EBX],EAX", "RET"],
    "width_diamond_loop": ["MOV ECX,3", "FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                           "FADD float ptr [EDI + 4]", "TEST EAX,1", "JZ 0x00100008",
                           "FSUB float ptr [ESI]", "JMP 0x00100009", "FMUL float ptr [EDI]",
                           "FMUL float ptr [ESI]", "DEC ECX", "JNZ 0x00100004",
                           "FNSTSW AX", "FCOMP float ptr [EDI]", "RET"],
    "width_mixed_join": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                         "FADD float ptr [EDI + 4]", "TEST EAX,1", "JZ 0x00100007",
                         "FSTP float ptr [EBX]", "FLD double ptr [ESI]",
                         "FMUL float ptr [EDI]", "FSTP float ptr [EBX]", "RET"],
    "width_incoming_loop": ["MOV ECX,3", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                            "FSUB float ptr [ESI]", "DEC ECX", "JNZ 0x00100001",
                            "FNSTSW AX", "FSTP float ptr [EBX]", "RET"],
    "width_widening_backedge": ["MOV ECX,3", "FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                                "FADD float ptr [EDI + 4]", "FSUB float ptr [ESI]",
                                "FSTP float ptr [EBX]", "FLD double ptr [ESI]",
                                "DEC ECX", "JNZ 0x00100002", "FNSTSW AX",
                                "FSTP float ptr [EBX]", "RET"],
    "width_alias": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                    "TEST EAX,1", "JZ 0x00100006", "MOV dword ptr [ESI],EDX",
                    "FMUL float ptr [ESI]", "FSTP float ptr [EBX]", "RET"],
    "width_observer_call": ["FLD float ptr [ESI]", "FMUL float ptr [EDI]",
                            "FADD float ptr [EDI + 4]", "TEST EAX,1", "JZ 0x00100006",
                            "FMUL float ptr [ESI]", "FNSTSW AX", "CALL 0x00200000",
                            "FMUL float ptr [EDI]", "FADD float ptr [EDI + 4]",
                            "FSUB float ptr [ESI]", "FSTP float ptr [EBX]", "RET"],
}


def emit(name, lines, mode):
    """Exercise the complete driver, including entry adapters and RET emission."""
    insns = T.parse_listing_text("\n".join(f"{0x100000+i:08x}  {s}" for i, s in enumerate(lines)))
    fn = T.Function(0x100000, name, len(insns), insns)
    cpu, x87 = MODES[mode]
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=True, cpu_locals=cpu, x87_locals=x87, x87_cfg_widths=mode == 2))
    tr.prepare(fn)
    entries = (0x100001,) if name == "alternate" else ()
    code = "\n".join(tr.translate(fn, entries))
    # All modes have the same guest identifiers, but distinct host symbols.
    code = re.sub(r"\b(fn|body)_([0-9a-f]{8})\b",
                  lambda m: f"{name}_{mode}_{m[1]}_{m[2]}", code)
    entry = 0x100001 if entries else fn.addr
    return code, f"{name}_{mode}_fn_{entry:08x}"


def run_checks(out, cmake, jobs):
    """Compile via the build wrapper and compare mapped CPU/memory exits."""
    out.mkdir(parents=True, exist_ok=True)
    code = ['#include "x86.h"', 'void fixture_call(X86 *c);',
            '#define CALL_FN(a) fixture_call(c)']
    declarations = [
        'static const char *mode_names[] = {"eager", "cpu", "cpu+x87", "x87"};',
        'static const unsigned normalize_empty_mask = 0, required_match_mask = 14;',
        '#define FIXTURE_SCRATCH_SIZE 1024',
        '#define FIXTURE_SETUP(c, n) do { (c)->r[R_ESP] = 0x10100; '
        'wr32(0x10100, GUEST_RETURN_SENTINEL); '
        '(c)->eflags_cf = (n) & 1; (c)->eflags_zf = ((n) >> 1) & 1; '
        '(c)->eflags_sf = ((n) >> 2) & 1; (c)->eflags_of = ((n) >> 3) & 1; '
        '(c)->eflags_pf = ((n) >> 4) & 1; (c)->eflags_af = ((n) >> 5) & 1; } while (0)',
        '#define FIXTURE_RESET(c) ((void)0)', '#define FIXTURE_BEFORE(mode) ((void)0)',
        '#define FIXTURE_AFTER(mode) ((void)0)',
        'void fixture_finish(void);', '#define FIXTURE_FINISH() fixture_finish()',
    ]
    functions = []
    for name, lines in CASES.items():
        symbols = []
        for mode in range(4):
            body, symbol = emit(name, lines, mode)
            code.append(body)
            declarations.append(f"void {symbol}(X86 *);")
            symbols.append(symbol)
        functions.append("{" + ",".join(symbols) + "}")
    for mode in range(4):
        body, _ = emit("null_fault", ["MOV EAX,ECX", "ADD EAX,EDX", "XOR ECX,EAX",
                                       "MOV EDX,dword ptr [0x10]", "RET"], mode)
        code.append(body)
    declarations += ["static const char *case_names[] = {" +
                     ",".join(f'"{name}"' for name in CASES) + "};",
                     "static void (*functions[][4])(X86 *) = {" + ",".join(functions) + "};"]
    (out / "generated.c").write_text("\n".join(code) + "\n")
    (out / "fixtures.h").write_text("\n".join(declarations) + "\n")
    subprocess.run([cmake, "-S", str(HERE), "-B", str(out),
                    f"-DKIT_RUNTIME={KIT / 'runtime'}"], check=True)
    subprocess.run([cmake, "--build", str(out), "--parallel", str(jobs)], check=True)
    for executable in ("cpu_locals", "cpu_locals_null_checks"):
        result = subprocess.run([str(out / executable)], check=True, capture_output=True, text=True)
        (out / f"{executable}-results.txt").write_text(result.stdout)
        print(executable + ":\n" + result.stdout, end="")
    print("Artifacts:", out)
