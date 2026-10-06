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
/// Signed bump offsets: little-endian U,V bytes, sampled as R,G in [-1,1].
/// Microsoft maps D3DFMT_V8U8 to R8G8_SNORM; missing B,A sample as 0,1.
/// https://learn.microsoft.com/en-us/windows/uwp/gaming/feature-mapping
pub const D3DFMT_V8U8: u32 = 60;

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

    /// Inverse of [`ColorFormat::to_rgba8_into`]: pack tightly packed `R,G,B,A`
    /// bytes back into this format's little-endian D3D8 layout. This is the
    /// render-target readback path. `data` must hold whole RGBA texels and is
    /// truncated to the largest whole-texel prefix. Channels are truncated to
    /// the destination width; the matching decode expands by bit replication,
    /// so the round trip is exact only for values that came from that decode.
    pub fn from_rgba8_into(self, data: &[u8], out: &mut Vec<u8>) {
        out.clear();
        match self {
            Self::A8R8G8B8 | Self::X8R8G8B8 => {
                let opaque = self.is_opaque();
                out.reserve(data.len());
                for px in data.chunks_exact(4) {
                    out.extend_from_slice(&[
                        px[2],
                        px[1],
                        px[0],
                        if opaque { 0xFF } else { px[3] },
                    ]);
                }
            }
            Self::R5G6B5 => {
                out.reserve(data.len() / 2);
                for px in data.chunks_exact(4) {
                    let texel = (((px[0] as u16) >> 3) << 11)
                        | (((px[1] as u16) >> 2) << 5)
                        | ((px[2] as u16) >> 3);
                    out.extend_from_slice(&texel.to_le_bytes());
                }
            }
            Self::A1R5G5B5 => {
                out.reserve(data.len() / 2);
                for px in data.chunks_exact(4) {
                    let alpha = if px[3] >= 0x80 { 1u16 } else { 0 };
                    let texel = (alpha << 15)
                        | (((px[0] as u16) >> 3) << 10)
                        | (((px[1] as u16) >> 3) << 5)
                        | ((px[2] as u16) >> 3);
                    out.extend_from_slice(&texel.to_le_bytes());
                }
            }
            Self::A4R4G4B4 => {
                out.reserve(data.len() / 2);
                for px in data.chunks_exact(4) {
                    let texel = (((px[3] as u16) >> 4) << 12)
                        | (((px[0] as u16) >> 4) << 8)
                        | (((px[1] as u16) >> 4) << 4)
                        | ((px[2] as u16) >> 4);
                    out.extend_from_slice(&texel.to_le_bytes());
                }
            }
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

/// S3TC / BC block FourCCs, written as the little-endian D3D8 values so
/// `0x31545844` reads `'DXT1'` in a hex dump. The guest's compressed block
/// bytes stay authoritative in CPU storage; these formats are decoded to
/// linear RGBA only at the wgpu upload point.
pub const D3DFMT_DXT1: u32 = 0x3154_5844;
pub const D3DFMT_DXT3: u32 = 0x3354_5844;
pub const D3DFMT_DXT5: u32 = 0x3554_5844;

/// Bytes per 4x4 block: DXT1 is 8, the explicit-alpha formats are 16.
pub const fn block_bytes(format: u32) -> u32 {
    match format {
        D3DFMT_DXT1 => 8,
        D3DFMT_DXT3 | D3DFMT_DXT5 => 16,
        _ => 0,
    }
}

/// True for the block-compressed formats this bridge stores and decodes.
pub const fn is_block_format(format: u32) -> bool {
    block_bytes(format) != 0
}

/// Effective block-row pitch and total byte size of one block-format level.
/// Width and height are texels; the last row/column of blocks is partial and
/// still stores a whole block, matching D3D8's `LockRect` pitch.
pub fn block_level_layout(width: u32, height: u32, format: u32) -> (u32, u32) {
    let bb = block_bytes(format);
    if bb == 0 {
        return (0, 0);
    }
    let pitch = ((width + 3) / 4) * bb;
    let size = pitch * ((height + 3) / 4);
    (pitch, size)
}

/// 5:6:5 to 8:8:8 by bit replication, the same rule the uncompressed 16-bit
/// path and the DirectDraw/D3D7 decoder use.
fn dxt_expand5(v: u32) -> u8 {
    ((v << 3) | (v >> 2)) as u8
}
fn dxt_expand6(v: u32) -> u8 {
    ((v << 2) | (v >> 4)) as u8
}

/// Decode the four-colour half of a DXT1/3/5 block. `always_four` selects the
/// DXT3/5 rule where the third and fourth colours are always interpolated;
/// DXT1's `c0 <= c1` mode leaves the fourth entry transparent.
fn decode_dxt_colour_block(p: &[u8], always_four: bool, out: &mut [[u8; 4]; 16]) {
    let c0 = u16::from_le_bytes([p[0], p[1]]);
    let c1 = u16::from_le_bytes([p[2], p[3]]);
    let r0 = dxt_expand5((c0 >> 11) as u32 & 0x1f);
    let g0 = dxt_expand6((c0 >> 5) as u32 & 0x3f);
    let b0 = dxt_expand5(c0 as u32 & 0x1f);
    let r1 = dxt_expand5((c1 >> 11) as u32 & 0x1f);
    let g1 = dxt_expand6((c1 >> 5) as u32 & 0x3f);
    let b1 = dxt_expand5(c1 as u32 & 0x1f);
    let mut pal = [[0u8; 4]; 4];
    pal[0] = [r0, g0, b0, 0xff];
    pal[1] = [r1, g1, b1, 0xff];
    if always_four || c0 > c1 {
        pal[2] = [
            ((2 * r0 as u32 + r1 as u32) / 3) as u8,
            ((2 * g0 as u32 + g1 as u32) / 3) as u8,
            ((2 * b0 as u32 + b1 as u32) / 3) as u8,
            0xff,
        ];
        pal[3] = [
            ((r0 as u32 + 2 * r1 as u32) / 3) as u8,
            ((g0 as u32 + 2 * g1 as u32) / 3) as u8,
            ((b0 as u32 + 2 * b1 as u32) / 3) as u8,
            0xff,
        ];
    } else {
        pal[2] = [
            ((r0 as u32 + r1 as u32) / 2) as u8,
            ((g0 as u32 + g1 as u32) / 2) as u8,
            ((b0 as u32 + b1 as u32) / 2) as u8,
            0xff,
        ];
        pal[3] = [0, 0, 0, 0]; // transparent in DXT1's three-colour mode
    }
    let bits = u32::from_le_bytes([p[4], p[5], p[6], p[7]]);
    for (i, entry) in out.iter_mut().enumerate() {
        *entry = pal[((bits >> (2 * i)) & 3) as usize];
    }
}

/// DXT3: sixteen 4-bit alpha values, then an always-four-colour block.
fn decode_dxt3(p: &[u8], out: &mut [[u8; 4]; 16]) {
    let mut colour = [[0u8; 4]; 16];
    decode_dxt_colour_block(&p[8..], true, &mut colour);
    for i in 0..16 {
        let a = (p[i >> 1] >> ((i & 1) * 4)) & 0xf;
        out[i] = [colour[i][0], colour[i][1], colour[i][2], (a << 4) | a];
    }
}

/// DXT5: two 8-bit alpha endpoints and 48 bits of 3-bit indices, then an
/// always-four-colour block.
fn decode_dxt5(p: &[u8], out: &mut [[u8; 4]; 16]) {
    let a0 = p[0];
    let a1 = p[1];
    let mut abits = 0u64;
    for i in 0..6 {
        abits |= (p[2 + i] as u64) << (8 * i);
    }
    let mut alpha = [0u8; 8];
    alpha[0] = a0;
    alpha[1] = a1;
    if a0 > a1 {
        for i in 1..=6u32 {
            alpha[i as usize + 1] =
                (((7 - i) as u32 * a0 as u32 + i * a1 as u32) / 7) as u8;
        }
    } else {
        for i in 1..=4u32 {
            alpha[i as usize + 1] =
                (((5 - i) as u32 * a0 as u32 + i * a1 as u32) / 5) as u8;
        }
        alpha[6] = 0x00;
        alpha[7] = 0xff;
    }
    let mut colour = [[0u8; 4]; 16];
    decode_dxt_colour_block(&p[8..], true, &mut colour);
    for i in 0..16 {
        let idx = ((abits >> (3 * i)) & 7) as usize;
        out[i] = [colour[i][0], colour[i][1], colour[i][2], alpha[idx]];
    }
}

/// Decode a whole block-format level into tightly packed `R,G,B,A` bytes at
/// `width * height * 4`. `data` must hold the complete block layout from
/// [`block_level_layout`]; the final partial block row/column is clipped, not
/// sampled. Unsupported formats are a named error.
pub fn decode_block_into(
    format: u32,
    data: &[u8],
    width: u32,
    height: u32,
    out: &mut Vec<u8>,
) -> Result<(), RenderError> {
    let bb = block_bytes(format);
    if bb == 0 {
        return Err(RenderError::new(
            "format::decode_block",
            format!("unsupported block D3DFORMAT {format}"),
        ));
    }
    if width == 0 || height == 0 {
        return Err(RenderError::invalid("format::decode_block", "zero dimension"));
    }
    let (pitch, size) = block_level_layout(width, height, format);
    if data.len() < size as usize {
        return Err(RenderError::invalid(
            "format::decode_block",
            "block level is shorter than its layout",
        ));
    }
    out.clear();
    out.resize((width as usize) * (height as usize) * 4, 0);
    let mut block = [[0u8; 4]; 16];
    let block_cols = (width + 3) / 4;
    let block_rows = (height + 3) / 4;
    for by in 0..block_rows {
        for bx in 0..block_cols {
            let off = (by * pitch + bx * bb) as usize;
            let p = &data[off..off + bb as usize];
            match format {
                D3DFMT_DXT1 => decode_dxt_colour_block(p, false, &mut block),
                D3DFMT_DXT3 => decode_dxt3(p, &mut block),
                D3DFMT_DXT5 => decode_dxt5(p, &mut block),
                _ => unreachable!("block format checked above"),
            }
            for py in 0..4 {
                let y = by * 4 + py;
                if y >= height {
                    break;
                }
                for px in 0..4 {
                    let x = bx * 4 + px;
                    if x >= width {
                        break;
                    }
                    let o = ((y * width + x) * 4) as usize;
                    out[o..o + 4].copy_from_slice(&block[(py * 4 + px) as usize]);
                }
            }
        }
    }
    debug_assert_eq!(out.len(), (width as usize) * (height as usize) * 4);
    Ok(())
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
    fn from_rgba8_into_round_trips_each_color_format() {
        // Every decodable texel must re-encode to the byte it came from.
        let cases: [(ColorFormat, Vec<u8>); 5] = [
            (
                ColorFormat::A8R8G8B8,
                vec![0x11, 0x22, 0x33, 0x44, 0xFF, 0x00, 0x80, 0x7F],
            ),
            (
                ColorFormat::X8R8G8B8,
                // X8 has no alpha: its decode/re-encode normalizes to 0xFF.
                vec![0x11, 0x22, 0x33, 0xFF, 0xFF, 0x00, 0x80, 0xFF],
            ),
            (
                ColorFormat::R5G6B5,
                vec![0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00],
            ),
            (
                ColorFormat::A1R5G5B5,
                vec![0x00, 0xFC, 0x1F, 0x00, 0x1F, 0x80],
            ),
            (
                ColorFormat::A4R4G4B4,
                vec![0x0F, 0x00, 0xF0, 0xFF, 0x12, 0x34],
            ),
        ];
        for (color, texels) in cases {
            let rgba = color.to_rgba8(texels.as_slice());
            let mut back = Vec::new();
            color.from_rgba8_into(&rgba, &mut back);
            assert_eq!(back, texels, "round trip for {color:?}");
        }
    }

    #[test]
    fn from_rgba8_into_truncates_channels_and_forces_opaque() {
        let mut out = Vec::new();
        ColorFormat::X8R8G8B8.from_rgba8_into(&[1, 2, 3, 4], &mut out);
        assert_eq!(out, [3, 2, 1, 0xFF]);
        ColorFormat::A8R8G8B8.from_rgba8_into(&[1, 2, 3, 4], &mut out);
        assert_eq!(out, [3, 2, 1, 4]);
        // 5/6-bit truncation keeps the high bits; the full-white texel is exact.
        ColorFormat::R5G6B5.from_rgba8_into(&[0xFF, 0xFF, 0xFF, 0xFF], &mut out);
        assert_eq!(out, [0xFF, 0xFF]);
        ColorFormat::A4R4G4B4.from_rgba8_into(&[0xFF, 0x00, 0xFF, 0x80], &mut out);
        assert_eq!(out, [0x0F, 0x8F]); // A=8, R=F, G=0, B=F
    }

    #[test]
    fn dxt1_decodes_four_and_three_colour_blocks() {
        // c0 = red (0xF800), c1 = blue (0x001F), c0 > c1: four colours.
        // Index bits 0b11_10_01_00 (packed per texel) select each palette entry.
        let mut data = vec![0u8; 8];
        data[0..2].copy_from_slice(&0xF800u16.to_le_bytes());
        data[2..4].copy_from_slice(&0x001Fu16.to_le_bytes());
        data[4..8].copy_from_slice(&0b0000_0000_1110_0100u32.to_le_bytes());
        let mut out = Vec::new();
        decode_block_into(D3DFMT_DXT1, &data, 4, 4, &mut out).unwrap();
        assert_eq!(&out[0..4], [0xFF, 0, 0, 0xFF]); // palette 0: red
        assert_eq!(&out[4..8], [0, 0, 0xFF, 0xFF]); // palette 1: blue
        // palette 2 = (2*red + blue)/3, palette 3 = (red + 2*blue)/3
        assert_eq!(&out[8..12], [(2 * 255 / 3) as u8, 0, 85, 0xFF]);
        assert_eq!(&out[12..16], [(255 / 3) as u8, 0, 170, 0xFF]);

        // c0 < c1: three-colour mode, palette entry 3 is transparent black.
        let mut data = vec![0u8; 8];
        data[0..2].copy_from_slice(&0x001Fu16.to_le_bytes());
        data[2..4].copy_from_slice(&0xF800u16.to_le_bytes());
        // texel 0 -> palette 0, texel 3 -> palette 3 (transparent).
        data[4..8].copy_from_slice(&0b0000_0000_1100_0000u32.to_le_bytes());
        let mut out = Vec::new();
        decode_block_into(D3DFMT_DXT1, &data, 4, 4, &mut out).unwrap();
        assert_eq!(&out[0..4], [0, 0, 0xFF, 0xFF]);
        assert_eq!(&out[12..16], [0, 0, 0, 0]);
    }

    #[test]
    fn dxt3_and_dxt5_decode_explicit_alpha() {
        // DXT3: a fully opaque alpha nibble pair then a white colour block.
        let mut data = vec![0u8; 16];
        data[0..8].fill(0xFF);
        data[8..10].copy_from_slice(&0xFFFFu16.to_le_bytes());
        data[10..12].copy_from_slice(&0xFFFFu16.to_le_bytes());
        let mut out = Vec::new();
        decode_block_into(D3DFMT_DXT3, &data, 4, 4, &mut out).unwrap();
        assert_eq!(&out[0..4], [0xFF, 0xFF, 0xFF, 0xFF]);

        // DXT5: a0 = 0xFF, a1 = 0x00, all index bits 0 -> alpha 0xFF.
        let mut data = vec![0u8; 16];
        data[0] = 0xFF;
        data[1] = 0x00;
        data[8..10].copy_from_slice(&0xFFFFu16.to_le_bytes());
        data[10..12].copy_from_slice(&0xFFFFu16.to_le_bytes());
        let mut out = Vec::new();
        decode_block_into(D3DFMT_DXT5, &data, 4, 4, &mut out).unwrap();
        assert_eq!(&out[0..4], [0xFF, 0xFF, 0xFF, 0xFF]);
    }

    #[test]
    fn block_layout_rounds_up_and_clips_partial_blocks() {
        assert_eq!(block_bytes(D3DFMT_DXT1), 8);
        assert_eq!(block_bytes(D3DFMT_DXT3), 16);
        assert_eq!(block_bytes(D3DFMT_DXT5), 16);
        assert!(is_block_format(D3DFMT_DXT1) && !is_block_format(D3DFMT_A8R8G8B8));
        // 6x6 is 2x2 blocks: DXT1 pitch 16, size 32.
        assert_eq!(block_level_layout(6, 6, D3DFMT_DXT1), (16, 32));
        let mut out = Vec::new();
        // Two blocks' worth of nonzero bytes; the 2x2 output only keeps 4x4 texels total.
        let data = vec![0xFFu8; 32];
        decode_block_into(D3DFMT_DXT1, &data, 6, 6, &mut out).unwrap();
        assert_eq!(out.len(), 6 * 6 * 4);
        assert!(decode_block_into(D3DFMT_DXT1, &data[..8], 6, 6, &mut out).is_err());
        assert!(decode_block_into(D3DFMT_A8R8G8B8, &data, 6, 6, &mut out).is_err());
    }

    #[test]
    fn block_levels_smaller_than_a_block_still_decode_one_block() {
        // A mip-tail level (2x2, then 1x1) still occupies one full 4x4 block in
        // the source layout; the RGBA output is clipped to the level's pixels.
        assert_eq!(block_level_layout(2, 2, D3DFMT_DXT1), (8, 8));
        assert_eq!(block_level_layout(1, 1, D3DFMT_DXT1), (8, 8));
        let data = vec![0xFFu8; 8];
        let mut out = Vec::new();
        decode_block_into(D3DFMT_DXT1, &data, 2, 2, &mut out).unwrap();
        assert_eq!(out.len(), 2 * 2 * 4);
        decode_block_into(D3DFMT_DXT1, &data, 1, 1, &mut out).unwrap();
        assert_eq!(out.len(), 1 * 1 * 4);
        // A too-short level is still refused.
        assert!(decode_block_into(D3DFMT_DXT1, &data[..4], 2, 2, &mut out).is_err());
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
