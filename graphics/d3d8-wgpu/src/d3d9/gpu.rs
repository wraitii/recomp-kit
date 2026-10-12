use super::format;
use crate::RenderError;
use crate::backend::GpuContext;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub const COPY_ALIGN: u32 = wgpu::COPY_BYTES_PER_ROW_ALIGNMENT;

fn align_up(value: u32, align: u32) -> u32 {
    value.div_ceil(align) * align
}

#[derive(Clone, Default)]
pub struct D3d9Loss {
    state: Arc<LossState>,
}

#[derive(Default)]
struct LossState {
    physical: AtomicBool,
    reset_required: AtomicBool,
    reason: Mutex<Option<String>>,
}

impl D3d9Loss {
    pub fn mark_physical(&self, reason: String) {
        if let Ok(mut slot) = self.state.reason.lock() {
            *slot = Some(reason);
        }
        self.state.physical.store(true, Ordering::Release);
    }

    pub fn is_physical(&self) -> bool {
        self.state.physical.load(Ordering::Acquire)
    }

    pub fn mark_reset_required(&self) {
        self.state.reset_required.store(true, Ordering::Release);
    }

    pub fn is_reset_required(&self) -> bool {
        self.state.reset_required.load(Ordering::Acquire)
    }

    pub fn is_blocked(&self) -> bool {
        self.is_physical() || self.is_reset_required()
    }

    pub fn clear_reset_required(&self) {
        self.state.reset_required.store(false, Ordering::Release);
    }

    pub fn reason(&self) -> Option<String> {
        self.state.reason.lock().ok().and_then(|slot| slot.clone())
    }

    pub fn physical_message(&self) -> String {
        let reason = self
            .reason()
            .unwrap_or_else(|| "reason unknown".to_string());
        format!("the physical wgpu device was lost ({reason})")
    }
}

pub struct D3d9Gpu {
    pub device: wgpu::Device,
    pub queue: wgpu::Queue,
    loss: D3d9Loss,
    mipgen: Arc<Mutex<MipGenState>>,
    depth_upload: Arc<Mutex<Option<DepthUpload>>>,
}

impl Clone for D3d9Gpu {
    fn clone(&self) -> Self {
        Self {
            device: self.device.clone(),
            queue: self.queue.clone(),
            loss: self.loss.clone(),
            mipgen: Arc::clone(&self.mipgen),
            depth_upload: Arc::clone(&self.depth_upload),
        }
    }
}

impl D3d9Gpu {
    pub fn from_context(gpu: &GpuContext) -> Self {
        Self::from_context_with_loss(gpu, D3d9Loss::default())
    }

    pub fn from_context_with_loss(gpu: &GpuContext, loss: D3d9Loss) -> Self {
        Self {
            device: gpu.device.clone(),
            queue: gpu.queue.clone(),
            loss,
            mipgen: Arc::new(Mutex::new(MipGenState::default())),
            depth_upload: Arc::new(Mutex::new(None)),
        }
    }

    pub fn loss(&self) -> &D3d9Loss {
        &self.loss
    }

    pub fn is_blocked(&self) -> bool {
        self.loss.is_blocked()
    }

    pub fn is_physical(&self) -> bool {
        self.loss.is_physical()
    }

    pub fn flush(&self) -> Result<(), RenderError> {
        self.validate("RestoreFlush", || {
            self.queue.submit(std::iter::empty());
            self.device
                .poll(wgpu::PollType::Wait {
                    submission_index: None,
                    timeout: None,
                })
                .map_err(|error| RenderError::new("RestoreFlush", error.to_string()))?;
            Ok(())
        })
    }

    pub fn features(&self) -> wgpu::Features {
        self.device.features()
    }

    pub fn limits(&self) -> wgpu::Limits {
        self.device.limits()
    }

    pub fn validate<T>(
        &self,
        operation: &'static str,
        f: impl FnOnce() -> Result<T, RenderError>,
    ) -> Result<T, RenderError> {
        if self.is_physical() {
            return Err(RenderError::new(operation, self.loss.physical_message()));
        }
        self.device.push_error_scope(wgpu::ErrorFilter::Validation);
        self.device.push_error_scope(wgpu::ErrorFilter::OutOfMemory);
        let value = f();
        let oom = pollster::block_on(self.device.pop_error_scope());
        let validation = pollster::block_on(self.device.pop_error_scope());
        if let Some(error) = oom {
            return Err(RenderError::out_of_memory(operation, error.to_string()));
        }
        if let Some(error) = validation {
            return Err(RenderError::new(operation, error.to_string()));
        }
        value
    }

    pub fn create_buffer(
        &self,
        size: u64,
        usage: wgpu::BufferUsages,
    ) -> Result<wgpu::Buffer, RenderError> {
        if size == 0 {
            return Err(RenderError::invalid("CreateBuffer", "zero buffer size"));
        }
        self.validate("CreateBuffer", || {
            Ok(self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("d3d9-buffer"),
                size,
                usage,
                mapped_at_creation: false,
            }))
        })
    }

    pub fn create_texture(
        &self,
        width: u32,
        height: u32,
        depth: u32,
        levels: u32,
        format: wgpu::TextureFormat,
        dimension: wgpu::TextureDimension,
        usage: wgpu::TextureUsages,
    ) -> Result<wgpu::Texture, RenderError> {
        self.validate("CreateTexture", || {
            Ok(self.device.create_texture(&wgpu::TextureDescriptor {
                label: Some("d3d9-texture"),
                size: wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: depth,
                },
                mip_level_count: levels,
                sample_count: 1,
                dimension,
                format,
                usage,
                view_formats: &[],
            }))
        })
    }

    pub fn write_texture(
        &self,
        texture: &wgpu::Texture,
        d3d_format: u32,
        level: u32,
        width: u32,
        height: u32,
        depth: u32,
        layer: u32,
        is_3d: bool,
        bytes: &[u8],
    ) -> Result<(), RenderError> {
        let (pitch, slice_size) = format::physical_layout(d3d_format, width, height)
            .ok_or_else(|| RenderError::invalid("UploadTexture", "invalid texture layout"))?;
        let (copy_w, copy_h, copy_rows) = format::physical_copy_extent(d3d_format, width, height);
        let slices = if is_3d { depth.max(1) } else { 1 };
        let required = (slice_size as usize)
            .checked_mul(slices as usize)
            .ok_or_else(|| RenderError::invalid("UploadTexture", "texture byte size overflow"))?;
        if bytes.len() < required {
            return Err(RenderError::invalid(
                "UploadTexture",
                "converted level is shorter than its physical layout",
            ));
        }
        self.validate("UploadTexture", || {
            for slice in 0..slices {
                let start = slice as usize * slice_size as usize;
                let chunk = &bytes[start..start + slice_size as usize];
                let z = if is_3d { slice } else { layer };
                let aspect = if format::is_depth(d3d_format) {
                    wgpu::TextureAspect::DepthOnly
                } else {
                    wgpu::TextureAspect::All
                };
                self.queue.write_texture(
                    wgpu::TexelCopyTextureInfo {
                        texture,
                        mip_level: level,
                        origin: wgpu::Origin3d { x: 0, y: 0, z },
                        aspect,
                    },
                    chunk,
                    wgpu::TexelCopyBufferLayout {
                        offset: 0,
                        bytes_per_row: Some(pitch),
                        rows_per_image: Some(copy_rows),
                    },
                    wgpu::Extent3d {
                        width: copy_w,
                        height: copy_h,
                        depth_or_array_layers: 1,
                    },
                );
            }
            Ok(())
        })
    }

    pub fn write_depth32(
        &self,
        texture: &wgpu::Texture,
        width: u32,
        height: u32,
        bytes: &[f32],
    ) -> Result<(), RenderError> {
        if bytes.len() < (width as usize) * (height as usize) {
            return Err(RenderError::invalid(
                "UploadTexture",
                "depth upload is shorter than its layout",
            ));
        }
        let mut state = self
            .depth_upload
            .lock()
            .expect("depth-upload mutex poisoned");
        if state.is_none() {
            *state = Some(self.depth_upload_pipeline()?);
        }
        let upload = state.as_ref().expect("depth-upload pipeline present");
        let pipeline = upload.pipeline.clone();
        let layout = upload.layout.clone();
        drop(state);

        let source = self.validate("UploadTexture", || {
            Ok(self.device.create_texture(&wgpu::TextureDescriptor {
                label: Some("d3d9-depth-source"),
                size: wgpu::Extent3d {
                    width,
                    height,
                    depth_or_array_layers: 1,
                },
                mip_level_count: 1,
                sample_count: 1,
                dimension: wgpu::TextureDimension::D2,
                format: wgpu::TextureFormat::R32Float,
                usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
                view_formats: &[],
            }))
        })?;
        let mut raw = Vec::new();
        raw.try_reserve_exact(bytes.len() * 4).map_err(|_| {
            RenderError::out_of_memory("UploadTexture", "depth upload staging failed")
        })?;
        for value in bytes {
            raw.extend_from_slice(&value.to_le_bytes());
        }
        self.validate("UploadTexture", || {
            self.queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &source,
                    mip_level: 0,
                    origin: wgpu::Origin3d::ZERO,
                    aspect: wgpu::TextureAspect::All,
                },
                &raw,
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
            Ok(())
        })?;
        let (source_view, dest_view, bind) = self.validate("UploadTexture", || {
            let source_view = source.create_view(&wgpu::TextureViewDescriptor::default());
            let dest_view = texture.create_view(&wgpu::TextureViewDescriptor {
                label: Some("d3d9-depth-dest"),
                format: None,
                dimension: Some(wgpu::TextureViewDimension::D2),
                aspect: wgpu::TextureAspect::DepthOnly,
                base_mip_level: 0,
                mip_level_count: Some(1),
                base_array_layer: 0,
                array_layer_count: Some(1),
                usage: None,
            });
            let bind = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some("d3d9-depth-upload"),
                layout: &layout,
                entries: &[wgpu::BindGroupEntry {
                    binding: 0,
                    resource: wgpu::BindingResource::TextureView(&source_view),
                }],
            });
            Ok((source_view, dest_view, bind))
        })?;
        let _ = source_view;
        self.validate("UploadTexture", || {
            let mut encoder = self
                .device
                .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                    label: Some("d3d9-depth-upload"),
                });
            {
                let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                    label: Some("d3d9-depth-upload"),
                    color_attachments: &[],
                    depth_stencil_attachment: Some(wgpu::RenderPassDepthStencilAttachment {
                        view: &dest_view,
                        depth_ops: Some(wgpu::Operations {
                            load: wgpu::LoadOp::Clear(0.0),
                            store: wgpu::StoreOp::Store,
                        }),
                        stencil_ops: None,
                    }),
                    timestamp_writes: None,
                    occlusion_query_set: None,
                });
                pass.set_pipeline(&pipeline);
                pass.set_bind_group(0, &bind, &[]);
                pass.draw(0..3, 0..1);
            }
            self.queue.submit(std::iter::once(encoder.finish()));
            Ok(())
        })
    }

    fn depth_upload_pipeline(&self) -> Result<DepthUpload, RenderError> {
        self.validate("UploadTexture", || {
            let shader = self
                .device
                .create_shader_module(wgpu::ShaderModuleDescriptor {
                    label: Some("d3d9-depth-upload"),
                    source: wgpu::ShaderSource::Wgsl(DEPTH_UPLOAD_WGSL.into()),
                });
            let layout = self
                .device
                .create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                    label: Some("d3d9-depth-upload"),
                    entries: &[wgpu::BindGroupLayoutEntry {
                        binding: 0,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: false },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    }],
                });
            let pipeline_layout =
                self.device
                    .create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                        label: Some("d3d9-depth-upload"),
                        bind_group_layouts: &[&layout],
                        push_constant_ranges: &[],
                    });
            let pipeline = self
                .device
                .create_render_pipeline(&wgpu::RenderPipelineDescriptor {
                    label: Some("d3d9-depth-upload"),
                    layout: Some(&pipeline_layout),
                    vertex: wgpu::VertexState {
                        module: &shader,
                        entry_point: Some("vs_main"),
                        buffers: &[],
                        compilation_options: Default::default(),
                    },
                    fragment: Some(wgpu::FragmentState {
                        module: &shader,
                        entry_point: Some("fs_main"),
                        targets: &[],
                        compilation_options: Default::default(),
                    }),
                    primitive: wgpu::PrimitiveState::default(),
                    depth_stencil: Some(wgpu::DepthStencilState {
                        format: wgpu::TextureFormat::Depth32Float,
                        depth_write_enabled: true,
                        depth_compare: wgpu::CompareFunction::Always,
                        stencil: wgpu::StencilState::default(),
                        bias: wgpu::DepthBiasState::default(),
                    }),
                    multisample: wgpu::MultisampleState::default(),
                    multiview: None,
                    cache: None,
                });
            Ok(DepthUpload { pipeline, layout })
        })
    }

    fn map_buffer(&self, buffer: &wgpu::Buffer, size: u64) -> Result<(), RenderError> {
        if size == 0 {
            return Ok(());
        }
        if size % wgpu::MAP_ALIGNMENT != 0 {
            return Err(RenderError::invalid(
                "Readback",
                "map size is not alignment safe",
            ));
        }
        let slice = buffer.slice(0..size);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |result| {
            let _ = tx.send(result);
        });
        self.device
            .poll(wgpu::PollType::wait_indefinitely())
            .map_err(|e| RenderError::new("Readback::poll", e.to_string()))?;
        rx.recv()
            .map_err(|e| RenderError::new("Readback::map_callback", e.to_string()))?
            .map_err(|e| RenderError::new("Readback::map_async", e.to_string()))
    }

    pub fn read_texture(
        &self,
        texture: &wgpu::Texture,
        d3d_format: u32,
        level: u32,
        width: u32,
        height: u32,
        depth: u32,
        layer: u32,
        is_3d: bool,
        out: &mut [u8],
    ) -> Result<(), RenderError> {
        let (pitch, slice_size) = format::physical_layout(d3d_format, width, height)
            .ok_or_else(|| RenderError::invalid("ReadbackTexture", "invalid texture layout"))?;
        let (_, _, copy_rows) = format::physical_copy_extent(d3d_format, width, height);
        let (copy_w, copy_h, _) = format::physical_copy_extent(d3d_format, width, height);
        let slices = if is_3d { depth.max(1) } else { 1 };
        let required = slice_size as usize * slices as usize;
        if out.len() < required {
            return Err(RenderError::invalid(
                "ReadbackTexture",
                "destination is shorter than the logical level",
            ));
        }
        let padded = align_up(pitch, COPY_ALIGN);
        let buffer_bytes = padded as u64 * copy_rows as u64;
        if format::is_depth(d3d_format) && !format::depth_readback_supported(d3d_format) {
            return Err(RenderError::new(
                "ReadbackTexture",
                "non-lockable depth formats cannot be read back",
            ));
        }
        let aspect = if format::is_depth(d3d_format) {
            wgpu::TextureAspect::DepthOnly
        } else {
            wgpu::TextureAspect::All
        };
        for slice in 0..slices {
            let z = if is_3d { slice } else { layer };
            self.validate("ReadbackTexture", || {
                let buffer = self.device.create_buffer(&wgpu::BufferDescriptor {
                    label: Some("d3d9-readback"),
                    size: buffer_bytes,
                    usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
                    mapped_at_creation: false,
                });
                let mut encoder =
                    self.device
                        .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                            label: Some("d3d9-readback"),
                        });
                encoder.copy_texture_to_buffer(
                    wgpu::TexelCopyTextureInfo {
                        texture,
                        mip_level: level,
                        origin: wgpu::Origin3d { x: 0, y: 0, z },
                        aspect,
                    },
                    wgpu::TexelCopyBufferInfo {
                        buffer: &buffer,
                        layout: wgpu::TexelCopyBufferLayout {
                            offset: 0,
                            bytes_per_row: Some(padded),
                            rows_per_image: Some(copy_rows),
                        },
                    },
                    wgpu::Extent3d {
                        width: copy_w,
                        height: copy_h,
                        depth_or_array_layers: 1,
                    },
                );
                self.queue.submit(std::iter::once(encoder.finish()));
                self.map_buffer(&buffer, buffer_bytes)?;
                {
                    let mapped = buffer.slice(0..buffer_bytes).get_mapped_range();
                    let base = slice as usize * slice_size as usize;
                    for row in 0..copy_rows as usize {
                        let src = row * padded as usize;
                        let dst = base + row * pitch as usize;
                        let row_len = pitch as usize;
                        if src + row_len > mapped.len() || dst + row_len > out.len() {
                            return Err(RenderError::invalid(
                                "ReadbackTexture",
                                "readback row lies outside the destination",
                            ));
                        }
                        out[dst..dst + row_len].copy_from_slice(&mapped[src..src + row_len]);
                    }
                }
                buffer.unmap();
                Ok(())
            })?;
        }
        Ok(())
    }

    pub fn write_buffer_padded(
        &self,
        buffer: &wgpu::Buffer,
        logical: &[u8],
        lo: u32,
        hi: u32,
    ) -> Result<(), RenderError> {
        if logical.is_empty() || hi <= lo {
            return Ok(());
        }
        let padded_len =
            (logical.len() as u32).next_multiple_of(wgpu::COPY_BUFFER_ALIGNMENT as u32);
        let align_lo = lo - (lo % wgpu::COPY_BUFFER_ALIGNMENT as u32);
        let align_hi = hi
            .min(padded_len)
            .next_multiple_of(wgpu::COPY_BUFFER_ALIGNMENT as u32);
        if align_hi <= align_lo {
            return Ok(());
        }
        let size = (align_hi - align_lo) as usize;
        let mut staged = Vec::new();
        staged.try_reserve_exact(size).map_err(|_| {
            RenderError::out_of_memory("UploadBuffer", "buffer staging allocation failed")
        })?;
        staged.resize(size, 0);
        let copy_lo = align_lo as usize;
        let copy_hi = (align_hi as usize).min(logical.len());
        if copy_hi > copy_lo {
            staged[..copy_hi - copy_lo].copy_from_slice(&logical[copy_lo..copy_hi]);
        }
        self.validate("UploadBuffer", || {
            self.queue.write_buffer(buffer, align_lo as u64, &staged);
            Ok(())
        })
    }

    pub fn read_buffer(
        &self,
        buffer: &wgpu::Buffer,
        logical_len: u32,
        out: &mut [u8],
    ) -> Result<(), RenderError> {
        if logical_len == 0 {
            return Ok(());
        }
        let padded = logical_len
            .next_multiple_of(wgpu::COPY_BUFFER_ALIGNMENT as u32)
            .next_multiple_of(8);
        if out.len() < logical_len as usize {
            return Err(RenderError::invalid(
                "ReadbackBuffer",
                "destination is smaller than the buffer",
            ));
        }
        let mut staged = Vec::new();
        staged
            .try_reserve_exact(logical_len as usize)
            .map_err(|_| {
                RenderError::out_of_memory("ReadbackBuffer", "readback allocation failed")
            })?;
        staged.resize(logical_len as usize, 0);
        self.validate("ReadbackBuffer", || {
            let staging = self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("d3d9-readback"),
                size: padded as u64,
                usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
                mapped_at_creation: false,
            });
            let mut encoder = self
                .device
                .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                    label: Some("d3d9-readback"),
                });
            encoder.copy_buffer_to_buffer(buffer, 0, &staging, 0, padded as u64);
            self.queue.submit(std::iter::once(encoder.finish()));
            self.map_buffer(&staging, padded as u64)?;
            {
                let mapped = staging.slice(0..padded as u64).get_mapped_range();
                staged.copy_from_slice(&mapped[..logical_len as usize]);
            }
            staging.unmap();
            Ok(())
        })?;
        out[..logical_len as usize].copy_from_slice(&staged);
        Ok(())
    }

    pub fn generate_mips(
        &self,
        texture: &wgpu::Texture,
        format: wgpu::TextureFormat,
        levels: u32,
        base_width: u32,
        base_height: u32,
        layers: u32,
        linear: bool,
    ) -> Result<(), RenderError> {
        if levels < 2 || layers == 0 {
            return Ok(());
        }
        let mut state = self.mipgen.lock().expect("mipgen mutex poisoned");
        self.validate("GenerateMipSubLevels", || {
            state.ensure(&self.device, format, linear)
        })?;
        let cached = state.cached.as_ref().expect("mipgen cache present");
        let pipeline = cached.pipeline.clone();
        let layout = cached.layout.clone();
        let sampler = cached.sampler.clone();
        drop(state);

        self.validate("GenerateMipSubLevels", || {
            for layer in 0..layers {
                for level in 1..levels {
                    let view = |mip: u32| {
                        texture.create_view(&wgpu::TextureViewDescriptor {
                            label: Some("d3d9-mipgen"),
                            format: Some(format),
                            dimension: Some(wgpu::TextureViewDimension::D2),
                            aspect: wgpu::TextureAspect::All,
                            base_mip_level: mip,
                            mip_level_count: Some(1),
                            base_array_layer: layer,
                            array_layer_count: Some(1),
                            usage: None,
                        })
                    };
                    let src = view(level - 1);
                    let dst = view(level);
                    let bind = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
                        label: Some("d3d9-mipgen"),
                        layout: &layout,
                        entries: &[
                            wgpu::BindGroupEntry {
                                binding: 0,
                                resource: wgpu::BindingResource::TextureView(&src),
                            },
                            wgpu::BindGroupEntry {
                                binding: 1,
                                resource: wgpu::BindingResource::Sampler(&sampler),
                            },
                        ],
                    });
                    let mut encoder =
                        self.device
                            .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                                label: Some("d3d9-mipgen"),
                            });
                    {
                        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                            label: Some("d3d9-mipgen"),
                            color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                                view: &dst,
                                resolve_target: None,
                                ops: wgpu::Operations {
                                    load: wgpu::LoadOp::Clear(wgpu::Color::TRANSPARENT),
                                    store: wgpu::StoreOp::Store,
                                },
                                depth_slice: None,
                            })],
                            depth_stencil_attachment: None,
                            timestamp_writes: None,
                            occlusion_query_set: None,
                        });
                        pass.set_pipeline(&pipeline);
                        pass.set_bind_group(0, &bind, &[]);
                        pass.draw(0..3, 0..1);
                    }
                    self.queue.submit(std::iter::once(encoder.finish()));
                }
            }
            let _ = (base_width, base_height);
            Ok(())
        })
    }
}

struct DepthUpload {
    pipeline: wgpu::RenderPipeline,
    layout: wgpu::BindGroupLayout,
}

#[derive(Default)]
struct MipGenState {
    cached: Option<MipGen>,
}

struct MipGen {
    pipeline: wgpu::RenderPipeline,
    layout: wgpu::BindGroupLayout,
    sampler: wgpu::Sampler,
    format: wgpu::TextureFormat,
    linear: bool,
}

impl MipGenState {
    fn ensure(
        &mut self,
        device: &wgpu::Device,
        format: wgpu::TextureFormat,
        linear: bool,
    ) -> Result<(), RenderError> {
        if matches!(&self.cached, Some(c) if c.format == format && c.linear == linear) {
            return Ok(());
        }
        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("d3d9-mipgen"),
            source: wgpu::ShaderSource::Wgsl(MIPGEN_WGSL.into()),
        });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("d3d9-mipgen"),
            entries: &[
                wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: linear },
                        view_dimension: wgpu::TextureViewDimension::D2,
                        multisampled: false,
                    },
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 1,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(if linear {
                        wgpu::SamplerBindingType::Filtering
                    } else {
                        wgpu::SamplerBindingType::NonFiltering
                    }),
                    count: None,
                },
            ],
        });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("d3d9-mipgen"),
            bind_group_layouts: &[&layout],
            push_constant_ranges: &[],
        });
        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("d3d9-mipgen"),
            layout: Some(&pipeline_layout),
            vertex: wgpu::VertexState {
                module: &shader,
                entry_point: Some("vs_main"),
                buffers: &[],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &shader,
                entry_point: Some("fs_main"),
                targets: &[Some(wgpu::ColorTargetState {
                    format,
                    blend: None,
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });
        let filter = if linear {
            wgpu::FilterMode::Linear
        } else {
            wgpu::FilterMode::Nearest
        };
        let sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            label: Some("d3d9-mipgen"),
            mag_filter: filter,
            min_filter: filter,
            mipmap_filter: wgpu::FilterMode::Nearest,
            ..Default::default()
        });
        self.cached = Some(MipGen {
            pipeline,
            layout,
            sampler,
            format,
            linear,
        });
        Ok(())
    }
}

const MIPGEN_WGSL: &str = r#"
@group(0) @binding(0) var src_tex: texture_2d<f32>;
@group(0) @binding(1) var src_samp: sampler;

struct VsOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vs_main(@builtin(vertex_index) idx: u32) -> VsOut {
    var out: VsOut;
    var pos = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0),
        vec2<f32>( 3.0, -1.0),
        vec2<f32>(-1.0,  3.0),
    );
    out.pos = vec4<f32>(pos[idx], 0.0, 1.0);
    out.uv = vec2<f32>((pos[idx].x + 1.0) * 0.5, (1.0 - pos[idx].y) * 0.5);
    return out;
}

@fragment
fn fs_main(in: VsOut) -> @location(0) vec4<f32> {
    return textureSampleLevel(src_tex, src_samp, in.uv, 0.0);
}
"#;

const DEPTH_UPLOAD_WGSL: &str = r#"
@group(0) @binding(0) var src_tex: texture_2d<f32>;

@vertex
fn vs_main(@builtin(vertex_index) idx: u32) -> @builtin(position) vec4<f32> {
    var pos = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -1.0),
        vec2<f32>( 3.0, -1.0),
        vec2<f32>(-1.0,  3.0),
    );
    return vec4<f32>(pos[idx], 0.0, 1.0);
}

@fragment
fn fs_main(@builtin(position) frag: vec4<f32>) -> @builtin(frag_depth) f32 {
    return textureLoad(src_tex, vec2<i32>(i32(frag.x), i32(frag.y)), 0).r;
}
"#;
