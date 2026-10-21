mod facts;

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write as _;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use clap::{Parser, ValueEnum};
use rayon::prelude::*;
use rk_core::contract::Fields;
use rk_core::ir::FunctionIr;
use rk_core::program::Program;
use rk_core::settings::Settings;
use rk_core::timing::Phases;
use rk_core::{parse_hex, Addr, Diagnostic, Hex};
use rk_output::{Body, Requires, Translation};
use rk_sleigh::Lifter;

#[derive(Parser)]
#[command(about = "Translate a game image from its code map to C")]
struct Args {
    #[arg(long, help = "resolved translation input written by tools/build.py")]
    input: PathBuf,
    #[arg(long, help = "directory receiving the generated sources")]
    out: Option<PathBuf>,
    #[arg(long, help = "translate one [modules.aux] image")]
    module: Option<String>,
    #[arg(long)]
    report: Option<PathBuf>,
    #[arg(long)]
    quiet: bool,
    #[arg(long)]
    jobs: Option<usize>,
    #[arg(long, value_parser = parse_addr, help = "restrict dumps to these functions or entries")]
    only: Vec<Addr>,
    #[arg(long, value_enum)]
    dump: Vec<Stage>,
    #[arg(long, value_enum, default_value_t = X87Mode::Auto)]
    x87_mode: X87Mode,
    #[arg(long, help = "run whole-image SSA and x87 verification sweeps")]
    check: bool,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, ValueEnum)]
enum X87Mode {
    #[default]
    Auto,
    Dynamic,
    Fast,
}

impl From<X87Mode> for rk_x87::ModeForce {
    fn from(mode: X87Mode) -> Self {
        match mode {
            X87Mode::Auto => Self::Auto,
            X87Mode::Dynamic => Self::Dynamic,
            X87Mode::Fast => Self::Fast,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Stage {
    Program,
    Fir,
    Ssa,
    Prepared,
    Global,
    Bodies,
    All,
}

impl Args {
    fn wants(&self, stage: Stage) -> bool {
        self.dump.iter().any(|s| *s == stage || *s == Stage::All)
    }
}

fn parse_addr(text: &str) -> std::result::Result<Addr, String> {
    parse_hex(text).ok_or_else(|| format!("{text:?} is not a hexadecimal address"))
}

fn parse_addr8(text: &str) -> Option<Addr> {
    (text.len() == 8 && text.bytes().all(|b| b.is_ascii_hexdigit()))
        .then(|| Addr::from_str_radix(text, 16).ok())
        .flatten()
}

struct SleighLengths {
    cache: std::sync::OnceLock<Box<[std::sync::atomic::AtomicU8]>>,
}

thread_local! {
    static SLEIGH_LIFTER: std::cell::RefCell<Option<Lifter>> = const { std::cell::RefCell::new(None) };
}

impl rk_discover::DecodeCheck for SleighLengths {
    fn length(&self, image: &rk_core::program::Image, at: Addr) -> Option<u8> {
        let cache = self.cache.get_or_init(|| {
            (0..image.size)
                .map(|_| std::sync::atomic::AtomicU8::new(0))
                .collect()
        });
        let slot = cache.get(at.saturating_sub(image.base) as usize)?;
        let cached = slot.load(std::sync::atomic::Ordering::Relaxed);
        if cached != 0 {
            return (cached != u8::MAX).then_some(cached);
        }
        let available = image.end().saturating_sub(at).min(15) as usize;
        let bytes = image.bytes(at, available)?;
        let out = SLEIGH_LIFTER.with(|lifter| {
            let mut lifter = lifter.borrow_mut();
            let lifter =
                lifter.get_or_insert_with(|| Lifter::new().expect("SLEIGH initialization"));
            lifter.length(at, bytes).ok()
        });
        slot.store(out.unwrap_or(u8::MAX), std::sync::atomic::Ordering::Relaxed);
        out
    }
}

type BodyRow = (
    Addr,
    Body,
    Option<(String, Option<String>)>,
    Option<rk_emit::EmitReport>,
);

#[derive(Default)]
struct EmitStats {
    emitted: usize,
    trapping: usize,
    rejections: BTreeMap<String, usize>,
    details: BTreeMap<String, usize>,
    detail_addrs: BTreeMap<String, Vec<Addr>>,
    c_field_residency_violations: usize,
    c_field_residency_violation_addrs: Vec<Addr>,
}

struct IrSummary {
    diagnostics: Vec<Diagnostic>,
    lifted: usize,
    instructions: usize,
    call_returns: Vec<Addr>,
}

impl IrSummary {
    fn new(analysis: &rk_frontend::Analysis) -> Self {
        let diagnostics: Vec<Diagnostic> = analysis
            .ir
            .values()
            .filter_map(|r| r.as_ref().err())
            .filter_map(|e| match e {
                rk_core::Error::Diagnostic(d) => Some(d.clone()),
                _ => None,
            })
            .collect();
        let lifted = analysis.ir.values().filter(|r| r.is_ok()).count();
        let instructions: usize = analysis
            .ir
            .values()
            .filter_map(|r| r.as_ref().ok())
            .map(|ir| ir.insns.len())
            .sum();
        let call_returns: Vec<Addr> = analysis
            .ir
            .values()
            .filter_map(|r| r.as_ref().ok())
            .flat_map(|ir| ir.call_returns.iter().copied())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        Self {
            diagnostics,
            lifted,
            instructions,
            call_returns,
        }
    }
}

struct BodyContext<'a> {
    program: &'a Program,
    facts: rk_global::ContractCallFacts<'a>,
    checked: &'a BTreeSet<Addr>,
    x87_modes: rk_x87::ModeInput,
    x87_options: rk_x87::Options,
    fault_state: rk_core::settings::FaultState,
    known_entries: &'a BTreeSet<Addr>,
    private_targets: &'a BTreeSet<Addr>,
    private_masks: BTreeMap<Addr, BTreeSet<Fields>>,
    private_input_arguments: BTreeMap<Addr, String>,
    known_modes: BTreeSet<Addr>,
}

impl<'a> BodyContext<'a> {
    fn emit_context<'b>(
        &'b self,
        ir: &'b FunctionIr,
        private: Option<&'b rk_emit::PrivateEmit>,
    ) -> rk_emit::EmitContext<'b> {
        rk_emit::EmitContext {
            ir,
            checked_returns: self.checked,
            private_masks: &self.private_masks,
            private_input_arguments: &self.private_input_arguments,
            known_modes: &self.known_modes,
            known_entries: self.known_entries,
            private,
        }
    }
}

fn plan_one(ctx: &BodyContext<'_>, ir: &FunctionIr) -> rk_core::Result<rk_plan::PlannedFunction> {
    let cg = rk_ir::lower(ir, &ctx.program.image, &rk_x87::Recipes)?;
    rk_plan::plan_function(
        &cg,
        &rk_plan::PlanOptions {
            call_facts: &ctx.facts,
            policy: rk_core::ir::Policy::Optimizing,
            x87_modes: &ctx.x87_modes,
            x87_options: ctx.x87_options.clone(),
            fault_state: ctx.fault_state,
            private_targets: ctx.private_targets,
        },
    )
}

fn error_reason(error: &rk_core::Error) -> (String, Option<String>) {
    let reason = match error {
        rk_core::Error::Diagnostic(diagnostic) => {
            let value = serde_json::to_value(&diagnostic.reason).ok();
            value
                .as_ref()
                .and_then(|value| {
                    value
                        .get("reason")
                        .and_then(|r| r.as_str())
                        .map(str::to_string)
                })
                .unwrap_or_else(|| "unknown".to_string())
        }
        other => other.to_string(),
    };
    let detail = match error {
        rk_core::Error::Diagnostic(diagnostic) => serde_json::to_value(&diagnostic.reason)
            .ok()
            .and_then(|value| {
                value
                    .get("detail")
                    .and_then(|d| d.as_str())
                    .map(str::to_string)
            }),
        _ => None,
    };
    (reason, detail)
}

fn error_row(
    ctx: &BodyContext<'_>,
    addr: Addr,
    ir: &FunctionIr,
    error: &rk_core::Error,
) -> BodyRow {
    (
        addr,
        trap_body(ctx.program, addr, Some(ir)),
        Some(error_reason(error)),
        None,
    )
}

fn emit_row(
    ctx: &BodyContext<'_>,
    addr: Addr,
    ir: &FunctionIr,
    planned: &rk_plan::PlannedFunction,
) -> BodyRow {
    let emit_ctx = ctx.emit_context(ir, None);
    match rk_emit::emit_body(planned, &emit_ctx) {
        Ok((mut body, emit_report)) => {
            let covered: BTreeSet<Addr> =
                planned.ssa.entries.iter().map(|entry| entry.addr).collect();
            for entry in ctx
                .program
                .interior_by_function
                .get(&addr)
                .into_iter()
                .flatten()
            {
                if !covered.contains(entry) {
                    body.text.push_str(&format!(
                        "\nvoid fn_{entry:08x}(X86 *c) {{ recomp_unmodelled(c, 0x{entry:08x}u); }}"
                    ));
                }
            }
            (addr, body, None, Some(emit_report))
        }
        Err(error) => error_row(ctx, addr, ir, &error),
    }
}

enum Prepared {
    Emitted(BodyRow),
    Retained {
        addr: Addr,
        ir: FunctionIr,
        planned: rk_plan::PlannedFunction,
    },
}

fn account(stats: &mut EmitStats, (addr, body, reason, report): BodyRow, bodies: &mut Vec<Body>) {
    if let Some(report) = report {
        if report.c_field_residency_violations != 0 {
            stats.c_field_residency_violations += report.c_field_residency_violations;
            stats.c_field_residency_violation_addrs.push(addr);
        }
    }
    if let Some((reason, detail)) = reason {
        stats.trapping += 1;
        *stats.rejections.entry(reason).or_default() += 1;
        if let Some(detail) = detail {
            *stats.details.entry(detail.clone()).or_default() += 1;
            stats.detail_addrs.entry(detail).or_default().push(addr);
        }
    } else {
        stats.emitted += 1;
    }
    bodies.push(body);
}

fn build_bodies(
    program: &Program,
    analysis: &rk_frontend::Analysis,
    owned: Option<BTreeMap<Addr, std::result::Result<FunctionIr, rk_core::Error>>>,
    global: &rk_global::GlobalFacts,
    settings: &Settings,
    x87_mode: X87Mode,
    only: &[Addr],
) -> Result<(
    Vec<Body>,
    EmitStats,
    BTreeMap<Addr, BTreeSet<Fields>>,
    BTreeMap<Addr, String>,
    BTreeSet<Addr>,
)> {
    let known_entries: BTreeSet<Addr> = program
        .functions
        .keys()
        .chain(program.entries.keys())
        .copied()
        .collect();
    let private_targets: BTreeSet<Addr> = if std::env::var_os("RK_NO_PRIVATE").is_some() {
        BTreeSet::new()
    } else {
        global.private.targets.keys().copied().collect()
    };
    let make_ctx = |private_masks, private_input_arguments, known_modes| BodyContext {
        program,
        facts: global.call_facts(),
        checked: &global.private.checked_returns,
        x87_modes: rk_x87::ModeInput::from(&global.x87),
        x87_options: rk_x87::Options {
            strict: settings.fault_state == rk_core::settings::FaultState::Exact,
            convention: settings.msvc_x87_convention,
            cw_clone: settings.x87_cw_clone,
            force: x87_mode.into(),
        },
        fault_state: settings.fault_state,
        known_entries: &known_entries,
        private_targets: &private_targets,
        private_masks,
        private_input_arguments,
        known_modes,
    };
    let ctx = make_ctx(BTreeMap::new(), BTreeMap::new(), BTreeSet::new());
    let selected: BTreeSet<Addr> = only.iter().copied().collect();
    let wanted = |addr: Addr| selected.is_empty() || selected.contains(&addr);
    let sources: Vec<(Addr, Option<FunctionIr>)> = match owned {
        Some(irs) => irs
            .into_iter()
            .filter(|(addr, _)| wanted(*addr))
            .map(|(addr, result)| (addr, result.ok()))
            .collect(),
        None => program
            .functions
            .keys()
            .copied()
            .filter(|addr| wanted(*addr))
            .map(|addr| {
                let ir = analysis
                    .ir
                    .get(&addr)
                    .and_then(|result| result.as_ref().ok().cloned());
                (addr, ir)
            })
            .collect(),
    };
    let prepared: Vec<Prepared> = sources
        .into_par_iter()
        .map(|(addr, ir)| {
            let Some(ir) = ir else {
                return Prepared::Emitted((addr, trap_body(program, addr, None), None, None));
            };
            match plan_one(&ctx, &ir) {
                Ok(planned) => {
                    let retain =
                        !planned.requests.by_caller.is_empty() || private_targets.contains(&addr);
                    if retain {
                        Prepared::Retained { addr, ir, planned }
                    } else {
                        Prepared::Emitted(emit_row(&ctx, addr, &ir, &planned))
                    }
                }
                Err(error) => Prepared::Emitted(error_row(&ctx, addr, &ir, &error)),
            }
        })
        .collect();
    let mut requests = rk_plan::ContractRequests::default();
    let mut retained = Vec::new();
    for item in prepared {
        match item {
            Prepared::Emitted(row) => retained.push(Err(row)),
            Prepared::Retained { addr, ir, planned } => {
                for (caller, wanted) in &planned.requests.by_caller {
                    requests
                        .by_caller
                        .entry(*caller)
                        .or_default()
                        .extend(wanted.iter().copied());
                }
                retained.push(Ok((addr, ir, planned)));
            }
        }
    }
    let specialized = rk_global::specialize(
        &requests.by_caller,
        &global.contracts,
        &global.private.input_arguments,
    );
    let private_arguments: BTreeMap<Addr, String> = specialized
        .input_arguments
        .iter()
        .map(|(&addr, &field)| (addr, field.name().to_string()))
        .collect();
    let ctx2 = make_ctx(
        specialized.masks.clone(),
        private_arguments,
        global.private.known_modes.clone(),
    );
    let phase2: Vec<(Vec<BodyRow>, Vec<Body>)> = retained
        .into_par_iter()
        .map(|item| -> Result<(Vec<BodyRow>, Vec<Body>)> {
            match item {
                Err(row) => Ok((vec![row], Vec::new())),
                Ok((addr, ir, planned)) => {
                    let rows = vec![emit_row(&ctx2, addr, &ir, &planned)];
                    let mut private_bodies = Vec::new();
                    if let Some(layouts) = specialized.layouts.get(&addr) {
                        for layout in layouts {
                            let private = rk_emit::PrivateEmit {
                                mask: layout.outputs,
                                inputs: layout.inputs,
                                input_argument: layout.input_argument,
                            };
                            let emit_ctx = ctx2.emit_context(&ir, Some(&private));
                            match rk_emit::emit_body(&planned, &emit_ctx) {
                                Ok((body, _)) => private_bodies.push(body),
                                Err(error) => {
                                    bail!(
                                        "private body {addr:08x}/{:04x} emission failed: {error}",
                                        layout.outputs.0
                                    );
                                }
                            }
                        }
                    }
                    Ok((rows, private_bodies))
                }
            }
        })
        .collect::<Result<_>>()?;
    let mut stats = EmitStats::default();
    let mut bodies = Vec::new();
    let mut private_bodies = Vec::new();
    for (rows, private) in phase2 {
        for row in rows {
            account(&mut stats, row, &mut bodies);
        }
        private_bodies.extend(private);
    }
    bodies.sort_by_key(|body| body.addr);
    private_bodies.sort_by_key(|body| (body.addr, body.private_mask.unwrap_or_default()));
    bodies.extend(private_bodies);
    Ok((
        bodies,
        stats,
        specialized.masks,
        specialized
            .input_arguments
            .iter()
            .map(|(&addr, &field)| (addr, field.name().to_string()))
            .collect(),
        global.private.known_modes.clone(),
    ))
}

fn trap_body(program: &Program, addr: Addr, ir: Option<&FunctionIr>) -> Body {
    let mut text = format!("void fn_{addr:08x}(X86 *c) {{ recomp_unmodelled(c, 0x{addr:08x}u); }}");
    for entry in program
        .interior_by_function
        .get(&addr)
        .into_iter()
        .flatten()
    {
        text.push_str(&format!(
            "\nvoid fn_{entry:08x}(X86 *c) {{ recomp_unmodelled(c, 0x{entry:08x}u); }}"
        ));
    }
    let (insns, first, last) = ir
        .map(|ir| {
            let first = ir.insns.first().map_or(addr, |i| i.addr);
            let last = ir.insns.last().map_or(addr, |i| i.addr);
            (ir.insns.len(), first, last)
        })
        .unwrap_or((0, addr, addr));
    Body {
        addr,
        insns,
        first,
        last,
        text,
        requires: Requires::default(),
        declarations: Default::default(),
        private_mask: None,
    }
}

fn dump_program(program: &Program, only: &[Addr], out: &mut impl std::io::Write) -> Result<()> {
    for &addr in only {
        let owner = program
            .entry_owner(addr)
            .with_context(|| format!("{} is not inside a mapped function", Hex(addr)))?;
        let function = &program.functions[&owner];
        writeln!(out, "== program {}", Hex(owner))?;
        writeln!(
            out,
            "noreturn {}  provisional {}",
            function.noreturn, function.provisional
        )?;
        for span in &function.spans {
            let lengths = span
                .lengths
                .as_ref()
                .map(|l| format!("  explicit lengths {}", l.len()))
                .unwrap_or_default();
            writeln!(
                out,
                "span {}..{}{lengths}",
                Hex(span.start),
                Hex(span.end())
            )?;
        }
        for entry in program
            .interior_by_function
            .get(&owner)
            .into_iter()
            .flatten()
        {
            writeln!(
                out,
                "entry {} {}",
                Hex(*entry),
                program.entries[entry].name()
            )?;
        }
        for ((function, site), targets) in program.tables.range((owner, 0)..=(owner, Addr::MAX)) {
            debug_assert_eq!(*function, owner);
            let targets: Vec<String> = targets.iter().map(|t| Hex(*t).to_string()).collect();
            writeln!(out, "table {} -> {}", Hex(*site), targets.join(","))?;
        }
    }
    Ok(())
}

fn dump_fir(
    program: &Program,
    analysis: &rk_frontend::Analysis,
    only: &[Addr],
    out: &mut impl std::io::Write,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    for &addr in only {
        let owner = program
            .entry_owner(addr)
            .with_context(|| format!("{} is not inside a mapped function", Hex(addr)))?;
        let Some(Ok(ir)) = analysis.ir.get(&owner) else {
            writeln!(out, "== function {owner:08x}: no function IR")?;
            continue;
        };
        if seen.insert(ir.addr) {
            rk_frontend::dump(ir, out)?;
        }
    }
    Ok(())
}

fn dump_ssa(
    program: &Program,
    analysis: &rk_frontend::Analysis,
    only: &[Addr],
    prepared: bool,
    out: &mut impl std::io::Write,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    for &addr in only {
        let owner = program
            .entry_owner(addr)
            .with_context(|| format!("{} is not inside a mapped function", Hex(addr)))?;
        let Some(Ok(ir)) = analysis.ir.get(&owner) else {
            writeln!(out, "== ssa {owner:08x}: no function IR")?;
            continue;
        };
        if !seen.insert(owner) {
            continue;
        }
        let cg = rk_ir::lower(ir, &program.image, &rk_ir::NoX87)?;
        if prepared {
            match rk_ssa::prepare(&cg, &rk_ssa::PrepareOptions::default()) {
                Ok(ssa) => rk_ssa::dump(&ssa, out)?,
                Err(error) => writeln!(out, "== prepared {owner:08x}: {error}")?,
            }
            continue;
        }
        let options = rk_ssa::BuildOptions {
            policy: rk_core::ir::Policy::Conservative,
            call_facts: &rk_ssa::ConservativeCallFacts,
            allow_slow: true,
            value_budget: rk_ssa::DEFAULT_VALUE_BUDGET,
        };
        match rk_ssa::build(&cg, &options) {
            Ok(ssa) => rk_ssa::dump(&ssa, out)?,
            Err(diagnostic) => writeln!(out, "== ssa {owner:08x}: {diagnostic:?}")?,
        }
    }
    Ok(())
}

fn dump_global(
    program: &Program,
    global: &rk_global::GlobalFacts,
    only: &[Addr],
    out: &mut impl std::io::Write,
) -> Result<()> {
    let mut text = Vec::new();
    global.dump(&mut text)?;
    let text = String::from_utf8_lossy(&text);
    let owners: BTreeSet<String> = only
        .iter()
        .filter_map(|&a| program.entry_owner(a))
        .map(|a| format!("{a:08x}"))
        .collect();
    for line in text.lines() {
        if owners.is_empty() || owners.iter().any(|o| line.contains(o.as_str())) {
            writeln!(out, "{line}")?;
        }
    }
    Ok(())
}

fn dump_bodies(
    program: &Program,
    bodies: &[Body],
    only: &[Addr],
    out: &mut impl std::io::Write,
) -> Result<()> {
    let selected: BTreeSet<Addr> = only
        .iter()
        .map(|&addr| {
            program
                .entry_owner(addr)
                .with_context(|| format!("{} is not inside a mapped function", Hex(addr)))
        })
        .collect::<Result<_>>()?;
    for body in bodies.iter().filter(|body| selected.contains(&body.addr)) {
        writeln!(out, "== body {}", Hex(body.addr))?;
        writeln!(out, "{}", body.text)?;
    }
    Ok(())
}

struct ReportInputs<'a> {
    settings: &'a Settings,
    program: &'a Program,
    analysis: &'a rk_frontend::Analysis,
    summary: &'a IrSummary,
    ssa: &'a rk_ssa::SweepReport,
    global: &'a rk_global::GlobalReport,
    x87: Option<&'a rk_x87::X87Report>,
    emitted: &'a EmitStats,
    written: &'a rk_output::Written,
    phases: &'a Phases,
}

fn report(input: &ReportInputs<'_>) -> Result<String> {
    let ReportInputs {
        settings,
        program,
        analysis,
        summary,
        ssa,
        global,
        x87,
        emitted,
        written,
        phases,
    } = input;
    let diagnostics = &summary.diagnostics;
    let mut kinds: BTreeMap<&str, usize> = BTreeMap::new();
    for row in &written.symbols {
        *kinds.entry(row.kind).or_default() += 1;
    }
    let mut histogram: BTreeMap<String, usize> = BTreeMap::new();
    for diagnostic in diagnostics {
        let reason = serde_json::to_value(&diagnostic.reason)?;
        let name = reason["reason"].as_str().unwrap_or("unknown").to_string();
        *histogram.entry(name).or_default() += 1;
    }
    let lifted = summary.lifted;
    let instructions = summary.instructions;
    let call_returns = summary.call_returns.len();
    let json = serde_json::json!({
        "engine": "rust",
        "module": settings.module.as_ref().map(|m| m.key.as_str()),
        "functions": program.functions.len(),
        "entry_points": program.entries.len(),
        "entry_kinds": kinds,
        "body_chunks": written.chunks,
        "entry_chunks": written.entry_chunks,
        "lifted": lifted,
        "instructions": instructions,
        "call_returns": call_returns,
        "seh_helpers": analysis.seh_helpers.len(),
        "ssa": {
            "built": ssa.built,
            "failed": ssa.failed,
            "values": ssa.values,
            "verify_failed": ssa.verify_failed,
            "prepared": ssa.prepared,
            "prepared_failed": ssa.prepared_failed,
            "prepared_verify_failed": ssa.prepared_verify_failed,
            "prepared_values": ssa.prepared_values,
            "live_raw": ssa.live_raw,
            "live_prepared": ssa.live_prepared,
            "slow": ssa.slow,
            "lowering_diagnostics": ssa.diagnostics,
            "errors": ssa.errors,
        },
        "global": {
            "contracts": global.contracts,
            "contract_excluded": global.contract_excluded,
            "indirect_contracts": global.indirect_contracts,
            "sccs": global.sccs,
            "private_targets": global.private_targets,
            "private_variants": global.private_variants,
            "checked_returns": global.checked_returns,
            "selected_callers": global.selected_callers,
            "known_modes": global.known_modes,
            "instruction_checks": global.instruction_checks,
            "instruction_mismatches": global.instruction_mismatches.len(),
        },
        "x87": x87.map(|x87| serde_json::json!({
            "functions": x87.functions,
            "built": x87.built,
            "failed": x87.failed,
            "recipes": x87.recipes,
            "residual": x87.residual,
            "residual_total": x87.residual_total,
            "build_failed": x87.build_failed,
            "verify_failed": x87.verify_failed,
            "prepare_failed": x87.prepare_failed,
            "prepared_verify_failed": x87.prepared_verify_failed,
            "plans": x87.plans,
            "plan_skipped": x87.plan_skipped,
            "plan_failed": x87.plan_failed,
            "plan_panics": x87.plan_panics,
            "plan_errors": x87.plan_errors,
            "errors": x87.errors,
            "seconds": x87.seconds,
        })),
        "bodies": {
            "trapping": emitted.trapping,
            "emitted": emitted.emitted,
            "rejections": emitted.rejections,
            "details": emitted.details,
            "detail_addrs": emitted.detail_addrs,
            "c_field_residency_violations": emitted.c_field_residency_violations,
            "c_field_residency_violation_addrs": emitted.c_field_residency_violation_addrs,
        },
        "diagnostics": diagnostics,
        "diagnostics_histogram": histogram,
        "phases": phases.seconds,
        "seconds": phases.total(),
    });
    Ok(serde_json::to_string_pretty(&json)? + "\n")
}

fn main() -> Result<()> {
    let args = Args::parse();
    if let Some(jobs) = args.jobs {
        rayon::ThreadPoolBuilder::new()
            .num_threads(jobs.max(1))
            .build_global()?;
    }
    let mut phases = Phases::new(args.quiet);
    let settings = Settings::load(&args.input, args.module.as_deref())?;
    phases.mark("settings");
    let mut program = rk_image::load(&settings)?;
    phases.mark("load");
    let lengths = SleighLengths {
        cache: Default::default(),
    };
    let discovered = rk_discover::discover_with(&settings, &program, Some(&lengths))?;
    rk_discover::merge(&mut program, &discovered)?;
    phases.mark("discover");
    let mut analysis = rk_frontend::analyze(&program);
    phases.mark("lift");
    let summary = IrSummary::new(&analysis);
    let facts = facts::load(&settings, &program, &analysis)?;
    phases.mark("facts");
    let whole = args.out.is_some() || args.dump.is_empty();
    let wants_global = whole || args.wants(Stage::Global);
    let ssa = if whole && args.check {
        rk_ssa::sweep(
            &analysis.ir,
            &program.image,
            &rk_ssa::ConservativeCallFacts,
            &rk_ir::NoX87,
        )?
    } else {
        rk_ssa::SweepReport::default()
    };
    phases.mark("ssa");
    let global = if wants_global {
        rk_global::GlobalFacts::analyze(&rk_global::GlobalInput {
            settings: &settings,
            program: &program,
            analysis: &analysis,
            indirect: &facts.call_facts,
        })?
    } else {
        rk_global::GlobalFacts::default()
    };
    phases.mark("global");
    let x87 = if whole && args.check {
        Some(rk_x87::sweep(&analysis, &program.image))
    } else {
        None
    };
    phases.mark("x87");
    let only = if whole { &[][..] } else { &args.only[..] };
    let wants_bodies = whole || args.wants(Stage::Bodies);
    let owned = (wants_bodies && args.dump.is_empty()).then(|| std::mem::take(&mut analysis.ir));
    let (bodies, emit_stats, private_masks, private_input_arguments, known_modes) = if wants_bodies
    {
        build_bodies(
            &program,
            &analysis,
            owned,
            &global,
            &settings,
            if std::env::var_os("RK_X87_DYNAMIC").is_some() {
                X87Mode::Dynamic
            } else {
                args.x87_mode
            },
            only,
        )?
    } else {
        (
            Vec::new(),
            EmitStats::default(),
            BTreeMap::new(),
            BTreeMap::new(),
            BTreeSet::new(),
        )
    };
    phases.mark("emit");
    let call_returns = summary.call_returns.clone();
    let translation = Translation {
        settings: &settings,
        program: &program,
        bodies,
        preludes: rk_emit::preludes(),
        call_returns,
        regions: Vec::new(),
        call_facts: facts.call_facts,
        cw_effects: facts.cw_effects,
        checked_returns: global.private.checked_returns.clone(),
        private_masks,
        private_input_arguments,
        known_modes,
    };

    if !args.dump.is_empty() {
        if args.only.is_empty() && args.dump.iter().any(|s| !matches!(s, Stage::Global)) {
            bail!("--dump needs at least one --only address, except --dump global");
        }
        let mut out = std::io::stdout().lock();
        if args.wants(Stage::Program) {
            dump_program(&program, &args.only, &mut out)?;
        }
        if args.wants(Stage::Fir) {
            dump_fir(&program, &analysis, &args.only, &mut out)?;
        }
        if args.wants(Stage::Ssa) {
            dump_ssa(&program, &analysis, &args.only, false, &mut out)?;
        }
        if args.wants(Stage::Prepared) {
            dump_ssa(&program, &analysis, &args.only, true, &mut out)?;
        }
        if args.wants(Stage::Global) {
            dump_global(&program, &global, &args.only, &mut out)?;
        }
        if args.wants(Stage::Bodies) {
            dump_bodies(&program, &translation.bodies, &args.only, &mut out)?;
        }
        out.flush()?;
        if args.out.is_none() {
            return Ok(());
        }
    }

    let Some(out) = args.out.as_ref() else {
        bail!("--out is required unless dumping");
    };
    let written = rk_output::write(out, &translation)?;
    phases.mark("write");

    if !args.quiet {
        let kinds: Vec<String> = {
            let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
            for row in &written.symbols {
                *counts.entry(row.kind).or_default() += 1;
            }
            counts
                .into_iter()
                .map(|(k, n)| format!("{n} {k}"))
                .collect()
        };
        eprintln!(
            "  {} functions, {} entries ({}), {} body chunks, {} entry chunks; {} bodies emitted, {} trapped",
            program.functions.len(),
            program.entries.len(),
            kinds.join(", "),
            written.chunks,
            written.entry_chunks,
            emit_stats.emitted,
            emit_stats.trapping
        );
        if !emit_stats.rejections.is_empty() {
            let rows: Vec<String> = emit_stats
                .rejections
                .iter()
                .map(|(reason, count)| format!("{reason}={count}"))
                .collect();
            eprintln!("  body rejections: {}", rows.join(", "));
        }
    }
    if let Some(path) = &args.report {
        let mut global_report = global.report.clone();
        global_report.private_variants =
            translation.private_masks.values().map(BTreeSet::len).sum();
        std::fs::write(
            path,
            report(&ReportInputs {
                settings: &settings,
                program: &program,
                analysis: &analysis,
                summary: &summary,
                ssa: &ssa,
                global: &global_report,
                x87: x87.as_ref(),
                emitted: &emit_stats,
                written: &written,
                phases: &phases,
            })?,
        )
        .with_context(|| path.display().to_string())?;
    }
    Ok(())
}
