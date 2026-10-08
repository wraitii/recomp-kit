"""Fixed-point join shapes for carried scalar x87 state.

The emitter's scalar tracker (`x87_scalar.py`) can retain unpublished x87 state
across internal CFG edges. Because a loop header's shape depends on its own
backedge, the per-block entry shape is a fixed point: each block's exit shape is
derived by replaying its x87 effects on a throwaway tracker seeded from the
entry shape, and a block's entry shape is the conservative merge of its
predecessors' exit shapes. Emission asserts that the live tracker still fits
the planned shape, so the analysis and the emitter cannot drift silently.
"""
from collections import deque

from .ssa import SSAError
from .x87_scalar import INACTIVE, UNSAFE, merge_shapes


#: Effect nodes that discard scalar x87 state inside a block, mirroring the
#: reset points in `emit_c.emit`: calls, string moves and division seams.
RESET_OPS = frozenset(("CALL", "CALLIND", "STRINGOP", "DIV32", "IDIV32"))


def _transfer(block, entry, scalar_factory, binary32):
    """Replay one block's x87 effects and return its exit shape."""
    scalar = scalar_factory()
    scalar.seed(entry if entry is not UNSAFE else INACTIVE)
    for value in block.ops:
        if value.opc in ("X87_REG", "X87_MEM"):
            scalar.binary32 = value.id in binary32
            scalar.statements(value.data, "carry_address", "carry_result")
        elif value.opc in RESET_OPS:
            scalar.reset()
    return scalar.snapshot()


def analyze(s, fir, scalar_factory, binary32):
    """Return per-block carried entry shapes (``CarryShape`` or ``UNSAFE``).

    A ``None`` value means the block was unreachable from the analysis roots;
    production CFGs always reach every block, so callers may treat it as a
    reset. Shapes merge monotonically, so the deduplicated worklist terminates
    in the finite eight-slot domain; the iteration bound turns any residual
    non-monotonicity into an explicit error instead of a hang.
    """
    predecessors = {i: set() for i in s.blocks}
    predecessors[s.entry].add(-1)
    for i in s.blocks:
        for j in set(fir.succ[i]):
            predecessors[j].add(i)
    # Reverse post-order seed keeps loop headers after their dominators, so
    # most shapes settle in one pass.
    order, seen = [], set()
    stack = [(s.entry, False)]
    while stack:
        i, done = stack.pop()
        if done:
            order.append(i)
            continue
        if i in seen:
            continue
        seen.add(i)
        stack.append((i, True))
        for j in reversed(sorted(set(fir.succ[i]))):
            if j not in seen:
                stack.append((j, False))
    for i in s.blocks:
        if i not in seen:
            order.append(i)
    seed_order = list(reversed(order))

    entry = {i: None for i in s.blocks}
    entry[s.entry] = INACTIVE
    exit_shape = {}
    work = deque(seed_order)
    queued = set(seed_order)
    limit = 10 * len(s.blocks) * (len(s.blocks) + 10) + 1000
    iterations = 0
    while work:
        iterations += 1
        if iterations > limit:
            raise SSAError("x87 carry analysis did not converge")
        i = work.popleft()
        queued.discard(i)
        # A window overflow is absorbing: predecessor shapes only grow, so a
        # block that fell back to reset can never become carryable again.
        if entry[i] is UNSAFE:
            continue
        shapes = []
        for p in sorted(predecessors[i]):
            if p == -1:
                shapes.append(INACTIVE)
            elif p in exit_shape:
                shapes.append(exit_shape[p])
        if not shapes:
            # No predecessor has produced an exit shape yet; a later
            # predecessor update re-enqueues this block.
            continue
        merged = merge_shapes(shapes)
        if merged is None:
            continue
        if merged == entry[i] and i in exit_shape:
            continue
        entry[i] = merged
        result = _transfer(s.blocks[i], merged, scalar_factory, binary32)
        if exit_shape.get(i) != result:
            exit_shape[i] = result
            for j in sorted(set(fir.succ[i])):
                if j not in queued:
                    queued.add(j)
                    work.append(j)
    return entry, exit_shape


def _agreement(slot_shape):
    return dict(slot_shape.const)


def assert_covers(join, live, where):
    """Fail closed when the live tracker no longer fits the planned shape."""
    if live is INACTIVE or not live.active:
        if join.active:
            raise SSAError("%s: carried x87 edge expected active state" % where)
        return
    if not join.active:
        raise SSAError("%s: live x87 state at an inactive join" % where)
    plan = dict(join.slots)
    for position, slot in live.slots:
        planned = plan.get(position)
        if planned is None:
            raise SSAError("%s: live x87 position %d absent from join shape"
                           % (where, position))
        if not slot.parts <= planned.parts:
            raise SSAError("%s: live x87 parts beyond join shape at %d"
                           % (where, position))
        if planned.narrow and not slot.narrow:
            raise SSAError("%s: binary32 join at %d from unproven operands"
                           % (where, position))
        agreements = _agreement(planned)
        live_const = _agreement(slot)
        for part in slot.parts:
            if part in agreements and live_const.get(part) != agreements[part]:
                raise SSAError("%s: x87 %s at %d is not the joined constant"
                               % (where, part, position))
    if live.low < join.low or live.high > join.high or live.base < join.base:
        raise SSAError("%s: live x87 window [%d,%d]/%d exceeds join [%d,%d]/%d"
                       % (where, live.low, live.high, live.base,
                          join.low, join.high, join.base))
    if (live.top_unknown or live.base != join.base) and not join.top_unknown:
        raise SSAError("%s: live x87 published TOP differs from join" % where)
    if live.status_dirty and not join.status_dirty:
        raise SSAError("%s: live x87 status publication not planned" % where)
