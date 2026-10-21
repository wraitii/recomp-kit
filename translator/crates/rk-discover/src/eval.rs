use std::collections::{BTreeMap, BTreeSet};

use rk_core::program::{Program, Span};
use rk_core::{Addr, Hex, Result};
use serde::Serialize;

use crate::{span_lengths, DecodeCheck, Discovered};

const LARGEST_RUNS: usize = 25;

pub fn keeps(addr: Addr, keep: f64) -> bool {
    (addr.wrapping_mul(0x9e37_79b1) as f64) < keep * 4_294_967_296.0
}

pub fn withhold(program: &Program, keep: f64) -> Result<Program> {
    let mut sample = program.clone();
    sample.functions.retain(|&addr, _| keeps(addr, keep));
    sample.spans = sample
        .functions
        .values()
        .flat_map(|f| f.spans.iter().map(move |s| (s.start, s.end(), f.addr)))
        .collect();
    sample.spans.sort_unstable();
    let kept = |addr: Addr| sample.functions.contains_key(&addr);
    sample.tables.retain(|&(function, _), _| kept(function));
    sample.interior.retain(|_, &mut (owner, _)| kept(owner));
    let contained: BTreeSet<Addr> = program
        .noreturn_calls
        .iter()
        .chain(&program.configured_entries)
        .copied()
        .filter(|&at| sample.containing(at).is_some())
        .collect();
    sample.noreturn_calls.retain(|at| contained.contains(at));
    sample.configured_entries.retain(|at| contained.contains(at));
    rk_image::index_entries(&mut sample)?;
    Ok(sample)
}

#[derive(Default, Serialize)]
pub struct FunctionStarts {
    pub total: usize,
    pub found_as_function: usize,
    pub found_as_entry: usize,
    pub instruction_start: usize,
    pub misaligned: usize,
    pub missed: usize,
}

#[derive(Default, Serialize)]
pub struct Instructions {
    pub total: usize,
    pub agree: usize,
    pub misaligned: usize,
    pub outside_reference: usize,
    pub outside_reference_bytes: u64,
    pub recall: f64,
    pub precision: f64,
}

#[derive(Default, Serialize)]
pub struct DiscoveredStarts {
    pub total: usize,
    pub reference_function: usize,
    pub reference_interior: usize,
    pub misaligned: usize,
    pub outside_reference: usize,
}

#[derive(Serialize)]
pub struct Run {
    pub start: String,
    pub bytes: u32,
    pub instructions: usize,
}

#[derive(Serialize)]
pub struct Outside {
    pub start: String,
    pub bytes: u32,
    pub evidence: Vec<String>,
}

#[derive(Serialize)]
pub struct Evaluation {
    pub keep: f64,
    pub reference_functions: usize,
    pub sample_functions: usize,
    pub withheld_instructions: usize,
    pub withheld_functions: FunctionStarts,
    pub kept: Instructions,
    pub recovered: Instructions,
    pub discovered_functions: DiscoveredStarts,
    pub largest_outside_runs: Vec<Run>,
    pub largest_missed_runs: Vec<Run>,
    pub outside_functions: Vec<Outside>,
    pub rejections: crate::report::RejectionCounts,
    pub conflicts: usize,
    pub traps: usize,
    pub discarded_seeds: usize,
}

struct Reference {
    starts: BTreeSet<Addr>,
    withheld: BTreeSet<Addr>,
    spans: Vec<(Addr, Addr, bool)>,
}

impl Reference {
    fn new(program: &Program, sample: &Program, check: Option<&dyn DecodeCheck>) -> Result<Self> {
        let mut starts = BTreeSet::new();
        let mut withheld = BTreeSet::new();
        let mut spans = Vec::new();
        for function in program.functions.values() {
            let hidden = !sample.functions.contains_key(&function.addr);
            for span in &function.spans {
                spans.push((span.start, span.end(), hidden));
                let mut at = span.start;
                for length in lengths(program, check, span)? {
                    starts.insert(at);
                    if hidden {
                        withheld.insert(at);
                    }
                    at += length as u32;
                }
            }
        }
        spans.sort_unstable();
        Ok(Self { starts, withheld, spans })
    }

    fn span(&self, at: Addr) -> Option<bool> {
        let index = self.spans.partition_point(|&(start, _, _)| start <= at).checked_sub(1)?;
        let &(_, end, hidden) = self.spans.get(index)?;
        (at < end).then_some(hidden)
    }

    fn instructions(&self, found: &BTreeMap<Addr, u8>) -> Instructions {
        let mut out = Instructions { total: found.len(), ..Default::default() };
        for (&at, &size) in found {
            if self.withheld.contains(&at) {
                out.agree += 1;
            } else if self.span(at).is_some() {
                out.misaligned += 1;
            } else {
                out.outside_reference += 1;
                out.outside_reference_bytes += size as u64;
            }
        }
        out.recall = ratio(out.agree, self.withheld.len());
        out.precision = ratio(out.agree, out.agree + out.misaligned);
        out
    }

    fn missed_runs(&self, found: &BTreeMap<Addr, u8>) -> Vec<Run> {
        let mut runs: Vec<Run> = Vec::new();
        let mut previous = None;
        for &at in &self.withheld {
            if found.contains_key(&at) {
                previous = None;
                continue;
            }
            let size = self.starts.range(at + 1..).next().map_or(0, |&next| next - at);
            match runs.last_mut().filter(|_| previous == self.starts.range(..at).next_back().copied()) {
                Some(run) => {
                    run.instructions += 1;
                    run.bytes += size;
                }
                None => runs.push(Run { start: Hex(at).to_string(), bytes: size, instructions: 1 }),
            }
            previous = Some(at);
        }
        runs.sort_by(|a, b| b.instructions.cmp(&a.instructions));
        runs.truncate(LARGEST_RUNS);
        runs
    }

    fn outside_runs(&self, found: &BTreeMap<Addr, u8>) -> Vec<Run> {
        let mut runs: Vec<Run> = Vec::new();
        let mut next = None;
        for (&at, &size) in found {
            if self.span(at).is_some() {
                next = None;
                continue;
            }
            if next == Some(at) {
                let run = runs.last_mut().unwrap();
                run.bytes += size as u32;
                run.instructions += 1;
            } else {
                runs.push(Run { start: Hex(at).to_string(), bytes: size as u32, instructions: 1 });
            }
            next = Some(at + size as u32);
        }
        runs.sort_by(|a, b| b.bytes.cmp(&a.bytes));
        runs.truncate(LARGEST_RUNS);
        runs
    }
}

fn lengths(program: &Program, check: Option<&dyn DecodeCheck>, span: &Span) -> Result<Vec<u8>> {
    match &span.lengths {
        Some(lengths) => Ok(lengths.clone()),
        None => span_lengths(&program.image, check, span.start, span.end()),
    }
}

fn ratio(part: usize, whole: usize) -> f64 {
    if whole == 0 {
        1.0
    } else {
        part as f64 / whole as f64
    }
}

pub fn evaluate(
    program: &Program,
    sample: &Program,
    keep: f64,
    discovered: &Discovered,
    check: Option<&dyn DecodeCheck>,
) -> Result<Evaluation> {
    let reference = Reference::new(program, sample, check)?;
    let mut kept_lengths = BTreeMap::new();
    for spans in discovered.functions.values() {
        for span in spans {
            let mut at = span.start;
            for &length in span.lengths.iter().flatten() {
                kept_lengths.insert(at, length);
                at += length as u32;
            }
        }
    }
    let mut withheld_functions = FunctionStarts::default();
    for &addr in program.functions.keys().filter(|addr| !sample.functions.contains_key(addr)) {
        withheld_functions.total += 1;
        if discovered.functions.contains_key(&addr) {
            withheld_functions.found_as_function += 1;
        } else if discovered.entries.contains_key(&addr) {
            withheld_functions.found_as_entry += 1;
        } else if kept_lengths.contains_key(&addr) {
            withheld_functions.instruction_start += 1;
        } else if kept_lengths.range(..addr).next_back().is_some_and(|(&at, &n)| at + n as u32 > addr) {
            withheld_functions.misaligned += 1;
        } else {
            withheld_functions.missed += 1;
        }
    }
    let mut discovered_functions = DiscoveredStarts { total: discovered.functions.len(), ..Default::default() };
    for &addr in discovered.functions.keys() {
        if program.functions.contains_key(&addr) {
            discovered_functions.reference_function += 1;
        } else if reference.starts.contains(&addr) {
            discovered_functions.reference_interior += 1;
        } else if reference.span(addr).is_some() {
            discovered_functions.misaligned += 1;
        } else {
            discovered_functions.outside_reference += 1;
        }
    }
    let outside_functions = discovered
        .functions
        .iter()
        .filter(|(&addr, _)| reference.span(addr).is_none())
        .map(|(&addr, spans)| Outside {
            start: Hex(addr).to_string(),
            bytes: spans.iter().map(|s| s.size).sum(),
            evidence: discovered.report.seeds.get(&Hex(addr).to_string()).cloned().unwrap_or_default(),
        })
        .collect();
    Ok(Evaluation {
        keep,
        reference_functions: program.functions.len(),
        sample_functions: sample.functions.len(),
        withheld_instructions: reference.withheld.len(),
        withheld_functions,
        kept: reference.instructions(&kept_lengths),
        recovered: reference.instructions(&discovered.instruction_lengths),
        discovered_functions,
        largest_outside_runs: reference.outside_runs(&kept_lengths),
        largest_missed_runs: reference.missed_runs(&discovered.instruction_lengths),
        outside_functions,
        rejections: discovered.report.rejection_counts.clone(),
        conflicts: discovered.report.conflicts.len(),
        traps: discovered.report.traps.len(),
        discarded_seeds: discovered.report.discarded_seeds.len(),
    })
}
