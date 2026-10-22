use rk_core::ir::Insn;

#[derive(Clone, Debug)]
pub(crate) enum SlowOperand {
    Gpr { index: usize, shift: u32, mask: u32 },
    Mm(u8),
    Xmm(u8),
    Imm(u64),
    Mem(String),
}

pub(crate) fn parse_slow_operands(insn: &Insn) -> Option<Vec<SlowOperand>> {
    if insn.operands.is_empty() {
        return Some(Vec::new());
    }
    insn.operands.split(',').map(parse_slow_operand).collect()
}

pub(crate) fn parse_slow_operand(text: &str) -> Option<SlowOperand> {
    let mut text = text.trim();
    let lower = text.to_ascii_lowercase();
    for prefix in [
        "extended double ptr ",
        "float ptr ",
        "xmmword ptr ",
        "mmword ptr ",
        "qword ptr ",
        "dword ptr ",
        "word ptr ",
        "byte ptr ",
    ] {
        if lower.starts_with(prefix) {
            text = &text[prefix.len()..];
            break;
        }
    }
    let mut segment = None;
    if let Some((prefix, rest)) = text.split_once(':') {
        let name = prefix.trim().to_ascii_uppercase();
        if matches!(name.as_str(), "CS" | "DS" | "ES" | "FS" | "GS" | "SS") {
            segment = Some(name);
            text = rest.trim();
        }
    }
    if text.starts_with('[') {
        return Some(SlowOperand::Mem(memory_expr(text, segment.as_deref())?));
    }
    if let Some(index) = text.strip_prefix("MM").and_then(|n| n.parse().ok()) {
        return Some(SlowOperand::Mm(index));
    }
    if let Some(index) = text.strip_prefix("XMM").and_then(|n| n.parse().ok()) {
        return Some(SlowOperand::Xmm(index));
    }
    if let Some((index, shift, mask)) = gpr_parts(text) {
        return Some(SlowOperand::Gpr { index, shift, mask });
    }
    if let Some(value) = parse_imm(text) {
        return Some(SlowOperand::Imm(value));
    }
    None
}

pub(crate) fn parse_imm(text: &str) -> Option<u64> {
    let text = text.trim();
    let (negative, digits) = text
        .strip_prefix('-')
        .map_or((false, text), |digits| (true, digits));
    let value = if let Some(hex) = digits.strip_prefix("0x") {
        u64::from_str_radix(hex, 16).ok()?
    } else {
        digits.parse().ok()?
    };
    Some(if negative { value.wrapping_neg() } else { value })
}

pub(crate) fn memory_expr(text: &str, segment: Option<&str>) -> Option<String> {
    let inner = text.trim().strip_prefix('[')?.strip_suffix(']')?.trim();
    let mut terms = Vec::new();
    for part in inner.split('+').map(str::trim).filter(|part| !part.is_empty()) {
        if let Some((register, scale)) = part.split_once('*') {
            let register = gpr_expr(register.trim())?;
            let scale = parse_imm(scale.trim())?;
            terms.push(format!("({register} * 0x{scale:x}u)"));
        } else if let Some(value) = parse_imm(part) {
            terms.push(format!("0x{value:x}u"));
        } else {
            terms.push(gpr_expr(part)?);
        }
    }
    if segment == Some("FS") {
        terms.push("c->fs_base".to_string());
    }
    Some(if terms.is_empty() { "0u".to_string() } else { terms.join(" + ") })
}

pub(crate) fn gpr_parts(name: &str) -> Option<(usize, u32, u32)> {
    const R32: [&str; 8] = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"];
    const R16: [&str; 8] = ["AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI"];
    const R8L: [&str; 4] = ["AL", "CL", "DL", "BL"];
    const R8H: [&str; 4] = ["AH", "CH", "DH", "BH"];
    if let Some(index) = R32.iter().position(|r| *r == name) {
        return Some((index, 0, 0xffff_ffff));
    }
    if let Some(index) = R16.iter().position(|r| *r == name) {
        return Some((index, 0, 0xffff));
    }
    if let Some(index) = R8L.iter().position(|r| *r == name) {
        return Some((index, 0, 0xff));
    }
    if let Some(index) = R8H.iter().position(|r| *r == name) {
        return Some((index, 8, 0xff));
    }
    None
}

pub(crate) fn gpr_expr(name: &str) -> Option<String> {
    let (index, shift, mask) = gpr_parts(name)?;
    Some(if shift == 0 && mask == 0xffff_ffff {
        format!("c->r[{index}]")
    } else {
        format!("((c->r[{index}] >> {shift}) & 0x{mask:x}u)")
    })
}

pub(crate) fn port_expr(operand: &SlowOperand) -> Option<String> {
    match operand {
        SlowOperand::Imm(value) => Some(format!("0x{:x}u", value & 0xffff)),
        SlowOperand::Gpr { index, shift, .. } => Some(format!(
            "((c->r[{index}] >> {shift}) & 0xffffu)"
        )),
        _ => None,
    }
}

pub(crate) fn gpr_size(mask: u32) -> usize {
    if mask == 0xff {
        1
    } else if mask == 0xffff {
        2
    } else {
        4
    }
}

pub(crate) fn write_gpr(index: usize, shift: u32, mask: u32, expr: &str) -> String {
    if shift == 0 && mask == 0xffff_ffff {
        format!("c->r[{index}] = (uint32_t)({expr});")
    } else {
        format!(
            "c->r[{index}] = (c->r[{index}] & ~(0x{mask:x}u << {shift})) | (((uint32_t)({expr}) & 0x{mask:x}u) << {shift});"
        )
    }
}

pub(crate) fn mmx_source(operand: &SlowOperand, width: u32) -> Option<String> {
    match operand {
        SlowOperand::Mm(index) => Some(format!("c->mm[{index}]")),
        SlowOperand::Gpr { index, shift, mask } => {
            let read = if *shift == 0 && *mask == 0xffff_ffff {
                format!("c->r[{index}]")
            } else {
                format!("((c->r[{index}] >> {shift}) & 0x{mask:x}u)")
            };
            Some(format!("(uint64_t)({read})"))
        }
        SlowOperand::Imm(value) => Some(format!("(uint64_t)0x{:x}u", value & 0xff)),
        SlowOperand::Mem(address) => Some(if width == 32 {
            format!("(uint64_t)recomp_rd32_(g_mem, (uint32_t)({address}))")
        } else {
            format!("recomp_rd64_(g_mem, (uint32_t)({address}))")
        }),
        _ => None,
    }
}

pub(crate) fn sse_logic(mnemonic: &str) -> Option<&'static str> {
    Some(match mnemonic {
        "ORPS" | "ORPD" => "|",
        "XORPS" | "XORPD" => "^",
        "ANDPS" | "ANDPD" | "ANDNPS" | "ANDNPD" => "&",
        _ => return None,
    })
}

pub(crate) fn mmx_shift(mnemonic: &str) -> Option<(&'static str, u32)> {
    Some(match mnemonic {
        "PSRLW" => ("mmx_psrl", 16),
        "PSRLD" => ("mmx_psrl", 32),
        "PSRLQ" => ("mmx_psrl", 64),
        "PSRAW" => ("mmx_psra", 16),
        "PSRAD" => ("mmx_psra", 32),
        "PSLLW" => ("mmx_psll", 16),
        "PSLLD" => ("mmx_psll", 32),
        "PSLLQ" => ("mmx_psll", 64),
        _ => return None,
    })
}

pub(crate) fn mmx_binary(mnemonic: &str) -> Option<(&'static str, u32)> {
    Some(match mnemonic {
        "PADDB" => ("mmx_padd", 8),
        "PADDW" => ("mmx_padd", 16),
        "PADDD" => ("mmx_padd", 32),
        "PADDQ" => ("mmx_padd", 64),
        "PSUBB" => ("mmx_psub", 8),
        "PSUBW" => ("mmx_psub", 16),
        "PSUBD" => ("mmx_psub", 32),
        "PSUBQ" => ("mmx_psub", 64),
        "PADDSB" => ("mmx_padds", 8),
        "PADDSW" => ("mmx_padds", 16),
        "PSUBSB" => ("mmx_psubs", 8),
        "PSUBSW" => ("mmx_psubs", 16),
        "PADDUSB" => ("mmx_paddus", 8),
        "PADDUSW" => ("mmx_paddus", 16),
        "PSUBUSB" => ("mmx_psubus", 8),
        "PSUBUSW" => ("mmx_psubus", 16),
        "PMULLW" => ("mmx_pmullw", 0),
        "PMULHW" => ("mmx_pmulhw", 0),
        "PMADDWD" => ("mmx_pmaddwd", 0),
        "PCMPEQB" => ("mmx_pcmpeq", 8),
        "PCMPEQW" => ("mmx_pcmpeq", 16),
        "PCMPEQD" => ("mmx_pcmpeq", 32),
        "PCMPGTB" => ("mmx_pcmpgt", 8),
        "PCMPGTW" => ("mmx_pcmpgt", 16),
        "PCMPGTD" => ("mmx_pcmpgt", 32),
        "PACKSSWB" => ("mmx_packsswb", 0),
        "PACKSSDW" => ("mmx_packssdw", 0),
        "PACKUSWB" => ("mmx_packuswb", 0),
        "PMINUB" => ("mmx_pminub", 0),
        "PMAXUB" => ("mmx_pmaxub", 0),
        "PUNPCKLBW" => ("mmx_punpckl", 8),
        "PUNPCKLWD" => ("mmx_punpckl", 16),
        "PUNPCKLDQ" => ("mmx_punpckl", 32),
        "PUNPCKHBW" => ("mmx_punpckh", 8),
        "PUNPCKHWD" => ("mmx_punpckh", 16),
        "PUNPCKHDQ" => ("mmx_punpckh", 32),
        "PAND" => ("mmx_pand", 0),
        "PANDN" => ("mmx_pandn", 0),
        "POR" => ("mmx_por", 0),
        "PXOR" => ("mmx_pxor", 0),
        _ => return None,
    })
}

pub(crate) fn xmm_binary(mnemonic: &str) -> Option<(&'static str, Option<u32>)> {
    Some(match mnemonic {
        "PADDB" => ("xmm_padd", Some(8)),
        "PADDW" => ("xmm_padd", Some(16)),
        "PADDD" => ("xmm_padd", Some(32)),
        "PADDQ" => ("xmm_padd", Some(64)),
        "PSUBB" => ("xmm_psub", Some(8)),
        "PSUBW" => ("xmm_psub", Some(16)),
        "PSUBD" => ("xmm_psub", Some(32)),
        "PSUBQ" => ("xmm_psub", Some(64)),
        "PADDSB" => ("xmm_padds", Some(8)),
        "PADDSW" => ("xmm_padds", Some(16)),
        "PSUBSB" => ("xmm_psubs", Some(8)),
        "PSUBSW" => ("xmm_psubs", Some(16)),
        "PADDUSB" => ("xmm_paddus", Some(8)),
        "PADDUSW" => ("xmm_paddus", Some(16)),
        "PSUBUSB" => ("xmm_psubus", Some(8)),
        "PSUBUSW" => ("xmm_psubus", Some(16)),
        "PMULLW" => ("xmm_pmullw", None),
        "PMULHW" => ("xmm_pmulhw", None),
        "PMADDWD" => ("xmm_pmaddwd", None),
        "PCMPEQB" => ("xmm_pcmpeq", Some(8)),
        "PCMPEQW" => ("xmm_pcmpeq", Some(16)),
        "PCMPEQD" => ("xmm_pcmpeq", Some(32)),
        "PCMPGTB" => ("xmm_pcmpgt", Some(8)),
        "PCMPGTW" => ("xmm_pcmpgt", Some(16)),
        "PCMPGTD" => ("xmm_pcmpgt", Some(32)),
        "PMINUB" => ("xmm_pminub", None),
        "PMAXUB" => ("xmm_pmaxub", None),
        "PAND" => ("xmm_pand", None),
        "PANDN" => ("xmm_pandn", None),
        "POR" => ("xmm_por", None),
        "PXOR" => ("xmm_pxor", None),
        _ => return None,
    })
}

pub(crate) fn xmm_shift(mnemonic: &str) -> Option<(&'static str, u32)> {
    Some(match mnemonic {
        "PSRLW" => ("xmm_psrl", 16),
        "PSRLD" => ("xmm_psrl", 32),
        "PSRLQ" => ("xmm_psrl", 64),
        "PSRAW" => ("xmm_psra", 16),
        "PSRAD" => ("xmm_psra", 32),
        "PSLLW" => ("xmm_psll", 16),
        "PSLLD" => ("xmm_psll", 32),
        "PSLLQ" => ("xmm_psll", 64),
        _ => return None,
    })
}

pub(crate) fn xmm_pack(mnemonic: &str) -> Option<(u32, u32)> {
    Some(match mnemonic {
        "PACKSSWB" => (16, 1),
        "PACKSSDW" => (32, 1),
        "PACKUSWB" => (16, 0),
        _ => return None,
    })
}

pub(crate) fn xmm_unpack(mnemonic: &str) -> Option<(u32, u32)> {
    Some(match mnemonic {
        "PUNPCKLBW" => (8, 0),
        "PUNPCKLWD" => (16, 0),
        "PUNPCKLDQ" => (32, 0),
        "PUNPCKHBW" => (8, 1),
        "PUNPCKHWD" => (16, 1),
        "PUNPCKHDQ" => (32, 1),
        _ => return None,
    })
}

