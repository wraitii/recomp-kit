/* entry_hook.c - installs a hook during pop_mod_init on the first eligible
 * symbol in the test's symbol catalog.
 *
 * The point of this fixture is timing, not behaviour: the hook is installed
 * while the loader is running, which is before any guest thread exists, so it
 * is queued rather than applied. If the loader does not publish the queue
 * before the entry point is called, this counter stays at zero for ever. */
#include "pop_mod_api.h"
#include "hook_first_entry.h"

POP_MOD_DECLARE_ABI();

unsigned g_entry_hook_calls;
int g_entry_hook_install_status = 99;

static void before(const PopModApi *api, pop_cpu_v1 *cpu, PopHookInvocation *inv, void *user) {
    (void)inv;
    (void)user;
    ++g_entry_hook_calls;
    /* Cancel, so the entry point itself does not run. The subject here is
     * whether the hook is live by the time the entry symbol is called, not
     * what the entry symbol does: letting the real one run would start the
     * whole game and end the test process at its ExitProcess. */
    api->hook_return(api, cpu, 0, 0);
}

PopModStatus pop_mod_init(const PopModApi *api) {
    uint32_t addr = 0, id = 0;
    g_entry_hook_install_status =
        fixture_hook_first_entry(api, before, POP_HOOK_BEFORE, &addr, &id);
    return (PopModStatus)g_entry_hook_install_status;
}
