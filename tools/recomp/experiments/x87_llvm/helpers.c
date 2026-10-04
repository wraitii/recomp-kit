/* ABI adapters keep native X86 layout in C, not duplicated in the IR emitter.
 * Compiled to bitcode, linked AFTER stack lifting, then inlined by LLVM.
 */
#include "x86.h"
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
    (void)c;
    return (double)rdf32(a);
}
INLINE void rk_store(X86 *c, unsigned a, double v) {
    wrf32(a, fto_float(c, v));
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
