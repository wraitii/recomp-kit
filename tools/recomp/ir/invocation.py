"""Structural invocation forms; C spellings live in the backend."""
from dataclasses import dataclass

from .x87_cw import CW_FAST, Proven
from .body import BodyVariant, EntryForm, ReturnForm


@dataclass(frozen=True)
class EntryBinding:
    """A production entry rendered as a macro, not a literal symbol."""

    target: int
    checked: bool


@dataclass(frozen=True)
class Preparation:
    """Private-entry eligibility guard, checked before the specialized call."""

    target: int
    proof: Proven | None

    def __post_init__(self):
        if self.proof is not None and (not isinstance(self.proof, Proven)
                                       or self.proof.condition != CW_FAST):
            raise ValueError("private mode proof must establish CW_FAST")

    @property
    def mode_proven(self):
        return self.proof is not None


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
    ecx_argument: bool = False

    @property
    def variant(self):
        if self.form == "private":
            return BodyVariant(EntryForm.PRIVATE, ReturnForm.PRIVATE)
        if self.form == "checked":
            return BodyVariant(EntryForm.CHECKED, ReturnForm.CHECKED)
        return BodyVariant()

def binding_invocation(target, binding, form, expected):
    symbol = binding if isinstance(binding, str) else None
    return Invocation(target, form, symbol, expected, None, None, None)


def private_invocation(target, binding, expected, proof, mask, ecx_argument=False):
    preparation = Preparation(target, proof)
    fallback = binding_invocation(target, binding, "checked", expected)
    return Invocation(target, "private", None, expected, preparation, mask, fallback, ecx_argument)


def tail_invocation(target, binding):
    return binding_invocation(target, binding, "tail", None)
