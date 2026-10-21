use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;

use rayon::prelude::*;
use rk_core::ir::{FunctionIr, Insn, SehEffect};
use rk_core::program::{Image, Program};
use rk_core::{Addr, Diagnostic, Error, Result};
use rk_decode::{
    branch_target, gpr32, imm, mem, msvc_eh3_finally_only, msvc_seh_epilog,
    msvc_seh_prolog, msvc_unwind_only, seh_constructor_helper, DecodedInsn, Decoder,
    OperandKind, JCC, MSVC_EH3_FRAME_LEN, MSVC_SEH_PROLOG_LEN,
};
use rk_sleigh::{format_varnode, Lifter};

const R_EAX: u8 = 0;
const R_ECX: u8 = 1;
const R_EDX: u8 = 2;
const R_ESP: u8 = 4;
const R_EBP: u8 = 5;

const TERMINATORS: [&str; 2] = ["RET", "JMP"];
const EXPLICIT_DESTINATION: [&str; 22] = [
    "MOV", "LEA", "ADD", "SUB", "AND", "OR", "XOR", "INC", "DEC", "NEG", "NOT", "SHL",
    "SHR", "SAR", "MOVZX", "MOVSX", "POP", "PUSH", "CMP", "TEST", "JMP", "NOP",
];
const READONLY: [&str; 17] = [
    "CMP", "TEST", "PUSH", "CALL", "JMP", "RET", "NOP", "WAIT", "PAUSE", "CLC", "STC",
    "CMC", "CLD", "STD", "SAHF", "CLI", "STI",
];
const DEST_WRITE: [&str; 38] = [
    "MOV", "MOVZX", "MOVSX", "LEA", "POP", "ADD", "ADC", "SUB", "SBB", "AND", "OR",
    "XOR", "INC", "DEC", "NEG", "NOT", "SHL", "SHR", "SAR", "ROL", "ROR", "RCL", "RCR",
    "SHLD", "SHRD", "BSWAP", "BSF", "BSR", "BTS", "BTR", "BTC", "IMUL", "PUSHAD",
    "PUSHFD", "PUSHF", "POPFD", "POPF", "LEAVE",
];

#[derive(Clone)]
struct RawFunction {
    addr: Addr,
    decoded: Vec<DecodedInsn>,
    insns: Vec<Insn>,
    index: BTreeMap<Addr, u32>,
    contiguous: Vec<bool>,
    fallthrough: Vec<Addr>,
    end: Addr,
    pushed_continuations: BTreeSet<Addr>,
    seh_sites: BTreeMap<u32, Addr>,
    seh_restores: BTreeSet<u32>,
    return_jumps: BTreeSet<u32>,
    seh_escapes: BTreeSet<u32>,
    dead_addrs: BTreeSet<Addr>,
    recovered_tables: BTreeMap<Addr, Vec<Addr>>,
}

#[derive(Clone, PartialEq, Eq)]
struct ReturnState {
    delta: Option<i32>,
    frame: Option<i32>,
    saved: BTreeSet<u8>,
}

impl ReturnState {
    fn unknown() -> Self {
        Self { delta: None, frame: None, saved: BTreeSet::new() }
    }
}

impl RawFunction {
    fn idx(&self, addr: Addr) -> Option<u32> {
        self.index.get(&addr).copied()
    }

    fn first(&self, i: usize) -> Option<&rk_decode::Operand> {
        self.decoded.get(i)?.operands.first()
    }

    fn push_ret_target(&self, i: usize) -> Option<Addr> {
        let ins = self.decoded.get(i)?;
        if ins.mnemonic != "PUSH" || i + 1 >= self.decoded.len() || !self.contiguous[i] {
            return None;
        }
        if self.decoded[i + 1].mnemonic != "RET" || !self.decoded[i + 1].operands.is_empty() {
            return None;
        }
        let target = imm(ins.operands.first()?)?;
        (operand_size(&ins.operands, Some(32))? == 32).then_some(target as Addr)
    }

    fn branch_target(&self, i: usize) -> Option<Addr> {
        branch_target(self.decoded.get(i)?)
    }

    fn import_tail_slot(&self, program: &Program, i: usize) -> Option<Addr> {
        let ins = self.decoded.get(i)?;
        if ins.mnemonic != "JMP" || ins.length != 6 || ins.raw[..2] != [0xff, 0x25] {
            return None;
        }
        let slot = u32::from_le_bytes([ins.raw[2], ins.raw[3], ins.raw[4], ins.raw[5]]);
        program.image.imports.contains_key(&slot).then_some(slot)
    }

    fn calls_seh_epilog(&self, program: &Program, i: usize) -> bool {
        self.decoded.get(i).is_some_and(|ins| {
            ins.mnemonic == "CALL"
                && self
                    .branch_target(i)
                    .is_some_and(|target| msvc_seh_epilog(&program.image, target))
        })
    }

    fn successors(&self, program: &Program, i: usize) -> Vec<u32> {
        let Some(ins) = self.decoded.get(i) else {
            return Vec::new();
        };
        let m = ins.mnemonic.as_str();
        if let Some(target) = self.push_ret_target(i) {
            if !self.pushed_continuations.contains(&target) {
                return self.idx(target).into_iter().collect();
            }
        }
        let next = self.fallthrough.get(i).copied().and_then(|addr| self.idx(addr));
        if m == "INT3" {
            return Vec::new();
        }
        if m == "RET" {
            return self
                .pushed_continuations
                .iter()
                .filter_map(|target| self.idx(*target))
                .collect();
        }
        if JCC.contains(&m) {
            let mut out: Vec<u32> = next.into_iter().collect();
            if let Some(target) = self.branch_target(i) {
                if let Some(index) = self.idx(target) {
                    out.push(index);
                }
            }
            return out;
        }
        if m == "JMP" {
            if self.import_tail_slot(program, i).is_some() {
                return Vec::new();
            }
            if let Some(target) = self.branch_target(i) {
                return self.idx(target).into_iter().collect();
            }
            let targets: Vec<Addr> = program
                .tables
                .get(&(self.addr, ins.addr))
                .or_else(|| self.recovered_tables.get(&ins.addr))
                .cloned()
                .unwrap_or_else(|| self.index.keys().copied().collect());
            return targets.into_iter().filter_map(|target| self.idx(target)).collect();
        }
        next.into_iter().collect()
    }

    fn popped_return_jumps(&self, entries: &[Addr]) -> BTreeSet<u32> {
        if !self.decoded.iter().any(|ins| ins.mnemonic == "POP") {
            return BTreeSet::new();
        }
        let candidates: Vec<usize> = self
            .decoded
            .iter()
            .enumerate()
            .filter(|(_, ins)| {
                ins.mnemonic == "JMP" && ins.operands.first().and_then(gpr32).is_some()
            })
            .map(|(i, _)| i)
            .collect();
        if candidates.is_empty() {
            return BTreeSet::new();
        }
        let mut states: BTreeMap<u32, ReturnState> = BTreeMap::new();
        let mut pending: Vec<u32> = Vec::new();
        let mut unknown_indirect_seen = false;

        fn merge(
            states: &mut BTreeMap<u32, ReturnState>,
            pending: &mut Vec<u32>,
            i: u32,
            state: ReturnState,
        ) {
            let merged = if let Some(old) = states.get(&i) {
                ReturnState {
                    delta: (old.delta == state.delta).then_some(state.delta).flatten(),
                    frame: (old.frame == state.frame).then_some(state.frame).flatten(),
                    saved: old.saved.intersection(&state.saved).copied().collect(),
                }
            } else {
                state
            };
            if states.get(&i) != Some(&merged) {
                states.insert(i, merged);
                pending.push(i);
            }
        }

        let roots: Vec<Addr> = std::iter::once(self.addr).chain(entries.iter().copied()).collect();
        for addr in roots {
            if let Some(i) = self.idx(addr) {
                merge(&mut states, &mut pending, i, ReturnState { delta: Some(0), frame: None, saved: BTreeSet::new() });
            }
        }
        while let Some(i) = pending.pop() {
            let index = i as usize;
            let Some(ins) = self.decoded.get(index) else {
                continue;
            };
            let Some(state) = states.get(&i).cloned() else {
                continue;
            };
            if candidates.contains(&index)
                && self.first(index).and_then(gpr32).is_some_and(|r| state.saved.contains(&r))
            {
                continue;
            }
            let out = self.transfer(ins, state);
            if ins.mnemonic == "JMP" && self.branch_target(index).is_none() {
                if !unknown_indirect_seen {
                    for j in 0..self.decoded.len() as u32 {
                        merge(&mut states, &mut pending, j, ReturnState::unknown());
                    }
                    unknown_indirect_seen = true;
                }
                continue;
            }
            for successor in self.successors_during_proof(index) {
                merge(&mut states, &mut pending, successor, out.clone());
            }
        }
        candidates
            .into_iter()
            .filter(|&i| {
                states.get(&(i as u32)).is_some_and(|state| {
                    self.first(i).and_then(gpr32).is_some_and(|r| state.saved.contains(&r))
                })
            })
            .map(|i| i as u32)
            .collect()
    }

    fn successors_during_proof(&self, i: usize) -> Vec<u32> {
        let Some(ins) = self.decoded.get(i) else {
            return Vec::new();
        };
        let m = ins.mnemonic.as_str();
        let next = self.fallthrough.get(i).copied().and_then(|addr| self.idx(addr));
        if m == "INT3" {
            Vec::new()
        } else if m == "RET" {
            self.pushed_continuations.iter().filter_map(|t| self.idx(*t)).collect()
        } else if JCC.contains(&m) {
            let mut out: Vec<u32> = next.into_iter().collect();
            if let Some(target) = self.branch_target(i) {
                if let Some(index) = self.idx(target) {
                    out.push(index);
                }
            }
            out
        } else if m == "JMP" {
            if let Some(target) = self.branch_target(i) {
                self.idx(target).into_iter().collect()
            } else if self.push_ret_target(i).is_some() {
                self.push_ret_target(i).and_then(|t| self.idx(t)).into_iter().collect()
            } else {
                self.index.keys().copied().filter_map(|t| self.idx(t)).collect()
            }
        } else {
            next.into_iter().collect()
        }
    }

    fn transfer(&self, ins: &DecodedInsn, state: ReturnState) -> ReturnState {
        let m = ins.mnemonic.as_str();
        let mut saved = state.saved.clone();
        let mut writes: BTreeSet<u8> = BTreeSet::new();
        let ops = &ins.operands;
        if READONLY.contains(&m) || JCC.contains(&m) || m.starts_with('F') {
            if m == "FNSTSW" || m == "FSTSW" {
                writes.insert(R_EAX);
            }
        } else if DEST_WRITE.contains(&m) || m.starts_with("SET") || m.starts_with("CMOV") {
            if let Some(reg) = ops.first().and_then(gpr32) {
                writes.insert(reg);
            }
        } else if matches!(m, "XCHG" | "XADD" | "CMPXCHG") {
            for reg in ops.iter().filter_map(gpr32) {
                writes.insert(reg);
            }
            if m == "CMPXCHG" {
                writes.insert(R_EAX);
            }
        } else if matches!(m, "PUSHAD" | "PUSHFD" | "PUSHF" | "POPFD" | "POPF") {
            writes.insert(R_ESP);
        } else if m == "LEAVE" {
            writes.extend([R_ESP, R_EBP]);
        } else if matches!(m, "MUL" | "DIV" | "IDIV" | "RDTSC") {
            writes.extend([R_EAX, R_EDX]);
        } else if matches!(m, "CDQ" | "CWD") {
            writes.insert(R_EDX);
        } else if matches!(m, "CBW" | "CWDE" | "LAHF" | "XLAT") {
            writes.insert(R_EAX);
        } else {
            return ReturnState::unknown();
        }
        if m == "IMUL" && ops.len() == 1 {
            writes.extend([R_EAX, R_EDX]);
        }
        if m.starts_with("LOOP") {
            writes.insert(R_ECX);
        }
        for reg in &writes {
            saved.remove(reg);
        }
        if m == "POP" && state.delta == Some(0) {
            if let Some(reg) = ops.first().and_then(gpr32) {
                if reg != R_ESP {
                    saved.insert(reg);
                }
            }
        }
        let adjustment = if matches!(m, "PUSH" | "POP") {
            operand_size(ops, Some(32)).map(|size| size as i32 / 8 * if m == "PUSH" { -1 } else { 1 })
        } else if matches!(m, "PUSHAD" | "PUSHFD" | "PUSHF" | "POPFD" | "POPF") {
            Some(match m {
                "PUSHAD" => -32,
                "PUSHFD" => -4,
                "PUSHF" => -2,
                "POPFD" => 4,
                _ => 2,
            })
        } else if m == "RET" {
            Some(4 + ops.first().and_then(imm).unwrap_or(0) as i32)
        } else if matches!(m, "ADD" | "SUB")
            && ops.first().and_then(gpr32) == Some(R_ESP)
        {
            ops.get(1).and_then(imm).map(|value| {
                let signed = value as u32 as i32;
                signed * if m == "SUB" { -1 } else { 1 }
            })
        } else {
            None
        };
        let new_delta = if m == "LEAVE" {
            state.frame.map(|frame| frame + 4)
        } else if m == "POP" && ops.first().and_then(gpr32) == Some(R_ESP) {
            None
        } else if let Some(adjustment) = adjustment {
            state.delta.map(|delta| delta + adjustment)
        } else if writes.contains(&R_ESP) {
            None
        } else {
            state.delta
        };
        let mut new_frame = if writes.contains(&R_EBP) { None } else { state.frame };
        if m == "MOV" && ops.len() == 2 && ops[0].display == "EBP" && ops[1].display == "ESP" {
            new_frame = state.delta;
        }
        ReturnState { delta: new_delta, frame: new_frame, saved }
    }

    fn reached(&self, program: &Program, roots: &BTreeSet<Addr>) -> BTreeSet<u32> {
        if self.decoded.iter().enumerate().any(|(i, ins)| {
            ins.mnemonic == "JMP"
                && self.branch_target(i).is_none()
                && !program.tables.contains_key(&(self.addr, ins.addr))
                && !self.recovered_tables.contains_key(&ins.addr)
                && !popped_jump(self, i)
        }) {
            return (0..self.decoded.len() as u32).collect();
        }
        let mut work: Vec<u32> = roots
            .iter()
            .chain(self.pushed_continuations.iter())
            .filter_map(|addr| self.idx(*addr))
            .collect();
        let mut live = BTreeSet::new();
        while let Some(i) = work.pop() {
            if !live.insert(i) {
                continue;
            }
            let index = i as usize;
            let ins = &self.decoded[index];
            if never_returns(program, ins) {
                continue;
            }
            if popped_jump(self, index) {
                work.extend(self.pushed_continuations.iter().filter_map(|t| self.idx(*t)));
                continue;
            }
            if !TERMINATORS.contains(&ins.mnemonic.as_str()) && !self.contiguous[index] {
                if let Some(target) = self.idx(self.fallthrough[index]) {
                    work.push(target);
                }
                continue;
            }
            work.extend(self.successors(program, index));
        }
        live
    }

    fn dead_after_noreturn(&self, program: &Program, entries: &[Addr]) -> BTreeSet<Addr> {
        if !self.decoded.iter().any(|ins| never_returns(program, ins)) {
            return BTreeSet::new();
        }
        let roots = std::iter::once(self.addr).chain(entries.iter().copied()).collect();
        let live = self.reached(program, &roots);
        let mut dead = BTreeSet::new();
        let mut after_noreturn = false;
        for (i, ins) in self.decoded.iter().enumerate() {
            if live.contains(&(i as u32)) {
                after_noreturn = never_returns(program, ins);
            } else if after_noreturn {
                dead.insert(ins.addr);
            }
        }
        dead
    }

    fn seh_escaping_returns(&self, program: &Program, helpers: &BTreeSet<Addr>) -> BTreeSet<u32> {
        if self.seh_sites.is_empty()
            && !self.decoded.iter().any(|ins| {
                ins.mnemonic == "CALL"
                    && self
                        .branch_target(self.index_of(ins.addr))
                        .is_some_and(|target| helpers.contains(&target))
            })
        {
            return BTreeSet::new();
        }
        let returns = self.popped_return_jumps(&[]);
        let mut work = vec![(self.idx(self.addr).unwrap_or(0), false)];
        let mut seen = BTreeSet::new();
        let mut escapes = BTreeSet::new();
        while let Some((i, mut active)) = work.pop() {
            if !seen.insert((i, active)) {
                continue;
            }
            let index = i as usize;
            let ins = &self.decoded[index];
            if self.seh_sites.contains_key(&i)
                || (ins.mnemonic == "CALL"
                    && self.branch_target(index).is_some_and(|t| helpers.contains(&t)))
            {
                active = true;
            } else if self.seh_restores.contains(&i) || self.calls_seh_epilog(program, index) {
                active = false;
            }
            if returns.contains(&i)
                || (ins.mnemonic == "RET" && self.push_ret_target(index).is_none())
            {
                if active {
                    escapes.insert(i);
                }
                if returns.contains(&i) {
                    continue;
                }
            }
            work.extend(
                self.successors(program, index)
                    .into_iter()
                    .map(|j| (j, active)),
            );
        }
        escapes
    }

    fn seh_effects(
        &self,
        program: &Program,
        helpers: &BTreeSet<Addr>,
    ) -> Result<BTreeMap<u32, Vec<SehEffect>>> {
        for &i in self.seh_sites.keys() {
            if self.decoded[i as usize].mnemonic != "MOV" {
                return Err(Diagnostic::error(
                    self.decoded[i as usize].addr,
                    rk_core::Reason::Unsupported { detail: "SEH frame site is not a MOV".into() },
                ));
            }
        }
        let mut effects = BTreeMap::new();
        let unlinks = !self.seh_sites.is_empty() || msvc_seh_epilog(&program.image, self.addr);
        for (i, ins) in self.decoded.iter().enumerate() {
            let i = i as u32;
            if seh_except_entry(ins) {
                effects.insert(i, vec![SehEffect::Landing]);
            } else if ins.mnemonic == "MOV" && unlinks && ins.operands.len() == 2 {
                let dst = &ins.operands[0];
                let src = &ins.operands[1];
                if (seh_chain_operand(dst, 0) || self.seh_sites.contains_key(&i))
                    && gpr32(src).is_some()
                {
                    effects.insert(
                        i,
                        vec![if self.seh_sites.contains_key(&i) {
                            SehEffect::Enter
                        } else {
                            SehEffect::Leave
                        }],
                    );
                }
            } else if ins.mnemonic == "POP" && self.seh_restores.contains(&i) {
                effects.insert(i, vec![SehEffect::Leave]);
            } else if ins.mnemonic == "CALL"
                && self.branch_target(i as usize).is_some_and(|t| helpers.contains(&t))
            {
                effects.insert(i, vec![SehEffect::Adopt]);
            } else if ins.mnemonic == "RET" && self.seh_escapes.contains(&i) {
                effects.insert(i, vec![SehEffect::Orphan]);
            }
        }
        Ok(effects)
    }

    fn index_of(&self, addr: Addr) -> usize {
        self.index.get(&addr).copied().unwrap_or(0) as usize
    }

    fn prepare(&mut self, program: &Program, relocated: &BTreeMap<Addr, Addr>) {
        self.index = self
            .decoded
            .iter()
            .enumerate()
            .map(|(i, ins)| (ins.addr, i as u32))
            .collect();
        self.fallthrough = self
            .decoded
            .iter()
            .map(|ins| ins.addr + ins.length as u32)
            .collect();
        for i in 0..self.decoded.len() {
            let next = self
                .decoded
                .get(i + 1)
                .map(|ins| ins.addr)
                .unwrap_or(self.end);
            self.contiguous[i] = self.fallthrough[i] == next;
        }
        self.recovered_tables = recover_tables(program, relocated, self);
        self.pushed_continuations = self.pushed_continuations();
        self.seh_sites = seh_frame_sites(self, &program.image);
        self.seh_restores = seh_restore_sites(self);
    }

    fn pushed_continuations(&self) -> BTreeSet<Addr> {
        let mut targets = BTreeSet::new();
        for ins in &self.decoded {
            if ins.mnemonic != "PUSH" {
                continue;
            }
            let Some(op) = ins.operands.first() else {
                continue;
            };
            let Some(target) = imm(op) else {
                continue;
            };
            let target = target as Addr;
            if operand_size(&ins.operands, Some(32)) == Some(32)
                && self.index.contains_key(&target)
            {
                targets.insert(target);
            }
        }
        targets
    }

    fn analyze(&mut self, program: &Program, entries: &[Addr], helpers: &BTreeSet<Addr>) {
        self.return_jumps = self.popped_return_jumps(entries);
        self.seh_escapes = self.seh_escaping_returns(program, helpers);
        self.dead_addrs = self.dead_after_noreturn(program, entries);
    }
}

pub struct Analysis {
    pub ir: BTreeMap<Addr, Result<FunctionIr>>,
    pub seh_helpers: BTreeSet<Addr>,
}

thread_local! {
    static LIFTERS: std::cell::RefCell<Option<(Lifter, Decoder)>> = const { std::cell::RefCell::new(None) };
}

fn with_lifters<R>(f: impl FnOnce(&mut Lifter, &Decoder) -> R) -> R {
    LIFTERS.with(|slot| {
        let mut slot = slot.borrow_mut();
        let (lifter, decoder) = slot.get_or_insert_with(|| {
            (
                Lifter::new().expect("SLEIGH initialization"),
                Decoder::new().expect("capstone initialization"),
            )
        });
        f(lifter, decoder)
    })
}

pub fn analyze(program: &Program) -> Analysis {
    let addresses: Vec<Addr> = program.functions.keys().copied().collect();
    let relocated: BTreeMap<Addr, Addr> = program
        .image
        .relocations
        .iter()
        .map(|relocation| (relocation.site, relocation.target))
        .collect();
    let mut raw: Vec<(Addr, Result<RawFunction>)> = addresses
        .par_iter()
        .map(|&addr| {
            let result = with_lifters(|lifter, decoder| {
                build_raw(program, addr, lifter, decoder, &relocated)
            });
            (addr, result)
        })
        .collect();
    raw.sort_by_key(|(addr, _)| *addr);
    let helpers = seh_helpers(program, &raw);
    let ir: BTreeMap<Addr, Result<FunctionIr>> = raw
        .into_par_iter()
        .map(|(addr, raw)| {
            let result = raw.and_then(|raw| finalize(program, addr, raw, &helpers));
            (addr, result)
        })
        .collect();
    Analysis { ir, seh_helpers: helpers }
}

pub fn analyze_function(program: &Program, addr: Addr, helpers: &BTreeSet<Addr>) -> Result<FunctionIr> {
    let mut lifter = Lifter::new()?;
    let decoder = Decoder::new()?;
    let relocated: BTreeMap<Addr, Addr> = program
        .image
        .relocations
        .iter()
        .map(|relocation| (relocation.site, relocation.target))
        .collect();
    let raw = build_raw(program, addr, &mut lifter, &decoder, &relocated)?;
    finalize(program, addr, raw, helpers)
}

fn seh_helpers(program: &Program, raw: &[(Addr, Result<RawFunction>)]) -> BTreeSet<Addr> {
    let mut helpers: BTreeSet<Addr> = raw
        .iter()
        .filter_map(|(addr, raw)| {
            let raw = raw.as_ref().ok()?;
            (!raw.seh_escaping_returns(program, &BTreeSet::new()).is_empty()).then_some(*addr)
        })
        .collect();
    loop {
        let pending: Vec<Addr> = raw
            .iter()
            .filter_map(|(addr, raw)| {
                let raw = raw.as_ref().ok()?;
                if helpers.contains(addr) {
                    return None;
                }
                let calls = raw.decoded.iter().enumerate().filter_map(|(i, ins)| {
                    (ins.mnemonic == "CALL").then(|| raw.branch_target(i)).flatten()
                });
                calls.into_iter().any(|target| helpers.contains(&target)).then_some(*addr)
            })
            .collect();
        if pending.is_empty() {
            return helpers;
        }
        let mut found = BTreeSet::new();
        for addr in pending {
            if let Some(Ok(raw)) = raw.iter().find(|(a, _)| *a == addr).map(|(_, r)| r) {
                if !raw.seh_escaping_returns(program, &helpers).is_empty() {
                    found.insert(addr);
                }
            }
        }
        if found.is_empty() {
            return helpers;
        }
        helpers.extend(found);
    }
}

fn build_raw(
    program: &Program,
    addr: Addr,
    lifter: &mut Lifter,
    decoder: &Decoder,
    relocated: &BTreeMap<Addr, Addr>,
) -> Result<RawFunction> {
    let function = program
        .functions
        .get(&addr)
        .ok_or_else(|| Diagnostic::error(addr, rk_core::Reason::EntryOutsideFunctions))?;
    let mut decoded = Vec::new();
    for span in &function.spans {
        let raw = program.image.bytes_at(span.start, span.size as usize)?;
        let lengths;
        let lengths: &[u8] = match &span.lengths {
            Some(lengths) => lengths,
            None => {
                lengths = lifter.lengths(span.start, raw)?;
                &lengths
            }
        };
        let mut at = span.start;
        for &length in lengths {
            let raw = program
                .image
                .bytes(at, length as usize)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: span.start }))?;
            let ins = decoder
                .decode_span(at, raw)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: span.start }))?;
            decoded.push(ins);
            at += length as u32;
        }
    }
    let starts: BTreeSet<Addr> = decoded.iter().map(|ins| ins.addr).collect();
    for entry in program.configured_entries.iter().copied() {
        if starts.contains(&entry) || program.containing(entry) != Some(addr) {
            continue;
        }
        let mut at = entry;
        let mut lengths = Vec::new();
        while !starts.contains(&at) {
            if program.containing(at) != Some(addr) || lengths.len() == 16 {
                return Err(Diagnostic::error(
                    addr,
                    rk_core::Reason::OverlappingEntryDoesNotRejoin { entry },
                ));
            }
            let bytes = program
                .image
                .bytes(at, 15)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: entry }))?;
            let length = lifter.length(at, bytes)?;
            lengths.push(length);
            at += length as u32;
            let instruction = program
                .image
                .bytes(at - length as u32, length as usize)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: entry }))?;
            if lifter.ends_flow(at - length as u32, instruction)? {
                break;
            }
        }
        let mut at = entry;
        for length in lengths {
            let raw = program
                .image
                .bytes(at, length as usize)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: entry }))?;
            let ins = decoder
                .decode_span(at, raw)
                .ok_or_else(|| Diagnostic::error(at, rk_core::Reason::DecoderCannotTile { span: entry }))?;
            decoded.push(ins);
            at += length as u32;
        }
    }
    decoded.sort_by_key(|ins| ins.addr);
    if !decoded.iter().any(|ins| ins.addr == addr) {
        return Err(Diagnostic::error(addr, rk_core::Reason::EntryNotInstructionBoundary));
    }
    let mut insns = Vec::with_capacity(decoded.len());
    for ins in &decoded {
        let lifted = lifter.lift(ins.addr, &ins.raw[..ins.length as usize])?;
        insns.push(Insn {
            addr: ins.addr,
            length: lifted.length,
            mnemonic: ins.mnemonic.clone(),
            operands: ins
                .operands
                .iter()
                .map(|o| o.display.clone())
                .collect::<Vec<_>>()
                .join(","),
            text: ins.text.clone(),
            ops: lifted.ops,
            internal_flow: lifted.internal_flow,
            x87: lifted.x87,
            x87_delta: lifted.x87_delta,
            userops: lifted.userops,
        });
    }
    let end = (addr + function.spans.iter().map(|s| s.size).sum::<u32>())
        .max(decoded.last().map(|ins| ins.addr + 1).unwrap_or(addr + 1));
    let contiguous = vec![true; decoded.len()];
    let mut raw = RawFunction {
        addr,
        decoded,
        insns,
        index: BTreeMap::new(),
        contiguous,
        fallthrough: Vec::new(),
        end,
        pushed_continuations: BTreeSet::new(),
        seh_sites: BTreeMap::new(),
        seh_restores: BTreeSet::new(),
        return_jumps: BTreeSet::new(),
        seh_escapes: BTreeSet::new(),
        dead_addrs: BTreeSet::new(),
        recovered_tables: BTreeMap::new(),
    };
    raw.prepare(program, relocated);
    Ok(raw)
}

fn finalize(program: &Program, addr: Addr, mut raw: RawFunction, helpers: &BTreeSet<Addr>) -> Result<FunctionIr> {
    let mut entries = program
        .interior_by_function
        .get(&addr)
        .cloned()
        .unwrap_or_default();
    raw.analyze(program, &entries, helpers);
    let mut succ = Vec::with_capacity(raw.decoded.len());
    for i in 0..raw.decoded.len() {
        succ.push(raw.successors(program, i));
    }
    let mut noreturn = BTreeMap::new();
    for (i, ins) in raw.decoded.iter().enumerate() {
        if never_returns(program, ins) {
            noreturn.insert(i as u32, ins.addr + ins.length as u32);
            succ[i] = Vec::new();
        }
    }
    let exits = external_exits(program, &raw);
    let tables = table_jumps(&raw);
    let dynamic: BTreeSet<u32> = raw
        .decoded
        .iter()
        .enumerate()
        .filter(|(i, ins)| {
            ins.mnemonic == "JMP"
                && raw.branch_target(*i).is_none()
                && !program.tables.contains_key(&(raw.addr, ins.addr))
                && !raw.recovered_tables.contains_key(&ins.addr)
                && !raw.return_jumps.contains(&(*i as u32))
        })
        .map(|(i, _)| i as u32)
        .collect();
    let call_returns = raw
        .decoded
        .iter()
        .filter(|ins| ins.mnemonic == "CALL")
        .map(|ins| raw.fallthrough[raw.index[&ins.addr] as usize])
        .collect();
    let seh = raw.seh_effects(program, helpers)?;
    entries.extend(seh.iter().filter_map(|(index, kinds)| {
        let next = raw.fallthrough[*index as usize];
        (kinds.iter().any(|kind| matches!(kind, SehEffect::Enter | SehEffect::Adopt))
            && raw.idx(next).is_some()).then_some(next)
    }));
    entries.sort_unstable();
    entries.dedup();
    let eh3 = msvc_eh3_finally_only(
        &program.image,
        addr,
        eh3_trylevels(&raw, program).as_ref(),
    )
    .then_some(addr + MSVC_EH3_FRAME_LEN - 7);
    let seh_unwind_only: BTreeSet<u32> = raw
        .seh_sites
        .iter()
        .filter_map(|(i, handler)| {
            (msvc_unwind_only(&program.image, Some(*handler))
                || raw.decoded[*i as usize].addr == eh3.unwrap_or(0))
            .then_some(*i)
        })
        .collect();
    let import_tails = raw
        .decoded
        .iter()
        .enumerate()
        .filter_map(|(i, _)| raw.import_tail_slot(program, i).map(|slot| (i as u32, slot)))
        .collect();
    let dead = raw.dead_addrs.iter().filter_map(|a| raw.idx(*a)).collect();
    Ok(FunctionIr {
        addr,
        insns: raw.insns,
        succ,
        exits,
        tables,
        dynamic,
        noreturn,
        entries,
        dead,
        call_returns,
        seh,
        seh_handlers: raw.seh_sites,
        seh_unwind_only,
        import_tails,
    })
}

fn external_exits(program: &Program, raw: &RawFunction) -> BTreeMap<u32, Vec<Addr>> {
    let mut exits = BTreeMap::new();
    for (i, ins) in raw.decoded.iter().enumerate() {
        let m = ins.mnemonic.as_str();
        if m == "INT3"
            || raw.push_ret_target(i).is_some()
            || never_returns(program, ins)
            || raw.dead_addrs.contains(&ins.addr)
        {
            continue;
        }
        let mut out = Vec::new();
        if m == "JMP" {
            if let Some(target) = raw.branch_target(i) {
                out.push(target);
            } else if !raw.return_jumps.contains(&(i as u32)) {
                let table = program
                    .tables
                    .get(&(raw.addr, ins.addr))
                    .or_else(|| raw.recovered_tables.get(&ins.addr));
                match table {
                    Some(targets) => out.extend(targets.iter().copied()),
                    None => {
                        exits.insert(i as u32, Vec::new());
                        continue;
                    }
                }
            }
        } else if JCC.contains(&m) {
            if let Some(target) = raw.branch_target(i) {
                out.push(target);
            }
        }
        if m != "RET" && m != "JMP" && !(i + 1 < raw.decoded.len() && raw.contiguous[i]) {
            out.push(raw.fallthrough[i]);
        }
        let out: BTreeSet<Addr> = out
            .into_iter()
            .filter(|target| !raw.index.contains_key(target))
            .collect();
        if !out.is_empty() {
            exits.insert(i as u32, out.into_iter().collect());
        }
    }
    exits
}

const MAX_JUMP_TABLE: usize = 4096;

fn recover_tables(
    program: &Program,
    relocated: &BTreeMap<Addr, Addr>,
    raw: &RawFunction,
) -> BTreeMap<Addr, Vec<Addr>> {
    let mut tables = BTreeMap::new();
    for ins in &raw.decoded {
        if ins.mnemonic != "JMP"
            || branch_target(ins).is_some()
            || program.tables.contains_key(&(raw.addr, ins.addr))
        {
            continue;
        }
        let Some((None, Some(_), 4, displacement, None)) =
            ins.operands.first().and_then(mem)
        else {
            continue;
        };
        let Ok(table) = u32::try_from(displacement) else {
            continue;
        };
        if table == 0 || program.image.bytes(table, 4).is_none() {
            continue;
        }
        let relocated_run = relocated.contains_key(&table);
        let mut seen = BTreeSet::new();
        let mut targets = Vec::new();
        for slot in 0..MAX_JUMP_TABLE {
            let at = table.wrapping_add((slot as u32) * 4);
            let Some(target) = program.image.rd32(at) else {
                break;
            };
            if relocated_run && relocated.get(&at) != Some(&target) {
                break;
            }
            if !raw.index.contains_key(&target)
                && !program.functions.contains_key(&target)
                && !program.entries.contains_key(&target)
            {
                break;
            }
            if seen.insert(target) {
                targets.push(target);
            }
        }
        if !targets.is_empty() {
            tables.insert(ins.addr, targets);
        }
    }
    tables
}

fn table_jumps(raw: &RawFunction) -> BTreeSet<u32> {
    raw.decoded
        .iter()
        .enumerate()
        .filter(|(i, ins)| {
            ins.mnemonic == "JMP"
                && !ins.operands.is_empty()
                && !matches!(ins.operands[0].kind, OperandKind::Imm(_))
                && !raw.return_jumps.contains(&(*i as u32))
        })
        .map(|(i, _)| i as u32)
        .collect()
}

fn never_returns(program: &Program, ins: &DecodedInsn) -> bool {
    if ins.mnemonic != "CALL" {
        return false;
    }
    if program.noreturn_calls.contains(&ins.addr) {
        return true;
    }
    let Some(target) = branch_target(ins) else {
        return false;
    };
    program.is_noreturn(target)
}

fn popped_jump(raw: &RawFunction, i: usize) -> bool {
    let Some(ins) = raw.decoded.get(i) else {
        return false;
    };
    ins.mnemonic == "JMP"
        && ins.operands.first().and_then(gpr32).is_some()
        && i > 0
        && raw.contiguous[i - 1]
        && raw.decoded[i - 1].mnemonic == "POP"
        && raw.decoded[i - 1].operands.first().map(|o| &o.display) == ins.operands.first().map(|o| &o.display)
}

fn operand_size(ops: &[rk_decode::Operand], hint: Option<u16>) -> Option<u16> {
    ops.iter().find_map(|o| match o.kind {
        OperandKind::Reg { .. } => o.size,
        _ => None,
    })
    .or_else(|| ops.iter().find_map(|o| match o.kind {
        OperandKind::Mem { .. } => o.size,
        _ => None,
    }))
    .or(hint)
}

fn seh_chain_operand(op: &rk_decode::Operand, zero_base: u8) -> bool {
    mem(op).is_some_and(|(base, index, _scale, disp, seg)| {
        op.size == Some(32)
            && seg == Some("FS")
            && (base.is_none() || base == Some(zero_base))
            && index.is_none()
            && disp == 0
    })
}

fn writes_reg32(ins: &DecodedInsn, reg: u8) -> bool {
    if ins.operands.is_empty() {
        return false;
    }
    if ins.mnemonic.starts_with("LOOP") {
        return reg == R_ECX;
    }
    if matches!(ins.mnemonic.as_str(), "CMP" | "TEST" | "PUSH" | "JMP") || JCC.contains(&ins.mnemonic.as_str()) {
        return false;
    }
    match ins.operands[0].kind {
        OperandKind::Reg { gpr, .. } => gpr == reg,
        _ => true,
    }
}

fn seh_zero_base(raw: &RawFunction, i: usize, reg: Option<u8>) -> bool {
    let Some(reg) = reg else {
        return true;
    };
    if reg == R_ESP {
        return false;
    }
    let targets: BTreeSet<Addr> = raw
        .decoded
        .iter()
        .enumerate()
        .filter(|(_, ins)| JCC.contains(&ins.mnemonic.as_str()) || ins.mnemonic == "JMP")
        .filter_map(|(i, _)| raw.branch_target(i))
        .collect();
    for j in (i.saturating_sub(15)..i).rev() {
        if !raw.contiguous[j] || targets.contains(&raw.decoded[j + 1].addr) {
            return false;
        }
        let ins = &raw.decoded[j];
        if ins.mnemonic == "XOR" && ins.operands.len() == 2 {
            if let (Some(a), Some(b)) = (gpr32(&ins.operands[0]), gpr32(&ins.operands[1])) {
                if a == b && a == reg {
                    return true;
                }
            }
        }
        if !matches!(
            ins.mnemonic.as_str(),
            "MOV" | "LEA" | "POP" | "PUSH" | "ADD" | "SUB" | "AND" | "OR" | "XOR" | "TEST"
                | "CMP" | "NOP"
        ) || writes_reg32(ins, reg)
        {
            return false;
        }
    }
    false
}

fn seh_restore_sites(raw: &RawFunction) -> BTreeSet<u32> {
    let mut result = BTreeSet::new();
    for (i, ins) in raw.decoded.iter().enumerate() {
        if !matches!(ins.mnemonic.as_str(), "POP" | "MOV") || ins.operands.is_empty() {
            continue;
        }
        let dst = &ins.operands[0];
        let base = mem(dst).and_then(|(base, _, _, _, _)| base);
        if !seh_chain_operand(dst, base.unwrap_or(0)) || !seh_zero_base(raw, i, base) {
            continue;
        }
        if ins.mnemonic == "POP" {
            result.insert(i as u32);
        } else if ins.operands.len() == 2 {
            if let Some(src) = gpr32(&ins.operands[1]) {
                if src != R_ESP {
                    result.insert(i as u32);
                }
            }
        }
    }
    result
}

fn seh_frame_sites(raw: &RawFunction, image: &Image) -> BTreeMap<u32, Addr> {
    let mut sites = BTreeMap::new();
    if let Some(handler) = msvc_seh_prolog(image, raw.addr) {
        let publish = raw.addr + MSVC_SEH_PROLOG_LEN - 7;
        for (i, ins) in raw.decoded.iter().enumerate() {
            if ins.addr == publish {
                sites.insert(i as u32, handler);
            }
        }
    }
    if let Some(stub) = seh_constructor_helper(image, raw.addr) {
        for (i, ins) in raw.decoded.iter().enumerate() {
            if ins.mnemonic == "MOV" && ins.operands.len() == 2 {
                let dst = &ins.operands[0];
                let src = &ins.operands[1];
                if seh_chain_operand(dst, 2)
                    && mem(dst).and_then(|(base, _, _, _, _)| base) == Some(R_EDX)
                    && gpr32(src) == Some(R_ECX)
                {
                    sites.insert(i as u32, stub);
                }
            }
        }
        for (i, ins) in raw.decoded.iter().enumerate() {
            if i == 0 || ins.mnemonic != "CALL" || !raw.contiguous[i - 1] {
                continue;
            }
            let Some(target) = raw.branch_target(i) else {
                continue;
            };
            let reserve = &raw.decoded[i - 1];
            if !matches!(reserve.mnemonic.as_str(), "ADD" | "SUB") || reserve.operands.len() != 2 {
                continue;
            }
            let dst = &reserve.operands[0];
            let amount = &reserve.operands[1];
            if gpr32(dst) != Some(R_ESP)
                || imm(amount).is_none_or(|v| {
                    v as u32 != if reserve.mnemonic == "ADD" { 0xffff_fff0 } else { 16 }
                })
            {
                continue;
            }
            if let Some(stub) = seh_constructor_helper(image, target) {
                sites.insert(i as u32, stub);
            }
        }
    }
    for i in 2..raw.decoded.len() {
        let mov = &raw.decoded[i];
        let push = &raw.decoded[i - 1];
        if mov.mnemonic != "MOV" || push.mnemonic != "PUSH" || mov.operands.len() != 2 {
            continue;
        }
        let dst = &mov.operands[0];
        let src = &mov.operands[1];
        let dst_base = mem(dst).and_then(|(base, _, _, _, _)| base);
        let zero_base = if seh_zero_base(raw, i, dst_base) { dst_base.unwrap_or(0) } else { 0 };
        if !seh_chain_operand(dst, zero_base)
            || gpr32(src) != Some(R_ESP)
            || !raw.contiguous[i - 1]
        {
            continue;
        }
        let Some(pushed) = push.operands.first() else {
            continue;
        };
        let mut handler_index = i as isize - 2;
        if !seh_chain_operand(pushed, zero_base) {
            let load = &raw.decoded[i - 2];
            let Some(pushed_reg) = gpr32(pushed) else {
                continue;
            };
            if pushed_reg == R_ESP || load.mnemonic != "MOV" || load.operands.len() != 2 || !raw.contiguous[i - 2] {
                continue;
            }
            let loaded = &load.operands[0];
            let head = &load.operands[1];
            let Some(loaded_reg) = gpr32(loaded) else {
                continue;
            };
            if loaded_reg != pushed_reg || loaded_reg == R_ESP {
                continue;
            }
            let mut head = head.clone();
            if head.size.is_none() && matches!(head.kind, OperandKind::Mem { .. }) {
                head.size = loaded.size;
            }
            if !seh_chain_operand(&head, zero_base) {
                continue;
            }
            handler_index -= 1;
        }
        for j in [handler_index, handler_index - 1] {
            if j < 0 {
                continue;
            }
            let prev = &raw.decoded[j as usize];
            if !raw.contiguous[j as usize] {
                break;
            }
            if prev.mnemonic == "PUSH" {
                if let Some(value) = prev.operands.first().and_then(imm) {
                    sites.insert(i as u32, value as Addr);
                }
                break;
            }
        }
    }
    sites
}

fn seh_except_entry(ins: &DecodedInsn) -> bool {
    if ins.mnemonic != "MOV" || ins.operands.len() != 2 || ins.operands[0].display != "ESP" {
        return false;
    }
    mem(&ins.operands[1]).is_some_and(|(base, index, _scale, disp, seg)| {
        base == Some(R_EBP)
            && index.is_none()
            && seg.is_none()
            && disp as u32 == 0xffff_ffe8
    })
}

fn register_constants(raw: &RawFunction, program: &Program) -> BTreeMap<u32, BTreeMap<u8, u32>> {
    let mut before: BTreeMap<u32, BTreeMap<u8, u32>> = BTreeMap::new();
    let Some(root) = raw.idx(raw.addr) else {
        return before;
    };
    before.insert(root, BTreeMap::new());
    let mut work = vec![root];
    while let Some(i) = work.pop() {
        let ins = &raw.decoded[i as usize];
        let mut state = before.get(&i).cloned().unwrap_or_default();
        let mnem = ins.mnemonic.as_str();
        if mnem == "CALL" {
            for reg in [R_EAX, R_ECX, R_EDX] {
                state.remove(&reg);
            }
        } else if EXPLICIT_DESTINATION.contains(&mnem)
            || (JCC.contains(&mnem) && !mnem.starts_with("LOOP"))
            || mnem.starts_with('F')
        {
            let first = ins.operands.first();
            let second = ins.operands.get(1);
            if first.is_none() || matches!(first.map(|o| &o.kind), Some(OperandKind::Other(_))) {
                state.clear();
            } else if let Some(reg) = first.and_then(gpr32) {
                if !matches!(mnem, "PUSH" | "CMP" | "TEST") {
                    state.remove(&reg);
                    if first.is_some_and(|o| o.size == Some(32)) {
                        if mnem == "XOR" {
                            if second.and_then(gpr32) == Some(reg) {
                                state.insert(reg, 0);
                            }
                        } else if mnem == "MOV" {
                            if let Some(value) = second.and_then(imm) {
                                state.insert(reg, value as u32);
                            }
                        }
                    }
                }
            }
        } else {
            state.clear();
        }
        for j in raw.successors(program, i as usize) {
            let joined = if let Some(old) = before.get(&j) {
                state
                    .iter()
                    .filter(|(reg, value)| old.get(reg) == Some(value))
                    .map(|(reg, value)| (*reg, *value))
                    .collect()
            } else {
                state.clone()
            };
            if before.get(&j) != Some(&joined) {
                before.insert(j, joined);
                work.push(j);
            }
        }
    }
    before
}

fn eh3_trylevels(raw: &RawFunction, program: &Program) -> Option<BTreeSet<u32>> {
    let mut levels = BTreeSet::new();
    let mut constants: Option<BTreeMap<u32, BTreeMap<u8, u32>>> = None;
    for (i, ins) in raw.decoded.iter().enumerate() {
        if ins.operands.is_empty() || matches!(ins.mnemonic.as_str(), "CMP" | "TEST" | "PUSH") {
            continue;
        }
        let dst = &ins.operands[0];
        let src = ins.operands.get(1);
        let Some((base, index, _scale, disp, seg)) = mem(dst) else {
            continue;
        };
        if base != Some(R_EBP) || index.is_some() || seg.is_some() || disp as u32 != 0xffff_fffc {
            continue;
        }
        let value = src.and_then(imm).map(|v| v as u32);
        let source_reg = src.and_then(gpr32);
        if ins.mnemonic == "MOV" && source_reg.is_some() {
            let constants = constants.get_or_insert_with(|| register_constants(raw, program));
            let value = constants.get(&(i as u32)).and_then(|s| s.get(&source_reg.unwrap()))?;
            levels.insert(*value);
        } else if ins.mnemonic == "MOV" && value.is_some() {
            levels.insert(value.unwrap());
        } else if ins.mnemonic == "AND" && value == Some(0) {
            levels.insert(0);
        } else if ins.mnemonic == "OR" && value == Some(0xffff_ffff) {
            levels.insert(value.unwrap());
        } else {
            return None;
        }
    }
    Some(levels)
}

pub fn dump(fir: &FunctionIr, out: &mut impl Write) -> Result<()> {
    let lifter = Lifter::new()?;
    writeln!(out, "== function {:08x}", fir.addr).map_err(|e| Error::input(e.to_string()))?;
    for (i, ins) in fir.insns.iter().enumerate() {
        writeln!(out, "{}", ins.text).map_err(|e| Error::input(e.to_string()))?;
        for op in &ins.ops {
            let lhs = op
                .output
                .as_ref()
                .map(|v| format!("{} = ", format_varnode(v, &lifter)))
                .unwrap_or_default();
            let inputs: Vec<String> = op
                .inputs
                .iter()
                .map(|v| format_varnode(v, &lifter))
                .collect();
            let suffix = op
                .userop
                .map(|id| format!(" userop={id}"))
                .unwrap_or_default();
            writeln!(out, "  {lhs}{:?} {}{suffix}", op.opcode, inputs.join(", "))
                .map_err(|e| Error::input(e.to_string()))?;
        }
        let next = fir.succ.get(i).cloned().unwrap_or_default();
        if !next.is_empty() {
            let next: Vec<String> = next.iter().map(|n| format!("{n}")).collect();
            writeln!(out, "    -> {}", next.join(",")).map_err(|e| Error::input(e.to_string()))?;
        }
        if let Some(exits) = fir.exits.get(&(i as u32)) {
            let exits: Vec<String> = exits.iter().map(|a| format!("{a:08x}")).collect();
            writeln!(out, "    exit {}", exits.join(",")).map_err(|e| Error::input(e.to_string()))?;
        }
    }
    Ok(())
}
