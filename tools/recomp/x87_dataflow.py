"""Opt-in decoded x87 dataflow for bounded C regions.

Compute stack effects and binary32 facts on the instruction CFG before emitting
locals. Register copies/exchanges carry values, tags and exact-integer metadata;
division keeps the eager runtime's double operation and ZE/status handling.
Unknown effects, calls, observers, alternate entries and gaps are publication
boundaries. Inconsistent TOP joins retain the existing local/eager lowering.

Optional bounded stack forwarding retains stores and rounded float reload values;
it does not relax outgoing state. The existing
x87-local-status/binary32 interior-fault and exponent-range policies still apply.

DIVERGENCE(original): [x87-local-status] interior faults can see the preceding
published x87 state/status; supported observers receive materialized state.
DIVERGENCE(original): [x87-binary32] proven binary32 arithmetic under PC=00 uses
the existing float exponent-range policy. Division stays double-backed.
"""
from dataclasses import dataclass
import re

from x87_locals import BranchRegion, FLOAT_OPS, REGISTER_OPS, eligible


ARITH = frozenset(('FADD', 'FSUB', 'FSUBR', 'FMUL', 'FDIV', 'FDIVR'))
EXTRA = frozenset(('FDIV', 'FDIVR', 'FDIVP', 'FDIVRP', 'FXCH', 'FCHS'))
MAX_INSTRUCTIONS = 512


@dataclass(frozen=True)
class Effect:
    """Logical stack reads/writes plus a metadata/width transfer description."""
    kind: str
    reads: tuple = ()
    writes: tuple = ()
    delta: int = 0
    narrow: bool = False


def effect(ins, parse_operand, image=None):
    """Decode only audited effects; unknown instructions have no local path.

    Single-register arithmetic spelling can hide its destination; decode its
    opcode direction exactly as the baseline does, or refuse without bytes.
    """
    m = ins.mnem
    if ins.rep:
        return None
    try:
        ops = [parse_operand(o) for o in ins.ops]
        if any(o.kind not in ('reg', 'imm', 'mem', 'st') or
               (o.kind == 'mem' and o.seg) for o in ops):
            return None
        if m in REGISTER_OPS | {'PUSH', 'POP'}:
            return Effect('integer') if all(o.kind in ('reg', 'imm', 'mem') for o in ops) else None
        if m not in FLOAT_OPS | EXTRA:
            return None
        if m not in EXTRA and not eligible(ins, parse_operand):
            return None
        if any(o.kind == 'mem' and o.size not in (32, 64) for o in ops):
            return None
        if m in ('FLD1', 'FLDZ'):
            return Effect('push', writes=(-1,), delta=-1, narrow=True)
        if m == 'FLD' and len(ops) == 1:
            if ops[0].kind == 'st':
                return Effect('copy', reads=(ops[0].sti,), writes=(-1,), delta=-1)
            return Effect('push', writes=(-1,), delta=-1, narrow=ops[0].size == 32)
        if m in ('FST', 'FSTP') and len(ops) == 1:
            delta = int(m == 'FSTP')
            if ops[0].kind == 'st':
                return Effect('copy', reads=(0,), writes=(ops[0].sti,), delta=delta)
            return Effect('store', reads=(0,), delta=delta)
        if m == 'FXCH' and all(o.kind == 'st' for o in ops):
            other = ops[-1].sti if ops else 1
            return Effect('swap', reads=(0, other), writes=(0, other))
        if m == 'FCHS' and not ops:
            return Effect('sign', reads=(0,), writes=(0,))
        base = m[:-1] if m.endswith('P') and m[:-1] in ARITH else m
        if base in ARITH:
            if m.endswith('P'):
                if any(o.kind != 'st' for o in ops):
                    return None
                dst = ops[0].sti if ops else 1
                return Effect('arithmetic', reads=(dst, 0), writes=(dst,), delta=1)
            if len(ops) == 2 and all(o.kind == 'st' for o in ops):
                dst, src = ops[0].sti, ops[1].sti
                return Effect('arithmetic', reads=(dst, src), writes=(dst,))
            if len(ops) == 1 and ops[0].kind == 'st':
                if image is None:
                    return None
                at = ins.addr
                while image.rd8(at) in (0x26, 0x2e, 0x36, 0x3e, 0x64, 0x65, 0x66, 0x67, 0x9b):
                    at += 1
                dst, src = (ops[0].sti, 0) if image.rd8(at) == 0xdc else (0, ops[0].sti)
                return Effect('arithmetic', reads=(dst, src), writes=(dst,))
            if len(ops) == 1 and ops[0].kind == 'mem':
                return Effect('arithmetic', reads=(0,), writes=(0,))
            return None
        if m in ('FCOM', 'FCOMP', 'FUCOM', 'FUCOMP'):
            other = (ops[0].sti,) if ops and ops[0].kind == 'st' else (() if ops else (1,))
            return Effect('compare', reads=(0, *other), delta=int(m.endswith('P')))
        if m in ('FCOMPP', 'FUCOMPP') and not ops:
            return Effect('compare', reads=(0, 1), delta=2)
        if m == 'FNSTSW':
            return Effect('status')
    except (ValueError, IndexError, AttributeError):
        return None
    return None


def width_transfer(e, top, incoming):
    """Proven binary32 slots conditional on PC=00; entry values are unknown.

    Popping does not erase residue. Copies/exchanges use simultaneous input
    facts, and arithmetic establishes a rounded result even from wider inputs.
    """
    result = set(incoming)
    if e.kind == 'swap':
        a, b = ((top + s) & 7 for s in e.writes)
        result.discard(a)
        result.discard(b)
        if b in incoming:
            result.add(a)
        if a in incoming:
            result.add(b)
    elif e.writes:
        dst = (top + e.writes[0]) & 7
        result.discard(dst)
        known = e.kind == 'arithmetic' or (e.kind == 'push' and e.narrow)
        if e.kind in ('copy', 'sign'):
            known = ((top + e.reads[0]) & 7) in incoming
        if known:
            result.add(dst)
    return frozenset(result)


@dataclass
class Plan:
    start: int
    end: int
    tops: dict
    widths: dict
    modified: dict


def written_slots(e, top):
    """Slot metadata changes on pops even when the retained value is unchanged."""
    return frozenset({(top + s) & 7 for s in e.writes} |
                     {(top + s) & 7 for s in range(max(0, e.delta))})


def solve(start, end, effects, successors):
    """Bounded fixed point: relative TOP equality and width intersection.

    Each region has one external entry. Loop backedges participate in the
    width meet with that entry; a narrow fact is never inferred from PC alone.
    """
    tops, widths, outputs = {start: 0}, {start: frozenset()}, {}
    queue = [start]
    pending = {start}
    while queue:
        i = queue.pop()
        pending.remove(i)
        e = effects[i]
        out = width_transfer(e, tops[i], widths[i])
        top = (tops[i] + e.delta) & 7
        if outputs.get(i) == out:
            continue
        outputs[i] = out
        for j in successors[i]:
            if not start <= j < end:
                continue
            if j in tops and tops[j] != top:
                return None
            tops[j] = top
            incoming = out if j not in widths else widths[j] & out
            if j not in widths or incoming != widths[j]:
                widths[j] = incoming
                if j not in pending:
                    queue.append(j)
                    pending.add(j)
    if len(tops) != end - start:
        return None
    # May-write facts are separate from binary32 must-facts. Publish only slots
    # that can have changed on an exit path; an early dispatch branch otherwise
    # keeps all incoming values/metadata live across unrelated arithmetic.
    modified, queue, pending = {start: frozenset()}, [start], {start}
    while queue:
        i = queue.pop()
        pending.remove(i)
        out = modified[i] | written_slots(effects[i], tops[i])
        for j in successors[i]:
            if not start <= j < end:
                continue
            incoming = modified.get(j, frozenset()) | out
            if j not in modified or incoming != modified[j]:
                modified[j] = incoming
                if j not in pending:
                    queue.append(j)
                    pending.add(j)
    return Plan(start, end, tops, widths, modified)


def stack_forwarding(fn, start, end, preds, effects, parse_operand):
    """Forward binary32 stack spills within one decoded basic block.

    At most four store locals and 32 instructions per proof. Every intervening
    write (even a disjoint-looking pointer), ESP/subregister mutation, branch,
    observer or unsupported effect kills the proof. Memory reads may alias the
    spill: retaining the original store preserves their order and result.
    No cross-join/backedge speculation or assumption of nonescaping stack.
    """
    stores, reloads, live = {}, {}, {}
    for i in range(start, end):
        if i != start and preds[i] != {i - 1}:
            live.clear()
        ins, e = fn.insns[i], effects[i]
        ops = [parse_operand(o) for o in ins.ops]
        stack = (ops[0] if len(ops) == 1 and ops[0].kind == 'mem' and
                 ops[0].size == 32 and ops[0].base == 4 and
                 ops[0].index is None and not ops[0].seg else None)
        if ins.mnem == 'FLD' and stack is not None:
            source = live.get(stack.disp)
            if source is not None and i - source <= 32:
                if source in stores or len(stores) < 4:
                    stores[source] = f'stack_float_{source}_'
                    reloads[i] = stores[source]
        # Only audited instructions with read-only guest memory can intervene.
        readonly = (e.kind in ('push', 'arithmetic', 'compare', 'copy', 'swap', 'sign') or
                    (ins.mnem == 'FNSTSW' and ops and ops[0].kind == 'reg') or
                    (e.kind == 'integer' and ins.mnem not in ('PUSH', 'POP') and
                     (not ops or ops[0].kind != 'mem' or ins.mnem in ('CMP', 'TEST')) and
                     (not ops or ops[0].kind != 'reg' or ops[0].reg != 4 or
                      ins.mnem in ('CMP', 'TEST'))))
        if not readonly:
            live.clear()
        if ins.mnem in ('FST', 'FSTP') and stack is not None:
            live[stack.disp] = i
    return stores, reloads


class DataflowRegion(BranchRegion):
    """Reuse baseline arithmetic expressions, with complete copy metadata."""
    def __init__(self):
        super().__init__()
        self.metadata = set()

    def copy(self, src, dst):
        self.used.update((src, dst))
        self.metadata.update((src, dst))
        self.modified.add(dst)
        self.writes += 1
        return '\n'.join(f'x87_{field}{dst}_ = x87_{field}{src}_;'
                         for field in ('s', 'bits', 'exact', 'tag'))

    def swap(self, other):
        a, b = self.top, (self.top + other) & 7
        self.used.update((a, b))
        self.metadata.update((a, b))
        self.modified.update((a, b))
        self.writes += 2
        lines = ['{']
        for field, typ in (('s', 'double'), ('bits', 'uint64_t'), ('exact', 'uint8_t'), ('tag', 'unsigned')):
            lines += [f'{typ} swap_{field}_ = x87_{field}{a}_;',
                      f'x87_{field}{a}_ = x87_{field}{b}_;',
                      f'x87_{field}{b}_ = swap_{field}_;']
        return lines + ['}']

    def append_effect(self, i, body, ins, e):
        if e.kind == 'swap':
            # Reuse the audited eager operand selection, preserving its comment.
            match = re.fullmatch(r'fxch\(c, (\d+)\);(?:  /\*.*\*/)?', body[0])
            if len(body) != 1 or not match:
                return False
            self.lines = self.swap(int(match[1]))
            return True
        if not self.append(i, body, ins.mnem in FLOAT_OPS | EXTRA):
            return False
        # fdivz only accesses status. Keep its original double division; do
        # not infer that a float division followed by rounding is equivalent.
        self.lines = [line.replace('fdivz(c,', 'fdivz(&x87_env_,') for line in self.lines]
        # Refuse an unmodeled helper/CPU-stack access instead of emitting a
        # mixture whose local state can be observed by an eager helper.
        return not any(re.search(r'\b(?:f[a-z0-9_]+\(c[,)]|ST\(c,|c->(?:st|fpu_top|fpu_tag))', line)
                       for line in self.lines)

    def declarations(self, entry):
        lines = super().declarations(entry)
        lines[0] = lines[0].replace('local x87 CFG', 'decoded x87 dataflow')
        for slot in sorted(self.metadata - self.modified):
            phys = f'((x87_top_ + {slot}u) & 7u)'
            lines += [f'uint64_t x87_bits{slot}_ = c->st_bits[{phys}];',
                      f'uint8_t x87_exact{slot}_ = c->st_exact[{phys}];',
                      f'unsigned x87_tag{slot}_ = ftag_of(c, {phys});']
        return lines

    def commit_exit(self, top, modified):
        saved = self.modified
        self.modified = saved & modified
        lines = self.commit(top)
        self.modified = saved
        return lines


def lower_function(fn, bodies, labels, dead, parse_operand, protected, cfg, image=None, forward_stack=False):
    """Plan from decoded effects, then emit bounded regions with full exits.

    Leave ordinary regions to the established pass. This stage is admitted
    where divisions, exchanges or copies previously cut local dataflow, or where
    a supported stack reload can be forwarded.
    """
    if not forward_stack and not any(ins.mnem in EXTRA or
               (ins.mnem in ('FLD', 'FST', 'FSTP') and
                any(parse_operand(o).kind == 'st' for o in ins.ops)) for ins in fn.insns):
        return dict(bodies), set(), 0
    successors, branch_target, conditional, entries = cfg
    n = len(fn.insns)
    succ = [successors(fn, i) for i in range(n)]
    preds = [set() for _ in range(n)]
    for i, targets in enumerate(succ):
        for j in targets:
            preds[j].add(i)
    for addr in (fn.addr, *entries, *fn.pushed_continuations):
        if addr in fn.index:
            preds[fn.index[addr]].add(-1)
    effects = {}
    for i, ins in enumerate(fn.insns):
        if (i in dead or ins.addr in protected or not fn.contiguous[i] or
                any('recomp_' in line or 'CALL_FN(' in line for line in bodies[i])):
            continue
        if ins.mnem in conditional or ins.mnem == 'JMP':
            if branch_target(ins) in fn.index:
                effects[i] = Effect('branch')
        else:
            e = effect(ins, parse_operand, image)
            if e is not None:
                effects[i] = e
    ranges, start = [], None
    for i in range(n + 1):
        if i in effects:
            if start is None:
                start = i
        elif start is not None:
            ranges.append((start, i))
            start = None
    work, ranges = ranges, []
    while work:
        a, b = work.pop()
        split = next((i for i in range(a + 1, b)
                      if any(j < a or j >= b for j in preds[i])), None)
        if split is not None:
            work.extend(((a, split), (split, b)))
        else:
            ranges.append((a, b))
    result, consumed, count = dict(bodies), set(), 0
    for a, b in sorted(ranges):
        if b - a > MAX_INSTRUCTIONS:
            continue
        stores, reloads = stack_forwarding(fn, a, b, preds, effects, parse_operand) if forward_stack else ({}, {})
        if not reloads and not any(
                fn.insns[i].mnem in EXTRA or effects[i].kind == 'copy' for i in range(a, b)):
            continue
        plan = solve(a, b, effects, succ)
        if plan is None:
            continue
        region, rewritten = DataflowRegion(), {}
        for i in range(a, b):
            region.top, region.single = plan.tops[i], set(plan.widths[i])
            region.lines = []
            body = list(bodies[i])
            if i in stores:
                # Capture the existing fto_float result, preserving CW rounding,
                # signaling-NaN conversion and the actual guest write.
                from translate import addr_expr
                operand = parse_operand(fn.insns[i].ops[0])
                if not any('wrf32(' + addr_expr(operand) + ', fto_float(c, ST(c, 0))' in line for line in body):
                    break
                body = [re.sub(r'(wrf32\(.*?, )(fto_float\(c, ST\(c, 0\)\))',
                               lambda m: m[1] + '(' + stores[i] + ' = ' + m[2] + ')', line)
                        for line in body]
                if not any(stores[i] in line for line in body):
                    break
            if i in reloads:
                from translate import addr_expr
                operand = parse_operand(fn.insns[i].ops[0])
                expr = 'rdf32(' + addr_expr(operand) + ')'
                if not any(expr in line for line in body):
                    break
                body = [line.replace(expr, reloads[i]) for line in body]
            if not region.append_effect(i, body, fn.insns[i], effects[i]):
                break
            rewritten[i] = region.lines
        if len(rewritten) != b - a or region.writes < 3:
            continue
        lines = region.declarations(fn.insns[a].addr)
        lines.extend(f"float {name};" for name in stores.values())
        for i in range(a, b):
            addr = fn.insns[i].addr
            if addr in labels or i == a:
                lines.append(f'L_x87_{addr:08x}: ;')
            top = (plan.tops[i] + effects[i].delta) & 7
            modified = plan.modified[i] | written_slots(effects[i], plan.tops[i])
            for line in rewritten[i]:
                def transfer(match):
                    target = int(match[1], 16)
                    if a <= fn.index[target] < b:
                        return f'goto L_x87_{target:08x};'
                    return '{ ' + ' '.join(region.commit_exit(top, modified)) + f' goto L_{target:08x}; }}'
                lines.append(re.sub(r'goto L_([0-9a-f]{8});', transfer, line))
            if i == b - 1 and fn.insns[i].mnem != 'JMP':
                lines.extend(region.commit_exit(top, modified))
        lines.append('}')
        result[a] = lines
        for i in range(a + 1, b):
            result[i] = []
            labels.discard(fn.insns[i].addr)
        consumed.update(range(a, b))
        count += 1
    return result, consumed, count
