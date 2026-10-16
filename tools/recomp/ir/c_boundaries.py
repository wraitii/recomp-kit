"""C printing for finalized CPU-field boundary operations."""
from . import c_symbols, flag_region
from .lowering import (Publish, Invoke, IndirectInvoke, SettleFlags, Guard, Reload, Preserve, Return)


CC_OP_CONST = {"add": "X86_CC_ADD", "sub": "X86_CC_SUB", "cmp": "X86_CC_SUB",
               "logic": "X86_CC_LOGIC", "inc": "X86_CC_INC", "dec": "X86_CC_DEC",
               "adc": "X86_CC_ADC", "sbb": "X86_CC_SBB"}

_CC_FLAG_BITS = {"cf": 1, "pf": 2, "af": 4, "zf": 8, "sf": 0x10, "of": 0x20}
#: Kinds whose carry/borrow and add/sub flag recipes read both operands.
_CC_AB_KINDS = frozenset(("add", "adc", "sub", "sbb", "cmp"))
#: Kinds whose AF/OF recipes read the first operand (INC/DEC read only `a`).
_CC_A_KINDS = _CC_AB_KINDS | frozenset(("inc", "dec"))


def cc_operand_names(kind, mask):
    """Operands `x86_cc_materialize` can read for `kind` and `mask`.

    The wrapped result `cc_res` is always stored: every non-logic recipe
    derives from it, and LOGIC's CF/OF/AF are constants.  A joined descriptor
    carries a PHI kind, so every operand stays.
    """
    if kind not in CC_OP_CONST:
        return frozenset(("a", "b", "carry"))
    additive = mask & (_CC_FLAG_BITS["cf"] | _CC_FLAG_BITS["af"] | _CC_FLAG_BITS["of"])
    names = set()
    if additive and kind in _CC_A_KINDS:
        names.add("a")
    if additive and kind in _CC_AB_KINDS:
        names.add("b")
    if kind in ("adc", "sbb") and mask & _CC_FLAG_BITS["cf"]:
        names.add("carry")
    return frozenset(names)


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
    def __init__(self, transport, fields, ref, mask, cache_name):
        self.plan = transport.boundaries
        self.fields, self.ref, self.mask = fields, ref, mask
        self.cache_name = cache_name

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
                used = cc_operand_names(record.kind, mask)
                lines.extend(["c->cc_op = %s;" % (self.ref(record.op) if record.op is not None else CC_OP_CONST[record.kind]),
                              "c->cc_size = %du;" % record.size,
                              "c->cc_mask = 0x%xu;" % mask])
                if "a" in used:
                    lines.append("c->cc_a = (uint32_t)%s;" % (self.ref(record.a) if record.a is not None else "0"))
                if "b" in used:
                    lines.append("c->cc_b = (uint32_t)%s;" % (self.ref(record.b) if record.b is not None else "0"))
                lines.append("c->cc_res = (uint32_t)%s;" % self.ref(record.res))
                if "carry" in used and record.carry is not None:
                    lines.append("c->cc_carry = (uint8_t)%s;" % self.ref(record.carry))
            lines.extend(["c->cc_op = X86_CC_NONE;"] * operation.clear_flags)
            return lines
        if isinstance(operation, Invoke):
            input_value = self.value(operation.input_value) if operation.input_value is not None else None
            return ["%s;" % c_symbols.render_invocation(operation.invocation, input_value)]
        if isinstance(operation, IndirectInvoke):
            target = "(uint32_t)%s" % self.ref(operation.target)
            if operation.helper == "recomp_call":
                return ["recomp_icall(c, %s, &%s);" % (target, self.cache_name())]
            return ["%s(c, %s);" % (operation.helper, target)]
        if isinstance(operation, SettleFlags):
            return cc_action_lines(operation.action)
        if isinstance(operation, Guard):
            condition = self.condition(operation)
            if operation.kind == "eligible" and operation.otherwise:
                return ["if (RECOMP_UNLIKELY(!(%s))) {" % condition,
                        *self.operations(operation.otherwise), "} else {",
                        *self.operations(operation.taken), "}"]
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
            dispatch = {"none": [], "always": ["recomp_return_fast_(c);"],
                        "checked": ["if (!checked_return_ || RECOMP_UNLIKELY(c->eip != expected_return_)) recomp_return_fast_(c);"]}
            return [*dispatch[operation.dispatch], "return;"]
        raise ValueError("unknown C boundary operation %s" % type(operation).__name__)

    def publication(self, event):
        return self.operation(self.plan.publications[event.id])

    def event(self, event):
        return self.operations(self.plan.events[event.id])

    def edge(self, source, target, label, fallthrough=None):
        edge = self.plan.edges[source, target]
        lines = ["{"]
        lines.extend("uint%d_t p%d = %s;" % (32 if dst.size <= 4 else 64, dst.id, self.ref(src)) for dst, src in edge.copies)
        lines.extend("v%d = p%d;" % (dst.id, dst.id) for dst, _ in edge.copies)
        if edge.target != fallthrough:
            lines.append("goto %s%d;" % (label, edge.target))
        return [*lines, "}"] if edge.copies or edge.target != fallthrough else []

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
