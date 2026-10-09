/* LH3DIsland_RenderCell (0x00874aa0), rendering only.
 *
 * Transforms a cell's 17x17 vertex grid into screen space, builds the index
 * list and issues the draw calls. The per-vertex math is ordinary C: no x87
 * stack, tag or flag state is kept. Draw and render-state calls keep the
 * original's order and go through the translated functions and the D3D shim
 * with their original calling conventions. RECOMP_TERRAIN_ORIGINAL=1 selects
 * the retained translated original (fn_00874aa0) for same-binary A/B.
 *
 * Cell (this) fields used: 0x908 table index, 0x90c/0x910 position, 0x91c
 * clip-in-vertex mode, 0x928 specular-fade pass, 0x930 step shift, 0x934,
 * 0x938 texture type (0 base, 1 detail, 2 faded), 0x940 shading mode, 0x948
 * base texture, 0x94c state flags, 0x9c0 detail texture holder, 0x9c4/0x9c8
 * detail UV offset. 8-byte source records: colour dword, height byte at +4,
 * specular index in the colour's top byte. */
#include "terrain_cells.h"
#include <math.h>

/* Retained translated original; bypasses the FN_00874aa0 override. */
void fn_00874aa0(X86 *c);

enum {
  VERTS = 0x00e437e0u,     /* 32-byte vertices */
  CLIPS = 0x00e3b5e0u,     /* u32 outside-plane bits per vertex */
  INDICES = 0x00ea5de0u,   /* u16 index list */
  INDICES2 = 0x00ea1de0u,  /* u16 filtered index list */
  INDEX_COUNT = 0x00ea9ea4u,
  VERT_COUNT = 0x00ea9ea8u,  /* u16 */
  SSE_PATH = 0x00ea9eb4u,
  MATRIX = 0x00ea9e40u,      /* 3x4 view-projection, floats */
  SPEC_TABLE = 0x00edd90cu,  /* u32 specular by colour alpha */
  DEVICE = 0x00eca638u,      /* IDirect3DDevice7* */
  RS_TEXTURE_BIND = 0x00eca64cu,
  RS_CACHE_CULL = 0x00eca248u,
  RS_CACHE_STATE17 = 0x00eca24cu,
  TILE_MODE = 0x00eca614u,
  TEX_FN_TABLE = 0x00eca618u,
  TEX_ENABLED = 0x00c38714u,
  TEX_SET = 0x00e9c564u,
  ALPHA_TEX = 0x00e9cd64u,
};

/* Real return sites inside the original, one per kind of call, so the runtime
 * treats each pushed address as a CALL continuation. */
enum {
  RET_APPLY_XFORM = 0x0087536du,
  RET_SET_STATE = 0x00875385u,
  RET_TEX_FN = 0x008753d0u,
  RET_TILE_OFF = 0x008753e6u,
  RET_TILE_ON = 0x008753efu,
  RET_DRAW = 0x00875574u,
  RET_BUILD_ALT = 0x00875349u,
  RET_FEB0 = 0x00875b7au,
  RET_FED0 = 0x00875babu,
  RET_XFORM2 = 0x00875c1eu,
};

/* Guest call with the original convention: arguments pushed right to left,
 * a return address, ECX/EDX loaded. The callee pops its own arguments unless
 * `caller_pops`. */
static uint32_t gcall(X86 *c, uint32_t target, uint32_t ret, uint32_t ecx,
                      uint32_t edx, int n, uint32_t a0, uint32_t a1,
                      uint32_t a2, int caller_pops) {
  const uint32_t args[3] = {a0, a1, a2};
  c->r[R_ECX] = ecx;
  c->r[R_EDX] = edx;
  for (int i = n - 1; i >= 0; --i) {
    c->r[R_ESP] -= 4u;
    wr32(c->r[R_ESP], args[i]);
  }
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], ret);
  recomp_call(c, target);
  if (caller_pops)
    c->r[R_ESP] += 4u * (uint32_t)n;
  return c->r[R_EAX];
}

static uint32_t set_render_state(X86 *c, uint32_t state, uint32_t value) {
  const uint32_t dev = rd32(DEVICE);
  const uint32_t fn = rd32(rd32(dev) + 0x50u);
  return gcall(c, fn, RET_SET_STATE, 0, 0, 3, dev, state, value, 0);
}

/* State 0x17 with value v, cached like the original: the cache keeps v on
 * success and -1 on failure. */
static void set_state17(X86 *c, uint32_t v) {
  if (rd32(RS_CACHE_STATE17) == v)
    return;
  const uint32_t r = set_render_state(c, 0x17, v);
  wr32(RS_CACHE_STATE17, r != 0 ? 0xffffffffu : v);
}

static void tile_off(X86 *c) {
  gcall(c, 0x0082ff50u, RET_TILE_OFF, 0, 0, 1, 0, 0, 0, 1);
}
static void tile_on(X86 *c) {
  gcall(c, 0x0082ff10u, RET_TILE_ON, 0, 0, 1, 0, 0, 0, 1);
}

static void draw_triangles(X86 *c, uint32_t indices, uint32_t count) {
  gcall(c, 0x0082f810u, RET_DRAW, VERTS, rd16(VERT_COUNT), 2, indices, count,
        0, 0);
}

/* Select and set up a texture object (tile mode and filter state), as the
 * original does before each pass. */
static void bind_texture(X86 *c, uint32_t obj) {
  if (rd32(TEX_ENABLED) == 0)
    return;
  wr32(RS_TEXTURE_BIND, obj);
  if (obj == 0)
    return;
  gcall(c, rd32(rd32(TEX_FN_TABLE) + rd32(obj) * 8u), RET_TEX_FN, obj, 0, 0, 0,
        0, 0, 0);
  if (rd32(TILE_MODE) == 0 && (rd8(obj + 5u) & 4u) == 0)
    tile_off(c);
  else
    tile_on(c);
  const uint32_t v = (((uint32_t)(uint8_t)~rd8(obj + 5u) & 1u) << 1) | 1u;
  if (rd32(RS_CACHE_CULL) != v) {
    const uint32_t r = set_render_state(c, 0x16, v);
    wr32(RS_CACHE_CULL, 0xffffffffu);
    if (r == 0)
      wr32(RS_CACHE_CULL, v);
  }
}

/* FISTP with the default round-to-nearest-even; out-of-range gives the x87
 * "integer indefinite". */
static int32_t fist(float v) {
  if (!(v >= -2147483648.0f && v < 2147483648.0f))
    return INT32_MIN;
  return (int32_t)nearbyintf(v);
}
/* __ftol: truncate toward zero. */
static int32_t ftol(double v) {
  if (!(v > -2147483649.0 && v < 2147483648.0))
    return INT32_MIN;
  return (int32_t)v;
}

typedef struct {
  float matrix[12];     /* DAT_00ea9e40.. */
  float height_scale;   /* DAT_00c3720c */
  float persp_x, persp_y, persp_z; /* 00e839f0, 00e839f4, 00e839e0 */
  float clamp_x, clamp_y;          /* 00c2ab00, 00c2ab04 */
  float depth_min, depth_max;      /* 00c37220, 00c37224 */
  float depth_range;               /* 00e9b6dc */
  float shade_r, shade_g, shade_b; /* 00c37214 / 18 / 1c */
  uint32_t shade_fade;             /* 00c37228 */
  uint32_t flat_colour;            /* 00e9b6d8 */
} FillK;

static inline float fz(uint32_t a) { return rdf32(a); }

/* Perspective divide and clamp of one transformed vertex. */
static inline void perspective(uint32_t pv, const FillK *k) {
  const double rw = 1.0 / (double)rdf32(pv + 12u);
  wrf32(pv + 12u, (float)rw);
  float x = (float)((rw * (double)rdf32(pv) + 1.0) * (double)k->persp_x);
  if (0.0f <= x) {
    if (k->clamp_x < x)
      x = k->clamp_x;
  } else {
    x = 0.0f;
  }
  wrf32(pv, x);
  float y = (float)((double)k->persp_y -
                    (double)rdf32(pv + 12u) * (double)rdf32(pv + 4u) *
                        (double)k->persp_y);
  if (0.0f <= y) {
    if (k->clamp_y < y)
      y = k->clamp_y;
  } else {
    y = 0.0f;
  }
  wrf32(pv + 4u, y);
  const float t = (float)((double)k->persp_z * (double)rdf32(pv + 12u));
  wrf32(pv + 12u, t);
  wrf32(pv + 8u, 1.0f - t);
}

/* Clip-bit classification, then the divide for vertices fully inside. */
static inline void classify_and_project(uint32_t pv, uint32_t pc,
                                        const FillK *k) {
  uint32_t f = (k->persp_z <= rdf32(pv + 12u)) ? 0u : 0x20u;
  const float x = rdf32(pv), y = rdf32(pv + 4u), w = rdf32(pv + 12u);
  if (x <= w) {
    if (x < -w)
      f |= 8u;
  } else {
    f |= 0x10u;
  }
  if (y <= w) {
    if (y < -w)
      f |= 2u;
  } else {
    f |= 4u;
  }
  wr32(pc, f);
  if (f == 0)
    perspective(pv, k);
}

// @port 0x00874aa0 80% correctness
// Ghidra 0x00874aa0 LH3DIsland_RenderCell. Whole-function native replacement;
// translated callees (ClipTriangle, texture setup, DrawTriangle, the D3D shim)
// are called in the original order.
// DIVERGENCE(original): intermediate float math is evaluated in double and
// rounded at the same stores the original makes, but the order of a few
// additions and the x87 stack/tag/status state, EFLAGS and dead registers are
// not reproduced. Output differences are at most float rounding in vertex
// positions, texture coordinates and fade alphas.
// TODO(decomp): the SSE twin (DAT_00ea9eb4 != 0) is not reimplemented; it
// runs the translated original.
void bw_render_cell(X86 *c) {
  if (bw_terrain_use_original() || rd32(SSE_PATH) != 0) {
    fn_00874aa0(c);
    return;
  }
  const uint32_t esp0 = c->r[R_ESP];
  const uint32_t cell = c->r[R_ECX];

  FillK k;
  for (int i = 0; i < 12; ++i)
    k.matrix[i] = fz(MATRIX + 4u * (uint32_t)i);
  k.height_scale = fz(0xc3720cu);
  k.persp_x = fz(0xe839f0u);
  k.persp_y = fz(0xe839f4u);
  k.persp_z = fz(0xe839e0u);
  k.clamp_x = fz(0xc2ab00u);
  k.clamp_y = fz(0xc2ab04u);
  k.depth_min = fz(0xc37220u);
  k.depth_max = fz(0xc37224u);
  k.depth_range = fz(0xe9b6dcu);
  k.shade_r = fz(0xc37214u);
  k.shade_g = fz(0xc37218u);
  k.shade_b = fz(0xc3721cu);
  k.shade_fade = rd32(0xc37228u);
  k.flat_colour = rd32(0xe9b6d8u);
  /* matrix indices: 0..2 = ea9e40/44/48, 3..5 = 4c/50/54, 6..8 = 58/5c/60,
   * 9..11 = 64/68/6c. */
  const float *m = k.matrix;

  const uint32_t step = 1u << (rd32(cell + 0x930u) & 31u);
  const uint32_t row_skip = step * 16u - 16u;
  const uint32_t type = rd32(cell + 0x938u);   /* 0x938 */
  const uint32_t shade = rd32(cell + 0x940u);  /* 0x940 */
  const uint32_t no_alpha = rd32(cell + 0x928u);
  const uint32_t clip_mode = rd32(cell + 0x91cu);
  const float eps = 0.003921569f;

  const float row_step = (float)step * 10.0f;
  float v_step = (float)step * 0.0625f;
  float u0 = 0.0f, v0 = 0.0f;
  if (type == 1) {
    v_step *= 0.2421875f;
    v0 = rdf32(cell + 0x9c8u) + eps;
    u0 = rdf32(cell + 0x9c4u) + eps;
  }
  float px = rdf32(cell + 0x90cu);
  uint32_t src = cell;
  uint32_t pv = VERTS, pc = CLIPS;

  for (uint32_t row = 0; row < 17u; row += step) {
    float py = rdf32(cell + 0x910u);
    float u = u0;
    for (uint32_t col = 0; col < 17u; col += step) {
      const uint32_t colour = rd32(src);
      const uint32_t height_byte = rd8(src + 4u);
      uint32_t diffuse = colour | 0xff000000u;
      float h = (float)height_byte * k.height_scale;
      if (height_byte < 4u) {
        h = 0.0f;
        if (no_alpha != 0 && height_byte < 2u)
          diffuse &= 0xffffffu;
      }
      wr32(pv + 20u, diffuse);
      wr32(pv + 16u, rd32(SPEC_TABLE + 4u * rd8(src + 3u)));

      float x, y, w;
      if (shade == 0) {
        x = (float)((double)m[6] * py + (double)m[3] * h +
                    (double)m[0] * px + (double)m[9]);
        y = (float)((double)m[7] * py + (double)m[4] * h +
                    (double)m[1] * px + (double)m[10]);
        w = (float)((double)m[8] * py + (double)m[5] * h +
                    (double)m[2] * px + (double)m[11]);
      } else {
        x = (float)((double)m[0] * px + (double)m[6] * py +
                    (double)m[3] * h + (double)m[9]);
        y = (float)((double)m[1] * px + (double)m[7] * py +
                    (double)m[4] * h + (double)m[10]);
        w = (float)((double)m[2] * px + (double)m[8] * py +
                    (double)m[5] * h + (double)m[11]);
      }
      wrf32(pv, x);
      wrf32(pv + 4u, y);
      wrf32(pv + 12u, w);

      if (shade != 0) {
        /* Depth shade colour and specular fade. */
        uint32_t packed, fade;
        if (shade == 2u) {
          packed = k.flat_colour;
          fade = k.shade_fade;
        } else {
          float d = w;
          if (d < k.depth_min)
            d = k.depth_min;
          else if (!(d <= k.depth_max))
            d = k.depth_max;
          const float t = (float)(((double)d - (double)k.depth_min) /
                                  (double)k.depth_range);
          fade = 0x100u - (uint32_t)ftol((double)(int32_t)(0x100u - k.shade_fade) *
                                         (double)t);
          const uint32_t r = (uint32_t)fist((float)((double)k.shade_r * t));
          const uint32_t g = (uint32_t)fist((float)((double)k.shade_g * t));
          const uint32_t b = (uint32_t)fist((float)((double)k.shade_b * t));
          packed = (((r << 8) | g) << 8) | b;
        }
        const uint32_t cur = rd32(pv + 20u);
        if (cur != 0) {
          /* Saturating byte-wise add of the shade colour onto the diffuse. */
          uint32_t b0 = (cur & 0xffu) + (packed & 0xffu);
          uint32_t b1 = ((cur >> 8) & 0xffu) + ((packed >> 8) & 0xffu);
          uint32_t b2 = ((cur >> 16) & 0xffu) + ((packed >> 16) & 0xffu);
          if (b0 > 0xffu) b0 = 0xffu;
          if (b1 > 0xffu) b1 = 0xffu;
          if (b2 > 0xffu) b2 = 0xffu;
          wr32(pv + 20u, (cur & 0xff000000u) | (b2 << 16) | (b1 << 8) | b0);
        } else {
          wr32(pv + 20u, packed);
        }
        if (fade < 0x100u) {
          const uint32_t s = rd32(pv + 16u);
          const uint32_t out =
              ((((s & 0xff0000u) * fade) & 0xff0000ffu) |
               (((s & 0xff00u) * fade) & 0xff0000u) |
               (((s & 0xffu) * fade) & 0xff00u)) >>
                  8 |
              (s & 0xff000000u);
          wr32(pv + 16u, out);
        }
      }

      if (clip_mode == 0)
        perspective(pv, &k);
      else
        classify_and_project(pv, pc, &k);

      wrf32(pv + 24u, u);
      wrf32(pv + 28u, v0);
      py += row_step;
      u += v_step;
      src += 8u * step;
      pc += 4u;
      pv += 32u;
    }
    src += 8u * row_skip;
    px += row_step;
    v0 += v_step;
  }

  /* Index list. */
  wr32(INDEX_COUNT, 0);
  const uint32_t side = 16u / step + 1u;
  wr16(VERT_COUNT, (uint16_t)(side * side));
  const uint32_t mode = rd32(cell + 0x930u);
  if (clip_mode != 0) {
    if (bw_cell_indices_native_ok(mode))
      bw_build_cell_indices_impl(c, cell, mode);
    else
      gcall(c, 0x00875c60u, 0x00875342u, cell, mode, 0, 0, 0, 0, 0);
  } else {
    gcall(c, 0x00876910u, RET_BUILD_ALT, cell, mode, 0, 0, 0, 0, 0);
  }

  uint32_t result = 0;
  if (rd32(INDEX_COUNT) == 0)
    goto done;

  const uint32_t flags = rd32(cell + 0x94cu);
  if (flags & 1u) {
    gcall(c, 0x00878f70u, RET_APPLY_XFORM, cell, 0, 0, 0, 0, 0, 0);
    set_state17(c, 3);
  }
  result = 1;

  if (type == 1) {
    bind_texture(c, rd32(rd32(cell + 0x9c0u) + 4u));
  } else {
    bind_texture(c, rd32(cell + 0x948u));
    if (rd32(rd32(TEX_SET + 4u * rd32(cell + 0x908u)) + 0x92cu) != 0) {
      bind_texture(c, rd32(rd32(cell + 0x9c0u) + 4u));
      wrf32(cell + 0x9c4u, rdf32(cell + 0x9c4u) + eps);
      wrf32(cell + 0x9c8u, rdf32(cell + 0x9c8u) + eps);
      uint32_t p = VERTS;
      for (uint32_t i = rd16(VERT_COUNT); i > 0; --i, p += 32u) {
        wrf32(p + 24u, (float)((double)rdf32(p + 24u) * 0.2421875 +
                               (double)rdf32(cell + 0x9c4u)));
        wrf32(p + 28u, (float)((double)rdf32(p + 28u) * 0.2421875 +
                               (double)rdf32(cell + 0x9c8u)));
      }
      wrf32(cell + 0x9c4u, rdf32(cell + 0x9c4u) - eps);
      wrf32(cell + 0x9c8u, rdf32(cell + 0x9c8u) - eps);
      draw_triangles(c, INDICES, rd32(INDEX_COUNT));
      c->r[R_EAX] = 1;
      goto ret;
    }
  }
  draw_triangles(c, INDICES, rd32(INDEX_COUNT));

  if (type == 2u) {
    bind_texture(c, rd32(rd32(cell + 0x9c0u) + 4u));
    const float scale = 255.0f / fz(0xe9c528u);
    const float e514 = fz(0xe9c514u), e510 = fz(0xe9c510u), e51c = fz(0xe9c51cu),
                e508 = fz(0xe9c508u), e52c = fz(0xe9c52cu);
    wrf32(cell + 0x9c4u, rdf32(cell + 0x9c4u) + eps);
    wrf32(cell + 0x9c8u, rdf32(cell + 0x9c8u) + eps);
    uint32_t p = VERTS;
    for (uint32_t i = rd16(VERT_COUNT); i > 0; --i, p += 32u) {
      const float d = (float)(((double)rdf32(p + 28u) * 160.0 +
                               (double)rdf32(cell + 0x90cu) - e514) *
                                  e510 -
                              ((double)rdf32(p + 24u) * 160.0 +
                               (double)rdf32(cell + 0x910u) - e51c) *
                                  e508);
      wrf32(p + 24u, (float)((double)rdf32(p + 24u) * 0.2421875 +
                             (double)rdf32(cell + 0x9c4u)));
      wrf32(p + 28u, (float)((double)rdf32(p + 28u) * 0.2421875 +
                             (double)rdf32(cell + 0x9c8u)));
      if (d <= e52c) {
        if (-e52c < d) {
          const int32_t a =
              fist((float)(((double)e52c - d) * scale));
          wr32(p + 16u, (rd32(p + 16u) & 0xffffffu) | ((uint32_t)a << 24));
        }
      } else {
        wr32(p + 16u, rd32(p + 16u) & 0xffffffu);
      }
    }
    wrf32(cell + 0x9c4u, rdf32(cell + 0x9c4u) - eps);
    wrf32(cell + 0x9c8u, rdf32(cell + 0x9c8u) - eps);
    draw_triangles(c, INDICES, rd32(INDEX_COUNT));
  }

  if (no_alpha != 0) {
    bind_texture(c, rd32(ALPHA_TEX));
    const float scale = 255.0f / fz(0xe9c550u);
    const float e53c = fz(0xe9c53cu), e538 = fz(0xe9c538u), e544 = fz(0xe9c544u),
                e530 = fz(0xe9c530u), e554 = fz(0xe9c554u);
    const float cx = rdf32(cell + 0x90cu), cy = rdf32(cell + 0x910u);
    const uint32_t nv = rd16(VERT_COUNT);
    uint32_t p = VERTS;
    for (uint32_t i = 0; i < nv; ++i, p += 32u) {
      if (type == 2u || type == 1u) {
        wrf32(p + 24u, (rdf32(p + 24u) - rdf32(cell + 0x9c4u)) * 4.0f);
        wrf32(p + 28u, (rdf32(p + 28u) - rdf32(cell + 0x9c8u)) * 4.0f);
      }
      const float d = (float)(((double)rdf32(p + 28u) * 160.0 + cx - e53c) *
                                  e538 -
                              ((double)rdf32(p + 24u) * 160.0 + cy - e544) *
                                  e530);
      if (d < e554) {
        if (-e554 < d) {
          const int32_t a = fist((float)(255.0 - ((double)e554 - d) * scale));
          if (rd32(p + 20u) & 0xff000000u)
            wr32(p + 16u, ((uint32_t)a << 24) | 0xffffffu);
          else
            wr32(p + 16u, 0);
        } else {
          wr32(p + 16u, 0);
        }
      } else {
        wr32(p + 16u, rd32(p + 20u) | 0xffffffu);
      }
      wrf32(p + 24u, rdf32(p + 24u) * 12.0f);
      wrf32(p + 28u, rdf32(p + 28u) * 12.0f);
    }
    /* Keep only triangles with a visible specular on some vertex. */
    const int32_t tris = (int32_t)rd32(INDEX_COUNT) / 3;
    uint32_t kept = 0, in = INDICES, out = INDICES2;
    for (int32_t t = 0; t < tris; ++t, in += 6u) {
      const uint16_t i0 = rd16(in), i1 = rd16(in + 2u), i2 = rd16(in + 4u);
      if ((rd32(VERTS + 32u * i0 + 16u) & 0xff000000u) ||
          (rd32(VERTS + 32u * i1 + 16u) & 0xff000000u) ||
          rd8(VERTS + 32u * i2 + 19u) != 0) {
        wr16(out, i0);
        wr16(out + 2u, i1);
        wr16(out + 4u, i2);
        out += 6u;
        kept += 3u;
      }
    }
    if (kept != 0) {
      gcall(c, 0x0082feb0u, RET_FEB0, 0, 0, 1, 0, 0, 0, 1);
      tile_on(c);
      draw_triangles(c, INDICES2, kept);
      tile_off(c);
      gcall(c, 0x0082fed0u, RET_FED0, 0, 0, 1, 0, 0, 0, 1);
    }
  }

  /* The original re-reads the state flags here; the texture-transform call
   * above can change them. */
  const uint32_t tail_flags = rd32(cell + 0x94cu);
  if (tail_flags & 1u)
    set_state17(c, 4);
  if (tail_flags & 2u) {
    const uint32_t low = tail_flags & 1u;
    if (low)
      set_state17(c, 3);
    gcall(c, 0x008790f0u, RET_XFORM2, cell, 0, 0, 0, 0, 0, 0);
    if (low)
      set_state17(c, 4);
  }

done:
  c->r[R_EAX] = result;
ret:
  c->eip = rd32(esp0);
  c->r[R_ESP] = esp0 + 4u;
  recomp_return(c);
}
