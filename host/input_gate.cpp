// input_gate.cpp - see input_gate.h.
#include "input_gate.h"
#include "../runtime/mods_seam.h"
#include "../runtime/win32.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <algorithm>
#include <cmath>
#include <mutex>
#include <utility>
#include "../platform/os.h"

// Drawable pixels at either edge that still count as the edge row/column: an
// iPadOS pointer stops half a point short of the left edge (drawable x 1), and
// the last window point lands a pixel or two short of the last drawable row.
constexpr int kEdgeSlackPx = 3;

namespace {

enum : uint32_t {
    WM_KEYDOWN_ = 0x0100,
    WM_KEYUP_ = 0x0101,
    WM_CHAR_ = 0x0102,
    WM_SYSKEYDOWN_ = 0x0104,
    WM_SYSKEYUP_ = 0x0105,
};

// What the GUEST believes, by virtual key: set only when a message was
// actually posted for it.
bool g_guest_key[256];
// What the KEYBOARD is doing, by virtual key, for the modifier sides only.
// Kept apart from the above so a consumed press still produces a transition
// when it comes up.
bool g_phys_mod[256];
// Shift 1, control 2, alt 4, counting only modifiers the guest was told about.
uint8_t g_guest_modifiers;

void post(uint32_t msg, uint32_t wparam, uint32_t lparam) {
    // Keyboard input is addressed to the focus, not to a window the host
    // picked: the runtime knows which window has it, and in a VCL application
    // the first window created is the invisible application one, which does
    // nothing with a keystroke. Mouse messages from this path carry a guest
    // position, so they are routed by it.
    if (msg >= 0x0100 && msg <= 0x0109) {
        host_post_key_message(msg, wparam, lparam);
        return;
    }
    if (msg >= 0x0200 && msg <= 0x0209) {
        host_post_client_mouse_message(host_main_window(), msg, wparam, int16_t(lparam),
                                       int16_t(lparam >> 16));
        return;
    }
    uint32_t hwnd = host_main_window();
    if (hwnd)
        host_post_message(hwnd, msg, wparam, lparam);
}

// `sys` decides WM_SYSKEYDOWN/UP against WM_KEYDOWN/UP and is about whether
// Alt was down when the keystroke happened; `context` is lParam bit 29 and is
// about whether Alt is down now. Releasing Alt itself is the case where they
// differ: Win32 sends WM_SYSKEYUP, with the context bit clear because Alt is
// no longer held.
void post_key(HostKeyMapping m, bool down, bool sys, bool context, uint32_t character) {
    if (!m.vk)
        return;
    bool was_down = g_guest_key[m.vk];
    g_guest_key[m.vk] = down;
    uint32_t lparam = host_key_lparam(m, down, context, down ? was_down : true);
    post(down ? (sys ? WM_SYSKEYDOWN_ : WM_KEYDOWN_) : (sys ? WM_SYSKEYUP_ : WM_KEYUP_), m.vk,
         lparam);
    // Printable characters, and the four control characters Win32 also
    // delivers as WM_CHAR.
    if (down && !sys && character &&
        ((character >= 0x20 && character < 0x7f) || character == 0x0d || character == 0x1b ||
         character == 0x09 || character == 0x08))
        post(WM_CHAR_, character, lparam);
}

// The modifier bit a scan code contributes: either side counts, which is what
// a game reading GetAsyncKeyState(VK_SHIFT) has to see. Caps lock contributes
// nothing, as host_modifier_bits also has it.
uint8_t bit_for_dik(uint8_t dik) {
    switch (dik) {
    case 0x2a:
    case 0x36:
        return 1; // DIK_LSHIFT, DIK_RSHIFT
    case 0x1d:
    case 0x9d:
        return 2; // DIK_LCONTROL, DIK_RCONTROL
    case 0x38:
    case 0xb8:
        return 4; // DIK_LMENU, DIK_RMENU
    default:
        return 0;
    }
}

// Rebuilt from delivered state rather than accumulated, so it cannot drift.
void recompute_guest_modifiers() {
    uint8_t bits = 0;
    for (int i = 0; i < host_modifier_key_count(); ++i) {
        HostKeyMapping m = host_key_mapping(host_modifier_key(i));
        if (m.vk && g_guest_key[m.vk])
            bits |= bit_for_dik(m.dik);
    }
    g_guest_modifiers = bits;
}

const int kMaxModifiers = 16;

} // namespace

// ---------------------------------------------------------------------------
// Pointer capture: the policy and the arithmetic. See input_gate.h.
// ---------------------------------------------------------------------------
namespace {
bool g_captured = false;
int g_mode_w = 640, g_mode_h = 480;
int32_t g_cursor_x = 320, g_cursor_y = 240;

// Presenter writes one slot; baton takes a value copy of the newest slot.
// The short mutex protects copies only, never callbacks, GPU or AppKit work.
std::mutex g_layout_mutex;
LayoutSnapshot g_layout_slots[3], g_layout;
uint64_t g_layout_serial = 0, g_layout_seen = 0;
HitResult g_drag;
double g_drawable_x = 0, g_drawable_y = 0;
double g_motion_remainder_x = 0, g_motion_remainder_y = 0;
int g_resize_w = 0, g_resize_h = 0;
uint64_t g_resize_serial = 0, g_resize_seen = 0;
bool g_drawable_valid = false;
bool g_window_valid = false;
int32_t g_window_x = 0, g_window_y = 0;

// Adopt the latest published layout/resize snapshot on the input side. Rescale the
// virtual pointer only when drawable dimensions change, not when scene mapping changes.
void take_layout() {
    LayoutSnapshot next;
    int w, h;
    {
        std::lock_guard lock(g_layout_mutex);
        if (g_layout_serial == g_layout_seen && g_resize_serial == g_resize_seen)
            return;
        next = g_layout_serial ? g_layout_slots[g_layout_serial % 3] : g_layout;
        w = g_resize_w;
        h = g_resize_h;
        g_layout_seen = g_layout_serial;
        g_resize_seen = g_resize_serial;
    }
    if (w > 0 && h > 0 && (next.drawable_w != w || next.drawable_h != h))
        next = compositor_resize_layout(next, w, h);
    // Geometry changes must not masquerade as mouse motion. The virtual pointer
    // lives in DRAWABLE pixels, so only a change of drawable SIZE moves it, and
    // then in proportion. Rescaling by the scene mapping instead made it jump
    // whenever the presenter alternated between a scene layout and the
    // full-frame compatibility layout, since those differ by the scene scale:
    // the pointer bounced between two positions and dragged the game's cursor
    // with it. A scene change with the same drawable leaves the pointer alone.
    if (g_captured && g_drawable_valid && g_layout.drawable_w > 1 && g_layout.drawable_h > 1 &&
        next.drawable_w > 1 && next.drawable_h > 1 &&
        (next.drawable_w != g_layout.drawable_w || next.drawable_h != g_layout.drawable_h)) {
        g_drawable_x = g_drawable_x / (g_layout.drawable_w - 1) * (next.drawable_w - 1);
        g_drawable_y = g_drawable_y / (g_layout.drawable_h - 1) * (next.drawable_h - 1);
    }
    if (next.drawable_w != g_layout.drawable_w || next.drawable_h != g_layout.drawable_h) {
        g_window_valid = false;
        if (!g_captured)
            g_drawable_valid = false; // Old backing coordinates cannot seed a delta.
    }
    g_layout = std::move(next);
    // Keep the mapped host pointer inside the drawable after a resize.
    if (g_layout.drawable_w > 0 && g_layout.drawable_h > 0) {
        g_drawable_x = std::clamp(g_drawable_x, 0.0, double(g_layout.drawable_w - 1));
        g_drawable_y = std::clamp(g_drawable_y, 0.0, double(g_layout.drawable_h - 1));
    }
    if (!std::isfinite(g_drawable_x) || !std::isfinite(g_drawable_y)) {
        g_drawable_valid = false;
        g_drawable_x = g_drawable_y = 0;
    }
}
int32_t pixel(double v) {
    return int32_t(std::clamp(std::floor(v), double(INT32_MIN), double(INT32_MAX)));
}
bool contains(const LayoutRect &r, double x, double y) {
    return x >= r.x && y >= r.y && x < double(r.x) + r.w && y < double(r.y) + r.h;
}
HitResult map_owner(HitResult hit, double x, double y) {
    if (hit.kind == HitResult::HIT_ELEMENT) {
        hit.gx = pixel(hit.guest.x + (x - hit.drawable.x) * hit.guest.w / hit.drawable.w);
        hit.gy = pixel(hit.guest.y + (y - hit.drawable.y) * hit.guest.h / hit.drawable.h);
    } else if (hit.kind == HitResult::HIT_SCENE) {
        hit.gx = pixel(
            std::clamp((x - hit.scene.offset_x) / hit.scene.scale_x, 0.0, double(hit.guest.w - 1)));
        hit.gy = pixel(
            std::clamp((y - hit.scene.offset_y) / hit.scene.scale_y, 0.0, double(hit.guest.h - 1)));
    }
    return hit;
}
HitResult hit_layout(const LayoutSnapshot &layout, double x, double y) {
    if (g_drag.kind != HitResult::HIT_NONE)
        return map_owner(g_drag, x, y);
    if (layout.drawable_w <= 0 || layout.drawable_h <= 0 || x < 0 || y < 0 ||
        x >= layout.drawable_w || y >= layout.drawable_h)
        return {};
    const LayoutElement *top = nullptr;
    for (const auto &e : layout.elements)
        if (!e.is_cursor && contains(e.drawable, x, y) && (!top || e.last_seq >= top->last_seq))
            top = &e;
    if (top) {
        HitResult h;
        h.kind = HitResult::HIT_ELEMENT;
        h.element = top->id;
        h.guest = top->guest;
        h.drawable = top->drawable;
        return map_owner(h, x, y);
    }
    HitResult h;
    h.kind = HitResult::HIT_SCENE;
    h.scene = layout.scene;
    h.guest = {0, 0, layout.scene.domain_w, layout.guest_h};
    h.drawable = {0, 0, layout.drawable_w, layout.drawable_h};
    h = map_owner(h, x, y);
    // The final drawable pixels must reach the guest edge even at 4x scale.
    // Pillarbox boundaries inside the drawable are not scrolling boundaries.
    // A pointer or finger at the window's last point lands a pixel or two
    // short of the drawable's last row once scaled (1666 of 1668 on an
    // iPad), so the last kEdgeSlackPx pixels all count as the edge.
    if (x <= kEdgeSlackPx)
        h.gx = 0;
    else if (x >= layout.drawable_w - 1 - kEdgeSlackPx)
        h.gx = h.guest.w - 1;
    else if (h.guest.w > 2)
        h.gx = std::clamp(h.gx, 1, h.guest.w - 2);
    if (y <= kEdgeSlackPx)
        h.gy = 0;
    else if (y >= layout.drawable_h - 1 - kEdgeSlackPx)
        h.gy = h.guest.h - 1;
    else if (h.guest.h > 2)
        h.gy = std::clamp(h.gy, 1, h.guest.h - 2);
    return h;
}

int32_t clamp_to(int32_t v, int32_t hi) {
    if (v < 0)
        return 0;
    if (v > hi)
        return hi;
    return v;
}
} // namespace

void host_pointer_set_mode(int w, int h) {
    if (w > 0)
        g_mode_w = w;
    if (h > 0)
        g_mode_h = h;
    // A mode change can leave the cursor outside the new frame.
    g_cursor_x = clamp_to(g_cursor_x, g_mode_w - 1);
    g_cursor_y = clamp_to(g_cursor_y, g_mode_h - 1);
}

void host_pointer_capture(bool captured) {
    if (g_captured == captured)
        return;
    g_captured = captured;
    if (!captured)
        g_drawable_valid = false;
    // Capture changes ownership, not the fractional motion still owed.
    if (!captured)
        host_gate_end_drag();
}
bool host_pointer_captured() {
    return g_captured;
}

void host_pointer_center() {
    g_motion_remainder_x = g_motion_remainder_y = 0;
    g_cursor_x = g_mode_w / 2;
    g_cursor_y = g_mode_h / 2;
    take_layout();
    g_drawable_valid = g_layout.drawable_w > 0 && g_layout.drawable_h > 0;
    if (g_drawable_valid) {
        g_drawable_x = g_layout.drawable_w / 2.0;
        g_drawable_y = g_layout.drawable_h / 2.0;
        auto h = hit_layout(g_layout, g_drawable_x, g_drawable_y);
        g_cursor_x = h.gx;
        g_cursor_y = h.gy;
        compositor_set_pointer_position(true, pixel(g_drawable_x), pixel(g_drawable_y));
    }
}

void host_pointer_cursor(int32_t *x, int32_t *y) {
    if (x)
        *x = g_cursor_x;
    if (y)
        *y = g_cursor_y;
}

bool host_pointer_motion(int32_t dx, int32_t dy) {
    // A delta that arrives while released belongs to the desktop, not to the
    // guest: the pointer is the user's again and moving the guest's cursor
    // with it is the desync this exists to stop.
    if (!g_captured)
        return false;
    int32_t nx = clamp_to(g_cursor_x + dx, g_mode_w - 1);
    int32_t ny = clamp_to(g_cursor_y + dy, g_mode_h - 1);
    if (nx == g_cursor_x && ny == g_cursor_y)
        return false; // against an edge
    g_cursor_x = nx;
    g_cursor_y = ny;
    return true;
}

bool host_gate_key(uint16_t mac, bool down) {
    HostKeyMapping m = host_key_mapping(mac);
    return mods_input_key(m.dik, m.vk, down);
}
uint32_t host_input_batch_limit(const HostInputStep *steps, uint32_t count) {
    if (!steps || count == 0)
        return 0;
    uint8_t pressed = 0;
    for (uint32_t i = 0; i < count; ++i) {
        if (!steps[i].is_button || steps[i].button > 2)
            continue;
        const uint8_t bit = uint8_t(1u << steps[i].button);
        if (steps[i].down) {
            pressed |= bit;
            continue;
        }
        if (pressed & bit)
            return i; // its press is in this batch: the release waits a turn
    }
    return count;
}

bool host_gate_button(int button, bool down, int32_t x, int32_t y) {
    return mods_input_button(button, down, x, y);
}
bool host_gate_motion(int32_t x, int32_t y, int32_t dx, int32_t dy) {
    return mods_input_motion(x, y, dx, dy);
}
bool host_gate_wheel(int32_t dz) {
    return mods_input_wheel(dz);
}

uint8_t host_guest_modifiers() {
    return g_guest_modifiers;
}
bool host_guest_key_down(uint8_t vk) {
    return g_guest_key[vk];
}

// The guest's own button mask, for the same reason g_guest_key exists: a
// consumed press must not leave the guest believing a button is down.
uint8_t g_guest_buttons = 0;

bool host_gate_inject_guest_click(int32_t gx, int32_t gy, int button, bool down) {
    if (button < 0 || button > 2)
        return false;
    if (gx < 0)
        gx = 0;
    if (gy < 0)
        gy = 0;
    // The decision first, and once: a gate that were asked for the motion and
    // the button separately could consume one and deliver the other.
    if (host_gate_button(button, down, gx, gy))
        return true;
    // Absolute, with no delta. A delta would be relative motion, which is the
    // physical mapping this exists to bypass.
    host_input_motion(gx, gy, 0, 0);
    if (down)
        g_guest_buttons |= (uint8_t)(1u << button);
    else
        g_guest_buttons &= (uint8_t)~(1u << button);
    host_input_button(button, down);
    static const uint32_t kMouseMove = 0x0200;
    static const uint32_t msgs[3][2] = {
        {0x0202, 0x0201}, // WM_LBUTTONUP,   WM_LBUTTONDOWN
        {0x0205, 0x0204}, // WM_RBUTTONUP,   WM_RBUTTONDOWN
        {0x0208, 0x0207}, // WM_MBUTTONUP,   WM_MBUTTONDOWN
    };
    const uint32_t lparam = ((uint32_t)(gy & 0xffff) << 16) | (uint32_t)(gx & 0xffff);
    const uint32_t wparam = host_mouse_wparam(g_guest_buttons, host_guest_modifiers());
    // The move first, so a window that reads the position from the move rather
    // than from the click's lParam sees the same place.
    post(kMouseMove, wparam, lparam);
    post(msgs[button][down ? 1 : 0], wparam, lparam);
    return false;
}

bool host_key_event(uint16_t mac, bool down, uint32_t character) {
    if (host_gate_key(mac, down))
        return true; // no state, no message
    HostKeyMapping m = host_key_mapping(mac);
    host_input_key(mac, down);
    // Alt AS THE GUEST KNOWS IT. An ordinary key event does not change the
    // modifiers, so "was Alt down" and "is Alt down" are the same question
    // here - but a consumed Alt never reached the guest, and must not change
    // how this key is classified.
    bool alt = (g_guest_modifiers & 4) != 0;
    post_key(m, down, alt, alt, character);
    return false;
}

// Translate physical modifier transitions through the input filter into guest key events.
// Track physical and delivered states separately so consumed presses still receive releases.
void host_modifier_event(uint32_t flags) {
    const int n =
        host_modifier_key_count() < kMaxModifiers ? host_modifier_key_count() : kMaxModifiers;
    HostKeyMapping map[kMaxModifiers];
    bool down_now[kMaxModifiers], deliver[kMaxModifiers];

    // Pass 1: PHYSICAL transitions decide what is offered, and the filter
    // decides what the guest gets. Physical, not delivered: a consumed press
    // has to be seen coming up too, or the filter is never told the key was
    // let go and answers every later press as a repeat.
    for (int i = 0; i < n; ++i) {
        uint16_t mac = host_modifier_key(i);
        map[i] = host_key_mapping(mac);
        down_now[i] = host_modifier_side_down(flags, mac);
        deliver[i] = false;
        if (!map[i].vk || g_phys_mod[map[i].vk] == down_now[i])
            continue;
        g_phys_mod[map[i].vk] = down_now[i];
        deliver[i] = !host_gate_key(mac, down_now[i]);
    }

    // The guest's Alt before this event and after it, counting only what it
    // will actually be told. Releasing Alt is where the two differ, and it is
    // why that release is a WM_SYSKEYUP rather than a plain WM_KEYUP.
    bool alt_before = (g_guest_modifiers & 4) != 0;
    bool alt_now = false;
    for (int i = 0; i < n; ++i) {
        if (!map[i].vk || bit_for_dik(map[i].dik) != 4)
            continue;
        bool after = deliver[i] ? down_now[i] : g_guest_key[map[i].vk];
        if (after)
            alt_now = true;
    }

    // Pass 2: deliver, one key at a time. host_input_modifiers sets every
    // modifier at once and a consumed one must not be among them, which is
    // why the diff is here rather than inside it.
    for (int i = 0; i < n; ++i) {
        if (!deliver[i])
            continue;
        host_input_key(host_modifier_key(i), down_now[i]);
        post_key(map[i], down_now[i], alt_before || alt_now, alt_now, 0);
    }
    recompute_guest_modifiers();
}

// The same diff host_modifier_event runs. Named separately because the reason
// to call it is different - recovering state nobody told us about, rather than
// reacting to a change we were told about - and the call sites should say
// which they are doing.
void host_gate_sync_modifiers(uint32_t flags) {
    host_modifier_event(flags);
}

void host_gate_release_all() {
    // Nothing that was down can be seen coming up while another application
    // has the focus, so it all comes up now - on BOTH sides. A filter left
    // holding a consumed key would answer the next press as a repeat and
    // never ask its callbacks again.
    host_input_release_all();
    mods_input_release_all();
    memset(g_guest_key, 0, sizeof g_guest_key);
    memset(g_phys_mod, 0, sizeof g_phys_mod);
    g_guest_modifiers = 0;
    // Focus loss releases the pointer as well: the window no longer owns it,
    // and the cursor has to come back so the user can reach anything else.
    g_captured = false;
    g_window_valid = false;
    g_drawable_valid = false;
    g_motion_remainder_x = g_motion_remainder_y = 0;
    g_guest_buttons = 0;
    host_gate_end_drag();
    compositor_set_pointer_position(false, 0, 0);
}

void host_gate_reset() {
    memset(g_guest_key, 0, sizeof g_guest_key);
    memset(g_phys_mod, 0, sizeof g_phys_mod);
    g_guest_modifiers = 0;
    g_captured = false;
    g_mode_w = 640;
    g_mode_h = 480;
    {
        std::lock_guard lock(g_layout_mutex);
        for (auto &slot : g_layout_slots)
            slot = {};
        g_layout_serial = g_layout_seen = 0;
        g_resize_w = g_resize_h = 0;
        g_resize_serial = g_resize_seen = 0;
    }
    g_layout = {};
    g_drag = {};
    g_drawable_valid = false;
    g_window_valid = false;
    g_guest_buttons = 0;
    // A reset starts a new session: nothing is owed and nothing is stale.
    compositor_set_pointer_position(false, 0, 0);
    host_pointer_center();
}

void host_gate_set_layout(const CompositorInput *in) {
    auto snapshot = compositor_layout_snapshot(in);
    host_gate_publish_layout(snapshot);
}
void host_gate_publish_layout(const LayoutSnapshot &snapshot) {
    std::lock_guard lock(g_layout_mutex);
    g_layout_slots[(g_layout_serial + 1) % 3] = snapshot;
    ++g_layout_serial;
}
void host_gate_fallback_layout(int w, int h) {
    take_layout();
    if (g_layout_seen || w <= 0 || h <= 0)
        return;
    double scale = std::min(double(w) / g_mode_w, double(h) / g_mode_h);
    g_layout.drawable_w = w;
    g_layout.drawable_h = h;
    g_layout.guest_w = g_mode_w;
    g_layout.guest_h = g_mode_h;
    g_layout.scene = {float(scale), float(scale), float((w - g_mode_w * scale) / 2),
                      float((h - g_mode_h * scale) / 2), g_mode_w};
}
HitResult host_gate_hit_test(const CompositorInput *in, int32_t x, int32_t y) {
    if (in)
        return hit_layout(compositor_layout_snapshot(in), x, y);
    take_layout();
    return hit_layout(g_layout, x, y);
}
void host_gate_begin_drag(const HitResult *owner) {
    if (g_drag.kind != HitResult::HIT_NONE)
        return;
    if (owner && owner->kind != HitResult::HIT_NONE && owner->guest.w > 0 && owner->guest.h > 0 &&
        owner->drawable.w > 0 && owner->drawable.h > 0)
        g_drag = *owner;
}
void host_gate_end_drag() {
    g_drag = {};
}
// Map native pointer motion through the current layout to guest input and compositor state.
// Captured motion uses relative deltas; free motion uses positions within the drawable.
HitResult host_gate_pointer_event(int32_t x, int32_t y, double dx, double dy, int32_t *guest_dx,
                                  int32_t *guest_dy) {
    *guest_dx = *guest_dy = 0;
    take_layout();
    if (g_layout.drawable_w <= 0 || g_layout.drawable_h <= 0)
        return {};
    if (!g_captured && (x < 0 || y < 0 || x >= g_layout.drawable_w || y >= g_layout.drawable_h)) {
        g_drawable_valid = false;
        return {};
    }
    const bool first = !g_drawable_valid;
    if (first) {
        g_drawable_x =
            g_captured ? g_cursor_x * double(g_layout.scene.scale_x) + g_layout.scene.offset_x : x;
        g_drawable_y =
            g_captured ? g_cursor_y * double(g_layout.scene.scale_y) + g_layout.scene.offset_y : y;
        g_drawable_valid = true;
    }
    const double old_x = g_drawable_x, old_y = g_drawable_y;
    const auto before = hit_layout(g_layout, old_x, old_y);
    // Free motion comes from positions; captured motion uses the caller delta
    // (window callers derive it from positions). Never difference against g_cursor:
    // that position may belong to a previous layout, clamp or hit owner.
    dx = g_captured ? (std::isfinite(dx) ? dx : 0) : (first ? 0 : x - old_x);
    dy = g_captured ? (std::isfinite(dy) ? dy : 0) : (first ? 0 : y - old_y);
    g_drawable_x =
        std::clamp(g_captured ? old_x + dx : double(x), 0.0, double(g_layout.drawable_w - 1));
    g_drawable_y =
        std::clamp(g_captured ? old_y + dy : double(y), 0.0, double(g_layout.drawable_h - 1));
    auto hit = hit_layout(g_layout, g_drawable_x, g_drawable_y);
    if (hit.kind == HitResult::HIT_NONE)
        return hit;
    double motion_x = dx / g_layout.scene.scale_x, motion_y = dy / g_layout.scene.scale_y;
    const bool anchored =
        g_layout.cls == HOST_SCREEN_GAMEPLAY && !g_layout.legacy && !g_layout.classic;
    if (anchored && before.kind == HitResult::HIT_ELEMENT && hit.kind == HitResult::HIT_ELEMENT &&
        before.element == hit.element) {
        motion_x = dx * hit.guest.w / hit.drawable.w;
        motion_y = dy * hit.guest.h / hit.drawable.h;
    } else if (anchored && (dx != 0 || dy != 0) && before.kind != HitResult::HIT_NONE &&
               (before.kind == HitResult::HIT_ELEMENT || hit.kind == HitResult::HIT_ELEMENT)) {
        // Anchored gameplay HUDs deliberately move between guest regions.
        // Compute that crossing in ONE current layout, never across frames.
        motion_x = double(hit.gx) - before.gx;
        motion_y = double(hit.gy) - before.gy;
    }
    auto accumulate = [](double value, double &remainder) {
        value += remainder;
        // Cancel floating-point noise at an integer boundary (e.g. ten
        // 0.1-pixel moves), without rounding genuine fractional movement.
        const double nearest = std::round(value);
        if (std::abs(value - nearest) < 1e-9)
            value = nearest;
        const double whole = std::trunc(value);
        remainder = value - whole;
        return pixel(whole);
    };
    *guest_dx = accumulate(motion_x, g_motion_remainder_x);
    *guest_dy = accumulate(motion_y, g_motion_remainder_y);
    // Preserve the mapped guest position for Win32 cursor queries.
    g_cursor_x = hit.gx;
    g_cursor_y = hit.gy;
    compositor_set_pointer_position(true, pixel(g_drawable_x), pixel(g_drawable_y));
    return hit;
}

void host_gate_publish_drawable_size(int w, int h) {
    if (w <= 0 || h <= 0)
        return;
    std::lock_guard lock(g_layout_mutex);
    if (w == g_resize_w && h == g_resize_h)
        return;
    g_resize_w = w;
    g_resize_h = h;
    ++g_resize_serial;
}
bool host_pointer_can_capture(bool key, bool inside, bool click, bool escape, bool page) {
    return key && inside && click && !escape && !page;
}
bool host_pointer_confinement_wanted(bool captured, int window_mode) {
    (void)window_mode;
    return captured;
}
bool host_pointer_at_resize_edge(double x, double y, double w, double h, double margin) {
    return w > 0 && h > 0 && (x < margin || y < margin || x >= w - margin || y >= h - margin);
}
void host_pointer_drawable_position(double *x, double *y) {
    *x = g_drawable_x;
    *y = g_drawable_y;
}

HitResult host_gate_window_pointer(int32_t x, int32_t y, int32_t *dx, int32_t *dy) {
    take_layout();
    // Both capture states use the OS-accelerated window pointer. Device counts
    // are a different gain and must never be mixed with position differences.
    if (g_layout.drawable_w <= 0 || g_layout.drawable_h <= 0) {
        *dx = *dy = 0;
        return {};
    }
    if (!g_captured && (x < 0 || y < 0 || x >= g_layout.drawable_w || y >= g_layout.drawable_h)) {
        g_window_valid = false;
        return host_gate_pointer_event(x, y, 0, 0, dx, dy);
    }
    x = std::clamp(x, 0, g_layout.drawable_w - 1);
    y = std::clamp(y, 0, g_layout.drawable_h - 1);
    // Release does not discard the previous position. Re-seed the mapping
    // after capture changes without delivering the seed as a second motion.
    if (!g_captured && !g_drawable_valid && g_window_valid) {
        int32_t ignored_x, ignored_y;
        host_gate_pointer_event(g_window_x, g_window_y, 0, 0, &ignored_x, &ignored_y);
    }
    // Captured remains position-based for window callers. Map the native
    // position, including the first event and after host/guest edge clamps.
    if (g_captured) {
        g_drawable_x = g_window_valid ? g_window_x : x;
        g_drawable_y = g_window_valid ? g_window_y : y;
        g_drawable_valid = true;
    }
    auto hit = host_gate_pointer_event(x, y, g_window_valid ? x - g_window_x : 0,
                                       g_window_valid ? y - g_window_y : 0, dx, dy);
    g_window_x = x;
    g_window_y = y;
    g_window_valid = true;
    return hit;
}

// Relative devices own their cursor integration and sensitivity. Preserve counts
// even beyond the cosmetic Win32 cursor's edges; never correct toward the OS.
bool host_gate_relative_motion(double dx, double dy) {
    if (!g_captured)
        return false;
    auto counts = [](double value, double &remainder) {
        if (!std::isfinite(value))
            return int32_t(0);
        value += remainder;
        const double nearest = std::round(value);
        if (std::abs(value - nearest) < 1e-9)
            value = nearest;
        const double whole = std::trunc(value);
        remainder = value - whole;
        return pixel(whole);
    };
    const int32_t mx = counts(dx, g_motion_remainder_x);
    const int32_t my = counts(dy, g_motion_remainder_y);
    const int32_t x =
        int32_t(std::clamp(int64_t(g_cursor_x) + mx, int64_t(0), int64_t(g_mode_w - 1)));
    const int32_t y =
        int32_t(std::clamp(int64_t(g_cursor_y) + my, int64_t(0), int64_t(g_mode_h - 1)));
    if (host_gate_motion(x, y, mx, my))
        return false;
    g_cursor_x = x;
    g_cursor_y = y;
    host_input_motion(x, y, mx, my);
    return true;
}

bool host_gate_window_motion(int32_t x, int32_t y, double dx, double dy, HitResult *hit) {
    int32_t gx, gy;
    *hit = host_gate_window_pointer(x, y, &gx, &gy);
    if (hit->kind == HitResult::HIT_NONE || host_gate_motion(hit->gx, hit->gy, gx, gy))
        return false;
    host_input_motion(hit->gx, hit->gy, gx, gy);
    return true;
}

// Map an absolute host position to the guest's ordinary Win32 mouse position.
// This does not update a game-private cursor object or alter DirectInput counts.
bool host_gate_pointer_place(int32_t x, int32_t y) {
    const auto hit = host_gate_hit_test(nullptr, x, y);
    if (hit.kind == HitResult::HIT_NONE || mods_page_visible() ||
        host_gate_motion(hit.gx, hit.gy, 0, 0))
        return false;
    // This updates the generic Win32 cursor state. A game-integrated DirectInput
    // cursor still moves only when the game processes the ordinary input.
    g_cursor_x = hit.gx;
    g_cursor_y = hit.gy;
    host_input_motion(hit.gx, hit.gy, 0, 0);
    return true;
}
