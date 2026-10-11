"""Structural direct-call and tail invocation forms.

``CallState`` names the semantic state crossing a call; this module names how
each call site is emitted. A binding is either a production entry (rendered
through the ``CALL_FN`` macros) or a literal custom symbol for harnesses that
supply their own callees. The finalized per-site plan is an immutable
``Invocation``, including an explicit fallback record for private entry
specialization, so rendering and declaration discovery never re-derive a form
from option sets or generated text.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EntryBinding:
    """A production entry rendered as a macro, not a literal symbol."""

    target: int
    checked: bool


@dataclass(frozen=True)
class Preparation:
    """Private-entry eligibility guard, checked before the specialized call."""

    symbol: str
    mode_proven: bool


@dataclass(frozen=True)
class Declaration:
    """One C prototype a body's invocations require its chunk to declare."""

    kind: str
    target: int
    mask: int = 0


@dataclass(frozen=True)
class Invocation:
    """How one direct call or tail is invoked.

    ``symbol`` is set only for a literal custom binding; ``None`` means the
    production macro for ``target``. ``form`` is ordinary, checked, private or
    tail. A private invocation carries its checked fallback as a full record,
    its preparation guard and its bound shared output mask; per-site semantic
    requirements stay in ``CallState``.
    """

    target: int
    form: str
    symbol: str | None
    expected: int | None
    preparation: Preparation | None
    output_mask: int | None
    fallback: "Invocation | None"

    def declarations(self):
        if self.form == "private":
            result = {Declaration("private_prepare", self.target),
                      Declaration("private_contract", self.target, self.output_mask)}
            if self.fallback is not None:
                result |= self.fallback.declarations()
            return result
        if self.symbol is not None:
            return frozenset()
        if self.form == "checked":
            return frozenset({Declaration("checked", self.target)})
        return frozenset({Declaration("entry", self.target)})

    def render(self):
        if self.symbol is not None:
            if self.form == "checked":
                return "%s(c, 0x%xu)" % (self.symbol, self.expected)
            return "%s(c)" % self.symbol
        if self.form == "private":
            return "entry_contract_%08x_%04x(c, 0x%xu)" % (self.target, self.output_mask,
                                                            self.expected)
        if self.form == "checked":
            return "CALL_FN_CHECKED(%08x, 0x%xu)" % (self.target, self.expected)
        return "CALL_FN(%08x)" % self.target


def binding_invocation(target, binding, form, expected):
    symbol = binding if isinstance(binding, str) else None
    return Invocation(target, form, symbol, expected, None, None, None)


def private_invocation(target, binding, expected, mode_proven, mask):
    preparation = Preparation("entry_contract_prepare_%08x" % target, mode_proven)
    fallback = binding_invocation(target, binding, "checked", expected)
    return Invocation(target, "private", None, expected, preparation, mask, fallback)


def tail_invocation(target, binding):
    return binding_invocation(target, binding, "tail", None)
