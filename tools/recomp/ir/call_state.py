"""Transport-independent state crossing a direct call.

Field sets describe the integer projection: the eight GPRs and six arithmetic
flags. State outside it is named in OPAQUE_STATE and stays resident in the
transport; the interface never claims to transfer or observe it away. SSA values
do not cross this interface: observation facts are symbolic field sets.
"""
from dataclasses import dataclass

OPAQUE_STATE = frozenset(("memory", "x87", "sse", "mxcsr", "eip", "fs", "df", "unknown"))


@dataclass(frozen=True)
class ContinuationState:
    """Continuation observation of one call site.

    ``fields`` is the authoritative per-site aggregate. ``returned`` are the
    fields the continuation reloads from the transport. ``absent_ssa`` are
    integer fields with no caller SSA input. ``caller_return_fields`` and
    ``caller_call_read_fields`` are caller-wide roots, not per-site causes:
    they name state the caller's own returns or direct calls observe (including
    this call's own callee reads) and must not be read as fields this
    continuation specifically requires.
    """

    fields: frozenset
    returned: frozenset
    absent_ssa: frozenset
    caller_return_fields: frozenset
    caller_call_read_fields: frozenset


@dataclass(frozen=True)
class CallState:
    """State crossing one direct call.

    ``required_outputs`` is None for an ordinary call: the conservative full
    integer boundary, where every field may be observed. ``resident`` names
    tracked integer fields proven already published in the transport before
    this call (known-before publication); it is not the set of inputs, since
    inputs may be resident, absent from caller SSA, or still to publish.
    ``opaque`` names state outside the integer projection that remains
    conservatively resident.
    """

    inputs: frozenset
    required_outputs: ContinuationState | None
    preserved: frozenset
    resident: frozenset
    opaque: frozenset
