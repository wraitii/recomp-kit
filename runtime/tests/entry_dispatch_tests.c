/* Check the emitted call boundary without executing an arbitrary game's code.
 * The table fixture retains a raw body beside a native replacement. Nested
 * direct calls and tail transfers must choose the replacement exactly once,
 * and runtime hooks must be able to delegate to it without re-entering hooks. */
#include "x86.h"
#include <stdio.h>
#include <stdlib.h>

void entry_0d001000(X86 *c);
void entry_0d001010(X86 *c);
void entry_0d001020(X86 *c);
void entry_0d001030(X86 *c);

const int recomp_profile_enabled = 0;
uint32_t recomp_frame_watch;
static unsigned native_calls, hook_calls, frame_changes, failures;
#define CHECK(x)                                                                                   \
    do {                                                                                           \
        if (!(x)) {                                                                                \
            fprintf(stderr, "line %d: %s\n", __LINE__, #x);                                        \
            failures++;                                                                            \
        }                                                                                          \
    } while (0)

void fixture_native(X86 *c) {
    native_calls++;
    c->r[0] += 10;
}

static void hook(X86 *c, uint32_t i) {
    CHECK(i == 0);
    hook_calls++;
    /* Call-original chooses the configured base, not the entry wrapper. */
    recomp_base_ptrs[i](c);
    c->r[0] += 100;
}

static void frame_hook(X86 *c, uint32_t i) {
    hook(c, i);
    c->r[5]++;
}

void recomp_frame_changed(X86 *c, uint32_t target, uint32_t before, uint32_t after) {
    CHECK(target >= 0x0d001000 && target <= 0x0d001030);
    CHECK(after == before + 1);
    CHECK(c->r[5] == after);
    frame_changes++;
}

void recomp_call(X86 *c, uint32_t target) {
    /* Profiling is disabled here; the real-table profile suite tests that
     * path. An unexpected fallback/lookup from a direct entry must fail. */
    (void)c;
    (void)target;
    abort();
}

int main(void) {
    X86 c = {0};
    entry_0d001010(&c);
    CHECK(c.r[0] == 10 && c.r[1] == 1 && native_calls == 1);
    entry_0d001020(&c);
    CHECK(c.r[0] == 20 && c.r[1] == 1 && native_calls == 2);
    recomp_raw_ptrs[0](&c);
    CHECK(c.r[0] == 21 && native_calls == 2);
    recomp_hook_ptrs[0] = hook;
    __atomic_store_n(&recomp_hooked[0], 1, __ATOMIC_RELEASE);
    entry_0d001030(&c);
#ifdef RECOMP_NO_HOOKS
    CHECK(c.r[0] == 31 && hook_calls == 0);
#else
    CHECK(c.r[0] == 131 && hook_calls == 1);
#endif
    CHECK(c.r[1] == 2 && native_calls == 3);
    recomp_frame_watch = 1;
    recomp_hook_ptrs[0] = frame_hook;
    entry_0d001030(&c);
#ifdef RECOMP_NO_HOOKS
    CHECK(frame_changes == 0 && c.r[5] == 0);
#else
    /* Native target, its caller and the outer caller each observe the change. */
    CHECK(frame_changes == 3 && c.r[5] == 1 && hook_calls == 2);
#endif
    __atomic_store_n(&recomp_hooked[0], 0, __ATOMIC_RELEASE);
    unsigned hooks_before = hook_calls;
    entry_0d001000(&c);
    CHECK(native_calls == 5 && hook_calls == hooks_before);
    printf("entry dispatch: %u failures\n", failures);
    return failures ? 1 : 0;
}
