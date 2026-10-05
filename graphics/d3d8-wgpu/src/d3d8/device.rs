//! Bounded D3D8 host device: full clear, unlit triangle lists, and present.
//! This is an isolated probe implementation, with no COM or guest addresses.
use super::{
    fixed_function::{
        AlphaTestUniform, FogUniform, FvfLayout, StagesUniform, TEXTURED_WGSL, TransformUniform,
        UNLIT_WGSL, lit_layout, lit_shader_source,
    },
    resource::{IndexedDraw, VertexBuffer, expand_indexed_into},
    state::{DeviceState, MAX_TEXTURE_STAGES},
    stats, survey,
    texture_cache::{TextureCache, TextureKey},
};
use crate::{
    RenderError,
    backend::{GpuContext, OffscreenTarget},
};

#[path = "render_targets.rs"]
mod render_targets;
use render_targets::Targets;

/// `Device::scene_boundary` modes.
pub const SCENE_POST_NONE: u32 = 0;
pub const SCENE_POST_FXAA: u32 = 1;

const CLEAR_TARGET: u32 = 0x1;
const CLEAR_ZBUFFER: u32 = 0x2;
const CLEAR_STENCIL: u32 = 0x4;

/// D3D8 rasterizes to pixel centers at integer framebuffer coordinates, while
/// wgpu's viewport maps NDC onto pixel centers at half-integer coordinates.
/// Shifting the viewport origin by half a pixel makes a primitive that D3D8
/// covers land on the same pixels: without it, an edge that falls exactly on a
/// pixel center is dropped by wgpu's right/bottom fill rule (observed as a
/// 1-pixel black right column and bottom row on a full-screen game quad whose
/// projected edges were exactly screen coordinates 639.5 and 479.5).
const D3D8_HALF_PIXEL: f32 = 0.5;

/// Print the survey table roughly every this many presented frames.
const SURVEY_REPORT_INTERVAL: u64 = 300;

/// Upper bound on resident uploaded textures. The kit releases a texture's
/// cache entry when the guest destroys it; this LRU cap is only a safety net
/// against unbounded growth in a very long session.
const MAX_CACHED_TEXTURES: usize = 4096;

/// Map a `D3DBLENDOP` to the wgpu equivalent. `D3DBLENDOP_SUBTRACT` is the
/// source minus the destination (`wgpu::BlendOperation::Subtract`);
/// `D3DBLENDOP_REVSUBTRACT` is the destination minus the source
/// (`wgpu::BlendOperation::ReverseSubtract`).
fn blend_operation(op: crate::d3d8::enums::D3DBLENDOP) -> wgpu::BlendOperation {
    use crate::d3d8::enums::D3DBLENDOP as O;
    match op {
        O::Add => wgpu::BlendOperation::Add,
        O::Subtract => wgpu::BlendOperation::Subtract,
        O::RevSubtract => wgpu::BlendOperation::ReverseSubtract,
        O::Min => wgpu::BlendOperation::Min,
        O::Max => wgpu::BlendOperation::Max,
    }
}

/// Map a `D3DBLEND` factor to the wgpu equivalent. `BOTHSRCALPHA` and
/// `BOTHINVSRCALPHA` are named errors: see [`Device::blend_state`].
fn blend_factor(factor: crate::d3d8::enums::D3DBLEND) -> Result<wgpu::BlendFactor, RenderError> {
    use crate::d3d8::enums::D3DBLEND as B;
    Ok(match factor {
        B::Zero => wgpu::BlendFactor::Zero,
        B::One => wgpu::BlendFactor::One,
        B::SrcColor => wgpu::BlendFactor::Src,
        B::InvSrcColor => wgpu::BlendFactor::OneMinusSrc,
        B::SrcAlpha => wgpu::BlendFactor::SrcAlpha,
        B::InvSrcAlpha => wgpu::BlendFactor::OneMinusSrcAlpha,
        B::DestAlpha => wgpu::BlendFactor::DstAlpha,
        B::InvDestAlpha => wgpu::BlendFactor::OneMinusDstAlpha,
        B::DestColor => wgpu::BlendFactor::Dst,
        B::InvDestColor => wgpu::BlendFactor::OneMinusDst,
        B::SrcAlphaSat => wgpu::BlendFactor::SrcAlphaSaturated,
        B::BothSrcAlpha | B::BothInvSrcAlpha => {
            return Err(RenderError::new(
                "DrawPrimitive",
                "D3DBLEND_BOTHSRCALPHA/D3DBLEND_BOTHINVSRCALPHA are not representable in wgpu",
            ));
        }
    })
}

/// DIAGNOSTIC ONLY: opt-in cull override from `RECOMP_D3D8_CULL`. This exists
/// to separate a bridge cull-winding bug from guest-side visibility culling in
/// one windowed run; it deviates from faithful D3D8 behaviour and must not be
/// enabled in a validation run. Unset means faithful. `flip` swaps CW and CCW,
/// `none` forces `D3DCULL_NONE`, and `cw`/`ccw` force one mode.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CullOverride {
    None,
    Cw,
    Ccw,
    Flip,
}

fn cull_override() -> Option<CullOverride> {
    static OVERRIDE: std::sync::OnceLock<Option<CullOverride>> = std::sync::OnceLock::new();
    *OVERRIDE.get_or_init(|| {
        let Ok(raw) = std::env::var("RECOMP_D3D8_CULL") else {
            return None;
        };
        let parsed = match raw.as_str() {
            "none" => CullOverride::None,
            "cw" => CullOverride::Cw,
            "ccw" => CullOverride::Ccw,
            "flip" => CullOverride::Flip,
            other => {
                eprintln!(
                    "[d3d8-cull] DIAGNOSTIC RECOMP_D3D8_CULL={other} is not none|cw|ccw|flip; ignoring"
                );
                return None;
            }
        };
        eprintln!(
            "[d3d8-cull] DIAGNOSTIC cull override active: {raw} (deviation from faithful D3D8 cull state)"
        );
        Some(parsed)
    })
}

/// One-shot diagnostic for the leading draws of a run: prints the topology,
/// FVF, counts, viewport, transforms and the first four vertices so a
/// rasterization or coverage mismatch can be attributed to real inputs.
/// Enabled by `RECOMP_D3D8_TRACE_DRAWS`; the value is the number of draws to
/// print (default 8). Unset means no work is done.
fn trace_draw(
    fvf: u32,
    topology: u32,
    layout: &FvfLayout,
    vertices: &VertexBuffer,
    start_vertex: u32,
    primitive_count: u32,
    state: &DeviceState,
    textured: bool,
    stage0: Option<&BoundTexture>,
    stage1: Option<&BoundTexture>,
) {
    use std::sync::atomic::{AtomicU32, Ordering};
    static COUNT: AtomicU32 = AtomicU32::new(0);
    let Ok(raw) = std::env::var("RECOMP_D3D8_TRACE_DRAWS") else {
        return;
    };
    let limit: u32 = raw.parse().unwrap_or(8);
    // `RECOMP_D3D8_TRACE_STAGE1=1` counts and prints only draws with a texture
    // bound at stage 1 (the world draws), so the cap is not spent on menus.
    if std::env::var_os("RECOMP_D3D8_TRACE_STAGE1").is_some() && stage1.is_none() {
        return;
    }
    let n = COUNT.fetch_add(1, Ordering::Relaxed);
    if n >= limit {
        return;
    }
    eprintln!(
        "[d3d8-trace] draw {n}: topo={topology} fvf=0x{fvf:06X} stride={} start={start_vertex} prims={primitive_count} textured={textured}",
        layout.stride
    );
    // Full stage/FVF/render-state dump (tss0 and tss1 included) plus whether a
    // real texture is bound at each stage. This is what distinguishes a
    // two-stage world draw whose stage 1 the bridge currently drops from one
    // that is genuinely single-texture; it is diagnostic only.
    eprintln!("[d3d8-trace]   state: {}", state.draw_state_summary());
    match stage0 {
        Some(t) => eprintln!(
            "[d3d8-trace]   stage0 texture {}x{} fmt={:#x}",
            t.width, t.height, t.format
        ),
        None => eprintln!("[d3d8-trace]   stage0 texture none (white fallback)"),
    }
    match stage1 {
        Some(t) => eprintln!(
            "[d3d8-trace]   stage1 texture {}x{} fmt={:#x}",
            t.width, t.height, t.format
        ),
        None => eprintln!("[d3d8-trace]   stage1 texture none"),
    }
    let vp = state.viewport;
    eprintln!(
        "[d3d8-trace]   viewport=({},{}) {}x{} z=[{},{}] z_enable={} z_write={} z_func={:?}",
        vp.x,
        vp.y,
        vp.width,
        vp.height,
        vp.min_z,
        vp.max_z,
        state.z_enable(),
        state.z_write_enable(),
        state.z_func()
    );
    eprintln!(
        "[d3d8-trace]   cull={:?} blend={} src={:?} dst={:?}",
        state.cull_mode(),
        state.alpha_blend_enable(),
        state.src_blend(),
        state.dest_blend()
    );
    eprintln!("[d3d8-trace]   world={:?}", state.world.rows);
    eprintln!("[d3d8-trace]   view={:?}", state.view.rows);
    eprintln!("[d3d8-trace]   proj={:?}", state.projection.rows);
    let bytes = vertices.bytes();
    let stride = layout.stride as usize;
    for i in 0..4usize {
        let off = (start_vertex as usize + i) * stride;
        if off + stride > bytes.len() {
            break;
        }
        let v = &bytes[off..off + stride];
        let x = f32::from_le_bytes([v[0], v[1], v[2], v[3]]);
        let y = f32::from_le_bytes([v[4], v[5], v[6], v[7]]);
        let z = f32::from_le_bytes([v[8], v[9], v[10], v[11]]);
        let color = u32::from_le_bytes([v[12], v[13], v[14], v[15]]);
        if stride >= 24 {
            let u = f32::from_le_bytes([v[16], v[17], v[18], v[19]]);
            let w = f32::from_le_bytes([v[20], v[21], v[22], v[23]]);
            eprintln!("[d3d8-trace]   v{i} pos=({x},{y},{z}) color=0x{color:08X} uv=({u},{w})");
        } else {
            eprintln!("[d3d8-trace]   v{i} pos=({x},{y},{z}) color=0x{color:08X}");
        }
    }
}

/// A D3D8 texture bound to a stage. It holds its own texture handle so the
/// default white texture (which has no cache entry) stays alive, and a clone
/// of the view so a cache eviction cannot invalidate a bound stage mid-frame.
struct BoundTexture {
    _texture: wgpu::Texture,
    view: wgpu::TextureView,
    /// Kit level-surface COM identity; `0` for the white fallback.
    texture_id: u32,
    width: u32,
    height: u32,
    format: u32,
}

/// One GPU-resident texture level held in [`Device::texture_cache`]. Keyed by
/// the guest texture identity, not by stage, so the same texture bound at two
/// stages or across draws is uploaded once.
struct CachedTexture {
    texture: wgpu::Texture,
    view: wgpu::TextureView,
    width: u32,
    height: u32,
    format: u32,
}

/// A 1x1 opaque white texture, sampled when a stage is active but no texture
/// is bound. D3D8's default texture is white, so an unbound active stage
/// modulates the vertex color by white (a no-op), which this reproduces.
fn create_white_texture(gpu: &GpuContext) -> BoundTexture {
    let texture = gpu.device.create_texture(&wgpu::TextureDescriptor {
        label: Some("D3D8 default white texture"),
        size: wgpu::Extent3d {
            width: 1,
            height: 1,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba8Unorm,
        usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    });
    gpu.queue.write_texture(
        wgpu::TexelCopyTextureInfo {
            texture: &texture,
            mip_level: 0,
            origin: wgpu::Origin3d::ZERO,
            aspect: wgpu::TextureAspect::All,
        },
        &[255, 255, 255, 255],
        wgpu::TexelCopyBufferLayout {
            offset: 0,
            bytes_per_row: Some(4),
            rows_per_image: Some(1),
        },
        wgpu::Extent3d {
            width: 1,
            height: 1,
            depth_or_array_layers: 1,
        },
    );
    BoundTexture {
        view: texture.create_view(&wgpu::TextureViewDescriptor::default()),
        _texture: texture,
        texture_id: 0,
        width: 1,
        height: 1,
        format: crate::d3d8::format::D3DFMT_A8R8G8B8,
    }
}

/// Build a wgpu sampler from the resolved stage filters/address modes. The
/// validator has already rejected values with no wgpu equivalent, so every
/// `unwrap` here is on a value that passed validation.
fn sampler_descriptor(
    stage: &crate::d3d8::state::TextureStage,
) -> wgpu::SamplerDescriptor<'static> {
    use crate::d3d8::enums::{D3DTEXTUREADDRESS as A, D3DTEXTUREFILTERTYPE as F};
    let address = |value: u32| match A::from_raw(value).expect("validated address mode") {
        A::Wrap => wgpu::AddressMode::Repeat,
        A::Mirror => wgpu::AddressMode::MirrorRepeat,
        A::Clamp => wgpu::AddressMode::ClampToEdge,
        A::Border => wgpu::AddressMode::ClampToBorder,
        A::MirrorOnce => unreachable!("validated: MIRRORONCE is refused"),
    };
    let filter = |value: u32| match F::from_raw(value).expect("validated filter") {
        // MaxAnisotropy is 1, so ANISOTROPIC is linear filtering (see
        // DeviceState::resolve_texture_stage).
        F::Linear | F::Anisotropic => wgpu::FilterMode::Linear,
        F::None | F::Point => wgpu::FilterMode::Nearest,
        F::PyramidQuad | F::GaussianQuad => {
            unreachable!("validated: only POINT/LINEAR/NONE/ANISOTROPIC are accepted")
        }
    };
    let border = matches!(A::from_raw(stage.address_u), Ok(A::Border))
        || matches!(A::from_raw(stage.address_v), Ok(A::Border));
    wgpu::SamplerDescriptor {
        label: Some("D3D8 stage 0 sampler"),
        address_mode_u: address(stage.address_u),
        address_mode_v: address(stage.address_v),
        address_mode_w: wgpu::AddressMode::ClampToEdge,
        mag_filter: filter(stage.mag_filter),
        min_filter: filter(stage.min_filter),
        mipmap_filter: filter(stage.mip_filter),
        border_color: border.then_some(wgpu::SamplerBorderColor::TransparentBlack),
        ..Default::default()
    }
}

/// A tiny value-keyed cache with hit/miss counters. Values are cloned out so a
/// caller never holds a map borrow while recording GPU work; used for samplers
/// (and uniform buffers). Counters feed `RECOMP_D3D8_DRAW_STATS`.
struct KeyedCache<K, V> {
    map: std::collections::HashMap<K, V>,
    hits: u64,
    misses: u64,
}

impl<K, V> Default for KeyedCache<K, V> {
    fn default() -> Self {
        Self {
            map: std::collections::HashMap::new(),
            hits: 0,
            misses: 0,
        }
    }
}

impl<K: Eq + std::hash::Hash, V: Clone> KeyedCache<K, V> {
    fn get_or_insert_with(&mut self, key: K, create: impl FnOnce() -> V) -> V {
        if let Some(value) = self.map.get(&key) {
            self.hits += 1;
            return value.clone();
        }
        self.misses += 1;
        let value = create();
        self.map.insert(key, value.clone());
        value
    }
}

/// Everything that varies a sampler object. `address_mode_w` is always
/// `ClampToEdge` and the remaining fields keep their wgpu defaults, so only
/// these raw D3D8 values distinguish two samplers.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct SamplerKey {
    address_u: u32,
    address_v: u32,
    mag_filter: u32,
    min_filter: u32,
    mip_filter: u32,
}

impl SamplerKey {
    fn from_stage(stage: &crate::d3d8::state::TextureStage) -> Self {
        Self {
            address_u: stage.address_u,
            address_v: stage.address_v,
            mag_filter: stage.mag_filter,
            min_filter: stage.min_filter,
            mip_filter: stage.mip_filter,
        }
    }
}

/// Byte offsets of the per-draw uniforms inside `Device::draw_buffer`. Each is
/// a multiple of the 256-byte uniform offset alignment; vertices follow.
const DRAW_UB_TRANSFORM: u64 = 0;
const DRAW_UB_FOG: u64 = 256;
const DRAW_UB_ALPHA_TEST: u64 = 512;
const DRAW_UB_STAGES: u64 = 768;
const DRAW_VERTEX_OFFSET: u64 = 1024;
const _: () = {
    assert!(std::mem::size_of::<TransformUniform>() <= 256);
    assert!(std::mem::size_of::<FogUniform>() <= 256);
    assert!(std::mem::size_of::<AlphaTestUniform>() <= 256);
    assert!(std::mem::size_of::<StagesUniform>() <= 256);
};

/// The raw `MTLTexture*` behind a wgpu texture. Only the Metal backend has one.
#[cfg(target_os = "macos")]
fn native_texture_ptr(texture: &wgpu::Texture) -> Result<*const core::ffi::c_void, RenderError> {
    // SAFETY: the pointer is only borrowed, never released or retained here;
    // the texture outlives every use because the ring owns it.
    unsafe {
        let hal = texture
            .as_hal::<wgpu::hal::api::Metal>()
            .ok_or_else(|| RenderError::new("present_handoff", "texture is not a Metal texture"))?;
        let raw: &metal::TextureRef = hal.raw_handle();
        Ok(raw as *const metal::TextureRef as *const core::ffi::c_void)
    }
}

#[cfg(not(target_os = "macos"))]
fn native_texture_ptr(_: &wgpu::Texture) -> Result<*const core::ffi::c_void, RenderError> {
    Err(RenderError::new(
        "present_handoff",
        "native texture handoff is macOS-only",
    ))
}

/// Slots in the present handoff ring. The host's staging blit of a slot runs
/// asynchronously on its own queue; a slot is reused only after the host
/// clears its busy flag, so the next frame never overwrites pixels in flight.
const FRAME_RING_SLOTS: usize = 3;

/// GPU copies of the backbuffer handed to the host presenter as raw native
/// textures, so a present moves no pixels through the CPU.
struct FrameRing {
    width: u32,
    height: u32,
    textures: Vec<wgpu::Texture>,
    /// 1 while the host still reads the slot. Leaked: the host clears it from a
    /// GPU completion callback that may outlive the device.
    busy: Vec<&'static std::sync::atomic::AtomicU32>,
    next: usize,
}

/// One frame handed to the host. `texture` is a borrowed native texture
/// (`MTLTexture*`), valid while the device lives; the host sets `*busy` to 0
/// when it no longer reads it.
pub struct FrameHandoff {
    pub texture: *const core::ffi::c_void,
    pub busy: &'static std::sync::atomic::AtomicU32,
    pub width: u32,
    pub height: u32,
}

/// Flush a recorded batch once its CPU-side block data reaches this size.
const BATCH_FLUSH_BYTES: usize = 8 << 20;

type TextureGroupKey = (
    wgpu::TextureView,
    wgpu::Sampler,
    wgpu::TextureView,
    wgpu::Sampler,
);
const MAX_TEXTURE_GROUPS: usize = 512;

/// Texture/sampler pairs of a textured draw, captured when it is recorded.
struct StageBindings {
    view0: wgpu::TextureView,
    view1: wgpu::TextureView,
    sampler0: wgpu::Sampler,
    sampler1: wgpu::Sampler,
}

/// One recorded draw. Its uniforms live in `DrawBatch::data` at `ub_base`
/// (at the `DRAW_UB_*` offsets; consecutive draws with identical uniforms share
/// one block) and its vertices at `vertex_base`.
struct PendingDraw {
    pipeline: wgpu::RenderPipeline,
    ub_base: u64,
    vertex_base: u64,
    vertex_len: u64,
    stages: bool,
    textures: Option<StageBindings>,
    viewport: crate::d3d8::state::Viewport,
    first_vertex: u32,
    count: u32,
}

/// Consecutive draws into one render target, replayed as a single render pass
/// and submit by `Device::flush_draws`. The pass uses Load/Store ops, so
/// replaying draws in order is equivalent to one pass per draw. Anything that
/// reads or writes GPU state outside a draw must flush first (clear, texture
/// writes, target changes, readback, present, end of scene).
#[derive(Default)]
struct DrawBatch {
    draws: Vec<PendingDraw>,
    data: Vec<u8>,
    /// Offset of the most recent uniform block in `data`, for sharing.
    last_ub: Option<usize>,
    color_view: Option<wgpu::TextureView>,
    depth: Option<(wgpu::TextureView, bool)>,
}

/// Explicit bind group layouts for the draw pipelines. Group 0 binds the four
/// per-draw uniform blocks with dynamic offsets into the shared draw buffer, so
/// one bind group serves every draw; group 1 (textured only) holds the stage
/// textures and samplers.
struct DrawLayouts {
    /// Pipeline layout for unlit draws: group 0 = transform, fog, alpha test.
    unlit: wgpu::PipelineLayout,
    /// Pipeline layout for textured draws: group 0 adds the stages block.
    textured: wgpu::PipelineLayout,
    bgl_unlit: wgpu::BindGroupLayout,
    bgl_textured: wgpu::BindGroupLayout,
    bgl_stage_textures: wgpu::BindGroupLayout,
}

impl DrawLayouts {
    fn new(gpu: &wgpu::Device) -> Self {
        let uniform = |binding: u32, size: usize| wgpu::BindGroupLayoutEntry {
            binding,
            visibility: wgpu::ShaderStages::VERTEX_FRAGMENT,
            ty: wgpu::BindingType::Buffer {
                ty: wgpu::BufferBindingType::Uniform,
                has_dynamic_offset: true,
                min_binding_size: wgpu::BufferSize::new(size as u64),
            },
            count: None,
        };
        let transform = uniform(0, std::mem::size_of::<TransformUniform>());
        let stages = uniform(1, std::mem::size_of::<StagesUniform>());
        let fog = uniform(2, std::mem::size_of::<FogUniform>());
        let alpha = uniform(3, std::mem::size_of::<AlphaTestUniform>());
        let bgl_unlit = gpu.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("D3D8 unlit uniforms"),
            entries: &[transform, fog, alpha],
        });
        let bgl_textured = gpu.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("D3D8 textured uniforms"),
            entries: &[transform, stages, fog, alpha],
        });
        let tex = |binding: u32| wgpu::BindGroupLayoutEntry {
            binding,
            visibility: wgpu::ShaderStages::FRAGMENT,
            ty: wgpu::BindingType::Texture {
                sample_type: wgpu::TextureSampleType::Float { filterable: true },
                view_dimension: wgpu::TextureViewDimension::D2,
                multisampled: false,
            },
            count: None,
        };
        let samp = |binding: u32| wgpu::BindGroupLayoutEntry {
            binding,
            visibility: wgpu::ShaderStages::FRAGMENT,
            ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
            count: None,
        };
        let bgl_stage_textures = gpu.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("D3D8 stage textures"),
            entries: &[tex(0), samp(1), tex(2), samp(3)],
        });
        let unlit = gpu.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("D3D8 unlit pipeline layout"),
            bind_group_layouts: &[&bgl_unlit],
            push_constant_ranges: &[],
        });
        let textured = gpu.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("D3D8 textured pipeline layout"),
            bind_group_layouts: &[&bgl_textured, &bgl_stage_textures],
            push_constant_ranges: &[],
        });
        Self {
            unlit,
            textured,
            bgl_unlit,
            bgl_textured,
            bgl_stage_textures,
        }
    }
}

/// `RECOMP_D3D8_DRAW_STATS=1` per-frame counters. Deltas are reset after each
/// `Present`, so a line is one frame's draw-path cost; `sampler_hits`/`misses`
/// are cumulative cache counters.
#[derive(Default)]
struct DrawStats {
    draws: u64,
    buffers_created: u64,
    uniform_writes: u64,
    submits: u64,
}

/// Everything that varies a cached draw pipeline. The attachment formats and
/// color write mask belong to the bound render target, which changes on every
/// `SetRenderTarget`, so they are part of the key (clearing the cache on a
/// target switch recompiled every pipeline each frame once shadows rendered
/// to texture targets).
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct DrawPipelineKey {
    color_format: wgpu::TextureFormat,
    color_write_mask: wgpu::ColorWrites,
    depth_format: Option<wgpu::TextureFormat>,
    fvf: u32,
    textured: bool,
    z_enable: bool,
    z_write: bool,
    z_func: u32,
    blend: u32,
    cull: u32,
}

pub struct Device {
    shader: Option<wgpu::ShaderModule>,
    textured_shader: Option<wgpu::ShaderModule>,
    lit_shader: Option<wgpu::ShaderModule>,
    lit_textured_shader: Option<wgpu::ShaderModule>,
    pipelines: std::collections::HashMap<DrawPipelineKey, wgpu::RenderPipeline>,
    pub state: DeviceState,
    pub gpu: GpuContext,
    target: OffscreenTarget,
    targets: Targets,
    /// Bound texture per stage; `None` means the default white texture.
    stage_textures: [Option<BoundTexture>; MAX_TEXTURE_STAGES],
    /// Identity-keyed cache of uploaded texture levels.
    texture_cache: TextureCache<CachedTexture>,
    /// Reused destination for `ColorFormat::to_rgba8_into`, so a genuine upload
    /// does not allocate a fresh RGBA buffer per bind.
    scratch_rgba: Vec<u8>,
    /// Descriptor-keyed sampler cache; D3D8 stage sampling state rarely changes.
    samplers: KeyedCache<SamplerKey, wgpu::Sampler>,
    /// Reused index-expansion output (triangle-list expansion of a guest index
    /// buffer); sized and fully overwritten by [`Device::draw_indexed_primitive`].
    index_scratch: Vec<u8>,
    /// Reused software-lighting output for the `0x152` FVF; sized and fully
    /// overwritten by `light_vertices`.
    lit_scratch: Vec<u8>,
    /// One persistent buffer holding a draw's uniforms (at the `DRAW_UB_*`
    /// offsets) and vertices (at `DRAW_VERTEX_OFFSET`), filled by a single
    /// `queue.write_buffer_with` per draw (five separate writes allocated five
    /// staging buffers). Safe because each draw submits its own command buffer
    /// (queue writes are ordered before the following submit); batching submits
    /// would require a per-draw ring.
    draw_buffer: std::cell::RefCell<Option<wgpu::Buffer>>,
    draw_layouts: DrawLayouts,
    /// Group-0 bind groups over `draw_buffer` (unlit, textured); the real block
    /// offsets are dynamic. Dropped when the buffer is recreated.
    uniform_groups: std::cell::RefCell<[Option<wgpu::BindGroup>; 2]>,
    /// Stage texture/sampler groups, keyed by the bound objects. Cleared when
    /// it grows past `MAX_TEXTURE_GROUPS` so it cannot pin textures forever.
    texture_groups: std::cell::RefCell<std::collections::HashMap<TextureGroupKey, wgpu::BindGroup>>,
    batch: std::cell::RefCell<DrawBatch>,
    /// Present handoff ring (see [`FrameRing`]); created on first use.
    frame_ring: std::cell::RefCell<Option<FrameRing>>,
    /// The batch's target needs `publish_target` after it is flushed.
    publish_pending: std::cell::Cell<bool>,
    submits: std::cell::Cell<u64>,
    buffers_created_at_flush: std::cell::Cell<u64>,
    /// `RECOMP_D3D8_DRAW_STATS=1` draw-path counters, reported per frame.
    draw_stats_enabled: bool,
    frame_stats: DrawStats,
    white: BoundTexture,
    scene: bool,
    /// `RECOMP_D3D8_SURVEY=1`: record and skip unsupported draws.
    survey: bool,
    /// `RECOMP_D3D8_STRICT_SCOPES=1`: restore one validation error scope per
    /// draw instead of one per frame. Debug aid only.
    strict_scopes: bool,
    /// True while a per-frame validation error scope is open (managed mode).
    frame_scope_open: bool,
    frame_index: u64,
    draw_index: u64,
}

impl Device {
    pub fn new(
        gpu: GpuContext,
        width: u32,
        height: u32,
        format: u32,
        depth_format: u32,
    ) -> Result<Self, RenderError> {
        let target = gpu.create_target(width, height, format, depth_format)?;
        let white = create_white_texture(&gpu);
        let draw_layouts = DrawLayouts::new(&gpu.device);
        let survey = survey::enabled();
        if survey {
            survey::register_exit_report();
        }
        let strict_scopes = std::env::var("RECOMP_D3D8_STRICT_SCOPES").as_deref() == Ok("1");
        let draw_stats_enabled = std::env::var("RECOMP_D3D8_DRAW_STATS").as_deref() == Ok("1");
        stats::register_exit_report();
        Ok(Self {
            shader: None,
            textured_shader: None,
            lit_shader: None,
            lit_textured_shader: None,
            pipelines: std::collections::HashMap::new(),
            state: DeviceState::new(width, height),
            stage_textures: std::array::from_fn(|_| None),
            texture_cache: TextureCache::new(MAX_CACHED_TEXTURES),
            scratch_rgba: Vec::new(),
            samplers: KeyedCache::default(),
            index_scratch: Vec::new(),
            lit_scratch: Vec::new(),
            draw_buffer: Default::default(),
            draw_layouts,
            uniform_groups: Default::default(),
            texture_groups: Default::default(),
            batch: Default::default(),
            frame_ring: Default::default(),
            publish_pending: Default::default(),
            submits: Default::default(),
            buffers_created_at_flush: Default::default(),
            draw_stats_enabled,
            frame_stats: DrawStats::default(),
            white,
            gpu,
            targets: Targets::new(target.clone()),
            target,
            scene: false,
            survey,
            strict_scopes,
            frame_scope_open: false,
            frame_index: 0,
            draw_index: 0,
        })
    }

    /// `IDirect3DDevice8::Clear`. `flags` is `D3DCLEAR_*`; `argb` is the color,
    /// `z` the depth value and `stencil` the stencil value. Only a full-target
    /// clear (no rectangles) is supported, which is what the guest uses; the
    /// flags select color and/or depth/stencil exactly, with no silent drop.
    pub fn clear(
        &mut self,
        rect_count: u32,
        flags: u32,
        argb: u32,
        z: f32,
        stencil: u32,
    ) -> Result<(), RenderError> {
        if rect_count != 0 {
            return Err(RenderError::new(
                "Clear",
                "clear rectangles are unsupported; only a full-target clear is implemented",
            ));
        }
        if flags & !(CLEAR_TARGET | CLEAR_ZBUFFER | CLEAR_STENCIL) != 0 || flags == 0 {
            return Err(RenderError::new(
                "Clear",
                format!("unsupported D3DCLEAR flags {flags:#x}"),
            ));
        }
        if !z.is_finite() || !(0.0..=1.0).contains(&z) {
            return Err(RenderError::new(
                "Clear",
                format!("depth clear value {z} is outside [0,1]"),
            ));
        }
        self.flush_draws();
        let v = self.state.viewport;
        self.gpu.clear_region(
            &self.target,
            flags,
            argb,
            z,
            stencil,
            v.x,
            v.y,
            v.width,
            v.height,
        )?;
        self.publish_target();
        Ok(())
    }

    /// `IDirect3DDevice8::SetTexture`. `data` is the level-0 CPU texel block
    /// (`width * height * bytes_per_pixel`, in the source D3D8 layout); empty
    /// or `texture_id == 0` unbinds and samples the default white texture.
    ///
    /// `texture_id`/`level`/`generation` are the kit's identity and content
    /// version for the level (see [`crate::d3d8::texture_cache`]);
    /// `force_upload` is set while a lock is still open. An unchanged level at
    /// the same generation is a cache hit and reuses its wgpu texture, so the
    /// same texture bound at either stage or on a later draw costs one upload.
    ///
    /// Textures are converted to linear UNORM `Rgba8Unorm` on upload; the
    /// supported source formats are `A8R8G8B8`, `X8R8G8B8`, `R5G6B5`,
    /// `A1R5G5B5` and `A4R4G4B4`, and every other format is a named error. Mip
    /// level 0 is the only level uploaded, matching the bounded
    /// `D3DTSS_MIPFILTER` handling in [`DeviceState::resolve_texture_stage`].
    #[allow(clippy::too_many_arguments)]
    pub fn set_texture(
        &mut self,
        stage: u32,
        texture_id: u32,
        level: u32,
        generation: u64,
        force_upload: bool,
        format: u32,
        width: u32,
        height: u32,
        data: &[u8],
    ) -> Result<(), RenderError> {
        use crate::d3d8::format::ColorFormat;
        if stage as usize >= MAX_TEXTURE_STAGES {
            return Err(RenderError::new(
                "SetTexture",
                format!("stage {stage} is beyond D3D8's {MAX_TEXTURE_STAGES} texture stages"),
            ));
        }
        if data.is_empty() || texture_id == 0 {
            self.stage_textures[stage as usize] = None;
            return Ok(());
        }
        if width == 0 || height == 0 {
            return Err(RenderError::new(
                "SetTexture",
                "bound texture dimensions must be nonzero",
            ));
        }
        // Block-compressed levels are sized by their block layout; the rest by
        // their per-texel layout. Both decode to the same upload shape below.
        let block = crate::d3d8::format::block_bytes(format);
        let color = if block == 0 {
            Some(ColorFormat::from_d3dformat(format)?)
        } else {
            None
        };
        let expected = if let Some(color) = color {
            (width as usize) * (height as usize) * (color.bytes_per_pixel() as usize)
        } else {
            crate::d3d8::format::block_level_layout(width, height, format).1 as usize
        };
        if data.len() < expected {
            return Err(RenderError::new(
                "SetTexture",
                format!(
                    "bound level has {} bytes but {width}x{height} {format} needs {expected}",
                    data.len()
                ),
            ));
        }
        let max = self.gpu.device.limits().max_texture_dimension_2d;
        if width > max || height > max {
            return Err(RenderError::new(
                "SetTexture",
                format!("bound texture {width}x{height} exceeds max_texture_dimension_2d {max}"),
            ));
        }

        stats::record_bind();
        let key = TextureKey::new(texture_id, level);
        if self.targets.textures.contains_key(&key) {
            self.flush_draws();
            self.sync_target_cpu(key, generation, format, width, height, data, force_upload)?;
            let rt = &self.targets.textures[&key];
            self.stage_textures[stage as usize] = Some(BoundTexture {
                _texture: rt.surface.texture.clone(),
                view: rt.surface.view.clone(),
                texture_id,
                width,
                height,
                format,
            });
            return Ok(());
        }
        if self
            .texture_cache
            .lookup(key, generation, force_upload, width, height, format)
        {
            let cached = self
                .texture_cache
                .resource(key)
                .expect("hit implies present");
            self.stage_textures[stage as usize] = Some(BoundTexture {
                _texture: cached.texture.clone(),
                view: cached.view.clone(),
                texture_id,
                width,
                height,
                format,
            });
            return Ok(());
        }

        // Miss: convert into the reusable scratch buffer, then either update
        // the resident texture in place (same shape) or create one. Conversion
        // happens only on a real content change now.
        // The upload below may rewrite a texture earlier recorded draws sample.
        self.flush_draws();
        match color {
            Some(color) => color.to_rgba8_into(&data[..expected], &mut self.scratch_rgba),
            None => crate::d3d8::format::decode_block_into(
                format,
                &data[..expected],
                width,
                height,
                &mut self.scratch_rgba,
            )?,
        }
        stats::record_upload();
        let same_shape = self
            .texture_cache
            .resource(key)
            .is_some_and(|c| c.width == width && c.height == height && c.format == format);
        if same_shape {
            let cached = self.texture_cache.resource(key).expect("checked above");
            self.gpu.queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &cached.texture,
                    mip_level: 0,
                    origin: wgpu::Origin3d::ZERO,
                    aspect: wgpu::TextureAspect::All,
                },
                &self.scratch_rgba,
                wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(width * 4),
                    rows_per_image: Some(height),
                },
                wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: 1,
                },
            );
            self.texture_cache
                .mark_uploaded(key, generation, width, height, format);
        } else {
            self.gpu
                .device
                .push_error_scope(wgpu::ErrorFilter::Validation);
            let texture = self.gpu.device.create_texture(&wgpu::TextureDescriptor {
                label: Some("D3D8 stage texture"),
                size: wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: 1,
                },
                mip_level_count: 1,
                sample_count: 1,
                dimension: wgpu::TextureDimension::D2,
                format: wgpu::TextureFormat::Rgba8Unorm,
                usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
                view_formats: &[],
            });
            if let Some(err) = pollster::block_on(self.gpu.device.pop_error_scope()) {
                return Err(RenderError::new("SetTexture", err.to_string()));
            }
            self.gpu.queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &texture,
                    mip_level: 0,
                    origin: wgpu::Origin3d::ZERO,
                    aspect: wgpu::TextureAspect::All,
                },
                &self.scratch_rgba,
                wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(width * 4),
                    rows_per_image: Some(height),
                },
                wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: 1,
                },
            );
            let view = texture.create_view(&wgpu::TextureViewDescriptor::default());
            self.texture_cache.insert(
                key,
                CachedTexture {
                    texture,
                    view,
                    width,
                    height,
                    format,
                },
                generation,
                width,
                height,
                format,
            );
        }
        let cached = self.texture_cache.resource(key).expect("just stored");
        self.stage_textures[stage as usize] = Some(BoundTexture {
            _texture: cached.texture.clone(),
            view: cached.view.clone(),
            texture_id,
            width,
            height,
            format,
        });
        Ok(())
    }

    /// Drop every resident level of a guest texture. Called from the kit when
    /// the texture's refcount reaches zero, so GPU textures do not outlive the
    /// guest object. The identity is never reused, so no stale entry can be
    /// read by a later texture.
    pub fn release_texture(&mut self, texture_id: u32) {
        if texture_id == 0 {
            return;
        }
        self.texture_cache.remove_texture(texture_id);
        self.targets
            .textures
            .retain(|key, _| key.texture_id != texture_id);
        for slot in &mut self.stage_textures {
            if slot.as_ref().is_some_and(|t| t.texture_id == texture_id) {
                *slot = None;
            }
        }
    }

    /// True when a depth attachment was created for the device.
    pub fn has_depth(&self) -> bool {
        self.target.depth.is_some()
    }

    /// The wgpu depth-stencil state a draw pipeline needs for the current
    /// D3D8 render state. `None` when depth testing is disabled (or the device
    /// has no depth attachment); otherwise it carries `D3DRS_ZWRITEENABLE` and
    /// `D3DRS_ZFUNC` and the attachment format. Stencil is not part of the
    /// unlit slice, so its wgpu state is all-off/keep.
    ///
    /// wgpu requires a pipeline's depth-stencil format to match the render
    /// pass attachment, so whenever the device owns a depth attachment the
    /// pipeline declares it. `D3DRS_ZENABLE` then selects behaviour: with
    /// depth off the pipeline compares `Always` and writes nothing, which is
    /// what D3D8 does (the attachment stays bound but does not affect output).
    fn depth_stencil_state(&self) -> Option<wgpu::DepthStencilState> {
        use crate::d3d8::enums::D3DCMPFUNC;
        let depth = self.target.depth.as_ref()?;
        let (compare, write) = if self.state.z_enable() {
            let compare = match self.state.z_func() {
                D3DCMPFUNC::Never => wgpu::CompareFunction::Never,
                D3DCMPFUNC::Less => wgpu::CompareFunction::Less,
                D3DCMPFUNC::Equal => wgpu::CompareFunction::Equal,
                D3DCMPFUNC::LessEqual => wgpu::CompareFunction::LessEqual,
                D3DCMPFUNC::Greater => wgpu::CompareFunction::Greater,
                D3DCMPFUNC::NotEqual => wgpu::CompareFunction::NotEqual,
                D3DCMPFUNC::GreaterEqual => wgpu::CompareFunction::GreaterEqual,
                D3DCMPFUNC::Always => wgpu::CompareFunction::Always,
            };
            (compare, self.state.z_write_enable())
        } else {
            (wgpu::CompareFunction::Always, false)
        };
        Some(wgpu::DepthStencilState {
            format: depth.format,
            depth_write_enabled: write,
            depth_compare: compare,
            stencil: wgpu::StencilState::default(),
            bias: wgpu::DepthBiasState::default(),
        })
    }

    /// Map the current D3D8 alpha-blend state onto a wgpu pipeline blend state.
    ///
    /// `None` when `D3DRS_ALPHABLENDENABLE` is FALSE (opaque). D3D8 has no
    /// separate alpha blend, so the same source/destination factors apply to
    /// the alpha channel. The two D3D8 "both" factors are rejected by name:
    /// their documented meaning (source alpha for both factors) does not map
    /// onto wgpu's independent colour/alpha components without an unproven
    /// reading, so the draw fails loudly instead of approximating.
    fn blend_state(&self) -> Result<Option<wgpu::BlendState>, RenderError> {
        if !self.state.alpha_blend_enable() {
            return Ok(None);
        }
        let src = blend_factor(self.state.src_blend())?;
        let dst = blend_factor(self.state.dest_blend())?;
        let op = blend_operation(self.state.blend_op());
        Ok(Some(wgpu::BlendState {
            color: wgpu::BlendComponent {
                src_factor: src,
                dst_factor: dst,
                operation: op,
            },
            alpha: wgpu::BlendComponent {
                src_factor: src,
                dst_factor: dst,
                operation: op,
            },
        }))
    }

    /// Stable key for the blend part of the pipeline cache key. Zero when
    /// blending is off.
    fn blend_key(&self) -> u32 {
        if !self.state.alpha_blend_enable() {
            return 0;
        }
        (self.state.blend_op().raw() & 0xff)
            | ((self.state.src_blend().raw() & 0xff) << 8)
            | ((self.state.dest_blend().raw() & 0xff) << 16)
    }

    /// Map `D3DRS_CULLMODE` onto wgpu's cull face and front-face winding.
    ///
    /// D3D8 and WebGPU both define front-face winding in framebuffer/screen
    /// space (y down), so the cull sense carries over directly: `D3DCULL_CW`
    /// culls clockwise faces, keeping counter-clockwise as front, and
    /// `D3DCULL_CCW` the opposite. The readback test
    /// (`probe` "cull winding/readback") exercises all three modes; see the
    /// 2026-10 cull verdict in the report.
    fn cull_state(&self) -> (Option<wgpu::Face>, wgpu::FrontFace) {
        Self::cull_mode_to_wgpu(self.effective_cull_mode())
    }

    /// Pure `D3DCULL` -> wgpu cull/front-face mapping, split out so the winding
    /// convention is unit-testable without a device. D3D8 culls the named winding
    /// (so `D3DCULL_CCW` keeps clockwise as front) and wgpu's framebuffer-space
    /// `FrontFace` uses the same y-down convention.
    fn cull_mode_to_wgpu(
        mode: crate::d3d8::enums::D3DCULL,
    ) -> (Option<wgpu::Face>, wgpu::FrontFace) {
        use crate::d3d8::enums::D3DCULL;
        match mode {
            D3DCULL::None => (None, wgpu::FrontFace::Ccw),
            D3DCULL::Cw => (Some(wgpu::Face::Back), wgpu::FrontFace::Ccw),
            D3DCULL::Ccw => (Some(wgpu::Face::Back), wgpu::FrontFace::Cw),
        }
    }

    /// The cull mode the pipeline will actually use, after the diagnostic
    /// override in [`cull_override`]. Faithful D3D8 behaviour when the
    /// override is unset.
    fn effective_cull_mode(&self) -> crate::d3d8::enums::D3DCULL {
        use crate::d3d8::enums::D3DCULL;
        let mode = self.state.cull_mode();
        match cull_override() {
            None => mode,
            Some(CullOverride::None) => D3DCULL::None,
            Some(CullOverride::Cw) => D3DCULL::Cw,
            Some(CullOverride::Ccw) => D3DCULL::Ccw,
            Some(CullOverride::Flip) => match mode {
                D3DCULL::Cw => D3DCULL::Ccw,
                D3DCULL::Ccw => D3DCULL::Cw,
                D3DCULL::None => D3DCULL::None,
            },
        }
    }

    /// Resolve the D3D8 table/pixel fog uniform for the current state.
    ///
    /// The D3D8 rule for additive blending adjusts the fog colour to black;
    /// `validate_unlit` has already refused the blend combinations whose
    /// adjustment is not implemented, so the cases handled here (opaque, the
    /// standard INVSRCALPHA destination, and DESTBLEND=ONE) are the only ones a
    /// validated draw can hold.
    fn fog_uniform(&self) -> FogUniform {
        use crate::d3d8::enums::D3DBLEND;
        let enable = self.state.fog_enable();
        let mut color = crate::d3d8::fixed_function::d3dcolor_to_rgba(self.state.fog_color());
        if enable && self.state.alpha_blend_enable() && self.state.dest_blend() == D3DBLEND::One {
            color = [0.0, 0.0, 0.0, 1.0];
        }
        // D3D8: a non-NONE FOGTABLEMODE selects per-pixel table fog; otherwise
        // FOGVERTEXMODE selects per-vertex fog, the only kind range-based
        // distance applies to (`validate_*` refuses range with table fog).
        let table = self.state.fog_table_mode();
        let vertex_fog = table == 0;
        FogUniform::new(
            enable,
            if vertex_fog {
                self.state.fog_vertex_mode()
            } else {
                table
            },
            vertex_fog,
            vertex_fog && self.state.range_fog_enable(),
            self.state.fog_start(),
            self.state.fog_end(),
            self.state.fog_density(),
            color,
            self.state.world.mul(self.state.view),
        )
    }

    pub fn begin_scene(&mut self) -> Result<(), RenderError> {
        if self.scene {
            return Err(RenderError::new("BeginScene", "scene already open"));
        }
        self.scene = true;
        Ok(())
    }

    pub fn end_scene(&mut self) -> Result<(), RenderError> {
        if !self.scene {
            return Err(RenderError::new("EndScene", "no open scene"));
        }
        self.scene = false;
        self.flush_draws();
        self.finish_frame_scope()?;
        Ok(())
    }

    pub fn draw_primitive(
        &mut self,
        topology: u32,
        fvf: u32,
        vertices: &VertexBuffer,
        start_vertex: u32,
        primitive_count: u32,
    ) -> Result<(), RenderError> {
        if !self.scene {
            return Err(RenderError::new(
                "DrawPrimitive",
                "requires an open scene (BeginScene)",
            ));
        }
        // A zero-primitive draw is a legal no-op in D3D, not an error. The
        // degenerate tail of a strip or fan (two or fewer vertices) lands here.
        if primitive_count == 0 {
            return Ok(());
        }
        self.draw_index += 1;
        self.frame_stats.draws += 1;
        // Survey mode records and skips a state/FVF/TSS rejection so one run
        // can enumerate every unsupported draw. Argument/range errors below
        // deliberately bypass this and stay hard failures.
        macro_rules! survey_or_skip {
            ($expr:expr) => {
                match $expr {
                    Ok(value) => value,
                    Err(error) => {
                        if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                            return Ok(());
                        }
                        return Err(error);
                    }
                }
            };
        }
        if topology != 4 {
            let error = RenderError::new(
                "DrawPrimitive",
                format!("unsupported topology {topology}; expected TRIANGLELIST (4)"),
            );
            if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                return Ok(());
            }
            return Err(error);
        }
        survey_or_skip!(self.state.validate_draw(fvf));
        if self.state.z_enable() && !self.has_depth() {
            let error = RenderError::new(
                "DrawPrimitive",
                "D3DRS_ZENABLE is on but the device has no depth attachment",
            );
            if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                return Ok(());
            }
            return Err(error);
        }
        let layout = survey_or_skip!(FvfLayout::decode(fvf));
        if u64::from(vertices.stride) != layout.stride {
            let error = RenderError::new("SetStreamSource", "stride does not match FVF");
            if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                return Ok(());
            }
            return Err(error);
        }
        let count = primitive_count
            .checked_mul(3)
            .and_then(|n| start_vertex.checked_add(n))
            .ok_or_else(|| RenderError::new("DrawPrimitive", "vertex range overflow"))?;
        if count as u64 * layout.stride > vertices.bytes().len() as u64 {
            return Err(RenderError::new(
                "DrawPrimitive",
                "vertex range exceeds buffer",
            ));
        }
        // Resolve both implemented stages. A stage with no explicit op and no
        // bound texture stays inactive and passes CURRENT through, preserving
        // the established untextured path. Stage >= 2 that is active fails by
        // name inside the resolver and is recorded by the survey.
        let stage0 = survey_or_skip!(
            self.state
                .resolve_texture_stage(0, self.stage_textures[0].is_some())
        );
        let stage1 = survey_or_skip!(
            self.state
                .resolve_texture_stage(1, self.stage_textures[1].is_some())
        );
        let textured = stage0.active || stage1.active;
        if let Some(key) = self.targets.current {
            for (stage, active) in [(0, stage0.active), (1, stage1.active)] {
                if active
                    && self.stage_textures[stage]
                        .as_ref()
                        .is_some_and(|t| t.texture_id == key.texture_id)
                {
                    return Err(RenderError::new(
                        "DrawPrimitive",
                        "sampling the current color render target is unsupported",
                    ));
                }
            }
        }
        if textured && !layout.attributes.iter().any(|a| a.shader_location == 2) {
            let error = RenderError::new(
                "DrawPrimitive",
                "an active texture stage requires D3DFVF_TEX1 texture coordinates",
            );
            if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                return Ok(());
            }
            return Err(error);
        }
        // Only the two-set FVF carries texcoord set 1. A single-set FVF aliases
        // set 1 to set 0 for the shared shader, but a stage that selects the
        // missing set is not an error and is not aliased: D3D8 fixed-function
        // leaves an undeclared coordinate uninitialized, and the reference
        // implementations resolve that to `(0, 0)`. `StagesUniform::for_fvf`
        // records which selected sets the FVF carries so the shader substitutes
        // the zero coordinate, matching WineD3D/DXVK rather than failing the
        // draw.
        trace_draw(
            fvf,
            topology,
            &layout,
            vertices,
            start_vertex,
            primitive_count,
            &self.state,
            textured,
            self.stage_textures[0].as_ref(),
            self.stage_textures[1].as_ref(),
        );
        let v = &self.state.viewport;
        if v.width == 0
            || v.height == 0
            || v.x
                .checked_add(v.width)
                .is_none_or(|n| n > self.target.width)
            || v.y
                .checked_add(v.height)
                .is_none_or(|n| n > self.target.height)
            || !v.min_z.is_finite()
            || !v.max_z.is_finite()
            || v.min_z < 0.0
            || v.max_z > 1.0
            || v.min_z > v.max_z
        {
            return Err(RenderError::new(
                "SetViewport",
                "viewport outside target or invalid depth interval",
            ));
        }
        // Target formats are fixed for this device. The supported slice only
        // varies vertex layout and effective depth/blend pipeline state.
        let depth_state = self.depth_stencil_state();
        let blend = survey_or_skip!(self.blend_state());
        let (cull_mode, front_face) = self.cull_state();
        let key = DrawPipelineKey {
            color_format: self.target.format,
            color_write_mask: self.target.color_write_mask(),
            depth_format: self.target.depth.as_ref().map(|d| d.format),
            fvf,
            textured,
            z_enable: self.state.z_enable(),
            z_write: self.state.z_enable() && self.state.z_write_enable(),
            z_func: if self.state.z_enable() {
                self.state.z_func().raw()
            } else {
                8
            },
            blend: self.blend_key(),
            cull: self.effective_cull_mode().raw(),
        };
        // Resolve fog and alpha-test state before the shader borrow because
        // these methods read several state fields (a call borrows all of `self`).
        let fog_uniform = self.fog_uniform();
        let alpha_test_uniform = AlphaTestUniform::new(
            self.state.alpha_test_enable(),
            self.state.alpha_func().raw(),
            self.state.alpha_ref(),
        );
        // Compute fixed-function vertex lighting before rasterization. Only
        // the requested vertex interval is read; prefix slots preserve draw's
        // start_vertex without touching unused guest vertices. The scratch is
        // moved out for the duration of the draw so the rest of this method can
        // borrow `self` mutably; it is restored below before it is reused.
        let mut lit_scratch = if fvf == 0x152 {
            Some(std::mem::take(&mut self.lit_scratch))
        } else {
            None
        };
        if let Some(out) = lit_scratch.as_mut() {
            if let Err(error) = self.state.light_vertices(
                vertices.bytes(),
                start_vertex as usize,
                count as usize,
                out,
            ) {
                self.lit_scratch = lit_scratch.take().unwrap();
                if self.note_draw_rejection(&error, topology, fvf, vertices.stride) {
                    return Ok(());
                }
                return Err(error);
            }
        }
        let lit_bytes = lit_scratch.as_deref();
        let upload_layout = if lit_bytes.is_some() {
            lit_layout()
        } else {
            layout.clone()
        };
        let gpu = &self.gpu.device;
        if self.strict_scopes {
            gpu.push_error_scope(wgpu::ErrorFilter::Validation);
        } else if !self.frame_scope_open {
            // Managed mode: one validation error scope per frame. A validation
            // error is retained until EndScene, which fails the frame loudly
            // instead of polling the device after every draw (the per-draw
            // pop was the main device poll / Metal fence wait in a frame).
            gpu.push_error_scope(wgpu::ErrorFilter::Validation);
            self.frame_scope_open = true;
        }
        let shader = if fvf == 0x152 {
            let slot = if textured {
                &mut self.lit_textured_shader
            } else {
                &mut self.lit_shader
            };
            slot.get_or_insert_with(|| {
                gpu.create_shader_module(wgpu::ShaderModuleDescriptor {
                    label: Some("D3D8 software-lit float diffuse"),
                    source: wgpu::ShaderSource::Wgsl(lit_shader_source(textured).into()),
                })
            })
        } else if textured {
            self.textured_shader.get_or_insert_with(|| {
                gpu.create_shader_module(wgpu::ShaderModuleDescriptor {
                    label: Some("D3D8 textured FVF"),
                    source: wgpu::ShaderSource::Wgsl(TEXTURED_WGSL.into()),
                })
            })
        } else {
            self.shader.get_or_insert_with(|| {
                gpu.create_shader_module(wgpu::ShaderModuleDescriptor {
                    label: Some("D3D8 unlit FVF"),
                    source: wgpu::ShaderSource::Wgsl(UNLIT_WGSL.into()),
                })
            })
        };
        let mut uniform =
            TransformUniform::new(self.state.world, self.state.view, self.state.projection);
        if layout.pre_transformed {
            // XYZRHW: the vertex entry point maps screen pixels to NDC through
            // the same viewport the rasterizer uses. `rhw[0]` marks it; the
            // reciprocal-w field becomes clip w when nonzero (else 1.0).
            uniform.viewport = [v.x as f32, v.y as f32, v.width as f32, v.height as f32];
            uniform.rhw[0] = 1;
        }
        // D3DRS_SPECULARENABLE gates the shader's specular add.
        uniform.rhw[1] = u32::from(self.state.specular_enable());
        let stages_uniform =
            textured.then(|| StagesUniform::for_fvf(&stage0, &stage1, layout.texcoord_sets));
        // Upload only the vertex bytes this draw reads. Both the lit stream and
        // `count * stride` are multiples of `COPY_BUFFER_ALIGNMENT` (4), which
        // `queue.write_buffer_with` requires; the guest buffer's trailing bytes
        // are not part of the draw and are not uploaded.
        let (vertex_source, vertex_len) = if let Some(lit) = lit_bytes {
            (lit, lit.len())
        } else {
            let used = count as usize * vertices.stride as usize;
            (&vertices.bytes()[..used], used)
        };
        let (ub_base, vertex_base) = {
            let mut batch = self.batch.borrow_mut();
            if batch.draws.is_empty() {
                batch.color_view = Some(self.target.view.clone());
                batch.depth = self
                    .target
                    .depth
                    .as_ref()
                    .map(|d| (d.view.clone(), d.has_stencil));
            }
            // Uniform blocks are rebuilt on the stack and shared with the
            // previous draw when byte-identical: state rarely changes between
            // consecutive draws, and the block is 1 KB against a few hundred
            // bytes of vertices.
            let mut ub = [0u8; DRAW_VERTEX_OFFSET as usize];
            let put = |region: &mut [u8], offset: u64, bytes: &[u8]| {
                region[offset as usize..offset as usize + bytes.len()].copy_from_slice(bytes);
            };
            put(&mut ub, DRAW_UB_TRANSFORM, bytemuck::bytes_of(&uniform));
            put(&mut ub, DRAW_UB_FOG, bytemuck::bytes_of(&fog_uniform));
            put(
                &mut ub,
                DRAW_UB_ALPHA_TEST,
                bytemuck::bytes_of(&alpha_test_uniform),
            );
            if let Some(stages) = &stages_uniform {
                put(&mut ub, DRAW_UB_STAGES, bytemuck::bytes_of(stages));
            }
            let shared = batch
                .last_ub
                .filter(|&at| batch.data[at..at + ub.len()] == ub);
            let ub_base = match shared {
                Some(at) => at,
                None => {
                    // Uniform binding offsets need 256-byte alignment.
                    let at = batch.data.len().next_multiple_of(256);
                    batch.data.resize(at, 0);
                    batch.data.extend_from_slice(&ub);
                    batch.last_ub = Some(at);
                    at
                }
            };
            // Vertex buffer offsets need 4-byte alignment only.
            let vertex_base = batch.data.len().next_multiple_of(4);
            batch.data.resize(vertex_base, 0);
            batch.data.extend_from_slice(vertex_source);
            // `write_buffer` needs a multiple of COPY_BUFFER_ALIGNMENT.
            let padded = batch.data.len().next_multiple_of(4);
            batch.data.resize(padded, 0);
            (ub_base as u64, vertex_base as u64)
        };
        self.frame_stats.uniform_writes += 1;
        if let Some(scratch) = lit_scratch.take() {
            self.lit_scratch = scratch;
        }
        let pipeline = self.pipelines.entry(key).or_insert_with(|| {
            gpu.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
                label: Some(if textured {
                    "D3D8 textured triangle"
                } else {
                    "D3D8 unlit triangle"
                }),
                layout: Some(if textured {
                    &self.draw_layouts.textured
                } else {
                    &self.draw_layouts.unlit
                }),
                vertex: wgpu::VertexState {
                    module: shader,
                    entry_point: Some(if layout.pre_transformed {
                        "vs_rhw_main"
                    } else {
                        "vs_main"
                    }),
                    compilation_options: Default::default(),
                    buffers: &[upload_layout.vertex_buffer_layout()],
                },
                primitive: wgpu::PrimitiveState {
                    topology: wgpu::PrimitiveTopology::TriangleList,
                    cull_mode,
                    front_face,
                    ..Default::default()
                },
                depth_stencil: depth_state,
                multisample: Default::default(),
                fragment: Some(wgpu::FragmentState {
                    module: shader,
                    entry_point: Some("fs_main"),
                    compilation_options: Default::default(),
                    targets: &[Some(wgpu::ColorTargetState {
                        format: self.target.format,
                        blend,
                        write_mask: self.target.color_write_mask(),
                    })],
                }),
                multiview: None,
                cache: None,
            })
        });
        let pipeline = pipeline.clone();
        let textures = if textured {
            let view0 = self.stage_textures[0]
                .as_ref()
                .map(|t| &t.view)
                .unwrap_or(&self.white.view);
            let view1 = self.stage_textures[1]
                .as_ref()
                .map(|t| &t.view)
                .unwrap_or(&self.white.view);
            let sampler0 = self
                .samplers
                .get_or_insert_with(SamplerKey::from_stage(&stage0), || {
                    gpu.create_sampler(&sampler_descriptor(&stage0))
                });
            let sampler1 = self
                .samplers
                .get_or_insert_with(SamplerKey::from_stage(&stage1), || {
                    gpu.create_sampler(&sampler_descriptor(&stage1))
                });
            Some(StageBindings {
                view0: view0.clone(),
                view1: view1.clone(),
                sampler0,
                sampler1,
            })
        } else {
            None
        };
        self.batch.borrow_mut().draws.push(PendingDraw {
            pipeline,
            ub_base,
            vertex_base,
            vertex_len: vertex_len as u64,
            stages: stages_uniform.is_some(),
            textures,
            viewport: *v,
            first_vertex: start_vertex,
            count,
        });
        self.publish_pending.set(true);
        if self.strict_scopes || self.batch.borrow().data.len() >= BATCH_FLUSH_BYTES {
            self.flush_draws();
        }
        if self.strict_scopes {
            if let Some(err) = pollster::block_on(gpu.pop_error_scope()) {
                self.pipelines.remove(&key);
                self.shader = None;
                self.textured_shader = None;
                return Err(RenderError::new("DrawPrimitive", err.to_string()));
            }
        }
        Ok(())
    }

    /// Replay every recorded draw as one render pass and one submit, then
    /// publish the target if a draw wrote it. See [`DrawBatch`].
    pub(super) fn flush_draws(&self) {
        let mut batch = self.batch.borrow_mut();
        if batch.draws.is_empty() {
            return;
        }
        let gpu = &self.gpu.device;
        let mut slot = self.draw_buffer.borrow_mut();
        let needed = batch.data.len() as u64;
        if slot.as_ref().map_or(true, |b| b.size() < needed) {
            *slot = Some(gpu.create_buffer(&wgpu::BufferDescriptor {
                label: Some("D3D8 draw uniforms+vertices"),
                size: needed.next_power_of_two().max(4096),
                usage: wgpu::BufferUsages::UNIFORM
                    | wgpu::BufferUsages::VERTEX
                    | wgpu::BufferUsages::COPY_DST,
                mapped_at_creation: false,
            }));
            self.buffers_created_at_flush
                .set(self.buffers_created_at_flush.get() + 1);
            *self.uniform_groups.borrow_mut() = [None, None];
        }
        let db = slot.as_ref().unwrap();
        self.gpu.queue.write_buffer(db, 0, &batch.data);
        {
            let mut groups = self.uniform_groups.borrow_mut();
            let entry = |binding: u32, size: usize| wgpu::BindGroupEntry {
                binding,
                resource: wgpu::BindingResource::Buffer(wgpu::BufferBinding {
                    buffer: db,
                    offset: 0,
                    size: wgpu::BufferSize::new(size as u64),
                }),
            };
            let t = entry(0, std::mem::size_of::<TransformUniform>());
            let st = entry(1, std::mem::size_of::<StagesUniform>());
            let f = entry(2, std::mem::size_of::<FogUniform>());
            let a = entry(3, std::mem::size_of::<AlphaTestUniform>());
            if groups[0].is_none() {
                groups[0] = Some(gpu.create_bind_group(&wgpu::BindGroupDescriptor {
                    label: Some("D3D8 unlit uniforms binding"),
                    layout: &self.draw_layouts.bgl_unlit,
                    entries: &[t.clone(), f.clone(), a.clone()],
                }));
            }
            if groups[1].is_none() {
                groups[1] = Some(gpu.create_bind_group(&wgpu::BindGroupDescriptor {
                    label: Some("D3D8 textured uniforms binding"),
                    layout: &self.draw_layouts.bgl_textured,
                    entries: &[t, st, f, a],
                }));
            }
        }
        let uniform_groups = self.uniform_groups.borrow();
        let mut texture_groups = self.texture_groups.borrow_mut();
        let color_view = batch.color_view.take().expect("batch has a color target");
        let depth = batch.depth.take();
        let mut encoder = gpu.create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("D3D8 draw batch"),
        });
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("D3D8 draw batch"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &color_view,
                    resolve_target: None,
                    depth_slice: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Load,
                        store: wgpu::StoreOp::Store,
                    },
                })],
                // The pass must attach the same depth format the pipelines
                // declare; stencil is only present for a stencil format.
                depth_stencil_attachment: depth.as_ref().map(|(view, has_stencil)| {
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
                }),
                ..Default::default()
            });
            for d in &batch.draws {
                // Uniform blocks are addressed by dynamic offset into the one
                // draw buffer, so a single group per layout serves every draw.
                let kind = usize::from(d.stages);
                let group = uniform_groups[kind].as_ref().unwrap();
                let base = d.ub_base;
                let offsets = [
                    (base + DRAW_UB_TRANSFORM) as u32,
                    (base + DRAW_UB_STAGES) as u32,
                    (base + DRAW_UB_FOG) as u32,
                    (base + DRAW_UB_ALPHA_TEST) as u32,
                ];
                pass.set_pipeline(&d.pipeline);
                // Binding order: transform, [stages], fog, alpha test.
                if d.stages {
                    pass.set_bind_group(0, group, &offsets);
                } else {
                    pass.set_bind_group(0, group, &[offsets[0], offsets[2], offsets[3]]);
                }
                if let Some(t) = &d.textures {
                    if texture_groups.len() >= MAX_TEXTURE_GROUPS {
                        // Groups already recorded in this pass stay alive in it.
                        texture_groups.clear();
                    }
                    let texture_group = texture_groups
                        .entry((
                            t.view0.clone(),
                            t.sampler0.clone(),
                            t.view1.clone(),
                            t.sampler1.clone(),
                        ))
                        .or_insert_with(|| {
                            gpu.create_bind_group(&wgpu::BindGroupDescriptor {
                                label: Some("D3D8 stage textures binding"),
                                layout: &self.draw_layouts.bgl_stage_textures,
                                entries: &[
                                    wgpu::BindGroupEntry {
                                        binding: 0,
                                        resource: wgpu::BindingResource::TextureView(&t.view0),
                                    },
                                    wgpu::BindGroupEntry {
                                        binding: 1,
                                        resource: wgpu::BindingResource::Sampler(&t.sampler0),
                                    },
                                    wgpu::BindGroupEntry {
                                        binding: 2,
                                        resource: wgpu::BindingResource::TextureView(&t.view1),
                                    },
                                    wgpu::BindGroupEntry {
                                        binding: 3,
                                        resource: wgpu::BindingResource::Sampler(&t.sampler1),
                                    },
                                ],
                            })
                        });
                    pass.set_bind_group(1, &*texture_group, &[]);
                }
                let vstart = d.vertex_base;
                pass.set_vertex_buffer(0, db.slice(vstart..vstart + d.vertex_len));
                let v = &d.viewport;
                pass.set_scissor_rect(v.x, v.y, v.width, v.height);
                pass.set_viewport(
                    v.x as f32 + D3D8_HALF_PIXEL,
                    v.y as f32 + D3D8_HALF_PIXEL,
                    v.width as f32,
                    v.height as f32,
                    v.min_z,
                    v.max_z,
                );
                pass.draw(d.first_vertex..d.count, 0..1);
            }
        }
        self.gpu.queue.submit([encoder.finish()]);
        self.submits.set(self.submits.get() + 1);
        batch.draws.clear();
        batch.data.clear();
        batch.last_ub = None;
        drop(batch);
        drop(slot);
        if self.publish_pending.replace(false) {
            self.publish_target();
        }
    }

    /// Close the managed per-frame validation error scope, failing loudly when
    /// any draw in the frame produced a wgpu validation error. The diagnostic
    /// names the frame and its draw count, since the report is deferred from
    /// the offending draw to the frame end. A no-op under `RECOMP_D3D8_STRICT_SCOPES`,
    /// where each draw popped its own scope.
    fn finish_frame_scope(&mut self) -> Result<(), RenderError> {
        if self.strict_scopes || !self.frame_scope_open {
            return Ok(());
        }
        self.frame_scope_open = false;
        if let Some(err) = pollster::block_on(self.gpu.device.pop_error_scope()) {
            self.pipelines.clear();
            self.shader = None;
            self.textured_shader = None;
            self.lit_shader = None;
            self.lit_textured_shader = None;
            return Err(RenderError::new(
                "DrawPrimitive",
                format!(
                    "frame {} ({} draws): {err}",
                    self.frame_index, self.draw_index
                ),
            ));
        }
        Ok(())
    }

    /// `DrawIndexedPrimitive`: expand the guest index buffer into a reused
    /// scratch vertex stream, then draw it. Expansion errors keep their kind so
    /// the ABI layer can apply survey-mode rejection exactly as before.
    pub fn draw_indexed_primitive(
        &mut self,
        topology: u32,
        fvf: u32,
        vertices: &[u8],
        indices: &[u8],
        draw: IndexedDraw,
    ) -> Result<(), RenderError> {
        if draw.primitive_count == 0 {
            return Ok(());
        }
        let mut scratch = std::mem::take(&mut self.index_scratch);
        let result = (|| {
            expand_indexed_into(&mut scratch, vertices, indices, draw)?;
            let view = VertexBuffer::borrowed(&scratch, draw.stride)?;
            // Expansion always produces a triangle list, whatever the source
            // topology was, so the list draw path is the one to run.
            let _ = topology;
            self.draw_primitive(4, fvf, &view, 0, draw.primitive_count)
        })();
        self.index_scratch = scratch;
        result
    }

    /// `IDirect3DDevice8::Reset` for the single implicit swap chain.
    ///
    /// The new target is created first, so an invalid or oversized request
    /// leaves the device untouched. On success the device state is reset to
    /// D3D8 defaults, then the post-Reset adjustments Wine's `d3d8` applies
    /// (`d3d8_device_Reset`): `D3DRS_ZENABLE` follows `EnableAutoDepthStencil`
    /// and the viewport returns to the full target. Cached pipelines are
    /// dropped because a backbuffer format change alters the color target
    /// state they were built with. The scene/clear frame bookkeeping is
    /// cleared, matching a fresh device.
    ///
    /// TODO(decomp): D3D8 returns D3DERR_DEVICELOST while any referenced
    /// D3DPOOL_DEFAULT resource exists (Wine `reset_enum_callback`); this
    /// bridge does not track pool references yet and rebuilds unconditionally.
    pub fn reset(
        &mut self,
        width: u32,
        height: u32,
        format: u32,
        depth_format: u32,
    ) -> Result<(), RenderError> {
        self.flush_draws();
        if self.frame_scope_open {
            // Drop the managed frame scope's pending diagnostics: the target
            // and its pipelines are rebuilt below.
            let _ = pollster::block_on(self.gpu.device.pop_error_scope());
            self.frame_scope_open = false;
        }
        let target = self
            .gpu
            .create_target(width, height, format, depth_format)?;
        self.targets = Targets::new(target.clone());
        self.target = target;
        self.state = DeviceState::new(width, height);
        let z_enable = u32::from(depth_format != 0);
        self.state.set_render_state(
            crate::d3d8::enums::D3DRENDERSTATETYPE::ZEnable as u32,
            z_enable,
        )?;
        self.pipelines.clear();
        self.stage_textures = std::array::from_fn(|_| None);
        self.texture_cache.clear();
        self.scratch_rgba.clear();
        self.scene = false;
        Ok(())
    }

    /// Tightly packed RGBA8, regardless of the D3D target format's channel order.
    pub fn read_pixels(&self) -> Result<Vec<u8>, RenderError> {
        self.flush_draws();
        self.gpu.read_pixels(&self.targets.backbuffer)
    }

    /// Copy the backbuffer into the next ring slot on the GPU, wait for the
    /// work to finish, and return the slot's native texture. This replaces
    /// `read_pixels_into` for hosts that share the Metal device; the wait for
    /// the render to finish is the same one the readback made. Fails (so the
    /// caller falls back to readback) when no native handle is available.
    pub fn present_handoff(&self) -> Result<FrameHandoff, RenderError> {
        use std::sync::atomic::Ordering;
        self.flush_draws();
        let b = &self.targets.backbuffer;
        let mut ring = self.frame_ring.borrow_mut();
        if ring
            .as_ref()
            .is_some_and(|r| r.width != b.width || r.height != b.height)
        {
            // Resized: the old slots may still be read by the host.
            let old = ring.take().unwrap();
            for busy in &old.busy {
                self.wait_slot(busy)?;
            }
        }
        let ring = ring.get_or_insert_with(|| FrameRing {
            width: b.width,
            height: b.height,
            textures: (0..FRAME_RING_SLOTS)
                .map(|_| {
                    self.gpu.device.create_texture(&wgpu::TextureDescriptor {
                        label: Some("d3d8-present-ring"),
                        size: wgpu::Extent3d {
                            width: b.width,
                            height: b.height,
                            depth_or_array_layers: 1,
                        },
                        mip_level_count: 1,
                        sample_count: 1,
                        dimension: wgpu::TextureDimension::D2,
                        format: b.format,
                        usage: wgpu::TextureUsages::COPY_DST | wgpu::TextureUsages::TEXTURE_BINDING,
                        view_formats: &[],
                    })
                })
                .collect(),
            busy: (0..FRAME_RING_SLOTS)
                .map(|_| &*Box::leak(Box::new(std::sync::atomic::AtomicU32::new(0))))
                .collect(),
            next: 0,
        });
        let slot = ring.next;
        let native = native_texture_ptr(&ring.textures[slot])?;
        self.wait_slot(ring.busy[slot])?;
        let mut encoder = self
            .gpu
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                label: Some("d3d8-present-copy"),
            });
        encoder.copy_texture_to_texture(
            wgpu::TexelCopyTextureInfo {
                texture: &b.texture,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::TexelCopyTextureInfo {
                texture: &ring.textures[slot],
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::Extent3d {
                width: b.width,
                height: b.height,
                depth_or_array_layers: 1,
            },
        );
        self.gpu.queue.submit([encoder.finish()]);
        // The host's queue has no ordering against ours: the render and copy
        // must have finished before it reads the slot.
        self.gpu
            .device
            .poll(wgpu::PollType::wait_indefinitely())
            .map_err(|e| RenderError::new("present_handoff::poll", e.to_string()))?;
        ring.busy[slot].store(1, Ordering::Release);
        ring.next = (slot + 1) % FRAME_RING_SLOTS;
        Ok(FrameHandoff {
            texture: native,
            busy: ring.busy[slot],
            width: b.width,
            height: b.height,
        })
    }

    fn wait_slot(&self, busy: &std::sync::atomic::AtomicU32) -> Result<(), RenderError> {
        let start = std::time::Instant::now();
        while busy.load(std::sync::atomic::Ordering::Acquire) != 0 {
            if start.elapsed() > std::time::Duration::from_secs(2) {
                return Err(RenderError::new(
                    "present_handoff",
                    "host never released a presented frame slot",
                ));
            }
            std::thread::yield_now();
        }
        Ok(())
    }

    /// Required tightly packed backbuffer readback size, independent of viewport.
    pub fn read_pixels_len(&self) -> u64 {
        u64::from(self.targets.backbuffer.width) * u64::from(self.targets.backbuffer.height) * 4
    }

    /// Read into caller storage without an intermediate full-frame allocation.
    pub fn read_pixels_into(&self, out: &mut [u8]) -> Result<(), RenderError> {
        self.flush_draws();
        self.gpu.read_pixels_into(&self.targets.backbuffer, out)
    }

    /// True when this device records unsupported draw rejections
    /// (`RECOMP_D3D8_SURVEY=1`).
    pub fn survey_enabled(&self) -> bool {
        self.survey
    }

    /// Record one state/FVF/TSS draw rejection in survey mode. Returns true
    /// when the caller must skip the draw and return success so the guest
    /// continues; false means the error is a hard failure and must propagate.
    ///
    /// The key combines the named error with a compact dump of the draw state
    /// (topology, FVF/vertex-shader handle, stride, fog/lighting/blend and the
    /// first two texture stages).
    pub fn note_draw_rejection(
        &self,
        error: &RenderError,
        topology: u32,
        fvf: u32,
        stride: u32,
    ) -> bool {
        if !self.survey {
            return false;
        }
        let state = format!(
            "topology={topology} fvf=0x{fvf:08X} stride={stride} {}",
            self.state.draw_state_summary()
        );
        survey::record(error, &state, self.frame_index, self.draw_index);
        true
    }

    /// Survey-mode counterpart for a rejection raised before
    /// [`Device::draw_primitive`] (the indexed expansion path). Counts the
    /// draw attempt, then records like [`Device::note_draw_rejection`].
    pub fn note_indexed_rejection(
        &mut self,
        error: &RenderError,
        topology: u32,
        fvf: u32,
        stride: u32,
    ) -> bool {
        self.draw_index += 1;
        self.note_draw_rejection(error, topology, fvf, stride)
    }

    /// The guest's world/UI boundary: everything drawn so far is the 3D scene,
    /// everything drawn next is overlay. `mode` selects an optional host
    /// post-process of the scene ([`SCENE_POST_NONE`] does nothing). Draws are
    /// flushed first, so the filter sees exactly the scene. Only the implicit
    /// backbuffer is filtered; an unknown mode, or a render-target texture
    /// bound at the boundary, is a named error rather than a silent skip.
    ///
    /// `RECOMP_D3D8_TRACE_DRAWS` prints a marker with the running draw count so
    /// a trace shows which draws precede the boundary.
    pub fn scene_boundary(&mut self, mode: u32) -> Result<(), RenderError> {
        if std::env::var_os("RECOMP_D3D8_TRACE_DRAWS").is_some() {
            eprintln!(
                "[d3d8-trace] scene-boundary frame={} draws={} mode={mode}",
                self.frame_index, self.draw_index
            );
        }
        match mode {
            SCENE_POST_NONE => Ok(()),
            SCENE_POST_FXAA => {
                if self.targets.current.is_some() {
                    return Err(RenderError::new(
                        "SceneBoundary",
                        "FXAA requested while a render-target texture is bound",
                    ));
                }
                // A partial viewport at the boundary is a sub-view (for
                // example a preview) drawn over an overlay that already
                // exists, so filtering the whole target would hit the overlay.
                let v = &self.state.viewport;
                let b = &self.targets.backbuffer;
                if (v.x, v.y, v.width, v.height) != (0, 0, b.width, b.height) {
                    use std::sync::atomic::{AtomicBool, Ordering};
                    static WARNED: AtomicBool = AtomicBool::new(false);
                    if !WARNED.swap(true, Ordering::Relaxed) {
                        eprintln!(
                            "[d3d8] scene boundary: FXAA skipped for partial viewport {}x{}+{}+{}",
                            v.width, v.height, v.x, v.y
                        );
                    }
                    return Ok(());
                }
                self.flush_draws();
                self.gpu.fxaa_in_place(&self.targets.backbuffer)
            }
            other => Err(RenderError::new(
                "SceneBoundary",
                format!("unknown scene post-process mode {other}"),
            )),
        }
    }

    pub fn present(&mut self) -> Result<(), RenderError> {
        if self.scene {
            return Err(RenderError::new(
                "Present",
                "requires an ended frame (EndScene)",
            ));
        }
        self.flush_draws();
        self.gpu.present(&self.targets.backbuffer)?;
        self.frame_index += 1;
        if self.draw_stats_enabled {
            let mut stats = std::mem::take(&mut self.frame_stats);
            stats.submits = self.submits.take();
            stats.buffers_created += self.buffers_created_at_flush.take();
            eprintln!(
                "[d3d8-draw] frame={} draws={} buffers_created={} uniform_writes={} sampler_hits={} sampler_misses={} submits={}",
                self.frame_index,
                stats.draws,
                stats.buffers_created,
                stats.uniform_writes,
                self.samplers.hits,
                self.samplers.misses,
                stats.submits,
            );
        }
        if self.survey && self.frame_index % SURVEY_REPORT_INTERVAL == 0 {
            survey::report_to_stderr();
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::blend_factor;
    use crate::d3d8::enums::D3DBLEND;

    #[test]
    fn cull_modes_map_to_the_d3d8_winding() {
        use super::Device;
        use crate::d3d8::enums::D3DCULL;
        // D3DCULL_CCW (the D3D8 default) culls counter-clockwise faces, so
        // clockwise is front; D3DCULL_CW is the opposite; NONE culls nothing.
        assert_eq!(
            Device::cull_mode_to_wgpu(D3DCULL::None),
            (None, wgpu::FrontFace::Ccw)
        );
        assert_eq!(
            Device::cull_mode_to_wgpu(D3DCULL::Cw),
            (Some(wgpu::Face::Back), wgpu::FrontFace::Ccw)
        );
        assert_eq!(
            Device::cull_mode_to_wgpu(D3DCULL::Ccw),
            (Some(wgpu::Face::Back), wgpu::FrontFace::Cw)
        );
    }

    #[test]
    fn blend_factors_map_to_wgpu() {
        assert_eq!(
            blend_factor(D3DBLEND::Zero).unwrap(),
            wgpu::BlendFactor::Zero
        );
        assert_eq!(blend_factor(D3DBLEND::One).unwrap(), wgpu::BlendFactor::One);
        assert_eq!(
            blend_factor(D3DBLEND::SrcAlpha).unwrap(),
            wgpu::BlendFactor::SrcAlpha
        );
        assert_eq!(
            blend_factor(D3DBLEND::InvSrcAlpha).unwrap(),
            wgpu::BlendFactor::OneMinusSrcAlpha
        );
        assert_eq!(
            blend_factor(D3DBLEND::DestColor).unwrap(),
            wgpu::BlendFactor::Dst
        );
        assert_eq!(
            blend_factor(D3DBLEND::SrcAlphaSat).unwrap(),
            wgpu::BlendFactor::SrcAlphaSaturated
        );
    }

    #[test]
    fn both_alpha_blend_factors_fail_by_name() {
        let err = blend_factor(D3DBLEND::BothSrcAlpha).unwrap_err();
        assert!(err.cause.contains("BOTHSRCALPHA"), "{}", err.cause);
        let err = blend_factor(D3DBLEND::BothInvSrcAlpha).unwrap_err();
        assert!(err.cause.contains("BOTHINVSRCALPHA"), "{}", err.cause);
    }

    #[test]
    fn sampler_keys_dedupe_by_sampling_state() {
        use super::{SamplerKey, SamplerKey as K};
        use crate::d3d8::state::TextureStage;
        let stage = TextureStage {
            active: true,
            color_op: 0,
            color_arg1: 0,
            color_arg2: 0,
            alpha_op: 0,
            alpha_arg1: 0,
            alpha_arg2: 0,
            texture_factor: 0,
            tex_coord_index: 0,
            min_filter: 2,
            mag_filter: 2,
            mip_filter: 1,
            address_u: 1,
            address_v: 1,
        };
        let same = TextureStage {
            color_op: 7,
            texture_factor: 0x11223344,
            ..stage
        };
        let other = TextureStage {
            address_v: 3,
            ..stage
        };
        // Combine state that does not affect the sampler must not split the
        // cache; a sampling-mode difference must.
        assert_eq!(K::from_stage(&stage), K::from_stage(&same));
        assert_ne!(K::from_stage(&stage), K::from_stage(&other));
        let _: fn(&TextureStage) -> SamplerKey = K::from_stage;
    }

    #[test]
    fn keyed_cache_clones_on_hit_and_counts() {
        use super::KeyedCache;
        let mut cache: KeyedCache<u32, u32> = KeyedCache::default();
        let mut created = 0;
        for _ in 0..3 {
            let value = cache.get_or_insert_with(7, || {
                created += 1;
                42
            });
            assert_eq!(value, 42);
        }
        let value = cache.get_or_insert_with(8, || {
            created += 1;
            99
        });
        assert_eq!(value, 99);
        assert_eq!(created, 2);
        assert_eq!(cache.hits, 2);
        assert_eq!(cache.misses, 2);
    }

    #[test]
    fn blend_operations_map_to_wgpu() {
        use super::blend_operation;
        use crate::d3d8::enums::D3DBLENDOP as O;
        assert_eq!(blend_operation(O::Add), wgpu::BlendOperation::Add);
        assert_eq!(blend_operation(O::Subtract), wgpu::BlendOperation::Subtract);
        assert_eq!(
            blend_operation(O::RevSubtract),
            wgpu::BlendOperation::ReverseSubtract
        );
        assert_eq!(blend_operation(O::Min), wgpu::BlendOperation::Min);
        assert_eq!(blend_operation(O::Max), wgpu::BlendOperation::Max);
    }
}
