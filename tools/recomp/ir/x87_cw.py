"""Control-word conditions, masks and per-activation arithmetic choices.

A condition constrains only the x87 control word; known bits, symbolic entry
bits, return preservation, configured premises, entry verification and runtime
guards stay distinct.
"""
from collections import namedtuple
from dataclasses import dataclass

import capstone

from . import x87

CW_MASK = 0xffff
CW_PC_RC = 0x0f00
CW_PC = 0x0300
CW_RC = 0x0c00
CW_EXCEPTION_MASK = 0x003f
CW_RESET = 0x037f
CW_D3D8_SETUP_MASK = CW_PC_RC | CW_EXCEPTION_MASK
CW_D3D8_SETUP_VALUE = CW_EXCEPTION_MASK
CW_D3D8_PRESERVED = CW_MASK & ~CW_D3D8_SETUP_MASK


@dataclass(frozen=True)
class ModeCondition:
    mask: int
    value: int

    def __post_init__(self):
        if type(self.mask) is not int or type(self.value) is not int:
            raise TypeError("control-word condition requires integer mask and value")
        if not 0 <= self.mask <= CW_MASK or not 0 <= self.value <= CW_MASK or self.value & ~self.mask:
            raise ValueError("control-word condition out of range")

    def holds(self, cw):
        return (cw & self.mask) == self.value


CW_FAST = ModeCondition(CW_PC_RC, 0)
CW_D3D8_SETUP = ModeCondition(CW_D3D8_SETUP_MASK, CW_D3D8_SETUP_VALUE)


@dataclass(frozen=True)
class Dynamic:
    pass


@dataclass(frozen=True)
class Guarded:
    condition: ModeCondition
    boundary: str = "activation"

    def __post_init__(self):
        if self.boundary not in ("entry", "activation"):
            raise ValueError("unknown control-word guard boundary")


def implies(fact_condition, condition):
    return ((fact_condition.mask & condition.mask) == condition.mask
            and (fact_condition.value & condition.mask) == condition.value)


@dataclass(frozen=True)
class InstructionProof:
    facts: tuple
    scope: object
    provenance: object

    def __post_init__(self):
        from .x87_facts import InstructionFact
        if not isinstance(self.facts, tuple) or not self.facts or not all(
                isinstance(fact, InstructionFact) for fact in self.facts):
            raise TypeError("instruction proof requires InstructionFact records")
        if self.scope is None or self.provenance is None:
            raise ValueError("instruction proof requires scope and provenance")
        if any(fact.scope != self.scope for fact in self.facts):
            raise ValueError("instruction proof facts disagree on scope")
        if any(fact.provenance is not self.provenance for fact in self.facts):
            raise ValueError("instruction proof facts disagree on provenance")


@dataclass(frozen=True)
class Proven:
    condition: ModeCondition
    proof: InstructionProof

    def __post_init__(self):
        if not isinstance(self.condition, ModeCondition) or not isinstance(self.proof, InstructionProof):
            raise TypeError("proven arithmetic requires a condition and an instruction proof")
        for fact in self.proof.facts:
            if not isinstance(fact.mode, ModeCondition) or not implies(fact.mode, self.condition):
                raise ValueError("instruction facts do not imply the proven condition")


def proven_fact(fact, condition):
    """A `Proven` record for one typed `InstructionFact`, else ``None``."""
    if fact is None or condition_of(fact) != condition:
        return None
    try:
        proof = InstructionProof((fact,), getattr(fact, "scope", None),
                                 getattr(fact, "provenance", None))
        return Proven(condition, proof)
    except (TypeError, ValueError):
        return None


class Fact(namedtuple("Fact", ("known_mask", "known_value", "entry_mask"))):
    __slots__ = ()

    @property
    def known_condition(self):
        return ModeCondition(self.known_mask, self.known_value)


ENTRY = Fact(0, 0, CW_MASK)
UNKNOWN = Fact(0, 0, 0)
ReturnSummary = namedtuple("ReturnSummary", ("preserved_mask", "known_mask", "known_value"))
UNKNOWN_SUMMARY = ReturnSummary(0, 0, 0)

CW_NEUTRAL = x87.SUPPORTED | frozenset((
    "FCOMI", "FCOMIP", "FUCOMI", "FUCOMIP", "FEMMS", "WAIT",
))
CW_NEUTRAL_USER_OPS = frozenset((
    "CPUID", "RDTSC", "PACKSSDW", "PACKSSWB", "PSRAW", "PSWAPD",
    "RCPPS", "RCPSS", "RSQRTSS", "PF2ID", "PFACC", "PFADD", "PFCMPEQ",
    "PFCMPGE", "PFCMPGT", "PFMAX", "PFMIN", "PFMUL", "PFNACC", "PFPNACC",
    "PFRCP", "PFRCPIT1", "PFRCPIT2", "PFRSQIT1", "PFRSQRT", "PFSUB",
    "PFSUBR", "PI2FD",
))
_DECODER = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)


def join(facts):
    """Keep only equal known bits and entry bits common to every path."""
    facts = tuple(facts)
    if not facts:
        return UNKNOWN
    known = facts[0].known_mask
    value = facts[0].known_value
    entry = facts[0].entry_mask
    for fact in facts[1:]:
        equal = ~(value ^ fact.known_value) & CW_MASK
        known &= fact.known_mask & equal
        entry &= fact.entry_mask
    return Fact(known, value & known, entry & ~known & CW_MASK)


def overwrite(fact, mask, value):
    """Set selected bits to a constant while retaining other facts."""
    mask &= CW_MASK
    known = (fact.known_mask & ~mask) | mask
    known_value = (fact.known_value & ~mask) | (value & mask)
    entry = fact.entry_mask & ~mask
    return Fact(known & CW_MASK, known_value & known & CW_MASK, entry & CW_MASK)


def invalidate():
    return UNKNOWN


def fact_dict(fact):
    return {"known_mask": "%04x" % fact.known_mask,
            "known_value": "%04x" % fact.known_value,
            "entry_mask": "%04x" % fact.entry_mask}


def summary_dict(summary):
    return {"preserved_mask": "%04x" % summary.preserved_mask,
            "known_mask": "%04x" % summary.known_mask,
            "known_value": "%04x" % summary.known_value}


def instruction_mnemonic(ins):
    """Decode and validate the byte-backed instruction name used by effects."""
    if not ins.raw:
        return None
    record = getattr(ins, "decode", None)
    if (record is not None and record.address == ins.addr
            and record.source_bytes == ins.raw and record.length == ins.length):
        name = record.raw_mnemonic.upper()
        return "WAIT" if name == "WAIT" else name
    decoded = list(_DECODER.disasm(ins.raw, ins.addr))
    if sum(item.size for item in decoded) != ins.length:
        return None
    meaningful = [item.mnemonic.upper() for item in decoded if item.mnemonic.lower() != "wait"]
    if not meaningful and decoded:
        return "WAIT"
    if len(meaningful) != 1:
        return None
    return meaningful[0]


def instruction_effect(mnem):
    if mnem in ("FINIT", "FNINIT", "FSAVE", "FNSAVE"):
        return ("set", CW_MASK, CW_RESET, "reset control word")
    if mnem == "FNSTENV" or mnem == "FSTENV":
        return ("set", CW_EXCEPTION_MASK, CW_EXCEPTION_MASK, "environment save masks exceptions")
    if mnem in ("FLDCW", "FLDENV", "FRSTOR"):
        return ("unknown", 0, 0, "control word loaded from guest memory")
    if mnem in ("FXRSTOR", "XRSTOR", "XRSTORS"):
        return ("unknown", 0, 0, "extended state restored from guest memory")
    if mnem.startswith("F") and mnem not in CW_NEUTRAL:
        return ("unknown", 0, 0, "unclassified x87 instruction %s" % mnem)
    return ("keep", 0, 0, None)


_CW_WRITERS = frozenset(("FINIT", "FNINIT", "FSAVE", "FNSAVE", "FNSTENV", "FSTENV",
                         "FLDCW", "FLDENV", "FRSTOR", "FXRSTOR", "XRSTOR", "XRSTORS"))


def writer_effect(mnem):
    """Return the audited CW effect for a concrete writer mnemonic, else None.

    Callers must supply a mnemonic decoded from the original instruction bytes.
    Unknown x87 instructions are reported separately as possible boundaries,
    never promoted to definite writer sites by this helper.
    """
    mnem = str(mnem).upper()
    return instruction_effect(mnem) if mnem in _CW_WRITERS else None


def condition_of(value):
    mode = getattr(value, "mode", value)
    return ModeCondition(*mode) if isinstance(mode, tuple) else mode
