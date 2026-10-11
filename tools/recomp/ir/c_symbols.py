"""C spellings for semantic entries, layouts and body signatures."""
from dataclasses import dataclass
from .body import EntryForm, BodyVariant, ReturnForm


@dataclass(frozen=True)
class Declaration:
    kind: str
    target: int
    mask: int = 0


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


def signature(variant, name):
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
