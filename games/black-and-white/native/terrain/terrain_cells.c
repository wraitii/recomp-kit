/* Terrain cell index builder (rendering only).
 *
 * Plain C over the guest arena: no x87 stack, tag or flag state is kept, since
 * nothing after this function can observe it (see DIVERGENCE below). The
 * translated original stays available as fn_00875c60; RECOMP_TERRAIN_ORIGINAL=1
 * selects it for same-binary A/B. */
#include "terrain_cells.h"
#include <stdlib.h>

/* Retained translated original; bypasses the FN_00875c60 override. */
void fn_00875c60(X86 *c);

enum {
  CLIP_FLAGS = 0x00e3b5e0u,  /* u32 per vertex: outside-plane bits */
  VERTS = 0x00e437e0u,       /* 32-byte vertices: x at +0, y at +4 */
  INDICES = 0x00ea5de0u,     /* u16 index list */
  INDEX_COUNT = 0x00ea9ea4u, /* running count of indices */
  SSE_PATH = 0x00ea9eb4u,    /* nonzero: original delegates to the SSE twin */
  FLIPPED = 0x00fa92dcu,     /* nonzero: mirrored pass (reflection) */
  LOD_TABLE = 0x00e9a130u,   /* 6 dwords per LOD */
  FACING_EPS = 0x008aa398u,  /* float 0.0, the cross-product threshold */
  CLIP_TRIANGLE = 0x0081a760u,
  /* A real return site inside the original, so the runtime treats the pushed
   * address as a CALL continuation. */
  CLIP_RETURN = 0x00875e75u,
};

static int terrain_use_original(void) {
  static int original = -1;
  int v = __atomic_load_n(&original, __ATOMIC_RELAXED);
  if (v < 0) {
    const char *e = getenv("RECOMP_TERRAIN_ORIGINAL");
    v = e && e[0] == '1' && e[1] == '\0';
    __atomic_store_n(&original, v, __ATOMIC_RELAXED);
  }
  return v;
}

static inline uint32_t clipf(uint32_t i) {
  return rd32(CLIP_FLAGS + 4u * (i & 0xffffu));
}
static inline double vx(uint32_t i) {
  return (double)rdf32(VERTS + 32u * (i & 0xffffu));
}
static inline double vy(uint32_t i) {
  return (double)rdf32(VERTS + 32u * (i & 0xffffu) + 4u);
}

/* Twice the signed area of (p0,p1,p2) as the original evaluates it:
 * (x1-x0)*(y2-y0) - (x2-x0)*(y1-y0), in double like the translated x87. */
static inline int facing(uint32_t i0, uint32_t i1, uint32_t i2, double eps) {
  const double x0 = vx(i0), y0 = vy(i0);
  const double a = (vx(i1) - x0) * (vy(i2) - y0);
  const double b = (vx(i2) - x0) * (vy(i1) - y0);
  return a - b > eps;
}

typedef struct {
  X86 *c;
  uint32_t n; /* index count, mirrored to INDEX_COUNT after every triangle */
  double eps;
} Builder;

static inline void emit(Builder *b, uint32_t i0, uint32_t i1, uint32_t i2) {
  wr16(INDICES + 2u * b->n, (uint16_t)i0);
  wr16(INDICES + 2u * (b->n + 1u), (uint16_t)i1);
  wr16(INDICES + 2u * (b->n + 2u), (uint16_t)i2);
  b->n += 3u;
  wr32(INDEX_COUNT, b->n);
}

/* LH3DTech_ClipTriangle(ecx = v0, edx = v1, stack v2, 0x20), callee pops 8.
 * It appends clipped triangles and moves INDEX_COUNT. */
static void clip_triangle(Builder *b, uint32_t v0, uint32_t v1, uint32_t v2) {
  X86 *c = b->c;
  c->r[R_ECX] = v0;
  c->r[R_EDX] = v1;
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], 0x20u);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], v2);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], CLIP_RETURN);
  recomp_call(c, CLIP_TRIANGLE);
  b->n = rd32(INDEX_COUNT);
}

/* Unclipped triangle -> cull by facing; otherwise clip when the three vertices
 * share no outside plane. `all_in` says every vertex of the quad is inside. */
static inline void tri(Builder *b, int all_in, uint32_t i0, uint32_t i1,
                       uint32_t i2) {
  if (all_in) {
    if (facing(i0, i1, i2, b->eps))
      emit(b, i0, i1, i2);
  } else if ((clipf(i0) & clipf(i1) & clipf(i2)) == 0) {
    clip_triangle(b, i0, i1, i2);
  }
}

/* One grid quad: A B / C D with the diagonal chosen by `diag` (flags & 0x80).
 * The clip tests combine different vertex triples per diagonal, as the
 * original does, so the two cases are kept apart. */
static inline void quad(Builder *b, uint32_t A, uint32_t B, uint32_t C,
                        uint32_t D, int diag) {
  const int all_in =
      (clipf(A) | clipf(B) | clipf(C) | clipf(D)) == 0;
  if (diag) {
    tri(b, all_in, B, D, C);
    tri(b, all_in, B, C, A);
  } else {
    tri(b, all_in, A, B, D);
    tri(b, all_in, A, D, C);
  }
}

/* Fan-stitching triangle lists (u16 triples in the image) for LOD borders:
 * {address, triangle count} by [mode][lod bits], or count 0. */
static uint32_t stitch_list(uint32_t mode, uint32_t lod, uint32_t *count) {
  *count = 0;
  if (mode == 0) {
    if (lod & 1) {
      if (lod & 4) { *count = 0x2e; return 0xc37584; }
      if (lod & 2) { *count = 0x2e; return 0xc37470; }
      *count = 0x18; return 0xc37230;
    }
    if (lod & 8) {
      if (lod & 4) { *count = 0x2e; return 0xc377ac; }
      if (lod & 2) { *count = 0x2e; return 0xc37698; }
      *count = 0x18; return 0xc372c0;
    }
    if (lod & 4) { *count = 0x18; return 0xc373e0; }
    if (lod & 2) { *count = 0x18; return 0xc37350; }
    return 0;
  }
  if (mode == 1) {
    if (lod & 1) {
      if (lod & 4) { *count = 0x16; return 0xc37a64; }
      if (lod & 2) { *count = 0x16; return 0xc379e0; }
      *count = 0xc; return 0xc378c0;
    }
    if (lod & 8) {
      if (lod & 4) { *count = 0x16; return 0xc37b6c; }
      if (lod & 2) { *count = 0x16; return 0xc37ae8; }
      *count = 0xc; return 0xc37908;
    }
    if (lod & 4) { *count = 0xc; return 0xc37998; }
    if (lod & 2) { *count = 0xc; return 0xc37950; }
  }
  return 0;
}

// @port 0x00875c60 85% correctness
// Ghidra 0x00875c60 LH3DIsland_BuildCellIndices. Builds the cell's index list
// from the transformed 17x17 (mode 0) / 9x9 (mode 1) / 5x5 (mode 2) grid:
// back-face culling by signed area, LOD-border stitching lists, and the
// guest ClipTriangle for partly clipped triangles (called, not reimplemented).
// DIVERGENCE(original): x87 stack/tag/status state, EFLAGS and the dead
// registers (EAX, ECX, EDX) are left as the caller had them rather than as
// the original leaves them; arithmetic is the same double math the translated
// original performs. Modes above 2 and the SSE path (DAT_00ea9eb4 != 0) run
// the translated original.
// TODO(decomp): verified against the translated original by A/B only.
int bw_terrain_use_original(void) { return terrain_use_original(); }

int bw_cell_indices_native_ok(uint32_t mode) {
  return !terrain_use_original() && mode <= 2u && rd32(SSE_PATH) == 0;
}

void bw_build_cell_indices_impl(X86 *c, uint32_t cell, uint32_t mode) {
  Builder b = {c, 0, (double)rdf32(FACING_EPS)};
  const uint32_t lod = rd32(cell + 0x93cu);
  const uint32_t t = LOD_TABLE + lod * 24u;
  const uint32_t t_a = rd32(t + 4u), t_r = rd32(t + 8u), t_e = rd32(t + 12u),
                 t_f = rd32(t + 16u);

  /* Grid window (rows a..G-r, columns e..G-f) and the first quad's indices. */
  int32_t row0, rows_end, col0, col_end;
  uint32_t A, B, C, D, step = 1;
  if (mode == 2u) {
    row0 = 0; rows_end = 4; col0 = 0; col_end = 4;
    A = 0; B = 1; C = 5; D = 6;
  } else {
    const uint32_t G = mode == 0u ? 16u : 8u, W = G + 1u;
    row0 = (int32_t)t_a;
    rows_end = (int32_t)(G - t_r);
    col0 = (int32_t)t_e;
    col_end = (int32_t)(G - t_f);
    A = (t_a != 0 ? W : 0u) + (t_e != 0 ? 1u : 0u);
    B = A + 1u;
    C = A + W;
    D = A + W + 1u;
    if (t_e != 0 || t_f != 0)
      step = 2;
  }
  if (rd32(FLIPPED) != 0) {
    const uint32_t tmp = B;
    B = C;
    C = tmp;
  }

  wr32(0x00ea9eb8u, 0);
  b.n = rd32(INDEX_COUNT);
  const int skip_check = rd32(cell + 0x934u) == 0; /* honour flag bit 0x200 */
  uint32_t rec = cell;                             /* 8-byte records, flags at +6 */
  if (row0 < rows_end) {
    for (int32_t rows = rows_end - row0; rows > 0; --rows) {
      if (col0 < col_end) {
        for (int32_t cols = col_end - col0; cols > 0; --cols) {
          const uint32_t flags = rd16(rec + 6u);
          if (!(skip_check && (flags & 0x200u)))
            quad(&b, A, B, C, D, (flags & 0x80u) != 0);
          ++A; ++B; ++C; ++D;
          rec += 8u;
        }
      }
      A += step; B += step; C += step; D += step;
      rec += 8u;
    }
  }

  /* LOD border stitching. */
  if (rd32(t) != 0) {
    uint32_t count;
    const uint32_t list = stitch_list(rd32(cell + 0x930u), lod, &count);
    wr32(0x00ea9eb8u, 0);
    if (list != 0) {
      const int flipped = rd32(FLIPPED) != 0;
      for (uint32_t i = 0; i < count; ++i) {
        const uint32_t t0 = rd16(list + 6u * i), t1 = rd16(list + 6u * i + 2u),
                       t2 = rd16(list + 6u * i + 4u);
        const uint32_t i1 = flipped ? t2 : t1, i2 = flipped ? t1 : t2;
        if ((clipf(t0) | clipf(t1) | clipf(t2)) == 0) {
          if (facing(t0, i1, i2, b.eps))
            emit(&b, t0, i1, i2);
        } else if ((clipf(t0) & clipf(t1) & clipf(t2)) == 0) {
          clip_triangle(&b, t0, i1, i2);
        }
      }
    }
  }

}

void bw_build_cell_indices(X86 *c) {
  const uint32_t mode = c->r[R_EDX];
  if (!bw_cell_indices_native_ok(mode)) {
    fn_00875c60(c);
    return;
  }
  const uint32_t esp0 = c->r[R_ESP];
  bw_build_cell_indices_impl(c, c->r[R_ECX], mode);
  /* Return like the original: no stack arguments. */
  c->eip = rd32(esp0);
  c->r[R_ESP] = esp0 + 4u;
  recomp_return(c);
}
