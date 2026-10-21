use std::collections::{BTreeMap, BTreeSet};

use rk_core::contract::Fields;
use rk_core::ir::{FunctionIr, Insn, OpCode, PcodeOp, SehEffect, Space, Varnode};
use rk_core::program::Image;
use rk_core::{Addr, Diagnostic, Reason};

use crate::op::{Op, SlowFamily, SlowInsn, StringOp, X87Recipes};
use crate::regs::Registers;
use crate::string;

#[derive(Clone, Debug)]
pub struct CgOp {
    pub op: Op,
    pub output: Option<Varnode>,
    pub inputs: Vec<Varnode>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlagShape {
    Add,
    Sub,
    Cmp,
    Inc,
    Dec,
    Adc,
    Sbb,
    Logic,
}

#[derive(Clone, Debug)]
pub struct CgInsn {
    pub insn: u32,
    pub addr: Addr,
    pub length: u8,
    pub ops: Vec<CgOp>,
    pub x87: bool,
    pub flag: Option<FlagShape>,
    pub flag_operands: Option<(Varnode, Varnode)>,
}

#[derive(Clone, Debug)]
pub struct CgFunction {
    pub addr: Addr,
    pub insns: Vec<CgInsn>,
    pub succ: Vec<Vec<u32>>,
    pub exits: BTreeMap<u32, Vec<Addr>>,
    pub entries: Vec<Addr>,
    pub tables: BTreeSet<u32>,
    pub dynamic: BTreeSet<u32>,
    pub noreturn: BTreeMap<u32, Addr>,
    pub import_tails: BTreeMap<u32, Addr>,
    pub seh: BTreeMap<u32, Vec<SehEffect>>,
    pub slow: Vec<SlowInsn>,
    pub diagnostics: Vec<Diagnostic>,
}

impl CgFunction {
    pub fn index_of(&self, addr: Addr) -> Option<u32> {
        self.insns.binary_search_by_key(&addr, |insn| insn.addr).ok().map(|n| n as u32)
    }
}

const SUPPORTED: &[&str] = &[
    "MOV", "MOVSX", "MOVZX", "XCHG", "NOT", "LEAVE", "LEA", "PUSH", "POP", "RET", "NOP", "ADD",
    "SUB", "CMP", "INC", "DEC", "ADC", "SBB", "SHL", "SHR", "SAR", "DIV", "IDIV", "NEG", "MUL",
    "IMUL", "CDQ", "CWD", "CBW", "CWDE", "CLD", "STD", "SAHF", "LAHF", "TEST", "AND", "OR", "XOR",
    "ROL", "ROR", "RCL", "RCR", "SHLD", "SHRD", "JMP", "JZ", "JNZ",
    "JE", "JNE", "JA", "JAE", "JB", "JBE", "JC", "JNC", "JG", "JGE", "JL", "JLE", "JS", "JNS",
    "JO", "JNO", "JP", "JNP", "JPE", "JPO", "CALL", "SETA", "SETAE", "SETB", "SETBE", "SETC",
    "SETE", "SETG", "SETGE", "SETL", "SETLE", "SETNA", "SETNAE", "SETNB", "SETNBE", "SETNC",
    "SETNE", "SETNG", "SETNGE", "SETNL", "SETNLE", "SETNO", "SETNP", "SETNS", "SETNZ", "SETO",
    "SETP", "SETPE", "SETPO", "SETS", "SETZ",
    "XLAT", "BT", "BTS", "BTR", "BTC", "CLC", "STC", "CMC", "SALC", "AAA", "AAS", "AAM", "AAD",
    "DAA", "DAS", "ARPL", "PUSHAD", "POPAD", "LOOP", "LOOPE", "LOOPNE", "JECXZ",
];

fn is_media(mnemonic: &str) -> bool {
    matches!(
        mnemonic,
        "MOVD" | "MOVQ" | "EMMS" | "PXOR" | "PADDW" | "PADDD" | "PSRLW" | "PUNPCKLBW"
            | "PACKUSWB" | "PMADDWD" | "PMULHW" | "PSUBSW" | "PSHUFW" | "ORPS"
    )
}

fn call_other_family(mnemonic: &str, userop: Option<u64>) -> SlowFamily {
    if is_media(mnemonic) {
        return SlowFamily::Mmx;
    }
    match userop {
        Some(1) => SlowFamily::In,
        Some(2) => SlowFamily::Out,
        Some(16) => SlowFamily::Int,
        Some(44) => SlowFamily::Cpuid,
        Some(63) => SlowFamily::Segment,
        Some(72) => SlowFamily::Rdtsc,
        _ => SlowFamily::CallOther,
    }
}

fn unmodelled_family(mnemonic: &str) -> SlowFamily {
    if is_media(mnemonic) {
        return SlowFamily::Mmx;
    }
    match mnemonic {
        "PUSHFD" | "POPFD" => SlowFamily::Flags,
        "LDS" | "LES" | "SLDT" => SlowFamily::Segment,
        "HLT" => SlowFamily::Hlt,
        "CLI" => SlowFamily::Cli,
        "STI" => SlowFamily::Sti,
        "INT" | "INT1" | "INTO" => SlowFamily::Int,
        "IRET" => SlowFamily::Iret,
        "RETF" => SlowFamily::Retf,
        "BOUND" => SlowFamily::Bound,
        "RDTSC" => SlowFamily::Rdtsc,
        "CPUID" => SlowFamily::Cpuid,
        "IN" | "INSB" | "INSD" => SlowFamily::In,
        "OUT" | "OUTSB" | "OUTSD" => SlowFamily::Out,
        _ => SlowFamily::Unmodelled,
    }
}

struct Lowerer<'a> {
    regs: &'static Registers,
    image: &'a Image,
    x87: &'a dyn X87Recipes,
    unique: u64,
    slow: Vec<SlowInsn>,
    diagnostics: Vec<Diagnostic>,
}

pub fn lower(fir: &FunctionIr, image: &Image, x87: &dyn X87Recipes) -> rk_core::Result<CgFunction> {
    let mut lowerer = Lowerer {
        regs: Registers::get(),
        image,
        x87,
        unique: 0x8000_0000,
        slow: Vec::new(),
        diagnostics: Vec::new(),
    };
    let mut insns = Vec::with_capacity(fir.insns.len());
    for (index, insn) in fir.insns.iter().enumerate() {
        let (ops, is_x87, flag_operands) = lowerer.lower_insn(fir, index as u32, insn)?;
        let flag = if is_x87 {
            None
        } else {
            match insn.mnemonic.to_ascii_uppercase().as_str() {
                "ADD" => Some(FlagShape::Add),
                "SUB" => Some(FlagShape::Sub),
                "CMP" => Some(FlagShape::Cmp),
                "INC" => Some(FlagShape::Inc),
                "DEC" => Some(FlagShape::Dec),
                "ADC" => Some(FlagShape::Adc),
                "SBB" => Some(FlagShape::Sbb),
                "AND" | "OR" | "XOR" | "TEST" => Some(FlagShape::Logic),
                _ => None,
            }
        };
        insns.push(CgInsn {
            insn: index as u32,
            addr: insn.addr,
            length: insn.length,
            ops,
            x87: is_x87,
            flag,
            flag_operands,
        });
    }
    Ok(CgFunction {
        addr: fir.addr,
        insns,
        succ: fir.succ.clone(),
        exits: fir.exits.clone(),
        entries: fir.entries.clone(),
        tables: fir.tables.clone(),
        dynamic: fir.dynamic.clone(),
        noreturn: fir.noreturn.clone(),
        import_tails: fir.import_tails.clone(),
        seh: fir.seh.clone(),
        slow: lowerer.slow,
        diagnostics: lowerer.diagnostics,
    })
}

impl Lowerer<'_> {
    fn fresh(&mut self, size: u32) -> Varnode {
        let out = Varnode::new(Space::Unique, self.unique, size);
        self.unique += 0x10;
        out
    }

    fn slow(&mut self, insn: &Insn, family: SlowFamily) -> Op {
        let index = self.slow.len() as u32;
        self.slow.push(SlowInsn { addr: insn.addr, length: insn.length, family });
        Op::Slow(index)
    }

    fn reject(&mut self, insn: &Insn, reason: Reason, family: SlowFamily) -> Op {
        self.diagnostics.push(Diagnostic::new(insn.addr, reason));
        self.slow(insn, family)
    }

    fn lower_insn(
        &mut self,
        fir: &FunctionIr,
        index: u32,
        insn: &Insn,
    ) -> rk_core::Result<(Vec<CgOp>, bool, Option<(Varnode, Varnode)>)> {
        let mnem = insn.mnemonic.to_ascii_uppercase();
        if mnem == "WAIT" || mnem == "NOP" || mnem == "PAUSE" {
            return Ok((Vec::new(), false, None));
        }
        if mnem == "INT3" {
            return Ok((vec![cg(Op::Breakpoint)], false, None));
        }
        if insn.x87 || mnem.starts_with('F') {
            let mut unique = self.unique;
            let lowering = {
                let mut fresh = |size| {
                    let out = Varnode::new(Space::Unique, unique, size);
                    unique += 0x10;
                    out
                };
                self.x87.lower(insn, self.image, &mut fresh)
            };
            self.unique = unique;
            if let Some(lowering) = lowering {
                let ops = lowering
                    .recipes
                    .into_iter()
                    .map(|recipe| CgOp { op: recipe.op, output: recipe.output, inputs: recipe.inputs })
                    .collect();
                return Ok((ops, true, None));
            }
            let op = self.slow(insn, SlowFamily::X87);
            return Ok((vec![cg(op)], true, None));
        }
        let raw = self.image.bytes(insn.addr, insn.length as usize).unwrap_or(&[]);
        if let Some(helper) = string::helper(raw) {
            return Ok((vec![cg(Op::String(StringOp(helper)))], false, None));
        }
        if let Some(slot) = fir.import_tails.get(&index) {
            return Ok((vec![cg(Op::ImportTail { slot: *slot })], false, None));
        }
        let mut stripped = Vec::new();
        let raw_ops: &[PcodeOp] = if insn
            .ops
            .iter()
            .any(|op| op.opcode == OpCode::CallOther && matches!(op.userop, Some(17 | 18)))
        {
            stripped.extend(
                insn.ops
                    .iter()
                    .filter(|op| !(op.opcode == OpCode::CallOther && matches!(op.userop, Some(17 | 18))))
                    .cloned(),
            );
            &stripped
        } else {
            &insn.ops
        };
        if let Some(call) = raw_ops.iter().find(|op| op.opcode == OpCode::CallOther) {
            let family = call_other_family(&mnem, call.userop);
            let op = self.slow(insn, family);
            let mut out = vec![cg(op)];
            self.add_tail(fir, index, &mut out);
            return Ok((out, false, None));
        }
        let divide_by_zero = matches!(mnem.as_str(), "AAM")
            && self.image.bytes(insn.addr, insn.length as usize).and_then(|b| b.last()) == Some(&0);
        if divide_by_zero || !SUPPORTED.contains(&mnem.as_str()) {
            let family = unmodelled_family(&mnem);
            let op = self.reject(
                insn,
                Reason::Unmodelled { mnemonic: mnem.clone() },
                family,
            );
            return Ok((vec![cg(op)], false, None));
        }
        let ops = match normalize_direct_ram(raw_ops, &mut || self.fresh(4)) {
            Ok(ops) => ops,
            Err(reason) => {
                let op = self.reject(insn, reason, SlowFamily::Unmodelled);
                let mut out = vec![cg(op)];
                self.add_tail(fir, index, &mut out);
                return Ok((out, false, None));
            }
        };
        if mnem == "DIV" || mnem == "IDIV" {
            let (mut converted, has_address) = match correct_div_cg(
                &mnem,
                &ops,
                self.regs,
                insn.addr,
                &mut || self.fresh(8),
            ) {
                Ok(ops) => (ops, true),
                Err(reason) => {
                    let op = self.reject(insn, reason, SlowFamily::Unmodelled);
                    (vec![cg(op)], false)
                }
            };
            if has_address {
                if let Some(kinds) = fir.seh.get(&index) {
                    apply_seh(&mut converted, kinds, insn.addr);
                }
                if let Some(target) = fir.noreturn.get(&index) {
                    converted.push(cg(Op::Trap(*target)));
                }
                self.add_tail(fir, index, &mut converted);
                return Ok((converted, false, None));
            }
            self.add_tail(fir, index, &mut converted);
            return Ok((converted, false, None));
        }
        let rewrites_memory = matches!(
            mnem.as_str(),
            "ADD" | "SUB" | "CMP" | "INC" | "DEC" | "ADC" | "SBB" | "SHL" | "SHR" | "SAR"
                | "NEG" | "AND" | "OR" | "XOR" | "NOT" | "ROL" | "ROR" | "RCL" | "RCR" | "SHLD"
                | "SHRD"
        ) && ops.iter().any(|op| op.opcode == OpCode::Store);
        let prepared = if rewrites_memory {
            prepare_rmw(&ops, &mut || self.fresh(4))
        } else {
            Ok(ops.clone())
        };
        let mut flag_operands = None;
        let corrected = prepared.and_then(|ops| {
            let corrected = match mnem.as_str() {
                "ADD" | "SUB" | "CMP" | "INC" | "DEC" | "ADC" | "SBB" => {
                    correct_arithmetic(
                        &mnem,
                        &ops,
                        self.regs,
                        &mut || self.fresh(4),
                        &mut flag_operands,
                    )
                }
                "SHL" | "SHR" => correct_shift(&mnem, &ops, self.regs, &mut || self.fresh(4)),
                "SAR" => correct_sar(&ops, self.regs, &mut || self.fresh(4)),
                "NEG" => correct_neg(&ops, self.regs, &mut || self.fresh(4)),
                "AND" | "OR" | "XOR" | "TEST" => Ok(with_af_zero(&ops, self.regs)),
                _ => Ok(ops.clone()),
            }?;
            Ok(if rewrites_memory {
                defer_flags(corrected, self.regs, &mut || self.fresh(4))
            } else {
                corrected
            })
        });
        let ops = match corrected {
            Ok(ops) => ops,
            Err(reason) => {
                let family = match mnem.as_str() {
                    "ADC" => SlowFamily::Adc,
                    "SBB" => SlowFamily::Sbb,
                    "ROL" => SlowFamily::Rol,
                    "ROR" => SlowFamily::Ror,
                    "RCL" => SlowFamily::Rcl,
                    "RCR" => SlowFamily::Rcr,
                    "SHLD" => SlowFamily::Shld,
                    "SHRD" => SlowFamily::Shrd,
                    _ => SlowFamily::Unmodelled,
                };
                let op = self.reject(insn, reason, family);
                let mut out = vec![cg(op)];
                self.add_tail(fir, index, &mut out);
                return Ok((out, false, None));
            }
        };
        let mut converted: Vec<CgOp> = ops.into_iter().map(from_pcode).collect();
        if let Some(kinds) = fir.seh.get(&index) {
            apply_seh(&mut converted, kinds, insn.addr);
        }
        if let Some(target) = fir.noreturn.get(&index) {
            converted.push(cg(Op::Trap(*target)));
        }
        self.add_tail(fir, index, &mut converted);
        Ok((converted, false, flag_operands))
    }

    fn add_tail(&self, fir: &FunctionIr, index: u32, out: &mut Vec<CgOp>) {
        if fir.succ.get(index as usize).is_some_and(|s| !s.is_empty()) {
            return;
        }
        if let Some(targets) = fir.exits.get(&index) {
            for target in targets {
                out.push(cg(Op::Tail { target: *target }));
            }
        }
    }
}

fn cg(op: Op) -> CgOp {
    CgOp { op, output: None, inputs: Vec::new() }
}

fn from_pcode(op: PcodeOp) -> CgOp {
    match op.opcode {
        OpCode::Branch => {
            let target = op.inputs.first().and_then(target_addr);
            CgOp {
                op: target.map_or(Op::P(OpCode::Branch), |target| Op::Branch { target }),
                output: op.output,
                inputs: Vec::new(),
            }
        }
        OpCode::CBranch => {
            let target = op.inputs.first().and_then(target_addr);
            let inputs = if target.is_some() { op.inputs.get(1..).unwrap_or(&[]).to_vec() } else { op.inputs };
            CgOp {
                op: target.map_or(Op::P(OpCode::CBranch), |target| Op::CBranch { target }),
                output: op.output,
                inputs,
            }
        }
        OpCode::Call => {
            let target = op.inputs.first().and_then(target_addr);
            CgOp {
                op: target.map_or(Op::P(OpCode::Call), |target| Op::Call { target }),
                output: op.output,
                inputs: Vec::new(),
            }
        }
        OpCode::CallInd => CgOp { op: Op::CallInd, output: op.output, inputs: op.inputs },
        OpCode::Return => CgOp { op: Op::Return, output: op.output, inputs: Vec::new() },
        OpCode::BranchInd => CgOp { op: Op::BranchInd, output: op.output, inputs: op.inputs },
        OpCode::CallOther => CgOp { op: Op::CallOther(op.userop.unwrap_or(0)), output: op.output, inputs: op.inputs },
        other => CgOp { op: Op::P(other), output: op.output, inputs: op.inputs },
    }
}

fn target_addr(v: &Varnode) -> Option<Addr> {
    match v.space {
        Space::Const | Space::Ram => Some(v.offset as Addr),
        _ => None,
    }
}

fn is_control(op: &PcodeOp) -> bool {
    matches!(
        op.opcode,
        OpCode::Branch
            | OpCode::CBranch
            | OpCode::Call
            | OpCode::CallInd
            | OpCode::BranchInd
            | OpCode::CallOther
    )
}

fn normalize_direct_ram(
    ops: &[PcodeOp],
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<PcodeOp>, Reason> {
    let mut ops = ops.to_vec();
    if let Some(index) = ops.iter().position(|op| {
        op.opcode == OpCode::BranchInd
            && op.inputs.first().is_some_and(|v| v.space == Space::Ram)
    }) {
        let target = ops[index].inputs[0];
        let unique = fresh();
        let unique = Varnode::new(unique.space, unique.offset, target.size);
        ops[index].inputs[0] = unique;
        ops.insert(
            index,
            PcodeOp {
                opcode: OpCode::Load,
                output: Some(unique),
                inputs: vec![Varnode::constant(target.offset, 4)],
                userop: None,
            },
        );
    }
    let touched: Vec<usize> = ops
        .iter()
        .enumerate()
        .filter(|(_, op)| {
            !is_control(op)
                && std::iter::once(op.output.as_ref())
                    .chain(op.inputs.iter().map(Some))
                    .flatten()
                    .any(|v| v.space == Space::Ram)
        })
        .map(|(index, _)| index)
        .collect();
    if touched.is_empty() {
        return Ok(ops);
    }
    let mut operands = BTreeSet::new();
    for &index in &touched {
        let op = &ops[index];
        for v in std::iter::once(op.output.as_ref()).chain(op.inputs.iter().map(Some)).flatten() {
            if v.space == Space::Ram {
                operands.insert((v.offset, v.size));
            }
        }
    }
    if operands.len() != 1 {
        return Err(Reason::UnsupportedDirectRam { detail: "multiple direct-ram operands".into() });
    }
    let (offset, size) = operands.pop_first().unwrap();
    let address = Varnode::constant(offset, 4);
    let has_write = touched.iter().any(|&i| ops[i].output.is_some_and(|v| v.space == Space::Ram));
    let has_read = touched
        .iter()
        .any(|&i| ops[i].inputs.iter().any(|v| v.space == Space::Ram));
    if has_write && has_read {
        let writes = touched.iter().filter(|&&i| ops[i].output.is_some_and(|v| v.space == Space::Ram)).count();
        if writes != 1 {
            return Err(Reason::UnsupportedDirectRam {
                detail: "unsupported direct-ram read-modify-write".into(),
            });
        }
        let mut operand = fresh();
        operand = Varnode::new(operand.space, operand.offset, size);
        let mut out = Vec::with_capacity(ops.len() + 2);
        for mut op in ops {
            if is_control(&op) {
                out.push(op);
                continue;
            }
            let mut changed = false;
            for input in &mut op.inputs {
                if input.space == Space::Ram {
                    *input = operand;
                    changed = true;
                }
            }
            if changed {
                out.push(PcodeOp {
                    opcode: OpCode::Load,
                    output: Some(operand),
                    inputs: vec![address],
                    userop: None,
                });
            }
            if op.output.is_some_and(|v| v.space == Space::Ram) {
                op.output = Some(operand);
                out.push(op);
                out.push(PcodeOp {
                    opcode: OpCode::Store,
                    output: None,
                    inputs: vec![address, operand],
                    userop: None,
                });
            } else {
                out.push(op);
            }
        }
        return Ok(out);
    }
    if has_write {
        if let Some(index) = touched
            .iter()
            .copied()
            .find(|&i| ops[i].output.is_some_and(|v| v.space == Space::Ram))
        {
            let mut dst = fresh();
            dst = Varnode::new(dst.space, dst.offset, size);
            let output = ops[index].output.replace(dst);
            let _ = output;
            ops.insert(
                index + 1,
                PcodeOp {
                    opcode: OpCode::Store,
                    output: None,
                    inputs: vec![address, dst],
                    userop: None,
                },
            );
        }
        return Ok(ops);
    }
    let mut src = fresh();
    src = Varnode::new(src.space, src.offset, size);
    let first = touched[0];
    let mut out = Vec::with_capacity(ops.len() + 1);
    for (index, mut op) in ops.into_iter().enumerate() {
        if index == first {
            out.push(PcodeOp {
                opcode: OpCode::Load,
                output: Some(src),
                inputs: vec![address],
                userop: None,
            });
        }
        if !is_control(&op) {
            for input in &mut op.inputs {
                if input.space == Space::Ram {
                    *input = src;
                }
            }
        }
        out.push(op);
    }
    Ok(out)
}

fn flag(regs: &Registers, name: &str) -> Varnode {
    regs.require(name)
}
fn copy(output: Varnode, input: Varnode) -> PcodeOp {
    PcodeOp { opcode: OpCode::Copy, output: Some(output), inputs: vec![input], userop: None }
}
fn op(opcode: OpCode, output: Varnode, inputs: Vec<Varnode>) -> PcodeOp {
    PcodeOp { opcode, output: Some(output), inputs, userop: None }
}

fn with_af_zero(ops: &[PcodeOp], regs: &Registers) -> Vec<PcodeOp> {
    let mut out = ops.to_vec();
    out.push(copy(flag(regs, "AF"), Varnode::constant(0, 1)));
    out
}

fn prepare_rmw(
    ops: &[PcodeOp],
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<PcodeOp>, Reason> {
    let stores: Vec<usize> = ops.iter().enumerate()
        .filter(|(_, op)| op.opcode == OpCode::Store)
        .map(|(index, _)| index)
        .collect();
    if stores.is_empty() {
        return Ok(ops.to_vec());
    }
    let loads: Vec<usize> = ops.iter().enumerate()
        .filter(|(_, op)| op.opcode == OpCode::Load)
        .map(|(index, _)| index)
        .collect();
    if stores.len() != 1 || loads.is_empty() {
        return Err(Reason::UnsupportedReadModifyWrite {
            detail: "unsupported memory operand shape".into(),
        });
    }
    let load = &ops[loads[0]];
    if loads.iter().any(|&index| {
        ops[index].inputs != load.inputs || ops[index].output != load.output
    }) {
        return Err(Reason::UnsupportedReadModifyWrite {
            detail: "repeated memory operand loads disagree".into(),
        });
    }
    let store = &ops[stores[0]];
    let loaded = load.output.ok_or_else(|| Reason::UnsupportedReadModifyWrite {
        detail: "memory load without an output".into(),
    })?;
    if store.inputs.len() != 2
        || store.inputs[0] != load.inputs[0]
        || store.inputs[1].size != loaded.size
    {
        return Err(Reason::UnsupportedReadModifyWrite {
            detail: "read-modify-write addresses disagree".into(),
        });
    }
    let mut old = fresh();
    old = Varnode::new(old.space, old.offset, loaded.size);
    let mut stored = fresh();
    stored = Varnode::new(stored.space, stored.offset, store.inputs[1].size);
    let mut out = Vec::with_capacity(ops.len() + 2);
    let mut seen_load = false;
    let mut seen_store = false;
    for op in ops {
        match op.opcode {
            OpCode::Load => {
                if !seen_load {
                    out.push(op.clone());
                    out.push(copy(old, op.output.unwrap()));
                    seen_load = true;
                } else {
                    out.push(copy(op.output.unwrap(), if seen_store { stored } else { old }));
                }
            }
            OpCode::Store => {
                out.push(copy(stored, op.inputs[1]));
                out.push(op.clone());
                seen_store = true;
            }
            _ => out.push(op.clone()),
        }
    }
    Ok(out)
}

fn defer_flags(ops: Vec<PcodeOp>, regs: &Registers, fresh: &mut dyn FnMut() -> Varnode) -> Vec<PcodeOp> {
    let mut names = BTreeMap::new();
    let mut deferred = Vec::new();
    let mut out = Vec::with_capacity(ops.len() + 8);
    for op in ops {
        let mut op = op;
        for input in &mut op.inputs {
            if let Some(&replacement) = names.get(input) {
                *input = replacement;
            }
        }
        if let Some(output) = op.output {
            let is_flag = output.space == Space::Register
                && ["CF", "PF", "AF", "ZF", "SF", "OF"].iter().any(|name| {
                    let flag = regs.require(name);
                    output.offset == flag.offset && output.size == flag.size
                });
            if is_flag {
                let mut replacement = fresh();
                replacement = Varnode::new(replacement.space, replacement.offset, output.size);
                op.output = Some(replacement);
                deferred.push((output, replacement));
                names.insert(output, replacement);
            }
        }
        out.push(op);
    }
    out.extend(deferred.into_iter().map(|(register, value)| copy(register, value)));
    out
}

fn correct_arithmetic(
    mnem: &str,
    ops: &[PcodeOp],
    regs: &Registers,
    fresh: &mut dyn FnMut() -> Varnode,
    flag_operands: &mut Option<(Varnode, Varnode)>,
) -> Result<Vec<PcodeOp>, Reason> {
    let mut ops = ops.to_vec();
    let (primary_index, a, b, result, insert) = if mnem == "ADC" || mnem == "SBB" {
        let opcode = if mnem == "ADC" { OpCode::IntAdd } else { OpCode::IntSub };
        let primary = ops
            .iter()
            .rposition(|op| {
                op.opcode == opcode
                    && op.output.is_some_and(|v| matches!(v.space, Space::Register | Space::Unique))
            })
            .ok_or_else(|| Reason::UnsupportedArithmetic { mnemonic: mnem.into() })?;
        let result = ops[primary].output.unwrap();
        let first_cf = ops
            .iter()
            .position(|op| op.output == Some(flag(regs, "CF")))
            .ok_or_else(|| Reason::UnsupportedArithmetic { mnemonic: mnem.into() })?;
        let expected = if mnem == "ADC" { OpCode::IntCarry } else { OpCode::IntLess };
        let inputs = if ops[first_cf].opcode == expected {
            ops[first_cf].inputs.clone()
        } else if ops[first_cf].opcode == OpCode::Copy
            && ops[first_cf].inputs == [Varnode::constant(0, 1)]
        {
            vec![result, result]
        } else {
            return Err(Reason::UnsupportedArithmetic { mnemonic: mnem.into() });
        };
        if inputs.len() < 2 {
            return Err(Reason::UnsupportedArithmetic { mnemonic: mnem.into() });
        }
        let mut a = fresh();
        a = Varnode::new(a.space, a.offset, result.size);
        let mut b = fresh();
        b = Varnode::new(b.space, b.offset, result.size);
        *flag_operands = Some((a, b));
        ops.splice(first_cf..first_cf, [copy(a, inputs[0]), copy(b, inputs[1])]);
        (primary + 2, a, b, result, first_cf + 3)
    } else {
        let opcode = if mnem == "ADD" || mnem == "INC" { OpCode::IntAdd } else { OpCode::IntSub };
        let Some(primary) = ops.iter().position(|op| {
            op.opcode == opcode
                && op.output.is_some_and(|v| {
                    v.space == Space::Register || v.space == Space::Unique
                })
        }) else {
            let is_flag = |v: Varnode| {
                ["CF", "PF", "AF", "ZF", "SF", "OF"].iter().any(|name| {
                    let flag = regs.require(name);
                    v.space == Space::Register && v.offset == flag.offset && v.size == flag.size
                })
            };
            let zero_result = ops.iter().any(|op| {
                op.opcode == OpCode::Copy
                    && op.inputs.first().is_some_and(|v| {
                        v.space == Space::Const && v.offset == 0
                    })
                    && op.output.is_some_and(|v| v.space == Space::Register && !is_flag(v))
            });
            if (mnem == "SUB" || mnem == "CMP") && zero_result {
                let mut out = ops.to_vec();
                let af = regs.require("AF");
                if !out.iter().any(|op| op.output == Some(af)) {
                    out.push(copy(af, Varnode::constant(0, 1)));
                }
                return Ok(out);
            }
            return Err(Reason::UnsupportedArithmetic { mnemonic: mnem.into() });
        };
        let result = ops[primary].output.unwrap();
        let inputs = ops[primary].inputs.clone();
        if inputs.len() < 2 {
            return Err(Reason::UnsupportedArithmetic { mnemonic: mnem.into() });
        }
        let mut a = fresh();
        a = Varnode::new(a.space, a.offset, result.size);
        let mut b = fresh();
        b = Varnode::new(b.space, b.offset, result.size);
        ops.splice(primary..primary, [copy(a, inputs[0]), copy(b, inputs[1])]);
        (primary + 2, a, b, result, primary + 3)
    };
    let _ = primary_index;
    let mut x = fresh();
    x = Varnode::new(x.space, x.offset, result.size);
    let mut y = fresh();
    y = Varnode::new(y.space, y.offset, result.size);
    let mut z = fresh();
    z = Varnode::new(z.space, z.offset, result.size);
    let recipe = vec![
        op(OpCode::IntXor, x, vec![a, b]),
        op(OpCode::IntXor, y, vec![x, result]),
        op(OpCode::IntRight, z, vec![y, Varnode::constant(4, result.size)]),
        op(OpCode::IntAnd, flag(regs, "AF"), vec![z, Varnode::constant(1, result.size)]),
    ];
    ops.splice(insert..insert, recipe);
    Ok(ops)
}

fn correct_shift(
    mnem: &str,
    ops: &[PcodeOp],
    regs: &Registers,
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<PcodeOp>, Reason> {
    let mut ops = ops.to_vec();
    let opcode = if mnem == "SHL" { OpCode::IntLeft } else { OpCode::IntRight };
    let index = ops
        .iter()
        .position(|op| {
            op.opcode == opcode
                && op.output.is_some_and(|v| matches!(v.space, Space::Register | Space::Unique) && matches!(v.size, 1 | 2 | 4))
        })
        .ok_or_else(|| Reason::UnsupportedShift { mnemonic: mnem.into() })?;
    let source = ops[index].output.unwrap();
    let count = ops[index].inputs[1];
    let mut original = fresh();
    original = Varnode::new(original.space, original.offset, source.size);
    let mut old_of = fresh();
    old_of = Varnode::new(old_of.space, old_of.offset, 1);
    ops.insert(index, copy(original, ops[index].inputs[0]));
    ops.insert(0, copy(old_of, flag(regs, "OF")));
    let mut nonzero = fresh();
    nonzero = Varnode::new(nonzero.space, nonzero.offset, 1);
    let mut zero = fresh();
    zero = Varnode::new(zero.space, zero.offset, 1);
    let mut sign = fresh();
    sign = Varnode::new(sign.space, sign.offset, 1);
    let mut changed = fresh();
    changed = Varnode::new(changed.space, changed.offset, 1);
    let mut kept = fresh();
    kept = Varnode::new(kept.space, kept.offset, 1);
    let mut updated = fresh();
    updated = Varnode::new(updated.space, updated.offset, 1);
    let sign_source = if mnem == "SHL" { source } else { original };
    let mut recipe = vec![
        op(OpCode::IntNotEqual, nonzero, vec![count, Varnode::constant(0, count.size)]),
        PcodeOp {
            opcode: OpCode::BoolNegate,
            output: Some(zero),
            inputs: vec![nonzero],
            userop: None,
        },
        op(OpCode::IntSLess, sign, vec![sign_source, Varnode::constant(0, sign_source.size)]),
    ];
    if mnem == "SHL" {
        let mut x = fresh();
        x = Varnode::new(x.space, x.offset, 1);
        recipe.push(op(OpCode::IntXor, x, vec![sign, flag(regs, "CF")]));
        recipe.push(op(OpCode::IntAnd, changed, vec![nonzero, x]));
    } else {
        recipe.push(op(OpCode::IntAnd, changed, vec![nonzero, sign]));
    }
    recipe.push(op(OpCode::IntAnd, kept, vec![zero, old_of]));
    recipe.push(op(OpCode::IntOr, updated, vec![changed, kept]));
    recipe.push(copy(flag(regs, "OF"), updated));
    ops.extend(recipe);
    Ok(ops)
}

fn correct_sar(
    ops: &[PcodeOp],
    regs: &Registers,
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<PcodeOp>, Reason> {
    let mut ops = ops.to_vec();
    let index = ops
        .iter()
        .position(|op| {
            op.opcode == OpCode::IntSRight
                && op.output.is_some_and(|v| matches!(v.space, Space::Register | Space::Unique) && matches!(v.size, 1 | 2 | 4))
        })
        .ok_or_else(|| Reason::UnsupportedShift { mnemonic: "SAR".into() })?;
    let count = ops[index].inputs[1];
    let mut old_of = fresh();
    old_of = Varnode::new(old_of.space, old_of.offset, 1);
    let mut nonzero = fresh();
    nonzero = Varnode::new(nonzero.space, nonzero.offset, 1);
    let mut zero = fresh();
    zero = Varnode::new(zero.space, zero.offset, 1);
    let mut updated = fresh();
    updated = Varnode::new(updated.space, updated.offset, 1);
    ops.insert(0, copy(old_of, flag(regs, "OF")));
    ops.extend([
        op(OpCode::IntNotEqual, nonzero, vec![count, Varnode::constant(0, count.size)]),
        PcodeOp {
            opcode: OpCode::BoolNegate,
            output: Some(zero),
            inputs: vec![nonzero],
            userop: None,
        },
        op(OpCode::IntAnd, updated, vec![zero, old_of]),
        copy(flag(regs, "OF"), updated),
    ]);
    Ok(ops)
}

fn correct_neg(
    ops: &[PcodeOp],
    regs: &Registers,
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<PcodeOp>, Reason> {
    let mut ops = ops.to_vec();
    let result = ops
        .iter()
        .find(|op| op.opcode == OpCode::Int2Comp && op.output.is_some_and(|v| matches!(v.space, Space::Register | Space::Unique)))
        .and_then(|op| op.output)
        .ok_or_else(|| Reason::UnsupportedArithmetic { mnemonic: "NEG".into() })?;
    let mut low = fresh();
    low = Varnode::new(low.space, low.offset, result.size);
    let mut af = fresh();
    af = Varnode::new(af.space, af.offset, 1);
    ops.extend([
        op(OpCode::IntAnd, low, vec![result, Varnode::constant(0xf, result.size)]),
        op(OpCode::IntNotEqual, af, vec![low, Varnode::constant(0, result.size)]),
        copy(flag(regs, "AF"), af),
    ]);
    Ok(ops)
}

fn correct_div_cg(
    mnem: &str,
    ops: &[PcodeOp],
    regs: &Registers,
    addr: Addr,
    fresh: &mut dyn FnMut() -> Varnode,
) -> Result<Vec<CgOp>, Reason> {
    let signed = mnem == "IDIV";
    let div = if signed { OpCode::IntSDiv } else { OpCode::IntDiv };
    let rem = if signed { OpCode::IntSRem } else { OpCode::IntRem };
    let index = ops
        .iter()
        .position(|op| op.opcode == div)
        .ok_or_else(|| Reason::UnsupportedDivide { mnemonic: mnem.into() })?;
    let division = &ops[index];
    let remainder = ops
        .iter()
        .find(|op| op.opcode == rem && op.inputs == division.inputs)
        .ok_or_else(|| Reason::UnsupportedDivide { mnemonic: mnem.into() })?;
    let eax = regs.require("EAX");
    let edx = regs.require("EDX");
    let shape = division.output.is_some_and(|v| v.size == 8)
        && division.inputs.iter().all(|v| v.size == 8)
        && ops.len() > index + 3
        && ops[index + 1].opcode == OpCode::SubPiece
        && ops[index + 1].output == Some(eax)
        && ops[index + 1].inputs == [division.output.unwrap(), Varnode::constant(0, 4)]
        && ops[index + 2].opcode == rem
        && ops[index + 3].opcode == OpCode::SubPiece
        && ops[index + 3].output == Some(edx)
        && ops[index + 3].inputs == [remainder.output.unwrap(), Varnode::constant(0, 4)];
    if !shape {
        return Err(Reason::UnsupportedDivide { mnemonic: mnem.into() });
    }
    let mut result = fresh();
    result = Varnode::new(result.space, result.offset, 8);
    let mut inputs = division.inputs.clone();
    inputs.push(Varnode::constant(addr as u64, 4));
    let opcode = if signed { Op::Idiv32 { addr } } else { Op::Div32 { addr } };
    let mut out: Vec<CgOp> = ops[..index].iter().cloned().map(from_pcode).collect();
    out.extend([
        CgOp { op: opcode, output: Some(result), inputs },
        CgOp {
            op: Op::P(OpCode::SubPiece),
            output: Some(eax),
            inputs: vec![result, Varnode::constant(0, 4)],
        },
        CgOp {
            op: Op::P(OpCode::SubPiece),
            output: Some(edx),
            inputs: vec![result, Varnode::constant(4, 4)],
        },
    ]);
    Ok(out)
}

fn apply_seh(ops: &mut Vec<CgOp>, kinds: &[SehEffect], addr: Addr) {
    for kind in kinds {
        match kind {
            SehEffect::Landing => ops.insert(0, cg(Op::Seh(*kind))),
            SehEffect::Orphan => {
                if let Some(at) = ops.iter().position(|op| op.op == Op::Return) {
                    ops.insert(at, cg(Op::Seh(*kind)));
                } else {
                    ops.push(cg(Op::Seh(*kind)));
                }
            }
            _ => ops.push(cg(Op::Seh(*kind))),
        }
    }
    let _ = addr;
}

pub fn string_fields(_helper: &str) -> (Fields, Fields, bool) {
    string::effect(_helper)
}
