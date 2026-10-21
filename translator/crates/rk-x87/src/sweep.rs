use std::collections::BTreeMap;

use rayon::prelude::*;
use rk_core::ir::Policy;
use rk_core::program::Image;
use rk_core::Addr;
use rk_frontend::Analysis;
use rk_ir::x87::X87Op;
use rk_ir::{lower, Op, SlowFamily};
use rk_ssa::{build, prepare, verify, BuildOptions, ConservativeCallFacts, PrepareOptions, DEFAULT_VALUE_BUDGET};

use crate::Recipes;

#[derive(Clone, Debug, Default)]
pub struct X87Report {
    pub functions: usize,
    pub built: usize,
    pub failed: usize,
    pub recipes: BTreeMap<&'static str, usize>,
    pub residual: BTreeMap<String, usize>,
    pub residual_total: usize,
    pub build_failed: usize,
    pub verify_failed: usize,
    pub prepare_failed: usize,
    pub prepared_verify_failed: usize,
    pub plans: usize,
    pub plan_skipped: usize,
    pub plan_failed: usize,
    pub plan_panics: usize,
    pub plan_errors: BTreeMap<String, usize>,
    pub errors: Vec<(Addr, String)>,
    pub seconds: f64,
}

struct Row {
    recipes: BTreeMap<&'static str, usize>,
    residual: BTreeMap<String, usize>,
    residual_total: usize,
    build_failed: bool,
    verify_failed: bool,
    prepare_failed: bool,
    prepared_verify_failed: bool,
    plans: usize,
    plan_skipped: usize,
    plan_failed: usize,
    plan_panics: usize,
    plan_errors: BTreeMap<String, usize>,
    error: Option<(Addr, String)>,
}

pub fn sweep(analysis: &Analysis, image: &Image) -> X87Report {
    let started = std::time::Instant::now();
    let mut rows: Vec<(Addr, Row)> = analysis
        .ir
        .par_iter()
        .map(|(&addr, result)| (addr, one(addr, result, image)))
        .collect();
    rows.sort_by_key(|(addr, _)| *addr);
    let mut report = X87Report { functions: analysis.ir.len(), ..X87Report::default() };
    for (addr, row) in rows {
        for (name, count) in row.recipes {
            *report.recipes.entry(name).or_default() += count;
        }
        for (name, count) in row.residual {
            *report.residual.entry(name).or_default() += count;
        }
        report.residual_total += row.residual_total;
        report.build_failed += row.build_failed as usize;
        report.verify_failed += row.verify_failed as usize;
        report.prepare_failed += row.prepare_failed as usize;
        report.prepared_verify_failed += row.prepared_verify_failed as usize;
        report.plans += row.plans;
        report.plan_skipped += row.plan_skipped;
        report.plan_failed += row.plan_failed;
        report.plan_panics += row.plan_panics;
        for (reason, count) in row.plan_errors {
            *report.plan_errors.entry(reason).or_default() += count;
        }
        match row.error {
            Some(error) => {
                report.failed += 1;
                if report.errors.len() < 64 {
                    report.errors.push(error);
                }
            }
            None => report.built += 1,
        }
        let _ = addr;
    }
    report.seconds = started.elapsed().as_secs_f64();
    report
}

fn one(addr: Addr, result: &rk_core::Result<rk_core::ir::FunctionIr>, image: &Image) -> Row {
    let mut row = Row {
        recipes: BTreeMap::new(),
        residual: BTreeMap::new(),
        residual_total: 0,
        build_failed: false,
        verify_failed: false,
        prepare_failed: false,
        prepared_verify_failed: false,
        plans: 0,
        plan_skipped: 0,
        plan_failed: 0,
        plan_panics: 0,
        plan_errors: BTreeMap::new(),
        error: None,
    };
    let fir = match result {
        Ok(fir) => fir,
        Err(error) => {
            row.error = Some((addr, error.to_string()));
            return row;
        }
    };
    let cg = match lower(fir, image, &Recipes) {
        Ok(cg) => cg,
        Err(error) => {
            row.error = Some((addr, error.to_string()));
            return row;
        }
    };
    for insn in &cg.insns {
        for op in &insn.ops {
            if let Op::X87(op) = op.op {
                *row.recipes.entry(recipe(op)).or_default() += 1;
            }
        }
    }
    for slow in &cg.slow {
        if slow.family != SlowFamily::X87 {
            continue;
        }
        let name = fir
            .insns
            .binary_search_by_key(&slow.addr, |insn| insn.addr)
            .map(|index| fir.insns[index].mnemonic.to_ascii_uppercase())
            .unwrap_or_else(|_| "<unknown>".into());
        row.residual_total += 1;
        *row.residual.entry(name).or_default() += 1;
    }
    let options = BuildOptions {
        policy: Policy::Optimizing,
        call_facts: &ConservativeCallFacts,
        allow_slow: true,
        value_budget: DEFAULT_VALUE_BUDGET,
    };
    match build(&cg, &options) {
        Ok(ssa) => {
            if let Err(error) = verify(&ssa) {
                row.verify_failed = true;
                row.error = Some((addr, error.to_string()));
            }
        }
        Err(error) => {
            row.build_failed = true;
            row.error = Some((addr, error.to_string()));
        }
    }
    match prepare(&cg, &PrepareOptions::default()) {
        Ok(ssa) => {
            if let Err(error) = verify(&ssa) {
                row.prepared_verify_failed = true;
                if row.error.is_none() {
                    row.error = Some((addr, error.to_string()));
                }
            }
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                crate::prepare(&ssa, &crate::ModeInput::default(), &crate::Options::default())
            })) {
                Ok(Ok(plan)) if plan.used => row.plans += 1,
                Ok(Ok(_)) => row.plan_skipped += 1,
                Ok(Err(reason)) => {
                    row.plan_failed += 1;
                    *row.plan_errors.entry(format!("{reason:?}")).or_default() += 1;
                }
                Err(_) => row.plan_panics += 1,
            }
        }
        Err(error) => {
            row.prepare_failed = true;
            if row.error.is_none() {
                row.error = Some((addr, error.to_string()));
            }
        }
    }
    row
}

fn recipe(op: X87Op) -> &'static str {
    match op {
        X87Op::Constant(_) => "Constant",
        X87Op::Push { .. } => "Push",
        X87Op::PushInteger { .. } => "PushInteger",
        X87Op::Store { .. } => "Store",
        X87Op::Arithmetic(_) => "Arithmetic",
        X87Op::Compare { .. } => "Compare",
        X87Op::CompareFlags { .. } => "CompareFlags",
        X87Op::Unary(_) => "Unary",
        X87Op::Remainder { .. } => "Remainder",
        X87Op::Exchange { .. } => "Exchange",
        X87Op::Free { .. } => "Free",
        X87Op::Status => "Status",
        X87Op::Environment(_) => "Environment",
        X87Op::Clear => "Clear",
        X87Op::Init => "Init",
        X87Op::Rotate(_) => "Rotate",
        X87Op::Conditional(..) => "Conditional",
        X87Op::Extract => "Extract",
        X87Op::LegacyEnvironment(_) => "LegacyEnvironment",
        X87Op::EmptyTags(_) => "EmptyTags",
        X87Op::Nop => "Nop",
        X87Op::Eager(_) => "Eager",
    }
}
