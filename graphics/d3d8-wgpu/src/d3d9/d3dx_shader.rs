use crate::RenderError;
use crate::bridge::{
    D3dxConstantDesc, D3dxConstantDescResult, D3dxConstantTableDesc, D3dxConstantTableResult,
    D3dxProfilesResult, D3dxSemantic, D3dxSemanticsResult, Status, StatusCode, status,
};
use crate::d3d9::D3d9Device;
use crate::d3d9::shader::shader_word_count;
use crate::d3d9::state::{PS_CONST_B, PS_CONST_F, PS_CONST_I, VS_CONST_B, VS_CONST_F, VS_CONST_I};

const D3DXPC_SCALAR: u32 = 0;
const D3DXPC_VECTOR: u32 = 1;
const D3DXPC_MATRIX_ROWS: u32 = 2;
const D3DXPC_MATRIX_COLUMNS: u32 = 3;
const D3DXPC_OBJECT: u32 = 4;
const D3DXPC_STRUCT: u32 = 5;

const D3DXPT_VOID: u32 = 0;
const D3DXPT_BOOL: u32 = 1;
const D3DXPT_INT: u32 = 2;
const D3DXPT_FLOAT: u32 = 3;
const D3DXPT_UNSUPPORTED: u32 = 19;

const D3DXRS_BOOL: u32 = 0;
const D3DXRS_INT4: u32 = 1;
const D3DXRS_FLOAT4: u32 = 2;
const D3DXRS_SAMPLER: u32 = 3;

pub const D3DX_SET_VALUE: u32 = 0;
pub const D3DX_SET_BOOL: u32 = 1;
pub const D3DX_SET_BOOL_ARRAY: u32 = 2;
pub const D3DX_SET_INT: u32 = 3;
pub const D3DX_SET_INT_ARRAY: u32 = 4;
pub const D3DX_SET_FLOAT: u32 = 5;
pub const D3DX_SET_FLOAT_ARRAY: u32 = 6;
pub const D3DX_SET_VECTOR: u32 = 7;
pub const D3DX_SET_VECTOR_ARRAY: u32 = 8;
pub const D3DX_SET_MATRIX: u32 = 9;
pub const D3DX_SET_MATRIX_ARRAY: u32 = 10;
pub const D3DX_SET_MATRIX_POINTER_ARRAY: u32 = 11;
pub const D3DX_SET_MATRIX_TRANSPOSE: u32 = 12;
pub const D3DX_SET_MATRIX_TRANSPOSE_ARRAY: u32 = 13;
pub const D3DX_SET_MATRIX_TRANSPOSE_POINTER_ARRAY: u32 = 14;

const CTAB_FOURCC: u32 = 0x4241_5443;
const OP_COMMENT: u32 = 0xfffe;
const OP_END: u32 = 0xffff;
const OP_DCL: u32 = 31;
const OP_DEF: u32 = 81;
const OP_DEFB: u32 = 47;
const OP_DEFI: u32 = 48;
const NO_DEFAULT: u32 = u32::MAX;

const D3DSPR_TEMP: u32 = 0;
const D3DSPR_INPUT: u32 = 1;
const D3DSPR_TEXTURE: u32 = 3;
const D3DSPR_RASTOUT: u32 = 4;
const D3DSPR_ATTROUT: u32 = 5;
const D3DSPR_OUTPUT: u32 = 6;
const D3DSPR_DEPTHOUT: u32 = 9;
const D3DSPR_SAMPLER: u32 = 10;

const D3DSP_REGTYPE_SHIFT: u32 = 28;
const D3DSP_REGTYPE_SHIFT2: u32 = 8;
const D3DSP_REGTYPE_MASK: u32 = 0x7000_0000;
const D3DSP_REGTYPE_MASK2: u32 = 0x0000_1800;
const D3DSP_REGNUM_MASK: u32 = 0x0000_07ff;
const D3DSI_INSTLENGTH_MASK: u32 = 0x0f00_0000;
const D3DSI_INSTLENGTH_SHIFT: u32 = 24;
const D3DSI_COMMENTSIZE_MASK: u32 = 0x7fff_0000;
const D3DSI_COMMENTSIZE_SHIFT: u32 = 16;

const D3DDECLUSAGE_POSITION: u32 = 0;
const D3DDECLUSAGE_PSIZE: u32 = 4;
const D3DDECLUSAGE_TEXCOORD: u32 = 5;
const D3DDECLUSAGE_COLOR: u32 = 10;
const D3DDECLUSAGE_FOG: u32 = 11;
const D3DDECLUSAGE_DEPTH: u32 = 12;

const MAX_TYPE_DEPTH: u32 = 32;
const MAX_NODE_COUNT: u32 = 4096;

fn invalid(message: impl Into<String>) -> RenderError {
    RenderError::invalid("D3DXConstantTable", message)
}

fn invalid_data(message: impl Into<String>) -> RenderError {
    RenderError::invalid_data("D3DXConstantTable", message)
}

fn unsupported(message: impl Into<String>) -> RenderError {
    RenderError::new("D3DXConstantTable", message)
}

fn failed(error: RenderError) -> Status {
    let code = match error.kind {
        crate::RenderErrorKind::InvalidArgument => StatusCode::InvalidArgument,
        crate::RenderErrorKind::InvalidData => StatusCode::InvalidData,
        crate::RenderErrorKind::OutOfMemory => StatusCode::OutOfMemory,
        crate::RenderErrorKind::Unsupported => StatusCode::Unsupported,
    };
    status(code, error.to_string())
}

fn shader_status(error: RenderError) -> Status {
    if error.kind == crate::RenderErrorKind::Unsupported {
        status(StatusCode::Unsupported, error.to_string())
    } else {
        status(StatusCode::InvalidData, error.to_string())
    }
}

fn empty_table_result(invalid_bytecode: bool) -> D3dxConstantTableResult {
    D3dxConstantTableResult {
        status: status(StatusCode::Ok, ""),
        buffer: Vec::new(),
        desc: D3dxConstantTableDesc {
            creator_offset: 0,
            version: 0,
            constants: 0,
        },
        node_count: 0,
        invalid_bytecode,
    }
}

fn empty_desc() -> D3dxConstantDesc {
    D3dxConstantDesc {
        name_offset: 0,
        register_set: 0,
        register_index: 0,
        register_count: 0,
        class_: 0,
        type_: 0,
        rows: 0,
        columns: 0,
        elements: 0,
        struct_members: 0,
        bytes: 0,
        default_offset: NO_DEFAULT,
    }
}

struct Node {
    name: Vec<u8>,
    name_offset: u32,
    register_set: u32,
    register_index: u32,
    register_count: u32,
    class: u32,
    type_: u32,
    rows: u32,
    columns: u32,
    elements: u32,
    struct_members: u32,
    bytes: u32,
    default_offset: u32,
    children: Vec<u32>,
}

struct ParsedNode {
    name: Vec<u8>,
    name_offset: u32,
    register_set: u32,
    register_index: u32,
    register_count: u32,
    class: u32,
    type_: u32,
    rows: u32,
    columns: u32,
    elements: u32,
    struct_members: u32,
    bytes: u32,
    default_offset: u32,
    default_bytes: u32,
    children: Vec<ParsedNode>,
}

pub struct D3dxConstantTable {
    buffer: Vec<u8>,
    creator_offset: u32,
    version: u32,
    constants: u32,
    nodes: Vec<Node>,
    roots: Vec<u32>,
}

enum Chunk {
    F { start: u32, values: Vec<f32> },
    I { start: u32, values: Vec<i32> },
    B { start: u32, values: Vec<u32> },
}

impl Chunk {
    fn range(&self, pixel: bool) -> (u32, usize, usize) {
        match self {
            Chunk::F { start, values } => (
                *start,
                values.len() / 4,
                if pixel { PS_CONST_F } else { VS_CONST_F },
            ),
            Chunk::I { start, values } => (
                *start,
                values.len() / 4,
                if pixel { PS_CONST_I } else { VS_CONST_I },
            ),
            Chunk::B { start, values } => (
                *start,
                values.len(),
                if pixel { PS_CONST_B } else { VS_CONST_B },
            ),
        }
    }
}

fn rd_u32(data: &[u8], offset: usize) -> Result<u32, RenderError> {
    let bytes = data
        .get(offset..offset + 4)
        .ok_or_else(|| invalid_data("CTAB offset is outside the payload"))?;
    Ok(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
}

fn rd_u16(data: &[u8], offset: usize) -> Result<u16, RenderError> {
    let bytes = data
        .get(offset..offset + 2)
        .ok_or_else(|| invalid_data("CTAB offset is outside the payload"))?;
    Ok(u16::from_le_bytes([bytes[0], bytes[1]]))
}

fn rd_name(data: &[u8], offset: usize) -> Result<Vec<u8>, RenderError> {
    if offset >= data.len() {
        return Err(invalid_data("CTAB string offset is outside the payload"));
    }
    let end = data[offset..]
        .iter()
        .position(|&byte| byte == 0)
        .map(|length| offset + length)
        .ok_or_else(|| invalid_data("CTAB string is not NUL-terminated"))?;
    Ok(data[offset..end].to_vec())
}

fn validate_name(data: &[u8], offset: u32) -> Result<(), RenderError> {
    if offset == 0 {
        return Ok(());
    }
    rd_name(data, offset as usize).map(|_| ())
}

fn clamp_count(max_index: u32, index: u32, size: u32) -> u32 {
    size.min(max_index.saturating_sub(index))
}

fn sum_default_bytes(children: &[ParsedNode]) -> Result<u32, RenderError> {
    let mut total = 0u32;
    for child in children {
        total = total
            .checked_add(child.default_bytes)
            .ok_or_else(|| invalid_data("CTAB default value size overflows"))?;
    }
    Ok(total)
}

#[allow(clippy::too_many_arguments)]
fn parse_type(
    data: &[u8],
    type_offset: u32,
    is_element: bool,
    index: u32,
    max_index: u32,
    default: &mut Option<u32>,
    name_offset: u32,
    register_set: u32,
    depth: u32,
    budget: &mut u32,
) -> Result<ParsedNode, RenderError> {
    if depth > MAX_TYPE_DEPTH {
        return Err(invalid_data("CTAB type nesting is too deep"));
    }
    if *budget == 0 {
        return Err(invalid_data("CTAB constant tree is too large"));
    }
    *budget -= 1;
    if register_set > D3DXRS_SAMPLER {
        return Err(unsupported(
            "constant register set is not in the published enumeration",
        ));
    }
    let base = type_offset as usize;
    let class = rd_u16(data, base)? as u32;
    let ptype = rd_u16(data, base + 2)? as u32;
    let rows = rd_u16(data, base + 4)? as u32;
    let columns = rd_u16(data, base + 6)? as u32;
    let elements = rd_u16(data, base + 8)? as u32;
    let struct_members = rd_u16(data, base + 10)? as u32;
    let struct_member_info = rd_u32(data, base + 12)? as usize;
    if class > D3DXPC_STRUCT {
        return Err(unsupported(
            "constant class is not in the published enumeration",
        ));
    }
    if ptype > D3DXPT_UNSUPPORTED {
        return Err(invalid_data(
            "CTAB constant parameter type is not in the published enumeration",
        ));
    }
    if rows == 0 || columns == 0 || elements == 0 {
        return Err(invalid_data("CTAB constant has an empty shape"));
    }
    if class != D3DXPC_STRUCT && (rows > 4 || columns > 4) {
        return Err(invalid_data(
            "CTAB constant dimensions exceed four components",
        ));
    }
    if class == D3DXPC_STRUCT && struct_members == 0 {
        return Err(invalid_data("CTAB struct constant has no members"));
    }
    if class != D3DXPC_STRUCT
        && register_set != D3DXRS_SAMPLER
        && !matches!(ptype, D3DXPT_VOID | D3DXPT_BOOL | D3DXPT_INT | D3DXPT_FLOAT)
    {
        return Err(unsupported(
            "non-numeric types in numeric register sets are not implemented",
        ));
    }
    let name = rd_name(data, name_offset as usize)?;
    let default_offset = default.unwrap_or(NO_DEFAULT);
    let byte_elements = if is_element { 1 } else { elements };
    let bytes = 4u32
        .checked_mul(byte_elements)
        .and_then(|value| value.checked_mul(rows))
        .and_then(|value| value.checked_mul(columns))
        .ok_or_else(|| invalid_data("CTAB constant byte size overflows"))?;

    let mut children = Vec::new();
    let register_count;
    let default_bytes;
    if elements > 1 && !is_element {
        let mut size = 0u32;
        for _ in 0..elements {
            let child = parse_type(
                data,
                type_offset,
                true,
                index
                    .checked_add(size)
                    .ok_or_else(|| invalid_data("CTAB register index overflows"))?,
                max_index,
                default,
                name_offset,
                register_set,
                depth + 1,
                budget,
            )?;
            size = size
                .checked_add(child.register_count)
                .ok_or_else(|| invalid_data("CTAB register count overflows"))?;
            children.push(child);
        }
        register_count = clamp_count(max_index, index, size);
        default_bytes = sum_default_bytes(&children)?;
    } else if class == D3DXPC_STRUCT {
        let mut size = 0u32;
        for member in 0..struct_members {
            let member_offset = struct_member_info
                .checked_add(member as usize * 8)
                .ok_or_else(|| invalid_data("CTAB struct member offset overflows"))?;
            let member_name = rd_u32(data, member_offset)?;
            let member_type = rd_u32(data, member_offset + 4)?;
            validate_name(data, member_name)?;
            let child = parse_type(
                data,
                member_type,
                false,
                index
                    .checked_add(size)
                    .ok_or_else(|| invalid_data("CTAB register index overflows"))?,
                max_index,
                default,
                member_name,
                register_set,
                depth + 1,
                budget,
            )?;
            size = size
                .checked_add(child.register_count)
                .ok_or_else(|| invalid_data("CTAB register count overflows"))?;
            children.push(child);
        }
        register_count = clamp_count(max_index, index, size);
        default_bytes = sum_default_bytes(&children)?;
    } else {
        let mut offset_diff = columns.checked_mul(rows).unwrap_or(u32::MAX);
        let mut size = columns.checked_mul(rows).unwrap_or(u32::MAX);
        let mut fail = false;
        match register_set {
            D3DXRS_BOOL => {
                fail = !matches!(
                    class,
                    D3DXPC_SCALAR | D3DXPC_VECTOR | D3DXPC_MATRIX_ROWS | D3DXPC_MATRIX_COLUMNS
                );
            }
            D3DXRS_FLOAT4 | D3DXRS_INT4 => match class {
                D3DXPC_VECTOR => {
                    size = 1;
                    offset_diff = rows.saturating_mul(4);
                }
                D3DXPC_SCALAR => {
                    offset_diff = rows.saturating_mul(4);
                }
                D3DXPC_MATRIX_ROWS => {
                    offset_diff = rows.saturating_mul(4);
                    size = rows;
                }
                D3DXPC_MATRIX_COLUMNS => {
                    offset_diff = columns.saturating_mul(4);
                    size = columns;
                }
                _ => fail = true,
            },
            D3DXRS_SAMPLER => {
                size = 1;
                fail = class != D3DXPC_OBJECT;
            }
            _ => fail = true,
        }
        if fail {
            return Err(unsupported(format!(
                "constant class {class} is not valid for register set {register_set}"
            )));
        }
        if let Some(current) = default {
            *current = current
                .checked_add(offset_diff.checked_mul(4).unwrap_or(u32::MAX))
                .ok_or_else(|| invalid_data("CTAB default value offset overflows"))?;
        }
        register_count = clamp_count(max_index, index, size);
        default_bytes = offset_diff
            .checked_mul(4)
            .ok_or_else(|| invalid_data("CTAB default value size overflows"))?;
    }

    if default_offset != NO_DEFAULT {
        let end = (default_offset as usize)
            .checked_add(default_bytes as usize)
            .ok_or_else(|| invalid_data("CTAB default value range overflows"))?;
        if end > data.len() {
            return Err(invalid_data(
                "CTAB default value range is outside the payload",
            ));
        }
    }

    Ok(ParsedNode {
        name,
        name_offset,
        register_set,
        register_index: index,
        register_count,
        class,
        type_: ptype,
        rows,
        columns,
        elements: if is_element { 1 } else { elements },
        struct_members,
        bytes,
        default_offset,
        default_bytes,
        children,
    })
}

fn flatten(node: ParsedNode, nodes: &mut Vec<Node>) -> u32 {
    let index = nodes.len() as u32;
    nodes.push(Node {
        name: node.name,
        name_offset: node.name_offset,
        register_set: node.register_set,
        register_index: node.register_index,
        register_count: node.register_count,
        class: node.class,
        type_: node.type_,
        rows: node.rows,
        columns: node.columns,
        elements: node.elements,
        struct_members: node.struct_members,
        bytes: node.bytes,
        default_offset: node.default_offset,
        children: Vec::new(),
    });
    let children: Vec<u32> = node
        .children
        .into_iter()
        .map(|child| flatten(child, nodes))
        .collect();
    nodes[index as usize].children = children;
    index
}

fn register_type(token: u32) -> u32 {
    ((token & D3DSP_REGTYPE_MASK) >> D3DSP_REGTYPE_SHIFT)
        | ((token & D3DSP_REGTYPE_MASK2) >> D3DSP_REGTYPE_SHIFT2)
}

fn instruction_length(words: &[u32], at: usize, major: u32) -> Result<usize, RenderError> {
    let token = *words
        .get(at)
        .ok_or_else(|| invalid_data("shader instruction is outside the bytecode"))?;
    let opcode = token & 0xffff;
    if opcode == OP_COMMENT {
        return Ok(((token & D3DSI_COMMENTSIZE_MASK) >> D3DSI_COMMENTSIZE_SHIFT) as usize);
    }
    if major > 1 {
        return Ok(((token & D3DSI_INSTLENGTH_MASK) >> D3DSI_INSTLENGTH_SHIFT) as usize);
    }
    match opcode {
        OP_END => Err(invalid_data(
            "shader bytecode contains an unexpected END token",
        )),
        OP_DEF | OP_DEFI => Ok(5),
        OP_DEFB => Ok(2),
        _ => {
            let mut cursor = at + 1;
            let mut length = 0usize;
            loop {
                let operand = *words
                    .get(cursor)
                    .ok_or_else(|| invalid_data("shader operands extend past the bytecode"))?;
                if operand & 0x8000_0000 == 0 {
                    break;
                }
                cursor += 1;
                length += 1;
            }
            Ok(length)
        }
    }
}

fn find_ctab_payload(words: &[u32]) -> Result<Option<Vec<u8>>, RenderError> {
    if words.is_empty() {
        return Ok(None);
    }
    let major = (words[0] >> 8) & 0xff;
    let mut at = 1usize;
    while at < words.len() {
        let token = words[at];
        let opcode = token & 0xffff;
        if opcode == OP_END {
            return Ok(None);
        }
        if opcode == OP_COMMENT {
            let length = ((token >> 16) & 0x7fff) as usize;
            if length == 0 {
                return Err(invalid_data("shader comment has no payload"));
            }
            let payload = at + 1;
            if payload + length > words.len() {
                return Err(invalid_data("shader comment extends past the bytecode"));
            }
            if words[payload] == CTAB_FOURCC {
                let mut bytes = Vec::with_capacity((length - 1) * 4);
                for word in &words[payload + 1..payload + length] {
                    bytes.extend_from_slice(&word.to_le_bytes());
                }
                return Ok(Some(bytes));
            }
            at = payload + length;
            continue;
        }
        let length = instruction_length(words, at, major)?;
        at = at
            .checked_add(1 + length)
            .ok_or_else(|| invalid_data("shader instruction length overflows"))?;
    }
    Ok(None)
}

pub(crate) struct CtabConstant {
    pub name: Vec<u8>,
    pub register_set: u32,
    pub register_index: u32,
    pub register_count: u32,
    pub class_: u32,
    pub rows: u32,
    pub columns: u32,
    pub elements: u32,
}

pub(crate) fn ctab_constants(code: &[u8]) -> Result<Vec<CtabConstant>, RenderError> {
    if code.len() < 4 || code.len() % 4 != 0 {
        return Err(invalid_data("CTAB source is not DWORD aligned"));
    }
    let words = code.len() / 4;
    let mut at = 1usize;
    while at < words {
        let token = rd_u32(code, at * 4)?;
        if token & 0xffff != OP_COMMENT {
            break;
        }
        let length = ((token >> 16) & 0x7fff) as usize;
        if length == 0 || at + 1 + length > words {
            return Err(invalid_data("CTAB comment runs past the bytecode"));
        }
        if rd_u32(code, (at + 1) * 4)? == CTAB_FOURCC {
            let mut payload = Vec::with_capacity((length - 1) * 4);
            for word in at + 2..at + 1 + length {
                payload.extend_from_slice(&code[word * 4..word * 4 + 4]);
            }
            let (_creator, _version, _constants, nodes) = parse_ctab(&payload)?;
            return Ok(nodes
                .into_iter()
                .map(|node| CtabConstant {
                    name: node.name,
                    register_set: node.register_set,
                    register_index: node.register_index,
                    register_count: node.register_count,
                    class_: node.class,
                    rows: node.rows,
                    columns: node.columns,
                    elements: node.elements,
                })
                .collect());
        }
        at += 1 + length;
    }
    Ok(Vec::new())
}

fn parse_ctab(data: &[u8]) -> Result<(u32, u32, u32, Vec<ParsedNode>), RenderError> {
    if data.len() < 28 {
        return Err(invalid_data("CTAB payload is shorter than its header"));
    }
    if rd_u32(data, 0)? != 28 {
        return Err(invalid_data("CTAB header size is not the published size"));
    }
    let creator = rd_u32(data, 4)?;
    validate_name(data, creator)?;
    let version = rd_u32(data, 8)?;
    let constants = rd_u32(data, 12)?;
    let constant_info = rd_u32(data, 16)? as usize;
    let entries = (constants as usize)
        .checked_mul(20)
        .ok_or_else(|| invalid_data("CTAB constant table size overflows"))?;
    if constant_info
        .checked_add(entries)
        .map(|end| end > data.len())
        .unwrap_or(true)
    {
        return Err(invalid_data("CTAB constant table extends past the payload"));
    }
    let mut budget = MAX_NODE_COUNT;
    let mut roots = Vec::with_capacity(constants as usize);
    for constant in 0..constants as usize {
        let entry = constant_info + constant * 20;
        let name = rd_u32(data, entry)?;
        let register_set = rd_u16(data, entry + 4)? as u32;
        let register_index = rd_u16(data, entry + 6)? as u32;
        let register_count = rd_u16(data, entry + 8)? as u32;
        let type_info = rd_u32(data, entry + 12)?;
        let default_raw = rd_u32(data, entry + 16)?;
        let max_index = register_index.saturating_add(register_count);
        let mut default = if default_raw == 0 {
            None
        } else {
            Some(default_raw)
        };
        let mut node = parse_type(
            data,
            type_info,
            false,
            register_index,
            max_index,
            &mut default,
            name,
            register_set,
            0,
            &mut budget,
        )?;
        if register_set == D3DXRS_INT4 {
            node.register_count = register_count;
            if node.default_offset != NO_DEFAULT {
                let end = node.default_offset as usize + register_count as usize * 16;
                if end > data.len() {
                    return Err(invalid_data(
                        "CTAB integer defaults extend past the payload",
                    ));
                }
            }
        }
        roots.push(node);
    }
    Ok((creator, version, constants, roots))
}

pub fn new_d3dx_constant_table() -> Box<D3dxConstantTable> {
    Box::new(D3dxConstantTable {
        buffer: Vec::new(),
        creator_offset: 0,
        version: 0,
        constants: 0,
        nodes: Vec::new(),
        roots: Vec::new(),
    })
}

pub fn d3dx_constant_table_create(
    table: &mut D3dxConstantTable,
    code: &[u32],
) -> D3dxConstantTableResult {
    if code.is_empty() {
        return empty_table_result(true);
    }
    let high = code[0] >> 16;
    if high != 0xfffe && high != 0xffff {
        return empty_table_result(true);
    }
    let pixel = high == 0xffff;
    let count = match shader_word_count(code, pixel) {
        Ok(count) => count,
        Err(error) => {
            return D3dxConstantTableResult {
                status: shader_status(error),
                buffer: Vec::new(),
                desc: D3dxConstantTableDesc {
                    creator_offset: 0,
                    version: 0,
                    constants: 0,
                },
                node_count: 0,
                invalid_bytecode: false,
            };
        }
    };
    let payload = match find_ctab_payload(&code[..count]) {
        Ok(Some(payload)) => payload,
        Ok(None) => {
            let result = empty_table_result(false);
            return D3dxConstantTableResult {
                status: status(
                    StatusCode::InvalidData,
                    "shader bytecode has no CTAB comment",
                ),
                ..result
            };
        }
        Err(error) => {
            let result = empty_table_result(false);
            return D3dxConstantTableResult {
                status: failed(error),
                ..result
            };
        }
    };
    match parse_ctab(&payload) {
        Ok((creator, version, constants, parsed_roots)) => {
            let mut nodes = Vec::new();
            let mut roots = Vec::with_capacity(parsed_roots.len());
            for root in parsed_roots {
                roots.push(flatten(root, &mut nodes));
            }
            table.buffer = payload;
            table.creator_offset = creator;
            table.version = version;
            table.constants = constants;
            table.nodes = nodes;
            table.roots = roots;
            let node_count = table.nodes.len() as u32;
            D3dxConstantTableResult {
                status: status(StatusCode::Ok, ""),
                buffer: table.buffer.clone(),
                desc: D3dxConstantTableDesc {
                    creator_offset: creator,
                    version,
                    constants,
                },
                node_count,
                invalid_bytecode: false,
            }
        }
        Err(error) => D3dxConstantTableResult {
            status: failed(error),
            buffer: Vec::new(),
            desc: D3dxConstantTableDesc {
                creator_offset: 0,
                version: 0,
                constants: 0,
            },
            node_count: 0,
            invalid_bytecode: false,
        },
    }
}

pub fn d3dx_constant_table_info(table: &D3dxConstantTable) -> D3dxConstantTableDesc {
    D3dxConstantTableDesc {
        creator_offset: table.creator_offset,
        version: table.version,
        constants: table.constants,
    }
}

pub fn d3dx_constant_table_desc(table: &D3dxConstantTable, node: u32) -> D3dxConstantDescResult {
    match table.nodes.get(node as usize) {
        Some(entry) => D3dxConstantDescResult {
            status: status(StatusCode::Ok, ""),
            desc: D3dxConstantDesc {
                name_offset: entry.name_offset,
                register_set: entry.register_set,
                register_index: entry.register_index,
                register_count: entry.register_count,
                class_: entry.class,
                type_: entry.type_,
                rows: entry.rows,
                columns: entry.columns,
                elements: entry.elements,
                struct_members: entry.struct_members,
                bytes: entry.bytes,
                default_offset: entry.default_offset,
            },
        },
        None => D3dxConstantDescResult {
            status: status(
                StatusCode::InvalidArgument,
                "constant table node is out of range",
            ),
            desc: empty_desc(),
        },
    }
}

pub fn d3dx_constant_table_constant(table: &D3dxConstantTable, parent: u32, index: u32) -> i32 {
    if parent == u32::MAX {
        return table
            .roots
            .get(index as usize)
            .map(|root| *root as i32)
            .unwrap_or(-1);
    }
    let Some(node) = table.nodes.get(parent as usize) else {
        return -1;
    };
    if index >= node.struct_members {
        return -1;
    }
    node.children
        .get(index as usize)
        .map(|child| *child as i32)
        .unwrap_or(-1)
}

pub fn d3dx_constant_table_element(table: &D3dxConstantTable, node: u32, index: u32) -> i32 {
    let Some(entry) = table.nodes.get(node as usize) else {
        return -1;
    };
    if index >= entry.elements {
        return -1;
    }
    if entry.elements > 1 {
        entry
            .children
            .get(index as usize)
            .map(|child| *child as i32)
            .unwrap_or(-1)
    } else {
        node as i32
    }
}

pub fn d3dx_constant_table_sampler(table: &D3dxConstantTable, node: u32) -> u32 {
    match table.nodes.get(node as usize) {
        Some(entry) if entry.register_set == D3DXRS_SAMPLER => entry.register_index,
        _ => u32::MAX,
    }
}

fn resolve(table: &D3dxConstantTable, parent: Option<u32>, name: &[u8]) -> Option<u32> {
    if name.is_empty() {
        return None;
    }
    let split = name
        .iter()
        .position(|&byte| byte == b'.' || byte == b'[')
        .unwrap_or(name.len());
    let head = &name[..split];
    let tail = &name[split..];
    let candidates: Vec<u32> = match parent {
        None => table.roots.clone(),
        Some(node) => {
            let parent = table.nodes.get(node as usize)?;
            let count = (parent.struct_members as usize).min(parent.children.len());
            parent.children[..count].to_vec()
        }
    };
    for candidate in candidates {
        if table.nodes[candidate as usize].name != head {
            continue;
        }
        if tail.is_empty() {
            return Some(candidate);
        }
        if tail.first() == Some(&b'.') {
            return resolve(table, Some(candidate), &tail[1..]);
        }
        if tail.first() == Some(&b'[') {
            return resolve_element(table, candidate, tail);
        }
        return None;
    }
    None
}

fn resolve_element(table: &D3dxConstantTable, node: u32, name: &[u8]) -> Option<u32> {
    if name.first() != Some(&b'[') {
        return None;
    }
    let rest = &name[1..];
    let close = rest.iter().position(|&byte| byte == b']')?;
    let digits = &rest[..close];
    if digits.is_empty() {
        return None;
    }
    let mut index: u32 = 0;
    for &digit in digits {
        if !digit.is_ascii_digit() {
            return None;
        }
        index = index.checked_mul(10)?.checked_add((digit - b'0') as u32)?;
    }
    let current = &table.nodes[node as usize];
    let element = if current.elements > 1 {
        if index >= current.elements {
            return None;
        }
        *current.children.get(index as usize)?
    } else {
        if index != 0 {
            return None;
        }
        current.children.first().copied().unwrap_or(node)
    };
    let after = &rest[close + 1..];
    if after.is_empty() {
        return Some(element);
    }
    if after.first() == Some(&b'.') {
        return resolve(table, Some(element), &after[1..]);
    }
    if after.first() == Some(&b'[') {
        return resolve_element(table, element, after);
    }
    None
}

pub fn d3dx_constant_table_find(table: &D3dxConstantTable, parent: u32, name: &[u8]) -> i32 {
    if name.is_empty() {
        return -1;
    }
    let parent = if parent == u32::MAX {
        None
    } else if (parent as usize) < table.nodes.len() {
        Some(parent)
    } else {
        return -1;
    };
    resolve(table, parent, name)
        .map(|node| node as i32)
        .unwrap_or(-1)
}

fn get_float(intype: u32, word: u32) -> f32 {
    match intype {
        D3DXPT_FLOAT | D3DXPT_VOID => f32::from_bits(word),
        D3DXPT_INT => (word as i32) as f32,
        D3DXPT_BOOL => (word != 0) as i32 as f32,
        _ => 0.0,
    }
}

fn get_int(intype: u32, word: u32) -> i32 {
    match intype {
        D3DXPT_FLOAT => {
            let value = f32::from_bits(word);
            if value >= i32::MIN as f32 && value < 2147483648.0 {
                value as i32
            } else {
                i32::MIN
            }
        }
        D3DXPT_INT | D3DXPT_VOID => word as i32,
        D3DXPT_BOOL => (word != 0) as i32,
        _ => 0,
    }
}

fn get_bool(intype: u32, word: u32) -> u32 {
    match intype {
        D3DXPT_FLOAT | D3DXPT_INT | D3DXPT_BOOL => (word != 0) as u32,
        D3DXPT_VOID => word,
        _ => 0,
    }
}

fn convert(intype: u32, word: u32, outtype: u32) -> u32 {
    if intype == outtype {
        return word;
    }
    match outtype {
        D3DXPT_FLOAT => get_float(intype, word).to_bits(),
        D3DXPT_INT => get_int(intype, word) as u32,
        D3DXPT_BOOL => get_bool(intype, word),
        _ => 0,
    }
}

impl D3dxConstantTable {
    #[allow(clippy::too_many_arguments)]
    fn set_node(
        &self,
        node_index: u32,
        source: &[u32],
        intype: u32,
        size: &mut usize,
        incol: u32,
        inclass: u32,
        index: usize,
        out: &mut Vec<Chunk>,
    ) -> Result<u32, RenderError> {
        let node = &self.nodes[node_index as usize];
        let rows = node.rows;
        let columns = node.columns;
        if *size < (rows as usize).saturating_mul(columns as usize) {
            *size = 0;
            return Ok(0);
        }
        if node.class == D3DXPC_STRUCT {
            return Err(unsupported(
                "struct constant conversion is not implemented for setters",
            ));
        }
        if node.elements > 1 {
            let mut last = 0u32;
            let mut element = 0u32;
            while element < node.elements && *size > 0 {
                let child_index = node.children[element as usize];
                last = last.saturating_add(self.set_node(
                    child_index,
                    source,
                    intype,
                    size,
                    incol,
                    inclass,
                    index + last as usize,
                    out,
                )?);
                if inclass == D3DXPC_MATRIX_ROWS
                    && node.class == D3DXPC_VECTOR
                    && (element % 4) == 3
                {
                    last = last.saturating_add(12);
                    *size = if *size < 12 { 0 } else { *size - 12 };
                }
                element += 1;
            }
            return Ok(last);
        }

        let mut regcount;
        let regsize;
        let mut cin = 1u32;
        let mut rin = 1u32;
        match node.class {
            D3DXPC_SCALAR | D3DXPC_VECTOR | D3DXPC_MATRIX_ROWS => {
                regcount = node.register_count.min(rows);
                if inclass == D3DXPC_MATRIX_ROWS {
                    cin = incol;
                } else {
                    rin = incol;
                }
                regsize = columns;
            }
            D3DXPC_MATRIX_COLUMNS => {
                regcount = node.register_count.min(columns);
                if inclass == D3DXPC_MATRIX_ROWS {
                    rin = incol;
                } else {
                    cin = incol;
                }
                regsize = rows;
            }
            _ => {
                return Err(unsupported(
                    "constant class is not valid for numeric setters",
                ));
            }
        }
        if regsize == 0 {
            return Err(invalid("constant has a zero register size"));
        }

        let (ret, subtract): (u32, usize) = match inclass {
            D3DXPC_SCALAR => {
                let amount = columns.saturating_mul(rows) as usize;
                (columns.saturating_mul(rows), amount)
            }
            D3DXPC_VECTOR => match node.class {
                D3DXPC_MATRIX_ROWS => {
                    let need = (regcount as usize).saturating_mul(4);
                    if *size < need {
                        *size = 0;
                        return Ok(0);
                    }
                    (regcount.saturating_mul(4), need)
                }
                D3DXPC_MATRIX_COLUMNS => (
                    regsize.saturating_mul(4),
                    (regcount as usize).saturating_mul(4),
                ),
                D3DXPC_SCALAR => (1, 1),
                D3DXPC_VECTOR => (4, 4),
                _ => {
                    return Err(unsupported("constant class is not valid for this setter"));
                }
            },
            D3DXPC_MATRIX_ROWS => {
                let amount = match node.class {
                    D3DXPC_MATRIX_ROWS | D3DXPC_MATRIX_COLUMNS => {
                        if *size < 16 {
                            *size = 0;
                            return Ok(0);
                        }
                        16
                    }
                    D3DXPC_SCALAR => 4,
                    D3DXPC_VECTOR => 1,
                    _ => {
                        return Err(unsupported("constant class is not valid for this setter"));
                    }
                };
                (amount, amount as usize)
            }
            D3DXPC_MATRIX_COLUMNS => {
                let amount = match node.class {
                    D3DXPC_MATRIX_ROWS | D3DXPC_MATRIX_COLUMNS => {
                        if *size < 16 {
                            *size = 0;
                            return Ok(0);
                        }
                        16
                    }
                    D3DXPC_SCALAR => 1,
                    D3DXPC_VECTOR => 4,
                    _ => {
                        return Err(unsupported("constant class is not valid for this setter"));
                    }
                };
                (amount, amount as usize)
            }
            _ => return Err(unsupported("source class is not valid for this setter")),
        };
        *size = (*size).saturating_sub(subtract);

        let read = |position: usize| {
            source.get(position).copied().ok_or_else(|| {
                unsupported(
                    "partial setter layout requires source words outside the supplied input",
                )
            })
        };
        match node.register_set {
            D3DXRS_BOOL => {
                regcount = node.register_count.min(columns.saturating_mul(rows));
                let mut values = Vec::with_capacity(regcount as usize);
                let mut lane = 0u32;
                for register in 0..regcount {
                    let position = index
                        + (register / regsize) as usize * rin as usize
                        + lane as usize * cin as usize;
                    let value = convert(
                        node.type_,
                        convert(intype, read(position)?, node.type_),
                        D3DXPT_BOOL,
                    );
                    values.push(value);
                    lane += 1;
                    if lane >= regsize {
                        lane = 0;
                    }
                }
                out.push(Chunk::B {
                    start: node.register_index,
                    values,
                });
            }
            D3DXRS_INT4 => {
                let mut values = Vec::with_capacity(regcount as usize * 4);
                for register in 0..regcount {
                    let mut vector = [0i32, 0, 1, 0];
                    for lane in 0..regsize {
                        let position =
                            index + register as usize * rin as usize + lane as usize * cin as usize;
                        if (lane as usize) < 4 {
                            let value = convert(
                                node.type_,
                                convert(intype, read(position)?, node.type_),
                                D3DXPT_INT,
                            );
                            vector[lane as usize] = value as i32;
                        }
                    }
                    values.extend_from_slice(&vector);
                }
                out.push(Chunk::I {
                    start: node.register_index,
                    values,
                });
            }
            D3DXRS_FLOAT4 => {
                let mut values = Vec::with_capacity(regcount as usize * 4);
                for register in 0..regcount {
                    let mut vector = [0.0f32; 4];
                    for lane in 0..regsize {
                        let position =
                            index + register as usize * rin as usize + lane as usize * cin as usize;
                        if (lane as usize) < 4 {
                            let value = convert(
                                node.type_,
                                convert(intype, read(position)?, node.type_),
                                D3DXPT_FLOAT,
                            );
                            vector[lane as usize] = f32::from_bits(value);
                        }
                    }
                    values.extend_from_slice(&vector);
                }
                out.push(Chunk::F {
                    start: node.register_index,
                    values,
                });
            }
            _ => {
                return Err(unsupported(
                    "constant register set is not valid for numeric setters",
                ));
            }
        }
        Ok(ret)
    }

    fn apply_chunks(&self, device: &mut D3d9Device, chunks: &[Chunk]) -> Status {
        let pixel = (self.version & 0xffff0000) != 0xfffe0000;
        for chunk in chunks {
            let (start, count, limit) = chunk.range(pixel);
            if start as usize + count > limit {
                return status(
                    StatusCode::InvalidArgument,
                    "shader constant register range is out of bounds",
                );
            }
        }
        for chunk in chunks {
            let result = match chunk {
                Chunk::F { start, values } => device.state.set_constants_f(pixel, *start, values),
                Chunk::I { start, values } => device.state.set_constants_i(pixel, *start, values),
                Chunk::B { start, values } => device.state.set_constants_b(pixel, *start, values),
            };
            if let Err(error) = result {
                return failed(error);
            }
        }
        status(StatusCode::Ok, "")
    }
}

fn setter_applies(setter: u32, class: u32) -> bool {
    match setter {
        D3DX_SET_BOOL | D3DX_SET_INT | D3DX_SET_FLOAT => class == D3DXPC_SCALAR,
        D3DX_SET_VECTOR => class != D3DXPC_MATRIX_ROWS && class != D3DXPC_MATRIX_COLUMNS,
        D3DX_SET_VALUE
        | D3DX_SET_BOOL_ARRAY
        | D3DX_SET_INT_ARRAY
        | D3DX_SET_FLOAT_ARRAY
        | D3DX_SET_VECTOR_ARRAY
        | D3DX_SET_MATRIX
        | D3DX_SET_MATRIX_ARRAY
        | D3DX_SET_MATRIX_TRANSPOSE
        | D3DX_SET_MATRIX_TRANSPOSE_ARRAY => true,
        D3DX_SET_MATRIX_POINTER_ARRAY | D3DX_SET_MATRIX_TRANSPOSE_POINTER_ARRAY => true,
        _ => false,
    }
}

pub fn d3dx_constant_table_input_size(
    table: &D3dxConstantTable,
    node: u32,
    setter: u32,
    requested_bytes: u64,
) -> u64 {
    let Some(entry) = table.nodes.get(node as usize) else {
        return 0;
    };
    if setter > D3DX_SET_MATRIX_TRANSPOSE_POINTER_ARRAY {
        return 0;
    }
    if entry.register_set == D3DXRS_SAMPLER || entry.class == D3DXPC_OBJECT {
        return 0;
    }
    let cap = (entry.bytes as u64).saturating_mul(16).max(64);
    requested_bytes.min(cap)
}

pub fn d3dx_constant_table_apply(
    device: &mut D3d9Device,
    table: &D3dxConstantTable,
    node: u32,
    setter: u32,
    data: &[u8],
) -> Status {
    let Some(entry) = table.nodes.get(node as usize) else {
        return status(
            StatusCode::InvalidArgument,
            "constant table handle is not valid",
        );
    };
    if entry.register_set == D3DXRS_SAMPLER || entry.class == D3DXPC_OBJECT {
        return status(
            StatusCode::InvalidArgument,
            "sampler and object constants are not numeric shader constants",
        );
    }
    let pointer_array = matches!(
        setter,
        D3DX_SET_MATRIX_POINTER_ARRAY | D3DX_SET_MATRIX_TRANSPOSE_POINTER_ARRAY
    );
    if pointer_array && entry.class != D3DXPC_MATRIX_ROWS && entry.class != D3DXPC_MATRIX_COLUMNS {
        return status(
            StatusCode::Unsupported,
            "pointer array setters for scalar or vector constants are not implemented",
        );
    }
    let (intype, inclass, incol) = match setter {
        D3DX_SET_VALUE => (entry.type_, D3DXPC_SCALAR, entry.columns),
        D3DX_SET_BOOL | D3DX_SET_BOOL_ARRAY => (D3DXPT_BOOL, D3DXPC_SCALAR, entry.columns),
        D3DX_SET_INT | D3DX_SET_INT_ARRAY => (D3DXPT_INT, D3DXPC_SCALAR, entry.columns),
        D3DX_SET_FLOAT | D3DX_SET_FLOAT_ARRAY => (D3DXPT_FLOAT, D3DXPC_SCALAR, entry.columns),
        D3DX_SET_VECTOR | D3DX_SET_VECTOR_ARRAY => (D3DXPT_FLOAT, D3DXPC_VECTOR, 4),
        D3DX_SET_MATRIX | D3DX_SET_MATRIX_ARRAY | D3DX_SET_MATRIX_POINTER_ARRAY => {
            (D3DXPT_FLOAT, D3DXPC_MATRIX_COLUMNS, 4)
        }
        D3DX_SET_MATRIX_TRANSPOSE
        | D3DX_SET_MATRIX_TRANSPOSE_ARRAY
        | D3DX_SET_MATRIX_TRANSPOSE_POINTER_ARRAY => (D3DXPT_FLOAT, D3DXPC_MATRIX_ROWS, 4),
        _ => {
            return status(StatusCode::Unsupported, "unknown constant setter");
        }
    };
    if !setter_applies(setter, entry.class) {
        return status(StatusCode::Ok, "");
    }
    let mut source = Vec::with_capacity(data.len() / 4);
    for word in data.chunks_exact(4) {
        source.push(u32::from_le_bytes([word[0], word[1], word[2], word[3]]));
    }
    let mut size = source.len();
    let mut chunks = Vec::new();
    if let Err(error) = table.set_node(
        node,
        &source,
        intype,
        &mut size,
        incol,
        inclass,
        0,
        &mut chunks,
    ) {
        return failed(error);
    }
    table.apply_chunks(device, &chunks)
}

pub fn d3dx_constant_table_defaults(device: &mut D3d9Device, table: &D3dxConstantTable) -> Status {
    let mut chunks = Vec::new();
    for root in &table.roots {
        let node = &table.nodes[*root as usize];
        if node.default_offset == NO_DEFAULT || node.register_set == D3DXRS_SAMPLER {
            continue;
        }
        let count = node.register_count as usize;
        let base = node.default_offset as usize;
        match node.register_set {
            D3DXRS_FLOAT4 => {
                let mut values = Vec::with_capacity(count * 4);
                for word in 0..count * 4 {
                    let value = match rd_u32(&table.buffer, base + word * 4) {
                        Ok(value) => value,
                        Err(error) => return failed(error),
                    };
                    values.push(f32::from_bits(value));
                }
                chunks.push(Chunk::F {
                    start: node.register_index,
                    values,
                });
            }
            D3DXRS_INT4 => {
                let mut values = Vec::with_capacity(count * 4);
                for word in 0..count * 4 {
                    let value = match rd_u32(&table.buffer, base + word * 4) {
                        Ok(value) => value,
                        Err(error) => return failed(error),
                    };
                    values.push(value as i32);
                }
                chunks.push(Chunk::I {
                    start: node.register_index,
                    values,
                });
            }
            D3DXRS_BOOL => {
                let mut values = Vec::with_capacity(count);
                for word in 0..count {
                    let value = match rd_u32(&table.buffer, base + word * 4) {
                        Ok(value) => value,
                        Err(error) => return failed(error),
                    };
                    values.push(value);
                }
                chunks.push(Chunk::B {
                    start: node.register_index,
                    values,
                });
            }
            _ => {
                return status(
                    StatusCode::Unsupported,
                    "default value register set is not supported",
                );
            }
        }
    }
    table.apply_chunks(device, &chunks)
}

pub fn d3dx_shader_version(code: &[u32]) -> u32 {
    code.first().copied().unwrap_or(0)
}

fn shader_semantics(code: &[u32], output: bool) -> Result<Vec<D3dxSemantic>, RenderError> {
    if code.is_empty() {
        return Err(invalid_data("shader bytecode is empty"));
    }
    let version = code[0];
    let high = version >> 16;
    if high != 0xfffe && high != 0xffff {
        return Err(invalid_data("shader version token is not valid"));
    }
    let is_pixel = high == 0xffff;
    let major = (version >> 8) & 0xff;
    let has_dcl = (!is_pixel && (!output || major == 3)) || (is_pixel && !output && major >= 2);

    let mut semantics = Vec::new();
    let mut colors: u32 = 0;
    let mut rastout: u32 = 0;
    let mut texcoords: u32 = 0;
    let mut depth = false;

    let mut at = 1usize;
    while at < code.len() {
        let token = code[at];
        let opcode = token & 0xffff;
        if opcode == OP_END {
            break;
        }
        if has_dcl && opcode == OP_DCL {
            let usage_token = *code
                .get(at + 1)
                .ok_or_else(|| invalid_data("DCL instruction is truncated"))?;
            let register = *code
                .get(at + 2)
                .ok_or_else(|| invalid_data("DCL instruction is truncated"))?;
            let reg_type = register_type(register);
            let index = register & D3DSP_REGNUM_MASK;
            if is_pixel && !output && major == 2 {
                if reg_type != D3DSPR_SAMPLER {
                    let usage = match reg_type {
                        0 | 1 | 5 => D3DDECLUSAGE_COLOR,
                        3 | 6 => D3DDECLUSAGE_TEXCOORD,
                        9 => D3DDECLUSAGE_DEPTH,
                        _ => D3DDECLUSAGE_COLOR,
                    };
                    semantics.push(D3dxSemantic {
                        usage,
                        usage_index: index,
                    });
                }
            } else if (!output && reg_type == D3DSPR_INPUT) || (output && reg_type == D3DSPR_OUTPUT)
            {
                semantics.push(D3dxSemantic {
                    usage: usage_token & 0x0f,
                    usage_index: (usage_token >> 16) & 0x0f,
                });
            }
            at += 3;
            continue;
        }
        if !has_dcl {
            let length = instruction_length(code, at, major)?;
            match opcode {
                OP_COMMENT | OP_DEF | OP_DEFB | OP_DEFI => {}
                _ => {
                    let mut cursor = at + 1;
                    while let Some(&operand) = code.get(cursor) {
                        if operand & 0x8000_0000 == 0 {
                            break;
                        }
                        let reg_type = register_type(operand);
                        let index = operand & D3DSP_REGNUM_MASK;
                        if (reg_type == D3DSPR_TEMP && is_pixel && major == 1)
                            || (reg_type == D3DSPR_INPUT && is_pixel)
                            || (reg_type == D3DSPR_TEXTURE && is_pixel && !output)
                            || reg_type == D3DSPR_RASTOUT
                            || reg_type == D3DSPR_ATTROUT
                            || reg_type == D3DSPR_OUTPUT
                            || reg_type == D3DSPR_DEPTHOUT
                        {
                            if reg_type == D3DSPR_RASTOUT {
                                if index < 32 {
                                    rastout |= 1 << index;
                                }
                            } else if reg_type == D3DSPR_DEPTHOUT {
                                depth = true;
                            } else if reg_type == D3DSPR_TEXTURE || reg_type == D3DSPR_OUTPUT {
                                if index < 32 {
                                    texcoords |= 1 << index;
                                }
                            } else if index < 32 {
                                colors |= 1 << index;
                            }
                        }
                        cursor += 1;
                    }
                }
            }
            at = at
                .checked_add(1 + length)
                .ok_or_else(|| invalid_data("shader instruction length overflows"))?;
            continue;
        }
        let length = instruction_length(code, at, major)?;
        at = at
            .checked_add(1 + length)
            .ok_or_else(|| invalid_data("shader instruction length overflows"))?;
    }

    if !has_dcl {
        let mut index = 0u32;
        let mut remaining = texcoords;
        while remaining != 0 {
            if remaining & 1 != 0 {
                semantics.push(D3dxSemantic {
                    usage: D3DDECLUSAGE_TEXCOORD,
                    usage_index: index,
                });
            }
            remaining >>= 1;
            index += 1;
        }
        index = 0;
        remaining = colors;
        while remaining != 0 {
            if remaining & 1 != 0 {
                semantics.push(D3dxSemantic {
                    usage: D3DDECLUSAGE_COLOR,
                    usage_index: index,
                });
            }
            remaining >>= 1;
            index += 1;
        }
        index = 0;
        remaining = rastout;
        while remaining != 0 {
            if remaining & 1 != 0 {
                let usage = match index {
                    0 => D3DDECLUSAGE_POSITION,
                    1 => D3DDECLUSAGE_FOG,
                    2 => D3DDECLUSAGE_PSIZE,
                    _ => 0,
                };
                semantics.push(D3dxSemantic {
                    usage,
                    usage_index: 0,
                });
            }
            remaining >>= 1;
            index += 1;
        }
        if depth {
            semantics.push(D3dxSemantic {
                usage: D3DDECLUSAGE_DEPTH,
                usage_index: 0,
            });
        }
    }
    Ok(semantics)
}

pub fn d3dx_shader_input_semantics(code: &[u32]) -> D3dxSemanticsResult {
    if code.is_empty() {
        return D3dxSemanticsResult {
            status: status(StatusCode::InvalidData, "shader bytecode is empty"),
            semantics: Vec::new(),
        };
    }
    let high = code[0] >> 16;
    if high != 0xfffe && high != 0xffff {
        return D3dxSemanticsResult {
            status: status(StatusCode::InvalidData, "shader version token is not valid"),
            semantics: Vec::new(),
        };
    }
    let pixel = high == 0xffff;
    let count = match shader_word_count(code, pixel) {
        Ok(count) => count,
        Err(error) => {
            return D3dxSemanticsResult {
                status: shader_status(error),
                semantics: Vec::new(),
            };
        }
    };
    match shader_semantics(&code[..count], false) {
        Ok(semantics) => D3dxSemanticsResult {
            status: status(StatusCode::Ok, ""),
            semantics,
        },
        Err(error) => D3dxSemanticsResult {
            status: failed(error),
            semantics: Vec::new(),
        },
    }
}

pub fn d3dx_shader_profiles(device: &D3d9Device) -> D3dxProfilesResult {
    let caps = super::d3d9_device_caps(device);
    if caps.status.code != StatusCode::Ok {
        return D3dxProfilesResult {
            status: caps.status,
            vertex: String::new(),
            pixel: String::new(),
        };
    }
    let vertex_version = rd_u32(&caps.caps, 196).unwrap_or(0);
    let pixel_version = rd_u32(&caps.caps, 204).unwrap_or(0);
    let vertex = match vertex_version {
        0xfffe_0101 => "vs_1_1",
        0xfffe_0200 => "vs_2_0",
        0xfffe_0300 => "vs_3_0",
        _ => "",
    };
    let pixel = match pixel_version {
        0xffff_0101 => "ps_1_1",
        0xffff_0102 => "ps_1_2",
        0xffff_0103 => "ps_1_3",
        0xffff_0104 => "ps_1_4",
        0xffff_0200 => "ps_2_0",
        0xffff_0300 => "ps_3_0",
        _ => "",
    };
    D3dxProfilesResult {
        status: status(StatusCode::Ok, ""),
        vertex: vertex.to_string(),
        pixel: pixel.to_string(),
    }
}
