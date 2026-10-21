pub mod report;

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use iced_x86::{Decoder, DecoderOptions, FlowControl, Instruction, Mnemonic, OpKind, Register};
use rk_core::program::{Function, Image, InteriorKind, Program, Span};
use rk_core::settings::Settings;
use rayon::prelude::*;
use rk_core::{Addr, Error, Hex, Result};

use report::{
    Conflict, DiscardedSeed, DiscoveryReport, EntryReject, EntryRejection, FunctionReject,
    FunctionRejection, Gap, PointerRun, RejectionCounts,
};

const RANK_TRUSTED: u16 = 100;
const RANK_MAPPED_FLOW: u16 = 90;
const RANK_RELOCATED_FLOW: u16 = 70;
const RANK_RELOCATION: u16 = 60;
const RANK_RELOCATED_IMMEDIATE: u16 = 55;
const RANK_SWEEP: u16 = 30;
const RANK_POINTER: u16 = 20;
const BODY_BYTES: Addr = 4096;

type Functions = BTreeMap<Addr, Vec<Span>>;
type Entries = BTreeMap<Addr, (Addr, InteriorKind)>;

pub trait DecodeCheck: Sync {
    fn length(&self, image: &Image, at: Addr) -> Option<u8>;
}

#[derive(Clone, Copy, Debug)]
enum SimpleOp {
    None,
    Reg(Register),
    Mem(Mem),
    Imm(u32),
}

#[derive(Clone, Copy, Debug)]
struct Mem {
    base: Register,
    index: Register,
    scale: u32,
    displacement: u32,
    size: u32,
}

#[derive(Clone, Debug)]
struct Insn {
    addr: Addr,
    size: u8,
    flow: FlowControl,
    mnemonic: Mnemonic,
    ops: [SimpleOp; 4],
    op_count: u8,
    direct: Option<(Addr, bool)>,
    imm4: Option<(u8, u32)>,
    disp4: Option<(u8, u32)>,
    padding: bool,
}

impl Insn {
    fn is_call(&self) -> bool {
        matches!(self.flow, FlowControl::Call | FlowControl::IndirectCall)
    }

    fn is_jump(&self) -> bool {
        matches!(
            self.flow,
            FlowControl::ConditionalBranch
                | FlowControl::UnconditionalBranch
                | FlowControl::IndirectBranch
        )
    }

    fn is_ret(&self) -> bool {
        self.flow == FlowControl::Return
    }

    fn seeds_flow(&self) -> bool {
        self.is_call()
            || (self.is_jump()
                && !matches!(self.mnemonic, Mnemonic::Loop | Mnemonic::Loope | Mnemonic::Loopne))
    }

    fn ends_flow(&self) -> bool {
        self.is_ret()
            || matches!(self.mnemonic, Mnemonic::Jmp | Mnemonic::Int3 | Mnemonic::Ud0 | Mnemonic::Ud1 | Mnemonic::Ud2)
    }
}

#[derive(Clone, Debug)]
pub struct Discovered {
    pub functions: Functions,
    pub entries: Entries,
    pub instruction_lengths: BTreeMap<Addr, u8>,
    pub report: DiscoveryReport,
}

impl Discovered {
    pub fn is_empty(&self) -> bool {
        self.functions.is_empty()
    }

    pub fn instruction_count(&self) -> usize {
        self.report.instructions
    }
}

pub fn discover(settings: &Settings, program: &Program) -> Result<Discovered> {
    discover_with(settings, program, None)
}

pub fn discover_with(
    settings: &Settings,
    program: &Program,
    check: Option<&dyn DecodeCheck>,
) -> Result<Discovered> {
    if !settings.discover {
        return Ok(Discovered {
            functions: BTreeMap::new(),
            entries: BTreeMap::new(),
            instruction_lengths: BTreeMap::new(),
            report: DiscoveryReport::empty(),
        });
    }
    let details = rk_image::details::load(&settings.exe, program.image.base)?;
    let mut recovery = Recovery::new(program, details, check);
    recovery.initial_seeds();
    recovery.mapped_flow_seeds()?;
    recovery.pointer_tables();
    recovery.index_tables();
    recovery.sweep()?;
    recovery.index_tables();
    let (functions, entries) = recovery.code_map_rows()?;
    let (functions, entries) = recovery.filter(functions, entries)?;
    let report = recovery.report();
    let instruction_lengths = recovery.instructions.iter().map(|(&at, ins)| (at, ins.size)).collect();
    Ok(Discovered { functions, entries, instruction_lengths, report })
}

pub fn merge(program: &mut Program, discovered: &Discovered) -> Result<()> {
    for (&addr, spans) in &discovered.functions {
        program.functions.entry(addr).or_insert_with(|| Function {
            addr,
            spans: spans.clone(),
            noreturn: false,
            provisional: true,
        });
    }
    for (&addr, &(owner, kind)) in &discovered.entries {
        program.interior.entry(addr).or_insert((owner, kind));
    }
    program.spans = program
        .functions
        .values()
        .flat_map(|f| f.spans.iter().map(move |s| (s.start, s.end(), f.addr)))
        .collect();
    program.spans.sort_unstable();
    rk_image::index_entries(program)
}

struct Recovery<'a> {
    image: &'a Image,
    details: rk_image::details::PeDetails,
    check: Option<&'a dyn DecodeCheck>,
    delta: u32,
    instructions: BTreeMap<Addr, Insn>,
    covered: BTreeMap<Addr, Addr>,
    speculative: BTreeSet<Addr>,
    speculative_seeds: BTreeSet<Addr>,
    trusted_seeds: BTreeSet<Addr>,
    seed_rank: BTreeMap<Addr, u16>,
    instruction_rank: BTreeMap<Addr, u16>,
    discarded_seeds: Vec<(Addr, String, Vec<String>)>,
    ghidra_spans: Vec<(Addr, Addr, Addr)>,
    ghidra_lengths: BTreeMap<Addr, Vec<u8>>,
    ghidra_entries: BTreeMap<Addr, Addr>,
    seeds: BTreeMap<Addr, BTreeSet<String>>,
    pending: VecDeque<Addr>,
    roots: BTreeSet<Addr>,
    tables: Vec<(Addr, Addr)>,
    gaps: Vec<(Addr, Addr, &'static str)>,
    traps: BTreeMap<Addr, String>,
    data_ranges: Vec<(Addr, Addr, &'static str)>,
    known_mask: Vec<u8>,
    data_mask: Vec<u8>,
    data_owner: Vec<i32>,
    conflicts: BTreeSet<(Addr, Addr, String)>,
    reloc_slots: BTreeSet<Addr>,
    known_entries: BTreeSet<Addr>,
    excluded_bytes: u64,
    kept_functions: usize,
    kept_entries: usize,
    function_rejections: Vec<(Addr, FunctionReject)>,
    entry_rejections: Vec<(Addr, EntryReject)>,
}

impl<'a> Recovery<'a> {
    fn new(program: &'a Program, details: rk_image::details::PeDetails, check: Option<&'a dyn DecodeCheck>) -> Self {
        let image = &program.image;
        let mut ghidra_spans = Vec::new();
        let mut ghidra_lengths = BTreeMap::new();
        let mut known_mask = vec![0u8; image.size as usize];
        let mut data_ranges = details.data_ranges.clone();
        let mut excluded_bytes = 0u64;
        for function in program.functions.values() {
            for span in &function.spans {
                ghidra_spans.push((span.start, span.end(), function.addr));
                if let Some(lengths) = &span.lengths {
                    ghidra_lengths.insert(span.start, lengths.clone());
                }
                let lo = span.start.saturating_sub(image.base) as usize;
                let hi = span.end().saturating_sub(image.base) as usize;
                if lo < known_mask.len() {
                    let hi = hi.min(known_mask.len());
                    known_mask[lo..hi].fill(1);
                }
                data_ranges.push((span.start, span.end(), "Ghidra code map"));
                excluded_bytes += span.size as u64;
            }
        }
        ghidra_spans.sort_unstable();
        let reloc_slots = image.relocations.iter().map(|r| r.site).collect();
        let known_entries = program.entries.keys().copied().collect();
        Self {
            image,
            delta: details.delta,
            details,
            check,
            instructions: BTreeMap::new(),
            covered: BTreeMap::new(),
            speculative: BTreeSet::new(),
            speculative_seeds: BTreeSet::new(),
            trusted_seeds: BTreeSet::new(),
            seed_rank: BTreeMap::new(),
            instruction_rank: BTreeMap::new(),
            discarded_seeds: Vec::new(),
            ghidra_spans,
            ghidra_lengths,
            ghidra_entries: BTreeMap::new(),
            seeds: BTreeMap::new(),
            pending: VecDeque::new(),
            roots: BTreeSet::new(),
            tables: Vec::new(),
            gaps: Vec::new(),
            traps: BTreeMap::new(),
            data_ranges,
            known_mask,
            data_mask: Vec::new(),
            data_owner: Vec::new(),
            conflicts: BTreeSet::new(),
            reloc_slots,
            known_entries,
            excluded_bytes,
            kept_functions: 0,
            kept_entries: 0,
            function_rejections: Vec::new(),
            entry_rejections: Vec::new(),
        }
    }

    fn decode_at(&self, at: Addr) -> Option<Insn> {
        decode_at(self.image, self.check, at)
    }

    fn at_known_data(&self, at: Addr) -> bool {
        at.checked_sub(self.image.base)
            .and_then(|off| self.known_mask.get(off as usize))
            .is_some_and(|&b| b != 0)
    }

    fn ghidra_owner(&mut self, target: Addr) -> Option<Addr> {
        let index = self
            .ghidra_spans
            .partition_point(|&(start, _, _)| start <= target)
            .checked_sub(1)?;
        let &(start, end, owner) = self.ghidra_spans.get(index)?;
        if target >= end {
            return None;
        }
        let lengths = self.span_lengths(start, end).ok()?;
        let mut at = start;
        for length in lengths {
            if at == target {
                return Some(owner);
            }
            at += length as u32;
            if at > target {
                return None;
            }
        }
        None
    }

    fn span_lengths(&mut self, start: Addr, end: Addr) -> Result<Vec<u8>> {
        if let Some(lengths) = self.ghidra_lengths.get(&start) {
            return Ok(lengths.clone());
        }
        let lengths = span_lengths(self.image, self.check, start, end)?;
        self.ghidra_lengths.insert(start, lengths.clone());
        Ok(lengths)
    }

    fn seed(&mut self, target: Addr, evidence: &str, rank: u16, root: bool) {
        if !self.image.is_exec(target) {
            return;
        }
        if evidence.starts_with("direct:") && self.at_known_data(target) {
            if let Some(owner) = self.ghidra_owner(target) {
                if owner != target {
                    self.ghidra_entries.insert(target, owner);
                }
            }
            return;
        }
        if root {
            self.roots.insert(target);
        }
        let old_rank = self.seed_rank.get(&target).copied();
        let effective = old_rank.map_or(rank, |old| old.max(rank));
        let mut first = old_rank.is_none() || effective > old_rank.unwrap_or(0);
        self.seeds.entry(target).or_default().insert(evidence.to_string());
        self.seed_rank.insert(target, effective);
        if effective >= RANK_TRUSTED {
            self.speculative_seeds.remove(&target);
            self.trusted_seeds.insert(target);
        } else if !self.trusted_seeds.contains(&target) {
            self.speculative_seeds.insert(target);
        }
        let mut owner = self.covered.get(&target).copied();
        while let Some(current) = owner {
            if current == target || self.instruction_rank.get(&current).copied().unwrap_or(0) >= effective {
                break;
            }
            self.retract(current);
            owner = self.covered.get(&target).copied();
            first = true;
        }
        if owner == Some(target) {
            if self.instruction_rank.get(&target).copied().unwrap_or(0) < effective {
                self.retract(target);
                first = true;
            }
        } else if let Some(current) = owner {
            if effective >= RANK_TRUSTED {
                self.conflicts.insert((target, current, evidence.to_string()));
            } else if effective < self.instruction_rank.get(&current).copied().unwrap_or(0) {
                self.discard_seed(target, &format!("covered by {current:08x} rank {}", self.instruction_rank[&current]));
            }
            return;
        }
        if first {
            self.pending.push_back(target);
        }
    }

    fn discard_seed(&mut self, target: Addr, reason: &str) {
        let Some(evidence) = self.seeds.remove(&target) else {
            return;
        };
        self.seed_rank.remove(&target);
        self.speculative_seeds.remove(&target);
        self.trusted_seeds.remove(&target);
        self.roots.remove(&target);
        self.discarded_seeds.push((target, reason.to_string(), evidence.into_iter().collect()));
    }

    fn retract(&mut self, address: Addr) {
        self.speculative.remove(&address);
        self.instruction_rank.remove(&address);
        let Some(ins) = self.instructions.remove(&address) else {
            return;
        };
        for at in address..address + ins.size as u32 {
            if self.covered.get(&at) == Some(&address) {
                self.covered.remove(&at);
            }
        }
    }

    fn pointer_run_at(&self, at: Addr) -> bool {
        (0..2).all(|step| {
            let slot = at + step * 4;
            self.reloc_slots.contains(&slot)
                && self.image.rd32(slot).is_some_and(|target| self.image.is_exec(target))
        })
    }

    fn record(&mut self, ins: &Insn, rank: u16, flow: bool) -> bool {
        let speculative = rank < RANK_TRUSTED;
        if self.pointer_run_at(ins.addr) {
            return false;
        }
        let end = ins.addr + ins.size as u32;
        if (ins.addr..end).any(|at| self.at_known_data(at)) {
            return false;
        }
        let mut retract = BTreeSet::new();
        for at in ins.addr..end {
            if let Some(&owner) = self.covered.get(&at) {
                if owner != ins.addr {
                    let owner_rank = self.instruction_rank.get(&owner).copied().unwrap_or(0);
                    if owner_rank > rank
                        || (owner_rank == rank && !(flow && self.speculative.contains(&owner)))
                    {
                        if rank >= RANK_TRUSTED {
                            self.conflicts.insert((ins.addr, owner, "overlapping decode".into()));
                        }
                        return false;
                    }
                    retract.insert(owner);
                }
            }
            if at != ins.addr {
                if let Some(seed_rank) = self.seed_rank.get(&at).copied() {
                    if seed_rank >= rank {
                        if rank >= RANK_TRUSTED {
                            let evidence = self
                                .seeds
                                .get(&at)
                                .map(|v| v.iter().cloned().collect::<Vec<_>>().join("; "))
                                .unwrap_or_default();
                            self.conflicts.insert((at, ins.addr, evidence));
                        }
                        return false;
                    }
                    self.discard_seed(at, &format!("covered by {:08x} rank {rank}", ins.addr));
                }
            }
        }
        for owner in retract {
            let keep = self.instructions.contains_key(&owner)
                && (self.instruction_rank.get(&owner).copied().unwrap_or(0) < rank
                    || (flow && self.speculative.contains(&owner)));
            if keep {
                self.retract(owner);
            }
        }
        self.instructions.insert(ins.addr, ins.clone());
        for at in ins.addr..end {
            self.covered.insert(at, ins.addr);
        }
        self.instruction_rank.insert(ins.addr, rank);
        if speculative {
            self.speculative.insert(ins.addr);
        }
        if ins.seeds_flow() {
            if let Some((target, call)) = ins.direct {
                self.seed(target, &format!("direct:{:08x}", ins.addr), rank, call && rank >= RANK_MAPPED_FLOW);
            }
        }
        true
    }

    fn trace(&mut self) {
        while let Some(at) = self.pending.pop_front() {
            if !self.seeds.contains_key(&at) {
                continue;
            }
            let rank = self.seed_rank.get(&at).copied().unwrap_or(RANK_SWEEP);
            let speculative = rank < RANK_TRUSTED;
            let mut at = at;
            while self.image.is_exec(at) {
                if self.at_known_data(at) {
                    if let Some(owner) = self.ghidra_owner(at) {
                        if owner != at {
                            self.ghidra_entries.insert(at, owner);
                        }
                    }
                    break;
                }
                if let Some(&owner) = self.covered.get(&at) {
                    let owner_rank = self.instruction_rank.get(&owner).copied().unwrap_or(0);
                    if rank > owner_rank
                        || (rank == owner_rank && owner != at && self.speculative.contains(&owner))
                    {
                        self.retract(owner);
                        continue;
                    }
                    if owner != at && !speculative {
                        self.conflicts.insert((at, owner, "entry alignment".into()));
                    }
                    break;
                }
                let Some(ins) = self.decode_at(at) else {
                    break;
                };
                if !self.record(&ins, rank, true) {
                    break;
                }
                if ins.ends_flow() {
                    break;
                }
                at += ins.size as u32;
            }
        }
    }

    fn initial_seeds(&mut self) {
        self.seed(self.details.entry_point, "PE entry", RANK_TRUSTED, true);
        let exports = self.details.exports.clone();
        for addr in exports {
            self.seed(addr, "export", RANK_TRUSTED, true);
        }
        let relocations = self.image.relocations.clone();
        for r in &relocations {
            if !self.image.is_exec(r.site) && !self.at_known_data(r.target) {
                self.seed(r.target, &format!("relocation:{:08x}", r.site), RANK_RELOCATION, false);
            }
        }
        if let Some(mut at) = self.details.tls_callbacks {
            while let Some(target) = self.image.rd32(at) {
                if target == 0 {
                    break;
                }
                self.seed(target, &format!("TLS:{at:08x}"), RANK_TRUSTED, true);
                at = at.wrapping_add(4);
            }
        }
        self.trace();
        for r in &relocations {
            let Some(&owner) = self.covered.get(&r.site) else {
                continue;
            };
            let Some(ins) = self.instructions.get(&owner) else {
                continue;
            };
            let direct = ins.imm4.is_some_and(|(_, value)| value == r.target);
            if direct && !self.at_known_data(r.target) {
                let rank = if ins.seeds_flow() {
                    RANK_RELOCATED_FLOW
                } else {
                    RANK_RELOCATED_IMMEDIATE
                };
                self.seed(r.target, &format!("relocated-immediate:{owner:08x}"), rank, false);
            }
        }
        self.trace();
    }

    fn mapped_flow_seeds(&mut self) -> Result<()> {
        let spans = self.ghidra_spans.clone();
        let missing: Vec<(Addr, Addr)> = spans
            .iter()
            .filter(|(start, _, _)| !self.ghidra_lengths.contains_key(start))
            .map(|&(start, end, _)| (start, end))
            .collect();
        let image = self.image;
        let check = self.check;
        let computed: Vec<(Addr, Result<Vec<u8>>)> = missing
            .par_iter()
            .map(|&(start, end)| (start, span_lengths(image, check, start, end)))
            .collect();
        for (start, lengths) in computed {
            self.ghidra_lengths.insert(start, lengths?);
        }
        let mut instructions = Vec::new();
        for (start, end, _) in spans {
            let lengths = self.span_lengths(start, end)?;
            let mut at = start;
            for size in lengths {
                instructions.push((at, size));
                at += size as u32;
            }
        }
        let mut targets: Vec<(Addr, Option<(Addr, bool)>)> = instructions
            .par_iter()
            .map(|&(at, size)| (at, direct_flow_target(image, check, at, size)))
            .collect();
        targets.sort_by_key(|(at, _)| *at);
        for (at, target) in targets {
            if let Some((target, call)) = target {
                if !self.at_known_data(target) {
                    self.seed(target, &format!("mapped-flow:{at:08x}"), RANK_MAPPED_FLOW, call);
                }
            }
        }
        self.trace();
        Ok(())
    }

    fn pointer_tables(&mut self) {
        let base = self.image.base;
        let size = self.image.size as usize;
        let mut executable = vec![0u8; size];
        for &(low, high) in &self.image.exec_ranges {
            let lo = low.saturating_sub(base) as usize;
            let hi = high.saturating_sub(base) as usize;
            if lo < size {
                executable[lo..hi.min(size)].fill(1);
            }
        }
        let mut blocked = vec![0u8; size];
        for &at in self.covered.keys() {
            if let Some(off) = at.checked_sub(base) {
                if let Some(slot) = blocked.get_mut(off as usize) {
                    *slot = 1;
                }
            }
        }
        let sections = self.details.sections.clone();
        for section in &sections {
            if section.characteristics & 0x4000_0000 == 0 {
                continue;
            }
            let executable_section = section.characteristics & 0x2000_0000 != 0;
            let start = base + section.virtual_address;
            let end = start + section.raw_size;
            let lo = start.saturating_sub(base) as usize;
            let hi = (end.saturating_sub(base) as usize).min(size);
            if lo >= hi {
                continue;
            }
            let data = &self.image.data[lo..hi];
            let mut candidates = BTreeMap::new();
            for alignment in 0..4 {
                let stop = alignment + (data.len().saturating_sub(alignment)) / 4 * 4;
                let mut offset = alignment;
                while offset + 4 <= stop {
                    let target = u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap());
                    let Some(relative) = target.checked_sub(base) else {
                        offset += 4;
                        continue;
                    };
                    if relative as usize >= size || executable[relative as usize] == 0 {
                        offset += 4;
                        continue;
                    }
                    let image_offset = lo + offset;
                    let absolute = base + image_offset as u32;
                    if executable_section && !self.reloc_slots.contains(&absolute) {
                        offset += 4;
                        continue;
                    }
                    if blocked[image_offset..image_offset + 4].iter().any(|&b| b != 0) {
                        offset += 4;
                        continue;
                    }
                    let original = self
                        .details
                        .original
                        .get(image_offset..image_offset + 4)
                        .map(|b| u32::from_le_bytes(b.try_into().unwrap()).wrapping_add(self.delta));
                    if original == Some(target) {
                        candidates.insert(absolute, target);
                    }
                    offset += 4;
                }
            }
            let mut next_at = start;
            for &site in candidates.keys() {
                if site < next_at || site + 8 > end {
                    continue;
                }
                let mut at = site;
                let mut slots = Vec::new();
                while let Some(&target) = candidates.get(&at) {
                    slots.push((at, target));
                    at += 4;
                }
                if slots.len() >= 2 {
                    self.tables.push((slots[0].0, at));
                    for (slot, target) in slots {
                        if !self.at_known_data(target) && self.pointer_source(slot, target) {
                            self.seed(target, &format!("pointer-run:{slot:08x}"), RANK_POINTER, false);
                        }
                    }
                    next_at = at;
                } else {
                    next_at = slots[0].0 + 1;
                }
            }
        }
        self.trace();
    }

    fn pointer_source(&mut self, slot: Addr, target: Addr) -> bool {
        let index = self
            .ghidra_spans
            .partition_point(|&(start, _, _)| start <= slot + 3)
            .saturating_sub(1);
        let Some(&(start, end, _)) = self.ghidra_spans.get(index) else {
            return true;
        };
        if end <= slot {
            return true;
        }
        let lengths = match self.span_lengths(start, end) {
            Ok(lengths) => lengths,
            Err(_) => return false,
        };
        if slot < start || slot + 4 > end {
            return false;
        }
        let mut at = start;
        for length in lengths {
            let size = length as u32;
            if at + size > slot {
                let Some(ins) = self.decode_at(at) else {
                    return false;
                };
                if slot + 4 > at + size {
                    return false;
                }
                let field = slot - at;
                let immediate = ins.imm4 == Some((field as u8, target));
                let displacement = ins.disp4 == Some((field as u8, target));
                return immediate || displacement;
            }
            at += size;
        }
        false
    }

    fn index_tables(&mut self) {
        let ordered: Vec<Addr> = self.instructions.keys().copied().collect();
        let pointer_tables: BTreeMap<Addr, Addr> = self.tables.iter().copied().collect();
        for (position, &at) in ordered.iter().enumerate() {
            let ins = &self.instructions[&at];
            if ins.mnemonic != Mnemonic::Movzx || ins.op_count != 2 {
                continue;
            }
            let (SimpleOp::Reg(dest), SimpleOp::Mem(source)) = (ins.ops[0], ins.ops[1]) else {
                continue;
            };
            if source.size != 1 {
                continue;
            }
            let index = if source.base != Register::None {
                if source.index != Register::None {
                    continue;
                }
                source.base
            } else {
                source.index
            };
            if index == Register::None || source.scale != 1 {
                continue;
            }
            let lo = source.displacement;
            let next_at = at + ins.size as u32;
            let Some(jump) = self.instructions.get(&next_at) else {
                continue;
            };
            if !jump.is_jump() || jump.op_count == 0 {
                continue;
            }
            let SimpleOp::Mem(dispatch) = jump.ops[0] else {
                continue;
            };
            let table = dispatch.displacement;
            if dispatch.base != Register::None
                || dispatch.index != dest
                || dispatch.scale != 4
                || !pointer_tables.contains_key(&table)
            {
                continue;
            }
            let mut bound = None;
            let from = position.saturating_sub(5);
            for &previous in ordered[from..position].iter().rev() {
                let compare = &self.instructions[&previous];
                if compare.mnemonic == Mnemonic::Cmp && compare.op_count == 2 {
                    if let (SimpleOp::Reg(left), SimpleOp::Imm(right)) = (compare.ops[0], compare.ops[1]) {
                        if left == index {
                            bound = Some(right);
                        }
                    }
                    break;
                }
                if !matches!(compare.mnemonic, Mnemonic::Ja | Mnemonic::Push | Mnemonic::Nop) {
                    break;
                }
            }
            let Some(bound) = bound else {
                continue;
            };
            if bound >= 65536 || !self.image.is_exec(lo) {
                continue;
            }
            let hi = lo + bound + 1;
            let count = (pointer_tables[&table] - table) / 4;
            if !self.image.is_exec(hi - 1) {
                continue;
            }
            if (lo..hi).any(|p| self.covered.contains_key(&p)) {
                continue;
            }
            let Some(values) = self.image.bytes(lo, (hi - lo) as usize) else {
                continue;
            };
            if values.iter().any(|&value| value as u32 >= count) {
                continue;
            }
            self.data_ranges.push((lo, hi, "switch byte table"));
        }
    }

    fn build_data_mask(&mut self) {
        let size = self.image.size as usize;
        self.data_mask = vec![0u8; size];
        self.data_owner = vec![-1; size];
        for (index, &(lo, hi, _)) in self.data_ranges.iter().enumerate() {
            let start = lo.saturating_sub(self.image.base) as usize;
            let end = (hi.saturating_sub(self.image.base) as usize).min(size);
            for offset in start.min(size)..end {
                if self.data_mask[offset] == 0 {
                    self.data_mask[offset] = 1;
                    self.data_owner[offset] = index as i32;
                }
            }
        }
    }

    fn data_containing(&self, at: Addr) -> Option<(Addr, Addr, &'static str)> {
        let offset = at.checked_sub(self.image.base)? as usize;
        if self.data_mask.get(offset).copied().unwrap_or(0) == 0 {
            return None;
        }
        let index = self.data_owner[offset] as usize;
        Some(self.data_ranges[index])
    }

    fn first_data_start(&self, at: Addr, size: u8) -> Option<Addr> {
        let offset = at.checked_sub(self.image.base)? as usize;
        (1..size as usize).find_map(|step| {
            self.data_mask
                .get(offset + step)
                .is_some_and(|&b| b != 0)
                .then_some(at + step as u32)
        })
    }

    fn sweep(&mut self) -> Result<()> {
        let tables: BTreeMap<Addr, Addr> = self.tables.iter().copied().collect();
        let table_starts: Vec<Addr> = tables.keys().copied().collect();
        self.build_data_mask();
        let ranges = self.image.exec_ranges.clone();
        for (start, end) in ranges {
            let mut at = start;
            while at < end {
                if let Some(&owner) = self.covered.get(&at) {
                    let size = self.instructions.get(&owner).map_or(1, |ins| ins.size as u32);
                    at = owner + size;
                    continue;
                }
                if let Some((_, hi, kind)) = self.data_containing(at) {
                    self.gaps.push((at, hi.min(end), kind));
                    at = hi.min(end);
                    continue;
                }
                if let Some(&table_end) = tables.get(&at) {
                    self.gaps.push((at, table_end, "pointer-table"));
                    at = table_end;
                    continue;
                }
                let Some(ins) = self.decode_at(at) else {
                    self.gaps.push((at, at + 1, "undecodable"));
                    at += 1;
                    continue;
                };
                let size = ins.size as u32;
                let next_table = table_starts.partition_point(|&table| table <= at);
                let table_start = table_starts
                    .get(next_table)
                    .copied()
                    .filter(|&table| table < at + size);
                let data_start = table_start.or_else(|| self.first_data_start(at, ins.size));
                if let Some(data_start) = data_start {
                    self.gaps.push((at, data_start, "data-boundary"));
                    at = data_start;
                    continue;
                }
                let rank = RANK_SWEEP.max(self.seed_rank.get(&at).copied().unwrap_or(0));
                let mut blocked = false;
                for inner in at + 1..at + size {
                    if self
                        .covered
                        .get(&inner)
                        .is_some_and(|owner| self.instruction_rank.get(owner).copied().unwrap_or(0) >= rank)
                    {
                        blocked = true;
                        break;
                    }
                    if self.seed_rank.get(&inner).is_some_and(|&rank| rank >= RANK_SWEEP) {
                        blocked = true;
                        break;
                    }
                }
                if blocked {
                    self.gaps.push((at, at + 1, "sweep-padding"));
                    at += 1;
                    continue;
                }
                if !self.record(&ins, rank, false) {
                    at += 1;
                    continue;
                }
                self.trace();
                at += size;
            }
        }
        let instructions: Vec<(Addr, Insn)> = self.instructions.iter().map(|(&a, i)| (a, i.clone())).collect();
        for (at, ins) in instructions {
            if !self.speculative.contains(&at) && ins.seeds_flow() {
                if let Some((target, _)) = ins.direct {
                    if !self.instructions.contains_key(&target) {
                        self.traps.insert(at, format!("direct transfer to unmapped instruction {target:08x}"));
                    }
                }
            }
        }
        for (&target, evidence) in &self.seeds {
            if self.trusted_seeds.contains(&target)
                && !self.instructions.contains_key(&target)
                && !self.at_known_data(target)
            {
                let owner = self.covered.get(&target).copied().unwrap_or(target);
                let joined = evidence.iter().cloned().collect::<Vec<_>>().join("; ");
                self.conflicts.insert((target, owner, joined));
            }
        }
        Ok(())
    }

    fn code_map_rows(&self) -> Result<(Functions, Entries)> {
        let mut groups: BTreeMap<Addr, Vec<(Addr, u8)>> = BTreeMap::new();
        let mut window = None;
        let mut previous: Option<(Addr, Addr)> = None;
        let mut flows_into = None;
        for (&at, ins) in &self.instructions {
            if Some(at) != flows_into
                && !self.seeds.contains_key(&at)
                && ins.padding
            {
                continue;
            }
            flows_into = if ins.ends_flow() { None } else { Some(at + ins.size as u32) };
            let owner = if let Some((next, owner)) = previous {
                if next == at && self.roots.contains(&at) {
                    Some(owner)
                } else {
                    None
                }
            } else {
                None
            };
            let owner = owner.unwrap_or_else(|| {
                if window.is_none()
                    || previous.is_some_and(|(_, owner)| Some(owner) != window)
                    || self.roots.contains(&at)
                    || at >= window.unwrap_or(0) + BODY_BYTES
                {
                    window = Some(at);
                }
                window.unwrap()
            });
            groups.entry(owner).or_default().push((at, ins.size));
            previous = Some((at + ins.size as u32, owner));
        }
        let mut functions = BTreeMap::new();
        let mut entries = BTreeMap::new();
        for (owner, group) in groups {
            if !self.instructions.contains_key(&owner) {
                return Err(Error::input(format!("function {} has no instruction at its entry", Hex(owner))));
            }
            let mut runs: Vec<(Addr, Vec<u8>)> = Vec::new();
            for (at, size) in group {
                let extend = runs.last().is_some_and(|(start, lengths)| {
                    at == *start + lengths.iter().map(|&n| n as u32).sum::<u32>()
                });
                if !extend {
                    runs.push((at, Vec::new()));
                }
                runs.last_mut().unwrap().1.push(size);
                if at != owner {
                    entries.insert(at, (owner, InteriorKind::Branch));
                }
            }
            functions.insert(
                owner,
                runs.into_iter()
                    .map(|(start, lengths)| Span {
                        start,
                        size: lengths.iter().map(|&n| n as u32).sum(),
                        lengths: Some(lengths),
                    })
                    .collect(),
            );
        }
        for (&target, &owner) in &self.ghidra_entries {
            entries.entry(target).or_insert((owner, InteriorKind::Branch));
        }
        Ok((functions, entries))
    }

    fn mapped_span_at(&self, at: Addr) -> Option<(Addr, Addr, Addr)> {
        let index = self
            .ghidra_spans
            .partition_point(|&(start, _, _)| start <= at)
            .checked_sub(1)?;
        let &(start, end, owner) = self.ghidra_spans.get(index)?;
        (at < end).then_some((start, end, owner))
    }

    fn mapped_instructions(&mut self) -> Result<BTreeSet<Addr>> {
        let spans = self.ghidra_spans.clone();
        let mut starts = BTreeSet::new();
        for (start, end, _) in spans {
            let lengths = self.span_lengths(start, end)?;
            let mut at = start;
            for length in lengths {
                starts.insert(at);
                at += length as u32;
            }
        }
        Ok(starts)
    }

    fn filter(&mut self, functions: Functions, entries: Entries) -> Result<(Functions, Entries)> {
        let mapped_starts = self.mapped_instructions()?;
        let mut discovered_spans: Vec<(Addr, Addr, Addr)> = functions
            .iter()
            .flat_map(|(&owner, spans)| spans.iter().map(move |span| (span.start, span.end(), owner)))
            .collect();
        discovered_spans.sort_unstable();
        let discovered_at = |at: Addr| -> Option<Addr> {
            let index = discovered_spans.partition_point(|&(start, _, _)| start <= at).checked_sub(1)?;
            let &(_, end, owner) = discovered_spans.get(index)?;
            (at < end).then_some(owner)
        };
        let mut kept = BTreeMap::new();
        let mut kept_instructions = BTreeSet::new();
        for (&owner, spans) in &functions {
            let mut reason = None;
            if self.mapped_span_at(owner).is_some() {
                if !mapped_starts.contains(&owner) {
                    reason = Some(FunctionReject::StartsInsideMappedInstruction);
                } else if !self.known_entries.contains(&owner) {
                    reason = Some(FunctionReject::StartsInMappedFunction);
                }
            }
            if reason.is_none() {
                'spans: for span in spans {
                    let Some(lengths) = &span.lengths else {
                        return Err(Error::input(format!("{}: discovered span has no lengths", Hex(span.start))));
                    };
                    let mut at = span.start;
                    for &length in lengths {
                        for byte in at..at + length as u32 {
                            if self.mapped_span_at(byte).is_some() {
                                reason = Some(FunctionReject::OverlapsMappedCode);
                                break 'spans;
                            }
                        }
                        if let Some(ins) = self.instructions.get(&at) {
                            if ins.seeds_flow() {
                                if let Some((target, call)) = ins.direct {
                                    if self.mapped_span_at(target).is_some() {
                                        if !mapped_starts.contains(&target) {
                                            reason = Some(FunctionReject::FlowsIntoMappedInstruction);
                                            break 'spans;
                                        }
                                        if !self.known_entries.contains(&target) {
                                            reason = Some(FunctionReject::FlowsToMappedInterior);
                                            break 'spans;
                                        }
                                    } else if call {
                                        let entry = functions.contains_key(&target)
                                            || self.known_entries.contains(&target);
                                        if !entry {
                                            if discovered_at(target).is_some() {
                                                reason = Some(FunctionReject::CallsToDiscoveredInterior);
                                            } else {
                                                reason = Some(FunctionReject::CallsToUngroundedTarget);
                                            }
                                            break 'spans;
                                        }
                                    } else if let Some(other) = discovered_at(target) {
                                        if other != owner && target != other {
                                            reason = Some(FunctionReject::JumpsToDiscoveredInterior);
                                            break 'spans;
                                        }
                                    }
                                }
                            }
                        }
                        at += length as u32;
                    }
                }
            }
            if let Some(reason) = reason {
                self.function_rejections.push((owner, reason));
            } else {
                self.kept_functions += 1;
                for span in spans {
                    let mut at = span.start;
                    for &length in span.lengths.iter().flatten() {
                        kept_instructions.insert(at);
                        at += length as u32;
                    }
                }
                kept.insert(owner, spans.clone());
            }
        }
        let mut remaining = BTreeMap::new();
        for (addr, (owner, kind)) in entries {
            let reason = if self.mapped_span_at(addr).is_some() {
                if !mapped_starts.contains(&addr) {
                    Some(EntryReject::EntryInsideMappedInstruction)
                } else if !self.known_entries.contains(&addr) {
                    Some(EntryReject::EntryInMappedFunction)
                } else {
                    None
                }
            } else if !kept.contains_key(&owner) {
                Some(EntryReject::EntryInRejectedFunction)
            } else if !kept_instructions.contains(&addr) {
                Some(EntryReject::EntryNotInFunction)
            } else {
                None
            };
            match reason {
                Some(reason) => self.entry_rejections.push((addr, reason)),
                None => {
                    self.kept_entries += 1;
                    remaining.insert(addr, (owner, kind));
                }
            }
        }
        Ok((kept, remaining))
    }

    fn report(&self) -> DiscoveryReport {
        let pointer_runs = self
            .tables
            .iter()
            .map(|&(start, end)| PointerRun { start: Hex(start).to_string(), end: Hex(end).to_string() })
            .collect();
        let gaps = self
            .gaps
            .iter()
            .map(|&(start, end, kind)| Gap {
                start: Hex(start).to_string(),
                end: Hex(end).to_string(),
                kind,
            })
            .collect();
        let traps = self
            .traps
            .iter()
            .map(|(&at, reason)| (Hex(at).to_string(), reason.clone()))
            .collect();
        let conflicts = self
            .conflicts
            .iter()
            .map(|&(target, instruction, ref evidence)| Conflict {
                target: Hex(target).to_string(),
                instruction: Hex(instruction).to_string(),
                evidence: evidence.clone(),
                kind: if self.instructions.contains_key(&target) { "shared" } else { "rejected" },
            })
            .collect();
        let seeds = self
            .seeds
            .iter()
            .map(|(&at, evidence)| (Hex(at).to_string(), evidence.iter().cloned().collect()))
            .collect();
        let seed_ranks = self
            .seed_rank
            .iter()
            .map(|(&at, &rank)| (Hex(at).to_string(), rank))
            .collect();
        let discarded_seeds = self
            .discarded_seeds
            .iter()
            .map(|(at, reason, evidence)| DiscardedSeed {
                address: Hex(*at).to_string(),
                reason: reason.clone(),
                evidence: evidence.clone(),
            })
            .collect();
        let mut rejection_counts = RejectionCounts::default();
        for (_, reason) in &self.function_rejections {
            match reason {
                FunctionReject::StartsInsideMappedInstruction => rejection_counts.functions_starts_inside_mapped_instruction += 1,
                FunctionReject::StartsInMappedFunction => rejection_counts.functions_starts_in_mapped_function += 1,
                FunctionReject::OverlapsMappedCode => rejection_counts.functions_overlaps_mapped_code += 1,
                FunctionReject::FlowsIntoMappedInstruction => rejection_counts.functions_flows_into_mapped_instruction += 1,
                FunctionReject::FlowsToMappedInterior => rejection_counts.functions_flows_to_mapped_interior += 1,
                FunctionReject::CallsToDiscoveredInterior => rejection_counts.functions_calls_to_discovered_interior += 1,
                FunctionReject::CallsToUngroundedTarget => rejection_counts.functions_calls_to_ungrounded_target += 1,
                FunctionReject::JumpsToDiscoveredInterior => rejection_counts.functions_jumps_to_discovered_interior += 1,
            }
        }
        for (_, reason) in &self.entry_rejections {
            match reason {
                EntryReject::EntryInsideMappedInstruction => rejection_counts.entries_inside_mapped_instruction += 1,
                EntryReject::EntryInMappedFunction => rejection_counts.entries_in_mapped_function += 1,
                EntryReject::EntryInRejectedFunction => rejection_counts.entries_in_rejected_function += 1,
                EntryReject::EntryNotInFunction => rejection_counts.entries_not_in_function += 1,
            }
        }
        let function_rejections = self
            .function_rejections
            .iter()
            .map(|&(addr, reason)| FunctionRejection { address: Hex(addr).to_string(), reason })
            .collect();
        let entry_rejections = self
            .entry_rejections
            .iter()
            .map(|&(addr, reason)| EntryRejection { address: Hex(addr).to_string(), reason })
            .collect();
        DiscoveryReport {
            instructions: self.instructions.len(),
            entry_seeds: self.seeds.len(),
            excluded_bytes: self.excluded_bytes,
            discovered_functions: self.kept_functions + self.function_rejections.len(),
            kept_functions: self.kept_functions,
            rejected_functions: function_rejections,
            discovered_entries: self.kept_entries + self.entry_rejections.len(),
            kept_entries: self.kept_entries,
            rejected_entries: entry_rejections,
            rejection_counts,
            pointer_runs,
            gaps,
            traps,
            conflicts,
            seeds,
            seed_ranks,
            discarded_seeds,
        }
    }
}

fn span_lengths(
    image: &Image,
    check: Option<&dyn DecodeCheck>,
    start: Addr,
    end: Addr,
) -> Result<Vec<u8>> {
    let mut lengths = Vec::new();
    let mut at = start;
    while at < end {
        let ins = decode_at(image, check, at)
            .ok_or_else(|| Error::input(format!("{}: cannot recover mapped instruction boundaries", Hex(at))))?;
        if at + ins.size as u32 > end {
            return Err(Error::input(format!("{}: cannot recover mapped instruction boundaries", Hex(at))));
        }
        lengths.push(ins.size);
        at += ins.size as u32;
    }
    Ok(lengths)
}

fn decode_at(image: &Image, check: Option<&dyn DecodeCheck>, at: Addr) -> Option<Insn> {
    let end = image
        .exec_ranges
        .iter()
        .find(|&&(lo, hi)| lo <= at && at < hi)
        .map(|&(_, hi)| hi)?;
    let len = 15usize.min((end - at) as usize);
    let bytes = image.bytes(at, len)?;
    let found = if let Some(mut next) = (decode_one(bytes, at, None)?).mnemonic.eq(&Mnemonic::Wait)
        .then(|| decode_one(&bytes[1..], at + 1, Some(1)))
        .flatten()
        .filter(|next| mnemonic_name(next.mnemonic).starts_with("fn"))
    {
        next.addr = at;
        next
    } else {
        decode_one(bytes, at, None)?
    };
    if let Some(check) = check {
        if check.length(image, at)? != found.size {
            return None;
        }
    }
    Some(found)
}

fn direct_flow_target(
    image: &Image,
    check: Option<&dyn DecodeCheck>,
    at: Addr,
    size: u8,
) -> Option<(Addr, bool)> {
    let ins = decode_at(image, check, at)?;
    if !ins.seeds_flow() || ins.size != size {
        return None;
    }
    ins.direct
}

fn decode_one(bytes: &[u8], at: Addr, folded: Option<u8>) -> Option<Insn> {
    let mut decoder = Decoder::with_ip(32, bytes, at as u64, DecoderOptions::NONE);
    let mut insn = Instruction::default();
    decoder.decode_out(&mut insn);
    if insn.is_invalid() || insn.len() == 0 {
        return None;
    }
    let base = folded.unwrap_or(0);
    let prefix = if folded.is_some() { 1u8 } else { 0u8 };
    let size = insn.len() as u8 + prefix;
    let mut ops = [SimpleOp::None; 4];
    let op_count = insn.op_count().min(4) as u8;
    for (index, op) in ops.iter_mut().enumerate().take(op_count as usize) {
        *op = match insn.op_kind(index as u32) {
            OpKind::Register => SimpleOp::Reg(insn.op_register(index as u32)),
            OpKind::Memory => SimpleOp::Mem(Mem {
                base: insn.memory_base(),
                index: insn.memory_index(),
                scale: insn.memory_index_scale(),
                displacement: insn.memory_displacement64() as u32,
                size: insn.memory_size().size() as u32,
            }),
            OpKind::Immediate8
            | OpKind::Immediate8_2nd
            | OpKind::Immediate16
            | OpKind::Immediate32
            | OpKind::Immediate64
            | OpKind::Immediate8to16
            | OpKind::Immediate8to32
            | OpKind::Immediate8to64
            | OpKind::Immediate32to64 => SimpleOp::Imm(insn.immediate(index as u32) as u32),
            _ => SimpleOp::None,
        };
    }
    let constant = decoder.get_constant_offsets(&insn);
    let imm4 = (constant.immediate_size() == 4).then(|| {
        (
            constant.immediate_offset().wrapping_add(base as usize) as u8,
            insn.immediate(0) as u32,
        )
    });
    let disp4 = (constant.displacement_size() == 4).then(|| {
        (
            constant.displacement_offset().wrapping_add(base as usize) as u8,
            insn.memory_displacement64() as u32,
        )
    });
    let flow = insn.flow_control();
    let direct = if matches!(flow, FlowControl::Call | FlowControl::ConditionalBranch | FlowControl::UnconditionalBranch)
        && matches!(
            insn.op0_kind(),
            OpKind::NearBranch16 | OpKind::NearBranch32 | OpKind::NearBranch64
        )
    {
        Some((insn.near_branch_target() as u32, flow == FlowControl::Call))
    } else {
        None
    };
    Some(Insn {
        addr: at,
        size,
        flow,
        mnemonic: insn.mnemonic(),
        ops,
        op_count,
        direct,
        imm4,
        disp4,
        padding: bytes[..size as usize].iter().all(|&b| b == 0x90 || b == 0xcc),
    })
}

fn mnemonic_name(mnemonic: Mnemonic) -> String {
    format!("{mnemonic:?}").to_lowercase()
}
