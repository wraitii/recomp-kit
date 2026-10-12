"""C spellings for semantic entries, layouts and body signatures."""
from dataclasses import dataclass
from .body import EntryForm, BodyVariant, ReturnForm
from .guest import X86_32


@dataclass(frozen=True)
class Declaration:
    kind: str
    target: int
    mask: int = 0
    layout: object | None = None


def field_type(field):
    return "uint%d_t" % next(register.width for register in X86_32.registers if register.name == field)


def argument(field):
    return "in_%s_" % field.lower()


def result_type(layout, target):
    return symbol("PrivateResult", target, layout.output_mask)


def result_declaration(layout, target):
    name = result_type(layout, target)
    fields = " ".join("%s %s;" % (field_type(field), field.lower()) for field in layout.outputs)
    return "#ifndef %s_DEFINED\n#define %s_DEFINED\ntypedef struct { %s } %s;\n#endif" % (
        name, name, fields or "uint8_t unused;", name)


def value_parameters(layout, target, expected="expected"):
    return ", ".join(("X86 *c", "uint32_t %s" % expected,
                      *("%s %s" % (field_type(field), argument(field)) for field in layout.inputs),
                      "%s *out_" % result_type(layout, target)))


def value_signature(name, layout, target):
    return "void %s(%s)" % (name, value_parameters(layout, target))


def value_arguments(layout, expected="expected"):
    return ", ".join(("c", expected, *(argument(field) for field in layout.inputs), "out_"))


def invocation_declarations(invocation):
    if invocation.form == "private":
        return frozenset({Declaration("private_prepare", invocation.target),
                          Declaration("private_contract", invocation.target, invocation.output_mask)}) | invocation_declarations(invocation.fallback)
    if invocation.symbol is not None:
        return frozenset()
    return frozenset({Declaration("checked" if invocation.form == "checked" else "entry", invocation.target)})


def render_invocation(invocation):
    if invocation.symbol is not None:
        return "%s(%s)" % (invocation.symbol, entry_arguments(invocation.variant, invocation.expected))
    if invocation.form == "private":
        return "%s(%s)" % (symbol("entry_contract", invocation.target, invocation.output_mask),
                           entry_arguments(invocation.variant, invocation.expected))
    if invocation.form == "checked":
        return "CALL_FN_CHECKED(%08x, 0x%xu)" % (invocation.target, invocation.expected)
    return "CALL_FN(%08x)" % invocation.target


def symbol(kind, target, mask=None):
    suffix = "%08x" % target
    if mask is not None:
        suffix += "_%04x" % mask
    return "%s_%s" % (kind, suffix)


def body_symbol(variant, target, layout=None):
    kind = {EntryForm.ORDINARY: "fn", EntryForm.MULTIPLE: "body",
            EntryForm.CHECKED: "body_checked", EntryForm.PRIVATE: "body_contract"}[variant.entry]
    return symbol(kind, target, layout.output_mask if layout is not None else None)


def signature(variant, name, layout=None, target=None):
    if layout is not None:
        if target is None:
            raise ValueError("value body signature requires its target identity")
        return "static void %s(%s) {" % (name, value_parameters(layout, target, "expected_return_"))
    parameters = {EntryForm.ORDINARY: "X86 *c",
                  EntryForm.MULTIPLE: "X86 *c, uint32_t entry_",
                  EntryForm.CHECKED: "X86 *c, uint32_t expected_return_, int checked_return_",
                  EntryForm.PRIVATE: "X86 *c, uint32_t expected_return_"}[variant.entry]
    storage = "" if variant.entry == EntryForm.ORDINARY else "static "
    return "%svoid %s(%s) {" % (storage, name, parameters)


def entry_signature(variant, name):
    parameters = "X86 *c, uint32_t expected" if variant.checked else "X86 *c"
    return "void %s(%s)" % (name, parameters)


def entry_arguments(variant, expected):
    return "c, 0x%xu" % expected if variant.checked else "c"


def declaration(record):
    if record.kind == "private_prepare":
        return "int %s(X86 *c, int mode_proven);" % symbol("entry_contract_prepare", record.target)
    if record.kind == "private_values":
        if record.layout is None or record.layout.output_mask != record.mask:
            raise ValueError("value declaration requires its finalized layout")
        return value_signature(symbol("entry_values", record.target, record.mask), record.layout, record.target) + ";"
    if record.kind == "private_contract":
        variant = BodyVariant(EntryForm.PRIVATE, ReturnForm.PRIVATE)
        name = symbol("entry_contract", record.target, record.mask)
    elif record.kind == "checked":
        variant = BodyVariant(EntryForm.CHECKED, ReturnForm.CHECKED)
        name = symbol("entry_checked", record.target)
    elif record.kind == "entry":
        variant, name = BodyVariant(), symbol("entry", record.target)
    else:
        raise ValueError("unknown C declaration kind %s" % record.kind)
    return entry_signature(variant, name) + ";"


def prepared_declarations(prepared):
    from .lowering import ValueTransport
    result = set()
    for invocation in prepared.invocations.values():
        result |= invocation_declarations(invocation)
    if isinstance(prepared.transport, ValueTransport):
        result = {record for record in result if record.kind != "private_contract"}
        result.update(Declaration("private_values", invocation.target,
                                 invocation.output_mask, prepared.final.values[event].layout)
                      for event, invocation in prepared.invocations.items() if invocation.form == "private")
    return frozenset(result)


def cpu_field(name):
    gprs = X86_32.names("gpr")
    if name in gprs:
        return "c->r[%d]" % gprs.index(name)
    if name in ("EIP", "FS_OFFSET"):
        return "c->" + {"EIP": "eip", "FS_OFFSET": "fs_base"}[name]
    if name in (*X86_32.names("arithmetic_flag"), *X86_32.names("direction_flag")):
        return "c->eflags_" + name.lower()
    raise ValueError("unknown runtime CPU field " + name)
