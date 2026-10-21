use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use rayon::prelude::*;
use rk_core::contract::{Contract, Field, Fields};
use rk_core::ir::Policy;
use rk_core::ir::{FunctionIr, OpCode, Space, Varnode};
use rk_core::program::Program;
use rk_core::{Addr, Diagnostic, Error, Reason, Result};
use rk_frontend::Analysis;
use rk_ir::x87::{MemoryEffect, X87Environment, X87Eager, X87Legacy, X87Op};
use rk_ir::{CgFunction, Op, Registers, StateKey};
use rk_x87::Recipes;

use crate::callgraph::CallGraph;
use crate::cfg::{closed, is_return, mnemonic};
use rk_ssa::{BuildOptions, CallEffect, CallFacts};

const GPRS: [Field; 8] = [
    Field::Eax,
    Field::Ecx,
    Field::Edx,
    Field::Ebx,
    Field::Esp,
    Field::Ebp,
    Field::Esi,
    Field::Edi,
];
const FLAGS: [Field; 6] = [
    Field::Cf,
    Field::Pf,
    Field::Af,
    Field::Zf,
    Field::Sf,
    Field::Of,
];
const ALL: u64 = (1u64 << 38) - 1;
const GPR_LANES: u64 = (1u64 << 32) - 1;
const FLAG_LANES: u64 = ALL & !GPR_LANES;
const MAX_INSTRUCTIONS: usize = 16384;
const MAX_ROUNDS: usize = 4096;
const PRESERVATION: bool = true;
const AF_WRITERS: &[&str] = &[
    "ADD", "ADC", "SUB", "SBB", "CMP", "INC", "DEC", "AND", "OR", "XOR", "TEST",
];
const FLAG_FREE: &[&str] = &[
    "MOV", "MOVSX", "MOVZX", "XCHG", "NOT", "LEAVE", "LEA", "PUSH", "POP", "RET", "NOP", "CALL",
    "JMP", "CLD", "STD", "CDQ", "CWDE", "CBW", "WAIT",
];

pub(crate) struct ContractAnalysis {
    pub contracts: BTreeMap<Addr, Contract>,
    pub indirect: BTreeMap<Addr, Contract>,
    pub rejected: BTreeMap<Addr, Reason>,
    pub instruction_checks: usize,
    pub instruction_mismatches: BTreeMap<Addr, (usize, usize)>,
}

#[derive(Clone, Debug)]
struct CallSite {
    index: u32,
    site: Addr,
    targets: Vec<Addr>,
    indirect: bool,
}

#[derive(Clone, Debug)]
struct Prepared {
    entry: u32,
    order: Vec<u32>,
    succ: Vec<Vec<u32>>,
    preds: Vec<Vec<u32>>,
    use_: Vec<u64>,
    defs: Vec<u64>,
    writes: u64,
    calls: Vec<CallSite>,
    returns: Vec<u32>,
    cg: Option<CgFunction>,
    saved: Fields,
    visit_mismatch: Option<(usize, usize)>,
}

struct LaneMap {
    lanes: BTreeMap<u32, u64>,
}

impl LaneMap {
    fn get() -> &'static LaneMap {
        static MAP: OnceLock<LaneMap> = OnceLock::new();
        MAP.get_or_init(|| {
            let regs = Registers::get();
            let mut lanes = BTreeMap::new();
            for (position, field) in GPRS.iter().enumerate() {
                let Some(register) = regs.register(field.name()) else {
                    continue;
                };
                if register.space != Space::Register {
                    continue;
                }
                for lane in 0..register.size.min(4) {
                    lanes.insert(
                        (register.offset + lane as u64) as u32,
                        1u64 << (position * 4 + lane as usize),
                    );
                }
            }
            for (position, field) in FLAGS.iter().enumerate() {
                let Some(register) = regs.register(field.name()) else {
                    continue;
                };
                if register.space != Space::Register {
                    continue;
                }
                lanes.insert(register.offset as u32, 1u64 << (32 + position));
            }
            LaneMap { lanes }
        })
    }

    fn mask(&self, varnode: &Varnode) -> u64 {
        if varnode.space != Space::Register {
            return 0;
        }
        let mut mask = 0;
        for n in 0..varnode.size as u64 {
            if let Some(bit) = self.lanes.get(&((varnode.offset + n) as u32)) {
                mask |= bit;
            }
        }
        mask
    }
}

fn field_mask(field: Field) -> u64 {
    let position = Field::ALL
        .iter()
        .position(|candidate| *candidate == field)
        .unwrap();
    if position < 8 {
        (0xf_u64) << (position * 4)
    } else {
        1u64 << (32 + position - 8)
    }
}

fn fields_mask(fields: Fields) -> u64 {
    fields
        .iter()
        .fold(0, |mask, field| mask | field_mask(field))
}

fn project(mask: u64, field: Field) -> bool {
    mask & field_mask(field) == field_mask(field)
}

fn any(mask: u64, field: Field) -> bool {
    mask & field_mask(field) != 0
}

pub(crate) fn analyze(
    program: &Program,
    analysis: &Analysis,
    indirect: &BTreeMap<Addr, Vec<Addr>>,
    native: &BTreeSet<Addr>,
) -> Result<ContractAnalysis> {
    let lanes = LaneMap::get();
    let stack_rows: Vec<(Addr, Option<i32>, bool, Option<Addr>)> = analysis
        .ir
        .par_iter()
        .map(|(&addr, result)| {
            result.as_ref().ok().map_or((addr, None, false, None), |fir| {
                let info = stack_info(program, fir);
                let tail = fir.exits.iter().any(|(&index, _)| {
                    fir.insns[index as usize].mnemonic.eq_ignore_ascii_case("JMP")
                        && fir.succ[index as usize].is_empty()
                });
                (addr, info.1, tail, tail_target(fir))
            })
        })
        .collect();
    let mut raw_pops = BTreeMap::new();
    let mut tail_targets = BTreeMap::new();
    let mut has_tails = BTreeMap::new();
    for (addr, pop, tail, target) in stack_rows {
        raw_pops.insert(addr, pop);
        tail_targets.insert(addr, target);
        has_tails.insert(addr, tail);
    }
    let pops = resolve_pops(&raw_pops, &tail_targets);
    let prepared: Vec<(Addr, Option<Prepared>, Option<Reason>)> = analysis
        .ir
        .par_iter()
        .map(|(&addr, result)| {
            let fir = match result {
                Ok(fir) => fir,
                Err(error) => return (addr, None, Some(conservative_reason(error))),
            };
            if native.contains(&addr) {
                return (
                    addr,
                    None,
                    Some(Reason::Conservative {
                        detail: "native replacement".into(),
                    }),
                );
            }
            match prepare(
                program,
                fir,
                indirect,
                lanes,
                has_tails.get(&addr).copied().unwrap_or(false),
            ) {
                Ok(prepared) => (addr, Some(prepared), None),
                Err(reason) => (addr, None, Some(reason)),
            }
        })
        .collect();
    let mut prepared_map = BTreeMap::new();
    let mut rejected = BTreeMap::new();
    for (addr, prepared, reason) in prepared {
        if let Some(prepared) = prepared {
            prepared_map.insert(addr, prepared);
        } else {
            rejected.insert(
                addr,
                reason.unwrap_or(Reason::Conservative {
                    detail: "body excluded".into(),
                }),
            );
        }
    }
    let graph = CallGraph::build(analysis, indirect, |addr| prepared_map.contains_key(&addr));
    let mut contracts: BTreeMap<Addr, Contract> = BTreeMap::new();
    for level in &graph.levels {
        let mut components: Vec<&Vec<u32>> = level
            .iter()
            .map(|&component| &graph.components[component as usize])
            .collect();
        components.sort_by_key(|members| members.first().copied().unwrap_or(u32::MAX));
        let results: Vec<Result<BTreeMap<Addr, Contract>>> = components
            .par_iter()
            .map(|members| {
                let recursive = members.len() > 1
                    || members
                        .iter()
                        .any(|&node| graph.succ[node as usize].contains(&node));
                analyze_component(
                    members,
                    &graph,
                    &prepared_map,
                    &contracts,
                    indirect,
                    &pops,
                    recursive,
                )
            })
            .collect();
        for result in results {
            contracts.extend(result?);
        }
    }
    let mut indirect_contracts = BTreeMap::new();
    for prep in prepared_map.values() {
        for call in &prep.calls {
            if !call.indirect || call.targets.is_empty() {
                continue;
            }
            let resolved: Option<Vec<Contract>> = call
                .targets
                .iter()
                .map(|target| contracts.get(target).copied())
                .collect();
            if let Some(resolved) = resolved {
                indirect_contracts.insert(call.site, join_contracts(&resolved));
            }
        }
    }
    let mut instruction_mismatches = BTreeMap::new();
    for (&addr, prepared) in &prepared_map {
        if let Some(mismatch) = prepared.visit_mismatch {
            instruction_mismatches.insert(addr, mismatch);
        }
    }
    Ok(ContractAnalysis {
        contracts,
        indirect: indirect_contracts,
        rejected,
        instruction_checks: prepared_map.len(),
        instruction_mismatches,
    })
}

fn analyze_component(
    members: &[u32],
    graph: &CallGraph,
    prepared: &BTreeMap<Addr, Prepared>,
    contracts: &BTreeMap<Addr, Contract>,
    indirect: &BTreeMap<Addr, Vec<Addr>>,
    pop_facts: &BTreeMap<Addr, i32>,
    recursive: bool,
) -> Result<BTreeMap<Addr, Contract>> {
    let addresses: Vec<Addr> = members
        .iter()
        .map(|&node| graph.nodes[node as usize])
        .collect();
    let excluded: BTreeSet<Addr> = if recursive {
        addresses.iter().copied().collect()
    } else {
        BTreeSet::new()
    };
    let mut preserves = BTreeMap::new();
    for &addr in &addresses {
        let prep = &prepared[&addr];
        let preserved = match &prep.cg {
            Some(cg) => {
                let facts = PreservationCallFacts {
                    contracts,
                    indirect,
                    excluded: &excluded,
                };
                preserved_registers(cg, pop_facts, prep.saved, &facts)
            }
            None => Fields::NONE,
        };
        preserves.insert(addr, preserved);
    }
    solve_component(
        &addresses,
        prepared,
        contracts,
        indirect,
        recursive,
        &preserves,
    )
}

fn solve_component(
    addresses: &[Addr],
    prepared: &BTreeMap<Addr, Prepared>,
    contracts: &BTreeMap<Addr, Contract>,
    indirect: &BTreeMap<Addr, Vec<Addr>>,
    recursive: bool,
    preserves: &BTreeMap<Addr, Fields>,
) -> Result<BTreeMap<Addr, Contract>> {
    let lookup = |live: &BTreeMap<Addr, Contract>, target: Addr| {
        live.get(&target)
            .copied()
            .or_else(|| contracts.get(&target).copied())
    };
    let preserved = |addr: Addr| preserves.get(&addr).copied().unwrap_or(Fields::NONE);
    if !recursive {
        let mut out = BTreeMap::new();
        for &addr in addresses {
            let prep = &prepared[&addr];
            out.insert(
                addr,
                summarize(
                    prep,
                    |target| lookup(&BTreeMap::new(), target),
                    indirect,
                    None,
                    preserved(addr),
                ),
            );
        }
        return Ok(out);
    }
    let mut live: BTreeMap<Addr, Contract> = addresses
        .iter()
        .map(|&addr| {
            (
                addr,
                Contract {
                    reads: Fields::NONE,
                    kills: Fields::NONE,
                    writes: Fields::NONE,
                    preserves: Fields::NONE,
                },
            )
        })
        .collect();
    for _ in 0..MAX_ROUNDS {
        let mut changed = false;
        for &addr in addresses {
            let prep = &prepared[&addr];
            let next = summarize(
                prep,
                |target| lookup(&live, target),
                indirect,
                None,
                preserved(addr),
            );
            if live.get(&addr) != Some(&next) {
                live.insert(addr, next);
                changed = true;
            }
        }
        if !changed {
            return Ok(live);
        }
    }
    let component = addresses.first().copied().unwrap_or(0);
    Err(Error::Diagnostic(Diagnostic::new(
        component,
        Reason::ContractNonConvergence { component },
    )))
}

fn summarize(
    prep: &Prepared,
    lookup: impl Fn(Addr) -> Option<Contract>,
    _indirect: &BTreeMap<Addr, Vec<Addr>>,
    _body: Option<&CgFunction>,
    preserves: Fields,
) -> Contract {
    let mut use_ = prep.use_.clone();
    let mut defs = prep.defs.clone();
    let mut writes = prep.writes;
    for call in &prep.calls {
        let contract = if call.targets.is_empty() {
            None
        } else {
            let found: Option<Vec<Contract>> =
                call.targets.iter().map(|&target| lookup(target)).collect();
            found.map(|found| join_contracts(&found))
        };
        match contract {
            Some(contract) => {
                use_[call.index as usize] |= fields_mask(contract.reads);
                defs[call.index as usize] |= fields_mask(contract.kills);
                writes |= fields_mask(contract.writes);
            }
            None => {
                use_[call.index as usize] = ALL;
                defs[call.index as usize] = ALL;
                writes = ALL;
            }
        }
    }
    let mut live_in = vec![0u64; use_.len()];
    let mut queued = vec![false; use_.len()];
    let mut work = prep.order.clone();
    for &index in &prep.order {
        queued[index as usize] = true;
    }
    while let Some(index) = work.pop() {
        queued[index as usize] = false;
        let mut out = 0;
        for &target in &prep.succ[index as usize] {
            out |= live_in[target as usize];
        }
        let new = use_[index as usize] | (out & !defs[index as usize]);
        if new != live_in[index as usize] {
            live_in[index as usize] = new;
            for &pred in &prep.preds[index as usize] {
                if !queued[pred as usize] {
                    queued[pred as usize] = true;
                    work.push(pred);
                }
            }
        }
    }
    let mut must_out = vec![ALL; use_.len()];
    let mut queued = vec![false; use_.len()];
    let mut work: Vec<u32> = prep.order.iter().rev().copied().collect();
    for &index in &prep.order {
        queued[index as usize] = true;
    }
    while let Some(index) = work.pop() {
        queued[index as usize] = false;
        let mut must_in = if index == prep.entry { 0 } else { ALL };
        if index != prep.entry {
            for &pred in &prep.preds[index as usize] {
                must_in &= must_out[pred as usize];
            }
        }
        let new = must_in | defs[index as usize];
        if new != must_out[index as usize] {
            must_out[index as usize] = new;
            for &target in &prep.succ[index as usize] {
                if !queued[target as usize] {
                    queued[target as usize] = true;
                    work.push(target);
                }
            }
        }
    }
    let mut killed = if prep.returns.is_empty() { 0 } else { ALL };
    for &index in &prep.returns {
        killed &= must_out[index as usize];
    }
    let mut reads = Fields::NONE;
    let mut kills = Fields::NONE;
    let mut wrote = Fields::NONE;
    for field in Field::ALL {
        let written = any(writes, field);
        if any(live_in[prep.entry as usize], field) || (written && !project(killed, field)) {
            reads.insert(field);
        }
        if project(killed, field) {
            kills.insert(field);
        }
        if written {
            wrote.insert(field);
        }
    }
    kills = kills.difference(preserves);
    Contract {
        reads,
        kills,
        writes: wrote,
        preserves,
    }
}

fn join_contracts(contracts: &[Contract]) -> Contract {
    let Some(first) = contracts.first() else {
        return Contract::CONSERVATIVE;
    };
    let mut reads = first.reads;
    let mut writes = first.writes;
    let mut kills = first.kills;
    let mut preserves = first.preserves;
    for contract in &contracts[1..] {
        reads |= contract.reads;
        writes |= contract.writes;
        kills = kills.intersection(contract.kills);
        preserves = preserves.intersection(contract.preserves);
    }
    Contract {
        reads,
        kills: kills.difference(preserves),
        writes,
        preserves,
    }
}

fn prepare(
    program: &Program,
    fir: &FunctionIr,
    indirect: &BTreeMap<Addr, Vec<Addr>>,
    lanes: &LaneMap,
    has_tails: bool,
) -> std::result::Result<Prepared, Reason> {
    if fir.insns.len() > MAX_INSTRUCTIONS {
        return Err(Reason::Conservative {
            detail: "SSA instruction budget".into(),
        });
    }
    if fir.seh.iter().any(|(index, kinds)| {
        kinds.iter().any(|kind| {
            !matches!(
                kind,
                rk_core::ir::SehEffect::Enter | rk_core::ir::SehEffect::Leave
            )
        }) || !fir.seh_unwind_only.contains(index)
    }) {
        return Err(Reason::Conservative {
            detail: "SEH body".into(),
        });
    }
    if !fir.dead.is_empty() {
        return Err(Reason::Conservative {
            detail: "dead code after noreturn".into(),
        });
    }
    let mut tails = BTreeSet::new();
    let mut call_rows: BTreeMap<u32, CallSite> = BTreeMap::new();
    for (&index, targets) in &fir.exits {
        let insn = &fir.insns[index as usize];
        if insn.mnemonic.eq_ignore_ascii_case("JMP") && fir.succ[index as usize].is_empty() {
            if targets.len() == 1 {
                tails.insert(index);
                call_rows.insert(
                    index,
                    CallSite {
                        index,
                        site: insn.addr,
                        targets: vec![targets[0]],
                        indirect: false,
                    },
                );
            } else if insn.ops.iter().any(|op| op.opcode == OpCode::BranchInd) {
                tails.insert(index);
                call_rows.insert(
                    index,
                    CallSite {
                        index,
                        site: insn.addr,
                        targets: Vec::new(),
                        indirect: true,
                    },
                );
            }
        }
    }
    if fir.exits.keys().any(|index| !tails.contains(index)) {
        return Err(Reason::Conservative {
            detail: "external exit".into(),
        });
    }
    if !closed(fir, &tails) {
        return Err(Reason::Conservative {
            detail: "control flow is not closed".into(),
        });
    }
    let cg =
        rk_ir::lower(fir, &program.image, &Recipes).map_err(|error| Reason::Conservative {
            detail: format!("lowering failed: {error}"),
        })?;
    let n = fir.insns.len();
    let mut use_ = vec![0u64; n];
    let mut defs = vec![0u64; n];
    let mut writes = 0u64;
    for insn in &cg.insns {
        let index = insn.insn as usize;
        let source = &fir.insns[index];
        let mut local = 0u64;
        let mut unknown = false;
        for cg_op in &insn.ops {
            match cg_op.op {
                Op::Slow(_) | Op::CallOther(_) => {
                    unknown = true;
                    continue;
                }
                Op::Call { target } => {
                    call_rows.entry(insn.insn).or_insert(CallSite {
                        index: insn.insn,
                        site: insn.addr,
                        targets: vec![target],
                        indirect: false,
                    });
                    continue;
                }
                Op::CallInd => {
                    let targets = indirect
                        .get(&(insn.addr + insn.length as Addr))
                        .cloned()
                        .unwrap_or_default();
                    call_rows.entry(insn.insn).or_insert(CallSite {
                        index: insn.insn,
                        site: insn.addr,
                        targets,
                        indirect: true,
                    });
                    continue;
                }
                Op::String(name) => {
                    let (reads, writes_fields, flags) = rk_ir::string::effect(name.0);
                    use_[index] |= fields_mask(reads) & !local;
                    let produced = fields_mask(writes_fields) | if flags { FLAG_LANES } else { 0 };
                    local |= produced;
                    defs[index] |= produced;
                    continue;
                }
                Op::Div32 { .. } | Op::Idiv32 { .. } => {
                    let regs = Registers::get();
                    for name in ["EAX", "EDX"] {
                        use_[index] |= lanes.mask(&regs.require(name)) & !local;
                    }
                }
                _ => {}
            }
            for input in &cg_op.inputs {
                use_[index] |= lanes.mask(input) & !local;
            }
            if let Some(output) = cg_op.output {
                let produced = lanes.mask(&output);
                local |= produced;
                defs[index] |= produced;
            }
        }
        let mnemonic = mnemonic(source);
        if AF_WRITERS.contains(&mnemonic.as_ref()) {
            defs[index] |= field_mask(Field::Af);
        }
        if !flag_free(&mnemonic) {
            writes |= FLAG_LANES;
        }
        if unknown {
            use_[index] = ALL;
            writes = ALL;
        }
        if mnemonic == "RDTSC" {
            defs[index] |= field_mask(Field::Eax) | field_mask(Field::Edx);
        }
        writes |= defs[index];
    }
    let mut preds = vec![Vec::new(); n];
    for (index, successors) in cg.succ.iter().enumerate() {
        for &target in successors {
            if tails.contains(&(index as u32)) {
                continue;
            }
            preds[target as usize].push(index as u32);
        }
    }
    let mut successors: Vec<Vec<u32>> = cg.succ.clone();
    for &index in &tails {
        successors[index as usize].clear();
    }
    let mut seen = vec![false; n];
    let mut pending = vec![cg.index_of(fir.addr).ok_or_else(|| Reason::Conservative {
        detail: "function address is not an instruction".into(),
    })?];
    while let Some(index) = pending.pop() {
        if seen[index as usize] {
            continue;
        }
        seen[index as usize] = true;
        pending.extend(successors[index as usize].iter().copied());
    }
    let order: Vec<u32> = (0..n as u32)
        .filter(|&index| seen[index as usize])
        .collect();
    let expected = reachable(fir, cg.index_of(fir.addr).unwrap());
    let visit_mismatch = (expected != order).then_some((expected.len(), order.len()));
    let returns: Vec<u32> = order
        .iter()
        .copied()
        .filter(|&index| tails.contains(&index) || is_return(&fir.insns[index as usize]))
        .collect();
    let raw = stack_info(program, fir);
    let calls: Vec<CallSite> = call_rows.into_values().collect();
    let entry = cg.index_of(fir.addr).unwrap();
    let preserve = PRESERVATION && raw.0 && !has_tails && !raw.2.is_empty();
    Ok(Prepared {
        entry,
        order,
        succ: successors,
        preds,
        use_,
        defs,
        writes,
        calls,
        returns,
        cg: preserve.then_some(preservation_cg(cg)),
        saved: raw.2,
        visit_mismatch,
    })
}

fn preservation_cg(mut cg: CgFunction) -> CgFunction {
    for insn in &mut cg.insns {
        let mut ops = Vec::with_capacity(insn.ops.len());
        for mut op in insn.ops.drain(..) {
            let Op::X87(x87) = op.op else {
                ops.push(op);
                continue;
            };
            match x87.memory() {
                Some(MemoryEffect::Read) => {
                    let address = op.inputs.first().copied().unwrap_or(Varnode::constant(0, 4));
                    op.op = Op::P(OpCode::Load);
                    op.output = None;
                    op.inputs = vec![address];
                    ops.push(op);
                }
                Some(MemoryEffect::Write) => {
                    let address = op.inputs.first().copied().unwrap_or(Varnode::constant(0, 4));
                    op.op = Op::P(OpCode::Store);
                    op.output = None;
                    op.inputs = vec![address, Varnode::constant(0, x87_store_size(x87))];
                    ops.push(op);
                }
                None if x87.result_size() != 0 || x87.compare_flags() => ops.push(op),
                None => {}
            }
        }
        insn.ops = ops;
    }
    cg
}

fn x87_store_size(x87: X87Op) -> u32 {
    match x87 {
        X87Op::Store { bits, .. } => (bits / 8) as u32,
        X87Op::Eager(X87Eager::Fbstp) => 10,
        X87Op::Environment(X87Environment::StoreCw | X87Environment::StoreSw) => 2,
        X87Op::Environment(X87Environment::StoreEnv)
        | X87Op::LegacyEnvironment(X87Legacy::StoreEnv) => 32,
        X87Op::Environment(X87Environment::Save)
        | X87Op::LegacyEnvironment(X87Legacy::Save) => 128,
        X87Op::LegacyEnvironment(X87Legacy::BcdStore) => 10,
        _ => 16,
    }
}

fn reachable(fir: &FunctionIr, entry: u32) -> Vec<u32> {
    let mut seen = vec![false; fir.insns.len()];
    let mut pending = vec![entry];
    while let Some(index) = pending.pop() {
        if seen[index as usize] {
            continue;
        }
        seen[index as usize] = true;
        pending.extend(fir.succ[index as usize].iter().copied());
    }
    (0..fir.insns.len() as u32)
        .filter(|&index| seen[index as usize])
        .collect()
}

struct PreservationCallFacts<'a> {
    contracts: &'a BTreeMap<Addr, Contract>,
    indirect: &'a BTreeMap<Addr, Vec<Addr>>,
    excluded: &'a BTreeSet<Addr>,
}

impl PreservationCallFacts<'_> {
    fn effect(contract: Contract) -> CallEffect {
        CallEffect {
            reads: contract.reads,
            kills: contract.kills,
            writes: contract.writes,
            preserves: contract.preserves,
        }
    }
}

impl CallFacts for PreservationCallFacts<'_> {
    fn call(&self, site: Addr, target: Option<Addr>) -> CallEffect {
        let contract = match target {
            Some(target) if !self.excluded.contains(&target) => {
                self.contracts.get(&target).copied()
            }
            Some(_) => None,
            None => self.indirect.get(&site).and_then(|targets| {
                if targets.is_empty() || targets.iter().any(|target| self.excluded.contains(target)) {
                    return None;
                }
                targets
                    .iter()
                    .map(|&target| self.contracts.get(&target).copied())
                    .collect::<Option<Vec<_>>>()
                    .map(|found| join_contracts(&found))
            }),
        };
        contract.map_or(CallEffect::CONSERVATIVE, Self::effect)
    }
}

struct StackSolver<'a> {
    ssa: &'a rk_ssa::Ssa,
    pop_facts: &'a BTreeMap<Addr, i32>,
    esp: &'static [StateKey],
    ebp: &'static [StateKey],
    frame: bool,
    root: rk_ssa::ValueId,
    memo: RefCell<Vec<Option<(rk_ssa::ValueId, i32)>>>,
    active: RefCell<Vec<bool>>,
}

impl StackSolver<'_> {
    fn get(&self, id: rk_ssa::ValueId) -> Option<(rk_ssa::ValueId, i32)> {
        if let Some(found) = self.memo.borrow()[id as usize] {
            return Some(found);
        }
        if self.active.borrow_mut()[id as usize] {
            return Some((id, 0));
        }
        self.active.borrow_mut()[id as usize] = true;
        let result = self.compute(id);
        self.active.borrow_mut()[id as usize] = false;
        if let Some(found) = result {
            self.memo.borrow_mut()[id as usize] = Some(found);
        }
        result
    }

    fn all(&self, args: &[rk_ssa::ValueId], skip: Option<rk_ssa::ValueId>) -> Option<(rk_ssa::ValueId, i32)> {
        let mut found = None;
        for &arg in args {
            if Some(arg) == skip {
                continue;
            }
            let next = self.get(arg)?;
            if found.is_some_and(|found| found != next) {
                return None;
            }
            found = Some(next);
        }
        found
    }

    fn compute(&self, id: rk_ssa::ValueId) -> Option<(rk_ssa::ValueId, i32)> {
        let value = self.ssa.value(id);
        let args = self.ssa.args(id);
        match value.op {
            Op::Byte(_) => self.get(*args.first()?),
            Op::P(OpCode::Copy) | Op::P(OpCode::IntZExt) if args.len() == 1 => self.get(args[0]),
            Op::Pack | Op::Phi => self.all(args, (value.op == Op::Phi).then_some(id)),
            Op::P(OpCode::IntAdd) | Op::P(OpCode::IntSub) if value.size == 4 => {
                let (a, b) = (*args.first()?, *args.get(1)?);
                match (self.ssa.value(a).op, self.ssa.value(b).op) {
                    (_, Op::Const(data)) => {
                        let (root, offset) = self.get(a)?;
                        let delta = data as i32;
                        Some((
                            root,
                            if value.op == Op::P(OpCode::IntAdd) {
                                offset.wrapping_add(delta)
                            } else {
                                offset.wrapping_sub(delta)
                            },
                        ))
                    }
                    (Op::Const(data), _) if value.op == Op::P(OpCode::IntAdd) => {
                        let (root, offset) = self.get(b)?;
                        Some((root, offset.wrapping_add(data as i32)))
                    }
                    _ => None,
                }
            }
            Op::StateReload(key) if self.ssa.field(key) == Some(Field::Esp) => {
                let site = *args.first()?;
                let Op::Call { target } = self.ssa.value(site).op else {
                    return None;
                };
                let count = *self.pop_facts.get(&target)?;
                let state = self.ssa.boundary(site)?;
                let base = state
                    .iter()
                    .find(|(candidate, _)| *candidate == self.esp[0])
                    .map(|(_, value)| *value)?;
                let (root, offset) = self.get(base)?;
                Some((root, offset.wrapping_add(4 + count)))
            }
            Op::Input(key) if self.esp.contains(&key) => Some((self.root, 0)),
            Op::Input(key) if self.frame && self.ebp.contains(&key) => Some((self.root, -4)),
            _ => None,
        }
    }
}

fn frame_pointer(cg: &CgFunction) -> bool {
    let regs = Registers::get();
    let (Some(ebp), Some(esp)) = (regs.register("EBP"), regs.register("ESP")) else {
        return false;
    };
    let (Some(first), Some(second)) = (cg.insns.first(), cg.insns.get(1)) else {
        return false;
    };
    first.ops.iter().any(|op| op.inputs.contains(&ebp))
        && first.ops.iter().any(|op| op.output == Some(esp))
        && second
            .ops
            .iter()
            .any(|op| op.output == Some(ebp) && op.inputs.contains(&esp))
}

fn preserved_registers(
    cg: &CgFunction,
    pop_facts: &BTreeMap<Addr, i32>,
    candidates: Fields,
    call_facts: &dyn CallFacts,
) -> Fields {
    let options = BuildOptions {
        policy: Policy::Optimizing,
        call_facts,
        allow_slow: true,
        value_budget: rk_ssa::DEFAULT_VALUE_BUDGET,
    };
    let Ok(mut ssa) = rk_ssa::build(cg, &options) else {
        return Fields::NONE;
    };
    let esp = rk_ir::lanes(Field::Esp);
    let ebp = rk_ir::lanes(Field::Ebp);
    let Some(root) = ssa.input(esp[0]) else {
        return Fields::NONE;
    };
    let solver = StackSolver {
        ssa: &ssa,
        pop_facts,
        esp,
        ebp,
        frame: frame_pointer(cg),
        root,
        memo: RefCell::new(vec![None; ssa.values.len()]),
        active: RefCell::new(vec![false; ssa.values.len()]),
    };
    for id in 0..ssa.values.len() as rk_ssa::ValueId {
        solver.get(id);
    }
    let offsets = solver.memo.into_inner();
    let stack_reload = |value: rk_ssa::ValueId| -> Option<(rk_ssa::ValueId, i32)> {
        offsets.get(value as usize).copied().flatten()
    };
    if !rk_ssa::forward(&mut ssa, Some(&stack_reload)) {
        return Fields::NONE;
    }
    let returns: Vec<rk_ssa::ValueId> = ssa
        .blocks
        .iter()
        .flat_map(|block| block.ops.iter().copied())
        .filter(|&value| matches!(ssa.values[value as usize].op, Op::Return))
        .collect();
    if returns.is_empty() {
        return Fields::NONE;
    }
    let mut states = Vec::new();
    for value in returns {
        let Some(state) = ssa.boundary(value) else {
            return Fields::NONE;
        };
        states.push(state.to_vec());
    }
    let mut preserved = Fields::NONE;
    for field in GPRS {
        if !candidates.contains(field) {
            continue;
        }
        let keys = rk_ir::lanes(field);
        if !keys.iter().all(|key| ssa.input(*key).is_some()) {
            continue;
        }
        let good = states.iter().all(|state| {
            keys.iter().all(|key| {
                let input = ssa.input(*key).unwrap();
                let output = state
                    .iter()
                    .find(|(candidate, _)| candidate == key)
                    .map(|(_, value)| *value);
                output.is_some_and(|output| same_lane(&ssa, input, output))
            })
        });
        if good {
            preserved.insert(field);
        }
    }
    preserved
}

fn same_lane(ssa: &rk_ssa::Ssa, left: rk_ssa::ValueId, right: rk_ssa::ValueId) -> bool {
    if left == right {
        return true;
    }
    let a = &ssa.values[left as usize];
    let b = &ssa.values[right as usize];
    if let (Op::Byte(x), Op::Byte(y)) = (a.op, b.op) {
        return x == y && same_lane(ssa, ssa.args(left)[0], ssa.args(right)[0]);
    }
    false
}

fn tail_target(fir: &FunctionIr) -> Option<Addr> {
    let mut found = None;
    for (&index, targets) in &fir.exits {
        let insn = &fir.insns[index as usize];
        if !insn.mnemonic.eq_ignore_ascii_case("JMP") || !fir.succ[index as usize].is_empty() {
            return None;
        }
        if targets.len() != 1 || found.is_some() {
            return None;
        }
        found = Some(targets[0]);
    }
    found
}

fn resolve_pops(
    raw: &BTreeMap<Addr, Option<i32>>,
    tails: &BTreeMap<Addr, Option<Addr>>,
) -> BTreeMap<Addr, i32> {
    fn resolve(
        addr: Addr,
        raw: &BTreeMap<Addr, Option<i32>>,
        tails: &BTreeMap<Addr, Option<Addr>>,
        memo: &mut BTreeMap<Addr, Option<i32>>,
        active: &mut BTreeSet<Addr>,
    ) -> Option<i32> {
        if let Some(found) = memo.get(&addr) {
            return *found;
        }
        if !active.insert(addr) {
            return None;
        }
        let found = match raw.get(&addr).copied().flatten() {
            Some(count) => Some(count),
            None => tails
                .get(&addr)
                .copied()
                .flatten()
                .and_then(|target| resolve(target, raw, tails, memo, active)),
        };
        active.remove(&addr);
        memo.insert(addr, found);
        found
    }

    let mut memo = BTreeMap::new();
    let mut active = BTreeSet::new();
    for &addr in tails.keys() {
        resolve(addr, raw, tails, &mut memo, &mut active);
    }
    memo.into_iter()
        .filter_map(|(addr, count)| count.map(|count| (addr, count)))
        .collect()
}

fn stack_info(program: &Program, fir: &FunctionIr) -> (bool, Option<i32>, Fields) {
    let mut pushed = Fields::NONE;
    let mut popped = Fields::NONE;
    let mut leave = false;
    let mut pop_counts = BTreeSet::new();
    for insn in &fir.insns {
        let bytes = program
            .image
            .bytes(insn.addr, insn.length as usize)
            .unwrap_or(&[]);
        if bytes.len() == 1 && (0x50..=0x57).contains(&bytes[0]) {
            pushed.insert(GPRS[(bytes[0] - 0x50) as usize]);
        }
        if bytes.len() == 1 && (0x58..=0x5f).contains(&bytes[0]) {
            popped.insert(GPRS[(bytes[0] - 0x58) as usize]);
        }
        if bytes.len() == 1 && bytes[0] == 0xc9 {
            leave = true;
        }
        let raw = bytes.strip_prefix(&[0xf3]).unwrap_or(bytes);
        if raw.first() == Some(&0xc3) {
            pop_counts.insert(0);
        } else if raw.first() == Some(&0xc2) && raw.len() == 3 {
            pop_counts.insert(i32::from(u16::from_le_bytes([raw[1], raw[2]])));
        }
    }
    let pops = if pop_counts.len() == 1 {
        pop_counts.first().copied()
    } else {
        None
    };
    let mut saved = Fields(pushed.0 & popped.0);
    if leave && pushed.contains(Field::Ebp) {
        saved.insert(Field::Ebp);
    }
    (!pushed.is_empty() && (!popped.is_empty() || leave), pops, saved)
}

fn flag_free(mnemonic: &str) -> bool {
    FLAG_FREE.contains(&mnemonic)
        || mnemonic.starts_with('J')
        || mnemonic.starts_with("SET")
        || mnemonic.starts_with("CMOV")
        || (mnemonic.starts_with('F')
            && !matches!(mnemonic, "FCOMI" | "FCOMIP" | "FUCOMI" | "FUCOMIP"))
}

fn conservative_reason(error: &Error) -> Reason {
    match error {
        Error::Diagnostic(diagnostic) => diagnostic.reason.clone(),
        other => Reason::Conservative {
            detail: other.to_string(),
        },
    }
}
