//! Bounded software fixed-function vertex lighting. Guest stream bytes are
//! never modified. Output XYZ/float RGBA/UV feeds the existing raster shaders.
//! Specular and vertex blending remain named draw rejections.
use super::*;
use crate::d3d8::fixed_function::d3dcolor_to_rgba;

fn dot(a: [f32; 3], b: [f32; 3]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn normalize(v: [f32; 3]) -> [f32; 3] {
    let length = dot(v, v).sqrt();
    if length == 0.0 {
        [0.0; 3]
    } else {
        v.map(|x| x / length)
    }
}
fn transform(m: Mat4, v: [f32; 3], w: f32) -> [f32; 3] {
    let v = m.transform([v[0], v[1], v[2], w]);
    [v[0], v[1], v[2]]
}

// Row-vector normal transform n * inverse(world * view)^T. Translation
// does not participate. Preserve normal length unless NORMALIZENORMALS is on.
fn inverse3(m: Mat4) -> Result<[[f32; 3]; 3], RenderError> {
    let [[a, b, c, _], [d, e, f, _], [g, h, i, _], _] = m.rows;
    let cofactors = [
        [e * i - f * h, c * h - b * i, b * f - c * e],
        [f * g - d * i, a * i - c * g, c * d - a * f],
        [d * h - e * g, b * g - a * h, a * e - b * d],
    ];
    let det = a * cofactors[0][0] + b * cofactors[1][0] + c * cofactors[2][0];
    if det == 0.0 || !det.is_finite() {
        return Err(RenderError::new(
            "vertex lighting",
            "singular/nonfinite world-view normal transform",
        ));
    }
    Ok(cofactors.map(|row| row.map(|v| v / det)))
}

/// Guest vertex layout consumed by [`DeviceState::light_vertices`].
///
/// The output is always the fixed 36-byte lit layout from
/// [`lit_layout`](super::fixed_function::lit_layout): XYZ at 0, float RGBA
/// diffuse at 12 and the 2-float texture coordinate at 28.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LitInput {
    /// Bytes between consecutive input vertices.
    pub stride: usize,
    /// Byte offset of the 3-float normal.
    pub normal_offset: usize,
    /// Byte offset of the `D3DCOLOR` diffuse, or `None` when the FVF has no
    /// COLOR1 and the material supplies it.
    pub diffuse_offset: Option<usize>,
    /// Byte offset of the 2-float texture coordinate.
    pub uv_offset: usize,
}

impl LitInput {
    /// `XYZ | NORMAL | DIFFUSE | TEX1` (0x152): diffuse at 24, UV at 28.
    pub const XYZ_NORMAL_DIFFUSE_TEX1: Self = Self {
        stride: 36,
        normal_offset: 12,
        diffuse_offset: Some(24),
        uv_offset: 28,
    };

    /// `XYZ | NORMAL | TEX1` (0x112): no per-vertex diffuse, UV at 24.
    pub const XYZ_NORMAL_TEX1: Self = Self {
        stride: 32,
        normal_offset: 12,
        diffuse_offset: None,
        uv_offset: 24,
    };
}

impl DeviceState {
    /// 0x152: XYZ at 0, NORMAL at 12, D3DCOLOR at 24, float2 UV at 28.
    /// 0x112: XYZ at 0, NORMAL at 12, float2 UV at 24, material diffuse.
    /// Evaluate in camera space and keep floating diffuse until rasterization.
    /// Equations: Microsoft Mathematics of Lighting / Diffuse Lighting /
    /// Attenuation and Spotlight Factor (fixed-function D3D8/9 model).
    /// Writes exactly `end * 36` bytes into `out`, overwriting every byte of
    /// the `start..end` vertex range. `out` is a caller-owned scratch buffer so
    /// a draw loop reuses one allocation; `resize` only zero-fills the first
    /// time a larger draw grows it.
    pub(crate) fn light_vertices(
        &self,
        bytes: &[u8],
        start: usize,
        end: usize,
        layout: LitInput,
        out: &mut Vec<u8>,
    ) -> Result<(), RenderError> {
        let s = &self.states;
        let enabled: Vec<_> = self
            .lights
            .iter()
            .zip(self.light_enabled)
            .filter_map(|(light, on)| on.then_some(light))
            .collect();
        if s.lighting {
            for light in &enabled {
                if !(1..=3).contains(&light.light_type) {
                    return Err(RenderError::new(
                        "vertex lighting",
                        format!("unsupported enabled D3DLIGHTTYPE {}", light.light_type),
                    ));
                }
            }
        }
        let world_view = self.world.mul(self.view);
        let normal_matrix = if s.lighting {
            Some(inverse3(world_view)?)
        } else {
            None
        };
        let material_source = |source, material, vertex: Option<[f32; 4]>| {
            // COLOR1 with no diffuse component (0x112) and COLOR2 in every lit
            // FVF fall back to the material, matching D3D8's
            // D3DMCS_* material-source rule for a missing vertex colour.
            if s.color_vertex && source == D3DMATERIALCOLORSOURCE::Color1 {
                vertex.unwrap_or(material)
            } else {
                material
            }
        };
        let global_ambient = d3dcolor_to_rgba(s.ambient);
        out.resize(end * 36, 0);
        for index in start..end {
            let begin = index * layout.stride;
            let vertex = &bytes[begin..begin + layout.stride];
            let float = |offset| f32::from_le_bytes(vertex[offset..offset + 4].try_into().unwrap());
            let position = [float(0), float(4), float(8)];
            let color = layout.diffuse_offset.map(|offset| {
                d3dcolor_to_rgba(u32::from_le_bytes(
                    vertex[offset..offset + 4].try_into().unwrap(),
                ))
            });
            let diffuse = material_source(s.diffuse_material_source, self.material.diffuse, color);
            let mut result = if s.lighting {
                let ambient =
                    material_source(s.ambient_material_source, self.material.ambient, color);
                let emissive =
                    material_source(s.emissive_material_source, self.material.emissive, color);
                let normal = [
                    float(layout.normal_offset),
                    float(layout.normal_offset + 4),
                    float(layout.normal_offset + 8),
                ];
                let mut normal = normal_matrix.unwrap().map(|row| dot(row, normal));
                if s.normalize_normals {
                    normal = normalize(normal);
                }
                let position = transform(world_view, position, 1.0);
                let mut result = [0.0; 4];
                for c in 0..3 {
                    result[c] = emissive[c] + ambient[c] * global_ambient[c];
                }
                // Lighting changes RGB; alpha comes from the diffuse source.
                result[3] = diffuse[3];
                for light in &enabled {
                    let (direction, mut attenuation) = if light.light_type == 3 {
                        (
                            normalize(transform(self.view, light.direction, 0.0)).map(|v| -v),
                            1.0,
                        )
                    } else {
                        let light_position = transform(self.view, light.position, 1.0);
                        let delta = std::array::from_fn(|c| light_position[c] - position[c]);
                        let distance = dot(delta, delta).sqrt();
                        if distance > light.range {
                            continue;
                        }
                        let denominator = light.attenuation0
                            + light.attenuation1 * distance
                            + light.attenuation2 * distance * distance;
                        if denominator <= 0.0 || !denominator.is_finite() {
                            return Err(RenderError::new(
                                "vertex lighting",
                                "nonpositive/nonfinite point/spot attenuation denominator",
                            ));
                        }
                        (normalize(delta), 1.0 / denominator)
                    };
                    if light.light_type == 2 {
                        let spot_direction = normalize(transform(self.view, light.direction, 0.0));
                        let rho = -dot(direction, spot_direction);
                        let inner = (light.theta * 0.5).cos();
                        let outer = (light.phi * 0.5).cos();
                        let spot = if rho > inner {
                            1.0
                        } else if rho <= outer {
                            0.0
                        } else {
                            ((rho - outer) / (inner - outer)).powf(light.falloff)
                        };
                        attenuation *= spot;
                    }
                    let lambert = dot(normal, direction).max(0.0);
                    for c in 0..3 {
                        result[c] += attenuation
                            * (ambient[c] * light.ambient[c]
                                + diffuse[c] * light.diffuse[c] * lambert);
                    }
                }
                result
            } else if let Some(color) = color.filter(|_| s.color_vertex) {
                color
            } else {
                self.material.diffuse
            };
            for value in &mut result {
                *value = value.clamp(0.0, 1.0);
            }
            let dst = &mut out[index * 36..(index + 1) * 36];
            dst[..12].copy_from_slice(&vertex[..12]);
            dst[12..28].copy_from_slice(bytemuck::bytes_of(&result));
            dst[28..36].copy_from_slice(&vertex[layout.uv_offset..layout.uv_offset + 8]);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn vertex(normal: [f32; 3], color: u32) -> Vec<u8> {
        let mut bytes = Vec::new();
        for value in [0.0f32, 0.0, 0.5].into_iter().chain(normal) {
            bytes.extend(value.to_le_bytes());
        }
        bytes.extend(color.to_le_bytes());
        bytes.extend([0; 8]);
        bytes
    }
    fn lit_color(state: &DeviceState, normal: [f32; 3], color: u32) -> [f32; 4] {
        let mut out = Vec::new();
        state
            .light_vertices(
                &vertex(normal, color),
                0,
                1,
                LitInput::XYZ_NORMAL_DIFFUSE_TEX1,
                &mut out,
            )
            .unwrap();
        std::array::from_fn(|c| f32::from_le_bytes(out[12 + c * 4..16 + c * 4].try_into().unwrap()))
    }

    fn vertex_xyz_normal_tex1(normal: [f32; 3], uv: [f32; 2]) -> Vec<u8> {
        let mut bytes = Vec::new();
        for value in [0.0f32, 0.0, 0.5].into_iter().chain(normal) {
            bytes.extend(value.to_le_bytes());
        }
        for value in uv {
            bytes.extend(value.to_le_bytes());
        }
        bytes
    }
    fn directional() -> DeviceState {
        let mut state = DeviceState::new(32, 32);
        state
            .set_light(
                0,
                Light {
                    light_type: 3,
                    direction: [0.0, 0.0, -1.0],
                    diffuse: [1.0; 4],
                    ..Light::default()
                },
            )
            .unwrap();
        state.light_enable(0, true).unwrap();
        state
    }
    #[test]
    fn reused_scratch_truncates_and_matches_a_fresh_lighting() {
        let state = directional();
        let mut scratch = Vec::new();
        // Fill the scratch with two vertices first, then light one vertex into
        // the same buffer. The result must equal a fresh one-vertex pass and
        // must not expose the previous second vertex.
        let two = [
            vertex([0.0, 0.0, 1.0], 0x80402010),
            vertex([0.0, 0.0, -1.0], 0xff00ff00),
        ]
        .concat();
        state
            .light_vertices(&two, 0, 2, LitInput::XYZ_NORMAL_DIFFUSE_TEX1, &mut scratch)
            .unwrap();
        assert_eq!(scratch.len(), 72);
        let one = vertex([0.0, 0.0, -1.0], 0xff00ff00);
        state
            .light_vertices(&one, 0, 1, LitInput::XYZ_NORMAL_DIFFUSE_TEX1, &mut scratch)
            .unwrap();
        let mut fresh = Vec::new();
        state
            .light_vertices(&one, 0, 1, LitInput::XYZ_NORMAL_DIFFUSE_TEX1, &mut fresh)
            .unwrap();
        assert_eq!(scratch, fresh);
        assert_eq!(scratch.len(), 36);
    }

    #[test]
    fn xyz_normal_tex1_uses_material_and_copies_uv() {
        // 0x112 has no COLOR1, so its diffuse must equal the material diffuse
        // (default opaque white), which the 0x152 helper can express with a
        // white vertex colour.
        let state = directional();
        let mut out152 = Vec::new();
        state
            .light_vertices(
                &vertex([0.0, 0.0, 1.0], 0xffff_ffff),
                0,
                1,
                LitInput::XYZ_NORMAL_DIFFUSE_TEX1,
                &mut out152,
            )
            .unwrap();
        let mut out112 = Vec::new();
        state
            .light_vertices(
                &vertex_xyz_normal_tex1([0.0, 0.0, 1.0], [0.25, 0.5]),
                0,
                1,
                LitInput::XYZ_NORMAL_TEX1,
                &mut out112,
            )
            .unwrap();
        assert_eq!(out112.len(), 36);
        assert_eq!(&out112[..12], &out152[..12]);
        assert_eq!(&out112[12..28], &out152[12..28]);
        let uv = [0.25f32, 0.5]
            .into_iter()
            .flat_map(f32::to_le_bytes)
            .collect::<Vec<_>>();
        assert_eq!(&out112[28..36], uv.as_slice());
    }

    #[test]
    fn directional_front_back_and_diffuse_alpha() {
        let state = directional();
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0x80402010),
            d3dcolor_to_rgba(0x80402010)
        );
        assert_eq!(
            lit_color(&state, [0.0, 0.0, -1.0], 0x80402010),
            [0.0, 0.0, 0.0, 128.0 / 255.0]
        );
    }
    #[test]
    fn normal_inverse_transpose_and_normalization() {
        let mut state = directional();
        state.world = Mat4::scale(1.0, 1.0, 4.0);
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff),
            [0.25, 0.25, 0.25, 1.0]
        );
        state.set_render_state(143, 1).unwrap();
        assert_eq!(lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff), [1.0; 4]);
        state.view = Mat4::translation(10.0, 20.0, 30.0);
        assert_eq!(lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff), [1.0; 4]);
    }
    #[test]
    fn material_sources_ambient_emissive_and_disabled_lights() {
        let mut state = directional();
        state.light_enable(0, false).unwrap();
        state.material.ambient = [0.5; 4];
        state.material.emissive = [0.125; 4];
        state.states.ambient = 0xffffffff;
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0x80ffffff),
            [0.625, 0.625, 0.625, 128.0 / 255.0]
        );
        state.states.color_vertex = false;
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0),
            [0.625, 0.625, 0.625, 1.0]
        );
        state.states.emissive_material_source = D3DMATERIALCOLORSOURCE::Color2;
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0),
            [0.625, 0.625, 0.625, 1.0]
        );
    }
    #[test]
    fn point_attenuation_range_and_spot_cone() {
        let mut state = directional();
        state.lights[0] = Light {
            light_type: 1,
            position: [0.0, 0.0, 2.5],
            range: 3.0,
            attenuation1: 1.0,
            diffuse: [1.0; 4],
            ..Light::default()
        };
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff),
            [0.5, 0.5, 0.5, 1.0]
        );
        state.lights[0].range = 1.0;
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff),
            [0.0, 0.0, 0.0, 1.0]
        );
        state.lights[0].range = 3.0;
        state.lights[0].light_type = 2;
        state.lights[0].direction = [0.0, 0.0, -1.0];
        state.lights[0].theta = 0.5;
        state.lights[0].phi = 1.0;
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff),
            [0.5, 0.5, 0.5, 1.0]
        );
        state.lights[0].direction = [0.0, 0.0, 1.0];
        assert_eq!(
            lit_color(&state, [0.0, 0.0, 1.0], 0xffffffff),
            [0.0, 0.0, 0.0, 1.0]
        );
    }
}
