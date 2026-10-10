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

#: Results of an operation whose two operands are the same value.
SELF = {"INT_EQUAL": 1, "INT_LESSEQUAL": 1, "INT_SLESSEQUAL": 1, "INT_NOTEQUAL": 0,
        "INT_LESS": 0, "INT_SLESS": 0, "INT_XOR": 0, "INT_SUB": 0, "INT_SBORROW": 0}

REWRITABLE = frozenset(("PHI", "COPY", "INT_ZEXT", "BYTE", "PACK", "INT_ADD",
                       "INT_SUB", "INT_MULT", "INT_AND", "INT_OR", "INT_XOR",
                       "INT_LEFT", "INT_RIGHT", "SUBPIECE", "PIECE")) | SELF.keys()


def canonicalize(s):
    """Propagate copies/constants and cancel byte decomposition/reassembly.

Only equal-width values may alias one another. PACK of an entire BYTE sequence
recovers its source; partial packs stay explicit. Constant evaluation uses the
output width and p-code's zero result for shifts beyond the input width.
Only values with operands that can still alias are revisited.
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

    pending = list(s.values)
    changed = True
    while changed:
        changed = False
        s.simplify_phis()
        remaining = []
        for v in pending:
            if v.id in s.aliases:
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
            elif len(a) == 2 and a[0] is a[1] and v.opc in SELF:
                replacement = constant(v.size, SELF[v.opc])
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
            else:
                for arg in a:
                    if arg.opc in REWRITABLE:
                        remaining.append(v)
                        break
        pending = remaining


def whole(s, lanes):
    """The value whose complete byte sequence `lanes` is, or None."""
    lanes = [s.resolve(x) for x in lanes]
    if lanes[0].opc != "BYTE":
        return None
    source = s.resolve(lanes[0].args[0])
    if source.size != len(lanes) or any(
            x.opc != "BYTE" or x.data != n or s.resolve(x.args[0]) is not source
            for n, x in enumerate(lanes)):
        return None
    return source


def state_roots(s, state, keys, groups=()):
    """Values a publication of `keys` from `state` reads; a whole group reads its source."""
    keys = [key for key in keys if key in state]
    wanted, roots = set(keys), []
    for group in groups:
        if all(key in wanted for key in group):
            source = whole(s, [state[key] for key in group])
            if source is not None:
                roots.append(source)
                wanted.difference_update(group)
    return roots + [state[key] for key in keys if key in wanted]


def live_values(s, publications=None, extra_roots=(), removable=(), groups=()):
    """Find values needed by effects, control flow and observable CPU states.

    Outgoing state is observable on return. Each required effect snapshot is a
root, including registers/flags used only by a fault or helper observation.
An optional publication plan identifies fields that already reside in the CPU
and therefore need no SSA computation or assignment at that observation.
Memory tokens retain the dependency chain without authorizing load forwarding.

``removable`` names opcodes that are otherwise treated as roots but stay only
when used, such as the emitter's ``CALL_RELOAD`` callee-state reads.
"""
    todo = list(extra_roots)
    for b in s.blocks.values():
        for v in b.ops:
            # Unknown operations must survive to the consumer's diagnostic;
            # absence of a result use is not proof of absent effects.
            if v.opc not in PURE and v.opc not in removable:
                todo.append(v)
                if v.opc in EFFECTS or v.opc in EXITS:
                    state = b.snapshots[v.id] if v.opc in EFFECTS else b.exit
                    todo.extend(state_roots(s, state, state if publications is None
                                            else publications[v.id], groups))
    live = set()
    while todo:
        v = s.resolve(todo.pop())
        if v.id not in live:
            live.add(v.id)
            todo.extend(v.args)
    return live


def simplify(s, publications=None, *, canonical=True, extra_roots=(), removable=(), groups=()):
    """Canonicalize to a fixed point, then eliminate unobserved pure values.

    ``canonical=False`` skips the canonicalization pass for a caller that has
    already run it and made no operand change since (the production emitter
    canonicalizes before the publication plan). The pass is idempotent, so
    the emitted body is byte-identical; this only avoids the repeated work.
    """
    if canonical:
        canonicalize(s)
    live = live_values(s, publications, extra_roots, removable, groups)
    for b in s.blocks.values():
        b.ops = [v for v in b.ops if v.id in live and s.resolve(v) is v]
        b.phis = [v for v in b.phis if v.id in live and s.resolve(v) is v]
    return live
