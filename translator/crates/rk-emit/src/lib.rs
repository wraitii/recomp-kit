use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use rk_core::contract::{Field, Fields};
use rk_core::ir::{FunctionIr, Insn, OpCode, SehEffect};
use rk_core::{Addr, Diagnostic, Reason, Result};
use rk_ir::{field_lane, Op, Registers, SlowFamily, StateKey};
use rk_plan::PlannedFunction;
use rk_output::{Body, Declaration, Preludes, Requires};
use rk_ssa::{FlagDescriptor, ValueId, DISPATCH_BLOCK};

#[derive(Clone, Copy)]
pub struct PrivateEmit {
    pub mask: Fields,
    pub inputs: Fields,
    pub input_argument: Option<Field>,
}

pub struct EmitContext<'a> {
    pub ir: &'a FunctionIr,
    pub checked_returns: &'a BTreeSet<Addr>,
    pub private_masks: &'a BTreeMap<Addr, BTreeSet<Fields>>,
    pub private_input_arguments: &'a BTreeMap<Addr, String>,
    pub known_modes: &'a BTreeSet<Addr>,
    pub known_entries: &'a BTreeSet<Addr>,
    pub private: Option<&'a PrivateEmit>,
}

pub const RETURN_HELPERS: &str = "\
RECOMP_COLD void recomp_return_slow_(X86 *c) { recomp_return(c); }
static inline void recomp_return_fast_(X86 *c) {
    if (RECOMP_UNLIKELY(c->eip == GUEST_RETURN_SENTINEL) ||
        RECOMP_UNLIKELY(!recomp_is_call_return(c->eip)))
        recomp_return_slow_(c);
}
";

pub const MEMORY_HELPERS: &str = "\
RECOMP_HOT_INLINE uint8_t recomp_rd8_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rd8(a);
#else
    uint8_t v;
    RECOMP_BITCOPY(&v, arena + a, 1);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wr8_(uint8_t *arena, uint32_t a, uint8_t v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wr8(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 1);
#endif
}
RECOMP_HOT_INLINE uint16_t recomp_rd16_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rd16(a);
#else
    uint16_t v;
    RECOMP_BITCOPY(&v, arena + a, 2);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wr16_(uint8_t *arena, uint32_t a, uint16_t v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wr16(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 2);
#endif
}
RECOMP_HOT_INLINE uint32_t recomp_rd32_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rd32(a);
#else
    uint32_t v;
    RECOMP_BITCOPY(&v, arena + a, 4);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wr32_(uint8_t *arena, uint32_t a, uint32_t v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wr32(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 4);
#endif
}
RECOMP_HOT_INLINE uint64_t recomp_rd64_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rd64(a);
#else
    uint64_t v;
    RECOMP_BITCOPY(&v, arena + a, 8);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wr64_(uint8_t *arena, uint32_t a, uint64_t v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wr64(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 8);
#endif
}
RECOMP_HOT_INLINE float recomp_rdf32_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rdf32(a);
#else
    float v;
    RECOMP_BITCOPY(&v, arena + a, 4);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wrf32_(uint8_t *arena, uint32_t a, float v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wrf32(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 4);
#endif
}
RECOMP_HOT_INLINE double recomp_rdf64_(uint8_t *arena, uint32_t a) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    return rdf64(a);
#else
    double v;
    RECOMP_BITCOPY(&v, arena + a, 8);
    return v;
#endif
}
RECOMP_HOT_INLINE void recomp_wrf64_(uint8_t *arena, uint32_t a, double v) {
#if RECOMP_STORE_HOOKS || (defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS)
    wrf64(a, v);
#else
    RECOMP_BITCOPY(arena + a, &v, 8);
#endif
}
";

pub const FLOAT_HELPERS: &str = "\
RECOMP_HOT_INLINE void recomp_fcom_(uint16_t *status, double a, double b, int quiet) {
    if (RECOMP_UNLIKELY(isnan(a) || isnan(b))) {
        fcom_status(status, a, b, quiet);
    } else {
        *status = (uint16_t)((*status & (uint16_t)~0x4700u) |
                            ((uint16_t)(a < b) << 8) | ((uint16_t)(a == b) << 14));
    }
}
";

pub fn preludes() -> Preludes {
    Preludes {
        memory_helpers: MEMORY_HELPERS.to_string(),
        return_helpers: RETURN_HELPERS.to_string(),
        float_helpers: FLOAT_HELPERS.to_string(),
    }
}

pub struct EmitReport {
    pub c_field_residency_violations: usize,
}

pub fn emit_body(planned: &PlannedFunction, ctx: &EmitContext<'_>) -> Result<(Body, EmitReport)> {
    let mut emitter = Emitter::new(planned, ctx);
    emitter.run()?;
    let ir = ctx.ir;
    let report = EmitReport {
        c_field_residency_violations: emitter.residency_violations,
    };
    Ok((Body {
        addr: planned.ssa.addr,
        insns: ir.insns.len(),
        first: ir.insns.first().map_or(ir.addr, |insn| insn.addr),
        last: ir.insns.last().map_or(ir.addr, |insn| insn.addr),
        text: emitter.out,
        requires: emitter.requires,
        declarations: emitter.declarations,
        private_mask: ctx.private.map(|private| private.mask),
    }, report))
}

struct Emitter<'a> {
    plan: &'a PlannedFunction,
    s: &'a rk_ssa::Ssa,
    ctx: &'a EmitContext<'a>,
    out: String,
    requires: Requires,
    declarations: BTreeSet<Declaration>,
    multi: bool,
    checked: bool,
    private: Option<&'a PrivateEmit>,
    materialized: Vec<bool>,
    uses_arena: bool,
    value_block: Vec<u32>,
    successors: Vec<Vec<usize>>,
    key_location: rk_core::Lookup<StateKey, Option<Location>>,
    clone: usize,
    reload_defs: BTreeMap<ValueId, Vec<ValueId>>,
    available: std::cell::RefCell<Vec<Option<bool>>>,
    keep_lane: rk_core::Lookup<ValueId, (ValueId, u8)>,
    keep_group: BTreeMap<ValueId, KeepGroup>,
    inline_ok: Vec<bool>,
    inlined: Vec<bool>,
    in_block: Vec<bool>,
    residency_violations: usize,
}

#[derive(Clone, Copy)]
struct KeepGroup {
    field: usize,
    olds: [ValueId; 4],
}

impl<'a> Emitter<'a> {
    fn new(plan: &'a PlannedFunction, ctx: &'a EmitContext<'a>) -> Self {
        let s = &plan.ssa;
        let mut materialized = vec![false; s.values.len()];
        let mut value_block = vec![0u32; s.values.len()];
        let mut successors = vec![Vec::new(); s.blocks.len()];
        for (index, block) in s.blocks.iter().enumerate() {
            for value in block.phis.iter().chain(block.ops.iter()) {
                materialized[*value as usize] = true;
                value_block[*value as usize] = index as u32;
            }
            for pred in &block.preds {
                if *pred != DISPATCH_BLOCK {
                    successors[*pred as usize].push(index);
                }
            }
        }
        let key_location = s
            .keys
            .iter()
            .map(|key| (*key, location(*key)))
            .collect();
        let entry_targets: BTreeSet<usize> =
            s.entries.iter().map(|entry| entry.target as usize).collect();
        let mut indirect_targets: BTreeSet<usize> = BTreeSet::new();
        for (index, block) in s.blocks.iter().enumerate() {
            if block.ops.iter().any(|value| matches!(s.value(*value).op, Op::BranchInd)) {
                indirect_targets.extend(successors[index].iter().copied());
            }
        }
        let inline_ok = (0..s.blocks.len())
            .map(|index| {
                let block = &s.blocks[index];
                index > 0
                    && block.preds.len() == 1
                    && block.preds[0] != DISPATCH_BLOCK
                    && (block.preds[0] as usize) < index
                    && !entry_targets.contains(&index)
                    && !indirect_targets.contains(&index)
            })
            .collect();
        let uses_arena = s.values.iter().any(|value| {
            matches!(
                value.op,
                Op::P(OpCode::Load) | Op::P(OpCode::Store) | Op::ImportTail { .. }
            ) || matches!(value.op, Op::X87(op) if op.touches_memory())
        });
        let mut in_block = vec![false; s.values.len()];
        for block in &s.blocks {
            for value in &block.ops {
                in_block[*value as usize] = true;
            }
        }
        let mut slots: BTreeMap<(ValueId, usize), [Option<ValueId>; 4]> = BTreeMap::new();
        for (index, value) in s.values.iter().enumerate() {
            let id = index as ValueId;
            if !plan.live.contains(&id) {
                continue;
            }
            let Op::StateKeep(key) = value.op else { continue };
            let Some((field, lane)) = field_lane(key) else { continue };
            if field as usize >= 8 || lane >= 4 {
                continue;
            }
            let site = s.args(id)[0];
            slots.entry((site, field as usize)).or_insert([None; 4])[lane as usize] = Some(id);
        }
        let mut keep_lane: rk_core::Lookup<ValueId, (ValueId, u8)> = rk_core::Lookup::default();
        let mut keep_group: BTreeMap<ValueId, KeepGroup> = BTreeMap::new();
        for ((_site, field), lanes) in slots {
            let Some(ids) = lanes.iter().copied().collect::<Option<Vec<_>>>() else { continue };
            let carrier = ids[0];
            let olds = [s.args(ids[0])[1], s.args(ids[1])[1], s.args(ids[2])[1], s.args(ids[3])[1]];
            keep_group.insert(carrier, KeepGroup { field, olds });
            for (lane, id) in ids.into_iter().enumerate() {
                keep_lane.insert(id, (carrier, lane as u8));
            }
        }
        let mut reload_defs: BTreeMap<ValueId, Vec<ValueId>> = BTreeMap::new();
        for (index, value) in s.values.iter().enumerate() {
            let id = index as ValueId;
            if !plan.live.contains(&id) || value.size == 0 || materialized[index] {
                continue;
            }
            match value.op {
                Op::Input(key) => materialized[index] = private_loads(ctx.private, key),
                Op::StateKeep(_) => {
                    materialized[index] = true;
                    let carrier = keep_lane.get(&id).map_or(id, |(carrier, _)| *carrier);
                    if id == carrier {
                        reload_defs.entry(s.args(id)[0]).or_default().push(id);
                    }
                }
                Op::StateReload(_) => {
                    materialized[index] = true;
                    reload_defs.entry(s.args(id)[0]).or_default().push(id);
                }
                _ => {}
            }
        }
        let residency_violations = (0..s.values.len())
            .filter(|index| {
                let id = *index as ValueId;
                plan.live.contains(&id)
                    && !materialized[*index]
                    && reads_c_field(s, id, &materialized, ctx.private)
            })
            .count();
        Self {
            plan,
            s,
            ctx,
            out: String::new(),
            requires: Requires::default(),
            declarations: BTreeSet::new(),
            multi: s.entries.len() > 1,
            checked: ctx.checked_returns.contains(&s.addr) || ctx.private.is_some(),
            private: ctx.private,
            materialized,
            uses_arena,
            value_block,
            successors,
            key_location,
            clone: 0,
            reload_defs,
            available: std::cell::RefCell::new(vec![None; s.values.len()]),
            keep_lane,
            keep_group,
            inline_ok,
            inlined: vec![false; s.blocks.len()],
            in_block,
            residency_violations,
        }
    }

    fn run(&mut self) -> Result<()> {
        let addr = self.s.addr;
        let body_name = if let Some(private) = self.private {
            format!("fn_contract_{addr:08x}_{:04x}", private.mask.0)
        } else if self.checked {
            format!("body_checked_{addr:08x}")
        } else if self.multi {
            format!("body_{addr:08x}")
        } else {
            format!("fn_{addr:08x}")
        };
        let signature = self.signature(&body_name);
        self.out.push_str(&signature);
        self.out.push_str(" {\n");
        self.write_declarations();
        self.write_x87_declarations();
        if self.uses_arena {
            self.out.push_str("uint8_t *arena_ = g_mem;\n");
            self.requires.arena = true;
        }
        if self.plan.entry_settle {
            self.out.push_str("x86_cc_settle(c);\n");
        }
        self.write_inputs()?;
        self.entry_dispatch()?;
        if let Some(site) = self.reload_defs.keys().find(|site| !self.in_block[**site as usize]) {
            return Err(self.error(self.s.value_addr[*site as usize], "state reload site is not a block op"));
        }
        let clones = self.plan.x87.as_ref().map_or(1, rk_x87::Plan::clones);
        for clone in 0..clones {
            self.clone = clone;
            self.blocks()?;
        }
        self.out.push_str("}\n");
        self.wrappers(&body_name);
        self.requires.return_fast = true;
        Ok(())
    }

    fn signature(&self, name: &str) -> String {
        let mut params = vec!["X86 *c".to_string()];
        if let Some(private) = self.private {
            if let Some(argument) = private.input_argument {
                params.push(format!("uint32_t {}_", argument.name().to_lowercase()));
            }
            params.push("uint32_t expected_return_".to_string());
        } else {
            if self.multi {
                params.push("uint32_t entry_".to_string());
            }
            if self.checked {
                params.push("uint32_t expected_return_".to_string());
                params.push("int checked_return_".to_string());
            }
        }
        format!("void {name}({})", params.join(", "))
    }

    fn call_args(&self, entry: Option<Addr>, expected: Option<&str>) -> String {
        let mut args = vec!["c".to_string()];
        if self.multi {
            args.push(entry.map_or("0u".to_string(), |addr| format!("0x{addr:08x}u")));
        }
        if self.checked {
            args.push(expected.unwrap_or("0u").to_string());
            args.push(if expected.is_some() { "1" } else { "0" }.to_string());
        }
        args.join(", ")
    }

    fn wrappers(&mut self, body: &str) {
        let addr = self.s.addr;
        if self.private.is_some() {
            return;
        }
        if body != format!("fn_{addr:08x}") {
            let args = self.call_args(None, None);
            let _ = writeln!(self.out, "void fn_{addr:08x}(X86 *c) {{ {body}({args}); }}");
        }
        if self.checked {
            let args = self.call_args(None, Some("expected"));
            let _ = writeln!(
                self.out,
                "void fn_checked_{addr:08x}(X86 *c, uint32_t expected) {{ {body}({args}); }}"
            );
        }
        if self.multi {
            for entry in &self.s.entries {
                if entry.addr == addr {
                    continue;
                }
                let args = self.call_args(Some(entry.addr), None);
                let _ = writeln!(
                    self.out,
                    "void fn_{:08x}(X86 *c) {{ {body}({args}); }}",
                    entry.addr
                );
            }
        }
    }

    fn write_declarations(&mut self) {
        let mut narrow = Vec::new();
        let mut wide = Vec::new();
        for (index, value) in self.s.values.iter().enumerate() {
            let id = index as ValueId;
            if !self.plan.live.contains(&id) || value.size == 0 || !self.materialized[index] {
                continue;
            }
            if self.keep_lane.get(&id).is_some_and(|(carrier, _)| *carrier != id) {
                continue;
            }
            if value.size > 4 {
                wide.push(id);
            } else {
                narrow.push(id);
            }
        }
        for (ctype, ids) in [("uint32_t", narrow), ("uint64_t", wide)] {
            if ids.is_empty() {
                continue;
            }
            let mut names = ids.iter().map(|id| format!("v{id}"));
            let first = names.next().unwrap();
            let _ = write!(self.out, "{ctype} {first}");
            for name in names {
                let _ = write!(self.out, ", {name}");
            }
            self.out.push_str(";\n");
        }
    }

    fn write_x87(&mut self, lines: Vec<String>) {
        for line in lines {
            if identity_assignment(&line) {
                continue;
            }
            if line.contains("recomp_fcom_(") {
                self.requires.fcom = true;
            }
            if x87_reads_flags(&line) {
                self.out.push_str("x86_cc_settle(c);\n");
            }
            let _ = writeln!(self.out, "{line}");
        }
    }

    fn write_x87_declarations(&mut self) {
        let Some(x87) = self.plan.x87.as_ref() else { return };
        for line in x87.declaration_lines(&ValueNames(self)) {
            let _ = writeln!(self.out, "{line}");
        }
    }

    fn write_inputs(&mut self) -> Result<()> {
        for index in 0..self.s.values.len() {
            let id = index as ValueId;
            let Op::Input(key) = self.s.value(id).op else { continue };
            if !self.plan.live.contains(&id) || !self.materialized[index] {
                continue;
            }
            if let Some(private) = self.private {
                if let Some((field, _)) = field_lane(key) {
                    if !private.inputs.contains(field) && private.input_argument != Some(field) {
                        continue;
                    }
                }
            }
            let expr = self.field_expr(key, self.s.value(id).size)?;
            let _ = writeln!(self.out, "{}", self.assignment(id, &expr));
        }
        Ok(())
    }

    fn materialize_defs(&mut self, site: ValueId) -> Result<()> {
        let Some(values) = self.reload_defs.get(&site).cloned() else { return Ok(()) };
        for id in values {
            if let Some(group) = self.keep_group.get(&id).copied() {
                let old = self.pack_bytes(&group.olds, false)?;
                let loaded = format!("c->r[{}]", group.field);
                let expr = keep_expr(&loaded, &old);
                let _ = writeln!(self.out, "v{id} = ({expr});");
                continue;
            }
            let expr = self.operation(id)?;
            let _ = writeln!(self.out, "{}", self.assignment(id, &expr));
        }
        Ok(())
    }

    fn block_prefix(&self) -> char {
        match self.plan.x87.as_ref().and_then(|plan| plan.programs.get(self.clone)).and_then(|program| program.clone) {
            Some("fast") => 'F',
            _ => 'B',
        }
    }

    fn x87_lines(&self, lines: impl FnOnce(&rk_x87::Plan) -> Vec<String>) -> Vec<String> {
        let Some(x87) = self.plan.x87.as_ref() else { return Vec::new() };
        lines(x87)
    }

    fn entry_dispatch(&mut self) -> Result<()> {
        let primary = self
            .s
            .entries
            .iter()
            .find(|entry| entry.addr == self.s.addr)
            .or_else(|| self.s.entries.first())
            .ok_or_else(|| self.error(self.s.addr, "SSA has no entry"))?;
        if self.multi {
            self.out.push_str("switch (entry_) {\n");
            for entry in &self.s.entries {
                if entry.addr == primary.addr {
                    continue;
                }
                let _ = writeln!(self.out, "case 0x{:08x}u:", entry.addr);
                self.edge(None, entry.target as usize)?;
            }
            self.out.push_str("default:;\n");
            self.edge(None, primary.target as usize)?;
            self.out.push_str("}\n");
        } else if primary.target == 0 {
            self.edge_copies(DISPATCH_BLOCK, primary.target as usize)?;
        } else {
            self.edge(None, primary.target as usize)?;
        }
        Ok(())
    }

    fn blocks(&mut self) -> Result<()> {
        self.inlined.fill(false);
        for block in 0..self.s.blocks.len() {
            if self.inlined[block] {
                continue;
            }
            self.block(block, true)?;
        }
        Ok(())
    }

    fn can_inline(&self, source: usize, target: usize) -> bool {
        !self.inlined[target] && target > source && self.inline_ok[target]
    }

    fn inline_block(&mut self, source: usize, target: usize) -> Result<()> {
        self.edge_copies(source as u32, target)?;
        self.inlined[target] = true;
        self.block(target, false)
    }

    fn block(&mut self, block: usize, label: bool) -> Result<()> {
        if label {
            let _ = writeln!(self.out, "{}{block}:;", self.block_prefix());
        }
        {
            let item = &self.s.blocks[block];
            if let Some(insn) = self.ctx.ir.insns.get(item.start as usize) {
                let _ = writeln!(
                    self.out,
                    "// {:08x}: {}",
                    insn.addr, insn.text
                );
            }
            let block_lines = self.x87_lines(|x87| x87.block_lines(self.clone, block as u32, &ValueNames(self)));
            self.write_x87(block_lines);
            let ops = item.ops.clone();
            let mut continues = true;
            for (position, value) in ops.iter().copied().enumerate() {
                let live = self.plan.live.contains(&value);
                if live {
                    if let (
                        Op::Branch { target: branch },
                        Some(Op::Tail { target: tail }),
                    ) = (
                        self.s.value(value).op,
                        ops.get(position + 1).map(|next| self.s.value(*next).op),
                    ) {
                        if branch == tail {
                            let lines = self.x87_lines(|x87| x87.event_lines(self.clone, value, &ValueNames(self)));
                            self.write_x87(lines);
                            self.publish(value)?;
                            self.materialize_defs(value)?;
                            continue;
                        }
                    }
                    continues = self.value(value)?;
                }
                self.materialize_defs(value)?;
            }
            let ended = ops.last().is_some_and(|value| {
                match self.s.value(*value).op {
                    Op::Slow(_) => !continues,
                    op => ends_block(op),
                }
            });
            let external_branch = ops
                .last()
                .is_some_and(|value| matches!(self.s.value(*value).op, Op::CBranch { .. }))
                && !self
                    .successors(block)
                    .iter()
                    .copied()
                    .any(|next| self.s.blocks[next].start == self.s.blocks[block].end);
            if !ended || external_branch {
                let end = self.s.blocks[block].end;
                let next = self.successors(block)
                    .iter()
                    .copied()
                    .find(|next| self.s.blocks[*next].start == end);
                let end_addr = self.block_end_addr(block);
                let end_lines = self.x87_lines(|x87| x87.end_lines(self.clone, block as u32, &ValueNames(self)));
                self.write_x87(end_lines);
                match next {
                    Some(next) if self.can_inline(block, next) => self.inline_block(block, next)?,
                    Some(next) => self.edge(Some(block), next)?,
                    None if self.s.blocks[block].preds.is_empty() => {}
                    None if end_addr.is_some_and(|addr| self.ctx.known_entries.contains(&addr)) => {
                        let addr = end_addr.unwrap();
                        self.declare_entry(addr);
                        let _ = writeln!(self.out, "CALL_FN({addr:08x}); return;");
                    }
                    None if end_addr.is_some() => {
                        let addr = end_addr.unwrap();
                        let _ = writeln!(
                            self.out,
                            "c->eip = 0x{addr:08x}u; recomp_jump(c, 0x{addr:08x}u); return;"
                        );
                    }
                    None => {
                        return Err(self.error(self.s.addr, "block falls through to no successor"))
                    }
                }
            }
        }
        Ok(())
    }

    fn value(&mut self, value: ValueId) -> Result<bool> {
        let item = *self.s.value(value);
        let addr = self.s.value_addr[value as usize];
        if !matches!(item.op, Op::X87(_)) {
            let lines = self.x87_lines(|x87| x87.event_lines(self.clone, value, &ValueNames(self)));
            self.write_x87(lines);
        }
        match item.op {
            Op::Memory => Ok(true),
            Op::X87(op) => {
                if op.touches_memory() {
                    self.publish(value)?;
                }
                let lines = self.x87_lines(|x87| x87.event_lines(self.clone, value, &ValueNames(self)));
                self.write_x87(lines);
                Ok(true)
            }
            Op::Slow(index) => {
                let slow = self.s.slow(index).copied().ok_or_else(|| self.error(addr, "missing slow record"))?;
                self.publish(value)?;
                if matches!(slow.family, SlowFamily::X87) {
                    return Err(Diagnostic::error(addr, Reason::UnsupportedX87 {
                        detail: "unsupported x87 instruction or operand".into(),
                    }));
                }
                self.slow(slow.family, addr)
            }
            Op::CallOther(userop) => {
                self.publish(value)?;
                self.unsupported(addr, &format!("callother userop {userop}"))
            }
            Op::Call { target } => self.call(value, target).map(|_| true),
            Op::CallInd => self.call_ind(value, addr).map(|_| true),
            Op::Tail { target } => {
                self.publish(value)?;
                if self.ctx.known_entries.contains(&target) {
                    self.declare_entry(target);
                    let _ = writeln!(self.out, "CALL_FN({target:08x}); return;");
                } else {
                    let _ = writeln!(self.out, "c->eip = 0x{addr:08x}u; recomp_jump(c, 0x{target:08x}u); return;");
                }
                Ok(false)
            }
            Op::ImportTail { slot } => {
                self.publish(value)?;
                let _ = writeln!(
                    self.out,
                    "c->eip = 0x{addr:08x}u; recomp_jump(c, recomp_rd32_(arena_, 0x{slot:08x}u)); return;"
                );
                Ok(false)
            }
            Op::Branch { target } => self.branch(value, target).map(|_| true),
            Op::CBranch { target } => self.cbranch(value, target).map(|_| true),
            Op::BranchInd => self.branch_ind(value, addr).map(|_| true),
            Op::Return => self.return_value(value).map(|_| false),
            Op::Trap(target) => {
                let _ = writeln!(self.out, "recomp_unmodelled(c, 0x{target:08x}u); return;");
                Ok(false)
            }
            Op::Breakpoint => {
                self.publish(value)?;
                let _ = writeln!(self.out, "recomp_breakpoint(c, 0x{addr:08x}u); return;");
                Ok(false)
            }
            Op::Seh(kind) => {
                self.publish(value)?;
                self.seh(kind, addr);
                Ok(true)
            }
            Op::Div32 { .. } | Op::Idiv32 { .. } => self.divide(value, addr).map(|_| true),
            Op::String(name) => {
                self.publish(value)?;
                let _ = writeln!(self.out, "{} (c);", name.0);
                self.out.push_str("x86_cc_settle(c);\n");
                Ok(true)
            }
            Op::P(OpCode::Load) => self.load(value).map(|_| true),
            Op::P(OpCode::Store) => self.store(value).map(|_| true),
            _ => {
                let expr = self.operation(value)?;
                let _ = writeln!(self.out, "{}", self.assignment(value, &expr));
                Ok(true)
            }
        }
    }

    fn insn_at(&self, addr: Addr) -> Option<&Insn> {
        self.ctx
            .ir
            .insns
            .binary_search_by_key(&addr, |insn| insn.addr)
            .ok()
            .map(|index| &self.ctx.ir.insns[index])
    }

    fn slow_trap(&mut self, addr: Addr, reason: &str) {
        let reason = reason.replace(['"', '\\'], "_");
        let _ = writeln!(
            self.out,
            "recomp_unmodelled_named(c, 0x{addr:08x}u, \"{reason}\"); return;"
        );
    }

    fn unsupported(&mut self, addr: Addr, reason: &str) -> Result<bool> {
        self.slow_trap(addr, reason);
        Ok(false)
    }

    fn slow(&mut self, family: SlowFamily, addr: Addr) -> Result<bool> {
        let insn = self
            .insn_at(addr)
            .cloned()
            .ok_or_else(|| self.error(addr, "slow instruction missing from IR"))?;
        let mnemonic = insn.mnemonic.to_ascii_uppercase();
        match family {
            SlowFamily::Rdtsc => {
                self.out.push_str("recomp_rdtsc(c);\n");
                Ok(true)
            }
            SlowFamily::Cpuid => {
                self.out.push_str("recomp_cpuid(c);\n");
                Ok(true)
            }
            SlowFamily::Flags => match mnemonic.as_str() {
                "PUSHFD" => {
                    self.out.push_str("recomp_pushfd(c);\n");
                    Ok(true)
                }
                "POPFD" => {
                    self.out.push_str("recomp_popfd(c);\n");
                    Ok(true)
                }
                "PUSHF" => {
                    self.out.push_str("recomp_pushf(c);\n");
                    Ok(true)
                }
                "POPF" => {
                    self.out.push_str("recomp_popf(c);\n");
                    Ok(true)
                }
                _ => {
                    self.unsupported(addr, &mnemonic)
                }
            },
            SlowFamily::Int => {
                let vector = match mnemonic.as_str() {
                    "INT1" => Some(1),
                    "INTO" => Some(4),
                    _ => parse_slow_operands(&insn).and_then(|ops| match ops.first() {
                        Some(SlowOperand::Imm(value)) => Some(*value as u32),
                        _ => None,
                    }),
                };
                let Some(vector) = vector else {
                    return self.unsupported(addr, &mnemonic);
                };
                let _ = writeln!(self.out, "recomp_int(c, 0x{vector:x}u);");
                Ok(true)
            }
            SlowFamily::In => self.slow_port(&insn, true, addr),
            SlowFamily::Out => self.slow_port(&insn, false, addr),
            SlowFamily::Mmx => self.slow_mmx(&insn, addr),
            SlowFamily::Segment => self.slow_segment(&insn, addr),
            _ => {
                self.unsupported(addr, &mnemonic)
            }
        }
    }

    fn slow_port(&mut self, insn: &Insn, input: bool, addr: Addr) -> Result<bool> {
        let mnemonic = insn.mnemonic.to_ascii_uppercase();
        let repeat = insn.text.to_ascii_uppercase().contains(".REP");
        let prefix = if repeat { "rep_" } else { "" };
        if mnemonic.starts_with("INS") || mnemonic.starts_with("OUTS") {
            let suffix = mnemonic.chars().last().unwrap_or('B').to_ascii_lowercase();
            if !matches!(suffix, 'b' | 'w' | 'd') {
                return self.unsupported(addr, &mnemonic);
            }
            let root = if mnemonic.starts_with("INS") { "ins" } else { "outs" };
            let _ = writeln!(self.out, "{prefix}{root}{suffix}(c);");
            return Ok(true);
        }
        let Some(operands) = parse_slow_operands(insn) else {
            return self.unsupported(addr, &mnemonic);
        };
        if input && mnemonic == "IN" {
            let Some(SlowOperand::Gpr { index, shift, mask }) = operands.first() else {
                return self.unsupported(addr, "IN destination");
            };
            let Some(port) = operands.get(1).and_then(port_expr) else {
                return self.unsupported(addr, "IN port");
            };
            let size = gpr_size(*mask);
            let _ = writeln!(
                self.out,
                "recomp_insn_in(c, {port}, {size}, &c->r[{index}], {shift}, 0x{mask:x}u);"
            );
            return Ok(true);
        }
        if !input && mnemonic == "OUT" {
            let Some(port) = operands.first().and_then(port_expr) else {
                return self.unsupported(addr, "OUT port");
            };
            let Some(SlowOperand::Gpr { index, shift, mask }) = operands.get(1) else {
                return self.unsupported(addr, "OUT source");
            };
            let size = gpr_size(*mask);
            let _ = writeln!(
                self.out,
                "recomp_insn_out(c, {port}, &c->r[{index}], {shift}, 0x{mask:x}u, {size});"
            );
            return Ok(true);
        }
        self.unsupported(addr, &mnemonic)
    }

    fn slow_segment(&mut self, insn: &Insn, addr: Addr) -> Result<bool> {
        let mnemonic = insn.mnemonic.to_ascii_uppercase();
        if matches!(mnemonic.as_str(), "LDS" | "LES") {
            let Some(operands) = parse_slow_operands(insn) else {
                return self.unsupported(addr, &mnemonic);
            };
            let (Some(SlowOperand::Gpr { index, shift, mask }), Some(SlowOperand::Mem(address))) =
                (operands.first(), operands.get(1))
            else {
                return self.unsupported(addr, &mnemonic);
            };
            let temp = format!("a_{addr:08x}_");
            let _ = writeln!(self.out, "uint32_t {temp} = (uint32_t)({address});");
            let line = write_gpr(*index, *shift, *mask, &format!("recomp_rd32_(g_mem, {temp})"));
            let _ = writeln!(self.out, "{line}");
            let _ = writeln!(self.out, "(void)recomp_rd16_(g_mem, {temp} + 4u);");
            return Ok(true);
        }
        self.unsupported(addr, &mnemonic)
    }

    fn slow_mmx(&mut self, insn: &Insn, addr: Addr) -> Result<bool> {
        let mnemonic = insn.mnemonic.to_ascii_uppercase();
        if matches!(mnemonic.as_str(), "EMMS" | "FEMMS") {
            self.out.push_str("c->fpu_tag = 0xffffu;\n");
            return Ok(true);
        }
        let Some(operands) = parse_slow_operands(insn) else {
            return self.unsupported(addr, &mnemonic);
        };
        let Some(dst) = operands.first() else {
            return self.unsupported(addr, &mnemonic);
        };
        if let Some(operator) = sse_logic(&mnemonic) {
            let (SlowOperand::Xmm(destination), Some(source)) = (dst, operands.get(1)) else {
                return self.unsupported(addr, &mnemonic);
            };
            for lane in 0..4u32 {
                let source = match source {
                    SlowOperand::Xmm(index) => format!("c->xmm[{index}][{lane}]"),
                    SlowOperand::Mem(address) => format!(
                        "recomp_rd32_(g_mem, (uint32_t)({address}) + {}u)",
                        lane * 4
                    ),
                    _ => {
                        return self.unsupported(addr, &mnemonic);
                    }
                };
                let _ = writeln!(
                    self.out,
                    "c->xmm[{destination}][{lane}] = c->xmm[{destination}][{lane}] {operator} {source};"
                );
            }
            return Ok(true);
        }
        match mnemonic.as_str() {
            "MOVD" => return self.slow_movd(dst, operands.get(1), addr),
            "MOVQ" => return self.slow_movq(dst, operands.get(1), addr),
            "MOVNTQ" => {
                if let (SlowOperand::Mem(address), Some(SlowOperand::Mm(source))) = (dst, operands.get(1)) {
                    let _ = writeln!(
                        self.out,
                        "recomp_wr64_(g_mem, (uint32_t)({address}), c->mm[{source}]);"
                    );
                    return Ok(true);
                }
                return self.unsupported(addr, &mnemonic);
            }
            "PSHUFW" => {
                if let (SlowOperand::Mm(destination), Some(source), Some(SlowOperand::Imm(order))) =
                    (dst, operands.get(1), operands.get(2))
                {
                    let Some(source) = mmx_source(source, 64) else {
                        return self.unsupported(addr, &mnemonic);
                    };
                    let _ = writeln!(
                        self.out,
                        "c->mm[{destination}] = mmx_pshufw({source}, 0x{:x}u);",
                        order & 0xff
                    );
                    return Ok(true);
                }
                return self.unsupported(addr, &mnemonic);
            }
            _ => {}
        }
        let SlowOperand::Mm(destination) = dst else {
            return self.unsupported(addr, &mnemonic);
        };
        let Some(source) = operands.get(1).and_then(|operand| mmx_source(operand, 64)) else {
            return self.unsupported(addr, &mnemonic);
        };
        if let Some((function, bits)) = mmx_shift(&mnemonic) {
            let _ = writeln!(
                self.out,
                "c->mm[{destination}] = {function}(c->mm[{destination}], {source}, {bits}u);"
            );
            return Ok(true);
        }
        if let Some((function, bits)) = mmx_binary(&mnemonic) {
            if bits == 0 {
                let _ = writeln!(
                    self.out,
                    "c->mm[{destination}] = {function}(c->mm[{destination}], {source});"
                );
            } else {
                let _ = writeln!(
                    self.out,
                    "c->mm[{destination}] = {function}(c->mm[{destination}], {source}, {bits}u);"
                );
            }
            return Ok(true);
        }
        self.unsupported(addr, &mnemonic)
    }

    fn slow_movd(&mut self, dst: &SlowOperand, src: Option<&SlowOperand>, addr: Addr) -> Result<bool> {
        match (dst, src) {
            (SlowOperand::Mm(destination), Some(source)) => {
                let Some(source) = mmx_source(source, 32) else {
                    return self.unsupported(addr, "MOVD source");
                };
                let _ = writeln!(self.out, "c->mm[{destination}] = {source};");
            }
            (SlowOperand::Mem(address), Some(SlowOperand::Mm(source))) => {
                let _ = writeln!(
                    self.out,
                    "recomp_wr32_(g_mem, (uint32_t)({address}), (uint32_t)c->mm[{source}]);"
                );
            }
            (SlowOperand::Gpr { index, shift, mask }, Some(SlowOperand::Mm(source))) => {
                let line = write_gpr(*index, *shift, *mask, &format!("(uint32_t)c->mm[{source}]"));
                let _ = writeln!(self.out, "{line}");
            }
            _ => {
                return self.unsupported(addr, "MOVD form");
            }
        }
        Ok(true)
    }

    fn slow_movq(&mut self, dst: &SlowOperand, src: Option<&SlowOperand>, addr: Addr) -> Result<bool> {
        match (dst, src) {
            (SlowOperand::Mm(destination), Some(source)) => {
                let Some(source) = mmx_source(source, 64) else {
                    return self.unsupported(addr, "MOVQ source");
                };
                let _ = writeln!(self.out, "c->mm[{destination}] = {source};");
            }
            (SlowOperand::Mem(address), Some(SlowOperand::Mm(source))) => {
                let _ = writeln!(
                    self.out,
                    "recomp_wr64_(g_mem, (uint32_t)({address}), c->mm[{source}]);"
                );
            }
            _ => {
                return self.unsupported(addr, "MOVQ form");
            }
        }
        Ok(true)
    }

    fn call(&mut self, value: ValueId, target: Addr) -> Result<()> {
        self.publish(value)?;
        self.publish_guard(value)?;
        if let Some((private_target, wanted)) = self.plan.call_masks.get(&value).copied() {
            if private_target == target {
                if let Some(mask) = self
                    .ctx
                    .private_masks
                    .get(&target)
                    .and_then(|masks| select_mask(masks, wanted))
                {
                    let expected = self
                        .plan
                        .return_addr
                        .get(&value)
                        .copied()
                        .ok_or_else(|| self.error(self.s.value_addr[value as usize], "call without return address"))?;
                    let argument = self
                        .ctx
                        .private_input_arguments
                        .get(&target)
                        .and_then(|name| Field::from_name(name));
                    self.declarations.insert(Declaration::PrivatePrepare(target));
                    self.declarations.insert(Declaration::Checked(target));
                    self.declarations.insert(Declaration::PrivateContract {
                        addr: target,
                        mask,
                        input_argument: argument.map(|field| field.name().to_string()),
                    });
                    let args = argument.map_or_else(
                        || format!("c, 0x{expected:08x}u"),
                        |field| format!("c, c->r[{}], 0x{expected:08x}u", field as usize),
                    );
                    let _ = writeln!(
                        self.out,
                        "if (entry_contract_prepare_{target:08x}(c, 0)) {{ entry_contract_{target:08x}_{:04x}({args}); }} else {{ CALL_FN_CHECKED({target:08x}, 0x{expected:08x}u); }}", mask.0
                    );
                    if self.plan.settle_calls.contains(&value) {
                        self.out.push_str("x86_cc_settle(c);\n");
                    }
                    return Ok(());
                }
            }
        }
        let checked = self.ctx.checked_returns.contains(&target);
        if checked {
            let expected = self
                .plan
                .return_addr
                .get(&value)
                .copied()
                .ok_or_else(|| self.error(self.s.value_addr[value as usize], "call without return address"))?;
            self.declarations.insert(Declaration::Checked(target));
            let _ = writeln!(self.out, "CALL_FN_CHECKED({target:08x}, 0x{expected:08x}u);");
        } else {
            self.declare_entry(target);
            let _ = writeln!(self.out, "CALL_FN({target:08x});");
        }
        if self.plan.settle_calls.contains(&value) {
            self.out.push_str("x86_cc_settle(c);\n");
        }
        Ok(())
    }

    fn call_ind(&mut self, value: ValueId, addr: Addr) -> Result<()> {
        self.publish(value)?;
        self.publish_guard(value)?;
        let args = self.s.args(value);
        let target = args.get(1).or_else(|| args.first()).copied()
            .ok_or_else(|| self.error(addr, "indirect call without target"))?;
        let target = self.expr(target)?;
        let name = format!("ic_{:08x}_{:08x}", self.s.addr, addr);
        self.requires.icaches.insert(name.clone());
        let _ = writeln!(
            self.out,
            "recomp_icall(c, (uint32_t)({target}), &{name}, 0x{addr:08x}u);"
        );
        if self.plan.settle_calls.contains(&value) {
            self.out.push_str("x86_cc_settle(c);\n");
        }
        Ok(())
    }

    fn divide(&mut self, value: ValueId, addr: Addr) -> Result<()> {
        self.publish(value)?;
        let args = self.s.args(value);
        let divisor = args.get(2).copied()
            .ok_or_else(|| self.error(addr, "division without divisor"))?;
        let divisor = self.expr(divisor)?;
        let helper = match self.s.value(value).op {
            Op::Div32 { .. } => "div32",
            _ => "idiv32",
        };
        let _ = writeln!(self.out, "{helper}(c, (uint32_t)({divisor}), 0x{addr:08x}u);");
        let _ = writeln!(
            self.out,
            "{}",
            self.assignment(value, "c->r[0] | ((uint64_t)c->r[2] << 32)")
        );
        Ok(())
    }

    fn load(&mut self, value: ValueId) -> Result<()> {
        self.publish(value)?;
        let size = self.s.value(value).size;
        let args = self.s.args(value);
        let address = self.expr(args.get(1).copied().ok_or_else(|| self.error(0, "load without address"))?)?;
        let read = match size {
            1 => "rd8",
            2 => "rd16",
            4 => "rd32",
            8 => "rd64",
            _ => return Err(self.error(self.s.value_addr[value as usize], "unsupported load size")),
        };
        let helper = format!("recomp_{read}_");
        let _ = writeln!(
            self.out,
            "{}",
            self.assignment(value, &format!("{helper}(arena_, (uint32_t)({address}))"))
        );
        Ok(())
    }

    fn store(&mut self, value: ValueId) -> Result<()> {
        self.publish(value)?;
        let args = self.s.args(value);
        let address = self.expr(args.get(1).copied().ok_or_else(|| self.error(0, "store without address"))?)?;
        let stored = args.get(2).copied().ok_or_else(|| self.error(0, "store without value"))?;
        let stored = self.expr(stored)?;
        let size = self.s.value(args[2]).size;
        let write = match size {
            1 => "wr8",
            2 => "wr16",
            4 => "wr32",
            8 => "wr64",
            _ => return Err(self.error(self.s.value_addr[value as usize], "unsupported store size")),
        };
        let helper = format!("recomp_{write}_");
        let _ = writeln!(
            self.out,
            "{helper}(arena_, (uint32_t)({address}), ({}){stored});",
            ctype(size)
        );
        Ok(())
    }

    fn branch(&mut self, value: ValueId, target: Addr) -> Result<()> {
        let current = self.block_of(value);
        if let Some(target_block) = self.successor(current, Some(target))? {
            if self.can_inline(current, target_block) {
                return self.inline_block(current, target_block);
            }
            return self.edge(Some(current), target_block);
        }
        self.publish(value)?;
        if self.ctx.known_entries.contains(&target) {
            self.declare_entry(target);
            let _ = writeln!(self.out, "CALL_FN({target:08x}); return;");
        } else {
            let at = self.s.value_addr[value as usize];
            let _ = writeln!(
                self.out,
                "c->eip = 0x{at:08x}u; recomp_jump(c, 0x{target:08x}u); return;"
            );
        }
        Ok(())
    }

    fn cbranch(&mut self, value: ValueId, target: Addr) -> Result<()> {
        self.publish(value)?;
        let current = self.block_of(value);
        let args = self.s.args(value);
        let condition = self.expr(*args.first().ok_or_else(|| self.error(0, "branch without condition"))?)?;
        let taken = self.successor(current, Some(target))?;
        if let Some(taken) = taken {
            let fallthrough = self
                .successors(current)
                .iter()
                .copied()
                .find(|block| *block != taken)
                .unwrap_or(taken);
            let _ = writeln!(self.out, "if ((uint32_t)({condition}))");
            self.edge(Some(current), taken)?;
            if fallthrough != taken && self.can_inline(current, fallthrough) {
                return self.inline_block(current, fallthrough);
            }
            self.out.push_str("else\n");
            self.edge(Some(current), fallthrough)?;
            return Ok(());
        }
        let at = self.s.value_addr[value as usize];
        if self.ctx.known_entries.contains(&target) {
            self.declare_entry(target);
            let _ = writeln!(self.out, "if ((uint32_t)({condition})) {{ CALL_FN({target:08x}); return; }}");
        } else {
            let _ = writeln!(
                self.out,
                "if ((uint32_t)({condition})) {{ c->eip = 0x{at:08x}u; recomp_jump(c, 0x{target:08x}u); return; }}"
            );
        }
        let fallthrough = self
            .successors(current)
            .iter()
            .copied()
            .find(|block| self.s.blocks[*block].start == self.s.blocks[current].end);
        match fallthrough {
            Some(fallthrough) => self.edge(Some(current), fallthrough),
            None => Ok(()),
        }
    }

    fn branch_ind(&mut self, value: ValueId, addr: Addr) -> Result<()> {
        let args = self.s.args(value);
        let target = self.expr(*args.first().ok_or_else(|| self.error(addr, "indirect jump without target"))?)?;
        let current = self.block_of(value);
        let successors = self.successors(current).to_vec();
        let _ = writeln!(self.out, "switch ((uint32_t)({target})) {{");
        for target_block in &successors {
            let _ = writeln!(self.out, "case 0x{:08x}u:", self.plan.block_addrs[*target_block]);
            self.edge(Some(current), *target_block)?;
        }
        self.out.push_str("default:\n");
        let default_lines = self.x87_lines(|x87| x87.default_lines(self.clone, value, &ValueNames(self)));
        self.write_x87(default_lines);
        self.publish(value)?;
        let _ = writeln!(self.out, "c->eip = 0x{addr:08x}u; recomp_jump(c, (uint32_t)({target})); return;");
        self.out.push_str("}\n");
        Ok(())
    }

    fn return_value(&mut self, value: ValueId) -> Result<()> {
        if let Some(private) = self.private {
            self.publish_private_return(value, private)?;
            self.out.push_str(
                "if (RECOMP_UNLIKELY(c->eip != expected_return_)) {\n",
            );
            self.publish_private_fallback(value)?;
            self.out.push_str("recomp_return_fast_(c);\nreturn;\n}\nreturn;\n");
            return Ok(());
        }
        self.publish(value)?;
        if self.checked {
            self.out.push_str(
                "if (!checked_return_ || RECOMP_UNLIKELY(c->eip != expected_return_)) recomp_return_fast_(c);\nreturn;\n",
            );
        } else {
            self.out.push_str("recomp_return_fast_(c);\nreturn;\n");
        }
        Ok(())
    }

    fn publish_private_return(&mut self, value: ValueId, private: &PrivateEmit) -> Result<()> {
        let Some(keys) = self.plan.publish.get(&value).cloned() else { return Ok(()) };
        let keys: Vec<StateKey> = keys
            .into_iter()
            .filter(|key| {
                self.s
                    .field(*key)
                    .is_none_or(|field| private.mask.contains(field))
            })
            .collect();
        self.publish_keys(value, &keys, true)
    }

    fn publish_private_fallback(&mut self, value: ValueId) -> Result<()> {
        let Some(keys) = self.plan.publish.get(&value).cloned() else { return Ok(()) };
        let Some(state) = self.s.boundary(value) else { return Ok(()) };
        let keys: Vec<StateKey> = keys
            .into_iter()
            .filter(|key| state.iter().any(|(candidate, _)| candidate == key))
            .collect();
        self.publish_keys(value, &keys, true)
    }

    fn seh(&mut self, kind: SehEffect, addr: Addr) {
        let _ = writeln!(self.out, "c->eip = 0x{addr:08x}u;");
        match kind {
            SehEffect::Enter => self.out.push_str(
                "{ jmp_buf *b_ = recomp_seh_frame_enter(c); if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } }\n",
            ),
            SehEffect::Adopt => self.out.push_str(
                "{ jmp_buf *b_ = recomp_seh_frame_adopt(c); if (b_) { if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } } }\n",
            ),
            SehEffect::Leave => self.out.push_str("recomp_seh_frame_leave(c);\n"),
            SehEffect::Landing => {
                let _ = writeln!(self.out, "if (recomp_seh_pending_target()) recomp_seh_intercept(c, 0x{addr:08x}u);");
            }
            SehEffect::Orphan => {
                let _ = writeln!(self.out, "recomp_seh_frame_orphan(c, 0x{addr:08x}u);");
            }
        }
    }

    fn publish(&mut self, value: ValueId) -> Result<()> {
        let Some(keys) = self.plan.publish.get(&value).cloned() else { return Ok(()) };
        self.publish_keys(value, &keys, true)
    }

    fn publish_guard(&mut self, value: ValueId) -> Result<()> {
        let Some(keys) = self.plan.guard.get(&value).cloned() else { return Ok(()) };
        self.out.push_str("if (RECOMP_UNLIKELY(recomp_hooks_ever)) {\n");
        self.publish_keys(value, &keys, false)?;
        self.out.push_str("}\n");
        Ok(())
    }

    fn publish_keys(&mut self, value: ValueId, keys: &[StateKey], flags_action: bool) -> Result<()> {
        let Some(state) = self.s.boundary(value) else { return Ok(()) };
        let state: BTreeMap<StateKey, ValueId> = state
            .iter()
            .copied()
            .filter(|(_, value)| flags_action || self.available(*value))
            .collect();
        let mut locations = BTreeSet::new();
        let mut cs_write = false;
        for key in keys {
            if let StateKey::Register(offset) = key {
                if Registers::get()
                    .covering(*offset as u64)
                    .is_some_and(|(name, _)| name == "CS")
                {
                    cs_write = true;
                }
            }
            if let Some(Some(location)) = self.key_location.get(key) {
                locations.insert(*location);
            }
        }
        if cs_write {
            self.out.push_str("recomp_int(c, 6u);\n");
        }
        let flag_fields: Fields = keys
            .iter()
            .filter_map(|key| self.s.field(*key))
            .filter(|field| Fields::FLAGS.contains(*field))
            .collect();
        let descriptor = flags_action
            .then(|| self.plan.flag_publish.get(&value))
            .flatten()
            .and_then(|publication| publication.descriptor)
            .map(|descriptor| (descriptor, descriptor.mask & flag_fields))
            .filter(|(_, mask)| !mask.is_empty());
        if !flag_fields.is_empty() {
            let need = cc_mask(Fields::FLAGS.difference(flag_fields));
            if need == 0 {
                self.out.push_str("x86_cc_drop(c);\n");
            } else {
                let _ = writeln!(self.out, "x86_cc_settle_or_drop(c, 0x{need:02x}u);");
            }
        }
        if let Some((_, mask)) = descriptor {
            locations.retain(|location| {
                !matches!(location, Location::Flag(field) if mask.contains(*field))
            });
        }
        for location in locations {
            self.publish_location(location, &state)?;
        }
        if let Some((descriptor, mask)) = descriptor {
            self.publish_descriptor(&descriptor, mask)?;
        }
        Ok(())
    }

    fn publish_descriptor(&mut self, descriptor: &FlagDescriptor, mask: Fields) -> Result<()> {
        let op = match descriptor.kind {
            rk_ssa::FlagKind::Add => "X86_CC_ADD",
            rk_ssa::FlagKind::Sub | rk_ssa::FlagKind::Cmp => "X86_CC_SUB",
            rk_ssa::FlagKind::Logic => "X86_CC_LOGIC",
            rk_ssa::FlagKind::Inc => "X86_CC_INC",
            rk_ssa::FlagKind::Dec => "X86_CC_DEC",
            rk_ssa::FlagKind::Adc => "X86_CC_ADC",
            rk_ssa::FlagKind::Sbb => "X86_CC_SBB",
            rk_ssa::FlagKind::Incoming | rk_ssa::FlagKind::Join => return Ok(()),
        };
        let (a, b, carry) = match descriptor.kind {
            rk_ssa::FlagKind::Logic => ("0u".to_string(), "0u".to_string(), "0u".to_string()),
            rk_ssa::FlagKind::Inc | rk_ssa::FlagKind::Dec => (
                self.expr(descriptor.a.unwrap_or(0))?,
                "0u".to_string(),
                "0u".to_string(),
            ),
            rk_ssa::FlagKind::Adc | rk_ssa::FlagKind::Sbb => (
                self.expr(descriptor.a.unwrap_or(0))?,
                self.expr(descriptor.b.unwrap_or(0))?,
                self.expr(descriptor.carry.unwrap_or(0))?,
            ),
            _ => (
                self.expr(descriptor.a.unwrap_or(0))?,
                self.expr(descriptor.b.unwrap_or(0))?,
                "0u".to_string(),
            ),
        };
        let result = self.expr(descriptor.result)?;
        let _ = writeln!(self.out, "c->cc_op = {op};");
        let _ = writeln!(self.out, "c->cc_size = {}u;", descriptor.size);
        let _ = writeln!(self.out, "c->cc_mask = 0x{:02x}u;", cc_mask(mask));
        let _ = writeln!(self.out, "c->cc_carry = (uint8_t)(({carry}) & 1u);");
        let _ = writeln!(self.out, "c->cc_a = (uint32_t)({a});");
        let _ = writeln!(self.out, "c->cc_b = (uint32_t)({b});");
        let _ = writeln!(self.out, "c->cc_res = (uint32_t)({result});");
        Ok(())
    }

    fn available(&self, value: ValueId) -> bool {
        if let Some(known) = self.available.borrow()[value as usize] {
            return known;
        }
        let result = if self.materialized[value as usize] {
            self.plan.live.contains(&value)
        } else {
            match self.s.value(value).op {
                Op::Const(_) | Op::Target(_) | Op::Memory | Op::Input(_) | Op::StateReload(_) => true,
                Op::Byte(_) | Op::Pack | Op::StateKeep(_) | Op::P(OpCode::Copy | OpCode::IntZExt) => {
                    self.s.args(value).iter().all(|arg| self.available(*arg))
                }
                _ => false,
            }
        };
        self.available.borrow_mut()[value as usize] = Some(result);
        result
    }

    fn publish_location(&mut self, location: Location, state: &BTreeMap<StateKey, ValueId>) -> Result<()> {
        match location {
            Location::Gpr(index) => {
                let field = Field::ALL[index];
                let lanes = rk_ir::lanes(field);
                let values: Vec<Option<ValueId>> = (0..4)
                    .map(|lane| lanes.get(lane).and_then(|key| state.get(key).copied()))
                    .collect();
                self.publish_lanes(&format!("r[{index}]"), values)?;
            }
            Location::Flag(field) => {
                let key = rk_ir::lanes(field).first().copied();
                let value = key.and_then(|key| state.get(&key).copied());
                let expr = match value {
                    Some(value) => self.expr(value)?,
                    None => format!("c->eflags_{}", field.name().to_ascii_lowercase()),
                };
                let _ = writeln!(self.out, "c->eflags_{} = (uint32_t)(({expr}) & 1u);", field.name().to_ascii_lowercase());
            }
            Location::Eip => self.publish_register("eip", "EIP", state)?,
            Location::Fs => self.publish_register("fs_base", "FS_OFFSET", state)?,
            Location::Df => {
                let key = StateKey::Register(Registers::get().require("DF").offset as u32);
                let value = state.get(&key).copied();
                let expr = match value {
                    Some(value) => self.expr(value)?,
                    None => "c->eflags_df".to_string(),
                };
                let _ = writeln!(self.out, "c->eflags_df = (uint32_t)(({expr}) & 1u);");
            }
        }
        Ok(())
    }

    fn publish_register(
        &mut self,
        field: &str,
        register: &str,
        state: &BTreeMap<StateKey, ValueId>,
    ) -> Result<()> {
        let varnode = Registers::get().require(register);
        let values: Vec<Option<ValueId>> = (0..varnode.size)
            .map(|lane| state.get(&StateKey::Register((varnode.offset + lane as u64) as u32)).copied())
            .collect();
        self.publish_lanes(field, values)
    }

    fn publish_lanes(&mut self, field: &str, values: Vec<Option<ValueId>>) -> Result<()> {
        if let Some(all) = values.iter().copied().collect::<Option<Vec<_>>>() {
            if let Some(text) = self.pack_lanes(&all) {
                let _ = writeln!(self.out, "c->{field} = {text};");
                return Ok(());
            }
        }
        let mut parts = Vec::new();
        for (lane, value) in values.into_iter().enumerate() {
            let expr = match value {
                Some(value) => self.expr(value)?,
                None => format!("((c->{field} >> {}) & 0xffu)", lane * 8),
            };
            parts.push(format!("((({expr}) & 0xffu) << {})", lane * 8));
        }
        let _ = writeln!(self.out, "c->{field} = {};", parts.join(" | "));
        Ok(())
    }

    fn successor(&self, current: usize, target: Option<Addr>) -> Result<Option<usize>> {
        let successors = self.successors(current);
        match target {
            Some(addr) => Ok(successors
                .iter()
                .copied()
                .find(|block| self.plan.block_addrs[*block] == addr)),
            None => successors
                .first()
                .copied()
                .map(Some)
                .ok_or_else(|| self.error(self.s.addr, "block has no fallthrough")),
        }
    }

    fn successors(&self, current: usize) -> &[usize] {
        &self.successors[current]
    }

    fn block_of(&self, value: ValueId) -> usize {
        self.value_block[value as usize] as usize
    }

    fn block_end_addr(&self, block: usize) -> Option<Addr> {
        let insns = &self.ctx.ir.insns;
        match insns.get(self.s.blocks[block].end as usize) {
            Some(insn) => Some(insn.addr),
            None => insns.last().map(|insn| insn.addr + insn.length as Addr),
        }
    }

    fn edge(&mut self, source: Option<usize>, target: usize) -> Result<()> {
        let pred = source.map_or(DISPATCH_BLOCK, |block| block as u32);
        self.out.push_str("{\n");
        self.edge_copies(pred, target)?;
        let _ = writeln!(self.out, "goto {}{target};", self.block_prefix());
        self.out.push_str("}\n");
        Ok(())
    }

    fn edge_copies(&mut self, pred: u32, target: usize) -> Result<()> {
        let block = &self.s.blocks[target];
        let position = block.preds.iter().position(|candidate| *candidate == pred);
        if let Some(position) = position {
            for &phi in &block.phis {
                if !self.plan.live.contains(&phi) || self.s.value(phi).size == 0 {
                    continue;
                }
                let arg = *self.s.args(phi).get(position).ok_or_else(|| self.error(0, "phi arity"))?;
                let ctype = ctype(self.s.value(phi).size);
                let expr = self.expr(arg)?;
                let _ = writeln!(self.out, "{ctype} p{phi} = ({ctype})({expr});");
            }
            for &phi in &block.phis {
                if !self.plan.live.contains(&phi) || self.s.value(phi).size == 0 {
                    continue;
                }
                let _ = writeln!(self.out, "v{phi} = p{phi};");
            }
        }
        Ok(())
    }

    fn declare_entry(&mut self, target: Addr) {
        self.declarations.insert(Declaration::Entry(target));
    }

    fn assignment(&self, value: ValueId, expr: &str) -> String {
        let size = self.s.value(value).size;
        let ctype = ctype(size);
        if matches!(size, 4 | 8) {
            format!("v{value} = ({ctype})({expr});")
        } else {
            format!("v{value} = ({ctype})(({expr}) & {});", c_mask(size))
        }
    }

    fn expr(&self, value: ValueId) -> Result<String> {
        if let Some((carrier, lane)) = self.keep_lane.get(&value).copied() {
            return Ok(if lane == 0 {
                format!("(v{carrier} & 0xffu)")
            } else {
                format!("((v{carrier} >> {}) & 0xffu)", lane as u32 * 8)
            });
        }
        if self.materialized[value as usize] {
            return Ok(format!("v{value}"));
        }
        let item = *self.s.value(value);
        match item.op {
            Op::Const(data) => Ok(const_expr(data, item.size)),
            Op::Input(key) | Op::StateReload(key) => self.field_expr(key, item.size),
            Op::Target(addr) => Ok(format!("0x{addr:08x}u")),
            Op::Memory => Ok("0u".to_string()),
            Op::Byte(index) => Ok(format!(
                "((({}) >> {}) & 0xffu)",
                self.expr(*self.s.args(value).first().ok_or_else(|| self.error(0, "missing argument"))?)?,
                index as u32 * 8
            )),
            Op::Pack => match self.pack_lanes(self.s.args(value)) {
                Some(text) => Ok(text),
                None => self.pack_bytes(self.s.args(value), item.size > 4),
            },
            Op::StateKeep(key) => {
                let old = self.expr(*self.s.args(value).get(1).ok_or_else(|| self.error(0, "missing argument"))?)?;
                let loaded = self.field_expr(key, item.size)?;
                let loaded = if field_lane(key).is_some_and(|(field, _)| Fields::FLAGS.contains(field)) {
                    format!("(x86_cc_settle(c), {loaded})")
                } else {
                    loaded
                };
                Ok(keep_expr(&loaded, &old))
            }
            Op::P(OpCode::Copy) | Op::P(OpCode::IntZExt) => self.expr(
                *self.s.args(value).first().ok_or_else(|| self.error(0, "missing argument"))?,
            ),
            _ => Ok(format!("v{value}")),
        }
    }

    fn operation(&self, value: ValueId) -> Result<String> {
        let item = *self.s.value(value);
        let args = self.s.args(value);
        let a = |index: usize| -> Result<String> { self.expr(*args.get(index).ok_or_else(|| self.error(0, "missing argument"))?) };
        let wide = item.size > 4 || item.op == Op::P(OpCode::SubPiece);
        let cast = |text: String| if wide { format!("(uint64_t)({text})") } else { text };
        let zero = if wide { "0ull" } else { "0u" };
        let one = if wide { "1ull" } else { "1u" };
        let result = match item.op {
            Op::Const(data) => const_expr(data, item.size),
            Op::Input(key) => self.field_expr(key, item.size)?,
            Op::Target(addr) => format!("0x{addr:08x}u"),
            Op::Byte(index) => format!("(({}) >> {})", a(0)?, index as u32 * 8),
            Op::Pack => match self.pack_lanes(args) {
                Some(text) => text,
                None => self.pack_bytes(args, item.size > 4)?,
            },
            Op::StateReload(key) => self.field_expr(key, item.size)?,
            Op::StateKeep(key) => {
                let old = a(1)?;
                let loaded = self.field_expr(key, item.size)?;
                let loaded = if field_lane(key).is_some_and(|(field, _)| Fields::FLAGS.contains(field)) {
                    format!("(x86_cc_settle(c), {loaded})")
                } else {
                    loaded
                };
                keep_expr(&loaded, &old)
            }
            Op::Memory => "0u".to_string(),
            Op::Phi => format!("v{value}"),
            Op::P(code) => match code {
                OpCode::Copy | OpCode::IntZExt => a(0)?,
                OpCode::IntSExt => {
                    let from = args.first().map_or(1, |arg| self.s.value(*arg).size) * 8;
                    let to = item.size * 8;
                    format!("(uint{to}_t)(int{from}_t)({})", a(0)?)
                }
                OpCode::IntAdd => format!("{} + {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntSub => format!("{} - {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntMult => format!("{} * {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntAnd => format!("{} & {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntOr => format!("{} | {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntXor => format!("{} ^ {}", cast(a(0)?), cast(a(1)?)),
                OpCode::IntEqual => format!("(({}) == ({}))", a(0)?, a(1)?),
                OpCode::IntNotEqual => format!("(({}) != ({}))", a(0)?, a(1)?),
                OpCode::IntLess => format!("(({}) < ({}))", a(0)?, a(1)?),
                OpCode::IntLessEqual => format!("(({}) <= ({}))", a(0)?, a(1)?),
                OpCode::IntSLess | OpCode::IntSLessEqual => {
                    let bits = args.first().map_or(32, |arg| self.s.value(*arg).size) * 8;
                    let sign = if bits >= 64 { format!("0x{:x}ull", 1u64 << 63) } else { format!("0x{:x}u", 1u32 << (bits - 1)) };
                    let op = if code == OpCode::IntSLess { "<" } else { "<=" };
                    format!("((({}) ^ {sign}) {op} (({}) ^ {sign}))", a(0)?, a(1)?)
                }
                OpCode::IntLeft | OpCode::IntRight => {
                    let bits = args.first().map_or(32, |arg| self.s.value(*arg).size) * 8;
                    let op = if code == OpCode::IntLeft { "<<" } else { ">>" };
                    format!("(({}) >= {bits} ? {zero} : (({}) {op} ({})))", a(1)?, a(0)?, a(1)?)
                }
                OpCode::IntSRight => {
                    let bits = args.first().map_or(32, |arg| self.s.value(*arg).size) * 8;
                    let sign = if bits >= 64 { format!("0x{:x}ull", 1u64 << 63) } else { format!("0x{:x}u", 1u32 << (bits - 1)) };
                    format!("(({}) >= {bits} ? (({}) - (({}) >> {})) : (((({}) ^ {sign}) >> ({})) - ({sign} >> ({}))))",
                        a(1)?, zero, a(0)?, bits - 1, a(0)?, a(1)?, a(1)?)
                }
                OpCode::IntCarry => {
                    let operand = c_mask(args.first().map_or(4, |arg| self.s.value(*arg).size));
                    format!("(({}) > ({operand} - ({})))", a(0)?, a(1)?)
                }
                OpCode::IntSCarry | OpCode::IntSBorrow => {
                    let bits = args.first().map_or(32, |arg| self.s.value(*arg).size) * 8;
                    let op = if code == OpCode::IntSCarry { "+" } else { "-" };
                    let not = if code == OpCode::IntSCarry { "~" } else { "" };
                    let x = a(0)?;
                    let y = a(1)?;
                    let diff = format!("(({x}) ^ ({y}))");
                    let sum = format!("(({x}) {op} ({y}))");
                    let merged = format!("({not}{diff} & (({x}) ^ {sum}))");
                    format!("(({merged} >> {}) & {one})", bits - 1)
                }
                OpCode::Int2Comp => format!("{zero} - {}", cast(a(0)?)),
                OpCode::IntNegate => format!("~({})", cast(a(0)?)),
                OpCode::BoolNegate => format!("!({})", a(0)?),
                OpCode::BoolXor => format!("(({}) != ({}))", a(0)?, a(1)?),
                OpCode::BoolAnd => format!("(({}) && ({}))", a(0)?, a(1)?),
                OpCode::BoolOr => format!("(({}) || ({}))", a(0)?, a(1)?),
                OpCode::IntDiv => format!("(({}) / ({}))", cast(a(0)?), cast(a(1)?)),
                OpCode::IntRem => format!("(({}) % ({}))", cast(a(0)?), cast(a(1)?)),
                OpCode::IntSDiv => format!("(({})({}) / ({})({}))", signed_type(item.size), a(0)?, signed_type(item.size), a(1)?),
                OpCode::IntSRem => format!("(({})({}) % ({})({}))", signed_type(item.size), a(0)?, signed_type(item.size), a(1)?),
                OpCode::PopCount => {
                    let helper = if args.first().map_or(4, |arg| self.s.value(*arg).size) > 4 { "__builtin_popcountll" } else { "__builtin_popcount" };
                    format!("{helper}(({}))", a(0)?)
                }
                OpCode::LzCount => {
                    let bits = args.first().map_or(4, |arg| self.s.value(*arg).size) * 8;
                    let helper = if bits > 32 { "__builtin_clzll" } else { "__builtin_clz" };
                    format!("(({}) ? ({helper}(({})) - {}) : {bits})", a(0)?, a(0)?, 32u32.saturating_sub(bits as u32))
                }
                OpCode::Piece => {
                    let shift = args.get(1).map_or(0, |arg| self.s.value(*arg).size) * 8;
                    if shift >= 32 {
                        format!("((((uint64_t)({})) << {shift}) | ((uint64_t)({})))", a(0)?, a(1)?)
                    } else {
                        format!("((({}) << {shift}) | ({}))", a(0)?, a(1)?)
                    }
                }
                OpCode::SubPiece => format!("(({}) >= 8 ? 0ull : (({}) >> (({}) * 8)))", a(1)?, a(0)?, a(1)?),
                _ => return Err(self.error(self.s.value_addr[value as usize], &format!("unsupported p-code {code:?} at v{value}"))),
            },
            _ => return Err(self.error(self.s.value_addr[value as usize], &format!("unsupported SSA op {:?} at v{value}", item.op))),
        };
        Ok(result)
    }

    fn field_expr(&self, key: StateKey, size: u8) -> Result<String> {
        if size <= 1 {
            return self.lane_expr(key);
        }
        let (base, lane, width) = self
            .lane_origin(key)
            .ok_or_else(|| self.error(0, &format!("unsupported register lane {key:?}")))?;
        let size = size as u32;
        if lane + size > width {
            return Err(self.error(0, &format!("register field {key:?} has no {size}-byte slice")));
        }
        if lane == 0 && size == width {
            Ok(base)
        } else if lane == 0 {
            Ok(format!("({base} & 0x{:x}u)", (1u64 << (size * 8)) - 1))
        } else {
            Ok(format!("(({base} >> {}) & 0x{:x}u)", lane * 8, (1u64 << (size * 8)) - 1))
        }
    }

    fn lane_expr(&self, key: StateKey) -> Result<String> {
        let (base, lane, width) = self
            .lane_origin(key)
            .ok_or_else(|| self.error(0, &format!("unsupported register lane {key:?}")))?;
        if width == 1 {
            Ok(format!("({base} & 0xffu)"))
        } else {
            Ok(format!("(({base} >> {}) & 0xffu)", lane * 8))
        }
    }

    fn lane_origin(&self, key: StateKey) -> Option<(String, u32, u32)> {
        if let Some((field, lane)) = field_lane(key) {
            if (field as usize) < 8 {
                if let Some(private) = self.private {
                    if private.input_argument == Some(field) {
                        return Some((format!("{}_", field.name().to_lowercase()), lane as u32, 4));
                    }
                }
                return Some((format!("c->r[{}]", field as usize), lane as u32, 4));
            }
            return Some((
                format!("c->eflags_{}", field.name().to_ascii_lowercase()),
                0,
                1,
            ));
        }
        let StateKey::Register(offset) = key else {
            return None;
        };
        let (name, register) = Registers::get().covering(offset as u64)?;
        let lane = (offset as u64 - register.offset) as u32;
        let base = match name {
            "EIP" | "IP" => "c->eip",
            "FS_OFFSET" => "c->fs_base",
            "DF" => "c->eflags_df",
            "ES" | "DS" | "SS" => "0x23u",
            "CS" => "0x1bu",
            "FS" => "0x3bu",
            "GS" | "GS_OFFSET" => "0u",
            _ => return None,
        };
        Some((base.to_string(), lane, register.size))
    }

    fn pack_bytes(&self, args: &[ValueId], wide: bool) -> Result<String> {
        let cast = if wide { "uint64_t" } else { "uint32_t" };
        let mut parts = Vec::new();
        for (index, arg) in args.iter().enumerate() {
            parts.push(format!(
                "((({cast})({}) & 0xffu) << {})",
                self.expr(*arg)?,
                index * 8
            ));
        }
        Ok(format!("({})", parts.join(" | ")))
    }

    fn pack_lanes(&self, args: &[ValueId]) -> Option<String> {
        let constants: Option<Vec<u64>> = args
            .iter()
            .map(|arg| match self.s.value(*arg).op {
                Op::Const(data) => Some(data),
                _ => None,
            })
            .collect();
        if let Some(constants) = constants {
            let mut value = 0u64;
            for (index, byte) in constants.iter().enumerate() {
                value |= (byte & 0xff) << (index * 8);
            }
            return Some(if args.len() > 4 {
                format!("0x{value:x}ull")
            } else {
                format!("0x{value:x}u")
            });
        }
        let mut inner: Option<ValueId> = None;
        let mut bytes = true;
        for (index, arg) in args.iter().enumerate() {
            let value = *arg;
            match self.s.value(value).op {
                Op::Byte(byte) if byte as usize == index => {
                    let source = *self.s.args(value).first()?;
                    if inner.is_some_and(|inner| inner != source) {
                        bytes = false;
                        break;
                    }
                    inner = Some(source);
                }
                _ => {
                    bytes = false;
                    break;
                }
            }
        }
        if bytes {
            let inner = inner?;
            let bits = args.len() * 8;
            let text = self.expr(inner).ok()?;
            if self.s.value(inner).size as usize * 8 == bits {
                return Some(format!("({text})"));
            }
            if bits < 64 {
                return Some(format!("(({text}) & 0x{:x}u)", (1u64 << bits) - 1));
            }
        }
        if args
            .iter()
            .any(|arg| self.materialized[*arg as usize])
        {
            return None;
        }
        let mut origin: Option<(String, u32, u32)> = None;
        let mut lanes = Vec::new();
        for (index, arg) in args.iter().enumerate() {
            let value = *arg;
            let item = *self.s.value(value);
            let (key, offset) = match item.op {
                Op::Input(key) | Op::StateReload(key) => (key, 0),
                Op::Byte(byte) => {
                    if byte as usize != index {
                        return None;
                    }
                    let inner = *self.s.args(value).first()?;
                    match self.s.value(inner).op {
                        Op::Input(key) | Op::StateReload(key) => (key, byte as u32),
                        _ => return None,
                    }
                }
                _ => return None,
            };
            let (base, lane, width) = self.lane_origin(key)?;
            let lane = lane + offset;
            if lane >= width {
                return None;
            }
            match &origin {
                None => origin = Some((base, lane, width)),
                Some((first_base, _, first_width)) => {
                    if *first_base != base || *first_width != width {
                        return None;
                    }
                }
            }
            lanes.push(lane);
        }
        let (base, first, width) = origin?;
        if lanes.iter().enumerate().any(|(index, lane)| *lane != first + index as u32) {
            return None;
        }
        let bits = args.len() * 8;
        if first == 0 && bits >= width as usize * 8 {
            Some(format!("({base})"))
        } else if first == 0 {
            Some(format!("({base} & 0x{:x}u)", (1u64 << bits) - 1))
        } else {
            Some(format!("(({base} >> {}) & 0x{:x}u)", first * 8, (1u64 << bits) - 1))
        }
    }

    fn error(&self, addr: Addr, detail: &str) -> rk_core::Error {
        Diagnostic::error(addr, Reason::Unsupported { detail: detail.into() })
    }
}

fn const_expr(data: u64, size: u8) -> String {
    if size > 4 {
        format!("0x{data:x}ull")
    } else {
        format!("0x{:x}u", data & ((1u64 << (size as u32 * 8)) - 1))
    }
}

fn keep_expr(loaded: &str, old: &str) -> String {
    if loaded == old {
        old.to_string()
    } else {
        format!("(RECOMP_UNLIKELY(recomp_hooks_ever) ? ({loaded}) : ({old}))")
    }
}

fn reads_c_field(
    s: &rk_ssa::Ssa,
    value: ValueId,
    materialized: &[bool],
    private: Option<&PrivateEmit>,
) -> bool {
    if materialized[value as usize] {
        return false;
    }
    match s.value(value).op {
        Op::Input(StateKey::Memory) | Op::StateReload(StateKey::Memory) | Op::StateKeep(StateKey::Memory) => false,
        Op::Input(key) | Op::StateReload(key) | Op::StateKeep(key) => private_loads(private, key),
        Op::Byte(_) => s
            .args(value)
            .first()
            .is_some_and(|arg| reads_c_field(s, *arg, materialized, private)),
        Op::Pack => s
            .args(value)
            .iter()
            .any(|arg| reads_c_field(s, *arg, materialized, private)),
        Op::P(OpCode::Copy | OpCode::IntZExt) => s
            .args(value)
            .first()
            .is_some_and(|arg| reads_c_field(s, *arg, materialized, private)),
        _ => false,
    }
}

fn identity_assignment(line: &str) -> bool {
    let text = line.trim().trim_end_matches(';');
    text.split_once('=').is_some_and(|(target, value)| target.trim() == value.trim())
}

#[derive(Clone, Debug)]
enum SlowOperand {
    Gpr { index: usize, shift: u32, mask: u32 },
    Mm(u8),
    Xmm(u8),
    Imm(u64),
    Mem(String),
}

fn parse_slow_operands(insn: &Insn) -> Option<Vec<SlowOperand>> {
    if insn.operands.is_empty() {
        return Some(Vec::new());
    }
    insn.operands.split(',').map(parse_slow_operand).collect()
}

fn parse_slow_operand(text: &str) -> Option<SlowOperand> {
    let mut text = text.trim();
    let lower = text.to_ascii_lowercase();
    for prefix in [
        "extended double ptr ",
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

fn parse_imm(text: &str) -> Option<u64> {
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

fn memory_expr(text: &str, segment: Option<&str>) -> Option<String> {
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

fn gpr_parts(name: &str) -> Option<(usize, u32, u32)> {
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

fn gpr_expr(name: &str) -> Option<String> {
    let (index, shift, mask) = gpr_parts(name)?;
    Some(if shift == 0 && mask == 0xffff_ffff {
        format!("c->r[{index}]")
    } else {
        format!("((c->r[{index}] >> {shift}) & 0x{mask:x}u)")
    })
}

fn port_expr(operand: &SlowOperand) -> Option<String> {
    match operand {
        SlowOperand::Imm(value) => Some(format!("0x{:x}u", value & 0xffff)),
        SlowOperand::Gpr { index, shift, .. } => Some(format!(
            "((c->r[{index}] >> {shift}) & 0xffffu)"
        )),
        _ => None,
    }
}

fn gpr_size(mask: u32) -> usize {
    if mask == 0xff {
        1
    } else if mask == 0xffff {
        2
    } else {
        4
    }
}

fn write_gpr(index: usize, shift: u32, mask: u32, expr: &str) -> String {
    if shift == 0 && mask == 0xffff_ffff {
        format!("c->r[{index}] = (uint32_t)({expr});")
    } else {
        format!(
            "c->r[{index}] = (c->r[{index}] & ~(0x{mask:x}u << {shift})) | (((uint32_t)({expr}) & 0x{mask:x}u) << {shift});"
        )
    }
}

fn mmx_source(operand: &SlowOperand, width: u32) -> Option<String> {
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

fn sse_logic(mnemonic: &str) -> Option<&'static str> {
    Some(match mnemonic {
        "ORPS" | "ORPD" => "|",
        "XORPS" | "XORPD" => "^",
        "ANDPS" | "ANDPD" => "&",
        "ANDNPS" | "ANDNPD" => "& ~",
        _ => return None,
    })
}

fn mmx_shift(mnemonic: &str) -> Option<(&'static str, u32)> {
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

fn mmx_binary(mnemonic: &str) -> Option<(&'static str, u32)> {
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

struct ValueNames<'b, 'a>(&'b Emitter<'a>);

impl rk_x87::External for ValueNames<'_, '_> {
    fn value(&self, id: ValueId) -> String {
        self.0.expr(id).unwrap_or_else(|_| format!("v{id}"))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Location {
    Gpr(usize),
    Flag(Field),
    Eip,
    Fs,
    Df,
}

fn x87_reads_flags(line: &str) -> bool {
    ["c->eflags_cf", "c->eflags_pf", "c->eflags_af", "c->eflags_zf", "c->eflags_sf", "c->eflags_of"]
        .iter()
        .any(|field| line.contains(field))
}

fn cc_mask(fields: Fields) -> u8 {
    let mut mask = 0u8;
    for (field, bit) in [
        (Field::Cf, 0),
        (Field::Pf, 1),
        (Field::Af, 2),
        (Field::Zf, 3),
        (Field::Sf, 4),
        (Field::Of, 5),
    ] {
        if fields.contains(field) {
            mask |= 1u8 << bit;
        }
    }
    mask
}

fn location(key: StateKey) -> Option<Location> {
    if let Some((field, _)) = field_lane(key) {
        if (field as usize) < 8 {
            return Some(Location::Gpr(field as usize));
        }
        return Some(Location::Flag(field));
    }
    let StateKey::Register(offset) = key else { return None };
    let (name, _) = Registers::get().covering(offset as u64)?;
    match name {
        "EIP" | "IP" => Some(Location::Eip),
        "FS_OFFSET" => Some(Location::Fs),
        "DF" => Some(Location::Df),
        _ => None,
    }
}

fn private_loads(private: Option<&PrivateEmit>, key: StateKey) -> bool {
    let Some(private) = private else { return true };
    match field_lane(key) {
        Some((field, _)) => private.inputs.contains(field) || private.input_argument == Some(field),
        None => true,
    }
}

fn select_mask(masks: &BTreeSet<Fields>, wanted: Fields) -> Option<Fields> {
    masks
        .iter()
        .copied()
        .filter(|mask| wanted & !*mask == Fields::NONE)
        .min_by_key(|mask| (mask.0.count_ones(), *mask))
}

fn ends_block(op: Op) -> bool {
    matches!(
        op,
        Op::Branch { .. }
            | Op::CBranch { .. }
            | Op::BranchInd
            | Op::Return
            | Op::Tail { .. }
            | Op::ImportTail { .. }
            | Op::Trap(_)
            | Op::Breakpoint
            | Op::CallOther(_)
    )
}

fn ctype(size: u8) -> &'static str {
    match size {
        1 => "uint8_t",
        2 => "uint16_t",
        4 => "uint32_t",
        _ => "uint64_t",
    }
}

fn c_mask(size: u8) -> String {
    if size >= 8 {
        "0xffffffffffffffffull".to_string()
    } else {
        format!("0x{:x}u", rk_core::mask(size as u32))
    }
}

fn signed_type(size: u8) -> &'static str {
    match size {
        1 => "int8_t",
        2 => "int16_t",
        4 => "int32_t",
        _ => "int64_t",
    }
}
