use std::collections::BTreeMap;

use rk_core::ir::{OpCode, Space};
use rk_core::{Addr, Lookup};
use rk_frontend::Analysis;

#[derive(Clone, Debug)]
pub struct CallGraph {
    pub nodes: Vec<Addr>,
    pub succ: Vec<Vec<u32>>,
    pub components: Vec<Vec<u32>>,
    pub levels: Vec<Vec<u32>>,
}

impl CallGraph {
    pub fn build(
        analysis: &Analysis,
        indirect: &BTreeMap<Addr, Vec<Addr>>,
        indirect_jumps: &BTreeMap<Addr, Vec<Addr>>,
        include: impl Fn(Addr) -> bool,
    ) -> Self {
        let nodes: Vec<Addr> = analysis
            .ir
            .keys()
            .copied()
            .filter(|&addr| include(addr))
            .collect();
        let index: Lookup<Addr, u32> = nodes
            .iter()
            .enumerate()
            .map(|(n, &addr)| (addr, n as u32))
            .collect();
        let mut succ = vec![Vec::new(); nodes.len()];
        for (n, &addr) in nodes.iter().enumerate() {
            let Some(Ok(fir)) = analysis.ir.get(&addr) else {
                continue;
            };
            for ins in &fir.insns {
                for op in &ins.ops {
                    if op.opcode == OpCode::Call {
                        let Some(target) = op.inputs.first().and_then(|v| match v.space {
                            Space::Const | Space::Ram => Some(v.offset as Addr),
                            _ => None,
                        }) else {
                            continue;
                        };
                        if let Some(&target) = index.get(&target) {
                            succ[n].push(target);
                        }
                    } else if op.opcode == OpCode::CallInd {
                        if let Some(targets) = indirect.get(&ins.addr) {
                            for &target in targets {
                                if let Some(&target) = index.get(&target) {
                                    succ[n].push(target);
                                }
                            }
                        }
                    }
                }
                if let Some(targets) = indirect_jumps.get(&ins.addr) {
                    for &target in targets {
                        if let Some(&target) = index.get(&target) {
                            succ[n].push(target);
                        }
                    }
                }
            }
            for targets in fir.exits.values() {
                for &target in targets {
                    if let Some(&target) = index.get(&target) {
                        succ[n].push(target);
                    }
                }
            }
            succ[n].sort_unstable();
            succ[n].dedup();
        }
        Self::from_succ(nodes, succ)
    }

    pub fn from_succ(nodes: Vec<Addr>, succ: Vec<Vec<u32>>) -> Self {
        let components = tarjan(&succ);
        let mut component_of = Lookup::with_capacity(nodes.len());
        for (component, members) in components.iter().enumerate() {
            for &member in members {
                component_of.insert(nodes[member as usize], component as u32);
            }
        }
        let mut level = vec![0usize; components.len()];
        for (component, members) in components.iter().enumerate() {
            for &member in members {
                for &target in &succ[member as usize] {
                    let other =
                        component_of.get(&nodes[target as usize]).copied().unwrap() as usize;
                    if other != component {
                        level[component] = level[component].max(level[other] + 1);
                    }
                }
            }
        }
        let mut by_level: BTreeMap<usize, Vec<u32>> = BTreeMap::new();
        for (component, &l) in level.iter().enumerate() {
            by_level.entry(l).or_default().push(component as u32);
        }
        Self {
            nodes,
            succ,
            components,
            levels: by_level.into_values().collect(),
        }
    }
}

fn tarjan(succ: &[Vec<u32>]) -> Vec<Vec<u32>> {
    let n = succ.len();
    let mut index = vec![u32::MAX; n];
    let mut low = vec![0u32; n];
    let mut on_stack = vec![false; n];
    let mut stack = Vec::new();
    let mut out = Vec::new();
    let mut next = 0u32;
    for root in 0..n {
        if index[root] != u32::MAX {
            continue;
        }
        index[root] = next;
        low[root] = next;
        next += 1;
        stack.push(root as u32);
        on_stack[root] = true;
        let mut work: Vec<(u32, usize)> = vec![(root as u32, 0)];
        while let Some(&mut (node, ref mut child)) = work.last_mut() {
            if *child < succ[node as usize].len() {
                let target = succ[node as usize][*child];
                *child += 1;
                if index[target as usize] == u32::MAX {
                    index[target as usize] = next;
                    low[target as usize] = next;
                    next += 1;
                    stack.push(target);
                    on_stack[target as usize] = true;
                    work.push((target, 0));
                } else if on_stack[target as usize] {
                    low[node as usize] = low[node as usize].min(index[target as usize]);
                }
                continue;
            }
            work.pop();
            if let Some(&mut (parent, _)) = work.last_mut() {
                low[parent as usize] = low[parent as usize].min(low[node as usize]);
            }
            if low[node as usize] == index[node as usize] {
                let mut component = Vec::new();
                loop {
                    let member = stack.pop().unwrap();
                    on_stack[member as usize] = false;
                    component.push(member);
                    if member == node {
                        break;
                    }
                }
                component.sort_unstable();
                out.push(component);
            }
        }
    }
    out
}
