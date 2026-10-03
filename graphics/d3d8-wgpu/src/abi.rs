//! C ABI surface with opaque host handles.
//!
//! This is the boundary the recomp-kit bridge calls. It passes **host handles
//! and plain data only**: no guest addresses, no 32-bit COM vtables, no guest
//! object layouts, and no raw host pointers stored in guest fields. Guest COM
//! dispatch, pointer/layout conversion and resource-lock copies are the bridge's
//! responsibility, not this crate's.
//!
//! Every fallible call fills a caller-owned [`D3d8Error`] and returns a
//! [`D3d8Status`] as an `i32` (`0` is success). A handle is only ever a pointer
//! to a leaked box owned by this module; the header never defines its layout.

use crate::RenderError;
use crate::backend::GpuContext;
use crate::d3d8::device::Device;
use crate::d3d8::math::Mat4;
use crate::d3d8::resource::{
    CpuStorage, D3d8LevelLayout, IndexedDraw, VertexBuffer, format_bytes, level_layout,
};
use crate::d3d8::state::{Light, Material, Viewport};

/// Version of the C ABI described by this module.
///
/// 5: adds `IDirect3DDevice8::Reset` (implicit swap-chain recreation).
/// 6: adds `IDirect3DDevice8::SetTexture` (level-0 upload + sampling).
pub const ABI_VERSION: u32 = 6;

/// Opaque device handle. The bridge never inspects the pointee.
pub struct D3d8Device {
    _private: (),
}

/// Error codes returned across the ABI. `OK` is 0; every failure names a cause.
#[repr(i32)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum D3d8Status {
    Ok = 0,
    /// A required parameter was null or inconsistent.
    InvalidArgument = 1,
    /// A D3D8 value (format, state, FVF, ...) is not supported by this build.
    Unsupported = 2,
    /// GPU/backend initialization or submission failed.
    Backend = 3,
    /// An operation is planned but not implemented yet.
    NotImplemented = 4,
    OutOfMemory = 5,
}

/// Fixed-size diagnostic buffer filled by fallible ABI calls.
pub const DIAGNOSTIC_CAPACITY: usize = 256;

/// Out-parameter diagnostic. `status` mirrors the returned [`D3d8Status`].
#[repr(C)]
pub struct D3d8Error {
    pub status: D3d8Status,
    /// NUL-terminated, always valid; truncated if necessary.
    pub message: [u8; DIAGNOSTIC_CAPACITY],
}

impl D3d8Error {
    /// An error carrying no message.
    pub const fn empty() -> Self {
        Self {
            status: D3d8Status::Ok,
            message: [0; DIAGNOSTIC_CAPACITY],
        }
    }
}

/// Adapter facts the guest `D3DCAPS8` and `D3DADAPTER_IDENTIFIER8` are derived
/// from. These are reported from the real wgpu adapter; nothing is invented.
#[repr(C)]
pub struct D3d8AdapterInfo {
    /// UTF-8 adapter description, NUL-terminated.
    pub name: [u8; 128],
    /// PCI vendor id as wgpu reports it.
    pub vendor_id: u32,
    /// PCI device id as wgpu reports it.
    pub device_id: u32,
    /// wgpu `max_texture_dimension_2d`.
    pub max_texture_dimension_2d: u32,
    /// wgpu `max_bind_groups`.
    pub max_bind_groups: u32,
    /// wgpu `max_vertex_buffers`.
    pub max_vertex_buffers: u32,
    /// wgpu `max_vertex_attributes`.
    pub max_vertex_attributes: u32,
}

/// A `D3DMATRIX` by value, row-major, as D3D8 stores it.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct D3d8Matrix {
    pub rows: [[f32; 4]; 4],
}

/// `D3DCOLORVALUE`: four floats in `r, g, b, a` order.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct D3d8ColorValue {
    pub r: f32,
    pub g: f32,
    pub b: f32,
    pub a: f32,
}

/// `D3DMATERIAL8`, 68 bytes, field-for-field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct D3d8Material {
    pub diffuse: D3d8ColorValue,
    pub ambient: D3d8ColorValue,
    pub specular: D3d8ColorValue,
    pub emissive: D3d8ColorValue,
    pub power: f32,
}

/// `D3DLIGHT8`, 104 bytes, field-for-field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct D3d8Light {
    pub light_type: u32,
    pub diffuse: D3d8ColorValue,
    pub specular: D3d8ColorValue,
    pub ambient: D3d8ColorValue,
    pub position: [f32; 3],
    pub direction: [f32; 3],
    pub range: f32,
    pub falloff: f32,
    pub attenuation0: f32,
    pub attenuation1: f32,
    pub attenuation2: f32,
    pub theta: f32,
    pub phi: f32,
}

/// `D3DPRESENT_PARAMETERS` fields the bridge's single implicit swap chain cares
/// about, copied from the guest struct by the C++ side. This is plain data; the
/// guest 52-byte layout never crosses the ABI. Validation and the target
/// rebuild both happen on the Rust side.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct D3d8PresentParams {
    pub back_buffer_width: u32,
    pub back_buffer_height: u32,
    pub back_buffer_format: u32,
    pub back_buffer_count: u32,
    pub multisample_type: u32,
    pub swap_effect: u32,
    pub device_window: u32,
    pub windowed: u32,
    pub enable_auto_depth_stencil: u32,
    pub auto_depth_stencil_format: u32,
    pub flags: u32,
    pub fullscreen_refresh_rate: u32,
    pub fullscreen_presentation_interval: u32,
}

fn color_to_state(c: D3d8ColorValue) -> [f32; 4] {
    [c.r, c.g, c.b, c.a]
}

fn color_from_state(c: [f32; 4]) -> D3d8ColorValue {
    D3d8ColorValue {
        r: c[0],
        g: c[1],
        b: c[2],
        a: c[3],
    }
}

fn material_to_state(m: &D3d8Material) -> Material {
    Material {
        diffuse: color_to_state(m.diffuse),
        ambient: color_to_state(m.ambient),
        specular: color_to_state(m.specular),
        emissive: color_to_state(m.emissive),
        power: m.power,
    }
}

fn material_from_state(m: &Material) -> D3d8Material {
    D3d8Material {
        diffuse: color_from_state(m.diffuse),
        ambient: color_from_state(m.ambient),
        specular: color_from_state(m.specular),
        emissive: color_from_state(m.emissive),
        power: m.power,
    }
}

fn light_to_state(l: &D3d8Light) -> Light {
    Light {
        light_type: l.light_type,
        diffuse: color_to_state(l.diffuse),
        specular: color_to_state(l.specular),
        ambient: color_to_state(l.ambient),
        position: l.position,
        direction: l.direction,
        range: l.range,
        falloff: l.falloff,
        attenuation0: l.attenuation0,
        attenuation1: l.attenuation1,
        attenuation2: l.attenuation2,
        theta: l.theta,
        phi: l.phi,
    }
}

fn light_from_state(l: &Light) -> D3d8Light {
    D3d8Light {
        light_type: l.light_type,
        diffuse: color_from_state(l.diffuse),
        specular: color_from_state(l.specular),
        ambient: color_from_state(l.ambient),
        position: l.position,
        direction: l.direction,
        range: l.range,
        falloff: l.falloff,
        attenuation0: l.attenuation0,
        attenuation1: l.attenuation1,
        attenuation2: l.attenuation2,
        theta: l.theta,
        phi: l.phi,
    }
}

fn write_error(err: *mut D3d8Error, status: D3d8Status, message: &str) {
    if err.is_null() {
        return;
    }
    // SAFETY: the caller promises a writable, aligned D3d8Error.
    let slot = unsafe { &mut *err };
    slot.status = status;
    slot.message = [0; DIAGNOSTIC_CAPACITY];
    let bytes = message.as_bytes();
    let n = bytes.len().min(DIAGNOSTIC_CAPACITY - 1);
    slot.message[..n].copy_from_slice(&bytes[..n]);
}

fn report(err: *mut D3d8Error, result: Result<(), RenderError>) -> i32 {
    match result {
        Ok(()) => {
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
        Err(failure) => {
            let status = match failure.kind {
                crate::RenderErrorKind::Unsupported => D3d8Status::Unsupported,
                crate::RenderErrorKind::InvalidArgument => D3d8Status::InvalidArgument,
                crate::RenderErrorKind::OutOfMemory => D3d8Status::OutOfMemory,
            };
            write_error(err, status, &failure.to_string());
            status as i32
        }
    }
}

fn device_ref<'a>(dev: *mut D3d8Device) -> Option<&'a mut Device> {
    if dev.is_null() {
        None
    } else {
        // SAFETY: the only producer of a D3d8Device is d3d8_device_create,
        // which leaks a Box<Device> behind this pointer. A null check protects
        // the caller's side; a stale pointer is the caller's contract breach.
        Some(unsafe { &mut *dev.cast::<Device>() })
    }
}

/// ABI version, so the bridge can refuse a mismatched library at load time.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_abi_version() -> u32 {
    ABI_VERSION
}

/// Query the real adapter facts once, without creating a render device.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_adapter_info(out: *mut D3d8AdapterInfo, err: *mut D3d8Error) -> i32 {
    if out.is_null() {
        write_error(err, D3d8Status::InvalidArgument, "adapter_info: null out");
        return D3d8Status::InvalidArgument as i32;
    }
    let gpu = match pollster::block_on(GpuContext::new_headless()) {
        Ok(gpu) => gpu,
        Err(failure) => {
            write_error(
                err,
                D3d8Status::Backend,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            return D3d8Status::Backend as i32;
        }
    };
    let info = &gpu.adapter_info;
    let limits = gpu.device.limits();
    let mut slot = D3d8AdapterInfo {
        name: [0; 128],
        vendor_id: info.vendor,
        device_id: info.device,
        max_texture_dimension_2d: limits.max_texture_dimension_2d,
        max_bind_groups: limits.max_bind_groups,
        max_vertex_buffers: limits.max_vertex_buffers,
        max_vertex_attributes: limits.max_vertex_attributes,
    };
    let bytes = info.name.as_bytes();
    let n = bytes.len().min(slot.name.len() - 1);
    slot.name[..n].copy_from_slice(&bytes[..n]);
    // SAFETY: `out` was checked non-null; the caller provides D3d8AdapterInfo.
    unsafe { *out = slot };
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// Create an offscreen render device for a D3D8 backbuffer format.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_create(
    width: u32,
    height: u32,
    format: u32,
    depth_format: u32,
    err: *mut D3d8Error,
) -> *mut D3d8Device {
    if width == 0 || height == 0 {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "device_create: zero backbuffer dimension",
        );
        return core::ptr::null_mut();
    }
    let gpu = match pollster::block_on(GpuContext::new_headless()) {
        Ok(gpu) => gpu,
        Err(failure) => {
            write_error(
                err,
                D3d8Status::Backend,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            return core::ptr::null_mut();
        }
    };
    match Device::new(gpu, width, height, format, depth_format) {
        Ok(device) => {
            write_error(err, D3d8Status::Ok, "");
            Box::into_raw(Box::new(device)).cast::<D3d8Device>()
        }
        Err(failure) => {
            write_error(
                err,
                D3d8Status::Unsupported,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            core::ptr::null_mut()
        }
    }
}

/// `IDirect3DDevice8::Reset` for the single implicit swap chain. Validates the
/// presentation subset the bridge accepts and recreates the offscreen target
/// (and its autodepth attachment). The C++ side owns the guest COM surface
/// handles, which it discards and lazily recreates.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_reset(
    dev: *mut D3d8Device,
    params: *const D3d8PresentParams,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "reset: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    if params.is_null() {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "reset: null present params",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: the caller passes a valid D3d8PresentParams for the call.
    let params = unsafe { *params };
    let depth_format = match validate_present_params(&params) {
        Ok(depth) => depth,
        Err(failure) => {
            report(err, Err(failure));
            return D3d8Status::InvalidArgument as i32;
        }
    };
    report(
        err,
        device.reset(
            params.back_buffer_width,
            params.back_buffer_height,
            params.back_buffer_format,
            depth_format,
        ),
    )
}

/// Validate the `D3DPRESENT_PARAMETERS` subset the bridge accepts, matching
/// `D8_CreateDevice`: one backbuffer, no multisampling, DISCARD (1) or
/// COPY_VSYNC (4) swap effect, zero flags/refresh/interval, and an autodepth
/// format the backend maps. Returns the depth format to hand the target (0 when
/// autodepth is off). Bad values are `InvalidArgument` (the bridge's
/// `D3DERR_INVALIDCALL`), never a fail-loud unsupported abort.
fn validate_present_params(p: &D3d8PresentParams) -> Result<u32, RenderError> {
    use crate::d3d8::format::{self, DepthFormat};
    let invalid = |what: &str| RenderError::invalid("reset", what);
    if p.back_buffer_width == 0 || p.back_buffer_height == 0 {
        return Err(invalid("zero backbuffer dimension"));
    }
    if u64::from(p.back_buffer_width) * u64::from(p.back_buffer_height) * 4 > u64::from(u32::MAX) {
        return Err(invalid("backbuffer size overflow"));
    }
    if p.back_buffer_format != format::D3DFMT_A8R8G8B8
        && p.back_buffer_format != format::D3DFMT_X8R8G8B8
    {
        return Err(invalid("unsupported backbuffer format"));
    }
    if p.back_buffer_count > 1 || p.multisample_type != 0 {
        return Err(invalid("unsupported backbuffer count or multisample type"));
    }
    if p.swap_effect != 1 && p.swap_effect != 4 {
        return Err(invalid("unsupported swap effect"));
    }
    if p.flags != 0 || p.fullscreen_refresh_rate != 0 || p.fullscreen_presentation_interval != 0 {
        return Err(invalid("unsupported present flags/refresh/interval"));
    }
    let depth = DepthFormat::from_d3dformat(p.auto_depth_stencil_format)
        .map_err(|_| invalid("unsupported autodepth format"))?;
    if p.enable_auto_depth_stencil == 0 {
        return Ok(0);
    }
    match depth {
        Some(d) => Ok(d.d3dformat()),
        None => Err(invalid("autodepth enabled without a depth format")),
    }
}

/// Destroy a device created by [`d3d8_device_create`]. Null is ignored.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_destroy(dev: *mut D3d8Device) {
    if !dev.is_null() {
        // SAFETY: the pointer came from Box::into_raw in device_create.
        drop(unsafe { Box::from_raw(dev.cast::<Device>()) });
    }
}

/// `IDirect3DDevice8::Clear` for the supported full-target clear. `flags` is
/// the D3DCLEAR_* mask; `z`/`stencil` are the depth and stencil clear values.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_clear(
    dev: *mut D3d8Device,
    rect_count: u32,
    flags: u32,
    color: u32,
    z: f32,
    stencil: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "clear: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    report(err, device.clear(rect_count, flags, color, z, stencil))
}

/// `IDirect3DDevice8::BeginScene`.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_begin_scene(dev: *mut D3d8Device, err: *mut D3d8Error) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "begin_scene: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    report(err, device.begin_scene())
}

/// `IDirect3DDevice8::EndScene`.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_end_scene(dev: *mut D3d8Device, err: *mut D3d8Error) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "end_scene: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    report(err, device.end_scene())
}

/// `IDirect3DDevice8::SetRenderState`, restricted to the modelled slice.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_render_state(
    dev: *mut D3d8Device,
    state: u32,
    value: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_render_state: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    report(err, device.state.set_render_state(state, value))
}

/// `IDirect3DDevice8::SetTextureStageState`. Validates the stage and state id;
/// the raw value is stored faithfully for the fixed-function path.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_texture_stage_state(
    dev: *mut D3d8Device,
    stage: u32,
    state: u32,
    value: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_texture_stage_state: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    report(
        err,
        device.state.set_texture_stage_state(stage, state, value),
    )
}

/// `IDirect3DDevice8::SetTexture` for one stage. `data` is the guest's
/// level-0 CPU texel block in D3D8 `B,G,R,A` order; a null/empty block or
/// `texture_id == 0` unbinds the stage and selects the default white texture.
///
/// `texture_id`/`level` identify the guest texture (the kit's level-surface COM identity,
/// unique for the process lifetime) and its mip level; `generation` is the
/// kit's content version, bumped on every write path. Together with
/// `force_upload` (a lock still open at draw time) they let the device reuse a
/// resident upload without converting or copying unchanged bytes. The bridge
/// converts the guest COM texture into these plain bytes; no guest address
/// reaches the renderer's stored state.
#[allow(clippy::too_many_arguments)]
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_texture(
    dev: *mut D3d8Device,
    stage: u32,
    texture_id: u32,
    level: u32,
    generation: u64,
    force_upload: u32,
    format: u32,
    width: u32,
    height: u32,
    data: *const u8,
    bytes: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "set_texture: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    let slice: &[u8] = if data.is_null() || bytes == 0 {
        &[]
    } else {
        // SAFETY: the caller promises `bytes` readable bytes at `data`.
        unsafe { core::slice::from_raw_parts(data, bytes as usize) }
    };
    report(
        err,
        device.set_texture(
            stage,
            texture_id,
            level,
            generation,
            force_upload != 0,
            format,
            width,
            height,
            slice,
        ),
    )
}

/// Bind a level identity, or id zero for the implicit backbuffer. The bridge
/// validates guest owners/usage and supplies host CPU bytes only. `depth` names
/// the shared implicit depth attachment; no fresh depth storage is substituted.
#[allow(clippy::too_many_arguments)]
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_render_target(
    dev: *mut D3d8Device,
    id: u32,
    level: u32,
    generation: u64,
    format: u32,
    width: u32,
    height: u32,
    data: *const u8,
    bytes: u32,
    depth: u32,
    change_color: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_render_target: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    let data = if data.is_null() || bytes == 0 {
        &[]
    } else {
        // SAFETY: caller promises bytes readable host bytes.
        unsafe { core::slice::from_raw_parts(data, bytes as usize) }
    };
    report(
        err,
        device.set_render_target(
            id,
            level,
            generation,
            format,
            width,
            height,
            data,
            depth != 0,
            change_color != 0,
        ),
    )
}

/// Read a GPU-authoritative level into little-endian guest-format CPU bytes.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_read_texture(
    dev: *mut D3d8Device,
    id: u32,
    level: u32,
    generation: u64,
    out: *mut u8,
    capacity: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "read_texture: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    let pixels = match device.read_texture_if_current(id, level, generation) {
        Ok(Some(pixels)) => pixels,
        Ok(None) => {
            write_error(err, D3d8Status::Ok, "");
            return D3d8Status::Ok as i32;
        }
        Err(failure) => return report(err, Err(failure)),
    };
    if out.is_null() || (capacity as usize) < pixels.len() {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "read_texture: output buffer too small",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: caller promises capacity writable bytes.
    unsafe { core::ptr::copy_nonoverlapping(pixels.as_ptr(), out, pixels.len()) };
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// Drop resident GPU storage for a level-surface identity. The kit calls this
/// when its surface is destroyed, including after the last binding reference.
/// Null devices are ignored.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_release_texture(dev: *mut D3d8Device, texture_id: u32) {
    if let Some(device) = device_ref(dev) {
        device.release_texture(texture_id);
    }
}

/// `IDirect3DDevice8::SetTransform` for `D3DTS_*` world/view/projection.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_transform(
    dev: *mut D3d8Device,
    kind: u32,
    matrix: *const D3d8Matrix,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_transform: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    if matrix.is_null() {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_transform: null matrix",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: the caller promises a readable D3d8Matrix.
    let value = Mat4 {
        rows: unsafe { (*matrix).rows },
    };
    let target = match kind {
        256 => &mut device.state.world,    // D3DTS_WORLD
        2 => &mut device.state.view,       // D3DTS_VIEW
        3 => &mut device.state.projection, // D3DTS_PROJECTION
        other => {
            write_error(
                err,
                D3d8Status::Unsupported,
                &format!("set_transform: unsupported transform {other}"),
            );
            return D3d8Status::Unsupported as i32;
        }
    };
    *target = value;
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// `IDirect3DDevice8::SetViewport`.
#[allow(clippy::too_many_arguments)]
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_viewport(
    dev: *mut D3d8Device,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
    min_z: f32,
    max_z: f32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_viewport: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    device.state.viewport = Viewport {
        x,
        y,
        width,
        height,
        min_z,
        max_z,
    };
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// `IDirect3DDevice8::SetMaterial`. The full 68-byte record is stored.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_material(
    dev: *mut D3d8Device,
    material: *const D3d8Material,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_material: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    if material.is_null() {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "set_material: null material",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: the caller promises a readable D3d8Material.
    device
        .state
        .set_material(material_to_state(unsafe { &*material }));
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// `IDirect3DDevice8::GetMaterial`.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_get_material(
    dev: *mut D3d8Device,
    out: *mut D3d8Material,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "get_material: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    if out.is_null() {
        write_error(err, D3d8Status::InvalidArgument, "get_material: null out");
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: caller-provided out-parameter.
    unsafe { *out = material_from_state(&device.state.material()) };
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// `IDirect3DDevice8::SetLight`. `index` must be below 8.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_set_light(
    dev: *mut D3d8Device,
    index: u32,
    light: *const D3d8Light,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "set_light: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    if light.is_null() {
        write_error(err, D3d8Status::InvalidArgument, "set_light: null light");
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: the caller promises a readable D3d8Light.
    match device
        .state
        .set_light(index, light_to_state(unsafe { &*light }))
    {
        // An out-of-range index is a parameter error, not an unsupported
        // feature: D3D8 returns D3DERR_INVALIDCALL and does not fail loudly.
        Err(failure) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            D3d8Status::InvalidArgument as i32
        }
        Ok(()) => {
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
    }
}

/// `IDirect3DDevice8::GetLight`. `index` must be below 8.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_get_light(
    dev: *mut D3d8Device,
    index: u32,
    out: *mut D3d8Light,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "get_light: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    if out.is_null() {
        write_error(err, D3d8Status::InvalidArgument, "get_light: null out");
        return D3d8Status::InvalidArgument as i32;
    }
    match device.state.light(index) {
        Ok(light) => {
            // SAFETY: caller-provided out-parameter.
            unsafe { *out = light_from_state(&light) };
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
        Err(failure) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            D3d8Status::InvalidArgument as i32
        }
    }
}

/// `IDirect3DDevice8::LightEnable`. `index` must be below 8.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_light_enable(
    dev: *mut D3d8Device,
    index: u32,
    enable: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "light_enable: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    // D3D8 treats any nonzero BOOL as enabled.
    match device.state.light_enable(index, enable != 0) {
        Err(failure) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            D3d8Status::InvalidArgument as i32
        }
        Ok(()) => {
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
    }
}

/// `IDirect3DDevice8::GetLightEnable`. `index` must be below 8.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_get_light_enable(
    dev: *mut D3d8Device,
    index: u32,
    out: *mut u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "get_light_enable: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    if out.is_null() {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "get_light_enable: null out",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    match device.state.light_enabled(index) {
        Ok(enabled) => {
            // SAFETY: caller-provided out-parameter.
            unsafe { *out = enabled as u32 };
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
        Err(failure) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            D3d8Status::InvalidArgument as i32
        }
    }
}

/// `IDirect3DDevice8::DrawPrimitive` with a caller-supplied CPU vertex block.
///
/// The bridge owns the guest vertex buffer and passes an explicit
/// `vertices`/`bytes`/`stride` view; no guest address crosses this boundary.
#[allow(clippy::too_many_arguments)]
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_draw_primitive(
    dev: *mut D3d8Device,
    topology: u32,
    fvf: u32,
    vertices: *const u8,
    bytes: u32,
    stride: u32,
    start_vertex: u32,
    primitive_count: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "draw_primitive: null device",
        );
        return D3d8Status::InvalidArgument as i32;
    };
    if vertices.is_null() || bytes == 0 {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "draw_primitive: null or empty vertices",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: the caller promises `bytes` readable bytes at `vertices`.
    let slice = unsafe { core::slice::from_raw_parts(vertices, bytes as usize) };
    let buffer = match VertexBuffer::borrowed(slice, stride) {
        Ok(buffer) => buffer,
        Err(failure) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                &format!("{}: {}", failure.operation, failure.cause),
            );
            return D3d8Status::InvalidArgument as i32;
        }
    };
    report(
        err,
        device.draw_primitive(topology, fvf, &buffer, start_vertex, primitive_count),
    )
}

/// Copy the current target as tightly packed RGBA8.
///
/// `out_len` receives the number of bytes actually written. If `capacity` is
/// too small, nothing is written and the call returns [`D3d8Status::InvalidArgument`]
/// with `out_len` set to the required size.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_read_pixels(
    dev: *mut D3d8Device,
    out: *mut u8,
    capacity: u32,
    out_len: *mut u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "read_pixels: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    let needed = match u32::try_from(device.read_pixels_len()) {
        Ok(size) => size,
        Err(_) => {
            write_error(
                err,
                D3d8Status::InvalidArgument,
                "read_pixels: target size exceeds u32",
            );
            return D3d8Status::InvalidArgument as i32;
        }
    };
    if !out_len.is_null() {
        // SAFETY: caller-provided out-parameter.
        unsafe { *out_len = needed };
    }
    if out.is_null() || capacity < needed {
        write_error(
            err,
            D3d8Status::InvalidArgument,
            "read_pixels: output buffer too small",
        );
        return D3d8Status::InvalidArgument as i32;
    }
    // SAFETY: capacity >= needed and the caller promises a writable buffer.
    let pixels = unsafe { core::slice::from_raw_parts_mut(out, needed as usize) };
    if let Err(failure) = device.read_pixels_into(pixels) {
        return report(err, Err(failure));
    }
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

/// `IDirect3DDevice8::Present` for the offscreen headless target.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_present(dev: *mut D3d8Device, err: *mut D3d8Error) -> i32 {
    let Some(device) = device_ref(dev) else {
        write_error(err, D3d8Status::InvalidArgument, "present: null device");
        return D3d8Status::InvalidArgument as i32;
    };
    report(err, device.present())
}

/// Opaque CPU allocation, usable without an adapter or GPU. Calls on a resource
/// are serialized by the bridge; borrowed data pointers expire at destruction.
pub struct D3d8Storage {
    _private: (),
}

#[unsafe(no_mangle)]
pub extern "C" fn d3d8_storage_create(size: u32, err: *mut D3d8Error) -> *mut D3d8Storage {
    match CpuStorage::new(size) {
        Ok(storage) => {
            write_error(err, D3d8Status::Ok, "");
            Box::into_raw(Box::new(storage)).cast()
        }
        Err(failure) => {
            report(err, Err(failure));
            core::ptr::null_mut()
        }
    }
}

/// Caller must destroy each owned storage handle exactly once, or pass NULL.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_storage_destroy(storage: *mut D3d8Storage) {
    if !storage.is_null() {
        unsafe { drop(Box::from_raw(storage.cast::<CpuStorage>())) };
    }
}

/// Stable host-only bytes, never a guest address. No resource function resizes
/// the allocation. The bridge must not retain the pointer after destruction.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_storage_data(storage: *mut D3d8Storage) -> *mut u8 {
    if storage.is_null() {
        return core::ptr::null_mut();
    }
    unsafe { (*storage.cast::<CpuStorage>()).bytes.as_mut_ptr() }
}

#[unsafe(no_mangle)]
pub extern "C" fn d3d8_storage_copy(
    dst: *mut D3d8Storage,
    src: *mut D3d8Storage,
    err: *mut D3d8Error,
) -> i32 {
    if dst.is_null() || src.is_null() || dst == src {
        return report(
            err,
            Err(crate::RenderError::invalid(
                "UpdateTexture",
                "invalid storage handles",
            )),
        );
    }
    let (dst, src) = unsafe { (&mut *dst.cast::<CpuStorage>(), &*src.cast::<CpuStorage>()) };
    if dst.bytes.len() != src.bytes.len() {
        return report(
            err,
            Err(crate::RenderError::invalid(
                "UpdateTexture",
                "storage sizes differ",
            )),
        );
    }
    dst.bytes.copy_from_slice(&src.bytes);
    write_error(err, D3d8Status::Ok, "");
    D3d8Status::Ok as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn d3d8_format_bytes(format: u32) -> u32 {
    format_bytes(format)
}

/// CPU layout policy, independent of GPU format support.
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_texture_level_layout(
    width: u32,
    height: u32,
    levels: u32,
    level: u32,
    format: u32,
    out: *mut D3d8LevelLayout,
    err: *mut D3d8Error,
) -> i32 {
    if out.is_null() {
        return report(
            err,
            Err(crate::RenderError::invalid("GetLevelDesc", "null output")),
        );
    }
    match level_layout(width, height, levels, level, format) {
        Ok(layout) => {
            unsafe { *out = layout };
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
        Err(failure) => report(err, Err(failure)),
    }
}

/// D3D8 indexed draw preparation belongs to the renderer. The bridge supplies
/// host byte views and the unchanged guest parameters; no guest pointer crosses.
#[allow(clippy::too_many_arguments)]
#[unsafe(no_mangle)]
pub extern "C" fn d3d8_device_draw_indexed_primitive(
    dev: *mut D3d8Device,
    topology: u32,
    fvf: u32,
    vertices: *const u8,
    vertex_bytes: u32,
    stride: u32,
    indices: *const u8,
    index_bytes: u32,
    index_format: u32,
    base_vertex: u32,
    min_index: u32,
    num_vertices: u32,
    start_index: u32,
    primitive_count: u32,
    err: *mut D3d8Error,
) -> i32 {
    let Some(device) = device_ref(dev) else {
        return report(
            err,
            Err(crate::RenderError::invalid(
                "DrawIndexedPrimitive",
                "null device",
            )),
        );
    };
    if vertices.is_null() || indices.is_null() || vertex_bytes == 0 || index_bytes == 0 {
        return report(
            err,
            Err(crate::RenderError::invalid(
                "DrawIndexedPrimitive",
                "null or empty buffers",
            )),
        );
    }
    let vertices = unsafe { core::slice::from_raw_parts(vertices, vertex_bytes as usize) };
    let indices = unsafe { core::slice::from_raw_parts(indices, index_bytes as usize) };
    let draw = IndexedDraw {
        topology,
        index_format,
        stride,
        base_vertex,
        min_index,
        num_vertices,
        start_index,
        primitive_count,
    };
    match device.draw_indexed_primitive(topology, fvf, vertices, indices, draw) {
        Ok(()) => {
            write_error(err, D3d8Status::Ok, "");
            D3d8Status::Ok as i32
        }
        Err(failure) => {
            // Only the unsupported-topology rejection is state/FVF/TSS-like;
            // the range/handle errors stay hard failures. Survey mode records
            // it and skips the draw so the guest continues.
            if failure.kind == crate::RenderErrorKind::Unsupported
                && device.note_indexed_rejection(&failure, topology, fvf, stride)
            {
                write_error(err, D3d8Status::Ok, "");
                return D3d8Status::Ok as i32;
            }
            report(err, Err(failure))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_storage_abi_round_trip_and_rejections_without_gpu() {
        let mut err = D3d8Error::empty();
        let src = d3d8_storage_create(17, &mut err);
        let dst = d3d8_storage_create(17, &mut err);
        let small = d3d8_storage_create(1, &mut err);
        assert!(!src.is_null() && !dst.is_null() && !small.is_null());
        unsafe {
            let bytes = core::slice::from_raw_parts_mut(d3d8_storage_data(src), 17);
            for (i, byte) in bytes.iter_mut().enumerate() {
                *byte = i as u8;
            }
        }
        assert_eq!(d3d8_storage_copy(dst, src, &mut err), D3d8Status::Ok as i32);
        unsafe {
            assert_eq!(
                core::slice::from_raw_parts(d3d8_storage_data(dst), 17),
                (0..17u8).collect::<Vec<_>>()
            );
        }
        assert_eq!(
            d3d8_storage_copy(dst, dst, &mut err),
            D3d8Status::InvalidArgument as i32
        );
        assert_eq!(
            d3d8_storage_copy(small, src, &mut err),
            D3d8Status::InvalidArgument as i32
        );
        unsafe {
            assert_eq!(*d3d8_storage_data(small), 0);
        }
        assert!(d3d8_storage_create(0, &mut err).is_null());
        assert_eq!(err.status, D3d8Status::InvalidArgument);
        assert!(d3d8_storage_data(core::ptr::null_mut()).is_null());
        d3d8_storage_destroy(src);
        d3d8_storage_destroy(dst);
        d3d8_storage_destroy(small);
        d3d8_storage_destroy(core::ptr::null_mut());
    }

    #[test]
    fn all_plain_data_layouts_match_c_contract() {
        use core::mem::{align_of, offset_of, size_of};
        assert_eq!(size_of::<D3d8Status>(), 4);
        assert_eq!(size_of::<D3d8Error>(), 260);
        assert_eq!(offset_of!(D3d8Error, message), 4);
        assert_eq!(size_of::<D3d8AdapterInfo>(), 152);
        assert_eq!(size_of::<D3d8Matrix>(), 64);
        assert_eq!(size_of::<D3d8LevelLayout>(), 20);
        assert_eq!(align_of::<D3d8LevelLayout>(), 4);
    }

    #[test]
    fn abi_version_is_defined() {
        assert_eq!(d3d8_abi_version(), ABI_VERSION);
    }

    #[test]
    fn null_handles_are_named_errors() {
        let mut err = D3d8Error::empty();
        let status = d3d8_device_clear(
            core::ptr::null_mut(),
            0,
            1,
            0,
            1.0,
            0,
            &mut err as *mut D3d8Error,
        );
        assert_eq!(status, D3d8Status::InvalidArgument as i32);
        assert_eq!(err.status, D3d8Status::InvalidArgument);
        assert!(err.message.starts_with(b"clear: null device"));

        let status = d3d8_device_set_transform(
            core::ptr::null_mut(),
            256,
            core::ptr::null(),
            &mut err as *mut D3d8Error,
        );
        assert_eq!(status, D3d8Status::InvalidArgument as i32);
    }

    #[test]
    fn zero_size_device_is_rejected_before_gpu() {
        let mut err = D3d8Error::empty();
        let dev = d3d8_device_create(0, 0, 21, 0, &mut err as *mut D3d8Error);
        assert!(dev.is_null());
        assert_eq!(err.status, D3d8Status::InvalidArgument);
    }

    #[test]
    fn null_destroy_is_a_noop() {
        d3d8_device_destroy(core::ptr::null_mut());
    }

    #[test]
    fn null_out_adapter_info_is_rejected() {
        let mut err = D3d8Error::empty();
        let status = d3d8_adapter_info(core::ptr::null_mut(), &mut err as *mut D3d8Error);
        assert_eq!(status, D3d8Status::InvalidArgument as i32);
        assert!(err.message.starts_with(b"adapter_info: null out"));
    }

    #[test]
    fn material_and_light_abi_structs_match_guest_layouts() {
        use core::mem::{align_of, size_of};
        // D3DMATERIAL8 = 4 * 16 + 4 = 68; D3DLIGHT8 = 4 + 3*16 + 2*12 + 7*4 = 104.
        assert_eq!(size_of::<D3d8ColorValue>(), 16);
        assert_eq!(size_of::<D3d8Material>(), 68);
        assert_eq!(size_of::<D3d8Light>(), 104);
        assert_eq!(align_of::<D3d8Material>(), 4);
        assert_eq!(align_of::<D3d8Light>(), 4);
        // Field offsets the guest layout depends on.
        let material = D3d8Material::default();
        let base = (&material as *const D3d8Material) as usize;
        let power = (&material.power as *const f32) as usize;
        assert_eq!(power - base, 64);
        let light = D3d8Light::default();
        let base = (&light as *const D3d8Light) as usize;
        let direction = (&light.direction[0] as *const f32) as usize;
        assert_eq!(direction - base, 64);
        let phi = (&light.phi as *const f32) as usize;
        assert_eq!(phi - base, 100);
    }

    #[test]
    fn material_light_null_arguments_are_named_errors() {
        let mut err = D3d8Error::empty();
        let ptr = &mut err as *mut D3d8Error;
        assert_eq!(
            d3d8_device_set_material(core::ptr::null_mut(), core::ptr::null(), ptr),
            D3d8Status::InvalidArgument as i32
        );
        assert!(err.message.starts_with(b"set_material: null device"));
        assert_eq!(
            d3d8_device_set_light(core::ptr::null_mut(), 0, core::ptr::null(), ptr),
            D3d8Status::InvalidArgument as i32
        );
        assert!(err.message.starts_with(b"set_light: null device"));
        assert_eq!(
            d3d8_device_get_light_enable(core::ptr::null_mut(), 0, core::ptr::null_mut(), ptr),
            D3d8Status::InvalidArgument as i32
        );
        assert!(err.message.starts_with(b"get_light_enable: null device"));
    }
}
