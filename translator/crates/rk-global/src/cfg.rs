use std::borrow::Cow;
use std::collections::BTreeSet;

use rk_core::ir::{FunctionIr, Insn, OpCode};

pub(crate) fn closed(fir: &FunctionIr, tails: &BTreeSet<u32>) -> bool {
    for (index, insn) in fir.insns.iter().enumerate() {
        let index = index as u32;
        if tails.contains(&index) {
            continue;
        }
        if fir.exits.contains_key(&index) {
            return false;
        }
        let mnemonic = mnemonic(insn);
        let mnemonic = mnemonic.as_ref();
        let succ = &fir.succ[index as usize];
        if mnemonic == "RET" {
            if !succ.is_empty() {
                return false;
            }
        } else if mnemonic == "JMP" {
            if succ.is_empty() {
                return false;
            }
        } else if mnemonic == "CALL" {
            if succ.is_empty() && !fir.noreturn.contains_key(&index) {
                return false;
            }
        } else if succ.is_empty() && !is_return(insn) && !fir.noreturn.contains_key(&index) {
            return false;
        }
    }
    true
}

pub(crate) fn is_return(insn: &Insn) -> bool {
    insn.mnemonic.eq_ignore_ascii_case("RET")
        || insn.ops.iter().any(|op| op.opcode == OpCode::Return)
}

pub(crate) fn mnemonic(insn: &Insn) -> Cow<'_, str> {
    let name = insn.mnemonic.as_str();
    let name = name
        .get(..5)
        .filter(|prefix| prefix.eq_ignore_ascii_case("WAIT "))
        .map_or(name, |_| &name[5..]);
    if name.bytes().any(|byte| byte.is_ascii_lowercase()) {
        Cow::Owned(name.to_ascii_uppercase())
    } else {
        Cow::Borrowed(name)
    }
}
