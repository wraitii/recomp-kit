"""UNPROVEN SSA "ceiling" relaxations: a corpus-only measurement experiment.

WARNING: nothing here is part of the agreed performance-mode contract. Each
relaxation deliberately discards guest-visible state that no analysis or
evidence has shown to be dead. They exist only to measure how much speed such
relaxations could buy *before* any of them is proven. Output of these modes is
expected to differ from eager C in full CPU state and is judged only against
declared observations by the corpus runner. They are reachable solely through
the private ``_ceiling`` argument of ``emit_c.emit`` and the corpus-only
``--corpus-ir-ssa-ceiling`` option. ``production.py`` and ``game.toml`` have no
way to select them (a regression test enforces this).

Relaxations (each independently switchable):

A  no access snapshots: loads and stores also skip EIP/ESP/EBP. (Production
   scalar x87 and the locals state policy already defer every other field
   there.) Only calls, returns, division/string seams publish.
C  x87 values only: tags, st_bits and st_exact are neither maintained nor
   published, at edges as well as calls and returns, and popped values are
   never written back. Exact-integer (FILD/FISTP m64) shadows are dropped.
   Production `ir_ssa_msvc_convention` already skips popped residue under an
   exact tag word; C measures the remaining live-register bookkeeping.

D  no sticky IE/ZE status and no per-operation NaN canonicalization; the
   indefinite NaN is produced only when a value is stored to guest memory.
   FCOM condition bits C0/C2/C3 stay exact.
E  constant control word (PC=00, RC=nearest, no selector) with every x87 slot
   a binary32 C float and plain float arithmetic. Functions containing FLDCW
   (or any x87 instruction the ceiling lowering does not model) take the
   ordinary SSA body whole; no partial-region application exists.

The former B (flags dead across calls and returns) landed as production
`ir_ssa_msvc_convention`; the letter is retired.
"""

RELAXATIONS = "ACDE"
X87_RELAXATIONS = frozenset("CDE")


def parse_relaxations(text):
    """Return a validated frozenset from ``"A,C"``, ``"ACE"``, ``"all"`` or ``""``."""
    if text is None:
        return frozenset()
    if isinstance(text, (set, frozenset, list, tuple)):
        items = list(text)
    else:
        text = str(text).strip()
        if text.lower() == "all":
            return frozenset(RELAXATIONS)
        items = [part for chunk in text.split(",") for part in (chunk.strip(),) if part]
        if len(items) == 1 and len(items[0]) > 1 and "," not in text:
            items = list(items[0])
    items = [str(item).upper() for item in items]
    unknown = sorted(set(items) - set(RELAXATIONS))
    if unknown:
        raise ValueError("unknown ceiling relaxation %s (choose from A,C,D,E or all)" % ",".join(unknown))
    return frozenset(items)


def label(relax):
    """Stable column label listing the enabled relaxations."""
    relax = frozenset(relax)
    return "SSA ceiling [%s]" % ",".join(sorted(relax))
