use crate::RenderError;

pub const NONE: u32 = 0x0000_0001;
pub const POINT: u32 = 0x0000_0002;
pub const LINEAR: u32 = 0x0000_0003;
pub const TRIANGLE: u32 = 0x0000_0004;
pub const BOX: u32 = 0x0000_0005;
pub const MIRROR_U: u32 = 0x0001_0000;
pub const MIRROR_V: u32 = 0x0002_0000;
pub const MIRROR_W: u32 = 0x0004_0000;
pub const MIRROR: u32 = MIRROR_U | MIRROR_V | MIRROR_W;
pub const DITHER: u32 = 0x0008_0000;
pub const DITHER_DIFFUSION: u32 = 0x0010_0000;
pub const SRGB_IN: u32 = 0x0020_0000;
pub const SRGB_OUT: u32 = 0x0040_0000;
pub const SRGB: u32 = SRGB_IN | SRGB_OUT;
pub const DEFAULT: u32 = 0xFFFF_FFFF;
pub const CREATION_DEFAULT: u32 = TRIANGLE | DITHER;
pub const MIP_DEFAULT: u32 = BOX;

pub const KIND_MASK: u32 = 0x0000_0007;
pub const SKIP_MASK: u32 = 0x1F << 26;

const KNOWN: u32 = KIND_MASK | MIRROR | DITHER | DITHER_DIFFUSION | SRGB;

pub fn validate(flags: u32, default: u32, allow_skip: bool) -> Result<u32, RenderError> {
    let flags = if flags == DEFAULT { default } else { flags };
    if flags & DITHER_DIFFUSION != 0 {
        return Err(RenderError::new(
            "D3DX",
            "D3DX_FILTER_DITHER_DIFFUSION is unsupported",
        ));
    }
    let skip = flags & SKIP_MASK;
    if skip != 0 && !allow_skip {
        return Err(RenderError::invalid(
            "D3DX",
            "D3DX_SKIP_DDS_MIP_LEVELS is only valid while loading DDS mip levels",
        ));
    }
    let known = if allow_skip { KNOWN | SKIP_MASK } else { KNOWN };
    let unknown = flags & !known;
    if unknown != 0 {
        return Err(RenderError::invalid(
            "D3DX",
            format!("unrecognized D3DX_FILTER bits 0x{unknown:08x}"),
        ));
    }
    let kind = flags & KIND_MASK;
    if !(NONE..=BOX).contains(&kind) {
        return Err(RenderError::invalid(
            "D3DX",
            format!("unrecognized D3DX_FILTER enum {kind}"),
        ));
    }
    Ok(flags & !SKIP_MASK)
}

pub fn resize(
    src: &[[f32; 4]],
    sw: u32,
    sh: u32,
    sd: u32,
    dw: u32,
    dh: u32,
    dd: u32,
    flags: u32,
) -> Result<Vec<[f32; 4]>, RenderError> {
    if sw == 0 || sh == 0 || sd == 0 || dw == 0 || dh == 0 || dd == 0 {
        return Err(RenderError::invalid(
            "D3DX",
            "filter dimensions must be non-zero",
        ));
    }
    let src_len = pixel_count(sw, sh, sd)?;
    if src.len() != src_len {
        return Err(RenderError::invalid(
            "D3DX",
            format!(
                "filter source holds {} pixels but {sw}x{sh}x{sd} needs {src_len}",
                src.len()
            ),
        ));
    }
    let dst_len = pixel_count(dw, dh, dd)?;

    let kind = flags & KIND_MASK;
    let mirror_u = flags & MIRROR_U != 0;
    let mirror_v = flags & MIRROR_V != 0;
    let mirror_w = flags & MIRROR_W != 0;
    let srgb_in = flags & SRGB_IN != 0;
    let srgb_out = flags & SRGB_OUT != 0;

    let mut working: Vec<[f32; 4]> = Vec::new();
    working.try_reserve_exact(src.len()).map_err(|_| {
        RenderError::out_of_memory("D3DX", "filter working image allocation failed")
    })?;
    working.extend_from_slice(src);
    if srgb_in {
        for px in working.iter_mut() {
            px[0] = srgb_to_linear(px[0]);
            px[1] = srgb_to_linear(px[1]);
            px[2] = srgb_to_linear(px[2]);
        }
    }

    let plan_x = axis_plan(sw, dw, kind, mirror_u, "U")?;
    let plan_y = axis_plan(sh, dh, kind, mirror_v, "V")?;
    let plan_z = axis_plan(sd, dd, kind, mirror_w, "W")?;

    let mut out = alloc_pixels(dst_len)?;
    let su = sw as usize;
    let sv = sh as usize;
    let du = dw as usize;
    let dv = dh as usize;
    for z in 0..dd as usize {
        let z_plan = &plan_z[z];
        for y in 0..dv {
            let y_plan = &plan_y[y];
            for x in 0..du {
                let x_plan = &plan_x[x];
                let mut acc = [0.0f32; 4];
                for tz in z_plan {
                    let z_offset = tz.index * sv * su;
                    for ty in y_plan {
                        let y_offset = z_offset + ty.index * su;
                        for tx in x_plan {
                            let px = working[y_offset + tx.index];
                            let weight = tz.weight * ty.weight * tx.weight;
                            for c in 0..4 {
                                acc[c] += px[c] * weight;
                            }
                        }
                    }
                }
                out[z * dv * du + y * du + x] = acc;
            }
        }
    }

    if srgb_out {
        for px in out.iter_mut() {
            px[0] = linear_to_srgb(px[0]);
            px[1] = linear_to_srgb(px[1]);
            px[2] = linear_to_srgb(px[2]);
        }
    }
    Ok(out)
}

#[derive(Clone, Copy)]
struct Tap {
    index: usize,
    weight: f32,
}

fn axis_plan(
    src: u32,
    dst: u32,
    kind: u32,
    mirror: bool,
    axis: &str,
) -> Result<Vec<Vec<Tap>>, RenderError> {
    let src_i = src as i64;
    let dst_i = dst as i64;
    let ratio = src as f64 / dst as f64;
    let mut plan: Vec<Vec<Tap>> = Vec::new();
    plan.try_reserve_exact(dst as usize)
        .map_err(|_| RenderError::out_of_memory("D3DX", "filter axis plan allocation failed"))?;
    for d in 0..dst_i {
        let mut taps: Vec<Tap> = Vec::new();
        match kind {
            NONE => {
                if d < src_i {
                    taps.push(Tap {
                        index: d as usize,
                        weight: 1.0,
                    });
                }
            }
            POINT => {
                let s = ((2 * d as i128 + 1) * src as i128) / (2 * dst as i128);
                taps.push(Tap {
                    index: wrap(s as i64, src_i, mirror),
                    weight: 1.0,
                });
            }
            LINEAR => {
                let center = (d as f64 + 0.5) * ratio - 0.5;
                let base = center.floor();
                let t = (center - base) as f32;
                let i0 = base as i64;
                if 1.0 - t > 0.0 {
                    taps.push(Tap {
                        index: wrap(i0, src_i, mirror),
                        weight: 1.0 - t,
                    });
                }
                if t > 0.0 {
                    taps.push(Tap {
                        index: wrap(i0 + 1, src_i, mirror),
                        weight: t,
                    });
                }
            }
            TRIANGLE => {
                let support = ratio.max(1.0);
                let center = (d as f64 + 0.5) * ratio - 0.5;
                let lo = (center - support).ceil() as i64;
                let hi = (center + support).floor() as i64;
                let mut sum = 0.0f32;
                for i in lo..=hi {
                    let weight = 1.0 - (i as f64 - center).abs() / support;
                    if weight > 0.0 {
                        let weight = weight as f32;
                        taps.push(Tap {
                            index: wrap(i, src_i, mirror),
                            weight,
                        });
                        sum += weight;
                    }
                }
                normalize(&mut taps, sum);
            }
            BOX => {
                if src == dst {
                    taps.push(Tap {
                        index: d as usize,
                        weight: 1.0,
                    });
                } else if src >= 2 && (dst == src / 2 || dst == src.div_ceil(2)) {
                    let mut sum = 0.0f32;
                    for i in [2 * d, 2 * d + 1] {
                        if i < src_i {
                            taps.push(Tap {
                                index: i as usize,
                                weight: 0.5,
                            });
                            sum += 0.5;
                        }
                    }
                    normalize(&mut taps, sum);
                } else {
                    return Err(RenderError::new(
                        "D3DX",
                        format!(
                            "D3DX_FILTER_BOX unsupported {axis} ratio {src}:{dst}; requires a unit axis or exact 2:1 mip reduction"
                        ),
                    ));
                }
            }
            _ => {
                return Err(RenderError::invalid(
                    "D3DX",
                    format!("unvalidated D3DX_FILTER enum {kind}"),
                ));
            }
        }
        plan.push(taps);
    }
    Ok(plan)
}

fn normalize(taps: &mut [Tap], sum: f32) {
    if sum > 0.0 {
        for tap in taps.iter_mut() {
            tap.weight /= sum;
        }
    }
}

fn wrap(value: i64, size: i64, mirror: bool) -> usize {
    if size <= 1 {
        return 0;
    }
    if mirror {
        let period = 2 * size;
        let mut v = value.rem_euclid(period);
        if v >= size {
            v = period - 1 - v;
        }
        v as usize
    } else {
        value.rem_euclid(size) as usize
    }
}

fn pixel_count(width: u32, height: u32, depth: u32) -> Result<usize, RenderError> {
    (width as usize)
        .checked_mul(height as usize)
        .and_then(|pixels| pixels.checked_mul(depth as usize))
        .ok_or_else(|| RenderError::invalid("D3DX", "filter image layout overflow"))
}

fn alloc_pixels(count: usize) -> Result<Vec<[f32; 4]>, RenderError> {
    let mut out: Vec<[f32; 4]> = Vec::new();
    out.try_reserve_exact(count)
        .map_err(|_| RenderError::out_of_memory("D3DX", "filter output allocation failed"))?;
    out.resize(count, [0.0f32; 4]);
    Ok(out)
}

fn srgb_to_linear(c: f32) -> f32 {
    if c <= 0.04045 {
        c / 12.92
    } else {
        ((c + 0.055) / 1.055).powf(2.4)
    }
}

fn linear_to_srgb(c: f32) -> f32 {
    if c <= 0.003_130_8 {
        c * 12.92
    } else {
        1.055 * c.powf(1.0 / 2.4) - 0.055
    }
}
