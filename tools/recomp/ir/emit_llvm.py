"""LLVM integer CFG emission with typed C bridges for finalized effects."""
from dataclasses import dataclass

from . import c_symbols, lowering
from .body import EntryForm
from .c_boundaries import Printer as BoundaryPrinter, CC_OP_CONST
from .c_x87 import Printer as X87Printer
from .ssa import SSAError
from .llvm_cfg import Graph
from .x87_transition import Activation, Assign
from .x87_cw import condition_of, CW_PC_RC


@dataclass(frozen=True)
class Artifact:
    symbol: str
    llvm: str
    bridges: str


class BridgeX87Printer(X87Printer):
    def __init__(self, external, result, local):
        super().__init__(external, local=local)
        self.result = result

    def operations(self, sequence):
        lines, pending = [], []
        for operation in sequence:
            if isinstance(operation, Assign) and operation.target.kind == 'external':
                if operation.operator != '=':
                    raise SSAError('LLVM unsupported x87 external update ' + operation.operator)
                lines.extend(super().operations(pending))
                pending = []
                lines.append('%s = %s;' % (self.result(operation.target.data), self.expr(operation.value)))
            else:
                pending.append(operation)
        return [*lines, *super().operations(pending)]


class Emitter:
    def __init__(self, prepared):
        if prepared.final is None or prepared.transport is None:
            raise SSAError('LLVM emission requires finalized boundary operations')
        if prepared.seh_mark or prepared.ir.seh:
            raise SSAError('LLVM unsupported operation SEH: setjmp must remain in its owning frame')
        self.p, self.s = prepared, prepared.ssa
        self.symbol = 'llvm_' + prepared.options.symbol
        self.frame = self.symbol + '_frame'
        values = [v for v in self.s.values if v.size and v.id in prepared.live
                  and self.s.resolve(v) is v and v.opc not in ('CONST', 'TARGET', 'CC_KIND')]
        self.value_ids = {v.id for v in values}
        self.graph = Graph()
        self.bridges, self.declarations = [], []
        self.c_inputs, self.c_outputs = {}, {}
        self.serial = 0
        self.edges = {}
        self.boundary = BoundaryPrinter(prepared.transport,
            {name: c_symbols.cpu_field(name) for name, keys in prepared.state_groups},
            self.cref, self.mask, self.cresult, lambda event: 'f->call%d_' % event)
        self.boundary.arguments = {field: 'f->' + c_symbols.argument(field)
                                   for field in getattr(prepared.transport, 'entry_fields', ())}
        self.x87 = BridgeX87Printer(self.cref, self.cresult, local=lambda name: 'f->' + name)
        self.base = X87Printer(self.cref)
        self.context = ['uint32_t expected_return_ = f->expected;',
                        'int checked_return_ = f->checked;']
        layout = self.layout
        if layout is not None:
            self.context += ['%s *out_ = f->out;' % c_symbols.result_type(layout, prepared.ir.addr)]

    @property
    def layout(self):
        return self.p.options.request.layout if isinstance(self.p.transport, lowering.ValueTransport) else None

    def mask(self, size):
        return '0x%xull' % ((1 << (size * 8)) - 1)

    def cref(self, value):
        value = self.s.resolve(value)
        if value.opc in ('CONST', 'TARGET'):
            return '0x%xull' % value.data
        if value.opc == 'CC_KIND':
            return CC_OP_CONST[value.data]
        self.c_inputs[value.id] = value
        return 'arg_v%d' % value.id

    def line(self, text):
        self.graph.line(text)

    def temp(self, expression):
        name = '%%t%d' % self.serial
        self.serial += 1
        self.line(name + ' = ' + expression)
        return name

    def label(self, name):
        self.graph.block(name)

    def fresh(self, prefix):
        name = '%s%d' % (prefix, self.serial)
        self.serial += 1
        return name

    def cast(self, value, source, target, signed=False):
        if source == target:
            return value
        return self.temp('%s i%d %s to i%d' % (
            'trunc' if source > target else 'sext' if signed else 'zext', source, value, target))

    def cresult(self, value):
        self.c_outputs[value.id] = value
        return 'result_v%d' % value.id

    def ref(self, value, bits=None):
        value = self.s.resolve(value)
        width = value.size * 8
        if value.opc in ('CONST', 'TARGET'):
            return str(value.data & ((1 << (bits or width)) - 1))
        if value.opc == 'CC_KIND':
            result = self.bridge(['return %s;' % CC_OP_CONST[value.data]], result='i64')
            return self.cast(result, 64, bits or width)
        return self.cast(self.graph.read(value), width, bits or width)

    def store(self, value, result, bits=None):
        if value.id not in self.value_ids:
            return
        width = value.size * 8
        result = self.cast(result, bits or width, width)
        self.graph.write(value, result)

    def bridge(self, statements, predicate=False, result=None):
        if not statements:
            return None
        inputs, outputs = self.c_inputs, self.c_outputs
        self.c_inputs, self.c_outputs = {}, {}
        name = self.fresh(self.symbol + '_h')
        result = result or ('i32' if predicate else 'i64' if len(outputs) == 1 else 'void')
        c_type = {'i32': 'int', 'i64': 'uint64_t', 'void': 'void'}[result]
        parameters = ['X86 *c', 'struct %s *f' % self.frame]
        parameters.extend('uint64_t arg_v%d' % value for value in inputs)
        types = ['ptr', 'ptr', *('i64' for _ in inputs)]
        arguments = ['ptr %c', 'ptr %frame']
        arguments.extend('i64 ' + self.ref(value, 64) for value in inputs.values())
        statements = list(statements)
        if len(outputs) > 1:
            parameters.append('uint64_t *outputs')
            types.append('ptr')
            buffer = self.temp('alloca [%d x i64], align 8' % len(outputs))
            arguments.append('ptr ' + buffer)
            statements.extend('outputs[%d] = result_v%d;' % (index, value)
                              for index, value in enumerate(outputs))
        elif outputs:
            statements.append('return result_v%d;' % next(iter(outputs)))
        self.declarations.append('declare %s @%s(%s)' % (result, name, ', '.join(types)))
        self.bridges += ['%s %s(%s) {' % (c_type, name, ', '.join(parameters)),
                         '(void)c; (void)f;', *self.context,
                         *('uint64_t result_v%d;' % value for value in outputs), *statements, '}']
        call = 'call %s @%s(%s)' % (result, name, ', '.join(arguments))
        operand = self.temp(call) if result != 'void' else self.line(call)
        if len(outputs) > 1:
            for index, value in enumerate(outputs.values()):
                pointer = self.temp('getelementptr i64, ptr %s, i32 %d' % (buffer, index))
                loaded = self.temp('load i64, ptr %s, align 8' % pointer)
                self.store(value, loaded, 64)
        elif outputs:
            self.store(next(iter(outputs.values())), operand, 64)
        return operand

    def boundaries(self, sequence):
        for operation in sequence:
            if isinstance(operation, lowering.Guard):
                condition = self.bridge(['return %s;' % self.boundary.condition(operation)], True)
                test = self.temp('icmp ne i32 %s, 0' % condition)
                taken, other, merge = (self.fresh('guard') for _ in range(3))
                self.line('br i1 %s, label %%%s, label %%%s' % (test, taken, other))
                self.label(taken)
                ended = self.boundaries(operation.taken)
                if not ended:
                    self.line('br label %' + merge)
                self.label(other)
                other_ended = self.boundaries(operation.otherwise)
                if not other_ended:
                    self.line('br label %' + merge)
                if ended and other_ended:
                    return True
                self.label(merge)
            else:
                self.bridge(self.boundary.operation(operation))
                if isinstance(operation, lowering.Return):
                    self.line('ret void')
                    return True
        return False

    def x87_ops(self, sequence, clone):
        pending = []
        for operation in sequence:
            if isinstance(operation, Activation):
                self.bridge(self.x87.operations(pending))
                pending = []
                if clone == 'general':
                    label = 'X87CW%d' % operation.index
                    self.line('br label %' + label)
                    self.label(label)
                statements = ['f->x87_top_ = c->fpu_top;', 'f->x87_cw_ = c->fpu_cw;']
                self.bridge(statements)
                if clone == 'fast' and not operation.known_mode:
                    condition = self.bridge(['return recomp_hooks_ever || (f->x87_cw_ & 0x%xu) != 0u;' % CW_PC_RC], True)
                    test = self.temp('icmp ne i32 %s, 0' % condition)
                    resume = self.fresh('activation')
                    self.line('br i1 %s, label %%X87CW%d, label %%%s' % (test, operation.index, resume))
                    self.label(resume)
                self.bridge(['f->x87_sw_ = c->fpu_sw;'])
            else:
                pending.append(operation)
        self.bridge(self.x87.operations(pending))

    def integer(self, v):
        opc, bits = v.opc, v.size * 8
        args = v.args
        width = args[0].size * 8 if args else bits
        a = self.ref(args[0]) if args else None
        if opc in ('COPY', 'INT_ZEXT', 'INT_SEXT'):
            self.store(v, self.cast(a, width, bits, opc == 'INT_SEXT'))
            return
        if opc == 'PACK':
            result = '0'
            for n, arg in enumerate(args):
                part = self.ref(arg, bits)
                if n:
                    part = self.temp('shl i%d %s, %d' % (bits, part, n * 8))
                result = self.temp('or i%d %s, %s' % (bits, result, part))
            self.store(v, result)
            return
        if opc == 'BYTE':
            a = self.temp('lshr i%d %s, %d' % (width, a, v.data * 8))
            self.store(v, a, width)
            return
        if opc == 'POPCOUNT':
            decl = 'declare i%d @llvm.ctpop.i%d(i%d)' % (width, width, width)
            if decl not in self.declarations:
                self.declarations.append(decl)
            result = self.temp('call i%d @llvm.ctpop.i%d(i%d %s)' % (width, width, width, a))
            self.store(v, result, width)
            return
        if opc in ('INT_NEGATE', 'INT_2COMP'):
            result = self.temp(('xor i%d %s, -1' if opc == 'INT_NEGATE' else 'sub i%d 0, %s') % (width, a))
            self.store(v, result, width)
            return
        if opc == 'BOOL_NEGATE':
            result = self.temp('icmp eq i%d %s, 0' % (width, a))
            self.store(v, result, 1)
            return
        if opc == 'BOOL_XOR':
            result = self.temp('icmp ne i%d %s, %s' % (width, a, self.ref(args[1], width)))
            self.store(v, result, 1)
            return
        if opc in ('BOOL_AND', 'BOOL_OR'):
            b = self.ref(args[1])
            a = self.temp('icmp ne i%d %s, 0' % (width, a))
            b = self.temp('icmp ne i%d %s, 0' % (args[1].size * 8, b))
            result = self.temp('%s i1 %s, %s' % ({'BOOL_AND': 'and', 'BOOL_OR': 'or', 'BOOL_XOR': 'xor'}[opc], a, b))
            self.store(v, result, 1)
            return
        if opc == 'PIECE':
            a = self.cast(a, width, bits)
            a = self.temp('shl i%d %s, %d' % (bits, a, args[1].size * 8))
            result = self.temp('or i%d %s, %s' % (bits, a, self.ref(args[1], bits)))
            self.store(v, result)
            return
        if opc in ('INT_LEFT', 'INT_RIGHT', 'INT_SRIGHT', 'SUBPIECE'):
            count_width = max(64, args[1].size * 8)
            count = self.ref(args[1], count_width)
            limit = 8 if opc == 'SUBPIECE' else width
            invalid = self.temp('icmp uge i%d %s, %d' % (count_width, count, limit))
            safe = self.temp('select i1 %s, i%d 0, i%d %s' % (invalid, count_width, count_width, count))
            if opc == 'SUBPIECE':
                safe = self.temp('mul i%d %s, 8' % (count_width, safe))
                a = self.cast(a, width, 64)
                width = 64
            safe = self.cast(safe, count_width, width)
            operation = {'INT_LEFT': 'shl', 'INT_RIGHT': 'lshr', 'INT_SRIGHT': 'ashr', 'SUBPIECE': 'lshr'}[opc]
            shifted = self.temp('%s i%d %s, %s' % (operation, width, a, safe))
            saturation = self.temp('ashr i%d %s, %d' % (width, a, width - 1)) if opc == 'INT_SRIGHT' else '0'
            result = self.temp('select i1 %s, i%d %s, i%d %s' % (invalid, width, saturation, width, shifted))
            self.store(v, result, width)
            return
        b = self.ref(args[1], width)
        binary = {'INT_ADD': 'add', 'INT_SUB': 'sub', 'INT_MULT': 'mul', 'INT_AND': 'and', 'INT_OR': 'or', 'INT_XOR': 'xor'}
        comparisons = {'INT_EQUAL': 'eq', 'INT_NOTEQUAL': 'ne', 'INT_LESS': 'ult', 'INT_LESSEQUAL': 'ule',
                       'INT_SLESS': 'slt', 'INT_SLESSEQUAL': 'sle'}
        if opc in binary:
            result = self.temp('%s i%d %s, %s' % (binary[opc], width, a, b))
        elif opc in comparisons:
            result = self.temp('icmp %s i%d %s, %s' % (comparisons[opc], width, a, b))
            width = 1
        elif opc == 'INT_CARRY':
            extended_a = self.cast(a, width, width + 1)
            extended_b = self.cast(b, width, width + 1)
            summed = self.temp('add i%d %s, %s' % (width + 1, extended_a, extended_b))
            result = self.temp('icmp ugt i%d %s, %d' % (width + 1, summed, (1 << width) - 1))
            width = 1
        elif opc in ('INT_SCARRY', 'INT_SBORROW'):
            result = self.temp('%s i%d %s, %s' % ('add' if opc == 'INT_SCARRY' else 'sub', width, a, b))
            ab = self.temp('xor i%d %s, %s' % (width, a, b))
            if opc == 'INT_SCARRY':
                ab = self.temp('xor i%d %s, -1' % (width, ab))
            ar = self.temp('xor i%d %s, %s' % (width, a, result))
            overflow = self.temp('and i%d %s, %s' % (width, ab, ar))
            result = self.temp('icmp slt i%d %s, 0' % (width, overflow))
            width = 1
        else:
            raise SSAError('LLVM unsupported integer operation ' + opc)
        self.store(v, result, width)

    def edge(self, clone, source, target):
        key = clone, source, target
        if key not in self.edges:
            self.edges[key] = self.fresh('edge')
        return self.edges[key]

    def emit(self):
        p, s, arithmetic = self.p, self.s, self.p.arithmetic
        signature = 'define void @%s(ptr %%c, ptr %%frame, i32 %%entry) {' % self.symbol
        self.label('start')
        self.boundaries(p.transport.boundaries.entry)
        init = []
        for v in s.values:
            if v.id not in self.value_ids or v.opc != 'INPUT':
                continue
            field_name, lane = p.mapping[v.data]
            field = c_symbols.cpu_field(field_name)
            field_name = p.options.lane_field.get(v.data)
            if self.layout is not None and field_name in self.layout.inputs:
                field = 'f->' + c_symbols.argument(field_name)
            init.append('%s = (%s >> %d) & %s;' % (self.cresult(v), field, lane * 8, self.mask(v.size)))
        verify = p.options.request.entry_verify
        if verify:
            condition = condition_of(getattr(verify, 'condition', verify))
            init.append('if (!recomp_hooks_ever && (c->fpu_cw & 0x%xu) != 0x%xu) recomp_x87_contract_violation(c, 0x%08xu, 0x%xu, 0x%xu);' % (condition.mask, condition.value, p.ir.addr, condition.mask, condition.value))
        self.bridge(init)
        for v in s.entry_ops:
            if v.id in self.value_ids:
                self.integer(v)
        first = arithmetic.programs[0].clone
        entry_edge = self.edge(first, -1, s.entry)
        if p.ir.entries:
            cases = ' '.join('i32 %d, label %%%s' % (addr, self.edge(first, -1, block))
                             for addr, block in zip(p.ir.entries, s.alternates))
            self.line('switch i32 %%entry, label %%%s [ %s ]' % (entry_edge, cases))
        else:
            self.line('br label %' + entry_edge)
        for program in arithmetic.programs:
            clone = program.clone
            prefix = 'F' if clone == 'fast' else 'B'
            for index, block in s.blocks.items():
                self.label(prefix + str(index))
                predecessors = [source for source, target in p.transport.boundaries.edges if target == index
                                and (source != -1 or clone == first)]
                for source in predecessors:
                    self.edge(clone, source, index)
                self.x87_ops(program.blocks[index], clone)
                terminated = False
                for v in block.ops:
                    if v.opc not in ('X87_REG', 'X87_MEM'):
                        self.x87_ops(program.events[v.id], clone)
                    if v.opc == 'MEMORY':
                        continue
                    if v.opc in ('X87_REG', 'X87_MEM'):
                        if v.opc == 'X87_MEM':
                            self.boundaries((p.transport.boundaries.publications[v.id],))
                        self.x87_ops(program.events[v.id], clone)
                    elif v.id in p.transport.boundaries.events:
                        terminated = self.boundaries(p.transport.boundaries.events[v.id])
                    elif v.opc in ('LOAD', 'STORE', 'DIV32', 'IDIV32', 'STRINGOP'):
                        self.boundaries((p.transport.boundaries.publications[v.id],))
                        if v.opc == 'LOAD':
                            statements = ['%s = rd%d((uint32_t)%s);' % (self.cresult(v), v.size * 8, self.cref(v.args[1]))]
                        elif v.opc == 'STORE':
                            statements = ['wr%d((uint32_t)%s, (uint%d_t)%s);' % (v.args[2].size * 8, self.cref(v.args[1]), v.args[2].size * 8, self.cref(v.args[2]))]
                        elif v.opc == 'STRINGOP':
                            statements = ['%s(c);' % v.data['helper']]
                        else:
                            statements = ['%s(c, (uint32_t)%s, (uint32_t)%s);' % ('div32' if v.opc == 'DIV32' else 'idiv32', self.cref(v.args[2]), self.cref(v.args[3])),
                                          '%s = c->r[R_EAX] | ((uint64_t)c->r[R_EDX] << 32);' % self.cresult(v)]
                        self.bridge(statements)
                    elif v.opc == 'TRAP':
                        self.bridge(['recomp_unknown_call(c, 0x%xu);' % v.data])
                        self.line('ret void')
                        terminated = True
                    elif v.opc == 'BRANCH':
                        target = arithmetic.indices[v.args[0].data]
                        self.line('br label %' + self.edge(clone, index, target))
                        terminated = True
                    elif v.opc == 'CBRANCH':
                        condition = self.ref(v.args[1])
                        test = self.temp('icmp ne i%d %s, 0' % (v.args[1].size * 8, condition))
                        target = arithmetic.indices[v.args[0].data]
                        other = arithmetic.indices[block.insn.addr + block.insn.length]
                        self.line('br i1 %s, label %%%s, label %%%s' % (test, self.edge(clone, index, target), self.edge(clone, index, other)))
                        terminated = True
                    elif v.opc == 'BRANCHIND':
                        target = self.ref(v.args[0], 32)
                        default = self.fresh('jump_default')
                        cases = ' '.join('i32 %d, label %%%s' % (p.ir.insns[j].addr, self.edge(clone, index, j)) for j in sorted(set(p.ir.succ[index])))
                        self.line('switch i32 %s, label %%%s [ %s ]' % (target, default, cases))
                        self.label(default)
                        self.x87_ops(program.defaults.get(v.id, ()), clone)
                        self.boundaries((p.transport.boundaries.publications[v.id],))
                        self.bridge(['c->eip = 0x%xu; recomp_jump(c, (uint32_t)%s);' % (block.insn.addr, self.cref(v.args[0]))])
                        self.line('ret void')
                        terminated = True
                    elif v.opc == 'SEH':
                        raise SSAError('LLVM unsupported operation SEH')
                    else:
                        self.integer(v)
                    if terminated:
                        break
                if not terminated:
                    self.x87_ops(program.ends[index], clone)
                    self.line('br label %' + self.edge(clone, index, p.ir.succ[index][0]))
        for (clone, source, target), name in self.edges.items():
            self.label(name)
            edge = p.transport.boundaries.edges[source, target]
            copies = [(dst, self.ref(src, dst.size * 8)) for dst, src in edge.copies if dst.id in self.value_ids]
            for dst, operand in copies:
                self.store(dst, operand)
            self.line('br label %' + ('F' if clone == 'fast' else 'B') + str(target))
        lines = [signature, *self.graph.render(), '}']
        members = ['uint32_t expected;', 'int checked;']
        members += [self.base.declaration(d) for d in (*arithmetic.declarations(), *arithmetic.environment_declarations(), *arithmetic.temporaries)]
        if isinstance(p.transport, lowering.ValueTransport):
            members += ['%s call%d_;' % (c_symbols.result_type(values.layout, p.invocations[event].target), event)
                        for event, values in p.transport.calls.items()]
        if self.layout is not None:
            members += ['%s *out;' % c_symbols.result_type(self.layout, p.ir.addr)]
            members += ['%s %s;' % (c_symbols.field_type(field), c_symbols.argument(field)) for field in self.layout.inputs]
        header = ['struct %s {' % self.frame, *dict.fromkeys(members), '};',
                  'void %s(X86 *, void *, uint32_t);' % self.symbol]
        wrapper = [c_symbols.signature(p.options.request.variant, p.options.symbol, self.layout, p.ir.addr),
                   'struct %s f;' % self.frame]
        entry = p.options.request.variant.entry
        wrapper += ['f.expected = %s;' % ('expected_return_' if entry in (EntryForm.CHECKED, EntryForm.PRIVATE) else '0u'),
                    'f.checked = %s;' % ('checked_return_' if entry == EntryForm.CHECKED else '0')]
        if self.layout is not None:
            wrapper += ['f.out = out_;'] + ['f.%s = %s;' % (c_symbols.argument(field), c_symbols.argument(field)) for field in self.layout.inputs]
        wrapper += ['%s(c, &f, %s);' % (self.symbol, 'entry_' if entry == EntryForm.MULTIPLE else '0u'), '}']
        return Artifact(self.symbol, '\n'.join((*self.declarations, *lines)), '\n'.join((*header, *self.bridges, *wrapper)))


def emit_prepared(prepared):
    return Emitter(prepared).emit()
