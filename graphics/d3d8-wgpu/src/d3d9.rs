use crate::RenderError;
use crate::backend::{DepthAttachment, GpuContext, OffscreenTarget};
use crate::bridge::{
    D3d9AdapterIdentifierResult, D3d9Bindings as BridgeBindings, D3d9BindingsResult,
    D3d9BlockBindingsResult, D3d9BufferDescriptor, D3d9BufferDescriptorResult, D3d9BufferKind,
    D3d9BufferLockRequest, D3d9BufferLockResult, D3d9BufferRequest, D3d9BufferUnlockResult,
    D3d9CapsResult, D3d9CheckDepthStencilRequest, D3d9CheckDeviceFormatRequest,
    D3d9CheckDeviceTypeRequest, D3d9CheckFormatConversionRequest, D3d9CheckMultiSampleRequest,
    D3d9CreateResult, D3d9CreationParameters, D3d9CreationParametersResult, D3d9DeclarationResult,
    D3d9DeviceRequest, D3d9DisplayModeRequest, D3d9DisplayModeResult, D3d9FrameResult,
    D3d9LevelDescriptor, D3d9LevelDescriptorResult, D3d9LevelLockRequest, D3d9LevelLockResult,
    D3d9LightResult, D3d9PresentParams, D3d9PrivateDataResult, D3d9ResetRequest, D3d9ResetResult,
    D3d9ScissorResult, D3d9StreamResult, D3d9SurfaceDescriptor, D3d9SurfaceDescriptorResult,
    D3d9SurfaceRequest, D3d9TextureCreateResult, D3d9TextureDescriptor,
    D3d9TextureDescriptorResult, D3d9TextureKind, D3d9TextureRequest, D3d9VertexElement,
    FloatValuesResult, Status, StatusCode, ValueResult, ValuesResult, Viewport, ViewportResult,
    status,
};
use crate::d3d9::gpu::{D3d9Gpu, D3d9Loss};
pub use crate::d3d9::resource::D3d9Resource;
use crate::d3d9::resource::StagedStorage;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

pub mod format;
pub mod gpu;
pub mod resource;
pub mod shader;
pub mod state;
pub mod wgsl;

static LIVE_DEVICES: AtomicUsize = AtomicUsize::new(0);

struct DeviceReservation;

impl DeviceReservation {
    fn acquire() -> Result<Self, ()> {
        LIVE_DEVICES
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Self)
            .map_err(|_| ())
    }
}

impl Drop for DeviceReservation {
    fn drop(&mut self) {
        LIVE_DEVICES.fetch_sub(1, Ordering::AcqRel);
    }
}

const D3DDEVTYPE_HAL: u32 = 1;
const D3DDEVTYPE_REF: u32 = 2;
const D3DDEVTYPE_SW: u32 = 3;
const D3DDEVTYPE_NULLREF: u32 = 4;

const D3DCREATE_FPU_PRESERVE: u32 = 0x0000_0002;
const D3DCREATE_MULTITHREADED: u32 = 0x0000_0004;
const D3DCREATE_PUREDEVICE: u32 = 0x0000_0010;
const D3DCREATE_SOFTWARE_VERTEXPROCESSING: u32 = 0x0000_0020;
const D3DCREATE_HARDWARE_VERTEXPROCESSING: u32 = 0x0000_0040;
const D3DCREATE_MIXED_VERTEXPROCESSING: u32 = 0x0000_0080;
const D3DCREATE_DISABLE_DRIVER_MANAGEMENT: u32 = 0x0000_0100;
const D3DCREATE_ADAPTERGROUP_DEVICE: u32 = 0x0000_0200;
const D3DCREATE_DISABLE_DRIVER_MANAGEMENT_EX: u32 = 0x0000_0400;
const D3DCREATE_NOWINDOWCHANGES: u32 = 0x0000_0800;
const D3DCREATE_DISABLE_PSGP_THREADING: u32 = 0x0000_2000;
const D3DCREATE_ENABLE_PRESENTSTATS: u32 = 0x0000_4000;
const D3DCREATE_DISABLE_PRINTSCREEN: u32 = 0x0000_8000;
const D3DCREATE_SCREENSAVER: u32 = 0x1000_0000;

const D3DCREATE_KNOWN: u32 = D3DCREATE_FPU_PRESERVE
    | D3DCREATE_MULTITHREADED
    | D3DCREATE_PUREDEVICE
    | D3DCREATE_SOFTWARE_VERTEXPROCESSING
    | D3DCREATE_HARDWARE_VERTEXPROCESSING
    | D3DCREATE_MIXED_VERTEXPROCESSING
    | D3DCREATE_DISABLE_DRIVER_MANAGEMENT
    | D3DCREATE_ADAPTERGROUP_DEVICE
    | D3DCREATE_DISABLE_DRIVER_MANAGEMENT_EX
    | D3DCREATE_NOWINDOWCHANGES
    | D3DCREATE_DISABLE_PSGP_THREADING
    | D3DCREATE_ENABLE_PRESENTSTATS
    | D3DCREATE_DISABLE_PRINTSCREEN
    | D3DCREATE_SCREENSAVER;
const D3DCREATE_VERTEXPROCESSING: u32 = D3DCREATE_SOFTWARE_VERTEXPROCESSING
    | D3DCREATE_HARDWARE_VERTEXPROCESSING
    | D3DCREATE_MIXED_VERTEXPROCESSING;
const D3DCREATE_IMPLEMENTED: u32 =
    D3DCREATE_HARDWARE_VERTEXPROCESSING | D3DCREATE_FPU_PRESERVE | D3DCREATE_NOWINDOWCHANGES;

const D3DFMT_A8R8G8B8: u32 = 21;
const D3DFMT_X8R8G8B8: u32 = 22;

const D3DSWAPEFFECT_DISCARD: u32 = 1;
const D3DSWAPEFFECT_FLIP: u32 = 2;
const D3DSWAPEFFECT_COPY: u32 = 3;

const D3DPRESENTFLAG_KNOWN: u32 = 0x0000_0001 | 0x0000_0002 | 0x0000_0004 | 0x0000_0010;

const D3DCAPS2_CANAUTOGENMIPMAP: u32 = 0x4000_0000;
const D3DPTEXTURECAPS_ALPHA: u32 = 0x0000_0004;
const D3DPTEXTURECAPS_CUBEMAP: u32 = 0x0000_0800;
const D3DPTEXTURECAPS_VOLUMEMAP: u32 = 0x0000_2000;
const D3DPTEXTURECAPS_MIPMAP: u32 = 0x0000_4000;
const D3DPTEXTURECAPS_MIPVOLUMEMAP: u32 = 0x0000_8000;
const D3DPTEXTURECAPS_MIPCUBEMAP: u32 = 0x0001_0000;

struct ImplicitTargets {
    target: OffscreenTarget,
    format: u32,
    depth_format: u32,
}

struct RestorePlan {
    gpu: Option<GpuContext>,
    d3d9_gpu: D3d9Gpu,
    loss: Arc<D3d9Loss>,
    targets: ImplicitTargets,
    present: D3d9PresentParams,
    expected: BTreeMap<u32, u32>,
    staged: BTreeMap<u32, StagedStorage>,
    committed: usize,
    ready: bool,
}

struct ResourceRegistration {
    resource_type: u32,
    pool: u32,
}

struct D3d9Probe {
    description: String,
    vendor_id: u32,
    device_id: u32,
    max_texture_dimension_2d: u32,
    max_texture_dimension_3d: u32,
    features: wgpu::Features,
}

pub struct D3d9Device {
    gpu: Option<GpuContext>,
    d3d9_gpu: Option<D3d9Gpu>,
    reservation: Option<DeviceReservation>,
    adapter: u32,
    device_type: u32,
    behavior_flags: u32,
    present: D3d9PresentParams,
    loss: Option<Arc<D3d9Loss>>,
    targets: Option<ImplicitTargets>,
    resources: BTreeMap<u32, ResourceRegistration>,
    creator_thread: u32,
    features: wgpu::Features,
    limits: wgpu::Limits,
    replacement: Option<(GpuContext, Arc<D3d9Loss>)>,
    restore: Option<RestorePlan>,
    state: state::D3d9State,
    objects: shader::D3d9Objects,
}

impl D3d9Device {
    fn resource_gpu(&self) -> Result<&D3d9Gpu, RenderError> {
        self.d3d9_gpu
            .as_ref()
            .ok_or_else(|| RenderError::invalid("D3D9", "device is not initialized"))
    }
}

fn bridge_error(error: RenderError) -> Status {
    let code = match error.kind {
        crate::RenderErrorKind::InvalidArgument => StatusCode::InvalidArgument,
        crate::RenderErrorKind::OutOfMemory => StatusCode::OutOfMemory,
        crate::RenderErrorKind::Unsupported => StatusCode::Unsupported,
    };
    status(code, error.to_string())
}

fn bridge_ok(result: Result<(), RenderError>) -> Status {
    match result {
        Ok(()) => status(StatusCode::Ok, ""),
        Err(error) => bridge_error(error),
    }
}

fn bridge_gpu_ok(gpu: &D3d9Gpu, result: Result<(), RenderError>) -> Status {
    if gpu.is_physical() {
        status(StatusCode::DeviceLost, gpu.loss().physical_message())
    } else {
        bridge_ok(result)
    }
}

fn live_gpu(resource: &D3d9Resource) -> Result<D3d9Gpu, Status> {
    match resource.gpu_handle() {
        Ok(gpu) if !gpu.is_blocked() => Ok(gpu),
        Ok(_) => Err(status(
            StatusCode::DeviceLost,
            "the wgpu device is not operational",
        )),
        Err(error) => Err(bridge_error(error)),
    }
}

pub fn new_d3d9_resource() -> Box<D3d9Resource> {
    Box::default()
}

pub fn d3d9_create_buffer(
    device: &D3d9Device,
    resource: &mut D3d9Resource,
    request: &D3d9BufferRequest,
) -> Status {
    let gpu = match device.resource_gpu() {
        Ok(gpu) => gpu.clone(),
        Err(error) => return bridge_error(error),
    };
    let kind = match request.kind {
        D3d9BufferKind::Vertex => resource::BufferKind::Vertex,
        D3d9BufferKind::Index => resource::BufferKind::Index,
        _ => return status(StatusCode::InvalidArgument, "invalid D3D9 buffer kind"),
    };
    let mapped = resource::BufferRequest {
        kind,
        size: request.size,
        usage: request.usage,
        pool: request.pool,
        fvf: request.fvf,
        format: request.format,
        shared_handle: request.shared_handle,
    };
    bridge_ok(resource::create_buffer(&gpu, resource, &mapped))
}

pub fn d3d9_buffer_descriptor(resource: &D3d9Resource) -> D3d9BufferDescriptorResult {
    match resource.state.as_ref() {
        Some(resource::ResourceState::Buffer(b)) => D3d9BufferDescriptorResult {
            status: status(StatusCode::Ok, ""),
            descriptor: D3d9BufferDescriptor {
                format: b.format,
                resource_type: resource.resource_type().unwrap_or(0),
                usage: b.usage,
                pool: b.pool,
                size: b.size,
                fvf: b.fvf,
            },
        },
        _ => D3d9BufferDescriptorResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 buffer is not initialized",
            ),
            descriptor: D3d9BufferDescriptor {
                format: 0,
                resource_type: 0,
                usage: 0,
                pool: 0,
                size: 0,
                fvf: 0,
            },
        },
    }
}

fn lock_fail(code: StatusCode, message: impl Into<String>) -> D3d9BufferLockResult {
    D3d9BufferLockResult {
        status: status(code, message),
        offset: 0,
        size: 0,
        total: 0,
    }
}

fn validate_buffer_lock(
    resource: &D3d9Resource,
    request: &D3d9BufferLockRequest,
) -> Result<(u32, u32, u32), D3d9BufferLockResult> {
    let Some(resource::ResourceState::Buffer(b)) = resource.state.as_ref() else {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3D9 buffer is not initialized",
        ));
    };
    let unknown = request.flags & !resource::D3DLOCK_KNOWN;
    if unknown != 0 {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            format!("unknown D3DLOCK flag(s) {unknown:#x}"),
        ));
    }
    if request.flags & resource::D3DLOCK_DYNAMIC_ONLY != 0
        && b.usage & resource::D3DUSAGE_DYNAMIC == 0
    {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3DLOCK_DISCARD and D3DLOCK_NOOVERWRITE require D3DUSAGE_DYNAMIC",
        ));
    }
    if request.flags & resource::D3DLOCK_DONOTWAIT != 0 {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3DLOCK_DONOTWAIT is invalid for vertex and index buffers",
        ));
    }
    if request.flags & resource::D3DLOCK_READONLY != 0
        && request.flags & resource::D3DLOCK_DISCARD != 0
    {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3DLOCK_READONLY cannot be combined with D3DLOCK_DISCARD",
        ));
    }
    if b.usage & resource::D3DUSAGE_WRITEONLY != 0
        && request.flags & resource::D3DLOCK_READONLY != 0
    {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3DUSAGE_WRITEONLY buffers cannot be locked D3DLOCK_READONLY",
        ));
    }
    if b.size == 0 {
        return Err(lock_fail(
            StatusCode::Unsupported,
            "zero-length D3D9 buffer locks are not implemented",
        ));
    }
    let total = b.size;
    if request.offset > total {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3D9 buffer lock offset is past the end",
        ));
    }
    let size = if request.size == 0 {
        total - request.offset
    } else {
        request.size
    };
    if size > total - request.offset {
        return Err(lock_fail(
            StatusCode::InvalidArgument,
            "D3D9 buffer lock range is past the end",
        ));
    }
    Ok((request.offset, size, total))
}

pub fn d3d9_buffer_lock_prepare(
    resource: &mut D3d9Resource,
    request: &D3d9BufferLockRequest,
) -> D3d9BufferLockResult {
    let (offset, size, total) = match validate_buffer_lock(resource, request) {
        Ok(value) => value,
        Err(error) => return error,
    };
    let discard = request.flags & resource::D3DLOCK_DISCARD != 0;
    let first = matches!(
        resource.state.as_ref(),
        Some(resource::ResourceState::Buffer(b)) if b.lock_count == 0
    );
    if !discard && first {
        if let Ok(gpu) = resource.gpu_handle() {
            if let Err(error) = resource::sync_buffer_for_lock(&gpu, resource) {
                return D3d9BufferLockResult {
                    status: bridge_error(error),
                    offset: 0,
                    size: 0,
                    total: 0,
                };
            }
        }
    }
    D3d9BufferLockResult {
        status: status(StatusCode::Ok, ""),
        offset,
        size,
        total,
    }
}

pub fn d3d9_buffer_lock_commit(
    resource: &mut D3d9Resource,
    request: &D3d9BufferLockRequest,
) -> D3d9BufferLockResult {
    let (offset, size, total) = match validate_buffer_lock(resource, request) {
        Ok(value) => value,
        Err(error) => return error,
    };
    let Some(resource::ResourceState::Buffer(b)) = resource.state.as_mut() else {
        return lock_fail(
            StatusCode::InvalidArgument,
            "D3D9 buffer is not initialized",
        );
    };
    let count = match b.lock_count.checked_add(1) {
        Some(count) => count,
        None => return lock_fail(StatusCode::OutOfMemory, "D3D9 buffer lock count overflow"),
    };
    let writable = request.flags & resource::D3DLOCK_READONLY == 0;
    if writable && b.writable.try_reserve(1).is_err() {
        return lock_fail(
            StatusCode::OutOfMemory,
            "D3D9 buffer lock record allocation failed",
        );
    }
    b.lock_count = count;
    if writable {
        if request.flags & resource::D3DLOCK_NO_DIRTY_UPDATE == 0 {
            b.writable_dirty = true;
        }
        b.writable.push((offset, offset.saturating_add(size)));
    }
    D3d9BufferLockResult {
        status: status(StatusCode::Ok, ""),
        offset,
        size,
        total,
    }
}

pub fn d3d9_buffer_bytes(resource: &D3d9Resource) -> &[u8] {
    match resource.state.as_ref() {
        Some(resource::ResourceState::Buffer(b)) => &b.bytes,
        _ => &[],
    }
}

pub fn d3d9_buffer_unlock(resource: &mut D3d9Resource, bytes: &[u8]) -> D3d9BufferUnlockResult {
    let default_pool = matches!(
        resource.state.as_ref(),
        Some(resource::ResourceState::Buffer(b)) if b.pool == resource::D3DPOOL_DEFAULT
    );
    let gpu = if default_pool {
        resource.gpu_handle().ok().filter(|gpu| !gpu.is_blocked())
    } else {
        None
    };
    let Some(resource::ResourceState::Buffer(b)) = resource.state.as_mut() else {
        return D3d9BufferUnlockResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 buffer is not initialized",
            ),
            closed: false,
        };
    };
    if b.lock_count == 0 {
        return D3d9BufferUnlockResult {
            status: status(StatusCode::InvalidArgument, "D3D9 buffer is not locked"),
            closed: false,
        };
    }
    if bytes.len() != b.bytes.len() {
        return D3d9BufferUnlockResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 buffer unlock staging size does not match the buffer",
            ),
            closed: false,
        };
    }
    b.lock_count -= 1;
    let closed = b.lock_count == 0;
    if closed && !b.writable.is_empty() {
        for &(lo, hi) in &b.writable {
            let lo = lo as usize;
            let hi = (hi as usize).min(b.bytes.len());
            if lo < hi {
                b.bytes[lo..hi].copy_from_slice(&bytes[lo..hi]);
            }
        }
        let (lo, hi) = b
            .writable
            .iter()
            .fold((u32::MAX, 0u32), |(lo, hi), &(a, c)| (lo.min(a), hi.max(c)));
        b.writable.clear();
        if hi > lo {
            if default_pool || b.writable_dirty {
                if b.dirty_hi <= b.dirty_lo {
                    b.dirty_lo = lo;
                    b.dirty_hi = hi;
                } else {
                    b.dirty_lo = b.dirty_lo.min(lo);
                    b.dirty_hi = b.dirty_hi.max(hi);
                }
            }
            b.generation = b.generation.wrapping_add(1);
        }
        b.writable_dirty = false;
    }
    if closed {
        if let Some(gpu) = gpu {
            if let Err(error) = resource::upload_buffer(&gpu, resource) {
                return D3d9BufferUnlockResult {
                    status: if gpu.is_physical() {
                        status(StatusCode::Ok, "")
                    } else {
                        bridge_error(error)
                    },
                    closed: true,
                };
            }
        }
    }
    D3d9BufferUnlockResult {
        status: status(StatusCode::Ok, ""),
        closed,
    }
}

pub fn d3d9_resource_type(resource: &D3d9Resource) -> ValueResult {
    match resource.resource_type() {
        Some(value) => ValueResult {
            status: status(StatusCode::Ok, ""),
            value,
        },
        None => ValueResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 resource is not initialized",
            ),
            value: 0,
        },
    }
}

fn texture_kind(kind: D3d9TextureKind) -> Result<resource::TextureKind, RenderError> {
    match kind {
        D3d9TextureKind::Texture2D => Ok(resource::TextureKind::Texture2D),
        D3d9TextureKind::Cube => Ok(resource::TextureKind::Cube),
        D3d9TextureKind::Volume => Ok(resource::TextureKind::Volume),
        _ => Err(RenderError::invalid(
            "CreateTexture",
            "invalid D3D9 texture kind",
        )),
    }
}

pub fn d3d9_create_texture(
    device: &D3d9Device,
    resource: &mut D3d9Resource,
    request: &D3d9TextureRequest,
) -> D3d9TextureCreateResult {
    let gpu = match device.resource_gpu() {
        Ok(gpu) => gpu.clone(),
        Err(error) => {
            return D3d9TextureCreateResult {
                status: bridge_error(error),
                no_autogen: false,
            };
        }
    };
    let kind = match texture_kind(request.kind) {
        Ok(kind) => kind,
        Err(error) => {
            return D3d9TextureCreateResult {
                status: bridge_error(error),
                no_autogen: false,
            };
        }
    };
    let mapped = resource::TextureRequest {
        kind,
        width: request.width,
        height: request.height,
        depth: request.depth,
        levels: request.levels,
        usage: request.usage,
        format: request.format,
        pool: request.pool,
        shared_handle: request.shared_handle,
    };
    match resource::create_texture(&gpu, resource, &mapped) {
        Ok(()) => {
            let no_autogen = request.usage & resource::D3DUSAGE_AUTOGENMIPMAP != 0
                && !matches!(resource.state.as_ref(), Some(resource::ResourceState::Texture(t)) if t.autogen);
            D3d9TextureCreateResult {
                status: status(StatusCode::Ok, ""),
                no_autogen,
            }
        }
        Err(error) => D3d9TextureCreateResult {
            status: bridge_error(error),
            no_autogen: false,
        },
    }
}

pub fn d3d9_create_surface(
    device: &D3d9Device,
    resource: &mut D3d9Resource,
    request: &D3d9SurfaceRequest,
) -> Status {
    let gpu = match device.resource_gpu() {
        Ok(gpu) => gpu.clone(),
        Err(error) => return bridge_error(error),
    };
    let mapped = resource::SurfaceRequest {
        width: request.width,
        height: request.height,
        format: request.format,
        pool: request.pool,
        usage: request.usage,
        lockable: request.lockable,
        discard: request.discard,
        multisample_type: request.multisample_type,
        multisample_quality: request.multisample_quality,
        shared_handle: request.shared_handle,
    };
    bridge_ok(resource::create_surface(&gpu, resource, &mapped))
}

pub fn d3d9_texture_descriptor(resource: &D3d9Resource) -> D3d9TextureDescriptorResult {
    match resource.state.as_ref() {
        Some(resource::ResourceState::Texture(t)) => D3d9TextureDescriptorResult {
            status: status(StatusCode::Ok, ""),
            descriptor: D3d9TextureDescriptor {
                format: t.format,
                resource_type: resource.resource_type().unwrap_or(0),
                usage: t.usage,
                pool: t.pool,
                width: t.width,
                height: t.height,
                depth: t.depth,
                levels: t.levels,
            },
        },
        _ => D3d9TextureDescriptorResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 texture is not initialized",
            ),
            descriptor: D3d9TextureDescriptor {
                format: 0,
                resource_type: 0,
                usage: 0,
                pool: 0,
                width: 0,
                height: 0,
                depth: 0,
                levels: 0,
            },
        },
    }
}

pub fn d3d9_surface_descriptor(resource: &D3d9Resource) -> D3d9SurfaceDescriptorResult {
    match resource.state.as_ref() {
        Some(resource::ResourceState::Surface(s)) => D3d9SurfaceDescriptorResult {
            status: status(StatusCode::Ok, ""),
            descriptor: D3d9SurfaceDescriptor {
                format: s.format,
                resource_type: resource.resource_type().unwrap_or(0),
                usage: s.usage,
                pool: s.pool,
                multisample_type: s.multisample_type,
                multisample_quality: s.multisample_quality,
                width: s.width,
                height: s.height,
            },
        },
        _ => D3d9SurfaceDescriptorResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 surface is not initialized",
            ),
            descriptor: D3d9SurfaceDescriptor {
                format: 0,
                resource_type: 0,
                usage: 0,
                pool: 0,
                multisample_type: 0,
                multisample_quality: 0,
                width: 0,
                height: 0,
            },
        },
    }
}

fn level_descriptor(resource: &D3d9Resource, face: u32, level: u32) -> D3d9LevelDescriptorResult {
    let format = match resource.state.as_ref() {
        Some(resource::ResourceState::Texture(t)) => t.format,
        Some(resource::ResourceState::Surface(s)) => s.format,
        _ => {
            return D3d9LevelDescriptorResult {
                status: status(
                    StatusCode::InvalidArgument,
                    "D3D9 texture is not initialized",
                ),
                descriptor: D3d9LevelDescriptor {
                    format: 0,
                    width: 0,
                    height: 0,
                    depth: 0,
                    levels: 0,
                },
            };
        }
    };
    match resource::level_descriptor(resource, face, level) {
        Ok((width, height, depth, levels)) => D3d9LevelDescriptorResult {
            status: status(StatusCode::Ok, ""),
            descriptor: D3d9LevelDescriptor {
                format,
                width,
                height,
                depth,
                levels,
            },
        },
        Err(error) => D3d9LevelDescriptorResult {
            status: bridge_error(error),
            descriptor: D3d9LevelDescriptor {
                format: 0,
                width: 0,
                height: 0,
                depth: 0,
                levels: 0,
            },
        },
    }
}

pub fn d3d9_texture_level_descriptor(
    resource: &D3d9Resource,
    face: u32,
    level: u32,
) -> D3d9LevelDescriptorResult {
    level_descriptor(resource, face, level)
}

pub fn d3d9_texture_volume_descriptor(
    resource: &D3d9Resource,
    level: u32,
) -> D3d9LevelDescriptorResult {
    level_descriptor(resource, 0, level)
}

fn map_lock_request(request: &D3d9LevelLockRequest) -> resource::LockRequest {
    resource::LockRequest {
        face: request.face,
        level: request.level,
        volume: request.volume,
        full_region: request.full_region,
        left: if request.volume {
            request.volume_box.left as i64
        } else {
            request.rect.left as i64
        },
        top: if request.volume {
            request.volume_box.top as i64
        } else {
            request.rect.top as i64
        },
        front: request.volume_box.front as i64,
        right: if request.volume {
            request.volume_box.right as i64
        } else {
            request.rect.right as i64
        },
        bottom: if request.volume {
            request.volume_box.bottom as i64
        } else {
            request.rect.bottom as i64
        },
        back: request.volume_box.back as i64,
        flags: request.flags,
    }
}

fn lock_result_fail(error: RenderError) -> D3d9LevelLockResult {
    D3d9LevelLockResult {
        status: bridge_error(error),
        pitch: 0,
        slice_pitch: 0,
        offset: 0,
        size: 0,
        total: 0,
    }
}

pub fn d3d9_level_lock_prepare(
    resource: &mut D3d9Resource,
    request: &D3d9LevelLockRequest,
) -> D3d9LevelLockResult {
    let mapped = map_lock_request(request);
    if let Err(error) = resource::validate_level_lock(resource, &mapped) {
        return lock_result_fail(error);
    }
    let gpu = match resource.gpu_handle() {
        Ok(gpu) => gpu,
        Err(error) => return lock_result_fail(error),
    };
    if request.flags & resource::D3DLOCK_DONOTWAIT != 0
        && request.flags & resource::D3DLOCK_DISCARD == 0
        && !gpu.is_blocked()
        && resource::lock_would_block(resource, request.face, request.level)
    {
        return D3d9LevelLockResult {
            status: status(
                StatusCode::WasStillDrawing,
                "the surface is busy and cannot be locked without waiting",
            ),
            pitch: 0,
            slice_pitch: 0,
            offset: 0,
            size: 0,
            total: 0,
        };
    }
    match resource::level_lock_prepare(&gpu, resource, &mapped) {
        Ok(result) => D3d9LevelLockResult {
            status: status(StatusCode::Ok, ""),
            pitch: result.pitch,
            slice_pitch: result.slice_pitch,
            offset: result.offset,
            size: result.size,
            total: result.total,
        },
        Err(error) => lock_result_fail(error),
    }
}

pub fn d3d9_level_lock_commit(
    resource: &mut D3d9Resource,
    request: &D3d9LevelLockRequest,
) -> D3d9LevelLockResult {
    let gpu = match resource.gpu_handle() {
        Ok(gpu) => gpu,
        Err(error) => return lock_result_fail(error),
    };
    match resource::level_lock_commit(&gpu, resource, &map_lock_request(request)) {
        Ok(result) => D3d9LevelLockResult {
            status: status(StatusCode::Ok, ""),
            pitch: result.pitch,
            slice_pitch: result.slice_pitch,
            offset: result.offset,
            size: result.size,
            total: result.total,
        },
        Err(error) => lock_result_fail(error),
    }
}

pub fn d3d9_level_bytes(resource: &D3d9Resource, face: u32, level: u32) -> &[u8] {
    resource::level_bytes(resource, face, level).unwrap_or(&[])
}

pub fn d3d9_level_unlock(
    resource: &mut D3d9Resource,
    face: u32,
    level: u32,
    bytes: &[u8],
) -> D3d9BufferUnlockResult {
    let closed = match resource::level_unlock(resource, face, level, bytes) {
        Ok(closed) => closed,
        Err(error) => {
            return D3d9BufferUnlockResult {
                status: bridge_error(error),
                closed: false,
            };
        }
    };
    if closed && resource::is_default_pool(resource) {
        if let Some(gpu) = resource.gpu_handle().ok().filter(|gpu| !gpu.is_blocked()) {
            if let Err(error) = resource::upload_level(&gpu, resource, face, level) {
                return D3d9BufferUnlockResult {
                    status: if gpu.is_physical() {
                        status(StatusCode::Ok, "")
                    } else {
                        bridge_error(error)
                    },
                    closed: true,
                };
            }
            if resource::autogen_capable(resource) {
                if let Err(error) = resource::generate_mips(&gpu, resource) {
                    return D3d9BufferUnlockResult {
                        status: if gpu.is_physical() {
                            status(StatusCode::Ok, "")
                        } else {
                            bridge_error(error)
                        },
                        closed: true,
                    };
                }
            }
        }
    }
    D3d9BufferUnlockResult {
        status: status(StatusCode::Ok, ""),
        closed,
    }
}

pub fn d3d9_set_priority(resource: &mut D3d9Resource, priority: u32) -> ValueResult {
    let old = resource.set_priority(priority);
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: old,
    }
}

pub fn d3d9_priority(resource: &D3d9Resource) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: resource.priority,
    }
}

pub fn d3d9_set_lod(resource: &mut D3d9Resource, lod: u32) -> ValueResult {
    let old = resource.set_lod(lod);
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: old,
    }
}

pub fn d3d9_lod(resource: &D3d9Resource) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: resource.lod,
    }
}

pub fn d3d9_set_private_data(
    resource: &mut D3d9Resource,
    view: u32,
    guid: &[u8],
    data: &[u8],
    flags: u32,
) -> Status {
    if flags & resource::D3DSPD_IUNKNOWN != 0 {
        return status(
            StatusCode::Unsupported,
            "D3DSPD_IUNKNOWN private data is not implemented",
        );
    }
    if flags != 0 {
        return status(StatusCode::InvalidArgument, "unknown private-data flag");
    }
    if guid.len() != 16 {
        return status(
            StatusCode::InvalidArgument,
            "private-data GUID must be 16 bytes",
        );
    }
    let mut key = [0u8; 16];
    key.copy_from_slice(guid);
    let mut bytes = Vec::new();
    if bytes.try_reserve_exact(data.len()).is_err() {
        return status(StatusCode::OutOfMemory, "private-data allocation failed");
    }
    bytes.extend_from_slice(data);
    match resource.set_private_data(view, key, bytes) {
        Ok(()) => status(StatusCode::Ok, ""),
        Err(error) => bridge_error(error),
    }
}

pub fn d3d9_get_private_data(
    resource: &D3d9Resource,
    view: u32,
    guid: &[u8],
) -> D3d9PrivateDataResult {
    if guid.len() != 16 {
        return D3d9PrivateDataResult {
            status: status(
                StatusCode::InvalidArgument,
                "private-data GUID must be 16 bytes",
            ),
            found: false,
            data: Vec::new(),
        };
    }
    let mut key = [0u8; 16];
    key.copy_from_slice(guid);
    match resource.get_private_data(view, &key) {
        Some(bytes) => match format::copy_bytes(bytes) {
            Ok(data) => D3d9PrivateDataResult {
                status: status(StatusCode::Ok, ""),
                found: true,
                data,
            },
            Err(error) => D3d9PrivateDataResult {
                status: bridge_error(error),
                found: false,
                data: Vec::new(),
            },
        },
        None => D3d9PrivateDataResult {
            status: status(StatusCode::Ok, ""),
            found: false,
            data: Vec::new(),
        },
    }
}

pub fn d3d9_free_private_data(resource: &mut D3d9Resource, view: u32, guid: &[u8]) -> Status {
    if guid.len() != 16 {
        return status(
            StatusCode::InvalidArgument,
            "private-data GUID must be 16 bytes",
        );
    }
    let mut key = [0u8; 16];
    key.copy_from_slice(guid);
    if resource.free_private_data(view, &key) {
        status(StatusCode::Ok, "")
    } else {
        status(StatusCode::InvalidArgument, "private data is not present")
    }
}

pub fn d3d9_add_dirty_rect(
    resource: &mut D3d9Resource,
    face: u32,
    level: u32,
    full_region: bool,
    rect: &crate::bridge::D3d9LockRect,
) -> Status {
    bridge_ok(resource::add_dirty_rect(
        resource,
        face,
        level,
        full_region,
        rect.left,
        rect.top,
        rect.right,
        rect.bottom,
    ))
}

pub fn d3d9_upload_buffer(resource: &mut D3d9Resource) -> Status {
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(&gpu, resource::upload_buffer(&gpu, resource))
}

pub fn d3d9_readback_buffer(resource: &mut D3d9Resource) -> Status {
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(&gpu, resource::readback_buffer(&gpu, resource))
}

pub fn d3d9_upload_level(resource: &mut D3d9Resource, face: u32, level: u32) -> Status {
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(&gpu, resource::upload_level(&gpu, resource, face, level))
}

pub fn d3d9_readback_level(resource: &mut D3d9Resource, face: u32, level: u32) -> Status {
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(
        &gpu,
        resource::readback_level_explicit(&gpu, resource, face, level),
    )
}

pub fn d3d9_preload(resource: &mut D3d9Resource) -> Status {
    if !resource.initialized() {
        return status(StatusCode::Ok, "");
    }
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(&gpu, resource::preload(&gpu, resource))
}

pub fn d3d9_add_dirty_box(
    resource: &mut D3d9Resource,
    level: u32,
    full_region: bool,
    bounds: &crate::bridge::D3d9LockBox,
) -> Status {
    bridge_ok(resource::add_dirty_box(
        resource,
        level,
        full_region,
        bounds.left,
        bounds.top,
        bounds.right,
        bounds.bottom,
        bounds.front,
        bounds.back,
    ))
}

pub fn d3d9_set_autogen_filter(resource: &mut D3d9Resource, filter: u32) -> Status {
    bridge_ok(resource::set_autogen_filter(resource, filter))
}

pub fn d3d9_get_autogen_filter(resource: &D3d9Resource) -> ValueResult {
    match resource::autogen_filter(resource) {
        Ok(value) => ValueResult {
            status: status(StatusCode::Ok, ""),
            value,
        },
        Err(error) => ValueResult {
            status: bridge_error(error),
            value: 0,
        },
    }
}

pub fn d3d9_generate_mips(resource: &mut D3d9Resource) -> Status {
    let gpu = match live_gpu(resource) {
        Ok(gpu) => gpu,
        Err(status) => return status,
    };
    bridge_gpu_ok(&gpu, resource::generate_mips(&gpu, resource))
}

pub fn d3d9_autogen_capable(resource: &D3d9Resource) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: u32::from(resource::autogen_capable(resource)),
    }
}

pub fn d3d9_format_autogen_capable(format: u32) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: u32::from(format::autogen_capable(format)),
    }
}

impl Default for D3d9Device {
    fn default() -> Self {
        Self {
            gpu: None,
            d3d9_gpu: None,
            reservation: None,
            adapter: 0,
            device_type: 0,
            behavior_flags: 0,
            loss: None,
            targets: None,
            resources: BTreeMap::new(),
            creator_thread: 0,
            features: wgpu::Features::empty(),
            limits: wgpu::Limits::default(),
            replacement: None,
            restore: None,
            state: state::D3d9State::default(),
            objects: shader::D3d9Objects::default(),
            present: D3d9PresentParams {
                width: 0,
                height: 0,
                format: 0,
                count: 0,
                multisample_type: 0,
                multisample_quality: 0,
                swap_effect: 0,
                windowed: 0,
                enable_auto_depth_stencil: 0,
                auto_depth_stencil_format: 0,
                flags: 0,
                fullscreen_refresh_rate: 0,
                presentation_interval: 0,
            },
        }
    }
}

pub fn new_d3d9_device() -> Box<D3d9Device> {
    Box::default()
}

fn fail(
    present: &D3d9PresentParams,
    code: StatusCode,
    message: impl Into<String>,
) -> D3d9CreateResult {
    D3d9CreateResult {
        status: status(code, message),
        present: *present,
    }
}

fn validate_present_params(
    request: &D3d9PresentParams,
    desktop_format: u32,
) -> Result<D3d9PresentParams, RenderError> {
    let mut present = *request;
    if present.count == 0 {
        present.count = 1;
    }
    if present.count > 1 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            "back buffer count greater than one is not implemented",
        ));
    }
    if present.format == 0 {
        if present.windowed == 0 {
            return Err(RenderError::invalid(
                "D3DPRESENT_PARAMETERS",
                "fullscreen device requires a back buffer format",
            ));
        }
        if desktop_format == 0 {
            return Err(RenderError::new(
                "D3DPRESENT_PARAMETERS",
                "windowed D3DFMT_UNKNOWN needs a desktop format the host cannot provide",
            ));
        }
        present.format = desktop_format;
    }
    if present.format != D3DFMT_A8R8G8B8 && present.format != D3DFMT_X8R8G8B8 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            format!(
                "back buffer D3DFORMAT {} is not implemented; only A8R8G8B8 (21) and X8R8G8B8 (22)",
                present.format
            ),
        ));
    }
    if present.windowed == 0 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            "fullscreen D3D9 devices are not implemented",
        ));
    }
    if present.width == 0 || present.height == 0 {
        return Err(RenderError::invalid(
            "D3DPRESENT_PARAMETERS",
            "back buffer dimensions must be nonzero",
        ));
    }
    match present.swap_effect {
        D3DSWAPEFFECT_DISCARD => {}
        D3DSWAPEFFECT_COPY => {
            return Err(RenderError::new(
                "D3DPRESENT_PARAMETERS",
                "D3DSWAPEFFECT_COPY is not implemented; only D3DSWAPEFFECT_DISCARD",
            ));
        }
        D3DSWAPEFFECT_FLIP => {
            return Err(RenderError::new(
                "D3DPRESENT_PARAMETERS",
                "D3DSWAPEFFECT_FLIP is not implemented",
            ));
        }
        other => {
            return Err(RenderError::invalid(
                "D3DPRESENT_PARAMETERS",
                format!("unknown D3DSWAPEFFECT {other}"),
            ));
        }
    }
    if present.multisample_type != 0 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            format!(
                "D3DMULTISAMPLE_TYPE {} is not implemented",
                present.multisample_type
            ),
        ));
    }
    if present.multisample_quality != 0 {
        return Err(RenderError::invalid(
            "D3DPRESENT_PARAMETERS",
            "multisample quality must be zero without multisampling",
        ));
    }
    if present.presentation_interval != 0 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            format!(
                "presentation interval {:#x} is not implemented; only D3DPRESENT_INTERVAL_DEFAULT",
                present.presentation_interval
            ),
        ));
    }
    if present.fullscreen_refresh_rate != 0 {
        return Err(RenderError::invalid(
            "D3DPRESENT_PARAMETERS",
            "windowed device requires a zero fullscreen refresh rate",
        ));
    }
    let unknown_flags = present.flags & !D3DPRESENTFLAG_KNOWN;
    if unknown_flags != 0 {
        return Err(RenderError::invalid(
            "D3DPRESENT_PARAMETERS",
            format!("unknown D3DPRESENTFLAG(s) {unknown_flags:#x}"),
        ));
    }
    if present.flags != 0 {
        return Err(RenderError::new(
            "D3DPRESENT_PARAMETERS",
            format!("D3DPRESENTFLAG(s) {:#x} are not implemented", present.flags),
        ));
    }
    if present.enable_auto_depth_stencil != 0 {
        if present.auto_depth_stencil_format == 0 {
            return Err(RenderError::invalid(
                "D3DPRESENT_PARAMETERS",
                "automatic depth stencil requires a format",
            ));
        }
        if !format::is_depth(present.auto_depth_stencil_format) {
            return Err(RenderError::new(
                "D3DPRESENT_PARAMETERS",
                format!(
                    "automatic depth stencil format {} is not implemented",
                    present.auto_depth_stencil_format
                ),
            ));
        }
    } else {
        present.auto_depth_stencil_format = 0;
    }
    Ok(present)
}

fn create_d3d9_depth(
    gpu: &GpuContext,
    width: u32,
    height: u32,
    depth_format: u32,
) -> Result<DepthAttachment, RenderError> {
    let wgpu_format = format::d3d_to_wgpu(depth_format).ok_or_else(|| {
        RenderError::new(
            "D3D9DepthTarget",
            format!("D3DFORMAT {depth_format} has no depth mapping"),
        )
    })?;
    let max = gpu.device.limits().max_texture_dimension_2d;
    if width == 0 || height == 0 || width > max || height > max {
        return Err(RenderError::new(
            "D3D9DepthTarget",
            format!("depth target {width}x{height} exceeds max_texture_dimension_2d {max}"),
        ));
    }
    let d3d9_gpu = D3d9Gpu::from_context(gpu);
    d3d9_gpu.validate("D3D9DepthTarget", || {
        let texture = d3d9_gpu.device.create_texture(&wgpu::TextureDescriptor {
            label: Some("d3d9-implicit-depth"),
            size: wgpu::Extent3d {
                width,
                height,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu_format,
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
            view_formats: &[],
        });
        let view = texture.create_view(&wgpu::TextureViewDescriptor::default());
        Ok(DepthAttachment {
            texture,
            view,
            format: wgpu_format,
            has_stencil: depth_format == format::D3DFMT_D24S8,
            d3d_format: depth_format,
        })
    })
}

fn create_targets(
    gpu: &GpuContext,
    present: &D3d9PresentParams,
) -> Result<ImplicitTargets, RenderError> {
    let depth_format = if present.enable_auto_depth_stencil != 0 {
        present.auto_depth_stencil_format
    } else {
        0
    };
    let mut target = gpu.create_target(present.width, present.height, present.format, 0)?;
    if depth_format != 0 {
        target.depth = Some(create_d3d9_depth(
            gpu,
            present.width,
            present.height,
            depth_format,
        )?);
    }
    Ok(ImplicitTargets {
        target,
        format: present.format,
        depth_format,
    })
}

fn attach_loss(gpu: &GpuContext, loss: &Arc<D3d9Loss>) {
    let callback_loss = Arc::clone(loss);
    gpu.device.set_device_lost_callback(move |reason, message| {
        callback_loss.mark_physical(format!("{reason:?}: {message}"));
    });
    let handler_loss = Arc::clone(loss);
    let handler: Arc<dyn wgpu::UncapturedErrorHandler> = Arc::new(move |error: wgpu::Error| {
        if handler_loss.is_physical() {
            log::error!("d3d9-wgpu error during physical device loss: {error}");
        } else {
            panic!("d3d9-wgpu uncaptured wgpu error: {error}");
        }
    });
    gpu.device.on_uncaptured_error(handler);
}

pub fn d3d9_create_device(
    device: &mut D3d9Device,
    request: &D3d9DeviceRequest,
) -> D3d9CreateResult {
    if device.gpu.is_some() {
        return fail(
            &request.present,
            StatusCode::InvalidArgument,
            "D3D9 device is already initialized",
        );
    }
    if request.adapter != 0 {
        return fail(
            &request.present,
            StatusCode::InvalidArgument,
            format!(
                "adapter {} does not exist; only adapter 0 is available",
                request.adapter
            ),
        );
    }
    if request.device_type != D3DDEVTYPE_HAL {
        return fail(
            &request.present,
            StatusCode::Unsupported,
            format!(
                "D3DDEVTYPE {} is not implemented; only D3DDEVTYPE_HAL",
                request.device_type
            ),
        );
    }
    let unknown = request.behavior_flags & !D3DCREATE_KNOWN;
    if unknown != 0 {
        return fail(
            &request.present,
            StatusCode::InvalidArgument,
            format!("unknown D3DCREATE flag(s) {unknown:#x}"),
        );
    }
    let vertex_processing = request.behavior_flags & D3DCREATE_VERTEXPROCESSING;
    if vertex_processing == 0 || vertex_processing.count_ones() != 1 {
        return fail(
            &request.present,
            StatusCode::InvalidArgument,
            "exactly one vertex processing D3DCREATE flag is required",
        );
    }
    if vertex_processing != D3DCREATE_HARDWARE_VERTEXPROCESSING {
        return fail(
            &request.present,
            StatusCode::Unsupported,
            format!(
                "D3DCREATE vertex processing flag {vertex_processing:#x} is not implemented; \
                 only D3DCREATE_HARDWARE_VERTEXPROCESSING"
            ),
        );
    }
    let extra = request.behavior_flags & !D3DCREATE_IMPLEMENTED;
    if extra != 0 {
        return fail(
            &request.present,
            StatusCode::Unsupported,
            format!("D3DCREATE flag(s) {extra:#x} are not implemented"),
        );
    }
    let present = match validate_present_params(&request.present, request.desktop_format) {
        Ok(present) => present,
        Err(error) => return fail_status(&request.present, bridge_error(error)),
    };
    let reservation = match DeviceReservation::acquire() {
        Ok(reservation) => reservation,
        Err(()) => {
            return fail_status(
                &present,
                status(
                    StatusCode::Unsupported,
                    "more than one D3D9 device is not implemented",
                ),
            );
        }
    };
    let gpu = match pollster::block_on(GpuContext::new_headless_d3d9()) {
        Ok(gpu) => gpu,
        Err(error) => return fail_status(&present, status(StatusCode::Backend, error.to_string())),
    };
    let loss = Arc::new(D3d9Loss::default());
    attach_loss(&gpu, &loss);
    let targets = match create_targets(&gpu, &present) {
        Ok(targets) => targets,
        Err(error) => return fail_status(&present, bridge_error(error)),
    };
    device.features = gpu.device.features();
    device.limits = gpu.device.limits();
    device.d3d9_gpu = Some(D3d9Gpu::from_context_with_loss(&gpu, loss.as_ref().clone()));
    device.gpu = Some(gpu);
    device.reservation = Some(reservation);
    device.adapter = request.adapter;
    device.device_type = request.device_type;
    device.behavior_flags = request.behavior_flags;
    device.present = present;
    device.state.set_target_size(present.width, present.height);
    if present.enable_auto_depth_stencil != 0 {
        let _ = device.state.set_render_state(7, 1);
    }
    device.creator_thread = request.thread_id;
    device.loss = Some(loss);
    device.targets = Some(targets);
    D3d9CreateResult {
        status: status(StatusCode::Ok, ""),
        present,
    }
}

pub fn d3d9_register_resource(device: &mut D3d9Device, id: u32, resource: &D3d9Resource) -> Status {
    let Some(pool) = resource.pool() else {
        return status(
            StatusCode::Unsupported,
            "resource metadata is not initialized",
        );
    };
    let Some(resource_type) = resource.resource_type() else {
        return status(
            StatusCode::Unsupported,
            "resource metadata is not initialized",
        );
    };
    if device
        .resources
        .insert(
            id,
            ResourceRegistration {
                resource_type,
                pool,
            },
        )
        .is_some()
    {
        return status(
            StatusCode::InvalidArgument,
            "resource id is already registered",
        );
    }
    status(StatusCode::Ok, "")
}

pub fn d3d9_unregister_resource(device: &mut D3d9Device, id: u32) -> Status {
    device.resources.remove(&id);
    status(StatusCode::Ok, "")
}

pub fn d3d9_registered_resource_ids(device: &D3d9Device) -> ValuesResult {
    if device.gpu.is_none() {
        return ValuesResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            values: Vec::new(),
        };
    }
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values: device.resources.keys().copied().collect(),
    }
}

fn poll_device(gpu: &GpuContext) -> Result<(), Status> {
    gpu.device
        .poll(wgpu::PollType::Poll)
        .map_err(|error| status(StatusCode::Backend, format!("device poll failed: {error}")))?;
    Ok(())
}

fn reset_validation(
    device: &D3d9Device,
    request: &D3d9ResetRequest,
) -> Result<D3d9PresentParams, Status> {
    if !request.window_valid {
        return Err(status(
            StatusCode::InvalidArgument,
            "Reset requires a usable presentation window",
        ));
    }
    let present =
        validate_present_params(&request.present, request.desktop_format).map_err(bridge_error)?;
    if request.implicit_external_refs {
        return Err(status(
            StatusCode::InvalidArgument,
            "Reset requires outstanding implicit surface references to be released first",
        ));
    }
    if device.state.has_live_state_blocks() {
        return Err(status(
            StatusCode::InvalidArgument,
            "Reset requires every live state block to be released first",
        ));
    }
    if device
        .resources
        .values()
        .any(|registration| registration.pool == resource::D3DPOOL_DEFAULT)
    {
        return Err(status(
            StatusCode::InvalidArgument,
            "Reset requires every live D3DPOOL_DEFAULT resource to be released first",
        ));
    }
    Ok(present)
}

fn replacement_ready(device: &mut D3d9Device) -> Result<(), String> {
    if let Some((gpu, loss)) = device.replacement.as_ref() {
        if !loss.is_physical() && poll_device(gpu).is_ok() && !loss.is_physical() {
            return Ok(());
        }
    }
    device.replacement = None;
    let gpu =
        pollster::block_on(GpuContext::new_headless_d3d9()).map_err(|error| error.to_string())?;
    if !gpu.device.features().contains(device.features)
        || !device.limits.check_limits(&gpu.device.limits())
    {
        return Err(
            "replacement adapter is not compatible with the original device capabilities"
                .to_string(),
        );
    }
    let loss = Arc::new(D3d9Loss::default());
    attach_loss(&gpu, &loss);
    poll_device(&gpu).map_err(|error| error.message)?;
    if loss.is_physical() {
        return Err("replacement device was lost during creation".to_string());
    }
    device.replacement = Some((gpu, loss));
    Ok(())
}

pub fn d3d9_reset(device: &mut D3d9Device, request: &D3d9ResetRequest) -> D3d9ResetResult {
    if device.gpu.is_none() {
        return reset_fail(
            request.present,
            status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
        );
    }
    if request.thread_id != device.creator_thread {
        return reset_fail(
            request.present,
            status(
                StatusCode::InvalidArgument,
                "Reset must be called from the thread that created the device",
            ),
        );
    }
    if device.restore.is_some() {
        return reset_fail(
            request.present,
            status(
                StatusCode::InvalidArgument,
                "a reset is already in progress",
            ),
        );
    }
    if let Err(error) = poll_device(device.gpu.as_ref().expect("device is initialized")) {
        if let Some(loss) = device.loss.as_ref() {
            loss.mark_reset_required();
        }
        return reset_fail(request.present, error);
    }
    let physical = device.loss.as_ref().is_some_and(|loss| loss.is_physical());
    let present = match reset_validation(device, request) {
        Ok(present) => present,
        Err(error) => {
            if let Some(loss) = device.loss.as_ref() {
                loss.mark_reset_required();
            }
            return reset_fail(request.present, error);
        }
    };
    let expected: BTreeMap<u32, u32> = device
        .resources
        .iter()
        .map(|(id, registration)| (*id, registration.pool))
        .collect();
    if physical {
        if let Err(message) = replacement_ready(device) {
            if let Some(loss) = device.loss.as_ref() {
                loss.mark_reset_required();
            }
            return reset_fail(
                present,
                status(
                    StatusCode::DeviceLost,
                    format!("device recovery unavailable: {message}"),
                ),
            );
        }
        let (gpu, loss) = device.replacement.take().expect("replacement is present");
        let targets = match create_targets(&gpu, &present) {
            Ok(targets) => targets,
            Err(error) => {
                device.replacement = Some((gpu, loss));
                return reset_fail(present, status(StatusCode::DeviceLost, error.to_string()));
            }
        };
        let d3d9_gpu = D3d9Gpu::from_context_with_loss(&gpu, loss.as_ref().clone());
        device.restore = Some(RestorePlan {
            gpu: Some(gpu),
            d3d9_gpu,
            loss,
            targets,
            present,
            expected,
            staged: BTreeMap::new(),
            committed: 0,
            ready: false,
        });
    } else {
        if let Some(loss) = device.loss.as_ref() {
            loss.mark_reset_required();
        }
        let gpu = device.gpu.as_ref().expect("device is initialized");
        let targets = match create_targets(gpu, &present) {
            Ok(targets) => targets,
            Err(error) => {
                let status = match device.loss.as_ref() {
                    Some(loss) if loss.is_physical() => {
                        status(StatusCode::DeviceLost, loss.physical_message())
                    }
                    _ => bridge_error(error),
                };
                return reset_fail(present, status);
            }
        };
        let d3d9_gpu = device
            .d3d9_gpu
            .clone()
            .expect("device GPU context is initialized");
        let loss = Arc::clone(device.loss.as_ref().expect("device loss is initialized"));
        device.restore = Some(RestorePlan {
            gpu: None,
            d3d9_gpu,
            loss,
            targets,
            present,
            expected,
            staged: BTreeMap::new(),
            committed: 0,
            ready: false,
        });
    }
    D3d9ResetResult {
        status: status(StatusCode::Ok, ""),
        present: reset_output_present(present),
    }
}

pub fn d3d9_prepare_resource_reset(
    device: &mut D3d9Device,
    id: u32,
    resource: &D3d9Resource,
) -> Status {
    let Some(plan) = device.restore.as_mut() else {
        return status(StatusCode::InvalidArgument, "no reset is in progress");
    };
    let Some(expected_pool) = plan.expected.get(&id).copied() else {
        return status(
            StatusCode::InvalidArgument,
            "resource is not part of the reset set",
        );
    };
    if plan.staged.contains_key(&id) {
        return status(StatusCode::InvalidArgument, "resource is already staged");
    }
    if resource.pool() != Some(expected_pool) {
        return status(
            StatusCode::InvalidArgument,
            "resource pool does not match its registration",
        );
    }
    if plan.d3d9_gpu.is_physical() {
        return status(StatusCode::DeviceLost, "the replacement device is lost");
    }
    match resource::stage_restore(&plan.d3d9_gpu, resource) {
        Ok(staged) => {
            plan.staged.insert(id, staged);
            status(StatusCode::Ok, "")
        }
        Err(error) => {
            if plan.d3d9_gpu.is_physical() || plan.loss.is_physical() {
                return status(StatusCode::DeviceLost, plan.loss.physical_message());
            }
            bridge_error(error)
        }
    }
}

pub fn d3d9_reset_ready(device: &mut D3d9Device) -> Status {
    let required_features = device.features;
    let required_limits = device.limits.clone();
    let Some(plan) = device.restore.as_mut() else {
        return status(StatusCode::InvalidArgument, "no reset is in progress");
    };
    if plan.staged.len() != plan.expected.len()
        || !plan.expected.keys().all(|id| plan.staged.contains_key(id))
    {
        return status(
            StatusCode::InvalidArgument,
            "reset staged set is incomplete",
        );
    }
    if plan.loss.is_physical() || plan.d3d9_gpu.is_physical() {
        return status(StatusCode::DeviceLost, plan.loss.physical_message());
    }
    if let Some(gpu) = plan.gpu.as_ref() {
        if !gpu.device.features().contains(required_features)
            || !required_limits.check_limits(&gpu.device.limits())
        {
            return status(
                StatusCode::DeviceLost,
                "replacement capabilities changed before commit",
            );
        }
    }
    if let Err(error) = plan.d3d9_gpu.flush() {
        if plan.loss.is_physical() || plan.d3d9_gpu.is_physical() {
            return status(StatusCode::DeviceLost, plan.loss.physical_message());
        }
        return bridge_error(error);
    }
    if plan.loss.is_physical() || plan.d3d9_gpu.is_physical() {
        return status(StatusCode::DeviceLost, plan.loss.physical_message());
    }
    plan.ready = true;
    status(StatusCode::Ok, "")
}

pub fn d3d9_commit_resource_reset(device: &mut D3d9Device, id: u32, resource: &mut D3d9Resource) {
    let plan = device
        .restore
        .as_mut()
        .expect("D3D9 reset commit invariant: no reset is in progress");
    assert!(
        plan.ready,
        "D3D9 reset commit invariant: reset is not ready"
    );
    let staged = plan
        .staged
        .remove(&id)
        .expect("D3D9 reset commit invariant: resource was not staged");
    resource::install_staged(resource, staged, &plan.d3d9_gpu);
    plan.committed += 1;
}

pub fn d3d9_finish_reset(device: &mut D3d9Device) -> D3d9ResetResult {
    let plan = device
        .restore
        .take()
        .expect("D3D9 reset finish invariant: no reset is in progress");
    assert!(
        plan.ready,
        "D3D9 reset finish invariant: reset is not ready"
    );
    assert_eq!(
        plan.committed,
        plan.expected.len(),
        "D3D9 reset finish invariant: reset is not fully committed"
    );
    let lost = plan.loss.is_physical();
    device.targets = Some(plan.targets);
    device.present = plan.present;
    device
        .state
        .set_target_size(plan.present.width, plan.present.height);
    device.state.clear_pipeline();
    if plan.present.enable_auto_depth_stencil != 0 {
        let _ = device.state.set_render_state(7, 1);
    }
    if let Some(gpu) = plan.gpu {
        device.features = gpu.device.features();
        device.limits = gpu.device.limits();
        device.gpu = Some(gpu);
        device.d3d9_gpu = Some(plan.d3d9_gpu);
        device.loss = Some(plan.loss);
        if let Some(loss) = device.loss.as_ref() {
            loss.clear_reset_required();
        }
    } else {
        plan.loss.clear_reset_required();
    }
    let status = if lost {
        status(
            StatusCode::DeviceLost,
            "replacement device was lost before completion",
        )
    } else {
        status(StatusCode::Ok, "")
    };
    D3d9ResetResult {
        status,
        present: reset_output_present(plan.present),
    }
}

pub fn d3d9_abort_reset(device: &mut D3d9Device) -> Status {
    device.restore = None;
    device.replacement = None;
    status(StatusCode::Ok, "")
}

pub fn d3d9_test_cooperative_level(device: &mut D3d9Device, thread_id: u32) -> ValueResult {
    if device.gpu.is_none() {
        return ValueResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            value: 0,
        };
    }
    if thread_id != device.creator_thread {
        return ValueResult {
            status: status(
                StatusCode::InvalidArgument,
                "TestCooperativeLevel must be called from the thread that created the device",
            ),
            value: 0,
        };
    }
    if let Err(error) = poll_device(device.gpu.as_ref().expect("device is initialized")) {
        return ValueResult {
            status: error,
            value: 0,
        };
    }
    let value = if device.loss.as_ref().is_some_and(|loss| loss.is_physical()) {
        if replacement_ready(device).is_ok() {
            2
        } else {
            1
        }
    } else if device
        .loss
        .as_ref()
        .is_some_and(|loss| loss.is_reset_required())
    {
        2
    } else {
        0
    };
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value,
    }
}

fn frame_fail(status: Status) -> D3d9FrameResult {
    D3d9FrameResult {
        status,
        width: 0,
        height: 0,
        bytes: Vec::new(),
    }
}

pub fn d3d9_present(device: &mut D3d9Device) -> D3d9FrameResult {
    if device.gpu.is_none() {
        return frame_fail(status(
            StatusCode::InvalidArgument,
            "D3D9 device is not initialized",
        ));
    }
    if let Some(loss) = device.loss.as_ref() {
        if loss.is_physical() {
            return frame_fail(status(StatusCode::DeviceLost, loss.physical_message()));
        }
        if loss.is_reset_required() {
            return frame_fail(status(
                StatusCode::DeviceLost,
                "the device needs a successful Reset before Present",
            ));
        }
    }
    let Some(targets) = device.targets.as_ref() else {
        return frame_fail(status(
            StatusCode::InvalidArgument,
            "D3D9 device has no implicit swap chain",
        ));
    };
    let width = targets.target.width;
    let height = targets.target.height;
    let mut bytes = Vec::new();
    let len = (width as usize)
        .saturating_mul(height as usize)
        .saturating_mul(4);
    if bytes.try_reserve_exact(len).is_err() {
        return frame_fail(status(
            StatusCode::OutOfMemory,
            "present readback allocation failed",
        ));
    }
    bytes.resize(len, 0);
    let gpu = device.gpu.as_ref().expect("device is initialized");
    if let Err(error) = gpu.read_pixels_into(&targets.target, &mut bytes) {
        if let Some(loss) = device.loss.as_ref().filter(|loss| loss.is_physical()) {
            return frame_fail(status(StatusCode::DeviceLost, loss.physical_message()));
        }
        return frame_fail(bridge_error(error));
    }
    D3d9FrameResult {
        status: status(StatusCode::Ok, ""),
        width,
        height,
        bytes,
    }
}

fn empty_surface_descriptor() -> D3d9SurfaceDescriptor {
    D3d9SurfaceDescriptor {
        format: 0,
        resource_type: 0,
        usage: 0,
        pool: 0,
        multisample_type: 0,
        multisample_quality: 0,
        width: 0,
        height: 0,
    }
}

fn implicit_surface_descriptor(device: &D3d9Device, depth: bool) -> D3d9SurfaceDescriptorResult {
    let Some(targets) = device.targets.as_ref() else {
        return D3d9SurfaceDescriptorResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device has no implicit swap chain",
            ),
            descriptor: empty_surface_descriptor(),
        };
    };
    if depth && targets.target.depth.is_none() {
        return D3d9SurfaceDescriptorResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device has no depth buffer",
            ),
            descriptor: empty_surface_descriptor(),
        };
    }
    D3d9SurfaceDescriptorResult {
        status: status(StatusCode::Ok, ""),
        descriptor: D3d9SurfaceDescriptor {
            format: if depth {
                targets.depth_format
            } else {
                targets.format
            },
            resource_type: resource::D3DRTYPE_SURFACE,
            usage: if depth {
                resource::D3DUSAGE_DEPTHSTENCIL
            } else {
                resource::D3DUSAGE_RENDERTARGET
            },
            pool: resource::D3DPOOL_DEFAULT,
            multisample_type: 0,
            multisample_quality: 0,
            width: targets.target.width,
            height: targets.target.height,
        },
    }
}

pub fn d3d9_backbuffer_descriptor(device: &D3d9Device) -> D3d9SurfaceDescriptorResult {
    implicit_surface_descriptor(device, false)
}

pub fn d3d9_depthbuffer_descriptor(device: &D3d9Device) -> D3d9SurfaceDescriptorResult {
    implicit_surface_descriptor(device, true)
}

pub fn d3d9_number_of_swap_chains(device: &D3d9Device) -> ValueResult {
    if device.gpu.is_none() {
        return ValueResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            value: 0,
        };
    }
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value: 1,
    }
}

pub fn d3d9_display_mode(
    device: &D3d9Device,
    request: &D3d9DisplayModeRequest,
) -> D3d9DisplayModeResult {
    let empty = D3d9DisplayModeResult {
        status: status(StatusCode::Ok, ""),
        width: 0,
        height: 0,
        refresh_rate: 0,
        format: 0,
    };
    if device.gpu.is_none() {
        return D3d9DisplayModeResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            ..empty
        };
    }
    if request.swap_chain != 0 {
        return D3d9DisplayModeResult {
            status: status(
                StatusCode::InvalidArgument,
                format!("swap chain {} does not exist", request.swap_chain),
            ),
            ..empty
        };
    }
    if request.format == 0 {
        return D3d9DisplayModeResult {
            status: status(
                StatusCode::NotAvailable,
                "the host desktop bit depth has no D3DFORMAT",
            ),
            ..empty
        };
    }
    D3d9DisplayModeResult {
        status: status(StatusCode::Ok, ""),
        width: request.width,
        height: request.height,
        refresh_rate: request.refresh_rate,
        format: request.format,
    }
}

fn adapter_probe() -> Result<&'static D3d9Probe, RenderError> {
    static PROBE: std::sync::OnceLock<Result<D3d9Probe, String>> = std::sync::OnceLock::new();
    let entry = PROBE.get_or_init(|| {
        let gpu = pollster::block_on(GpuContext::new_headless_d3d9())
            .map_err(|error| error.to_string())?;
        let limits = gpu.device.limits();
        Ok(D3d9Probe {
            description: gpu.adapter_info.name.clone(),
            vendor_id: gpu.adapter_info.vendor,
            device_id: gpu.adapter_info.device,
            max_texture_dimension_2d: limits.max_texture_dimension_2d,
            max_texture_dimension_3d: limits.max_texture_dimension_3d,
            features: gpu.device.features(),
        })
    });
    match entry {
        Ok(probe) => Ok(probe),
        Err(message) => Err(RenderError::new("D3D9AdapterProbe", message.clone())),
    }
}

fn put_u32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn caps_image(probe: &D3d9Probe) -> Vec<u8> {
    let mut caps = vec![0u8; 304];
    put_u32(&mut caps, 0, D3DDEVTYPE_HAL);
    put_u32(&mut caps, 12, D3DCAPS2_CANAUTOGENMIPMAP);
    put_u32(
        &mut caps,
        60,
        D3DPTEXTURECAPS_ALPHA
            | D3DPTEXTURECAPS_CUBEMAP
            | D3DPTEXTURECAPS_VOLUMEMAP
            | D3DPTEXTURECAPS_MIPMAP
            | D3DPTEXTURECAPS_MIPVOLUMEMAP
            | D3DPTEXTURECAPS_MIPCUBEMAP,
    );
    put_u32(&mut caps, 88, probe.max_texture_dimension_2d);
    put_u32(&mut caps, 92, probe.max_texture_dimension_2d);
    put_u32(&mut caps, 96, probe.max_texture_dimension_3d);
    put_u32(&mut caps, 100, probe.max_texture_dimension_2d);
    put_u32(&mut caps, 104, probe.max_texture_dimension_2d);
    put_u32(&mut caps, 108, state::MAX_ANISOTROPY);
    put_u32(&mut caps, 148, 8);
    put_u32(&mut caps, 152, 8);
    put_u32(&mut caps, 160, 8);
    put_u32(&mut caps, 164, 6);
    put_u32(&mut caps, 188, 16);
    put_u32(&mut caps, 192, 0xffff);
    put_u32(&mut caps, 200, 256);
    put_u32(&mut caps, 232, 1);
    put_u32(&mut caps, 236, 0x1ffff);
    put_u32(&mut caps, 240, 1);
    caps
}

pub fn d3d9_adapter_caps(adapter: u32) -> D3d9CapsResult {
    if adapter != 0 {
        return D3d9CapsResult {
            status: status(
                StatusCode::InvalidArgument,
                format!("adapter {adapter} does not exist"),
            ),
            caps: Vec::new(),
        };
    }
    match adapter_probe() {
        Ok(probe) => D3d9CapsResult {
            status: status(StatusCode::Ok, ""),
            caps: caps_image(probe),
        },
        Err(error) => D3d9CapsResult {
            status: bridge_error(error),
            caps: Vec::new(),
        },
    }
}

pub fn d3d9_device_caps(device: &D3d9Device) -> D3d9CapsResult {
    if device.gpu.is_none() {
        return D3d9CapsResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            caps: Vec::new(),
        };
    }
    d3d9_adapter_caps(device.adapter)
}

pub fn d3d9_adapter_identifier(adapter: u32) -> D3d9AdapterIdentifierResult {
    if adapter != 0 {
        return D3d9AdapterIdentifierResult {
            status: status(
                StatusCode::InvalidArgument,
                format!("adapter {adapter} does not exist"),
            ),
            description: String::new(),
            vendor_id: 0,
            device_id: 0,
            sub_sys_id: 0,
            revision: 0,
            whql_level: 0,
            driver_version_low: 0,
            driver_version_high: 0,
        };
    }
    match adapter_probe() {
        Ok(probe) => D3d9AdapterIdentifierResult {
            status: status(StatusCode::Ok, ""),
            description: probe.description.clone(),
            vendor_id: probe.vendor_id,
            device_id: probe.device_id,
            sub_sys_id: 0,
            revision: 0,
            whql_level: 0,
            driver_version_low: 0,
            driver_version_high: 0,
        },
        Err(error) => D3d9AdapterIdentifierResult {
            status: bridge_error(error),
            description: String::new(),
            vendor_id: 0,
            device_id: 0,
            sub_sys_id: 0,
            revision: 0,
            whql_level: 0,
            driver_version_low: 0,
            driver_version_high: 0,
        },
    }
}

fn check_buffer_format(request: &D3d9CheckDeviceFormatRequest) -> Status {
    let unknown = request.usage & !resource::BUFFER_USAGE_MASK;
    if unknown != 0 {
        return status(
            StatusCode::NotAvailable,
            format!("unknown D3DUSAGE bits {unknown:#x}"),
        );
    }
    let ok = if request.resource_type == resource::D3DRTYPE_VERTEXBUFFER {
        request.check_format == format::D3DFMT_VERTEXDATA
    } else {
        request.check_format == format::D3DFMT_INDEX16
            || request.check_format == format::D3DFMT_INDEX32
    };
    if !ok {
        return status(
            StatusCode::NotAvailable,
            format!("buffer D3DFORMAT {} is not available", request.check_format),
        );
    }
    status(StatusCode::Ok, "")
}

fn check_adapter(adapter: u32, device_type: u32) -> Result<(), Status> {
    if adapter != 0 {
        return Err(status(
            StatusCode::InvalidArgument,
            format!("adapter {adapter} does not exist"),
        ));
    }
    match device_type {
        D3DDEVTYPE_HAL => Ok(()),
        D3DDEVTYPE_REF | D3DDEVTYPE_SW | D3DDEVTYPE_NULLREF => Err(status(
            StatusCode::NotAvailable,
            format!("D3DDEVTYPE {device_type} is not available"),
        )),
        _ => Err(status(
            StatusCode::InvalidArgument,
            format!("D3DDEVTYPE {device_type} is invalid"),
        )),
    }
}

fn adapter_format_supported(adapter_format: u32) -> bool {
    adapter_format == D3DFMT_A8R8G8B8 || adapter_format == D3DFMT_X8R8G8B8
}

fn format_allows_render_target(probe: &D3d9Probe, fmt: u32) -> bool {
    if format::is_depth(fmt) || format::is_block(fmt) {
        return false;
    }
    let Some(wgpu_format) = format::d3d_to_wgpu(fmt) else {
        return false;
    };
    wgpu_format
        .guaranteed_format_features(probe.features)
        .allowed_usages
        .contains(wgpu::TextureUsages::RENDER_ATTACHMENT)
}

pub fn d3d9_check_device_type(request: &D3d9CheckDeviceTypeRequest) -> Status {
    if let Err(status) = check_adapter(request.adapter, request.device_type) {
        return status;
    }
    if request.windowed == 0 {
        return status(
            StatusCode::NotAvailable,
            "fullscreen devices are not available",
        );
    }
    if !adapter_format_supported(request.adapter_format) {
        return status(
            StatusCode::NotAvailable,
            format!("adapter format {} is not available", request.adapter_format),
        );
    }
    if request.backbuffer_format == 0 {
        return status(StatusCode::Ok, "");
    }
    if !adapter_format_supported(request.backbuffer_format) {
        return status(
            StatusCode::NotAvailable,
            format!(
                "back buffer D3DFORMAT {} is not available",
                request.backbuffer_format
            ),
        );
    }
    status(StatusCode::Ok, "")
}

pub fn d3d9_check_device_format(request: &D3d9CheckDeviceFormatRequest) -> Status {
    if let Err(status) = check_adapter(request.adapter, request.device_type) {
        return status;
    }
    if !adapter_format_supported(request.adapter_format) {
        return status(
            StatusCode::NotAvailable,
            format!("adapter format {} is not available", request.adapter_format),
        );
    }
    let probe = match adapter_probe() {
        Ok(probe) => probe,
        Err(error) => return bridge_error(error),
    };
    let fmt = request.check_format;
    if request.usage & resource::D3DUSAGE_QUERY_MASK != 0 {
        return status(
            StatusCode::NotAvailable,
            "D3DUSAGE query capabilities are not implemented",
        );
    }
    if matches!(
        request.resource_type,
        resource::D3DRTYPE_VERTEXBUFFER | resource::D3DRTYPE_INDEXBUFFER
    ) {
        return check_buffer_format(request);
    }
    if !format::is_supported(fmt) {
        return status(
            StatusCode::NotAvailable,
            format!("D3DFORMAT {fmt} is not available"),
        );
    }
    let usage_mask = match request.resource_type {
        resource::D3DRTYPE_VERTEXBUFFER | resource::D3DRTYPE_INDEXBUFFER => {
            resource::BUFFER_USAGE_MASK
        }
        resource::D3DRTYPE_TEXTURE
        | resource::D3DRTYPE_CUBETEXTURE
        | resource::D3DRTYPE_VOLUMETEXTURE
        | resource::D3DRTYPE_VOLUME => resource::TEXTURE_USAGE_MASK,
        resource::D3DRTYPE_SURFACE => resource::SURFACE_USAGE_MASK,
        _ => {
            return status(
                StatusCode::NotAvailable,
                format!("unknown resource type {}", request.resource_type),
            );
        }
    };
    let unknown = request.usage & !(usage_mask | resource::D3DUSAGE_QUERY_MASK);
    if unknown != 0 {
        return status(
            StatusCode::NotAvailable,
            format!("unknown D3DUSAGE bits {unknown:#x}"),
        );
    }
    let depth = format::is_depth(fmt);
    let render_target = request.usage & resource::D3DUSAGE_RENDERTARGET != 0;
    let depth_stencil = request.usage & resource::D3DUSAGE_DEPTHSTENCIL != 0;
    let autogen = request.usage & resource::D3DUSAGE_AUTOGENMIPMAP != 0;
    if let Some(feature) = format::required_feature(fmt) {
        if !probe.features.contains(feature) {
            return status(
                StatusCode::NotAvailable,
                format!("D3DFORMAT {fmt} needs wgpu feature {feature:?}"),
            );
        }
    }
    match request.resource_type {
        resource::D3DRTYPE_TEXTURE | resource::D3DRTYPE_CUBETEXTURE => {
            if request.resource_type == resource::D3DRTYPE_CUBETEXTURE && depth {
                return status(
                    StatusCode::NotAvailable,
                    "cube textures cannot be depth stencils",
                );
            }
            if depth {
                if render_target {
                    return status(
                        StatusCode::NotAvailable,
                        "depth formats are not render targets",
                    );
                }
                if !depth_stencil {
                    return status(
                        StatusCode::NotAvailable,
                        "depth formats need D3DUSAGE_DEPTHSTENCIL",
                    );
                }
            } else if depth_stencil {
                return status(
                    StatusCode::NotAvailable,
                    "D3DUSAGE_DEPTHSTENCIL needs a depth format",
                );
            }
            if render_target && !format_allows_render_target(probe, fmt) {
                return status(
                    StatusCode::NotAvailable,
                    format!("D3DFORMAT {fmt} is not a render target"),
                );
            }
            if autogen
                && (!format::autogen_capable(fmt) || !format_allows_render_target(probe, fmt))
            {
                return status(StatusCode::NoAutogen, "");
            }
        }
        resource::D3DRTYPE_VOLUMETEXTURE | resource::D3DRTYPE_VOLUME => {
            if depth || depth_stencil {
                return status(StatusCode::NotAvailable, "volumes cannot be depth stencils");
            }
            if render_target {
                return status(
                    StatusCode::NotAvailable,
                    "volume textures cannot be render targets",
                );
            }
            if autogen {
                return status(
                    StatusCode::NotAvailable,
                    "volume textures cannot use autogen mipmaps",
                );
            }
            if format::is_block(fmt)
                && !probe
                    .features
                    .contains(wgpu::Features::TEXTURE_COMPRESSION_BC_SLICED_3D)
            {
                return status(
                    StatusCode::NotAvailable,
                    "compressed volumes need TEXTURE_COMPRESSION_BC_SLICED_3D",
                );
            }
        }
        resource::D3DRTYPE_SURFACE => {
            if autogen {
                return status(
                    StatusCode::NotAvailable,
                    "surfaces cannot use autogen mipmaps",
                );
            }
            if depth {
                if render_target {
                    return status(
                        StatusCode::NotAvailable,
                        "depth formats are not render targets",
                    );
                }
                if !depth_stencil {
                    return status(
                        StatusCode::NotAvailable,
                        "depth formats need D3DUSAGE_DEPTHSTENCIL",
                    );
                }
            } else if depth_stencil {
                return status(
                    StatusCode::NotAvailable,
                    "D3DUSAGE_DEPTHSTENCIL needs a depth format",
                );
            }
            if render_target && !format_allows_render_target(probe, fmt) {
                return status(
                    StatusCode::NotAvailable,
                    format!("D3DFORMAT {fmt} is not a render target"),
                );
            }
        }
        _ => unreachable!(),
    }
    status(StatusCode::Ok, "")
}

pub fn d3d9_check_device_multisample_type(request: &D3d9CheckMultiSampleRequest) -> ValueResult {
    if let Err(status) = check_adapter(request.adapter, request.device_type) {
        return ValueResult { status, value: 0 };
    }
    if request.windowed == 0 {
        return ValueResult {
            status: status(
                StatusCode::NotAvailable,
                "fullscreen devices are not available",
            ),
            value: 0,
        };
    }
    if !format::is_supported(request.surface_format) {
        return ValueResult {
            status: status(
                StatusCode::NotAvailable,
                format!("D3DFORMAT {} is not available", request.surface_format),
            ),
            value: 0,
        };
    }
    let probe = match adapter_probe() {
        Ok(probe) => probe,
        Err(error) => {
            return ValueResult {
                status: bridge_error(error),
                value: 0,
            };
        }
    };
    if let Some(feature) = format::required_feature(request.surface_format) {
        if !probe.features.contains(feature) {
            return ValueResult {
                status: status(
                    StatusCode::NotAvailable,
                    format!(
                        "D3DFORMAT {} needs wgpu feature {feature:?}",
                        request.surface_format
                    ),
                ),
                value: 0,
            };
        }
    }
    if request.multisample_type == 0 {
        if !format::is_depth(request.surface_format)
            && !format_allows_render_target(probe, request.surface_format)
        {
            return ValueResult {
                status: status(
                    StatusCode::NotAvailable,
                    format!(
                        "D3DFORMAT {} is not a render target",
                        request.surface_format
                    ),
                ),
                value: 0,
            };
        }
        return ValueResult {
            status: status(StatusCode::Ok, ""),
            value: 1,
        };
    }
    ValueResult {
        status: status(
            StatusCode::NotAvailable,
            format!(
                "D3DMULTISAMPLE_TYPE {} is not available",
                request.multisample_type
            ),
        ),
        value: 0,
    }
}

pub fn d3d9_check_depth_stencil_match(request: &D3d9CheckDepthStencilRequest) -> Status {
    if let Err(status) = check_adapter(request.adapter, request.device_type) {
        return status;
    }
    let probe = match adapter_probe() {
        Ok(probe) => probe,
        Err(error) => return bridge_error(error),
    };
    if !format::is_supported(request.render_target_format)
        || format::is_depth(request.render_target_format)
        || !format_allows_render_target(probe, request.render_target_format)
    {
        return status(
            StatusCode::NotAvailable,
            format!(
                "render target D3DFORMAT {} is not available",
                request.render_target_format
            ),
        );
    }
    if !format::is_supported(request.depth_stencil_format)
        || !format::is_depth(request.depth_stencil_format)
    {
        return status(
            StatusCode::NotAvailable,
            format!(
                "depth stencil D3DFORMAT {} is not available",
                request.depth_stencil_format
            ),
        );
    }
    if request.adapter_format != D3DFMT_A8R8G8B8 && request.adapter_format != D3DFMT_X8R8G8B8 {
        return status(
            StatusCode::NotAvailable,
            format!("adapter format {} is not available", request.adapter_format),
        );
    }
    status(StatusCode::Ok, "")
}

pub fn d3d9_check_device_format_conversion(request: &D3d9CheckFormatConversionRequest) -> Status {
    if let Err(status) = check_adapter(request.adapter, request.device_type) {
        return status;
    }
    status(
        StatusCode::NotAvailable,
        "format conversion is not available",
    )
}

fn reset_output_present(mut present: D3d9PresentParams) -> D3d9PresentParams {
    present.width = 0;
    present.height = 0;
    if present.windowed != 0 {
        present.format = 0;
    }
    present.count = 0;
    present
}

fn fail_status(present: &D3d9PresentParams, status: Status) -> D3d9CreateResult {
    D3d9CreateResult {
        status,
        present: *present,
    }
}

fn reset_fail(present: D3d9PresentParams, status: Status) -> D3d9ResetResult {
    D3d9ResetResult {
        status,
        present: reset_output_present(present),
    }
}

pub fn d3d9_creation_parameters(device: &D3d9Device) -> D3d9CreationParametersResult {
    if device.gpu.is_none() {
        return D3d9CreationParametersResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3D9 device is not initialized",
            ),
            parameters: D3d9CreationParameters {
                adapter: 0,
                device_type: 0,
                behavior_flags: 0,
            },
        };
    }
    D3d9CreationParametersResult {
        status: status(StatusCode::Ok, ""),
        parameters: D3d9CreationParameters {
            adapter: device.adapter,
            device_type: device.device_type,
            behavior_flags: device.behavior_flags,
        },
    }
}

fn ok_value(value: u32) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value,
    }
}

fn err_value(error: RenderError) -> ValueResult {
    ValueResult {
        status: bridge_error(error),
        value: 0,
    }
}

fn ok_values(values: Vec<u32>) -> ValuesResult {
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values,
    }
}

fn err_values(error: RenderError) -> ValuesResult {
    ValuesResult {
        status: bridge_error(error),
        values: Vec::new(),
    }
}

fn ok_floats(values: Vec<f32>) -> FloatValuesResult {
    FloatValuesResult {
        status: status(StatusCode::Ok, ""),
        values,
    }
}

fn err_floats(error: RenderError) -> FloatValuesResult {
    FloatValuesResult {
        status: bridge_error(error),
        values: Vec::new(),
    }
}

fn validate_binding(device: &D3d9Device, id: u32, allowed: &[u32]) -> Result<(), RenderError> {
    if id == 0 {
        return Ok(());
    }
    let Some(registration) = device.resources.get(&id) else {
        return Err(RenderError::invalid(
            "D3D9",
            "resource id is not registered with this device",
        ));
    };
    if !allowed.contains(&registration.resource_type) {
        return Err(RenderError::invalid(
            "D3D9",
            "resource kind cannot be bound to this slot",
        ));
    }
    if registration.pool == resource::D3DPOOL_SYSTEMMEM
        || registration.pool == resource::D3DPOOL_SCRATCH
    {
        return Err(RenderError::invalid(
            "D3D9",
            "SYSTEMMEM and SCRATCH resources cannot be bound",
        ));
    }
    Ok(())
}

fn bridge_bindings(bindings: state::D3d9Bindings) -> BridgeBindings {
    BridgeBindings {
        textures: bindings.textures.to_vec(),
        streams: bindings.streams.to_vec(),
        indices: bindings.indices,
        vertex_shader: bindings.vertex_shader,
        pixel_shader: bindings.pixel_shader,
        declaration: bindings.declaration,
    }
}

fn empty_bridge_bindings() -> BridgeBindings {
    BridgeBindings {
        textures: vec![0; state::MAX_SAMPLERS],
        streams: vec![0; state::MAX_STREAMS],
        indices: 0,
        vertex_shader: 0,
        pixel_shader: 0,
        declaration: 0,
    }
}

pub fn d3d9_set_render_state(device: &mut D3d9Device, state: u32, value: u32) -> Status {
    bridge_ok(device.state.set_render_state(state, value))
}

pub fn d3d9_get_render_state(device: &D3d9Device, state: u32) -> ValueResult {
    match device.state.render_state(state) {
        Ok(value) => ok_value(value),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_texture_stage_state(
    device: &mut D3d9Device,
    stage: u32,
    state: u32,
    value: u32,
) -> Status {
    bridge_ok(device.state.set_texture_stage_state(stage, state, value))
}

pub fn d3d9_get_texture_stage_state(device: &D3d9Device, stage: u32, state: u32) -> ValueResult {
    match device.state.texture_stage_state(stage, state) {
        Ok(value) => ok_value(value),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_sampler_state(
    device: &mut D3d9Device,
    sampler: u32,
    state: u32,
    value: u32,
) -> Status {
    bridge_ok(device.state.set_sampler_state(sampler, state, value))
}

pub fn d3d9_get_sampler_state(device: &D3d9Device, sampler: u32, state: u32) -> ValueResult {
    match device.state.sampler_state(sampler, state) {
        Ok(value) => ok_value(value),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_transform(device: &mut D3d9Device, state: u32, values: &[f32]) -> Status {
    let Ok(matrix) = <[f32; 16]>::try_from(values) else {
        return bridge_error(RenderError::invalid(
            "SetTransform",
            "a transform requires exactly 16 floats",
        ));
    };
    bridge_ok(device.state.set_transform(state, matrix))
}

pub fn d3d9_get_transform(device: &D3d9Device, state: u32) -> FloatValuesResult {
    match device.state.transform(state) {
        Ok(matrix) => ok_floats(matrix.to_vec()),
        Err(error) => err_floats(error),
    }
}

pub fn d3d9_multiply_transform(device: &mut D3d9Device, state: u32, values: &[f32]) -> Status {
    let Ok(matrix) = <[f32; 16]>::try_from(values) else {
        return bridge_error(RenderError::invalid(
            "MultiplyTransform",
            "a transform requires exactly 16 floats",
        ));
    };
    bridge_ok(device.state.multiply_transform(state, matrix))
}

pub fn d3d9_set_viewport(device: &mut D3d9Device, viewport: &Viewport) -> Status {
    bridge_ok(device.state.set_viewport(state::Viewport {
        x: viewport.x,
        y: viewport.y,
        width: viewport.width,
        height: viewport.height,
        min_z: viewport.min_z,
        max_z: viewport.max_z,
    }))
}

pub fn d3d9_get_viewport(device: &D3d9Device) -> ViewportResult {
    let viewport = device.state.viewport();
    ViewportResult {
        status: status(StatusCode::Ok, ""),
        viewport: Viewport {
            x: viewport.x,
            y: viewport.y,
            width: viewport.width,
            height: viewport.height,
            min_z: viewport.min_z,
            max_z: viewport.max_z,
        },
    }
}

pub fn d3d9_set_scissor(
    device: &mut D3d9Device,
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
) -> Status {
    bridge_ok(device.state.set_scissor(state::Scissor {
        left,
        top,
        right,
        bottom,
    }))
}

pub fn d3d9_get_scissor(device: &D3d9Device) -> D3d9ScissorResult {
    let scissor = device.state.scissor();
    D3d9ScissorResult {
        status: status(StatusCode::Ok, ""),
        left: scissor.left,
        top: scissor.top,
        right: scissor.right,
        bottom: scissor.bottom,
    }
}

pub fn d3d9_set_material(device: &mut D3d9Device, values: &[f32]) -> Status {
    if values.len() != 17 {
        return bridge_error(RenderError::invalid(
            "SetMaterial",
            "a material requires exactly 17 floats",
        ));
    }
    let mut material = [0u32; 17];
    for (slot, value) in material.iter_mut().zip(values) {
        *slot = value.to_bits();
    }
    bridge_ok(device.state.set_material(material))
}

pub fn d3d9_get_material(device: &D3d9Device) -> FloatValuesResult {
    ok_floats(
        device
            .state
            .material()
            .iter()
            .map(|b| f32::from_bits(*b))
            .collect(),
    )
}

pub fn d3d9_set_light(
    device: &mut D3d9Device,
    index: u32,
    light_type: u32,
    values: &[f32],
) -> Status {
    if values.len() != 25 {
        return bridge_error(RenderError::invalid(
            "SetLight",
            "a light requires exactly 25 floats",
        ));
    }
    let mut light = [0u32; 26];
    light[0] = light_type;
    for (slot, value) in light[1..].iter_mut().zip(values) {
        *slot = value.to_bits();
    }
    bridge_ok(device.state.set_light(index, light))
}

pub fn d3d9_get_light(device: &D3d9Device, index: u32) -> D3d9LightResult {
    match device.state.light(index) {
        Ok(light) => D3d9LightResult {
            status: status(StatusCode::Ok, ""),
            light_type: light[0],
            values: light[1..].iter().map(|b| f32::from_bits(*b)).collect(),
        },
        Err(error) => D3d9LightResult {
            status: bridge_error(error),
            light_type: 0,
            values: Vec::new(),
        },
    }
}

pub fn d3d9_set_light_enable(device: &mut D3d9Device, index: u32, enable: bool) -> Status {
    bridge_ok(device.state.set_light_enable(index, enable))
}

pub fn d3d9_get_light_enable(device: &D3d9Device, index: u32) -> ValueResult {
    match device.state.light_enabled(index) {
        Ok(enabled) => ok_value(u32::from(enabled)),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_clip_plane(device: &mut D3d9Device, index: u32, values: &[f32]) -> Status {
    let Ok(plane) = <[f32; 4]>::try_from(values) else {
        return bridge_error(RenderError::invalid(
            "SetClipPlane",
            "a clip plane requires exactly 4 floats",
        ));
    };
    bridge_ok(device.state.set_clip_plane(index, plane))
}

pub fn d3d9_get_clip_plane(device: &D3d9Device, index: u32) -> FloatValuesResult {
    if index as usize >= state::MAX_CLIP_PLANES {
        return err_floats(RenderError::invalid(
            "GetClipPlane",
            format!("clip plane {index} is out of range"),
        ));
    }
    ok_floats(device.state.clip_plane(index).to_vec())
}

pub fn d3d9_set_texture(device: &mut D3d9Device, stage: u32, id: u32) -> Status {
    if let Err(error) = validate_binding(
        device,
        id,
        &[
            resource::D3DRTYPE_TEXTURE,
            resource::D3DRTYPE_CUBETEXTURE,
            resource::D3DRTYPE_VOLUMETEXTURE,
        ],
    ) {
        return bridge_error(error);
    }
    bridge_ok(device.state.set_texture(stage, id))
}

pub fn d3d9_get_texture(device: &D3d9Device, stage: u32) -> ValueResult {
    match device.state.texture(stage) {
        Ok(value) => ok_value(value),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_stream_source(
    device: &mut D3d9Device,
    stream: u32,
    buffer: u32,
    offset: u32,
    stride: u32,
) -> Status {
    if let Err(error) = validate_binding(device, buffer, &[resource::D3DRTYPE_VERTEXBUFFER]) {
        return bridge_error(error);
    }
    bridge_ok(device.state.set_stream_source(
        stream,
        state::StreamSource {
            buffer,
            offset,
            stride,
        },
    ))
}

pub fn d3d9_get_stream_source(device: &D3d9Device, stream: u32) -> D3d9StreamResult {
    match device.state.stream_source(stream) {
        Ok(source) => D3d9StreamResult {
            status: status(StatusCode::Ok, ""),
            buffer: source.buffer,
            offset: source.offset,
            stride: source.stride,
        },
        Err(error) => D3d9StreamResult {
            status: bridge_error(error),
            buffer: 0,
            offset: 0,
            stride: 0,
        },
    }
}

pub fn d3d9_set_stream_freq(device: &mut D3d9Device, stream: u32, divider: u32) -> Status {
    bridge_ok(device.state.set_stream_freq(stream, divider))
}

pub fn d3d9_get_stream_freq(device: &D3d9Device, stream: u32) -> ValueResult {
    match device.state.stream_freq(stream) {
        Ok(value) => ok_value(value),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_indices(device: &mut D3d9Device, id: u32) -> Status {
    if let Err(error) = validate_binding(device, id, &[resource::D3DRTYPE_INDEXBUFFER]) {
        return bridge_error(error);
    }
    device.state.set_indices(id);
    status(StatusCode::Ok, "")
}

pub fn d3d9_get_indices(device: &D3d9Device) -> ValueResult {
    ok_value(device.state.bindings().indices)
}

pub fn d3d9_create_fvf_declaration(device: &mut D3d9Device, fvf: u32) -> ValueResult {
    let elements = match shader::fvf_declaration(fvf) {
        Ok(elements) => elements,
        Err(error) => return err_value(error),
    };
    match device.objects.add_fvf_declaration(fvf, &elements) {
        Ok(handle) => ok_value(handle),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_fvf_binding(device: &mut D3d9Device, handle: u32, fvf: u32) -> Status {
    device.state.set_declaration(handle, fvf);
    status(StatusCode::Ok, "")
}

pub fn d3d9_get_fvf(device: &D3d9Device) -> ValueResult {
    ok_value(device.state.fvf())
}

pub fn d3d9_create_vertex_declaration(
    device: &mut D3d9Device,
    elements: &[D3d9VertexElement],
) -> ValueResult {
    let mapped: Vec<shader::VertexElement> = elements
        .iter()
        .map(|element| shader::VertexElement {
            stream: element.stream,
            offset: element.offset,
            element_type: element.element_type,
            method: element.method,
            usage: element.usage,
            usage_index: element.usage_index,
        })
        .collect();
    match device.objects.add_declaration(&mapped) {
        Ok(handle) => ok_value(handle),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_destroy_declaration(device: &mut D3d9Device, handle: u32) -> Status {
    bridge_ok(device.objects.destroy_declaration(handle))
}

pub fn d3d9_declaration_elements(device: &D3d9Device, handle: u32) -> D3d9DeclarationResult {
    match device.objects.declaration(handle) {
        Ok(elements) => D3d9DeclarationResult {
            status: status(StatusCode::Ok, ""),
            elements: elements
                .iter()
                .map(|element| D3d9VertexElement {
                    stream: element.stream,
                    offset: element.offset,
                    element_type: element.element_type,
                    method: element.method,
                    usage: element.usage,
                    usage_index: element.usage_index,
                })
                .collect(),
        },
        Err(error) => D3d9DeclarationResult {
            status: bridge_error(error),
            elements: Vec::new(),
        },
    }
}

pub fn d3d9_set_vertex_declaration(device: &mut D3d9Device, handle: u32) -> Status {
    if handle != 0 && device.objects.declaration(handle).is_err() {
        return bridge_error(RenderError::invalid(
            "SetVertexDeclaration",
            "invalid vertex declaration handle",
        ));
    }
    device.state.set_declaration(handle, 0);
    status(StatusCode::Ok, "")
}

pub fn d3d9_get_vertex_declaration(device: &D3d9Device) -> ValueResult {
    ok_value(device.state.declaration())
}

pub fn d3d9_create_shader(device: &mut D3d9Device, pixel: bool, code: &[u32]) -> ValueResult {
    match device.objects.add_shader(pixel, code) {
        Ok(handle) => ok_value(handle),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_destroy_shader(device: &mut D3d9Device, pixel: bool, handle: u32) -> Status {
    let status_value = bridge_ok(device.objects.destroy_shader(pixel, handle));
    if status_value.code == StatusCode::Ok && device.state.shader(pixel) == handle {
        device.state.set_shader(pixel, 0);
    }
    status_value
}

pub fn d3d9_set_shader(device: &mut D3d9Device, pixel: bool, handle: u32) -> Status {
    if handle != 0 && device.objects.shader(pixel, handle).is_err() {
        return bridge_error(RenderError::invalid(
            "SetShader",
            "invalid shader handle for the shader stage",
        ));
    }
    device.state.set_shader(pixel, handle);
    status(StatusCode::Ok, "")
}

pub fn d3d9_get_shader(device: &D3d9Device, pixel: bool) -> ValueResult {
    ok_value(device.state.shader(pixel))
}

pub fn d3d9_shader_function(device: &D3d9Device, pixel: bool, handle: u32) -> ValuesResult {
    match device.objects.shader(pixel, handle) {
        Ok(record) => ok_values(record.code.clone()),
        Err(error) => err_values(error),
    }
}

pub fn d3d9_shader_size(device: &D3d9Device, pixel: bool, handle: u32) -> ValueResult {
    match device.objects.shader(pixel, handle) {
        Ok(record) => ok_value(record.code.len() as u32 * 4),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_set_shader_constants_f(
    device: &mut D3d9Device,
    pixel: bool,
    start: u32,
    values: &[f32],
) -> Status {
    bridge_ok(device.state.set_constants_f(pixel, start, values))
}

pub fn d3d9_get_shader_constants_f(
    device: &D3d9Device,
    pixel: bool,
    start: u32,
    count: u32,
) -> FloatValuesResult {
    match device.state.constants_f(pixel, start, count) {
        Ok(values) => ok_floats(values),
        Err(error) => err_floats(error),
    }
}

pub fn d3d9_set_shader_constants_i(
    device: &mut D3d9Device,
    pixel: bool,
    start: u32,
    values: &[i32],
) -> Status {
    bridge_ok(device.state.set_constants_i(pixel, start, values))
}

pub fn d3d9_get_shader_constants_i(
    device: &D3d9Device,
    pixel: bool,
    start: u32,
    count: u32,
) -> ValuesResult {
    match device.state.constants_i(pixel, start, count) {
        Ok(values) => ok_values(values.into_iter().map(|value| value as u32).collect()),
        Err(error) => err_values(error),
    }
}

pub fn d3d9_set_shader_constants_b(
    device: &mut D3d9Device,
    pixel: bool,
    start: u32,
    values: &[u32],
) -> Status {
    bridge_ok(device.state.set_constants_b(pixel, start, values))
}

pub fn d3d9_get_shader_constants_b(
    device: &D3d9Device,
    pixel: bool,
    start: u32,
    count: u32,
) -> ValuesResult {
    match device.state.constants_b(pixel, start, count) {
        Ok(values) => ok_values(values),
        Err(error) => err_values(error),
    }
}

pub fn d3d9_create_state_block(device: &mut D3d9Device, block_type: u32) -> ValueResult {
    match device.state.create_block(block_type) {
        Ok(handle) => ok_value(handle),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_begin_state_block(device: &mut D3d9Device) -> Status {
    bridge_ok(device.state.begin_block())
}

pub fn d3d9_end_state_block(device: &mut D3d9Device) -> ValueResult {
    match device.state.end_block() {
        Ok(handle) => ok_value(handle),
        Err(error) => err_value(error),
    }
}

pub fn d3d9_capture_state_block(device: &mut D3d9Device, handle: u32) -> Status {
    bridge_ok(device.state.capture_block(handle))
}

pub fn d3d9_apply_state_block(device: &mut D3d9Device, handle: u32) -> Status {
    bridge_ok(device.state.apply_block(handle))
}

pub fn d3d9_destroy_state_block(device: &mut D3d9Device, handle: u32) -> Status {
    bridge_ok(device.state.destroy_block(handle))
}

pub fn d3d9_state_block_bindings(device: &D3d9Device, handle: u32) -> D3d9BlockBindingsResult {
    match device.state.block_bindings(handle) {
        Ok(bindings) => D3d9BlockBindingsResult {
            status: status(StatusCode::Ok, ""),
            bindings: bridge_bindings(bindings),
        },
        Err(error) => D3d9BlockBindingsResult {
            status: bridge_error(error),
            bindings: empty_bridge_bindings(),
        },
    }
}

pub fn d3d9_current_bindings(device: &D3d9Device) -> D3d9BindingsResult {
    let (recording, recording_active) = device.state.recording_bindings();
    D3d9BindingsResult {
        status: status(StatusCode::Ok, ""),
        live: bridge_bindings(device.state.bindings()),
        recording: bridge_bindings(recording),
        recording_active,
    }
}
