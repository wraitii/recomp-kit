use std::collections::BTreeMap;

use rk_core::ir::Policy;
use rk_core::settings::FaultState;
use rk_core::LookupSet;
use rk_ir::{Op, StateKey};

use crate::{
    canonicalize, forward, simplify, BuildOptions, CallFacts, ConservativeCallFacts, Ssa, ValueId,
    DEFAULT_VALUE_BUDGET,
};

pub struct PrepareOptions<'a> {
    pub policy: Policy,
    pub call_facts: &'a dyn CallFacts,
    pub allow_slow: bool,
    pub value_budget: usize,
    pub stack_reload: Option<&'a dyn Fn(ValueId) -> Option<(ValueId, i32)>>,
    pub signed_sub: bool,
    pub lazy_publications: bool,
    pub fault_state: FaultState,
}

impl Default for PrepareOptions<'static> {
    fn default() -> Self {
        Self {
            policy: Policy::Optimizing,
            call_facts: &ConservativeCallFacts,
            allow_slow: true,
            value_budget: DEFAULT_VALUE_BUDGET,
            stack_reload: None,
            signed_sub: true,
            lazy_publications: true,
            fault_state: FaultState::Relaxed,
        }
    }
}

impl<'a> PrepareOptions<'a> {
    pub fn new(call_facts: &'a dyn CallFacts) -> Self {
        Self {
            policy: Policy::Optimizing,
            call_facts,
            allow_slow: true,
            value_budget: DEFAULT_VALUE_BUDGET,
            stack_reload: None,
            signed_sub: true,
            lazy_publications: true,
            fault_state: FaultState::Relaxed,
        }
    }
}

pub fn prepare(cg: &rk_ir::CgFunction, opts: &PrepareOptions) -> rk_core::Result<Ssa> {
    let build_options = BuildOptions {
        policy: opts.policy,
        call_facts: opts.call_facts,
        allow_slow: opts.allow_slow,
        value_budget: opts.value_budget,
    };
    let mut ssa = crate::build::build_with_faults(cg, &build_options, opts.fault_state)
        .map_err(rk_core::Error::Diagnostic)?;
    canonicalize(&mut ssa, opts.signed_sub);
    forward(&mut ssa, opts.stack_reload);
    crate::coalesce::coalesce(&mut ssa);
    canonicalize(&mut ssa, opts.signed_sub);
    let publications = opts.lazy_publications.then(|| {
        let mut plan = publications(&ssa, opts.call_facts);
        for boundary in &ssa.boundaries {
            if matches!(ssa.value(boundary.at).op, Op::Call { .. } | Op::CallInd) {
                plan.insert(boundary.at, ssa.keys.to_vec());
            }
        }
        plan
    });
    let flag_roots: Vec<ValueId> = ssa
        .flags
        .iter()
        .flat_map(|record| match record.kind {
            crate::FlagKind::Logic => vec![record.result],
            crate::FlagKind::Inc | crate::FlagKind::Dec => vec![record.a, record.result],
            crate::FlagKind::Adc | crate::FlagKind::Sbb => {
                vec![record.a, record.b, record.carry, record.result]
            }
            _ => vec![record.a, record.b, record.result],
        })
        .flatten()
        .collect();
    simplify(&mut ssa, publications.as_ref(), &flag_roots, &[]);
    Ok(ssa)
}

pub fn publications(s: &Ssa, call_facts: &dyn CallFacts) -> BTreeMap<ValueId, Vec<StateKey>> {
    let all: Vec<StateKey> = s.keys.to_vec();
    let mut plan = BTreeMap::new();
    for boundary in &s.boundaries {
        let value = &s.values[boundary.at as usize];
        let fields = match value.op {
            Op::Call { target } => Some(call_facts.call(s.value_addr[boundary.at as usize], Some(target)).reads),
            Op::CallInd => Some(call_facts.call(s.value_addr[boundary.at as usize], None).reads),
            _ => None,
        };
        let keys: Vec<StateKey> = match fields {
            Some(fields) => {
                let wanted: LookupSet<rk_core::contract::Field> = fields.iter().collect();
                s.keys
                    .iter()
                    .enumerate()
                    .filter(|(index, key)| {
                        **key == StateKey::Memory
                            || s.field_of[*index].is_some_and(|field| wanted.contains(&field))
                    })
                    .map(|(_, key)| *key)
                    .collect()
            }
            None => all.clone(),
        };
        plan.insert(boundary.at, keys);
    }
    plan
}
