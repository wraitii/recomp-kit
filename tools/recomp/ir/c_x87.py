"""C printing of typed x87 recipes and state transitions."""
from .x87_transition import (Expr, Type, Assign, Evaluate, Store, PublishTags,
                             Activation, Reset, Declare, Conditional, EmptyTags)
from .x87_cw import Guarded, CW_PC_RC
from .ssa import SSAError

TYPES = {Type.F64: 'double', Type.F32: 'float', Type.U8: 'uint8_t', Type.U16: 'uint16_t',
         Type.U32: 'uint32_t', Type.U64: 'uint64_t', Type.I16: 'int16_t', Type.I32: 'int32_t',
         Type.I64: 'int64_t', Type.CW: 'uint16_t', Type.SW: 'uint16_t', Type.TAG: 'unsigned', Type.BOOL: 'int'}
CONSTANTS = {'FLD1': '1.0', 'FLDZ': '0.0', 'FLDPI': '3.14159265358979323846',
             'FLDLN2': '0.69314718055994530942', 'FLDL2E': '1.44269504088896340736',
             'FLDLG2': '0.30102999566398119521', 'FLDL2T': '3.32192809488736234787'}


def carry_name(position, part):
    tag = 'm%d' % -position if position < 0 else '%d' % position
    return 'x87c%s_%s' % (tag, part)


class Printer:
    def __init__(self, external, decoded=False, conditions=None):
        self.external, self.decoded, self.conditions = external, decoded, conditions or {}

    def expr(self, value):
        kind, data = value.kind, value.data
        args = [self.expr(arg) for arg in value.args]
        if kind == 'literal':
            return 'FTAG_EMPTY' if data == 'empty' else str(data)
        if kind == 'constant':
            return CONSTANTS[data]
        if kind == 'local':
            return 'x87s%d' % data
        if kind == 'named':
            return 'v_' if self.decoded or data == 'captured' else 'operand'
        if kind == 'carry':
            return carry_name(*data)
        if kind == 'environment':
            return 'x87_%s_' % data
        if kind == 'resident':
            return 'c->fpu_%s' % data
        if kind == 'cw_constant':
            return '0u' if isinstance(data[1], Guarded) else '0x%xu' % data[0]
        if kind == 'cw_partial':
            return '((%s & 0x%xu) | 0x%xu)' % (args[0], data[0], data[1])
        if kind == 'hook_control':
            return '(recomp_hooks_ever ? %s : %s)' % tuple(args)
        if kind == 'external':
            return self.external(data)
        if kind == 'cpu':
            return 'c'
        if kind == 'ln2':
            return 'M_LN2'
        if kind == 'flag_condition':
            return self.conditions[data][0]
        if kind == 'eax':
            return 'c->r[0]'
        if kind == 'ax_merge':
            return '(c->r[0] & 0xffff0000u) | %s' % args[0]
        if kind == 'st':
            return 'ST(c, %d)' % data
        if kind == 'slot':
            if data == 'tag':
                return 'ftag_of(c, %s)' % args[0]
            return 'c->%s[%s]' % ({'value': 'st', 'bits': 'st_bits', 'exact': 'st_exact'}[data], args[0])
        if kind == 'physical':
            return '(%s + %du) & 7u' % (args[0], data)
        if kind == 'rotate':
            return '(%s %s 1u) & 7u' % (args[0], '-' if data < 0 else '+')
        if kind == 'binary':
            return '%s %s %s' % (args[0], data, args[1])
        if kind == 'group':
            return '(%s)' % args[0]
        if kind == 'cast':
            return '(%s)%s' % (TYPES[value.type], args[0])
        if kind == 'negate':
            return '-(%s)' % args[0]
        if kind == 'clear_mask':
            return '(uint16_t)~0x%04xu' % data
        if kind == 'nan_fold':
            return '(%s != %s ? x87_indefinite() : %s)' % (args[0], args[0], args[0])
        if kind == 'nan_test':
            return '(uint16_t)(%s != %s)' % (args[0], args[0])
        if kind == 'float_store':
            return '((%s & 0x300u) == 0u && %s == %s) ? (float)(%s) : fto_float_cw(%s, %s)' % (args[0], args[1], args[1], args[1], args[0], args[1])
        if kind == 'pc_round':
            return '((%s & 0x300u) == 0u ? (double)(float)(%s) : (%s))' % (args[0], args[1], args[1])
        if kind == 'float_arithmetic':
            narrow = '(double)((float)(%s) %s (float)(%s))' % (args[1], data, args[2])
            if len(args) == 5:
                narrow = 'fx87_exact_sw(&%s, %s)' % (args[4], narrow)
            return '((%s & 0x300u) == 0u ? %s : %s)' % (args[0], narrow, args[3])
        if kind == 'status_word':
            return '(uint16_t)((%s & (uint16_t)~0x3800u) | ((%s) << 11))' % (args[0], args[1])
        if kind == 'load':
            bits, integer = data
            address = args[0] if self.decoded else '(uint32_t)%s' % args[0]
            return '(int%d_t)rd%d(%s)' % (bits, bits, address) if integer else 'rdf%d(%s)' % (bits, address)
        if kind == 'load_word':
            return 'rd16(%s)' % (args[0] if self.decoded else '(uint32_t)%s' % args[0])
        if kind == 'integer_cpu':
            return 'fist_i%d(c)' % data
        if kind == 'integer_convert':
            return 'fist_i%d_sw(&%s, %s, %s, %s, %s)' % (data, *args)
        if kind == 'nop':
            return ''
        if kind == 'helper':
            resident = bool(value.args and value.args[0].kind == 'resident')
            if data == 'fx87':
                return 'fx87(c, %s)' % args[2] if resident else 'fx87_sw(&%s, %s, %s)' % tuple(args)
            if data == 'fx87_exact':
                return 'fx87_exact(c, %s)' % args[1] if resident else 'fx87_exact_sw(&%s, %s)' % tuple(args)
            if data == 'fround':
                return 'fround_cw(c, %s)' % args[1] if resident else 'fround_rc(%s, %s)' % tuple(args)
            if data == 'fto_float':
                return 'fto_float(c, %s)' % args[1] if resident else 'fto_float_cw(%s, %s)' % tuple(args)
            if data == 'fdivz':
                return 'fdivz(c, %s, %s)' % tuple(args[1:]) if resident else 'fdivz_sw(&%s, %s, %s)' % tuple(args)
            if data == 'fprem':
                return 'fprem_common(c, %s, %s, %s)' % tuple(args[1:]) if resident else 'fprem_sw(&%s, %s, %s, %s)' % tuple(args)
            if data in ('fcom', 'fucom'):
                return '%s(c, %s, %s)' % (data, *args[1:]) if resident else '%s_sw(&%s, %s, %s)' % (data, *args)
            if data == 'fcomi_flags':
                return 'fcomi_flags_sw(&%s, %s, %s, %s)' % tuple(args)
            return '%s(%s)' % (data, ', '.join(args))
        raise SSAError('unknown x87 C expression %s' % kind)

    def declaration(self, declaration):
        return '%s %s;' % (TYPES[declaration.value.type], self.expr(declaration.value))

    def operations(self, operations):
        lines = []
        previous = None
        for operation in operations:
            if isinstance(operation, Reset):
                continue
            if isinstance(operation, Assign):
                text = '%s %s %s;' % (self.expr(operation.target), operation.operator, self.expr(operation.value))
                if lines and isinstance(previous, Declare) and previous.value == operation.target:
                    lines[-1] = lines[-1][:-1] + ' = %s;' % self.expr(operation.value)
                else:
                    lines.append(text)
            elif isinstance(operation, Evaluate):
                text = '%s;' % self.expr(operation.value)
                if self.decoded and isinstance(previous, Evaluate) and previous.value.data == operation.value.data == 'fdrop':
                    lines[-1] += ' ' + text
                else:
                    lines.append(text)
            elif isinstance(operation, Store):
                address = self.expr(operation.address)
                if not self.decoded:
                    address = '(uint32_t)%s' % address
                lines.append('wr%s%d(%s, %s);' % ('bcd' if operation.bcd else 'f' if operation.floating else '', operation.bits, address, self.expr(operation.value)))
            elif isinstance(operation, Declare):
                lines.append(self.declaration(operation))
            elif isinstance(operation, PublishTags):
                tags = [(self.expr(physical), self.expr(tag)) for physical, tag in operation.values]
                masks = ' | '.join('(3u << (2u * (%s)))' % physical for physical, _ in tags)
                values = ' | '.join('((%s) << (2u * (%s)))' % (tag, physical) for physical, tag in tags)
                lines.append('c->fpu_tag = (uint16_t)((c->fpu_tag & ~(%s)) | %s);' % (masks, values))
            elif isinstance(operation, EmptyTags):
                if operation.reset_top:
                    lines.append('c->fpu_top = 0u;')
                lines.append('c->fpu_tag = 0xffffu;')
            elif isinstance(operation, Activation):
                part = ['x87_top_ = c->fpu_top;', 'x87_cw_ = c->fpu_cw;']
                if operation.clone == 'fast' and not operation.known_mode:
                    part.append('if (RECOMP_UNLIKELY(recomp_hooks_ever || (x87_cw_ & 0x%xu) != 0u)) goto X87CW%d;' % (CW_PC_RC, operation.index))
                elif operation.clone == 'general':
                    part.insert(0, 'X87CW%d:;' % operation.index)
                lines.extend((*part, 'x87_sw_ = c->fpu_sw;'))
            elif isinstance(operation, Conditional):
                lines.append('if (%s) { %s }' % (self.expr(operation.condition), ' '.join(self.operations(operation.operations))))
            else:
                raise SSAError('unknown x87 C transition %s' % type(operation).__name__)
            previous = operation
        return lines
