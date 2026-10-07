"""Adapt original lifted instruction CFGs into conservative contract facts.

This inventory is deliberately separate from the convention census and the C
emitter. Raw x87, user operations and intra-instruction flow require corrected
effect models; they contribute top rather than being guessed pure. Memory
footprints are unknown until an alias analysis resolves them. All calls remain
outside observer boundaries, including calls to functions present in a report.
"""
from .model import Boundary, Effects, Mask, Node, Observer


# Known p-code forms whose explicit register operands can be inventoried.
# Admission here does not admit an instruction for optimized code generation.
INTEGER_OPS = frozenset((
    "COPY", "LOAD", "STORE", "BRANCH", "CBRANCH", "BRANCHIND", "CALL", "CALLIND", "RETURN",
    "INT_EQUAL", "INT_NOTEQUAL", "INT_LESS", "INT_SLESS", "INT_LESSEQUAL", "INT_SLESSEQUAL",
    "INT_ZEXT", "INT_SEXT", "INT_ADD", "INT_SUB", "INT_CARRY", "INT_SCARRY", "INT_SBORROW",
    "INT_2COMP", "INT_NEGATE", "INT_XOR", "INT_AND", "INT_OR", "INT_LEFT", "INT_RIGHT",
    "INT_SRIGHT", "INT_MULT", "INT_DIV", "INT_SDIV", "INT_REM", "INT_SREM",
    "BOOL_NEGATE", "BOOL_XOR", "BOOL_AND", "BOOL_OR", "PIECE", "SUBPIECE", "POPCOUNT",
))


def lanes(varnode):
    """Architecture-neutral register byte identities from the lifted layout."""
    if varnode is None or varnode[0] != "register":
        return frozenset()
    return frozenset("register:%x" % (varnode[1] + i) for i in range(varnode[2]))


def inventory(fir):
    """Return nodes without narrowing calls, memory footprints or x87 effects.

    Register reads are a conservative syntactic inventory, not semantic entry
    inputs. AF is absent from raw SLEIGH: arithmetic gets an unresolved note and
    an unknown CPU write footprint, with no definite-definition claim. This
    protects future consumers from treating raw lifting as a complete proof.
    """
    addresses = {ins.addr for ins in fir.insns}
    nodes = []
    for ins in fir.insns:
        reads, writes = set(), set()
        memory_reads, memory_writes = Mask(), Mask()
        events, problems, boundaries = set(), set(), []
        opaque = ins.x87 or ins.internal_flow or bool(ins.userops)
        arithmetic = False
        for op in ins.ops:
            reads.update(atom for var in op.ins for atom in lanes(var))
            writes.update(lanes(op.out))
            opaque |= op.opc not in INTEGER_OPS
            if op.opc == "LOAD" or any(var[0] == "ram" for var in op.ins
                                        if op.opc not in ("BRANCH", "CBRANCH", "CALL")):
                memory_reads = Mask.unknown()
                events.add("fault")
            if op.opc == "STORE" or (op.out and op.out[0] == "ram"):
                memory_writes = Mask.unknown()
                events.add("fault")
            if op.opc in ("INT_DIV", "INT_SDIV", "INT_REM", "INT_SREM"):
                events.add("fault")
            arithmetic |= op.opc in (
                "INT_ADD", "INT_SUB", "INT_2COMP", "INT_LEFT", "INT_RIGHT", "INT_SRIGHT")
            arithmetic |= bool(op.out and op.out[0] == 'register'
                               and 0x200 <= op.out[1] <= 0x20b)
            if op.opc in ("CALL", "CALLIND"):
                target = (op.ins[0][1] if op.opc == "CALL" and op.ins
                          and op.ins[0][0] == "ram" else None)
                boundaries.append(Boundary(ins.addr, "call", target))
                events.add("call")
            elif op.opc == "RETURN":
                events.add("return")
            elif op.opc == "BRANCHIND" or (op.opc in ("BRANCH", "CBRANCH")
                    and op.ins and op.ins[0][0] == "ram" and op.ins[0][1] not in addresses):
                target = op.ins[0][1] if op.ins and op.ins[0][0] == "ram" else None
                boundaries.append(Boundary(ins.addr, "external-transfer", target))
        if arithmetic:
            problems.add("raw arithmetic flags require corrected semantics")
        effects = Effects(Mask(reads), Mask.unknown() if arithmetic else Mask(writes),
                          memory_reads, memory_writes, frozenset(events), frozenset(problems))
        if opaque:
            effects = effects.union(Effects.unknown("unmodeled lifted effect at %08x" % ins.addr))
        if "fault" in effects.events:
            boundaries.append(Boundary(ins.addr, "fault", observer=Observer(
                Effects(cpu_reads=Mask.unknown(), memory_reads=Mask.unknown(),
                        events=frozenset({"fault"}),
                        unresolved=frozenset({"exceptional reconstruction not established"})))))
        nodes.append(Node(ins.addr, effects,
                          frozenset() if opaque or arithmetic or boundaries else frozenset(writes),
                          tuple(boundaries)))
    return tuple(nodes)
