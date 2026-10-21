use std::collections::{BTreeMap, BTreeSet};

use rk_core::contract::Fields;
use rk_core::ir::Policy;
use rk_core::{Addr, Diagnostic, LookupSet, Result};
use rk_ir::{CgFunction, StateKey};
use rk_ssa::{live_values, prepare, publications, CallFacts, FlagPublication, PrepareOptions, Ssa, ValueId};

#[derive(Clone, Debug, Default)]
pub struct ContractRequests {
    pub by_caller: BTreeMap<Addr, std::collections::BTreeSet<(Addr, Fields)>>,
}

pub struct PlanOptions<'a> {
    pub call_facts: &'a dyn CallFacts,
    pub policy: Policy,
    pub x87_modes: &'a rk_x87::ModeInput,
    pub x87_options: rk_x87::Options,
    pub fault_state: rk_core::settings::FaultState,
    pub private_targets: &'a BTreeSet<Addr>,
}

impl<'a> PlanOptions<'a> {
    pub fn new(call_facts: &'a dyn CallFacts) -> Self {
        static MODES: std::sync::OnceLock<rk_x87::ModeInput> = std::sync::OnceLock::new();
        static TARGETS: std::sync::OnceLock<BTreeSet<Addr>> = std::sync::OnceLock::new();
        Self {
            call_facts,
            policy: Policy::Optimizing,
            x87_modes: MODES.get_or_init(rk_x87::ModeInput::default),
            x87_options: rk_x87::Options::default(),
            fault_state: rk_core::settings::FaultState::Relaxed,
            private_targets: TARGETS.get_or_init(BTreeSet::new),
        }
    }
}

pub struct PlannedFunction {
    pub ssa: Ssa,
    pub publish: BTreeMap<ValueId, Vec<StateKey>>,
    pub guard: BTreeMap<ValueId, Vec<StateKey>>,
    pub live: LookupSet<ValueId>,
    pub return_addr: BTreeMap<ValueId, Addr>,
    pub block_addrs: Vec<Addr>,
    pub settle_calls: BTreeSet<ValueId>,
    pub flag_publish: BTreeMap<ValueId, FlagPublication>,
    pub entry_settle: bool,
    pub call_masks: BTreeMap<ValueId, (Addr, Fields)>,
    pub requests: ContractRequests,
    pub x87: Option<rk_x87::Plan>,
}

pub fn plan_function(cg: &CgFunction, opts: &PlanOptions<'_>) -> Result<PlannedFunction> {
    let mut prepare_options = PrepareOptions::new(opts.call_facts);
    prepare_options.policy = opts.policy;
    prepare_options.fault_state = opts.fault_state;
    let ssa = prepare(cg, &prepare_options)?;
    let base = publications(&ssa, opts.call_facts);
    let (publish, guard) = reduce_publications(&ssa, &base, opts.call_facts);
    let flag_plan = ssa.flag_plan();
    let mut flag_publish = BTreeMap::new();
    let mut descriptor_roots = Vec::new();
    let mut live_publish = publish.clone();
    for (value, keys) in &publish {
        let mut publication = flag_plan.publication(&ssa, *value, keys);
        if std::env::var_os("RK_NO_DESCRIPTORS").is_some() {
            publication.eager |= publication.descriptor.map_or(Fields::NONE, |d| d.mask);
            publication.descriptor = None;
        }
        if let Some(descriptor) = &publication.descriptor {
            descriptor_roots.extend(descriptor.values());
            if let Some(live_keys) = live_publish.get_mut(value) {
                live_keys.retain(|key| {
                    !ssa.field(*key).is_some_and(|field| descriptor.mask.contains(field))
                });
            }
        }
        flag_publish.insert(*value, publication);
    }
    for (&value, keys) in &guard {
        live_publish.entry(value).or_default().extend(keys);
    }
    let live = live_values(&ssa, Some(&live_publish), &descriptor_roots, &[]);
    let entry_settle = (0..ssa.values.len()).any(|index| {
        let id = index as ValueId;
        live.contains(&id)
            && matches!(ssa.value(id).op, rk_ir::Op::Input(key) if ssa.field(key).is_some_and(|field| Fields::FLAGS.contains(field)))
    });
    let mut return_addr = BTreeMap::new();
    for (index, value) in ssa.values.iter().enumerate() {
        if !matches!(value.op, rk_ir::Op::Call { .. }) {
            continue;
        }
        let addr = ssa.value_addr[index];
        if let Some(insn) = cg.index_of(addr).and_then(|n| cg.insns.get(n as usize)) {
            return_addr.insert(index as ValueId, addr + insn.length as Addr);
        }
    }
    let block_addrs = ssa
        .blocks
        .iter()
        .map(|block| cg.insns[block.start as usize].addr)
        .collect();
    let mut settle_calls = BTreeSet::new();
    for (index, value) in ssa.values.iter().enumerate() {
        if !live.contains(&(index as ValueId)) {
            continue;
        }
        let (rk_ir::Op::StateKeep(key) | rk_ir::Op::StateReload(key)) = value.op else { continue };
        if !ssa.field(key).is_some_and(|field| Fields::FLAGS.contains(field)) {
            continue;
        }
        let site = ssa.args(index as ValueId)[0];
        if matches!(ssa.value(site).op, rk_ir::Op::Call { .. } | rk_ir::Op::CallInd) {
            settle_calls.insert(site);
        }
    }
    let mut reload_masks: BTreeMap<ValueId, Fields> = BTreeMap::new();
    for (index, value) in ssa.values.iter().enumerate() {
        let id = index as ValueId;
        let rk_ir::Op::StateReload(key) = value.op else { continue };
        let Some(field) = ssa.field(key) else { continue };
        let site = ssa.args(id)[0];
        *reload_masks.entry(site).or_default() |= Fields(field.bit());
    }
    for (value, keys) in &base {
        let Some(state) = ssa.boundary(*value) else { continue };
        for &(key, state_value) in state {
            if !keys.contains(&key) {
                continue;
            }
            let rk_ir::Op::StateReload(loaded) = ssa.value(state_value).op else { continue };
            let Some(field) = ssa.field(loaded) else { continue };
            let site = ssa.args(state_value)[0];
            *reload_masks.entry(site).or_default() |= Fields(field.bit());
        }
    }
    let mut call_masks = BTreeMap::new();
    let mut requested: BTreeMap<(Addr, Fields), ()> = BTreeMap::new();
    for (index, value) in ssa.values.iter().enumerate() {
        let id = index as ValueId;
        let rk_ir::Op::Call { target } = value.op else { continue };
        if !opts.private_targets.contains(&target) {
            continue;
        }
        let mask = reload_masks.get(&id).copied().unwrap_or_default();
        call_masks.insert(id, (target, mask));
        requested.insert((target, mask), ());
    }
    let mut requests = ContractRequests::default();
    if !requested.is_empty() {
        requests
            .by_caller
            .insert(ssa.addr, requested.keys().copied().collect());
    }
    let x87 = rk_x87::prepare(&ssa, opts.x87_modes, &opts.x87_options)
        .map_err(|reason| Diagnostic::error(ssa.addr, reason))?;
    let x87 = x87.used.then_some(x87);
    Ok(PlannedFunction {
        ssa,
        publish,
        guard,
        live,
        return_addr,
        block_addrs,
        settle_calls,
        flag_publish,
        entry_settle,
        call_masks,
        requests,
        x87,
    })
}

fn reduce_publications(
    s: &Ssa,
    base: &BTreeMap<ValueId, Vec<StateKey>>,
    call_facts: &dyn CallFacts,
) -> (BTreeMap<ValueId, Vec<StateKey>>, BTreeMap<ValueId, Vec<StateKey>>) {
    let block_count = s.blocks.len();
    if block_count == 0 || s.keys.is_empty() {
        return (BTreeMap::new(), BTreeMap::new());
    }
    let n = s.keys.len();
    let key_index: rk_core::Lookup<StateKey, usize> = s
        .keys
        .iter()
        .enumerate()
        .map(|(index, key)| (*key, index))
        .collect();
    let dispatch: Vec<Option<ValueId>> = s
        .keys
        .iter()
        .map(|key| {
            s.dispatch
                .loads
                .iter()
                .find(|(loaded, _)| loaded == key)
                .map(|(_, value)| *value)
        })
        .collect();
    let successors = successors(s);
    let mut out = vec![vec![None; n]; block_count];
    let mut pending: std::collections::VecDeque<usize> = (0..block_count).collect();
    let mut queued = vec![true; block_count];
    while let Some(block) = pending.pop_front() {
        queued[block] = false;
        let mut resident = resident_in(s, block, &out, &dispatch);
        walk_block(s, block, base, call_facts, &key_index, &mut resident, None, None);
        if resident != out[block] {
            out[block] = resident;
            for &next in &successors[block] {
                if !queued[next] {
                    queued[next] = true;
                    pending.push_back(next);
                }
            }
        }
    }
    let mut publish = BTreeMap::new();
    let mut guard = BTreeMap::new();
    for block in 0..block_count {
        let mut resident = resident_in(s, block, &out, &dispatch);
        walk_block(
            s,
            block,
            base,
            call_facts,
            &key_index,
            &mut resident,
            Some(&mut publish),
            Some(&mut guard),
        );
    }
    (publish, guard)
}

fn successors(s: &Ssa) -> Vec<Vec<usize>> {
    let mut successors = vec![Vec::new(); s.blocks.len()];
    for (index, block) in s.blocks.iter().enumerate() {
        for pred in &block.preds {
            if *pred != rk_ssa::DISPATCH_BLOCK {
                successors[*pred as usize].push(index);
            }
        }
    }
    successors
}

fn resident_in(
    s: &Ssa,
    block: usize,
    out: &[Vec<Option<ValueId>>],
    dispatch: &[Option<ValueId>],
) -> Vec<Option<ValueId>> {
    let preds = &s.blocks[block].preds;
    let mut resident: Option<Vec<Option<ValueId>>> = None;
    for pred in preds {
        let source: &[Option<ValueId>] = if *pred == rk_ssa::DISPATCH_BLOCK {
            dispatch
        } else {
            &out[*pred as usize]
        };
        match &mut resident {
            None => resident = Some(source.to_vec()),
            Some(current) => {
                for (left, right) in current.iter_mut().zip(source) {
                    if left != right {
                        *left = None;
                    }
                }
            }
        }
    }
    resident.unwrap_or_else(|| vec![None; dispatch.len()])
}

fn walk_block(
    s: &Ssa,
    block: usize,
    base: &BTreeMap<ValueId, Vec<StateKey>>,
    call_facts: &dyn CallFacts,
    key_index: &rk_core::Lookup<StateKey, usize>,
    resident: &mut [Option<ValueId>],
    mut publish: Option<&mut BTreeMap<ValueId, Vec<StateKey>>>,
    mut guard: Option<&mut BTreeMap<ValueId, Vec<StateKey>>>,
) {
    let item = &s.blocks[block];
    for value in item.phis.iter().chain(item.ops.iter()).copied() {
        let Some(state) = s.boundary(value) else { continue };
        for &(key, state_value) in state {
            let Some(&index) = key_index.get(&key) else { continue };
            match s.value(state_value).op {
                rk_ir::Op::StateReload(loaded) if loaded == key => {
                    resident[index] = Some(state_value);
                }
                rk_ir::Op::StateKeep(loaded) if loaded == key => {
                    let old = s.args(state_value)[1];
                    if resident[index] == Some(old) {
                        resident[index] = Some(state_value);
                    }
                }
                _ => {}
            }
        }
        let Some(keys) = base.get(&value) else { continue };
        let current: BTreeMap<StateKey, ValueId> =
            state.iter().copied().collect();
        for key in keys {
            let Some(&state_value) = current.get(key) else { continue };
            let Some(&index) = key_index.get(key) else { continue };
            if resident[index] != Some(state_value) {
                resident[index] = Some(state_value);
                if let Some(out) = publish.as_deref_mut() {
                    out.entry(value).or_default().push(*key);
                }
            }
        }
        if let Some(out) = guard.as_deref_mut() {
            if matches!(
                s.value(value).op,
                rk_ir::Op::Call { .. } | rk_ir::Op::CallInd
            ) {
                for (index, key) in s.keys.iter().enumerate() {
                    if *key == StateKey::Memory || s.field(*key).is_none() {
                        continue;
                    }
                    if keys.iter().any(|required| required == key) {
                        continue;
                    }
                    let Some(&state_value) = current.get(key) else { continue };
                    if resident[index] == Some(state_value) {
                        continue;
                    }
                    out.entry(value).or_default().push(*key);
                }
            }
        }
        let writes = boundary_writes(s, call_facts, value);
        if writes != Fields::NONE {
            for (index, key) in s.keys.iter().enumerate() {
                if s.field(*key).is_some_and(|field| writes.contains(field)) {
                    resident[index] = None;
                }
            }
        }
    }
}

fn boundary_writes(s: &Ssa, call_facts: &dyn CallFacts, value: ValueId) -> Fields {
    match s.value(value).op {
        rk_ir::Op::Call { target } => call_facts.call(s.value_addr[value as usize], Some(target)).writes,
        rk_ir::Op::CallInd => call_facts.call(s.value_addr[value as usize], None).writes,
        rk_ir::Op::String(name) => rk_ir::string::effect(name.0).1,
        rk_ir::Op::Slow(_) | rk_ir::Op::Div { .. } | rk_ir::Op::Idiv { .. } => Fields::ALL,
        rk_ir::Op::P(rk_core::ir::OpCode::Call)
        | rk_ir::Op::P(rk_core::ir::OpCode::CallInd)
        | rk_ir::Op::P(rk_core::ir::OpCode::CallOther)
        | rk_ir::Op::P(rk_core::ir::OpCode::Unknown) => Fields::ALL,
        _ => Fields::NONE,
    }
}
