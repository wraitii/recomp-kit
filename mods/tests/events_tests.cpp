// events_tests.cpp - event subscriptions do not depend on a particular game's
// event addresses or guest data layout.
#include "mods_tests.h"
#include "../mods_internal.h"
#include "../../runtime/loader.h"
#include "../../runtime/memory.h"
#include "../../runtime/mods_seam.h"
#include "../../runtime/win32.h"

#include <string>
#include <vector>

namespace {
std::vector<std::string> sequence;
PopModApi api;

const PopModApi *provider(uint32_t owner) {
    return owner == MODS_OWNER_FIRST_MOD ? &api : nullptr;
}

void setup() {
    sched_set_guest_thread(true);
    sequence.clear();
    mem_init();
    loader_load(nullptr);
    MOD_CHECK(mods_symbols_load(nullptr));
    mods_hooks_reset();
    mods_events_reset();
    mods_settings_reset();
    memset(&api, 0, sizeof api);
    api.version = POP_MOD_API_VERSION;
    api.size = (uint32_t)sizeof(PopModApi);
    api.mod_index = MODS_OWNER_FIRST_MOD;
    api.mod_id = "test.events";
    mods_set_context_provider(provider);
    mods_fill_hooks_api(&api);
    mods_fill_events_api(&api);
    MOD_CHECK(mods_events_init());
}
} // namespace

MOD_TEST_SUITE(events_fire_registered_phases_in_order) {
    setup();
    uint32_t ignored = 0;
    MOD_CHECK_EQ(mods_on_turn(
                     MODS_OWNER_FIRST_MOD, POP_EVENT_BEFORE,
                     [](const PopModApi *a, void *) {
                         sequence.push_back(a->mod_id);
                         sequence.push_back("turn-before");
                     },
                     nullptr, &ignored),
                 POP_OK);
    MOD_CHECK_EQ(mods_on_turn(
                     MODS_OWNER_FIRST_MOD, POP_EVENT_AFTER,
                     [](const PopModApi *, void *) { sequence.push_back("turn-after"); }, nullptr,
                     &ignored),
                 POP_OK);
    MOD_CHECK_EQ(mods_on_frame(
                     MODS_OWNER_FIRST_MOD, POP_EVENT_BEFORE,
                     [](const PopModApi *, void *) { sequence.push_back("frame-before"); }, nullptr,
                     &ignored),
                 POP_OK);

    mods_events_test_fire(1, POP_EVENT_BEFORE);
    mods_events_test_fire(1, POP_EVENT_AFTER);
    mods_events_test_fire(0, POP_EVENT_BEFORE);
    MOD_CHECK_EQ(sequence.size(), 4u);
    if (sequence.size() == 4) {
        MOD_CHECK_STR(sequence[0].c_str(), "test.events");
        MOD_CHECK_STR(sequence[1].c_str(), "turn-before");
        MOD_CHECK_STR(sequence[2].c_str(), "turn-after");
        MOD_CHECK_STR(sequence[3].c_str(), "frame-before");
    }
}

MOD_TEST_SUITE(events_removal_and_level_events) {
    setup();
    uint32_t id = 0;
    MOD_CHECK_EQ(mods_on_level_load(
                     MODS_OWNER_FIRST_MOD,
                     [](const PopModApi *, void *) { sequence.push_back("load"); }, nullptr, &id),
                 POP_OK);
    MOD_CHECK_EQ(mods_on_level_end(
                     MODS_OWNER_FIRST_MOD,
                     [](const PopModApi *, void *) { sequence.push_back("end"); }, nullptr, &id),
                 POP_OK);
    mods_events_test_fire(2, 0);
    mods_events_test_fire(3, 0);
    MOD_CHECK_EQ(sequence.size(), 2u);
    if (sequence.size() == 2) {
        MOD_CHECK_STR(sequence[0].c_str(), "load");
        MOD_CHECK_STR(sequence[1].c_str(), "end");
    }
    mods_events_remove_all(MODS_OWNER_FIRST_MOD);
    sequence.clear();
    mods_events_test_fire(2, 0);
    mods_events_test_fire(3, 0);
    MOD_CHECK(sequence.empty());
}
