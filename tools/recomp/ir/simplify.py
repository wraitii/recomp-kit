"""Width-aware, effect-preserving simplification of integer SSA.

Aliases keep value IDs stable for snapshots and diagnostics. Pure rewrites never
move effects: even unused LOAD results retain their access and pre-access state.
The passes work on SSA rather than C spelling and can serve other consumers.
"""

EFFECTS = frozenset(("LOAD", "STORE", "DIV32", "IDIV32", "X87_MEM", "CALL",
                     "CALLIND", "STRINGOP", "SEH"))
ORDERED = EFFECTS | {"X87_REG"}
TERMINATORS = frozenset(("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP"))
#: Terminators that publish the block's exit state.
EXITS = ("RETURN", "BRANCHIND", "TAIL")
PURE = frozenset((
    "PACK", "BYTE", "COPY", "INT_ZEXT", "INT_SEXT", "INT_ADD", "INT_SUB", "INT_MULT",
    "INT_AND", "INT_OR", "INT_XOR", "INT_EQUAL", "INT_NOTEQUAL", "INT_LESS",
    "INT_LESSEQUAL", "INT_SLESS", "INT_SLESSEQUAL", "BOOL_AND", "BOOL_OR",
    "BOOL_XOR", "INT_LEFT", "INT_RIGHT", "INT_SRIGHT", "INT_CARRY", "INT_SCARRY",
    "INT_SBORROW", "BOOL_NEGATE", "INT_NEGATE", "INT_2COMP", "POPCOUNT",
    "SUBPIECE", "PIECE", "MEMORY",
))


def canonicalize(s):
    """Propagate copies/constants and cancel byte decomposition/reassembly.

Only equal-width values may alias one another. PACK of an entire BYTE sequence
recovers its source; partial packs stay explicit. Constant evaluation uses the
output width and p-code's zero result for shifts beyond the input width.
"""
    constants = {}
    for v in s.values:
        if v.opc == "CONST":
            constants.setdefault((v.size, v.data), v)

    def constant(size, data):
        key = size, data & ((1 << (8 * size)) - 1)
        if key not in constants:
            constants[key] = s.value("CONST", size, data=key[1])
        return constants[key]

    changed = True
    while changed:
        changed = False
        s.simplify_phis()
        for v in list(s.values):
            if s.resolve(v) is not v:
                continue
            v.args = tuple(s.resolve(a) for a in v.args)
            a, replacement = v.args, None
            if v.opc in ("COPY", "INT_ZEXT") and a[0].size == v.size:
                replacement = a[0]
            elif v.opc == "BYTE":
                if a[0].opc == "PACK":
                    replacement = a[0].args[v.data]
                elif a[0].opc == "CONST":
                    replacement = constant(1, a[0].data >> (8 * v.data))
            elif v.opc == "PACK":
                if all(x.opc == "CONST" for x in a):
                    replacement = constant(v.size, sum(x.data << (8 * n) for n, x in enumerate(a)))
                elif all(x.opc == "BYTE" and x.data == n for n, x in enumerate(a)):
                    source = s.resolve(a[0].args[0])
                    if source.size == v.size and all(s.resolve(x.args[0]) is source for x in a):
                        replacement = source
            elif v.size and a and all(x.opc == "CONST" for x in a):
                x = [arg.data for arg in a]
                result = None
                if v.opc in ("COPY", "INT_ZEXT"):
                    result = x[0]
                elif v.opc == "INT_ADD":
                    result = x[0] + x[1]
                elif v.opc == "INT_SUB":
                    result = x[0] - x[1]
                elif v.opc == "INT_MULT":
                    result = x[0] * x[1]
                elif v.opc == "INT_AND":
                    result = x[0] & x[1]
                elif v.opc == "INT_OR":
                    result = x[0] | x[1]
                elif v.opc == "INT_XOR":
                    result = x[0] ^ x[1]
                elif v.opc in ("INT_LEFT", "INT_RIGHT"):
                    result = 0 if x[1] >= a[0].size * 8 else (
                        x[0] << x[1] if v.opc == "INT_LEFT" else x[0] >> x[1])
                elif v.opc == "SUBPIECE":
                    result = x[0] >> (8 * x[1]) if x[1] < a[0].size else 0
                elif v.opc == "PIECE":
                    result = (x[0] << (a[1].size * 8)) | x[1]
                if result is not None:
                    replacement = constant(v.size, result)
            if replacement is not None and replacement is not v:
                assert replacement.size == v.size
                s.aliases[v.id] = replacement
                changed = True
    # Every non-aliased value's args were resolved by the last pass above;
    # an aliased value's args are never read because consumers resolve first.
    # The trailing all-values pass this replaced only repeated that work.


def live_values(s, publications=None, extra_roots=(), removable=()):
    """Find values needed by effects, control flow and observable CPU states.

    Outgoing state is observable on return. Each required effect snapshot is a
root, including registers/flags used only by a fault or helper observation.
An optional publication plan identifies fields that already reside in the CPU
and therefore need no SSA computation or assignment at that observation.
Memory tokens retain the dependency chain without authorizing load forwarding.

``removable`` names opcodes that are otherwise treated as roots but that a
caller may ignore when asking what a value is needed for.  The SSA emitter uses
it for ``CALL_RELOAD``: a callee-state read is emitted even when its value is
unused, so a settle decision needs a use-only view.
"""
    todo = list(extra_roots)
    for b in s.blocks.values():
        for v in b.ops:
            # Unknown operations must survive to the consumer's diagnostic;
            # absence of a result use is not proof of absent effects.
            if v.opc not in PURE and v.opc not in removable:
                todo.append(v)
                if v.opc in EFFECTS:
                    state = b.snapshots[v.id]
                    todo.extend(state[key] for key in (
                        state if publications is None else publications[v.id]))
                elif v.opc in EXITS:
                    todo.extend(b.exit[key] for key in (
                        b.exit if publications is None else publications[v.id]))
    live = set()
    while todo:
        v = s.resolve(todo.pop())
        if v.id not in live:
            live.add(v.id)
            todo.extend(v.args)
    return live


def simplify(s, publications=None, *, canonical=True, extra_roots=()):
    """Canonicalize to a fixed point, then eliminate unobserved pure values.

    ``canonical=False`` skips the canonicalization pass for a caller that has
    already run it and made no operand change since (the production emitter
    canonicalizes before the publication plan). The pass is idempotent, so
    the emitted body is byte-identical; this only avoids the repeated work.
    """
    if canonical:
        canonicalize(s)
    live = live_values(s, publications, extra_roots)
    for b in s.blocks.values():
        b.ops = [v for v in b.ops if v.id in live and s.resolve(v) is v]
        b.phis = [v for v in b.phis if v.id in live and s.resolve(v) is v]
    return live
