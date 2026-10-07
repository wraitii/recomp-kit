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

A  no store snapshots: ordinary guest stores publish no GPR/flag state. Only
   calls, returns, division/string seams publish. (Production scalar x87
   already publishes no x87 state at loads or stores.)
B  flags dead at call/return boundaries: CF/PF/AF/ZF/SF/OF are not live-in,
   not live-out at return and neither published before nor reloaded after a
   call (DF stays exact). Flags consumed inside the function stay exact.
C  x87 MSVC call convention: popped-slot residue, tags, st_bits and st_exact
   are neither maintained nor published. The stack is assumed empty at
   calls/return except for values still live (ST0 of a float return), inferred
   from which slots remain pushed. Exact-integer (FILD/FISTP m64) shadows are
   simply dropped.
D  no sticky IE/ZE status and no per-operation NaN canonicalization; the
   indefinite NaN is produced only when a value is stored to guest memory.
   FCOM condition bits C0/C2/C3 stay exact.
E  constant control word (PC=00, RC=nearest, no selector) with every x87 slot
   a binary32 C float and plain float arithmetic. Functions containing FLDCW
   (or any x87 instruction the ceiling lowering does not model) take the
   ordinary SSA body whole; no partial-region application exists.
"""

RELAXATIONS = "ABCDE"
FLAG_FIELDS = frozenset("c->eflags_" + n for n in ("cf", "pf", "af", "zf", "sf", "of"))
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
        raise ValueError("unknown ceiling relaxation %s (choose from A,B,C,D,E or all)" % ",".join(unknown))
    return frozenset(items)


def label(relax):
    """Stable column label listing the enabled relaxations."""
    relax = frozenset(relax)
    return "SSA ceiling [%s]" % ",".join(sorted(relax))
