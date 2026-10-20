"""Typed x87 operands, recipes and ordered state transitions."""
from dataclasses import dataclass
from enum import Enum

from .ssa import SSAError


class Type(Enum):
    F64 = 'f64'
    F32 = 'f32'
    U8 = 'u8'
    U16 = 'u16'
    CW = 'cw16'
    SW = 'sw16'
    U32 = 'u32'
    U64 = 'u64'
    I16 = 'i16'
    I32 = 'i32'
    I64 = 'i64'
    TAG = 'tag'
    BOOL = 'bool'
    CPU = 'cpu'
    VOID = 'void'


@dataclass(frozen=True)
class Expr:
    kind: str
    type: Type
    args: tuple = ()
    data: object = None

    def __post_init__(self):
        if not isinstance(self.type, Type) or not all(isinstance(arg, Expr) for arg in self.args):
            raise SSAError('x87 expression requires typed operands')

    @property
    def atomic(self):
        return self.kind in ('local', 'carry', 'environment', 'external')


def literal(value, type=Type.U32):
    return Expr('literal', type, data=value)


def local(index, type):
    return Expr('local', type, data=index)


def environment(part, resident=False):
    type = {'cw': Type.CW, 'sw': Type.SW, 'top': Type.U32, 'tag': Type.U16}[part]
    return Expr('resident' if resident else 'environment', type, data=part)


def control(choice, hooks=False):
    from .x87_cw import Dynamic, Guarded, Proven, CW_FAST, CW_PC_RC
    source = environment('cw')
    if isinstance(choice, Dynamic):
        return source
    if isinstance(choice, Guarded):
        if choice.condition != CW_FAST:
            raise SSAError('unsupported guarded x87 control-word operand')
        return Expr('cw_constant', Type.CW, data=(0, choice))
    if not isinstance(choice, Proven):
        raise SSAError('untyped x87 control-word choice')
    condition = choice.condition
    value = (Expr('cw_constant', Type.CW, data=(condition.value, choice))
             if condition.mask == CW_PC_RC else
             Expr('cw_partial', Type.CW, (source,), (CW_PC_RC & ~condition.mask, condition.value, choice)))
    return Expr('hook_control', Type.CW, (source, value), choice.proof) if hooks else value


def external(value, type=Type.U32):
    return Expr('external', type, data=value)


def carry(position, part):
    return Expr('carry', PART_TYPES[part], data=(position, part))


def group(value):
    return Expr('group', value.type, (value,))


def cast(type, value):
    return Expr('cast', type, (value,))


def binary(operator, lhs, rhs, type=None):
    return Expr('binary', type or lhs.type, (lhs, rhs), operator)


def helper(name, type, *args):
    signature = HELPERS.get(name)
    if signature is None or len(args) != len(signature):
        raise SSAError('unknown x87 helper or arity: %s' % name)
    if type != HELPER_RESULTS[name]:
        raise SSAError('x87 helper %s has incompatible result type' % name)
    for value, accepted in zip(args, signature):
        if value.type not in accepted:
            raise SSAError('x87 helper %s has incompatible %s operand' % (name, value.type.value))
    return Expr('helper', type, tuple(args), name)


NUMBER = frozenset((Type.F64, Type.F32, Type.I16, Type.I32, Type.I64,
                    Type.U8, Type.U16, Type.U32, Type.U64, Type.TAG))
FLOAT = frozenset((Type.F64, Type.F32))
WORD = frozenset((Type.U16,))
CW = frozenset((Type.CW,))
SW = frozenset((Type.SW,))
CPU = Expr('cpu', Type.CPU)
CPU_ARG = frozenset((Type.CPU,))
HELPERS = {name: (FLOAT,) for name in ('fabs', 'sqrt', 'sin', 'cos', 'tan', 'expm1', 'log2', 'log1p',
                                      'fxtract_exponent', 'fxtract_significand')}
HELPERS.update({
    'fx87': (SW, CW, FLOAT), 'fx87_exact': (SW, FLOAT),
    'fround': (CW, FLOAT), 'fto_float': (CW, FLOAT),
    'fdivz': (SW, NUMBER, NUMBER), 'fprem': (SW, FLOAT, FLOAT, NUMBER),
    'fcom': (SW, FLOAT, NUMBER), 'fucom': (SW, FLOAT, NUMBER),
    'fcomi_flags': (SW, FLOAT, FLOAT, NUMBER),
    'fscale': (FLOAT, FLOAT), 'atan2': (FLOAT, FLOAT),
    'ftag_classify': (FLOAT,), 'x87_indefinite': (),
    'fist': (SW, CW, FLOAT, NUMBER, NUMBER),
    'fpush': (CPU_ARG, FLOAT), 'fpush_st': (CPU_ARG, NUMBER), 'fpush_int': (CPU_ARG, NUMBER),
    'fset': (CPU_ARG, NUMBER, FLOAT), 'fcopy': (CPU_ARG, NUMBER, NUMBER),
    'fdrop': (CPU_ARG,), 'fpop': (CPU_ARG,), 'fxch': (CPU_ARG, NUMBER),
    'fxam': (CPU_ARG,), 'fstsw': (CPU_ARG,), 'x87_set_cw': (CPU_ARG, WORD),
    'x87_finit': (CPU_ARG,), 'ftag_put': (CPU_ARG, NUMBER, NUMBER),
    'fist_cpu': (CPU_ARG,), 'fcomi': (CPU_ARG, FLOAT, FLOAT), 'fucomi': (CPU_ARG, FLOAT, FLOAT),
    **{name: (CPU_ARG, NUMBER) for name in ('x87_fnsave', 'x87_frstor', 'x87_fnstenv', 'x87_fldenv')},
})
HELPER_RESULTS = {name: Type.F64 for name in HELPERS}
HELPER_RESULTS.update({name: Type.VOID for name in ('fcom', 'fucom', 'fpush', 'fpush_st', 'fpush_int',
    'fset', 'fcopy', 'fdrop', 'fxch', 'fxam', 'x87_set_cw', 'x87_finit', 'ftag_put',
    'fcomi', 'fucomi', 'x87_fnsave', 'x87_frstor', 'x87_fnstenv', 'x87_fldenv')})
HELPER_RESULTS.update({'ftag_classify': Type.TAG, 'fstsw': Type.U16, 'fto_float': Type.F32,
                       'fist': Type.I64, 'fist_cpu': Type.I64})
HELPER_RESULTS['fcomi_flags'] = Type.U16
PART_TYPES = {'value': Type.F64, 'bits': Type.U64, 'exact': Type.U8, 'tag': Type.TAG}
ZERO = literal(0, Type.U8)
ONE = literal(1, Type.U8)
EMPTY = literal('empty', Type.TAG)


def zero(value):
    return value is not None and value.kind == 'literal' and value.data == 0


def slot(part, physical):
    return Expr('slot', PART_TYPES[part], (physical,), part)


def st(index):
    return Expr('st', Type.F64, data=index)


def load(bits, address, integer=False):
    type = {16: Type.I16, 32: Type.I32, 64: Type.I64}[bits] if integer else Type.F64
    return Expr('load', type, (address,), (bits, integer))


@dataclass(frozen=True)
class Assign:
    target: Expr
    value: Expr
    operator: str = '='


@dataclass(frozen=True)
class Evaluate:
    value: Expr


@dataclass(frozen=True)
class Store:
    bits: int
    address: Expr
    value: Expr
    floating: bool = False
    bcd: bool = False


@dataclass(frozen=True)
class PublishTags:
    values: tuple


@dataclass(frozen=True)
class Activation:
    index: int
    clone: str | None
    choice: object
    known_mode: bool


@dataclass(frozen=True)
class EmptyTags:
    reset_top: bool = False


@dataclass(frozen=True)
class Reset:
    pass


@dataclass(frozen=True)
class Declare:
    value: Expr


@dataclass(frozen=True)
class Recipe:
    mnem: str
    operands: tuple
    action: str
    bits: int = 0
    slots: tuple = ()
    dst: int = 0
    src: int = 0
    operator: str | None = None
    integer: bool = False
    reverse: bool = False
    pops: int = 0


ARITH = {'FADD': '+', 'FSUB': '-', 'FSUBR': '-', 'FMUL': '*', 'FDIV': '/', 'FDIVR': '/'}
INTEGER_ARITH = {'FI' + name[1:]: op for name, op in ARITH.items()}
CONSTANTS = {name: Expr('constant', Type.F64, data=name) for name in
             ('FLD1', 'FLDZ', 'FLDPI', 'FLDLN2', 'FLDL2E', 'FLDLG2', 'FLDL2T')}
UNARY = frozenset(('FABS', 'FCHS', 'FSQRT', 'FRNDINT', 'FSIN', 'FCOS', 'F2XM1'))
COMPARE = frozenset(('FCOM', 'FCOMP', 'FUCOM', 'FUCOMP', 'FICOM', 'FICOMP', 'FCOMPP', 'FUCOMPP'))


def recipe(data, extended=False):
    m, operands = data['mnem'], tuple(data['operands'])
    memory = [size for kind, size in operands if kind == 'mem']
    slots = tuple(index for kind, index in operands if kind == 'st')
    bits = memory[0] * 8 if memory else 0
    if any(not 0 <= index < 8 for index in slots) or len(memory) > 1:
        raise SSAError('unsupported x87 operand shape %s %r' % (m, operands))
    action, dst, src, op, integer, reverse, pops = None, 0, 0, None, False, False, 0
    if m in CONSTANTS and not operands:
        action = 'constant'
    elif m == 'FLD' and len(operands) == 1 and (slots or bits in (32, 64, 80)):
        action = 'push'
    elif m == 'FILD' and operands == (('mem', bits // 8),) and bits in (16, 32, 64):
        action, integer = 'push_integer', True
    elif m in ('FST', 'FSTP') and len(operands) == 1 and (slots or bits in (32, 64, 80)):
        action, pops = 'store', int(m == 'FSTP')
    elif m in ('FIST', 'FISTP') and len(operands) == 1 and bits in (16, 32, 64):
        action, integer, pops = 'store_integer', True, int(m == 'FISTP')
    elif m in ARITH or m in INTEGER_ARITH or (m.endswith('P') and m[:-1] in ARITH):
        pops = int(m.endswith('P'))
        base = m[:-1] if pops else m
        integer = base in INTEGER_ARITH
        op = (INTEGER_ARITH if integer else ARITH)[base]
        reverse = base.endswith('R')
        if memory and len(operands) == 1 and not pops and bits in ((16, 32, 64) if integer else (32, 64, 80)):
            pass
        elif slots and not memory and len(slots) == len(operands) and len(slots) <= 2 and not integer:
            dst = slots[0] if len(slots) == 2 or pops else 0
            src = slots[1] if len(slots) == 2 else (0 if pops else slots[0])
        elif not operands and pops:
            dst, src = 1, 0
        else:
            raise SSAError('unsupported x87 operand shape %s %r' % (m, operands))
        action = 'arithmetic'
    elif m in COMPARE:
        integer = m.startswith('FI')
        if memory and len(operands) == 1 and not m.endswith('PP') and bits in ((16, 32, 64) if integer else (32, 64, 80)):
            pass
        elif not memory and len(slots) == len(operands) and len(slots) <= 2:
            src = slots[-1] if slots else 1
        else:
            raise SSAError('unsupported x87 operand shape %s %r' % (m, operands))
        action, pops = 'compare', 2 if m.endswith('PP') else int(m.endswith('P'))
    elif m in UNARY and not operands:
        action = 'unary'
    elif m in ('FXCH',) and not memory and len(slots) == len(operands) and len(slots) <= 2:
        action, src = 'exchange', slots[-1] if slots else 1
    elif m == 'FFREE' and len(operands) == 1 and len(slots) == 1:
        action, src = 'free', slots[0]
    elif m == 'FNSTSW' and operands == (('ax', 0),):
        action = 'status'
    elif m in ('FNSTSW', 'FNSTCW', 'FLDCW') and operands == (('mem', 2),):
        action = 'environment'
    elif m in ('FPREM', 'FPREM1', 'FPTAN', 'FSINCOS', 'FSCALE', 'FPATAN', 'FYL2X', 'FYL2XP1',
               'FTST', 'FXAM', 'FNCLEX', 'FCLEX', 'FNINIT', 'FINIT', 'FDECSTP', 'FINCSTP', 'FNOP') and not operands:
        action = {'FTST': 'compare', 'FPREM': 'remainder', 'FPREM1': 'remainder',
                  'FNCLEX': 'clear', 'FCLEX': 'clear', 'FDECSTP': 'rotate', 'FINCSTP': 'rotate',
                  'FNOP': 'nop'}.get(m, 'runtime')
    if action is None and extended:
        if m in ('EMMS', 'FEMMS') and not operands:
            action = 'empty_tags'
        elif m in ('FST', 'FSTP') and not operands:
            action, pops = 'store', int(m == 'FSTP')
        elif m in ('FCOMI', 'FCOMIP', 'FUCOMI', 'FUCOMIP') and not memory and len(slots) == len(operands) and len(slots) <= 2:
            action, src, pops = 'compare_flags', slots[-1] if slots else 1, int(m.endswith('IP'))
        elif m in ('FCMOVB', 'FCMOVE', 'FCMOVBE', 'FCMOVU', 'FCMOVNB', 'FCMOVNE', 'FCMOVNBE', 'FCMOVNU') and slots and len(slots) == len(operands):
            action, src = 'conditional', slots[-1]
        elif m == 'FXTRACT' and not operands:
            action = 'extract'
        elif m in ('FBSTP', 'FSAVE', 'FNSAVE', 'FRSTOR', 'FNSTENV', 'FSTENV', 'FLDENV') and len(memory) == len(operands) == 1:
            action = 'legacy_environment'
    if action is None:
        raise SSAError('unsupported x87 operand shape %s %r' % (m, operands))
    return Recipe(m, operands, action, bits, slots, dst, src, op, integer, reverse, pops)


def arithmetic(operator, lhs, rhs, sw):
    return helper('fdivz', Type.F64, sw, lhs, rhs) if operator == '/' else binary(operator, lhs, rhs)


def unary(mnem, value, sw, cw):
    if mnem == 'FABS':
        return helper('fabs', Type.F64, value)
    if mnem == 'FCHS':
        return Expr('negate', Type.F64, (value,))
    if mnem == 'FSQRT':
        return helper('fx87', Type.F64, sw, cw, helper('sqrt', Type.F64, value))
    inner = helper('fround', Type.F64, cw, value) if mnem == 'FRNDINT' else helper(
        {'FSIN': 'sin', 'FCOS': 'cos', 'F2XM1': 'expm1'}[mnem], Type.F64,
        binary('*', value, Expr('ln2', Type.F64)) if mnem == 'F2XM1' else value)
    return helper('fx87_exact', Type.F64, sw, inner)


@dataclass(frozen=True)
class Conditional:
    condition: Expr
    operations: tuple


def eager(r, address=None, result=None, decoded=False):
    """Interpret a validated recipe under eager, CPU-resident execution policy."""
    m, bits, slots = r.mnem, r.bits, r.slots
    sw, cw = environment('sw', True), environment('cw', True)
    operations = []
    def call(name, *args, type=Type.VOID):
        return helper(name, type, *args)
    def effect(name, *args):
        operations.append(Evaluate(call(name, *args)))
    def set_slot(index, value):
        effect('fset', CPU, literal(index), value)
    def memory(integer=False):
        value = load(bits, address, integer)
        return cast(Type.F64, value) if decoded and (integer or bits == 32) else value
    if r.action == 'constant':
        effect('fpush', CPU, CONSTANTS[m])
    elif r.action == 'push':
        effect('fpush_st', CPU, literal(slots[0])) if slots else effect('fpush', CPU, memory())
    elif r.action == 'push_integer':
        effect('fpush_int', CPU, load(bits, address, True))
    elif r.action == 'store':
        if slots:
            if slots[0]:
                effect('fcopy', CPU, literal(slots[0]), literal(0))
        elif bits:
            value = helper('fto_float', Type.F32, cw, st(0)) if bits == 32 else st(0)
            operations.append(Store(bits, address, value, True))
    elif r.action == 'store_integer':
        value = Expr('integer_cpu', Type.I64, (CPU,), bits)
        operations.append(Store(bits, address, cast({16: Type.U16, 32: Type.U32, 64: Type.U64}[bits], value)))
    elif r.action == 'arithmetic':
        lhs = st(r.dst)
        if bits:
            operand = Expr('named', Type.F64, data='operand')
            operations.append(Declare(operand))
            operations.append(Assign(operand, memory(r.integer)))
            rhs = operand
        else:
            rhs = st(r.src)
        if r.reverse:
            lhs, rhs = rhs, lhs
        value = arithmetic(r.operator, lhs, rhs, sw)
        set_slot(r.dst, helper('fx87', Type.F64, sw, cw, value))
    elif r.action == 'compare':
        if bits:
            if decoded:
                other = memory(r.integer)
            else:
                other = Expr('named', Type.F64, data='operand')
                operations.extend((Declare(other), Assign(other, memory(r.integer))))
        else:
            other = literal(0.0, Type.F64) if m == 'FTST' else st(r.src)
        effect('fucom' if m.startswith('FU') else 'fcom', sw, st(0), other)
    elif r.action == 'unary':
        set_slot(0, unary(m, st(0), sw, cw))
    elif r.action == 'remainder':
        set_slot(0, helper('fprem', Type.F64, sw, st(0), st(1), literal(int(m == 'FPREM1'))))
    elif m in ('FPTAN', 'FSINCOS'):
        value = Expr('named', Type.F64, data='captured')
        operations.extend((Declare(value), Assign(value, st(0))))
        set_slot(0, helper('fx87_exact', Type.F64, sw, helper('tan' if m == 'FPTAN' else 'sin', Type.F64, value)))
        effect('fpush', CPU, CONSTANTS['FLD1'] if m == 'FPTAN' else
               helper('fx87_exact', Type.F64, sw, helper('cos', Type.F64, value)))
    elif m in ('FSCALE', 'FPATAN', 'FYL2X', 'FYL2XP1'):
        if m == 'FSCALE':
            value, dst = helper('fscale', Type.F64, st(0), st(1)), 0
        elif m == 'FPATAN':
            value, dst = helper('atan2', Type.F64, st(1), st(0)), 1
        else:
            value, dst = binary('*', st(1), helper('log2' if m == 'FYL2X' else 'log1p', Type.F64, st(0))), 1
            if m == 'FYL2XP1':
                value = binary('/', value, Expr('ln2', Type.F64))
        set_slot(dst, helper('fx87_exact', Type.F64, sw, value))
        if dst:
            effect('fdrop', CPU)
    elif r.action == 'exchange':
        effect('fxch', CPU, literal(r.src))
    elif m == 'FXAM':
        effect('fxam', CPU)
    elif r.action == 'clear':
        operations.append(Assign(sw, Expr('clear_mask', Type.U16, data=0x80ff), '&='))
    elif r.action == 'status':
        value = helper('fstsw', Type.U16, CPU)
        if decoded:
            operations.append(Assign(Expr('eax', Type.U32), Expr('ax_merge', Type.U32, (value,))))
        else:
            operations.append(Assign(result, value))
    elif r.action == 'environment':
        if m == 'FLDCW':
            effect('x87_set_cw', CPU, Expr('load_word', Type.U16, (address,)))
        else:
            operations.append(Store(16, address, helper('fstsw', Type.U16, CPU) if m == 'FNSTSW' else cw))
    elif m in ('FNINIT', 'FINIT'):
        effect('x87_finit', CPU)
    elif r.action == 'rotate':
        operations.extend((Assign(environment('top', True), Expr('rotate', Type.U32, (environment('top', True),), -1 if m == 'FDECSTP' else 1)),
                           Assign(sw, Expr('clear_mask', Type.U16, data=0x200), '&=')))
    elif r.action == 'free':
        effect('ftag_put', CPU, Expr('physical', Type.U32, (environment('top', True),), r.src), EMPTY)
    elif r.action == 'compare_flags':
        effect('fucomi' if m.startswith('FU') else 'fcomi', CPU, st(0), st(r.src))
    elif r.action == 'conditional':
        operations.append(Conditional(Expr('flag_condition', Type.BOOL, data=m),
                          (Evaluate(helper('fset', Type.VOID, CPU, literal(0), st(r.src))),)))
    elif r.action == 'extract':
        value = Expr('named', Type.F64, data='captured')
        operations.extend((Declare(value), Assign(value, st(0))))
        set_slot(0, helper('fxtract_exponent', Type.F64, value))
        effect('fpush', CPU, helper('fxtract_significand', Type.F64, value))
    elif r.action == 'legacy_environment':
        if m == 'FBSTP':
            operations.append(Store(80, address, helper('fpop', Type.F64, CPU), bcd=True))
        else:
            name = {'FSAVE': 'x87_fnsave', 'FNSAVE': 'x87_fnsave', 'FRSTOR': 'x87_frstor',
                    'FNSTENV': 'x87_fnstenv', 'FSTENV': 'x87_fnstenv', 'FLDENV': 'x87_fldenv'}[m]
            effect(name, CPU, address)
    elif r.action == 'empty_tags':
        operations.append(EmptyTags(m == 'FEMMS'))
    elif r.action == 'nop':
        operations.append(Evaluate(Expr('nop', Type.VOID)))
    else:
        raise SSAError('unhandled eager x87 recipe %s' % m)
    for _ in range(r.pops):
        effect('fdrop', CPU)
    return tuple(operations)


def validate(operations):
    seen = set()
    kinds = frozenset(('literal', 'constant', 'local', 'named', 'carry', 'environment', 'resident',
        'cw_constant', 'cw_partial', 'hook_control', 'external', 'cpu', 'ln2', 'eax', 'ax_merge', 'st',
        'slot', 'physical', 'rotate', 'binary', 'group', 'cast', 'negate', 'clear_mask', 'nan_fold',
        'nan_test', 'float_store', 'pc_round', 'float_arithmetic', 'status_word', 'load', 'load_word',
        'integer_cpu', 'integer_convert', 'nop', 'helper', 'flag_condition'))
    def expression(value):
        if not isinstance(value, Expr):
            raise SSAError('x87 transition requires a typed expression')
        if id(value) in seen:
            return
        seen.add(id(value))
        if value.kind not in kinds:
            raise SSAError('unknown x87 expression %s' % value.kind)
        for arg in value.args:
            expression(arg)
    for operation in operations:
        if isinstance(operation, Assign):
            expression(operation.target)
            expression(operation.value)
        elif isinstance(operation, (Evaluate, Declare)):
            expression(operation.value)
        elif isinstance(operation, Store):
            expression(operation.address)
            expression(operation.value)
            if operation.address.type != Type.U32:
                raise SSAError('x87 guest memory address must be 32-bit')
        elif isinstance(operation, PublishTags):
            for physical, tag in operation.values:
                expression(physical)
                expression(tag)
                if physical.type != Type.U32 or tag.type != Type.TAG:
                    raise SSAError('x87 publication has incompatible tag operands')
        elif isinstance(operation, Conditional):
            expression(operation.condition)
            validate(operation.operations)
        elif not isinstance(operation, (Activation, Reset, EmptyTags)):
            raise SSAError('unknown x87 transition %s' % type(operation).__name__)
