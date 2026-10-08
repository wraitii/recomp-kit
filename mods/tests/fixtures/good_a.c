/* good_a.c - a before hook that counts calls without running guest code. */
#include "pop_mod_api.h"
#include "hook_first_entry.h"

POP_MOD_DECLARE_ABI();

unsigned g_good_a_calls;
static uint32_t g_hook_id;

static void before(const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *user) {
    (void)inv;
    (void)user;
    ++g_good_a_calls;
    api->hook_return(api, cpu, 0, 0);
}

PopModStatus pop_mod_init(const PopModApi *api) {
    uint32_t addr = 0;
    return fixture_hook_first_entry(api, before, POP_HOOK_BEFORE, &addr, &g_hook_id);
}

PopModStatus pop_mod_exit(void) {
    return POP_OK; /* the host reclaims what this mod registered */
}
