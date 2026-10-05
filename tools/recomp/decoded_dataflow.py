"""Bounded decoded effects for whole-function scalar flag dataflow.

Guest loads and audited x87 helpers cannot observe integer flags in the current
runtime. Their instruction-exact interior fault snapshots are outside the
agreed performance-mode contract. Integer stores retain watch/callback barriers;
calls, exceptions, unknown helpers and environment observers retain every flag.
The C driver emits the conservative path under RECOMP_NULL_CHECKS.

DIVERGENCE(original): [decoded-flag-dataflow] ordinary interior load/FP faults
may see an earlier integer-flag snapshot. All supported observers and outgoing
CPU state remain exact against the eager runtime. No dead outgoing state is
assumed and no callee ABI is inferred.
"""
from dataclasses import dataclass


MAX_INSTRUCTIONS = 2048


@dataclass(frozen=True)
class Effects:
    flag_defs: frozenset
    flag_uses: frozenset
    memory_read: bool = False
    memory_write: bool = False
    observer: bool = False


def decode(ins, image=None):
    """Describe audited effects, refusing unfamiliar spellings/helpers.

    These are runtime effects, including preserved architecturally undefined
    bits. TEST/AND/OR/XOR leave AF untouched in the comparison runtime.
    """
    import translate as T
    from x87_dataflow import effect
    all_flags, none = T.ALL_FLAGS, T.NO_FLAGS
    try:
        ops = [T.parse_operand(o) for o in ins.ops]
    except T.TranslateError:
        return Effects(none, all_flags, observer=True)
    if ins.rep or any(o.kind not in ('reg', 'imm', 'mem', 'st') or
                      (o.kind == 'mem' and o.seg) for o in ops):
        return Effects(none, all_flags, observer=True)
    memory = any(o.kind == 'mem' for o in ops)
    m = ins.mnem
    if m in T.JCC:
        return Effects(none, frozenset(T.JCC[m][1]))
    if m == 'JMP':
        return Effects(none, all_flags, observer=True)  # internal edges handled by the solver
    if m.startswith('F') and effect(ins, T.parse_operand, image) is not None:
        return Effects(none, none, memory_read=memory,
                       memory_write=m in ('FST', 'FSTP'))
    arith = {'ADD', 'ADC', 'SUB', 'SBB', 'CMP', 'INC', 'DEC', 'NEG',
             'AND', 'OR', 'XOR', 'TEST', 'NOT'}
    moves = {'MOV', 'MOVZX', 'MOVSX', 'LEA', 'NOP'}
    if m not in arith | moves or any(o.kind not in ('reg', 'imm', 'mem') for o in ops):
        return Effects(none, all_flags, observer=True)
    write = bool(ops and ops[0].kind == 'mem' and m not in ('CMP', 'TEST', 'LEA'))
    if write:
        # wr8/16/32 can invoke recomp_watch_hit; don't infer an observer ABI.
        return Effects(none, all_flags, memory_read=memory, memory_write=True, observer=True)
    defs, uses = T.FLAG_EFFECT.get(m, (none, none))
    if m in ('AND', 'OR', 'XOR', 'TEST'):
        defs -= {'af'}
    return Effects(defs, uses, memory_read=memory and m != 'LEA')


def flag_liveness(tr, fn):
    """Compute must-definition/may-read facts across loops and joins.

    Boundaries still require all six outgoing flags. Unknown effects and
    configured patches retain conservative transfer facts. Returning None
    means the function exceeds the analysis budget and uses the old solver.
    """
    import translate as T
    n = len(fn.insns)
    if n > MAX_INSTRUCTIONS:
        return None
    effects = [decode(ins, tr.image) for ins in fn.insns]
    succ = [tr.liveness_successors(fn, i) for i in range(n)]
    preds = [[] for _ in range(n)]
    for i, targets in enumerate(succ):
        for j in targets:
            preds[j].append(i)
    incoming, outgoing = [T.NO_FLAGS] * n, [T.NO_FLAGS] * n
    work, queued = list(range(n)), set(range(n))
    while work:
        i = work.pop()
        queued.remove(i)
        ins, e = fn.insns[i], effects[i]
        out = T.NO_FLAGS
        for j in succ[i]:
            out |= incoming[j]
        if tr.liveness_exit(fn, i, ins):
            out = T.ALL_FLAGS
        defs, uses = e.flag_defs, e.flag_uses
        if ins.addr in T.INSTRUCTION_PATCHES or ins.addr in T.VISUAL_ANIMATION_READS:
            defs, uses = T.NO_FLAGS, T.ALL_FLAGS
        elif ins.mnem == 'JMP' and tr.liveness_stays_inside(fn, ins):
            uses = T.NO_FLAGS
        live = (out - defs) | uses
        if live != incoming[i] or out != outgoing[i]:
            incoming[i], outgoing[i] = live, out
            for j in preds[i]:
                if j not in queued:
                    queued.add(j)
                    work.append(j)
    return outgoing
