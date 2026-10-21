use std::collections::BTreeMap;

use rayon::prelude::*;
use rk_core::ir::{FunctionIr, Policy};
use rk_core::program::Image;
use rk_core::{Addr, Diagnostic, Error, Result};
use rk_ir::{lower, CgFunction};

use crate::{build, publications, verify, BuildOptions, CallFacts, PrepareOptions};

#[derive(Clone, Debug, Default)]
pub struct SweepReport {
    pub functions: usize,
    pub built: usize,
    pub failed: usize,
    pub values: usize,
    pub verify_failed: usize,
    pub prepared: usize,
    pub prepared_failed: usize,
    pub prepared_verify_failed: usize,
    pub prepared_values: usize,
    pub live_raw: usize,
    pub live_prepared: usize,
    pub instructions: usize,
    pub slow: BTreeMap<String, usize>,
    pub diagnostics: BTreeMap<String, usize>,
    pub seconds: f64,
    pub errors: Vec<Diagnostic>,
}

struct Built {
    values: usize,
    verify_failed: bool,
    prepared: bool,
    prepared_failed: bool,
    prepared_verify_failed: bool,
    prepared_values: usize,
    live_raw: usize,
    live_prepared: usize,
    instructions: usize,
    slow: BTreeMap<String, usize>,
    diagnostics: BTreeMap<String, usize>,
    error: Option<Diagnostic>,
    verify_error: Option<Diagnostic>,
}

pub fn sweep(
    functions: &BTreeMap<Addr, std::result::Result<FunctionIr, Error>>,
    image: &Image,
    call_facts: &dyn CallFacts,
    x87: &dyn rk_ir::X87Recipes,
) -> Result<SweepReport> {
    let started = std::time::Instant::now();
    let mut rows: Vec<(Addr, std::result::Result<Built, Error>)> = functions
        .par_iter()
        .map(|(&addr, result)| (addr, build_one(addr, result, image, call_facts, x87)))
        .collect();
    rows.sort_by_key(|(addr, _)| *addr);

    let mut report = SweepReport { functions: functions.len(), ..SweepReport::default() };
    for (_, row) in rows {
        let built = row?;
        report.instructions += built.instructions;
        for (family, count) in built.slow {
            *report.slow.entry(family).or_default() += count;
        }
        for (reason, count) in built.diagnostics {
            *report.diagnostics.entry(reason).or_default() += count;
        }
        match built.error {
            Some(error) => {
                report.failed += 1;
                if report.errors.len() < 128 {
                    report.errors.push(error);
                }
            }
            None => {
                report.built += 1;
                report.values += built.values;
            }
        }
        report.verify_failed += built.verify_failed as usize;
        report.prepared += built.prepared as usize;
        report.prepared_failed += built.prepared_failed as usize;
        report.prepared_verify_failed += built.prepared_verify_failed as usize;
        report.prepared_values += built.prepared_values;
        report.live_raw += built.live_raw;
        report.live_prepared += built.live_prepared;
        if let Some(error) = built.verify_error {
            if report.errors.len() < 128 {
                report.errors.push(error);
            }
        }
    }
    report.seconds = started.elapsed().as_secs_f64();
    Ok(report)
}

fn build_one(
    _addr: Addr,
    result: &std::result::Result<FunctionIr, Error>,
    image: &Image,
    call_facts: &dyn CallFacts,
    x87: &dyn rk_ir::X87Recipes,
) -> std::result::Result<Built, Error> {
    let fir = result.as_ref().map_err(|error| Error::Input(error.to_string()))?;
    let cg: CgFunction = lower(fir, image, x87)?;
    let mut slow = BTreeMap::new();
    for item in &cg.slow {
        *slow.entry(format!("{:?}", item.family)).or_default() += 1;
    }
    let mut diagnostics = BTreeMap::new();
    for diagnostic in &cg.diagnostics {
        *diagnostics.entry(reason_name(diagnostic)).or_default() += 1;
    }
    let options = BuildOptions {
        policy: Policy::Conservative,
        call_facts,
        allow_slow: true,
        value_budget: crate::DEFAULT_VALUE_BUDGET,
    };
    let (values, error, mut verify_failed, mut verify_error, live_raw) = match build(&cg, &options) {
        Ok(ssa) => {
            let live = crate::live_values(&ssa, None, &[], &[]).len();
            match verify(&ssa) {
                Ok(()) => (ssa.values.len(), None, false, None, live),
                Err(diagnostic) => (ssa.values.len(), None, true, Some(diagnostic), live),
            }
        }
        Err(diagnostic) => (0, Some(diagnostic), false, None, 0),
    };
    let mut prepared = false;
    let mut prepared_failed = false;
    let mut prepared_verify_failed = false;
    let mut prepared_values = 0;
    let mut live_prepared = 0;
    if error.is_none() {
        let prepare_options = PrepareOptions::new(call_facts);
        match crate::prepare(&cg, &prepare_options) {
            Ok(ssa) => {
                prepared = true;
                prepared_values = ssa.values.len();
                let plan = publications(&ssa, call_facts);
                live_prepared = crate::live_values(&ssa, Some(&plan), &[], &[]).len();
                match verify(&ssa) {
                    Ok(()) => {}
                    Err(diagnostic) => {
                        prepared_verify_failed = true;
                        verify_failed = true;
                        if verify_error.is_none() {
                            verify_error = Some(diagnostic);
                        }
                    }
                }
            }
            Err(rk_core::Error::Diagnostic(diagnostic)) => {
                prepared_failed = true;
                if verify_error.is_none() {
                    verify_error = Some(diagnostic);
                }
            }
            Err(_) => prepared_failed = true,
        }
    }
    Ok(Built {
        values,
        verify_failed,
        prepared,
        prepared_failed,
        prepared_verify_failed,
        prepared_values,
        live_raw,
        live_prepared,
        instructions: fir.insns.len(),
        slow,
        diagnostics,
        error,
        verify_error,
    })
}

fn reason_name(diagnostic: &Diagnostic) -> String {
    serde_json::to_value(&diagnostic.reason)
        .ok()
        .and_then(|value| value.get("reason").and_then(|v| v.as_str()).map(str::to_string))
        .unwrap_or_else(|| "unknown".into())
}
