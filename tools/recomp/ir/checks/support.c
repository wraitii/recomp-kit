/* Synthetic return/fault seam for integer/x87 SSA comparisons. The divide-error
 * observer records the complete pre-fault CPU, then simulates a handler returning
 * with changed quotient/remainder registers. This is not real guest SEH. */
#include "x86.h"
#include <stdio.h>
#include <stdlib.h>

const int recomp_resumable_stacks = 0;

static void unexpected(const char *name, uint32_t addr) {
    fprintf(stderr, "IR checks: unexpected %s at %08x\n", name, addr);
    abort();
}

void recomp_null_access(uint32_t addr, int write) {
    (void)write;
    unexpected("null access", addr);
}

/* Observe the complete CPU at every guest store, including stores inside the
 * returning division-error seam. The watch callback reads state but cannot
 * modify it, matching the runtime's diagnostic watcher. */
typedef struct StoreObservation {
    X86 cpu;
    uint32_t addr, width;
    uint64_t value;
} StoreObservation;
static StoreObservation observations[32], reference[32];
static unsigned observation_count, reference_count;
static X86 *observed_cpu;

/* Byte-translated fixture callees RET through recomp_return, which asks
 * whether the target is a known call continuation. The generated wrapper
 * registers each caller fallthrough before it invokes the fixture. */
static uint32_t accepted_call_returns[8];
static unsigned accepted_call_return_count;

void ir_accept_call_return(uint32_t addr) {
    for (unsigned i = 0; i < accepted_call_return_count; ++i)
        if (accepted_call_returns[i] == addr)
            return;
    if (accepted_call_return_count >= sizeof accepted_call_returns / sizeof *accepted_call_returns)
        unexpected("call return capacity", addr);
    accepted_call_returns[accepted_call_return_count++] = addr;
}

void ir_observer_begin(X86 *c) {
    observed_cpu = c;
    observation_count = 0;
    accepted_call_return_count = 0;
    memset(observations, 0, sizeof observations);
    g_watch_base = 0x10000;
    g_watch_len = 2048;
    recomp_store_hook_update();
}

void ir_observer_end(void) {
    g_watch_len = 0;
    recomp_store_hook_update();
    observed_cpu = NULL;
}

void ir_observe_store(uint32_t addr, uint32_t width, uint64_t value) {
    if (!observed_cpu || observation_count >= 16)
        unexpected("store observation capacity", addr);
    StoreObservation *o = &observations[observation_count++];
    memcpy(&o->cpu, observed_cpu, sizeof o->cpu);
    o->addr = addr;
    o->width = width;
    o->value = value;
}

/* Scalar x87 publishes no x87 state at guest stores (see x87_scalar.py), so
 * those columns compare store snapshots with the x87 stack, tags, TOP and
 * status cleared. CW, GPRs, flags and the store itself stay exact; the final
 * state comparison still covers every x87 field. */
static void clear_x87_store_state(StoreObservation *o) {
    memset(o->cpu.st, 0, sizeof o->cpu.st);
    memset(o->cpu.st_bits, 0, sizeof o->cpu.st_bits);
    memset(o->cpu.st_exact, 0, sizeof o->cpu.st_exact);
    o->cpu.fpu_top = 0;
    o->cpu.fpu_sw = 0;
    o->cpu.fpu_tag = 0;
}

void ir_observer_compare(unsigned mode, int x87_deferred_at_stores) {
    static StoreObservation expected[32];
    if (!mode) {
        reference_count = observation_count;
        memcpy(reference, observations, sizeof reference);
        return;
    }
    memcpy(expected, reference, sizeof expected);
    if (x87_deferred_at_stores) {
        for (unsigned i = 0; i < 32; ++i) {
            clear_x87_store_state(&expected[i]);
            clear_x87_store_state(&observations[i]);
        }
    }
    if (observation_count != reference_count || memcmp(expected, observations, sizeof expected)) {
        fprintf(stderr, "IR checks: store CPU observations differ in mode %u\n", mode);
        abort();
    }
}

void recomp_callback_return(X86 *c) {
    if (c->eip != GUEST_RETURN_SENTINEL)
        unexpected("callback return", c->eip);
}

int recomp_is_call_return(uint32_t addr) {
    for (unsigned i = 0; i < accepted_call_return_count; ++i)
        if (accepted_call_returns[i] == addr)
            return 1;
    unexpected("return", addr);
    return 0;
}

int recomp_module_is_call_return(uint32_t addr) {
    unexpected("module return", addr);
    return 0;
}

int32_t recomp_index_of(uint32_t addr) {
    unexpected("dispatch", addr);
    return -1;
}

int32_t recomp_module_lookup(uint32_t addr) {
    unexpected("module dispatch", addr);
    return -1;
}

void recomp_call(X86 *c, uint32_t addr) {
    /* Generated dispatch table for the synthetic indirect-call fixtures. */
    extern void ir_indirect_dispatch(X86 * c, uint32_t addr);
    ir_indirect_dispatch(c, addr);
}

void ir_unexpected_call(uint32_t target) {
    unexpected("indirect call", target);
}

void recomp_div_error(X86 *c, uint32_t addr) {
    memcpy(g_mem + 0x10200, c, sizeof *c);
    wr32(0x107f0, addr);
    /* A returning divide-error handler may mutate arbitrary guest state. Move
     * EAX/EDX (the division result registers), a preserved-looking GPR and
     * every flag so a variant that fails to reload non-EAX/EDX fields differs
     * from the eager comparison body. The eager and SSA bodies share this seam,
     * so this checks reload coverage rather than inventing handler behavior. */
    c->r[R_EAX] ^= 0x1234u;
    c->r[R_EDX] ^= 0x4567u;
    /* Keep the non-EAX/EDX mutations small so a fixture that uses EBX/ESI as a
     * scratch pointer after the divide cannot leave the mapped arena. */
    c->r[R_EBX] ^= 0x20u;
    c->r[R_ESI] ^= 0x10u;
    c->eflags_cf ^= 1;
    c->eflags_zf ^= 1;
    c->eflags_sf ^= 1;
    c->eflags_of ^= 1;
    c->eflags_pf ^= 1;
    c->eflags_af ^= 1;
}
