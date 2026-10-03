//! Mapping between `D3DFORMAT` and `wgpu::TextureFormat`.
//!
//! The bridge supports the two 32-bit ARGB formats the Ghost Recon presentation
//! path is known to use, `D3DFMT_A8R8G8B8` (21) and `D3DFMT_X8R8G8B8` (22), plus
//! the 16-bit formats the shell's textures use: `D3DFMT_R5G6B5` (23),
//! `D3DFMT_A1R5G5B5` (25) and `D3DFMT_A4R4G4B4` (26). Any other format is a
//! named error rather than an unchecked substitution.
//!
//! Every format maps to a **linear UNORM** `wgpu::TextureFormat::Rgba8Unorm`.
//! The mapping is a channel conversion, not a raw-memory reinterpretation: an
//! `A8R8G8B8` integer is decoded into `R,G,B,A` components (see
//! [`d3dcolor_to_rgba8`]), a 16-bit texel is expanded into those components, and
//! readback returns those components in that order, tightly packed. No claim is
//! made that little-endian D3D8 texel bytes equal RGBA8 bytes.
//!
//! Alpha rule:
//! * `A8R8G8B8` carries alpha, which is preserved through clear and readback.
//! * `X8R8G8B8` has no alpha. The clear path forces alpha to 1.0 (byte 255) and
//!   the readback path forces every alpha byte to 255. A draw pipeline must not
//!   disturb that alpha; the backend exposes an RGB-only write mask for this
//!   (`OffscreenTarget::color_write_mask`). The present blit copies the stored
//!   RGB texels unchanged.
//!
//! Format values follow the pinned Wine D3D8 headers
//! ([`reference/wine/d3d8types.h`](../../reference/wine/d3d8types.h)).

use crate::RenderError;

/// `D3DFMT_A8R8G8B8` from `d3d8types.h`.
pub const D3DFMT_A8R8G8B8: u32 = 21;
/// `D3DFMT_X8R8G8B8` from `d3d8types.h`.
pub const D3DFMT_X8R8G8B8: u32 = 22;
/// `D3DFMT_R5G6B5` from `d3d8types.h`.
pub const D3DFMT_R5G6B5: u32 = 23;
/// `D3DFMT_A1R5G5B5` from `d3d8types.h`.
pub const D3DFMT_A1R5G5B5: u32 = 25;
/// `D3DFMT_A4R4G4B4` from `d3d8types.h`.
pub const D3DFMT_A4R4G4B4: u32 = 26;

/// Byte order produced by [`d3dcolor_to_rgba8`] and by readback.
pub const READBACK_ORDER: &str = "RGBA8 (R,G,B,A), tightly packed, row-major";

/// Depth/stencil formats the bridge accepts, from `d3d8types.h`.
pub const D3DFMT_D16: u32 = 80;
pub const D3DFMT_D24S8: u32 = 75;
pub const D3DFMT_D24X8: u32 = 77;
pub const D3DFMT_D32: u32 = 71;

/// Supported D3D8 depth/stencil formats, and their honest wgpu equivalents.
///
/// D3D8's depth formats are legacy layouts; wgpu has no exact D16/D24/D32 bit
/// promises, so each maps to the nearest format with the same depth precision
/// and stencil presence:
///
/// * `D16`    -> `Depth16Unorm` (exact: 16-bit unsigned normalized depth).
/// * `D24X8`  -> `Depth24Plus` (at least 24 bits of depth, no stencil).
/// * `D24S8`  -> `Depth24PlusStencil8` (at least 24 depth + 8 stencil).
/// * `D32`    -> `Depth32Float` (exact: 32-bit float depth).
///
/// `Depth24Plus`/`Depth24PlusStencil8` are the portable wgpu spellings for a
/// 24-bit depth buffer; the exact storage bit width is backend-defined, so a
/// readback of depth bytes is not promised. The bridge only uses these as
/// render attachments, where the comparison semantics are what D3D8 asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DepthFormat {
    D16,
    D24X8,
    D24S8,
    D32,
}

impl DepthFormat {
    /// Convert a raw `D3DFORMAT`. `0` (no depth buffer) is `Ok(None)`; other
    /// unsupported values are a named error.
    pub fn from_d3dformat(raw: u32) -> Result<Option<Self>, RenderError> {
        match raw {
            0 => Ok(None),
            D3DFMT_D16 => Ok(Some(Self::D16)),
            D3DFMT_D24X8 => Ok(Some(Self::D24X8)),
            D3DFMT_D24S8 => Ok(Some(Self::D24S8)),
            D3DFMT_D32 => Ok(Some(Self::D32)),
            other => Err(RenderError::new(
                "format::depth_from_d3dformat",
                format!(
                    "unsupported depth D3DFORMAT {other}; only D16 ({D3DFMT_D16}), \
                     D24S8 ({D3DFMT_D24S8}), D24X8 ({D3DFMT_D24X8}) and D32 ({D3DFMT_D32}) \
                     are implemented"
                ),
            )),
        }
    }

    /// The raw originating `D3DFORMAT`.
    pub const fn d3dformat(self) -> u32 {
        match self {
            Self::D16 => D3DFMT_D16,
            Self::D24X8 => D3DFMT_D24X8,
            Self::D24S8 => D3DFMT_D24S8,
            Self::D32 => D3DFMT_D32,
        }
    }

    /// The wgpu render-attachment format.
    pub const fn wgpu_format(self) -> wgpu::TextureFormat {
        match self {
            Self::D16 => wgpu::TextureFormat::Depth16Unorm,
            Self::D24X8 => wgpu::TextureFormat::Depth24Plus,
            Self::D24S8 => wgpu::TextureFormat::Depth24PlusStencil8,
            Self::D32 => wgpu::TextureFormat::Depth32Float,
        }
    }

    /// True when the wgpu format carries a stencil aspect.
    pub const fn has_stencil(self) -> bool {
        matches!(self, Self::D24S8)
    }
}

/// Supported D3D8 color formats.
///
/// This is intentionally a small closed set. `X1R5G5B5` (24) and palette/
/// compressed formats are deliberately absent and fail by name.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorFormat {
    /// 32-bit ARGB with alpha, `D3DFMT_A8R8G8B8 = 21`.
    A8R8G8B8,
    /// 32-bit ARGB with ignored alpha, `D3DFMT_X8R8G8B8 = 22`.
    X8R8G8B8,
    /// 16-bit, no alpha: red 5, green 6, blue 5 (`D3DFMT_R5G6B5 = 23`).
    R5G6B5,
    /// 16-bit, 1-bit alpha: A1 R5 G5 B5 (`D3DFMT_A1R5G5B5 = 25`).
    A1R5G5B5,
    /// 16-bit, 4-bit alpha: A4 R4 G4 B4 (`D3DFMT_A4R4G4B4 = 26`).
    A4R4G4B4,
}

impl ColorFormat {
    /// Convert a raw `D3DFORMAT` value. Unsupported values are a named error.
    pub fn from_d3dformat(raw: u32) -> Result<Self, RenderError> {
        match raw {
            D3DFMT_A8R8G8B8 => Ok(Self::A8R8G8B8),
            D3DFMT_X8R8G8B8 => Ok(Self::X8R8G8B8),
            D3DFMT_R5G6B5 => Ok(Self::R5G6B5),
            D3DFMT_A1R5G5B5 => Ok(Self::A1R5G5B5),
            D3DFMT_A4R4G4B4 => Ok(Self::A4R4G4B4),
            other => Err(RenderError::new(
                "format::from_d3dformat",
                format!(
                    "unsupported D3DFORMAT {other}; implemented are A8R8G8B8 \
                     ({D3DFMT_A8R8G8B8}), X8R8G8B8 ({D3DFMT_X8R8G8B8}), R5G6B5 \
                     ({D3DFMT_R5G6B5}), A1R5G5B5 ({D3DFMT_A1R5G5B5}) and A4R4G4B4 \
                     ({D3DFMT_A4R4G4B4})"
                ),
            )),
        }
    }

    /// The raw `D3DFORMAT` value.
    pub const fn d3dformat(self) -> u32 {
        match self {
            Self::A8R8G8B8 => D3DFMT_A8R8G8B8,
            Self::X8R8G8B8 => D3DFMT_X8R8G8B8,
            Self::R5G6B5 => D3DFMT_R5G6B5,
            Self::A1R5G5B5 => D3DFMT_A1R5G5B5,
            Self::A4R4G4B4 => D3DFMT_A4R4G4B4,
        }
    }

    /// The wgpu texture format used for offscreen targets.
    ///
    /// Always linear (never sRGB) so exact UNORM clear/readback checks are not
    /// disturbed by color-space conversion.
    pub const fn wgpu_format(self) -> wgpu::TextureFormat {
        wgpu::TextureFormat::Rgba8Unorm
    }

    /// Bytes per texel in the source D3D8 layout: 4 for the 32-bit ARGB
    /// formats, 2 for the packed 16-bit formats.
    pub const fn bytes_per_pixel(self) -> u32 {
        match self {
            Self::A8R8G8B8 | Self::X8R8G8B8 => 4,
            Self::R5G6B5 | Self::A1R5G5B5 | Self::A4R4G4B4 => 2,
        }
    }

    /// True when the format carries no alpha channel and must sample opaque.
    pub const fn is_opaque(self) -> bool {
        matches!(self, Self::X8R8G8B8 | Self::R5G6B5)
    }

    /// Decode tightly packed source texels in this format into wgpu's `R,G,B,A`
    /// byte order. `data` is the complete level-0 block
    /// (`width * height * bytes_per_pixel`, little-endian D3D8 layout);
    /// `chunks_exact` ignores any trailing partial texel.
    pub fn to_rgba8(self, data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        self.to_rgba8_into(data, &mut out);
        out
    }

    /// Same conversion as [`ColorFormat::to_rgba8`], writing into a reusable
    /// buffer instead of allocating per call. `out` is cleared first and grows
    /// to exactly the decoded texel count. The device keeps one such buffer so
    /// a per-draw bind does not allocate (the dominant cost when the source
    /// content is unchanged is avoided entirely by the upload cache, but a
    /// genuine upload still reuses this scratch).
    pub fn to_rgba8_into(self, data: &[u8], out: &mut Vec<u8>) {
        match self {
            Self::A8R8G8B8 => bgra_to_rgba_into(data, false, out),
            Self::X8R8G8B8 => bgra_to_rgba_into(data, true, out),
            Self::R5G6B5 | Self::A1R5G5B5 | Self::A4R4G4B4 => decode_16_into(data, self, out),
        }
    }
}

/// Swizzle D3D8 little-endian `A8R8G8B8`/`X8R8G8B8` texels (`B,G,R,A`) into
/// wgpu's `R,G,B,A` byte order. `force_opaque` reproduces the `X8R8G8B8`
/// alpha rule (alpha byte = 255).
fn bgra_to_rgba_into(data: &[u8], force_opaque: bool, out: &mut Vec<u8>) {
    out.clear();
    out.extend_from_slice(data);
    for px in out.chunks_exact_mut(4) {
        px.swap(0, 2);
        if force_opaque {
            px[3] = 255;
        }
    }
}

/// Expand an `n`-bit UNORM channel to 8 bits by bit replication, D3D8's
/// channel conversion: `v << (8-n) | v >> (2n-8)` for `n >= 4`. A 1-bit
/// channel has no lower bits to replicate, so it maps 0 -> 0 and 1 -> 0xFF.
/// (The `n >= 4` formula is underflow for `n = 1`, which is why it is split
/// out; no 2- or 3-bit channel reaches this bridge.)
fn expand_channel(value: u32, bits: u32) -> u8 {
    match bits {
        1 => {
            debug_assert!(value <= 1);
            if value == 0 { 0 } else { 0xFF }
        }
        n if n >= 4 => {
            debug_assert!(value < (1u32 << n));
            ((value << (8 - n)) | (value >> (2 * n - 8))) as u8
        }
        _ => unreachable!("no 2/3-bit channel is decoded by this bridge"),
    }
}

/// Decode a little-endian packed 16-bit level into `R,G,B,A`. The D3D8 names
/// list the most-significant channel first (as in `A8R8G8B8`), so `R5G6B5` is
/// `R` bits 15..11, `G` 10..5, `B` 4..0, and so on.
fn decode_16_into(data: &[u8], format: ColorFormat, out: &mut Vec<u8>) {
    out.clear();
    out.reserve(data.len() / 2 * 4);
    for texel in data.chunks_exact(2) {
        let texel = u16::from_le_bytes([texel[0], texel[1]]) as u32;
        let (r, g, b, a) = match format {
            ColorFormat::R5G6B5 => (
                expand_channel((texel >> 11) & 0x1F, 5),
                expand_channel((texel >> 5) & 0x3F, 6),
                expand_channel(texel & 0x1F, 5),
                0xFF,
            ),
            ColorFormat::A1R5G5B5 => (
                expand_channel((texel >> 10) & 0x1F, 5),
                expand_channel((texel >> 5) & 0x1F, 5),
                expand_channel(texel & 0x1F, 5),
                expand_channel((texel >> 15) & 0x1, 1),
            ),
            ColorFormat::A4R4G4B4 => (
                expand_channel((texel >> 8) & 0xF, 4),
                expand_channel((texel >> 4) & 0xF, 4),
                expand_channel(texel & 0xF, 4),
                expand_channel((texel >> 12) & 0xF, 4),
            ),
            _ => unreachable!("decode_16 is only called for 16-bit formats"),
        };
        out.extend_from_slice(&[r, g, b, a]);
    }
}

/// Map a raw `D3DFORMAT` to a wgpu texture format, rejecting unsupported values.
pub fn d3d_to_wgpu_color(raw: u32) -> Result<wgpu::TextureFormat, RenderError> {
    Ok(ColorFormat::from_d3dformat(raw)?.wgpu_format())
}

/// Bytes per pixel for a raw `D3DFORMAT`, rejecting unsupported values.
pub fn bytes_per_pixel(raw: u32) -> Result<u32, RenderError> {
    Ok(ColorFormat::from_d3dformat(raw)?.bytes_per_pixel())
}

/// Decode a D3D `D3DCOLOR` (`0xAARRGGBB`) into wgpu clear components.
///
/// When `force_opaque` is set (the `X8R8G8B8` rule), the alpha component of the
/// input is ignored and 1.0 is used.
pub fn d3dcolor_to_wgpu(argb: u32, force_opaque: bool) -> wgpu::Color {
    let a = if force_opaque {
        255
    } else {
        (argb >> 24) & 0xFF
    };
    let r = (argb >> 16) & 0xFF;
    let g = (argb >> 8) & 0xFF;
    let b = argb & 0xFF;
    wgpu::Color {
        r: r as f64 / 255.0,
        g: g as f64 / 255.0,
        b: b as f64 / 255.0,
        a: a as f64 / 255.0,
    }
}

/// Decode a D3D `D3DCOLOR` (`0xAARRGGBB`) into a tightly packed RGBA8 texel.
///
/// When `force_opaque` is set (the `X8R8G8B8` rule), the returned alpha byte is
/// 255 regardless of the input.
pub fn d3dcolor_to_rgba8(argb: u32, force_opaque: bool) -> [u8; 4] {
    let a = if force_opaque {
        255
    } else {
        (argb >> 24) & 0xFF
    };
    [
        ((argb >> 16) & 0xFF) as u8,
        ((argb >> 8) & 0xFF) as u8,
        (argb & 0xFF) as u8,
        a as u8,
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn le16(value: u16) -> [u8; 2] {
        value.to_le_bytes()
    }

    #[test]
    fn supported_formats_map_to_linear_unorm() {
        for (raw, bpp) in [
            (D3DFMT_A8R8G8B8, 4),
            (D3DFMT_X8R8G8B8, 4),
            (D3DFMT_R5G6B5, 2),
            (D3DFMT_A1R5G5B5, 2),
            (D3DFMT_A4R4G4B4, 2),
        ] {
            assert_eq!(
                d3d_to_wgpu_color(raw).unwrap(),
                wgpu::TextureFormat::Rgba8Unorm
            );
            assert_eq!(bytes_per_pixel(raw).unwrap(), bpp);
            assert_eq!(ColorFormat::from_d3dformat(raw).unwrap().d3dformat(), raw);
        }
    }

    #[test]
    fn opaque_rule_applies_to_x8_and_r5g6b5() {
        for raw in [D3DFMT_X8R8G8B8, D3DFMT_R5G6B5] {
            assert!(ColorFormat::from_d3dformat(raw).unwrap().is_opaque());
        }
        for raw in [D3DFMT_A8R8G8B8, D3DFMT_A1R5G5B5, D3DFMT_A4R4G4B4] {
            assert!(!ColorFormat::from_d3dformat(raw).unwrap().is_opaque());
        }
    }

    #[test]
    fn unsupported_format_is_a_named_error() {
        // D3DFMT_X1R5G5B5 = 24 is deliberately not mapped.
        let err = d3d_to_wgpu_color(24).unwrap_err();
        assert_eq!(err.operation, "format::from_d3dformat");
        assert!(
            err.cause.contains("24"),
            "cause should name the value: {err}"
        );
    }

    #[test]
    fn bit_replication_expansion_is_exact() {
        // 4-bit: v*17; the 0xF -> 0xFF case.
        assert_eq!(expand_channel(0x0, 4), 0x00);
        assert_eq!(expand_channel(0x1, 4), 0x11);
        assert_eq!(expand_channel(0x8, 4), 0x88);
        assert_eq!(expand_channel(0xF, 4), 0xFF);
        // 5-bit: v<<3 | v>>2.
        assert_eq!(expand_channel(0x00, 5), 0x00);
        assert_eq!(expand_channel(0x01, 5), 0x08);
        assert_eq!(expand_channel(0x10, 5), 0x84);
        assert_eq!(expand_channel(0x1F, 5), 0xFF);
        // 6-bit: v<<2 | v>>4.
        assert_eq!(expand_channel(0x01, 6), 0x04);
        assert_eq!(expand_channel(0x20, 6), 0x82);
        assert_eq!(expand_channel(0x3F, 6), 0xFF);
        // 1-bit alpha: only the endpoints exist.
        assert_eq!(expand_channel(0, 1), 0x00);
        assert_eq!(expand_channel(1, 1), 0xFF);
    }

    #[test]
    fn r5g6b5_decodes_rgb_and_is_opaque() {
        let f = ColorFormat::R5G6B5;
        assert_eq!(f.to_rgba8(&le16(0x0000)), [0, 0, 0, 0xFF]);
        assert_eq!(f.to_rgba8(&le16(0xF800)), [0xFF, 0, 0, 0xFF]);
        assert_eq!(f.to_rgba8(&le16(0x07E0)), [0, 0xFF, 0, 0xFF]);
        assert_eq!(f.to_rgba8(&le16(0x001F)), [0, 0, 0xFF, 0xFF]);
        assert_eq!(f.to_rgba8(&le16(0xFFFF)), [0xFF, 0xFF, 0xFF, 0xFF]);
        // Two texels stay tightly packed with no cross-texel carry.
        assert_eq!(
            f.to_rgba8(&[
                le16(0xF800)[0],
                le16(0xF800)[1],
                le16(0x001F)[0],
                le16(0x001F)[1]
            ]),
            [0xFF, 0, 0, 0xFF, 0, 0, 0xFF, 0xFF]
        );
    }

    #[test]
    fn a4r4g4b4_decodes_each_nibble_and_alpha() {
        let f = ColorFormat::A4R4G4B4;
        // 0xF nibbles all expand to 0xFF, including alpha.
        assert_eq!(f.to_rgba8(&le16(0xFFFF)), [0xFF, 0xFF, 0xFF, 0xFF]);
        // Alpha nibble alone: 0xF -> 0xFF.
        assert_eq!(f.to_rgba8(&le16(0xF000)), [0, 0, 0, 0xFF]);
        assert_eq!(f.to_rgba8(&le16(0x1000)), [0, 0, 0, 0x11]);
        // Red nibble alone: 0xF -> 0xFF.
        assert_eq!(f.to_rgba8(&le16(0x0F00)), [0xFF, 0, 0, 0]);
        // Green and blue nibbles.
        assert_eq!(f.to_rgba8(&le16(0x00F0)), [0, 0xFF, 0, 0]);
        assert_eq!(f.to_rgba8(&le16(0x000F)), [0, 0, 0xFF, 0]);
    }

    #[test]
    fn a1r5g5b5_decodes_the_1_bit_alpha() {
        let f = ColorFormat::A1R5G5B5;
        // A=1, R=31: 0x8000 | 0x7C00.
        assert_eq!(f.to_rgba8(&le16(0xFC00)), [0xFF, 0, 0, 0xFF]);
        // A=0, R=31.
        assert_eq!(f.to_rgba8(&le16(0x7C00)), [0xFF, 0, 0, 0]);
        // A=0, G=31, B=31.
        assert_eq!(f.to_rgba8(&le16(0x03E0)), [0, 0xFF, 0, 0]);
        assert_eq!(f.to_rgba8(&le16(0x001F)), [0, 0, 0xFF, 0]);
    }

    #[test]
    fn to_rgba8_swizzles_32_bit_bgra() {
        // D3DCOLOR 0xAARRGGBB is stored little-endian B,G,R,A.
        assert_eq!(
            ColorFormat::A8R8G8B8.to_rgba8(&[0x44, 0x33, 0x22, 0x11]),
            [0x22, 0x33, 0x44, 0x11]
        );
        assert_eq!(
            ColorFormat::X8R8G8B8.to_rgba8(&[0x44, 0x33, 0x22, 0x11]),
            [0x22, 0x33, 0x44, 0xFF]
        );
    }

    #[test]
    fn depth_formats_map_to_honest_wgpu_formats() {
        use wgpu::TextureFormat as F;
        assert_eq!(
            DepthFormat::from_d3dformat(D3DFMT_D16).unwrap(),
            Some(DepthFormat::D16)
        );
        assert_eq!(
            DepthFormat::from_d3dformat(D3DFMT_D24X8).unwrap(),
            Some(DepthFormat::D24X8)
        );
        assert_eq!(
            DepthFormat::from_d3dformat(D3DFMT_D24S8).unwrap(),
            Some(DepthFormat::D24S8)
        );
        assert_eq!(
            DepthFormat::from_d3dformat(D3DFMT_D32).unwrap(),
            Some(DepthFormat::D32)
        );
        assert_eq!(DepthFormat::from_d3dformat(0).unwrap(), None);
        assert_eq!(DepthFormat::D16.wgpu_format(), F::Depth16Unorm);
        assert_eq!(DepthFormat::D24X8.wgpu_format(), F::Depth24Plus);
        assert_eq!(DepthFormat::D24S8.wgpu_format(), F::Depth24PlusStencil8);
        assert_eq!(DepthFormat::D32.wgpu_format(), F::Depth32Float);
        assert!(DepthFormat::D24S8.has_stencil());
        assert!(!DepthFormat::D24X8.has_stencil());
        assert!(!DepthFormat::D32.has_stencil());
        for f in [
            DepthFormat::D16,
            DepthFormat::D24X8,
            DepthFormat::D24S8,
            DepthFormat::D32,
        ] {
            assert_eq!(DepthFormat::from_d3dformat(f.d3dformat()).unwrap(), Some(f));
        }
        let err = DepthFormat::from_d3dformat(0x7fff_ffff).unwrap_err();
        assert_eq!(err.operation, "format::depth_from_d3dformat");
    }

    #[test]
    fn d3dcolor_argb_decodes_to_rgba() {
        // A=0x11, R=0x22, G=0x33, B=0x44.
        assert_eq!(
            d3dcolor_to_rgba8(0x1122_3344, false),
            [0x22, 0x33, 0x44, 0x11]
        );
        assert_eq!(
            d3dcolor_to_rgba8(0x1122_3344, true),
            [0x22, 0x33, 0x44, 0xFF]
        );
    }

    #[test]
    fn clear_color_channels_match_argb() {
        let c = d3dcolor_to_wgpu(0x8040_2010, false);
        assert!((c.r - 0x40 as f64 / 255.0).abs() < f64::EPSILON);
        assert!((c.g - 0x20 as f64 / 255.0).abs() < f64::EPSILON);
        assert!((c.b - 0x10 as f64 / 255.0).abs() < f64::EPSILON);
        assert!((c.a - 0x80 as f64 / 255.0).abs() < f64::EPSILON);
        let c = d3dcolor_to_wgpu(0x8040_2010, true);
        assert!((c.a - 1.0).abs() < f64::EPSILON);
    }
}
