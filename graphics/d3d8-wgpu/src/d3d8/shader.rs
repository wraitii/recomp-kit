//! Shader-model 1 token translation. Token layouts are from the pinned Wine
//! d3d8types.h; texbem follows Wine's shader_glsl_texbem. No guest pointers.
//! Unsupported versions, instructions and declarations fail during creation.
use super::fixed_function::{FvfLayout, TEXTURED_WGSL, lit_shader_source};
use crate::RenderError;

fn fail(s: impl Into<String>) -> RenderError {
    RenderError::new("D3D8 shader", s)
}

#[derive(Clone, Debug)]
pub struct Declaration {
    pub layout: FvfLayout,
    fields: Vec<(u32, u32)>,
    pub constants: Vec<(usize, [f32; 4])>,
}
impl Declaration {
    /// Decode a copied declaration, checking stream layout and constant bounds.
    pub fn parse(words: &[u32]) -> Result<Self, RenderError> {
        let mut attributes = Vec::new();
        let mut fields = Vec::new();
        let mut constants = Vec::new();
        let mut offset = 0;
        let mut stream = None;
        let mut i = 0;
        while i < words.len() {
            let t = words[i];
            i += 1;
            if t == u32::MAX {
                if i != words.len() || fields.is_empty() {
                    return Err(fail("invalid declaration END"));
                }
                return Ok(Self {
                    layout: FvfLayout {
                        stride: offset,
                        attributes,
                        pre_transformed: false,
                        texcoord_sets: 2,
                    },
                    fields,
                    constants,
                });
            }
            match t >> 29 {
                0 if t == 0 => {}
                1 if t == 0x20000000 => stream = Some(0),
                2 if stream == Some(0) => {
                    if t & 0x10000000 != 0 {
                        offset += u64::from((t >> 16) & 15) * 4;
                        continue;
                    }
                    let reg = t & 31;
                    let ty = (t >> 16) & 15;
                    if reg >= 16 || fields.iter().any(|(r, _)| *r == reg) {
                        return Err(fail("duplicate/out-of-range declaration register"));
                    }
                    let format = match ty {
                        0 => wgpu::VertexFormat::Float32,
                        1 => wgpu::VertexFormat::Float32x2,
                        2 => wgpu::VertexFormat::Float32x3,
                        3 => wgpu::VertexFormat::Float32x4,
                        4 => wgpu::VertexFormat::Uint32,
                        5 => wgpu::VertexFormat::Uint8x4,
                        6 => wgpu::VertexFormat::Sint16x2,
                        7 => wgpu::VertexFormat::Sint16x4,
                        _ => return Err(fail(format!("declaration data type {ty}"))),
                    };
                    attributes.push(wgpu::VertexAttribute {
                        format,
                        offset,
                        shader_location: reg,
                    });
                    offset += format.size();
                    fields.push((reg, ty));
                }
                4 => {
                    let start = (t & 127) as usize;
                    let n = ((t >> 25) & 15) as usize;
                    if start + n > 96 || i + n * 4 > words.len() {
                        return Err(fail("declaration constant range"));
                    }
                    for k in 0..n {
                        constants.push((
                            start + k,
                            std::array::from_fn(|c| f32::from_bits(words[i + k * 4 + c])),
                        ));
                    }
                    i += n * 4;
                }
                _ => {
                    return Err(fail(format!(
                        "unsupported declaration token {t:#010x} (only stream 0 is supported)"
                    )));
                }
            }
        }
        Err(fail("unterminated declaration"))
    }
    /// Declaration-only shaders select fixed-function processing. Restrict
    /// this mapping to existing FVFs, including byte offsets and stride.
    pub fn fixed_fvf(&self) -> Result<u32, RenderError> {
        for fvf in [0x42, 0x142, 0x242, 0x112, 0x152] {
            let layout = FvfLayout::decode(fvf)?;
            let expected: Vec<_> = match fvf {
                0x42 => vec![(0, 2), (5, 4)],
                0x142 => vec![(0, 2), (5, 4), (7, 1)],
                0x242 => vec![(0, 2), (5, 4), (7, 1), (8, 1)],
                0x112 => vec![(0, 2), (3, 2), (7, 1)],
                _ => vec![(0, 2), (3, 2), (5, 4), (7, 1)],
            };
            if self.fields == expected
                && self.layout.stride == layout.stride
                && self
                    .layout
                    .attributes
                    .iter()
                    .zip(expected.iter())
                    .all(|(a, (_, ty))| {
                        a.format.size()
                            == match ty {
                                2 => 12,
                                1 => 8,
                                _ => 4,
                            }
                    })
            {
                // No SKIP padding may occur between fields either.
                let mut offset = 0;
                let packed = self.layout.attributes.iter().all(|a| {
                    let ok = a.offset == offset;
                    offset += a.format.size();
                    ok
                });
                if packed {
                    return Ok(fvf);
                }
            }
        }
        Err(fail(
            "declaration-only shader layout has no supported fixed-function FVF",
        ))
    }
    fn inputs(&self) -> (String, String) {
        let mut fields = String::from("struct VertexInput {\n");
        let mut loads = String::new();
        for &(r, ty) in &self.fields {
            let typ = match ty {
                0 => "f32",
                1 => "vec2<f32>",
                2 => "vec3<f32>",
                3 => "vec4<f32>",
                4 => "u32",
                5 => "vec4<u32>",
                6 => "vec2<i32>",
                _ => "vec4<i32>",
            };
            fields += &format!("@location({r}) v{r}: {typ},\n");
            let expr = match ty {
                0 => format!("vec4<f32>(in.v{r}, 0.0, 0.0, 1.0)"),
                1 | 6 => format!("vec4<f32>(vec2<f32>(in.v{r}), 0.0, 1.0)"),
                2 => format!("vec4<f32>(in.v{r}, 1.0)"),
                4 => format!(
                    "vec4<f32>(f32((in.v{r} >> 16u) & 255u), f32((in.v{r} >> 8u) & 255u), f32(in.v{r} & 255u), f32(in.v{r} >> 24u)) / 255.0"
                ),
                _ => format!("vec4<f32>(in.v{r})"),
            };
            loads += &format!("v[{r}] = {expr};\n");
        }
        fields += "};\n";
        (fields, loads)
    }
}

#[derive(Clone, Debug)]
pub struct Program {
    pub pixel: bool,
    pub declaration_only: bool,
    pub body: String,
    pub samplers: [bool; 4],
}
fn register(t: u32, pixel: bool, dest: bool) -> Result<String, RenderError> {
    if t & 0x80000000 == 0 {
        return Err(fail("parameter missing high bit"));
    }
    let ty = (t >> 28) & 7;
    let n = t & 0x7ff;
    let reg = match (ty, n, pixel) {
        (0, 0..=1, true) | (0, 0..=11, false) => format!("r[{n}]"),
        (1, 0..=1, true) | (1, 0..=15, false) if !dest => format!("v[{n}]"),
        (2, 0..=7, true) | (2, 0..=95, false) if !dest => {
            if t & 0x2000 != 0 {
                if pixel {
                    return Err(fail("relative pixel constant"));
                }
                // DIVERGENCE(original): out-of-range relative constant indices
                // clamp to the available bank; original GPU behavior is unknown.
                format!("c[u32(clamp(i32(a.x) + {n}, 0, 95))]")
            } else {
                format!("c[{n}]")
            }
        }
        (2, 0..=7, true) | (2, 0..=95, false) if dest => format!("c[{n}]"),
        (3, 0..=3, true) => format!("t[{n}]"),
        (3, 0, false) => "a".into(),
        (4, 0, false) if dest => "opos".into(),
        (4, 1, false) if dest => "ofog".into(),
        (5, 0..=1, false) if dest => format!("od[{n}]"),
        (6, 0..=7, false) if dest => format!("ot[{n}]"),
        _ => {
            return Err(fail(format!(
                "invalid {} register type {ty}, index {n}",
                if dest { "destination" } else { "source" }
            )));
        }
    };
    if t & 0x2000 != 0 && ty != 2 {
        return Err(fail("relative nonconstant register"));
    }
    Ok(reg)
}
fn source(t: u32, pixel: bool) -> Result<String, RenderError> {
    let reg = register(t, pixel, false)?;
    let sw: String = (0..4)
        .map(|i| b"xyzw"[((t >> (16 + i * 2)) & 3) as usize] as char)
        .collect();
    let e = format!("({reg}).{sw}");
    Ok(match (t >> 24) & 15 {
        0 => e,
        1 => format!("-({e})"),
        2 => format!("({e} - vec4<f32>(0.5))"),
        3 => format!("-( {e} - vec4<f32>(0.5))"),
        4 => format!("({e} * 2.0 - vec4<f32>(1.0))"),
        5 => format!("-({e} * 2.0 - vec4<f32>(1.0))"),
        6 => format!("(vec4<f32>(1.0) - {e})"),
        _ => return Err(fail(format!("source modifier {}", (t >> 24) & 15))),
    })
}
impl Program {
    /// Decode one bounded SM1.1 program; unknown instructions fail by name.
    pub fn parse(words: &[u32], pixel: bool) -> Result<Self, RenderError> {
        if words.first().copied() != Some(if pixel { 0xffff0101 } else { 0xfffe0101 }) {
            return Err(fail("only vs.1.1 / ps.1.1 are supported"));
        }
        let mut i = 1;
        let mut body = String::new();
        let mut samplers = [false; 4];
        let mut seq = 0;
        while i < words.len() {
            let ins = words[i];
            i += 1;
            let op = ins & 0xffff;
            if op == 0xffff {
                if i != words.len() {
                    return Err(fail("trailing shader tokens"));
                }
                return Ok(Self {
                    pixel,
                    declaration_only: false,
                    body,
                    samplers,
                });
            }
            if op == 0xfffe {
                i += ((ins >> 16) & 0x7fff) as usize;
                if i > words.len() {
                    return Err(fail("truncated comment"));
                }
                continue;
            }
            if ins & !0xffff != 0 {
                return Err(fail(format!("instruction flags/coissue {ins:#010x}")));
            }
            if op == 0 {
                continue;
            }
            let n = match op {
                1 | 6 | 7 | 14 | 15 | 16 | 19 | 78 | 79 => 1,
                2 | 3 | 5 | 8 | 9 | 10 | 11 | 12 | 13 | 17 | 20..=24 | 67..=70 => 2,
                4 | 18 | 80 => 3,
                64..=66 => 0,
                81 => 4,
                _ => return Err(fail(format!("unsupported opcode {op}"))),
            };
            // TEXBEM / TEXREG have one source, plus destination.
            let n = if (67..=70).contains(&op) { 1 } else { n };
            if i + 1 + n > words.len() {
                return Err(fail("truncated instruction"));
            }
            let dst = words[i];
            let args = &words[i + 1..i + 1 + n];
            i += 1 + n;
            let name = register(dst, pixel, true)?;
            if op == 81 {
                if (dst >> 28) & 7 != 2 {
                    return Err(fail("DEF destination must be a constant"));
                }
                body += &format!(
                    "{name} = bitcast<vec4<f32>>(vec4<u32>({}u,{}u,{}u,{}u));\n",
                    args[0], args[1], args[2], args[3]
                );
                continue;
            }
            if (dst >> 28) & 7 == 2 {
                return Err(fail("constant destination outside DEF"));
            }
            let src: Vec<String> = args
                .iter()
                .map(|&t| source(t, pixel))
                .collect::<Result<_, _>>()?;
            let x = src.first().map(String::as_str).unwrap_or("");
            let y = src.get(1).map(String::as_str).unwrap_or("");
            let z = src.get(2).map(String::as_str).unwrap_or("");
            let expr = match op {
                1 => x.into(),
                2 => format!("({x}+{y})"),
                3 => format!("({x}-{y})"),
                4 => format!("({x}*{y}+{z})"),
                5 => format!("({x}*{y})"),
                6 => format!("vec4<f32>(1.0/({x}).w)"),
                7 => format!("vec4<f32>(inverseSqrt(abs(({x}).w)))"),
                8 | 9 => format!(
                    "vec4<f32>(dot(({x}).{},({y}).{}))",
                    if op == 8 { "xyz" } else { "xyzw" },
                    if op == 8 { "xyz" } else { "xyzw" }
                ),
                10 => format!("min({x},{y})"),
                11 => format!("max({x},{y})"),
                12 | 13 => format!(
                    "select(vec4<f32>(0.0),vec4<f32>(1.0),{x} {} {y})",
                    if op == 12 { "<" } else { ">=" }
                ),
                14 => format!("vec4<f32>(exp2(({x}).w))"),
                15 => format!("vec4<f32>(log2(abs(({x}).w)))"),
                78 => format!("vec4<f32>(exp2(floor(({x}).w)),fract(({x}).w),exp2(({x}).w),1.0)"),
                79 => format!(
                    "vec4<f32>(floor(log2(abs(({x}).w))),abs(({x}).w)/exp2(floor(log2(abs(({x}).w)))),log2(abs(({x}).w)),1.0)"
                ),
                16 => format!(
                    "vec4<f32>(1.0,max(({x}).x,0.0),select(0.0,pow(max(({x}).y,0.0),clamp(({x}).w,-128.0,128.0)),({x}).x>0.0),1.0)"
                ),
                17 => format!("vec4<f32>(1.0,({x}).y*({y}).y,({x}).z,({y}).w)"),
                18 => format!("({x}*{y}+(vec4<f32>(1.0)-{x})*{z})"),
                19 => format!("fract({x})"),
                20..=24 if !pixel => {
                    let rows = match op {
                        20 | 22 => 4,
                        21 | 23 => 3,
                        _ => 2,
                    };
                    let dim = if op <= 21 { "xyzw" } else { "xyz" };
                    let mut e = Vec::new();
                    for row in 0..rows {
                        let s = source(args[1] + row, pixel)?;
                        e.push(format!("dot(({x}).{dim},({s}).{dim})"));
                    }
                    while e.len() < 4 {
                        e.push("0.0".into());
                    }
                    format!("vec4<f32>({})", e.join(","))
                }
                64..=70 if pixel => {
                    let stage = (dst & 0x7ff) as usize;
                    if (dst >> 28) & 7 != 3 || stage >= 4 {
                        return Err(fail("texture instruction destination"));
                    }
                    if op == 65 {
                        body +=
                            &format!("if (any(({name}).xyz < vec3<f32>(0.0))) {{ discard; }}\n");
                        continue;
                    }
                    if op == 64 {
                        format!("clamp({name},vec4<f32>(0.0),vec4<f32>(1.0))")
                    } else {
                        samplers[stage] = true;
                        let uv = match op {
                            66 => format!("({name}).xy"),
                            67 | 68 => format!(
                                "({name}).xy + vec2<f32>(dot(program.bump[{stage}].xz,({x}).xy),dot(program.bump[{stage}].yw,({x}).xy))"
                            ),
                            69 => format!("({x}).wx"),
                            _ => format!("({x}).yz"),
                        };
                        let sample = format!(
                            "textureSampleBias(stage{stage}_tex,stage{stage}_sampler,{uv},program.lod[{stage}].x)"
                        );
                        if op == 68 {
                            format!(
                                "({sample} * (({x}).z * program.lum[{stage}].x + program.lum[{stage}].y))"
                            )
                        } else {
                            sample
                        }
                    }
                }
                80 if pixel => format!("select({z},{y},({x}).a > 0.5)"),
                _ => {
                    return Err(fail(format!(
                        "opcode {op} is invalid for this shader stage"
                    )));
                }
            };
            let mask = (dst >> 16) & 15;
            if mask == 0 {
                return Err(fail("empty destination mask"));
            }
            let shift = (dst >> 24) & 15;
            let scale = match shift {
                0 => 1.0,
                1 => 2.0,
                2 => 4.0,
                3 => 8.0,
                13 => 0.125,
                14 => 0.25,
                15 => 0.5,
                _ => return Err(fail("destination shift")),
            };
            let mut expr = format!("({expr}) * {scale:.3}");
            match (dst >> 20) & 15 {
                0 => {}
                1 => expr = format!("clamp({expr},vec4<f32>(0.0),vec4<f32>(1.0))"),
                _ => return Err(fail("destination modifier")),
            }
            // A temporary preserves reads when source and destination alias,
            // and scalar stores implement masks without WGSL swizzle lvalues.
            body += &format!("let result{seq} = {expr};\n");
            for k in 0..4 {
                if mask & (1 << k) != 0 {
                    let c = b"xyzw"[k] as char;
                    let value = if !pixel && name == "a" {
                        format!("floor(result{seq}.{c})")
                    } else {
                        format!("result{seq}.{c}")
                    };
                    body += &format!("{name}.{c} = {value};\n");
                }
            }
            seq += 1;
        }
        Err(fail("unterminated shader"))
    }
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
pub struct Uniform {
    pub vc: [[f32; 4]; 96],
    pub pc: [[f32; 4]; 8],
    pub bump: [[f32; 4]; 4],
    pub lum: [[f32; 4]; 4],
    pub lod: [[f32; 4]; 4],
}
impl Default for Uniform {
    fn default() -> Self {
        bytemuck::Zeroable::zeroed()
    }
}

fn replace_block(s: &mut String, start: &str, replacement: &str) {
    let begin = s.find(start).expect("shader template marker");
    let brace = s[begin..].find('{').unwrap() + begin;
    let mut depth = 1;
    let mut end = brace + 1;
    while depth > 0 {
        match s.as_bytes()[end] {
            b'{' => depth += 1,
            b'}' => depth -= 1,
            _ => {}
        }
        end += 1;
    }
    if s.as_bytes().get(end) == Some(&b';') {
        end += 1;
    }
    s.replace_range(begin..end, replacement);
}
/// Compose programmable and fixed-function stages independently. Raster fog
/// and alpha testing remain post-fragment operations; specular addition is
/// bypassed for a pixel shader. D3D7 can reuse the token-free fixed stages.
pub fn compose(vs: Option<(&Declaration, &Program)>, ps: Option<&Program>, lit: bool) -> String {
    let mut s = if lit {
        lit_shader_source(true)
    } else {
        TEXTURED_WGSL.into()
    };
    s += "\nstruct ProgramUniform { vc: array<vec4<f32>,96>, pc: array<vec4<f32>,8>, bump: array<vec4<f32>,4>, lum: array<vec4<f32>,4>, lod: array<vec4<f32>,4>, };\n@group(0) @binding(4) var<uniform> program: ProgramUniform;\n@group(1) @binding(4) var stage2_tex: texture_2d<f32>;\n@group(1) @binding(5) var stage2_sampler: sampler;\n@group(1) @binding(6) var stage3_tex: texture_2d<f32>;\n@group(1) @binding(7) var stage3_sampler: sampler;\n";
    s=s.replace("    out.specular = vec3<f32>(\n", "    out.specular_alpha = f32((in.specular >> 24u) & 255u) / 255.0;\n    out.specular = vec3<f32>(\n");
    let out = "    @location(7) uv2: vec2<f32>,\n    @location(8) uv3: vec2<f32>,\n    @location(9) specular_alpha: f32,\n";
    s = s.replace(
        "    @location(6) cam_pos: vec4<f32>,",
        &format!("    @location(6) cam_pos: vec4<f32>,\n{out}"),
    );
    if vs.is_some() {
        s = s.replace("@interpolate(linear) ", "");
    }
    if let Some((decl, p)) = vs {
        let (input, mut loads) = decl.inputs();
        // Wine load_local_constants keeps D3DVSD_CONST values on the shader,
        // independently of the Set/GetVertexShaderConstant device bank.
        for &(reg, value) in &decl.constants {
            let b = value.map(f32::to_bits);
            loads += &format!(
                "c[{reg}]=bitcast<vec4<f32>>(vec4<u32>({}u,{}u,{}u,{}u));\n",
                b[0], b[1], b[2], b[3]
            );
        }
        replace_block(&mut s, "struct VertexInput {", &input);
        let body = format!(
            "@vertex\nfn vs_main(in:VertexInput)->VertexOutput{{\nvar r:array<vec4<f32>,12>;var v:array<vec4<f32>,16>;var c=program.vc;var a=vec4<f32>(0.0);var opos=vec4<f32>(0.0);var ofog=vec4<f32>(1.0);var od:array<vec4<f32>,2>;var ot:array<vec4<f32>,8>;\n{loads}\n{}\nvar out:VertexOutput;out.position=opos;out.color=clamp(od[0],vec4<f32>(0.0),vec4<f32>(1.0));out.specular=clamp(od[1].xyz,vec3<f32>(0.0),vec3<f32>(1.0));out.specular_alpha=clamp(od[1].w,0.0,1.0);out.uv0=ot[0].xy;out.uv1=ot[1].xy;out.uv2=ot[2].xy;out.uv3=ot[3].xy;out.fogdist=abs(opos.w);out.fogfactor=clamp(ofog.x,0.0,1.0);return out;}}",
            p.body
        );
        replace_block(&mut s, "@vertex\nfn vs_main", &body);
        // The fixed RHW entry references the original input independently.
    }
    if let Some(p) = ps {
        let body = format!(
            "@fragment\nfn fs_main(in:VertexOutput)->@location(0) vec4<f32>{{\nvar r:array<vec4<f32>,2>;var v:array<vec4<f32>,2>;v[0]=clamp(in.color,vec4<f32>(0.0),vec4<f32>(1.0));v[1]=clamp(vec4<f32>(in.specular,in.specular_alpha),vec4<f32>(0.0),vec4<f32>(1.0));var c=program.pc;var t:array<vec4<f32>,4>;t[0]=vec4<f32>(in.uv0,0.0,1.0);t[1]=vec4<f32>(in.uv1,0.0,1.0);t[2]=vec4<f32>(in.uv2,0.0,1.0);t[3]=vec4<f32>(in.uv3,0.0,1.0);\n{}\nif(!alpha_test_pass(r[0].a)){{discard;}}return vec4<f32>(apply_fog(r[0].rgb,in.fogdist,in.fogfactor),r[0].a);}}",
            p.body
        );
        let body = if vs.is_none() {
            let mut body = body;
            for stage in 0..4 {
                let init = format!("t[{stage}]=vec4<f32>(in.uv{stage},0.0,1.0);");
                let replace = format!(
                    "t[{stage}]=vec4<f32>(0.0,0.0,0.0,1.0);if(program.lod[{stage}].y==0.0){{t[{stage}]=vec4<f32>(in.uv0,0.0,1.0);}}else if(program.lod[{stage}].y==1.0){{t[{stage}]=vec4<f32>(in.uv1,0.0,1.0);}}"
                );
                body = body.replace(&init, &replace);
            }
            body
        } else {
            body
        };
        replace_block(&mut s, "@fragment\nfn fs_main", &body);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dst(ty: u32, n: u32, mask: u32) -> u32 {
        0x80000000 | (ty << 28) | (mask << 16) | n
    }
    fn src(ty: u32, n: u32) -> u32 {
        0x80e40000 | (ty << 28) | n
    }
    fn validate(s: &str) {
        let module =
            naga::front::wgsl::parse_str(s).unwrap_or_else(|e| panic!("{}", e.emit_to_string(s)));
        naga::valid::Validator::new(
            naga::valid::ValidationFlags::all(),
            naga::valid::Capabilities::empty(),
        )
        .validate(&module)
        .unwrap();
    }
    #[test]
    fn declarations_offsets_and_constants() {
        let d = Declaration::parse(&[
            0x20000000,
            0x40020000,
            0x50010000,
            0x40020003,
            0x40010007,
            0x82000002,
            1,
            2,
            3,
            4,
            u32::MAX,
        ])
        .unwrap();
        assert_eq!(d.layout.stride, 36);
        assert_eq!(d.layout.attributes[1].offset, 16);
        assert_eq!(d.constants[0].0, 2);
        assert_eq!(d.constants[0].1[3].to_bits(), 4);
        assert!(Declaration::parse(&[0x20000001, 0x40020000, u32::MAX]).is_err());
        assert!(Declaration::parse(&[0x20000000, 0x40020000, 0x40020000, u32::MAX]).is_err());
    }
    #[test]
    fn compose_stages_independently_and_water_instructions() {
        let d = Declaration::parse(&[0x20000000, 0x40020000, 0x40020003, 0x40010007, u32::MAX])
            .unwrap();
        let mut words = vec![0xfffe0101];
        for k in 0..4 {
            words.extend([9, dst(4, 0, 1 << k), src(1, 0), src(2, k)]);
        }
        words.extend([
            1,
            dst(5, 0, 15),
            src(2, 5),
            1,
            dst(5, 1, 15),
            src(2, 6),
            1,
            dst(4, 1, 1),
            src(2, 7),
        ]);
        for k in 0..4 {
            words.extend([1, dst(6, k, 3), src(1, 7)]);
        }
        words.push(0xffff);
        let vs = Program::parse(&words, false).unwrap();
        let ps = Program::parse(
            &[
                0xffff0101,
                66,
                dst(3, 0, 15),
                66,
                dst(3, 1, 15),
                67,
                dst(3, 2, 15),
                src(3, 1),
                67,
                dst(3, 3, 15),
                src(3, 1),
                4,
                dst(0, 1, 15),
                src(3, 3) | (4 << 24),
                src(3, 0) | (6 << 24),
                src(3, 0),
                18,
                dst(0, 0, 15),
                src(1, 0),
                src(1, 1),
                src(0, 1),
                0xffff,
            ],
            true,
        )
        .unwrap();
        assert_eq!(ps.samplers, [true; 4]);
        for (v, p, lit) in [
            (Some((&d, &vs)), Some(&ps), false),
            (Some((&d, &vs)), None, false),
            (None, Some(&ps), false),
            (None, Some(&ps), true),
        ] {
            validate(&compose(v, p, lit));
        }
    }
    #[test]
    fn unsupported_and_truncated_tokens_fail_at_creation() {
        for words in [
            vec![0xffff0104, 0xffff],
            vec![0xffff0101, 4, dst(0, 0, 15)],
            vec![0xffff0101, 0x40000001, dst(0, 0, 15), src(1, 0), 0xffff],
            vec![0xffff0101, 71, 0xffff],
        ] {
            assert!(Program::parse(&words, true).is_err());
        }
        assert!(
            Program::parse(&[0xfffe0101, 1, dst(0, 12, 15), src(1, 0), 0xffff], false).is_err()
        );
    }
    #[test]
    fn immediate_end_pattern_and_comment_are_not_terminators() {
        let p = Program::parse(
            &[
                0xffff0101,
                0x0002fffe,
                0xffff,
                0xffffffff,
                81,
                dst(2, 0, 15),
                0xffffffff,
                0xffff,
                0,
                0,
                1,
                dst(0, 0, 15),
                src(2, 0),
                0xffff,
            ],
            true,
        )
        .unwrap();
        validate(&compose(None, Some(&p), false));
    }
}

#[cfg(test)]
mod gpu_tests {
    use crate::{
        abi::*,
        backend::GpuContext,
        d3d8::{
            device::{Device, TextureLevelUpload},
            resource::VertexBuffer,
        },
    };
    // Synthetic programs/vertices: no game fixtures or shader asset text.
    #[test]
    fn shader_bindings_constants_snapshots_and_texbem_readback() {
        let gpu = pollster::block_on(GpuContext::new_headless()).expect("Metal adapter");
        let mut device = Device::new(gpu, 64, 64, 21, 0).unwrap();
        device.state.set_render_state(7, 0).unwrap(); // ZENABLE
        device.state.set_render_state(22, 1).unwrap(); // CULL_NONE
        let raw = (&mut device as *mut Device).cast::<D3d8Device>();
        let mut err = D3d8Error::empty();
        let decl = [
            0x20000000,
            0x40030000,
            0x82000001,
            0.125f32.to_bits(),
            0.5f32.to_bits(),
            0,
            1.0f32.to_bits(),
            u32::MAX,
        ];
        let vs = [
            0xfffe0101, 1, 0xc00f0000, 0x90e40000, 1, 0xe00f0003, 0xa0e40001, 0xffff,
        ];
        let ps = [0xffff0101, 1, 0x800f0000, 0xa0e40000, 0xffff];
        assert_eq!(
            d3d8_device_create_shader(
                raw,
                0x10000,
                0,
                decl.as_ptr(),
                decl.len() as u32,
                vs.as_ptr(),
                vs.len() as u32,
                &mut err
            ),
            0
        );
        assert_eq!(
            d3d8_device_create_shader(
                raw,
                0x10001,
                1,
                std::ptr::null(),
                0,
                ps.as_ptr(),
                ps.len() as u32,
                &mut err
            ),
            0
        );
        assert_eq!(d3d8_device_shader_action(raw, 0x10000, 0, 0, &mut err), 0);
        assert_eq!(d3d8_device_shader_action(raw, 0x10001, 1, 0, &mut err), 0);
        let data: [f32; 12] = [
            -1.0, -1.0, 0.5, 1.0, 3.0, -1.0, 0.5, 1.0, -1.0, 3.0, 0.5, 1.0,
        ];
        let vertices = VertexBuffer::borrowed(bytemuck::cast_slice(&data), 16).unwrap();
        device.clear(0, 1, 0xff000000, 1.0, 0).unwrap();
        device.begin_scene().unwrap();
        for (x, color) in [(0, [1.0f32, 0.0, 0.0, 1.0]), (32, [0.0, 1.0, 0.0, 1.0])] {
            let bits = color.map(f32::to_bits);
            assert_eq!(
                d3d8_device_shader_constants(raw, 1, 0, bits.as_ptr(), 1, &mut err),
                0
            );
            device.state.viewport.x = x;
            device.state.viewport.width = 32;
            device.draw_primitive(4, 0x10000, &vertices, 0, 1).unwrap();
        }
        device.end_scene().unwrap();
        let pixels = device.read_pixels().unwrap();
        assert_eq!(
            &pixels[(32 * 64 + 16) * 4..(32 * 64 + 16) * 4 + 4],
            &[255, 0, 0, 255]
        );
        assert_eq!(
            &pixels[(32 * 64 + 48) * 4..(32 * 64 + 48) * 4 + 4],
            &[0, 255, 0, 255]
        );
        // The same declaration permits a larger stream stride. A cached
        // pipeline must include that stride rather than treating padding as
        // the next vertex's attributes.
        let mut padded = Vec::new();
        for position in data.chunks_exact(4) {
            padded.extend_from_slice(bytemuck::cast_slice(position));
            padded.extend_from_slice(&[0u8; 16]);
        }
        let padded = VertexBuffer::borrowed(&padded, 32).unwrap();
        device.state.viewport.x = 0;
        device.state.viewport.width = 64;
        device.clear(0, 1, 0xff000000, 1.0, 0).unwrap();
        device.begin_scene().unwrap();
        device.draw_primitive(4, 0x10000, &padded, 0, 1).unwrap();
        device.end_scene().unwrap();
        let pixels = device.read_pixels().unwrap();
        assert_eq!(
            &pixels[(32 * 64 + 32) * 4..(32 * 64 + 32) * 4 + 4],
            &[0, 255, 0, 255]
        );
        // TEXBEM uses destination stage 3's matrix and samples stage 3;
        // source stage 1 supplies the dependent displacement (red = 1).
        let bump = [
            0xffff0101, 66, 0xb00f0001, 67, 0xb00f0003, 0xb0e40001, 1, 0x800f0000, 0xb0e40003,
            0xffff,
        ];
        assert_eq!(
            d3d8_device_create_shader(
                raw,
                0x10002,
                1,
                std::ptr::null(),
                0,
                bump.as_ptr(),
                bump.len() as u32,
                &mut err
            ),
            0
        );
        assert_eq!(d3d8_device_shader_action(raw, 0x10002, 1, 0, &mut err), 0);
        let coords = [0.0f32, 0.5, 0.0, 1.0].map(f32::to_bits);
        assert_eq!(
            d3d8_device_shader_constants(raw, 0, 1, coords.as_ptr(), 1, &mut err),
            0
        );
        for (stage, width, bytes) in [
            (1, 1, &[0u8, 255, 0, 255][..]),
            (3, 2, &[0u8, 0, 255, 255, 255, 0, 0, 255][..]),
        ] {
            device
                .set_texture(
                    stage,
                    stage + 1,
                    21,
                    &[TextureLevelUpload {
                        level: 0,
                        generation: 1,
                        force_upload: false,
                        width,
                        height: 1,
                        data: bytes,
                    }],
                )
                .unwrap();
            device.state.set_texture_stage_state(stage, 13, 3).unwrap(); // CLAMP
        }
        device
            .state
            .set_texture_stage_state(3, 9, 0.45f32.to_bits())
            .unwrap();
        device.state.set_texture_stage_state(1, 7, 0).unwrap();
        assert_eq!(device.shader_uniform.vc[1][0], 0.0); // declaration CONST did not overwrite the bank
        device.state.viewport.x = 0;
        device.state.viewport.width = 64;
        device.begin_scene().unwrap();
        device.draw_primitive(4, 0x10000, &vertices, 0, 1).unwrap();
        device.end_scene().unwrap();
        let pixels = device.read_pixels().unwrap();
        assert_eq!(
            &pixels[(32 * 64 + 32) * 4..(32 * 64 + 32) * 4 + 4],
            &[0, 0, 255, 255]
        );
        // Changing only the VS preserves the PS; TEXCOORDINDEX selects
        // the second FVF UV set for t3 instead of silently aliasing UV0.
        assert_eq!(d3d8_device_shader_action(raw, 0, 0, 0, &mut err), 0);
        device.state.set_render_state(137, 0).unwrap();
        device.state.set_texture_stage_state(3, 11, 1).unwrap();
        let mut fixed_bytes = Vec::new();
        for p in [[-1.0f32, -1.0, 0.5], [3.0, -1.0, 0.5], [-1.0, 3.0, 0.5]] {
            fixed_bytes.extend_from_slice(bytemuck::cast_slice(&p));
            fixed_bytes.extend_from_slice(&0xffffffffu32.to_le_bytes());
            fixed_bytes.extend_from_slice(bytemuck::cast_slice(&[0.0f32, 0.5, 0.125, 0.5]));
        }
        let fixed = VertexBuffer::borrowed(&fixed_bytes, 32).unwrap();
        device.begin_scene().unwrap();
        device.draw_primitive(4, 0x242, &fixed, 0, 1).unwrap();
        device.end_scene().unwrap();
        let pixels = device.read_pixels().unwrap();
        assert_eq!(
            &pixels[(32 * 64 + 32) * 4..(32 * 64 + 32) * 4 + 4],
            &[0, 0, 255, 255]
        );
        device.state.set_texture_stage_state(3, 24, 2).unwrap();
        device.begin_scene().unwrap();
        assert!(
            device
                .draw_primitive(4, 0x242, &fixed, 0, 1)
                .unwrap_err()
                .cause
                .contains("texture transforms")
        );
        device.end_scene().unwrap();
        assert_eq!(d3d8_device_shader_action(raw, 0x10002, 1, 1, &mut err), 0);
        assert_eq!(device.pixel_shader, 0);
        assert_eq!(d3d8_device_shader_action(raw, 0x10002, 1, 0, &mut err), 1);
    }
}
