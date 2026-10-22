use std::collections::BTreeMap;

use rk_core::ir::{FunctionIr, OpCode};
use rk_core::program::Program;
use rk_core::{Addr, Error};
use rk_ir::Op;
use rk_ssa::{Ssa, ValueId};

use super::{address_form, prepare_ssa, whole};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Base {
    Value(ValueId),
    Const(Addr),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Ref {
    pub base: Base,
    pub offset: i32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum ShapeKind {
    Call,
    Jump,
}

impl ShapeKind {
    pub fn name(self) -> &'static str {
        match self {
            Self::Call => "call",
            Self::Jump => "jump",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum ShapeForm {
    Vptr { receiver: Ref, vptr: ValueId },
    Table { table: Addr },
    Target { target: Addr },
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Shape {
    pub site: Addr,
    pub kind: ShapeKind,
    pub slot: i32,
    pub form: ShapeForm,
}

#[derive(Clone, Debug)]
pub struct ConstantStore {
    pub site: Addr,
    pub base: Option<Ref>,
    pub value: Addr,
}

#[derive(Clone, Debug, Default)]
pub struct FunctionSummary {
    pub cleanup: Option<i32>,
    pub constants: Vec<ConstantStore>,
    pub sites: Vec<Shape>,
    pub requirements: BTreeMap<Addr, Vec<Addr>>,
    pub shape_rejections: BTreeMap<String, usize>,
    pub site_block: Option<String>,
    pub lifted: bool,
}

impl FunctionSummary {
    pub fn blocked(&self) -> Option<&str> {
        self.site_block.as_deref()
    }
}

pub fn summarize(
    program: &Program,
    _addr: Addr,
    result: &std::result::Result<FunctionIr, Error>,
    contracts: &dyn rk_ssa::CallFacts,
) -> FunctionSummary {
    let (fir, ssa) = match prepare_ssa(program, result, contracts) {
        Ok(value) => value,
        Err(error) => {
            let mut summary = FunctionSummary {
                site_block: Some(error),
                ..FunctionSummary::default()
            };
            if let Ok(fir) = result.as_ref() {
                summary.constants = raw_constant_stores(fir);
            }
            return summary;
        }
    };
    let mut summary = FunctionSummary {
        site_block: site_block(&fir),
        lifted: true,
        ..FunctionSummary::default()
    };
    let mut seen_sites = std::collections::BTreeSet::new();
    let mut locations = BTreeMap::new();
    for (block_index, block) in ssa.blocks.iter().enumerate() {
        for (position, &id) in block.ops.iter().enumerate() {
            let value = ssa.value(id);
            match value.op {
                Op::P(OpCode::Store) => {
                    if let Some(store) = constant_store(&ssa, id) {
                        summary.constants.push(store);
                    }
                }
                Op::CallInd | Op::BranchInd => {
                    let site = ssa.value_addr[id as usize];
                    let kind = if value.op == Op::CallInd {
                        ShapeKind::Call
                    } else {
                        ShapeKind::Jump
                    };
                    if !seen_sites.insert((site, kind)) {
                        continue;
                    }
                    if kind == ShapeKind::Jump && !tail_jump(&fir, site) {
                        *summary
                            .shape_rejections
                            .entry("indirect jump is not a straight-line tail JMP".into())
                            .or_default() += 1;
                        continue;
                    }
                    let args = ssa.args(id);
                    let target = if kind == ShapeKind::Call {
                        args.get(1).copied()
                    } else {
                        args.first().copied()
                    };
                    match target.and_then(|target| shape_form(&ssa, target)) {
                        Some((slot, form)) => {
                            locations.insert(site, (block_index, position));
                            let shape = Shape {
                                site,
                                kind,
                                slot,
                                form,
                            };
                            summary.sites.push(shape);
                        }
                        None => {
                            *summary
                                .shape_rejections
                                .entry(shape_reason(&ssa, target).into())
                                .or_default() += 1;
                        }
                    }
                }
                _ => {}
            }
        }
    }
    for site in &summary.sites {
        if !matches!(site.form, ShapeForm::Vptr { .. }) {
            continue;
        }
        let (block, position) = locations[&site.site];
        let requirements = summary
            .sites
            .iter()
            .filter(|previous| {
                if previous.site == site.site || previous.form != site.form {
                    return false;
                }
                let (prior_block, prior_position) = locations[&previous.site];
                if prior_block == block {
                    prior_position < position
                } else {
                    dominates(&ssa, prior_block, block)
                }
            })
            .map(|previous| previous.site)
            .collect();
        summary.requirements.insert(site.site, requirements);
    }
    summary.sites.sort_by_key(|site| (site.site, site.kind));
    summary.sites.dedup_by_key(|site| (site.site, site.kind));
    summary
}

fn dominates(ssa: &Ssa, earlier: usize, later: usize) -> bool {
    let mut seen = std::collections::BTreeSet::new();
    let mut pending = vec![later];
    let mut reached = false;
    while let Some(block) = pending.pop() {
        if block == earlier {
            reached = true;
            continue;
        }
        if !seen.insert(block) {
            continue;
        }
        let Some(block) = ssa.blocks.get(block) else {
            return false;
        };
        if block.preds.is_empty() {
            return false;
        }
        pending.extend(block.preds.iter().map(|&pred| pred as usize));
    }
    reached
}

fn raw_constant_stores(fir: &FunctionIr) -> Vec<ConstantStore> {
    let mut out = Vec::new();
    for insn in &fir.insns {
        for op in &insn.ops {
            if op.opcode != OpCode::Store || op.inputs.len() < 2 {
                continue;
            }
            let stored = op.inputs[op.inputs.len() - 1];
            if stored.space != rk_core::ir::Space::Const || stored.size != 4 {
                continue;
            }
            let address = op.inputs[0];
            let base = (address.space == rk_core::ir::Space::Ram).then(|| Ref {
                base: Base::Const(address.offset as Addr),
                offset: 0,
            });
            out.push(ConstantStore {
                site: insn.addr,
                base,
                value: stored.offset as u32 as Addr,
            });
        }
    }
    out
}

fn constant_store(ssa: &Ssa, id: ValueId) -> Option<ConstantStore> {
    let args = ssa.args(id);
    if args.len() < 3 {
        return None;
    }
    let (_, stored) = whole(ssa, args[2]);
    let Op::Const(data) = stored.op else {
        return None;
    };
    let base = address_form(ssa, args[1])?;
    Some(ConstantStore {
        site: ssa.value_addr[id as usize],
        base: Some(base),
        value: data as u32 as Addr,
    })
}

fn shape_form(ssa: &Ssa, target: ValueId) -> Option<(i32, ShapeForm)> {
    let (id, value) = whole(ssa, target);
    if let Op::Const(data) = value.op {
        return Some((
            0,
            ShapeForm::Target {
                target: data as u32 as Addr,
            },
        ));
    }
    if value.op != Op::P(OpCode::Load) || value.size != 4 {
        return None;
    }
    let args = ssa.args(id);
    let address = *args.get(1)?;
    let anchor = address_form(ssa, address)?;
    let slot = anchor.offset;
    if slot < 0 || slot % 4 != 0 {
        return None;
    }
    match anchor.base {
        Base::Const(table) => Some((slot, ShapeForm::Table { table })),
        Base::Value(root) => {
            let (root, value) = whole(ssa, root);
            if value.op != Op::P(OpCode::Load) || value.size != 4 {
                return None;
            }
            let load_args = ssa.args(root);
            let receiver = address_form(ssa, *load_args.get(1)?)?;
            Some((
                slot,
                ShapeForm::Vptr {
                    receiver,
                    vptr: root,
                },
            ))
        }
    }
}

fn shape_reason(ssa: &Ssa, target: Option<ValueId>) -> &'static str {
    let Some(target) = target else {
        return "indirect target is missing";
    };
    let (id, value) = whole(ssa, target);
    if value.op != Op::P(OpCode::Load) || value.size != 4 {
        return "indirect target is not a 32-bit load or constant";
    }
    let args = ssa.args(id);
    let Some(address) = args.get(1).copied() else {
        return "indirect target load has no address";
    };
    let Some(form) = address_form(ssa, address) else {
        return "indirect target address is not a base plus constant";
    };
    let Base::Value(root) = form.base else {
        return "vtable slot is not offset from a constant table";
    };
    let (_, value) = whole(ssa, root);
    if value.op != Op::P(OpCode::Load) || value.size != 4 {
        "vtable pointer is not a 32-bit object-field load"
    } else {
        "vtable field address is not an object base plus constant"
    }
}

fn tail_jump(fir: &FunctionIr, site: Addr) -> bool {
    let Some(index) = fir.index_of(site) else {
        return false;
    };
    fir.insns[index as usize]
        .mnemonic
        .eq_ignore_ascii_case("JMP")
        && fir.succ[index as usize].is_empty()
        && fir.exits.contains_key(&index)
}

fn site_block(fir: &FunctionIr) -> Option<String> {
    if !fir.entries.is_empty() {
        return Some("multiple entries".into());
    }
    if !fir.seh.is_empty() {
        return Some("exception flow".into());
    }
    if !fir.dynamic.is_empty() {
        return Some("dynamic control flow".into());
    }
    None
}
