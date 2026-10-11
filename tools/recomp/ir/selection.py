"""Production eligibility policy shared by driver selection phases."""
from dataclasses import dataclass

from . import contract_variants, x87_mode


@dataclass(frozen=True)
class Eligibility:
    settings: object
    scanned: dict
    opaque: set

    @property
    def enabled(self):
        return (self.settings.call_contracts and self.settings.emit.local_state
                and not self.settings.module)

    def checked(self, addr):
        row = self.scanned.get(addr)
        return bool(row and row.register_facts and row.exclusion is None and addr not in self.opaque)

    def candidates(self):
        if not self.enabled:
            return set()
        configured = {a for a in self.settings.checked_returns if not self.scanned[a].calls}
        result = configured if self.settings.state_contract_callers else set()
        if self.settings.private_leaf_contracts:
            result |= contract_variants.eligible_leaves(self.scanned, self.opaque)
        return result

    def known_modes(self, candidates, prepared):
        if not self.settings.emit.x87_cw_clone or self.settings.emit.x87_scalar_strict:
            return set()
        return {addr for addr in candidates if self.scanned[addr].x87_count and addr not in self.opaque
                and x87_mode.mode_preserving_leaf(prepared.get(addr))}

    def callers(self, private_targets):
        if not self.enabled:
            return set()
        result = set(self.settings.state_contract_callers)
        if self.settings.private_leaf_contracts:
            result |= {addr for addr, row in self.scanned.items()
                       if addr not in self.opaque and row.register_facts and row.cw_facts
                       and any(t in private_targets for t in row.calls)}
        return result
