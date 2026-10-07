"""Immutable domains for machine observations and possible execution effects.

CPU atoms name byte lanes or environment fields. Memory atoms are symbolic
footprints owned by a future alias analysis, never host addresses. Unknown is
the universal set, distinct from a proven empty set. These are may-facts, not
proofs that a narrowed private ABI is safe.
"""
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple


@dataclass(frozen=True)
class Mask:
    """A finite set, or the universal set excluding the named atoms.

    Complements keep 'all except definitely overwritten lanes' representable
    without enumerating an architecture's register universe.
    """
    atoms: FrozenSet[str] = frozenset()
    all: bool = False

    def __post_init__(self):
        object.__setattr__(self, "atoms", frozenset(self.atoms))
        if type(self.all) is not bool:
            raise ValueError("mask all must be boolean")
        if any(not isinstance(atom, str) or not atom for atom in self.atoms):
            raise ValueError("state atoms must be nonempty strings")

    @classmethod
    def unknown(cls):
        return cls(all=True)

    def union(self, other):
        if self.all and other.all:
            return Mask(self.atoms & other.atoms, True)
        if self.all:
            return Mask(self.atoms - other.atoms, True)
        if other.all:
            return Mask(other.atoms - self.atoms, True)
        return Mask(self.atoms | other.atoms)

    def without(self, definite):
        """Remove a finite set of *definite* definitions, never may-writes."""
        definite = frozenset(definite)
        return Mask(self.atoms | definite if self.all else self.atoms - definite,
                    self.all)

    def as_json(self):
        return {"all": self.all, "except" if self.all else "atoms": sorted(self.atoms)}


@dataclass(frozen=True)
class Effects:
    """Unordered may-effect summary; event order remains in the original CFG."""
    cpu_reads: Mask = field(default_factory=Mask)
    cpu_writes: Mask = field(default_factory=Mask)
    memory_reads: Mask = field(default_factory=Mask)
    memory_writes: Mask = field(default_factory=Mask)
    events: FrozenSet[str] = frozenset()
    unresolved: FrozenSet[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "events", frozenset(self.events))
        object.__setattr__(self, "unresolved", frozenset(self.unresolved))

    @classmethod
    def unknown(cls, reason):
        return cls(*(Mask.unknown() for _ in range(4)),
                   events=frozenset({"call", "callback", "yield", "fault", "nonreturn"}),
                   unresolved=frozenset({reason}))

    def union(self, other):
        return Effects(*(getattr(self, name).union(getattr(other, name)) for name in (
            "cpu_reads", "cpu_writes", "memory_reads", "memory_writes")),
            self.events | other.events, self.unresolved | other.unresolved)

    def as_json(self):
        return {**{name: getattr(self, name).as_json() for name in (
            "cpu_reads", "cpu_writes", "memory_reads", "memory_writes")},
            "events": sorted(self.events), "unresolved": sorted(self.unresolved)}


@dataclass(frozen=True)
class Observer:
    """Outside reads and possible responses, with evidence attached.

    No observer is inferred from a calling-convention signature. An unreviewed
    observer can read/write any CPU or memory and callback, yield or fail.
    """
    effects: Effects = field(default_factory=lambda: Effects.unknown("unreviewed observer"))
    evidence: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))


def _guest_address(address):
    if type(address) is not int or not 0 <= address <= 0xffffffff:
        raise ValueError("contract addresses must be 32-bit guest addresses")


@dataclass(frozen=True)
class Boundary:
    site: int
    kind: str
    target: Optional[int] = None
    observer: Observer = field(default_factory=Observer)

    def __post_init__(self):
        _guest_address(self.site)
        if self.target is not None:
            _guest_address(self.target)
        if not self.kind:
            raise ValueError("boundary kind required")


@dataclass(frozen=True)
class Node:
    """One instruction's conservative uses, definite definitions and effects.

    defs concerns only normal continuation; a fault observer must separately
    demand its pre-effect state. Reconstructing exceptional state is not
    implemented here. CPU may-writes cannot stand in for defs.
    """
    site: int
    effects: Effects = field(default_factory=Effects)
    defs: FrozenSet[str] = frozenset()
    boundaries: Tuple[Boundary, ...] = ()

    def __post_init__(self):
        _guest_address(self.site)
        object.__setattr__(self, "defs", frozenset(self.defs))
        object.__setattr__(self, "boundaries", tuple(self.boundaries))
        if any(b.site != self.site for b in self.boundaries):
            raise ValueError("boundary must belong to its instruction node")


@dataclass(frozen=True)
class FunctionFacts:
    address: int
    local: Effects
    callees: FrozenSet[int] = frozenset()

    def __post_init__(self):
        _guest_address(self.address)
        object.__setattr__(self, "callees", frozenset(self.callees))
        for address in self.callees:
            _guest_address(address)
