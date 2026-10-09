#include "projection_cull.h"
#include "projection_cull_core.h"
#include <stdlib.h>

int gr_projection_use_original(void) {
  /* Same-binary A/B switch, read once and shared by the projection mods. */
  static int original = -1;
  int use_original = __atomic_load_n(&original, __ATOMIC_RELAXED);
  if (use_original < 0) {
    const char *value = getenv("RECOMP_PROJECTION_ORIGINAL");
    use_original = value && value[0] == '1' && value[1] == '\0';
    __atomic_store_n(&original, use_original, __ATOMIC_RELAXED);
  }
  return use_original;
}

static void projection_slots(X86 *c, double dot2, double spilled1,
                             double scratch) {
  unsigned p1 = (c->fpu_top - 1u) & 7u;
  unsigned p2 = (c->fpu_top - 2u) & 7u;
  unsigned p3 = (c->fpu_top - 3u) & 7u;
  {
    c->st[p1] = dot2;
    c->st[p2] = spilled1;
    c->st[p3] = scratch;
    c->st_bits[p1] = c->st_bits[p2] = c->st_bits[p3] = 0;
  }
  c->st_exact[p1] = c->st_exact[p2] = c->st_exact[p3] = 0;
  c->fpu_tag |=
      (uint16_t)((3u << (p1 * 2u)) | (3u << (p2 * 2u)) | (3u << (p3 * 2u)));
}

// @port 0x0081ae30 90% correctness
// Ghidra 0x0081ae30 ProjectionMeshBuilder::CullTriangleAgainstVolume.
// Completed C-runtime state matches in the isolated replay (early-out path
// excepted: DIVERGENCE(original) dead stack bytes, see below); original x86,
// fault-time state and actual callee execution have not been compared.
// TODO(decomp): preserve intermediate guest state if a memory access faults.
void fn_0081ae30(
    X86 *c); /* Retained mechanical original, bypasses FN override. */
void gr_projection_cull(X86 *c) {
  int use_original = gr_projection_use_original();
  if (use_original) {
    fn_0081ae30(c);
    return;
  }

  /* Registers, the status word and the spill live in locals for the whole
   * loop; it only reads guest memory, so *c cannot be observed mid-call. The
   * original stack frame is built only for the tail call into ProjectTriangle.
   * DIVERGENCE(original): the early-out path no longer writes the three
   * register saves below ESP or the float spill into the argument slot. Both
   * are dead stack bytes; the callee pops its own arguments. Loop-invariant
   * guest reads (plane list, vertex pointers, limit) are done once. */
  const uint32_t ebx_in = c->r[R_EBX], esi_in = c->r[R_ESI],
                 edi_in = c->r[R_EDI];
  const uint32_t esp0 = c->r[R_ESP];
  const uint32_t ecx = c->r[R_ECX];
  const uint32_t ebx = rd32(esp0 + 4u);
  const uint32_t edx0 = rd32(esp0 + 8u);
  const double limit = (double)rdf32(0x85448cu);
  const int single = ((c->fpu_cw >> 8) & 3u) == 0u;
  unsigned ie = 0;
  ProjCullOut o;
  if (!(single && projection_cull_fast(ecx, ebx, edx0, (float)limit,
                                       c->eflags_af, &o)))
    projection_cull_core(c, ecx, ebx, edx0, limit, single, &ie, c->eflags_af,
                         &o);
  const uint32_t edx = o.edx, diff = o.diff, plane = o.plane, last = edx - diff;
  const double dot1 = o.dot1, dot2 = o.dot2, spilled1 = o.spilled1,
               scratch = o.scratch;
  const int all_outside = o.all_outside;
  const unsigned af = o.af;
  const uint32_t v0 = rd32(rd32(ebx + 0x10u) + 0x34u),
                 v2 = rd32(rd32(ebx + 0x18u) + 0x34u);

  /* Status word as left by the last FCOMP (vertex2 against the limit). */
  {
    uint16_t sw = (uint16_t)(c->fpu_sw & (uint16_t)~0x4700u);
    if (dot2 != dot2 || limit != limit)
      sw |= 0x4500u;
    else if (dot2 < limit)
      sw |= 0x0100u;
    else if (dot2 == limit)
      sw |= 0x4000u;
    c->fpu_sw = (uint16_t)(sw | (ie & 1u));
  }
  projection_slots(c, dot2, spilled1, scratch);

  if (all_outside) {
    c->r[R_EAX] = (plane & 0xffff0000u) | fstsw(c);
    c->r[R_EDX] = edx;
    /* Last TEST AH,1. AF is undefined; the baseline keeps the INC's. */
    c->eflags_af = af;
    c->eflags_cf = c->eflags_of = c->eflags_zf = c->eflags_sf = c->eflags_pf =
        0;
    c->eip = rd32(esp0);
    c->r[R_ESP] = esp0 + 12u;
    recomp_return(c);
    return;
  }

  /* Last plane reached with the triangle not fully outside: hand it to
   * ProjectTriangle with the original frame (three saves, the spill in the
   * argument slot, argument, return address). */
  wr32(esp0 - 4u, ebx_in);
  wr32(esp0 - 8u, esi_in);
  wr32(esp0 - 12u, edi_in);
  wrf32(esp0 + 4u, fto_float(c, dot1));
  c->r[R_EAX] = last;
  c->r[R_EDX] = edx;
  c->r[R_EBX] = ebx;
  c->r[R_ESI] = v0;
  c->r[R_EDI] = v2;
  {
    uint32_t a = edx, b = last;
    c->eflags_cf = a < b;
    c->eflags_of = ((a ^ b) & (a ^ diff)) >> 31;
    c->eflags_af = ((a ^ b ^ diff) >> 4) & 1u;
    c->eflags_zf = 1;
    c->eflags_sf = diff >> 31;
    c->eflags_pf = parity8(diff);
  }
  c->r[R_ESP] = esp0 - 12u - 4u;
  wr32(c->r[R_ESP], ebx);
  c->r[R_ESP] -= 4u;
  wr32(c->r[R_ESP], 0x81af03u);
  recomp_call(c, 0x0081b010u);

  c->r[R_EDI] = rd32(c->r[R_ESP]);
  c->r[R_ESP] += 4u;
  c->r[R_ESI] = rd32(c->r[R_ESP]);
  c->r[R_ESP] += 4u;
  c->r[R_EBX] = rd32(c->r[R_ESP]);
  c->r[R_ESP] += 4u;
  c->eip = rd32(c->r[R_ESP]);
  c->r[R_ESP] += 12u;
  recomp_return(c);
}
