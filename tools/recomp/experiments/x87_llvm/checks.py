"""Small contract checks for the compiled pass, invoked by the build experiment."""
import subprocess

from experiments.x87_llvm.run import DECLARATIONS


def check_pass(opt, plugin, out):
    strip_id = lambda s: '\n'.join(line for line in s.splitlines() if not line.startswith('; ModuleID'))
    cases = {
        "incoming": ("%v = call double @rk_read(ptr %cpu, i32 0)", "locally defined"),
        "underflow": ("call void @rk_pop(ptr %cpu)", "incoming stack"),
        "overflow": ('\n'.join(["call void @rk_push(ptr %cpu, double 1.0)"] * 9), "overflow"),
        "register": ("%r = call i32 @rk_reg(ptr %cpu, i32 8)", "register index"),
        "observer": ("call void @external(ptr %cpu)", "unsupported call"),
        "memory": ("%v = load double, ptr %cpu", "unsupported instruction"),
        "fast_math": ("%v = fadd fast double 1.0, 2.0", "relaxation"),
        "call_fast_math": ("%v = call fast double @rk_round(ptr %cpu, double 1.0)", "invalid x87 semantic"),
        "overflow_flags": ("%v = add nsw i32 1, 2", "relaxation"),
        "cycle": ("br label %loop\nloop:\nbr label %loop\nend:", "cycle"),
        "unreachable": ("ret void\nend:", "unreachable"),
        "join_depth": ("br i1 true, label %left, label %join\nleft:\ncall void @rk_push(ptr %cpu, double 1.0)\nbr label %join\njoin:", "incompatible x87 join"),
        "join_touched": ("br i1 true, label %left, label %join\nleft:\ncall void @rk_push(ptr %cpu, double 1.0)\ncall void @rk_pop(ptr %cpu)\nbr label %join\njoin:", "incompatible x87 join"),
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
    # Analysis is non-mutating. SSA and materialization are separately inspectable.
    analysis = directory / 'analysis.ll'
    subprocess.run([str(opt), f'-load-pass-plugin={plugin}', '-passes=recomp-x87-analyze',
                    '-verify-each', '-S', str(out / 'input.ll'), '-o', str(analysis)], check=True)
    plain = directory / 'plain.ll'
    subprocess.run([str(opt), '-passes=verify', '-S', str(out / 'input.ll'), '-o', str(plain)], check=True)
    assert strip_id(analysis.read_text()) == strip_id(plain.read_text())
    ssa = (out / 'ssa.ll').read_text()
    body = ssa.split('define void @cfg_join_llvm_lifted(', 1)[1].split('\n}', 1)[0]
    assert body.count('phi double') == 2 and '@rk_snapshot(' in body
    assert '@rk_slot(' not in body
    # Reapplying the pass must be an identity once its attribute is consumed.
    again = directory / 'again.ll'
    subprocess.run([str(opt), f'-load-pass-plugin={plugin}', '-passes=recomp-x87-stack',
                    '-verify-each', '-S', str(out / 'lifted.ll'), '-o', str(again)], check=True)
    assert strip_id(again.read_text()) == strip_id((out / 'lifted.ll').read_text())
    print(f'PASS CONTRACT: {len(cases)} refusals and idempotence checked')
