"""Intrinsic operation properties shared by translator consumers.

The table says what an operation may do to guest state. It deliberately does
not encode consumer policy: relaxable memory observation lives in
publication.py, lazy-flag observers in flag_region.py/the emitter, x87
flush/reset in x87_carry.py/the x87 planner, and liveness roots in simplify.py.
A missing description raises a named diagnostic so the body falls back to
decoded code.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Properties:
    name: str
    ordered: bool = False
    observes: bool = False
    exit_publish: bool = False
    faults: bool = False
    memory: str | None = None
    opaque: bool = False
    terminator: bool = False
    pure: bool = False
    synthetic: bool = False


_TABLE = {}
FLOAT_COMPARISONS = ("FLOAT_NAN", "FLOAT_EQUAL", "FLOAT_LESS")
FLOAT_ARITHMETIC = ("FLOAT_ADD", "FLOAT_SUB", "FLOAT_MULT", "FLOAT_DIV", "FLOAT_MIN", "FLOAT_MAX")
FLOAT_CONVERSIONS = ("CVTTSS2SI", "CVTSS2SI", "CVTSI2SS")


def _add(name, **properties):
    _TABLE[name] = Properties(name, **properties)


for _name in (
    "PACK", "BYTE", "COPY", "INT_ZEXT", "INT_SEXT", "INT_ADD", "INT_SUB", "INT_MULT",
    "INT_AND", "INT_OR", "INT_XOR", "INT_EQUAL", "INT_NOTEQUAL", "INT_LESS",
    "INT_LESSEQUAL", "INT_SLESS", "INT_SLESSEQUAL", "BOOL_AND", "BOOL_OR", "BOOL_XOR",
    "INT_LEFT", "INT_RIGHT", "INT_SRIGHT", "INT_CARRY", "INT_SCARRY", "INT_SBORROW",
    "BOOL_NEGATE", "INT_NEGATE", "INT_2COMP", "POPCOUNT", "SUBPIECE", "PIECE",
    "MEMORY", "CC_KIND",
) + FLOAT_COMPARISONS + FLOAT_ARITHMETIC + FLOAT_CONVERSIONS:
    _add(_name, pure=True, synthetic=_name in ("MEMORY", "CC_KIND"))
for _name in ("CONST", "TARGET", "CALL_RELOAD", "CALL_KEEP", "INPUT", "PHI"):
    _add(_name, pure=True, synthetic=True)

# X87_MEM direction is fixed by the instruction mnemonic, not the opcode.
_add("LOAD", ordered=True, observes=True, faults=True, memory="read")
_add("STORE", ordered=True, observes=True, faults=True, memory="write")
_add("X87_MEM", ordered=True, observes=True, faults=True, memory="mnemonic")
_add("X87_REG", ordered=True)
_add("DIV32", ordered=True, observes=True, faults=True, memory="readwrite", opaque=True)
_add("IDIV32", ordered=True, observes=True, faults=True, memory="readwrite", opaque=True)
_add("STRINGOP", ordered=True, observes=True, faults=True, memory="readwrite", opaque=True)
_add("CALL", ordered=True, observes=True, faults=True, memory="readwrite", opaque=True)
_add("CALLIND", ordered=True, observes=True, faults=True, memory="readwrite", opaque=True)
_add("SEH", ordered=True, observes=True, faults=True, memory="readwrite")
_add("RETURN", observes=True, exit_publish=True, terminator=True)
_add("TAIL", observes=True, exit_publish=True, terminator=True)
_add("BRANCHIND", observes=True, exit_publish=True, terminator=True)
_add("BRANCH", terminator=True)
_add("CBRANCH", terminator=True)
_add("TRAP", terminator=True, faults=True, opaque=True)
_add("BREAKPOINT", observes=True, exit_publish=True, terminator=True, faults=True)
_add("CALLOTHER", faults=True, opaque=True)


def describe(opc):
    """Intrinsic properties of `opc`; unknown operations raise ``SSAError``."""
    found = _TABLE.get(opc)
    if found is None:
        from .ssa import SSAError
        raise SSAError("no operation description for %s" % (opc,))
    return found


def validate(ssa):
    """Raise for any operation in `ssa` without a description."""
    for block in ssa.blocks.values():
        for value in block.ops:
            describe(value.opc)
        for value in block.phis:
            describe(value.opc)


EFFECTS = frozenset(n for n, p in _TABLE.items() if p.observes and not p.exit_publish)
EXITS = frozenset(n for n, p in _TABLE.items() if p.observes and p.exit_publish)
OBSERVES = frozenset(n for n, p in _TABLE.items() if p.observes)
ORDERED = frozenset(n for n, p in _TABLE.items() if p.ordered)
TERMINATORS = frozenset(n for n, p in _TABLE.items() if p.terminator)
PURE = frozenset(n for n, p in _TABLE.items() if p.pure)
OPAQUE = frozenset(n for n, p in _TABLE.items() if p.opaque)

# Policy classifiers, derived from the table plus explicit boundaries.
RETURNING_OPAQUE = frozenset(n for n, p in _TABLE.items()
                             if p.opaque and p.ordered and not p.terminator)
FLAG_EAGER_OBSERVERS = ((RETURNING_OPAQUE - frozenset(("CALL", "CALLIND")))
                        | (EXITS - frozenset(("RETURN", "TAIL")))
                        | frozenset(("CALLOTHER",)))
FLAG_EAGER_CODEGEN = FLAG_EAGER_OBSERVERS - frozenset(("CALLOTHER",))
X87_RESET_OPS = RETURNING_OPAQUE
X87_FLUSH_OPS = X87_RESET_OPS | frozenset(("RETURN", "TAIL", "BREAKPOINT"))
X87_FLUSH_STRICT_OPS = frozenset(("LOAD", "STORE"))
