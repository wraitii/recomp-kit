"""Lifted CFG regions, real call contexts and saved-CW provenance."""
import decoded
from ir import x87_mode as mode, x87_provenance as bits
from ir.call_contracts import cfg_is_closed
from ir.cfg import FunctionIR
from ir.lift import BRANCHES
from .common import MASK

CW_NEUTRAL_TRANSCENDENTALS = frozenset(('F2XM1', 'FPATAN', 'FPREM', 'FPREM1',
                                      'FSCALE', 'FSIN', 'FCOS', 'FSINCOS',
                                      'FPTAN', 'FYL2X', 'FYL2XP1'))


def internal_pcode_flow(ins):
    return ins.internal_flow and not (
        mode.instruction_mnemonic(ins) == 'JMP' and len(ins.ops) == 1
        and ins.ops[0].opc == 'BRANCH' and ins.ops[0].ins[0][0] == 'ram')


def tail_region(ctx, addr, lifted, opaque, limit=20000):
    """Follow mapped transfers without synthesizing calls or return pushes."""
    def body(owner):
        if owner in opaque:
            raise ValueError('tail enters opaque body %08x' % owner)
        if owner not in lifted:
            fn, entries = ctx.load(owner)
            ctx.tr.analyze(fn, entries)
            fir = decoded.function_ir(ctx.tr, ctx.lifter, fn)
            fir.entries = tuple(entries)
            if fn.pushed_continuations or fn.return_jumps or fn.dead_addrs:
                raise ValueError('rewritten flow in %08x' % owner)
            lifted[owner] = fir
        return lifted[owner]

    root = body(addr)
    pending, nodes, edges, transfers = [addr, *root.entries], {}, {}, []
    while pending:
        site = pending.pop()
        if site in nodes:
            continue
        if len(nodes) >= limit:
            raise ValueError('tail region exceeds instruction limit')
        owner = ctx.program.containing(site)
        if owner is None:
            raise ValueError('unmapped tail destination %08x' % site)
        fir = body(owner)
        index = {ins.addr: i for i, ins in enumerate(fir.insns)}
        if site not in index:
            raise ValueError('tail destination is not an instruction %08x' % site)
        i = index[site]
        if fir.seh or fir.noreturn:
            raise ValueError('SEH or nonreturning flow in %08x' % owner)
        ins = fir.insns[i]
        successors = [fir.insns[j].addr for j in fir.succ[i]]
        if i in fir.exits:
            targets = fir.exits[i]
            if not targets:
                raise ValueError('unresolved transfer at %08x' % site)
            successors.extend(targets)
            transfers.append({'site': '%08x' % site,
                              'targets': ['%08x' % t for t in targets]})
        nodes[site] = ins
        edges[site] = tuple(dict.fromkeys(successors))
        pending.extend(edges[site])
    sites = [addr] + sorted(set(nodes) - {addr})
    index = {site: i for i, site in enumerate(sites)}
    insns = [nodes[s] for s in sites]
    succ = [tuple(index[t] for t in edges[s]) for s in sites]
    tables = {i for i, ins in enumerate(insns)
              if any(op.opc == 'BRANCHIND' for op in ins.ops) and succ[i]}
    region = FunctionIR(addr, insns, succ, tables=tables,
                        entries=tuple(s for s in root.entries if s in index and s != addr))
    if not cfg_is_closed(region):
        raise ValueError('tail region has an unclosed terminal instruction')
    return region, sorted(transfers, key=lambda row: row['site'])


def cfg_relations(fir, lifter, inlined=(), return_targets=None, details=None,
                  track_escaped_stack=False, call_boundaries=None, terminating=(),
                  entry_cw_mask=0, entry_cw_value=0, stack_residue=None):
    call_boundaries = call_boundaries or {}
    def failure(reason, index=None):
        if details is not None:
            details['failure'] = {'reason': reason, 'node': index}
        return None

    closed = cfg_is_closed(fir) if not return_targets and not terminating else all(
        not fir.succ[i] if i in terminating else
        bool(fir.succ[i]) == (i in return_targets) if mode.instruction_mnemonic(ins) == 'RET'
        else bool(fir.succ[i]) for i, ins in enumerate(fir.insns))
    if (not closed or fir.entries or fir.seh or fir.exits or fir.noreturn
            or any(internal_pcode_flow(i) or not i.raw or any(o.opc == 'CALLOTHER' for o in i.ops)
                   and mode.instruction_mnemonic(i) not in CW_NEUTRAL_TRANSCENDENTALS
                   for i in fir.insns)):
        return failure('unsupported CFG, alternate entry or instruction semantics')
    cw, esp = lifter.register('FPUControlWord'), lifter.register('ESP')
    cells = {}
    bits.write_bits(cells, cw, tuple((entry_cw_value >> i) & 1 if entry_cw_mask & (1 << i)
                               else (bits.ENTRY_CW, i) for i in range(16)))
    bits.write_bits(cells, esp, bits.affine_bits(0, 32))
    initial = (cells, {})
    preds = [[] for _ in fir.insns]
    for i, targets in enumerate(fir.succ):
        for j in targets:
            preds[j].append(i)
    incoming, outgoing, relations = {}, {}, {}
    call_facts, return_values, cw_facts = {}, {}, {}
    active, branch_choices = {}, {}
    memory_invalidations, call_arguments = {}, {}
    work, queued = [0], {0}
    while work:
        i = work.pop()
        queued.discard(i)
        states = [outgoing[p] for p in preds[i] if p in outgoing and i in active.get(p, ())]
        if i == 0:
            states.append(initial)
        cells, memory = (dict(x) for x in states[0])
        for other_cells, other_memory in states[1:]:
            cells = {k: v for k, v in cells.items() if v is not None and other_cells.get(k) == v}
            memory = {k: tuple(v if v == other_memory.get(k, bits.unknown(8))[j] else None
                               for j, v in enumerate(value))
                      for k, value in memory.items() if k in other_memory}
        state = (cells, memory)
        if incoming.get(i) == state:
            continue
        incoming[i] = (dict(cells), dict(memory))
        cells = {k: v for k, v in cells.items() if k[0] != 'unique'}

        if i in terminating:
            outgoing[i] = (dict(cells), dict(memory))
            continue

        def foreign(value):
            if len(value) != 32 or not isinstance(value[0], tuple) or value[0][0] != 'foreign-address':
                return None
            _, identity, delta, _ = value[0]
            return (identity, delta) if value == tuple(('foreign-address', identity, delta, j) for j in range(32)) else None

        def read(var):
            space, off, size = var
            if space == 'const':
                return bits.const_bits(off, size * 8)
            return tuple(cells.get((space, off + byte, bit), None)
                         for byte in range(size) for bit in range(8))

        def load(address, size):
            delta = bits.affine(address)
            if delta is None:
                return bits.unknown(size * 8)
            return tuple(v for byte in range(size)
                         for v in memory.get((delta + byte) & 0xffffffff, bits.unknown(8)))

        def store(address, value):
            delta = bits.affine(address)
            if delta is None:
                identity = foreign(address)
                if identity is not None:
                    origin, offset = identity
                    if 0 <= offset and offset + len(value) // 8 <= call_boundaries[origin]['foreign_size']:
                        return True
                if (not track_escaped_stack
                        and any(isinstance(v, tuple) and v[0] == bits.STACK_ADDR for v in value)):
                    return False
                memory_invalidations[i] = bits.constant(address)
                memory.clear()
            else:
                for byte in range(len(value) // 8):
                    memory[(delta + byte) & 0xffffffff] = value[byte * 8:(byte + 1) * 8]
            return True

        ins = fir.insns[i]
        mnem = mode.instruction_mnemonic(ins)
        if mnem is None:
            return failure('instruction bytes do not decode to one mnemonic', i)
        cw_written = False
        branch = None
        for op in ins.ops:
            if op.opc == 'CBRANCH' and len(op.ins) == 2 and op.ins[0][0] == 'ram':
                branch = (op.ins[0][1], bits.constant(read(op.ins[1])))
            if op.opc == 'RETURN' and return_targets and i in return_targets:
                return_values[i] = bits.constant(read(op.ins[0])) if op.ins else None
            if op.opc == 'STORE':
                if len(op.ins) != 2 or not store(read(op.ins[0]), read(op.ins[1])):
                    return failure('stack address escapes through an unresolved store', i)
                continue
            if op.opc in BRANCHES or op.out is None:
                continue
            if op.out[0] == 'ram':
                if not store(bits.const_bits(op.out[1], 32), read(op.ins[0])
                             if op.opc == 'COPY' else bits.unknown(op.out[2] * 8)):
                    return failure('stack address escapes through a direct store', i)
                continue
            if op.out[0] not in ('register', 'unique', 'stin', 'stout'):
                return failure('unsupported pcode output space ' + op.out[0], i)
            cw_written |= (op.out[0] == 'register' and op.out[1] < cw[1] + cw[2]
                           and op.out[1] + op.out[2] > cw[1])
            width = op.out[2] * 8
            value = bits.unknown(width)
            if op.opc == 'COPY' and op.ins:
                value = read(op.ins[0])
            elif op.opc == 'LOAD' and op.ins:
                value = load(read(op.ins[-1]), op.out[2])
            elif op.opc in ('INT_OR', 'INT_AND', 'INT_XOR') and len(op.ins) == 2:
                value = bits.bitwise(op.opc, read(op.ins[0]), read(op.ins[1]))
                if op.opc == 'INT_AND' and stack_residue is not None and width == 32:
                    for pointer, mask_value in ((read(op.ins[0]), read(op.ins[1])),
                                                (read(op.ins[1]), read(op.ins[0]))):
                        offset, mask = bits.affine(pointer), bits.constant(mask_value)
                        if offset is not None and mask is not None:
                            cleared = (~mask) & 0xffffffff
                            if cleared <= 15 and not cleared & (cleared + 1):
                                delta = ((offset + stack_residue) & mask) - stack_residue
                                value = bits.affine_bits(bits.signed(delta, 32), 32)
            elif op.opc in ('INT_ADD', 'INT_SUB') and len(op.ins) == 2:
                left, right = read(op.ins[0]), read(op.ins[1])
                la, ra = bits.affine(left), bits.affine(right)
                lc, rc = bits.constant(left), bits.constant(right)
                delta = None
                if la is not None and rc is not None:
                    delta = la + bits.signed(rc, len(right)) * (1 if op.opc == 'INT_ADD' else -1)
                elif op.opc == 'INT_ADD' and ra is not None and lc is not None:
                    delta = ra + bits.signed(lc, len(left))
                if delta is not None:
                    value = bits.affine_bits(delta, width)
                elif lc is not None and rc is not None:
                    value = bits.const_bits(lc + rc if op.opc == 'INT_ADD' else lc - rc, width)
                elif foreign(left) is not None and rc is not None:
                    identity, offset = foreign(left)
                    offset = (offset + bits.signed(rc, len(right)) * (1 if op.opc == 'INT_ADD' else -1)) & 0xffffffff
                    value = tuple(('foreign-address', identity, offset, j) for j in range(width))
            elif op.opc in ('INT_ZEXT', 'INT_SEXT') and op.ins:
                source = read(op.ins[0])
                if len(source) <= width:
                    value = source + (0 if op.opc == 'INT_ZEXT' else source[-1],) * (width - len(source))
            elif op.opc == 'SUBPIECE' and len(op.ins) == 2:
                off = bits.constant(read(op.ins[1]))
                if off is not None:
                    value = read(op.ins[0])[off * 8:off * 8 + width]
            elif op.opc == 'PIECE' and len(op.ins) == 2:
                value = read(op.ins[1]) + read(op.ins[0])
            elif op.opc in ('INT_EQUAL', 'INT_NOTEQUAL') and len(op.ins) == 2:
                left, right = read(op.ins[0]), read(op.ins[1])
                unequal = any(a in (0, 1) and b in (0, 1) and a != b for a, b in zip(left, right))
                equal = all(a is not None and a == b for a, b in zip(left, right))
                if unequal or equal:
                    value = bits.const_bits(int(equal) ^ int(op.opc == 'INT_NOTEQUAL'), width)
            elif op.opc == 'BOOL_NEGATE' and op.ins:
                source = bits.constant(read(op.ins[0]))
                if source is not None:
                    value = bits.const_bits(int(not source), width)
            elif op.opc == 'INT_NEGATE' and op.ins:
                source = read(op.ins[0])
                value = tuple(1 - bit if bit in (0, 1) else None for bit in source)
            elif op.opc == 'INT_MULT' and len(op.ins) == 2:
                left, right = (bits.constant(read(var)) for var in op.ins)
                if left is not None and right is not None:
                    value = bits.const_bits(left * right, width)
            elif op.opc in ('BOOL_AND', 'BOOL_OR', 'BOOL_XOR') and len(op.ins) == 2:
                value = bits.bitwise(op.opc.replace('BOOL_', 'INT_'), read(op.ins[0]), read(op.ins[1]))
            elif op.opc in ('INT_LESS', 'INT_SLESS', 'INT_LESSEQUAL', 'INT_SLESSEQUAL') and len(op.ins) == 2:
                left, right = (read(var) for var in op.ins)
                lc, rc = bits.constant(left), bits.constant(right)
                if lc is not None and rc is not None:
                    if op.opc.startswith('INT_S'):
                        lc, rc = bits.signed(lc, len(left)), bits.signed(rc, len(right))
                    value = bits.const_bits(int(lc <= rc if op.opc.endswith('LESSEQUAL') else lc < rc), width)
            bits.write_bits(cells, op.out, value)
        if mnem in bits.SETTERS:
            bits.write_bits(cells, cw, bits.const_bits(0x037f, 16))
        elif mnem in bits.ENV_MASKERS:
            bits.write_bits(cells, cw, tuple(1 if j < 6 else v for j, v in enumerate(read(cw))))
        elif mnem in bits.ENV_RESTORES and (mnem != 'FLDENV' or not cw_written):
            bits.write_bits(cells, cw, bits.unknown(16))
        elif mnem == 'FLDCW' and not cw_written:
            bits.write_bits(cells, cw, bits.unknown(16))
        elif (mnem not in CW_NEUTRAL_TRANSCENDENTALS and mode.writer_effect(mnem) is None
              and mode.instruction_effect(mnem)[0] == 'unknown'):
            bits.write_bits(cells, cw, bits.unknown(16))
        if mnem in ('FLDCW', 'FLDENV'):
            relations[i] = bits.fact(read(cw))
        if any(o.opc in ('CALL', 'CALLIND') for o in ins.ops):
            call_facts[i] = bits.fact(read(cw))
            delta = bits.affine(read(esp))
            call_arguments[i] = [bits.fact(load(bits.affine_bits(delta + offset, 32), 4))
                                 for offset in (4, 8, 12, 16)] if delta is not None else []
            if i in call_boundaries:
                boundary = call_boundaries[i]
                old_cw, old_esp = read(cw), read(esp)
                kept = {name: read(lifter.register(name)) for name in ('EBX', 'ESI', 'EDI', 'EBP')}
                cells.clear()
                for name, value in kept.items():
                    bits.write_bits(cells, lifter.register(name), value)
                delta = bits.affine(old_esp)
                if delta is not None:
                    bits.write_bits(cells, esp, bits.affine_bits(delta + 4 + boundary['stack_pop'], 32))
                mask = int(boundary['preserved_mask'], 16)
                bits.write_bits(cells, cw, tuple(v if mask & (1 << j) else None for j, v in enumerate(old_cw)))
                if boundary.get('foreign_eax'):
                    bits.write_bits(cells, lifter.register('EAX'),
                                tuple(('foreign-address', i, 0, j) for j in range(32)))
                if not boundary.get('preserve_stack'):
                    memory.clear()
            elif i not in inlined:
                cells.clear()
                memory.clear()
        cw_facts[i] = bits.fact(read(cw))
        state = (cells, memory)
        enabled = set(fir.succ[i])
        if return_targets and i in return_targets and return_values.get(i) is not None:
            enabled = {j for j in enabled if fir.insns[j].addr == return_values[i]}
        if branch is not None and branch[1] is not None:
            enabled = {j for j in enabled if (fir.insns[j].addr == branch[0]) == bool(branch[1])}
            branch_choices[i] = bool(branch[1])
        enabled |= active.get(i, set())
        if outgoing.get(i) != state or active.get(i) != enabled:
            active[i] = enabled
            outgoing[i] = (dict(cells), dict(memory))
            for j in enabled:
                if j not in queued:
                    work.append(j)
                    queued.add(j)
    unmatched = [i for i, value in return_values.items()
                 if value not in (return_targets[i] if isinstance(return_targets[i], tuple) else (return_targets[i],))]
    if details is not None:
        details.update(calls=call_facts, outgoing=cw_facts, unmatched_returns=unmatched,
                       reachable=sorted(outgoing), active_edges={i: sorted(js) for i, js in active.items()},
                       branch_choices=branch_choices, memory_invalidations=memory_invalidations,
                       call_arguments=call_arguments, load_relations=relations)
    return None if unmatched else relations


def context_graph(ctx, root, depth, limit, opaque, contracts=()):
    bodies, validated, target_errors = {}, {}, {}
    boundary_functions = {int(row['function'], 16): row for row in contracts if row['kind'] == 'frame_call'}
    terminate_functions = {int(row['function'], 16) for row in contracts if row['kind'] == 'terminate_function'}
    terminate_calls = {int(row['site'], 16) for row in contracts if row['kind'] == 'terminate_call'}

    def locate(site):
        owner = ctx.program.containing(site)
        if owner is None or owner in opaque:
            raise ValueError('unmapped or opaque destination %08x' % site)
        if owner not in bodies:
            fn, entries = ctx.load(owner)
            ctx.tr.analyze(fn, entries)
            fir = decoded.function_ir(ctx.tr, ctx.lifter, fn)
            if (fn.pushed_continuations or fn.return_jumps or fn.dead_addrs
                    or fir.seh or fir.noreturn) and owner not in terminate_functions:
                raise ValueError('unsupported flow in %08x' % owner)
            bodies[owner] = (fir, {ins.addr: i for i, ins in enumerate(fir.insns)})
        fir, indices = bodies[owner]
        if site not in indices:
            raise ValueError('destination is not an instruction %08x' % site)
        return fir, indices[site]

    entry = (root, ())
    pending, nodes, edges = [entry], {}, {}
    inlined, returns, frontier, transfers = set(), {}, {}, set()
    boundaries, terminating = {}, set()
    while pending:
        key = pending.pop()
        if key in nodes:
            continue
        if len(nodes) >= limit:
            raise ValueError('context graph exceeds %d instructions' % limit)
        site, stack = key
        fir, i = locate(site)
        ins = fir.insns[i]
        if site in terminate_functions or site in terminate_calls:
            nodes[key], edges[key] = ins, ()
            terminating.add(key)
            continue
        successors = [(fir.insns[j].addr, stack) for j in fir.succ[i]]
        if i in fir.exits:
            if not fir.exits[i]:
                raise ValueError('unknown transfer %08x' % site)
            successors.extend((t, stack) for t in fir.exits[i])
            transfers.add(site)
        call = any(op.opc in ('CALL', 'CALLIND') for op in ins.ops)
        if call:
            target = mode.direct_call_target(ins)
            reason = None
            if target in boundary_functions:
                boundaries[key] = boundary_functions[target]
                nodes[key], edges[key] = ins, tuple(successors)
                pending.extend(edges[key])
                continue
            if target is None:
                reason = 'indirect call'
            elif len(stack) >= depth:
                reason = 'call depth limit'
            elif site in {frame[1] for frame in stack}:
                reason = 'recursive call'
            elif len(successors) != 1 or i in fir.exits:
                reason = 'ambiguous call continuation'
            else:
                try:
                    locate(target)
                    if target not in target_errors:
                        try:
                            if not terminate_functions:
                                tail_region(ctx, target, validated, opaque, limit)
                            target_errors[target] = None
                        except ValueError as error:
                            target_errors[target] = str(error)
                    reason = target_errors[target]
                except ValueError as error:
                    reason = str(error)
            if reason:
                frontier[key] = (target, reason)
            else:
                continuation = successors[0][0]
                successors = [(target, stack + ((continuation, site),))]
                inlined.add(key)
        if any(op.opc == 'RETURN' for op in ins.ops) and stack:
            returns[key] = tuple(frame[0] for frame in stack)
            successors = [(frame[0], stack[:index]) for index, frame in enumerate(stack)]
        nodes[key], edges[key] = ins, tuple(dict.fromkeys(successors))
        pending.extend(edges[key])
    keys = [entry] + sorted(set(nodes) - {entry})
    indices = {key: i for i, key in enumerate(keys)}
    insns = [nodes[key] for key in keys]
    succ = [tuple(indices[t] for t in edges[key]) for key in keys]
    tables = {i for i, ins in enumerate(insns)
              if any(op.opc == 'BRANCHIND' for op in ins.ops) and succ[i]}
    fir = FunctionIR(root, insns, succ, tables=tables)
    # RET successors and inline CALL successors deliberately model the guest
    # stack instructions, with return destinations checked by provenance.
    return (fir, keys, {indices[k] for k in inlined}, {indices[k]: v for k, v in returns.items()},
            frontier, transfers, {indices[k]: v for k, v in boundaries.items()}, {indices[k] for k in terminating})
