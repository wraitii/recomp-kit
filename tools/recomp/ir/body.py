"""Semantic body variants and immutable preparation inputs."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contract_variants import PrivateLayout

from .x87_cw import CW_FAST, Dynamic, Guarded


@dataclass(frozen=True)
class FrozenMap(Mapping):
    items_tuple: tuple = ()
    _index: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_index", MappingProxyType(dict(self.items_tuple)))

    @classmethod
    def of(cls, values):
        if isinstance(values, cls):
            return values
        def freeze(value):
            if isinstance(value, dict):
                return cls.of(value)
            if isinstance(value, set):
                return frozenset(value)
            if isinstance(value, list):
                return tuple(value)
            return value
        return cls(tuple((key, freeze(value)) for key, value in (values or {}).items()))

    def __getitem__(self, key):
        return self._index[key]

    def __iter__(self):
        return iter(self._index)

    def __len__(self):
        return len(self._index)

    def __reduce__(self):
        return type(self), (self.items_tuple,)


class EntryForm(Enum):
    ORDINARY = "ordinary"
    MULTIPLE = "multiple"
    CHECKED = "checked"
    PRIVATE = "private"


class ReturnForm(Enum):
    ORDINARY = "ordinary"
    CHECKED = "checked"
    PRIVATE = "private"


@dataclass(frozen=True)
class BodyVariant:
    entry: EntryForm = EntryForm.ORDINARY
    returns: ReturnForm = ReturnForm.ORDINARY
    arithmetic: Dynamic | Guarded = field(default_factory=Dynamic)

    def __post_init__(self):
        supported = {(EntryForm.ORDINARY, ReturnForm.ORDINARY),
                     (EntryForm.MULTIPLE, ReturnForm.ORDINARY),
                     (EntryForm.CHECKED, ReturnForm.CHECKED),
                     (EntryForm.PRIVATE, ReturnForm.PRIVATE)}
        if (self.entry, self.returns) not in supported:
            raise ValueError("unsupported body entry/return combination")
        if not isinstance(self.arithmetic, (Dynamic, Guarded)):
            raise TypeError("body arithmetic requires an explicit dynamic or guarded choice")
        if isinstance(self.arithmetic, Guarded) and (
                self.entry != EntryForm.PRIVATE or self.arithmetic != Guarded(CW_FAST, "entry")):
            raise ValueError("body arithmetic guard requires a private CW_FAST entry")

    @property
    def checked(self):
        return self.returns in (ReturnForm.CHECKED, ReturnForm.PRIVATE)

    @property
    def known_mode(self):
        return isinstance(self.arithmetic, Guarded)


@dataclass(frozen=True)
class EmissionPolicy:
    x87_scalar_strict: bool = False
    local_state: bool = True
    msvc_convention: bool = True
    lazy_flags: bool = False
    indirect_call_symbol: str | None = None
    x87_cw_clone: bool = True
    private_value_transport: bool = False


@dataclass(frozen=True)
class BodyRequest:
    symbol: str
    variant: BodyVariant = field(default_factory=BodyVariant)
    policy: EmissionPolicy = field(default_factory=EmissionPolicy)
    call_symbols: FrozenMap = field(default_factory=FrozenMap)
    tail_symbols: FrozenMap = field(default_factory=FrozenMap)
    call_contracts: FrozenMap = field(default_factory=FrozenMap)
    checked_calls: frozenset = frozenset()
    contract_calls: frozenset = frozenset()
    layout: "PrivateLayout | None" = None
    mode_sites: FrozenMap = field(default_factory=FrozenMap)
    entry_verify: object | None = None
    report_calls: bool = False

    def __post_init__(self):
        for name in ("call_symbols", "tail_symbols", "call_contracts", "mode_sites"):
            object.__setattr__(self, name, FrozenMap.of(getattr(self, name)))
        for name in ("checked_calls", "contract_calls"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        if (self.variant.returns == ReturnForm.PRIVATE) != (self.layout is not None):
            raise ValueError("private body requires a shared layout")


@dataclass(frozen=True)
class PreparedOptions:
    request: BodyRequest
    call_symbols: FrozenMap
    tail_symbols: FrozenMap
    mode_sites: FrozenMap
    lane_field: FrozenMap

    @property
    def symbol(self):
        return self.request.symbol

    @property
    def checked_return(self):
        return self.request.variant.checked

    @property
    def return_fields(self):
        return None if self.request.layout is None else frozenset(self.request.layout.outputs)
