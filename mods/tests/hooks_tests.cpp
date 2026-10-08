// hooks_tests.cpp - the mutable dispatch layer over immutable translated code.
//
// Tests drive the selected image's dispatch tables with returning replacement
// hooks. No original game routine runs; the guest image is mapped headlessly.
#include "mods_tests.h"
#include "shim_call.h"
#include "../mods_internal.h"
#include "../../runtime/loader.h"
#include "../../runtime/memory.h"
#include "../../runtime/guest.h"
#include "../../runtime/win32.h"
#include "../../runtime/imports.h"

#include <string.h>
#include <string>
#include <thread>
#include <vector>

namespace {

// Select entries from the current image's verified hook catalog, rather than
// assigning gameplay meanings to addresses from another game.
uint32_t OUTER = 0, INNER = 0;
const uint32_t RET_ADDR = 0x03000000u; // test-only return sentinel

std::vector<std::string> g_log;
PopModApi g_api[4]; // stable instances, one per test owner

void note(const char *what) {
    g_log.push_back(what);
}

} // namespace

// The loader's per-mod contexts are Task 10's. This suite installs its own
// provider through the one seam; it never defines mods_api_for, because there
// is exactly one definition of that in the tree.
static const PopModApi *test_provider(uint32_t owner) {
    return owner < 4 ? &g_api[owner] : nullptr;
}

namespace {

void reset_world() {
    // This thread plays the part run_entry plays in a real process: it drives
    // translated code directly, so it is the run thread and it is index 0,
    // which is where the baton starts. Registering it is what makes a
    // mutation from here apply inline rather than queue, and it is the same
    // one-line registration run_entry does for exactly the same reason.
    sched_set_guest_thread(true);
    g_log.clear();
    mods_set_context_provider(test_provider);
    mods_hooks_reset();
    mem_init();
    loader_load(nullptr);
    MOD_CHECK(mods_symbols_load(nullptr));
    OUTER = INNER = 0;
    for (uint32_t i = 0; i < recomp_func_count; ++i) {
        if (!mods_symbol_hookable(recomp_func_addrs[i]))
            continue;
        if (!OUTER)
            OUTER = recomp_func_addrs[i];
        else {
            INNER = recomp_func_addrs[i];
            break;
        }
    }
    if (!OUTER || !INNER) {
        mod_test_fail("hook tests require two verified entries", __FILE__, __LINE__);
        abort();
    }
    for (uint32_t i = 0; i < 4; ++i) {
        memset(&g_api[i], 0, sizeof g_api[i]);
        g_api[i].version = POP_MOD_API_VERSION;
        g_api[i].size = (uint32_t)sizeof(PopModApi);
        g_api[i].mod_index = i;
        g_api[i].mod_id = (i == 0) ? "test.mod" : "test.other";
        mods_fill_hooks_api(&g_api[i]);
        mods_hooks_set_load_order(i, i);
        mods_hooks_set_cpu_size(i, POP_CPU_V1_BASELINE_SIZE);
    }
}

// Runs mods_registry_pump from a thread that holds the baton, which is where
// the scheduler's checkpoint calls it. The CreateThread shim gives us a real
// guest thread; polling GetExitCodeThread is both the wait and the thing that
// lets the scheduler run it.
void pump_body(X86 *c) {
    mods_registry_pump();
    set_eax(c, 1);
}

void pump_on_guest_thread() {
    X86 *c = loader_context();
    static uint32_t tramp = 0;
    if (!tramp)
        tramp = imports_alloc_trampoline("test", "pump", pump_body, 1);
    uint32_t th = mod_test_call_import(c, "KERNEL32.dll", "CreateThread", {0, 0, tramp, 0, 0, 0});
    uint32_t pcode = mod_test_scratch(0x400);
    wr32(pcode, 0x103u);
    for (int i = 0; i < 4096 && rd32(pcode) == 0x103u; ++i)
        mod_test_call_import(c, "KERNEL32.dll", "GetExitCodeThread", {th, pcode});
    mod_test_call_import(c, "KERNEL32.dll", "CloseHandle", {th});
}

// Supply a returning replacement when a test exercises only before/after
// callbacks. This keeps arbitrary original game bodies out of framework tests.
void dispatch_test(X86 *c, uint32_t addr) {
    uint32_t fallback = 0;
    const auto status = mods_hook_install(
        0, addr,
        [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
            MOD_CHECK_EQ(api->hook_return(api, cpu, cpu->eax, 0), POP_OK);
        },
        POP_HOOK_REPLACE, nullptr, &fallback);
    MOD_CHECK(status == POP_OK || status == POP_E_CONFLICT);
    const int32_t i = recomp_index_of(addr);
    MOD_CHECK(i >= 0);
    if (i >= 0)
        recomp_hook_ptrs[i](c, (uint32_t)i);
    if (fallback)
        MOD_CHECK_EQ(mods_hook_remove(0, fallback), POP_OK);
}

// Push the test return sentinel exactly as a generated call site would.
void enter(uint32_t addr) {
    X86 *c = loader_context();
    loader_init_context(c);
    c->r[R_ESP] -= 4;
    wr32(c->r[R_ESP], RET_ADDR);
    dispatch_test(c, addr);
}

} // namespace

MOD_TEST_SUITE(hook_guest_call_import_preserves_registers) {
    reset_world();
    MOD_CHECK(mods_symbols_load(nullptr));
    const uint32_t trampoline = imports_alloc_trampoline(
        "test", "hook_import",
        [](X86 *c) {
            MOD_CHECK_EQ(arg(c, 0), 17u);
            MOD_CHECK_EQ(arg(c, 1), 25u);
            MOD_CHECK_EQ(c->r[R_ECX], 123u);
            c->r[R_EBX] = 456;
            c->r[R_EDX] = 789;
            set_eax(c, arg(c, 0) + arg(c, 1));
        },
        2);
    uint32_t result = 0, id = 0;
    MOD_CHECK_EQ(g_api[0].guest_call(&g_api[0], trampoline, 0, nullptr, 0, &result), POP_E_STATE);
    auto callback = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *user) {
        X86 *c = loader_context();
        const X86 saved = *c;
        uint32_t args[] = {17, 25}, value = 0;
        MOD_CHECK_EQ(api->guest_call(api, *(uint32_t *)user, 123, args, 2, &value), POP_OK);
        MOD_CHECK_EQ(value, 42u);
        MOD_CHECK(memcmp(c, &saved, sizeof saved) == 0);
        MOD_CHECK_EQ(api->guest_call(api, TRAMP_LIMIT - TRAMP_STRIDE, 0, nullptr, 0, &value),
                     POP_E_NOSYMBOL);
        MOD_CHECK_EQ(api->guest_call(api, *(uint32_t *)user + 1, 0, nullptr, 0, &value),
                     POP_E_NOSYMBOL);
        MOD_CHECK_EQ(api->hook_return(api, cpu, value, 0), POP_OK);
    };
    const uint32_t entry = loader_entry_point();
    MOD_CHECK_EQ(g_api[0].hook_install_ex(&g_api[0], entry, 0, callback, POP_HOOK_REPLACE,
                                          (void *)&trampoline, &id),
                 POP_OK);
    if (id) {
        enter(entry);
        MOD_CHECK_EQ(loader_context()->r[R_EAX], 42u);
        MOD_CHECK_EQ(g_api[0].hook_remove(&g_api[0], id), POP_OK);
    }
}

extern "C" uint64_t mods_view_test_push_count();
MOD_TEST_SUITE(hook_callsite_filter_snapshot_and_entry_identity) {
    reset_world();
    constexpr uint32_t target = 0x401000, match = 0x517613, other = 0x5176d7;
    uint32_t ids[3]{};
    auto before = [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        note("before");
        MOD_CHECK_EQ(mods_view_depth(), 1u);
        // AFTER/WRAP filtering must use entry identity even if BEFORE changes
        // the guest return address. The original RET must still see the edit.
        wr32(cpu->esp, 0x5176d7);
    };
    auto wrap = [](const PopModApi *a, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *) {
        note("wrap-in");
        MOD_CHECK_EQ(mods_view_depth(), 1u);
        MOD_CHECK_EQ(a->call_next(a, inv, cpu), POP_OK);
        MOD_CHECK_EQ(cpu->eax, 100u);
        note("wrap-out");
    };
    auto after = [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        note("after");
        MOD_CHECK_EQ(mods_view_depth(), 1u);
        ++cpu->eax;
    };
    auto install = g_api[0].hook_install_at_callsite;
    MOD_CHECK(install != nullptr);
    MOD_CHECK_EQ(install(&g_api[0], target, 0, before, POP_HOOK_BEFORE, nullptr, &ids[0]),
                 POP_E_INVAL);
    MOD_CHECK_EQ(ids[0], 0u);
    MOD_CHECK_EQ(install(&g_api[0], target, match, before, POP_HOOK_BEFORE, nullptr, &ids[0]),
                 POP_OK);
    MOD_CHECK_EQ(install(&g_api[0], target, match, wrap, POP_HOOK_WRAP, nullptr, &ids[1]), POP_OK);
    MOD_CHECK_EQ(install(&g_api[0], target, match, after, POP_HOOK_AFTER, nullptr, &ids[2]),
                 POP_OK);
    for (uint32_t ret : {other, match, other, match}) {
        X86 *c = loader_context();
        loader_init_context(c);
        uint32_t sp = c->r[R_ESP] - 16;
        c->r[R_ESP] = sp;
        wr32(sp, ret);
        wr32(sp + 4, 200);
        wr32(sp + 8, 100);
        const auto snapshots = mods_view_test_push_count();
        g_log.clear();
        auto index = recomp_index_of(target);
        recomp_hook_ptrs[index](c, index);
        MOD_CHECK_EQ(c->r[R_EAX], ret == match ? 101u : 100u);
        MOD_CHECK_EQ(c->r[R_ESP], sp + 4);
        MOD_CHECK_EQ(c->eip, other);
        MOD_CHECK_EQ(mods_view_test_push_count() - snapshots, ret == match ? 3u : 0u);
        MOD_CHECK_EQ(g_log.size(), ret == match ? 4u : 0u);
        if (g_log.size() == 4) {
            MOD_CHECK_STR(g_log[0].c_str(), "before");
            MOD_CHECK_STR(g_log[1].c_str(), "wrap-in");
            MOD_CHECK_STR(g_log[2].c_str(), "wrap-out");
            MOD_CHECK_STR(g_log[3].c_str(), "after");
        }
        MOD_CHECK_EQ(mods_view_depth(), 0u);
        MOD_CHECK_EQ(mods_hook_depth(), 0u);
    }
    for (auto id : ids)
        MOD_CHECK_EQ(mods_hook_remove(0, id), POP_OK);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(target)], 0u);
}

MOD_TEST_SUITE(hook_install_eligibility) {
    reset_world();
    uint32_t id = 0;
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};

    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id), POP_OK);
    MOD_CHECK(id != 0);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);

    // All dispatchable non-entry symbols must fail eligibility. Some images
    // have no such symbols; the absent-address checks still run for them.
    uint32_t id2 = 0;
    for (uint32_t i = 0; i < recomp_func_count; ++i) {
        const uint32_t addr = recomp_func_addrs[i];
        if (!mods_symbol_hookable(addr))
            MOD_CHECK_EQ(mods_hook_install(0, addr, nop, POP_HOOK_BEFORE, nullptr, &id2),
                         POP_E_NOSYMBOL);
    }
    MOD_CHECK_EQ(mods_hook_install(0, TRAMP_BASE, nop, POP_HOOK_BEFORE, nullptr, &id2),
                 POP_E_NOSYMBOL);
    MOD_CHECK_EQ(mods_hook_install(0, 0u, nop, POP_HOOK_BEFORE, nullptr, &id2), POP_E_NOSYMBOL);

    MOD_CHECK_EQ(mods_hook_remove(0, id), POP_OK);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    uint32_t id3 = 0;
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id3), POP_OK);
    MOD_CHECK(id3 != id); // handles are never reused
    MOD_CHECK_EQ(mods_hook_remove(0, id), POP_E_NOTFOUND);

    // A seventeenth hook on one function is refused rather than resized under
    // a running invocation.
    reset_world();
    for (int i = 0; i < 16; ++i)
        MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id), POP_E_LIMIT);
}

MOD_TEST_SUITE(hook_replace_conflict_and_wrap) {
    reset_world();
    uint32_t a = 0, b = 0, w = 0;
    auto ret0 = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        api->hook_return(api, cpu, 0, 0);
    };
    MOD_CHECK_EQ(mods_hook_install(0, INNER, ret0, POP_HOOK_REPLACE, nullptr, &a), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, ret0, POP_HOOK_REPLACE, nullptr, &b), POP_E_CONFLICT);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, ret0, POP_HOOK_WRAP, nullptr, &w), POP_OK);
}

MOD_TEST_SUITE(hook_before_order_cancel_and_registers) {
    reset_world();
    // Load order decides, not registration order: mod 1 registers first and
    // would still run second. Mod 0's hook cancels, so mod 1's never runs at
    // all - which is what a cancel means, and why nothing may then read state
    // the skipped hook was supposed to set.
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     1, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("before-1-must-not-run");
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("before-0");
                         MOD_CHECK_EQ(api->mod_index, 0u); // its own API
                         MOD_CHECK_EQ(cpu->phase, (uint32_t)POP_PHASE_BEFORE);
                         MOD_CHECK_EQ(cpu->eip, INNER); // normalised entry
                         MOD_CHECK_EQ(cpu->target, INNER);
                         cpu->ebx = 0x0b0b0b0bu;                 // an edit that sticks
                         api->hook_return(api, cpu, 0x1234u, 0); // cancel
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     1, INNER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("after-1");
                         MOD_CHECK_EQ(cpu->phase, (uint32_t)POP_PHASE_AFTER);
                         MOD_CHECK_EQ(cpu->eip, RET_ADDR); // the RET already ran
                         MOD_CHECK_EQ(cpu->target, INNER);
                         MOD_CHECK_EQ(cpu->eax, 0x1234u); // the cancel's result
                         cpu->eax = 0x5678u;              // after may edit eax
                         cpu->ebx = 0xdeadbeefu;          // and this is ignored
                     },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *) {
                         note("after-0");
                         // An after hook cannot cancel, delegate or return: the guest RET
                         // has already happened.
                         MOD_CHECK_EQ(api->hook_return(api, cpu, 1, 0), POP_E_STATE);
                         MOD_CHECK_EQ(api->call_original(api, INNER, cpu), POP_E_STATE);
                         MOD_CHECK_EQ(api->call_next(api, inv, cpu), POP_E_STATE);
                     },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);

    enter(INNER);

    // Three entries: the cancelling before hook, then both after hooks in the
    // reverse of before order. The skipped hook logged nothing.
    MOD_CHECK_EQ(g_log.size(), 3u);
    MOD_CHECK_STR(g_log[0].c_str(), "before-0");
    MOD_CHECK_STR(g_log[1].c_str(), "after-1");
    MOD_CHECK_STR(g_log[2].c_str(), "after-0");

    X86 *c = loader_context();
    MOD_CHECK_EQ(c->eip, RET_ADDR);         // the cancel performed the RET
    MOD_CHECK_EQ(c->r[R_EAX], 0x5678u);     // the after hook's edit stuck
    MOD_CHECK_EQ(c->r[R_EBX], 0x0b0b0b0bu); // the before hook's edit stuck
    MOD_CHECK(c->r[R_EBX] != 0xdeadbeefu);  // the after hook's ebx did not
}

MOD_TEST_SUITE(hook_a_handle_belongs_to_the_mod_that_made_it) {
    reset_world();
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};
    const uint32_t A = MODS_OWNER_FIRST_MOD, B = MODS_OWNER_FIRST_MOD + 1;

    uint32_t mine = 0;
    MOD_CHECK_EQ(mods_hook_install(A, OUTER, nop, POP_HOOK_BEFORE, nullptr, &mine), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // Handles are small integers from one sequence, so another mod can name
    // one without ever having been given it. It is not found, because from
    // where that caller stands there is no such handle - and nothing would put
    // it back: a rollback undoes what the failing mod registered, and this
    // would be a removal of something it never registered.
    MOD_CHECK_EQ(mods_hook_remove(B, mine), POP_E_NOTFOUND);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    // The runtime's own owner has no special claim on it either.
    MOD_CHECK_EQ(mods_hook_remove(MODS_OWNER_RUNTIME, mine), POP_E_NOTFOUND);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // And a queued handle is protected the same way, before it is published.
    uint32_t queued = 0;
    std::thread([&] {
        MOD_CHECK_EQ(mods_hook_install(A, INNER, nop, POP_HOOK_BEFORE, nullptr, &queued), POP_OK);
    }).join();
    MOD_CHECK_EQ(mods_hook_remove(B, queued), POP_E_NOTFOUND);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 2u);

    // Its owner can still remove both, which is what makes the refusal a
    // question of ownership rather than of the handle being gone.
    MOD_CHECK_EQ(mods_hook_remove(A, mine), POP_OK);
    MOD_CHECK_EQ(mods_hook_remove(A, queued), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);

    // A rollback of one mod leaves another's alone, which is the property the
    // ownership check exists to keep true.
    uint32_t keep = 0, drop = 0;
    MOD_CHECK_EQ(mods_hook_install(A, OUTER, nop, POP_HOOK_BEFORE, nullptr, &keep), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(B, OUTER, nop, POP_HOOK_BEFORE, nullptr, &drop), POP_OK);
    mods_hooks_remove_all(B);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    MOD_CHECK_EQ(mods_hook_remove(A, keep), POP_OK);
}

MOD_TEST_SUITE(hook_mid_callback_install_takes_effect_next_time) {
    reset_world();
    static uint32_t added = 0;
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("first");
                         if (!added)
                             mods_hook_install(
                                 0, INNER,
                                 [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                                     note("second");
                                 },
                                 POP_HOOK_BEFORE, nullptr, &added);
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);

    enter(INNER);
    MOD_CHECK_EQ(g_log.size(), 1u); // the new hook did not join this one
    enter(INNER);
    MOD_CHECK_EQ(g_log.size(), 3u); // and runs from the next invocation
}

MOD_TEST_SUITE(hook_unwind_abandons_only_the_frames_above_it) {
    reset_world();
    static uint32_t esp_seen = 0;
    uint32_t id = 0;
    // A before hook does to this frame what a guest longjmp does: unwinds past
    // it. The invocation is abandoned there and then, so the after hook on the
    // same function never runs and the dispatcher must not pop a frame that is
    // no longer its own.
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("before");
                         esp_seen = cpu->esp;
                         MOD_CHECK_EQ(mods_hook_depth(), 1u);
                         mods_hooks_unwind_to_esp(cpu->esp + 0x100u);
                         MOD_CHECK_EQ(mods_hook_depth(), 0u); // the frame is gone
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("after-must-not-run");
                     },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);

    enter(INNER);
    MOD_CHECK(esp_seen != 0);
    MOD_CHECK_EQ(g_log.size(), 1u);
    MOD_CHECK_STR(g_log[0].c_str(), "before");
    MOD_CHECK_EQ(mods_hook_depth(), 0u);

    // An inner invocation abandoned from the inside leaves the OUTER one
    // intact: an unwind drops the frames above an ESP, not everything the
    // thread has.
    //
    // The nesting is made explicitly through framework dispatch. The outer callback
    // dispatches the inner function exactly as a generated call site does -
    // push a return address, then call through recomp_hook_ptrs - so the inner
    // hook is guaranteed to run and the two frames' ESPs differ by exactly the
    // one dword that was pushed.
    reset_world();
    static uint32_t outer_esp = 0;
    static uint32_t depth_in_inner = 0;
    static uint32_t depth_after_inner = 0;
    static uint32_t views_before = 0, views_after = 0;
    static bool view_still_active = false;
    MOD_CHECK_EQ(mods_hook_install(
                     0, OUTER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("outer-in");
                         outer_esp = cpu->esp;
                         MOD_CHECK_EQ(mods_hook_depth(), 1u);
                         // The outer callback scope must survive an inner unwind.
                         views_before = mods_view_depth();
                         MOD_CHECK(mods_view_active());
                         X86 *c = loader_context();
                         c->r[R_ESP] -= 4;
                         wr32(c->r[R_ESP], RET_ADDR);
                         dispatch_test(c, INNER);
                         depth_after_inner = mods_hook_depth();
                         views_after = mods_view_depth();
                         view_still_active = mods_view_active();
                         note("outer-out");
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("inner");
                         depth_in_inner = mods_hook_depth();
                         // Everything strictly above the outer frame's entry ESP, which is
                         // this frame and nothing else.
                         mods_hooks_unwind_to_esp(outer_esp);
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);

    enter(OUTER);
    MOD_CHECK_EQ(g_log.size(), 3u);
    MOD_CHECK_STR(g_log[0].c_str(), "outer-in");
    MOD_CHECK_STR(g_log[1].c_str(), "inner");
    MOD_CHECK_STR(g_log[2].c_str(), "outer-out");
    // Two frames while the inner one ran, one once it had abandoned itself,
    // and none by the time the outer invocation finished.
    MOD_CHECK_EQ(depth_in_inner, 2u);
    MOD_CHECK_EQ(depth_after_inner, 1u);
    MOD_CHECK_EQ(mods_hook_depth(), 0u);
    // The outer callback scope survives the inner unwind at the same depth.
    MOD_CHECK_EQ(views_after, views_before);
    MOD_CHECK(view_still_active);
}

MOD_TEST_SUITE(hook_bounds_every_cpu_transfer) {
    reset_world();
    // A plugin built against an older, shorter pop_cpu_v1. The host serves only
    // the fields it compiled against and never writes past them; the buffer is
    // real, and the bytes past the declared size are poisoned, so an over-long
    // write fails the test rather than escaping it.
    static uint8_t buffer[sizeof(pop_cpu_v1) + 16];
    static uint32_t declared = POP_CPU_V1_MIN_SIZE;
    mods_hooks_set_cpu_size(2, declared);

    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     2, INNER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         MOD_CHECK_EQ(cpu->size, declared);
                         MOD_CHECK_EQ(cpu->eax, 0xfeedfaceu);
                         const uint8_t *raw = (const uint8_t *)cpu;
                         for (uint32_t i = declared; i < sizeof buffer; ++i)
                             MOD_CHECK_EQ(raw[i], 0xa5);
                         note("truncated");
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);

    memset(buffer, 0xa5, sizeof buffer);
    mods_hooks_set_test_cpu_buffer(buffer, sizeof buffer);
    X86 *c = loader_context();
    loader_init_context(c);
    c->r[R_EAX] = 0xfeedfaceu;
    c->r[R_ESP] -= 4;
    wr32(c->r[R_ESP], RET_ADDR);
    dispatch_test(c, INNER);
    MOD_CHECK_EQ(g_log.size(), 1u);

    // Storage smaller than the declared size bounds the transfer too. Three
    // numbers govern it - the host's layout, the plugin's declaration and the
    // bytes actually available - and the smallest of them wins.
    reset_world();
    static uint8_t small[POP_CPU_V1_MIN_SIZE];
    memset(small, 0xa5, sizeof small);
    mods_hooks_set_test_cpu_buffer(small, sizeof small);
    mods_hooks_set_cpu_size(2, POP_CPU_V1_BASELINE_SIZE); // the plugin is new
    uint32_t id2 = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     2, INNER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         MOD_CHECK_EQ(cpu->size, (uint32_t)sizeof small);
                         note("small");
                     },
                     POP_HOOK_BEFORE, nullptr, &id2),
                 POP_OK);
    loader_init_context(c);
    c->r[R_ESP] -= 4;
    wr32(c->r[R_ESP], RET_ADDR);
    dispatch_test(c, INNER);
    MOD_CHECK_EQ(g_log.size(), 1u);
    MOD_CHECK_STR(g_log[0].c_str(), "small");
    mods_hooks_set_test_cpu_buffer(nullptr, 0);
}

MOD_TEST_SUITE(hook_attribution_and_rollback) {
    reset_world();
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         // A crash here names this hook and its owner, from a preformatted
                         // string a fault handler can print without allocating.
                         const char *d = mods_active_callback_desc();
                         char addr[16];
                         snprintf(addr, sizeof addr, "%08x", INNER);
                         MOD_CHECK(strstr(d, addr) != nullptr);
                         MOD_CHECK(strstr(d, "before") != nullptr);
                         // The mod's ID, not just an index: a crash report saying "mod 0"
                         // names nothing anyone can act on.
                         MOD_CHECK(strstr(d, "test.mod") != nullptr);
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    enter(INNER);
    MOD_CHECK_STR(mods_active_callback_desc(), ""); // nothing active now

    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    mods_hooks_remove_all(0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 0);
    // A reset returns the counter to zero as well, so a later suite starts
    // from a clean registry.
    mods_hooks_reset();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);
}

MOD_TEST_SUITE(hook_mutation_off_the_baton_is_validated_then_queued) {
    reset_world();
    uint32_t id = 0, replaced = 0, extra = 0;
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) { note("queued"); };
    auto ret0 = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        api->hook_return(api, cpu, 0, 0);
    };
    MOD_CHECK_EQ(mods_hook_install(0, INNER, ret0, POP_HOOK_REPLACE, nullptr, &replaced), POP_OK);

    PopModStatus queued = POP_OK, conflict = POP_OK, removed = POP_OK, absent = POP_OK;
    std::thread([&] {
        // No baton here, so the mutation is queued - but it is VALIDATED now.
        queued = mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id);
        conflict = mods_hook_install(0, INNER, ret0, POP_HOOK_REPLACE, nullptr, &extra);
        removed = mods_hook_remove(0, replaced);
        absent = mods_hook_remove(0, 0xbeefu);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    MOD_CHECK_EQ(conflict, POP_E_CONFLICT); // not discovered later at the pump
    MOD_CHECK_EQ(removed, POP_OK);
    MOD_CHECK_EQ(absent, POP_E_NOTFOUND);
    MOD_CHECK(id != 0);

    // Nothing published yet: the tables belong to whoever holds the baton.
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 1);
    // A pump from a thread with no baton does nothing either.
    std::thread([] { mods_registry_pump(); }).join();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    // The test drives the pump the way the scheduler checkpoint does, from a
    // thread that holds the baton.
    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
}

MOD_TEST_SUITE(hook_wrap_wraps_an_existing_replacement) {
    reset_world();
    uint32_t id = 0;
    auto inner = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        note("inner");
        api->hook_return(api, cpu, 7u, 0);
    };
    auto outer = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *) {
        note("outer");
        api->call_next(api, inv, cpu);
    };
    MOD_CHECK_EQ(mods_hook_install(0, INNER, inner, POP_HOOK_REPLACE, nullptr, &id), POP_OK);
    // wrap is its own mode all the way through validation: converting it to
    // replace first would refuse exactly the case it exists for.
    MOD_CHECK_EQ(mods_hook_install(0, INNER, outer, POP_HOOK_WRAP, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, inner, POP_HOOK_REPLACE, nullptr, &id),
                 POP_E_CONFLICT);
    enter(INNER);
    MOD_CHECK_EQ(g_log.size(), 2u);
    MOD_CHECK_STR(g_log[0].c_str(), "outer");
    MOD_CHECK_STR(g_log[1].c_str(), "inner");
    MOD_CHECK_EQ(loader_context()->r[R_EAX], 7u);
}

// ---------------------------------------------------------------------------
// The queue is part of the registry, not a separate world. A conflict or a
// limit has to be reported at the moment it is asked even when neither the new
// install nor the one it collides with has been applied yet: a status returned
// now and a failure discovered at the pump is a lie the caller cannot act on.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_queued_installs_are_registry_state) {
    reset_world();
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};
    uint32_t a = 0, b = 0;
    PopModStatus first = POP_OK, second = POP_OK;

    // Two replacements queued from a thread with no baton: the second must be
    // refused now, not accepted now and discarded later.
    std::thread([&] {
        first = mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &a);
        second = mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &b);
    }).join();
    MOD_CHECK_EQ(first, POP_OK);
    MOD_CHECK_EQ(second, POP_E_CONFLICT);

    // An inline replacement competing with the queued one is refused too: the
    // queued install reserved the chain when it was accepted.
    uint32_t c = 0;
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &c), POP_E_CONFLICT);

    // Sixteen queued before-hooks fill the chain; the seventeenth is refused
    // while all sixteen are still only queued.
    reset_world();
    PopModStatus last = POP_OK;
    std::thread([&] {
        uint32_t id = 0;
        for (int i = 0; i < 16; ++i)
            if (mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id) != POP_OK)
                last = POP_E_STATE; // an early refusal is itself a failure
        last = mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id);
    }).join();
    MOD_CHECK_EQ(last, POP_E_LIMIT);

    // And everything accepted really does apply, so the reservation was not
    // simply a refusal in disguise.
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 16u);
}

// ---------------------------------------------------------------------------
// Removing a queued install cancels it. Reporting success and then publishing
// it anyway is the worst of both answers.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_removing_a_queued_install_cancels_it) {
    reset_world();
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};
    uint32_t id = 0;
    PopModStatus queued = POP_OK;
    std::thread([&] {
        queued = mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    MOD_CHECK(id != 0);

    MOD_CHECK_EQ(mods_hook_remove(0, id), POP_OK);
    // The handle is gone the moment it is cancelled: a second removal has
    // nothing to find.
    MOD_CHECK_EQ(mods_hook_remove(0, id), POP_E_NOTFOUND);

    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);
}

// ---------------------------------------------------------------------------
// A bulk mutation publishes exactly as a single one does, so it waits for the
// baton in exactly the same way.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_bulk_mutations_wait_for_the_baton) {
    reset_world();
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);

    // Off the baton, remove_all changes nothing that is published.
    std::thread([] { mods_hooks_remove_all(0); }).join();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);

    // The same for a reset.
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    std::thread([] { mods_hooks_reset(); }).join();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);
}

// ---------------------------------------------------------------------------
// An inner replacement's edits to the result reach the outer callback and the
// after hooks. Delegation is not a one-way door.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_inner_results_reach_the_outer_view) {
    reset_world();
    uint32_t id = 0;
    auto inner = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        note("inner");
        api->hook_return(api, cpu, 0x1111u, 0);
        cpu->eax = 0x2222u; // edited AFTER returning
    };
    auto outer = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *) {
        note("outer");
        MOD_CHECK_EQ(api->call_next(api, inv, cpu), POP_OK);
        // The inner hook's later edit is what the outer callback sees.
        MOD_CHECK_EQ(cpu->eax, 0x2222u);
        cpu->eax = 0x3333u; // and the outer may edit it again
    };
    MOD_CHECK_EQ(mods_hook_install(0, INNER, inner, POP_HOOK_REPLACE, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, outer, POP_HOOK_WRAP, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("after");
                         MOD_CHECK_EQ(cpu->eax, 0x3333u);
                     },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);

    enter(INNER);
    MOD_CHECK_EQ(g_log.size(), 3u);
    MOD_CHECK_EQ(loader_context()->r[R_EAX], 0x3333u);
}

// ---------------------------------------------------------------------------
// A wrap chain abandoned from the inside runs nothing more of that invocation,
// and attribution names a callback that is still on the stack rather than the
// one that vanished.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_unwind_through_a_wrap_stops_every_later_phase) {
    reset_world();
    uint32_t id = 0;
    auto inner = [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
        note("inner");
        // Abandon this invocation from inside the innermost callback.
        mods_hooks_unwind_to_esp(cpu->esp + 0x100u);
    };
    auto outer = [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *) {
        note("outer-in");
        api->call_next(api, inv, cpu);
        note("outer-out");
    };
    MOD_CHECK_EQ(mods_hook_install(0, INNER, inner, POP_HOOK_REPLACE, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, outer, POP_HOOK_WRAP, nullptr, &id), POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("after-must-not-run");
                     },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);

    enter(INNER);
    // The outer callback still returns - its own C frame is real - but no
    // later phase of the invocation runs.
    MOD_CHECK_EQ(g_log.size(), 3u);
    MOD_CHECK_STR(g_log[0].c_str(), "outer-in");
    MOD_CHECK_STR(g_log[1].c_str(), "inner");
    MOD_CHECK_STR(g_log[2].c_str(), "outer-out");
    MOD_CHECK_EQ(mods_hook_depth(), 0u);
    // Nothing is running now, so nothing is attributed.
    MOD_CHECK_STR(mods_active_callback_desc(), "");
}

// ---------------------------------------------------------------------------
// After a partial unwind, attribution names the callback that survived. A
// crash report that blames a hook which is no longer on the stack sends
// somebody to the wrong mod.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_attribution_survives_a_partial_unwind) {
    reset_world();
    static std::string seen_in_unwind;
    static bool inner_ran = false;
    static uint32_t unwind_esp = 0;
    uint32_t id = 0;
    // The same explicit nesting the unwind test uses, so both callbacks are
    // guaranteed to run rather than left to the game's control flow.
    MOD_CHECK_EQ(mods_hook_install(
                     0, OUTER,
                     [](const PopModApi *, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         note("outer");
                         unwind_esp = cpu->esp;
                         X86 *c = loader_context();
                         c->r[R_ESP] -= 4;
                         wr32(c->r[R_ESP], RET_ADDR);
                         dispatch_test(c, INNER);
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("inner");
                         inner_ran = true;
                         mods_hooks_unwind_to_esp(unwind_esp);
                         // Sampled HERE: inside the unwind path, before run_one's normal
                         // restoration would put the outer description back anyway. A real
                         // guest longjmp never reaches that restoration, so this is the
                         // only point at which the unwind's own answer can be seen.
                         seen_in_unwind = mods_active_callback_desc();
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);

    enter(OUTER);
    MOD_CHECK(inner_ran);
    MOD_CHECK_EQ(g_log.size(), 2u);
    MOD_CHECK_STR(g_log[0].c_str(), "outer");
    MOD_CHECK_STR(g_log[1].c_str(), "inner");
    // The surviving frame's hook is named; the abandoned one is not.
    MOD_CHECK(seen_in_unwind.find("004ec6f0") != std::string::npos);
    MOD_CHECK(seen_in_unwind.find("0040c690") == std::string::npos);
}

// ---------------------------------------------------------------------------
// Acceptance order is application order. A status returned now must not be
// contradicted at the pump, in either direction: an inline mutation applies
// everything already queued before it validates, so it never validates against
// a registry the queue has not reached yet.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_inline_and_queued_agree_in_both_orders) {
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};

    // Queued REPLACE, then inline WRAP. Both are legal - a wrap may wrap an
    // existing replacement - and both must still be there after the pump.
    reset_world();
    uint32_t qid = 0, iid = 0;
    PopModStatus queued = POP_OK, inline_st = POP_OK;
    std::thread([&] {
        queued = mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &qid);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    inline_st = mods_hook_install(0, INNER, nop, POP_HOOK_WRAP, nullptr, &iid);
    MOD_CHECK_EQ(inline_st, POP_OK);
    pump_on_guest_thread();
    // Neither was silently dropped: two hooks on one entry.
    MOD_CHECK_EQ(mods_hooks_installed_count(), 2u);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 1);

    // Queued WRAP, then inline REPLACE. The wrap is applied first, so the
    // replace chain is no longer empty and the plain replacement must be
    // refused NOW rather than accepted and dropped later.
    reset_world();
    std::thread([&] {
        queued = mods_hook_install(0, INNER, nop, POP_HOOK_WRAP, nullptr, &qid);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &iid), POP_E_CONFLICT);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // Inline REPLACE, then queued WRAP: the mirror image, and both survive.
    reset_world();
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &iid), POP_OK);
    std::thread([&] {
        queued = mods_hook_install(0, INNER, nop, POP_HOOK_WRAP, nullptr, &qid);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 2u);

    // Inline REPLACE, then queued REPLACE: refused at the moment it is asked.
    reset_world();
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &iid), POP_OK);
    std::thread([&] {
        queued = mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &qid);
    }).join();
    MOD_CHECK_EQ(queued, POP_E_CONFLICT);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
}

// ---------------------------------------------------------------------------
// Queued work belongs to the scheduler's checkpoint and to nothing else. An
// inline call may get in line behind it; it may not publish it as a side
// effect of doing something unrelated.
// ---------------------------------------------------------------------------
MOD_TEST_SUITE(hook_validation_sees_queued_removals_too) {
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};

    // A committed replacement with its removal already queued is not a
    // conflict. The checkpoint frees the chain before it reaches the install,
    // so refusing here would be a lie in the opposite direction from the one
    // counting only queued installs was written to prevent.
    reset_world();
    uint32_t first = 0, second = 0;
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &first), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    std::thread([&] { MOD_CHECK_EQ(mods_hook_remove(0, first), POP_OK); }).join();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u); // queued, not applied
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &second), POP_OK);
    // Queued behind the removal, and the pair leaves exactly one replacement.
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 1);
    // And it is the second one that is there: removing it empties the chain.
    MOD_CHECK_EQ(mods_hook_remove(0, second), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);

    // A full chain with a removal queued against it has room for one more.
    reset_world();
    uint32_t ids[16] = {0};
    for (int i = 0; i < 16; ++i)
        MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &ids[i]), POP_OK);
    uint32_t over = 0;
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &over), POP_E_LIMIT);
    std::thread([&] { MOD_CHECK_EQ(mods_hook_remove(0, ids[0]), POP_OK); }).join();
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &over), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 16u); // neither applied yet
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 16u); // one out, one in

    // A handle whose removal is already queued is no longer a handle. Both
    // calls returning POP_OK made the second one a no-op at the checkpoint,
    // which is the failure a synchronous status exists to rule out.
    reset_world();
    uint32_t once = 0;
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &once), POP_OK);
    std::thread([&] { MOD_CHECK_EQ(mods_hook_remove(0, once), POP_OK); }).join();
    MOD_CHECK_EQ(mods_hook_remove(0, once), POP_E_NOTFOUND);
    // Queued twice is the same question asked from the other side.
    std::thread([&] { MOD_CHECK_EQ(mods_hook_remove(0, once), POP_E_NOTFOUND); }).join();
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);

    // A queued reset empties the registry, so an install asked for afterwards
    // is checked against nothing and applies after it.
    reset_world();
    uint32_t taken = 0, after_reset = 0;
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &taken), POP_OK);
    std::thread([] { mods_hooks_reset(); }).join();
    // Without the reset in view this would be POP_E_CONFLICT.
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &after_reset), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u); // still the first
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u); // now only the second
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 1);
    MOD_CHECK_EQ(mods_hook_remove(0, taken), POP_E_NOTFOUND);
    MOD_CHECK_EQ(mods_hook_remove(0, after_reset), POP_OK);
}

MOD_TEST_SUITE(hook_an_inline_call_never_publishes_the_queue) {
    auto nop = [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {};

    // A queued reset must not erase an inline install that was made before it,
    // and must not fire during an unrelated inline call afterwards.
    reset_world();
    uint32_t a = 0, b = 0;
    MOD_CHECK_EQ(mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &a), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    std::thread([] { mods_hooks_reset(); }).join(); // queued, not applied
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // An unrelated inline install: it is queued behind the reset, and the
    // reset still has not run.
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_BEFORE, nullptr, &b), POP_OK);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 0);

    // Only at the checkpoint does any of it happen, and then in order: the
    // reset erases the first install, then the second one applies.
    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 1);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // A failed inline mutation changes nothing at all, including the queue.
    reset_world();
    uint32_t q = 0, bad = 0;
    PopModStatus queued = POP_OK;
    std::thread([&] {
        queued = mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &q);
    }).join();
    MOD_CHECK_EQ(queued, POP_OK);
    // Refused, because the queued replacement already reserved the chain.
    MOD_CHECK_EQ(mods_hook_install(0, INNER, nop, POP_HOOK_REPLACE, nullptr, &bad), POP_E_CONFLICT);
    // The refusal published nothing: the queued install is still queued.
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(INNER)], 0);
    MOD_CHECK_EQ(mods_hooks_installed_count(), 0u);
    pump_on_guest_thread();
    MOD_CHECK_EQ(mods_hooks_installed_count(), 1u);

    // A removal that finds nothing is equally inert.
    reset_world();
    std::thread([&] {
        queued = mods_hook_install(0, OUTER, nop, POP_HOOK_BEFORE, nullptr, &q);
    }).join();
    MOD_CHECK_EQ(mods_hook_remove(0, 0xbeefu), POP_E_NOTFOUND);
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 0); // still queued
    pump_on_guest_thread();
    MOD_CHECK_EQ(recomp_hooked[recomp_index_of(OUTER)], 1);
}

// ---------------------------------------------------------------------------
// Queued input waits for a mod callback to return.
//
// A hook that delegates into the original can end up inside a blocking guest
// wait with its own frame still live, and that is where the scheduler parks
// and drains queued input. Dispatching input callbacks there runs one mod's
// callback inside another's, with the outer one holding a game view it took
// before any of it happened. The drain is gated on this thread's hook depth
// for that reason.
// ---------------------------------------------------------------------------
static volatile int g_hd_queued = 0;
static volatile int g_hd_drained = 0;
static volatile int g_hd_drained_inside = 0;
static volatile int g_hd_depth_at_drain = -1;
static uint32_t g_hd_event = 0;

static bool hd_pending() {
    return g_hd_queued > g_hd_drained;
}
static void hd_drain() {
    g_hd_depth_at_drain = (int)mods_hook_depth();
    ++g_hd_drained;
    win32_signal_event(g_hd_event, false);
}

MOD_TEST_SUITE(hook_queued_input_waits_for_the_callback_to_return) {
    reset_world();
    g_hd_queued = 0;
    g_hd_drained = 0;
    g_hd_drained_inside = 0;
    g_hd_depth_at_drain = -1;
    sched_set_input_queue(hd_pending, hd_drain);

    X86 *c = loader_context();
    g_hd_event = mod_test_call_import(c, "KERNEL32.dll", "CreateEventA", {0, 1, 0, 0});
    MOD_CHECK(g_hd_event != 0);

    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) {
                         note("in-callback");
                         // Input is queued while this callback is on the stack, and this
                         // thread then blocks. Nothing else is runnable, so this is exactly
                         // the park that drains - and it must not, because a mod callback
                         // is live right here.
                         g_hd_queued = 1;
                         sched_input_arrived();
                         X86 *cc = loader_context();
                         mod_test_call_import(cc, "KERNEL32.dll", "WaitForSingleObject",
                                              {g_hd_event, 120});
                         g_hd_drained_inside = g_hd_drained;
                         note("out-callback");
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);

    enter(INNER);

    MOD_CHECK_EQ(g_log.size(), 2u);
    MOD_CHECK_STR(g_log[0].c_str(), "in-callback");
    MOD_CHECK_STR(g_log[1].c_str(), "out-callback");
    // Nothing was drained while the callback was on the stack, so the wait
    // inside it timed out rather than being satisfied by an input callback.
    MOD_CHECK_EQ(g_hd_drained_inside, 0);
    // And it is not that the drain is broken: once the callback has returned
    // and the stack is clear, the same queued input is drained.
    for (int i = 0; i < 200 && g_hd_drained == 0; ++i)
        mod_test_call_import(c, "KERNEL32.dll", "WaitForSingleObject", {g_hd_event, 5});
    MOD_CHECK_EQ(g_hd_drained, 1);
    MOD_CHECK_EQ(g_hd_depth_at_drain, 0); // and only at depth zero

    sched_set_input_queue(nullptr, nullptr);
    mod_test_call_import(c, "KERNEL32.dll", "CloseHandle", {g_hd_event});
}

#include "../display_settings.h"
#include "../../dx/host_api.h"
MOD_TEST_SUITE(wide_view_keeps_resolution_callbacks_active) {
    reset_world();
    mods_host_set_main_thread();
    mods_display_reset();
    mods_display_init();
    g_api[2].mod_id = "core.display";
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     0, INNER,
                     [](const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *, void *) {
                         api->hook_return(api, cpu, 1, 0);
                     },
                     POP_HOOK_BEFORE, nullptr, &id),
                 POP_OK);
    static int calls = 0;
    calls = 0;
    MOD_CHECK_EQ(mods_hook_install(
                     2, INNER,
                     [](const PopModApi *, pop_cpu_v1 *, PopHookInvocation *, void *) { ++calls; },
                     POP_HOOK_AFTER, nullptr, &id),
                 POP_OK);
    mods_display_transition(1, HOST_SCREEN_GAMEPLAY);
    MOD_CHECK_EQ(mods_display_set(DISPLAY_WIDE, 0), POP_OK);
    enter(INNER);
    MOD_CHECK_EQ(calls, 1); // queued, still enabled
    mods_display_transition(2, HOST_SCREEN_GAMEPLAY);
    enter(INNER);
    MOD_CHECK_EQ(calls, 2); // sky/resolution correction remains active
    MOD_CHECK_EQ(mods_display_set(DISPLAY_WIDE, 1), POP_OK);
    mods_display_transition(3, HOST_SCREEN_GAMEPLAY);
    enter(INNER);
    MOD_CHECK_EQ(calls, 3); // no reload or lost callback
    mods_hooks_reset();
    mods_display_reset();
}
