"""Width-aware, effect-preserving simplification of integer SSA.

Aliases keep value IDs stable for snapshots and diagnostics. Pure rewrites never
move effects: even unused LOAD results retain their access and pre-access state.
The passes work on SSA rather than C spelling and can serve other consumers.
"""

# Effect/observation/termination classifications come from operations.py.
from .operations import EFFECTS, EXITS, OBSERVES, ORDERED, PURE, TERMINATORS

#: Policy: pure values that still count as liveness roots unless removable.
ROOTED_PURE = frozenset(("CONST", "TARGET", "INPUT", "PHI", "CALL_RELOAD", "CALL_KEEP"))
DEFAULT_NON_ROOTS = PURE - ROOTED_PURE

#: Results of an operation whose two operands are the same value.
SELF = {"INT_EQUAL": 1, "INT_LESSEQUAL": 1, "INT_SLESSEQUAL": 1, "INT_NOTEQUAL": 0,
        "INT_LESS": 0, "INT_SLESS": 0, "INT_XOR": 0, "INT_SUB": 0, "INT_SBORROW": 0}

REWRITABLE = frozenset(("PHI", "COPY", "INT_ZEXT", "BYTE", "PACK", "INT_ADD",
                       "INT_SUB", "INT_MULT", "INT_AND", "INT_OR", "INT_XOR",
                       "INT_EQUAL", "INT_NOTEQUAL", "INT_SLESS", "INT_SLESSEQUAL",
                       "BOOL_AND", "BOOL_OR", "BOOL_XOR", "INT_LEFT", "INT_RIGHT",
                       "SUBPIECE", "PIECE")) | SELF.keys()


def _signed_sub_parts(s, value):
    """Return ``(a, b, result)`` for a same-width signed-subtract predicate."""
    value = s.resolve(value)
    if value.size != 1 or value.opc not in ("INT_XOR", "INT_NOTEQUAL", "INT_EQUAL", "BOOL_XOR"):
        return None
    left, right = (s.resolve(arg) for arg in value.args)
    if left.size != 1 or right.size != 1:
        return None
    for sign, overflow in ((left, right), (right, left)):
        sign, overflow = s.resolve(sign), s.resolve(overflow)
        if (sign.opc != "INT_SLESS" or len(sign.args) != 2
                or overflow.opc != "INT_SBORROW" or len(overflow.args) != 2):
            continue
        result, zero = (s.resolve(arg) for arg in sign.args)
        a, b = (s.resolve(arg) for arg in overflow.args)
        if (result.opc != "INT_SUB" or result.size == 0
                or result.size != a.size or a.size != b.size
                or zero.opc != "CONST" or zero.size != result.size or zero.data != 0):
            continue
        sub_a, sub_b = (s.resolve(arg) for arg in result.args)
        if _same_packed_value(s, sub_a, a) and _same_packed_value(s, sub_b, b):
            return a, b, result
    return None


def _same_packed_value(s, left, right):
    """Accept independently assembled PACKs of the same byte values."""
    left, right = s.resolve(left), s.resolve(right)
    if left is right:
        return True
    return (left.opc == right.opc == "PACK" and left.size == right.size
            and len(left.args) == len(right.args)
            and all(s.resolve(a) is s.resolve(b) for a, b in zip(left.args, right.args)))


def _rewrite_signed_sub_predicate(s, value):
    """Replace overflow/sign combinations only when both describe one SUB."""
    parts = _signed_sub_parts(s, value)
    if parts is None:
        return False
    a, b, _ = parts
    value = s.resolve(value)
    if value.opc in ("INT_XOR", "INT_NOTEQUAL", "BOOL_XOR"):
        value.opc, value.args = "INT_SLESS", (a, b)
        return True
    if value.opc == "INT_EQUAL":
        value.opc, value.args = "INT_SLESSEQUAL", (b, a)
        return True
    return False


def _rewrite_signed_sub_le(s, value):
    """Fold ``(SF xor OF) || ZF`` for the same subtraction to signed <=."""
    value = s.resolve(value)
    if value.opc not in ("INT_OR", "BOOL_OR") or value.size != 1 or len(value.args) != 2:
        return False
    for relation, equal in ((value.args[0], value.args[1]), (value.args[1], value.args[0])):
        relation = s.resolve(relation)
        if relation.opc != "INT_SLESS" or relation.size != 1 or len(relation.args) != 2:
            continue
        a, b = (s.resolve(arg) for arg in relation.args)
        if a.size == 0 or a.size != b.size:
            continue
        equal = s.resolve(equal)
        if equal.opc != "INT_EQUAL" or equal.size != 1 or len(equal.args) != 2:
            continue
        left, right = (s.resolve(arg) for arg in equal.args)
        if left.opc == "CONST" and left.data == 0:
            zero, result = left, right
        elif right.opc == "CONST" and right.data == 0:
            zero, result = right, left
        else:
            continue
        if (zero.size != result.size or result.opc != "INT_SUB" or result.size != a.size
                or not _same_packed_value(s, result.args[0], a)
                or not _same_packed_value(s, result.args[1], b)):
            continue
        value.opc, value.args = "INT_SLESSEQUAL", (a, b)
        return True
    return False


def canonicalize(s, signed_sub=True):
    """Propagate copies/constants and cancel byte decomposition/reassembly.

Only equal-width values may alias one another. PACK of an entire BYTE sequence
recovers its source; partial packs stay explicit. Constant evaluation uses the
output width and p-code's zero result for shifts beyond the input width.
Only values with operands that can still alias are revisited. `signed_sub`
controls the predicate rewrites needed by codegen but not by preservation.
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
            if signed_sub and (_rewrite_signed_sub_predicate(s, v) or _rewrite_signed_sub_le(s, v)):
                changed = True
                remaining.append(v)
                continue
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
            if v.opc not in DEFAULT_NON_ROOTS and v.opc not in removable:
                todo.append(v)
                if v.opc in OBSERVES:
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
