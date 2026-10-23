use std::collections::{BTreeMap, VecDeque};

use rk_core::contract::{Field, Fields};
use rk_core::Lookup;
use rk_ir::{CcKind, Op, StateKey};

use crate::{BlockId, FlagId, FlagKind, Ssa, ValueId, DISPATCH_BLOCK};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FlagSource {
    Record(FlagId),
    Input(StateKey),
    Const(u64),
    Incoming,
    Resident,
    Phi(Vec<FlagSource>),
    Unknown,
}

#[derive(Clone, Copy, Debug)]
pub struct FlagDescriptor {
    pub kind: FlagKind,
    pub kind_value: Option<ValueId>,
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
            FlagKind::Join => self
                .kind_value
                .into_iter()
                .chain(self.a)
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
    pub forwarded: Fields,
}

pub struct FlagPlan {
    record_of: Lookup<ValueId, FlagId>,
    sources: BTreeMap<ValueId, BTreeMap<Field, FlagSource>>,
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
        let sources = self.carried_sources();
        FlagPlan { record_of, sources }
    }

    fn carried_sources(&self) -> BTreeMap<ValueId, BTreeMap<Field, FlagSource>> {
        let mut record_by_op = BTreeMap::new();
        let mut owned = BTreeMap::<ValueId, Fields>::new();
        let mut state_values = BTreeMap::<ValueId, Fields>::new();
        for (index, record) in self.flags.iter().enumerate() {
            if let Some(op) = record.op {
                record_by_op.insert(op, index as FlagId);
            }
            for (field, value) in &record.flags {
                *owned.entry(*value).or_default() |= Fields(field.bit());
                *state_values.entry(*value).or_default() |= Fields(field.bit());
            }
        }
        for boundary in &self.boundaries {
            for &(key, value) in &self.state[boundary.state_start as usize
                ..(boundary.state_start + boundary.state_len) as usize]
            {
                if let Some(field) = self.field(key) {
                    if is_flag(field) {
                        *state_values.entry(value).or_default() |= Fields(field.bit());
                    }
                }
            }
        }
        let mut successors = vec![Vec::new(); self.blocks.len()];
        for (block, item) in self.blocks.iter().enumerate() {
            for pred in &item.preds {
                if *pred != DISPATCH_BLOCK {
                    successors[*pred as usize].push(block as BlockId);
                }
            }
        }
        for list in &mut successors {
            list.sort_unstable();
            list.dedup();
        }
        let mut entry: Vec<Option<BTreeMap<Field, FlagSource>>> = vec![None; self.blocks.len()];
        let mut work = VecDeque::new();
        for (index, block) in self.blocks.iter().enumerate() {
            if block.preds.is_empty() {
                entry[index] = Some(incoming_sources());
                work.push_back(index);
            } else if block.preds.iter().any(|pred| *pred == DISPATCH_BLOCK) {
                entry[index] = Some(unknown_sources());
                work.push_back(index);
            }
        }
        while let Some(block) = work.pop_front() {
            let Some(start) = entry[block].clone() else { continue };
            let end = self.walk_sources(block, start, &record_by_op, &owned, &state_values, None);
            for &next in &successors[block] {
                let next = next as usize;
                let merged = match &entry[next] {
                    Some(current) => Some(merge_sources(current, &end)),
                    None => Some(end.clone()),
                };
                if merged != entry[next] {
                    entry[next] = merged;
                    work.push_back(next);
                }
            }
        }
        let mut sources = BTreeMap::new();
        for (block, start) in entry.into_iter().enumerate() {
            let start = start.unwrap_or_else(unknown_sources);
            self.walk_sources(block, start, &record_by_op, &owned, &state_values, Some(&mut sources));
        }
        sources
    }

    fn walk_sources(
        &self,
        block: usize,
        mut sources: BTreeMap<Field, FlagSource>,
        record_by_op: &BTreeMap<ValueId, FlagId>,
        owned: &BTreeMap<ValueId, Fields>,
        state_values: &BTreeMap<ValueId, Fields>,
        mut out: Option<&mut BTreeMap<ValueId, BTreeMap<Field, FlagSource>>>,
    ) -> BTreeMap<Field, FlagSource> {
        let item = &self.blocks[block];
        for value in item.phis.iter().chain(item.ops.iter()).copied() {
            if let Some(out) = out.as_deref_mut() {
                if self.boundary(value).is_some() {
                    out.insert(value, sources.clone());
                }
            }
            if let Some(&id) = record_by_op.get(&value) {
                for field in self.flags[id as usize].defined.iter() {
                    sources.insert(field, FlagSource::Record(id));
                }
                continue;
            }
            let op = self.value(value).op;
            if let Op::StateReload(key) = op {
                if let Some(field) = self.field(key) {
                    if is_flag(field) {
                        sources.insert(field, FlagSource::Resident);
                        continue;
                    }
                }
            }
            if matches!(op, Op::Input(_) | Op::Const(_) | Op::Phi) {
                continue;
            }
            if let Some(fields) = state_values.get(&value) {
                if !owned.contains_key(&value) {
                    for field in fields.iter() {
                        sources.insert(field, FlagSource::Unknown);
                    }
                }
            }
        }
        sources
    }
}

fn incoming_sources() -> BTreeMap<Field, FlagSource> {
    Fields::FLAGS.iter().map(|field| (field, FlagSource::Incoming)).collect()
}

fn unknown_sources() -> BTreeMap<Field, FlagSource> {
    Fields::FLAGS.iter().map(|field| (field, FlagSource::Unknown)).collect()
}

fn merge_sources(left: &BTreeMap<Field, FlagSource>, right: &BTreeMap<Field, FlagSource>) -> BTreeMap<Field, FlagSource> {
    Fields::FLAGS
        .iter()
        .map(|field| {
            let value = if left.get(&field) == right.get(&field) {
                left.get(&field).cloned().unwrap_or(FlagSource::Unknown)
            } else {
                FlagSource::Unknown
            };
            (field, value)
        })
        .collect()
}

fn flag_key_index(s: &Ssa, field: Field) -> Option<usize> {
    s.keys.iter().position(|key| s.field(*key) == Some(field))
}

fn block_entry_state(s: &Ssa, block: usize) -> Vec<Option<ValueId>> {
    let mut state = vec![None; s.keys.len()];
    for &phi in &s.blocks[block].phis {
        if let Some(&index) = s.phi_key.get(&phi) {
            state[index] = Some(phi);
        }
    }
    if state.iter().all(Option::is_some) {
        return state;
    }
    for index in 0..s.keys.len() {
        if state[index].is_some() {
            continue;
        }
        let mut value = None;
        let mut same = true;
        for pred in &s.blocks[block].preds {
            let candidate = if *pred == DISPATCH_BLOCK {
                s.dispatch.loads.get(index).map(|(_, value)| *value)
            } else {
                s.exit_state[*pred as usize].get(index).copied().flatten()
            };
            match candidate {
                Some(candidate) if value.is_some_and(|value| value != candidate) => same = false,
                Some(candidate) => value = Some(candidate),
                None => same = false,
            }
        }
        if same {
            state[index] = value;
        }
    }
    state
}

fn entry_flag_value(s: &Ssa, block: usize, field: Field, position: usize) -> Option<ValueId> {
    let index = flag_key_index(s, field)?;
    let item = &s.blocks[block];
    if let Some(&phi) = item.phis.iter().find(|phi| s.phi_key.get(*phi) == Some(&index)) {
        return s.args(phi).get(position).copied();
    }
    let pred = *item.preds.get(position)?;
    if pred == DISPATCH_BLOCK {
        return None;
    }
    s.exit_state.get(pred as usize)?.get(index).copied().flatten()
}

fn record_index(s: &Ssa) -> BTreeMap<(ValueId, Field), FlagId> {
    let mut index = BTreeMap::new();
    for (id, record) in s.flags.iter().enumerate() {
        for (&field, &value) in &record.flags {
            if record.defined.contains(field) && !matches!(s.value(value).op, Op::Const(_)) {
                index.entry((value, field)).or_insert(id as FlagId);
            }
        }
    }
    index
}

fn merge_join_sources(
    s: &mut Ssa,
    block: usize,
    preds: &[BlockId],
    index: &mut BTreeMap<(ValueId, Field), FlagId>,
) {
    let mut candidates = Fields::NONE;
    let mut ids_by_field: BTreeMap<Field, Vec<FlagId>> = BTreeMap::new();
    for field in Fields::FLAGS.iter() {
        let mut values = Vec::with_capacity(preds.len());
        let mut complete = true;
        for position in 0..preds.len() {
            let Some(value) = entry_flag_value(s, block, field, position) else {
                complete = false;
                break;
            };
            values.push(value);
        }
        if !complete || values.windows(2).all(|pair| pair[0] == pair[1]) {
            continue;
        }
        let mut ids = Vec::with_capacity(preds.len());
        let mut size = None;
        let mut compatible = true;
        for value in values {
            let Some(&id) = index.get(&(value, field)) else {
                compatible = false;
                break;
            };
            let Some(record) = s.flag(id) else {
                compatible = false;
                break;
            };
            if !record.defined.contains(field)
                || record.size == 0
                || record.size > 4
                || size.is_some_and(|size| size != record.size)
            {
                compatible = false;
                break;
            }
            size = Some(record.size);
            ids.push(id);
        }
        if compatible {
            candidates.insert(field);
            ids_by_field.insert(field, ids);
        }
    }
    if candidates.is_empty() {
        return;
    }
    let entry = block_entry_state(s, block);
    let mut groups: BTreeMap<u8, Vec<(Field, Vec<FlagId>, ValueId)>> = BTreeMap::new();
    for field in candidates.iter() {
        let Some(key) = flag_key_index(s, field) else { continue };
        let Some(Some(value)) = entry.get(key) else { continue };
        let ids = ids_by_field.remove(&field).unwrap_or_default();
        let Some(record) = ids.first().and_then(|id| s.flag(*id)) else { continue };
        groups.entry(record.size).or_default().push((field, ids, *value));
    }
    let Some((size, fields)) = groups.into_iter().max_by_key(|(_, fields)| fields.len()) else {
        return;
    };
    let mut pred_records = Vec::with_capacity(preds.len());
    for position in 0..preds.len() {
        let mut id = None;
        for (_, ids, _) in &fields {
            let current = ids[position];
            if id.is_some_and(|id| id != current) {
                return;
            }
            id = Some(current);
        }
        let Some(id) = id else { return };
        pred_records.push(id);
    }
    let records: Vec<crate::FlagRecord> = pred_records
        .iter()
        .filter_map(|id| s.flag(*id).cloned())
        .collect();
    if records.len() != preds.len() {
        return;
    }
    let addr = s.blocks[block]
        .phis
        .first()
        .map_or(0, |value| s.value_addr[*value as usize]);
    let mut kind_args = Vec::with_capacity(preds.len());
    let mut part_values: [Vec<Option<ValueId>>; 4] = std::array::from_fn(|_| Vec::with_capacity(preds.len()));
    for record in &records {
        if record.kind == FlagKind::Join {
            let Some(value) = record.kind_value else { return };
            kind_args.push(value);
        } else {
            let Some(kind) = cc_kind(record.kind) else { return };
            kind_args.push(s.new_value(Op::CcKind(kind), 1, &[], addr));
        }
        let Some(result_value) = record.result else { return };
        part_values[0].push(record.a);
        part_values[1].push(record.b);
        part_values[2].push(record.carry);
        part_values[3].push(Some(result_value));
    }
    let mut constants = BTreeMap::new();
    let mut parts = Vec::with_capacity(4);
    for values in part_values.iter() {
        let mut part_size = None;
        for value in values.iter().flatten() {
            let value_size = s.value(*value).size;
            if part_size.is_some_and(|size| size != value_size) {
                return;
            }
            part_size = Some(value_size);
        }
        let part_size = part_size.unwrap_or(size);
        let zero = s.constant(&mut constants, part_size, 0);
        let args: Vec<ValueId> = values.iter().map(|value| value.unwrap_or(zero)).collect();
        parts.push(s.new_value(Op::Phi, part_size, &args, addr));
    }
    let kind_value = s.new_value(Op::Phi, 1, &kind_args, addr);
    let [a, b, carry, result_value] = parts.as_slice() else { return };
    let (a, b, carry, result_value) = (*a, *b, *carry, *result_value);
    s.blocks[block].phis.push(kind_value);
    s.blocks[block].phis.push(a);
    s.blocks[block].phis.push(b);
    s.blocks[block].phis.push(carry);
    s.blocks[block].phis.push(result_value);
    let id = s.flags.len() as FlagId;
    let mut flags = BTreeMap::new();
    let mut defined = Fields::NONE;
    for (field, _, value) in &fields {
        flags.insert(*field, *value);
        defined.insert(*field);
    }
    s.flags.push(crate::FlagRecord {
        kind: FlagKind::Join,
        size,
        op: None,
        kind_value: Some(kind_value),
        a: Some(a),
        b: Some(b),
        carry: Some(carry),
        result: Some(result_value),
        flags,
        defined,
        guarded: true,
    });
    for (field, _, value) in &fields {
        index.insert((*value, *field), id);
    }
}

pub fn cc_kind(kind: FlagKind) -> Option<CcKind> {
    match kind {
        FlagKind::Add => Some(CcKind::Add),
        FlagKind::Sub | FlagKind::Cmp => Some(CcKind::Sub),
        FlagKind::Logic => Some(CcKind::Logic),
        FlagKind::Inc => Some(CcKind::Inc),
        FlagKind::Dec => Some(CcKind::Dec),
        FlagKind::Adc => Some(CcKind::Adc),
        FlagKind::Sbb => Some(CcKind::Sbb),
        FlagKind::Incoming | FlagKind::Join => None,
    }
}

pub(crate) fn build_joins(s: &mut Ssa) {
    let mut index = record_index(s);
    for block in 0..s.blocks.len() {
        let preds = s.blocks[block].preds.clone();
        if preds.len() < 2 || preds.iter().any(|pred| *pred == DISPATCH_BLOCK) {
            continue;
        }
        merge_join_sources(s, block, &preds, &mut index);
    }
}

impl FlagPlan {
    pub fn source(&self, s: &Ssa, field: Field, value: ValueId) -> FlagSource {
        match s.value(value).op {
            Op::Input(key) => return FlagSource::Input(key),
            Op::Const(data) => return FlagSource::Const(data),
            _ => {}
        }
        if let Some(&record) = self.record_of.get(&value) {
            if s.flag(record).is_some_and(|record| record.flags.get(&field) == Some(&value)) {
                return FlagSource::Record(record);
            }
        }
        if s.value(value).op == Op::Phi {
            FlagSource::Phi(Vec::new())
        } else {
            FlagSource::Unknown
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

    pub fn publication(
        &self,
        s: &Ssa,
        at: ValueId,
        keys: &[StateKey],
        guard_only: &[StateKey],
    ) -> FlagPublication {
        let Some(state) = s.boundary(at) else { return FlagPublication::default() };
        let wanted: Lookup<StateKey, ()> = keys.iter().map(|key| (*key, ())).collect();
        let guard_only: Lookup<StateKey, ()> = guard_only.iter().map(|key| (*key, ())).collect();
        let mut forwarded = Fields::NONE;
        let mut hot = Fields::NONE;
        let mut groups: BTreeMap<FlagId, Fields> = BTreeMap::new();
        for (key, value) in state {
            if wanted.get(key).is_none() {
                continue;
            }
            let Some(field) = s.field(*key) else { continue };
            if !is_flag(field) {
                continue;
            }
            let cold = guard_only.get(key).is_some();
            if !cold {
                hot.insert(field);
            }
            if cold && matches!(s.value(*value).op, Op::StateKeep(k) | Op::StateReload(k) if k == *key) {
                forwarded.insert(field);
                continue;
            }
            let carried = self.sources.get(&at).and_then(|sources| sources.get(&field));
            if matches!(carried, Some(FlagSource::Incoming | FlagSource::Resident))
                && matches!(s.value(*value).op, Op::Input(k) | Op::StateReload(k) if k == *key)
            {
                forwarded.insert(field);
                continue;
            }
            let FlagSource::Record(id) = self.source(s, field, *value) else { continue };
            let Some(record) = s.flag(id) else { continue };
            if record.size == 0 || record.size > 4 || !record.defined.contains(field) || !record_has_operands(record) {
                continue;
            }
            groups.entry(id).or_default().insert(field);
        }
        let score = |fields: Fields| ((fields & hot).0.count_ones(), fields.0.count_ones());
        let best = groups.into_iter().fold(None, |best: Option<(FlagId, Fields)>, (id, fields)| {
            if best.is_none_or(|(_, current)| score(fields) > score(current)) {
                Some((id, fields))
            } else {
                best
            }
        });
        let descriptor = best.and_then(|(id, mask)| {
            s.flag(id).map(|record| FlagDescriptor {
                kind: record.kind,
                kind_value: record.kind_value,
                size: record.size,
                mask,
                a: record.a,
                b: record.b,
                carry: record.carry,
                result: record.result.unwrap_or(0),
            })
        });
        FlagPublication { descriptor, forwarded }
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
        FlagKind::Join => {
            record.kind_value.is_some()
                && record.a.is_some()
                && record.b.is_some()
                && record.carry.is_some()
                && record.result.is_some()
        }
        FlagKind::Incoming => false,
    }
}
