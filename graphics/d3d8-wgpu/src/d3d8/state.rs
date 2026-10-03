//! Device state: transforms, viewport and the bounded render-state set.
//!
//! D3D8 defaults must be modelled exactly (for example `D3DRS_LIGHTING = TRUE`,
//! `D3DRS_ZFUNC = D3DCMP_LESSEQUAL`, `D3DRS_CULLMODE = D3DCULL_CCW`). Defaults
//! are the real D3D8 values, so the unlit probe must explicitly disable lighting
//! and depth and select `D3DCULL_NONE`.
//!
//! [`DeviceState::validate_unlit`] rejects a draw that reaches an unsupported
//! combination *before* submission. The 0x152 path implements diffuse, ambient
//! and emissive vertex lighting. Vertex/range fog, dithering, specular adds
//! and stencil remain unsupported. Table fog, alpha test and ordinary depth
//! are supported.

use crate::RenderError;
use crate::d3d8::enums::{
    D3DBLEND, D3DBLENDOP, D3DCMPFUNC, D3DCULL, D3DFILLMODE, D3DFOGMODE, D3DMATERIALCOLORSOURCE,
    D3DRENDERSTATETYPE, D3DSHADEMODE, D3DTEXTUREADDRESS, D3DTEXTUREFILTERTYPE, D3DTEXTUREOP,
    D3DTEXTURESTAGESTATETYPE, D3DZBUFFERTYPE, d3dta,
};
use crate::d3d8::math::Mat4;
use std::collections::BTreeMap;

/// D3D8 exposes eight fixed-function texture stages.
pub const MAX_TEXTURE_STAGES: usize = 8;

/// Highest stage index the draw path implements. Stages above this that are
/// active fail by name rather than being silently dropped.
pub const MAX_SUPPORTED_TEXTURE_STAGES: u32 = 2;

/// A resolved texture stage, ready for the pipeline/shader.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextureStage {
    /// True when the stage samples/combines; false keeps the untextured path.
    pub active: bool,
    pub color_op: u32,
    pub color_arg1: u32,
    pub color_arg2: u32,
    pub alpha_op: u32,
    pub alpha_arg1: u32,
    pub alpha_arg2: u32,
    /// `D3DRS_TEXTUREFACTOR` (raw ARGB `D3DCOLOR`) supplied to `D3DTA_TFACTOR`.
    pub texture_factor: u32,
    /// `D3DTSS_TEXCOORDINDEX`: which vertex texture-coordinate set to sample
    /// (`0` or `1`). The guest's three TSS functions only emit plain `0`/`1`.
    pub tex_coord_index: u32,
    pub min_filter: u32,
    pub mag_filter: u32,
    pub mip_filter: u32,
    pub address_u: u32,
    pub address_v: u32,
}

/// A fixed-function device exposes eight active lights (`D3DCAPS8::MaxActiveLights`).
pub const MAX_LIGHTS: usize = 8;

/// A `D3DMATERIAL8` channel: four floats in `r, g, b, a` order.
pub type ColorValue = [f32; 4];

/// A `D3DMATERIAL8` with the same field order and widths. The bridge copies
/// the guest's 68-byte record verbatim; the renderer never reinterprets it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Material {
    pub diffuse: ColorValue,
    pub ambient: ColorValue,
    pub specular: ColorValue,
    pub emissive: ColorValue,
    pub power: f32,
}

impl Material {
    /// The documented D3D8 default material: opaque white diffuse, everything
    /// else zero. Lighting is enabled by default, so this is the material the
    /// fixed-function path would use before the guest calls `SetMaterial`.
    pub fn d3d_default() -> Material {
        Material {
            diffuse: [1.0, 1.0, 1.0, 1.0],
            ambient: [0.0; 4],
            specular: [0.0; 4],
            emissive: [0.0; 4],
            power: 0.0,
        }
    }
}

/// A `D3DLIGHT8` with the same field order, widths and 104-byte layout. `Type`
/// is the raw `D3DLIGHTTYPE`; it is not resolved until the fixed-function
/// lighting path exists. `position`/`direction` are `D3DVECTOR` (3 floats).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Light {
    pub light_type: u32,
    pub diffuse: ColorValue,
    pub specular: ColorValue,
    pub ambient: ColorValue,
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

impl Default for Light {
    /// wined3d zero-initializes the eight device light slots; `GetLight`
    /// returns those zeros before any `SetLight`. The renderer refuses to use
    /// a light until a fixed-function path can honour it, so this default is
    /// only observable through `GetLight`.
    fn default() -> Light {
        Light {
            light_type: 0,
            diffuse: [0.0; 4],
            specular: [0.0; 4],
            ambient: [0.0; 4],
            position: [0.0; 3],
            direction: [0.0; 3],
            range: 0.0,
            falloff: 0.0,
            attenuation0: 0.0,
            attenuation1: 0.0,
            attenuation2: 0.0,
            theta: 0.0,
            phi: 0.0,
        }
    }
}

/// A `D3DVIEWPORT8` with the same field widths and ordering.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Viewport {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
    pub min_z: f32,
    pub max_z: f32,
}

impl Viewport {
    /// The D3D8 device-creation viewport: the full target with `min_z = 0`,
    /// `max_z = 1`.
    pub fn full(width: u32, height: u32) -> Viewport {
        Viewport {
            x: 0,
            y: 0,
            width,
            height,
            min_z: 0.0,
            max_z: 1.0,
        }
    }
}

/// The modelled slice of `D3DRENDERSTATETYPE`, initialised to the documented
/// D3D8 defaults (Wine `d3d8` device defaults). Fields are private because
/// callers must go through [`DeviceState::set_render_state`] (fallible raw
/// conversion) and [`DeviceState::validate_unlit`].
#[derive(Clone, Debug, PartialEq)]
struct RenderStates {
    z_enable: D3DZBUFFERTYPE,
    fill_mode: D3DFILLMODE,
    shade_mode: D3DSHADEMODE,
    z_write_enable: bool,
    alpha_test_enable: bool,
    src_blend: D3DBLEND,
    dest_blend: D3DBLEND,
    cull_mode: D3DCULL,
    z_func: D3DCMPFUNC,
    alpha_ref: u32,
    alpha_func: D3DCMPFUNC,
    dither_enable: bool,
    alpha_blend_enable: bool,
    fog_enable: bool,
    specular_enable: bool,
    fog_color: u32,
    /// Raw `D3DFOGMODE` for pixel/table fog (`D3DRS_FOGTABLEMODE`).
    fog_table_mode: u32,
    /// Raw `D3DFOGMODE` for vertex fog (`D3DRS_FOGVERTEXMODE`).
    fog_vertex_mode: u32,
    /// `D3DRS_FOGSTART` as the raw float bit pattern.
    fog_start: u32,
    /// `D3DRS_FOGEND` as the raw float bit pattern.
    fog_end: u32,
    /// `D3DRS_FOGDENSITY` as the raw float bit pattern.
    fog_density: u32,
    /// `D3DRS_TEXTUREFACTOR` (raw `D3DCOLOR`, ARGB). Default white.
    texture_factor: u32,
    range_fog_enable: bool,
    stencil_enable: bool,
    clipping: bool,
    lighting: bool,
    ambient: u32,
    color_vertex: bool,
    local_viewer: bool,
    normalize_normals: bool,
    diffuse_material_source: D3DMATERIALCOLORSOURCE,
    specular_material_source: D3DMATERIALCOLORSOURCE,
    ambient_material_source: D3DMATERIALCOLORSOURCE,
    emissive_material_source: D3DMATERIALCOLORSOURCE,
    blend_op: D3DBLENDOP,
    /// Every valid `D3DRS_*` not represented by a typed field above, exactly
    /// as the guest set it. Draw validation reads the typed fields; this map
    /// keeps the full render state faithful for later fixed-function work.
    other: BTreeMap<u32, u32>,
}

impl RenderStates {
    /// Documented D3D8 defaults. See the D3D8 `D3DRENDERSTATETYPE` reference.
    fn d3d_defaults() -> RenderStates {
        RenderStates {
            z_enable: D3DZBUFFERTYPE::True,
            fill_mode: D3DFILLMODE::Solid,
            shade_mode: D3DSHADEMODE::Gouraud,
            z_write_enable: true,
            alpha_test_enable: false,
            src_blend: D3DBLEND::One,
            dest_blend: D3DBLEND::Zero,
            cull_mode: D3DCULL::Ccw,
            z_func: D3DCMPFUNC::LessEqual,
            alpha_ref: 0,
            alpha_func: D3DCMPFUNC::Always,
            dither_enable: false,
            alpha_blend_enable: false,
            fog_enable: false,
            specular_enable: false,
            fog_color: 0,
            fog_table_mode: D3DFOGMODE::None.raw(),
            fog_vertex_mode: D3DFOGMODE::None.raw(),
            // Documented D3D8 defaults: FOGSTART = 0.0, FOGEND = FOGDENSITY = 1.0.
            fog_start: 0.0f32.to_bits(),
            fog_end: 1.0f32.to_bits(),
            fog_density: 1.0f32.to_bits(),
            // D3DRS_TEXTUREFACTOR defaults to opaque white.
            texture_factor: 0xFFFF_FFFF,
            range_fog_enable: false,
            stencil_enable: false,
            clipping: true,
            lighting: true,
            ambient: 0,
            color_vertex: true,
            local_viewer: true,
            normalize_normals: false,
            diffuse_material_source: D3DMATERIALCOLORSOURCE::Color1,
            specular_material_source: D3DMATERIALCOLORSOURCE::Color2,
            ambient_material_source: D3DMATERIALCOLORSOURCE::Material,
            emissive_material_source: D3DMATERIALCOLORSOURCE::Material,
            blend_op: D3DBLENDOP::Add,
            other: BTreeMap::new(),
        }
    }
}

/// CPU-owned D3D8 device state for the unlit probe slice.
///
/// Transforms are public because the coordinator contract fixes them as direct
/// fields; every raw render state goes through the fallible setter.
#[derive(Clone, Debug, PartialEq)]
pub struct DeviceState {
    pub world: Mat4,
    pub view: Mat4,
    pub projection: Mat4,
    pub viewport: Viewport,
    states: RenderStates,
    /// Raw `D3DTSS_*` values per texture stage, set through the fallible
    /// setter. D3D8 exposes eight fixed-function texture stages.
    texture_stages: Vec<BTreeMap<u32, u32>>,
    /// Current `D3DMATERIAL8`, consumed by the 0x152 vertex-lighting path.
    material: Material,
    /// The eight `D3DLIGHT8` slots and their enable flags, indexed as the
    /// guest's `SetLight`/`LightEnable`, consumed by vertex lighting.
    lights: [Light; MAX_LIGHTS],
    light_enabled: [bool; MAX_LIGHTS],
}

impl DeviceState {
    /// New device state at the given target size: identity transforms, the
    /// full-target viewport, and exact D3D8 render-state defaults.
    pub fn new(width: u32, height: u32) -> DeviceState {
        DeviceState {
            world: Mat4::IDENTITY,
            view: Mat4::IDENTITY,
            projection: Mat4::IDENTITY,
            viewport: Viewport::full(width, height),
            states: RenderStates::d3d_defaults(),
            texture_stages: vec![BTreeMap::new(); MAX_TEXTURE_STAGES],
            material: Material::d3d_default(),
            lights: [Light::default(); MAX_LIGHTS],
            light_enabled: [false; MAX_LIGHTS],
        }
    }

    /// `IDirect3DDevice8::SetMaterial`. The full record is stored verbatim.
    pub fn set_material(&mut self, material: Material) {
        self.material = material;
    }

    /// `IDirect3DDevice8::GetMaterial`.
    pub fn material(&self) -> Material {
        self.material
    }

    /// `IDirect3DDevice8::SetLight`. The index must be below
    /// [`MAX_LIGHTS`]; out-of-range is a named error, as in D3D8.
    pub fn set_light(&mut self, index: u32, light: Light) -> Result<(), RenderError> {
        let slot = self.light_slot(index, "set_light")?;
        self.lights[slot] = light;
        Ok(())
    }

    /// `IDirect3DDevice8::GetLight`.
    pub fn light(&self, index: u32) -> Result<Light, RenderError> {
        let slot = self.light_slot(index, "get_light")?;
        Ok(self.lights[slot])
    }

    /// `IDirect3DDevice8::LightEnable`.
    pub fn light_enable(&mut self, index: u32, enable: bool) -> Result<(), RenderError> {
        let slot = self.light_slot(index, "light_enable")?;
        self.light_enabled[slot] = enable;
        Ok(())
    }

    /// `IDirect3DDevice8::GetLightEnable`.
    pub fn light_enabled(&self, index: u32) -> Result<bool, RenderError> {
        let slot = self.light_slot(index, "get_light_enable")?;
        Ok(self.light_enabled[slot])
    }

    /// Bounds-check a light index against [`MAX_LIGHTS`].
    fn light_slot(&self, index: u32, op: &'static str) -> Result<usize, RenderError> {
        let slot = index as usize;
        if slot >= MAX_LIGHTS {
            return Err(RenderError::new(
                op,
                format!("light index {index} is beyond D3D8's {MAX_LIGHTS} active lights"),
            ));
        }
        Ok(slot)
    }

    /// Apply one `D3DRS_*` state.
    ///
    /// Fails on a state outside the modelled slice, on an enum value outside the
    /// D3D8 range, and on a boolean that is not exactly `0`/`1` (the model does
    /// not guess at non-portable truthiness).
    pub fn set_render_state(&mut self, state: u32, value: u32) -> Result<(), RenderError> {
        use D3DRENDERSTATETYPE as Rs;
        match Rs::from_raw(state)? {
            Rs::ZEnable => self.states.z_enable = D3DZBUFFERTYPE::from_raw(value)?,
            Rs::FillMode => self.states.fill_mode = D3DFILLMODE::from_raw(value)?,
            Rs::ShadeMode => self.states.shade_mode = D3DSHADEMODE::from_raw(value)?,
            Rs::ZWriteEnable => {
                self.states.z_write_enable = bool_state(Rs::ZWriteEnable, value)?;
            }
            Rs::AlphaTestEnable => {
                self.states.alpha_test_enable = bool_state(Rs::AlphaTestEnable, value)?;
            }
            Rs::SrcBlend => self.states.src_blend = D3DBLEND::from_raw(value)?,
            Rs::DestBlend => self.states.dest_blend = D3DBLEND::from_raw(value)?,
            Rs::CullMode => self.states.cull_mode = D3DCULL::from_raw(value)?,
            Rs::ZFunc => self.states.z_func = D3DCMPFUNC::from_raw(value)?,
            Rs::AlphaRef => self.states.alpha_ref = value,
            Rs::AlphaFunc => self.states.alpha_func = D3DCMPFUNC::from_raw(value)?,
            Rs::DitherEnable => {
                self.states.dither_enable = bool_state(Rs::DitherEnable, value)?;
            }
            Rs::AlphaBlendEnable => {
                self.states.alpha_blend_enable = bool_state(Rs::AlphaBlendEnable, value)?;
            }
            Rs::FogEnable => self.states.fog_enable = bool_state(Rs::FogEnable, value)?,
            Rs::SpecularEnable => {
                self.states.specular_enable = bool_state(Rs::SpecularEnable, value)?;
            }
            Rs::FogColor => self.states.fog_color = value,
            Rs::FogTableMode => {
                self.states.fog_table_mode = D3DFOGMODE::from_raw(value)?.raw();
            }
            Rs::FogVertexMode => {
                self.states.fog_vertex_mode = D3DFOGMODE::from_raw(value)?.raw();
            }
            Rs::FogStart => self.states.fog_start = value,
            Rs::FogEnd => self.states.fog_end = value,
            Rs::FogDensity => self.states.fog_density = value,
            Rs::TextureFactor => self.states.texture_factor = value,
            Rs::RangeFogEnable => {
                self.states.range_fog_enable = bool_state(Rs::RangeFogEnable, value)?;
            }
            Rs::StencilEnable => {
                self.states.stencil_enable = bool_state(Rs::StencilEnable, value)?;
            }
            Rs::Clipping => self.states.clipping = bool_state(Rs::Clipping, value)?,
            Rs::Lighting => self.states.lighting = bool_state(Rs::Lighting, value)?,
            Rs::Ambient => self.states.ambient = value,
            Rs::ColorVertex => self.states.color_vertex = bool_state(Rs::ColorVertex, value)?,
            Rs::LocalViewer => self.states.local_viewer = bool_state(Rs::LocalViewer, value)?,
            Rs::NormalizeNormals => {
                self.states.normalize_normals = bool_state(Rs::NormalizeNormals, value)?;
            }
            Rs::DiffuseMaterialSource => {
                self.states.diffuse_material_source = D3DMATERIALCOLORSOURCE::from_raw(value)?;
            }
            Rs::SpecularMaterialSource => {
                self.states.specular_material_source = D3DMATERIALCOLORSOURCE::from_raw(value)?;
            }
            Rs::AmbientMaterialSource => {
                self.states.ambient_material_source = D3DMATERIALCOLORSOURCE::from_raw(value)?;
            }
            Rs::EmissiveMaterialSource => {
                self.states.emissive_material_source = D3DMATERIALCOLORSOURCE::from_raw(value)?;
            }
            Rs::BlendOp => self.states.blend_op = D3DBLENDOP::from_raw(value)?,
            // Any other valid D3DRS_* is stored verbatim; it is not applied by
            // the current draw path, but it must not abort a state-setting call.
            _ => {
                self.states.other.insert(state, value);
            }
        }
        Ok(())
    }

    /// Apply one `D3DTSS_*` state to a texture stage.
    ///
    /// Validates the stage index and the state id; the raw value is stored
    /// faithfully. Draw execution decides later which stage states it can
    /// honour.
    pub fn set_texture_stage_state(
        &mut self,
        stage: u32,
        state: u32,
        value: u32,
    ) -> Result<(), RenderError> {
        if stage as usize >= MAX_TEXTURE_STAGES {
            return Err(RenderError::new(
                "d3d8::state::set_texture_stage_state",
                format!("stage {stage} is beyond D3D8's {MAX_TEXTURE_STAGES} texture stages"),
            ));
        }
        D3DTEXTURESTAGESTATETYPE::from_raw(state)?;
        self.texture_stages[stage as usize].insert(state, value);
        Ok(())
    }

    /// The raw value of one texture-stage state, or `None` when never set.
    pub fn texture_stage_state(&self, stage: u32, state: u32) -> Option<u32> {
        self.texture_stages
            .get(stage as usize)
            .and_then(|states| states.get(&state).copied())
    }

    /// The raw value of an unmodelled render state, or `None` when never set.
    pub fn raw_render_state(&self, state: u32) -> Option<u32> {
        self.states.other.get(&state).copied()
    }

    /// Resolve one texture stage's fixed-function state into the bounded
    /// configuration the textured draw path can honour.
    ///
    /// `texture_bound` is true when the device has a texture bound to this
    /// stage. D3D8's stage-0 defaults are `COLOROP=MODULATE`,
    /// `COLORARG1=TEXTURE`, `COLORARG2=CURRENT`, `ALPHAOP=SELECTARG1`,
    /// `ALPHAARG1=TEXTURE`, filters `POINT`/`POINT`/`NONE` and address `WRAP`.
    /// The bridge only treats a stage as *active* when the guest set an op
    /// explicitly or a texture is bound, so an untextured FVF that never
    /// touched a stage keeps its existing untextured path.
    ///
    /// Stages 0 and 1 are implemented. The op set is the one the guest's TSS
    /// setup functions (`FUN_007c2940`, `FUN_007c2c70`, `FUN_007c2f80` and the
    /// sibling `FUN_007c3c80`/`FUN_007c3730`/`FUN_007c3f80`) actually emit:
    /// `DISABLE`, `SELECTARG1`, `SELECTARG2`, `MODULATE`, `MODULATE2X`,
    /// `MODULATE4X`, `ADD`, `ADDSIGNED`, `ADDSIGNED2X`, `SUBTRACT` and the four
    /// `BLEND*ALPHA` forms, with `DIFFUSE`/`CURRENT`/`TEXTURE`/`TFACTOR` and the
    /// `COMPLEMENT`/`ALPHAREPLICATE` modifiers. `CURRENT` at stage 0 is the
    /// diffuse colour and at stage 1 the stage-0 result (handled by the
    /// shader). Every other op, argument, filter or address mode fails by name,
    /// including an active stage >= 2.
    pub fn resolve_texture_stage(
        &self,
        stage: u32,
        texture_bound: bool,
    ) -> Result<TextureStage, RenderError> {
        use D3DTEXTURESTAGESTATETYPE as Ts;
        use d3dta;
        let fail = |what: String| RenderError::new("d3d8::state::resolve_texture_stage", what);
        if stage as usize >= MAX_TEXTURE_STAGES {
            return Err(fail(format!(
                "stage {stage} is beyond {MAX_TEXTURE_STAGES}"
            )));
        }
        let raw = |state: Ts, default: u32| {
            self.texture_stage_state(stage, state.raw())
                .unwrap_or(default)
        };
        let explicit_color = self.texture_stage_state(stage, Ts::ColorOp.raw());
        let explicit_alpha = self.texture_stage_state(stage, Ts::AlphaOp.raw());
        let color_op = raw(Ts::ColorOp, D3DTEXTUREOP::Modulate.raw());
        let alpha_op = raw(Ts::AlphaOp, D3DTEXTUREOP::SelectArg1.raw());
        // A stage is active when an op is explicitly set to something other
        // than DISABLE, or when a texture is bound and the resolved default/op
        // actually combines it. With no explicit op and no bound texture the
        // stage stays on the established untextured path.
        let active = if explicit_color.is_some() || explicit_alpha.is_some() || texture_bound {
            color_op != D3DTEXTUREOP::Disable.raw() || alpha_op != D3DTEXTUREOP::Disable.raw()
        } else {
            false
        };
        if stage >= MAX_SUPPORTED_TEXTURE_STAGES && active {
            return Err(fail(format!(
                "texture stage {stage} is active but only stages 0 and 1 are implemented"
            )));
        }
        let op = |name: &str, value: u32| -> Result<(), RenderError> {
            match D3DTEXTUREOP::from_raw(value)? {
                D3DTEXTUREOP::Disable
                | D3DTEXTUREOP::SelectArg1
                | D3DTEXTUREOP::SelectArg2
                | D3DTEXTUREOP::Modulate
                | D3DTEXTUREOP::Modulate2x
                | D3DTEXTUREOP::Modulate4x
                | D3DTEXTUREOP::Add
                | D3DTEXTUREOP::AddSigned
                | D3DTEXTUREOP::AddSigned2x
                | D3DTEXTUREOP::Subtract
                | D3DTEXTUREOP::BlendDiffuseAlpha
                | D3DTEXTUREOP::BlendTextureAlpha
                | D3DTEXTUREOP::BlendFactorAlpha
                | D3DTEXTUREOP::BlendCurrentAlpha => Ok(()),
                other => Err(fail(format!(
                    "{name} = {other:?} is not in the implemented subset"
                ))),
            }
        };
        op("D3DTSS_COLOROP", color_op)?;
        op("D3DTSS_ALPHAOP", alpha_op)?;
        let arg = |name: &str, value: u32| -> Result<u32, RenderError> {
            let modifiers = value & !d3dta::SELECTMASK;
            if modifiers & !(d3dta::COMPLEMENT | d3dta::ALPHAREPLICATE) != 0 {
                return Err(fail(format!(
                    "{name} = {value:#010x} uses an argument modifier other than COMPLEMENT/ALPHAREPLICATE"
                )));
            }
            match value & d3dta::SELECTMASK {
                d3dta::DIFFUSE | d3dta::CURRENT | d3dta::TEXTURE | d3dta::TFACTOR => Ok(value),
                other => Err(fail(format!(
                    "{name} = {other:#x} is not DIFFUSE/CURRENT/TEXTURE/TFACTOR"
                ))),
            }
        };
        let color_arg1 = arg("D3DTSS_COLORARG1", raw(Ts::ColorArg1, d3dta::TEXTURE))?;
        let color_arg2 = arg("D3DTSS_COLORARG2", raw(Ts::ColorArg2, d3dta::CURRENT))?;
        let alpha_arg1 = arg("D3DTSS_ALPHAARG1", raw(Ts::AlphaArg1, d3dta::TEXTURE))?;
        let alpha_arg2 = arg("D3DTSS_ALPHAARG2", raw(Ts::AlphaArg2, d3dta::CURRENT))?;
        let tex_coord_index = raw(Ts::TexCoordIndex, 0);
        // Only plain vertex-coordinate set 0/1 is emitted. D3D8 packs
        // camera-space generation modes in the high bits (`D3DTSS_TCI_*`);
        // those are a named refusal, not a silent set selection.
        if tex_coord_index > 1 {
            return Err(fail(format!(
                "D3DTSS_TEXCOORDINDEX = {tex_coord_index:#010x} needs a texture-coordinate index/transform that is not implemented"
            )));
        }
        // `D3DTSS_RESULTARG` is only interesting with a temp register, which
        // the guest never uses; refuse anything but the default CURRENT.
        if let Some(result_arg) = self.texture_stage_state(stage, Ts::ResultArg.raw()) {
            if result_arg != d3dta::CURRENT {
                return Err(fail(format!(
                    "D3DTSS_RESULTARG = {result_arg:#010x} is not implemented"
                )));
            }
        }
        let transform_flags = raw(Ts::TextureTransformFlags, 0);
        if transform_flags != 0 {
            return Err(fail(format!(
                "D3DTSS_TEXTURETRANSFORMFLAGS = {transform_flags:#010x} is not implemented"
            )));
        }
        let filter = |name: &str, value: u32| -> Result<u32, RenderError> {
            match D3DTEXTUREFILTERTYPE::from_raw(value)? {
                D3DTEXTUREFILTERTYPE::None
                | D3DTEXTUREFILTERTYPE::Point
                | D3DTEXTUREFILTERTYPE::Linear => Ok(value),
                D3DTEXTUREFILTERTYPE::Anisotropic
                | D3DTEXTUREFILTERTYPE::PyramidQuad
                | D3DTEXTUREFILTERTYPE::GaussianQuad => Err(fail(format!(
                    "{name} = {value:#x} is not POINT/LINEAR/NONE in the bounded subset"
                ))),
            }
        };
        let address = |name: &str, value: u32| -> Result<u32, RenderError> {
            match D3DTEXTUREADDRESS::from_raw(value)? {
                D3DTEXTUREADDRESS::Wrap
                | D3DTEXTUREADDRESS::Mirror
                | D3DTEXTUREADDRESS::Clamp
                | D3DTEXTUREADDRESS::Border => Ok(value),
                D3DTEXTUREADDRESS::MirrorOnce => Err(fail(format!(
                    "{name} = D3DTADDRESS_MIRRORONCE has no wgpu equivalent"
                ))),
            }
        };
        let min_filter = filter(
            "D3DTSS_MINFILTER",
            raw(Ts::MinFilter, D3DTEXTUREFILTERTYPE::Point.raw()),
        )?;
        let mag_filter = filter(
            "D3DTSS_MAGFILTER",
            raw(Ts::MagFilter, D3DTEXTUREFILTERTYPE::Point.raw()),
        )?;
        let mip_filter = filter(
            "D3DTSS_MIPFILTER",
            raw(Ts::MipFilter, D3DTEXTUREFILTERTYPE::None.raw()),
        )?;
        let address_u = address(
            "D3DTSS_ADDRESSU",
            raw(Ts::AddressU, D3DTEXTUREADDRESS::Wrap.raw()),
        )?;
        let address_v = address(
            "D3DTSS_ADDRESSV",
            raw(Ts::AddressV, D3DTEXTUREADDRESS::Wrap.raw()),
        )?;
        Ok(TextureStage {
            active,
            color_op,
            color_arg1,
            color_arg2,
            alpha_op,
            alpha_arg1,
            alpha_arg2,
            texture_factor: self.states.texture_factor,
            tex_coord_index,
            min_filter,
            mag_filter,
            mip_filter,
            address_u,
            address_v,
        })
    }

    /// Compact, deterministic dump of the draw-relevant device state used as
    /// the rejection key by the opt-in survey (`RECOMP_D3D8_SURVEY=1`). It
    /// includes the typed render states, the raw fog states and the first two
    /// texture stages. Only called when the survey is enabled.
    pub fn draw_state_summary(&self) -> String {
        use D3DTEXTURESTAGESTATETYPE as Ts;
        let s = &self.states;
        let tss = |stage: u32| -> String {
            let value = |state: Ts| -> String {
                match self.texture_stage_state(stage, state as u32) {
                    Some(value) => format!("{value:#x}"),
                    None => "-".to_string(),
                }
            };
            format!(
                "tss{stage}[colorop={} alphaop={} c1={} c2={} a1={} a2={} tci={} ttf={}]",
                value(Ts::ColorOp),
                value(Ts::AlphaOp),
                value(Ts::ColorArg1),
                value(Ts::ColorArg2),
                value(Ts::AlphaArg1),
                value(Ts::AlphaArg2),
                value(Ts::TexCoordIndex),
                value(Ts::TextureTransformFlags),
            )
        };
        format!(
            "lighting={} colorvertex={} fog={} fogcolor={:#010x} fogtable={} fogvertex={} rangefog={} fogstart={} fogend={} fogdensity={} z={:?} zwrite={} zfunc={:?} alpha_test={} dither={} specular={} stencil={} clip={} fill={:?} shade={:?} cull={:?} blend={} src={:?} dst={:?} blendop={:?} {} {}",
            s.lighting,
            s.color_vertex,
            s.fog_enable,
            s.fog_color,
            format!("{:#x}", s.fog_table_mode),
            format!("{:#x}", s.fog_vertex_mode),
            s.range_fog_enable,
            format!("{:#x}", s.fog_start),
            format!("{:#x}", s.fog_end),
            format!("{:#x}", s.fog_density),
            s.z_enable,
            s.z_write_enable,
            s.z_func,
            s.alpha_test_enable,
            s.dither_enable,
            s.specular_enable,
            s.stencil_enable,
            s.clipping,
            s.fill_mode,
            s.shade_mode,
            s.cull_mode,
            s.alpha_blend_enable,
            s.src_blend,
            s.dest_blend,
            s.blend_op,
            tss(0),
            tss(1),
        )
    }

    /// True when depth testing is enabled (`D3DRS_ZENABLE` is not `D3DZB_FALSE`).
    /// `D3DZB_USEW` counts as enabled here; the device maps its comparison the
    /// same way because D3D8's w-buffer is not modelled separately.
    pub fn z_enable(&self) -> bool {
        self.states.z_enable != D3DZBUFFERTYPE::False
    }

    /// `D3DRS_ZWRITEENABLE`.
    pub fn z_write_enable(&self) -> bool {
        self.states.z_write_enable
    }

    /// `D3DRS_ZFUNC`.
    pub fn z_func(&self) -> D3DCMPFUNC {
        self.states.z_func
    }

    /// `D3DRS_FOGENABLE`.
    pub fn fog_enable(&self) -> bool {
        self.states.fog_enable
    }

    /// `D3DRS_FOGCOLOR` (raw ARGB D3DCOLOR).
    pub fn fog_color(&self) -> u32 {
        self.states.fog_color
    }

    /// `D3DRS_FOGTABLEMODE` as a raw `D3DFOGMODE`.
    pub fn fog_table_mode(&self) -> u32 {
        self.states.fog_table_mode
    }

    /// `D3DRS_FOGVERTEXMODE` as a raw `D3DFOGMODE`.
    pub fn fog_vertex_mode(&self) -> u32 {
        self.states.fog_vertex_mode
    }

    /// `D3DRS_FOGSTART` as the raw float bit pattern.
    pub fn fog_start(&self) -> u32 {
        self.states.fog_start
    }

    /// `D3DRS_FOGEND` as the raw float bit pattern.
    pub fn fog_end(&self) -> u32 {
        self.states.fog_end
    }

    /// `D3DRS_FOGDENSITY` as the raw float bit pattern.
    pub fn fog_density(&self) -> u32 {
        self.states.fog_density
    }

    /// `D3DRS_RANGEFOGENABLE`.
    pub fn range_fog_enable(&self) -> bool {
        self.states.range_fog_enable
    }

    /// `D3DRS_TEXTUREFACTOR` (raw `D3DCOLOR`, ARGB).
    pub fn texture_factor(&self) -> u32 {
        self.states.texture_factor
    }

    /// `D3DRS_ALPHABLENDENABLE`.
    pub fn alpha_blend_enable(&self) -> bool {
        self.states.alpha_blend_enable
    }

    /// `D3DRS_SRCBLEND`.
    pub fn src_blend(&self) -> D3DBLEND {
        self.states.src_blend
    }

    /// `D3DRS_DESTBLEND`.
    pub fn dest_blend(&self) -> D3DBLEND {
        self.states.dest_blend
    }

    /// `D3DRS_BLENDOP`.
    pub fn blend_op(&self) -> D3DBLENDOP {
        self.states.blend_op
    }

    /// `D3DRS_CULLMODE`.
    pub fn cull_mode(&self) -> D3DCULL {
        self.states.cull_mode
    }

    /// `D3DRS_ALPHATESTENABLE`.
    pub fn alpha_test_enable(&self) -> bool {
        self.states.alpha_test_enable
    }

    /// `D3DRS_ALPHAFUNC` as a raw `D3DCMPFUNC`.
    pub fn alpha_func(&self) -> D3DCMPFUNC {
        self.states.alpha_func
    }

    /// `D3DRS_ALPHAREF` (0..255 at a validated draw).
    pub fn alpha_ref(&self) -> u32 {
        self.states.alpha_ref
    }

    /// Verify that the current state is inside the supported unlit triangle
    /// slice. Must be called before submitting a draw.
    ///
    /// Unsupported here: fixed-function lighting, `D3DZB_USEW`, vertex/range
    /// fog, dithering, specular adds and the stencil buffer. Table/pixel fog
    /// (EXP/EXP2/LINEAR) and the alpha test are honoured by the shader from
    /// the eye-space depth and the final stage-blended alpha respectively,
    /// subject to the D3D8 blend-colour adjustment rule.
    /// Values outside Solid fill/Gouraud shade, a disabled clipper, and
    /// enabling any of the above are named errors rather than approximations. Ordinary depth test/write is supported and configured
    /// by the device from `z_enable`/`z_write_enable`/`z_func`.
    pub fn validate_unlit(&self) -> Result<(), RenderError> {
        self.validate_fixed_function(false)
    }

    /// Validate the state consumed by the supported guest vertex layout.
    pub fn validate_draw(&self, fvf: u32) -> Result<(), RenderError> {
        self.validate_fixed_function(fvf == 0x152)
    }

    fn validate_fixed_function(&self, normals: bool) -> Result<(), RenderError> {
        let s = &self.states;
        let unsupported = |what: &str| {
            RenderError::new(
                "d3d8::state::validate_unlit",
                format!("unsupported state for unlit draw: {what}"),
            )
        };

        // TODO(draw): the state model stores every valid D3DRS_*/D3DTSS_* value
        // faithfully, but the unlit slice honours only the typed fields below.
        // Until a broader fixed-function path exists, any stored state that it
        // would otherwise drop must fail the draw by name.
        if let Some((state, value)) = s
            .other
            .iter()
            .find(|(state, value)| raw_state_reached(**state, **value))
        {
            return Err(unsupported(&format!(
                "D3DRS_{state:#x} = {value:#010x} is stored but not honoured"
            )));
        }

        // Only the normal-bearing layout feeds the implemented lighting stage.
        // Keep other lit FVFs unsupported until their inputs are implemented.
        if s.lighting && !normals {
            return Err(unsupported(
                "D3DRS_LIGHTING must be FALSE (material/light state is stored but not applied)",
            ));
        }
        // With lighting off, D3D8 takes the vertex color from the material
        // (instead of the vertex) when COLORVERTEX is disabled. The unlit
        // shader only implements the per-vertex source, so refuse the other.
        if !s.color_vertex && !normals {
            return Err(unsupported(
                "D3DRS_COLORVERTEX must be TRUE (material-supplied vertex color is not implemented)",
            ));
        }
        // Ordinary depth testing/writing is honoured by the draw pipeline (the
        // device must own a depth attachment; the draw checks that separately).
        // D3DZB_USEW asks for a w-buffer, which this bounded path does not
        // model, so it stays a named error rather than silently using ordinary
        // depth compare.
        if s.z_enable == D3DZBUFFERTYPE::UseW {
            return Err(unsupported("D3DRS_ZENABLE must not be D3DZB_USEW"));
        }
        if s.fog_enable {
            if s.range_fog_enable {
                return Err(unsupported(
                    "D3DRS_RANGEFOGENABLE must be FALSE (range-based fog distance is not implemented)",
                ));
            }
            if D3DFOGMODE::from_raw(s.fog_vertex_mode)? != D3DFOGMODE::None {
                return Err(unsupported(
                    "D3DRS_FOGVERTEXMODE must be D3DFOG_NONE (vertex fog is not implemented)",
                ));
            }
            // Pixel/table fog is applied by the shader. D3D8 adjusts the fog
            // colour for some destination blends; only the normal
            // INVSRCALPHA case (unadjusted) and additive DESTBLEND=ONE
            // (adjusted to black, any source factor) are implemented, anything else fails by name.
            if s.alpha_blend_enable {
                match s.dest_blend {
                    D3DBLEND::InvSrcAlpha => {}
                    D3DBLEND::One => {}
                    other => {
                        return Err(unsupported(&format!(
                            "fog with D3DRS_DESTBLEND={other:?} needs the D3D8 fog-colour adjustment, which is not implemented (only INVSRCALPHA and DESTBLEND=ONE are)"
                        )));
                    }
                }
            }
        }
        if s.alpha_test_enable && s.alpha_ref > 0xff {
            // D3DRS_ALPHAREF is an 8-bit reference; the shader compares it
            // against the quantised source alpha, so refuse a value that would
            // silently truncate.
            return Err(unsupported(&format!(
                "D3DRS_ALPHAREF = {:#x} is outside 0..255",
                s.alpha_ref
            )));
        }
        if s.dither_enable {
            return Err(unsupported("D3DRS_DITHERENABLE must be FALSE"));
        }
        if s.specular_enable {
            return Err(unsupported("D3DRS_SPECULARENABLE must be FALSE"));
        }
        if s.stencil_enable {
            return Err(unsupported("D3DRS_STENCILENABLE must be FALSE"));
        }
        if !s.clipping {
            return Err(unsupported("D3DRS_CLIPPING must be TRUE"));
        }
        if s.fill_mode != D3DFILLMODE::Solid {
            return Err(unsupported("D3DRS_FILLMODE must be D3DFILL_SOLID"));
        }
        if s.shade_mode != D3DSHADEMODE::Gouraud {
            return Err(unsupported("D3DRS_SHADEMODE must be D3DSHADE_GOURAUD"));
        }
        Ok(())
    }
}

/// True when a raw `D3DRS_*` stored in the "other" map would change an unlit
/// triangle draw. States belonging to a feature the draw path already refuses
/// when enabled, or to a primitive it never draws, are inert and must not fail
/// the draw: D3D8 stores them regardless of whether the feature is on.
fn raw_state_reached(state: u32, value: u32) -> bool {
    use D3DRENDERSTATETYPE as Rs;
    match Rs::from_raw(state) {
        // Stencil state has no effect while D3DRS_STENCILENABLE is FALSE.
        Ok(Rs::StencilFail)
        | Ok(Rs::StencilZFail)
        | Ok(Rs::StencilPass)
        | Ok(Rs::StencilFunc)
        | Ok(Rs::StencilRef)
        | Ok(Rs::StencilMask)
        | Ok(Rs::StencilWriteMask) => false,
        // Software-rasterizer details and line/point/patch state do not affect
        // a hardware triangle.
        Ok(Rs::LinePattern)
        | Ok(Rs::LastPixel)
        | Ok(Rs::EdgeAntiAlias)
        | Ok(Rs::PointSize)
        | Ok(Rs::PointSizeMin)
        | Ok(Rs::PointSpriteEnable)
        | Ok(Rs::PointScaleEnable)
        | Ok(Rs::PointScaleA)
        | Ok(Rs::PointScaleB)
        | Ok(Rs::PointScaleC)
        | Ok(Rs::PointSizeMax)
        | Ok(Rs::PatchEdgeStyle)
        | Ok(Rs::PatchSegments)
        | Ok(Rs::DebugMonitorToken)
        | Ok(Rs::ZVisible)
        | Ok(Rs::SoftwareVertexProcessing)
        | Ok(Rs::PositionOrder)
        | Ok(Rs::NormalOrder)
        | Ok(Rs::MultiSampleAntiAlias)
        | Ok(Rs::MultiSampleMask)
        | Ok(Rs::TweenFactor)
        | Ok(Rs::IndexedVertexBlendEnable) => false,
        // Texture-coordinate wrap only feeds sampling, which is refused
        // whenever a texture stage is configured. D3DRS_TEXTUREFACTOR is
        // honoured by the stage resolver as D3DTA_TFACTOR.
        Ok(Rs::Wrap0) | Ok(Rs::Wrap1) | Ok(Rs::Wrap2) | Ok(Rs::Wrap3) | Ok(Rs::Wrap4)
        | Ok(Rs::Wrap5) | Ok(Rs::Wrap6) | Ok(Rs::Wrap7) => false,
        // States whose default value is a no-op: they only constrain the draw
        // once the guest asks for something the path does not implement.
        Ok(Rs::ZBias) => value != 0,
        Ok(Rs::ClipPlaneEnable) => value != 0,
        Ok(Rs::VertexBlend) => value != 0, // D3DVBF_DISABLE
        Ok(Rs::ColorWriteEnable) => value != 0x0000_000f, // RGBA channels
        // Everything else stored is a state this path cannot honour.
        _ => true,
    }
}

/// Convert a D3D8 BOOL render-state value. D3D8 treats any nonzero value as
/// true, but this bounded implementation only supports the canonical `0`/`1`;
/// other truthy encodings are reported as unsupported rather than approximated.
/// The state id is used in the diagnostic.
fn bool_state(state: D3DRENDERSTATETYPE, value: u32) -> Result<bool, RenderError> {
    match value {
        0 => Ok(false),
        1 => Ok(true),
        other => Err(RenderError::new(
            "d3d8::state::set_render_state",
            format!(
                "{} value {other:#010x} is unsupported; bounded implementation accepts only 0 or 1",
                state_label(state)
            ),
        )),
    }
}

/// Exhaustive `D3DRS_*` display name; keeps diagnostics independent of the
/// render-state enum's `Debug` formatting.
fn state_label(state: D3DRENDERSTATETYPE) -> &'static str {
    use D3DRENDERSTATETYPE as Rs;
    match state {
        Rs::ZEnable => "D3DRS_ZENABLE",
        Rs::FillMode => "D3DRS_FILLMODE",
        Rs::ShadeMode => "D3DRS_SHADEMODE",
        Rs::ZWriteEnable => "D3DRS_ZWRITEENABLE",
        Rs::AlphaTestEnable => "D3DRS_ALPHATESTENABLE",
        Rs::SrcBlend => "D3DRS_SRCBLEND",
        Rs::DestBlend => "D3DRS_DESTBLEND",
        Rs::CullMode => "D3DRS_CULLMODE",
        Rs::ZFunc => "D3DRS_ZFUNC",
        Rs::AlphaRef => "D3DRS_ALPHAREF",
        Rs::AlphaFunc => "D3DRS_ALPHAFUNC",
        Rs::DitherEnable => "D3DRS_DITHERENABLE",
        Rs::AlphaBlendEnable => "D3DRS_ALPHABLENDENABLE",
        Rs::FogEnable => "D3DRS_FOGENABLE",
        Rs::SpecularEnable => "D3DRS_SPECULARENABLE",
        Rs::FogColor => "D3DRS_FOGCOLOR",
        Rs::StencilEnable => "D3DRS_STENCILENABLE",
        Rs::Clipping => "D3DRS_CLIPPING",
        Rs::Lighting => "D3DRS_LIGHTING",
        Rs::Ambient => "D3DRS_AMBIENT",
        Rs::ColorVertex => "D3DRS_COLORVERTEX",
        Rs::LocalViewer => "D3DRS_LOCALVIEWER",
        Rs::NormalizeNormals => "D3DRS_NORMALIZENORMALS",
        Rs::DiffuseMaterialSource => "D3DRS_DIFFUSEMATERIALSOURCE",
        Rs::SpecularMaterialSource => "D3DRS_SPECULARMATERIALSOURCE",
        Rs::AmbientMaterialSource => "D3DRS_AMBIENTMATERIALSOURCE",
        Rs::EmissiveMaterialSource => "D3DRS_EMISSIVEMATERIALSOURCE",
        Rs::BlendOp => "D3DRS_BLENDOP",
        _ => "D3DRS_OTHER",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn configure_probe_states(state: &mut DeviceState) {
        state
            .set_render_state(D3DRENDERSTATETYPE::ZEnable.raw(), 0)
            .unwrap();
        state
            .set_render_state(D3DRENDERSTATETYPE::Lighting.raw(), 0)
            .unwrap();
        state
            .set_render_state(D3DRENDERSTATETYPE::CullMode.raw(), D3DCULL::None.raw())
            .unwrap();
    }

    #[test]
    fn new_uses_d3d_defaults() {
        let state = DeviceState::new(800, 600);
        assert_eq!(state.world, Mat4::IDENTITY);
        assert_eq!(state.view, Mat4::IDENTITY);
        assert_eq!(state.projection, Mat4::IDENTITY);
        assert_eq!(
            state.viewport,
            Viewport {
                x: 0,
                y: 0,
                width: 800,
                height: 600,
                min_z: 0.0,
                max_z: 1.0
            }
        );
        // D3D defaults enable lighting/depth and cull CCW, so an unconfigured
        // draw must be rejected.
        assert!(state.validate_unlit().is_err());
    }

    #[test]
    fn probe_configuration_passes_validation() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.validate_unlit().unwrap();
    }

    #[test]
    fn each_unsupported_reached_state_is_named() {
        fn cause_after(mutate: impl FnOnce(&mut DeviceState)) -> String {
            let mut s = DeviceState::new(64, 64);
            configure_probe_states(&mut s);
            mutate(&mut s);
            let err = s.validate_unlit().unwrap_err();
            assert_eq!(err.operation, "d3d8::state::validate_unlit");
            err.cause
        }

        assert!(cause_after(|s| s.set_render_state(137, 1).unwrap()).contains("LIGHTING"));
        // CULLMODE and ALPHABLENDENABLE are honoured by the draw pipeline.
        // Table fog is honoured too; vertex fog and range fog stay named
        // failures.
        assert!(
            cause_after(|s| {
                s.set_render_state(28, 1).unwrap(); // FOGENABLE
                s.set_render_state(140, D3DFOGMODE::Exp.raw()).unwrap(); // FOGVERTEXMODE
            })
            .contains("FOGVERTEXMODE")
        );
        assert!(
            cause_after(|s| {
                s.set_render_state(28, 1).unwrap(); // FOGENABLE
                s.set_render_state(48, 1).unwrap(); // RANGEFOGENABLE
            })
            .contains("RANGEFOGENABLE")
        );
        assert!(
            cause_after(|s| {
                s.set_render_state(15, 1).unwrap(); // ALPHATESTENABLE
                s.set_render_state(24, 0x100).unwrap(); // ALPHAREF out of range
            })
            .contains("ALPHAREF")
        );
        assert!(cause_after(|s| s.set_render_state(26, 1).unwrap()).contains("DITHERENABLE"));
        assert!(cause_after(|s| s.set_render_state(29, 1).unwrap()).contains("SPECULARENABLE"));
        assert!(cause_after(|s| s.set_render_state(52, 1).unwrap()).contains("STENCILENABLE"));
        assert!(cause_after(|s| s.set_render_state(136, 0).unwrap()).contains("CLIPPING"));
        assert!(cause_after(|s| s.set_render_state(8, 2).unwrap()).contains("FILLMODE"));
        assert!(cause_after(|s| s.set_render_state(9, 1).unwrap()).contains("SHADEMODE"));
    }

    #[test]
    fn depth_only_states_are_not_reached_when_depth_is_off() {
        // Z write/func and blend factors only take effect when depth/blend are
        // enabled; with those rejected, their stored values are not reached.
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(14, 0).unwrap(); // ZWRITEENABLE off
        state
            .set_render_state(23, D3DCMPFUNC::Always.raw())
            .unwrap();
        state
            .set_render_state(19, D3DBLEND::SrcAlpha.raw())
            .unwrap();
        state
            .set_render_state(20, D3DBLEND::InvSrcAlpha.raw())
            .unwrap();
        state.set_render_state(171, D3DBLENDOP::Max.raw()).unwrap();
        state.validate_unlit().unwrap();
    }

    #[test]
    fn alpha_test_is_honoured_by_validation() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(15, 1).unwrap(); // ALPHATESTENABLE
        state.set_render_state(24, 0x80).unwrap(); // ALPHAREF
        state
            .set_render_state(25, D3DCMPFUNC::GreaterEqual.raw())
            .unwrap();
        state.validate_unlit().unwrap();
        assert!(state.alpha_test_enable());
        assert_eq!(state.alpha_ref(), 0x80);
        assert_eq!(state.alpha_func(), D3DCMPFUNC::GreaterEqual);
    }

    #[test]
    fn use_w_depth_is_rejected() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state
            .set_render_state(7, D3DZBUFFERTYPE::UseW.raw())
            .unwrap();
        assert!(state.validate_unlit().is_err());
    }

    #[test]
    fn unknown_render_state_is_an_error() {
        let mut state = DeviceState::new(64, 64);
        let err = state.set_render_state(999, 0).unwrap_err();
        assert_eq!(err.operation, "d3d8::enums::D3DRENDERSTATETYPE");
        assert!(err.cause.contains("0x000003e7"), "{}", err.cause);
        // The sentinel is still rejected.
        assert!(state.set_render_state(0x7fff_ffff, 0).is_err());
    }

    #[test]
    fn material_defaults_and_round_trip() {
        let mut state = DeviceState::new(64, 64);
        assert_eq!(state.material(), Material::d3d_default());
        let material = Material {
            diffuse: [0.25, 0.5, 0.75, 1.0],
            ambient: [0.1, 0.2, 0.3, 1.0],
            specular: [0.9, 0.8, 0.7, 1.0],
            emissive: [0.0, 0.0, 0.0, 1.0],
            power: 8.0,
        };
        state.set_material(material);
        assert_eq!(state.material(), material);
    }

    #[test]
    fn lights_are_indexed_and_enable_flags_are_independent() {
        let mut state = DeviceState::new(64, 64);
        let light = Light {
            light_type: 1,
            diffuse: [1.0, 0.0, 0.0, 1.0],
            specular: [0.0; 4],
            ambient: [0.0; 4],
            position: [1.0, 2.0, 3.0],
            direction: [0.0, 0.0, -1.0],
            range: 100.0,
            falloff: 1.0,
            attenuation0: 1.0,
            attenuation1: 0.0,
            attenuation2: 0.0,
            theta: 0.0,
            phi: 0.0,
        };
        assert_eq!(state.light(0).unwrap(), Light::default());
        state.set_light(3, light).unwrap();
        assert_eq!(state.light(3).unwrap(), light);
        assert_eq!(state.light(0).unwrap(), Light::default());
        state.light_enable(3, true).unwrap();
        assert!(state.light_enabled(3).unwrap());
        assert!(!state.light_enabled(0).unwrap());
        state.light_enable(3, false).unwrap();
        assert!(!state.light_enabled(3).unwrap());
        // Out-of-range slots are named errors on every entry point.
        assert!(state.set_light(MAX_LIGHTS as u32, light).is_err());
        assert!(state.light(MAX_LIGHTS as u32).is_err());
        assert!(state.light_enable(MAX_LIGHTS as u32, true).is_err());
        assert!(state.light_enabled(MAX_LIGHTS as u32).is_err());
    }

    #[test]
    fn color_vertex_off_is_refused_without_lighting() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(141, 0).unwrap(); // D3DRS_COLORVERTEX
        let err = state.validate_unlit().unwrap_err();
        assert!(err.cause.contains("COLORVERTEX"), "{}", err.cause);
    }

    #[test]
    fn stored_material_does_not_break_unlit_draws() {
        // Lighting is off and COLORVERTEX is on (the defaults), so D3D8 would
        // ignore the material for vertex color. Storing it must not fail an
        // otherwise valid unlit draw; it is not silently applied either.
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_material(Material {
            diffuse: [1.0, 0.0, 0.0, 1.0],
            ..Material::d3d_default()
        });
        state.set_light(0, Light::default()).unwrap();
        state.light_enable(0, true).unwrap();
        state.validate_unlit().unwrap();
    }

    #[test]
    fn inert_raw_states_do_not_fail_the_draw() {
        // Fog/stencil/line/point/wrap state is stored by D3D8 even when the
        // feature is off; with FOGENABLE/STENCILENABLE off (the required
        // state) it cannot affect an unlit triangle and must not fail it.
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(35, 0).unwrap(); // FOGTABLEMODE
        state.set_render_state(36, 0.5f32.to_bits()).unwrap(); // FOGSTART
        state.set_render_state(60, 0xffff_ffff).unwrap(); // TEXTUREFACTOR
        state.set_render_state(128, 0).unwrap(); // WRAP0
        state.set_render_state(161, 1).unwrap(); // MULTISAMPLEANTIALIAS
        state.validate_unlit().unwrap();
    }

    #[test]
    fn disabled_texture_stage_does_not_fail_the_draw() {
        // An untextured draw names COLOROP/ALPHAOP = DISABLE; the stage samples
        // nothing, so the stored state must not fail the draw.
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_texture_stage_state(0, 1, 1).unwrap(); // COLOROP = DISABLE
        state.set_texture_stage_state(0, 4, 1).unwrap(); // ALPHAOP = DISABLE
        state.set_texture_stage_state(0, 11, 0).unwrap(); // TEXCOORDINDEX
        state.validate_unlit().unwrap();
    }

    #[test]
    fn every_valid_render_state_is_accepted_and_stored() {
        let mut state = DeviceState::new(64, 64);
        // A previously unmodelled state (D3DRS_LINEPATTERN) is accepted and
        // stored verbatim; D3DRS_TEXTUREFACTOR, which stopped the startup
        // replay, is now modelled and validated like the other typed states.
        state.set_render_state(10, 0x1122_3344).unwrap();
        assert_eq!(state.raw_render_state(10), Some(0x1122_3344));
        state.set_render_state(60, 0xffff_ffff).unwrap();
        // D3DRS_TEXTUREFACTOR is now typed and read back through its getter.
        assert_eq!(state.texture_factor(), 0xffff_ffff);
        assert_eq!(state.raw_render_state(60), None);
        // The typed subset is still validated, not silently stored raw.
        assert!(state.set_render_state(7, 3).is_err());
    }

    #[test]
    fn texture_stage_states_are_validated_and_stored() {
        let mut state = DeviceState::new(64, 64);
        state.set_texture_stage_state(0, 1, 4).unwrap(); // COLOROP = MODULATE
        assert_eq!(state.texture_stage_state(0, 1), Some(4));
        // Stage 0 was set; stage 1 was not.
        assert_eq!(state.texture_stage_state(1, 1), None);
        // An unknown state id and an out-of-range stage are named errors.
        assert_eq!(
            state
                .set_texture_stage_state(0, 12, 0)
                .unwrap_err()
                .operation,
            "d3d8::enums::D3DTEXTURESTAGESTATETYPE"
        );
        assert!(state.set_texture_stage_state(8, 1, 0).is_err());
    }

    #[test]
    fn unsupported_bool_encodings_are_rejected() {
        let mut state = DeviceState::new(64, 64);
        // CULLMODE=0 is not a valid D3DCULL.
        assert!(state.set_render_state(22, 0).is_err());
        // ZENABLE=3 is outside D3DZBUFFERTYPE.
        assert!(state.set_render_state(7, 3).is_err());
        // The bounded implementation supports only canonical 0/1 BOOLs; other
        // truthy encodings are unsupported, not silently treated as true.
        let err = state.set_render_state(27, 2).unwrap_err();
        assert_eq!(err.operation, "d3d8::state::set_render_state");
        assert!(err.cause.contains("ALPHABLENDENABLE"), "{}", err.cause);
        assert!(err.cause.contains("unsupported"), "{}", err.cause);
        assert!(state.set_render_state(137, 0xffff_ffff).is_err());
        // No state was modified by the failed calls: probe config still needed.
        configure_probe_states(&mut state);
        state.validate_unlit().unwrap();
    }

    #[test]
    fn stored_but_unhonoured_state_fails_the_draw() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(47, 0x0000_0001).unwrap(); // D3DRS_ZBIAS
        let err = state.validate_unlit().unwrap_err();
        assert_eq!(err.operation, "d3d8::state::validate_unlit");
        assert!(err.cause.contains("not honoured"), "{}", err.cause);
    }

    #[test]
    fn texture_stage_resolution_models_the_supported_subset() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        // No explicit op and no texture: the stage stays on the untextured path.
        let stage = state.resolve_texture_stage(0, false).unwrap();
        assert!(!stage.active);
        // A bound texture activates the default MODULATE/SELECTARG1 stage.
        let stage = state.resolve_texture_stage(0, true).unwrap();
        assert!(stage.active);
        assert_eq!(stage.color_op, D3DTEXTUREOP::Modulate.raw());
        assert_eq!(stage.alpha_op, D3DTEXTUREOP::SelectArg1.raw());
        assert_eq!(stage.color_arg1, d3dta::TEXTURE);
        assert_eq!(stage.color_arg2, d3dta::CURRENT);
        // An explicit MODULATE activates without a bound texture as well.
        state.set_texture_stage_state(0, 1, 4).unwrap();
        let stage = state.resolve_texture_stage(0, false).unwrap();
        assert!(stage.active);
        // Both ops explicitly DISABLE with no bound texture is inert.
        state
            .set_texture_stage_state(0, 1, D3DTEXTUREOP::Disable.raw())
            .unwrap();
        state
            .set_texture_stage_state(0, 4, D3DTEXTUREOP::Disable.raw())
            .unwrap();
        assert!(!state.resolve_texture_stage(0, false).unwrap().active);
    }

    #[test]
    fn texture_factor_is_a_supported_stage_argument() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(60, 0x4080c020).unwrap(); // D3DRS_TEXTUREFACTOR
        state.set_texture_stage_state(0, 1, 4).unwrap(); // COLOROP = MODULATE
        state.set_texture_stage_state(0, 2, d3dta::TFACTOR).unwrap(); // COLORARG1
        state.set_texture_stage_state(0, 3, d3dta::TEXTURE).unwrap(); // COLORARG2
        state.set_texture_stage_state(0, 4, 3).unwrap(); // ALPHAOP = SELECTARG2
        state.set_texture_stage_state(0, 5, d3dta::TEXTURE).unwrap(); // ALPHAARG1
        state.set_texture_stage_state(0, 6, d3dta::TFACTOR).unwrap(); // ALPHAARG2
        let stage = state.resolve_texture_stage(0, false).unwrap();
        assert!(stage.active);
        assert_eq!(stage.color_arg1, d3dta::TFACTOR);
        assert_eq!(stage.alpha_arg2, d3dta::TFACTOR);
        assert_eq!(stage.texture_factor, 0x4080c020);
        assert_eq!(state.texture_factor(), 0x4080c020);
    }

    #[test]
    fn texture_stage_unsupported_values_fail_by_name() {
        fn resolve(mutate: impl FnOnce(&mut DeviceState)) -> String {
            let mut state = DeviceState::new(64, 64);
            mutate(&mut state);
            state.resolve_texture_stage(0, true).unwrap_err().cause
        }
        // An op outside the implemented set (ADDSMOOTH is not emitted by the
        // guest's TSS setup functions).
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 1, D3DTEXTUREOP::AddSmooth.raw())
                    .unwrap();
            })
            .contains("COLOROP")
        );
        // An argument modifier other than COMPLEMENT/ALPHAREPLICATE, or an
        // unsupported selector.
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 2, d3dta::DIFFUSE | 0x40)
                    .unwrap();
            })
            .contains("modifier")
        );
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 2, d3dta::SPECULAR).unwrap();
            })
            .contains("COLORARG1")
        );
        // A texture coordinate index beyond the two vertex sets.
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 11, 2).unwrap();
            })
            .contains("TEXCOORDINDEX")
        );
        // A texture coordinate transform.
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 24, 2).unwrap();
            })
            .contains("TEXTURETRANSFORMFLAGS")
        );
        // An anisotropic filter and MIRRORONCE addressing.
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 17, D3DTEXTUREFILTERTYPE::Anisotropic.raw())
                    .unwrap();
            })
            .contains("MINFILTER")
        );
        assert!(
            resolve(|s| {
                s.set_texture_stage_state(0, 13, D3DTEXTUREADDRESS::MirrorOnce.raw())
                    .unwrap();
            })
            .contains("MIRRORONCE")
        );
        // Stage 2 is a named boundary, not a silent drop.
        let mut state = DeviceState::new(64, 64);
        state
            .set_texture_stage_state(2, 1, D3DTEXTUREOP::Modulate.raw())
            .unwrap();
        assert!(
            state
                .resolve_texture_stage(2, true)
                .unwrap_err()
                .cause
                .contains("stage 2")
        );
    }

    #[test]
    fn stage_one_resolves_the_two_stage_guest_ops() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        // FUN_007c2f80 two-stage form: stage 0 MODULATE(TEXTURE, TFACTOR) with
        // ALPHAOP MOD4X(TFACTOR); stage 1 ADD(CURRENT, DIFFUSE) with
        // ALPHAOP SELECTARG2(DIFFUSE).
        state.set_texture_stage_state(0, 1, 4).unwrap(); // COLOROP MODULATE
        state.set_texture_stage_state(0, 2, 2).unwrap(); // COLORARG1 TEXTURE
        state.set_texture_stage_state(0, 3, 3).unwrap(); // COLORARG2 TFACTOR
        state.set_texture_stage_state(0, 4, 6).unwrap(); // ALPHAOP MOD4X
        state.set_texture_stage_state(0, 6, 3).unwrap(); // ALPHAARG2 TFACTOR
        state.set_texture_stage_state(1, 1, 7).unwrap(); // COLOROP ADD
        state.set_texture_stage_state(1, 2, 1).unwrap(); // COLORARG1 CURRENT
        state.set_texture_stage_state(1, 3, 0).unwrap(); // COLORARG2 DIFFUSE
        state.set_texture_stage_state(1, 4, 3).unwrap(); // ALPHAOP SELECTARG2
        state.set_texture_stage_state(1, 6, 0).unwrap(); // ALPHAARG2 DIFFUSE
        state.set_texture_stage_state(1, 11, 1).unwrap(); // TEXCOORDINDEX 1
        let stage0 = state.resolve_texture_stage(0, false).unwrap();
        let stage1 = state.resolve_texture_stage(1, false).unwrap();
        assert!(stage0.active && stage1.active);
        assert_eq!(stage1.color_op, D3DTEXTUREOP::Add.raw());
        assert_eq!(stage1.color_arg1, d3dta::CURRENT);
        assert_eq!(stage1.tex_coord_index, 1);
        // COMPLEMENT and ALPHAREPLICATE are accepted and carried through.
        state
            .set_texture_stage_state(1, 2, d3dta::CURRENT | d3dta::COMPLEMENT)
            .unwrap();
        state
            .set_texture_stage_state(1, 3, d3dta::DIFFUSE | d3dta::ALPHAREPLICATE)
            .unwrap();
        let stage1 = state.resolve_texture_stage(1, false).unwrap();
        assert_eq!(stage1.color_arg1, d3dta::CURRENT | d3dta::COMPLEMENT);
        assert_eq!(stage1.color_arg2, d3dta::DIFFUSE | d3dta::ALPHAREPLICATE);
    }

    #[test]
    fn draw_state_summary_reports_fog_lighting_and_texture_stages() {
        let mut state = DeviceState::new(64, 64);
        state.set_render_state(28, 1).unwrap(); // FOGENABLE
        state.set_render_state(34, 0x0011_2233).unwrap(); // FOGCOLOR
        state.set_render_state(35, 3).unwrap(); // FOGTABLEMODE = LINEAR
        state.set_render_state(36, 1.5f32.to_bits()).unwrap(); // FOGSTART
        state.set_render_state(38, 2.5f32.to_bits()).unwrap(); // FOGDENSITY
        state.set_render_state(137, 1).unwrap(); // LIGHTING
        state.set_texture_stage_state(0, 1, 7).unwrap(); // COLOROP = ADDSIGNED
        state.set_texture_stage_state(0, 2, 3).unwrap(); // COLORARG1 = TFACTOR
        state.set_texture_stage_state(1, 1, 4).unwrap(); // stage-1 COLOROP = MODULATE
        let summary = state.draw_state_summary();
        assert!(summary.contains("lighting=true"), "{summary}");
        assert!(summary.contains("fog=true"), "{summary}");
        assert!(summary.contains("fogcolor=0x00112233"), "{summary}");
        assert!(summary.contains("fogtable=0x3"), "{summary}");
        assert!(summary.contains("fogstart=0x3fc00000"), "{summary}");
        assert!(summary.contains("fogdensity=0x40200000"), "{summary}");
        assert!(summary.contains("tss0[colorop=0x7"), "{summary}");
        assert!(summary.contains("c1=0x3"), "{summary}");
        assert!(summary.contains("tss1[colorop=0x4"), "{summary}");
    }

    #[test]
    fn table_fog_states_are_stored_and_validated() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(28, 1).unwrap(); // FOGENABLE
        state
            .set_render_state(35, D3DFOGMODE::Linear.raw())
            .unwrap(); // FOGTABLEMODE
        state.set_render_state(34, 0xffb2b2b2).unwrap(); // FOGCOLOR
        state.set_render_state(36, 190.0f32.to_bits()).unwrap(); // FOGSTART
        state.set_render_state(37, 240.0f32.to_bits()).unwrap(); // FOGEND
        state.set_render_state(38, 1.0f32.to_bits()).unwrap(); // FOGDENSITY
        state.validate_unlit().unwrap();
        assert!(state.fog_enable());
        assert_eq!(state.fog_table_mode(), D3DFOGMODE::Linear.raw());
        assert_eq!(state.fog_color(), 0xffb2b2b2);
        assert_eq!(state.fog_start(), 190.0f32.to_bits());
        assert_eq!(state.fog_end(), 240.0f32.to_bits());
        assert_eq!(state.fog_density(), 1.0f32.to_bits());
        assert!(!state.range_fog_enable());
    }

    #[test]
    fn fog_with_unsupported_blend_fails_by_name() {
        let mut state = DeviceState::new(64, 64);
        configure_probe_states(&mut state);
        state.set_render_state(28, 1).unwrap(); // FOGENABLE
        state
            .set_render_state(35, D3DFOGMODE::Linear.raw())
            .unwrap();
        state.set_render_state(27, 1).unwrap(); // ALPHABLENDENABLE
        state
            .set_render_state(19, D3DBLEND::SrcAlpha.raw())
            .unwrap(); // SRCBLEND
        state
            .set_render_state(20, D3DBLEND::DestColor.raw())
            .unwrap(); // DESTBLEND
        let err = state.validate_unlit().unwrap_err();
        assert!(err.cause.contains("DESTBLEND"), "{}", err.cause);

        // The standard INVSRCALPHA destination is accepted.
        state
            .set_render_state(20, D3DBLEND::InvSrcAlpha.raw())
            .unwrap();
        state.validate_unlit().unwrap();

        // Additive ONE/ONE is accepted (the renderer forces a black fog colour).
        state.set_render_state(19, D3DBLEND::One.raw()).unwrap();
        state.set_render_state(20, D3DBLEND::One.raw()).unwrap();
        state.validate_unlit().unwrap();

        // DESTBLEND=ONE with another source factor (e.g. SRCALPHA) is also additive.
        state
            .set_render_state(19, D3DBLEND::SrcAlpha.raw())
            .unwrap();
        state.validate_unlit().unwrap();
    }

    #[test]
    fn defaults_of_modelled_states_match_d3d8() {
        let s = RenderStates::d3d_defaults();
        assert_eq!(s.z_enable, D3DZBUFFERTYPE::True);
        assert_eq!(s.fill_mode, D3DFILLMODE::Solid);
        assert_eq!(s.shade_mode, D3DSHADEMODE::Gouraud);
        assert!(s.z_write_enable);
        assert!(!s.alpha_test_enable);
        assert_eq!(s.texture_factor, 0xFFFF_FFFF);
        assert_eq!(s.src_blend, D3DBLEND::One);
        assert_eq!(s.dest_blend, D3DBLEND::Zero);
        assert_eq!(s.cull_mode, D3DCULL::Ccw);
        assert_eq!(s.z_func, D3DCMPFUNC::LessEqual);
        assert_eq!(s.alpha_func, D3DCMPFUNC::Always);
        assert!(!s.alpha_blend_enable);
        assert!(!s.fog_enable);
        assert!(s.clipping);
        assert!(s.lighting);
        assert!(s.color_vertex);
        assert!(s.local_viewer);
        assert_eq!(s.blend_op, D3DBLENDOP::Add);
    }
}

#[path = "lighting.rs"]
mod lighting;
