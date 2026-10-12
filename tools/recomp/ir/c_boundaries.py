"""C printing for finalized CPU-field boundary operations."""
from . import c_symbols, flag_region
from .lowering import (Publish, Invoke, IndirectInvoke, SettleFlags, Guard, Reload, Preserve, Return)


CC_OP_CONST = {"add": "X86_CC_ADD", "sub": "X86_CC_SUB", "cmp": "X86_CC_SUB",
               "logic": "X86_CC_LOGIC", "inc": "X86_CC_INC", "dec": "X86_CC_DEC",
               "adc": "X86_CC_ADC", "sbb": "X86_CC_SBB"}


def cc_action_lines(action):
    if action == flag_region.DROP:
        return ["x86_cc_drop(c);"]
    if action == flag_region.REMOVE:
        return []
    if isinstance(action, tuple):
        helper, flags = action
        bits = " | ".join("X86_CCF_%s" % f.upper() for f in sorted(flags))
        return ["%s(c, %s);" % (helper, bits)]
    return ["x86_cc_settle(c);"]


class Printer:
    def __init__(self, transport, fields, ref, mask):
        self.plan = transport.boundaries
        self.fields, self.ref, self.mask = fields, ref, mask

    def value(self, value):
        if value.whole is not None:
            return self.ref(value.whole)
        field = self.fields[value.field]
        return " | ".join("(%s << %d)" % (self.ref(v), n * 8) if v is not None
                          else "(%s & 0x%xu)" % (field, 255 << (n * 8))
                          for n, v in enumerate(value.lanes))

    def operations(self, operations):
        lines = []
        previous = None
        for operation in operations:
            if isinstance(operation, Return) and operation.dispatch == "none" and isinstance(previous, Invoke):
                lines[-1] += " return;"
                previous = operation
                continue
            lines.extend(self.operation(operation))
            previous = operation
        return lines

    def operation(self, operation):
        if isinstance(operation, Publish):
            lines = ["%s = (uint32_t)(%s);" % (self.fields[v.field], self.value(v))
                     for v in operation.values]
            if operation.flags is not None:
                record, mask = operation.flags.record, operation.flags.mask
                lines.extend(["c->cc_op = %s;" % (self.ref(record.op) if record.op is not None else CC_OP_CONST[record.kind]),
                              "c->cc_size = %du;" % record.size,
                              "c->cc_mask = 0x%xu;" % mask,
                              "c->cc_a = (uint32_t)%s;" % (self.ref(record.a) if record.a is not None else "0"),
                              "c->cc_b = (uint32_t)%s;" % (self.ref(record.b) if record.b is not None else "0"),
                              "c->cc_res = (uint32_t)%s;" % self.ref(record.res)])
                if record.carry is not None:
                    lines.append("c->cc_carry = (uint8_t)%s;" % self.ref(record.carry))
            lines.extend(["c->cc_op = X86_CC_NONE;"] * operation.clear_flags)
            return lines
        if isinstance(operation, Invoke):
            return ["%s;" % c_symbols.render_invocation(operation.invocation)]
        if isinstance(operation, IndirectInvoke):
            return ["%s(c, (uint32_t)%s);" % (operation.helper, self.ref(operation.target))]
        if isinstance(operation, SettleFlags):
            return cc_action_lines(operation.action)
        if isinstance(operation, Guard):
            condition = self.condition(operation)
            lines = ["if (%s) {" % condition, *self.operations(operation.taken)]
            if operation.otherwise:
                lines.extend(["} else {", *self.operations(operation.otherwise)])
            return [*lines, "}"]
        if isinstance(operation, Reload):
            return ["v%d = (%s >> %d) & %s;" % (operation.result.id, self.fields[operation.field],
                                              operation.lane * 8, self.mask(operation.result.size))]
        if isinstance(operation, Preserve):
            kept = (self.ref(operation.whole) if operation.whole is not None else
                    " | ".join("(%s << %d)" % (self.ref(v), n * 8) for n, v in enumerate(operation.values)))
            settle = "x86_cc_settle(c), " if operation.settle_flags else ""
            return ["v%d = RECOMP_UNLIKELY(recomp_hooks_ever) ? (%s(%s >> %d) & %s) : (%s);"
                    % (operation.result.id, settle, self.fields[operation.field], operation.lane * 8,
                       self.mask(operation.result.size), kept)]
        if isinstance(operation, Return):
            dispatch = {"none": [], "always": ["recomp_return(c);"],
                        "checked": ["if (!checked_return_ || RECOMP_UNLIKELY(c->eip != expected_return_)) recomp_return(c);"]}
            return [*dispatch[operation.dispatch], "return;"]
        raise ValueError("unknown C boundary operation %s" % type(operation).__name__)

    def publication(self, event):
        return self.operation(self.plan.publications[event.id])

    def event(self, event):
        return self.operations(self.plan.events[event.id])

    def edge(self, source, target, label):
        edge = self.plan.edges[source, target]
        lines = ["{"]
        lines.extend("uint64_t p%d = %s;" % (dst.id, self.ref(src)) for dst, src in edge.copies)
        lines.extend("v%d = p%d;" % (dst.id, dst.id) for dst, _ in edge.copies)
        return [*lines, "goto %s%d;" % (label, edge.target), "}"]

    def condition(self, operation):
        if operation.kind == "eligible":
            preparation = operation.operand
            condition = "%s(c, %du)" % (c_symbols.symbol("entry_contract_prepare", preparation.target),
                                1 if preparation.mode_proven else 0)
        elif operation.kind == "hooks":
            condition = "RECOMP_UNLIKELY(recomp_hooks_ever)"
        elif operation.kind == "return_mismatch":
            condition = "RECOMP_UNLIKELY(%s != expected_return_)" % self.value(operation.operand)
        else:
            raise ValueError("unknown C boundary guard %s" % operation.kind)
        return condition
