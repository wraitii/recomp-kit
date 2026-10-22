use rk_core::ir::OpCode;
use rk_core::{Lookup, LookupSet};
use rk_ir::Op;

use crate::canonical::{canonicalize, rewrite_aliases};
use crate::{CallFacts, Ssa, ValueId};

pub fn forward(
    s: &mut Ssa,
    stack_reload: Option<&dyn Fn(ValueId) -> Option<(ValueId, i32)>>,
    call_facts: Option<&dyn CallFacts>,
) -> bool {
    let mut shapes: Lookup<ValueId, (ValueId, u32)> = Lookup::default();
    let mut bounds: Lookup<ValueId, (ValueId, u32, u32)> = Lookup::default();
    let mut active = LookupSet::default();

    fn address(
        s: &Ssa,
        value: ValueId,
        stack_reload: Option<&dyn Fn(ValueId) -> Option<(ValueId, i32)>>,
        shapes: &mut Lookup<ValueId, (ValueId, u32)>,
        active: &mut LookupSet<ValueId>,
    ) -> (ValueId, u32) {
        if let Some(&shape) = shapes.get(&value) {
            return shape;
        }
        if !active.insert(value) {
            return (value, 0);
        }
        let mut result = (value, 0);
        let special = stack_reload.and_then(|reload| reload(value));
        if let Some((base, delta)) = special {
            let (root, offset) = address(s, base, stack_reload, shapes, active);
            result = (root, offset.wrapping_add(delta as u32));
        } else {
            match s.value(value).op {
                Op::Phi => {
                    let mut forms = Vec::new();
                    for &arg in s.args(value) {
                        if arg != value {
                            forms.push(address(s, arg, stack_reload, shapes, active));
                        }
                    }
                    if !forms.is_empty() && forms.iter().all(|form| *form == forms[0]) {
                        result = forms[0];
                    }
                }
                Op::P(OpCode::IntAdd) | Op::P(OpCode::IntSub) if s.value(value).size == 4 => {
                    let args = s.args(value);
                    if args.len() == 2 {
                        let (a, b) = (args[0], args[1]);
                        if let Op::Const(data) = s.value(b).op {
                            let (root, offset) = address(s, a, stack_reload, shapes, active);
                            result = (root, if s.value(value).op == Op::P(OpCode::IntAdd) {
                                offset.wrapping_add(data as u32)
                            } else {
                                offset.wrapping_sub(data as u32)
                            });
                        } else if let Op::Const(data) = s.value(a).op {
                            if s.value(value).op == Op::P(OpCode::IntAdd) {
                                let (root, offset) = address(s, b, stack_reload, shapes, active);
                                result = (root, offset.wrapping_add(data as u32));
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        active.remove(&value);
        shapes.insert(value, result);
        result
    }

    fn interval(
        s: &Ssa,
        value: ValueId,
        stack_reload: Option<&dyn Fn(ValueId) -> Option<(ValueId, i32)>>,
        shapes: &mut Lookup<ValueId, (ValueId, u32)>,
        bounds: &mut Lookup<ValueId, (ValueId, u32, u32)>,
        active: &mut LookupSet<ValueId>,
    ) -> (ValueId, u32, u32) {
        if let Some(&bound) = bounds.get(&value) {
            return bound;
        }
        let (root, offset) = address(s, value, stack_reload, shapes, active);
        let mut result = (root, offset, offset);
        let base = s.value(root);
        if base.op == Op::P(OpCode::IntAnd) && base.size == 4 {
            let args = s.args(root);
            if args.len() == 2 {
                let (a, b) = (args[0], args[1]);
                let (a, b) = if matches!(s.value(a).op, Op::Const(_)) { (b, a) } else { (a, b) };
                if let Op::Const(data) = s.value(b).op {
                    let cleared = !data as u32;
                    if cleared < 256 && cleared & (cleared.wrapping_add(1)) == 0 {
                        let (origin, low, high) =
                            interval(s, a, stack_reload, shapes, bounds, active);
                        result = (origin, low.wrapping_add(offset).wrapping_sub(cleared), high.wrapping_add(offset));
                    }
                }
            }
        }
        bounds.insert(value, result);
        result
    }

    #[allow(clippy::too_many_arguments)]
    fn disjoint(
        s: &Ssa,
        a: ValueId,
        size_a: u32,
        b: ValueId,
        size_b: u32,
        offset_b: u32,
        stack_reload: Option<&dyn Fn(ValueId) -> Option<(ValueId, i32)>>,
        shapes: &mut Lookup<ValueId, (ValueId, u32)>,
        bounds: &mut Lookup<ValueId, (ValueId, u32, u32)>,
        active: &mut LookupSet<ValueId>,
    ) -> bool {
        let (root_a, low_a, high_a) = interval(s, a, stack_reload, shapes, bounds, active);
        let (root_b, low_b, high_b) = interval(s, b, stack_reload, shapes, bounds, active);
        if root_a != root_b {
            return false;
        }
        let low = low_a.wrapping_sub(high_b.wrapping_add(offset_b));
        let high = high_a.wrapping_sub(low_b.wrapping_add(offset_b));
        size_b <= low && low <= high && high <= 0x1_0000_0000u64.wrapping_sub(size_a as u64) as u32
    }

    let target = |s: &Ssa, load: ValueId| {
        let args = s.args(load);
        args.get(1).copied()
    };
    let mut alias: Lookup<ValueId, ValueId> = Lookup::default();
    let blocks = s.blocks.len();
    for block in 0..blocks {
        let ops = s.blocks[block].ops.clone();
        for load in ops {
            let value = s.value(load);
            if value.op != Op::P(OpCode::Load) || value.size != 4 {
                continue;
            }
            let Some(load_address) = target(s, load) else { continue };
            let target_address = address(s, load_address, stack_reload, &mut shapes, &mut active);
            let mut pending = vec![s.args(load)[0]];
            let mut visited = LookupSet::default();
            let mut saved = None;
            let mut failed = false;
            while let Some(token) = pending.pop() {
                if !visited.insert(token) {
                    continue;
                }
                if visited.len() > 512 {
                    failed = true;
                    break;
                }
                match s.value(token).op {
                    Op::Phi => {
                        pending.extend_from_slice(s.args(token));
                    }
                    Op::Memory => {
                        let effect = s.args(token)[0];
                        match s.value(effect).op {
                            Op::P(OpCode::Store) => {
                                let args = s.args(effect);
                                let (source, stored) = (args[1], args[2]);
                                let source_address =
                                    address(s, source, stack_reload, &mut shapes, &mut active);
                                if source_address == target_address && s.value(stored).size == 4 {
                                    if saved.is_some_and(|previous| previous != stored) {
                                        failed = true;
                                        break;
                                    }
                                    saved = Some(stored);
                                } else if disjoint(
                                    s,
                                    load_address,
                                    4,
                                    source,
                                    s.value(stored).size as u32,
                                    0,
                                    stack_reload,
                                    &mut shapes,
                                    &mut bounds,
                                    &mut active,
                                ) {
                                    pending.push(args[0]);
                                } else {
                                    failed = true;
                                    break;
                                }
                            }
                            Op::P(OpCode::Load) => {
                                let args = s.args(effect);
                                if s.value(effect).size == 4
                                    && address(s, args[1], stack_reload, &mut shapes, &mut active)
                                        == target_address
                                {
                                    if saved.is_some_and(|previous| previous != effect) {
                                        failed = true;
                                        break;
                                    }
                                    saved = Some(effect);
                                } else {
                                    pending.push(args[0]);
                                }
                            }
                            Op::Call { .. } | Op::CallInd => {
                                let target = match s.value(effect).op {
                                    Op::Call { target } => Some(target),
                                    _ => None,
                                };
                                let summary = call_facts.and_then(|facts| facts.stack(s.value_addr[effect as usize], target));
                                let Some(summary) = summary else { failed = true; break; };
                                if !summary.writes.is_empty() {
                                    let Some(base) = call_stack(s, effect) else { failed = true; break; };
                                    if !summary.writes.iter().all(|&(offset, size)| {
                                        disjoint(s, load_address, 4, base, size, offset as u32,
                                            stack_reload, &mut shapes, &mut bounds, &mut active)
                                    }) {
                                        failed = true;
                                        break;
                                    }
                                }
                                pending.push(s.args(effect)[0]);
                            }
                            _ => {
                                failed = true;
                                break;
                            }
                        }
                    }
                    _ => {
                        failed = true;
                        break;
                    }
                }
            }
            if !failed {
                if let Some(saved) = saved {
                    if saved != load {
                        alias.insert(load, saved);
                    }
                }
            }
        }
    }
    if alias.is_empty() {
        return false;
    }
    for index in 0..s.values.len() {
        if s.values[index].op != Op::Memory {
            continue;
        }
        let args = s.args(index as ValueId);
        if args.is_empty() {
            continue;
        }
        if let Some(&previous) = alias.get(&args[0]) {
            let _ = previous;
            let memory = s.args(args[0])[0];
            alias.insert(index as ValueId, memory);
        }
    }
    rewrite_aliases(s, &alias);
    canonicalize(s, true);
    true
}

fn call_stack(s: &Ssa, call: ValueId) -> Option<ValueId> {
    let state = s.boundary(call)?;
    let lanes = rk_ir::lanes(rk_core::contract::Field::Esp);
    let mut source = None;
    for (lane, key) in lanes.iter().enumerate() {
        let id = state.iter().find(|(candidate, _)| candidate == key)?.1;
        if s.value(id).op != Op::Byte(lane as u8) { return None; }
        let root = *s.args(id).first()?;
        if s.value(root).size != 4 || source.is_some_and(|prior| prior != root) { return None; }
        source = Some(root);
    }
    source
}
