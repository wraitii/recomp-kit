"""Small contract checks for the compiled pass, invoked by the build experiment."""
import subprocess
import re

from experiments.x87_llvm.run import DECLARATIONS


def check_pass(opt, plugin, out):
    strip_id = lambda s: '\n'.join(line for line in s.splitlines() if not line.startswith('; ModuleID'))
    cases = {
        "direct_contract": ("%v = call double @rk_direct_load(ptr %cpu, i32 0)", "invalid x87 semantic"),
        "effects_contract": ("", "effects requires the direct-access contract"),
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
        attrs = ' "recomp.x87.effects"' if name == 'effects_contract' else ''
        path.write_text(DECLARATIONS + '\ndeclare void @external(ptr)\ndeclare double @rk_direct_load(ptr, i32)\n' +
                        f'define void @bad(ptr %cpu) "recomp.x87.region"{attrs} {{\n' + body + '\nret void\n}\n')
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
    # Every supported memory access has a complete snapshot immediately before
    # it, including the return-address read. Check all transformed fixtures.
    for name, text in re.findall(r'define void @(\w+)\([^\n]*\n(.*?)\n}', ssa, re.S):
        if not (name.endswith('_llvm_lifted') or name == 'real_lifted'):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(r'@rk_(?:load|load64|store|ret)\(', line):
                assert '@rk_snapshot(' in lines[i - 1], (name, line)
    # Effect-directed snapshots: retain full observers/exits, supply TOP as a
    # value for plain FNSTSW, and leave every arithmetic/access in place.
    effects = (out / 'effects.ll').read_text()
    functions = lambda text: dict(re.findall(r'define void @(\w+)\([^\n]*\n(.*?)\n}', text, re.S))
    before, after = functions(ssa), functions(effects)
    for name, body in before.items():
        lowered = after[name]
        if not name.endswith('_direct_effects'):
            assert lowered == body, name  # Strict mode and the ablation are unchanged.
            continue
        for helper in ('round', 'compare', 'direct_load', 'direct_load64', 'direct_store', 'direct_ret', 'observe'):
            assert body.count(f'@rk_{helper}(') == lowered.count(f'@rk_{helper}('), (name, helper)
        assert lowered.count('@rk_status_at(') == body.count('@rk_direct_fnstsw(')
        assert '@rk_direct_fnstsw(' not in lowered
        lines = lowered.splitlines()
        for i, line in enumerate(lines):
            if re.search(r'@rk_(?:observe|direct_ret)\(', line):
                assert '@rk_snapshot(' in lines[i - 1], (name, line)
            if re.search(r'@rk_direct_(?:load|load64|store)\(', line):
                assert '@rk_snapshot(' not in lines[i - 1], (name, line)
            if 'ret void' in line:
                assert '@rk_snapshot(' in lines[i - 1] or '@rk_direct_ret(' in lines[i - 1], name
    status = after['status_top_direct_effects']
    assert status.count('@rk_snapshot(') == 1 and status.count('@rk_status_at(') == 1
    # ABI opacity must survive O2: access definitions never enter the IR module.
    optimized = (out / 'optimized.ll').read_text()
    assert not re.search(r'^define .*@rk_access_', optimized, re.M)
    for helper in ('f32', 'f64', 'u32', 'store32'):
        assert re.search(r'call .*@rk_access_' + helper + r'\(', optimized)

    for name, body in functions(optimized).items():
        if '_direct_' in name:
            assert '@rk_access_' not in body and '@rk_snapshot(' not in body
            if name != 'cfg_join_direct_effects' and not name.startswith('cfg_join_'):
                assert '@rk_observe(' not in body

    # Reapplying the pass must be an identity once its attribute is consumed.
    again = directory / 'again.ll'
    subprocess.run([str(opt), f'-load-pass-plugin={plugin}', '-passes=recomp-x87-stack',
                    '-verify-each', '-S', str(out / 'lifted.ll'), '-o', str(again)], check=True)
    assert strip_id(again.read_text()) == strip_id((out / 'lifted.ll').read_text())
    print(f'PASS CONTRACT: {len(cases)} refusals, effect boundaries and idempotence checked')
