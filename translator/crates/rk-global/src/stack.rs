use std::collections::{BTreeMap, BTreeSet, VecDeque};

use rk_core::contract::{Field, Fields};
use rk_core::ir::{FunctionIr, Insn, OpCode, PcodeOp, Space, Varnode};
use rk_core::program::Program;
use rk_core::Addr;
use rk_ir::Registers;
use rk_ssa::StackEffect;

type Checked<T> = std::result::Result<T, &'static str>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Value {
    Stack(u32),
    Constant(u64),
    Input(Varnode),
    ReturnAddress,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct State {
    registers: BTreeMap<Varnode, Value>,
    slots: BTreeMap<(u32, u32), Value>,
}

#[derive(Clone, Debug)]
pub struct Summary {
    pub effect: StackEffect,
    pub reads: BTreeSet<(i32, u32)>,
    pub reads_other: bool,
    pub preserves: Fields,
    pub mapped_noreturn: bool,
}

#[derive(Clone, Debug, Default)]
pub struct Analysis {
    pub functions: BTreeMap<Addr, Summary>,
    pub sites: BTreeMap<Addr, Summary>,
    pub rejected: BTreeMap<String, usize>,
    pub rejected_sites: BTreeMap<String, usize>,
}

pub struct CallFacts<'a> {
    pub registers: crate::ContractCallFacts<'a>,
    pub stack: &'a Analysis,
}

impl rk_ssa::CallFacts for CallFacts<'_> {
    fn call(&self, site: Addr, target: Option<Addr>) -> rk_ssa::CallEffect {
        let mut effect = self.registers.call(site, target);
        if let Some(summary) = self.summary(site, target) {
            effect.preserves |= summary.preserves;
            effect.writes = effect.writes & !summary.preserves;
            if summary.mapped_noreturn {
                effect.reads = Fields::ALL;
                effect.kills = Fields::NONE;
            }
        }
        effect
    }

    fn stack(&self, site: Addr, target: Option<Addr>) -> Option<&StackEffect> {
        self.summary(site, target).map(|summary| &summary.effect)
    }
}

impl CallFacts<'_> {
    fn summary(&self, site: Addr, target: Option<Addr>) -> Option<&Summary> {
        match target {
            Some(target) => self.stack.functions.get(&target),
            None => self.stack.sites.get(&site),
        }
    }
}

fn overlap(a: u32, width_a: u32, b: u32, width_b: u32) -> bool {
    a.wrapping_sub(b) < width_b || b.wrapping_sub(a) < width_a
}

fn register_overlap(a: Varnode, b: Varnode) -> bool {
    a.space == b.space && a.offset < b.offset + b.size as u64 && b.offset < a.offset + a.size as u64
}

fn fields() -> impl Iterator<Item = (Field, Varnode)> {
    Field::ALL.into_iter().filter_map(|field| {
        Registers::get()
            .register(field.name())
            .map(|register| (field, register))
    })
}

impl State {
    fn entry() -> Self {
        let mut state = Self::default();
        for (field, register) in fields() {
            state.registers.insert(
                register,
                if field == Field::Esp {
                    Value::Stack(0)
                } else {
                    Value::Input(register)
                },
            );
        }
        state.slots.insert((0, 4), Value::ReturnAddress);
        state
    }

    fn read(&self, input: Varnode, locals: &BTreeMap<Varnode, Value>) -> Option<Value> {
        match input.space {
            Space::Const => Some(Value::Constant(input.offset & mask(input.size))),
            Space::Register => self.registers.get(&input).copied(),
            Space::Unique => locals.get(&input).copied(),
            _ => None,
        }
    }

    fn write(&mut self, output: Varnode, value: Option<Value>) -> Checked<()> {
        if output.space != Space::Register {
            return Err("non-register output");
        }
        if ["CS", "DS", "ES", "SS", "FS", "GS", "FS_OFFSET"]
            .iter()
            .filter_map(|name| Registers::get().register(name))
            .any(|segment| register_overlap(output, segment))
        {
            return Err("segment base write");
        }
        self.registers
            .retain(|&register, _| !register_overlap(output, register));
        if let Some(value) = value {
            self.registers.insert(output, value);
        }
        Ok(())
    }

    fn merge(&mut self, other: &Self) -> bool {
        let old = (self.registers.len(), self.slots.len());
        self.registers
            .retain(|key, value| other.registers.get(key) == Some(value));
        self.slots
            .retain(|key, value| other.slots.get(key) == Some(value));
        old != (self.registers.len(), self.slots.len())
    }

    fn invalidate(&mut self, offset: u32, size: u32) -> Checked<()> {
        if size == 0 || size > 32 {
            return Err("unsupported memory width");
        }
        if overlap(offset, size, 0, 4) {
            return Err("return-address write");
        }
        self.slots
            .retain(|&(at, width), _| !overlap(offset, size, at, width));
        Ok(())
    }

    fn returning(&self) -> Checked<Fields> {
        let esp = Registers::get().require("ESP");
        if self.registers.get(&esp) != Some(&Value::Stack(0)) {
            return Err("unproved ESP balance");
        }
        if self.slots.get(&(0, 4)) != Some(&Value::ReturnAddress) {
            return Err("unproved return address");
        }
        Ok(fields()
            .filter_map(|(field, register)| {
                (self.registers.get(&register) == Some(&Value::Input(register))).then_some(field)
            })
            .collect())
    }
}

fn mask(size: u32) -> u64 {
    if size >= 8 {
        u64::MAX
    } else {
        (1u64 << (size * 8)) - 1
    }
}

fn evaluate(op: &PcodeOp, state: &State, locals: &BTreeMap<Varnode, Value>) -> Option<Value> {
    let output = op.output?;
    let a = state.read(*op.inputs.first()?, locals)?;
    if op.opcode == OpCode::Copy && op.inputs[0].size == output.size {
        return Some(a);
    }
    match op.opcode {
        OpCode::IntZExt | OpCode::IntSExt => {
            let Value::Constant(mut data) = a else {
                return None;
            };
            if op.opcode == OpCode::IntSExt {
                let bits = op.inputs[0].size * 8;
                if bits == 0 || bits > 64 {
                    return None;
                }
                let sign = 1u64 << (bits - 1);
                data = (data ^ sign).wrapping_sub(sign);
            }
            Some(Value::Constant(data & mask(output.size)))
        }
        OpCode::IntAdd | OpCode::IntSub if output.size == 4 => {
            let b = state.read(*op.inputs.get(1)?, locals)?;
            let subtract = op.opcode == OpCode::IntSub;
            match (a, b) {
                (Value::Stack(offset), Value::Constant(delta)) => Some(Value::Stack(if subtract {
                    offset.wrapping_sub(delta as u32)
                } else {
                    offset.wrapping_add(delta as u32)
                })),
                (Value::Constant(delta), Value::Stack(offset)) if !subtract => {
                    Some(Value::Stack(offset.wrapping_add(delta as u32)))
                }
                (Value::Constant(a), Value::Constant(b)) => Some(Value::Constant(
                    (if subtract {
                        a.wrapping_sub(b)
                    } else {
                        a.wrapping_add(b)
                    }) & mask(output.size),
                )),
                _ => None,
            }
        }
        _ => None,
    }
}

fn target(insn: &Insn) -> Option<Addr> {
    insn.ops.iter().find_map(|op| {
        if !matches!(op.opcode, OpCode::Call | OpCode::Branch) {
            return None;
        }
        let input = op.inputs.first()?;
        matches!(input.space, Space::Ram | Space::Const).then_some(input.offset as Addr)
    })
}

fn compose(
    state: &mut State,
    summary: &Summary,
    offset: u32,
    reads: &mut BTreeSet<(i32, u32)>,
    writes: &mut BTreeSet<(i32, u32)>,
) -> Checked<()> {
    for &(at, width) in &summary.effect.writes {
        let at = offset.wrapping_add(at as u32);
        state.invalidate(at, width)?;
        writes.insert((at as i32, width));
    }
    reads.extend(
        summary
            .reads
            .iter()
            .map(|&(at, width)| (offset.wrapping_add(at as u32) as i32, width)),
    );
    for (field, register) in fields() {
        if !summary.preserves.contains(field) {
            state.write(register, None)?;
        }
    }
    Ok(())
}

fn summarize(
    program: &Program,
    fir: &FunctionIr,
    known: &BTreeMap<Addr, Summary>,
    excluded: &BTreeSet<Addr>,
) -> Checked<Summary> {
    if !fir.entries.is_empty()
        || !fir.dynamic.is_empty()
        || !fir.seh.is_empty()
        || !fir.seh_handlers.is_empty()
        || !fir.seh_unwind_only.is_empty()
        || !fir.import_tails.is_empty()
    {
        return Err("unmodeled entry or exception flow");
    }
    let entry = fir.index_of(fir.addr).ok_or("missing entry")?;
    let mut before = BTreeMap::from([(entry, State::entry())]);
    let mut pending = VecDeque::from([entry]);
    let mut reads = BTreeSet::new();
    let mut writes = BTreeSet::new();
    let mut cleanups = BTreeSet::new();
    let mut preserves = Fields::ALL;
    let mut reads_other = false;
    let mut mapped_noreturn = false;
    let mut steps = 0;
    while let Some(index) = pending.pop_front() {
        steps += 1;
        if steps > 10000 || reads.len() + writes.len() > 512 {
            return Err("summary budget");
        }
        let insn = fir.insns.get(index as usize).ok_or("invalid CFG index")?;
        let successors = fir.succ.get(index as usize).ok_or("missing successors")?;
        if insn.internal_flow || insn.x87 || !insn.userops.is_empty() {
            return Err("unmodeled instruction effects");
        }
        if fir.noreturn.contains_key(&index) {
            if target(insn).is_some_and(|target| excluded.contains(&target)) {
                return Err("opaque noreturn callee");
            }
            if !successors.is_empty() {
                return Err("noreturn call has successors");
            }
            mapped_noreturn = true;
            continue;
        }
        let mut state = before[&index].clone();
        if insn.mnemonic.eq_ignore_ascii_case("RET") {
            if !successors.is_empty()
                || fir
                    .exits
                    .get(&index)
                    .is_some_and(|targets| !targets.is_empty())
            {
                return Err("return has successors");
            }
            preserves = preserves & state.returning()?;
            cleanups.insert(return_pop(program, insn).ok_or("unsupported return encoding")?);
            reads.insert((0, 4));
            continue;
        }
        let tail = fir.exits.get(&index).filter(|targets| !targets.is_empty());
        let mut locals = BTreeMap::new();
        for op in &insn.ops {
            let mut value = evaluate(op, &state, &locals);
            if op.inputs.iter().any(|input| input.space == Space::Ram) && !op.opcode.is_branch() {
                reads_other = true;
            }
            match op.opcode {
                OpCode::Store => {
                    if op.inputs.len() != 2 {
                        return Err("unsupported store");
                    }
                    let Some(Value::Stack(offset)) = state.read(op.inputs[0], &locals) else {
                        return Err("potentially aliased non-stack write");
                    };
                    let size = op.inputs[1].size;
                    let stored = state.read(op.inputs[1], &locals);
                    state.invalidate(offset, size)?;
                    writes.insert((offset as i32, size));
                    if let Some(stored) = stored {
                        state.slots.insert((offset, size), stored);
                    }
                    if state.slots.len() > 128 {
                        return Err("stack slot budget");
                    }
                }
                OpCode::Load => {
                    let output = op.output.ok_or("load has no output")?;
                    let address =
                        state.read(*op.inputs.first().ok_or("load has no address")?, &locals);
                    if let Some(Value::Stack(offset)) = address {
                        reads.insert((offset as i32, output.size));
                        value = state.slots.get(&(offset, output.size)).copied();
                    } else {
                        reads_other = true;
                        value = None;
                    }
                }
                OpCode::Call => {
                    let callee = known
                        .get(&target(insn).ok_or("unknown direct call")?)
                        .ok_or("unresolved direct call")?;
                    let esp = Registers::get().require("ESP");
                    let Some(Value::Stack(offset)) = state.registers.get(&esp).copied() else {
                        return Err("unknown call frame");
                    };
                    compose(&mut state, callee, offset, &mut reads, &mut writes)?;
                    state.write(
                        esp,
                        Some(Value::Stack(
                            offset.wrapping_add(4 + callee.effect.cleanup as u32),
                        )),
                    )?;
                    reads_other |= callee.reads_other;
                    mapped_noreturn |= callee.mapped_noreturn;
                }
                OpCode::CallInd
                | OpCode::CallOther
                | OpCode::Unknown
                | OpCode::BranchInd
                | OpCode::Return => {
                    return Err("unmodeled control or user operation");
                }
                _ => {}
            }
            if let Some(output) = op.output {
                match output.space {
                    Space::Register => state.write(output, value)?,
                    Space::Unique => {
                        locals.retain(|&key, _| !register_overlap(key, output));
                        if let Some(value) = value {
                            locals.insert(output, value);
                        }
                    }
                    _ => return Err("unmodeled output space"),
                }
            }
        }
        if let Some(targets) = tail {
            if !insn.mnemonic.eq_ignore_ascii_case("JMP")
                || !successors.is_empty()
                || targets.len() != 1
                || target(insn) != Some(targets[0])
            {
                return Err("unmodeled tail exit");
            }
            state.returning()?;
            if program.is_noreturn(targets[0]) {
                if excluded.contains(&targets[0]) {
                    return Err("opaque noreturn callee");
                }
                mapped_noreturn = true;
                continue;
            }
            let callee = known.get(&targets[0]).ok_or("unresolved tail")?;
            compose(&mut state, callee, 0, &mut reads, &mut writes)?;
            state.write(Registers::get().require("ESP"), Some(Value::Stack(0)))?;
            preserves = preserves & state.returning()?;
            cleanups.insert(callee.effect.cleanup);
            reads_other |= callee.reads_other;
            mapped_noreturn |= callee.mapped_noreturn;
            continue;
        }
        if successors.is_empty() {
            return Err("unmodeled terminal instruction");
        }
        for &next in successors {
            if next as usize >= fir.insns.len() {
                return Err("external CFG edge");
            }
            if let Some(previous) = before.get_mut(&next) {
                if previous.merge(&state) {
                    pending.push_back(next);
                }
            } else {
                before.insert(next, state.clone());
                pending.push_back(next);
            }
        }
    }
    if cleanups.len() != 1 {
        return Err("missing or inconsistent normal returns");
    }
    Ok(Summary {
        effect: StackEffect {
            cleanup: *cleanups.first().unwrap(),
            writes: writes.into_iter().collect(),
        },
        reads,
        reads_other,
        preserves,
        mapped_noreturn,
    })
}

pub fn return_pop(program: &Program, insn: &Insn) -> Option<i32> {
    let raw = program.image.bytes(insn.addr, insn.length as usize)?;
    let raw = raw.strip_prefix(&[0xf3]).unwrap_or(raw);
    match raw {
        [0xc3] => Some(0),
        [0xc2, low, high] => Some(i32::from(u16::from_le_bytes([*low, *high]))),
        _ => None,
    }
}

pub fn analyze(
    program: &Program,
    analysis: &rk_frontend::Analysis,
    excluded: &BTreeSet<Addr>,
    indirect: &BTreeMap<Addr, Vec<Addr>>,
) -> Analysis {
    let mut output = Analysis::default();
    let mut callers: BTreeMap<Addr, BTreeSet<Addr>> = BTreeMap::new();
    for (&addr, result) in &analysis.ir {
        if let Ok(fir) = result {
            for insn in &fir.insns {
                if let Some(target) = target(insn) {
                    callers.entry(target).or_default().insert(addr);
                }
            }
        }
    }
    let mut pending: BTreeSet<Addr> = analysis.ir.keys().copied().collect();
    let mut failures = BTreeMap::new();
    while let Some(addr) = pending.pop_first() {
        if output.functions.contains_key(&addr) {
            continue;
        }
        let result = if excluded.contains(&addr) {
            Err("opaque or native function")
        } else {
            analysis.ir[&addr]
                .as_ref()
                .map_err(|_| "unlifted function")
                .and_then(|fir| summarize(program, fir, &output.functions, excluded))
        };
        match result {
            Ok(summary) => {
                output.functions.insert(addr, summary);
                failures.remove(&addr);
                if let Some(callers) = callers.get(&addr) {
                    pending.extend(callers);
                }
            }
            Err(reason) => {
                failures.insert(addr, reason);
            }
        }
    }
    for reason in failures.values() {
        *output.rejected.entry((*reason).into()).or_default() += 1;
    }
    for (&site, targets) in indirect {
        let result = join(program, excluded, targets, &output.functions);
        match result {
            Ok(summary) => {
                output.sites.insert(site, summary);
            }
            Err(reason) => {
                *output.rejected_sites.entry(reason.into()).or_default() += 1;
            }
        }
    }
    output
}

fn join(
    program: &Program,
    excluded: &BTreeSet<Addr>,
    targets: &[Addr],
    known: &BTreeMap<Addr, Summary>,
) -> Checked<Summary> {
    let mut result: Option<Summary> = None;
    let mut mapped_noreturn = false;
    for target in targets {
        if excluded.contains(target) {
            return Err("opaque or native target");
        }
        if program.is_noreturn(*target) {
            mapped_noreturn = true;
            continue;
        }
        let next = known.get(target).ok_or("unknown returning target")?;
        if let Some(result) = &mut result {
            if result.effect.cleanup != next.effect.cleanup {
                return Err("inconsistent target cleanup");
            }
            result.effect.writes.extend_from_slice(&next.effect.writes);
            result.reads.extend(&next.reads);
            result.reads_other |= next.reads_other;
            result.preserves = result.preserves & next.preserves;
            result.mapped_noreturn |= next.mapped_noreturn;
        } else {
            result = Some(next.clone());
        }
    }
    let mut result = result.ok_or("no returning target")?;
    result.mapped_noreturn |= mapped_noreturn;
    result.effect.writes.sort_unstable();
    result.effect.writes.dedup();
    Ok(result)
}
