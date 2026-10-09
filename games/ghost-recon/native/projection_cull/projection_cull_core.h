#ifndef GHOST_RECON_PROJECTION_CULL_CORE_H
#define GHOST_RECON_PROJECTION_CULL_CORE_H
/* The plane-volume cull of 0x0081ae30 as a pure function of guest memory and
 * the x87 precision control, shared by gr_projection_cull and the native
 * ProjectionMeshBuilder::Build. It reads guest memory only and keeps the
 * sticky NaN (IE) bit in a caller local; nothing in *c is touched. */
#include "x86.h"

/* x87 arithmetic result under the precision control the caller read once:
 * NaN becomes the indefinite and raises IE, PC=00 rounds to single. Mirrors
 * fx87() with the control and status words kept out of *c. */
#define PROJ_FX(r)                                                             \
  ({                                                                           \
    double r_ = (r);                                                           \
    if (r_ != r_) {                                                            \
      ie |= 1u;                                                                \
      r_ = x87_indefinite();                                                   \
    } else if (single) {                                                       \
      r_ = (double)(float)r_;                                                  \
    }                                                                          \
    r_;                                                                        \
  })

static inline __attribute__((always_inline)) double
projection_dot_value(uint32_t vertex, uint32_t plane, int single,
                     unsigned *ie_out, double *x_product) {
#pragma STDC FP_CONTRACT OFF
  /* Original load and arithmetic order: y*y, z*z, add, x*x, add, distance.
   * Every operation is rounded; build with FP contraction disabled. */
  unsigned ie = 0;
  double y = (double)rdf32(vertex + 4u);
  y = PROJ_FX(y * (double)rdf32(plane + 4u));
  double z = (double)rdf32(vertex + 8u);
  z = PROJ_FX(z * (double)rdf32(plane + 8u));
  double yz = PROJ_FX(y + z);
  double x = (double)rdf32(plane);
  x = PROJ_FX(x * (double)rdf32(vertex));
  double sum = PROJ_FX(yz + x);
  sum = PROJ_FX(sum + (double)rdf32(plane + 12u));
  *x_product = x;
  *ie_out |= ie;
  return sum;
}

/* n . (x, y, z) < limit, as the backface test's FADDP chain and FCOMP see it:
 * y*ny, z*nz, add, x*nx, add. Returns the C0 bit (less than or unordered). */
static inline __attribute__((always_inline)) int
projection_facing_c0(uint32_t tri, double px, double py, double pz,
                     double limit, int single, unsigned *ie_out) {
#pragma STDC FP_CONTRACT OFF
  unsigned ie = 0;
  double a = PROJ_FX(py * (double)rdf32(tri + 4u));
  double b = PROJ_FX(pz * (double)rdf32(tri + 8u));
  double ab = PROJ_FX(a + b);
  double d = PROJ_FX(px * (double)rdf32(tri));
  double sum = PROJ_FX(ab + d);
  ie |= (sum != sum) | (limit != limit);
  *ie_out |= ie;
  return !(sum >= limit);
}

typedef struct ProjCullOut {
  uint32_t edx, diff, plane;
  double dot1, dot2, spilled1, scratch;
  unsigned af;
  int all_outside;
} ProjCullOut;

/* Walks the planes from first_plane. all_outside: every vertex of some plane
 * is outside, so the triangle is rejected; otherwise the last plane was
 * reached and the triangle goes to ProjectTriangle. */
static inline __attribute__((always_inline)) void
projection_cull_core(X86 *c, uint32_t self, uint32_t tri, uint32_t first_plane,
                     double limit, int single, unsigned *ie_out, unsigned af0,
                     ProjCullOut *o) {
  const uint32_t tri0 = rd32(tri + 0x10u), tri1 = rd32(tri + 0x14u),
                 tri2 = rd32(tri + 0x18u);
  const uint32_t v0 = rd32(tri0 + 0x34u), v1 = rd32(tri1 + 0x34u),
                 v2 = rd32(tri2 + 0x34u);
  const uint32_t planes = rd32(self + 0x10u);
  const uint32_t last = rd32(self + 0xcu) - 1u;
  uint32_t edx = first_plane, plane, diff;
  unsigned ie = 0, af = af0;
  double dot1, dot2, spilled1, scratch;
  int all_outside;

  for (;;) {
    plane = rd32(planes + edx * 4u);
    dot1 = projection_dot_value(v1, plane, single, &ie, &scratch);
    /* The original spills vertex 1 through a float stack slot. */
    spilled1 = (double)fto_float(c, dot1);
    dot2 = projection_dot_value(v2, plane, single, &ie, &scratch);
    double dot0 = projection_dot_value(v0, plane, single, &ie, &scratch);

    /* FCOMP order is vertex0, spilled vertex1, vertex2; unordered sets C0
     * too, which !(a >= b) covers, and raises IE. */
    ie |= (dot0 != dot0) | (spilled1 != spilled1) | (dot2 != dot2);
    all_outside = !(dot0 >= limit) & !(spilled1 >= limit) & !(dot2 >= limit);
    diff = edx - last;
    if (all_outside | (diff == 0))
      break;
    /* INC leaves AF; TEST at the early exit does not touch it. */
    af = ((edx ^ 1u ^ (edx + 1u)) >> 4) & 1u;
    edx++;
  }
  *ie_out |= ie;
  o->edx = edx;
  o->diff = diff;
  o->plane = plane;
  o->dot1 = dot1;
  o->dot2 = dot2;
  o->spilled1 = spilled1;
  o->scratch = scratch;
  o->af = af;
  o->all_outside = all_outside;
}

/* Single-precision fast paths. With PC=00 every x87 add and multiply rounds to
 * binary32, and a binary64 operation followed by that rounding equals the
 * binary32 operation (53 >= 2*24+2), so plain float code gives the same
 * values. Sticky IE and the indefinite substitution only matter once a NaN
 * appears, so these return failure on any NaN (or a NaN limit) and the caller
 * reruns the exact code above. Locals only: no call inside, so the compiler can
 * keep the vertex data in registers. */
static inline __attribute__((always_inline)) float
projection_dot_f(float vy, float vz, float vx, float py, float pz, float px,
                 float pd) {
#pragma STDC FP_CONTRACT OFF
  float y = vy * py;
  float z = vz * pz;
  float yz = y + z;
  float x = px * vx;
  float sum = yz + x;
  return sum + pd;
}

/* Returns 0 when the exact path must be used. */
static inline __attribute__((always_inline)) int
projection_cull_fast(uint32_t self, uint32_t tri, uint32_t first_plane,
                     float limit, unsigned af0, ProjCullOut *o) {
#pragma STDC FP_CONTRACT OFF
  if (limit != limit)
    return 0;
  const uint32_t tri0 = rd32(tri + 0x10u), tri1 = rd32(tri + 0x14u),
                 tri2 = rd32(tri + 0x18u);
  const uint32_t v0 = rd32(tri0 + 0x34u), v1 = rd32(tri1 + 0x34u),
                 v2 = rd32(tri2 + 0x34u);
  const float v0x = rdf32(v0), v0y = rdf32(v0 + 4u), v0z = rdf32(v0 + 8u);
  const float v1x = rdf32(v1), v1y = rdf32(v1 + 4u), v1z = rdf32(v1 + 8u);
  const float v2x = rdf32(v2), v2y = rdf32(v2 + 4u), v2z = rdf32(v2 + 8u);
  const uint32_t planes = rd32(self + 0x10u);
  const uint32_t last = rd32(self + 0xcu) - 1u;
  uint32_t edx = first_plane, plane, diff;
  unsigned af = af0;
  float d0, d1, d2, px, py, pz;
  int all_outside;
  for (;;) {
    plane = rd32(planes + edx * 4u);
    px = rdf32(plane);
    py = rdf32(plane + 4u);
    pz = rdf32(plane + 8u);
    const float pd = rdf32(plane + 12u);
    d1 = projection_dot_f(v1y, v1z, v1x, py, pz, px, pd);
    d2 = projection_dot_f(v2y, v2z, v2x, py, pz, px, pd);
    d0 = projection_dot_f(v0y, v0z, v0x, py, pz, px, pd);
    if (d0 != d0 || d1 != d1 || d2 != d2)
      return 0;
    all_outside = !(d0 >= limit) & !(d1 >= limit) & !(d2 >= limit);
    diff = edx - last;
    if (all_outside | (diff == 0))
      break;
    af = ((edx ^ 1u ^ (edx + 1u)) >> 4) & 1u;
    edx++;
  }
  o->edx = edx;
  o->diff = diff;
  o->plane = plane;
  o->dot1 = (double)d1; /* already binary32: the float spill is exact */
  o->dot2 = (double)d2;
  o->spilled1 = (double)d1;
  o->scratch = (double)(px * v0x);
  o->af = af;
  o->all_outside = all_outside;
  return 1;
}

/* C0 of the backface FCOMP: 1 or 0, or -1 when the exact path is needed. */
static inline __attribute__((always_inline)) int
projection_facing_fast(uint32_t tri, float px, float py, float pz, float limit) {
#pragma STDC FP_CONTRACT OFF
  float a = py * rdf32(tri + 4u);
  float b = pz * rdf32(tri + 8u);
  float ab = a + b;
  float d = px * rdf32(tri);
  float sum = ab + d;
  if (sum != sum || limit != limit)
    return -1;
  return !(sum >= limit);
}
#endif
