"""Bounded whole-function frontend. Instruction/CFG lowering only, no stack analysis."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import translate as T

DECLARATIONS = '''
declare double @rk_load64(ptr, i32)
declare void @rk_compare(ptr, double, double)
declare void @rk_fnstsw(ptr)
declare void @rk_test_ah(ptr, i32)
declare void @rk_xor_eax(ptr, i32)
declare void @rk_write_reg(ptr, i32, i32)
declare i32 @rk_zf(ptr, i32)
declare void @rk_ret(ptr, i32)
declare void @rk_observe(ptr)
'''


def emit_function(name, insns, lift):
    """Emit a closed CFG with explicit guest operations; refuse missing targets.

    Preconditions: decoded contiguous function with a known entry and exact extent.
    Every instruction must match a supported operand form. No arithmetic rewrite,
    state liveness, stack tracking, or inferred source-level predicate occurs here.
    """
    if not insns or len({i.addr for i in insns}) != len(insns):
        raise ValueError('empty function or duplicate instruction address')
    addresses = {i.addr for i in insns}
    leaders = {insns[0].addr}
    for k, ins in enumerate(insns):
        if ins.mnem in {'JZ', 'JNZ', 'JE', 'JNE', 'JMP'}:
            target = T.Translator.branch_target(ins)
            if target not in addresses:
                raise ValueError('branch target outside function or inside instruction')
            leaders.add(target)
        if ins.mnem in {'JZ', 'JNZ', 'JE', 'JNE', 'JMP', 'RET'} and k + 1 < len(insns):
            leaders.add(insns[k + 1].addr)
    if insns[-1].mnem not in {'RET', 'JMP'}:
        raise ValueError('function falls through its extent')
    out, serial = [], 0

    def value(expr):
        nonlocal serial
        v = f'%v{serial}'
        serial += 1
        out.append(f'  {v} = {expr}')
        return v

    def call(helper, args=''):
        out.append(f'  call void @{helper}(ptr %cpu{args})')

    def read(index=0):
        return value(f'call double @rk_read(ptr %cpu, i32 {index})')

    def memory(op):
        if op.kind != 'mem' or op.size not in {32, 64} or op.seg:
            raise ValueError('requires ordinary float/double memory operand')
        a = str(op.disp & 0xffffffff)
        for reg, scale in ((op.base, 1), (op.index, op.scale)):
            if reg is not None:
                r = value(f'call i32 @rk_reg(ptr %cpu, i32 {reg})')
                if scale != 1:
                    r = value(f'mul i32 {r}, {scale}')
                a = value(f'add i32 {a}, {r}')
        helper = 'rk_load' if op.size == 32 else 'rk_load64'
        return value(f'call double @{helper}(ptr %cpu, i32 {a})')

    for k, ins in enumerate(insns):
        m, ops = ins.mnem, [T.parse_operand(o) for o in ins.ops]
        if ins.rep:
            raise ValueError('prefix unsupported')
        if ins.addr in leaders:
            if k and insns[k - 1].mnem not in {'JZ', 'JNZ', 'JE', 'JNE', 'JMP', 'RET'}:
                out.append(f'  br label %b{ins.addr:x}')
            out.append(f'b{ins.addr:x}:')
        out.append(f'  ; {ins.raw}')
        if m == 'FLD' and len(ops) == 1:
            call('rk_push', f', double {memory(ops[0])}')
        elif m in {'FADD', 'FSUB', 'FMUL'} and len(ops) == 1:
            operand = memory(ops[0])
            op = {'FADD': 'fadd', 'FSUB': 'fsub', 'FMUL': 'fmul'}[m]
            result = value(f'{op} double {read()}, {operand}')
            rounded = value(f'call double @rk_round(ptr %cpu, double {result})')
            call('rk_set', f', i32 0, double {rounded}')
        elif m == 'FADDP' and (not ops or ins.ops == ['ST1']):
            result = value(f'fadd double {read(1)}, {read(0)}')
            rounded = value(f'call double @rk_round(ptr %cpu, double {result})')
            call('rk_set', f', i32 1, double {rounded}')
            call('rk_pop')
        elif m == 'FCOMP' and len(ops) == 1:
            operand = memory(ops[0])
            call('rk_compare', f', double {read()}, double {operand}')
            call('rk_pop')
        elif m == 'FCHS' and not ops:
            neg = value(f'fneg double {read()}')
            call('rk_set', f', i32 0, double {neg}')
        elif m == 'FNSTSW' and ins.ops == ['AX']:
            call('rk_fnstsw')
        elif m == 'TEST' and len(ops) == 2 and ins.ops[0] == 'AH' and ops[1].kind == 'imm' and 0 <= ops[1].imm <= 255:
            call('rk_test_ah', f', i32 {ops[1].imm}')
        elif m == 'MOV' and len(ops) == 2 and ops[0].kind == 'reg' and ops[0].size == 32 and ops[1].kind == 'imm':
            call('rk_write_reg', f', i32 {ops[0].reg}, i32 {ops[1].imm & 0xffffffff}')
        elif m == 'XOR' and ins.ops == ['EAX', 'EAX']:
            call('rk_xor_eax', ', i32 0')
        elif m in {'JZ', 'JNZ', 'JE', 'JNE'}:
            if k + 1 == len(insns):
                raise ValueError('conditional branch lacks fallthrough')
            zf = value('call i32 @rk_zf(ptr %cpu, i32 0)')
            cond = value(f'icmp {"ne" if m in {"JZ", "JE"} else "eq"} i32 {zf}, 0')
            out.append(f'  br i1 {cond}, label %b{T.Translator.branch_target(ins):x}, label %b{insns[k+1].addr:x}')
        elif m == 'JMP':
            out.append(f'  br label %b{T.Translator.branch_target(ins):x}')
        elif m == 'RET' and not ops:
            call('rk_ret', ', i32 0')
            out.append('  ret void')
        else:
            raise ValueError(f'unsupported function instruction: {ins.raw}')
    attr = ' "recomp.x87.region"' if lift else ''
    return f'define void @{name}(ptr %cpu){attr} {{\n' + '\n'.join(out) + '\n}\n'


def prepare(profile_path, out):
    """Read game-owned evidence, verify PE identity/bytes, emit both LLVM and C.

    No executable bytes or game constants are stored in the reusable experiment.
    The profile supplies a harness fixture that uses the existing comparison loop.
    """
    profile_path = Path(profile_path).resolve()
    p = json.loads(profile_path.read_text())
    exe = (profile_path.parent / p['exe']).resolve()
    if hashlib.sha256(exe.read_bytes()).hexdigest() != p['sha256']:
        raise ValueError('function profile executable hash mismatch')
    image = T.Image(str(exe))
    start, size = int(p['entry'], 0), p['size']
    data = image.data[start-image.base:start-image.base+size]
    if hashlib.sha256(data).hexdigest() != p['function_sha256']:
        raise ValueError('function bytes differ from inspected Ghidra evidence')
    image.md.detail = True
    decoded = list(image.md.disasm(data, start))
    if not decoded or sum(i.size for i in decoded) != size or decoded[-1].address + decoded[-1].size != start + size:
        raise ValueError('incomplete function decode')
    insns = [image.to_insn(i) for i in decoded]
    module = [emit_function('real_raw', insns, False), emit_function('real_lifted', insns, True)]
    from experiments.x87_llvm.direct import direct_ir
    for mode in ('raw', 'full', 'effects'):
        module.append(direct_ir(emit_function(f'real_direct_{mode}', insns, mode != 'raw'),
                                effects=mode == 'effects'))
    fn = T.Function(start, 'real_baseline', size, insns)
    fn.measure(image)
    fn.index = {i.addr: k for k, i in enumerate(insns)}
    fn.seh_escapes, fn.pushed_continuations, fn.seh_sites = set(), set(), {}
    tr = T.Translator(image, {start}, SimpleNamespace(eager_flags=True))
    code = ['#include "access.h"', 'extern void rk_observe(X86 *);', 'void real_baseline(X86 *c) {']
    for k, ins in enumerate(insns):
        code.append(f'L_{ins.addr:08x}:;')
        if ins.mnem == "FNSTSW":
            code.append("rk_observe(c);")
        code.extend(tr.emit(fn, k, T.ALL_FLAGS))
    code.append('}')
    directory = out / 'function'
    directory.mkdir(exist_ok=True)
    (directory / 'fixtures.h').write_text((profile_path.parent / p['fixture']).read_text())
    from experiments.x87_llvm.instrument import instrument_memory
    direct = '\n'.join(code)
    original = direct.replace('void real_baseline(', 'void real_uninstrumented(')
    basic = direct.replace('void real_baseline(', 'void real_basic(').replace('rk_observe(c);', '')
    (directory / 'baseline.c').write_text(instrument_memory(direct) + '\n' + original + '\n' + basic)
    if p.get('direct_fixture'):
        direct_dir = out / 'function-direct'
        direct_dir.mkdir(exist_ok=True)
        (direct_dir / 'fixtures.h').write_text((profile_path.parent / p['direct_fixture']).read_text())
        (direct_dir / 'baseline.c').write_text(basic)
    (directory / 'decoded.txt').write_text('\n'.join(i.raw for i in insns) + '\n')
    return '\n'.join(module)
