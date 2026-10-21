use std::collections::BTreeMap;

use rk_core::contract::Field;
use rk_core::{Diagnostic, Lookup, LookupSet, Reason};
use rk_ir::{lanes, Op};

use crate::{BlockId, Ssa, ValueId, DISPATCH_BLOCK};

pub fn verify(s: &Ssa) -> Result<(), Diagnostic> {
    if s.values.len() != s.value_addr.len() {
        return fail(s, "value and address tables disagree");
    }
    let defs = definitions(s)?;
    check_args(s, &defs)?;
    check_boundaries(s, &defs)?;
    check_state(s)?;
    Ok(())
}

fn fail<T>(s: &Ssa, detail: impl Into<String>) -> Result<T, Diagnostic> {
    Err(Diagnostic::new(s.addr, Reason::SsaVerify { detail: detail.into() }))
}

fn definitions(s: &Ssa) -> Result<Vec<Option<(BlockId, u32)>>, Diagnostic> {
    let mut defs = vec![None; s.values.len()];
    for (block_id, block) in s.blocks.iter().enumerate() {
        for &phi in &block.phis {
            define(s, &mut defs, phi, block_id as BlockId, 0)?;
        }
        for (index, &value) in block.ops.iter().enumerate() {
            define(s, &mut defs, value, block_id as BlockId, index as u32 + 1)?;
        }
    }
    Ok(defs)
}

fn define(
    s: &Ssa,
    defs: &mut [Option<(BlockId, u32)>],
    value: ValueId,
    block: BlockId,
    position: u32,
) -> Result<(), Diagnostic> {
    let Some(slot) = defs.get_mut(value as usize) else {
        return fail(s, format!("value {value} is out of range"));
    };
    if let Some(previous) = *slot {
        return fail(
            s,
            format!("value {value} is defined twice at {previous:?} and ({block}, {position})"),
        );
    }
    *slot = Some((block, position));
    Ok(())
}

fn rooted(op: Op) -> bool {
    matches!(op, Op::Const(_) | Op::Input(_) | Op::Target(_))
}

fn rooted_expr(s: &Ssa, value: ValueId, stack: &mut LookupSet<ValueId>) -> bool {
    let item = s.value(value);
    if rooted(item.op) {
        return true;
    }
    if !matches!(item.op, Op::Pack | Op::Byte(_)) || !stack.insert(value) {
        return false;
    }
    let result = s.args(value).iter().all(|&arg| rooted_expr(s, arg, stack));
    stack.remove(&value);
    result
}

fn check_args(s: &Ssa, defs: &[Option<(BlockId, u32)>]) -> Result<(), Diagnostic> {
    let idom = dominators(s)?;
    let root = s.blocks.len();
    for (block_id, block) in s.blocks.iter().enumerate() {
        let block_id = block_id as BlockId;
        for &phi in &block.phis {
            let value = s.value(phi);
            if value.op != Op::Phi {
                return fail(s, format!("block {block_id} lists non-phi {phi}"));
            }
            if value.args_len as usize != block.preds.len() {
                return fail(
                    s,
                    format!(
                        "phi {phi} in block {block_id} has {} args for {} predecessors",
                        value.args_len,
                        block.preds.len()
                    ),
                );
            }
            for (position, &arg) in s.args(phi).iter().enumerate() {
                let pred = block.preds[position];
                if pred == DISPATCH_BLOCK {
                    let mut stack = LookupSet::default();
                    if defs.get(arg as usize).is_some_and(Option::is_some)
                        || !rooted_expr(s, arg, &mut stack)
                    {
                        return fail(s, format!("phi {phi} entry arg {arg} is not an input"));
                    }
                    continue;
                }
                let mut stack = LookupSet::default();
                check_edge(s, defs, &idom, root, arg, pred, None, Some(phi), &mut stack)?;
            }
        }
        for (index, &value) in block.ops.iter().enumerate() {
            if s.value(value).op == Op::Phi {
                return fail(s, format!("block {block_id} lists phi {value} as an op"));
            }
            for &arg in s.args(value) {
                let mut stack = LookupSet::default();
                check_edge(
                    s,
                    defs,
                    &idom,
                    root,
                    arg,
                    block_id,
                    Some(index as u32 + 1),
                    Some(value),
                    &mut stack,
                )?;
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn check_edge(
    s: &Ssa,
    defs: &[Option<(BlockId, u32)>],
    idom: &[usize],
    root: usize,
    arg: ValueId,
    use_block: BlockId,
    use_position: Option<u32>,
    user: Option<ValueId>,
    stack: &mut LookupSet<ValueId>,
) -> Result<(), Diagnostic> {
    let Some(value) = s.values.get(arg as usize) else {
        return fail(s, format!("argument {arg} is out of range"));
    };
    match defs[arg as usize] {
        Some((def_block, def_position)) => {
            if def_block == use_block {
                if use_position.is_some_and(|position| def_position >= position) {
                    return fail(
                        s,
                        format!("argument {arg} of {user:?} is used before its definition"),
                    );
                }
            } else if !dominates(idom, root, def_block as usize, use_block as usize) {
                return fail(
                    s,
                    format!("argument {arg} used in block {use_block} is defined in {def_block}"),
                );
            }
        }
        None if rooted(value.op) => {}
        None if value.op == Op::Phi => {
            return fail(s, format!("argument {arg} of {user:?} is an unattached phi"));
        }
        None => {
            if !stack.insert(arg) {
                return fail(s, format!("argument {arg} of {user:?} is cyclic"));
            }
            for &inner in s.args(arg) {
                check_edge(
                    s,
                    defs,
                    idom,
                    root,
                    inner,
                    use_block,
                    use_position,
                    Some(arg),
                    stack,
                )?;
            }
            stack.remove(&arg);
        }
    }
    Ok(())
}

fn is_defined(
    s: &Ssa,
    defs: &[Option<(BlockId, u32)>],
    value: ValueId,
    stack: &mut LookupSet<ValueId>,
) -> bool {
    if defs.get(value as usize).is_some_and(Option::is_some) {
        return true;
    }
    let Some(item) = s.values.get(value as usize) else {
        return false;
    };
    if rooted(item.op) {
        return true;
    }
    if item.op == Op::Phi || !stack.insert(value) {
        return false;
    }
    let result = s.args(value).iter().all(|&arg| is_defined(s, defs, arg, stack));
    stack.remove(&value);
    result
}

fn dominators(s: &Ssa) -> Result<Vec<usize>, Diagnostic> {
    let count = s.blocks.len();
    let root = count;
    let mut preds: Vec<Vec<usize>> = s
        .blocks
        .iter()
        .map(|block| {
            block
                .preds
                .iter()
                .map(|&pred| if pred == DISPATCH_BLOCK { root } else { pred as usize })
                .collect()
        })
        .collect();
    preds.push(Vec::new());
    let mut succ = vec![Vec::new(); count + 1];
    for (block, block_preds) in preds.iter().enumerate().take(count) {
        for &pred in block_preds {
            succ[pred].push(block);
        }
    }
    let order = rpo(root, &succ);
    let mut number = vec![usize::MAX; count + 1];
    for (index, &block) in order.iter().enumerate() {
        number[block] = index;
    }
    let mut idom = vec![usize::MAX; count + 1];
    idom[root] = root;
    let mut changed = true;
    while changed {
        changed = false;
        for &block in order.iter().skip(1) {
            let mut next = usize::MAX;
            for &pred in &preds[block] {
                if idom[pred] == usize::MAX {
                    continue;
                }
                next = if next == usize::MAX {
                    pred
                } else {
                    intersect(pred, next, &idom, &number)
                };
            }
            if next != usize::MAX && idom[block] != next {
                idom[block] = next;
                changed = true;
            }
        }
    }
    if idom.contains(&usize::MAX) {
        return fail(s, "control-flow graph has an unreachable block");
    }
    Ok(idom)
}

fn intersect(mut a: usize, mut b: usize, idom: &[usize], number: &[usize]) -> usize {
    while a != b {
        while number[a] > number[b] {
            a = idom[a];
        }
        while number[b] > number[a] {
            b = idom[b];
        }
    }
    a
}

fn dominates(idom: &[usize], root: usize, def: usize, use_block: usize) -> bool {
    let mut block = use_block;
    loop {
        if block == def {
            return true;
        }
        if block == root || idom[block] == block || idom[block] == usize::MAX {
            return false;
        }
        block = idom[block];
    }
}

fn rpo(root: usize, succ: &[Vec<usize>]) -> Vec<usize> {
    let mut seen = vec![false; succ.len()];
    let mut post = Vec::new();
    seen[root] = true;
    let mut stack = vec![(root, 0usize)];
    while let Some((block, next)) = stack.last_mut() {
        if *next < succ[*block].len() {
            let child = succ[*block][*next];
            *next += 1;
            if !seen[child] {
                seen[child] = true;
                stack.push((child, 0));
            }
        } else {
            post.push(*block);
            stack.pop();
        }
    }
    post.reverse();
    post
}

fn check_boundaries(s: &Ssa, defs: &[Option<(BlockId, u32)>]) -> Result<(), Diagnostic> {
    let key_index: BTreeMap<_, _> =
        s.keys.iter().enumerate().map(|(index, key)| (*key, index)).collect();
    for boundary in &s.boundaries {
        if boundary.at as usize >= s.values.len() {
            return fail(s, format!("boundary points at missing value {}", boundary.at));
        }
        if defs[boundary.at as usize].is_none() {
            return fail(s, format!("boundary value {} has no definition", boundary.at));
        }
        let start = boundary.state_start as usize;
        let end = start + boundary.state_len as usize;
        let Some(state) = s.state.get(start..end) else {
            return fail(s, format!("boundary {} state range is out of range", boundary.at));
        };
        let mut present = [0usize; Field::ALL.len()];
        for (key, value) in state {
            let mut stack = LookupSet::default();
            if !is_defined(s, defs, *value, &mut stack) {
                return fail(
                    s,
                    format!("boundary {} state value {value} for {key:?} has no definition", boundary.at),
                );
            }
            if let Some(&index) = key_index.get(key) {
                if let Some(field) = s.field_of[index] {
                    present[field as usize] += 1;
                }
            }
        }
        for field in Field::ALL {
            let count = present[field as usize];
            if count > 0 && count < lanes(field).len() {
                return fail(
                    s,
                    format!(
                        "boundary {} publishes {count} of {} lanes of {}",
                        boundary.at,
                        lanes(field).len(),
                        field.name()
                    ),
                );
            }
        }
    }
    Ok(())
}

fn check_state(s: &Ssa) -> Result<(), Diagnostic> {
    let mut grouped = LookupSet::default();
    let mut phi_of_key: Vec<Lookup<usize, ValueId>> = (0..s.blocks.len()).map(|_| Lookup::default()).collect();
    for (block_id, block) in s.blocks.iter().enumerate() {
        for &phi in &block.phis {
            if s.lane_groups.iter().any(|group| group.phi == phi) {
                grouped.insert(phi);
                continue;
            }
            let Some(&key) = s.phi_key.get(&phi) else {
                return fail(s, format!("phi {phi} in block {block_id} has no state key"));
            };
            if key >= s.keys.len() {
                return fail(s, format!("phi {phi} has state key {key} out of range"));
            }
            if phi_of_key[block_id].insert(key, phi).is_some() {
                return fail(s, format!("block {block_id} has two phis for key {key}"));
            }
        }
    }
    for group in &s.lane_groups {
        let block = group.block as usize;
        for lane in &group.lanes {
            if lane.key >= s.keys.len() {
                return fail(s, format!("grouped phi {} has key {} out of range", group.phi, lane.key));
            }
            if phi_of_key[block].insert(lane.key, lane.value).is_some() {
                return fail(s, format!("block {block} has two phis for key {}", lane.key));
            }
        }
    }

    let mut end_cache: Lookup<(BlockId, usize), Option<ValueId>> = Lookup::default();
    for (block_id, block) in s.blocks.iter().enumerate() {
        for &phi in &block.phis {
            if grouped.contains(&phi) {
                continue;
            }
            let Some(&key) = s.phi_key.get(&phi) else {
                return fail(s, format!("phi {phi} in block {block_id} has no state key"));
            };
            for (position, &arg) in s.args(phi).iter().enumerate() {
                let pred = block.preds[position];
                let expected = end_value(s, &phi_of_key, &mut end_cache, pred, key);
                if expected != Some(arg) {
                    return fail(
                        s,
                        format!(
                            "phi {phi} key {key} arg {position} is {arg}, predecessor {pred} ends with {expected:?}"
                        ),
                    );
                }
            }
        }
    }
    for group in &s.lane_groups {
        let block = group.block as usize;
        for lane in &group.lanes {
            for (position, &arg) in lane.args.iter().enumerate() {
                let pred = s.blocks[block].preds[position];
                let expected = end_value(s, &phi_of_key, &mut end_cache, pred, lane.key);
                if expected != Some(arg) {
                    return fail(
                        s,
                        format!(
                            "grouped phi {} key {} arg {position} is {arg}, predecessor {pred} ends with {expected:?}",
                            group.phi, lane.key
                        ),
                    );
                }
            }
        }
    }

    for (block_id, reads) in s.entry_reads.iter().enumerate() {
        if reads.is_empty() {
            continue;
        }
        let block_id = block_id as BlockId;
        let mut used = LookupSet::default();
        for &phi in &s.blocks[block_id as usize].phis {
            used.insert(phi);
            for &arg in s.args(phi) {
                used.insert(arg);
            }
        }
        for &value in &s.blocks[block_id as usize].ops {
            for &arg in s.args(value) {
                used.insert(arg);
            }
        }
        for &(key, recorded) in reads {
            if !used.contains(&recorded) {
                continue;
            }
            if key >= s.keys.len() {
                return fail(s, format!("block {block_id} read has key {key} out of range"));
            }
            let expected = if let Some(&phi) = phi_of_key[block_id as usize].get(&key) {
                Some(phi)
            } else {
                let mut value = None;
                let mut agree = true;
                for &pred in s.blocks[block_id as usize].preds.iter().filter(|&&pred| pred != block_id) {
                    match end_value(s, &phi_of_key, &mut end_cache, pred, key) {
                        Some(candidate) if value.is_none() || value == Some(candidate) => {
                            value = Some(candidate);
                        }
                        _ => {
                            agree = false;
                            break;
                        }
                    }
                }
                agree.then_some(value).flatten()
            };
            if expected != Some(recorded) {
                return fail(
                    s,
                    format!(
                        "block {block_id} enters key {key} as {recorded}, expected {expected:?}"
                    ),
                );
            }
        }
    }
    Ok(())
}

fn end_value(
    s: &Ssa,
    phi_of_key: &[Lookup<usize, ValueId>],
    cache: &mut Lookup<(BlockId, usize), Option<ValueId>>,
    block: BlockId,
    key: usize,
) -> Option<ValueId> {
    match end_inner(s, phi_of_key, cache, &mut LookupSet::default(), block, key) {
        End::Value(value) => Some(value),
        End::Vary | End::Cycle => None,
    }
}

enum End {
    Value(ValueId),
    Vary,
    Cycle,
}

fn end_inner(
    s: &Ssa,
    phi_of_key: &[Lookup<usize, ValueId>],
    cache: &mut Lookup<(BlockId, usize), Option<ValueId>>,
    active: &mut LookupSet<(BlockId, usize)>,
    block: BlockId,
    key: usize,
) -> End {
    if block == DISPATCH_BLOCK {
        return match s.dispatch.loads.get(key) {
            Some((_, value)) => End::Value(*value),
            None => End::Vary,
        };
    }
    if let Some(&value) = cache.get(&(block, key)) {
        return value.map_or(End::Vary, End::Value);
    }
    if !active.insert((block, key)) {
        return End::Cycle;
    }
    let mut saw_cycle = false;
    let result = if let Some(value) = s.exit_state[block as usize].get(key).copied().flatten() {
        End::Value(value)
    } else if let Some(&phi) = phi_of_key[block as usize].get(&key) {
        End::Value(phi)
    } else {
        let mut value = None;
        let mut disagree = false;
        for &pred in s.blocks[block as usize].preds.iter().filter(|&&pred| pred != block) {
            match end_inner(s, phi_of_key, cache, active, pred, key) {
                End::Value(candidate) => {
                    if value.is_some_and(|previous| previous != candidate) {
                        disagree = true;
                        break;
                    }
                    value = Some(candidate);
                }
                End::Vary => {
                    disagree = true;
                    break;
                }
                End::Cycle => saw_cycle = true,
            }
        }
        if disagree {
            End::Vary
        } else if let Some(value) = value {
            End::Value(value)
        } else if saw_cycle {
            End::Cycle
        } else {
            End::Vary
        }
    };
    active.remove(&(block, key));
    match result {
        End::Value(value) => {
            cache.insert((block, key), Some(value));
            End::Value(value)
        }
        End::Vary if !saw_cycle => {
            cache.insert((block, key), None);
            End::Vary
        }
        End::Vary => End::Vary,
        End::Cycle => End::Cycle,
    }
}
