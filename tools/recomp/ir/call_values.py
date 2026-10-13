"""Ordered call values, independent of their eventual transport."""
from dataclasses import dataclass

from .body import FrozenMap
from .call_contracts import FIELDS
from .contract_variants import PrivateLayout, select_layout
from .call_state import CallState
from .simplify import whole
from .ssa import SSAError, Value


@dataclass(frozen=True)
class FieldValue:
    field: str
    size: int
    lanes: tuple
    whole: Value | None


def field_value(s, field, keys, state):
    lanes = tuple(s.resolve(state[key]) if key in state else None for key in keys)
    source = whole(s, lanes) if all(value is not None for value in lanes) else None
    return FieldValue(field, len(keys), lanes, s.resolve(source) if source is not None else None)


@dataclass(frozen=True)
class CallResult:
    field: str
    continuation: FieldValue


@dataclass(frozen=True)
class PreservedValue:
    field: str
    before: FieldValue
    continuation: FieldValue


@dataclass(frozen=True)
class CallValues:
    requirements: CallState
    layout: PrivateLayout
    inputs: tuple
    outputs: tuple
    preserved: tuple
    fallback_inputs: tuple
    fallback_outputs: tuple
    resident_opaque: tuple

    def __post_init__(self):
        if tuple(value.field for value in self.inputs) != self.layout.inputs:
            raise SSAError("call input bindings do not match the shared layout")
        if tuple(value.field for value in self.outputs) != self.layout.outputs:
            raise SSAError("call result bindings do not match the shared layout")
        required_inputs = self.requirements.inputs - (frozenset((self.layout.input_argument,))
                                                      if self.layout.input_argument else frozenset())
        if not required_inputs <= set(self.layout.inputs):
            raise SSAError("call bindings omit required inputs")
        continuation = self.requirements.required_outputs
        if continuation is not None and not continuation.fields <= set(self.layout.outputs):
            raise SSAError("call bindings omit continuation outputs")
        if tuple(value.field for value in self.preserved) != tuple(f for f in FIELDS if f in self.requirements.preserved):
            raise SSAError("call bindings omit preserved continuation values")
        if not self.requirements.opaque <= set(self.resident_opaque):
            raise SSAError("call bindings omit resident opaque state")


def bind(prepared, invocations, layouts):
    s, groups = prepared.ssa, dict(prepared.state_groups)
    result = {}
    for block in s.blocks.values():
        for event in block.ops:
            if event.opc != "CALL":
                continue
            state = prepared.transfers.calls[event.id].state
            invocation = invocations[event.id]
            layout = (select_layout(layouts, event.data, invocation.output_mask) if invocation.form == "private"
                      else PrivateLayout(tuple(f for f in FIELDS if f in state.inputs), FIELDS))
            before, after = block.snapshots[event.id], block.reloads[event.id]
            def value(field, snapshot):
                return field_value(s, field, groups[field], snapshot)
            result[event.id] = CallValues(
                state, layout, tuple(value(f, before) for f in layout.inputs),
                tuple(CallResult(f, value(f, after)) for f in layout.outputs),
                tuple(PreservedValue(f, value(f, before), value(f, after))
                      for f in FIELDS if f in state.preserved),
                tuple(value(f, before) for f in FIELDS),
                tuple(CallResult(f, value(f, after)) for f in FIELDS),
                tuple(dict.fromkeys((*layout.resident_opaque, *sorted(state.opaque)))))
    return FrozenMap.of(result)
