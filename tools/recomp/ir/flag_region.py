"""Per-site lazy arithmetic-flag region analysis.

A *settle site* is the function entry or the instruction after a call.  The
region walks in-body control flow from the site until it reaches a boundary:

* a direct/indirect CALL or a RET, where the next site (the callee's entry or
  the caller's own site) takes over;
* an instruction whose arithmetic-flag effect is unknown, or a transfer that
  leaves the body, which is an observer and forces a settle.

Within the region, per path, it tracks whether an arithmetic flag is read
before being written (a may fact), which flags may be written, and which flags
are written on every path to a boundary.  The caller maps that to one of three
actions:

* no reads and no writes: ``REMOVE``.  The pending descriptor passes through
  untouched to the next site.
* no reads and every one of the six flags written on every path: ``DROP``.
  The descriptor can be cleared without materialising its fields.
* otherwise: ``SETTLE``.

The analysis is deliberately conservative at calls and unknown instructions:
a write followed by a call that does not overwrite all six settles rather than
risk losing a preserved flag, and an unclassified mnemonic is an observer.
"""

ALL_FLAGS = frozenset(("cf", "pf", "af", "zf", "sf", "of"))

#: Possible decisions, in increasing cost.
REMOVE = "remove"
DROP = "drop"
SETTLE = "settle"


def decision(reads, writes, must):
    """Turn a region summary into a settle-site action."""
    if reads:
        return SETTLE
    if not writes:
        return REMOVE
    if must == ALL_FLAGS:
        return DROP
    return SETTLE


def analyze(start, successors, access, is_end, leaves, count):
    """Classify the region that starts at instruction index ``start``."""
    found = summarize(start, successors, access, is_end, leaves, count)
    return SETTLE if found is None else decision(*found)


def summarize(start, successors, access, is_end, leaves, count):
    """Summarize the region that starts at instruction index ``start``.

    ``successors(i)``  in-body successor indices to continue to.
    ``access(i)``      ``(reads, writes)`` frozensets, or ``None`` when the
                       instruction's arithmetic-flag effect is unknown (an
                       observer that forces a settle).
    ``is_end(i)``      a normal region boundary (CALL/CALLIND/RET): stop here,
                       record the path, do not force a settle.
    ``leaves(i)``      control can leave the body after ``i``: an observer.

    Returns ``(reads, writes, must)`` over the paths to a boundary, or None
    when an observer or a region with no reachable boundary (an infinite loop)
    forces a settle.
    """
    if not 0 <= start < count:
        return None
    # index -> (read-before-write, may-write, must-write), all frozensets.
    state = {start: (frozenset(), frozenset(), frozenset())}
    work = [start]
    reads = set()
    writes = set()
    must_end = None
    ends = 0
    observer = False
    while work:
        i = work.pop()
        rbw, written, must = state[i]
        effect = access(i)
        if effect is None:
            observer = True
            continue
        r, w = effect
        rbw2 = rbw | (frozenset(r) - must)
        written2 = written | frozenset(w)
        must2 = must | frozenset(w)
        if is_end(i):
            reads |= rbw2
            writes |= written2
            must_end = must2 if must_end is None else (must_end & must2)
            ends += 1
            continue
        if leaves(i):
            observer = True
            continue
        nexts = successors(i)
        if not nexts:
            # A path that ends without a boundary (a tail jump, a trap, an
            # unmodelled exit) would otherwise drop its writes from the summary.
            observer = True
            continue
        for j in nexts:
            if not 0 <= j < count:
                observer = True
                continue
            current = state.get(j)
            if current is None:
                state[j] = (rbw2, written2, must2)
                work.append(j)
            else:
                merged = (current[0] | rbw2, current[1] | written2,
                          current[2] & must2)
                if merged != current:
                    state[j] = merged
                    work.append(j)
    if observer or not ends:
        return None
    return frozenset(reads), frozenset(writes), must_end
