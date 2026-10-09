#include "projection_cull.h"
#include "projection_cull_core.h"
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

/* Retained mechanical original, bypasses the FN override. */
void fn_0081aa00(X86 *c);

enum {
  G_TRI_POOL_COUNT_A = 0x00946718u,
  G_TRI_POOL_COUNT_B = 0x0094671cu,
  G_TRI_POOL_CURSOR = 0x00946720u,
  G_TRI_CURSOR_SRC = 0x00946724u,
  G_VTX_COUNT_A = 0x00946740u,
  G_VTX_COUNT_B = 0x00946744u,
  G_VTX_END = 0x00946748u,
  G_VTX_END_SRC = 0x0094674cu,
  G_VTX_CURSOR = 0x00946770u,
  G_VTX_CURSOR_SRC = 0x00946774u,
  G_Q_COUNT_A = 0x00946768u,
  G_Q_COUNT_B = 0x0094676cu,
  G_Q_CURSOR = 0x00946788u,
  G_Q_PTR = 0x00946790u,
  G_Q_CAPACITY = 0x00946794u,
  G_Q_COUNT = 0x00946798u,
  G_Q_GROWTH = 0x0094679cu,
};

/* Calls a guest function from native code: arguments pushed right to left
 * below a fixed frame base, the real return address, ECX for thiscall. The
 * callee's own RET n (or the caller's cleanup, for cdecl) is irrelevant:
 * ESP is put back to the base afterwards. */
static void gcall(X86 *c, uint32_t base, uint32_t target, uint32_t ret,
                  uint32_t ecx, unsigned n, const uint32_t *args) {
  uint32_t sp = base;
  for (unsigned i = n; i-- > 0;) {
    sp -= 4u;
    wr32(sp, args[i]);
  }
  sp -= 4u;
  wr32(sp, ret);
  c->r[R_ESP] = sp;
  c->r[R_ECX] = ecx;
  recomp_call(c, target);
  c->r[R_ESP] = base;
}

enum {
  F_PROJECT_TRIANGLE = 0x0081b010u,
  F_CULL_ALT = 0x0081af10u,
  F_SCRIPT_TRIANGLE = 0x00520960u,
};

typedef struct Build {
  X86 *c;
  uint32_t self, base;
  double limit;
  int single;
  unsigned ie;
} Build;

static inline void project_triangle(Build *b, uint32_t tri, uint32_t ret) {
  gcall(b->c, b->base, F_PROJECT_TRIANGLE, ret, b->self, 1, &tri);
}

static inline void script_triangle(Build *b, uint32_t tri, uint32_t ret) {
  gcall(b->c, b->base, F_SCRIPT_TRIANGLE, ret, b->self, 1, &tri);
}

static inline int facing(Build *b, uint32_t tri, double px, double py,
                         double pz, double limit) {
  if (b->single) {
    int r = projection_facing_fast(tri, (float)px, (float)py, (float)pz,
                                   (float)limit);
    if (r >= 0)
      return r;
  }
  return projection_facing_c0(tri, px, py, pz, limit, b->single, &b->ie);
}

/* CullTriangleAgainstVolume(tri, 0), or the alternate path 0x0081af10. */
static inline void cull_triangle(Build *b, uint32_t tri, uint32_t alt_ret) {
  if (rd8(b->self + 8u)) {
    uint32_t args[2] = {tri, 0};
    gcall(b->c, b->base, F_CULL_ALT, alt_ret, b->self, 2, args);
    return;
  }
  ProjCullOut o;
  if (!(b->single &&
        projection_cull_fast(b->self, tri, 0, (float)b->limit, 0, &o)))
    projection_cull_core(b->c, b->self, tri, 0, b->limit, b->single, &b->ie,
                         b->c->eflags_af, &o);
  if (!o.all_outside)
    project_triangle(b, tri, 0x81af03u);
}

// @port 0x0081aa00 85% correctness
// Ghidra 0x0081aa00 ProjectionMeshBuilder::Build.
// DIVERGENCE(original): dead x87 register contents, C0-C3 and the frame
// locals are not reproduced; the sticky IE bit is. The cull is inlined into
// the triangle loops, so the entry thunk, stack frame and register traffic of
// 0x0081ae30 disappear. Callees (volume planes, vtable calls, script
// triangle, ProjectTriangle, AppendQueuedTriangles) remain translated.
// TODO(decomp): fault-time state, and a differential run against the
// translated original over whole models.
static void projection_build_body(X86 *c);

/* RECOMP_PROJECTION_STATS=1: wall time per triangle inside Build, for either
 * path, printed every 1000 calls. Scene independent, unlike sampled shares. */
void gr_projection_build(X86 *c) {
  static int stats = -1;
  if (stats < 0) {
    const char *v = getenv("RECOMP_PROJECTION_STATS");
    stats = v && v[0] == '1';
  }
  if (!stats) {
    projection_build_body(c);
    return;
  }
  static uint64_t calls, tris, ns;
  uint32_t model = rd32(c->r[R_ESP] + 4u), count = rd32(model + 0x188u);
  uint64_t n = 0;
  for (uint32_t i = 0; i < count && i < 4096; i++)
    n += rd32(rd32(rd32(model + 0x18cu) + i * 4u) + 0x8cu);
  uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
  projection_build_body(c);
  ns += clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0;
  tris += n;
  if (++calls % 1000 == 0)
    fprintf(stderr,
            "projection build (%s): %llu calls, %llu triangles, %.1f ns/tri, "
            "%.1f us/call\n",
            gr_projection_use_original() ? "original" : "native",
            (unsigned long long)calls, (unsigned long long)tris,
            tris ? (double)ns / (double)tris : 0.0,
            (double)ns / (double)calls / 1000.0);
}

static void projection_build_body(X86 *c) {
  if (gr_projection_use_original()) {
    fn_0081aa00(c);
    return;
  }

  const uint32_t esp0 = c->r[R_ESP];
  const uint32_t self = c->r[R_ECX];
  const uint32_t model = rd32(esp0 + 4u);
  Build b = {.c = c,
             .self = self,
             .base = esp0 - 0x30u,
             .single = ((c->fpu_cw >> 8) & 3u) == 0u};
  const uint32_t base = b.base;

  /* projection->vtable[0x20](model + 0x94, model + 0xa0, float model[0xc4]).
   * FLD/FSTP of a float only quiets a signalling NaN. */
  uint32_t proj = rd32(self + 4u);
  uint32_t fbits = rd32(model + 0xc4u);
  if ((fbits & 0x7f800000u) == 0x7f800000u && (fbits & 0x007fffffu))
    fbits |= 0x00400000u;
  {
    uint32_t args[3] = {model + 0x94u, model + 0xa0u, fbits};
    gcall(c, base, rd32(rd32(proj) + 0x80u), 0x81aa30u, proj, 3, args);
  }
  gcall(c, base, 0x0081b8d0u, 0x81aa37u, self, 0, NULL);

  proj = rd32(self + 4u);
  uint32_t limit_bits =
      rd8(proj + 0x364u) == 0 ? rd32(0x0086e8fcu) : rd32(proj + 0x368u);
  float limit_f;
  memcpy(&limit_f, &limit_bits, 4);
  b.limit = (double)limit_f;

  /* Size the triangle queue for the total triangle count. */
  {
    int32_t count = (int32_t)rd32(model + 0x188u);
    int32_t growth = (int32_t)rd32(G_Q_GROWTH);
    int32_t quotient = 1;
    if (count > 0) {
      uint32_t list = rd32(model + 0x18cu);
      int32_t total = 0;
      for (int32_t i = count; i != 0; i--, list += 4u)
        total += (int32_t)rd32(rd32(list) + 0x8cu);
      if (total > 0) {
        if (growth == 0)
          abort(); /* the guest raises #DE here */
        quotient = (total - 1) / growth + 1;
      }
    }
    int32_t wanted = (int32_t)((uint32_t)quotient * (uint32_t)growth);
    if (wanted > (int32_t)rd32(G_Q_CAPACITY)) {
      uint32_t size = (uint32_t)wanted * 4u;
      gcall(c, base, 0x006f339fu, 0x81aaccu, c->r[R_ECX], 1, &size);
      uint32_t fresh = c->r[R_EAX];
      if (fresh) {
        for (int32_t i = 0; (int32_t)rd32(G_Q_COUNT) > 0 &&
                            i < (int32_t)rd32(G_Q_COUNT);
             i++)
          wr32(fresh + (uint32_t)i * 4u, rd32(rd32(G_Q_PTR) + (uint32_t)i * 4u));
        uint32_t old = rd32(G_Q_PTR);
        gcall(c, base, 0x006e7d41u, 0x81ab07u, c->r[R_ECX], 1, &old);
        wr32(G_Q_PTR, fresh);
        wr32(G_Q_CAPACITY, (uint32_t)wanted);
      }
    }
  }

  /* Reset the queue and pool cursors. */
  {
    uint32_t queue = rd32(G_Q_PTR);
    wr32(G_Q_COUNT, 0);
    wr32(G_Q_CURSOR, queue);
    wr32(G_Q_COUNT_A, 0);
    wr32(G_Q_COUNT_B, 0);
    uint32_t vcur = rd32(rd32(rd32(G_VTX_CURSOR_SRC)));
    wr32(G_VTX_COUNT_A, 0);
    wr32(G_VTX_CURSOR, vcur);
    wr32(G_VTX_COUNT_B, 0);
    uint32_t tcur = rd32(rd32(rd32(G_VTX_END_SRC)));
    wr32(G_TRI_POOL_COUNT_A, 0);
    wr32(G_VTX_END, tcur);
    wr32(G_TRI_POOL_COUNT_B, 0);
    wr32(G_TRI_POOL_CURSOR, rd32(rd32(rd32(G_TRI_CURSOR_SRC))));
  }

  for (int32_t m = 0; m < (int32_t)rd32(model + 0x188u); m++) {
    const uint32_t entry = rd32(rd32(model + 0x18cu) + (uint32_t)m * 4u);
    if (rd8(entry + 0x22u))
      continue;
    uint32_t six = 6;
    uint32_t owner = rd32(entry + 0x3cu);
    gcall(c, base, rd32(rd32(owner) + 0x24u), 0x81abb8u, owner, 1, &six);
    uint32_t e = c->r[R_EAX];
    if (!e)
      continue;
    e = rd32(e + 0x20u);
    if (!e)
      continue;
    e = rd32(e + 0x78u) + 0x3cu;
    if (rd8(e) || rd8(e + 1u))
      continue;

    uint32_t arg = entry + 0x54u;
    gcall(c, base, 0x004f1af0u, 0x81abf1u, rd32(self + 4u), 1, &arg);
    if (c->r[R_EAX] & 0xffu) {
      if (rd32(rd32(self + 4u) + 0x240u) == 1)
        continue;
      script_triangle(&b, entry, 0x81ac11u);
    }

    proj = rd32(self + 4u);
    const uint32_t mode230 = rd32(proj + 0x230u);
    const uint32_t mode234 = rd32(proj + 0x234u);
    const uint32_t mode240 = rd32(proj + 0x240u);
    if (rd32(entry + 0x8cu) == 0)
      continue;

    /* Which per-triangle action: all but the cull-only loops share the same
     * shape, so the choice is made once per model. */
    enum { PROJECT, FACING_CULL, FACING_PROJECT, CULL } kind;
    if (mode230 == 2)
      kind = PROJECT;
    else if (mode234 == 1)
      kind = mode240 == 1 ? FACING_CULL : FACING_PROJECT;
    else
      kind = mode240 == 1 ? CULL : PROJECT;

    double px = 0, py = 0, pz = 0;
    double face_limit = b.limit;
    if (kind == FACING_CULL || kind == FACING_PROJECT) {
      px = (double)rdf32(proj + 0x17cu);
      py = (double)rdf32(proj + 0x180u);
      pz = (double)rdf32(proj + 0x184u);
      if (kind == FACING_PROJECT)
        face_limit = (double)rdf32(0x0085448cu);
    }

    uint32_t i = 0;
    do {
      const uint32_t tri = rd32(entry + 0x90u) + i * 0x1cu;
      switch (kind) {
      case PROJECT:
        project_triangle(&b, tri, 0x81ac46u);
        break;
      case CULL:
        cull_triangle(&b, tri, 0x81ad9bu);
        break;
      case FACING_CULL:
        if (facing(&b, tri, px, py, pz, face_limit))
          cull_triangle(&b, tri, 0x81acdcu);
        break;
      case FACING_PROJECT:
        if (facing(&b, tri, px, py, pz, face_limit))
          project_triangle(&b, tri, 0x81ad57u);
        else
          script_triangle(&b, tri, 0x81ad5eu);
        break;
      }
      i++;
    } while (i < rd32(entry + 0x8cu));
  }

  {
    uint32_t args[6];
    for (unsigned k = 0; k < 6; k++)
      args[k] = rd32(esp0 + 8u + k * 4u);
    gcall(c, base, 0x0081c130u, 0x81ae1eu, self, 6, args);
  }

  c->fpu_sw |= (uint16_t)(b.ie & 1u);
  c->eip = rd32(esp0);
  c->r[R_ESP] = esp0 + 36u;
  recomp_return(c);
}
