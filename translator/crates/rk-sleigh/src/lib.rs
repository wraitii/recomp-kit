//! Wraps the pinned Ghidra x86-32 SLEIGH specification embedded by `sleigh-config`: instruction
//! lengths, flow ends, and p-code lifting into `rk_core::ir`. Unknown address spaces fail with a
//! diagnostic and unmapped opcodes become `OpCode::Unknown`. A `Lifter` numbers its own unique
//! varnodes, so callers keep one per thread.

use std::collections::{BTreeMap, BTreeSet};

use libsla::{
    Address, AddressSpace, BoolOp, FloatOp, GhidraSleigh, InstructionLoader, IntOp, IntSign,
    OpCode as SleighOp, PcodeInstruction, Sleigh, VarnodeData,
};
use rk_core::ir::{OpCode, PcodeOp, Space, Varnode};
use rk_core::{Addr, Diagnostic, Reason, Result};

const ST_BASE: u64 = 0x1100;
const ST_STRIDE: u64 = 0x10;
const ST_SIZE: u32 = 10;
const DIAGNOSTIC_REGISTERS: [&str; 5] = [
    "FPUInstructionPointer",
    "FPUDataPointer",
    "FPULastInstructionOpcode",
    "FPUPointerSelector",
    "FPUDataSelector",
];

#[derive(Clone, Debug)]
pub struct Lifted {
    pub length: u8,
    pub ops: Vec<PcodeOp>,
    pub x87: bool,
}

pub struct Lifter {
    sleigh: GhidraSleigh,
    code_space: AddressSpace,
    register_names: BTreeMap<(u64, u32), String>,
    diagnostic: BTreeSet<(u64, u32)>,
    unique: u64,
}

struct SliceLoader<'a> {
    base: u64,
    data: &'a [u8],
}

impl InstructionLoader for SliceLoader<'_> {
    fn load_instruction_bytes(&self, source: &VarnodeData) -> std::result::Result<Vec<u8>, String> {
        let Some(start) = source.address.offset.checked_sub(self.base) else {
            return Ok(Vec::new());
        };
        let start = usize::try_from(start).map_err(|e| e.to_string())?;
        if start >= self.data.len() {
            return Ok(Vec::new());
        }
        let end = start.saturating_add(source.size).min(self.data.len());
        Ok(self.data[start..end].to_vec())
    }
}

impl Lifter {
    pub fn new() -> Result<Self> {
        let sleigh = GhidraSleigh::builder()
            .processor_spec(sleigh_config::processor_x86::PSPEC_X86)
            .and_then(|b| b.build(sleigh_config::processor_x86::SLA_X86))
            .map_err(|e| rk_core::Error::input(format!("cannot initialize SLEIGH x86: {e}")))?;
        let code_space = sleigh.default_code_space();
        let mut register_names = BTreeMap::new();
        for (varnode, name) in sleigh.register_name_map() {
            register_names
                .entry((varnode.address.offset, varnode.size as u32))
                .or_insert(name);
        }
        let diagnostic = DIAGNOSTIC_REGISTERS
            .iter()
            .filter_map(|name| {
                let v = sleigh.register_from_name(*name).ok()?;
                Some((v.address.offset, v.size as u32))
            })
            .collect();
        Ok(Self {
            sleigh,
            code_space,
            register_names,
            diagnostic,
            unique: 0,
        })
    }

    pub fn register_name(&self, offset: u64, size: u32) -> Option<&str> {
        self.register_names.get(&(offset, size)).map(String::as_str)
    }

    pub fn lengths(&self, addr: Addr, data: &[u8]) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        let mut pos = 0;
        while pos < data.len() {
            let length = self.length(addr + pos as u32, &data[pos..])?;
            out.push(length);
            pos += length as usize;
        }
        if pos != data.len() {
            return Err(Diagnostic::error(
                addr,
                Reason::DecoderCannotTile { span: addr },
            ));
        }
        Ok(out)
    }

    pub fn length(&self, addr: Addr, data: &[u8]) -> Result<u8> {
        let decoded = self
            .translate_one(addr, data)
            .map_err(|e| Diagnostic::error(addr, Reason::LiftFailed { detail: e.to_string() }))?;
        let length = decoded.origin.size;
        let length = u8::try_from(length)
            .map_err(|_| Diagnostic::error(addr, Reason::LiftFailed { detail: "instruction longer than 255 bytes".into() }))?;
        if length == 0 {
            return Err(Diagnostic::error(
                addr,
                Reason::LiftFailed { detail: "no instruction mark".into() },
            ));
        }
        Ok(length)
    }

    pub fn ends_flow(&self, addr: Addr, data: &[u8]) -> Result<bool> {
        let decoded = self
            .translate_one(addr, data)
            .map_err(|e| Diagnostic::error(addr, Reason::LiftFailed { detail: e.to_string() }))?;
        Ok(decoded.instructions.iter().any(|op| {
            matches!(
                op.op_code,
                SleighOp::Branch | SleighOp::BranchIndirect | SleighOp::Return
            ) && !(op.op_code == SleighOp::Branch
                && op.inputs.first().is_some_and(|v| v.address.address_space.is_constant()))
        }))
    }

    pub fn lift(&mut self, addr: Addr, data: &[u8]) -> Result<Lifted> {
        if data == [0x9b] {
            return Ok(Lifted { length: 1, ops: Vec::new(), x87: false });
        }
        let mut ops = Vec::new();
        let mut x87 = false;
        let mut pos = 0usize;
        while pos < data.len() {
            let at = addr + pos as u32;
            let decoded = self.translate_one(at, &data[pos..]).map_err(|e| {
                Diagnostic::error(at, Reason::LiftFailed { detail: e.to_string() })
            })?;
            let size = decoded.origin.size;
            if size == 0 || pos + size > data.len() {
                return Err(Diagnostic::error(
                    at,
                    Reason::LiftFailed {
                        detail: format!(
                            "SLEIGH length {size} overruns listed length {}",
                            data.len()
                        ),
                    },
                ));
            }
            let sub = self.convert_all(decoded.instructions)?;
            let sub_flow = sub.iter().any(|op| {
                matches!(op.opcode, OpCode::Branch | OpCode::CBranch)
                    && op.inputs.first().is_some_and(|v| {
                        v.space == Space::Const
                            || (v.space == Space::Ram
                                && addr as u64 <= v.offset
                                && v.offset <= (addr + data.len() as u32) as u64)
                    })
            });
            let mut sub = sub;
            if sub
                .iter()
                .any(|op| st_index(op.output.as_ref()).is_some() || op.inputs.iter().any(|v| st_index(Some(v)).is_some()))
            {
                if sub_flow {
                    return Err(Diagnostic::error(
                        at,
                        Reason::Unsupported { detail: "x87 instruction with internal control flow".into() },
                    ));
                }
                sub = normalize_x87(self.rename_uniques(sub), |size| self.fresh_unique(size)).0;
                x87 = true;
            }
            if sub_flow && !ops.is_empty() {
                return Err(Diagnostic::error(
                    addr,
                    Reason::Unsupported { detail: "internal control flow after a folded prefix".into() },
                ));
            }
            ops.extend(sub);
            pos += size;
        }
        let length = u8::try_from(data.len()).map_err(|_| {
            Diagnostic::error(addr, Reason::LiftFailed { detail: "instruction longer than 255 bytes".into() })
        })?;
        Ok(Lifted { length, ops, x87 })
    }

    fn translate_one(&self, addr: Addr, data: &[u8]) -> std::result::Result<libsla::PcodeDisassembly, libsla::Error> {
        let loader = SliceLoader { base: addr as u64, data };
        self.sleigh
            .disassemble_pcode(&loader, Address::new(self.code_space.clone(), addr as u64))
    }

    fn convert_all(&self, raw: Vec<PcodeInstruction>) -> Result<Vec<PcodeOp>> {
        let mut out = Vec::with_capacity(raw.len());
        for op in raw {
            if let Some(converted) = self.convert(op)? {
                out.push(converted);
            }
        }
        Ok(out)
    }

    fn convert(&self, op: PcodeInstruction) -> Result<Option<PcodeOp>> {
        let output = op.output.as_ref().map(|v| self.varnode(v)).transpose()?;
        if let Some(v) = &output {
            if v.space == Space::Register && self.diagnostic.contains(&(v.offset, v.size)) {
                return Ok(None);
            }
        }
        let mut inputs: Vec<Varnode> = op
            .inputs
            .iter()
            .map(|v| self.varnode(v))
            .collect::<Result<_>>()?;
        let mut userop = None;
        match op.op_code {
            SleighOp::Load | SleighOp::Store => {
                if !inputs.is_empty() {
                    inputs.remove(0);
                }
            }
            SleighOp::Pseudo(libsla::PseudoOp::CallOther) => {
                if let Some(id) = inputs.first() {
                    userop = Some(id.offset);
                }
            }
            _ => {}
        }
        let opcode = map_opcode(op.op_code);
        if inputs.len() == 2 && inputs[0] == inputs[1] && inputs[0].space != Space::Const {
            if let Some(output) = output {
                let size = output.size;
                if matches!(
                    opcode,
                    OpCode::IntXor
                        | OpCode::IntSub
                        | OpCode::IntLess
                        | OpCode::IntSLess
                        | OpCode::IntNotEqual
                        | OpCode::IntSBorrow
                        | OpCode::BoolXor
                ) {
                    return Ok(Some(PcodeOp {
                        opcode: OpCode::Copy,
                        output: Some(output),
                        inputs: vec![Varnode::constant(0, size)],
                        userop,
                    }));
                }
                if matches!(
                    opcode,
                    OpCode::IntEqual | OpCode::IntLessEqual | OpCode::IntSLessEqual
                ) {
                    return Ok(Some(PcodeOp {
                        opcode: OpCode::Copy,
                        output: Some(output),
                        inputs: vec![Varnode::constant(1, size)],
                        userop,
                    }));
                }
            }
        }
        Ok(Some(PcodeOp { opcode, output, inputs, userop }))
    }

    fn varnode(&self, v: &VarnodeData) -> Result<Varnode> {
        let space = match v.address.address_space.name.as_ref() {
            "register" => Space::Register,
            "unique" => Space::Unique,
            "const" => Space::Const,
            "ram" => Space::Ram,
            other => {
                return Err(Diagnostic::error(
                    v.address.offset as Addr,
                    Reason::Unsupported { detail: format!("unexpected SLEIGH space {other}") },
                ))
            }
        };
        Ok(Varnode::new(space, v.address.offset, v.size as u32))
    }

    fn fresh_unique(&mut self, size: u32) -> Varnode {
        self.unique += 1;
        Varnode::new(Space::Unique, 0x1000_0000 + self.unique * 0x10, size)
    }

    fn rename_uniques(&mut self, ops: Vec<PcodeOp>) -> Vec<PcodeOp> {
        let mut names: BTreeMap<Varnode, Varnode> = BTreeMap::new();
        let mut out = Vec::with_capacity(ops.len());
        for mut op in ops {
            for input in &mut op.inputs {
                if input.space == Space::Unique {
                    if let Some(replacement) = names.get(input) {
                        *input = *replacement;
                    }
                }
            }
            if let Some(dst) = op.output {
                if dst.space == Space::Unique {
                    let replacement = self.fresh_unique(dst.size);
                    names.insert(dst, replacement);
                    op.output = Some(replacement);
                }
            }
            out.push(op);
        }
        out
    }
}

fn st_index(v: Option<&Varnode>) -> Option<u32> {
    let v = v?;
    if v.space != Space::Register || v.size != ST_SIZE || v.offset < ST_BASE {
        return None;
    }
    let rel = v.offset - ST_BASE;
    if rel < 8 * ST_STRIDE && rel.is_multiple_of(ST_STRIDE) {
        Some((rel / ST_STRIDE) as u32)
    } else {
        None
    }
}

enum Slot {
    Pre(u32),
    Tmp(Varnode),
}

fn normalize_x87(mut ops: Vec<PcodeOp>, mut fresh: impl FnMut(u32) -> Varnode) -> (Vec<PcodeOp>, i8) {
    let mut st: [Slot; 8] = std::array::from_fn(|i| Slot::Pre(i as u32));
    let mut alias: BTreeMap<Varnode, u32> = BTreeMap::new();
    let mut out = Vec::with_capacity(ops.len());

    fn value(v: &Varnode, st: &[Slot; 8]) -> Varnode {
        match st_index(Some(v)) {
            Some(k) => match st[k as usize] {
                Slot::Pre(j) => Varnode::new(Space::Stin, j as u64, ST_SIZE),
                Slot::Tmp(t) => t,
            },
            None => *v,
        }
    }

    for mut op in ops.drain(..) {
        op.inputs = op.inputs.iter().map(|v| value(v, &st)).collect();
        let k = st_index(op.output.as_ref());
        if op.opcode == OpCode::Copy
            && op.output.is_some_and(|v| v.space == Space::Unique)
            && op.inputs[0].space == Space::Stin
        {
            alias.insert(op.output.unwrap(), op.inputs[0].offset as u32);
            continue;
        }
        let Some(k) = k else {
            out.push(op);
            continue;
        };
        if op.opcode == OpCode::Copy {
            let src = op.inputs[0];
            if src.space == Space::Stin {
                st[k as usize] = Slot::Pre(src.offset as u32);
                continue;
            }
            if let Some(j) = alias.get(&src) {
                st[k as usize] = Slot::Pre(*j);
                continue;
            }
            if matches!(src.space, Space::Unique | Space::Const) {
                st[k as usize] = Slot::Tmp(src);
                continue;
            }
        }
        let t = fresh(ST_SIZE);
        out.push(PcodeOp {
            opcode: op.opcode,
            output: Some(t),
            inputs: op.inputs,
            userop: op.userop,
        });
        st[k as usize] = Slot::Tmp(t);
    }
    for op in &mut out {
        for input in &mut op.inputs {
            if let Some(j) = alias.get(input) {
                *input = Varnode::new(Space::Stin, *j as u64, ST_SIZE);
            }
        }
    }
    let mut best = -1i32;
    let mut delta = 0i8;
    for d in [0i8, -1, 1, -2] {
        let score = (0..8)
            .filter(|&i| matches!(st[i], Slot::Pre(j) if j as i32 == i as i32 - d as i32))
            .count();
        if score as i32 > best {
            best = score as i32;
            delta = d;
        }
    }
    for i in 0..8i32 {
        if delta < 0 && i >= 8 + delta as i32 {
            continue;
        }
        if matches!(st[i as usize], Slot::Pre(j) if j as i32 == i - delta as i32) {
            continue;
        }
        let src = match st[i as usize] {
            Slot::Pre(j) => Varnode::new(Space::Stin, j as u64, ST_SIZE),
            Slot::Tmp(t) => t,
        };
        out.push(PcodeOp {
            opcode: OpCode::Copy,
            output: Some(Varnode::new(Space::Stout, i as u64, ST_SIZE)),
            inputs: vec![src],
            userop: None,
        });
    }
    (out, delta)
}

fn map_opcode(op: SleighOp) -> OpCode {
    use SleighOp as S;
    match op {
        S::Copy => OpCode::Copy,
        S::Load => OpCode::Load,
        S::Store => OpCode::Store,
        S::Branch => OpCode::Branch,
        S::BranchConditional => OpCode::CBranch,
        S::BranchIndirect => OpCode::BranchInd,
        S::Call => OpCode::Call,
        S::CallIndirect => OpCode::CallInd,
        S::Pseudo(libsla::PseudoOp::CallOther) => OpCode::CallOther,
        S::Return => OpCode::Return,
        S::Piece => OpCode::Piece,
        S::Subpiece => OpCode::SubPiece,
        S::Popcount => OpCode::PopCount,
        S::LzCount => OpCode::LzCount,
        S::Bool(BoolOp::Negate) => OpCode::BoolNegate,
        S::Bool(BoolOp::Xor) => OpCode::BoolXor,
        S::Bool(BoolOp::And) => OpCode::BoolAnd,
        S::Bool(BoolOp::Or) => OpCode::BoolOr,
        S::Int(IntOp::Add) => OpCode::IntAdd,
        S::Int(IntOp::Negate) => OpCode::Int2Comp,
        S::Int(IntOp::Subtract) => OpCode::IntSub,
        S::Int(IntOp::Multiply) => OpCode::IntMult,
        S::Int(IntOp::Divide(IntSign::Unsigned)) => OpCode::IntDiv,
        S::Int(IntOp::Divide(IntSign::Signed)) => OpCode::IntSDiv,
        S::Int(IntOp::Remainder(IntSign::Unsigned)) => OpCode::IntRem,
        S::Int(IntOp::Remainder(IntSign::Signed)) => OpCode::IntSRem,
        S::Int(IntOp::Equal) => OpCode::IntEqual,
        S::Int(IntOp::NotEqual) => OpCode::IntNotEqual,
        S::Int(IntOp::LessThan(IntSign::Signed)) => OpCode::IntSLess,
        S::Int(IntOp::LessThan(IntSign::Unsigned)) => OpCode::IntLess,
        S::Int(IntOp::LessThanOrEqual(IntSign::Signed)) => OpCode::IntSLessEqual,
        S::Int(IntOp::LessThanOrEqual(IntSign::Unsigned)) => OpCode::IntLessEqual,
        S::Int(IntOp::Extension(IntSign::Unsigned)) => OpCode::IntZExt,
        S::Int(IntOp::Extension(IntSign::Signed)) => OpCode::IntSExt,
        S::Int(IntOp::Carry(IntSign::Unsigned)) => OpCode::IntCarry,
        S::Int(IntOp::Carry(IntSign::Signed)) => OpCode::IntSCarry,
        S::Int(IntOp::Borrow) => OpCode::IntSBorrow,
        S::Int(IntOp::ShiftLeft) => OpCode::IntLeft,
        S::Int(IntOp::ShiftRight(IntSign::Unsigned)) => OpCode::IntRight,
        S::Int(IntOp::ShiftRight(IntSign::Signed)) => OpCode::IntSRight,
        S::Int(IntOp::Bitwise(BoolOp::Negate)) => OpCode::IntNegate,
        S::Int(IntOp::Bitwise(BoolOp::Xor)) => OpCode::IntXor,
        S::Int(IntOp::Bitwise(BoolOp::And)) => OpCode::IntAnd,
        S::Int(IntOp::Bitwise(BoolOp::Or)) => OpCode::IntOr,
        S::Float(FloatOp::Equal) => OpCode::FloatEqual,
        S::Float(FloatOp::NotEqual) => OpCode::FloatNotEqual,
        S::Float(FloatOp::LessThan) => OpCode::FloatLess,
        S::Float(FloatOp::LessThanOrEqual) => OpCode::FloatLessEqual,
        S::Float(FloatOp::IsNaN) => OpCode::FloatNan,
        S::Float(FloatOp::Add) => OpCode::FloatAdd,
        S::Float(FloatOp::Divide) => OpCode::FloatDiv,
        S::Float(FloatOp::Multiply) => OpCode::FloatMult,
        S::Float(FloatOp::Subtract) => OpCode::FloatSub,
        S::Float(FloatOp::Negate) => OpCode::FloatNeg,
        S::Float(FloatOp::AbsoluteValue) => OpCode::FloatAbs,
        S::Float(FloatOp::SquareRoot) => OpCode::FloatSqrt,
        S::Float(FloatOp::IntToFloat) => OpCode::FloatInt2Float,
        S::Float(FloatOp::FloatToFloat) => OpCode::FloatFloat2Float,
        S::Float(FloatOp::Truncate) => OpCode::FloatTrunc,
        S::Float(FloatOp::Ceiling) => OpCode::FloatCeil,
        S::Float(FloatOp::Floor) => OpCode::FloatFloor,
        S::Float(FloatOp::Round) => OpCode::FloatRound,
        _ => OpCode::Unknown,
    }
}

pub fn format_varnode(v: &Varnode, lifter: &Lifter) -> String {
    match v.space {
        Space::Register => lifter
            .register_name(v.offset, v.size)
            .map(str::to_owned)
            .unwrap_or_else(|| format!("reg[{}:{}]", rk_core::Hex(v.offset as Addr), v.size)),
        Space::Const => format!("0x{:x}", v.offset),
        Space::Stin => format!("stin({})", v.offset),
        Space::Stout => format!("stout({})", v.offset),
        Space::Unique => format!("unique[0x{:x}:{}]", v.offset, v.size),
        Space::Ram => format!("ram[0x{:x}:{}]", v.offset, v.size),
    }
}
