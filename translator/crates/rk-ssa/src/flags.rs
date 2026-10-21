use std::collections::BTreeMap;

use rk_core::contract::{Field, Fields};
use rk_core::Lookup;
use rk_ir::{Op, StateKey};

use crate::{FlagId, FlagKind, Ssa, ValueId};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FlagSource {
    Record(FlagId),
    Input(StateKey),
    Const(u64),
    Phi(Vec<FlagSource>),
    Unknown,
}

#[derive(Clone, Copy, Debug)]
pub struct FlagDescriptor {
    pub kind: FlagKind,
    pub size: u8,
    pub mask: Fields,
    pub a: Option<ValueId>,
    pub b: Option<ValueId>,
    pub carry: Option<ValueId>,
    pub result: ValueId,
}

impl FlagDescriptor {
    pub fn values(&self) -> Vec<ValueId> {
        match self.kind {
            FlagKind::Logic => vec![self.result],
            FlagKind::Inc | FlagKind::Dec => self.a.into_iter().chain([self.result]).collect(),
            FlagKind::Adc | FlagKind::Sbb => self
                .a
                .into_iter()
                .chain(self.b)
                .chain(self.carry)
                .chain([self.result])
                .collect(),
            _ => self
                .a
                .into_iter()
                .chain(self.b)
                .chain([self.result])
                .collect(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct FlagPublication {
    pub descriptor: Option<FlagDescriptor>,
    pub eager: Fields,
}

pub struct FlagPlan {
    record_of: Lookup<ValueId, FlagId>,
}

fn is_flag(field: Field) -> bool {
    matches!(field, Field::Cf | Field::Pf | Field::Af | Field::Zf | Field::Sf | Field::Of)
}

impl Ssa {
    pub fn flag_plan(&self) -> FlagPlan {
        let mut record_of = Lookup::default();
        for (index, record) in self.flags.iter().enumerate() {
            for (field, value) in &record.flags {
                if record.defined.contains(*field) {
                    record_of.insert(*value, index as FlagId);
                }
            }
        }
        FlagPlan { record_of }
    }
}

impl FlagPlan {
    pub fn source(&self, s: &Ssa, field: Field, value: ValueId) -> FlagSource {
        let value = s.resolve(value);
        match s.value(value).op {
            Op::Input(key) => FlagSource::Input(key),
            Op::Const(data) => FlagSource::Const(data),
            Op::Phi => FlagSource::Phi(Vec::new()),
            _ => {
                if let Some(&record) = self.record_of.get(&value) {
                    if s.flag(record).is_some_and(|record| record.flags.get(&field) == Some(&value)) {
                        return FlagSource::Record(record);
                    }
                }
                FlagSource::Unknown
            }
        }
    }

    pub fn values(&self, s: &Ssa, at: ValueId) -> BTreeMap<Field, ValueId> {
        let mut out = BTreeMap::new();
        let Some(state) = s.boundary(at) else { return out };
        for (key, value) in state {
            if let Some(field) = s.field(*key) {
                if is_flag(field) {
                    out.insert(field, *value);
                }
            }
        }
        out
    }

    pub fn boundary(&self, s: &Ssa, at: ValueId) -> BTreeMap<Field, FlagSource> {
        self.values(s, at)
            .into_iter()
            .map(|(field, value)| (field, self.source(s, field, value)))
            .collect()
    }

    pub fn publication(&self, s: &Ssa, at: ValueId, keys: &[StateKey]) -> FlagPublication {
        let Some(state) = s.boundary(at) else { return FlagPublication::default() };
        let wanted: Lookup<StateKey, ()> = keys.iter().map(|key| (*key, ())).collect();
        let mut published = Fields::NONE;
        let mut groups: BTreeMap<FlagId, Fields> = BTreeMap::new();
        for (key, value) in state {
            if wanted.get(key).is_none() {
                continue;
            }
            let Some(field) = s.field(*key) else { continue };
            if !is_flag(field) {
                continue;
            }
            published.insert(field);
            let FlagSource::Record(id) = self.source(s, field, *value) else { continue };
            let Some(record) = s.flag(id) else { continue };
            if record.size == 0 || record.size > 4 || !record.defined.contains(field) {
                continue;
            }
            if !record_has_operands(record) {
                continue;
            }
            groups.entry(id).or_default().insert(field);
        }
        let mut best: Option<(FlagId, Fields)> = None;
        for (id, fields) in groups {
            if best.is_none_or(|(_, current)| fields.0.count_ones() > current.0.count_ones()) {
                best = Some((id, fields));
            }
        }
        let Some((id, mask)) = best else {
            return FlagPublication { descriptor: None, eager: published };
        };
        let Some(record) = s.flag(id) else {
            return FlagPublication { descriptor: None, eager: published };
        };
        FlagPublication {
            descriptor: Some(FlagDescriptor {
                kind: record.kind,
                size: record.size,
                mask,
                a: record.a,
                b: record.b,
                carry: record.carry,
                result: record.result.unwrap_or(0),
            }),
            eager: published & !mask,
        }
    }
}

fn record_has_operands(record: &crate::FlagRecord) -> bool {
    match record.kind {
        FlagKind::Logic => record.result.is_some(),
        FlagKind::Inc | FlagKind::Dec => record.a.is_some() && record.result.is_some(),
        FlagKind::Adc | FlagKind::Sbb => {
            record.a.is_some() && record.b.is_some() && record.carry.is_some() && record.result.is_some()
        }
        FlagKind::Add | FlagKind::Sub | FlagKind::Cmp => {
            record.a.is_some() && record.b.is_some() && record.result.is_some()
        }
        FlagKind::Incoming | FlagKind::Join => false,
    }
}
