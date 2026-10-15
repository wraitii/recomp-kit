use crate::RenderError;

pub(crate) const FOURCC_FXLC: u32 = 0x434c_5846;
pub(crate) const FOURCC_CLIT: u32 = 0x5449_4c43;

const OP_MASK: u32 = 0x7ff0_0000;
const OP_SHIFT: u32 = 20;
const SCALAR_FLAG: u32 = 0x8000_0000;
const NCOMP_MASK: u32 = 0x0000_ffff;

const TABLE_COUNT: usize = 8;
const TABLE_IMMED: u32 = 1;
const TABLE_CONST: u32 = 2;
const TABLE_INPUT: u32 = 3;
const TABLE_OCONST: u32 = 4;
const TABLE_OBCONST: u32 = 5;
const TABLE_OICONST: u32 = 6;
const TABLE_TEMP: u32 = 7;

const MAX_INPUTS: usize = 3;
const MAX_TABLE_COMPONENTS: u32 = 4096 * 4;

#[derive(Clone, Copy, Debug)]
pub(crate) struct Operand {
    pub table: u32,
    pub offset: u32,
    pub index: Option<(u32, u32)>,
}

struct Ins {
    op: u32,
    scalar: bool,
    count: u32,
    inputs: Vec<Operand>,
    output: Operand,
}

pub(crate) struct Preshader {
    ins: Vec<Ins>,
    literals: Vec<f64>,
    components: [u32; TABLE_COUNT],
}

fn invalid_data(message: impl Into<String>) -> RenderError {
    RenderError::invalid_data("D3DXEffect", message)
}

fn unsupported(message: impl Into<String>) -> RenderError {
    RenderError::new("D3DXEffect", message)
}

fn rd_u32(data: &[u8], offset: usize) -> Result<u32, RenderError> {
    let end = offset
        .checked_add(4)
        .ok_or_else(|| invalid_data("preshader offset overflow"))?;
    if end > data.len() {
        return Err(invalid_data("preshader read past end"));
    }
    Ok(u32::from_le_bytes([
        data[offset],
        data[offset + 1],
        data[offset + 2],
        data[offset + 3],
    ]))
}

fn comment_blocks(code: &[u8], start: usize) -> Result<Vec<(usize, usize)>, RenderError> {
    if code.len() % 4 != 0 {
        return Err(invalid_data("preshader is not DWORD aligned"));
    }
    let mut out = Vec::new();
    let mut p = start;
    while p + 4 <= code.len() {
        let tok = rd_u32(code, p)?;
        if (tok & 0xffff) != 0xfffe {
            break;
        }
        let words = ((tok >> 16) & 0x7fff) as usize;
        let end = p + 4 + 4 * words;
        if end > code.len() {
            return Err(invalid_data("preshader comment overruns blob"));
        }
        out.push((p, words * 4));
        p = end;
    }
    Ok(out)
}

fn find_block(code: &[u8], fourcc: u32) -> Result<Option<(usize, usize)>, RenderError> {
    for (offset, size) in comment_blocks(code, 4)? {
        if rd_u32(code, offset + 4)? == fourcc {
            return Ok(Some((offset + 8, size.saturating_sub(4))));
        }
    }
    Ok(None)
}

pub(crate) fn shader_preshader(code: &[u8]) -> Result<Option<&[u8]>, RenderError> {
    let Some((start, size)) = find_block(code, 0x5345_5250)? else {
        return Ok(None);
    };
    if size == 0 {
        return Ok(None);
    }
    Ok(Some(&code[start..start + size]))
}

fn read_literals(code: &[u8]) -> Result<Vec<f64>, RenderError> {
    let Some((start, size)) = find_block(code, FOURCC_CLIT)? else {
        return Ok(Vec::new());
    };
    let count = rd_u32(code, start)? as usize;
    let available = size.saturating_sub(4) / 8;
    if count > available {
        return Err(invalid_data("CLIT literal count exceeds block"));
    }
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let at = start + 4 + i * 8;
        let mut bytes = [0u8; 8];
        bytes.copy_from_slice(&code[at..at + 8]);
        out.push(f64::from_le_bytes(bytes));
    }
    Ok(out)
}

fn parse_reg(word: u32) -> Result<u32, RenderError> {
    match word {
        1..=7 => Ok(word),
        _ => Err(unsupported(format!(
            "preshader register table {word} is unsupported"
        ))),
    }
}

fn parse_operand(words: &[u32], at: &mut usize) -> Result<Operand, RenderError> {
    let relative = *words
        .get(*at)
        .ok_or_else(|| invalid_data("preshader operand truncated"))?;
    *at += 1;
    let index = if relative != 0 {
        if relative != 1 {
            return Err(unsupported(format!(
                "preshader relative addressing flag {relative} is unsupported"
            )));
        }
        let table = parse_reg(
            *words
                .get(*at)
                .ok_or_else(|| invalid_data("preshader index register truncated"))?,
        )?;
        *at += 1;
        let offset = *words
            .get(*at)
            .ok_or_else(|| invalid_data("preshader index offset truncated"))?;
        *at += 1;
        Some((table, offset))
    } else {
        None
    };
    let table = parse_reg(
        *words
            .get(*at)
            .ok_or_else(|| invalid_data("preshader register truncated"))?,
    )?;
    *at += 1;
    let offset = *words
        .get(*at)
        .ok_or_else(|| invalid_data("preshader offset truncated"))?;
    *at += 1;
    Ok(Operand {
        table,
        offset,
        index,
    })
}

fn op_inputs(op: u32) -> Option<u32> {
    Some(match op {
        0x100 | 0x101 | 0x103 | 0x104 | 0x105 | 0x106 | 0x107 => 1,
        0x200 | 0x201 | 0x202 | 0x203 | 0x204 | 0x205 | 0x208 | 0x500 => 2,
        0x300 | 0x301 => 3,
        _ => return None,
    })
}

fn bump(components: &mut [u32; TABLE_COUNT], table: u32, end: u32) -> Result<(), RenderError> {
    if end > MAX_TABLE_COMPONENTS {
        return Err(invalid_data(
            "preshader register span exceeds the allocation bound",
        ));
    }
    if table == TABLE_OBCONST || table == TABLE_OICONST {
        return Err(unsupported(
            "preshader boolean and integer register tables are not implemented",
        ));
    }
    let slot = components
        .get_mut(table as usize)
        .ok_or_else(|| unsupported(format!("preshader table {table} is unsupported")))?;
    *slot = (*slot).max(end);
    Ok(())
}

pub(crate) fn parse(code: &[u8]) -> Result<Preshader, RenderError> {
    if code.len() < 4 {
        return Err(invalid_data("preshader shorter than a version token"));
    }
    let version = rd_u32(code, 0)?;
    if version >> 16 != 0x4658 || (version >> 8) & 0xff != 2 {
        return Err(unsupported(format!(
            "preshader version {version:#010x} is unsupported"
        )));
    }
    let Some((start, size)) = find_block(code, FOURCC_FXLC)? else {
        return Err(unsupported("preshader has no FXLC block"));
    };
    let literals = read_literals(code)?;
    let count = rd_u32(code, start)? as usize;
    if count > 4096 {
        return Err(invalid_data("preshader instruction count is unreasonable"));
    }
    let payload = size.saturating_sub(4) / 4;
    let mut words = Vec::with_capacity(payload);
    for i in 0..payload {
        words.push(rd_u32(code, start + 4 + i * 4)?);
    }
    let mut at = 0usize;
    let mut ins = Vec::with_capacity(count);
    let mut components = [0u32; TABLE_COUNT];
    for _ in 0..count {
        let raw = *words
            .get(at)
            .ok_or_else(|| invalid_data("preshader instruction truncated"))?;
        at += 1;
        let op = (raw & OP_MASK) >> OP_SHIFT;
        let ncomp = raw & NCOMP_MASK;
        if !(1..=4).contains(&ncomp) {
            return Err(invalid_data("preshader component count is out of range"));
        }
        let nin = *words
            .get(at)
            .ok_or_else(|| invalid_data("preshader input count truncated"))?;
        at += 1;
        if nin as usize > MAX_INPUTS {
            return Err(unsupported(format!(
                "preshader input count {nin} is unsupported"
            )));
        }
        let expected = op_inputs(op)
            .ok_or_else(|| unsupported(format!("preshader opcode {op:#05x} is unsupported")))?;
        if nin != expected {
            return Err(invalid_data(format!(
                "preshader opcode {op:#05x} input count {nin} is invalid"
            )));
        }
        let scalar = raw & SCALAR_FLAG != 0;
        let mut inputs = Vec::with_capacity(nin as usize);
        for slot in 0..nin {
            let operand = parse_operand(&words, &mut at)?;
            if operand.index.is_some() {
                return Err(unsupported(
                    "preshader relative addressing is not implemented",
                ));
            }
            let lane = if scalar && slot == 0 { 0 } else { ncomp - 1 };
            let end = operand
                .offset
                .checked_add(lane)
                .and_then(|v| v.checked_add(1))
                .ok_or_else(|| invalid_data("preshader operand span overflow"))?;
            bump(&mut components, operand.table, end)?;
            inputs.push(operand);
        }
        let output = parse_operand(&words, &mut at)?;
        if output.index.is_some() {
            return Err(unsupported(
                "preshader relative output addressing is not implemented",
            ));
        }
        let end = output
            .offset
            .checked_add(if op == 0x500 { 1 } else { ncomp })
            .ok_or_else(|| invalid_data("preshader output span overflow"))?;
        bump(&mut components, output.table, end)?;
        ins.push(Ins {
            op,
            scalar,
            count: ncomp,
            inputs,
            output,
        });
    }
    Ok(Preshader {
        ins,
        literals,
        components,
    })
}

impl Preshader {
    pub(crate) fn output_registers(&self) -> Vec<u32> {
        let mut registers = std::collections::BTreeSet::new();
        for ins in &self.ins {
            if ins.output.table == TABLE_OCONST {
                let count = if ins.op == 0x500 { 1 } else { ins.count };
                for component in 0..count {
                    registers.insert((ins.output.offset + component) / 4);
                }
            }
        }
        registers.into_iter().collect()
    }
    fn registers(&self, table: u32) -> usize {
        let components = self.components[table as usize];
        ((components as usize) + 3) / 4
    }
}

fn read_component(
    preshader: &Preshader,
    inputs: &[[f32; 4]],
    output: &[[f64; 4]],
    temp: &[[f64; 4]],
    operand: &Operand,
    component: u32,
) -> Result<f64, RenderError> {
    if operand.index.is_some() {
        return Err(unsupported(
            "preshader relative addressing is not implemented",
        ));
    }
    let offset = operand
        .offset
        .checked_add(component)
        .ok_or_else(|| invalid_data("preshader operand offset overflow"))?;
    match operand.table {
        TABLE_IMMED => preshader
            .literals
            .get(offset as usize)
            .copied()
            .ok_or_else(|| invalid_data("preshader literal read is out of bounds")),
        TABLE_CONST | TABLE_INPUT => {
            let register = (offset / 4) as usize;
            let lane = (offset % 4) as usize;
            inputs
                .get(register)
                .map(|row| row[lane] as f64)
                .ok_or_else(|| invalid_data("preshader input register read is out of bounds"))
        }
        TABLE_OCONST => {
            let register = (offset / 4) as usize;
            let lane = (offset % 4) as usize;
            output
                .get(register)
                .map(|row| row[lane])
                .ok_or_else(|| invalid_data("preshader output register read is out of bounds"))
        }
        TABLE_TEMP => {
            let register = (offset / 4) as usize;
            let lane = (offset % 4) as usize;
            temp.get(register)
                .map(|row| row[lane])
                .ok_or_else(|| invalid_data("preshader temporary read is out of bounds"))
        }
        TABLE_OBCONST | TABLE_OICONST => Err(unsupported(
            "preshader boolean and integer register tables are not implemented",
        )),
        _ => Err(unsupported(format!(
            "preshader table {} is unsupported",
            operand.table
        ))),
    }
}

fn write_component(
    output: &mut [[f64; 4]],
    temp: &mut [[f64; 4]],
    operand: &Operand,
    component: u32,
    value: f64,
) -> Result<(), RenderError> {
    let value = value as f32 as f64;
    if operand.index.is_some() {
        return Err(unsupported(
            "preshader relative addressing is not implemented",
        ));
    }
    let offset = operand
        .offset
        .checked_add(component)
        .ok_or_else(|| invalid_data("preshader output offset overflow"))?;
    let register = (offset / 4) as usize;
    let lane = (offset % 4) as usize;
    match operand.table {
        TABLE_OCONST => output
            .get_mut(register)
            .map(|row| row[lane] = value)
            .ok_or_else(|| invalid_data("preshader output register write is out of bounds")),
        TABLE_TEMP => temp
            .get_mut(register)
            .map(|row| row[lane] = value)
            .ok_or_else(|| invalid_data("preshader temporary write is out of bounds")),
        TABLE_OBCONST | TABLE_OICONST => Err(unsupported(
            "preshader boolean and integer register tables are not implemented",
        )),
        _ => Err(unsupported(format!(
            "preshader output table {} is unsupported",
            operand.table
        ))),
    }
}

fn eval_op(op: u32, args: &[f64; MAX_INPUTS]) -> Result<f64, RenderError> {
    Ok(match op {
        0x100 => args[0],
        0x101 => -args[0],
        0x103 => 1.0 / args[0],
        0x104 => args[0] - args[0].floor(),
        0x105 => 2f64.powf(args[0]),
        0x106 => {
            let v = args[0].abs();
            if v == 0.0 { 0.0 } else { v.log2() }
        }
        0x107 => {
            let v = args[0].abs();
            if v == 0.0 {
                f64::INFINITY
            } else {
                1.0 / v.sqrt()
            }
        }
        0x200 => args[0].min(args[1]),
        0x201 => args[0].max(args[1]),
        0x202 => {
            if args[0] < args[1] {
                1.0
            } else {
                0.0
            }
        }
        0x203 => {
            if args[0] >= args[1] {
                1.0
            } else {
                0.0
            }
        }
        0x204 => args[0] + args[1],
        0x205 => args[0] * args[1],
        0x208 => args[0] / args[1],
        0x300 => {
            if args[0] >= 0.0 {
                args[1]
            } else {
                args[2]
            }
        }
        0x301 => {
            if args[0] != 0.0 {
                args[1]
            } else {
                args[2]
            }
        }
        _ => {
            return Err(unsupported(format!(
                "preshader opcode {op:#05x} is unsupported"
            )));
        }
    })
}

pub(crate) fn evaluate(
    preshader: &Preshader,
    inputs: &[[f32; 4]],
) -> Result<Vec<f32>, RenderError> {
    let mut temp = vec![[0.0f64; 4]; preshader.registers(TABLE_TEMP)];
    let mut output = vec![[0.0f64; 4]; preshader.registers(TABLE_OCONST)];
    for ins in &preshader.ins {
        if ins.op == 0x500 {
            let mut sum = 0.0;
            for component in 0..ins.count {
                let left = read_component(
                    preshader,
                    inputs,
                    &output,
                    &temp,
                    &ins.inputs[0],
                    if ins.scalar { 0 } else { component },
                )?;
                let right =
                    read_component(preshader, inputs, &output, &temp, &ins.inputs[1], component)?;
                sum += left * right;
            }
            write_component(&mut output, &mut temp, &ins.output, 0, sum)?;
            continue;
        }
        let mut results = Vec::with_capacity(ins.count as usize);
        for component in 0..ins.count {
            let mut args = [0.0f64; MAX_INPUTS];
            for (slot, operand) in ins.inputs.iter().enumerate() {
                let lane = if ins.scalar && slot == 0 {
                    0
                } else {
                    component
                };
                args[slot] = read_component(preshader, inputs, &output, &temp, operand, lane)?;
            }
            results.push(eval_op(ins.op, &args)?);
        }
        for (component, value) in results.into_iter().enumerate() {
            write_component(&mut output, &mut temp, &ins.output, component as u32, value)?;
        }
    }
    let mut flat = Vec::with_capacity(output.len() * 4);
    for row in &output {
        for value in row {
            flat.push(*value as f32);
        }
    }
    Ok(flat)
}
