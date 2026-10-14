use super::gpu::D3d9Gpu;
use super::shader::VertexElement;
use super::state::D3d9State;
use super::{SurfaceView, TargetRect, wgsl};
use crate::RenderError;
use std::borrow::Cow;
use std::collections::HashMap;

pub const D3DPT_POINTLIST: u32 = 1;
pub const D3DPT_LINELIST: u32 = 2;
pub const D3DPT_LINESTRIP: u32 = 3;
pub const D3DPT_TRIANGLELIST: u32 = 4;
pub const D3DPT_TRIANGLESTRIP: u32 = 5;
pub const D3DPT_TRIANGLEFAN: u32 = 6;

const D3DDECLTYPE_FLOAT1: u8 = 0;
const D3DDECLTYPE_FLOAT2: u8 = 1;
const D3DDECLTYPE_FLOAT3: u8 = 2;
const D3DDECLTYPE_FLOAT4: u8 = 3;
const D3DDECLTYPE_D3DCOLOR: u8 = 4;
const D3DDECLTYPE_UBYTE4: u8 = 5;
const D3DDECLTYPE_SHORT2: u8 = 6;
const D3DDECLTYPE_SHORT4: u8 = 7;
const D3DDECLTYPE_UBYTE4N: u8 = 8;
const D3DDECLTYPE_SHORT2N: u8 = 9;
const D3DDECLTYPE_SHORT4N: u8 = 10;
const D3DDECLTYPE_USHORT2N: u8 = 11;
const D3DDECLTYPE_USHORT4N: u8 = 12;
const D3DDECLTYPE_UDEC3: u8 = 13;
const D3DDECLTYPE_DEC3N: u8 = 14;
const D3DDECLTYPE_FLOAT16_2: u8 = 15;
const D3DDECLTYPE_FLOAT16_4: u8 = 16;
const D3DDECLTYPE_UNUSED: u8 = 17;

fn invalid(op: &'static str, why: impl Into<String>) -> RenderError {
    RenderError::invalid(op, why)
}

fn unsupported(op: &'static str, why: impl Into<String>) -> RenderError {
    RenderError::new(op, why)
}

fn clamp_signed(value: f32, _bits: u32) -> f32 {
    value.max(-1.0)
}

pub(crate) fn element_size(element_type: u8) -> Option<u32> {
    Some(match element_type {
        D3DDECLTYPE_FLOAT1 => 4,
        D3DDECLTYPE_FLOAT2 => 8,
        D3DDECLTYPE_FLOAT3 => 12,
        D3DDECLTYPE_FLOAT4 => 16,
        D3DDECLTYPE_D3DCOLOR | D3DDECLTYPE_UBYTE4 | D3DDECLTYPE_UBYTE4N | D3DDECLTYPE_DEC3N
        | D3DDECLTYPE_UDEC3 => 4,
        D3DDECLTYPE_SHORT2 | D3DDECLTYPE_SHORT2N | D3DDECLTYPE_USHORT2N | D3DDECLTYPE_FLOAT16_2 => {
            4
        }
        D3DDECLTYPE_SHORT4 | D3DDECLTYPE_SHORT4N | D3DDECLTYPE_USHORT4N | D3DDECLTYPE_FLOAT16_4 => {
            8
        }
        _ => return None,
    })
}

fn read_f32(bytes: &[u8]) -> f32 {
    f32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

fn read_f16(bytes: &[u8]) -> f32 {
    half_to_f32(u16::from_le_bytes([bytes[0], bytes[1]]))
}

fn half_to_f32(bits: u16) -> f32 {
    let sign = (bits >> 15) as u32;
    let exp = ((bits >> 10) & 0x1f) as u32;
    let frac = (bits & 0x3ff) as u32;
    let value = if exp == 0 {
        if frac == 0 {
            0.0
        } else {
            (frac as f32) * 2.0f32.powi(-24)
        }
    } else if exp == 0x1f {
        if frac == 0 { f32::INFINITY } else { f32::NAN }
    } else {
        (1.0 + frac as f32 / 1024.0) * 2.0f32.powi(exp as i32 - 15)
    };
    if sign != 0 { -value } else { value }
}

pub(crate) fn expand_element(element_type: u8, bytes: &[u8]) -> Result<[f32; 4], RenderError> {
    let mut out = [0.0f32, 0.0, 0.0, 1.0];
    match element_type {
        D3DDECLTYPE_FLOAT1 => out[0] = read_f32(bytes),
        D3DDECLTYPE_FLOAT2 => {
            for i in 0..2 {
                out[i] = read_f32(&bytes[i * 4..]);
            }
        }
        D3DDECLTYPE_FLOAT3 => {
            for i in 0..3 {
                out[i] = read_f32(&bytes[i * 4..]);
            }
        }
        D3DDECLTYPE_FLOAT4 => {
            for (i, v) in out.iter_mut().enumerate() {
                *v = read_f32(&bytes[i * 4..]);
            }
        }
        D3DDECLTYPE_D3DCOLOR => {
            let argb = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
            out[0] = ((argb >> 16) & 0xff) as f32 / 255.0;
            out[1] = ((argb >> 8) & 0xff) as f32 / 255.0;
            out[2] = (argb & 0xff) as f32 / 255.0;
            out[3] = ((argb >> 24) & 0xff) as f32 / 255.0;
        }
        D3DDECLTYPE_UBYTE4 | D3DDECLTYPE_UBYTE4N => {
            for i in 0..4 {
                let v = bytes[i] as f32;
                out[i] = if element_type == D3DDECLTYPE_UBYTE4N {
                    v / 255.0
                } else {
                    v
                };
            }
        }
        D3DDECLTYPE_SHORT2 | D3DDECLTYPE_SHORT2N | D3DDECLTYPE_USHORT2N => {
            for i in 0..2 {
                let raw = u16::from_le_bytes([bytes[i * 2], bytes[i * 2 + 1]]);
                out[i] = match element_type {
                    D3DDECLTYPE_SHORT2 => raw as i16 as f32,
                    D3DDECLTYPE_SHORT2N => clamp_signed(raw as i16 as f32 / 32767.0, 16),
                    _ => raw as f32 / 65535.0,
                };
            }
        }
        D3DDECLTYPE_SHORT4 | D3DDECLTYPE_SHORT4N | D3DDECLTYPE_USHORT4N => {
            for i in 0..4 {
                let raw = u16::from_le_bytes([bytes[i * 2], bytes[i * 2 + 1]]);
                out[i] = match element_type {
                    D3DDECLTYPE_SHORT4 => raw as i16 as f32,
                    D3DDECLTYPE_SHORT4N => clamp_signed(raw as i16 as f32 / 32767.0, 16),
                    _ => raw as f32 / 65535.0,
                };
            }
        }
        D3DDECLTYPE_UDEC3 | D3DDECLTYPE_DEC3N => {
            let raw = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
            let x = raw & 0x3ff;
            let y = (raw >> 10) & 0x3ff;
            let z = (raw >> 20) & 0x3ff;
            let norm = element_type == D3DDECLTYPE_DEC3N;
            for (i, v) in [x, y, z].into_iter().enumerate() {
                out[i] = if norm {
                    let signed = if v & 0x200 != 0 {
                        v as i32 - 0x400
                    } else {
                        v as i32
                    };
                    clamp_signed(signed as f32 / 511.0, 11)
                } else {
                    v as f32
                };
            }
        }
        D3DDECLTYPE_FLOAT16_2 | D3DDECLTYPE_FLOAT16_4 => {
            let count = if element_type == D3DDECLTYPE_FLOAT16_2 {
                2
            } else {
                4
            };
            for i in 0..count {
                out[i] = read_f16(&bytes[i * 2..]);
            }
        }
        _ => {
            return Err(unsupported(
                "Draw",
                format!("vertex element type {element_type} is not convertible"),
            ));
        }
    }
    Ok(out)
}

pub(crate) struct BoundTexture {
    pub stage: u32,
    pub view: wgpu::TextureView,
    pub width: u32,
    pub height: u32,
    pub depth: bool,
    pub cube: bool,
    pub volume: bool,
    pub format: u32,
    pub lod: u32,
    pub levels: u32,
}

pub(crate) struct StreamData {
    pub stream: u32,
    pub stride: u32,
    pub offset: u32,
    pub bytes: Vec<u8>,
}

pub(crate) struct DrawCommand {
    pub primitive: u32,
    pub primitive_count: u32,
    pub start_vertex: u32,
    pub base_vertex: i32,
    pub indexed: bool,
    pub index_format: u32,
    pub index_bytes: Vec<u8>,
    pub min_vertex: u32,
    pub num_vertices: u32,
    pub streams: Vec<StreamData>,
}

pub(crate) struct DrawSnapshot {
    pub command: DrawCommand,
    pub target: SurfaceView,
    pub depth: Option<SurfaceView>,
    pub textures: Vec<BoundTexture>,
}

struct LinkedInput {
    location: u32,
    stream: u16,
    offset: u16,
    element_type: u8,
}

fn link_inputs(
    decl: &[VertexElement],
    program: &wgsl::Program,
) -> Result<Vec<LinkedInput>, RenderError> {
    if program.inputs.is_empty() {
        return Ok(Vec::new());
    }
    let mut linked = Vec::new();
    for input in &program.inputs {
        let element = decl
            .iter()
            .find(|e| e.usage as u32 == input.usage && e.usage_index as u32 == input.usage_index);
        let Some(element) = element else {
            return Err(unsupported(
                "Draw",
                format!(
                    "shader input {}/{} has no matching vertex declaration element",
                    input.usage, input.usage_index
                ),
            ));
        };
        if element.element_type == D3DDECLTYPE_UNUSED {
            return Err(unsupported("Draw", "vertex declaration element is unused"));
        }
        if element_size(element.element_type).is_none() {
            return Err(unsupported("Draw", "unsupported vertex element type"));
        }
        linked.push(LinkedInput {
            location: input.index,
            stream: element.stream,
            offset: element.offset,
            element_type: element.element_type,
        });
    }
    Ok(linked)
}

pub(crate) fn primitive_vertex_count(primitive: u32, count: u32) -> Result<u32, RenderError> {
    let vertices = match primitive {
        D3DPT_POINTLIST => Some(count),
        D3DPT_LINELIST => count.checked_mul(2),
        D3DPT_LINESTRIP => count.checked_add(1),
        D3DPT_TRIANGLELIST => count.checked_mul(3),
        D3DPT_TRIANGLESTRIP | D3DPT_TRIANGLEFAN => count.checked_add(2),
        _ => {
            return Err(invalid(
                "Draw",
                format!("unknown D3DPRIMITIVETYPE {primitive}"),
            ));
        }
    };
    vertices.ok_or_else(|| invalid("Draw", "primitive vertex count overflow"))
}

fn index_count(index_format: u32, bytes: &[u8]) -> usize {
    let size = if index_format == 101 { 2 } else { 4 };
    bytes.len() / size
}

fn indexed_vertex_index(bytes: &[u8], index_format: u32, index: usize) -> u32 {
    if index_format == 101 {
        let at = index * 2;
        u16::from_le_bytes([bytes[at], bytes[at + 1]]) as u32
    } else {
        let at = index * 4;
        u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]])
    }
}

fn expanded_indices(
    command: &DrawCommand,
) -> Result<(Vec<u32>, u32, wgpu::PrimitiveTopology), RenderError> {
    let count = primitive_vertex_count(command.primitive, command.primitive_count)?;
    let mut order = Vec::new();
    order
        .try_reserve_exact(count as usize)
        .map_err(|_| RenderError::out_of_memory("Draw", "index expansion allocation failed"))?;
    if command.indexed {
        if !matches!(command.index_format, 101 | 102) {
            return Err(invalid("Draw", "invalid index format"));
        }
        let available = index_count(command.index_format, &command.index_bytes);
        let start = command.start_vertex as usize;
        let end = start
            .checked_add(count as usize)
            .ok_or_else(|| invalid("Draw", "index range overflow"))?;
        if end > available {
            return Err(invalid("Draw", "index range exceeds the index stream"));
        }
        for i in start..end {
            let raw = indexed_vertex_index(&command.index_bytes, command.index_format, i) as i64;
            if raw < i64::from(command.min_vertex)
                || raw >= i64::from(command.min_vertex) + i64::from(command.num_vertices)
            {
                return Err(invalid("Draw", "index outside MinVertexIndex/NumVertices"));
            }
            let absolute = raw + i64::from(command.base_vertex);
            if absolute < 0 || absolute > u32::MAX as i64 {
                return Err(invalid(
                    "Draw",
                    "index plus base vertex is outside the vertex window",
                ));
            }
            order.push(absolute as u32);
        }
    } else {
        let start = command.start_vertex;
        for i in 0..count {
            let absolute = u64::from(start) + u64::from(i);
            if absolute > u32::MAX as u64 {
                return Err(invalid("Draw", "vertex range overflow"));
            }
            order.push(absolute as u32);
        }
    }
    let topology = match command.primitive {
        D3DPT_POINTLIST => wgpu::PrimitiveTopology::PointList,
        D3DPT_LINELIST => wgpu::PrimitiveTopology::LineList,
        D3DPT_LINESTRIP => wgpu::PrimitiveTopology::LineStrip,
        D3DPT_TRIANGLELIST => wgpu::PrimitiveTopology::TriangleList,
        D3DPT_TRIANGLESTRIP => wgpu::PrimitiveTopology::TriangleStrip,
        D3DPT_TRIANGLEFAN => {
            let mut triangles = Vec::new();
            if order.len() >= 3 {
                triangles
                    .try_reserve_exact((order.len() - 2) * 3)
                    .map_err(|_| {
                        RenderError::out_of_memory("Draw", "triangle fan allocation failed")
                    })?;
                for i in 1..order.len() - 1 {
                    triangles.extend_from_slice(&[order[0], order[i], order[i + 1]]);
                }
            }
            order = triangles;
            wgpu::PrimitiveTopology::TriangleList
        }
        _ => return Err(invalid("Draw", "unknown D3DPRIMITIVETYPE")),
    };
    Ok((order, count, topology))
}

fn stream_for<'a>(command: &'a DrawCommand, stream: u32) -> Result<&'a StreamData, RenderError> {
    command
        .streams
        .iter()
        .find(|s| s.stream == stream)
        .ok_or_else(|| unsupported("Draw", format!("vertex stream {stream} is not bound")))
}

fn fetch_element(
    command: &DrawCommand,
    stream: u16,
    offset: u16,
    element_type: u8,
    index: u32,
) -> Result<[f32; 4], RenderError> {
    let data = stream_for(command, stream as u32)?;
    let base = (index as usize)
        .checked_mul(data.stride as usize)
        .and_then(|v| v.checked_add(data.offset as usize))
        .and_then(|v| v.checked_add(offset as usize))
        .ok_or_else(|| invalid("Draw", "vertex offset overflow"))?;
    let size = element_size(element_type)
        .ok_or_else(|| unsupported("Draw", "unsupported vertex element type"))?
        as usize;
    let end = base
        .checked_add(size)
        .ok_or_else(|| invalid("Draw", "vertex offset overflow"))?;
    if end > data.bytes.len() {
        return Err(invalid("Draw", "vertex fetch is outside the stream"));
    }
    expand_element(element_type, &data.bytes[base..end])
}

fn canonical_shader_vertices(
    command: &DrawCommand,
    linked: &[LinkedInput],
    order: &[u32],
) -> Result<Vec<f32>, RenderError> {
    let slots = linked.len();
    let total = order
        .len()
        .checked_mul(slots)
        .and_then(|v| v.checked_mul(4))
        .ok_or_else(|| invalid("Draw", "vertex buffer size overflow"))?;
    let mut out = Vec::new();
    out.try_reserve_exact(total)
        .map_err(|_| RenderError::out_of_memory("Draw", "vertex repack allocation failed"))?;
    for &index in order {
        for input in linked {
            let value = fetch_element(
                command,
                input.stream,
                input.offset,
                input.element_type,
                index,
            )?;
            out.extend_from_slice(&value);
        }
    }
    Ok(out)
}

fn flat_to_u8(values: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(values.len() * 4);
    for value in values {
        out.extend_from_slice(&value.to_le_bytes());
    }
    out
}

fn multiply_matrices(a: &[f32; 16], b: &[f32; 16]) -> [f32; 16] {
    std::array::from_fn(|i| (0..4).map(|k| a[i / 4 * 4 + k] * b[k * 4 + i % 4]).sum())
}

fn transform_point(m: &[f32; 16], v: [f32; 4]) -> [f32; 4] {
    std::array::from_fn(|c| (0..4).map(|r| v[r] * m[r * 4 + c]).sum())
}

fn dot3(a: [f32; 3], b: [f32; 3]) -> f32 {
    (0..3).map(|i| a[i] * b[i]).sum()
}
fn normalize3(v: [f32; 3]) -> [f32; 3] {
    let length = dot3(v, v).sqrt();
    if length > 0.0 {
        v.map(|x| x / length)
    } else {
        [0.0; 3]
    }
}
fn color(value: u32) -> [f32; 4] {
    [
        ((value >> 16) & 255) as f32 / 255.0,
        ((value >> 8) & 255) as f32 / 255.0,
        (value & 255) as f32 / 255.0,
        (value >> 24) as f32 / 255.0,
    ]
}
fn rs(state: &D3d9State, id: u32) -> u32 {
    state.render_state(id).expect("validated render state")
}
fn tss(state: &D3d9State, stage: u32, id: u32) -> u32 {
    state
        .texture_stage_state(stage, id)
        .expect("validated texture stage")
}
fn ss(state: &D3d9State, stage: u32, id: u32) -> u32 {
    state.sampler_state(stage, id).expect("validated sampler")
}
fn inverse3(m: &[f32; 16]) -> Result<[[f32; 3]; 3], RenderError> {
    let (a, b, c, d, e, f, g, h, i) = (m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]);
    let det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
    if det == 0.0 || !det.is_finite() {
        return Err(unsupported(
            "Draw",
            "singular fixed-function normal transform",
        ));
    }
    Ok([
        [e * i - f * h, c * h - b * i, b * f - c * e],
        [f * g - d * i, a * i - c * g, c * d - a * f],
        [d * h - e * g, b * g - a * h, a * e - b * d],
    ]
    .map(|r| r.map(|x| x / det)))
}
fn ff_colors(
    state: &D3d9State,
    p: [f32; 4],
    n: [f32; 3],
    vertex: [Option<[f32; 4]>; 2],
    view: &[f32; 16],
) -> Result<[[f32; 4]; 2], RenderError> {
    if rs(state, 137) == 0 {
        return Ok([vertex[0].unwrap_or([1.0; 4]), vertex[1].unwrap_or([0.0; 4])]);
    }
    let material = state.material().map(f32::from_bits);
    let source = |offset: usize, id: u32| -> [f32; 4] {
        let selected = if rs(state, 141) != 0 {
            match rs(state, id) {
                1 => vertex[0],
                2 => vertex[1],
                _ => None,
            }
        } else {
            None
        };
        selected.unwrap_or_else(|| {
            material[offset..offset + 4]
                .try_into()
                .expect("material color")
        })
    };
    let diffuse = source(0, 145);
    let ambient = source(4, 147);
    let specular = source(8, 146);
    let emissive = source(12, 148);
    let global = color(rs(state, 139));
    let mut result: [[f32; 4]; 2] = [
        std::array::from_fn(|i| emissive[i] + ambient[i] * global[i]),
        [0.0; 4],
    ];
    let viewer = if rs(state, 142) != 0 {
        normalize3([-p[0], -p[1], -p[2]])
    } else {
        [0.0, 0.0, 1.0]
    };
    for raw in state.enabled_lights() {
        let light = raw.map(f32::from_bits);
        let direction = transform_point(view, [light[16], light[17], light[18], 0.0]);
        let (l, mut attenuation) = if raw[0] == 3 {
            (
                normalize3([-direction[0], -direction[1], -direction[2]]),
                1.0,
            )
        } else {
            let lp = transform_point(view, [light[13], light[14], light[15], 1.0]);
            let delta = [lp[0] - p[0], lp[1] - p[1], lp[2] - p[2]];
            let distance = dot3(delta, delta).sqrt();
            if distance > light[19] {
                continue;
            }
            let denominator = light[21] + light[22] * distance + light[23] * distance * distance;
            if denominator <= 0.0 {
                return Err(unsupported(
                    "Draw",
                    "non-positive light attenuation denominator",
                ));
            }
            (normalize3(delta), 1.0 / denominator)
        };
        if raw[0] == 2 {
            let rho = -dot3(l, normalize3([direction[0], direction[1], direction[2]]));
            let inner = (light[24] * 0.5).cos();
            let outer = (light[25] * 0.5).cos();
            attenuation *= if rho >= inner {
                1.0
            } else if rho <= outer {
                0.0
            } else {
                ((rho - outer) / (inner - outer)).powf(light[20])
            };
        }
        let ndotl = dot3(n, l).max(0.0);
        let highlight = if rs(state, 29) != 0 && ndotl > 0.0 {
            dot3(n, normalize3(std::array::from_fn(|i| l[i] + viewer[i])))
                .max(0.0)
                .powf(material[16])
        } else {
            0.0
        };
        for i in 0..3 {
            result[0][i] +=
                attenuation * (ambient[i] * light[9 + i] + diffuse[i] * light[1 + i] * ndotl);
            result[1][i] += attenuation * specular[i] * light[5 + i] * highlight;
        }
    }
    result[0][3] = diffuse[3];
    Ok(result.map(|v| v.map(|x| x.clamp(0.0, 1.0))))
}

fn ff_vertices(
    command: &DrawCommand,
    decl: &[VertexElement],
    order: &[u32],
    state: &D3d9State,
) -> Result<Vec<f32>, RenderError> {
    for id in [151, 152, 167] {
        if rs(state, id) != 0 {
            return Err(unsupported(
                "Draw",
                format!("fixed-function render state {id} is not implemented"),
            ));
        }
    }
    let find = |usage: u8, index: u8| {
        decl.iter()
            .find(|e| e.usage == usage && e.usage_index == index)
    };
    let position = find(9, 0)
        .or_else(|| find(0, 0))
        .ok_or_else(|| invalid("Draw", "fixed-function declaration has no position"))?;
    let transformed = position.usage == 9;
    let world = state.transform(256)?;
    let view = state.transform(2)?;
    let projection = state.transform(3)?;
    let worldview = multiply_matrices(&world, &view);
    let wvp = multiply_matrices(&worldview, &projection);
    let needs_normal = rs(state, 137) != 0
        || (0..8).any(|stage| matches!(tss(state, stage, 11) & 0xffff0000, 0x10000 | 0x30000));
    let normal_matrix = if !transformed && needs_normal {
        Some(inverse3(&worldview)?)
    } else {
        None
    };
    let vp = state.viewport();
    let mut out = Vec::new();
    out.try_reserve_exact(
        order
            .len()
            .checked_mul(48)
            .ok_or_else(|| invalid("Draw", "vertex allocation overflow"))?,
    )
    .map_err(|_| RenderError::out_of_memory("Draw", "fixed-function vertices"))?;
    for &index in order {
        let fetch =
            |e: &VertexElement| fetch_element(command, e.stream, e.offset, e.element_type, index);
        let p = fetch(position)?;
        let viewp = transform_point(&worldview, p);
        let clip = if transformed {
            if p[3] == 0.0 || !p[3].is_finite() {
                return Err(invalid("Draw", "invalid POSITIONT reciprocal w"));
            }
            let w = 1.0 / p[3];
            let dz = vp.max_z - vp.min_z;
            [
                ((p[0] - vp.x as f32) / vp.width as f32 * 2.0 - 1.0) * w,
                (1.0 - (p[1] - vp.y as f32) / vp.height as f32 * 2.0) * w,
                if dz == 0.0 {
                    0.0
                } else {
                    (p[2] - vp.min_z) / dz * w
                },
                w,
            ]
        } else {
            transform_point(&wvp, p)
        };
        let vertex = [
            find(10, 0).map(&fetch).transpose()?,
            find(10, 1).map(&fetch).transpose()?,
        ];
        let normal = if needs_normal {
            let n = find(3, 0).map(&fetch).transpose()?.unwrap_or([0.0; 4]);
            let normal = if let Some(inv) = normal_matrix {
                std::array::from_fn(|i| (0..3).map(|j| n[j] * inv[i][j]).sum())
            } else {
                [n[0], n[1], n[2]]
            };
            if rs(state, 143) != 0 {
                normalize3(normal)
            } else {
                normal
            }
        } else {
            [0.0; 3]
        };
        let colors = if transformed {
            [vertex[0].unwrap_or([1.0; 4]), vertex[1].unwrap_or([0.0; 4])]
        } else {
            ff_colors(state, viewp, normal, vertex, &view)?
        };
        out.extend_from_slice(&clip);
        out.extend_from_slice(&colors[0]);
        out.extend_from_slice(&colors[1]);
        for stage in 0..8 {
            let coord = tss(state, stage, 11);
            if transformed && coord & 0xffff0000 != 0 {
                return Err(unsupported(
                    "Draw",
                    "texture-coordinate generation with POSITIONT is not implemented",
                ));
            }
            let mut uv = match coord & 0xffff0000 {
                0 => find(5, (coord & 0xffff) as u8)
                    .map(&fetch)
                    .transpose()?
                    .unwrap_or([0.0, 0.0, 0.0, 1.0]),
                0x10000 => [normal[0], normal[1], normal[2], 1.0],
                0x20000 => viewp,
                0x30000 => {
                    let incident = normalize3([viewp[0], viewp[1], viewp[2]]);
                    let twice_dot = 2.0 * dot3(incident, normal);
                    [
                        incident[0] - twice_dot * normal[0],
                        incident[1] - twice_dot * normal[1],
                        incident[2] - twice_dot * normal[2],
                        1.0,
                    ]
                }
                _ => {
                    return Err(unsupported(
                        "Draw",
                        format!("texture-coordinate generation {coord:#x} is not implemented"),
                    ));
                }
            };
            let flags = tss(state, stage, 24);
            if flags & 255 != 0 {
                uv = transform_point(&state.transform(16 + stage)?, uv);
            }
            out.extend_from_slice(&uv);
        }
        let fog = if transformed {
            vertex[1].map(|v| v[3]).unwrap_or(1.0)
        } else {
            fog_factor(state, viewp[2].abs(), rs(state, 140))
        };
        out.extend_from_slice(&[fog, viewp[2], 0.0, 0.0]);
    }
    Ok(out)
}
fn fog_factor(state: &D3d9State, z: f32, mode: u32) -> f32 {
    let density = f32::from_bits(rs(state, 38));
    let start = f32::from_bits(rs(state, 36));
    let end = f32::from_bits(rs(state, 37));
    match mode {
        1 => (-density * z).exp(),
        2 => {
            let d = density * z;
            (-d * d).exp()
        }
        3 => ((end - z) / (end - start)).clamp(0.0, 1.0),
        _ => 1.0,
    }
}

const V2F: &str = "struct V2F { @builtin(position) pos: vec4<f32>, @location(0) d0: vec4<f32>, @location(1) d1: vec4<f32>, @location(2) t0: vec4<f32>, @location(3) t1: vec4<f32>, @location(4) t2: vec4<f32>, @location(5) t3: vec4<f32>, @location(6) t4: vec4<f32>, @location(7) t5: vec4<f32>, @location(8) t6: vec4<f32>, @location(9) t7: vec4<f32>, @location(10) fog: f32, @location(11) fogz: f32 }\n";
const OUTPUT: &str = r#"
fn d9_output(input:vec4<f32>,vin:V2F)->vec4<f32> {
    var col=input;
    let a=clamp(col.a,0.0,1.0); let r=psp.alpha_ref;
    var alpha_pass=true;
    switch psp.alpha_func {case 1:{alpha_pass=false;} case 2:{alpha_pass=a<r;} case 3:{alpha_pass=a==r;} case 4:{alpha_pass=a<=r;} case 5:{alpha_pass=a>r;} case 6:{alpha_pass=a!=r;} case 7:{alpha_pass=a>=r;} default:{} }
    if (!alpha_pass) {discard;}
    var f=vin.fog;
    let z=abs(vin.fogz);
    switch psp.fog_mode {case 1:{f=exp(-psp.fog_density*z);} case 2:{let d=psp.fog_density*z;f=exp(-d*d);} case 3:{f=(psp.fog_end-z)/(psp.fog_end-psp.fog_start);} default:{} }
    if (psp.pad0!=0) {col=vec4<f32>(mix(psp.fog_color.rgb,col.rgb,clamp(f,0.0,1.0)),col.a);}
    if (psp.pad1!=0.0) {let c=clamp(col.rgb,vec3<f32>(0.0),vec3<f32>(1.0)); col=vec4<f32>(select(1.055*pow(c,vec3<f32>(1.0/2.4))-0.055,12.92*c,c<=vec3<f32>(0.0031308)),col.a);}
    return col;
}
"#;
fn ff_vertex_source() -> String {
    let mut source = String::from(V2F);
    source += "struct VSP { halfpix:vec4<f32>,ascale:array<vec4<f32>,16> }\n@group(0) @binding(1) var<uniform> vsp:VSP;\nstruct FfIn {\n";
    for i in 0..12 {
        source += &format!("@location({i}) v{i}:vec4<f32>,\n");
    }
    source += "}\n@vertex fn vs_main(v:FfIn)->V2F {var o:V2F;o.pos=vec4<f32>(v.v0.xy+vsp.halfpix.xy*v.v0.w,v.v0.zw);o.d0=v.v1;o.d1=v.v2;\n";
    for i in 0..8 {
        source += &format!("o.t{i}=v.v{};\n", i + 3);
    }
    source += "o.fog=v.v11.x;o.fogz=v.v11.y;return o;}\n";
    source
}
fn ff_arg(value: u32, stage: u32) -> Result<String, RenderError> {
    let base = match value & 15 {
        0 => "vin.d0".to_string(),
        1 => "current".to_string(),
        2 => "tex".to_string(),
        3 => "tfactor".to_string(),
        4 => "vin.d1".to_string(),
        5 => "temp".to_string(),
        6 => format!("constant{stage}"),
        _ => return Err(unsupported("Draw", format!("D3DTA {value:#x}"))),
    };
    let base = if value & 32 != 0 {
        format!("vec4<f32>(({base}).a)")
    } else {
        base
    };
    Ok(if value & 16 != 0 {
        format!("(vec4<f32>(1.0)-{base})")
    } else {
        base
    })
}
fn ff_op(op: u32, a: &str, b: &str, c: &str) -> Result<String, RenderError> {
    Ok(match op {
        2 => a.into(),
        3 => b.into(),
        4 => format!("({a}*{b})"),
        5 => format!("(2.0*{a}*{b})"),
        6 => format!("(4.0*{a}*{b})"),
        7 => format!("({a}+{b})"),
        8 => format!("({a}+{b}-vec4<f32>(0.5))"),
        9 => format!("(2.0*({a}+{b}-vec4<f32>(0.5)))"),
        10 => format!("({a}-{b})"),
        11 => format!("({a}+{b}*(vec4<f32>(1.0)-{a}))"),
        12 => format!("mix({b},{a},vin.d0.a)"),
        13 => format!("mix({b},{a},tex.a)"),
        14 => format!("mix({b},{a},tfactor.a)"),
        15 => format!("({a}+{b}*(1.0-tex.a))"),
        16 => format!("mix({b},{a},current.a)"),
        18 => format!("({a}+{b}*({a}).a)"),
        19 => format!("({a}*{b}+vec4<f32>(({a}).a))"),
        20 => format!("({a}+{b}*(1.0-({a}).a))"),
        21 => format!("((vec4<f32>(1.0)-{a})*{b}+vec4<f32>(({a}).a))"),
        24 => format!("vec4<f32>(4.0*dot(({a}).rgb-vec3<f32>(0.5),({b}).rgb-vec3<f32>(0.5)))"),
        25 => format!("({a}*{b}+{c})"),
        26 => format!("mix({b},{a},{c})"),
        _ => {
            return Err(unsupported(
                "Draw",
                format!("D3DTOP {op} is not implemented"),
            ));
        }
    })
}
fn ff_fragment_source(state: &D3d9State, textures: &[BoundTexture]) -> Result<String, RenderError> {
    let mut source = String::from(V2F);
    source += "struct PSP {alpha_ref:f32,alpha_func:i32,fog_mode:i32,pad0:i32,fog_color:vec4<f32>,fog_start:f32,fog_end:f32,fog_density:f32,pad1:f32}\n@group(0) @binding(3) var<uniform> psp:PSP;\n";
    let mut body = format!(
        "var current=vin.d0;var temp=vec4<f32>(0.0);let tfactor=vec4<f32>({},{},{},{});\n",
        color(rs(state, 60))[0],
        color(rs(state, 60))[1],
        color(rs(state, 60))[2],
        color(rs(state, 60))[3]
    );
    for stage in 0..8 {
        let op = tss(state, stage, 1);
        if op == 1 {
            break;
        }
        let bound = textures.iter().find(|t| t.stage == stage);
        if bound.is_none() && tss(state, stage, 2) & 15 == 2 {
            break;
        }
        let alpha_op = tss(state, stage, 4);
        if alpha_op == 1 {
            return Err(unsupported(
                "Draw",
                "D3DTSS_ALPHAOP DISABLE with enabled COLOROP is undefined",
            ));
        }
        if matches!(alpha_op, 18..=23) {
            return Err(unsupported(
                "Draw",
                format!("D3DTSS_ALPHAOP {alpha_op} is not implemented"),
            ));
        }
        let needs = |operation: u32, first: u32, second: u32, third: u32| {
            matches!(operation, 13 | 15)
                || match operation {
                    1 => false,
                    2 => first & 15 == 2,
                    3 => second & 15 == 2,
                    25 | 26 => [first, second, third].iter().any(|v| v & 15 == 2),
                    _ => [first, second].iter().any(|v| v & 15 == 2),
                }
        };
        let alpha_arg1 = if bound.is_none() && tss(state, stage, 5) & 15 == 2 {
            tss(state, stage, 5) & !15
        } else {
            tss(state, stage, 5)
        };
        let needs_texture = needs(
            op,
            tss(state, stage, 2),
            tss(state, stage, 3),
            tss(state, stage, 26),
        ) || needs(
            alpha_op,
            alpha_arg1,
            tss(state, stage, 6),
            tss(state, stage, 27),
        );
        let texture = textures
            .iter()
            .find(|t| t.stage == stage)
            .filter(|_| needs_texture);
        if needs_texture && texture.is_none() {
            return Err(unsupported(
                "Draw",
                format!("fixed-function stage {stage} references an unbound texture"),
            ));
        }
        let constant = color(tss(state, stage, 32));
        body += &format!(
            "let constant{stage}=vec4<f32>({},{},{},{});\n",
            constant[0], constant[1], constant[2], constant[3]
        );
        body += "{\n";
        if let Some(t) = texture {
            if t.depth {
                return Err(unsupported(
                    "Draw",
                    "fixed-function depth sampling is not implemented",
                ));
            }
            let dimension = if t.cube {
                "cube"
            } else if t.volume {
                "3d"
            } else {
                "2d"
            };
            source += &format!(
                "@group(0) @binding({}) var tx{stage}:texture_{dimension}<f32>;\n@group(0) @binding({}) var sm{stage}:sampler;\n",
                4 + stage,
                20 + stage
            );
            let flags = tss(state, stage, 24);
            let mut coord = format!("vin.t{stage}");
            if flags & 256 != 0 {
                let component = match flags & 255 {
                    2 => "y",
                    3 => "z",
                    4 => "w",
                    _ => {
                        return Err(unsupported(
                            "Draw",
                            "invalid projected texture coordinate count",
                        ));
                    }
                };
                coord = format!("({coord}/{coord}.{component})");
            }
            let sample = format!(
                "textureSample(tx{stage},sm{stage},({coord}).{})",
                if t.cube || t.volume { "xyz" } else { "xy" }
            );
            body += &format!(
                "let tex={};\n",
                if opaque_format(t.format) {
                    format!("vec4<f32>(({sample}).rgb,1.0)")
                } else {
                    sample
                }
            );
        } else {
            body += "let tex=vec4<f32>(1.0);\n";
        }
        let a = ff_arg(tss(state, stage, 2), stage)?;
        let b = ff_arg(tss(state, stage, 3), stage)?;
        let c = ff_arg(tss(state, stage, 26), stage)?;
        let rgb = ff_op(op, &a, &b, &c)?;
        let a = ff_arg(alpha_arg1, stage)?;
        let b = ff_arg(tss(state, stage, 6), stage)?;
        let c = ff_arg(tss(state, stage, 27), stage)?;
        let alpha = if tss(state, stage, 4) == 1 {
            "current.a".into()
        } else {
            format!("({}).a", ff_op(tss(state, stage, 4), &a, &b, &c)?)
        };
        let result = if tss(state, stage, 28) == 5 {
            "temp"
        } else {
            "current"
        };
        body += &format!(
            "{result}=clamp(vec4<f32>(({rgb}).rgb,{alpha}),vec4<f32>(0.0),vec4<f32>(1.0));\n}}\n"
        );
    }
    if rs(state, 29) != 0 {
        body += "current=vec4<f32>(clamp(current.rgb+vin.d1.rgb,vec3<f32>(0.0),vec3<f32>(1.0)),current.a);\n";
    }
    source += OUTPUT;
    source += &format!(
        "@fragment fn ps_main(vin:V2F)->@location(0) vec4<f32>{{{body}return d9_output(current,vin);}}\n"
    );
    Ok(source)
}

fn compare(value: u32) -> wgpu::CompareFunction {
    match value {
        1 => wgpu::CompareFunction::Never,
        2 => wgpu::CompareFunction::Less,
        3 => wgpu::CompareFunction::Equal,
        4 => wgpu::CompareFunction::LessEqual,
        5 => wgpu::CompareFunction::Greater,
        6 => wgpu::CompareFunction::NotEqual,
        7 => wgpu::CompareFunction::GreaterEqual,
        _ => wgpu::CompareFunction::Always,
    }
}
fn stencil_op(value: u32) -> wgpu::StencilOperation {
    match value {
        1 => wgpu::StencilOperation::Keep,
        2 => wgpu::StencilOperation::Zero,
        3 => wgpu::StencilOperation::Replace,
        4 => wgpu::StencilOperation::IncrementClamp,
        5 => wgpu::StencilOperation::DecrementClamp,
        6 => wgpu::StencilOperation::Invert,
        7 => wgpu::StencilOperation::IncrementWrap,
        _ => wgpu::StencilOperation::DecrementWrap,
    }
}
fn depth_state(
    state: &D3d9State,
    depth: Option<&SurfaceView>,
) -> Result<Option<wgpu::DepthStencilState>, RenderError> {
    let Some(depth) = depth else {
        if rs(state, 7) != 0 || rs(state, 52) != 0 {
            return Err(invalid(
                "Draw",
                "depth or stencil enabled without a depth surface",
            ));
        }
        return Ok(None);
    };
    if rs(state, 7) == 2 {
        return Err(unsupported("Draw", "W-buffer depth is not implemented"));
    }
    if rs(state, 52) != 0 && !super::format::has_stencil(depth.format) {
        return Err(invalid(
            "Draw",
            "stencil enabled on a format without stencil",
        ));
    }
    let face = |fail, zfail, pass, func| wgpu::StencilFaceState {
        compare: compare(rs(state, func)),
        fail_op: stencil_op(rs(state, fail)),
        depth_fail_op: stencil_op(rs(state, zfail)),
        pass_op: stencil_op(rs(state, pass)),
    };
    let stencil = if rs(state, 52) != 0 {
        wgpu::StencilState {
            front: face(53, 54, 55, 56),
            back: if rs(state, 185) != 0 {
                face(186, 187, 188, 189)
            } else {
                face(53, 54, 55, 56)
            },
            read_mask: rs(state, 58),
            write_mask: rs(state, 59),
        }
    } else {
        wgpu::StencilState::default()
    };
    let bias = f32::from_bits(rs(state, 195));
    if bias != 0.0 {
        return Err(unsupported(
            "Draw",
            "constant depth bias conversion is deferred",
        ));
    }
    Ok(Some(wgpu::DepthStencilState {
        format: depth.texture.format(),
        depth_write_enabled: rs(state, 7) != 0 && rs(state, 14) != 0,
        depth_compare: if rs(state, 7) != 0 {
            compare(rs(state, 23))
        } else {
            wgpu::CompareFunction::Always
        },
        stencil,
        bias: wgpu::DepthBiasState {
            constant: 0,
            slope_scale: f32::from_bits(rs(state, 175)),
            clamp: 0.0,
        },
    }))
}
fn blend_factor(v: u32, alpha: bool) -> Result<wgpu::BlendFactor, RenderError> {
    Ok(match v {
        1 => wgpu::BlendFactor::Zero,
        2 => wgpu::BlendFactor::One,
        3 => {
            if alpha {
                wgpu::BlendFactor::SrcAlpha
            } else {
                wgpu::BlendFactor::Src
            }
        }
        4 => {
            if alpha {
                wgpu::BlendFactor::OneMinusSrcAlpha
            } else {
                wgpu::BlendFactor::OneMinusSrc
            }
        }
        5 => wgpu::BlendFactor::SrcAlpha,
        6 => wgpu::BlendFactor::OneMinusSrcAlpha,
        7 => wgpu::BlendFactor::DstAlpha,
        8 => wgpu::BlendFactor::OneMinusDstAlpha,
        9 => {
            if alpha {
                wgpu::BlendFactor::DstAlpha
            } else {
                wgpu::BlendFactor::Dst
            }
        }
        10 => {
            if alpha {
                wgpu::BlendFactor::OneMinusDstAlpha
            } else {
                wgpu::BlendFactor::OneMinusDst
            }
        }
        11 => {
            if alpha {
                wgpu::BlendFactor::One
            } else {
                wgpu::BlendFactor::SrcAlphaSaturated
            }
        }
        14 => wgpu::BlendFactor::Constant,
        15 => wgpu::BlendFactor::OneMinusConstant,
        _ => {
            return Err(unsupported(
                "Draw",
                format!("D3DBLEND {v} is not implemented"),
            ));
        }
    })
}
fn blend_op(v: u32) -> wgpu::BlendOperation {
    match v {
        1 => wgpu::BlendOperation::Add,
        2 => wgpu::BlendOperation::Subtract,
        3 => wgpu::BlendOperation::ReverseSubtract,
        4 => wgpu::BlendOperation::Min,
        _ => wgpu::BlendOperation::Max,
    }
}
fn blend_state(state: &D3d9State, opaque: bool) -> Result<Option<wgpu::BlendState>, RenderError> {
    if rs(state, 27) == 0 {
        return Ok(None);
    }
    let component = |src, dst, op, alpha| -> Result<wgpu::BlendComponent, RenderError> {
        let (s, d) = match rs(state, src) {
            12 => (5, 6),
            13 => (6, 5),
            v => (v, rs(state, dst)),
        };
        Ok(wgpu::BlendComponent {
            src_factor: if opaque && s == 7 {
                wgpu::BlendFactor::One
            } else if opaque && s == 8 {
                wgpu::BlendFactor::Zero
            } else {
                blend_factor(s, alpha)?
            },
            dst_factor: if opaque && d == 7 {
                wgpu::BlendFactor::One
            } else if opaque && d == 8 {
                wgpu::BlendFactor::Zero
            } else {
                blend_factor(d, alpha)?
            },
            operation: blend_op(rs(state, op)),
        })
    };
    Ok(Some(wgpu::BlendState {
        color: component(19, 20, 171, false)?,
        alpha: if rs(state, 206) != 0 {
            component(207, 208, 209, true)?
        } else {
            component(19, 20, 171, true)?
        },
    }))
}
fn sampler(
    state: &D3d9State,
    t: &BoundTexture,
) -> Result<wgpu::SamplerDescriptor<'static>, RenderError> {
    let address = |id| -> Result<wgpu::AddressMode, RenderError> {
        Ok(match ss(state, t.stage, id) {
            1 => wgpu::AddressMode::Repeat,
            2 => wgpu::AddressMode::MirrorRepeat,
            3 => wgpu::AddressMode::ClampToEdge,
            v => {
                return Err(unsupported(
                    "Draw",
                    format!("sampler {} address mode {v} is not implemented", t.stage),
                ));
            }
        })
    };
    let filter = |id| -> Result<wgpu::FilterMode, RenderError> {
        Ok(match ss(state, t.stage, id) {
            0 | 1 => wgpu::FilterMode::Nearest,
            2 => wgpu::FilterMode::Linear,
            v => {
                return Err(unsupported(
                    "Draw",
                    format!("sampler {} filter {v} is not implemented", t.stage),
                ));
            }
        })
    };
    if ss(state, t.stage, 8) != 0 {
        return Err(unsupported("Draw", "sampler LOD bias is not implemented"));
    }
    if ss(state, t.stage, 11) != 0 {
        return Err(unsupported(
            "Draw",
            "sRGB texture sampling views are not implemented",
        ));
    }
    Ok(wgpu::SamplerDescriptor {
        label: Some("d3d9-sampler"),
        address_mode_u: address(1)?,
        address_mode_v: address(2)?,
        address_mode_w: address(3)?,
        mag_filter: filter(5)?,
        min_filter: filter(6)?,
        mipmap_filter: filter(7)?,
        lod_min_clamp: ss(state, t.stage, 9)
            .max(t.lod)
            .min(t.levels.saturating_sub(1)) as f32,
        lod_max_clamp: if ss(state, t.stage, 7) == 0 {
            ss(state, t.stage, 9)
                .max(t.lod)
                .min(t.levels.saturating_sub(1)) as f32
        } else {
            t.levels.saturating_sub(1) as f32
        },
        compare: if t.depth {
            Some(wgpu::CompareFunction::LessEqual)
        } else {
            None
        },
        ..Default::default()
    })
}
fn make_buffer(
    gpu: &D3d9Gpu,
    label: &'static str,
    bytes: &[u8],
    usage: wgpu::BufferUsages,
) -> wgpu::Buffer {
    use wgpu::util::DeviceExt;
    gpu.device
        .create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(label),
            contents: if bytes.is_empty() { &[0; 16] } else { bytes },
            usage,
        })
}
#[derive(Clone, PartialEq, Eq, Hash)]
struct PipelineKey {
    modules: [wgpu::ShaderModule; 2],
    layout: wgpu::BindGroupLayout,
    attributes: Vec<wgpu::VertexAttribute>,
    target: wgpu::ColorTargetState,
    primitive: wgpu::PrimitiveState,
    depth: Option<wgpu::DepthStencilState>,
    multisample: wgpu::MultisampleState,
}

#[derive(Default)]
pub(crate) struct D3d9DrawState {
    shaders: HashMap<String, wgpu::ShaderModule>,
    layouts: HashMap<Vec<wgpu::BindGroupLayoutEntry>, wgpu::BindGroupLayout>,
    pipelines: HashMap<PipelineKey, wgpu::RenderPipeline>,
}
impl D3d9DrawState {
    pub fn clear(&mut self) {
        self.pipelines.clear();
        self.layouts.clear();
        self.shaders.clear();
    }
    pub fn drop_resource(&mut self, _id: u32) {}

    fn shader(&mut self, gpu: &D3d9Gpu, source: String) -> Result<wgpu::ShaderModule, RenderError> {
        if let Some(shader) = self.shaders.get(&source) {
            return Ok(shader.clone());
        }
        wgsl::validate(&source)?;
        let shader = gpu
            .device
            .create_shader_module(wgpu::ShaderModuleDescriptor {
                label: Some("d3d9-cached"),
                source: wgpu::ShaderSource::Wgsl(Cow::Borrowed(&source)),
            });
        if self.shaders.len() >= 512 {
            self.clear();
        }
        self.shaders.insert(source, shader.clone());
        Ok(shader)
    }

    fn layout(
        &mut self,
        gpu: &D3d9Gpu,
        entries: Vec<wgpu::BindGroupLayoutEntry>,
    ) -> wgpu::BindGroupLayout {
        if let Some(layout) = self.layouts.get(&entries) {
            return layout.clone();
        }
        let layout = gpu
            .device
            .create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                label: Some("d3d9-cached"),
                entries: &entries,
            });
        if self.layouts.len() >= 128 {
            self.pipelines.clear();
            self.layouts.clear();
        }
        self.layouts.insert(entries, layout.clone());
        layout
    }

    fn pipeline(&mut self, gpu: &D3d9Gpu, key: PipelineKey) -> wgpu::RenderPipeline {
        if let Some(pipeline) = self.pipelines.get(&key) {
            return pipeline.clone();
        }
        let layout = gpu
            .device
            .create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("d3d9-cached"),
                bind_group_layouts: &[&key.layout],
                push_constant_ranges: &[],
            });
        let vertex_layout = wgpu::VertexBufferLayout {
            array_stride: (key.attributes.len() * 16) as u64,
            step_mode: wgpu::VertexStepMode::Vertex,
            attributes: &key.attributes,
        };
        let buffers = if key.attributes.is_empty() {
            Vec::new()
        } else {
            vec![vertex_layout]
        };
        let pipeline = gpu
            .device
            .create_render_pipeline(&wgpu::RenderPipelineDescriptor {
                label: Some("d3d9-cached"),
                layout: Some(&layout),
                vertex: wgpu::VertexState {
                    module: &key.modules[0],
                    entry_point: Some("vs_main"),
                    buffers: &buffers,
                    compilation_options: Default::default(),
                },
                fragment: Some(wgpu::FragmentState {
                    module: &key.modules[1],
                    entry_point: Some("ps_main"),
                    targets: &[Some(key.target.clone())],
                    compilation_options: Default::default(),
                }),
                primitive: key.primitive,
                depth_stencil: key.depth.clone(),
                multisample: key.multisample,
                multiview: None,
                cache: None,
            });
        if self.pipelines.len() >= 512 {
            self.pipelines.clear();
        }
        self.pipelines.insert(key, pipeline.clone());
        pipeline
    }
}
fn texture_dimension(t: &BoundTexture) -> wgpu::TextureViewDimension {
    if t.cube {
        wgpu::TextureViewDimension::Cube
    } else if t.volume {
        wgpu::TextureViewDimension::D3
    } else {
        wgpu::TextureViewDimension::D2
    }
}
fn opaque_format(format: u32) -> bool {
    matches!(format, 20 | 22 | 23 | 24 | 27 | 30 | 33 | 50)
}
fn opaque_samples(mut source: String, textures: &[BoundTexture]) -> String {
    for texture in textures.iter().filter(|t| opaque_format(t.format)) {
        for operation in ["textureSample", "textureSampleBias"] {
            let needle = format!("{operation}(tx{},", texture.stage);
            let mut start = 0;
            while let Some(at) = source[start..].find(&needle) {
                let at = start + at;
                let mut depth = 0;
                let mut end = at;
                for (offset, ch) in source[at..].char_indices() {
                    if ch == '(' {
                        depth += 1;
                    } else if ch == ')' {
                        depth -= 1;
                        if depth == 0 {
                            end = at + offset + 1;
                            break;
                        }
                    }
                }
                if end <= at {
                    break;
                }
                let replacement = format!("vec4<f32>(({}).rgb,1.0)", &source[at..end]);
                source.replace_range(at..end, &replacement);
                start = at + replacement.len();
            }
        }
    }
    source
}
fn translated_source(
    program: &wgsl::Program,
    state: &D3d9State,
    textures: &[BoundTexture],
) -> Result<String, RenderError> {
    let mut variant = wgsl::Variant::default();
    for stage in 0..16 {
        if state.texture(stage)? == 0 {
            variant.unbound |= 1 << stage;
        }
    }
    for t in textures {
        if t.cube {
            variant.cube |= 1 << t.stage;
        }
        if t.volume {
            variant.volume |= 1 << t.stage;
        }
        if t.depth {
            if t.cube || t.volume {
                return Err(unsupported("Draw", "cube/volume depth sampling"));
            }
            variant.depth |= 1 << t.stage;
        }
        if program.pixel && program.major == 1 && t.stage < 8 && tss(state, t.stage, 24) & 256 != 0
        {
            variant.projected |= 1 << t.stage;
        }
        if let Some(kind) = program.samplers.get(&t.stage) {
            if (*kind == 3) != t.cube || (*kind == 4) != t.volume {
                return Err(invalid(
                    "Draw",
                    format!("sampler {} texture dimension mismatch", t.stage),
                ));
            }
        }
    }
    let mut source = wgsl::translate(program, &variant)?;
    if program.pixel {
        source = opaque_samples(source, textures);
        source = source.replace("return col;", "return d9_output(col,vin);");
        source += OUTPUT;
    }
    Ok(source)
}
fn shading_source(mut source: String, state: &D3d9State) -> String {
    if rs(state, 9) == 1 {
        for (location, name) in [(0, "d0"), (1, "d1")] {
            source = source.replace(
                &format!("@location({location}) {name}:"),
                &format!("@location({location}) @interpolate(flat, first) {name}:"),
            );
        }
    }
    source
}
fn uniform_entry(
    binding: u32,
    size: u64,
    visibility: wgpu::ShaderStages,
) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding,
        visibility,
        ty: wgpu::BindingType::Buffer {
            ty: wgpu::BufferBindingType::Uniform,
            has_dynamic_offset: false,
            min_binding_size: wgpu::BufferSize::new(size),
        },
        count: None,
    }
}

pub(crate) fn execute(
    gpu: &D3d9Gpu,
    draw: &mut D3d9DrawState,
    state: &D3d9State,
    objects: &super::shader::D3d9Objects,
    snapshot: DrawSnapshot,
) -> Result<(), RenderError> {
    if snapshot.command.primitive_count == 0 {
        return Ok(());
    }
    let (order, _, topology) = expanded_indices(&snapshot.command)?;
    if !matches!(rs(state, 9), 1 | 2) {
        return Err(unsupported(
            "Draw",
            format!("D3DRS_SHADEMODE value {} is not implemented", rs(state, 9)),
        ));
    }
    for (id, default) in [
        (8, 3),
        (48, 0),
        (136, 1),
        (163, 0),
        (170, 0),
        (178, 1.0f32.to_bits()),
        (179, 1.0f32.to_bits()),
        (180, 0),
        (181, 0),
        (183, 0),
        (184, 0),
    ] {
        if rs(state, id) != default {
            return Err(unsupported(
                "Draw",
                format!(
                    "render state {id} value {} is not implemented",
                    rs(state, id)
                ),
            ));
        }
    }
    for id in (128..136).chain(198..206) {
        if rs(state, id) != 0 {
            return Err(unsupported(
                "Draw",
                format!("texture coordinate wrap render state {id}"),
            ));
        }
    }
    let declaration = objects.declaration(state.declaration())?;
    let vs = if state.shader(false) == 0 {
        None
    } else {
        Some(objects.shader(false, state.shader(false))?)
    };
    let ps = if state.shader(true) == 0 {
        None
    } else {
        Some(objects.shader(true, state.shader(true))?)
    };
    let linked = vs
        .map(|v| link_inputs(declaration, &v.program))
        .transpose()?;
    let vertices = if let Some(linked) = &linked {
        canonical_shader_vertices(&snapshot.command, linked, &order)?
    } else {
        ff_vertices(&snapshot.command, declaration, &order, state)?
    };
    if super::trace_enabled() {
        static COUNT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let count = COUNT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        if count < super::trace_draw_limit() && (count < 24 || count % 61 == 0) {
            eprintln!("[d3d9-trace] indices={:?}", &order[..order.len().min(12)]);
            for stream in &snapshot.command.streams {
                eprintln!(
                    "[d3d9-trace] stream={} offset={} stride={} sample={:?}",
                    stream.stream,
                    stream.offset,
                    stream.stride,
                    stream
                        .bytes
                        .get(
                            stream.offset as usize
                                ..stream.bytes.len().min(stream.offset as usize + 96)
                        )
                        .unwrap_or(&[])
                );
            }
            eprintln!(
                "[d3d9-trace] draw={} target={}x{} format={} viewport={:?} vs={} ps={} declaration={:?}",
                count,
                snapshot.target.width,
                snapshot.target.height,
                snapshot.target.format,
                [
                    state.viewport().x as f32,
                    state.viewport().y as f32,
                    state.viewport().width as f32,
                    state.viewport().height as f32,
                    state.viewport().min_z,
                    state.viewport().max_z
                ],
                state.shader(false),
                state.shader(true),
                declaration
            );
            for id in [7, 14, 15, 19, 20, 22, 23, 24, 25, 27, 137, 162, 168, 174] {
                eprintln!("[d3d9-trace] rs[{id}]={}", rs(state, id));
            }
            for texture in &snapshot.textures {
                eprintln!(
                    "[d3d9-trace] stage={} id={} size={}x{} format={} lod={} color_op={} color_args={}/{} alpha_op={} alpha_args={}/{}",
                    texture.stage,
                    state.texture(texture.stage)?,
                    texture.width,
                    texture.height,
                    texture.format,
                    texture.lod,
                    tss(state, texture.stage, 1),
                    tss(state, texture.stage, 2),
                    tss(state, texture.stage, 3),
                    tss(state, texture.stage, 4),
                    tss(state, texture.stage, 5),
                    tss(state, texture.stage, 6)
                );
            }
            let stride = linked.as_ref().map_or(48, |inputs| inputs.len() * 4);
            for vertex in vertices.chunks(stride.max(1)).take(6) {
                eprintln!("[d3d9-trace] vertex={vertex:?}");
            }
        }
    }
    let vertex_source = shading_source(
        if let Some(v) = vs {
            translated_source(&v.program, state, &snapshot.textures)?
        } else {
            ff_vertex_source()
        },
        state,
    );
    let pixel_source = shading_source(
        if let Some(p) = ps {
            translated_source(&p.program, state, &snapshot.textures)?
        } else {
            ff_fragment_source(state, &snapshot.textures)?
        },
        state,
    );
    if vs.is_some() && rs(state, 28) != 0 && rs(state, 35) != 0 {
        return Err(unsupported(
            "Draw",
            "D3DRS_FOGTABLEMODE with translated vertex shaders is deferred",
        ));
    }
    if rs(state, 152) != 0 {
        return Err(unsupported(
            "Draw",
            "D3DRS_CLIPPLANEENABLE is not implemented",
        ));
    }
    if snapshot.command.primitive == 1
        && (rs(state, 156) != 0 || rs(state, 157) != 0 || f32::from_bits(rs(state, 154)) != 1.0)
    {
        return Err(unsupported(
            "Draw",
            "point size/sprite/scaling state is not implemented",
        ));
    }
    for stage in 0..16 {
        if pixel_source.contains(&format!("var tx{stage}:"))
            && !snapshot.textures.iter().any(|t| t.stage == stage)
        {
            return Err(unsupported(
                "Draw",
                format!("shader samples unbound texture stage {stage}"),
            ));
        }
    }
    let textures: Vec<&BoundTexture> = snapshot
        .textures
        .iter()
        .filter(|t| pixel_source.contains(&format!("var tx{}:", t.stage)))
        .collect();
    let samplers: Vec<wgpu::Sampler> = textures
        .iter()
        .map(|t| sampler(state, t).map(|d| gpu.device.create_sampler(&d)))
        .collect::<Result<_, _>>()?;
    let vs_count = vs.map(|v| v.program.const_limit()).unwrap_or(1);
    let ps_count = ps.map(|p| p.program.const_limit()).unwrap_or(1);
    let mut layout_entries = vec![
        uniform_entry(0, u64::from(vs_count) * 16, wgpu::ShaderStages::VERTEX),
        uniform_entry(1, 272, wgpu::ShaderStages::VERTEX),
        uniform_entry(2, u64::from(ps_count) * 16, wgpu::ShaderStages::FRAGMENT),
        uniform_entry(3, 48, wgpu::ShaderStages::FRAGMENT),
    ];
    for t in &textures {
        let format = super::format::d3d_to_wgpu(t.format)
            .ok_or_else(|| unsupported("Draw", "texture format"))?;
        let filterable = format
            .guaranteed_format_features(gpu.features())
            .flags
            .contains(wgpu::TextureFormatFeatureFlags::FILTERABLE);
        if !t.depth && !filterable && [5, 6, 7].iter().any(|&id| ss(state, t.stage, id) == 2) {
            return Err(unsupported(
                "Draw",
                format!("non-filterable sampler format {}", t.format),
            ));
        }
        layout_entries.push(wgpu::BindGroupLayoutEntry {
            binding: 4 + t.stage,
            visibility: wgpu::ShaderStages::FRAGMENT,
            ty: wgpu::BindingType::Texture {
                sample_type: if t.depth {
                    wgpu::TextureSampleType::Depth
                } else {
                    wgpu::TextureSampleType::Float { filterable }
                },
                view_dimension: texture_dimension(t),
                multisampled: false,
            },
            count: None,
        });
        layout_entries.push(wgpu::BindGroupLayoutEntry {
            binding: 20 + t.stage,
            visibility: wgpu::ShaderStages::FRAGMENT,
            ty: wgpu::BindingType::Sampler(if t.depth {
                wgpu::SamplerBindingType::Comparison
            } else {
                wgpu::SamplerBindingType::Filtering
            }),
            count: None,
        });
    }
    let layout = draw.layout(gpu, layout_entries);
    let vp = state.viewport();
    if vp.width == 0 || vp.height == 0 || vp.min_z < 0.0 || vp.max_z > 1.0 || vp.min_z > vp.max_z {
        return Err(unsupported(
            "Draw",
            "viewport depth range cannot be represented by wgpu",
        ));
    }
    let mut vsp = vec![1.0 / vp.width as f32, -1.0 / vp.height as f32, 0.0, 0.0];
    for _ in 0..16 {
        vsp.extend_from_slice(&[1.0; 4]);
    }
    let mut psp = Vec::new();
    psp.extend_from_slice(&(rs(state, 24).min(255) as f32 / 255.0).to_le_bytes());
    psp.extend_from_slice(&(if rs(state, 15) != 0 { rs(state, 25) } else { 8 }).to_le_bytes());
    psp.extend_from_slice(&rs(state, 35).to_le_bytes());
    psp.extend_from_slice(&rs(state, 28).to_le_bytes());
    psp.extend_from_slice(&flat_to_u8(&color(rs(state, 34))));
    for id in [36, 37, 38] {
        psp.extend_from_slice(&rs(state, id).to_le_bytes());
    }
    psp.extend_from_slice(&(if rs(state, 194) != 0 { 1.0f32 } else { 0.0f32 }).to_le_bytes());
    let buffers = [
        make_buffer(
            gpu,
            "d3d9-vs-constants",
            &flat_to_u8(&state.constants_f(false, 0, vs_count)?),
            wgpu::BufferUsages::UNIFORM,
        ),
        make_buffer(
            gpu,
            "d3d9-vsp",
            &flat_to_u8(&vsp),
            wgpu::BufferUsages::UNIFORM,
        ),
        make_buffer(
            gpu,
            "d3d9-ps-constants",
            &flat_to_u8(&state.constants_f(true, 0, ps_count)?),
            wgpu::BufferUsages::UNIFORM,
        ),
        make_buffer(gpu, "d3d9-psp", &psp, wgpu::BufferUsages::UNIFORM),
    ];
    let mut entries: Vec<_> = buffers
        .iter()
        .enumerate()
        .map(|(i, b)| wgpu::BindGroupEntry {
            binding: i as u32,
            resource: b.as_entire_binding(),
        })
        .collect();
    for (t, s) in textures.iter().zip(&samplers) {
        entries.push(wgpu::BindGroupEntry {
            binding: 4 + t.stage,
            resource: wgpu::BindingResource::TextureView(&t.view),
        });
        entries.push(wgpu::BindGroupEntry {
            binding: 20 + t.stage,
            resource: wgpu::BindingResource::Sampler(s),
        });
    }
    let bind = gpu.device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("d3d9-draw"),
        layout: &layout,
        entries: &entries,
    });
    let modules = [
        draw.shader(gpu, vertex_source)?,
        draw.shader(gpu, pixel_source)?,
    ];
    let attributes: Vec<_> = if let Some(linked) = &linked {
        linked
            .iter()
            .enumerate()
            .map(|(i, l)| wgpu::VertexAttribute {
                format: wgpu::VertexFormat::Float32x4,
                offset: (i * 16) as u64,
                shader_location: l.location,
            })
            .collect()
    } else {
        (0..12)
            .map(|i| wgpu::VertexAttribute {
                format: wgpu::VertexFormat::Float32x4,
                offset: u64::from(i) * 16,
                shader_location: i,
            })
            .collect()
    };
    let has_attributes = !attributes.is_empty();
    let cull = match rs(state, 22) {
        1 => None,
        2 => Some(wgpu::Face::Front),
        _ => Some(wgpu::Face::Back),
    };
    let target = wgpu::ColorTargetState {
        format: snapshot.target.texture.format(),
        blend: blend_state(state, opaque_format(snapshot.target.format))?,
        write_mask: wgpu::ColorWrites::from_bits_truncate(rs(state, 168)),
    };
    let pipeline = draw.pipeline(
        gpu,
        PipelineKey {
            modules,
            layout,
            attributes,
            target,
            primitive: wgpu::PrimitiveState {
                topology,
                front_face: wgpu::FrontFace::Cw,
                cull_mode: cull,
                ..Default::default()
            },
            depth: depth_state(state, snapshot.depth.as_ref())?,
            multisample: wgpu::MultisampleState {
                mask: u64::from(rs(state, 162)),
                ..Default::default()
            },
        },
    );
    let vertex = make_buffer(
        gpu,
        "d3d9-vertices",
        &flat_to_u8(&vertices),
        wgpu::BufferUsages::VERTEX,
    );
    let mut encoder = gpu
        .device
        .create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("d3d9-draw"),
        });
    let colors = [Some(wgpu::RenderPassColorAttachment {
        view: &snapshot.target.view,
        resolve_target: None,
        depth_slice: None,
        ops: wgpu::Operations {
            load: wgpu::LoadOp::Load,
            store: wgpu::StoreOp::Store,
        },
    })];
    let depth = snapshot
        .depth
        .as_ref()
        .map(|d| wgpu::RenderPassDepthStencilAttachment {
            view: &d.view,
            depth_ops: Some(wgpu::Operations {
                load: wgpu::LoadOp::Load,
                store: wgpu::StoreOp::Store,
            }),
            stencil_ops: if super::format::has_stencil(d.format) {
                Some(wgpu::Operations {
                    load: wgpu::LoadOp::Load,
                    store: wgpu::StoreOp::Store,
                })
            } else {
                None
            },
        });
    {
        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
            label: Some("d3d9-draw"),
            color_attachments: &colors,
            depth_stencil_attachment: depth,
            timestamp_writes: None,
            occlusion_query_set: None,
        });
        pass.set_pipeline(&pipeline);
        pass.set_bind_group(0, &bind, &[]);
        if has_attributes {
            pass.set_vertex_buffer(0, vertex.slice(..));
        }
        pass.set_viewport(
            vp.x as f32,
            vp.y as f32,
            vp.width as f32,
            vp.height as f32,
            vp.min_z,
            vp.max_z,
        );
        pass.set_stencil_reference(rs(state, 57));
        let blend = color(rs(state, 193));
        pass.set_blend_constant(wgpu::Color {
            r: blend[0] as f64,
            g: blend[1] as f64,
            b: blend[2] as f64,
            a: blend[3] as f64,
        });
        if rs(state, 174) != 0 {
            let s = state.scissor();
            let l = s.left.clamp(0, snapshot.target.width as i32) as u32;
            let t = s.top.clamp(0, snapshot.target.height as i32) as u32;
            let r = s.right.clamp(0, snapshot.target.width as i32) as u32;
            let b = s.bottom.clamp(0, snapshot.target.height as i32) as u32;
            pass.set_scissor_rect(l, t, r.saturating_sub(l), b.saturating_sub(t));
        }
        pass.draw(0..order.len() as u32, 0..1);
    }
    gpu.queue.submit(std::iter::once(encoder.finish()));
    if super::trace_enabled() {
        static COUNT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let count = COUNT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        if count < super::trace_draw_limit()
            && (count < 24 || count % 61 == 0)
            && matches!(snapshot.target.format, 21 | 22)
        {
            let mut pixels =
                vec![0; snapshot.target.width as usize * snapshot.target.height as usize * 4];
            match gpu.read_texture(
                &snapshot.target.texture,
                snapshot.target.format,
                snapshot.target.subresource.1,
                snapshot.target.width,
                snapshot.target.height,
                1,
                snapshot.target.subresource.0,
                false,
                &mut pixels,
            ) {
                Ok(()) => eprintln!(
                    "[d3d9-trace] draw={} target_colored_pixels={}",
                    count,
                    pixels
                        .chunks_exact(4)
                        .filter(|p| p[..3].iter().any(|&b| b != 0))
                        .count()
                ),
                Err(error) => eprintln!("[d3d9-trace] draw={count} readback={error}"),
            }
        }
    }
    Ok(())
}

pub(crate) fn clear(
    gpu: &D3d9Gpu,
    draw: &mut D3d9DrawState,
    target: &SurfaceView,
    depth: Option<&SurfaceView>,
    flags: u32,
    color_value: u32,
    z: f32,
    stencil: u32,
    rects: &[TargetRect],
) -> Result<(), RenderError> {
    if flags == 0 || flags & !7 != 0 {
        return Err(invalid(
            "Clear",
            format!("invalid D3DCLEAR flags {flags:#x}"),
        ));
    }
    if flags & 6 != 0 && depth.is_none() {
        return Err(invalid(
            "Clear",
            "depth/stencil clear without a depth target",
        ));
    }
    if flags & 4 != 0 && !depth.is_some_and(|d| super::format::has_stencil(d.format)) {
        return Err(invalid(
            "Clear",
            "stencil clear on a format without stencil",
        ));
    }
    if flags & 2 != 0 && !(0.0..=1.0).contains(&z) {
        return Err(invalid("Clear", "depth outside 0..1"));
    }
    if rects.is_empty() {
        return Ok(());
    }
    let depth = depth.filter(|_| flags & 6 != 0);
    let mut source = String::from(
        "struct Params {color:vec4<f32>,depth:vec4<f32>}\n@group(0) @binding(0) var<uniform> params:Params;\n@vertex fn vs_main(@builtin(vertex_index) i:u32)->@builtin(position) vec4<f32>{let p=array<vec2<f32>,3>(vec2<f32>(-1.0,-1.0),vec2<f32>(3.0,-1.0),vec2<f32>(-1.0,3.0));return vec4<f32>(p[i],0.0,1.0);}\n",
    );
    if flags & 2 != 0 {
        source += "struct Out {@location(0) color:vec4<f32>,@builtin(frag_depth) depth:f32}\n@fragment fn ps_main()->Out{var o:Out;o.color=params.color;o.depth=params.depth.x;return o;}\n";
    } else {
        source += "@fragment fn ps_main()->@location(0) vec4<f32>{return params.color;}\n";
    }
    let shader = draw.shader(gpu, source)?;
    let layout = draw.layout(
        gpu,
        vec![uniform_entry(0, 32, wgpu::ShaderStages::FRAGMENT)],
    );
    let mut params = color(color_value).to_vec();
    if opaque_format(target.format) {
        params[3] = 1.0;
    }
    params.extend_from_slice(&[z, 0.0, 0.0, 0.0]);
    let buffer = make_buffer(
        gpu,
        "d3d9-clear",
        &flat_to_u8(&params),
        wgpu::BufferUsages::UNIFORM,
    );
    let bind = gpu.device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("d3d9-clear"),
        layout: &layout,
        entries: &[wgpu::BindGroupEntry {
            binding: 0,
            resource: buffer.as_entire_binding(),
        }],
    });
    let face = wgpu::StencilFaceState {
        compare: wgpu::CompareFunction::Always,
        fail_op: wgpu::StencilOperation::Keep,
        depth_fail_op: wgpu::StencilOperation::Keep,
        pass_op: wgpu::StencilOperation::Replace,
    };
    let ds = depth
        .map(|d| {
            Ok::<_, RenderError>(wgpu::DepthStencilState {
                format: d.texture.format(),
                depth_write_enabled: flags & 2 != 0,
                depth_compare: wgpu::CompareFunction::Always,
                stencil: wgpu::StencilState {
                    front: face,
                    back: face,
                    read_mask: u32::MAX,
                    write_mask: if flags & 4 != 0 { u32::MAX } else { 0 },
                },
                bias: Default::default(),
            })
        })
        .transpose()?;
    let pipeline = draw.pipeline(
        gpu,
        PipelineKey {
            modules: [shader.clone(), shader],
            layout,
            attributes: Vec::new(),
            target: wgpu::ColorTargetState {
                format: target.texture.format(),
                blend: None,
                write_mask: if flags & 1 != 0 {
                    wgpu::ColorWrites::ALL
                } else {
                    wgpu::ColorWrites::empty()
                },
            },
            primitive: Default::default(),
            depth: ds,
            multisample: Default::default(),
        },
    );
    let mut encoder = gpu
        .device
        .create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("d3d9-clear"),
        });
    let colors = [Some(wgpu::RenderPassColorAttachment {
        view: &target.view,
        resolve_target: None,
        depth_slice: None,
        ops: wgpu::Operations {
            load: wgpu::LoadOp::Load,
            store: wgpu::StoreOp::Store,
        },
    })];
    let depth = depth.map(|d| wgpu::RenderPassDepthStencilAttachment {
        view: &d.view,
        depth_ops: Some(wgpu::Operations {
            load: wgpu::LoadOp::Load,
            store: wgpu::StoreOp::Store,
        }),
        stencil_ops: if super::format::has_stencil(d.format) {
            Some(wgpu::Operations {
                load: wgpu::LoadOp::Load,
                store: wgpu::StoreOp::Store,
            })
        } else {
            None
        },
    });
    {
        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
            label: Some("d3d9-clear"),
            color_attachments: &colors,
            depth_stencil_attachment: depth,
            timestamp_writes: None,
            occlusion_query_set: None,
        });
        pass.set_pipeline(&pipeline);
        pass.set_bind_group(0, &bind, &[]);
        pass.set_stencil_reference(stencil);
        for r in rects {
            let l = r.left.clamp(0, target.width as i32) as u32;
            let t = r.top.clamp(0, target.height as i32) as u32;
            let right = r.right.clamp(0, target.width as i32) as u32;
            let bottom = r.bottom.clamp(0, target.height as i32) as u32;
            if right > l && bottom > t {
                pass.set_scissor_rect(l, t, right - l, bottom - t);
                pass.draw(0..3, 0..1);
            }
        }
    }
    gpu.queue.submit(std::iter::once(encoder.finish()));
    Ok(())
}
