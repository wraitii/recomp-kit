#include "ray_triangles.h"
#include "x86.h"
#include <stdlib.h>

/* Guest layouts (Ghidra types CollisionSet, CollisionInstance, CollisionMesh,
 * CollisionSubMesh, CollisionTriangle, Ray, RayQueryResult, RayHitList). */
#define F(x) fx87(c, (x))
#define FL(a) ((double)rdf32(a))
#define TOF(x) fto_float(c, (x))

/* C0 after FCOM a,b (a<b or unordered) and C0|C3 (a<=b or unordered). */
static inline int c0(double a, double b) { return !(a >= b); }
static inline int c03(double a, double b) { return !(a > b); }

typedef struct {
  X86 *c;
  int have;
  double a, b;
} Cmp;

/* FCOM/FCOMP: ordered compares only remember their operands; the status word
 * is written once at exit from the last one. NaN compares go through the
 * runtime at once so IE and the condition codes are exact. */
static inline void cmp(Cmp *k, double a, double b) {
  if (a != a || b != b) {
    fcom(k->c, a, b);
    k->have = 0;
  } else {
    k->have = 1;
    k->a = a;
    k->b = b;
  }
}

static inline void put3f(uint32_t a, float x, float y, float z) {
  wrf32(a, x);
  wrf32(a + 4, y);
  wrf32(a + 8, z);
}
static inline void copy3(uint32_t dst, uint32_t src) {
  uint32_t x = rd32(src), y = rd32(src + 4), z = rd32(src + 8);
  wr32(dst, x);
  wr32(dst + 4, y);
  wr32(dst + 8, z);
}

/* Guest calls. Frame layout below the entry ESP: scratch vectors A/B/C, then
 * the call frames. */
static void g_reserve(X86 *c, uint32_t esp, uint32_t list, uint32_t n) {
  c->r[R_ESP] = esp;
  c->r[R_ECX] = list;
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], n);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], 0u);
  recomp_call(c, 0x005587a0u); /* RET 4 */
}
static void g_cdecl3(X86 *c, uint32_t esp, uint32_t fn, uint32_t a0,
                     uint32_t a1, uint32_t a2) {
  c->r[R_ESP] = esp;
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], a2);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], a1);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], a0);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], 0u);
  recomp_call(c, fn);
  c->r[R_ESP] += 12u;
}

static void append_hit(X86 *c, uint32_t esp, uint32_t list, const uint32_t rec[9]) {
  uint32_t count = rd32(list + 8u);
  g_reserve(c, esp, list, count + 1u);
  uint32_t dst = rd32(list) + count * 0x24u;
  for (unsigned i = 0; i < 9; i++)
    wr32(dst + 4u * i, rec[i]);
}

static inline uint32_t fbits(float f) {
  uint32_t u;
  memcpy(&u, &f, 4);
  return u;
}

// @port 0x00556840 90% correctness
// Ghidra 0x00556840 CollisionSet::RayTestTriangles.
// Mirrors the x87 spill/rounding points of the original; guest calls
// (RayHitList::Reserve, Matrix3x3 helpers, mesh lookup) stay guest code.
// Ray, start point and result are assumed not to alias each other's memory
// (the original re-reads them between stores). Registers other than
// EAX/ECX/EDX and flags are unchanged; ECX/EDX/flags/EAX high bits and dead
// x87 slots differ from the original. TODO(decomp): compare against the
// original x86, not only the translated C.
void fn_00556840(X86 *c); /* Retained mechanical original. */
void gr_ray_triangles(X86 *c) {
  static int original = -1;
  int use_original = __atomic_load_n(&original, __ATOMIC_RELAXED);
  if (use_original < 0) {
    const char *value = getenv("RECOMP_RAYTRI_ORIGINAL");
    use_original = value && value[0] == '1' && value[1] == '\0';
    __atomic_store_n(&original, use_original, __ATOMIC_RELAXED);
  }
  if (use_original) {
    fn_00556840(c);
    return;
  }

  const uint32_t esp0 = c->r[R_ESP];
  const uint32_t self = c->r[R_ECX];
  const uint32_t owner = rd32(esp0 + 4u), ray = rd32(esp0 + 8u),
                 res = rd32(esp0 + 12u), list = rd32(esp0 + 16u),
                 sp = rd32(esp0 + 20u), flags = rd32(esp0 + 24u),
                 blockMask = rd32(esp0 + 28u), hitMask = rd32(esp0 + 32u);
  const uint32_t esp = esp0 - 0x40u;
  const uint32_t vA = esp0 - 0x10u, vB = esp0 - 0x20u, vC = esp0 - 0x30u;
  const double K_neg = FL(0x856090u), K_eps = FL(0x85b07cu),
               K1 = FL(0x856068u), K0 = FL(0x85448cu);
  const uint32_t stamp = rd32(0x8f37c0u);
  const int allowBack = (int)((flags >> 8) & 1u);
  Cmp k = {c, 0, 0, 0};
  uint32_t ret = 0;

  wr32(res + 0x34u, 0x7f7fffffu);
  double rt = FL(res + 0x34u);

  int ntri = (int)rd32(self + 0xcu);
  for (int i = 0; i < ntri; i++) {
    uint32_t T = rd32(rd32(self + 4u) + 4u * (uint32_t)i);
    if (rd32(T + 0x28u) == stamp)
      continue;
    wr32(T + 0x28u, stamp);
    uint32_t m = rd32(T + 0x14u);
    if (m != 0 && (blockMask & m) != 0) {
      if ((flags & 0x3000u) == 0 || (hitMask & m) == 0)
        continue;
    }
    uint32_t pa = rd32(T + 4u), pb = rd32(T + 8u), pc = rd32(T + 0xcu);
    double v0x = FL(pa), v0y = FL(pa + 4u), v0z = FL(pa + 8u);
    double v1x = FL(pb), v1y = FL(pb + 4u), v1z = FL(pb + 8u);
    double v2x = FL(pc), v2y = FL(pc + 4u), v2z = FL(pc + 8u);
    double e1x = TOF(F(v1x - v0x)), e1y = TOF(F(v1y - v0y)),
           e1z = TOF(F(v1z - v0z));
    double e2x = TOF(F(v2x - v0x)), e2y = TOF(F(v2y - v0y));
    double e2zs = F(v2z - v0z);
    double e2z = TOF(e2zs);
    double dx = FL(ray + 0xcu), dy = FL(ray + 0x10u), dz = FL(ray + 0x14u);
    double h0 = TOF(F(F(e2zs * dy) - F(e2y * dz)));
    double h1 = TOF(F(F(e2x * dz) - F(e2z * dx)));
    double h2 = TOF(F(F(e2y * dx) - F(e2x * dy)));
    double dets = F(F(F(h0 * e1x) + F(e1z * h2)) + F(e1y * h1));
    double det = TOF(dets);
    cmp(&k, dets, K_neg);
    if (!c03(dets, K_neg)) {
      cmp(&k, det, K_eps);
      if (c0(det, K_eps))
        continue;
    }
    if (!allowBack) {
      cmp(&k, det, K_eps);
      if (c0(det, K_eps))
        continue;
    }
    double inv = TOF(F(fdivz(c, K1, det)));
    double spx = FL(sp), spy = FL(sp + 4u), spz = FL(sp + 8u);
    double sx = TOF(F(spx - v0x)), sy = TOF(F(spy - v0y)),
           sz = TOF(F(spz - v0z));
    double us = F(F(F(F(F(sz * h2) + F(sy * h1)) + F(sx * h0))) * inv);
    double uf = TOF(us);
    cmp(&k, us, K0);
    if (c0(us, K0))
      continue;
    cmp(&k, uf, K1);
    if (!c03(uf, K1))
      continue;
    double q0 = TOF(F(F(sy * e1z) - F(sz * e1y)));
    double q1 = TOF(F(F(sz * e1x) - F(sx * e1z)));
    double q2s = F(F(sx * e1y) - F(sy * e1x));
    double q2 = TOF(q2s);
    double vs = F(F(F(F(q2s * dz) + F(q1 * dy)) + F(q0 * dx)) * inv);
    cmp(&k, vs, K0);
    if (c0(vs, K0))
      continue;
    double ws = F(vs + uf);
    cmp(&k, ws, K1);
    if (!c03(ws, K1))
      continue;
    double ts = F(F(F(F(q0 * e2x) + F(e2z * q2)) + F(e2y * q1)) * inv);
    double tf = TOF(ts);
    cmp(&k, ts, K0);
    if (c0(ts, K0))
      continue;
    cmp(&k, tf, rt);
    if (!c0(tf, rt))
      continue;

    uint32_t mk = rd32(T + 0x14u);
    if ((flags & 0x1000u) && (hitMask & mk))
      wr32(res + 0x4cu, rd32(res + 0x4cu) + 1u);
    mk = rd32(T + 0x14u);
    if ((flags & 0x2000u) && (hitMask & mk)) {
      uint32_t rec[9], nrm = rd32(T + 0x10u);
      rec[0] = rd32(nrm);
      rec[1] = rd32(nrm + 4u);
      rec[2] = rd32(nrm + 8u);
      rec[3] = fbits(TOF(F(F(tf * dx) + spx)));
      rec[4] = fbits(TOF(F(F(tf * dy) + spy)));
      rec[5] = fbits(TOF(F(F(tf * dz) + spz)));
      rec[6] = fbits((float)tf);
      rec[7] = T;
      rec[8] = owner;
      append_hit(c, esp, list, rec);
    }
    if ((blockMask & rd32(T + 0x14u)) != 0)
      continue;
    wrf32(res + 0x34u, (float)tf);
    rt = tf;
    double ex = TOF(F(tf * dx)), ey = TOF(F(tf * dy)), ez = TOF(F(tf * dz));
    float px = TOF(F(ex + spx)), py = TOF(F(ey + spy)), pz = TOF(F(ez + spz));
    ret = 1;
    put3f(res + 0x10u, px, py, pz);
    put3f(res + 0x28u, px, py, pz);
    copy3(res + 4u, rd32(T + 0x10u));
    copy3(res + 0x1cu, rd32(T + 0x10u));
    wr32(res + 0x3cu, T);
    wr32(res + 0x40u, owner);
    wr32(res + 0x38u, 0u);
    wr32(res + 0x48u, 0u);
  }

  if (!(flags & 0x4000u)) {
    int ninst = (int)rd32(self + 0x34u);
    for (int ii = 0; ii < ninst; ii++) {
      uint32_t inst = rd32(rd32(self + 0x2cu) + 4u * (uint32_t)ii);
      if (rd32(inst + 0x194u) == stamp)
        continue;
      wr32(inst + 0x194u, stamp);
      uint32_t mat = inst + 0xa0u;
      /* mesh = (*DAT_008d266c)->vtbl[0xb8/4](meshId) */
      uint32_t obj = rd32(0x8d266cu);
      uint32_t fnp = rd32(rd32(obj) + 0xb8u);
      c->r[R_ESP] = esp;
      c->r[R_ECX] = obj;
      c->r[R_ESP] -= 4u;
      wr32(c->r[R_ESP], rd32(inst + 0x164u));
      c->r[R_ESP] -= 4u;
      wr32(c->r[R_ESP], 0u);
      recomp_call(c, fnp);
      uint32_t mesh = c->r[R_EAX];

      double scale = FL(inst + 0xc4u);
      double ox = FL(ray), oy = FL(ray + 4u), oz = FL(ray + 8u);
      double px0 = FL(inst + 0x94u), py0 = FL(inst + 0x98u), pz0 = FL(inst + 0x9cu);
      put3f(vA, TOF(F(ox - px0)), TOF(F(oy - py0)), TOF(F(oz - pz0)));
      g_cdecl3(c, esp, 0x531970u, vB, vA, mat);
      double invs = F(fdivz(c, K1, scale));
      double olx = TOF(F(invs * FL(vB))), oly = TOF(F(invs * FL(vB + 4u))),
             olz = TOF(F(invs * FL(vB + 8u)));
      g_cdecl3(c, esp, 0x531970u, vC, ray + 0xcu, mat);
      double dlx = FL(vC), dly = FL(vC + 4u), dlz = FL(vC + 8u);
      double spx = FL(sp), spy = FL(sp + 4u), spz = FL(sp + 8u);
      put3f(vA, TOF(F(spx - px0)), TOF(F(spy - py0)), TOF(F(spz - pz0)));
      g_cdecl3(c, esp, 0x531970u, vB, vA, mat);
      double invs2 = F(fdivz(c, K1, scale));
      double slx = TOF(F(invs2 * FL(vB))), sly = TOF(F(invs2 * FL(vB + 4u))),
             slz = TOF(F(invs2 * FL(vB + 8u)));
      (void)olx; (void)oly; (void)olz;
      double dx = FL(ray + 0xcu), dy = FL(ray + 0x10u), dz = FL(ray + 0x14u);

      int nsub = (int)rd32(mesh + 0x1cu);
      for (int j = 0; j < (int)rd32(mesh + 0x1cu); j++) {
        (void)nsub;
        uint32_t sm = rd32(rd32(mesh + 0x14u) + 4u * (uint32_t)(j & 0xffff));
        if (rd8(sm + 0x10u) & 8u)
          continue;
        uint32_t m0 = rd32(rd32(sm + 0x1cu) + 0x14u);
        if ((blockMask & m0) != 0) {
          if ((flags & 0x3000u) == 0 || (hitMask & m0) == 0)
            continue;
        }
        int ntr = (int)rd32(sm + 0x24u);
        if (ntr <= 0)
          continue;
        uint32_t off = 0;
        for (int t = ntr; t != 0; t--, off += 0x2cu) {
          uint32_t base = rd32(sm + 0x1cu);
          uint32_t T = base + off;
          uint32_t pa = rd32(T + 4u), pb = rd32(T + 8u), pc = rd32(T + 0xcu);
          double v0x = FL(pa), v0y = FL(pa + 4u), v0z = FL(pa + 8u);
          double v1x = FL(pb), v1y = FL(pb + 4u), v1z = FL(pb + 8u);
          double v2x = FL(pc), v2y = FL(pc + 4u), v2z = FL(pc + 8u);
          double e1x = TOF(F(v1x - v0x)), e1y = TOF(F(v1y - v0y)),
                 e1z = TOF(F(v1z - v0z));
          double e2x = TOF(F(v2x - v0x)), e2y = TOF(F(v2y - v0y)),
                 e2z = TOF(F(v2z - v0z));
          double h0 = TOF(F(F(dly * e2z) - F(dlz * e2y)));
          double h1 = TOF(F(F(dlz * e2x) - F(e2z * dlx)));
          double h2 = TOF(F(F(e2y * dlx) - F(dly * e2x)));
          double dets = F(F(F(e1z * h2) + F(e1y * h1)) + F(h0 * e1x));
          double det = TOF(dets);
          cmp(&k, dets, K_neg);
          if (!c03(dets, K_neg)) {
            cmp(&k, det, K_eps);
            if (c0(det, K_eps))
              continue;
          }
          if (!allowBack) {
            cmp(&k, det, K_eps);
            if (c0(det, K_eps))
              continue;
          }
          double inv = TOF(F(fdivz(c, K1, det)));
          double sx = TOF(F(slx - v0x)), sy = TOF(F(sly - v0y));
          double szs = F(slz - v0z);
          double sz = TOF(szs);
          double us = F(F(F(F(szs * h2) + F(sy * h1)) + F(sx * h0)) * inv);
          double uf = TOF(us);
          cmp(&k, us, K0);
          if (c0(us, K0))
            continue;
          cmp(&k, uf, K1);
          if (!c03(uf, K1))
            continue;
          double q0 = TOF(F(F(sy * e1z) - F(sz * e1y)));
          double q1 = TOF(F(F(sz * e1x) - F(e1z * sx)));
          double q2 = TOF(F(F(e1y * sx) - F(sy * e1x)));
          double vs = F(F(F(F(dlz * q2) + F(dly * q1)) + F(q0 * dlx)) * inv);
          cmp(&k, vs, K0);
          if (c0(vs, K0))
            continue;
          double ws = F(vs + uf);
          cmp(&k, ws, K1);
          if (!c03(ws, K1))
            continue;
          double ts = F(F(F(F(e2z * q2) + F(e2y * q1)) + F(q0 * e2x)) * inv);
          cmp(&k, ts, K0);
          if (c0(ts, K0))
            continue;
          double tw = TOF(F(FL(inst + 0xc4u) * ts));
          cmp(&k, tw, rt);
          if (!c0(tw, rt))
            continue;

          if ((flags & 0x1000u) && (rd32(T + 0x14u) & hitMask)) {
            wr32(res + 0x4cu, rd32(res + 0x4cu) + 1u);
            continue;
          }
          if ((flags & 0x2000u) && (hitMask & rd32(base + 0x14u))) {
            double tx = F(tw * dx);
            double ty = TOF(F(tw * dy)), tz = TOF(F(tw * dz));
            float px = TOF(F(tx + spx)), py = TOF(F(ty + spy)),
                  pz = TOF(F(tz + spz));
            uint32_t rec[9];
            g_cdecl3(c, esp, 0x531910u, vB, mat, rd32(T + 0x10u));
            rec[0] = rd32(vB);
            rec[1] = rd32(vB + 4u);
            rec[2] = rd32(vB + 8u);
            rec[3] = fbits(px);
            rec[4] = fbits(py);
            rec[5] = fbits(pz);
            rec[6] = fbits((float)tw);
            rec[7] = T;
            rec[8] = owner;
            append_hit(c, esp, list, rec);
          }
          if ((rd32(rd32(sm + 0x1cu) + 0x14u) & blockMask) != 0)
            continue;
          wrf32(res + 0x34u, (float)tw);
          rt = tw;
          double tx = F(tw * dx);
          double ty = TOF(F(tw * dy)), tz = TOF(F(tw * dz));
          float px = TOF(F(tx + spx)), py = TOF(F(ty + spy)),
                pz = TOF(F(tz + spz));
          put3f(res + 0x10u, px, py, pz);
          put3f(res + 0x28u, px, py, pz);
          g_cdecl3(c, esp, 0x531910u, vB, mat, rd32(T + 0x10u));
          copy3(res + 4u, vB);
          copy3(res + 0x1cu, vB);
          ret = 1;
          wr32(res + 0x3cu, T);
          wr32(res + 0x40u, owner);
          wr32(res + 0x38u, 0u);
          wr32(res + 0x48u, 0u);
        }
      }
    }
  }

  if (k.have)
    fcom(c, k.a, k.b);
  c->r[R_EAX] = ret;
  c->r[R_ESP] = esp0;
  c->eip = rd32(esp0);
  c->r[R_ESP] = esp0 + 0x24u;
  recomp_return(c);
}
