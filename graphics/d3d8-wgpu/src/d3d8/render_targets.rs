//! GPU-authoritative color surfaces. Rendered levels are lifetime-owned, not
//! evictable CPU uploads. A padded color attachment allows D3D8's larger shared
//! depth surface without resizing, discarding, or copying its depth contents.
use super::*;
use std::collections::HashMap;

pub(super) struct RenderTexture {
    pub surface: OffscreenTarget,
    pub generation: u64,
    pub padded: Option<OffscreenTarget>,
}

pub(super) struct Targets {
    pub backbuffer: OffscreenTarget,
    pub textures: HashMap<TextureKey, RenderTexture>,
    pub current: Option<TextureKey>,
}

impl Targets {
    pub fn new(backbuffer: OffscreenTarget) -> Self {
        Self {
            backbuffer,
            textures: HashMap::new(),
            current: None,
        }
    }
}

impl Device {
    /// Bind a color level (id zero selects the implicit backbuffer), optionally
    /// with the original autodepth surface. Guest identity validation lives in
    /// COM. All validation/allocation precedes committing the binding.
    #[allow(clippy::too_many_arguments)]
    pub fn set_render_target(
        &mut self,
        id: u32,
        level: u32,
        generation: u64,
        format: u32,
        width: u32,
        height: u32,
        data: &[u8],
        depth: bool,
        change_color: bool,
    ) -> Result<(), RenderError> {
        // Recorded draws target the previous binding.
        self.flush_draws();
        let key = if id == 0 {
            None
        } else {
            Some(TextureKey::new(id, level))
        };
        let (width, height) = if key.is_none() {
            (
                self.targets.backbuffer.width,
                self.targets.backbuffer.height,
            )
        } else {
            (width, height)
        };
        if depth {
            let b = &self.targets.backbuffer;
            if b.depth.is_none() || width > b.width || height > b.height {
                return Err(RenderError::new(
                    "SetRenderTarget",
                    "autodepth absent or smaller than color target",
                ));
            }
        }
        if let Some(key) = key {
            if level != 0 {
                return Err(RenderError::new(
                    "SetRenderTarget",
                    "only level-0 color targets are implemented",
                ));
            }
            // Keep the accepted set equal to the CPU/GPU conversion table:
            // 32-bit ARGB plus the packed 16-bit color formats. create_target,
            // upload and readback all use the same table.
            crate::d3d8::format::ColorFormat::from_d3dformat(format)?;
            if !self.targets.textures.contains_key(&key) {
                let surface = self.gpu.create_target(width, height, format, 0)?;
                self.upload_target(&surface, format, data)?;
                self.targets.textures.insert(
                    key,
                    RenderTexture {
                        surface,
                        generation,
                        padded: None,
                    },
                );
                // Promotion retires the evictable upload, whose CPU bytes may
                // become stale as soon as the first render pass writes here.
                self.texture_cache.remove(key);
            } else {
                self.sync_target_cpu(key, generation, format, width, height, data, false)?;
            }
        }
        let mut target = match key {
            None => self.targets.backbuffer.clone(),
            Some(key) => self.targets.textures[&key].surface.clone(),
        };
        target.depth = if depth {
            self.targets.backbuffer.depth.clone()
        } else {
            None
        };
        if depth
            && (width != self.targets.backbuffer.width || height != self.targets.backbuffer.height)
        {
            let key = key.expect("backbuffer dimensions match autodepth");
            if self.targets.textures[&key].padded.is_none() {
                let padded = self.gpu.create_target(
                    self.targets.backbuffer.width,
                    self.targets.backbuffer.height,
                    format,
                    0,
                )?;
                self.targets.textures.get_mut(&key).unwrap().padded = Some(padded);
            }
            let mut padded = self.targets.textures[&key].padded.as_ref().unwrap().clone();
            self.copy_color(&target, &padded, width, height);
            padded.depth = target.depth;
            // Logical extent drives viewport validation/readback; the backing
            // texture retains its larger physical extent for wgpu attachments.
            padded.width = width;
            padded.height = height;
            target = padded;
        }
        self.target = target;
        self.targets.current = key;
        // Pipelines are keyed by the target's color/depth format and write mask,
        // so a target switch needs no cache invalidation.
        if change_color {
            self.state.viewport = crate::d3d8::state::Viewport::full(width, height);
        }
        // Already-bound stages must follow a promoted resource immediately.
        for slot in &mut self.stage_textures {
            if let Some(bound) = slot {
                if let Some(rt) = self
                    .targets
                    .textures
                    .get(&TextureKey::new(bound.texture_id, 0))
                {
                    bound._texture = rt.surface.texture.clone();
                    bound.view = rt.surface.view.clone();
                }
            }
        }
        Ok(())
    }

    fn copy_color(&self, src: &OffscreenTarget, dst: &OffscreenTarget, width: u32, height: u32) {
        let mut encoder = self
            .gpu
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                label: Some("D3D8 padded color copy"),
            });
        encoder.copy_texture_to_texture(
            wgpu::TexelCopyTextureInfo {
                texture: &src.texture,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::TexelCopyTextureInfo {
                texture: &dst.texture,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::Extent3d {
                width,
                height,
                depth_or_array_layers: 1,
            },
        );
        self.gpu.queue.submit([encoder.finish()]);
    }

    fn upload_target(
        &mut self,
        target: &OffscreenTarget,
        format: u32,
        data: &[u8],
    ) -> Result<(), RenderError> {
        let color = crate::d3d8::format::ColorFormat::from_d3dformat(format)?;
        let expected =
            target.width as usize * target.height as usize * color.bytes_per_pixel() as usize;
        if data.len() < expected {
            return Err(RenderError::new(
                "SetRenderTarget",
                "CPU level bytes are truncated",
            ));
        }
        color.to_rgba8_into(&data[..expected], &mut self.scratch_rgba);
        self.gpu.queue.write_texture(
            wgpu::TexelCopyTextureInfo {
                texture: &target.texture,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            &self.scratch_rgba,
            wgpu::TexelCopyBufferLayout {
                offset: 0,
                bytes_per_row: Some(target.width * 4),
                rows_per_image: Some(target.height),
            },
            wgpu::Extent3d {
                width: target.width,
                height: target.height,
                depth_or_array_layers: 1,
            },
        );
        Ok(())
    }

    pub(super) fn sync_target_cpu(
        &mut self,
        key: TextureKey,
        generation: u64,
        format: u32,
        width: u32,
        height: u32,
        data: &[u8],
        force: bool,
    ) -> Result<(), RenderError> {
        let rt = &self.targets.textures[&key];
        if rt.surface.width != width
            || rt.surface.height != height
            || rt.surface.d3d_format() != format
        {
            return Err(RenderError::new(
                "SetTexture",
                "rendered level descriptor changed",
            ));
        }
        if force {
            return Err(RenderError::new(
                "SetTexture",
                "sampling a locked render target is unsupported",
            ));
        }
        if rt.generation != generation {
            let surface = rt.surface.clone();
            // A CPU rewrite must follow queued draws sampling the old content.
            // Rebinding unchanged GPU content leaves their snapshots intact
            // and can stay in the same batch.
            self.flush_draws();
            self.upload_target(&surface, format, data)?;
            self.targets.textures.get_mut(&key).unwrap().generation = generation;
            if self.targets.current == Some(key)
                && (self.target.texture.width() != width || self.target.texture.height() != height)
            {
                self.copy_color(&surface, &self.target, width, height);
            }
        }
        Ok(())
    }

    /// Publish the logical color region after every write. Queue ordering makes
    /// sampling see the render result; the CPU generation is deliberately left
    /// unchanged, so a subsequent ordinary bind cannot re-upload stale bytes.
    pub(super) fn publish_target(&self) {
        if let Some(key) = self.targets.current {
            let surface = &self.targets.textures[&key].surface;
            if self.target.texture.width() != surface.width
                || self.target.texture.height() != surface.height
            {
                self.copy_color(&self.target, surface, surface.width, surface.height);
            }
        }
    }

    /// CPU writes after a render are newer than the resident GPU copy until
    /// the next bind uploads them. A lock must not overwrite those CPU bytes
    /// by reading back an older GPU generation.
    pub fn read_texture_if_current(
        &self,
        id: u32,
        level: u32,
        generation: u64,
    ) -> Result<Option<Vec<u8>>, RenderError> {
        let rt = self
            .targets
            .textures
            .get(&TextureKey::new(id, level))
            .ok_or_else(|| {
                RenderError::new("ReadTexture", "no GPU-rendered level for this identity")
            })?;
        if rt.generation != generation {
            return Ok(None);
        }
        self.read_texture(id, level).map(Some)
    }

    pub fn read_texture(&self, id: u32, level: u32) -> Result<Vec<u8>, RenderError> {
        self.flush_draws();
        let rt = self
            .targets
            .textures
            .get(&TextureKey::new(id, level))
            .ok_or_else(|| {
                RenderError::new("ReadTexture", "no GPU-rendered level for this identity")
            })?;
        let mut pixels = self.gpu.read_pixels(&rt.surface)?;
        // Re-encode wgpu's RGBA8 into this level's D3D8 format so a packed
        // 16-bit render target reads back at its true bytes per texel.
        let color = crate::d3d8::format::ColorFormat::from_d3dformat(rt.surface.d3d_format())?;
        let mut out = Vec::new();
        color.from_rgba8_into(&pixels, &mut out);
        pixels = out;
        Ok(pixels)
    }
}
