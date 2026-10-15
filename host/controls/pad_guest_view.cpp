#include "pad_guest_view.h"

#include <mutex>

namespace {
std::mutex mutex;
HostPadGuestState states[2] = {};
} // namespace

extern "C" void host_pad_report_guest_state(const HostPadGuestState *state) {
    if (!state || state->api < 1 || state->api > 2)
        return;
    std::lock_guard<std::mutex> lock(mutex);
    HostPadGuestState &saved = states[state->api - 1];
    if (!state->partial) {
        saved = *state;
        return;
    }
    saved.api = state->api;
    saved.packet = state->packet;
    saved.partial = 1;
    for (int i = 0; i < 6; ++i) {
        if (!state->present[i])
            continue;
        saved.present[i] = 1;
        saved.axes[i] = state->axes[i];
        saved.minimum[i] = state->minimum[i];
        saved.maximum[i] = state->maximum[i];
    }
    if (state->buttons_present) {
        saved.buttons = state->buttons;
        saved.buttons_present = 1;
    }
    if (state->pov_present) {
        saved.pov = state->pov;
        saved.pov_present = 1;
    }
}

namespace controls {
HostPadGuestState pad_guest_view_snapshot(uint32_t api) {
    std::lock_guard<std::mutex> lock(mutex);
    if (api == 0)
        api = states[0].api ? 1 : 2;
    return api >= 1 && api <= 2 ? states[api - 1] : HostPadGuestState{};
}
} // namespace controls
