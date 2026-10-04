/* ABI adapters keep native X86 layout in C, not duplicated in the IR emitter.
 * Compiled to bitcode, linked AFTER stack lifting, then inlined by LLVM.
 */
#include "access.h"
#define INLINE __attribute__((always_inline))
INLINE unsigned rk_reg(X86 *c, unsigned n) {
    return c->r[n];
}
INLINE unsigned rk_top(X86 *c) {
    return c->fpu_top;
}
INLINE void rk_set_top(X86 *c, unsigned n) {
    c->fpu_top = n;
}
INLINE double rk_load(X86 *c, unsigned a) {
    return (double)rk_access_f32(c, a);
}
INLINE void rk_store(X86 *c, unsigned a, double v) {
    rk_access_store32(c, a, fto_float(c, v));
}
INLINE double rk_round(X86 *c, double v) {
    return fx87(c, v);
}
INLINE void rk_push(X86 *c, double v) {
    fpush(c, v);
}
INLINE void rk_pop(X86 *c) {
    fdrop(c);
}
INLINE double rk_read(X86 *c, unsigned n) {
    return ST(c, n);
}
INLINE void rk_set(X86 *c, unsigned n, double v) {
    fset(c, n, v);
}
INLINE void rk_slot(X86 *c, unsigned physical, double v, unsigned live) {
    c->st[physical] = v;
    c->st_bits[physical] = 0;
    c->st_exact[physical] = 0;
    ftag_put(c, physical, live ? ftag_classify(v) : FTAG_EMPTY);
}

INLINE double rk_load64(X86 *c, unsigned a) {
    return rk_access_f64(c, a);
}
INLINE void rk_compare(X86 *c, double a, double b) {
    fcom(c, a, b);
}
/* Read-only x87 observer. The harness may capture the complete state here. */
extern void rk_observe(X86 *c);
INLINE void rk_fnstsw(X86 *c) {
    rk_observe(c);
    c->r[R_EAX] = (c->r[R_EAX] & 0xffff0000u) | fstsw(c);
}
INLINE void rk_test_ah(X86 *c, unsigned mask) {
    unsigned v = ((c->r[R_EAX] >> 8) & 255u) & mask;
    c->eflags_cf = c->eflags_of = 0;
    c->eflags_zf = v == 0;
    c->eflags_sf = (v >> 7) & 1u;
    c->eflags_pf = parity8(v);
}
INLINE void rk_xor_eax(X86 *c, unsigned unused) {
    (void)unused;
    c->r[R_EAX] = 0;
    c->eflags_cf = c->eflags_of = c->eflags_sf = 0;
    c->eflags_zf = c->eflags_pf = 1;
}
INLINE void rk_write_reg(X86 *c, unsigned reg, unsigned v) {
    c->r[reg] = v;
}
INLINE unsigned rk_zf(X86 *c, unsigned unused) {
    (void)unused;
    return c->eflags_zf;
}
INLINE void rk_ret(X86 *c, unsigned cleanup) {
    c->eip = rk_access_u32(c, c->r[R_ESP]);
    c->r[R_ESP] += 4u + cleanup;
    recomp_return(c);
}

/* Direct-access experiment: ordinary mapped guest memory cannot alias X86.
 * No artificial full-CPU observer is attached to FNSTSW. Numeric helpers above
 * are shared unchanged. Complete state is still required before dispatch. */
INLINE double rk_direct_load(X86 *c, unsigned a) {
    (void)c;
    return (double)rdf32(a);
}
INLINE double rk_direct_load64(X86 *c, unsigned a) {
    (void)c;
    return rdf64(a);
}
INLINE void rk_direct_store(X86 *c, unsigned a, double v) {
    wrf32(a, fto_float(c, v));
}
INLINE void rk_direct_fnstsw(X86 *c) {
    c->r[R_EAX] = (c->r[R_EAX] & 0xffff0000u) | fstsw(c);
}
INLINE void rk_status_at(X86 *c, unsigned top) {
    c->r[R_EAX] = (c->r[R_EAX] & 0xffff0000u) |
                  (uint16_t)((c->fpu_sw & (uint16_t)~0x3800u) | (uint16_t)(top << 11));
}
INLINE void rk_direct_ret(X86 *c, unsigned cleanup) {
    c->eip = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4u + cleanup;
    recomp_return(c);
}
