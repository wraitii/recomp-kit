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
    "integer_x87": ["MOV EAX,ECX", "ADD EAX,EDX", "XOR ECX,EAX",
                    "FILD qword ptr [ESI]", "FIST word ptr [EBX]",
                    "FIST dword ptr [EBX + 4]", "FISTP qword ptr [EBX + 8]",
                    "ADC EAX,ECX", "CALL 0x00200000", "ADD EAX,ECX", "RET"],
    "integer_x87_round": ["MOV EAX,ECX", "ADD EAX,EDX", "XOR ECX,EAX",
                          "FLD float ptr [ESI]", "FIST word ptr [EBX]",
                          "FIST dword ptr [EBX + 4]", "FISTP qword ptr [EBX + 8]",
                          "ADC EAX,ECX", "CALL 0x00200000", "ADD EAX,ECX", "RET"],
    "bitwise_values": ["MOV EAX,ECX", "ADD EAX,EDX", "AND EAX,ECX", "TEST EAX,EDX",
                       "JZ 0x00100007", "XOR ECX,EAX", "JMP 0x00100008", "SUB ECX,EAX",
                       "MOV dword ptr [EBX],ECX", "RET"],
    "signed_predicate": ["MOV EAX,ECX", "ADD EAX,EDX", "CMP EAX,ECX", "JG 0x00100006",
                         "AND EAX,ECX", "JMP 0x00100007", "OR EAX,ECX", "ADC ECX,EAX",
                         "CALL 0x00200000", "MOV dword ptr [EBX],ECX", "RET"],
}

DATAFLOW_CASES = {
    # A casted bitwise operand makes this whole x87 region opaque to CPU
    # scalarization. Both branch edges must refresh changed eager registers.
    "opaque_region_exit": ["MOV ECX,3", "TEST EAX,EAX", "FLD float ptr [ESI]",
                           "FLD ST0", "FDIV float ptr [EDI]", "MOV EAX,dword ptr [ESI]",
                           "MOV ECX,dword ptr [EDI]", "TEST AH,0x41", "JZ 0x0010000b",
                           "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]",
                           "ADD EAX,ECX", "CALL 0x00200000", "RET"],
    "division_chain": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]",
                       "MOV EAX,dword ptr [ESI + 4]", "FST float ptr [EBX]",
                       "FLD ST0", "FMUL float ptr [ESI]", "FSTP float ptr [EBX + 4]",
                       "FSTP float ptr [EBX + 8]", "RET"],
    "reverse_division": ["FLD double ptr [ESI]", "FLD float ptr [EDI]",
                         "FDIVR ST0,ST1", "FDIVRP ST1,ST0", "FCHS",
                         "FSTP double ptr [EBX]", "RET"],
    "incoming_copy": ["FLD ST0", "FNSTSW AX", "FXCH ST1", "FST ST2",
                      "FSTP ST0", "FSTP ST0", "RET"],
    "exact_copy": ["FILD qword ptr [ESI]", "FLD ST0", "FXCH ST2", "FST ST3",
                   "FSTP ST0", "FLD float ptr [EDI]", "FDIV float ptr [ESI]",
                   "FSTP double ptr [EBX]", "RET"],
    "copy_loop": ["MOV EDX,3", "FLD double ptr [ESI]", "FLD ST0",
                  "FDIV float ptr [EDI]", "FSTP double ptr [EBX]", "DEC EDX",
                  "JNZ 0x00100002", "FSTP float ptr [EBX + 8]", "RET"],
    "copy_diamond": ["FLD double ptr [ESI]", "TEST EAX,1", "JZ 0x00100006",
                     "FLD float ptr [EDI]", "FXCH ST1", "JMP 0x00100007",
                     "FLD ST0", "FDIV ST0,ST1", "FSTP float ptr [EBX]",
                     "FSTP double ptr [EBX + 8]", "RET"],
    "division_call": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]", "FLD ST0",
                      "CALL 0x00200000", "FXCH ST1", "FDIVR float ptr [ESI]",
                      "FSTP float ptr [EBX]", "FSTP double ptr [EBX + 8]", "RET"],
    "stack_interleave": ["FLD float ptr [ESI]", "PUSH EAX", "FDIV float ptr [EDI]",
                         "POP EDX", "FLD ST0", "FMUL float ptr [ESI]",
                         "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]", "RET"],
    "wide_copy_wrap": ["FLD double ptr [ESI]"] * 8 +
                      ["FLD ST7", "FXCH ST7", "FST ST6", "FSTP ST0", "RET"],
    "environment_observer": ["FLD double ptr [ESI]", "FDIV float ptr [EDI]",
                             "FLD ST0", "FNSTENV [EBX]", "FXCH ST1",
                             "FDIVR float ptr [ESI]", "FSTP double ptr [EBX + 32]",
                             "FSTP double ptr [EBX + 40]", "RET"],
    "control_boundary": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]", "FLD ST0",
                         "FLDCW word ptr [ESI + 4]", "FXCH ST1", "FDIVR float ptr [EDI]",
                         "FSTP double ptr [EBX]", "FSTP double ptr [EBX + 8]", "RET"],
}


# Binary32 reloads retain the rounded spill, including when inputs alias it.
STACK_CASES = {
    "rounded_stack": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]",
                      "FST float ptr [ESP + 12]", "FMUL float ptr [ESI + 4]",
                      "FLD float ptr [ESP + 12]", "FMUL float ptr [EDI + 4]",
                      "FLD float ptr [ESP + 12]", "FSTP float ptr [EBX]",
                      "FSTP float ptr [EBX + 4]", "FSTP float ptr [EBX + 8]", "RET"],
    "unaligned_stack": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]",
                        "FST float ptr [ESP + 13]", "FMUL float ptr [ESI + 4]",
                        "FLD float ptr [ESP + 13]", "FMUL float ptr [EDI + 4]",
                        "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]", "RET"],
    "stack_read_alias": ["LEA ESI,[ESP + 13]", "FLD float ptr [EDI]",
                         "FST float ptr [ESP + 12]", "FMUL float ptr [ESI]",
                         "FLD float ptr [ESP + 12]", "FMUL float ptr [EDI + 4]",
                         "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]", "RET"],
    # This partial write exposes signaling-NaN round trips. Keep the arithmetic
    # and second store: optimized casts previously lost quieting at that store.
    "stack_write_alias": ["LEA EDI,[ESP + 13]", "FLD float ptr [ESI]",
                          "FST float ptr [ESP + 12]", "MOV byte ptr [EDI],AL",
                          "FLD float ptr [ESP + 12]", "FMUL float ptr [ESI + 4]",
                          "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]", "RET"],
    "stack_plane_loop": ["MOV EDX,3", "FLD float ptr [ESI]",
                         "FSTP float ptr [ESP + 12]", "FLD float ptr [EDI]",
                         "FCOMP float ptr [ESI]", "FLD float ptr [ESP + 12]",
                         "FNSTSW AX", "FCOMP float ptr [EDI]", "TEST AH,1",
                         "DEC EDX", "JNZ 0x00100001", "CALL 0x00200000", "RET"],
    "stack_call_reload": ["FLD float ptr [ESI]", "FDIV float ptr [EDI]",
                          "FST float ptr [ESP + 12]", "FLD float ptr [ESP + 12]",
                          "FSTP float ptr [EBX]", "FSTP float ptr [EBX + 4]",
                          "CALL 0x00200000", "FLD float ptr [ESP + 12]",
                          "FMUL float ptr [EDI]", "FSTP float ptr [EBX]", "RET"],
}


def emit(name, lines, mode, x87_dataflow=False, decoded=False):
    """Exercise the complete driver, including entry adapters and RET emission."""
    insns = T.parse_listing_text("\n".join(f"{0x100000+i:08x}  {s}" for i, s in enumerate(lines)))
    fn = T.Function(0x100000, name, len(insns), insns)
    cpu, x87 = MODES[mode]
    tr = T.Translator(None, {fn.addr, 0x200000}, SimpleNamespace(
        eager_flags=not (decoded and mode == 2), cpu_locals=cpu, x87_locals=x87,
        x87_dataflow=x87_dataflow and x87, x87_stack_forwarding=x87_dataflow and x87, decoded_dataflow=decoded and mode == 2))
    tr.prepare(fn)
    entries = (0x100001,) if name == "alternate" else ()
    code = "\n".join(tr.translate(fn, entries))
    # All modes have the same guest identifiers, but distinct host symbols.
    code = re.sub(r"\b(fn|body)_([0-9a-f]{8})\b",
                  lambda m: f"{name}_{mode}_{m[1]}_{m[2]}", code)
    entry = 0x100001 if entries else fn.addr
    return code, f"{name}_{mode}_fn_{entry:08x}"


def run_checks(out, cmake, jobs, x87_dataflow=False, decoded=False):
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
    cases = {**CASES, **({**DATAFLOW_CASES, **STACK_CASES} if x87_dataflow else {})}
    for name, lines in cases.items():
        symbols = []
        for mode in range(4):
            body, symbol = emit(name, lines, mode, x87_dataflow, decoded)
            code.append(body)
            declarations.append(f"void {symbol}(X86 *);")
            symbols.append(symbol)
        functions.append("{" + ",".join(symbols) + "}")
    for mode in range(4):
        body, _ = emit("null_fault", ["MOV EAX,ECX", "ADD EAX,EDX", "XOR ECX,EAX",
                                       "MOV EDX,dword ptr [0x10]", "RET"], mode, decoded=decoded)
        code.append(body)
    declarations += ["static const char *case_names[] = {" +
                     ",".join(f'"{name}"' for name in cases) + "};",
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
