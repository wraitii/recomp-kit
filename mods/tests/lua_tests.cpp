// lua_tests.cpp - the Lua core plugin: what a script can read, what it cannot
// reach, and what happens when one of its callbacks throws.
#include "mods_tests.h"
#include "../mods_internal.h"
#include "../../runtime/loader.h"
#include "../../runtime/memory.h"
#include "../../runtime/win32.h"

namespace {
const uint32_t OWNER = MODS_OWNER_FIRST_MOD;
const uint32_t OWNER2 = MODS_OWNER_FIRST_MOD + 1;
PopModApi g_api, g_api2;
// One counter per owner, so "it went through the right API instance" is a
// measurement and not an assumption.
int g_log_calls[8];
int g_get_calls[8];
int g_set_calls[8];
} // namespace

// Declared before setup(), which installs them. Task 10 owns the loader's real
// per-mod contexts; this suite installs its own through the one seam and never
// defines mods_api_for, of which there is exactly one definition in the tree.
static const PopModApi *test_provider(uint32_t owner) {
    if (owner == OWNER)
        return &g_api;
    if (owner == OWNER2)
        return &g_api2;
    return nullptr;
}

// Task 10 fills log and the two settings pointers on the real contexts.
// Nothing fills them in this binary, so the provider fills them itself - which
// is also what makes the forwarding countable.
static PopModStatus test_log(const PopModApi *api, const char *text) {
    g_log_calls[api->mod_index & 7]++;
    LOGW("mods: [%s] %s", api->mod_id ? api->mod_id : "?", text);
    return (PopModStatus)POP_OK;
}
static PopModStatus test_settings_get(const PopModApi *api, const char *key, int64_t *out) {
    g_get_calls[api->mod_index & 7]++;
    return mods_settings_get(api->mod_index, key, out);
}
static PopModStatus test_settings_set(const PopModApi *api, const char *key, int64_t v) {
    g_set_calls[api->mod_index & 7]++;
    return mods_settings_set(api->mod_index, key, v);
}

namespace {

void fill_api(PopModApi &a, uint32_t owner, const char *id) {
    memset(&a, 0, sizeof a);
    a.version = POP_MOD_API_VERSION;
    a.size = (uint32_t)sizeof(PopModApi);
    a.mod_index = owner;
    a.mod_id = id;
    mods_fill_hooks_api(&a);
    mods_fill_events_api(&a);
    mods_fill_memory_api(&a);
    a.log = test_log;
    a.settings_get = test_settings_get;
    a.settings_set = test_settings_set;
    mods_hooks_set_load_order(owner, 2);
    mods_hooks_set_cpu_size(owner, POP_CPU_V1_BASELINE_SIZE);
}

void setup() {
    // This thread drives translated code directly, so it says it is the run
    // thread, as run_entry would.
    sched_set_guest_thread(true);
    mem_init();
    loader_load(nullptr);
    MOD_CHECK(mods_symbols_load(nullptr));
    mods_hooks_reset();
    mods_events_reset();
    mods_settings_reset();
    memset(g_log_calls, 0, sizeof g_log_calls);
    memset(g_get_calls, 0, sizeof g_get_calls);
    memset(g_set_calls, 0, sizeof g_set_calls);
    mods_set_context_provider(test_provider);
    fill_api(g_api, OWNER, "test.lua");
    fill_api(g_api2, OWNER2, "test.lua.two");
    MOD_CHECK(mods_events_init());
    MOD_CHECK(mods_lua_core_init());
    MOD_CHECK(mods_lua_available());
}

void run_turn() {
    mods_events_test_fire(1, POP_EVENT_BEFORE);
    mods_events_test_fire(1, POP_EVENT_AFTER);
}

} // namespace

MOD_TEST_SUITE(lua_event_callback_runs) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));
    run_turn();

    int64_t seen = -1;
    MOD_CHECK(mods_lua_global_int(OWNER, "seen_turns", &seen));
    MOD_CHECK_EQ(seen, 1);
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
}

MOD_TEST_SUITE(lua_error_disables_one_callback_not_the_mod) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/bad.lua"));
    run_turn();
    run_turn();
    MOD_CHECK_EQ(mods_lua_errors(), 1u); // errored once, then disabled
    int64_t survived = 0;
    MOD_CHECK(mods_lua_global_int(OWNER, "survived", &survived));
    MOD_CHECK_EQ(survived, 2); // the other callback kept running
}

MOD_TEST_SUITE(lua_bindings_are_curated) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.guest_read_u32)"), "function");
    // No guest-memory write access at any name, no hooks, no allocation.
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.guest_write_u32)"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.guest_alloc)"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.hook_install)"), "nil");
    // And no file system or arbitrary loading.
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(io)"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(os.execute)"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(package)"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(loadfile)"), "nil");
}

MOD_TEST_SUITE(lua_dropping_a_mod_unsubscribes_first) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));
    run_turn();
    int64_t before = 0;
    MOD_CHECK(mods_lua_global_int(OWNER, "seen_turns", &before));
    // Dropping the mod removes its subscriptions BEFORE its interpreter goes
    // away, so a later turn cannot reach a freed lua_State.
    mods_lua_drop_mod(OWNER);
    run_turn();
    MOD_CHECK(!mods_lua_global_int(OWNER, "seen_turns", &before));
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
}

// The four suites above are the brief's. This one covers the rest of the
// curated surface, which would otherwise ship untested: a binding that threw
// or returned nil for everything would pass every test above.
MOD_TEST_SUITE(lua_the_rest_of_the_surface_answers) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));

    // Reads and symbols work outside a callback: they are not snapshots.
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.symbol('no.such.symbol'))"), "nil");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.guest_read_u32(0xffffff00))"),
                  "nil"); // out of range, not a crash
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.guest_read_u8(0x1000))"), "number");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return type(pop.guest_read_u16(0x1000))"), "number");

    // The settings round trip, through the same owner the script runs as.
    mods_settings_declare(OWNER, "test.lua", "speed", "Speed", POP_SETTING_INT, 3, 0, 10);
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_get('speed'))"), "3");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_set('speed', 7))"), "true");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_get('speed'))"), "7");
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.mod_id())"), "test.lua");

    // A callback can use its own API instance and settings from the event.
    MOD_CHECK_STR(mods_lua_eval(OWNER, "pop.on_turn('after', function()\n"
                                       "  probe_id = pop.mod_id()\n"
                                       "  probe_speed = pop.settings_get('speed')\n"
                                       "end)\n"
                                       "return 'ok'\n"),
                  "ok");
    run_turn();

    int64_t v = -1;
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(probe_id)"), "test.lua");
    MOD_CHECK(mods_lua_global_int(OWNER, "probe_speed", &v));
    MOD_CHECK_EQ(v, 7);
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
}

// A registration made on a coroutine must not tie the callback to that
// coroutine. The registry reference retains the function; it does not retain
// the thread the function was created on, so a collected coroutine would leave
// the callback pointing at freed memory.
MOD_TEST_SUITE(lua_a_registration_from_a_coroutine_survives_collection) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));
    MOD_CHECK_STR(mods_lua_eval(OWNER,
                                "co_seen = 0\n"
                                "local f = coroutine.wrap(function()\n"
                                "  pop.on_turn('after', function() co_seen = co_seen + 1 end)\n"
                                "end)\n"
                                "f()\n"
                                "f = nil\n"
                                "collectgarbage('collect')\n"
                                "collectgarbage('collect')\n"
                                "return 'ok'\n"),
                  "ok");
    run_turn();
    int64_t seen = -1;
    MOD_CHECK(mods_lua_global_int(OWNER, "co_seen", &seen));
    MOD_CHECK_EQ(seen, 1);
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
}

// lua_close runs __gc finalizers, and it runs them after the mod's
// subscriptions have been removed. A finalizer that registers or writes would
// undo a teardown that had already finished, so both are refused while a mod
// is closing.
MOD_TEST_SUITE(lua_a_finalizer_cannot_change_anything_during_close) {
    setup();
    mods_settings_declare(OWNER, "test.lua", "gc_probe", "Probe", POP_SETTING_INT, 7, 0, 100);
    // The script installs the finalizer and then fails, which is the loader's
    // rollback path: a mod whose script threw still has to be torn down.
    const char *err = mods_lua_run_script(OWNER, "mods/tests/fixtures/finalizer.lua");
    MOD_CHECK(err != nullptr);
    int64_t v = -1;
    MOD_CHECK_EQ(mods_settings_get(OWNER, "gc_probe", &v), POP_OK);
    MOD_CHECK_EQ(v, 7);

    mods_lua_drop_mod(OWNER); // the finalizer runs inside this

    // Refused: the setting is untouched, nothing is subscribed in the mod's
    // name, and the turn that follows reaches no closed interpreter.
    v = -1;
    MOD_CHECK_EQ(mods_settings_get(OWNER, "gc_probe", &v), POP_OK);
    MOD_CHECK_EQ(v, 7);
    run_turn();
    run_turn();
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
    MOD_CHECK(!mods_lua_global_int(OWNER, "sentinel", &v));
}

// Every binding acts through the API instance its own mod was handed.
MOD_TEST_SUITE(lua_acts_through_its_own_api) {
    setup();
    MOD_CHECK(!mods_lua_run_script(OWNER, "mods/tests/fixtures/good.lua"));
    MOD_CHECK(!mods_lua_run_script(OWNER2, "mods/tests/fixtures/good.lua"));
    // good.lua logs once as it loads, through whichever api it was given.
    MOD_CHECK_EQ(g_log_calls[OWNER], 1);
    MOD_CHECK_EQ(g_log_calls[OWNER2], 1);
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return pop.mod_id()"), "test.lua");
    MOD_CHECK_STR(mods_lua_eval(OWNER2, "return pop.mod_id()"), "test.lua.two");

    // The same key, declared by both mods with different values: a read from
    // one interpreter must not see the other's.
    mods_settings_declare(OWNER, "test.lua", "speed", "Speed", POP_SETTING_INT, 3, 0, 10);
    mods_settings_declare(OWNER2, "test.lua.two", "speed", "Speed", POP_SETTING_INT, 9, 0, 10);
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_get('speed'))"), "3");
    MOD_CHECK_STR(mods_lua_eval(OWNER2, "return tostring(pop.settings_get('speed'))"), "9");
    MOD_CHECK_EQ(g_get_calls[OWNER], 1);
    MOD_CHECK_EQ(g_get_calls[OWNER2], 1);

    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_set('speed', 5))"), "true");
    MOD_CHECK_EQ(g_set_calls[OWNER], 1);
    MOD_CHECK_EQ(g_set_calls[OWNER2], 0);
    MOD_CHECK_STR(mods_lua_eval(OWNER, "return tostring(pop.settings_get('speed'))"), "5");
    MOD_CHECK_STR(mods_lua_eval(OWNER2, "return tostring(pop.settings_get('speed'))"), "9");

    // And an event registered from one mod's script runs as that mod: each
    // subscribes, each turn reaches both, and neither sees the other's global.
    MOD_CHECK_STR(
        mods_lua_eval(
            OWNER, "mine = 0\npop.on_turn('after', function() mine = mine + 1 end)\nreturn 'ok'\n"),
        "ok");
    MOD_CHECK_STR(
        mods_lua_eval(
            OWNER2,
            "theirs = 0\npop.on_turn('after', function() theirs = theirs + 1 end)\nreturn 'ok'\n"),
        "ok");
    run_turn();
    int64_t a = -1, b = -1;
    MOD_CHECK(mods_lua_global_int(OWNER, "mine", &a));
    MOD_CHECK(mods_lua_global_int(OWNER2, "theirs", &b));
    MOD_CHECK_EQ(a, 1);
    MOD_CHECK_EQ(b, 1);
    MOD_CHECK(!mods_lua_global_int(OWNER, "theirs", &a));
    MOD_CHECK(!mods_lua_global_int(OWNER2, "mine", &b));
    MOD_CHECK_EQ(mods_lua_errors(), 0u);
}
