mod callgraph;
mod contracts;
mod modes;
mod private;

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;

use rk_core::contract::{Contract, Field};
use rk_core::program::Program;
use rk_core::settings::Settings;
use rk_core::{Addr, Result};
use rk_frontend::Analysis;
use rk_ssa::{CallEffect, CallFacts};

pub use modes::{ModeCondition, ReturnSummary, X87ModeFacts, CW_MASK, CW_PC_RC};
pub use private::{field_by_name, specialize, PrivateAnalysis, PrivateLayout};

pub struct ContractCallFacts<'a> {
    contracts: &'a BTreeMap<Addr, Contract>,
    indirect: &'a BTreeMap<Addr, Contract>,
}

impl CallFacts for ContractCallFacts<'_> {
    fn call(&self, site: Addr, target: Option<Addr>) -> CallEffect {
        let contract = match target {
            Some(target) => self.contracts.get(&target).copied(),
            None => self.indirect.get(&site).copied(),
        };
        match contract {
            Some(contract) => CallEffect {
                reads: contract.reads,
                kills: contract.kills,
                writes: contract.writes,
                preserves: contract.preserves,
            },
            None => CallEffect::CONSERVATIVE,
        }
    }
}

impl From<&X87ModeFacts> for rk_x87::ModeInput {
    fn from(facts: &X87ModeFacts) -> Self {
        Self {
            known_modes: facts.known_modes.clone(),
            sites: facts.sites.clone(),
        }
    }
}

pub struct GlobalInput<'a> {
    pub settings: &'a Settings,
    pub program: &'a Program,
    pub analysis: &'a Analysis,
    pub indirect: &'a BTreeMap<Addr, Vec<Addr>>,
}

#[derive(Clone, Debug, Default)]
pub struct PrivateFacts {
    pub targets: BTreeMap<Addr, Contract>,
    pub layouts: BTreeMap<Addr, Vec<PrivateLayout>>,
    pub masks: BTreeMap<Addr, BTreeSet<u16>>,
    pub input_arguments: BTreeMap<Addr, Field>,
    pub checked_returns: BTreeSet<Addr>,
    pub callers: BTreeSet<Addr>,
    pub rejections: BTreeMap<Addr, rk_core::Reason>,
    pub known_modes: BTreeSet<Addr>,
}

#[derive(Clone, Debug, Default)]
pub struct GlobalReport {
    pub functions: usize,
    pub contract_excluded: BTreeMap<String, usize>,
    pub contracts: usize,
    pub indirect_contracts: usize,
    pub sccs: usize,
    pub private_targets: usize,
    pub private_variants: usize,
    pub checked_returns: usize,
    pub selected_callers: usize,
    pub known_modes: usize,
    pub instruction_checks: usize,
    pub instruction_mismatches: BTreeMap<Addr, (usize, usize)>,
}

#[derive(Clone, Debug, Default)]
pub struct GlobalFacts {
    pub contracts: BTreeMap<Addr, Contract>,
    pub indirect_contracts: BTreeMap<Addr, Contract>,
    pub private: PrivateFacts,
    pub x87: X87ModeFacts,
    pub report: GlobalReport,
}

impl GlobalFacts {
    pub fn analyze(input: &GlobalInput<'_>) -> Result<Self> {
        let opaque_helpers: BTreeSet<Addr> = input.analysis.seh_helpers.clone();
        let modes = modes::analyze(
            input.analysis,
            input.indirect,
            &input
                .program
                .functions
                .iter()
                .filter_map(|(&addr, function)| function.noreturn.then_some(addr))
                .collect(),
            &opaque_helpers,
        );
        let native = input.settings.native_replaced()?;
        let contracts = contracts::analyze(input.program, input.analysis, input.indirect, &native)?;
        let mut opaque = opaque_helpers;
        opaque.extend(contracts.rejected.keys().copied());
        let private = private::analyze(&private::PrivateInput {
            settings: input.settings,
            analysis: input.analysis,
            contracts: &contracts,
            modes: &modes,
            opaque: &opaque,
        });
        let mut report = GlobalReport {
            functions: input.program.functions.len(),
            contracts: contracts.contracts.len(),
            indirect_contracts: contracts.indirect.len(),
            private_targets: private.targets.len(),
            private_variants: private.masks.values().map(BTreeSet::len).sum(),
            checked_returns: private.checked_returns.len(),
            selected_callers: private.callers.len(),
            known_modes: private.known_modes.len(),
            sccs: modes.sccs,
            instruction_checks: contracts.instruction_checks,
            instruction_mismatches: contracts.instruction_mismatches,
            ..GlobalReport::default()
        };
        for reason in contracts.rejected.values() {
            let name = match reason {
                rk_core::Reason::Conservative { detail } => detail.clone(),
                other => format!("{other:?}"),
            };
            *report.contract_excluded.entry(name).or_default() += 1;
        }
        Ok(Self {
            contracts: contracts.contracts,
            indirect_contracts: contracts.indirect,
            private: PrivateFacts {
                targets: private.targets,
                layouts: private.layouts,
                masks: private.masks,
                input_arguments: private.input_arguments,
                checked_returns: private.checked_returns,
                callers: private.callers,
                rejections: private.rejections,
                known_modes: private.known_modes.clone(),
            },
            x87: modes.facts,
            report,
        })
    }

    pub fn call_facts(&self) -> ContractCallFacts<'_> {
        ContractCallFacts {
            contracts: &self.contracts,
            indirect: &self.indirect_contracts,
        }
    }

    pub fn summary(&self) -> String {
        format_report(&self.report)
    }

    pub fn dump(&self, out: &mut impl Write) -> std::io::Result<()> {
        writeln!(out, "global report {}", format_report(&self.report))?;
        for (&addr, &(expected, visited)) in self.report.instruction_mismatches.iter().take(8) {
            writeln!(
                out,
                "instruction-mismatch {:08x} expected={expected} visited={visited}",
                addr
            )?;
        }
        writeln!(out, "contracts")?;
        for (&addr, contract) in &self.contracts {
            writeln!(
                out,
                "contract {:08x} reads={} kills={} writes={} preserves={}",
                addr,
                fields(contract.reads),
                fields(contract.kills),
                fields(contract.writes),
                fields(contract.preserves),
            )?;
        }
        writeln!(out, "indirect-contracts")?;
        for (&site, contract) in &self.indirect_contracts {
            writeln!(
                out,
                "indirect {:08x} reads={} kills={} writes={} preserves={}",
                site,
                fields(contract.reads),
                fields(contract.kills),
                fields(contract.writes),
                fields(contract.preserves),
            )?;
        }
        writeln!(out, "private")?;
        for (&addr, contract) in &self.private.targets {
            writeln!(
                out,
                "private {:08x} reads={} kills={} writes={} preserves={}",
                addr,
                fields(contract.reads),
                fields(contract.kills),
                fields(contract.writes),
                fields(contract.preserves),
            )?;
        }
        for addr in &self.private.checked_returns {
            writeln!(out, "checked {:08x}", addr)?;
        }
        for addr in &self.private.callers {
            writeln!(out, "caller {:08x}", addr)?;
        }
        for (&addr, layouts) in &self.private.layouts {
            for layout in layouts {
                writeln!(
                    out,
                    "layout {:08x} mask={:04x} inputs={} argument={}",
                    addr,
                    layout.output_mask(),
                    fields(layout.inputs),
                    layout.input_argument.map_or("-", Field::name),
                )?;
            }
        }
        for (&addr, field) in &self.private.input_arguments {
            writeln!(out, "input-argument {:08x} {}", addr, field.name())?;
        }
        for addr in &self.private.known_modes {
            writeln!(out, "known-mode {:08x}", addr)?;
        }
        writeln!(out, "x87")?;
        for (&addr, summary) in &self.x87.returns {
            writeln!(
                out,
                "x87-return {:08x} preserved={:04x} known={:04x} value={:04x}",
                addr, summary.preserved, summary.known, summary.value
            )?;
        }
        Ok(())
    }
}



fn fields(fields: rk_core::contract::Fields) -> String {
    let names: Vec<&str> = fields.iter().map(Field::name).collect();
    if names.is_empty() {
        "-".to_string()
    } else {
        names.join(",")
    }
}

fn format_report(report: &GlobalReport) -> String {
    let excluded: Vec<String> = report
        .contract_excluded
        .iter()
        .map(|(reason, count)| format!("{reason}={count}"))
        .collect();
    format!(
        "functions={} contracts={} indirect={} excluded=[{}] private={} variants={} checked={} callers={} known_modes={} instruction_checks={} instruction_mismatches={}",
        report.functions,
        report.contracts,
        report.indirect_contracts,
        excluded.join(","),
        report.private_targets,
        report.private_variants,
        report.checked_returns,
        report.selected_callers,
        report.known_modes,
        report.instruction_checks,
        report.instruction_mismatches.len(),
    )
}
