/* A deterministic opaque callee records every CPU field, then changes fields
 * without any assumed calling convention. This is a boundary/mutation fixture,
 * not evidence about original game callees, actual hooks or OS exceptions. */
#include "x86.h"
#include <stdlib.h>
#include <stdio.h>
#include <setjmp.h>

const int recomp_resumable_stacks = 0;
void fixture_call(X86 *c) {
    memcpy(g_mem + 0x10200, c, sizeof *c);
    c->r[R_EAX] ^= 0xa5a5a5a5u;
    c->r[R_ECX] += 0x12345678u;
    c->r[R_EDX] -= 0x87654321u;
    c->r[R_EBX] = 0x10080;
    c->r[R_ESI] = 0x10040;
    c->r[R_EDI] = 0x10000;
    c->eflags_cf ^= 1;
    c->eflags_zf ^= 1;
    c->eflags_sf ^= 1;
    c->eflags_of ^= 1;
    c->eflags_pf ^= 1;
    c->eflags_af ^= 1;
    c->fpu_cw ^= 0x200;
    c->fpu_sw ^= 0x100;
    c->eip = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4;
}
void recomp_call(X86 *c, uint32_t target) {
    if (target != 0x200000)
        abort();
    fixture_call(c);
}
void recomp_callback_return(X86 *c) {
    (void)c;
}
int recomp_is_call_return(uint32_t target) {
    (void)target;
    return 0;
}
int recomp_module_is_call_return(uint32_t target) {
    (void)target;
    return 0;
}
int32_t recomp_index_of(uint32_t target) {
    (void)target;
    return -1;
}
int32_t recomp_module_lookup(uint32_t target) {
    (void)target;
    return -1;
}
static jmp_buf fault_jump;
static int fault_armed;
void recomp_null_access(uint32_t addr, int write) {
    if (!fault_armed || addr != 0x10 || write)
        abort();
    longjmp(fault_jump, 1);
}
void null_fault_0_fn_00100000(X86 *);
void null_fault_1_fn_00100000(X86 *);
void null_fault_2_fn_00100000(X86 *);
void null_fault_3_fn_00100000(X86 *);
void fixture_finish(void) {
#if defined(RECOMP_NULL_CHECKS) && RECOMP_NULL_CHECKS
    /* CPU storage lives outside the setjmp frame. This tests the compiled
     * eager fallback at an injected null access, not real guest SEH handlers. */
    static X86 initial, actual, expected;
    void (*functions[])(X86 *) = {null_fault_0_fn_00100000, null_fault_1_fn_00100000,
                                  null_fault_2_fn_00100000, null_fault_3_fn_00100000};
    memset(&initial, 0, sizeof initial);
    initial.r[R_EAX] = 0x98765432;
    initial.r[R_ECX] = 0xfedcba98;
    initial.r[R_EDX] = 0x12345678;
    fault_armed = 1;
    for (unsigned mode = 0; mode < 4; ++mode) {
        actual = initial;
        if (!setjmp(fault_jump)) {
            functions[mode](&actual);
            abort();
        }
        if (!mode)
            expected = actual;
        else if (memcmp(&actual, &expected, sizeof actual))
            abort();
    }
    fault_armed = 0;
    printf("NULL FAULT: CPU/flags match eager at the injected read in all four modes\n");
#endif
}
