#[cxx::bridge(namespace = "recomp::d3d")]
pub mod ffi {
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum StatusCode {
        Ok = 0,
        InvalidArgument = 1,
        Unsupported = 2,
        Backend = 3,
        NotImplemented = 4,
        OutOfMemory = 5,
    }

    struct Status {
        code: StatusCode,
        message: String,
        state_block_error: StateBlockError,
    }

    struct ShaderHandleResult {
        status: Status,
        handle: u32,
    }

    struct ConstantRead {
        status: Status,
        values: Vec<u32>,
    }

    struct ValueResult {
        status: Status,
        value: u32,
    }

    struct ValuesResult {
        status: Status,
        values: Vec<u32>,
    }

    struct FloatValuesResult {
        status: Status,
        values: Vec<f32>,
    }

    struct HandleResult {
        status: Status,
        handle: u32,
    }

    struct TextureSetResult {
        status: Status,
        old_id: u32,
        new_id: u32,
        recording: bool,
    }

    struct TextureBindings {
        old_values: Vec<u32>,
        new_values: Vec<u32>,
    }

    struct ApplyStateBlockResult {
        status: Status,
        bindings: TextureBindings,
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum StateBlockError {
        None = 0,
        InBeginStateBlock = 1,
        NotInBeginStateBlock = 2,
        InvalidStateBlock = 3,
        OutOfMemory = 4,
    }

    struct AdapterInfo {
        name: String,
        vendor_id: u32,
        device_id: u32,
        max_texture_dimension_2d: u32,
        max_bind_groups: u32,
        max_vertex_buffers: u32,
        max_vertex_attributes: u32,
    }
    struct AdapterInfoResult {
        status: Status,
        info: AdapterInfo,
    }
    struct Rect {
        x1: i32,
        y1: i32,
        x2: i32,
        y2: i32,
    }
    struct TextureLevel<'a> {
        level: u32,
        generation: u64,
        force_upload: bool,
        width: u32,
        height: u32,
        data: &'a [u8],
    }
    #[derive(Clone, Copy)]
    struct Viewport {
        x: u32,
        y: u32,
        width: u32,
        height: u32,
        min_z: f32,
        max_z: f32,
    }
    struct ViewportResult {
        status: Status,
        viewport: Viewport,
    }
    struct Material {
        diffuse_r: f32,
        diffuse_g: f32,
        diffuse_b: f32,
        diffuse_a: f32,
        ambient_r: f32,
        ambient_g: f32,
        ambient_b: f32,
        ambient_a: f32,
        specular_r: f32,
        specular_g: f32,
        specular_b: f32,
        specular_a: f32,
        emissive_r: f32,
        emissive_g: f32,
        emissive_b: f32,
        emissive_a: f32,
        power: f32,
    }
    struct MaterialResult {
        status: Status,
        material: Material,
    }
    struct Light {
        light_type: u32,
        diffuse_r: f32,
        diffuse_g: f32,
        diffuse_b: f32,
        diffuse_a: f32,
        specular_r: f32,
        specular_g: f32,
        specular_b: f32,
        specular_a: f32,
        ambient_r: f32,
        ambient_g: f32,
        ambient_b: f32,
        ambient_a: f32,
        position_x: f32,
        position_y: f32,
        position_z: f32,
        direction_x: f32,
        direction_y: f32,
        direction_z: f32,
        range: f32,
        falloff: f32,
        attenuation0: f32,
        attenuation1: f32,
        attenuation2: f32,
        theta: f32,
        phi: f32,
    }
    struct LightResult {
        status: Status,
        light: Light,
    }
    struct FrameHandoff {
        texture: *mut u8,
        busy: *mut u32,
        width: u32,
        height: u32,
    }
    struct FrameHandoffResult {
        status: Status,
        handoff: FrameHandoff,
    }

    extern "Rust" {
        type Device;
        type Storage;
        type Device7State;
        type Resource;

        fn new_device() -> Box<Device>;
        fn adapter_info() -> AdapterInfoResult;
        fn set_device_dimensions(device: &mut Device, width: u32, height: u32) -> Status;
        fn set_device_format(device: &mut Device, format: u32, depth_format: u32) -> Status;
        fn initialize_gpu(
            device: &mut Device,
            width: u32,
            height: u32,
            format: u32,
            depth_format: u32,
        ) -> Status;
        fn reset_device(device: &mut Device, params: &PresentParams) -> Status;
        fn global_reset(device: &mut Device) -> ValuesResult;
        fn gpu_enabled(device: &Device) -> bool;
        fn device_info(device: &Device) -> DeviceInfoResult;
        fn set_stream(device: &mut Device, buffer_id: u32, stride: u32) -> BindingResult;
        fn set_indices(device: &mut Device, buffer_id: u32, base_vertex: u32) -> BindingResult;
        fn set_bound_texture(device: &mut Device, stage: u32, texture_id: u32) -> BindingResult;
        fn bound_texture(device: &Device, stage: u32) -> ValueResult;

        fn new_resource() -> Box<Resource>;
        fn set_resource_flags(
            resource: &mut Resource,
            usage: u32,
            pool: u32,
            depth: bool,
            gpu_target: bool,
            buffer_fvf: u32,
            index_format: u32,
            buffer_usage: u32,
            buffer_pool: u32,
        );
        fn resource_info(resource: &Resource) -> ResourceInfo;
        fn set_resource_lod(resource: &mut Resource, lod: u32) -> ValueResult;
        fn set_resource_priority(resource: &mut Resource, priority: u32) -> ValueResult;
        fn set_resource_generation(resource: &mut Resource, generation: u64);
        fn resource_generation(resource: &Resource) -> u64;
        fn set_resource_level(
            resource: &mut Resource,
            level: u32,
            width: u32,
            height: u32,
            pitch: u32,
            byte_len: u32,
            generation: u64,
        ) -> Status;
        fn resource_level(resource: &Resource, level: u32) -> ResourceLevelResult;

        fn bump_resource_generation(resource: &mut Resource) -> u64;
        fn bump_resource_level_generation(
            resource: &mut Resource,
            level: u32,
        ) -> ResourceGenerationResult;
        fn configure_texture_resource(
            resource: &mut Resource,
            width: u32,
            height: u32,
            levels: u32,
            format: u32,
        ) -> ResourceConfigurationResult;
        fn create_shader(
            device: &mut Device,
            pixel: bool,
            declaration: &[u32],
            function: &[u32],
        ) -> ShaderHandleResult;
        fn bind_shader(device: &mut Device, pixel: bool, handle: u32) -> Status;
        fn delete_shader(device: &mut Device, pixel: bool, handle: u32) -> Status;
        fn selected_shader(device: &Device, pixel: bool) -> u32;
        fn set_shader_constants(
            device: &mut Device,
            pixel: bool,
            register_index: u32,
            values: &[u32],
        ) -> Status;
        fn get_shader_constants(
            device: &Device,
            pixel: bool,
            register_index: u32,
            count: u32,
        ) -> ConstantRead;
        fn shader_tokens(
            device: &Device,
            pixel: bool,
            handle: u32,
            declaration: bool,
        ) -> ValuesResult;

        fn new_storage() -> Box<Storage>;
        fn allocate_storage(storage: &mut Storage, size: u32) -> Status;
        fn storage_bytes(storage: &Storage) -> &[u8];
        fn storage_bytes_mut(storage: &mut Storage) -> &mut [u8];
        fn copy_storage(destination: &mut Storage, source: &Storage) -> Status;
        fn format_bytes(format: u32) -> u32;
        fn block_bytes(format: u32) -> u32;
        fn texture_level_layout(
            width: u32,
            height: u32,
            levels: u32,
            level: u32,
            format: u32,
        ) -> LevelLayoutResult;
        fn clear(
            device: &mut Device,
            rects: &[Rect],
            flags: u32,
            color: u32,
            z: f32,
            stencil: u32,
        ) -> Status;
        fn begin_scene(device: &mut Device) -> Status;
        fn end_scene(device: &mut Device) -> Status;
        fn set_render_state(device: &mut Device, state: u32, value: u32) -> Status;
        fn set_texture_stage_state(
            device: &mut Device,
            stage: u32,
            state: u32,
            value: u32,
        ) -> Status;
        fn set_texture(
            device: &mut Device,
            stage: u32,
            texture_id: u32,
            format: u32,
            levels: &[TextureLevel],
        ) -> Status;
        fn set_render_target(
            device: &mut Device,
            texture_id: u32,
            level: u32,
            generation: u64,
            format: u32,
            width: u32,
            height: u32,
            data: &[u8],
            depth: bool,
            depth_id: u32,
            depth_width: u32,
            depth_height: u32,
            depth_format: u32,
            change_color: bool,
        ) -> Status;
        fn read_texture(
            device: &Device,
            texture_id: u32,
            level: u32,
            generation: u64,
            out: &mut [u8],
        ) -> Status;
        fn release_texture(device: &mut Device, texture_id: u32);
        fn set_transform(device: &mut Device, kind: u32, values: &[f32]) -> Status;
        fn set_viewport(device: &mut Device, viewport: &Viewport) -> Status;
        fn get_viewport(device: &Device) -> ViewportResult;
        fn set_material(device: &mut Device, material: &Material) -> Status;
        fn get_material(device: &Device) -> MaterialResult;
        fn set_light(device: &mut Device, index: u32, light: &Light) -> Status;
        fn get_light(device: &Device, index: u32) -> LightResult;
        fn light_enable(device: &mut Device, index: u32, enabled: bool) -> Status;
        fn get_light_enable(device: &Device, index: u32) -> ValueResult;
        fn draw_primitive(
            device: &mut Device,
            topology: u32,
            fvf: u32,
            vertices: &[u8],
            stride: u32,
            start_vertex: u32,
            primitive_count: u32,
        ) -> Status;
        fn draw_indexed_primitive(
            device: &mut Device,
            topology: u32,
            fvf: u32,
            vertices: &[u8],
            stride: u32,
            indices: &[u8],
            index_format: u32,
            base_vertex: u32,
            min_index: u32,
            num_vertices: u32,
            start_index: u32,
            primitive_count: u32,
        ) -> Status;
        fn process_vertices(
            device: &mut Device,
            src: &[u8],
            src_fvf: u32,
            src_stride: u32,
            dest: &mut [u8],
            dest_fvf: u32,
            src_start: u32,
            dest_index: u32,
            count: u32,
        ) -> Status;
        fn read_pixels_len(device: &Device, out: &mut u64) -> Status;
        fn read_pixels(device: &Device, out: &mut [u8]) -> Status;
        fn present(device: &mut Device) -> Status;
        fn present_handoff(device: &Device, bpp: u32) -> FrameHandoffResult;
        fn scene_boundary(device: &mut Device, mode: u32) -> Status;

        fn new_device7_state() -> Box<Device7State>;
        fn d3d7_get_render_state(state: &Device7State, index: u32) -> ValueResult;
        fn d3d7_set_render_state(state: &mut Device7State, index: u32, value: u32) -> Status;
        fn d3d7_render_state_was_set(state: &Device7State, index: u32) -> bool;
        fn d3d7_get_texture_stage_state(
            state: &Device7State,
            stage: u32,
            index: u32,
        ) -> ValueResult;
        fn d3d7_set_texture_stage_state(
            state: &mut Device7State,
            stage: u32,
            index: u32,
            value: u32,
        ) -> Status;
        fn d3d7_texture_stage_state_was_set(state: &Device7State, stage: u32, index: u32) -> bool;
        fn d3d7_get_transform(state: &Device7State, index: u32) -> FloatValuesResult;
        fn d3d7_set_transform(state: &mut Device7State, index: u32, values: &[f32]) -> Status;
        fn d3d7_transform_was_set(state: &Device7State, index: u32) -> bool;
        fn d3d7_get_viewport(state: &Device7State) -> ViewportResult;
        fn d3d7_set_viewport(state: &mut Device7State, viewport: &Viewport) -> Status;
        fn d3d7_viewport_was_set(state: &Device7State) -> bool;
        fn d3d7_get_material(state: &Device7State) -> ValuesResult;
        fn d3d7_set_material(state: &mut Device7State, values: &[u32]) -> Status;
        fn d3d7_material_was_set(state: &Device7State) -> bool;
        fn d3d7_get_light(state: &Device7State, index: u32) -> ValuesResult;
        fn d3d7_set_light(state: &mut Device7State, index: u32, values: &[u32]) -> Status;
        fn d3d7_get_light_enable(state: &Device7State, index: u32) -> ValueResult;
        fn d3d7_set_light_enable(state: &mut Device7State, index: u32, value: u32) -> Status;
        fn d3d7_get_texture(state: &Device7State, stage: u32) -> ValueResult;
        fn d3d7_set_texture(
            state: &mut Device7State,
            stage: u32,
            object_id: u32,
        ) -> TextureSetResult;
        fn d3d7_get_render_target(state: &Device7State) -> u32;
        fn d3d7_set_render_target(state: &mut Device7State, object_id: u32);
        fn d3d7_recording(state: &Device7State) -> bool;
        fn d3d7_in_scene(state: &Device7State) -> bool;
        fn d3d7_set_in_scene(state: &mut Device7State, value: bool);
        fn d3d7_target_dirty(state: &Device7State) -> bool;
        fn d3d7_set_target_dirty(state: &mut Device7State, value: bool);
        fn d3d7_begin_state_block(state: &mut Device7State) -> Status;
        fn d3d7_end_state_block(state: &mut Device7State) -> HandleResult;
        fn d3d7_apply_state_block(state: &mut Device7State, handle: u32) -> ApplyStateBlockResult;
        fn d3d7_delete_state_block(state: &mut Device7State, handle: u32) -> ValuesResult;
        fn d3d7_release_refs(state: &mut Device7State) -> ValuesResult;
        fn d3d7_state_block_render_state_was_set(
            state: &Device7State,
            handle: u32,
            index: u32,
        ) -> bool;
        fn d3d7_state_block_texture_stage_state_was_set(
            state: &Device7State,
            handle: u32,
            stage: u32,
            index: u32,
        ) -> bool;
        fn d3d7_state_block_transform_was_set(
            state: &Device7State,
            handle: u32,
            index: u32,
        ) -> bool;
        fn d3d7_state_block_viewport_was_set(state: &Device7State, handle: u32) -> bool;
        fn d3d7_state_block_material_was_set(state: &Device7State, handle: u32) -> bool;
        fn d3d7_state_block_light_was_set(state: &Device7State, handle: u32, index: u32) -> bool;
        fn d3d7_state_block_light_enable_was_set(
            state: &Device7State,
            handle: u32,
            index: u32,
        ) -> bool;
        fn d3d7_state_block_texture_was_set(state: &Device7State, handle: u32, stage: u32) -> bool;
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum D3d7TranslationKind {
        Forward = 0,
        Ignore = 1,
        Invalid = 2,
    }
    struct D3d7RenderStateTranslation {
        kind: D3d7TranslationKind,
        state: u32,
    }
    struct D3d7TransformTranslation {
        valid: bool,
        state: u32,
    }
    struct D3d7TextureStageTranslation {
        count: i32,
        first: u32,
        second: u32,
    }

    extern "Rust" {
        fn translate_render_state(state: u32, value: u32) -> D3d7RenderStateTranslation;
        fn translate_transform(state: u32) -> D3d7TransformTranslation;
        fn translate_texture_stage_state(state: u32) -> D3d7TextureStageTranslation;
    }

    #[derive(Clone, Copy, Debug)]
    struct PresentParams {
        back_buffer_width: u32,
        back_buffer_height: u32,
        back_buffer_format: u32,
        back_buffer_count: u32,
        multisample_type: u32,
        swap_effect: u32,
        device_window: u32,
        windowed: u32,
        enable_auto_depth_stencil: u32,
        auto_depth_stencil_format: u32,
        flags: u32,
        fullscreen_refresh_rate: u32,
        fullscreen_presentation_interval: u32,
    }

    struct LevelLayout {
        width: u32,
        height: u32,
        pitch: u32,
        size: u32,
        levels: u32,
    }

    struct LevelLayoutResult {
        status: Status,
        layout: LevelLayout,
    }

    struct ResourceInfo {
        usage: u32,
        pool: u32,
        lod: u32,
        priority: u32,
        level_count: u32,
        depth: bool,
        gpu_target: bool,
        buffer_fvf: u32,
        index_format: u32,
        buffer_usage: u32,
        buffer_pool: u32,
    }
    struct ResourceLevel {
        width: u32,
        height: u32,
        pitch: u32,
        byte_len: u32,
        generation: u64,
    }
    struct ResourceLevelResult {
        status: Status,
        level: ResourceLevel,
    }
    struct ResourceConfigurationResult {
        status: Status,
        level_count: u32,
        levels: Vec<ResourceLevel>,
    }
    struct ResourceGenerationResult {
        status: Status,
        generation: u64,
    }
    struct BindingResult {
        status: Status,
        old_id: u32,
        new_id: u32,
        old_value: u32,
        new_value: u32,
    }
    struct DeviceInfo {
        width: u32,
        height: u32,
        format: u32,
        depth_format: u32,
        stream_vb: u32,
        stream_stride: u32,
        index_buffer: u32,
        base_vertex: u32,
    }
    struct DeviceInfoResult {
        status: Status,
        info: DeviceInfo,
    }
}

use crate::bridge_render::{
    adapter_info, begin_scene, clear, draw_indexed_primitive, draw_primitive, end_scene, get_light,
    get_light_enable, get_material, get_viewport, light_enable, present, present_handoff,
    process_vertices, read_pixels, read_pixels_len, read_texture, release_texture, scene_boundary,
    set_light, set_material, set_render_state, set_render_target, set_texture,
    set_texture_stage_state, set_transform, set_viewport,
};
use crate::d3d8::{
    resource::{format_bytes as cpu_format_bytes, level_layout, D3d8LevelLayout},
    shader::{Declaration, Program},
    state::DeviceState,
};
use crate::{
    backend::GpuContext, d3d8::device::Device as RenderDevice, RenderError, RenderErrorKind,
};
use std::collections::BTreeMap;

pub use ffi::{
    AdapterInfo, AdapterInfoResult, ApplyStateBlockResult, BindingResult, ConstantRead,
    D3d7RenderStateTranslation, D3d7TextureStageTranslation, D3d7TransformTranslation,
    D3d7TranslationKind, DeviceInfo, DeviceInfoResult, FloatValuesResult, FrameHandoff,
    FrameHandoffResult, HandleResult, LevelLayout, LevelLayoutResult, Light, LightResult, Material,
    MaterialResult, PresentParams, Rect, ResourceConfigurationResult, ResourceGenerationResult,
    ResourceInfo, ResourceLevel, ResourceLevelResult, ShaderHandleResult, StateBlockError, Status,
    StatusCode, TextureBindings, TextureLevel, TextureSetResult, ValueResult, ValuesResult,
    Viewport, ViewportResult,
};

pub struct Device {
    pub(crate) gpu: Option<RenderDevice>,
    pub(crate) cpu_state: DeviceState,
    pub(crate) viewport_was_set: bool,
    target_width: u32,
    target_height: u32,
    target_format: u32,
    depth_format: u32,
    stream_vb: u32,
    stream_stride: u32,
    index_buffer: u32,
    base_vertex: u32,
    bound_textures: [u32; 8],
    next_shader: u32,
    shaders: BTreeMap<u32, ShaderRecord>,
    vertex_shader: u32,
    vertex_fvf: u32,
    pixel_shader: u32,
    vertex_constants: Vec<u32>,
    pixel_constants: Vec<u32>,
}

struct ShaderRecord {
    pixel: bool,
    declaration: Vec<u32>,
    function: Vec<u32>,
    parsed_declaration: Option<Declaration>,
    parsed_program: Program,
}

#[derive(Clone)]
struct Device7Data {
    render_state: [u32; 256],
    render_state_set: [bool; 256],
    texture_stage_state: [[u32; 256]; 8],
    texture_stage_state_set: [[bool; 256]; 8],
    transforms: [[f32; 16]; 256],
    transform_set: [bool; 256],
    viewport: Viewport,
    viewport_set: bool,
    material: [u32; 17],
    material_set: bool,
    lights: [[u32; 26]; 8],
    light_set: [bool; 8],
    light_enable: [u32; 8],
    light_enable_set: [bool; 8],
    textures: [u32; 8],
    texture_set: [bool; 8],
    render_target: u32,
    in_scene: bool,
    target_dirty: bool,
}

impl Default for Device7Data {
    fn default() -> Self {
        let mut data = Self {
            render_state: [0; 256],
            render_state_set: [false; 256],
            texture_stage_state: [[0; 256]; 8],
            texture_stage_state_set: [[false; 256]; 8],
            transforms: [[0.0; 16]; 256],
            transform_set: [false; 256],
            viewport: Viewport {
                x: 0,
                y: 0,
                width: 0,
                height: 0,
                min_z: 0.0,
                max_z: 1.0,
            },
            viewport_set: false,
            material: [0; 17],
            material_set: false,
            lights: [[0; 26]; 8],
            light_set: [false; 8],
            light_enable: [0; 8],
            light_enable_set: [false; 8],
            textures: [0; 8],
            texture_set: [false; 8],
            render_target: 0,
            in_scene: false,
            target_dirty: true,
        };
        for index in 0..256 {
            data.render_state[index] = d3d7_render_default(index);
            data.transforms[index][0] = 1.0;
            data.transforms[index][5] = 1.0;
            data.transforms[index][10] = 1.0;
            data.transforms[index][15] = 1.0;
        }
        for stage in 0..8 {
            for index in 0..256 {
                data.texture_stage_state[stage][index] = d3d7_texture_default(stage, index);
            }
        }
        data.material[..4].fill(0x3f80_0000);
        data
    }
}

pub struct Device7State {
    live: Device7Data,
    recording: Option<Device7Data>,
    state_blocks: BTreeMap<u32, Device7Data>,
    next_state_block: u32,
}

impl Default for Device7State {
    fn default() -> Self {
        Self {
            live: Device7Data::default(),
            recording: None,
            state_blocks: BTreeMap::new(),
            next_state_block: 1,
        }
    }
}

fn d3d7_render_default(index: usize) -> u32 {
    match index {
        8 => 3,
        9 => 2,
        14 | 16 => 1,
        19 => 2,
        20 => 1,
        22 => 3,
        23 => 4,
        25 | 56 => 8,
        37 | 38 | 154 | 158 | 164 => 0x3f80_0000,
        58 | 59 | 60 | 162 => u32::MAX,
        136 | 137 | 141 | 142 | 161 => 1,
        146 => 1,
        147 | 148 => 2,
        168 => 0xf,
        171 => 1,
        _ => 0,
    }
}

fn d3d7_texture_default(stage: usize, index: usize) -> u32 {
    match index {
        1 => {
            if stage == 0 {
                4
            } else {
                1
            }
        }
        2 | 5 => 2,
        3 | 6 => 1,
        4 => {
            if stage == 0 {
                2
            } else {
                1
            }
        }
        12..=14 => 1,
        16 | 17 => 2,
        _ => 0,
    }
}

fn state_write_target(state: &mut Device7State) -> &mut Device7Data {
    if state.recording.is_some() {
        state.recording.as_mut().unwrap()
    } else {
        &mut state.live
    }
}

pub struct Storage {
    storage: Option<crate::d3d8::resource::CpuStorage>,
}

#[derive(Clone, Copy, Default)]
struct ResourceLevelData {
    width: u32,
    height: u32,
    pitch: u32,
    byte_len: u32,
    generation: u64,
}

#[derive(Clone, Copy, Default)]
struct ResourceMeta {
    usage: u32,
    pool: u32,
    lod: u32,
    priority: u32,
    level_count: u32,
    depth: bool,
    gpu_target: bool,
    buffer_fvf: u32,
    index_format: u32,
    buffer_usage: u32,
    buffer_pool: u32,
}

pub struct Resource {
    info: ResourceMeta,
    generation: u64,
    levels: BTreeMap<u32, ResourceLevelData>,
}

impl Default for Resource {
    fn default() -> Self {
        Self {
            info: ResourceMeta::default(),
            generation: 0,
            levels: BTreeMap::new(),
        }
    }
}

pub(crate) fn status(code: StatusCode, message: impl Into<String>) -> Status {
    Status {
        code,
        message: message.into(),
        state_block_error: StateBlockError::None,
    }
}

fn state_block_status(code: StatusCode, message: &'static str, error: StateBlockError) -> Status {
    Status {
        code,
        message: message.into(),
        state_block_error: error,
    }
}

pub(crate) fn status_from_error(error: RenderError) -> Status {
    let code = match error.kind {
        RenderErrorKind::InvalidArgument => StatusCode::InvalidArgument,
        RenderErrorKind::Unsupported => StatusCode::Unsupported,
        RenderErrorKind::OutOfMemory => StatusCode::OutOfMemory,
    };
    status(code, error.to_string())
}

impl Default for Device {
    fn default() -> Self {
        Self {
            gpu: None,
            cpu_state: DeviceState::new(0, 0),
            viewport_was_set: false,
            target_width: 0,
            target_height: 0,
            target_format: 0,
            depth_format: 0,
            stream_vb: 0,
            stream_stride: 0,
            index_buffer: 0,
            base_vertex: 0,
            bound_textures: [0; 8],
            next_shader: 0xf000_0001,
            shaders: BTreeMap::new(),
            vertex_shader: 0,
            vertex_fvf: 0,
            pixel_shader: 0,
            vertex_constants: vec![0; 96 * 4],
            pixel_constants: vec![0; 8 * 4],
        }
    }
}

pub fn new_device() -> Box<Device> {
    Box::default()
}

impl Device {
    pub(crate) fn state(&self) -> &DeviceState {
        self.gpu
            .as_ref()
            .map_or(&self.cpu_state, |renderer| &renderer.state)
    }
    pub(crate) fn state_mut(&mut self) -> &mut DeviceState {
        self.gpu
            .as_mut()
            .map_or(&mut self.cpu_state, |renderer| &mut renderer.state)
    }
}

pub fn set_device_dimensions(device: &mut Device, width: u32, height: u32) -> Status {
    if width == 0 || height == 0 {
        return status(
            StatusCode::InvalidArgument,
            "CreateDevice: zero backbuffer dimensions",
        );
    }
    device.target_width = width;
    device.target_height = height;
    device.state_mut().set_target_size(width, height);
    if !device.viewport_was_set {
        device.state_mut().viewport = crate::d3d8::state::Viewport::full(width, height);
    }
    status(StatusCode::Ok, "")
}

pub fn set_device_format(device: &mut Device, format: u32, depth_format: u32) -> Status {
    device.target_format = format;
    device.depth_format = depth_format;
    match device
        .state_mut()
        .set_render_state(7, u32::from(depth_format != 0))
    {
        Ok(()) => status(StatusCode::Ok, ""),
        Err(error) => status_from_error(error),
    }
}

pub fn initialize_gpu(
    device: &mut Device,
    width: u32,
    height: u32,
    format: u32,
    depth_format: u32,
) -> Status {
    if device.gpu.is_some() {
        return status(
            StatusCode::InvalidArgument,
            "initialize_gpu: device already initialized",
        );
    }
    if width == 0 || height == 0 {
        return status(
            StatusCode::InvalidArgument,
            "initialize_gpu: dimensions must be nonzero",
        );
    }
    let gpu = match pollster::block_on(GpuContext::new_headless()) {
        Ok(gpu) => gpu,
        Err(error) => return status(StatusCode::Backend, error.to_string()),
    };
    let renderer = match RenderDevice::new(gpu, width, height, format, depth_format) {
        Ok(renderer) => renderer,
        Err(error) => return status_from_error(error),
    };
    device.target_width = width;
    device.target_height = height;
    device.target_format = format;
    device.depth_format = depth_format;
    let mut state = device.cpu_state.clone();
    if let Err(error) = state.set_render_state(7, u32::from(depth_format != 0)) {
        return status_from_error(error);
    }
    state.set_target_size(width, height);
    if !device.viewport_was_set {
        state.viewport = crate::d3d8::state::Viewport::full(width, height);
    }
    device.cpu_state = state.clone();
    device.gpu = Some(renderer);
    for (handle, shader) in &device.shaders {
        let renderer = device.gpu.as_mut().unwrap();
        renderer.shaders.insert(
            *handle,
            (
                shader.parsed_declaration.clone(),
                shader.parsed_program.clone(),
            ),
        );
    }
    if let Some(renderer) = device.gpu.as_mut() {
        renderer.state = state;
        renderer.vertex_shader = device.vertex_shader;
        renderer.pixel_shader = device.pixel_shader;
        for (register, target) in renderer.shader_uniform.vc.iter_mut().enumerate() {
            for component in 0..4 {
                target[component] =
                    f32::from_bits(device.vertex_constants[register * 4 + component]);
            }
        }
        for (register, target) in renderer.shader_uniform.pc.iter_mut().enumerate() {
            for component in 0..4 {
                target[component] =
                    f32::from_bits(device.pixel_constants[register * 4 + component]);
            }
        }
    }
    status(StatusCode::Ok, "")
}

pub fn reset_device(device: &mut Device, params: &PresentParams) -> Status {
    let depth_format = match validate_present_params(params) {
        Ok(value) => value,
        Err(error) => return status_from_error(error),
    };
    if let Some(renderer) = device.gpu.as_mut() {
        if let Err(error) = renderer.reset(
            params.back_buffer_width,
            params.back_buffer_height,
            params.back_buffer_format,
            depth_format,
        ) {
            return status_from_error(error);
        }
    }
    let mut state = DeviceState::new(params.back_buffer_width, params.back_buffer_height);
    if let Err(error) = state.set_render_state(7, u32::from(depth_format != 0)) {
        return status_from_error(error);
    }
    if let Some(renderer) = device.gpu.as_ref() {
        state = renderer.state.clone();
    }
    device.cpu_state = state;
    device.target_width = params.back_buffer_width;
    device.target_height = params.back_buffer_height;
    device.target_format = params.back_buffer_format;
    device.depth_format = depth_format;
    device.viewport_was_set = false;
    device.vertex_shader = 0;
    device.vertex_fvf = 0;
    device.pixel_shader = 0;
    device.stream_vb = 0;
    device.stream_stride = 0;
    device.index_buffer = 0;
    device.base_vertex = 0;
    device.bound_textures.fill(0);
    device.vertex_constants.fill(0);
    device.pixel_constants.fill(0);
    status(StatusCode::Ok, "")
}

fn validate_present_params(params: &PresentParams) -> Result<u32, RenderError> {
    use crate::d3d8::format::{self, DepthFormat};
    const SWAPEFFECT_DISCARD: u32 = 1;
    const SWAPEFFECT_FLIP: u32 = 2;
    const SWAPEFFECT_COPY_VSYNC: u32 = 4;
    const PRESENT_INTERVAL_IMMEDIATE: u32 = 0x8000_0000;
    let invalid = |what: &str| RenderError::invalid("reset", what);
    if params.back_buffer_width == 0 || params.back_buffer_height == 0 {
        return Err(invalid("zero backbuffer dimension"));
    }
    if u64::from(params.back_buffer_width) * u64::from(params.back_buffer_height) * 4
        > u64::from(u32::MAX)
    {
        return Err(invalid("backbuffer size overflow"));
    }
    if params.back_buffer_format != format::D3DFMT_A8R8G8B8
        && params.back_buffer_format != format::D3DFMT_X8R8G8B8
    {
        return Err(invalid("unsupported backbuffer format"));
    }
    if params.back_buffer_count > 1 || params.multisample_type != 0 {
        return Err(invalid("unsupported backbuffer count or multisample type"));
    }
    let swap_ok = params.swap_effect == SWAPEFFECT_DISCARD
        || params.swap_effect == SWAPEFFECT_COPY_VSYNC
        || (params.swap_effect == SWAPEFFECT_FLIP && params.windowed == 0);
    if !swap_ok {
        return Err(invalid("unsupported swap effect"));
    }
    let interval_ok = params.fullscreen_presentation_interval == 0
        || params.fullscreen_presentation_interval == PRESENT_INTERVAL_IMMEDIATE;
    if params.flags != 0 || params.fullscreen_refresh_rate != 0 || !interval_ok {
        return Err(invalid("unsupported present flags/refresh/interval"));
    }
    let depth = DepthFormat::from_d3dformat(params.auto_depth_stencil_format)
        .map_err(|_| invalid("unsupported autodepth format"))?;
    if params.enable_auto_depth_stencil == 0 {
        return Ok(0);
    }
    match depth {
        Some(value) => Ok(value.d3dformat()),
        None => Err(invalid("autodepth enabled without a depth format")),
    }
}

pub fn global_reset(device: &mut Device) -> ValuesResult {
    let mut released = Vec::with_capacity(10);
    released.push(device.stream_vb);
    released.push(device.index_buffer);
    released.extend(device.bound_textures);
    device.shaders.clear();
    device.next_shader = 0xf000_0001;
    device.vertex_shader = 0;
    device.vertex_fvf = 0;
    device.pixel_shader = 0;
    device.vertex_constants.fill(0);
    device.pixel_constants.fill(0);
    device.stream_vb = 0;
    device.stream_stride = 0;
    device.index_buffer = 0;
    device.base_vertex = 0;
    device.bound_textures.fill(0);
    if let Some(renderer) = device.gpu.as_mut() {
        renderer.shaders.clear();
        renderer.vertex_shader = 0;
        renderer.pixel_shader = 0;
        renderer.shader_uniform = Default::default();
    }
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values: released,
    }
}

pub fn gpu_enabled(device: &Device) -> bool {
    device.gpu.is_some()
}

pub fn translate_render_state(state: u32, value: u32) -> D3d7RenderStateTranslation {
    crate::bridge_translate::translate_render_state(state, value)
}
pub fn translate_transform(state: u32) -> D3d7TransformTranslation {
    crate::bridge_translate::translate_transform(state)
}
pub fn translate_texture_stage_state(state: u32) -> D3d7TextureStageTranslation {
    crate::bridge_translate::translate_texture_stage_state(state)
}

pub fn device_info(device: &Device) -> DeviceInfoResult {
    DeviceInfoResult {
        status: status(StatusCode::Ok, ""),
        info: DeviceInfo {
            width: device.target_width,
            height: device.target_height,
            format: device.target_format,
            depth_format: device.depth_format,
            stream_vb: device.stream_vb,
            stream_stride: device.stream_stride,
            index_buffer: device.index_buffer,
            base_vertex: device.base_vertex,
        },
    }
}

pub fn set_stream(device: &mut Device, buffer_id: u32, stride: u32) -> BindingResult {
    let result = BindingResult {
        status: status(StatusCode::Ok, ""),
        old_id: device.stream_vb,
        new_id: buffer_id,
        old_value: device.stream_stride,
        new_value: stride,
    };
    device.stream_vb = buffer_id;
    device.stream_stride = stride;
    result
}

pub fn set_indices(device: &mut Device, buffer_id: u32, base_vertex: u32) -> BindingResult {
    let result = BindingResult {
        status: status(StatusCode::Ok, ""),
        old_id: device.index_buffer,
        new_id: buffer_id,
        old_value: device.base_vertex,
        new_value: base_vertex,
    };
    device.index_buffer = buffer_id;
    device.base_vertex = base_vertex;
    result
}

pub fn set_bound_texture(device: &mut Device, stage: u32, texture_id: u32) -> BindingResult {
    let Some(current) = device.bound_textures.get_mut(stage as usize) else {
        return BindingResult {
            status: status(
                StatusCode::InvalidArgument,
                "SetTexture: stage out of range",
            ),
            old_id: 0,
            new_id: texture_id,
            old_value: 0,
            new_value: 0,
        };
    };
    let old_id = *current;
    *current = texture_id;
    BindingResult {
        status: status(StatusCode::Ok, ""),
        old_id,
        new_id: texture_id,
        old_value: 0,
        new_value: 0,
    }
}

pub fn bound_texture(device: &Device, stage: u32) -> ValueResult {
    device
        .bound_textures
        .get(stage as usize)
        .copied()
        .map(value_result)
        .unwrap_or_else(|| invalid_value("GetTexture: stage out of range"))
}

pub fn new_resource() -> Box<Resource> {
    Box::default()
}
pub fn set_resource_flags(
    resource: &mut Resource,
    usage: u32,
    pool: u32,
    depth: bool,
    gpu_target: bool,
    buffer_fvf: u32,
    index_format: u32,
    buffer_usage: u32,
    buffer_pool: u32,
) {
    resource.info.usage = usage;
    resource.info.pool = pool;
    resource.info.depth = depth;
    resource.info.gpu_target = gpu_target;
    resource.info.buffer_fvf = buffer_fvf;
    resource.info.index_format = index_format;
    resource.info.buffer_usage = buffer_usage;
    resource.info.buffer_pool = buffer_pool;
}
pub fn resource_info(resource: &Resource) -> ResourceInfo {
    ResourceInfo {
        usage: resource.info.usage,
        pool: resource.info.pool,
        lod: resource.info.lod,
        priority: resource.info.priority,
        level_count: resource.info.level_count,
        depth: resource.info.depth,
        gpu_target: resource.info.gpu_target,
        buffer_fvf: resource.info.buffer_fvf,
        index_format: resource.info.index_format,
        buffer_usage: resource.info.buffer_usage,
        buffer_pool: resource.info.buffer_pool,
    }
}
pub fn set_resource_lod(resource: &mut Resource, lod: u32) -> ValueResult {
    let previous = resource.info.lod;
    let maximum = resource.info.level_count.saturating_sub(1);
    resource.info.lod = lod.min(maximum);
    value_result(previous)
}
pub fn set_resource_priority(resource: &mut Resource, priority: u32) -> ValueResult {
    let previous = resource.info.priority;
    resource.info.priority = priority;
    value_result(previous)
}
pub fn set_resource_generation(resource: &mut Resource, generation: u64) {
    resource.generation = generation;
}
pub fn bump_resource_generation(resource: &mut Resource) -> u64 {
    resource.generation = resource.generation.wrapping_add(1);
    resource.generation
}
pub fn bump_resource_level_generation(
    resource: &mut Resource,
    level: u32,
) -> ResourceGenerationResult {
    match resource.levels.get_mut(&level) {
        Some(value) => {
            value.generation = value.generation.wrapping_add(1);
            ResourceGenerationResult {
                status: status(StatusCode::Ok, ""),
                generation: value.generation,
            }
        }
        None => ResourceGenerationResult {
            status: status(
                StatusCode::InvalidArgument,
                "BumpResourceLevelGeneration: level out of range",
            ),
            generation: 0,
        },
    }
}
pub fn resource_generation(resource: &Resource) -> u64 {
    resource.generation
}
pub fn set_resource_level(
    resource: &mut Resource,
    level: u32,
    width: u32,
    height: u32,
    pitch: u32,
    byte_len: u32,
    generation: u64,
) -> Status {
    if width == 0 || height == 0 || pitch == 0 || byte_len == 0 {
        return status(
            StatusCode::InvalidArgument,
            "SetResourceLevel: invalid layout",
        );
    }
    resource.levels.insert(
        level,
        ResourceLevelData {
            width,
            height,
            pitch,
            byte_len,
            generation,
        },
    );
    resource.info.level_count = resource.info.level_count.max(level.saturating_add(1));
    status(StatusCode::Ok, "")
}
pub fn resource_level(resource: &Resource, level: u32) -> ResourceLevelResult {
    match resource.levels.get(&level) {
        Some(value) => ResourceLevelResult {
            status: status(StatusCode::Ok, ""),
            level: ResourceLevel {
                width: value.width,
                height: value.height,
                pitch: value.pitch,
                byte_len: value.byte_len,
                generation: value.generation,
            },
        },
        None => ResourceLevelResult {
            status: status(
                StatusCode::InvalidArgument,
                "GetResourceLevel: level out of range",
            ),
            level: ResourceLevel {
                width: 0,
                height: 0,
                pitch: 0,
                byte_len: 0,
                generation: 0,
            },
        },
    }
}

pub fn configure_texture_resource(
    resource: &mut Resource,
    width: u32,
    height: u32,
    levels: u32,
    format: u32,
) -> ResourceConfigurationResult {
    let first = match level_layout(width, height, levels, 0, format) {
        Ok(value) => value,
        Err(error) => {
            return ResourceConfigurationResult {
                status: status_from_error(error),
                level_count: 0,
                levels: Vec::new(),
            }
        }
    };
    let mut configured = Vec::new();
    if configured.try_reserve_exact(first.levels as usize).is_err() {
        return ResourceConfigurationResult {
            status: status(
                StatusCode::OutOfMemory,
                "ConfigureTexture: level metadata allocation failed",
            ),
            level_count: 0,
            levels: Vec::new(),
        };
    }
    for index in 0..first.levels {
        let layout = match level_layout(width, height, first.levels, index, format) {
            Ok(value) => value,
            Err(error) => {
                return ResourceConfigurationResult {
                    status: status_from_error(error),
                    level_count: 0,
                    levels: Vec::new(),
                }
            }
        };
        let value = ResourceLevel {
            width: layout.width,
            height: layout.height,
            pitch: layout.pitch,
            byte_len: layout.size,
            generation: 0,
        };
        configured.push(value);
    }
    resource.levels.clear();
    for (index, value) in configured.iter().enumerate() {
        resource.levels.insert(
            index as u32,
            ResourceLevelData {
                width: value.width,
                height: value.height,
                pitch: value.pitch,
                byte_len: value.byte_len,
                generation: value.generation,
            },
        );
    }
    resource.info.level_count = first.levels;
    ResourceConfigurationResult {
        status: status(StatusCode::Ok, ""),
        level_count: first.levels,
        levels: configured,
    }
}

pub fn create_shader(
    device: &mut Device,
    pixel: bool,
    declaration: &[u32],
    function: &[u32],
) -> ShaderHandleResult {
    if device.next_shader == u32::MAX {
        return ShaderHandleResult {
            status: status(
                StatusCode::InvalidArgument,
                "CreateShader: handle range exhausted",
            ),
            handle: 0,
        };
    }
    if declaration.len() > 4096 || function.len() > 4096 {
        return ShaderHandleResult {
            status: status(
                StatusCode::InvalidArgument,
                "CreateShader: token count exceeds 4096",
            ),
            handle: 0,
        };
    }
    if pixel && function.is_empty() {
        return ShaderHandleResult {
            status: status(
                StatusCode::InvalidArgument,
                "CreatePixelShader: empty function",
            ),
            handle: 0,
        };
    }
    let parsed_declaration = if pixel {
        None
    } else {
        if declaration.is_empty() {
            return ShaderHandleResult {
                status: status(
                    StatusCode::InvalidArgument,
                    "CreateVertexShader: empty declaration",
                ),
                handle: 0,
            };
        }
        match Declaration::parse(declaration) {
            Ok(value) => Some(value),
            Err(error) => {
                return ShaderHandleResult {
                    status: status_from_error(error),
                    handle: 0,
                }
            }
        }
    };
    let parsed_program = if function.is_empty() {
        if let Err(error) = parsed_declaration.as_ref().unwrap().fixed_fvf() {
            return ShaderHandleResult {
                status: status_from_error(error),
                handle: 0,
            };
        }
        Program {
            pixel: false,
            declaration_only: true,
            body: String::new(),
            samplers: [false; 4],
        }
    } else {
        match Program::parse(function, pixel) {
            Ok(value) => value,
            Err(error) => {
                return ShaderHandleResult {
                    status: status_from_error(error),
                    handle: 0,
                }
            }
        }
    };
    let handle = device.next_shader;
    if let Some(renderer) = device.gpu.as_mut() {
        renderer
            .shaders
            .insert(handle, (parsed_declaration.clone(), parsed_program.clone()));
    }
    device.shaders.insert(
        handle,
        ShaderRecord {
            pixel,
            declaration: declaration.to_vec(),
            function: function.to_vec(),
            parsed_declaration,
            parsed_program,
        },
    );
    device.next_shader += 1;
    ShaderHandleResult {
        status: status(StatusCode::Ok, ""),
        handle,
    }
}

pub fn bind_shader(device: &mut Device, pixel: bool, handle: u32) -> Status {
    if pixel {
        if handle != 0
            && !device
                .shaders
                .get(&handle)
                .is_some_and(|shader| shader.pixel)
        {
            return status(
                StatusCode::InvalidArgument,
                "SetPixelShader: invalid shader handle",
            );
        }
        device.pixel_shader = handle;
        if let Some(renderer) = device.gpu.as_mut() {
            renderer.pixel_shader = handle;
        }
    } else if handle == 0 {
        device.vertex_shader = 0;
        device.vertex_fvf = 0;
        if let Some(renderer) = device.gpu.as_mut() {
            renderer.vertex_shader = 0;
        }
    } else if device
        .shaders
        .get(&handle)
        .is_some_and(|shader| !shader.pixel)
    {
        device.vertex_shader = handle;
        device.vertex_fvf = 0;
        if let Some(renderer) = device.gpu.as_mut() {
            renderer.vertex_shader = handle;
        }
    } else {
        if handle > 0xf000_0000 {
            return status(
                StatusCode::InvalidArgument,
                "SetVertexShader: invalid shader handle or stage",
            );
        }
        if let Err(error) = crate::d3d8::fixed_function::FvfLayout::decode(handle) {
            return status_from_error(error);
        }
        device.vertex_shader = 0;
        device.vertex_fvf = handle;
        if let Some(renderer) = device.gpu.as_mut() {
            renderer.vertex_shader = 0;
        }
    }
    status(StatusCode::Ok, "")
}

pub fn delete_shader(device: &mut Device, pixel: bool, handle: u32) -> Status {
    if !device
        .shaders
        .get(&handle)
        .is_some_and(|shader| shader.pixel == pixel)
    {
        return status(
            StatusCode::InvalidArgument,
            "DeleteShader: invalid shader handle",
        );
    }
    if let Some(renderer) = device.gpu.as_mut() {
        renderer.retire_shader(handle);
        if pixel && renderer.pixel_shader == handle {
            renderer.pixel_shader = 0;
        }
        if !pixel && renderer.vertex_shader == handle {
            renderer.vertex_shader = 0;
        }
        renderer.shaders.remove(&handle);
    }
    if pixel && device.pixel_shader == handle {
        device.pixel_shader = 0;
    }
    if !pixel && device.vertex_shader == handle {
        device.vertex_shader = 0;
    }
    device.shaders.remove(&handle);
    status(StatusCode::Ok, "")
}

pub fn selected_shader(device: &Device, pixel: bool) -> u32 {
    if pixel {
        device.pixel_shader
    } else if device.vertex_shader != 0 {
        device.vertex_shader
    } else {
        device.vertex_fvf
    }
}

pub fn set_shader_constants(
    device: &mut Device,
    pixel: bool,
    register_index: u32,
    values: &[u32],
) -> Status {
    let constants = if pixel {
        &mut device.pixel_constants
    } else {
        &mut device.vertex_constants
    };
    if values.len() % 4 != 0
        || (!values.is_empty()
            && (register_index as usize > constants.len() / 4
                || values.len() / 4 > constants.len() / 4 - register_index as usize))
    {
        return status(
            StatusCode::InvalidArgument,
            "SetShaderConstant: invalid register range",
        );
    }
    if values.is_empty() {
        return status(StatusCode::Ok, "");
    }
    let start = register_index as usize * 4;
    constants[start..start + values.len()].copy_from_slice(values);
    if let Some(renderer) = device.gpu.as_mut() {
        let bank = if pixel {
            &mut renderer.shader_uniform.pc[..]
        } else {
            &mut renderer.shader_uniform.vc[..]
        };
        for (index, chunk) in values.chunks_exact(4).enumerate() {
            bank[register_index as usize + index] =
                std::array::from_fn(|component| f32::from_bits(chunk[component]));
        }
    }
    status(StatusCode::Ok, "")
}

pub fn get_shader_constants(
    device: &Device,
    pixel: bool,
    register_index: u32,
    count: u32,
) -> ConstantRead {
    let constants = if pixel {
        &device.pixel_constants
    } else {
        &device.vertex_constants
    };
    if count == 0 {
        return ConstantRead {
            status: status(StatusCode::Ok, ""),
            values: Vec::new(),
        };
    }
    let start = register_index as usize * 4;
    let len = count as usize * 4;
    if start > constants.len() || len > constants.len() - start {
        return ConstantRead {
            status: status(
                StatusCode::InvalidArgument,
                "GetShaderConstant: invalid register range",
            ),
            values: Vec::new(),
        };
    }
    ConstantRead {
        status: status(StatusCode::Ok, ""),
        values: constants[start..start + len].to_vec(),
    }
}

pub fn shader_tokens(device: &Device, pixel: bool, handle: u32, declaration: bool) -> ValuesResult {
    let Some(shader) = device
        .shaders
        .get(&handle)
        .filter(|shader| shader.pixel == pixel)
    else {
        return ValuesResult {
            status: status(
                StatusCode::InvalidArgument,
                "GetShader: invalid handle or stage",
            ),
            values: Vec::new(),
        };
    };
    if declaration {
        if pixel {
            return ValuesResult {
                status: status(
                    StatusCode::InvalidArgument,
                    "GetShaderDeclaration: pixel shader has no declaration",
                ),
                values: Vec::new(),
            };
        }
        ValuesResult {
            status: status(StatusCode::Ok, ""),
            values: shader.declaration.clone(),
        }
    } else {
        ValuesResult {
            status: status(StatusCode::Ok, ""),
            values: shader.function.clone(),
        }
    }
}

impl Default for Storage {
    fn default() -> Self {
        Self { storage: None }
    }
}
pub fn new_storage() -> Box<Storage> {
    Box::default()
}
pub fn allocate_storage(storage: &mut Storage, size: u32) -> Status {
    match crate::d3d8::resource::CpuStorage::new(size) {
        Ok(value) => {
            storage.storage = Some(value);
            status(StatusCode::Ok, "")
        }
        Err(error) => status_from_error(error),
    }
}
pub fn storage_bytes(storage: &Storage) -> &[u8] {
    storage.storage.as_ref().map_or(&[], |value| &value.bytes)
}
pub fn storage_bytes_mut(storage: &mut Storage) -> &mut [u8] {
    storage
        .storage
        .as_mut()
        .map_or(&mut [], |value| &mut value.bytes)
}
pub fn copy_storage(destination: &mut Storage, source: &Storage) -> Status {
    let (Some(destination), Some(source)) = (destination.storage.as_mut(), source.storage.as_ref())
    else {
        return status(
            StatusCode::InvalidArgument,
            "UpdateTexture: storage is not allocated",
        );
    };
    if destination.bytes.len() != source.bytes.len() {
        return status(
            StatusCode::InvalidArgument,
            "UpdateTexture: storage sizes differ",
        );
    }
    destination.bytes.copy_from_slice(&source.bytes);
    status(StatusCode::Ok, "")
}
pub fn format_bytes(format: u32) -> u32 {
    cpu_format_bytes(format)
}
pub fn block_bytes(format: u32) -> u32 {
    crate::d3d8::format::block_bytes(format)
}
pub fn texture_level_layout(
    width: u32,
    height: u32,
    levels: u32,
    level: u32,
    format: u32,
) -> LevelLayoutResult {
    match level_layout(width, height, levels, level, format) {
        Ok(layout) => LevelLayoutResult {
            status: status(StatusCode::Ok, ""),
            layout: layout.into(),
        },
        Err(error) => LevelLayoutResult {
            status: status_from_error(error),
            layout: LevelLayout::default(),
        },
    }
}

impl Default for LevelLayout {
    fn default() -> Self {
        Self {
            width: 0,
            height: 0,
            pitch: 0,
            size: 0,
            levels: 0,
        }
    }
}
impl From<D3d8LevelLayout> for LevelLayout {
    fn from(layout: D3d8LevelLayout) -> Self {
        Self {
            width: layout.width,
            height: layout.height,
            pitch: layout.pitch,
            size: layout.size,
            levels: layout.levels,
        }
    }
}

pub fn new_device7_state() -> Box<Device7State> {
    Box::default()
}

fn value_result(value: u32) -> ValueResult {
    ValueResult {
        status: status(StatusCode::Ok, ""),
        value,
    }
}
fn invalid_value(message: &str) -> ValueResult {
    ValueResult {
        status: status(StatusCode::InvalidArgument, message),
        value: 0,
    }
}

pub fn d3d7_get_render_state(state: &Device7State, index: u32) -> ValueResult {
    state
        .live
        .render_state
        .get(index as usize)
        .copied()
        .map(value_result)
        .unwrap_or_else(|| invalid_value("GetRenderState: index out of range"))
}
pub fn d3d7_set_render_state(state: &mut Device7State, index: u32, value: u32) -> Status {
    let Some(index) = usize::try_from(index).ok().filter(|i| *i < 256) else {
        return status(
            StatusCode::InvalidArgument,
            "SetRenderState: index out of range",
        );
    };
    if crate::bridge_translate::translate_render_state(index as u32, value).kind
        == D3d7TranslationKind::Invalid
    {
        return status(
            StatusCode::Unsupported,
            format!("SetRenderState: no D3D8 translation for state {index} value {value}"),
        );
    }
    let target = state_write_target(state);
    target.render_state[index] = value;
    target.render_state_set[index] = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_render_state_was_set(state: &Device7State, index: u32) -> bool {
    state
        .live
        .render_state_set
        .get(index as usize)
        .copied()
        .unwrap_or(false)
}

pub fn d3d7_get_texture_stage_state(state: &Device7State, stage: u32, index: u32) -> ValueResult {
    match (
        state.live.texture_stage_state.get(stage as usize),
        state.live.texture_stage_state_set.get(stage as usize),
    ) {
        (Some(values), _) if (index as usize) < values.len() => {
            value_result(values[index as usize])
        }
        _ => invalid_value("GetTextureStageState: stage or index out of range"),
    }
}
pub fn d3d7_set_texture_stage_state(
    state: &mut Device7State,
    stage: u32,
    index: u32,
    value: u32,
) -> Status {
    let (Some(stage), Some(index)) = (
        usize::try_from(stage).ok().filter(|i| *i < 8),
        usize::try_from(index).ok().filter(|i| *i < 256),
    ) else {
        return status(
            StatusCode::InvalidArgument,
            "SetTextureStageState: stage or index out of range",
        );
    };
    if crate::bridge_translate::translate_texture_stage_state(index as u32).count < 0 {
        return status(
            StatusCode::Unsupported,
            format!("SetTextureStageState: no D3D8 translation for state {index}"),
        );
    }
    let target = state_write_target(state);
    target.texture_stage_state[stage][index] = value;
    target.texture_stage_state_set[stage][index] = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_texture_stage_state_was_set(state: &Device7State, stage: u32, index: u32) -> bool {
    state
        .live
        .texture_stage_state_set
        .get(stage as usize)
        .and_then(|row| row.get(index as usize))
        .copied()
        .unwrap_or(false)
}

pub fn d3d7_get_transform(state: &Device7State, index: u32) -> FloatValuesResult {
    match state.live.transforms.get(index as usize) {
        Some(values) => FloatValuesResult {
            status: status(StatusCode::Ok, ""),
            values: values.to_vec(),
        },
        None => FloatValuesResult {
            status: status(
                StatusCode::InvalidArgument,
                "GetTransform: index out of range",
            ),
            values: Vec::new(),
        },
    }
}
pub fn d3d7_set_transform(state: &mut Device7State, index: u32, values: &[f32]) -> Status {
    let Some(index) = usize::try_from(index).ok().filter(|i| *i < 256) else {
        return status(
            StatusCode::InvalidArgument,
            "SetTransform: index out of range",
        );
    };
    if !crate::bridge_translate::translate_transform(index as u32).valid {
        return status(
            StatusCode::Unsupported,
            format!("SetTransform: no D3D8 translation for state {index}"),
        );
    }
    let Ok(matrix) = <&[f32; 16]>::try_from(values) else {
        return status(
            StatusCode::InvalidArgument,
            "SetTransform: expected 16 floats",
        );
    };
    let target = state_write_target(state);
    target.transforms[index] = *matrix;
    target.transform_set[index] = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_transform_was_set(state: &Device7State, index: u32) -> bool {
    state
        .live
        .transform_set
        .get(index as usize)
        .copied()
        .unwrap_or(false)
}

pub fn d3d7_get_viewport(state: &Device7State) -> ViewportResult {
    ViewportResult {
        status: status(StatusCode::Ok, ""),
        viewport: state.live.viewport,
    }
}
pub fn d3d7_set_viewport(state: &mut Device7State, viewport: &Viewport) -> Status {
    let target = state_write_target(state);
    target.viewport = *viewport;
    target.viewport_set = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_viewport_was_set(state: &Device7State) -> bool {
    state.live.viewport_set
}

pub fn d3d7_get_material(state: &Device7State) -> ValuesResult {
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values: state.live.material.to_vec(),
    }
}
pub fn d3d7_set_material(state: &mut Device7State, values: &[u32]) -> Status {
    let Ok(values) = <&[u32; 17]>::try_from(values) else {
        return status(
            StatusCode::InvalidArgument,
            "SetMaterial: expected 17 words",
        );
    };
    let target = state_write_target(state);
    target.material = *values;
    target.material_set = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_material_was_set(state: &Device7State) -> bool {
    state.live.material_set
}

pub fn d3d7_get_light(state: &Device7State, index: u32) -> ValuesResult {
    match state.live.lights.get(index as usize) {
        Some(values) => ValuesResult {
            status: status(StatusCode::Ok, ""),
            values: values.to_vec(),
        },
        None => ValuesResult {
            status: status(StatusCode::InvalidArgument, "GetLight: index out of range"),
            values: Vec::new(),
        },
    }
}
pub fn d3d7_set_light(state: &mut Device7State, index: u32, values: &[u32]) -> Status {
    let Some(index) = usize::try_from(index).ok().filter(|i| *i < 8) else {
        return status(StatusCode::InvalidArgument, "SetLight: index out of range");
    };
    let Ok(values) = <&[u32; 26]>::try_from(values) else {
        return status(StatusCode::InvalidArgument, "SetLight: expected 26 words");
    };
    let target = state_write_target(state);
    target.lights[index] = *values;
    target.light_set[index] = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_get_light_enable(state: &Device7State, index: u32) -> ValueResult {
    state
        .live
        .light_enable
        .get(index as usize)
        .copied()
        .map(value_result)
        .unwrap_or_else(|| invalid_value("GetLightEnable: index out of range"))
}
pub fn d3d7_set_light_enable(state: &mut Device7State, index: u32, value: u32) -> Status {
    let Some(index) = usize::try_from(index).ok().filter(|i| *i < 8) else {
        return status(
            StatusCode::InvalidArgument,
            "SetLightEnable: index out of range",
        );
    };
    let target = state_write_target(state);
    target.light_enable[index] = u32::from(value != 0);
    target.light_enable_set[index] = true;
    status(StatusCode::Ok, "")
}
pub fn d3d7_get_texture(state: &Device7State, stage: u32) -> ValueResult {
    state
        .live
        .textures
        .get(stage as usize)
        .copied()
        .map(value_result)
        .unwrap_or_else(|| invalid_value("GetTexture: stage out of range"))
}
pub fn d3d7_set_texture(state: &mut Device7State, stage: u32, object_id: u32) -> TextureSetResult {
    let Some(stage) = usize::try_from(stage).ok().filter(|i| *i < 8) else {
        return TextureSetResult {
            status: status(
                StatusCode::InvalidArgument,
                "SetTexture: stage out of range",
            ),
            old_id: 0,
            new_id: object_id,
            recording: state.recording.is_some(),
        };
    };
    let recording = state.recording.is_some();
    let old_id = if recording {
        state.recording.as_ref().unwrap().texture_set[stage]
            .then_some(state.recording.as_ref().unwrap().textures[stage])
            .unwrap_or(0)
    } else {
        state.live.textures[stage]
    };
    let target = state_write_target(state);
    target.textures[stage] = object_id;
    target.texture_set[stage] = true;
    TextureSetResult {
        status: status(StatusCode::Ok, ""),
        old_id,
        new_id: object_id,
        recording,
    }
}
pub fn d3d7_get_render_target(state: &Device7State) -> u32 {
    state.live.render_target
}
pub fn d3d7_set_render_target(state: &mut Device7State, object_id: u32) {
    state.live.render_target = object_id;
}
pub fn d3d7_recording(state: &Device7State) -> bool {
    state.recording.is_some()
}
pub fn d3d7_in_scene(state: &Device7State) -> bool {
    state.live.in_scene
}
pub fn d3d7_set_in_scene(state: &mut Device7State, value: bool) {
    state.live.in_scene = value;
}
pub fn d3d7_target_dirty(state: &Device7State) -> bool {
    state.live.target_dirty
}
pub fn d3d7_set_target_dirty(state: &mut Device7State, value: bool) {
    state.live.target_dirty = value;
}

pub fn d3d7_begin_state_block(state: &mut Device7State) -> Status {
    if state.recording.is_some() {
        return state_block_status(
            StatusCode::InvalidArgument,
            "D3DERR_INBEGINSTATEBLOCK",
            StateBlockError::InBeginStateBlock,
        );
    }
    state.recording = Some(Device7Data::default());
    status(StatusCode::Ok, "")
}
pub fn d3d7_end_state_block(state: &mut Device7State) -> HandleResult {
    if state.recording.is_none() {
        return HandleResult {
            status: state_block_status(
                StatusCode::InvalidArgument,
                "D3DERR_NOTINBEGINSTATEBLOCK",
                StateBlockError::NotInBeginStateBlock,
            ),
            handle: 0,
        };
    }
    if state.next_state_block == u32::MAX {
        return HandleResult {
            status: state_block_status(
                StatusCode::OutOfMemory,
                "EndStateBlock: handle range exhausted",
                StateBlockError::OutOfMemory,
            ),
            handle: 0,
        };
    }
    let recorded = state.recording.take().unwrap();
    let handle = state.next_state_block;
    state.next_state_block += 1;
    state.state_blocks.insert(handle, recorded);
    HandleResult {
        status: status(StatusCode::Ok, ""),
        handle,
    }
}
pub fn d3d7_apply_state_block(state: &mut Device7State, handle: u32) -> ApplyStateBlockResult {
    if state.recording.is_some() {
        return ApplyStateBlockResult {
            status: state_block_status(
                StatusCode::InvalidArgument,
                "D3DERR_INBEGINSTATEBLOCK",
                StateBlockError::InBeginStateBlock,
            ),
            bindings: TextureBindings {
                old_values: Vec::new(),
                new_values: Vec::new(),
            },
        };
    }
    let Some(patch) = state.state_blocks.get(&handle).cloned() else {
        return ApplyStateBlockResult {
            status: state_block_status(
                StatusCode::InvalidArgument,
                "D3DERR_INVALIDSTATEBLOCK",
                StateBlockError::InvalidStateBlock,
            ),
            bindings: TextureBindings {
                old_values: Vec::new(),
                new_values: Vec::new(),
            },
        };
    };
    let old_values = state.live.textures.to_vec();
    for i in 0..256 {
        if patch.render_state_set[i] {
            state.live.render_state[i] = patch.render_state[i];
            state.live.render_state_set[i] = true;
        }
        if patch.transform_set[i] {
            state.live.transforms[i] = patch.transforms[i];
            state.live.transform_set[i] = true;
        }
        for stage in 0..8 {
            if patch.texture_stage_state_set[stage][i] {
                state.live.texture_stage_state[stage][i] = patch.texture_stage_state[stage][i];
                state.live.texture_stage_state_set[stage][i] = true;
            }
        }
    }
    if patch.viewport_set {
        state.live.viewport = patch.viewport;
        state.live.viewport_set = true;
    }
    if patch.material_set {
        state.live.material = patch.material;
        state.live.material_set = true;
    }
    for i in 0..8 {
        if patch.light_set[i] {
            state.live.lights[i] = patch.lights[i];
            state.live.light_set[i] = true;
        }
        if patch.light_enable_set[i] {
            state.live.light_enable[i] = patch.light_enable[i];
            state.live.light_enable_set[i] = true;
        }
        if patch.texture_set[i] {
            state.live.textures[i] = patch.textures[i];
            state.live.texture_set[i] = true;
        }
    }
    ApplyStateBlockResult {
        status: status(StatusCode::Ok, ""),
        bindings: TextureBindings {
            old_values,
            new_values: state.live.textures.to_vec(),
        },
    }
}
pub fn d3d7_delete_state_block(state: &mut Device7State, handle: u32) -> ValuesResult {
    let Some(block) = state.state_blocks.remove(&handle) else {
        return ValuesResult {
            status: state_block_status(
                StatusCode::InvalidArgument,
                "D3DERR_INVALIDSTATEBLOCK",
                StateBlockError::InvalidStateBlock,
            ),
            values: Vec::new(),
        };
    };
    let values = (0..8)
        .filter(|i| block.texture_set[*i])
        .map(|i| block.textures[i])
        .collect();
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values,
    }
}

pub fn d3d7_release_refs(state: &mut Device7State) -> ValuesResult {
    let mut values = Vec::new();
    values.extend(state.live.textures.iter().copied());
    if let Some(recording) = state.recording.take() {
        values.extend(
            (0..8)
                .filter(|i| recording.texture_set[*i])
                .map(|i| recording.textures[i]),
        );
    }
    for block in state.state_blocks.values() {
        values.extend(
            (0..8)
                .filter(|i| block.texture_set[*i])
                .map(|i| block.textures[i]),
        );
    }
    state.state_blocks.clear();
    state.live.textures.fill(0);
    state.live.texture_set.fill(false);
    ValuesResult {
        status: status(StatusCode::Ok, ""),
        values,
    }
}

pub fn d3d7_state_block_render_state_was_set(
    state: &Device7State,
    handle: u32,
    index: u32,
) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.render_state_set.get(index as usize))
        .copied()
        .unwrap_or(false)
}
pub fn d3d7_state_block_texture_stage_state_was_set(
    state: &Device7State,
    handle: u32,
    stage: u32,
    index: u32,
) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.texture_stage_state_set.get(stage as usize))
        .and_then(|v| v.get(index as usize))
        .copied()
        .unwrap_or(false)
}
pub fn d3d7_state_block_transform_was_set(state: &Device7State, handle: u32, index: u32) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.transform_set.get(index as usize))
        .copied()
        .unwrap_or(false)
}
pub fn d3d7_state_block_viewport_was_set(state: &Device7State, handle: u32) -> bool {
    state
        .state_blocks
        .get(&handle)
        .is_some_and(|b| b.viewport_set)
}
pub fn d3d7_state_block_material_was_set(state: &Device7State, handle: u32) -> bool {
    state
        .state_blocks
        .get(&handle)
        .is_some_and(|b| b.material_set)
}
pub fn d3d7_state_block_light_was_set(state: &Device7State, handle: u32, index: u32) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.light_set.get(index as usize))
        .copied()
        .unwrap_or(false)
}
pub fn d3d7_state_block_light_enable_was_set(
    state: &Device7State,
    handle: u32,
    index: u32,
) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.light_enable_set.get(index as usize))
        .copied()
        .unwrap_or(false)
}
pub fn d3d7_state_block_texture_was_set(state: &Device7State, handle: u32, stage: u32) -> bool {
    state
        .state_blocks
        .get(&handle)
        .and_then(|b| b.texture_set.get(stage as usize))
        .copied()
        .unwrap_or(false)
}
