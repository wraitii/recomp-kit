//! Capstone-backed structural decoder for one instruction whose bytes are already known; a `WAIT`
//! followed by an `FN*` instruction is fused into one. Produces the canonical mnemonics, typed
//! operands and listing text that the frontend, x87 recipes and slow-path emission read. Also holds
//! the MSVC SEH and EH3 byte-pattern recognizers.

use capstone::arch::x86::{ArchMode, X86OperandType};
use capstone::prelude::*;
use capstone::{Capstone, Insn};
use rk_core::{Addr, Result};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Part {
    L,
    H,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum OperandKind {
    Reg {
        gpr: u8,
        part: Option<Part>,
    },
    Xmm(u8),
    Mm(u8),
    St(u8),
    Sreg {
        name: String,
        selector: u32,
    },
    Imm(i64),
    Mem {
        base: Option<u8>,
        index: Option<u8>,
        scale: u8,
        disp: i64,
        seg: Option<String>,
    },
    Other(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Operand {
    pub kind: OperandKind,
    pub size: Option<u16>,
    pub display: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Repeat {
    Rep,
    Repne,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DecodedInsn {
    pub addr: Addr,
    pub length: u8,
    pub mnemonic: String,
    pub raw_mnemonic: String,
    pub rep: Option<Repeat>,
    pub operands: Vec<Operand>,
    pub raw: [u8; 15],
    pub text: String,
    pub opcode: u16,
    pub address_size: u8,
}

pub const REG32: [&str; 8] = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"];
pub const REG16: [&str; 8] = ["AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI"];
pub const REG8L: [&str; 4] = ["AL", "CL", "DL", "BL"];
pub const REG8H: [&str; 4] = ["AH", "CH", "DH", "BH"];
pub const JCC: [&str; 35] = [
    "JA", "JAE", "JB", "JBE", "JC", "JE", "JECXZ", "JCXZ", "JG", "JGE", "JL", "JLE", "JNA", "JNAE",
    "JNB", "JNBE", "JNC", "JNE", "JNG", "JNGE", "JNL", "JNLE", "JNO", "JNP", "JNS", "JNZ", "JO",
    "JP", "JPE", "JPO", "JS", "JZ", "LOOP", "LOOPE", "LOOPNE",
];
pub const STRING_MNEM: [&str; 19] = [
    "STOSB", "STOSW", "STOSD", "MOVSB", "MOVSW", "MOVSD", "LODSB", "LODSW", "LODSD", "SCASB",
    "SCASW", "SCASD", "CMPSB", "CMPSW", "CMPSD", "INSB", "INSW", "INSD", "OUTSB",
];
const STRING_ROOTS: [&str; 7] = ["MOVS", "STOS", "LODS", "CMPS", "SCAS", "INS", "OUTS"];
const LEGACY_PREFIXES: [u8; 12] = [
    0xF0, 0xF2, 0xF3, 0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65, 0x66, 0x67, 0x00,
];
const REP_PREFIX: [(&str, Repeat); 5] = [
    ("rep", Repeat::Rep),
    ("repe", Repeat::Rep),
    ("repz", Repeat::Rep),
    ("repne", Repeat::Repne),
    ("repnz", Repeat::Repne),
];
const X87_INT: [&str; 20] = [
    "FILD", "FIST", "FISTP", "FIADD", "FISUB", "FISUBR", "FIMUL", "FIDIV", "FIDIVR", "FICOM",
    "FICOMP", "FNSTSW", "FSTSW", "FNSTCW", "FSTCW", "FLDCW", "FNSTENV", "FLDENV", "FNSAVE",
    "FSAVE",
];
const STRUCTURED: [&str; 6] = ["FNSTENV", "FSTENV", "FLDENV", "FNSAVE", "FSAVE", "FRSTOR"];

const MSVC_SEH_PROLOG_HEX: &str = "68????????64a100000000508b442410896c24108d6c24102be05356578b45f88965e8508b45fcc745fcffffffff8945f88d45f064a300000000c3";
const MSVC_SEH_EPILOG_HEX: &str = "8b4df064890d00000000595f5e5bc951c3";
const MSVC_EH3_FRAME_HEX: &str = "558bec6aff6800000000680000000064a1000000005064892500000000";
pub const MSVC_SEH_PROLOG_LEN: u32 = 59;
pub const MSVC_EH3_FRAME_LEN: u32 = 29;
const MSVC_EH3_HANDLER_HEX: &str = "558bec83ec0853565755fc8b5d0c8b4508f74004060000000f85";
fn hex_nibble(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn seh_pattern(hex: &str, wildcards: &[usize]) -> Vec<Option<u8>> {
    let bytes = hex.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    let mut i = 0;
    let mut at = 0;
    while i + 1 < bytes.len() {
        let hi = hex_nibble(bytes[i]).unwrap_or(0);
        let lo = hex_nibble(bytes[i + 1]).unwrap_or(0);
        out.push(if wildcards.contains(&at) {
            None
        } else {
            Some((hi << 4) | lo)
        });
        i += 2;
        at += 1;
    }
    out
}

pub struct Decoder {
    md: Capstone,
}

impl Decoder {
    pub fn new() -> Result<Self> {
        let md = Capstone::new()
            .x86()
            .mode(ArchMode::Mode32)
            .detail(true)
            .build()
            .map_err(|e| rk_core::Error::input(format!("cannot initialize capstone: {e}")))?;
        Ok(Self { md })
    }

    pub fn decode_span(&self, addr: Addr, raw: &[u8]) -> Option<DecodedInsn> {
        let decoded = self.md.disasm_count(raw, addr as u64, 2).ok()?;
        let instructions: Vec<&Insn> = decoded.iter().collect();
        if instructions.len() == 1 && instructions[0].len() == raw.len() {
            return self.record(instructions[0], raw, addr, None);
        }
        if instructions.len() == 2
            && matches!(instructions[0].mnemonic(), Some("wait" | "fwait"))
            && instructions[1]
                .mnemonic()
                .is_some_and(|m| m.starts_with("fn"))
            && instructions[0].len() + instructions[1].len() == raw.len()
        {
            let ci = instructions[1];
            let suffix = ci.mnemonic()?[2..].to_uppercase();
            let mnemonic = alias(&format!("F{suffix}"));
            return self.record(ci, raw, addr, Some(&mnemonic));
        }
        None
    }

    fn record(
        &self,
        ci: &Insn,
        source_bytes: &[u8],
        address: Addr,
        mnemonic_override: Option<&str>,
    ) -> Option<DecodedInsn> {
        let raw_mnemonic = ci.mnemonic()?.to_owned();
        let mut words: Vec<&str> = raw_mnemonic.split_whitespace().collect();
        let rep = words
            .first()
            .and_then(|w| REP_PREFIX.iter().find(|(p, _)| p == w).map(|(_, r)| *r));
        if rep.is_some() {
            words.remove(0);
        }
        let mut mnemonic = words.last()?.to_uppercase();
        mnemonic = alias(&mnemonic);
        if let Some(over) = mnemonic_override {
            mnemonic = over.to_owned();
        }
        let detail = self.md.insn_detail(ci).ok()?;
        let ArchDetail::X86Detail(x86) = detail.arch_detail();
        let address_size = x86.addr_size();
        let mut operands = Vec::new();
        for op in x86.operands() {
            let size = (op.size > 0).then_some(op.size as u16 * 8);
            let (kind, display) = match op.op_type {
                X86OperandType::Reg(reg) => {
                    let name = self.md.reg_name(reg)?.to_uppercase();
                    let kind = reg_kind(&name);
                    let display = if name.starts_with("ST(") && name.ends_with(')') {
                        format!("ST{}", &name[3..name.len() - 1])
                    } else {
                        name.clone()
                    };
                    (kind, display)
                }
                X86OperandType::Imm(value) => {
                    let display = if value < 0 {
                        format!("-0x{:x}", -(value as i128))
                    } else {
                        format!("0x{value:x}")
                    };
                    (OperandKind::Imm(value), display)
                }
                X86OperandType::Mem(mem) => {
                    let base = self
                        .md
                        .reg_name(mem.base())
                        .and_then(|n| gpr_index(&n.to_uppercase()));
                    let index = self
                        .md
                        .reg_name(mem.index())
                        .and_then(|n| gpr_index(&n.to_uppercase()));
                    let seg_name = self.md.reg_name(mem.segment()).map(|s| s.to_uppercase());
                    let kind = OperandKind::Mem {
                        base,
                        index,
                        scale: mem.scale() as u8,
                        disp: mem.disp(),
                        seg: seg_name.filter(|s| !s.is_empty() && s != "INVALID"),
                    };
                    let display = self.mem_text(&mnemonic, size, &kind, address_size);
                    (kind, display)
                }
                X86OperandType::Invalid => return None,
            };
            operands.push(Operand {
                kind,
                size,
                display,
            });
        }
        let mut rep = rep;
        if rep.is_none()
            && STRING_ROOTS.contains(&mnemonic.trim_end_matches(['B', 'W', 'D']))
            && !operands
                .iter()
                .any(|o| matches!(o.kind, OperandKind::Xmm(_)))
        {
            for &byte in source_bytes {
                if !LEGACY_PREFIXES.contains(&byte) {
                    break;
                }
                if byte == 0xF2 {
                    rep = Some(Repeat::Repne);
                } else if byte == 0xF3 {
                    rep = Some(Repeat::Rep);
                }
            }
        }
        if mnemonic == "XLATB" {
            mnemonic = "XLAT".to_owned();
        }
        if mnemonic == "XCHG"
            && source_bytes
                .first()
                .is_some_and(|b| (0x90..=0x97).contains(b))
            && operands.len() == 2
        {
            operands.reverse();
        }
        let hidden = rep.is_some()
            || (STRING_MNEM.contains(&mnemonic.as_str())
                && !operands
                    .iter()
                    .any(|o| matches!(o.kind, OperandKind::Xmm(_))));
        let operands_text = if hidden {
            String::new()
        } else {
            operands
                .iter()
                .map(|o| o.display.clone())
                .collect::<Vec<_>>()
                .join(",")
        };
        let text = format!(
            "{address:08x}  {mnemonic}{}{}",
            rep.map(|r| match r {
                Repeat::Rep => ".REP",
                Repeat::Repne => ".REPNE",
            })
            .unwrap_or(""),
            if operands_text.is_empty() {
                String::new()
            } else {
                format!(" {operands_text}")
            }
        );
        let operands = if hidden { Vec::new() } else { operands };
        let length = u8::try_from(source_bytes.len()).ok().filter(|&n| n <= 15)?;
        let mut raw = [0u8; 15];
        raw[..source_bytes.len()].copy_from_slice(source_bytes);
        Some(DecodedInsn {
            addr: address,
            length,
            mnemonic,
            raw_mnemonic,
            rep,
            operands,
            raw,
            text,
            opcode: ci.id().0 as u16,
            address_size,
        })
    }

    fn mem_text(
        &self,
        mnemonic: &str,
        size: Option<u16>,
        kind: &OperandKind,
        address_size: u8,
    ) -> String {
        let OperandKind::Mem {
            base,
            index,
            scale,
            disp,
            seg,
        } = kind
        else {
            return String::new();
        };
        let structured = STRUCTURED.contains(&mnemonic);
        let word = if mnemonic.starts_with('F') && !X87_INT.contains(&mnemonic) {
            match size.map(|s| s / 8) {
                Some(4) => Some("float"),
                Some(8) => Some("double"),
                Some(10) => Some("extended double"),
                _ => None,
            }
        } else {
            match size.map(|s| s / 8) {
                Some(1) => Some("byte"),
                Some(2) => Some("word"),
                Some(4) => Some("dword"),
                Some(8) => Some("qword"),
                Some(10) => Some("extended double"),
                Some(16) => Some("xmmword"),
                _ => None,
            }
        };
        let mut parts = Vec::new();
        let registers = if address_size == 2 {
            &REG16[..]
        } else {
            &REG32[..]
        };
        if let Some(reg) = base {
            parts.push(registers[*reg as usize].to_owned());
        }
        if let Some(reg) = index {
            parts.push(format!("{}*0x{:x}", registers[*reg as usize], scale));
        }
        if *disp != 0 || parts.is_empty() {
            if *disp < 0 {
                parts.push(format!("-0x{:x}", -(*disp as i128)));
            } else {
                parts.push(format!("0x{disp:x}"));
            }
        }
        let prefix = if structured {
            String::new()
        } else {
            word.map(|w| format!("{w} ptr ")).unwrap_or_default()
        };
        let segment = seg.as_ref().map(|s| format!("{s}:")).unwrap_or_default();
        format!("{prefix}{segment}[{}]", parts.join(" + "))
    }
}

fn alias(name: &str) -> String {
    match name {
        "POPAL" => "POPAD",
        "PUSHAL" => "PUSHAD",
        "POPFL" => "POPFD",
        "PUSHFL" => "PUSHFD",
        "CWTL" => "CWDE",
        "CLTD" => "CDQ",
        "CBTW" => "CBW",
        "FCOMPI" => "FCOMIP",
        "FUCOMPI" => "FUCOMIP",
        "IRETD" => "IRET",
        other => other,
    }
    .to_owned()
}

fn reg_kind(name: &str) -> OperandKind {
    if let Some(index) = REG32.iter().position(|r| *r == name) {
        return OperandKind::Reg {
            gpr: index as u8,
            part: None,
        };
    }
    if let Some(index) = REG16.iter().position(|r| *r == name) {
        return OperandKind::Reg {
            gpr: index as u8,
            part: None,
        };
    }
    if let Some(index) = REG8L.iter().position(|r| *r == name) {
        return OperandKind::Reg {
            gpr: index as u8,
            part: Some(Part::L),
        };
    }
    if let Some(index) = REG8H.iter().position(|r| *r == name) {
        return OperandKind::Reg {
            gpr: index as u8,
            part: Some(Part::H),
        };
    }
    if let Some(index) = name.strip_prefix("XMM").and_then(|n| n.parse().ok()) {
        return OperandKind::Xmm(index);
    }
    if let Some(index) = name.strip_prefix("MM").and_then(|n| n.parse().ok()) {
        return OperandKind::Mm(index);
    }
    if let Some(index) = name.strip_prefix("ST").and_then(|n| n.parse().ok()) {
        return OperandKind::St(index);
    }
    let selector = match name {
        "CS" => Some(0x1b),
        "DS" | "ES" | "SS" => Some(0x23),
        "FS" => Some(0x3b),
        "GS" => Some(0x00),
        _ => None,
    };
    if let Some(selector) = selector {
        return OperandKind::Sreg {
            name: name.to_owned(),
            selector,
        };
    }
    OperandKind::Other(name.to_owned())
}

pub fn gpr_index(name: &str) -> Option<u8> {
    REG32
        .iter()
        .position(|r| *r == name)
        .or_else(|| REG16.iter().position(|r| *r == name))
        .map(|i| i as u8)
}

pub fn gpr32(op: &Operand) -> Option<u8> {
    match op.kind {
        OperandKind::Reg { gpr, part: None } if op.size == Some(32) => Some(gpr),
        _ => None,
    }
}

pub fn imm(op: &Operand) -> Option<i64> {
    match op.kind {
        OperandKind::Imm(value) => Some(value),
        _ => None,
    }
}

pub type MemOperand<'a> = (Option<u8>, Option<u8>, u8, i64, Option<&'a str>);

pub fn mem<'a>(op: &'a Operand) -> Option<MemOperand<'a>> {
    match &op.kind {
        OperandKind::Mem {
            base,
            index,
            scale,
            disp,
            seg,
        } => Some((*base, *index, *scale, *disp, seg.as_deref())),
        _ => None,
    }
}

pub fn is_xmm(op: &Operand) -> bool {
    matches!(op.kind, OperandKind::Xmm(_))
}

pub fn names_an_xmm(ins: &DecodedInsn) -> bool {
    ins.operands.iter().any(is_xmm)
}

pub fn branch_target(ins: &DecodedInsn) -> Option<Addr> {
    match ins.operands.first()?.kind {
        OperandKind::Imm(value) => Some(value as Addr),
        _ => None,
    }
}

pub fn code_matches(image: &rk_core::program::Image, va: Addr, pattern: &[Option<u8>]) -> bool {
    if !image.is_exec(va) || pattern.is_empty() || !image.is_exec(va + pattern.len() as u32 - 1) {
        return false;
    }
    let Some(code) = image.bytes(va, pattern.len()) else {
        return false;
    };
    code.iter()
        .zip(pattern)
        .all(|(b, p)| p.is_none_or(|p| p == *b))
}

pub fn msvc_seh_prolog(image: &rk_core::program::Image, va: Addr) -> Option<Addr> {
    code_matches(image, va, &seh_pattern(MSVC_SEH_PROLOG_HEX, &[1, 2, 3, 4]))
        .then(|| image.rd32(va + 1))
        .flatten()
        .filter(|handler| image.is_exec(*handler))
}

pub fn msvc_seh_epilog(image: &rk_core::program::Image, va: Addr) -> bool {
    code_matches(image, va, &seh_pattern(MSVC_SEH_EPILOG_HEX, &[]))
}

pub fn msvc_unwind_only(image: &rk_core::program::Image, handler: Option<Addr>) -> bool {
    let Some(handler) = handler else {
        return false;
    };
    let mut pattern = vec![
        Some(0xb8),
        None,
        None,
        None,
        None,
        Some(0xe9),
        None,
        None,
        None,
        None,
    ];
    if !code_matches(image, handler, &pattern) {
        return false;
    }
    pattern.clear();
    let Some(info) = image.rd32(handler + 1) else {
        return false;
    };
    matches!(image.rd32(info), Some(0x19930520..=0x19930522)) && image.rd32(info + 12) == Some(0)
}

pub fn msvc_eh3_finally_only(
    image: &rk_core::program::Image,
    va: Addr,
    levels: Option<&std::collections::BTreeSet<u32>>,
) -> bool {
    let Some(levels) = levels else {
        return false;
    };
    if !code_matches(
        image,
        va,
        &seh_pattern(MSVC_EH3_FRAME_HEX, &[6, 7, 8, 9, 11, 12, 13, 14]),
    ) {
        return false;
    }
    let Some(handler) = image.rd32(va + 11) else {
        return false;
    };
    let handler_pattern = seh_pattern(MSVC_EH3_HANDLER_HEX, &[]);
    if !code_matches(image, handler, &handler_pattern) {
        return false;
    }
    let Some(scope) = image.rd32(va + 6) else {
        return false;
    };
    let mut seen = std::collections::BTreeSet::new();
    let mut todo: Vec<u32> = levels.iter().copied().collect();
    while let Some(level) = todo.pop() {
        if level == 0xffff_ffff || !seen.insert(level) {
            continue;
        }
        if level >= 0x100 || image.rd32(scope + 12 * level + 4) != Some(0) {
            return false;
        }
        if let Some(parent) = image.rd32(scope + 12 * level) {
            todo.push(parent);
        }
    }
    true
}

pub fn seh_constructor_helper(image: &rk_core::program::Image, va: Addr) -> Option<Addr> {
    let prefix = "52515384d27c03ff50f431d28d4c2410648b1a8919896908c74104";
    let suffix = "89410c64890a5b595ac3";
    let prefix = seh_pattern(prefix, &[]);
    let suffix = seh_pattern(suffix, &[]);
    let size = prefix.len() + 4 + suffix.len();
    if !image.is_exec(va) || !image.is_exec(va + size as u32 - 1) {
        return None;
    }
    let code = image.bytes(va, size)?;
    if !code[..prefix.len()]
        .iter()
        .zip(&prefix)
        .all(|(b, p)| p.is_none_or(|p| p == *b))
        || !code[prefix.len() + 4..]
            .iter()
            .zip(&suffix)
            .all(|(b, p)| p.is_none_or(|p| p == *b))
    {
        return None;
    }
    let candidate = image.rd32(va + prefix.len() as u32)?;
    image.is_exec(candidate).then_some(candidate)
}
