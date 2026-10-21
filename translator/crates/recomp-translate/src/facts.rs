use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use rk_core::ir::OpCode;
use rk_core::program::Program;
use rk_core::settings::Settings;
use rk_core::{Addr, Diagnostic, Error, Reason, Result};
use rk_frontend::Analysis;

use crate::parse_addr8;

const INDIRECT_FORMAT: &str = "recomp-indirect-targets-v1";
const INDIRECT_HEADER: &str = "site\tkind\ttargets";

#[derive(Default)]
pub struct Facts {
    pub call_facts: BTreeMap<Addr, Vec<Addr>>,
    pub cw_effects: BTreeMap<Addr, (u16, u16, u16)>,
}

pub fn load(settings: &Settings, program: &Program, analysis: &Analysis) -> Result<Facts> {
    let call_facts = match &settings.indirect_targets {
        Some(path) => read_indirect_targets(path, settings, program, analysis)?,
        None => BTreeMap::new(),
    };
    let mut cw_effects = BTreeMap::new();
    for (&site, effect) in &settings.call_effects.sites {
        if let Some(x87) = effect.x87 {
            cw_effects.insert(call_end(program, analysis, site)?.0, x87.control_word());
        }
    }
    Ok(Facts {
        call_facts,
        cw_effects,
    })
}

fn read_indirect_targets(
    path: &Path,
    settings: &Settings,
    program: &Program,
    analysis: &Analysis,
) -> Result<BTreeMap<Addr, Vec<Addr>>> {
    let text = std::fs::read_to_string(path).map_err(|e| Error::io(path, e))?;
    let mut lines = text.lines();
    let first = lines.next().unwrap_or_default();
    let mut fields = first.split('\t');
    if fields.next() != Some(INDIRECT_FORMAT)
        || fields.next() != Some(settings.sha256.to_lowercase().as_str())
    {
        return Err(Error::input(format!(
            "{}: first line must be '{INDIRECT_FORMAT}' and the executable SHA-256",
            path.display()
        )));
    }
    if lines.next() != Some(INDIRECT_HEADER) {
        return Err(Error::input(format!(
            "{}: second line must be '{INDIRECT_HEADER}'",
            path.display()
        )));
    }
    let mut seen = BTreeSet::new();
    let mut facts = BTreeMap::new();
    for (number, line) in lines.enumerate() {
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.len() != 3 {
            return Err(Error::input(format!(
                "{}: line {} must have three tab-separated fields",
                path.display(),
                number + 3
            )));
        }
        let site = parse_addr8(fields[0]).ok_or_else(|| {
            Error::input(format!(
                "{}: invalid site address {:?}",
                path.display(),
                fields[0]
            ))
        })?;
        let kind = fields[1];
        if kind != "call" && kind != "jump" {
            return Err(Error::input(format!(
                "{}: site {site:08x} has unknown kind {kind:?}",
                path.display()
            )));
        }
        if !seen.insert(site) {
            return Err(Error::input(format!(
                "{}: site {site:08x} repeats",
                path.display()
            )));
        }
        let mut targets = BTreeSet::new();
        for field in fields[2].split(',') {
            let target = parse_addr8(field).ok_or_else(|| {
                Error::input(format!(
                    "{}: site {site:08x} has invalid target {field:?}",
                    path.display()
                ))
            })?;
            if !program.functions.contains_key(&target) {
                return Err(Error::input(format!(
                    "{}: site {site:08x} target {target:08x} is not a function entry",
                    path.display()
                )));
            }
            targets.insert(target);
        }
        if kind == "jump" {
            continue;
        }
        let (end, indirect) = call_end(program, analysis, site)?;
        if !indirect {
            return Err(Error::input(format!(
                "{}: site {site:08x} is not an indirect call",
                path.display()
            )));
        }
        facts.insert(end, targets.into_iter().collect());
    }
    Ok(facts)
}

fn call_end(program: &Program, analysis: &Analysis, site: Addr) -> Result<(Addr, bool)> {
    let owner = program.entry_owner(site).ok_or_else(|| {
        Diagnostic::error(
            site,
            Reason::Unsupported {
                detail: "call site lies outside every mapped function".to_string(),
            },
        )
    })?;
    let ir = match analysis.ir.get(&owner) {
        Some(Ok(ir)) => ir,
        Some(Err(error)) => return Err(Error::input(format!("site {site:08x}: {error}"))),
        None => {
            return Err(Diagnostic::error(
                site,
                Reason::Unsupported {
                    detail: "call site has no function IR".to_string(),
                },
            ))
        }
    };
    let index = ir.index_of(site).ok_or_else(|| {
        Diagnostic::error(
            site,
            Reason::Unsupported {
                detail: "call site is not an instruction boundary".to_string(),
            },
        )
    })?;
    let insn = &ir.insns[index as usize];
    let call = insn
        .ops
        .iter()
        .any(|op| matches!(op.opcode, OpCode::Call | OpCode::CallInd));
    if !call {
        return Err(Diagnostic::error(
            site,
            Reason::Unsupported {
                detail: "instruction is not a call".to_string(),
            },
        ));
    }
    let indirect = insn.ops.iter().any(|op| op.opcode == OpCode::CallInd);
    Ok((insn.end(), indirect))
}
