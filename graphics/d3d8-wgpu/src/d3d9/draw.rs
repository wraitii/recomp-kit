use super::gpu::D3d9Gpu;
use super::shader::VertexElement;
use super::state::D3d9State;
use super::{SurfaceView, TargetRect, wgsl};
use crate::RenderError;
use std::borrow::Cow;
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::{Arc, Weak};

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
    pub bytes: Arc<Vec<u8>>,
}

pub(crate) struct DrawCommand {
    pub primitive: u32,
    pub primitive_count: u32,
    pub start_vertex: u32,
    pub base_vertex: i32,
    pub indexed: bool,
    pub index_format: u32,
    pub index_bytes: Arc<Vec<u8>>,
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

#[derive(Default)]
struct FastU32Hasher(u64);

impl FastU32Hasher {
    fn mix(&mut self, value: u64) {
        self.0 = value
            .wrapping_add(0x9e37_79b9_7f4a_7c15)
            .wrapping_mul(0xbf58_476d_1ce4_e5b9);
        self.0 ^= self.0 >> 30;
        self.0 = self.0.wrapping_mul(0xbf58_476d_1ce4_e5b9);
        self.0 ^= self.0 >> 27;
        self.0 = self.0.wrapping_mul(0x94d0_49bb_1331_11eb);
        self.0 ^= self.0 >> 31;
    }
}

impl Hasher for FastU32Hasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks(8) {
            let mut value = [0; 8];
            value[..chunk.len()].copy_from_slice(chunk);
            self.mix(u64::from_ne_bytes(value));
        }
    }

    fn write_u32(&mut self, value: u32) {
        self.mix(u64::from(value));
    }
}

type FastIndexMap = HashMap<u32, u32, BuildHasherDefault<FastU32Hasher>>;

fn expanded_indices(
    command: &DrawCommand,
    dense_remap: &mut Vec<u32>,
) -> Result<(Vec<u32>, Option<Vec<u32>>, u32, wgpu::PrimitiveTopology), RenderError> {
    let count = primitive_vertex_count(command.primitive, command.primitive_count)?;
    let packed_indices = command.indexed && command.primitive == D3DPT_TRIANGLELIST;
    let unique_capacity = if packed_indices {
        count.min(command.num_vertices) as usize
    } else {
        count as usize
    };
    let mut order = Vec::new();
    order
        .try_reserve_exact(unique_capacity)
        .map_err(|_| RenderError::out_of_memory("Draw", "index expansion allocation failed"))?;
    let dense_range = command.num_vertices as usize;
    let use_dense_remap = packed_indices
        && dense_range <= 1_048_576
        && dense_range <= (count as usize).saturating_mul(8).max(4096);
    if use_dense_remap {
        dense_remap
            .try_reserve(dense_range.saturating_sub(dense_remap.len()))
            .map_err(|_| {
                RenderError::out_of_memory("Draw", "dense index remap allocation failed")
            })?;
        dense_remap.resize(dense_range, u32::MAX);
        dense_remap.fill(u32::MAX);
    }
    let mut slots = FastIndexMap::default();
    let mut draw_indices = if packed_indices {
        if !use_dense_remap {
            slots.try_reserve(unique_capacity).map_err(|_| {
                RenderError::out_of_memory("Draw", "vertex index map allocation failed")
            })?;
        }
        let mut indices = Vec::new();
        indices
            .try_reserve_exact(count as usize)
            .map_err(|_| RenderError::out_of_memory("Draw", "draw index allocation failed"))?;
        Some(indices)
    } else {
        None
    };
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
            let absolute = absolute as u32;
            if let Some(indices) = &mut draw_indices {
                let slot = if use_dense_remap {
                    let slot = &mut dense_remap[(raw as u32 - command.min_vertex) as usize];
                    if *slot == u32::MAX {
                        *slot = u32::try_from(order.len())
                            .map_err(|_| invalid("Draw", "packed vertex count exceeds 32 bits"))?;
                        order.push(absolute);
                    }
                    *slot
                } else {
                    if let Some(&slot) = slots.get(&absolute) {
                        slot
                    } else {
                        let slot = u32::try_from(order.len())
                            .map_err(|_| invalid("Draw", "packed vertex count exceeds 32 bits"))?;
                        order.push(absolute);
                        slots.insert(absolute, slot);
                        slot
                    }
                };
                indices.push(slot);
            } else {
                order.push(absolute);
            }
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
    Ok((order, draw_indices, count, topology))
}

fn append_f32_le(out: &mut Vec<u8>, values: &[f32]) {
    if cfg!(target_endian = "little") {
        out.extend_from_slice(bytemuck::cast_slice(values));
    } else {
        let start = out.len();
        out.resize(start + values.len() * 4, 0);
        for (destination, value) in out[start..].chunks_exact_mut(4).zip(values) {
            destination.copy_from_slice(&value.to_le_bytes());
        }
    }
}

fn append_u32_le(out: &mut Vec<u8>, values: &[u32]) {
    if cfg!(target_endian = "little") {
        out.extend_from_slice(bytemuck::cast_slice(values));
    } else {
        let start = out.len();
        out.resize(start + values.len() * 4, 0);
        for (destination, value) in out[start..].chunks_exact_mut(4).zip(values) {
            destination.copy_from_slice(&value.to_le_bytes());
        }
    }
}

fn stream_for<'a>(command: &'a DrawCommand, stream: u32) -> Result<&'a StreamData, RenderError> {
    command
        .streams
        .iter()
        .find(|s| s.stream == stream)
        .ok_or_else(|| unsupported("Draw", format!("vertex stream {stream} is not bound")))
}

struct VertexFetch<'a> {
    bytes: &'a [u8],
    stride: usize,
    size: usize,
    element_type: u8,
}

impl<'a> VertexFetch<'a> {
    fn prepare(
        command: &'a DrawCommand,
        stream: u16,
        offset: u16,
        element_type: u8,
        max_index: u32,
    ) -> Result<Self, RenderError> {
        let data = stream_for(command, stream as u32)?;
        let size = element_size(element_type)
            .ok_or_else(|| unsupported("Draw", "unsupported vertex element type"))?
            as usize;
        let start = (data.offset as usize)
            .checked_add(offset as usize)
            .ok_or_else(|| invalid("Draw", "vertex offset overflow"))?;
        let end = (max_index as usize)
            .checked_mul(data.stride as usize)
            .and_then(|v| v.checked_add(start))
            .and_then(|v| v.checked_add(size))
            .ok_or_else(|| invalid("Draw", "vertex offset overflow"))?;
        if end > data.bytes.len() {
            return Err(invalid("Draw", "vertex fetch is outside the stream"));
        }
        Ok(Self {
            bytes: &data.bytes[start..],
            stride: data.stride as usize,
            size,
            element_type,
        })
    }

    fn bytes(&self, index: u32) -> &[u8] {
        let base = index as usize * self.stride;
        &self.bytes[base..base + self.size]
    }

    fn fetch(&self, index: u32) -> Result<[f32; 4], RenderError> {
        expand_element(self.element_type, self.bytes(index))
    }
}

fn native_vertex_format(element_type: u8) -> Option<wgpu::VertexFormat> {
    use wgpu::VertexFormat as F;
    Some(match element_type {
        D3DDECLTYPE_FLOAT1 => F::Float32,
        D3DDECLTYPE_FLOAT2 => F::Float32x2,
        D3DDECLTYPE_FLOAT3 => F::Float32x3,
        D3DDECLTYPE_FLOAT4 => F::Float32x4,
        D3DDECLTYPE_D3DCOLOR => F::Unorm8x4Bgra,
        D3DDECLTYPE_UBYTE4N => F::Unorm8x4,
        D3DDECLTYPE_SHORT2N => F::Snorm16x2,
        D3DDECLTYPE_SHORT4N => F::Snorm16x4,
        D3DDECLTYPE_USHORT2N => F::Unorm16x2,
        D3DDECLTYPE_USHORT4N => F::Unorm16x4,
        D3DDECLTYPE_FLOAT16_2 => F::Float16x2,
        D3DDECLTYPE_FLOAT16_4 => F::Float16x4,
        _ => return None,
    })
}

struct NativeGeometry {
    vertex: wgpu::Buffer,
    index: wgpu::Buffer,
    vertex_offset: u64,
    vertex_size: u64,
    index_offset: u64,
    index_size: u64,
    index_format: wgpu::IndexFormat,
    base_vertex: i32,
    count: u32,
}

struct CachedGeometry {
    bytes: Weak<Vec<u8>>,
    buffer: wgpu::Buffer,
    index_bounds: HashMap<(u32, u32, u32), (u32, u32)>,
}

struct PackedVertices {
    bytes: Vec<u8>,
    attributes: Vec<wgpu::VertexAttribute>,
    stride: u64,
}

fn pack_shader_vertices(
    command: &DrawCommand,
    linked: &[LinkedInput],
    order: &[u32],
) -> Result<PackedVertices, RenderError> {
    struct Copy<'a> {
        fetch: VertexFetch<'a>,
        native: bool,
        stream: u16,
        offset: usize,
    }
    let mut copies: Vec<Copy<'_>> = Vec::with_capacity(linked.len());
    let mut attributes = Vec::with_capacity(linked.len());
    let mut stride = 0u64;
    let max_index = order.iter().copied().max();
    for input in linked {
        let native = native_vertex_format(input.element_type);
        let format = native.unwrap_or(wgpu::VertexFormat::Float32x4);
        attributes.push(wgpu::VertexAttribute {
            format,
            offset: stride,
            shader_location: input.location,
        });
        stride += format.size();
        if let Some(max_index) = max_index {
            let fetch = VertexFetch::prepare(
                command,
                input.stream,
                input.offset,
                input.element_type,
                max_index,
            )?;
            if let Some(last) = copies.last_mut() {
                if native.is_some()
                    && last.native
                    && last.stream == input.stream
                    && last.offset + last.fetch.size == input.offset as usize
                {
                    last.fetch.size += fetch.size;
                    continue;
                }
            }
            copies.push(Copy {
                fetch,
                native: native.is_some(),
                stream: input.stream,
                offset: input.offset as usize,
            });
        }
    }
    let total = order
        .len()
        .checked_mul(stride as usize)
        .ok_or_else(|| invalid("Draw", "vertex buffer size overflow"))?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(total)
        .map_err(|_| RenderError::out_of_memory("Draw", "vertex repack allocation failed"))?;
    if let [copy] = copies.as_slice() {
        if copy.native
            && copy.fetch.stride == copy.fetch.size
            && order
                .windows(2)
                .all(|pair| u64::from(pair[0]) + 1 == u64::from(pair[1]))
        {
            if let Some(&first) = order.first() {
                let start = first as usize * copy.fetch.stride;
                bytes.extend_from_slice(&copy.fetch.bytes[start..start + total]);
            }
            return Ok(PackedVertices {
                bytes,
                attributes,
                stride,
            });
        }
    }
    for &index in order {
        for copy in &copies {
            if copy.native {
                bytes.extend_from_slice(copy.fetch.bytes(index));
            } else {
                append_f32_le(&mut bytes, &copy.fetch.fetch(index)?);
            }
        }
    }
    Ok(PackedVertices {
        bytes,
        attributes,
        stride,
    })
}

fn native_geometry(
    gpu: &D3d9Gpu,
    draw: &mut D3d9DrawState,
    command: &DrawCommand,
    linked: &[LinkedInput],
) -> Result<Option<(NativeGeometry, PackedVertices)>, RenderError> {
    if !command.indexed
        || command.primitive != D3DPT_TRIANGLELIST
        || linked.is_empty()
        || super::trace_enabled()
    {
        return Ok(None);
    }
    let stream = stream_for(command, u32::from(linked[0].stream))?;
    if stream.stride == 0
        || stream.stride % 4 != 0
        || stream.offset % 4 != 0
        || stream.stride > gpu.limits().max_vertex_buffer_array_stride
    {
        return Ok(None);
    }
    let mut attributes = Vec::with_capacity(linked.len());
    for input in linked {
        let Some(format) = native_vertex_format(input.element_type) else {
            return Ok(None);
        };
        if u32::from(input.stream) != stream.stream
            || u64::from(input.offset) % format.size().min(4) != 0
            || u64::from(input.offset) + format.size() > u64::from(stream.stride)
        {
            return Ok(None);
        }
        attributes.push(wgpu::VertexAttribute {
            format,
            offset: u64::from(input.offset),
            shader_location: input.location,
        });
    }
    if !matches!(command.index_format, 101 | 102) {
        return Err(invalid("Draw", "invalid index format"));
    }
    let count = primitive_vertex_count(command.primitive, command.primitive_count)?;
    let start = command.start_vertex as usize;
    let end = start
        .checked_add(count as usize)
        .ok_or_else(|| invalid("Draw", "index range overflow"))?;
    if end > index_count(command.index_format, &command.index_bytes) {
        return Err(invalid("Draw", "index range exceeds the index stream"));
    }
    let index_buffer = draw.geometry_buffer(gpu, &command.index_bytes)?;
    let cached = draw
        .geometry
        .get_mut(&(Arc::as_ptr(&command.index_bytes) as usize))
        .expect("index geometry was cached");
    let range = (command.start_vertex, count, command.index_format);
    let (min_raw, max_raw) = if let Some(&bounds) = cached.index_bounds.get(&range) {
        bounds
    } else {
        let mut min = u32::MAX;
        let mut max = 0;
        for index in start..end {
            let raw = indexed_vertex_index(&command.index_bytes, command.index_format, index);
            min = min.min(raw);
            max = max.max(raw);
        }
        if cached.index_bounds.len() >= 256 {
            cached.index_bounds.clear();
        }
        cached.index_bounds.insert(range, (min, max));
        (min, max)
    };
    if min_raw < command.min_vertex
        || u64::from(max_raw) >= u64::from(command.min_vertex) + u64::from(command.num_vertices)
    {
        return Err(invalid("Draw", "index outside MinVertexIndex/NumVertices"));
    }
    let max_index = i64::from(max_raw) + i64::from(command.base_vertex);
    if i64::from(min_raw) + i64::from(command.base_vertex) < 0 || max_index > i64::from(u32::MAX) {
        return Err(invalid(
            "Draw",
            "index plus base vertex is outside the vertex window",
        ));
    }
    let max_index = max_index as u32;
    for input in linked {
        VertexFetch::prepare(
            command,
            input.stream,
            input.offset,
            input.element_type,
            max_index,
        )?;
    }
    let index_stride = if command.index_format == 101 { 2 } else { 4 };
    let geometry = NativeGeometry {
        vertex: draw.geometry_buffer(gpu, &stream.bytes)?,
        index: index_buffer,
        vertex_offset: u64::from(stream.offset),
        vertex_size: stream.bytes.len() as u64 - u64::from(stream.offset),
        index_offset: start as u64 * index_stride,
        index_size: u64::from(count) * index_stride,
        index_format: if command.index_format == 101 {
            wgpu::IndexFormat::Uint16
        } else {
            wgpu::IndexFormat::Uint32
        },
        base_vertex: command.base_vertex,
        count,
    };
    Ok(Some((
        geometry,
        PackedVertices {
            bytes: Vec::new(),
            attributes,
            stride: u64::from(stream.stride),
        },
    )))
}

fn flat_to_u8(values: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(values.len().saturating_mul(4));
    append_f32_le(&mut out, values);
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
struct FfLight {
    kind: u32,
    values: [f32; 26],
    direction: [f32; 4],
    position: [f32; 4],
}
fn ff_colors(
    state: &D3d9State,
    p: [f32; 4],
    n: [f32; 3],
    vertex: [Option<[f32; 4]>; 2],
    lights: &[FfLight],
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
    for light in lights {
        let (l, mut attenuation) = if light.kind == 3 {
            (
                normalize3([
                    -light.direction[0],
                    -light.direction[1],
                    -light.direction[2],
                ]),
                1.0,
            )
        } else {
            let delta = [
                light.position[0] - p[0],
                light.position[1] - p[1],
                light.position[2] - p[2],
            ];
            let distance = dot3(delta, delta).sqrt();
            if distance > light.values[19] {
                continue;
            }
            let denominator = light.values[21]
                + light.values[22] * distance
                + light.values[23] * distance * distance;
            if denominator <= 0.0 {
                return Err(unsupported(
                    "Draw",
                    "non-positive light attenuation denominator",
                ));
            }
            (normalize3(delta), 1.0 / denominator)
        };
        if light.kind == 2 {
            let rho = -dot3(
                l,
                normalize3([light.direction[0], light.direction[1], light.direction[2]]),
            );
            let inner = (light.values[24] * 0.5).cos();
            let outer = (light.values[25] * 0.5).cos();
            attenuation *= if rho >= inner {
                1.0
            } else if rho <= outer {
                0.0
            } else {
                ((rho - outer) / (inner - outer)).powf(light.values[20])
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
            result[0][i] += attenuation
                * (ambient[i] * light.values[9 + i] + diffuse[i] * light.values[1 + i] * ndotl);
            result[1][i] += attenuation * specular[i] * light.values[5 + i] * highlight;
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
) -> Result<Vec<u8>, RenderError> {
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
    let mut lights = Vec::new();
    if !transformed && rs(state, 137) != 0 {
        let raw_lights = state.enabled_lights();
        lights
            .try_reserve_exact(raw_lights.len())
            .map_err(|_| RenderError::out_of_memory("Draw", "fixed-function lights"))?;
        for raw in raw_lights {
            let values = raw.map(f32::from_bits);
            let direction = transform_point(&view, [values[16], values[17], values[18], 0.0]);
            let position = if raw[0] == 3 {
                [0.0; 4]
            } else {
                transform_point(&view, [values[13], values[14], values[15], 1.0])
            };
            lights.push(FfLight {
                kind: raw[0],
                values,
                direction,
                position,
            });
        }
    }
    let mut stage_coords = Vec::new();
    stage_coords
        .try_reserve_exact(8)
        .map_err(|_| RenderError::out_of_memory("Draw", "fixed-function texture stages"))?;
    for stage in 0..8 {
        let coord = tss(state, stage, 11);
        let transform = if tss(state, stage, 24) & 255 != 0 {
            Some(state.transform(16 + stage)?)
        } else {
            None
        };
        stage_coords.push((coord, transform));
    }
    let vp = state.viewport();
    let mut out = Vec::new();
    out.try_reserve_exact(
        order
            .len()
            .checked_mul(192)
            .ok_or_else(|| invalid("Draw", "vertex allocation overflow"))?,
    )
    .map_err(|_| RenderError::out_of_memory("Draw", "fixed-function vertices"))?;
    let Some(&max_index) = order.iter().max() else {
        return Ok(out);
    };
    let prepare = |e: &VertexElement| {
        VertexFetch::prepare(command, e.stream, e.offset, e.element_type, max_index)
    };
    let position = prepare(position)?;
    let colors = [
        find(10, 0).map(&prepare).transpose()?,
        find(10, 1).map(&prepare).transpose()?,
    ];
    let normal = if needs_normal {
        find(3, 0).map(&prepare).transpose()?
    } else {
        None
    };
    let texcoords = stage_coords
        .iter()
        .map(|&(coord, _)| {
            if coord & 0xffff0000 == 0 {
                find(5, (coord & 0xffff) as u8).map(&prepare).transpose()
            } else {
                Ok(None)
            }
        })
        .collect::<Result<Vec<_>, RenderError>>()?;
    for &index in order {
        let fetch = |e: &VertexFetch<'_>| e.fetch(index);
        let p = fetch(&position)?;
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
            colors[0].as_ref().map(&fetch).transpose()?,
            colors[1].as_ref().map(&fetch).transpose()?,
        ];
        let normal = if needs_normal {
            let n = normal.as_ref().map(&fetch).transpose()?.unwrap_or([0.0; 4]);
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
            ff_colors(state, viewp, normal, vertex, &lights)?
        };
        let mut packed = [0.0f32; 48];
        packed[..4].copy_from_slice(&clip);
        packed[4..8].copy_from_slice(&colors[0]);
        packed[8..12].copy_from_slice(&colors[1]);
        for (stage, (&(coord, transform), texcoord)) in
            stage_coords.iter().zip(&texcoords).enumerate()
        {
            if transformed && coord & 0xffff0000 != 0 {
                return Err(unsupported(
                    "Draw",
                    "texture-coordinate generation with POSITIONT is not implemented",
                ));
            }
            let mut uv = match coord & 0xffff0000 {
                0 => texcoord
                    .as_ref()
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
            if let Some(transform) = transform {
                uv = transform_point(&transform, uv);
            }
            let start = 12 + stage * 4;
            packed[start..start + 4].copy_from_slice(&uv);
        }
        let fog = if transformed {
            vertex[1].map(|v| v[3]).unwrap_or(1.0)
        } else {
            fog_factor(state, viewp[2].abs(), rs(state, 140))
        };
        packed[44] = fog;
        packed[45] = viewp[2];
        append_f32_le(&mut out, &packed);
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
                    0 if state.shader(false) != 0 => "w",
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
) -> Result<(SamplerKey, wgpu::SamplerDescriptor<'static>), RenderError> {
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
    let lod_min = ss(state, t.stage, 9)
        .max(t.lod)
        .min(t.levels.saturating_sub(1)) as f32;
    let lod_max = if ss(state, t.stage, 7) == 0 {
        lod_min
    } else {
        t.levels.saturating_sub(1) as f32
    };
    let key = SamplerKey {
        address: [
            ss(state, t.stage, 1),
            ss(state, t.stage, 2),
            ss(state, t.stage, 3),
        ],
        filters: [
            ss(state, t.stage, 5),
            ss(state, t.stage, 6),
            ss(state, t.stage, 7),
        ],
        lod_min: lod_min.to_bits(),
        lod_max: lod_max.to_bits(),
        depth: t.depth,
    };
    Ok((
        key,
        wgpu::SamplerDescriptor {
            label: Some("d3d9-sampler"),
            address_mode_u: address(1)?,
            address_mode_v: address(2)?,
            address_mode_w: address(3)?,
            mag_filter: filter(5)?,
            min_filter: filter(6)?,
            mipmap_filter: filter(7)?,
            lod_min_clamp: lod_min,
            lod_max_clamp: lod_max,
            compare: if t.depth {
                Some(wgpu::CompareFunction::LessEqual)
            } else {
                None
            },
            ..Default::default()
        },
    ))
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
    vertex_stride: u64,
    target: wgpu::ColorTargetState,
    primitive: wgpu::PrimitiveState,
    depth: Option<wgpu::DepthStencilState>,
    multisample: wgpu::MultisampleState,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct ShaderVariantKey {
    pixel: bool,
    shader: u32,
    state: Vec<u32>,
}

#[derive(Clone)]
struct CachedShaderSource {
    source: String,
    texture_stages: Vec<u32>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct SamplerKey {
    address: [u32; 3],
    filters: [u32; 3],
    lod_min: u32,
    lod_max: u32,
    depth: bool,
}

struct PendingTexture {
    stage: u32,
    view: wgpu::TextureView,
    sampler: wgpu::Sampler,
}

#[derive(Clone, PartialEq, Eq, Hash)]
struct BindGroupKey {
    layout: wgpu::BindGroupLayout,
    uniform_sizes: [u64; 4],
    textures: Vec<(u32, wgpu::TextureView, wgpu::Sampler)>,
}

struct PendingDraw {
    pipeline: wgpu::RenderPipeline,
    layout: wgpu::BindGroupLayout,
    uniform_offsets: [u32; 4],
    uniform_sizes: [u64; 4],
    textures: Vec<PendingTexture>,
    vertex_offset: u64,
    vertex_size: u64,
    has_attributes: bool,
    native_geometry: Option<NativeGeometry>,
    index_range: Option<(u64, u64, wgpu::IndexFormat)>,
    vertex_count: u32,
    viewport: super::state::Viewport,
    stencil_reference: u32,
    blend_constant: [f32; 4],
    scissor: Option<[u32; 4]>,
}

#[derive(Default)]
struct DrawBatch {
    draws: Vec<PendingDraw>,
    data: Vec<u8>,
    color_view: Option<wgpu::TextureView>,
    depth: Option<(wgpu::TextureView, bool)>,
    width: u32,
    height: u32,
    last_uniform: Option<([u32; 4], [u64; 4])>,
}

#[derive(Default)]
pub(crate) struct D3d9DrawState {
    shaders: HashMap<String, wgpu::ShaderModule>,
    shader_variants: HashMap<ShaderVariantKey, Arc<CachedShaderSource>>,
    variant_shaders: HashMap<ShaderVariantKey, wgpu::ShaderModule>,
    samplers: HashMap<SamplerKey, wgpu::Sampler>,
    layouts: HashMap<Vec<wgpu::BindGroupLayoutEntry>, wgpu::BindGroupLayout>,
    pipelines: HashMap<PipelineKey, wgpu::RenderPipeline>,
    batch: DrawBatch,
    index_remap: Vec<u32>,
    geometry: HashMap<usize, CachedGeometry>,
    upload: Option<wgpu::Buffer>,
    staging: Option<wgpu::util::StagingBelt>,
    bind_groups: HashMap<BindGroupKey, wgpu::BindGroup>,
}
impl D3d9DrawState {
    pub fn clear(&mut self) {
        self.pipelines.clear();
        self.layouts.clear();
        self.shaders.clear();
        self.shader_variants.clear();
        self.variant_shaders.clear();
        self.samplers.clear();
        self.bind_groups.clear();
        self.geometry.clear();
    }

    pub(crate) fn discard_batch(&mut self) {
        self.batch = DrawBatch::default();
    }

    pub(crate) fn reset(&mut self) {
        self.clear();
        self.discard_batch();
        self.upload = None;
        self.staging = None;
        self.bind_groups.clear();
    }
    pub fn drop_resource(&mut self, _id: u32) {}

    fn geometry_buffer(
        &mut self,
        gpu: &D3d9Gpu,
        bytes: &Arc<Vec<u8>>,
    ) -> Result<wgpu::Buffer, RenderError> {
        let key = Arc::as_ptr(bytes) as usize;
        if let Some(cached) = self.geometry.get(&key) {
            return Ok(cached.buffer.clone());
        }
        if bytes.len() as u64 > gpu.limits().max_buffer_size {
            return Err(RenderError::out_of_memory(
                "Draw",
                "geometry exceeds the device buffer limit",
            ));
        }
        self.geometry
            .retain(|_, cached| cached.bytes.strong_count() != 0);
        let total: u64 = self
            .geometry
            .values()
            .map(|cached| cached.buffer.size())
            .sum();
        if total.saturating_add(bytes.len() as u64) > 128 << 20 {
            self.geometry.clear();
        }
        let buffer = make_buffer(
            gpu,
            "d3d9-geometry",
            bytes,
            wgpu::BufferUsages::VERTEX | wgpu::BufferUsages::INDEX,
        );
        self.geometry.insert(
            key,
            CachedGeometry {
                bytes: Arc::downgrade(bytes),
                buffer: buffer.clone(),
                index_bounds: HashMap::new(),
            },
        );
        Ok(buffer)
    }

    fn source(
        &mut self,
        key: ShaderVariantKey,
        generate: impl FnOnce() -> Result<String, RenderError>,
    ) -> Result<Arc<CachedShaderSource>, RenderError> {
        if let Some(source) = self.shader_variants.get(&key) {
            return Ok(Arc::clone(source));
        }
        let source = generate()?;
        let texture_stages = (0..16)
            .filter(|stage| source.contains(&format!("var tx{stage}:")))
            .collect();
        let cached = Arc::new(CachedShaderSource {
            source,
            texture_stages,
        });
        if self.shader_variants.len() >= 512 {
            self.clear();
        }
        self.shader_variants.insert(key, Arc::clone(&cached));
        Ok(cached)
    }

    fn sampler(
        &mut self,
        gpu: &D3d9Gpu,
        key: SamplerKey,
        descriptor: wgpu::SamplerDescriptor<'static>,
    ) -> wgpu::Sampler {
        if let Some(sampler) = self.samplers.get(&key) {
            return sampler.clone();
        }
        let sampler = gpu.device.create_sampler(&descriptor);
        if self.samplers.len() >= 128 {
            self.samplers.clear();
        }
        self.samplers.insert(key, sampler.clone());
        sampler
    }

    fn variant_shader(
        &mut self,
        gpu: &D3d9Gpu,
        key: ShaderVariantKey,
        source: &str,
    ) -> Result<wgpu::ShaderModule, RenderError> {
        if let Some(shader) = self.variant_shaders.get(&key) {
            return Ok(shader.clone());
        }
        let shader = self.shader(gpu, source)?;
        self.variant_shaders.insert(key, shader.clone());
        Ok(shader)
    }

    fn shader(&mut self, gpu: &D3d9Gpu, source: &str) -> Result<wgpu::ShaderModule, RenderError> {
        if let Some(shader) = self.shaders.get(source) {
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
        self.shaders.insert(source.to_owned(), shader.clone());
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
            array_stride: key.vertex_stride,
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
        if program.pixel
            && program.major == 1
            && program.minor <= 3
            && t.stage < 8
            && tss(state, t.stage, 24) & 256 != 0
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
fn translated_key(
    shader: u32,
    program: &wgsl::Program,
    state: &D3d9State,
    textures: &[BoundTexture],
) -> Result<ShaderVariantKey, RenderError> {
    let mut variant = wgsl::Variant::default();
    for stage in 0..16 {
        if state.texture(stage)? == 0 {
            variant.unbound |= 1 << stage;
        }
    }
    let mut opaque = 0u32;
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
        if opaque_format(t.format) {
            opaque |= 1 << t.stage;
        }
        if program.pixel
            && program.major == 1
            && program.minor <= 3
            && t.stage < 8
            && tss(state, t.stage, 24) & 256 != 0
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
    Ok(ShaderVariantKey {
        pixel: program.pixel,
        shader,
        state: vec![
            u32::from(variant.unbound),
            u32::from(variant.cube),
            u32::from(variant.volume),
            u32::from(variant.depth),
            u32::from(variant.projected),
            opaque,
            rs(state, 9),
        ],
    })
}

fn fixed_function_key(
    pixel: bool,
    state: &D3d9State,
    textures: &[BoundTexture],
) -> ShaderVariantKey {
    let mut values = vec![rs(state, 9), rs(state, 29), rs(state, 60)];
    if pixel {
        values.push(u32::from(state.shader(false) != 0));
        for stage in 0..8 {
            for id in [1, 2, 3, 4, 5, 6, 24, 26, 27, 28, 32] {
                values.push(tss(state, stage, id));
            }
        }
        for texture in textures {
            values.extend_from_slice(&[
                texture.stage,
                texture.format,
                texture.depth as u32,
                texture.cube as u32,
                texture.volume as u32,
            ]);
        }
    }
    ShaderVariantKey {
        pixel,
        shader: 0,
        state: values,
    }
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
            has_dynamic_offset: true,
            min_binding_size: wgpu::BufferSize::new(size),
        },
        count: None,
    }
}

fn static_uniform_entry(
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
    let direct = linked
        .as_ref()
        .map(|linked| native_geometry(gpu, draw, &snapshot.command, linked))
        .transpose()?
        .flatten();
    let (native_geometry, vertices, order, draw_indices, topology) =
        if let Some((geometry, vertices)) = direct {
            (
                Some(geometry),
                vertices,
                Vec::new(),
                None,
                wgpu::PrimitiveTopology::TriangleList,
            )
        } else {
            let (order, draw_indices, _, topology) =
                expanded_indices(&snapshot.command, &mut draw.index_remap)?;
            let vertices = if let Some(linked) = &linked {
                pack_shader_vertices(&snapshot.command, linked, &order)?
            } else {
                PackedVertices {
                    bytes: ff_vertices(&snapshot.command, declaration, &order, state)?,
                    attributes: (0..12)
                        .map(|i| wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x4,
                            offset: u64::from(i) * 16,
                            shader_location: i,
                        })
                        .collect(),
                    stride: 192,
                }
            };
            (None, vertices, order, draw_indices, topology)
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
            eprintln!(
                "[d3d9-trace] vertex_stride={} packed_bytes={}",
                vertices.stride,
                vertices.bytes.len()
            );
            for vertex in vertices
                .bytes
                .chunks((vertices.stride as usize).max(1))
                .take(6)
            {
                let values = if let Some(linked) = &linked {
                    linked
                        .iter()
                        .zip(&vertices.attributes)
                        .map(|(input, attribute)| {
                            let element_type = if native_vertex_format(input.element_type).is_some()
                            {
                                input.element_type
                            } else {
                                D3DDECLTYPE_FLOAT4
                            };
                            expand_element(element_type, &vertex[attribute.offset as usize..])
                        })
                        .collect::<Result<Vec<_>, _>>()?
                } else {
                    vertex
                        .chunks_exact(16)
                        .map(|bytes| expand_element(D3DDECLTYPE_FLOAT4, bytes))
                        .collect::<Result<Vec<_>, _>>()?
                };
                eprintln!("[d3d9-trace] vertex={values:?}");
            }
        }
    }
    let vertex_key = if let Some(v) = vs {
        translated_key(state.shader(false), &v.program, state, &snapshot.textures)?
    } else {
        fixed_function_key(false, state, &snapshot.textures)
    };
    let vertex_source = draw.source(vertex_key.clone(), || {
        let source = if let Some(v) = vs {
            translated_source(&v.program, state, &snapshot.textures)?
        } else {
            ff_vertex_source()
        };
        Ok(shading_source(source, state))
    })?;
    let pixel_key = if let Some(p) = ps {
        translated_key(state.shader(true), &p.program, state, &snapshot.textures)?
    } else {
        fixed_function_key(true, state, &snapshot.textures)
    };
    let pixel_source = draw.source(pixel_key.clone(), || {
        let source = if let Some(p) = ps {
            translated_source(&p.program, state, &snapshot.textures)?
        } else {
            ff_fragment_source(state, &snapshot.textures)?
        };
        Ok(shading_source(source, state))
    })?;
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
    for &stage in &pixel_source.texture_stages {
        if !snapshot.textures.iter().any(|t| t.stage == stage) {
            return Err(unsupported(
                "Draw",
                format!("shader samples unbound texture stage {stage}"),
            ));
        }
    }
    let textures: Vec<&BoundTexture> = snapshot
        .textures
        .iter()
        .filter(|t| pixel_source.texture_stages.contains(&t.stage))
        .collect();
    let mut samplers = Vec::with_capacity(textures.len());
    for texture in &textures {
        let (key, descriptor) = sampler(state, texture)?;
        samplers.push(draw.sampler(gpu, key, descriptor));
    }
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
    let modules = [
        draw.variant_shader(gpu, vertex_key, &vertex_source.source)?,
        draw.variant_shader(gpu, pixel_key, &pixel_source.source)?,
    ];
    let has_attributes = !vertices.attributes.is_empty();
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
            layout: layout.clone(),
            attributes: vertices.attributes,
            vertex_stride: vertices.stride,
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
    if draw
        .batch
        .color_view
        .as_ref()
        .is_some_and(|view| *view != snapshot.target.view)
        || draw.batch.depth.as_ref().map(|(view, _)| view)
            != snapshot.depth.as_ref().map(|depth| &depth.view)
    {
        flush(gpu, draw)?;
    }
    if draw.batch.draws.is_empty() {
        draw.batch.color_view = Some(snapshot.target.view.clone());
        draw.batch.depth = snapshot
            .depth
            .as_ref()
            .map(|depth| (depth.view.clone(), super::format::has_stencil(depth.format)));
        draw.batch.width = snapshot.target.width;
        draw.batch.height = snapshot.target.height;
    }
    let uniform_values = [
        flat_to_u8(&state.constants_f(false, 0, vs_count)?),
        flat_to_u8(&vsp),
        flat_to_u8(&state.constants_f(true, 0, ps_count)?),
        psp,
    ];
    let alignment = gpu.limits().min_uniform_buffer_offset_alignment.max(1) as usize;
    let uniform_sizes = uniform_values.each_ref().map(|bytes| bytes.len() as u64);
    let mut shared_uniforms = draw.batch.last_uniform.filter(|(offsets, sizes)| {
        *sizes == uniform_sizes
            && uniform_values.iter().enumerate().all(|(index, bytes)| {
                let start = offsets[index] as usize;
                draw.batch.data.get(start..start + bytes.len()) == Some(bytes.as_slice())
            })
    });
    let compact_indices = order.len() <= u16::MAX as usize + 1;
    let index_stride = if compact_indices { 2 } else { 4 };
    let index_size = draw_indices
        .as_ref()
        .map_or(Some(0usize), |indices| {
            indices.len().checked_mul(index_stride)
        })
        .ok_or_else(|| invalid("Draw", "index upload size overflow"))?;
    let vertex_size = vertices.bytes.len();
    let aligned = |value: usize, alignment: usize| {
        value
            .checked_add(alignment - 1)
            .map(|value| value / alignment * alignment)
    };
    let estimate_size = |mut size: usize, reuse_uniforms: bool| -> Option<usize> {
        if !reuse_uniforms {
            for bytes in &uniform_values {
                size = aligned(size, alignment)?.checked_add(bytes.len())?;
            }
        }
        size = aligned(size, 4)?.checked_add(vertex_size)?;
        if index_size != 0 {
            size = aligned(aligned(size, 4)?.checked_add(index_size)?, 4)?;
        }
        Some(size)
    };
    let upload_limit = gpu.limits().max_buffer_size.min(u32::MAX as u64) as usize;
    let estimated = estimate_size(draw.batch.data.len(), shared_uniforms.is_some())
        .ok_or_else(|| invalid("Draw", "draw upload size overflow"))?;
    if estimated > upload_limit && !draw.batch.draws.is_empty() {
        flush(gpu, draw)?;
        shared_uniforms = None;
    }
    let estimated = estimate_size(draw.batch.data.len(), shared_uniforms.is_some())
        .ok_or_else(|| invalid("Draw", "draw upload size overflow"))?;
    if estimated > upload_limit {
        return Err(RenderError::out_of_memory(
            "Draw",
            "draw upload exceeds the device buffer limit",
        ));
    }
    let uniform_offsets = if let Some((offsets, _)) = shared_uniforms {
        offsets
    } else {
        let mut offsets = [0u32; 4];
        for (index, bytes) in uniform_values.iter().enumerate() {
            let offset = draw.batch.data.len().next_multiple_of(alignment);
            draw.batch.data.resize(offset, 0);
            offsets[index] = u32::try_from(offset)
                .map_err(|_| invalid("Draw", "uniform upload offset exceeds 32 bits"))?;
            draw.batch.data.extend_from_slice(bytes);
        }
        draw.batch.last_uniform = Some((offsets, uniform_sizes));
        offsets
    };
    let vertex_offset = aligned(draw.batch.data.len(), 4)
        .ok_or_else(|| invalid("Draw", "vertex upload offset overflow"))?;
    draw.batch.data.resize(vertex_offset, 0);
    draw.batch.data.extend_from_slice(&vertices.bytes);
    let index_range = draw_indices.as_ref().map(|indices| {
        let offset = draw.batch.data.len().next_multiple_of(4);
        draw.batch.data.resize(offset, 0);
        let start = draw.batch.data.len();
        let format = if compact_indices {
            draw.batch.data.resize(start + index_size, 0);
            for (destination, &index) in draw.batch.data[start..].chunks_exact_mut(2).zip(indices) {
                destination.copy_from_slice(&(index as u16).to_le_bytes());
            }
            wgpu::IndexFormat::Uint16
        } else {
            append_u32_le(&mut draw.batch.data, indices);
            wgpu::IndexFormat::Uint32
        };
        let size = draw.batch.data.len() - start;
        let end = draw.batch.data.len().next_multiple_of(4);
        draw.batch.data.resize(end, 0);
        (start as u64, size as u64, format)
    });
    let texture_bindings = textures
        .iter()
        .zip(&samplers)
        .map(|(texture, sampler)| PendingTexture {
            stage: texture.stage,
            view: texture.view.clone(),
            sampler: sampler.clone(),
        })
        .collect();
    let scissor = if rs(state, 174) != 0 {
        let s = state.scissor();
        let left = s.left.clamp(0, snapshot.target.width as i32) as u32;
        let top = s.top.clamp(0, snapshot.target.height as i32) as u32;
        let right = s.right.clamp(0, snapshot.target.width as i32) as u32;
        let bottom = s.bottom.clamp(0, snapshot.target.height as i32) as u32;
        Some([
            left,
            top,
            right.saturating_sub(left),
            bottom.saturating_sub(top),
        ])
    } else {
        None
    };
    let vertex_count = native_geometry.as_ref().map_or_else(
        || {
            draw_indices
                .as_ref()
                .map_or(order.len(), |indices| indices.len()) as u32
        },
        |geometry| geometry.count,
    );
    let blend = color(rs(state, 193));
    draw.batch.draws.push(PendingDraw {
        pipeline,
        layout,
        uniform_offsets,
        uniform_sizes,
        textures: texture_bindings,
        vertex_offset: vertex_offset as u64,
        vertex_size: vertex_size as u64,
        has_attributes,
        native_geometry,
        index_range,
        vertex_count,
        viewport: vp,
        stencil_reference: rs(state, 57),
        blend_constant: blend,
        scissor,
    });
    if draw.batch.data.len() >= 32 << 20 {
        flush(gpu, draw)?;
    }
    if super::trace_enabled() {
        flush(gpu, draw)?;
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

pub(crate) fn flush(gpu: &D3d9Gpu, draw: &mut D3d9DrawState) -> Result<(), RenderError> {
    if draw.batch.draws.is_empty() {
        return Ok(());
    }
    match gpu.validate("DrawFlush", || flush_inner(gpu, draw)) {
        Ok(()) => Ok(()),
        Err(error) => {
            draw.discard_batch();
            Err(error)
        }
    }
}

fn flush_inner(gpu: &D3d9Gpu, draw: &mut D3d9DrawState) -> Result<(), RenderError> {
    if draw.batch.draws.is_empty() {
        return Ok(());
    }
    let needed = draw.batch.data.len().max(4) as u64;
    if needed > gpu.limits().max_buffer_size || needed > u32::MAX as u64 {
        return Err(RenderError::out_of_memory(
            "DrawFlush",
            "draw upload exceeds the device buffer limit",
        ));
    }
    if draw
        .upload
        .as_ref()
        .is_none_or(|buffer| buffer.size() < needed)
    {
        draw.bind_groups.clear();
        let size = needed
            .next_power_of_two()
            .max(4096)
            .min(gpu.limits().max_buffer_size)
            .max(needed);
        draw.upload = Some(gpu.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("d3d9-draw-upload"),
            size,
            usage: wgpu::BufferUsages::UNIFORM
                | wgpu::BufferUsages::VERTEX
                | wgpu::BufferUsages::INDEX
                | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        }));
    }
    let upload = draw
        .upload
        .as_ref()
        .expect("draw upload buffer was created");
    let mut encoder = gpu
        .device
        .create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("d3d9-draw-batch"),
        });
    let staging = draw
        .staging
        .get_or_insert_with(|| wgpu::util::StagingBelt::new(1 << 20));
    staging
        .write_buffer(
            &mut encoder,
            upload,
            0,
            wgpu::BufferSize::new(needed).expect("draw upload is nonempty"),
            &gpu.device,
        )
        .copy_from_slice(&draw.batch.data);
    staging.finish();
    let mut bind_groups = Vec::with_capacity(draw.batch.draws.len());
    for pending in &draw.batch.draws {
        let key = BindGroupKey {
            layout: pending.layout.clone(),
            uniform_sizes: pending.uniform_sizes,
            textures: pending
                .textures
                .iter()
                .map(|texture| (texture.stage, texture.view.clone(), texture.sampler.clone()))
                .collect(),
        };
        let bind_group = if let Some(bind_group) = draw.bind_groups.get(&key) {
            bind_group.clone()
        } else {
            let mut entries = Vec::with_capacity(4 + pending.textures.len() * 2);
            for binding in 0..4 {
                entries.push(wgpu::BindGroupEntry {
                    binding,
                    resource: wgpu::BindingResource::Buffer(wgpu::BufferBinding {
                        buffer: upload,
                        offset: 0,
                        size: wgpu::BufferSize::new(pending.uniform_sizes[binding as usize]),
                    }),
                });
            }
            for texture in &pending.textures {
                entries.push(wgpu::BindGroupEntry {
                    binding: 4 + texture.stage,
                    resource: wgpu::BindingResource::TextureView(&texture.view),
                });
                entries.push(wgpu::BindGroupEntry {
                    binding: 20 + texture.stage,
                    resource: wgpu::BindingResource::Sampler(&texture.sampler),
                });
            }
            let bind_group = gpu.device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some("d3d9-draw-bindings"),
                layout: &pending.layout,
                entries: &entries,
            });
            if draw.bind_groups.len() >= 512 {
                draw.bind_groups.clear();
            }
            draw.bind_groups.insert(key, bind_group.clone());
            bind_group
        };
        bind_groups.push(bind_group);
    }
    let color_view = draw
        .batch
        .color_view
        .as_ref()
        .expect("draw batch has a color target");
    let colors = [Some(wgpu::RenderPassColorAttachment {
        view: color_view,
        resolve_target: None,
        depth_slice: None,
        ops: wgpu::Operations {
            load: wgpu::LoadOp::Load,
            store: wgpu::StoreOp::Store,
        },
    })];
    let depth = draw.batch.depth.as_ref().map(|(view, has_stencil)| {
        wgpu::RenderPassDepthStencilAttachment {
            view,
            depth_ops: Some(wgpu::Operations {
                load: wgpu::LoadOp::Load,
                store: wgpu::StoreOp::Store,
            }),
            stencil_ops: has_stencil.then_some(wgpu::Operations {
                load: wgpu::LoadOp::Load,
                store: wgpu::StoreOp::Store,
            }),
        }
    });
    {
        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
            label: Some("d3d9-draw-batch"),
            color_attachments: &colors,
            depth_stencil_attachment: depth,
            timestamp_writes: None,
            occlusion_query_set: None,
        });
        for (pending, bind_group) in draw.batch.draws.iter().zip(&bind_groups) {
            pass.set_pipeline(&pending.pipeline);
            pass.set_bind_group(0, bind_group, &pending.uniform_offsets);
            if let Some(geometry) = &pending.native_geometry {
                pass.set_vertex_buffer(
                    0,
                    geometry.vertex.slice(
                        geometry.vertex_offset..geometry.vertex_offset + geometry.vertex_size,
                    ),
                );
            } else if pending.has_attributes {
                pass.set_vertex_buffer(
                    0,
                    upload
                        .slice(pending.vertex_offset..pending.vertex_offset + pending.vertex_size),
                );
            }
            pass.set_viewport(
                pending.viewport.x as f32,
                pending.viewport.y as f32,
                pending.viewport.width as f32,
                pending.viewport.height as f32,
                pending.viewport.min_z,
                pending.viewport.max_z,
            );
            pass.set_stencil_reference(pending.stencil_reference);
            let blend = pending.blend_constant;
            pass.set_blend_constant(wgpu::Color {
                r: blend[0] as f64,
                g: blend[1] as f64,
                b: blend[2] as f64,
                a: blend[3] as f64,
            });
            let [left, top, width, height] =
                pending
                    .scissor
                    .unwrap_or([0, 0, draw.batch.width, draw.batch.height]);
            pass.set_scissor_rect(left, top, width, height);
            if let Some(geometry) = &pending.native_geometry {
                pass.set_index_buffer(
                    geometry
                        .index
                        .slice(geometry.index_offset..geometry.index_offset + geometry.index_size),
                    geometry.index_format,
                );
                pass.draw_indexed(0..geometry.count, geometry.base_vertex, 0..1);
            } else if let Some((offset, size, format)) = pending.index_range {
                pass.set_index_buffer(upload.slice(offset..offset + size), format);
                pass.draw_indexed(0..pending.vertex_count, 0, 0..1);
            } else {
                pass.draw(0..pending.vertex_count, 0..1);
            }
        }
    }
    gpu.queue.submit(std::iter::once(encoder.finish()));
    staging.recall();
    draw.batch.draws.clear();
    draw.batch.data.clear();
    draw.batch.color_view = None;
    draw.batch.depth = None;
    draw.batch.last_uniform = None;
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
    flush(gpu, draw)?;
    let depth = depth.filter(|_| flags & 6 != 0);
    let mut source = String::from(
        "struct Params {color:vec4<f32>,depth:vec4<f32>}\n@group(0) @binding(0) var<uniform> params:Params;\n@vertex fn vs_main(@builtin(vertex_index) i:u32)->@builtin(position) vec4<f32>{let p=array<vec2<f32>,3>(vec2<f32>(-1.0,-1.0),vec2<f32>(3.0,-1.0),vec2<f32>(-1.0,3.0));return vec4<f32>(p[i],0.0,1.0);}\n",
    );
    if flags & 2 != 0 {
        source += "struct Out {@location(0) color:vec4<f32>,@builtin(frag_depth) depth:f32}\n@fragment fn ps_main()->Out{var o:Out;o.color=params.color;o.depth=params.depth.x;return o;}\n";
    } else {
        source += "@fragment fn ps_main()->@location(0) vec4<f32>{return params.color;}\n";
    }
    let shader = draw.shader(gpu, &source)?;
    let layout = draw.layout(
        gpu,
        vec![static_uniform_entry(0, 32, wgpu::ShaderStages::FRAGMENT)],
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
            vertex_stride: 0,
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
