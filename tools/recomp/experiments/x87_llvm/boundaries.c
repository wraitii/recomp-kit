/* Harness-only host return model. A single ordinary caller continuation is
 * allowed; callbacks, tail dispatch and unexpected returns fail loudly. */
#include "x86.h"
#include <stdlib.h>
const int recomp_resumable_stacks = 0;
int recomp_is_call_return(uint32_t a) {
    if (a != 0x12345678u)
        abort();
    return 1;
}
int recomp_module_is_call_return(uint32_t a) {
    (void)a;
    abort();
}
int32_t recomp_module_lookup(uint32_t a) {
    (void)a;
    abort();
}
int32_t recomp_index_of(uint32_t a) {
    (void)a;
    abort();
}
void recomp_call(X86 *c, uint32_t a) {
    (void)c;
    (void)a;
    abort();
}
void recomp_callback_return(X86 *c) {
    (void)c;
    abort();
}
#ifndef RK_FUNCTION_TEST
void rk_observe(X86 *c) {
    (void)c;
}
#endif
