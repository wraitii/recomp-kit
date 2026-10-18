"""Named driver phases and worker payloads."""
from dataclasses import dataclass, replace
from pathlib import Path

from ir.body import FrozenMap


@dataclass(frozen=True)
class ScanTask:
    addresses: tuple


@dataclass(frozen=True)
class EscapeTask:
    addresses: tuple
    helpers: frozenset


@dataclass(frozen=True)
class ProgramAnalysis:
    helpers: frozenset
    internal: FrozenMap
    contracts: FrozenMap
    mode_sites: FrozenMap
    entry_verification: FrozenMap
    indirect_contracts: FrozenMap = FrozenMap()

    def for_addresses(self, addresses, calls=None):
        targets = {target for addr in addresses for target in calls[addr]} if calls is not None else set()
        return replace(self,
            internal=FrozenMap.of({a: self.internal[a] for a in addresses if a in self.internal}),
            contracts=FrozenMap.of({t: self.contracts[t] for t in targets if t in self.contracts}),
            mode_sites=FrozenMap.of({a: self.mode_sites[a] for a in addresses if a in self.mode_sites}),
            entry_verification=FrozenMap.of({a: self.entry_verification[a] for a in addresses if a in self.entry_verification}))


@dataclass(frozen=True)
class Selection:
    private_targets: FrozenMap
    checked_returns: frozenset
    callers: frozenset
    known_modes: frozenset
    rejections: FrozenMap
    input_arguments: FrozenMap = FrozenMap()


@dataclass(frozen=True)
class PreparationTask:
    addresses: tuple
    analysis: ProgramAnalysis
    selection: Selection
    store: Path


@dataclass(frozen=True)
class FunctionPlan:
    addr: int
    path: Path | None
    reason: str | None
    requests: frozenset
    input_sites: tuple = ()


@dataclass(frozen=True)
class EmissionTask:
    plans: tuple
    analysis: ProgramAnalysis
    layouts: FrozenMap


@dataclass(frozen=True)
class EmissionResult:
    addr: int
    lines: list
    reason: str | None
    unmodelled: tuple
    requests: frozenset
    decisions: tuple
    dependencies: frozenset
    region: tuple | None = None


@dataclass(frozen=True)
class CandidateTask:
    addr: int
    helpers: frozenset
    known_mode: bool
    input_argument_heuristic: bool = False


@dataclass(frozen=True)
class CandidateResult:
    addr: int
    contract: object | None
    reason: str | None
    register_uses: FrozenMap = FrozenMap()


@dataclass(frozen=True)
class PrivatePreparationTask:
    addr: int
    layout: object
    known_mode: bool
    store: Path


@dataclass(frozen=True)
class PrivateEmissionResult:
    addr: int
    lines: tuple


@dataclass(frozen=True)
class FinalFunctionPlan:
    addr: int
    body: object | None
    reason: str | None


@dataclass(frozen=True)
class FinalEmissionTask:
    plans: tuple
    analysis: ProgramAnalysis
    layouts: FrozenMap
