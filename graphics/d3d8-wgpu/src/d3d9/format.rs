use crate::RenderError;
use half::f16;

pub const D3DFMT_UNKNOWN: u32 = 0;
pub const D3DFMT_R8G8B8: u32 = 20;
pub const D3DFMT_A8R8G8B8: u32 = 21;
pub const D3DFMT_X8R8G8B8: u32 = 22;
pub const D3DFMT_R5G6B5: u32 = 23;
pub const D3DFMT_X1R5G5B5: u32 = 24;
pub const D3DFMT_A1R5G5B5: u32 = 25;
pub const D3DFMT_A4R4G4B4: u32 = 26;
pub const D3DFMT_R3G3B2: u32 = 27;
pub const D3DFMT_A8: u32 = 28;
pub const D3DFMT_A8R3G3B2: u32 = 29;
pub const D3DFMT_X4R4G4B4: u32 = 30;
pub const D3DFMT_A2B10G10R10: u32 = 31;
pub const D3DFMT_A8B8G8R8: u32 = 32;
pub const D3DFMT_X8B8G8R8: u32 = 33;
pub const D3DFMT_G16R16: u32 = 34;
pub const D3DFMT_A2R10G10B10: u32 = 35;
pub const D3DFMT_A16B16G16R16: u32 = 36;
pub const D3DFMT_L8: u32 = 50;
pub const D3DFMT_A8L8: u32 = 51;
pub const D3DFMT_A4L4: u32 = 52;
pub const D3DFMT_V8U8: u32 = 60;
pub const D3DFMT_Q8W8V8U8: u32 = 63;
pub const D3DFMT_V16U16: u32 = 64;
pub const D3DFMT_D16_LOCKABLE: u32 = 70;
pub const D3DFMT_D32: u32 = 71;
pub const D3DFMT_D24S8: u32 = 75;
pub const D3DFMT_D24X8: u32 = 77;
pub const D3DFMT_D16: u32 = 80;
pub const D3DFMT_D32F_LOCKABLE: u32 = 82;
pub const D3DFMT_VERTEXDATA: u32 = 100;
pub const D3DFMT_INDEX16: u32 = 101;
pub const D3DFMT_INDEX32: u32 = 102;
pub const D3DFMT_Q16W16V16U16: u32 = 110;
pub const D3DFMT_R16F: u32 = 111;
pub const D3DFMT_G16R16F: u32 = 112;
pub const D3DFMT_A16B16G16R16F: u32 = 113;
pub const D3DFMT_R32F: u32 = 114;
pub const D3DFMT_G32R32F: u32 = 115;
pub const D3DFMT_A32B32G32R32F: u32 = 116;
pub const D3DFMT_DXT1: u32 = 0x3154_5844;
pub const D3DFMT_DXT2: u32 = 0x3254_5844;
pub const D3DFMT_DXT3: u32 = 0x3354_5844;
pub const D3DFMT_DXT4: u32 = 0x3454_5844;
pub const D3DFMT_DXT5: u32 = 0x3554_5844;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageClass {
    Uncompressed,
    Block,
    Depth,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Conversion {
    Direct,
    ToRgba8,
    SwapRgb10,
    BgraOpaque,
    RgbaOpaque,
    DepthDirect,
}

#[derive(Clone, Copy, Debug)]
pub struct FormatInfo {
    pub storage: StorageClass,
    pub bpp: u32,
    pub block: u32,
    pub wgpu: wgpu::TextureFormat,
    pub conversion: Conversion,
    pub feature: Option<wgpu::Features>,
    pub renderable: bool,
    pub depth_lockable: bool,
}

pub fn info(fmt: u32) -> Option<FormatInfo> {
    use wgpu::TextureFormat as T;
    let f = |storage, bpp, block, wgpu, conversion, feature, renderable, depth_lockable| {
        Some(FormatInfo {
            storage,
            bpp,
            block,
            wgpu,
            conversion,
            feature,
            renderable,
            depth_lockable,
        })
    };
    match fmt {
        D3DFMT_R8G8B8 => f(
            StorageClass::Uncompressed,
            3,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A8R8G8B8 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Bgra8Unorm,
            Conversion::Direct,
            None,
            true,
            false,
        ),
        D3DFMT_X8R8G8B8 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Bgra8Unorm,
            Conversion::BgraOpaque,
            None,
            true,
            false,
        ),
        D3DFMT_R5G6B5 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_X1R5G5B5 | D3DFMT_A1R5G5B5 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A4R4G4B4 | D3DFMT_X4R4G4B4 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_R3G3B2 => f(
            StorageClass::Uncompressed,
            1,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A8 => f(
            StorageClass::Uncompressed,
            1,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A8R3G3B2 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A2B10G10R10 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rgb10a2Unorm,
            Conversion::Direct,
            None,
            true,
            false,
        ),
        D3DFMT_A2R10G10B10 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rgb10a2Unorm,
            Conversion::SwapRgb10,
            None,
            true,
            false,
        ),
        D3DFMT_A8B8G8R8 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rgba8Unorm,
            Conversion::Direct,
            None,
            true,
            false,
        ),
        D3DFMT_X8B8G8R8 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rgba8Unorm,
            Conversion::RgbaOpaque,
            None,
            true,
            false,
        ),
        D3DFMT_G16R16 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rg16Unorm,
            Conversion::Direct,
            Some(wgpu::Features::TEXTURE_FORMAT_16BIT_NORM),
            true,
            false,
        ),
        D3DFMT_A16B16G16R16 => f(
            StorageClass::Uncompressed,
            8,
            0,
            T::Rgba16Unorm,
            Conversion::Direct,
            Some(wgpu::Features::TEXTURE_FORMAT_16BIT_NORM),
            true,
            false,
        ),
        D3DFMT_L8 => f(
            StorageClass::Uncompressed,
            1,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A8L8 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_A4L4 => f(
            StorageClass::Uncompressed,
            1,
            0,
            T::Rgba8Unorm,
            Conversion::ToRgba8,
            None,
            true,
            false,
        ),
        D3DFMT_V8U8 => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::Rg8Snorm,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_Q8W8V8U8 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rgba8Snorm,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_V16U16 => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rg16Snorm,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_Q16W16V16U16 => f(
            StorageClass::Uncompressed,
            8,
            0,
            T::Rgba16Snorm,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_R16F => f(
            StorageClass::Uncompressed,
            2,
            0,
            T::R16Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_G16R16F => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::Rg16Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_A16B16G16R16F => f(
            StorageClass::Uncompressed,
            8,
            0,
            T::Rgba16Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_R32F => f(
            StorageClass::Uncompressed,
            4,
            0,
            T::R32Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_G32R32F => f(
            StorageClass::Uncompressed,
            8,
            0,
            T::Rg32Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_A32B32G32R32F => f(
            StorageClass::Uncompressed,
            16,
            0,
            T::Rgba32Float,
            Conversion::Direct,
            None,
            false,
            false,
        ),
        D3DFMT_DXT1 => f(
            StorageClass::Block,
            0,
            8,
            T::Bc1RgbaUnorm,
            Conversion::Direct,
            Some(wgpu::Features::TEXTURE_COMPRESSION_BC),
            false,
            false,
        ),
        D3DFMT_DXT2 | D3DFMT_DXT3 => f(
            StorageClass::Block,
            0,
            16,
            T::Bc2RgbaUnorm,
            Conversion::Direct,
            Some(wgpu::Features::TEXTURE_COMPRESSION_BC),
            false,
            false,
        ),
        D3DFMT_DXT4 | D3DFMT_DXT5 => f(
            StorageClass::Block,
            0,
            16,
            T::Bc3RgbaUnorm,
            Conversion::Direct,
            Some(wgpu::Features::TEXTURE_COMPRESSION_BC),
            false,
            false,
        ),
        D3DFMT_D16_LOCKABLE => f(
            StorageClass::Depth,
            2,
            0,
            T::Depth16Unorm,
            Conversion::DepthDirect,
            None,
            false,
            true,
        ),
        D3DFMT_D16 => f(
            StorageClass::Depth,
            2,
            0,
            T::Depth16Unorm,
            Conversion::DepthDirect,
            None,
            false,
            false,
        ),
        D3DFMT_D24S8 => f(
            StorageClass::Depth,
            4,
            0,
            T::Depth24PlusStencil8,
            Conversion::DepthDirect,
            None,
            false,
            false,
        ),
        D3DFMT_D24X8 => f(
            StorageClass::Depth,
            4,
            0,
            T::Depth24Plus,
            Conversion::DepthDirect,
            None,
            false,
            false,
        ),
        D3DFMT_D32F_LOCKABLE => f(
            StorageClass::Depth,
            4,
            0,
            T::Depth32Float,
            Conversion::DepthDirect,
            None,
            false,
            true,
        ),
        D3DFMT_D32 => None,
        _ => None,
    }
}

pub fn is_depth(fmt: u32) -> bool {
    matches!(
        info(fmt),
        Some(FormatInfo {
            storage: StorageClass::Depth,
            ..
        })
    )
}

pub fn depth_lockable(fmt: u32) -> bool {
    matches!(
        info(fmt),
        Some(FormatInfo {
            depth_lockable: true,
            ..
        })
    )
}

pub fn autogen_capable(fmt: u32) -> bool {
    match info(fmt) {
        Some(FormatInfo {
            storage: StorageClass::Uncompressed,
            renderable: true,
            conversion,
            ..
        }) => {
            matches!(
                conversion,
                Conversion::Direct
                    | Conversion::ToRgba8
                    | Conversion::BgraOpaque
                    | Conversion::RgbaOpaque
                    | Conversion::SwapRgb10
            )
        }
        _ => false,
    }
}

pub fn required_feature(fmt: u32) -> Option<wgpu::Features> {
    info(fmt).and_then(|f| f.feature)
}

pub fn bpp(fmt: u32) -> u32 {
    info(fmt).map_or(0, |f| f.bpp)
}

pub fn block_bytes(fmt: u32) -> u32 {
    info(fmt).map_or(0, |f| f.block)
}

pub fn is_block(fmt: u32) -> bool {
    block_bytes(fmt) != 0
}

pub fn is_supported(fmt: u32) -> bool {
    info(fmt).is_some()
}

pub fn physical_bpp(fmt: u32) -> u32 {
    match info(fmt) {
        Some(FormatInfo {
            storage: StorageClass::Block,
            ..
        }) => 0,
        Some(f) => match f.conversion {
            Conversion::Direct | Conversion::DepthDirect => f.bpp,
            _ => 4,
        },
        None => 0,
    }
}

pub fn physical_pitch(fmt: u32, width: u32) -> Option<u32> {
    if is_block(fmt) {
        return row_pitch(fmt, width);
    }
    width.checked_mul(physical_bpp(fmt))
}

pub fn physical_layout(fmt: u32, width: u32, height: u32) -> Option<(u32, u32)> {
    if is_block(fmt) {
        return Some((row_pitch(fmt, width)?, level_size(fmt, width, height)?));
    }
    let pitch = physical_pitch(fmt, width)?;
    Some((pitch, pitch.checked_mul(height)?))
}

pub fn physical_copy_extent(fmt: u32, width: u32, height: u32) -> (u32, u32, u32) {
    if is_block(fmt) {
        let w = width.max(1).div_ceil(4) * 4;
        let h = height.max(1).div_ceil(4) * 4;
        (w, h, h / 4)
    } else {
        (width, height, height)
    }
}

pub fn physical_total(fmt: u32, width: u32, height: u32, depth: u32) -> Option<u32> {
    let (_, slice) = physical_layout(fmt, width, height)?;
    slice.checked_mul(depth)
}

pub fn needs_initial_upload(fmt: u32) -> bool {
    matches!(info(fmt), Some(f) if !matches!(f.conversion, Conversion::Direct | Conversion::DepthDirect))
}

pub fn depth_readback_supported(fmt: u32) -> bool {
    matches!(fmt, D3DFMT_D16_LOCKABLE | D3DFMT_D32F_LOCKABLE)
}

pub fn copy_bytes(src: &[u8]) -> Result<Vec<u8>, RenderError> {
    let mut out = Vec::new();
    out.try_reserve_exact(src.len())
        .map_err(|_| RenderError::out_of_memory("ResourceCopy", "byte copy allocation failed"))?;
    out.extend_from_slice(src);
    Ok(out)
}

pub fn d3d_to_wgpu(fmt: u32) -> Option<wgpu::TextureFormat> {
    info(fmt).map(|f| f.wgpu)
}

pub fn has_stencil(fmt: u32) -> bool {
    matches!(
        d3d_to_wgpu(fmt),
        Some(wgpu::TextureFormat::Depth24PlusStencil8)
            | Some(wgpu::TextureFormat::Depth32FloatStencil8)
            | Some(wgpu::TextureFormat::Stencil8)
    )
}

pub fn row_pitch(fmt: u32, width: u32) -> Option<u32> {
    let f = info(fmt)?;
    match f.storage {
        StorageClass::Block => width.checked_add(3)?.checked_div(4)?.checked_mul(f.block),
        _ => width.checked_mul(f.bpp),
    }
}

pub fn block_rows(height: u32) -> u32 {
    height.div_ceil(4)
}

pub fn level_size(fmt: u32, width: u32, height: u32) -> Option<u32> {
    let f = info(fmt)?;
    let pitch = row_pitch(fmt, width)?;
    let rows = match f.storage {
        StorageClass::Block => block_rows(height),
        _ => height,
    };
    pitch.checked_mul(rows)
}

pub fn volume_size(fmt: u32, width: u32, height: u32, depth: u32) -> Option<(u32, u32, u32)> {
    let slice = level_size(fmt, width, height)?;
    let total = slice.checked_mul(depth)?;
    Some((row_pitch(fmt, width)?, slice, total))
}

pub fn level_dimensions(width: u32, height: u32, depth: u32, level: u32) -> (u32, u32, u32) {
    (
        (width >> level).max(1),
        (height >> level).max(1),
        (depth >> level).max(1),
    )
}

pub fn full_level_count(width: u32, height: u32, depth: u32) -> u32 {
    let max = width.max(height).max(depth);
    1 + (u32::BITS - 1 - max.leading_zeros())
}

pub fn level_count(
    width: u32,
    height: u32,
    depth: u32,
    requested: u32,
) -> Result<u32, RenderError> {
    let full = full_level_count(width, height, depth);
    if requested == 0 {
        return Ok(full);
    }
    if requested > full {
        return Err(RenderError::invalid(
            "CreateTexture",
            "requested mip level count exceeds the full chain",
        ));
    }
    Ok(requested)
}

pub fn validate_dimensions(width: u32, height: u32, depth: u32) -> Result<(), RenderError> {
    if width == 0 || height == 0 || depth == 0 {
        return Err(RenderError::invalid(
            "CreateTexture",
            "zero texture dimension",
        ));
    }
    Ok(())
}

pub fn level_fits_device(
    fmt: u32,
    width: u32,
    height: u32,
    depth: u32,
    limits: &wgpu::Limits,
    volume: bool,
) -> Result<(), RenderError> {
    let max_2d = limits.max_texture_dimension_2d;
    let max_3d = limits.max_texture_dimension_3d;
    if volume {
        if width > max_3d || height > max_3d || depth > max_3d {
            return Err(RenderError::invalid(
                "CreateTexture",
                "volume texture exceeds the device 3D limit",
            ));
        }
    } else if width > max_2d || height > max_2d {
        return Err(RenderError::invalid(
            "CreateTexture",
            "texture exceeds the device 2D limit",
        ));
    }
    let _ = fmt;
    Ok(())
}

fn fallible_vec(len: usize) -> Result<Vec<u8>, RenderError> {
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| {
        RenderError::out_of_memory("TexelConversion", "texel conversion allocation failed")
    })?;
    Ok(out)
}

fn expand_n(value: u32, bits: u32) -> u8 {
    match bits {
        1 => {
            if value == 0 {
                0
            } else {
                0xFF
            }
        }
        2 => ((value << 6) | (value << 4) | (value << 2) | value) as u8,
        3 => ((value << 5) | (value << 2) | (value >> 1)) as u8,
        4 => ((value << 4) | value) as u8,
        5 => ((value << 3) | (value >> 2)) as u8,
        6 => ((value << 2) | (value >> 4)) as u8,
        8 => value as u8,
        _ => value as u8,
    }
}

fn decode_texel_to_rgba8(fmt: u32, texel: &[u8], out: &mut Vec<u8>) {
    match fmt {
        D3DFMT_R8G8B8 => out.extend_from_slice(&[texel[2], texel[1], texel[0], 0xFF]),
        D3DFMT_R5G6B5 => {
            let v = u16::from_le_bytes([texel[0], texel[1]]) as u32;
            out.extend_from_slice(&[
                expand_n((v >> 11) & 0x1F, 5),
                expand_n((v >> 5) & 0x3F, 6),
                expand_n(v & 0x1F, 5),
                0xFF,
            ]);
        }
        D3DFMT_X1R5G5B5 | D3DFMT_A1R5G5B5 => {
            let v = u16::from_le_bytes([texel[0], texel[1]]) as u32;
            let a = if fmt == D3DFMT_A1R5G5B5 {
                expand_n((v >> 15) & 1, 1)
            } else {
                0xFF
            };
            out.extend_from_slice(&[
                expand_n((v >> 10) & 0x1F, 5),
                expand_n((v >> 5) & 0x1F, 5),
                expand_n(v & 0x1F, 5),
                a,
            ]);
        }
        D3DFMT_A4R4G4B4 | D3DFMT_X4R4G4B4 => {
            let v = u16::from_le_bytes([texel[0], texel[1]]) as u32;
            let a = if fmt == D3DFMT_A4R4G4B4 {
                expand_n((v >> 12) & 0xF, 4)
            } else {
                0xFF
            };
            out.extend_from_slice(&[
                expand_n((v >> 8) & 0xF, 4),
                expand_n((v >> 4) & 0xF, 4),
                expand_n(v & 0xF, 4),
                a,
            ]);
        }
        D3DFMT_R3G3B2 => {
            let v = texel[0] as u32;
            out.extend_from_slice(&[
                expand_n((v >> 5) & 0x7, 3),
                expand_n((v >> 2) & 0x7, 3),
                expand_n(v & 0x3, 2),
                0xFF,
            ]);
        }
        D3DFMT_A8 => out.extend_from_slice(&[0, 0, 0, texel[0]]),
        D3DFMT_A8R3G3B2 => {
            let v = u16::from_le_bytes([texel[0], texel[1]]) as u32;
            out.extend_from_slice(&[
                expand_n((v >> 5) & 0x7, 3),
                expand_n((v >> 2) & 0x7, 3),
                expand_n(v & 0x3, 2),
                (v >> 8) as u8,
            ]);
        }
        D3DFMT_L8 => out.extend_from_slice(&[texel[0], texel[0], texel[0], 0xFF]),
        D3DFMT_A8L8 => out.extend_from_slice(&[texel[0], texel[0], texel[0], texel[1]]),
        D3DFMT_A4L4 => {
            let v = texel[0];
            let l = expand_n((v & 0xF) as u32, 4);
            let a = expand_n((v >> 4) as u32, 4);
            out.extend_from_slice(&[l, l, l, a]);
        }
        _ => {}
    }
}

fn encode_texel_from_rgba8(fmt: u32, px: &[u8], out: &mut Vec<u8>) {
    match fmt {
        D3DFMT_R8G8B8 => out.extend_from_slice(&[px[2], px[1], px[0]]),
        D3DFMT_R5G6B5 => {
            let v = ((px[0] as u16 >> 3) << 11) | ((px[1] as u16 >> 2) << 5) | (px[2] as u16 >> 3);
            out.extend_from_slice(&v.to_le_bytes());
        }
        D3DFMT_X1R5G5B5 | D3DFMT_A1R5G5B5 => {
            let a = if fmt == D3DFMT_A1R5G5B5 && px[3] >= 0x80 {
                1u16 << 15
            } else {
                0
            };
            let v = a
                | (((px[0] as u16) >> 3) << 10)
                | (((px[1] as u16) >> 3) << 5)
                | ((px[2] as u16) >> 3);
            out.extend_from_slice(&v.to_le_bytes());
        }
        D3DFMT_A4R4G4B4 | D3DFMT_X4R4G4B4 => {
            let a = if fmt == D3DFMT_A4R4G4B4 {
                (px[3] as u16 >> 4) << 12
            } else {
                0xF000
            };
            let v = a
                | (((px[0] as u16) >> 4) << 8)
                | (((px[1] as u16) >> 4) << 4)
                | ((px[2] as u16) >> 4);
            out.extend_from_slice(&v.to_le_bytes());
        }
        D3DFMT_R3G3B2 => {
            out.push(
                (((px[0] as u32) >> 5) << 5 | ((px[1] as u32) >> 5) << 2 | ((px[2] as u32) >> 6))
                    as u8,
            );
        }
        D3DFMT_A8 => out.push(px[3]),
        D3DFMT_A8R3G3B2 => {
            let v = ((px[3] as u16) << 8)
                | (((px[0] as u16) >> 5) << 5)
                | (((px[1] as u16) >> 5) << 2)
                | ((px[2] as u16) >> 6);
            out.extend_from_slice(&v.to_le_bytes());
        }
        D3DFMT_L8 => out.push(px[0]),
        D3DFMT_A8L8 => {
            out.push(px[0]);
            out.push(px[3]);
        }
        D3DFMT_A4L4 => out.push(((px[3] >> 4) << 4) | (px[0] >> 4)),
        _ => out.extend_from_slice(&px[0..4.min(px.len())]),
    }
}

fn swap_rgb10(word: u32) -> u32 {
    let a = word & 0xC000_0000;
    let r = (word >> 20) & 0x3FF;
    let g = (word >> 10) & 0x3FF;
    let b = word & 0x3FF;
    a | (b << 20) | (g << 10) | r
}

fn checked_pixel_count(width: u32, height: u32, depth: u32) -> Result<usize, RenderError> {
    if width == 0 || height == 0 || depth == 0 {
        return Err(RenderError::invalid(
            "TexelConversion",
            "zero texture dimension",
        ));
    }
    (width as usize)
        .checked_mul(height as usize)
        .and_then(|pixels| pixels.checked_mul(depth as usize))
        .ok_or_else(|| RenderError::invalid("TexelConversion", "pixel count overflow"))
}

fn fallible_pixels(len: usize) -> Result<Vec<[f32; 4]>, RenderError> {
    let mut out = Vec::new();
    out.try_reserve_exact(len).map_err(|_| {
        RenderError::out_of_memory("TexelConversion", "RGBA float conversion allocation failed")
    })?;
    Ok(out)
}

fn color_format(fmt: u32) -> Result<FormatInfo, RenderError> {
    match info(fmt) {
        None => Err(RenderError::new(
            "TexelConversion",
            format!("format {fmt} is not a supported texture format"),
        )),
        Some(f) if matches!(f.storage, StorageClass::Block) => Err(RenderError::new(
            "TexelConversion",
            format!(
                "format {fmt} is block-compressed; DXT decoding is owned by the D3DX image path"
            ),
        )),
        Some(f) if matches!(f.storage, StorageClass::Depth) => Err(RenderError::new(
            "TexelConversion",
            format!("format {fmt} is a depth format and has no color conversion"),
        )),
        Some(f) => Ok(f),
    }
}

fn unorm_scale(value: u32, bits: u32) -> f32 {
    value as f32 / ((1u32 << bits) - 1) as f32
}

fn byte_to_unorm(value: u8) -> f32 {
    unorm_scale(value as u32, 8)
}

fn half_at(texel: &[u8], offset: usize) -> f32 {
    f16::from_bits(u16::from_le_bytes([texel[offset], texel[offset + 1]])).to_f32()
}

fn f32_at(texel: &[u8], offset: usize) -> f32 {
    f32::from_le_bytes([
        texel[offset],
        texel[offset + 1],
        texel[offset + 2],
        texel[offset + 3],
    ])
}

fn snorm8(byte: u8) -> f32 {
    (byte as i8 as f32 / 127.0).clamp(-1.0, 1.0)
}

fn snorm16(word: u16) -> f32 {
    (word as i16 as f32 / 32767.0).clamp(-1.0, 1.0)
}

fn decode_color_texel(fmt: u32, texel: &[u8]) -> Option<[f32; 4]> {
    let le16 = |offset: usize| u16::from_le_bytes([texel[offset], texel[offset + 1]]) as u32;
    let le32 = |offset: usize| {
        u32::from_le_bytes([
            texel[offset],
            texel[offset + 1],
            texel[offset + 2],
            texel[offset + 3],
        ])
    };
    let field = |value: u32, shift: u32, bits: u32| {
        unorm_scale((value >> shift) & ((1u32 << bits) - 1), bits)
    };
    Some(match fmt {
        D3DFMT_R8G8B8 => [
            byte_to_unorm(texel[2]),
            byte_to_unorm(texel[1]),
            byte_to_unorm(texel[0]),
            1.0,
        ],
        D3DFMT_A8R8G8B8 | D3DFMT_X8R8G8B8 => [
            byte_to_unorm(texel[2]),
            byte_to_unorm(texel[1]),
            byte_to_unorm(texel[0]),
            if fmt == D3DFMT_X8R8G8B8 {
                1.0
            } else {
                byte_to_unorm(texel[3])
            },
        ],
        D3DFMT_A8B8G8R8 | D3DFMT_X8B8G8R8 => [
            byte_to_unorm(texel[0]),
            byte_to_unorm(texel[1]),
            byte_to_unorm(texel[2]),
            if fmt == D3DFMT_X8B8G8R8 {
                1.0
            } else {
                byte_to_unorm(texel[3])
            },
        ],
        D3DFMT_R5G6B5 => {
            let value = le16(0);
            [
                field(value, 11, 5),
                field(value, 5, 6),
                field(value, 0, 5),
                1.0,
            ]
        }
        D3DFMT_X1R5G5B5 | D3DFMT_A1R5G5B5 => {
            let value = le16(0);
            [
                field(value, 10, 5),
                field(value, 5, 5),
                field(value, 0, 5),
                if fmt == D3DFMT_A1R5G5B5 {
                    field(value, 15, 1)
                } else {
                    1.0
                },
            ]
        }
        D3DFMT_A4R4G4B4 | D3DFMT_X4R4G4B4 => {
            let value = le16(0);
            [
                field(value, 8, 4),
                field(value, 4, 4),
                field(value, 0, 4),
                if fmt == D3DFMT_A4R4G4B4 {
                    field(value, 12, 4)
                } else {
                    1.0
                },
            ]
        }
        D3DFMT_R3G3B2 => {
            let value = texel[0] as u32;
            [
                field(value, 5, 3),
                field(value, 2, 3),
                field(value, 0, 2),
                1.0,
            ]
        }
        D3DFMT_A8 => [0.0, 0.0, 0.0, byte_to_unorm(texel[0])],
        D3DFMT_A8R3G3B2 => {
            let value = le16(0);
            [
                field(value, 5, 3),
                field(value, 2, 3),
                field(value, 0, 2),
                field(value, 8, 8),
            ]
        }
        D3DFMT_L8 => {
            let luminance = byte_to_unorm(texel[0]);
            [luminance, luminance, luminance, 1.0]
        }
        D3DFMT_A8L8 => {
            let luminance = byte_to_unorm(texel[0]);
            [luminance, luminance, luminance, byte_to_unorm(texel[1])]
        }
        D3DFMT_A4L4 => {
            let luminance = field(texel[0] as u32, 0, 4);
            [
                luminance,
                luminance,
                luminance,
                field(texel[0] as u32, 4, 4),
            ]
        }
        D3DFMT_A2B10G10R10 | D3DFMT_A2R10G10B10 => {
            let value = le32(0);
            let (red, blue) = if fmt == D3DFMT_A2B10G10R10 {
                (field(value, 0, 10), field(value, 20, 10))
            } else {
                (field(value, 20, 10), field(value, 0, 10))
            };
            [red, field(value, 10, 10), blue, field(value, 30, 2)]
        }
        D3DFMT_G16R16 => [unorm_scale(le16(0), 16), unorm_scale(le16(2), 16), 1.0, 1.0],
        D3DFMT_A16B16G16R16 => [
            unorm_scale(le16(0), 16),
            unorm_scale(le16(2), 16),
            unorm_scale(le16(4), 16),
            unorm_scale(le16(6), 16),
        ],
        D3DFMT_V8U8 => [snorm8(texel[0]), snorm8(texel[1]), 1.0, 1.0],
        D3DFMT_Q8W8V8U8 => [
            snorm8(texel[0]),
            snorm8(texel[1]),
            snorm8(texel[2]),
            snorm8(texel[3]),
        ],
        D3DFMT_V16U16 => [snorm16(le16(0) as u16), snorm16(le16(2) as u16), 1.0, 1.0],
        D3DFMT_Q16W16V16U16 => [
            snorm16(le16(0) as u16),
            snorm16(le16(2) as u16),
            snorm16(le16(4) as u16),
            snorm16(le16(6) as u16),
        ],
        D3DFMT_R16F => [half_at(texel, 0), 1.0, 1.0, 1.0],
        D3DFMT_G16R16F => [half_at(texel, 0), half_at(texel, 2), 1.0, 1.0],
        D3DFMT_A16B16G16R16F => [
            half_at(texel, 0),
            half_at(texel, 2),
            half_at(texel, 4),
            half_at(texel, 6),
        ],
        D3DFMT_R32F => [f32_at(texel, 0), 1.0, 1.0, 1.0],
        D3DFMT_G32R32F => [f32_at(texel, 0), f32_at(texel, 4), 1.0, 1.0],
        D3DFMT_A32B32G32R32F => [
            f32_at(texel, 0),
            f32_at(texel, 4),
            f32_at(texel, 8),
            f32_at(texel, 12),
        ],
        _ => return None,
    })
}

fn quantize_unorm(value: f32, max: u32) -> u32 {
    if value.is_nan() || value <= 0.0 {
        return 0;
    }
    if value >= 1.0 {
        return max;
    }
    (value * max as f32 + 0.5) as u32
}

fn quantize_snorm(value: f32, max: i32) -> i32 {
    let scaled = (value.clamp(-1.0, 1.0) * max as f32).round();
    scaled.clamp((-max - 1) as f32, max as f32) as i32
}

fn unorm_field(value: f32, bits: u32) -> u32 {
    quantize_unorm(value, (1u32 << bits) - 1)
}

fn quantize_byte(value: f32) -> u8 {
    quantize_unorm(value, 255) as u8
}

fn snorm_byte(value: f32) -> u8 {
    quantize_snorm(value, 127) as i8 as u8
}

fn snorm_word(value: f32) -> u16 {
    quantize_snorm(value, 32767) as i16 as u16
}

fn encode_color_texel(fmt: u32, [red, green, blue, alpha]: [f32; 4], out: &mut Vec<u8>) -> bool {
    match fmt {
        D3DFMT_R8G8B8 => out.extend_from_slice(&[
            quantize_byte(blue),
            quantize_byte(green),
            quantize_byte(red),
        ]),
        D3DFMT_A8R8G8B8 => out.extend_from_slice(&[
            quantize_byte(blue),
            quantize_byte(green),
            quantize_byte(red),
            quantize_byte(alpha),
        ]),
        D3DFMT_X8R8G8B8 => out.extend_from_slice(&[
            quantize_byte(blue),
            quantize_byte(green),
            quantize_byte(red),
            0xFF,
        ]),
        D3DFMT_A8B8G8R8 => out.extend_from_slice(&[
            quantize_byte(red),
            quantize_byte(green),
            quantize_byte(blue),
            quantize_byte(alpha),
        ]),
        D3DFMT_X8B8G8R8 => out.extend_from_slice(&[
            quantize_byte(red),
            quantize_byte(green),
            quantize_byte(blue),
            0xFF,
        ]),
        D3DFMT_R5G6B5 => {
            let value =
                (unorm_field(red, 5) << 11) | (unorm_field(green, 6) << 5) | unorm_field(blue, 5);
            out.extend_from_slice(&(value as u16).to_le_bytes());
        }
        D3DFMT_X1R5G5B5 | D3DFMT_A1R5G5B5 => {
            let top = if fmt == D3DFMT_A1R5G5B5 {
                unorm_field(alpha, 1)
            } else {
                1
            };
            let value = (top << 15)
                | (unorm_field(red, 5) << 10)
                | (unorm_field(green, 5) << 5)
                | unorm_field(blue, 5);
            out.extend_from_slice(&(value as u16).to_le_bytes());
        }
        D3DFMT_A4R4G4B4 | D3DFMT_X4R4G4B4 => {
            let top = if fmt == D3DFMT_A4R4G4B4 {
                unorm_field(alpha, 4)
            } else {
                0xF
            };
            let value = (top << 12)
                | (unorm_field(red, 4) << 8)
                | (unorm_field(green, 4) << 4)
                | unorm_field(blue, 4);
            out.extend_from_slice(&(value as u16).to_le_bytes());
        }
        D3DFMT_R3G3B2 => out.push(
            ((unorm_field(red, 3) << 5) | (unorm_field(green, 3) << 2) | unorm_field(blue, 2))
                as u8,
        ),
        D3DFMT_A8 => out.push(quantize_byte(alpha)),
        D3DFMT_A8R3G3B2 => {
            let value = ((quantize_byte(alpha) as u16) << 8)
                | ((unorm_field(red, 3) << 5) | (unorm_field(green, 3) << 2) | unorm_field(blue, 2))
                    as u16;
            out.extend_from_slice(&value.to_le_bytes());
        }
        D3DFMT_L8 => out.push(quantize_byte(red)),
        D3DFMT_A8L8 => out.extend_from_slice(&[quantize_byte(red), quantize_byte(alpha)]),
        D3DFMT_A4L4 => out.push(((unorm_field(alpha, 4) << 4) | unorm_field(red, 4)) as u8),
        D3DFMT_A2B10G10R10 => {
            let value = unorm_field(red, 10)
                | (unorm_field(green, 10) << 10)
                | (unorm_field(blue, 10) << 20)
                | (unorm_field(alpha, 2) << 30);
            out.extend_from_slice(&value.to_le_bytes());
        }
        D3DFMT_A2R10G10B10 => {
            let value = unorm_field(blue, 10)
                | (unorm_field(green, 10) << 10)
                | (unorm_field(red, 10) << 20)
                | (unorm_field(alpha, 2) << 30);
            out.extend_from_slice(&value.to_le_bytes());
        }
        D3DFMT_G16R16 => {
            out.extend_from_slice(&(unorm_field(red, 16) as u16).to_le_bytes());
            out.extend_from_slice(&(unorm_field(green, 16) as u16).to_le_bytes());
        }
        D3DFMT_A16B16G16R16 => {
            out.extend_from_slice(&(unorm_field(red, 16) as u16).to_le_bytes());
            out.extend_from_slice(&(unorm_field(green, 16) as u16).to_le_bytes());
            out.extend_from_slice(&(unorm_field(blue, 16) as u16).to_le_bytes());
            out.extend_from_slice(&(unorm_field(alpha, 16) as u16).to_le_bytes());
        }
        D3DFMT_V8U8 => out.extend_from_slice(&[snorm_byte(red), snorm_byte(green)]),
        D3DFMT_Q8W8V8U8 => out.extend_from_slice(&[
            snorm_byte(red),
            snorm_byte(green),
            snorm_byte(blue),
            snorm_byte(alpha),
        ]),
        D3DFMT_V16U16 => {
            out.extend_from_slice(&snorm_word(red).to_le_bytes());
            out.extend_from_slice(&snorm_word(green).to_le_bytes());
        }
        D3DFMT_Q16W16V16U16 => {
            out.extend_from_slice(&snorm_word(red).to_le_bytes());
            out.extend_from_slice(&snorm_word(green).to_le_bytes());
            out.extend_from_slice(&snorm_word(blue).to_le_bytes());
            out.extend_from_slice(&snorm_word(alpha).to_le_bytes());
        }
        D3DFMT_R16F => out.extend_from_slice(&f16::from_f32(red).to_bits().to_le_bytes()),
        D3DFMT_G16R16F => {
            out.extend_from_slice(&f16::from_f32(red).to_bits().to_le_bytes());
            out.extend_from_slice(&f16::from_f32(green).to_bits().to_le_bytes());
        }
        D3DFMT_A16B16G16R16F => {
            for channel in [red, green, blue, alpha] {
                out.extend_from_slice(&f16::from_f32(channel).to_bits().to_le_bytes());
            }
        }
        D3DFMT_R32F => out.extend_from_slice(&red.to_le_bytes()),
        D3DFMT_G32R32F => {
            out.extend_from_slice(&red.to_le_bytes());
            out.extend_from_slice(&green.to_le_bytes());
        }
        D3DFMT_A32B32G32R32F => {
            for channel in [red, green, blue, alpha] {
                out.extend_from_slice(&channel.to_le_bytes());
            }
        }
        _ => return false,
    }
    true
}

pub fn to_rgba_f32(
    fmt: u32,
    width: u32,
    height: u32,
    depth: u32,
    src: &[u8],
) -> Result<Vec<[f32; 4]>, RenderError> {
    let f = color_format(fmt)?;
    let pixels = checked_pixel_count(width, height, depth)?;
    let texel = f.bpp as usize;
    let expected = pixels
        .checked_mul(texel)
        .ok_or_else(|| RenderError::invalid("TexelConversion", "source level size overflow"))?;
    if src.len() < expected {
        return Err(RenderError::invalid(
            "TexelConversion",
            format!(
                "source level holds {} bytes but {pixels} texels of format {fmt} need {expected}",
                src.len()
            ),
        ));
    }
    let mut out = fallible_pixels(pixels)?;
    for texel_bytes in src[..expected].chunks_exact(texel) {
        let Some(pixel) = decode_color_texel(fmt, texel_bytes) else {
            return Err(RenderError::new(
                "TexelConversion",
                format!("format {fmt} has no float color decoder"),
            ));
        };
        out.push(pixel);
    }
    Ok(out)
}

pub fn from_rgba_f32(
    fmt: u32,
    width: u32,
    height: u32,
    depth: u32,
    src: &[[f32; 4]],
) -> Result<Vec<u8>, RenderError> {
    let f = color_format(fmt)?;
    let pixels = checked_pixel_count(width, height, depth)?;
    if src.len() < pixels {
        return Err(RenderError::invalid(
            "TexelConversion",
            format!(
                "RGBA float source holds {} pixels but the level needs {pixels}",
                src.len()
            ),
        ));
    }
    let capacity = pixels
        .checked_mul(f.bpp as usize)
        .ok_or_else(|| RenderError::invalid("TexelConversion", "packed level size overflow"))?;
    let mut out = fallible_vec(capacity)?;
    for pixel in &src[..pixels] {
        if !encode_color_texel(fmt, *pixel, &mut out) {
            return Err(RenderError::new(
                "TexelConversion",
                format!("format {fmt} has no packed color encoder"),
            ));
        }
    }
    Ok(out)
}

pub fn upload_bytes(
    fmt: u32,
    width: u32,
    height: u32,
    depth: u32,
    src: &[u8],
) -> Result<Vec<u8>, RenderError> {
    let Some(f) = info(fmt) else {
        return Err(RenderError::new(
            "UploadTexture",
            "unsupported texture format",
        ));
    };
    let pixels = (width as usize) * (height as usize) * (depth as usize);
    let texel = f.bpp as usize;
    match f.conversion {
        Conversion::Direct | Conversion::DepthDirect => copy_bytes(src),
        Conversion::BgraOpaque | Conversion::RgbaOpaque => {
            if src.len() < pixels * 4 {
                return Err(RenderError::invalid(
                    "UploadTexture",
                    "source level is shorter than its layout",
                ));
            }
            let mut out = copy_bytes(&src[..pixels * 4])?;
            for px in out.chunks_exact_mut(4) {
                px[3] = 0xFF;
            }
            Ok(out)
        }
        Conversion::SwapRgb10 => {
            if src.len() < pixels * 4 {
                return Err(RenderError::invalid(
                    "UploadTexture",
                    "source level is shorter than its layout",
                ));
            }
            let mut out = fallible_vec(pixels * 4)?;
            for texel_bytes in src[..pixels * 4].chunks_exact(4) {
                let word = u32::from_le_bytes([
                    texel_bytes[0],
                    texel_bytes[1],
                    texel_bytes[2],
                    texel_bytes[3],
                ]);
                out.extend_from_slice(&swap_rgb10(word).to_le_bytes());
            }
            Ok(out)
        }
        Conversion::ToRgba8 => {
            if src.len() < pixels * texel {
                return Err(RenderError::invalid(
                    "UploadTexture",
                    "source level is shorter than its layout",
                ));
            }
            let mut out = fallible_vec(pixels * 4)?;
            for slice in 0..depth as usize {
                let base = slice * (width as usize) * (height as usize) * texel;
                for t in 0..(width as usize) * (height as usize) {
                    decode_texel_to_rgba8(
                        fmt,
                        &src[base + t * texel..base + t * texel + texel],
                        &mut out,
                    );
                }
            }
            Ok(out)
        }
    }
}

pub fn readback_bytes(
    fmt: u32,
    width: u32,
    height: u32,
    depth: u32,
    src: &[u8],
) -> Result<Vec<u8>, RenderError> {
    let Some(f) = info(fmt) else {
        return Err(RenderError::new(
            "ReadbackTexture",
            "unsupported texture format",
        ));
    };
    let pixels = (width as usize) * (height as usize) * (depth as usize);
    let texel = f.bpp as usize;
    match f.conversion {
        Conversion::Direct
        | Conversion::DepthDirect
        | Conversion::BgraOpaque
        | Conversion::RgbaOpaque => copy_bytes(src),
        Conversion::SwapRgb10 => {
            if src.len() < pixels * 4 {
                return Err(RenderError::invalid(
                    "ReadbackTexture",
                    "GPU level is shorter than its layout",
                ));
            }
            let mut out = fallible_vec(pixels * 4)?;
            for texel_bytes in src[..pixels * 4].chunks_exact(4) {
                let word = u32::from_le_bytes([
                    texel_bytes[0],
                    texel_bytes[1],
                    texel_bytes[2],
                    texel_bytes[3],
                ]);
                out.extend_from_slice(&swap_rgb10(word).to_le_bytes());
            }
            Ok(out)
        }
        Conversion::ToRgba8 => {
            if src.len() < pixels * 4 {
                return Err(RenderError::invalid(
                    "ReadbackTexture",
                    "GPU level is shorter than its layout",
                ));
            }
            let mut out = fallible_vec(pixels * texel)?;
            for slice in 0..depth as usize {
                let base = slice * (width as usize) * (height as usize) * 4;
                for t in 0..(width as usize) * (height as usize) {
                    encode_texel_from_rgba8(fmt, &src[base + t * 4..base + t * 4 + 4], &mut out);
                }
            }
            Ok(out)
        }
    }
}
