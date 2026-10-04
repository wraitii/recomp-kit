"""Small contract checks for the compiled pass, invoked by the build experiment."""
import subprocess

from experiments.x87_llvm.run import DECLARATIONS


def check_pass(opt, plugin, out):
    cases = {
        "incoming": ("%v = call double @rk_read(ptr %cpu, i32 0)", "locally defined"),
        "underflow": ("call void @rk_pop(ptr %cpu)", "incoming stack"),
        "overflow": ('\n'.join(["call void @rk_push(ptr %cpu, double 1.0)"] * 9), "overflow"),
        "register": ("%r = call i32 @rk_reg(ptr %cpu, i32 8)", "register index"),
        "observer": ("call void @external(ptr %cpu)", "unsupported call"),
        "memory": ("%v = load double, ptr %cpu", "unsupported instruction"),
        "fast_math": ("%v = fadd fast double 1.0, 2.0", "relaxation"),
        "overflow_flags": ("%v = add nsw i32 1, 2", "relaxation"),
        "branch": ("br label %end\nend:", "one block"),
    }
    directory = out / 'pass-checks'
    directory.mkdir(exist_ok=True)
    for name, (body, diagnostic) in cases.items():
        path = directory / f'{name}.ll'
        path.write_text(DECLARATIONS + '\ndeclare void @external(ptr)\n' +
                        'define void @bad(ptr %cpu) "recomp.x87.region" {\n' + body + '\nret void\n}\n')
        result = subprocess.run([str(opt), f'-load-pass-plugin={plugin}',
                                 '-passes=recomp-x87-stack', '-disable-output', str(path)],
                                capture_output=True, text=True)
        if result.returncode == 0 or diagnostic not in result.stderr:
            raise AssertionError(f'{name}: expected refusal containing {diagnostic!r}, got {result.stderr}')
    # Reapplying the pass must be an identity once its attribute is consumed.
    again = directory / 'again.ll'
    subprocess.run([str(opt), f'-load-pass-plugin={plugin}', '-passes=recomp-x87-stack',
                    '-verify-each', '-S', str(out / 'lifted.ll'), '-o', str(again)], check=True)
    strip_id = lambda s: '\n'.join(line for line in s.splitlines() if not line.startswith('; ModuleID'))
    assert strip_id(again.read_text()) == strip_id((out / 'lifted.ll').read_text())
    print(f'PASS CONTRACT: {len(cases)} refusals and idempotence checked')
