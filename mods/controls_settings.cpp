// controls_settings.cpp - see controls_settings.h.
#include "controls_settings.h"

#include "mods_internal.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <mutex>
#include <utility>

namespace {
// The layout names come from the host (mods_controls_set_names), which must
// run before mods_controls_init because mods/ cannot depend on host/controls.
// Until the host calls it, this is what a fresh profile sees. g_names is set
// once at startup but read every frame by the host loop laying the controls
// out, on a different thread than the settings page's nudges; g_names_mutex
// guards every access.
std::mutex g_names_mutex;
std::vector<std::string> g_names = {"pad", "keys", "pad+keys"};

// EDIT_ROW is an action, never declared or persisted.
constexpr int kStoredRowCount = CONTROLS_EDIT_ROW;
struct RowSpec {
    const char *key;
    const char *label;
    int minimum;
    int maximum;
    int initial;
};
constexpr RowSpec kRows[CONTROLS_ROW_COUNT] = {
    {"mobile_control", "Mobile controls", 0, 2, 1},
    {"layout", "Controls", 0, 0, 0},
    {"size", "Controls size", 0, 2, 1},
    {"opacity", "Controls opacity", 20, 100, 100},
    {"haptics", "Button haptics", 0, 1, 1},
    {"pad_with_controller", "Pad with controller", 0, 1, 0},
    {"snap", "Editor snapping", 0, 1, 1},
    {nullptr, nullptr, 0, 0, 0},
};

// Written by the settings page and the on-screen tab, read by the host loop
// that lays the controls out: each value is atomic on its own.
std::atomic<int> values[CONTROLS_ROW_COUNT];
// The hidden-group bits: one 16-bit lane per form factor, packed into the
// single stored "hidden" value.
constexpr int kHiddenLaneBits = 16;
constexpr int64_t kHiddenMax = (int64_t(1) << (kHiddenLaneBits * CONTROLS_FORM_COUNT)) - 1;
std::atomic<uint32_t> hidden_groups[CONTROLS_FORM_COUNT] = {};
// A hidden-group change the host has not flushed to the settings store yet.
std::atomic<bool> hidden_dirty{false};
std::atomic<bool> edit_request{false};
std::atomic<bool> editing{false};
std::atomic<bool> initialized{false};
std::atomic<bool> platform_touch{true};
std::atomic<bool> layout_available{true};

int layout_max() {
    std::lock_guard<std::mutex> lock(g_names_mutex);
    return int(g_names.size()); // the extra index past the names is Hidden
}

int index_of(const std::string &name) {
    std::lock_guard<std::mutex> lock(g_names_mutex);
    for (size_t i = 0; i < g_names.size(); ++i)
        if (g_names[i] == name)
            return int(i);
    return -1;
}

// A copy, not a reference: the caller must not hold g_names_mutex while
// reading the result, and the vector may be reassigned out from under it.
std::string name_at(int index) {
    std::lock_guard<std::mutex> lock(g_names_mutex);
    return index >= 0 && index < int(g_names.size()) ? g_names[index] : std::string();
}

// The three lanes as one stored value.
int64_t packed_hidden() {
    int64_t v = 0;
    for (int f = 0; f < CONTROLS_FORM_COUNT; ++f)
        v |= int64_t(hidden_groups[f].load()) << (kHiddenLaneBits * f);
    return v;
}

void unpack_hidden(int64_t v) {
    if (v < 0)
        v = 0;
    for (int f = 0; f < CONTROLS_FORM_COUNT; ++f)
        hidden_groups[f] = uint32_t((v >> (kHiddenLaneBits * f)) & 0xffff);
}
} // namespace

void mods_controls_set_names(std::vector<std::string> names) {
    std::lock_guard<std::mutex> lock(g_names_mutex);
    g_names = std::move(names);
}

void mods_controls_refresh_names() {
    if (!initialized)
        return;
    // The row was declared with the old name count, and mods_settings_declare
    // is a no-op once a key exists, so the maximum is moved in place. Without
    // this a layout saved under a new name could not be selected.
    mods_settings_set_range(MODS_OWNER_RUNTIME, kRows[CONTROLS_LAYOUT_ROW].key, 0, layout_max());
    mods_controls_set(CONTROLS_LAYOUT_ROW,
                      std::clamp(values[CONTROLS_LAYOUT_ROW].load(), 0, layout_max()));
}

void mods_controls_init(const char *default_layout, int mobile_default) {
    if (initialized.exchange(true))
        return;

    const std::string want = default_layout ? default_layout : "";
    const int found = index_of(want);
    for (int i = 0; i < CONTROLS_ROW_COUNT; ++i)
        values[i] = kRows[i].initial;
    values[CONTROLS_MOBILE_ROW] = std::clamp(mobile_default, 0, 2);
    values[CONTROLS_LAYOUT_ROW] = want == "hidden" ? layout_max() : found >= 0 ? found : 0;
    unpack_hidden(0);

    for (int i = 0; i < kStoredRowCount; ++i) {
        const RowSpec &row = kRows[i];
        const int max = i == CONTROLS_LAYOUT_ROW ? layout_max() : row.maximum;
        mods_settings_declare(MODS_OWNER_RUNTIME, "host.controls", row.key, row.label,
                              POP_SETTING_INT, values[i], row.minimum, max);
        int64_t v = values[i].load();
        mods_settings_get(MODS_OWNER_RUNTIME, row.key, &v);
        // A layout index out of range means the profile names a layout this
        // build no longer discovers (a deleted user copy, a game that ships
        // fewer files). The spec has that fall back to the game's
        // default_layout, which values[] already holds: clamping instead
        // landed on the Hidden slot, so losing one file left a blank screen.
        if (i == CONTROLS_LAYOUT_ROW && (v < row.minimum || v > max))
            continue;
        values[i] = int(std::clamp<int64_t>(v, row.minimum, max));
    }
    // The hidden bits' own value carries all three form lanes.
    const int64_t hidden_initial = packed_hidden();
    mods_settings_declare(MODS_OWNER_RUNTIME, "host.controls", "hidden", "hidden", POP_SETTING_INT,
                          hidden_initial, 0, kHiddenMax);
    int64_t hv = hidden_initial;
    mods_settings_get(MODS_OWNER_RUNTIME, "hidden", &hv);
    unpack_hidden(hv);
    hidden_dirty = false;
}

bool mods_controls_initialized() {
    return initialized;
}

void mods_controls_reset() {
    initialized = false;
    platform_touch = true;
    layout_available = true;
    {
        std::lock_guard<std::mutex> lock(g_names_mutex);
        g_names = {"pad", "keys", "pad+keys"};
    }
    for (int i = 0; i < CONTROLS_ROW_COUNT; ++i)
        values[i] = kRows[i].initial;
    unpack_hidden(0);
    hidden_dirty = false;
    edit_request = false;
    editing = false;
}

int mods_controls_value(ControlsRow row) {
    return row >= 0 && row < CONTROLS_ROW_COUNT ? values[row].load() : 0;
}

std::string mods_controls_layout_name() {
    return name_at(mods_controls_value(CONTROLS_LAYOUT_ROW));
}

PopModStatus mods_controls_set(ControlsRow row, int value) {
    if (row < 0 || row >= CONTROLS_ROW_COUNT)
        return POP_E_INVAL;
    if (row == CONTROLS_EDIT_ROW) {
        edit_request = true;
        return POP_OK;
    }
    const int max = row == CONTROLS_LAYOUT_ROW ? layout_max() : kRows[row].maximum;
    const int v = std::clamp(value, kRows[row].minimum, max);
    values[row] = v;
    if (!initialized)
        return POP_OK; // nothing declared yet: the value is applied when it is
    return mods_settings_set(MODS_OWNER_RUNTIME, kRows[row].key, v);
}

// The layout choice and the on/off rows turn over in either direction, like
// the display rows; size and opacity stop at their ends. EDIT_ROW never
// carries a value: any nudge just asks the host to open the editor.
PopModStatus mods_controls_nudge(ControlsRow row, int delta) {
    if (row < 0 || row >= CONTROLS_ROW_COUNT)
        return POP_E_INVAL;
    if (row == CONTROLS_EDIT_ROW) {
        edit_request = true;
        return POP_OK;
    }
    const int current = mods_controls_value(row);
    int value;
    if (row == CONTROLS_MOBILE_ROW) {
        value = (current + delta + 3) % 3;
    } else if (row == CONTROLS_LAYOUT_ROW) {
        const int count = layout_max() + 1;
        value = ((current + delta) % count + count) % count;
    } else if (row == CONTROLS_SIZE_ROW) {
        value = std::clamp(current + delta, 0, 2);
    } else if (row == CONTROLS_OPACITY_ROW) {
        value = std::clamp(current + delta * 10, 20, 100);
    } else { // haptics, pad_with_controller, snap: plain toggles
        value = current ? 0 : 1;
    }
    return mods_controls_set(row, value);
}

std::string mods_controls_line(ControlsRow row) {
    char buf[128];
    switch (row) {
    case CONTROLS_MOBILE_ROW: {
        static const char *const modes[3] = {"Off", "Mobile only", "Always"};
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 modes[std::clamp(mods_controls_value(row), 0, 2)]);
        break;
    }
    case CONTROLS_LAYOUT_ROW: {
        const std::string name = mods_controls_layout_name();
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 name.empty() ? "hidden" : name.c_str());
        break;
    }
    case CONTROLS_SIZE_ROW: {
        static const char *const sizes[3] = {"Small", "Medium", "Large"};
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 sizes[std::clamp(mods_controls_value(row), 0, 2)]);
        break;
    }
    case CONTROLS_OPACITY_ROW:
        snprintf(buf, sizeof buf, "%-22s %d%%", kRows[row].label, mods_controls_value(row));
        break;
    case CONTROLS_HAPTICS_ROW:
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 mods_controls_value(row) ? "on" : "off");
        break;
    case CONTROLS_PAD_WITH_CONTROLLER_ROW:
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 mods_controls_value(row) ? "on" : "off");
        break;
    case CONTROLS_SNAP_ROW:
        snprintf(buf, sizeof buf, "%-22s %s", kRows[row].label,
                 mods_controls_value(row) ? "on" : "off");
        break;
    case CONTROLS_EDIT_ROW:
        snprintf(buf, sizeof buf, "%-22s %s", "Edit controls", ">");
        break;
    default:
        buf[0] = '\0';
    }
    return buf;
}

void mods_controls_set_platform(bool touch_device) {
    platform_touch = touch_device;
}

void mods_controls_set_layout_available(bool available) {
    layout_available = available;
}

bool mods_controls_row_applies(ControlsRow row) {
    if (row == CONTROLS_MOBILE_ROW)
        return true;
    const int mobile = mods_controls_value(CONTROLS_MOBILE_ROW);
    const bool active = mobile == 2 || (mobile == 1 && platform_touch.load());
    if (!active)
        return false;
    return row != CONTROLS_EDIT_ROW || layout_available.load();
}

uint32_t mods_controls_hidden_groups(ControlsForm form) {
    if (form < 0 || form >= CONTROLS_FORM_COUNT)
        return 0;
    return hidden_groups[form].load();
}

void mods_controls_set_hidden_groups(ControlsForm form, uint32_t bits) {
    if (form < 0 || form >= CONTROLS_FORM_COUNT)
        return;
    const uint32_t v = bits & 0xffffu;
    if (hidden_groups[form].exchange(v) == v)
        return;
    // The write itself waits for mods_controls_flush: this runs on the SDL
    // input thread under the player's finger, and mods_settings_set saves the
    // whole profile to disk.
    hidden_dirty = true;
}

void mods_controls_flush() {
    if (!hidden_dirty.exchange(false) || !initialized)
        return;
    (void)mods_settings_set(MODS_OWNER_RUNTIME, "hidden", packed_hidden());
}

bool mods_controls_take_edit_request() {
    return edit_request.exchange(false);
}

bool mods_controls_editing() {
    return editing.load();
}

void mods_controls_set_editing(bool on) {
    editing = on;
}
