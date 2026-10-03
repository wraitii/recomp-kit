//! Direct3D 8 enumerations and bit flags.
//!
//! Values follow the pinned Wine 11.0 D3D8 headers (`reference/wine/d3d8types.h`),
//! not D3D9. Only the bounded slice needed by the unlit fixed-function probe is
//! modelled here: transform types, cull/compare/blend/fill/shade/Z state and the
//! FVF flags decoded by [`crate::d3d8::fixed_function`].
//!
//! Every raw `u32` coming from the guest/bridge must be converted with an
//! explicit fallible conversion; unknown values are a named error, never a
//! silent default.

use crate::RenderError;

/// Defines a fieldless enum whose variants are the raw D3D8 values, plus the
/// fallible `from_raw`/`raw` conversions shared by every enum in this module.
macro_rules! d3d_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $value:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub enum $name {
            $($variant = $value),+
        }

        impl $name {
            /// Convert a raw value. Unknown values (including the
            /// `*_FORCE_DWORD` sentinels) are a named error.
            pub fn from_raw(value: u32) -> Result<Self, RenderError> {
                match value {
                    $($value => Ok(Self::$variant),)+
                    other => Err(RenderError::new(
                        concat!("d3d8::enums::", stringify!($name)),
                        format!("unknown raw value {other:#010x}"),
                    )),
                }
            }

            /// Raw value as passed to the D3D8 API.
            pub fn raw(self) -> u32 {
                self as u32
            }
        }
    };
}

d3d_enum! {
    /// `D3DTRANSFORMSTATETYPE` (`d3d8types.h`).
    ///
    /// `D3DTS_WORLDMATRIX(index)` is `index + 256`; only indices 0..=3 are
    /// modelled, matching the fixed-function matrix palette used by D3D8.
    D3DTRANSFORMSTATETYPE {
        View = 2,
        Projection = 3,
        Texture0 = 16,
        Texture1 = 17,
        Texture2 = 18,
        Texture3 = 19,
        Texture4 = 20,
        Texture5 = 21,
        Texture6 = 22,
        Texture7 = 23,
        World = 256,
        World1 = 257,
        World2 = 258,
        World3 = 259,
    }
}

d3d_enum! {
    /// `D3DZBUFFERTYPE` (`D3DRS_ZENABLE`).
    D3DZBUFFERTYPE {
        False = 0,
        True = 1,
        UseW = 2,
    }
}

d3d_enum! {
    /// `D3DCULL` (`D3DRS_CULLMODE`).
    D3DCULL {
        None = 1,
        Cw = 2,
        Ccw = 3,
    }
}

d3d_enum! {
    /// `D3DFILLMODE` (`D3DRS_FILLMODE`).
    D3DFILLMODE {
        Point = 1,
        Wireframe = 2,
        Solid = 3,
    }
}

d3d_enum! {
    /// `D3DSHADEMODE` (`D3DRS_SHADEMODE`).
    D3DSHADEMODE {
        Flat = 1,
        Gouraud = 2,
        Phong = 3,
    }
}

d3d_enum! {
    /// `D3DCMPFUNC` (`D3DRS_ZFUNC`, `D3DRS_ALPHAFUNC`, ...).
    D3DCMPFUNC {
        Never = 1,
        Less = 2,
        Equal = 3,
        LessEqual = 4,
        Greater = 5,
        NotEqual = 6,
        GreaterEqual = 7,
        Always = 8,
    }
}

d3d_enum! {
    /// `D3DBLEND` (`D3DRS_SRCBLEND`, `D3DRS_DESTBLEND`).
    D3DBLEND {
        Zero = 1,
        One = 2,
        SrcColor = 3,
        InvSrcColor = 4,
        SrcAlpha = 5,
        InvSrcAlpha = 6,
        DestAlpha = 7,
        InvDestAlpha = 8,
        DestColor = 9,
        InvDestColor = 10,
        SrcAlphaSat = 11,
        BothSrcAlpha = 12,
        BothInvSrcAlpha = 13,
    }
}

d3d_enum! {
    /// `D3DBLENDOP` (`D3DRS_BLENDOP`).
    D3DBLENDOP {
        Add = 1,
        Subtract = 2,
        RevSubtract = 3,
        Min = 4,
        Max = 5,
    }
}

d3d_enum! {
    /// `D3DFOGMODE` (`D3DRS_FOGTABLEMODE`, `D3DRS_FOGVERTEXMODE`).
    /// Pixel/table fog uses EXP/EXP2/LINEAR; `None` selects vertex fog (or no
    /// fog at all when the other mode is also `None`).
    D3DFOGMODE {
        None = 0,
        Exp = 1,
        Exp2 = 2,
        Linear = 3,
    }
}

d3d_enum! {
    /// `D3DMATERIALCOLORSOURCE` (`D3DRS_*MATERIALSOURCE`).
    D3DMATERIALCOLORSOURCE {
        Material = 0,
        Color1 = 1,
        Color2 = 2,
    }
}

d3d_enum! {
    /// `D3DRENDERSTATETYPE` (`d3d8types.h`). The full enum so every valid
    /// state the guest sets is accepted; values are stored faithfully, and
    /// draw-time validation decides separately which states it can honour.
    D3DRENDERSTATETYPE {
        ZEnable = 7,
        FillMode = 8,
        ShadeMode = 9,
        LinePattern = 10,
        ZWriteEnable = 14,
        AlphaTestEnable = 15,
        LastPixel = 16,
        SrcBlend = 19,
        DestBlend = 20,
        CullMode = 22,
        ZFunc = 23,
        AlphaRef = 24,
        AlphaFunc = 25,
        DitherEnable = 26,
        AlphaBlendEnable = 27,
        FogEnable = 28,
        SpecularEnable = 29,
        ZVisible = 30,
        FogColor = 34,
        FogTableMode = 35,
        FogStart = 36,
        FogEnd = 37,
        FogDensity = 38,
        EdgeAntiAlias = 40,
        ZBias = 47,
        RangeFogEnable = 48,
        StencilEnable = 52,
        StencilFail = 53,
        StencilZFail = 54,
        StencilPass = 55,
        StencilFunc = 56,
        StencilRef = 57,
        StencilMask = 58,
        StencilWriteMask = 59,
        TextureFactor = 60,
        Wrap0 = 128,
        Wrap1 = 129,
        Wrap2 = 130,
        Wrap3 = 131,
        Wrap4 = 132,
        Wrap5 = 133,
        Wrap6 = 134,
        Wrap7 = 135,
        Clipping = 136,
        Lighting = 137,
        Ambient = 139,
        FogVertexMode = 140,
        ColorVertex = 141,
        LocalViewer = 142,
        NormalizeNormals = 143,
        DiffuseMaterialSource = 145,
        SpecularMaterialSource = 146,
        AmbientMaterialSource = 147,
        EmissiveMaterialSource = 148,
        VertexBlend = 151,
        ClipPlaneEnable = 152,
        SoftwareVertexProcessing = 153,
        PointSize = 154,
        PointSizeMin = 155,
        PointSpriteEnable = 156,
        PointScaleEnable = 157,
        PointScaleA = 158,
        PointScaleB = 159,
        PointScaleC = 160,
        MultiSampleAntiAlias = 161,
        MultiSampleMask = 162,
        PatchEdgeStyle = 163,
        PatchSegments = 164,
        DebugMonitorToken = 165,
        PointSizeMax = 166,
        IndexedVertexBlendEnable = 167,
        ColorWriteEnable = 168,
        TweenFactor = 170,
        BlendOp = 171,
        PositionOrder = 172,
        NormalOrder = 173,
    }
}

d3d_enum! {
    /// `D3DTEXTUREOP` (`D3DTSS_COLOROP`/`D3DTSS_ALPHAOP`). The full enum so
    /// every value the guest sets is recognised; the textured draw path
    /// honours a bounded subset and names the rest.
    D3DTEXTUREOP {
        Disable = 1,
        SelectArg1 = 2,
        SelectArg2 = 3,
        Modulate = 4,
        Modulate2x = 5,
        Modulate4x = 6,
        Add = 7,
        AddSigned = 8,
        AddSigned2x = 9,
        Subtract = 10,
        AddSmooth = 11,
        BlendDiffuseAlpha = 12,
        BlendTextureAlpha = 13,
        BlendFactorAlpha = 14,
        BlendTextureAlphaPm = 15,
        BlendCurrentAlpha = 16,
        PreModulate = 17,
        ModulateAlphaAddColor = 18,
        ModulateColorAddAlpha = 19,
        ModulateInvAlphaAddColor = 20,
        ModulateInvColorAddAlpha = 21,
        BumpEnvMap = 22,
        BumpEnvMapLuminance = 23,
        DotProduct3 = 24,
        MultiplyAdd = 25,
        Lerp = 26,
    }
}
d3d_enum! {
    /// `D3DTEXTUREFILTERTYPE` (`D3DTSS_MINFILTER`/`MAGFILTER`/`MIPFILTER`).
    D3DTEXTUREFILTERTYPE {
        None = 0,
        Point = 1,
        Linear = 2,
        Anisotropic = 3,
        PyramidQuad = 6,
        GaussianQuad = 7,
    }
}
d3d_enum! {
    /// `D3DTEXTUREADDRESS` (`D3DTSS_ADDRESSU`/`ADDRESSV`/`ADDRESSW`).
    D3DTEXTUREADDRESS {
        Wrap = 1,
        Mirror = 2,
        Clamp = 3,
        Border = 4,
        MirrorOnce = 5,
    }
}

/// `D3DTA_*` texture-stage argument selectors (`D3DTSS_COLORARG1`/...).
/// The low nibble is the selector and the high bits are modifiers; only the
/// selector is modelled and a non-zero modifier is refused by name.
pub mod d3dta {
    pub const SELECTMASK: u32 = 0x0000_000f;
    pub const DIFFUSE: u32 = 0;
    pub const CURRENT: u32 = 1;
    pub const TEXTURE: u32 = 2;
    pub const TFACTOR: u32 = 3;
    pub const SPECULAR: u32 = 4;
    pub const TEMP: u32 = 5;
    /// `D3DTA_COMPLEMENT`: use `1 - value`.
    pub const COMPLEMENT: u32 = 0x0000_0010;
    /// `D3DTA_ALPHAREPLICATE`: replicate the alpha channel into RGB.
    pub const ALPHAREPLICATE: u32 = 0x0000_0020;
}

d3d_enum! {
    /// `D3DTEXTURESTAGESTATETYPE` (`d3d8types.h`). Full enum; values are
    /// stored per stage and interpreted by the fixed-function path later.
    D3DTEXTURESTAGESTATETYPE {
        ColorOp = 1,
        ColorArg1 = 2,
        ColorArg2 = 3,
        AlphaOp = 4,
        AlphaArg1 = 5,
        AlphaArg2 = 6,
        BumpEnvMat00 = 7,
        BumpEnvMat01 = 8,
        BumpEnvMat10 = 9,
        BumpEnvMat11 = 10,
        TexCoordIndex = 11,
        AddressU = 13,
        AddressV = 14,
        BorderColor = 15,
        MagFilter = 16,
        MinFilter = 17,
        MipFilter = 18,
        MipMapLodBias = 19,
        MaxMipLevel = 20,
        MaxAnisotropy = 21,
        BumpEnvLScale = 22,
        BumpEnvLOffset = 23,
        TextureTransformFlags = 24,
        AddressW = 25,
        ColorArg0 = 26,
        AlphaArg0 = 27,
        ResultArg = 28,
    }
}

// Flexible vertex format flags (`D3DFVF_*`, `d3d8types.h`). Decoding and layout
// belong to `fixed_function`; these are the raw header values.
/// Vertex position, 3 floats. Required for all supported probe geometry.
pub const D3DFVF_XYZ: u32 = 0x0002;
/// Position already in screen space (unsupported by the fixed-function slice).
pub const D3DFVF_XYZRHW: u32 = 0x0004;
/// Vertex normal (lighting-only; unsupported by the current slice).
pub const D3DFVF_NORMAL: u32 = 0x0010;
/// Per-vertex diffuse colour, `D3DCOLOR` (ARGB DWORD).
pub const D3DFVF_DIFFUSE: u32 = 0x0040;
/// Per-vertex specular colour (lighting-only; unsupported).
pub const D3DFVF_SPECULAR: u32 = 0x0080;
/// Texture-coordinate-count mask in the low byte of the high word.
pub const D3DFVF_TEXCOUNT_MASK: u32 = 0x0f00;
/// Right shift for the texture-coordinate count.
pub const D3DFVF_TEXCOUNT_SHIFT: u32 = 8;
/// One set of texture coordinates.
pub const D3DFVF_TEX1: u32 = 0x0100;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn raw_values_match_wine_headers() {
        assert_eq!(D3DZBUFFERTYPE::False.raw(), 0);
        assert_eq!(D3DZBUFFERTYPE::True.raw(), 1);
        assert_eq!(D3DZBUFFERTYPE::UseW.raw(), 2);
        assert_eq!(D3DCULL::None.raw(), 1);
        assert_eq!(D3DCULL::Ccw.raw(), 3);
        assert_eq!(D3DFILLMODE::Solid.raw(), 3);
        assert_eq!(D3DSHADEMODE::Gouraud.raw(), 2);
        assert_eq!(D3DCMPFUNC::LessEqual.raw(), 4);
        assert_eq!(D3DBLEND::One.raw(), 2);
        assert_eq!(D3DBLEND::Zero.raw(), 1);
        assert_eq!(D3DBLENDOP::Add.raw(), 1);
        assert_eq!(D3DMATERIALCOLORSOURCE::Color1.raw(), 1);
        assert_eq!(D3DTRANSFORMSTATETYPE::View.raw(), 2);
        assert_eq!(D3DTRANSFORMSTATETYPE::Projection.raw(), 3);
        assert_eq!(D3DTRANSFORMSTATETYPE::World.raw(), 256);
        assert_eq!(D3DTRANSFORMSTATETYPE::Texture0.raw(), 16);
    }

    #[test]
    fn from_raw_accepts_all_defined_values() {
        assert_eq!(D3DCULL::from_raw(1).unwrap(), D3DCULL::None);
        assert_eq!(D3DCULL::from_raw(3).unwrap(), D3DCULL::Ccw);
        assert_eq!(D3DCMPFUNC::from_raw(8).unwrap(), D3DCMPFUNC::Always);
        assert_eq!(
            D3DTRANSFORMSTATETYPE::from_raw(257).unwrap(),
            D3DTRANSFORMSTATETYPE::World1
        );
    }

    #[test]
    fn from_raw_rejects_unknown_and_sentinels() {
        assert!(D3DCULL::from_raw(0).is_err());
        assert!(D3DCULL::from_raw(4).is_err());
        assert!(D3DCULL::from_raw(0x7fff_ffff).is_err());
        assert!(D3DZBUFFERTYPE::from_raw(3).is_err());
        assert!(D3DCMPFUNC::from_raw(0xffff_ffff).is_err());
        assert!(D3DTRANSFORMSTATETYPE::from_raw(888).is_err());
    }

    #[test]
    fn error_names_the_enum() {
        let err = D3DCULL::from_raw(99).unwrap_err();
        assert_eq!(err.operation, "d3d8::enums::D3DCULL");
        assert!(err.cause.contains("0x00000063"), "{}", err.cause);
    }
}
