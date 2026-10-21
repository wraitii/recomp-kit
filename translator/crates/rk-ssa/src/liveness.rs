use std::collections::BTreeMap;

use rk_core::LookupSet;
use rk_ir::{properties, Op, StateKey};

use crate::{Ssa, ValueId};

fn rooted(op: Op) -> bool {
    matches!(
        op,
        Op::Input(_) | Op::Const(_) | Op::Target(_) | Op::StateReload(_) | Op::StateKeep(_)
    )
}

pub fn live_values(
    s: &Ssa,
    publications: Option<&BTreeMap<ValueId, Vec<StateKey>>>,
    extra_roots: &[ValueId],
    removable: &[Op],
) -> LookupSet<ValueId> {
    let mut live = LookupSet::default();
    let mut work = extra_roots.to_vec();
    for block in &s.blocks {
        for &value in &block.ops {
            let op = s.value(value).op;
            if removable.contains(&op) {
                continue;
            }
            if properties(op).pure && !rooted(op) {
                continue;
            }
            work.push(value);
            if let Some(state) = s.boundary(value) {
                let keys = publications.and_then(|map| map.get(&value));
                let state: Vec<ValueId> = match keys {
                    Some(keys) => {
                        let wanted: LookupSet<StateKey> = keys.iter().copied().collect();
                        state
                            .iter()
                            .filter(|(key, _)| wanted.contains(key))
                            .map(|(_, value)| *value)
                            .collect()
                    }
                    None => state.iter().map(|(_, value)| *value).collect(),
                };
                work.extend(state);
            }
        }
    }
    while let Some(value) = work.pop() {
        if live.insert(value) {
            work.extend_from_slice(s.args(value));
        }
    }
    live
}

pub fn simplify(
    s: &mut Ssa,
    publications: Option<&BTreeMap<ValueId, Vec<StateKey>>>,
    extra_roots: &[ValueId],
    removable: &[Op],
) -> LookupSet<ValueId> {
    let live = live_values(s, publications, extra_roots, removable);
    for block in &mut s.blocks {
        block.ops.retain(|value| live.contains(value));
        block.phis.retain(|value| live.contains(value));
    }
    live
}
