#include "pad_mapper_editor.h"

#include "pad_guest_view.h"
#include "../keypad_layout.h"

#include <algorithm>
#include <cmath>
#include <cstdio>

namespace controls {

namespace {

const char *const kAxisNames[kSdlAxisCount] = {"Left X",  "Left Y",    "Right X",
                                               "Right Y", "L trigger", "R trigger"};
const char *const kGuestAxisNames[kSdlAxisCount] = {"LX", "LY", "RX", "RY", "L2", "R2"};
const char *const kButtonNames[13] = {"South / A", "East / B", "West / X", "North / Y", "LB / L1",
                                      "RB / R1",   "LT / L2",  "RT / R2",  "L3",        "R3",
                                      "View",      "Menu",     "Guide"};
const char *const kHatNames[4] = {"Hat up", "Hat right", "Hat down", "Hat left"};
const char *const kMapperAxes[] = {"none", "LX", "LY", "RX", "RY", "L2", "R2"};
const int kMapperAxisCount = int(sizeof kMapperAxes / sizeof kMapperAxes[0]);
const uint16_t kButtonBits[13] = {kPadCross,  kPadCircle, kPadSquare, kPadTriangle, kPadL1,
                                  kPadR1,     kPadL2,     kPadR2,     kPadL3,       kPadR3,
                                  kPadSelect, kPadStart,  kPadPs};
const uint8_t kHatBits[4] = {kHatUp, kHatRight, kHatDown, kHatLeft};

float axis_value(const PadState &p, int i) {
    switch (i) {
    case 0:
        return p.lx;
    case 1:
        return p.ly;
    case 2:
        return p.rx;
    case 3:
        return p.ry;
    case 4:
        return p.l2;
    default:
        return p.r2;
    }
}

std::string value_text(float value) {
    char text[16];
    snprintf(text, sizeof text, "%+.2f", value);
    return text;
}

std::string key_name(int key) {
    if (!key)
        return "none";
    const char *name = scancode_name(key);
    return name ? name : "key?";
}

std::string guest_button_name(const ButtonBinding &b) {
    if (b.guest_buttons) {
        for (int i = 0; i < 13; ++i)
            if (b.guest_buttons == kButtonBits[i])
                return kButtonNames[i];
        return "buttons";
    }
    if (b.guest_hat) {
        for (int i = 0; i < 4; ++i)
            if (b.guest_hat == kHatBits[i])
                return kHatNames[i];
        return "hat";
    }
    return "none";
}

std::string guest_button_short(uint16_t bits) {
    if (!bits)
        return "none";
    for (int i = 0; i < 13; ++i)
        if (bits == kButtonBits[i]) {
            static const char *const short_names[13] = {
                "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "L3", "R3", "View", "Menu", "Guide"};
            return short_names[i];
        }
    return "multi";
}

int clamp_int(int value, int low, int high) {
    return std::max(low, std::min(value, high));
}

int native_axis_slot(int canonical_axis) {
    static const char *const slots[] = {"x", "y", "z", "rx", "ry", "rz"};
    const char *configured = host_pad_native_axes();
    if (!configured)
        return canonical_axis;
    int configured_axis = 0;
    const char *start = configured;
    for (const char *p = configured;; ++p) {
        if (*p != ',' && *p != '\0')
            continue;
        const std::string token(start, p);
        if (configured_axis == canonical_axis)
            for (int slot = 0; slot < 6; ++slot)
                if (token == slots[slot])
                    return slot;
        if (*p == '\0')
            break;
        ++configured_axis;
        start = p + 1;
    }
    return canonical_axis;
}

const char *native_axis_name(int slot) {
    static const char *const names[] = {"X", "Y", "Z", "Rx", "Ry", "Rz"};
    return names[clamp_int(slot, 0, 5)];
}

} // namespace

void PadMapperEditor::open(const PadMapperProfile &profile, const Screen &screen) {
    profile_ = profile;
    screen_ = screen;
    page_ = Page::Calibration;
    axis_field_ = AxisField::Deadzone;
    capture_ = Capture::None;
    action_ = PadMapperEditorAction::None;
    validation_error_.clear();
    open_ = true;
    pointer_down_ = false;
    sweep_ = false;
    center_capture_ = false;
    center_sum_ = 0;
    center_samples_ = 0;
    selected_axis_ = 0;
    selected_binding_ = 0;
    binding_part_ = 0;
    scroll_ = 0;
    guest_api_ = 0;
    preset_ = -1;
    last_guest_view_hash_ = 0;
    have_telemetry_ = false;
    changed();
}

void PadMapperEditor::set_screen(const Screen &screen) {
    if (screen_.dw == screen.dw && screen_.dh == screen.dh && screen_.scale == screen.scale)
        return;
    screen_ = screen;
    changed();
}

void PadMapperEditor::close() {
    open_ = false;
    capture_ = Capture::None;
    sweep_ = false;
    center_capture_ = false;
    changed();
}

bool PadMapperEditor::is_open() const {
    return open_;
}

const PadMapperProfile &PadMapperEditor::profile() const {
    return profile_;
}

uint32_t PadMapperEditor::guest_api() const {
    return guest_api_;
}

void PadMapperEditor::changed() {
    ++revision_;
}

void PadMapperEditor::add_key(int x, int y, int w, int h, const std::string &label, bool lit,
                              int id) {
    if (w <= 0 || h <= 0)
        return;
    DrawControl d;
    d.kind = Kind::Key;
    d.rect = Rect{x, y, w, h};
    d.label = label;
    d.lit = lit;
    d.layer = 0;
    frame_.toolbar.push_back(std::move(d));
    widget_ids_.push_back({Rect{x, y, w, h}, id});
}

void PadMapperEditor::add_meter(int x, int y, int w, int h, const std::string &label, float value,
                                bool bipolar) {
    DrawMeter meter;
    meter.rect = Rect{x, y, w, h};
    meter.label = label;
    meter.value = value;
    meter.bipolar = bipolar;
    frame_.meters.push_back(std::move(meter));
}

ControlsView PadMapperEditor::view(const Screen &screen, const PadState &raw,
                                   const PadState &calibrated, const PadState &guest_preview,
                                   const ::HostPadGuestState &guest_actual,
                                   const std::string &status) {
    set_screen(screen);
    if (!have_telemetry_ || raw != last_raw_ || calibrated != last_calibrated_ ||
        guest_preview != last_guest_) {
        last_raw_ = raw;
        last_calibrated_ = calibrated;
        last_guest_ = guest_preview;
        have_telemetry_ = true;
        changed();
    }
    std::string visible_status = validation_error_.empty() ? status : validation_error_;
    if (capture_ != Capture::None && visible_status.empty())
        visible_status = "Press a key to bind; Esc cancels capture";
    else if ((center_capture_ || sweep_) && visible_status.empty())
        visible_status = center_capture_ ? "Center capture: hold the axis at rest"
                                         : "Sweep: move through both ends, then finish";
    else if (visible_status.empty())
        visible_status = "1-3 pages | arrows adjust | A apply | Esc cancel";
    uint64_t guest_hash = 1469598103934665603ull;
    auto hash_num = [&](uint64_t n) {
        for (int i = 0; i < 8; ++i) {
            guest_hash ^= uint8_t(n >> (i * 8));
            guest_hash *= 1099511628211ull;
        }
    };
    hash_num(guest_actual.api);
    hash_num(guest_actual.packet);
    hash_num(guest_actual.partial);
    hash_num(guest_actual.buttons_present);
    hash_num(guest_actual.pov_present);
    hash_num(guest_actual.buttons);
    hash_num(guest_actual.pov);
    for (int i = 0; i < 6; ++i) {
        hash_num(uint32_t(guest_actual.axes[i]));
        hash_num(uint32_t(guest_actual.minimum[i]));
        hash_num(uint32_t(guest_actual.maximum[i]));
        hash_num(guest_actual.present[i]);
    }
    for (unsigned char c : visible_status) {
        guest_hash ^= c;
        guest_hash *= 1099511628211ull;
    }
    if (guest_hash != last_guest_view_hash_) {
        last_guest_view_hash_ = guest_hash;
        changed();
    }
    if (center_capture_) {
        center_sum_ += axis_value(raw, selected_axis_);
        if (++center_samples_ >= 30) {
            AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
            c.center = float(center_sum_ / center_samples_);
            if (c.mode == MapperAxisMode::Pedal)
                c.minimum = c.center;
            center_capture_ = false;
            center_sum_ = 0;
            center_samples_ = 0;
            changed();
        }
    }
    if (sweep_) {
        const float value = axis_value(raw, selected_axis_);
        sweep_min_ = std::min(sweep_min_, value);
        sweep_max_ = std::max(sweep_max_, value);
        AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
        const float source_min = selected_axis_ >= 4 ? 0.0f : -1.0f;
        c.minimum = c.mode == MapperAxisMode::Pedal ? std::max(source_min, sweep_min_) : sweep_min_;
        c.maximum = std::max(c.minimum + 0.001f, sweep_max_);
        if (c.mode == MapperAxisMode::Pedal)
            c.center = c.minimum;
        changed();
    }

    frame_ = ControlsView{};
    widget_ids_.clear();
    frame_.wanted = true;
    frame_.editing = true;
    frame_.dw = screen.dw;
    frame_.dh = screen.dh;
    frame_.opacity = 1.0;
    frame_.layers.assign(1, ControlsView::Layer{});
    frame_.layers[0].rect = Rect{0, 0, screen.dw, screen.dh};
    int width = screen.dw;
    int height = screen.dh;
    const int margin = std::max(5, int(std::lround(6 * screen.scale)));
    const int gap = std::max(2, int(std::lround(4 * screen.scale)));
    const int top = std::max(22, int(std::lround(26 * screen.scale)));
    const int footer = std::max(90, int(std::lround(98 * screen.scale)));
    const int tab_w = (width - margin * 2 - gap * 2) / 3;
    add_key(margin, margin, tab_w, top, "Calibration", page_ == Page::Calibration, 1);
    add_key(margin + tab_w + gap, margin, tab_w, top, "Bindings", page_ == Page::Bindings, 2);
    add_key(margin + (tab_w + gap) * 2, margin, tab_w, top, "Game view", page_ == Page::GameView,
            3);

    if (page_ == Page::Calibration) {
        const int status_h =
            visible_status.empty() ? 0 : std::max(16, int(std::lround(18 * screen.scale)));
        if (status_h)
            add_key(margin, margin + top + gap, width - margin * 2, status_h, visible_status, false,
                    999);
        const int row_top = margin + top + gap + status_h + (status_h ? gap : 0);
        const int row_h = std::max(18, int(std::lround(22 * screen.scale)));
        const int visible =
            std::max(1, std::min(int(kSdlAxisCount), (height - row_top - footer - margin) / row_h));
        visible_rows_ = visible;
        scroll_ = clamp_int(scroll_, 0, kSdlAxisCount - visible);
        if (selected_axis_ < scroll_)
            scroll_ = selected_axis_;
        if (selected_axis_ >= scroll_ + visible)
            scroll_ = selected_axis_ - visible + 1;
        for (int i = scroll_; i < std::min(int(kSdlAxisCount), scroll_ + visible); ++i) {
            const int y = row_top + (i - scroll_) * row_h;
            const int label_w = std::max(46, int(std::lround(54 * screen.scale)));
            const int meter_w = (width - margin * 2 - label_w - gap * 3) / 2;
            add_key(margin, y, label_w, row_h - gap, kAxisNames[i], selected_axis_ == i, 100 + i);
            const float r = axis_value(raw, i), c = axis_value(calibrated, i);
            const bool pedal = profile_.axes[size_t(i)].mode == MapperAxisMode::Pedal;
            add_meter(margin + label_w + gap, y, meter_w, row_h - gap, "RAW " + value_text(r), r,
                      !pedal);
            add_meter(margin + label_w + gap * 2 + meter_w, y, meter_w, row_h - gap,
                      "CAL " + value_text(c), c, !pedal);
        }
        const int base_y = height - footer + gap;
        const int btn_h = std::max(16, (footer - gap * 4) / 3);
        const AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
        char field_label[64];
        switch (axis_field_) {
        case AxisField::Deadzone:
            snprintf(field_label, sizeof field_label, "Deadzone %.2f", c.deadzone);
            break;
        case AxisField::Saturation:
            snprintf(field_label, sizeof field_label, "Saturation %.2f", c.saturation);
            break;
        case AxisField::Curve:
            snprintf(field_label, sizeof field_label, "Curve %.1f", c.curve);
            break;
        case AxisField::Inverted:
            snprintf(field_label, sizeof field_label, "Invert %s", c.inverted ? "on" : "off");
            break;
        case AxisField::Mode:
            snprintf(field_label, sizeof field_label, "%s",
                     c.mode == MapperAxisMode::Pedal ? "Pedal" : "Centered");
            break;
        case AxisField::StickDeadzone:
            snprintf(field_label, sizeof field_label, "Stick pair DZ %.2f",
                     profile_.stick_deadzone);
            break;
        }
        const int field_width = width - margin * 2 - gap * 3;
        const int cell = field_width / 3;
        add_key(margin, base_y, cell, btn_h, std::string("< ") + field_label + " >", false, 200);
        add_key(margin + cell + gap, base_y, cell / 2, btn_h, "-", false, 202);
        add_key(margin + cell + gap + cell / 2 + gap, base_y, cell / 2, btn_h, "+", false, 203);
        const int second_y = base_y + btn_h + gap;
        const int action_w = (width - margin * 2 - gap * 3) / 4;
        add_key(margin, second_y, action_w, btn_h, center_capture_ ? "Centering" : "Capture center",
                center_capture_, 206);
        add_key(margin + action_w + gap, second_y, action_w, btn_h,
                sweep_ ? "Finish sweep" : "Start sweep", sweep_, 207);
        add_key(margin + (action_w + gap) * 2, second_y, action_w, btn_h, "Cancel sweep", false,
                208);
        static const char *const preset_names[] = {"Native", "Centered", "Separate pedals",
                                                   "Combined pedals", "Keyboard"};
        const std::string preset_label =
            preset_ < 0 ? "Preset" : std::string("Preset: ") + preset_names[preset_];
        add_key(margin + (action_w + gap) * 3, second_y, action_w, btn_h, preset_label, false, 800);
        const int third_y = second_y + btn_h + gap;
        const int toggle_w = (width - margin * 2 - gap * 3) / 4;
        add_key(margin, third_y, toggle_w, btn_h,
                profile_.native_enabled ? "Joystick on" : "Joystick off", profile_.native_enabled,
                204);
        add_key(margin + toggle_w + gap, third_y, toggle_w, btn_h,
                profile_.legacy_mapped ? "Default keys on" : "Default keys off",
                profile_.legacy_mapped, 205);
        add_key(margin + (toggle_w + gap) * 2, third_y, toggle_w, btn_h, "Apply", false, 600);
        add_key(margin + (toggle_w + gap) * 3, third_y, toggle_w, btn_h, "Cancel", false, 601);
    } else if (page_ == Page::Bindings) {
        const int status_h =
            visible_status.empty() ? 0 : std::max(16, int(std::lround(18 * screen.scale)));
        if (status_h)
            add_key(margin, margin + top + gap, width - margin * 2, status_h, visible_status, false,
                    999);
        const int row_top = margin + top + gap + status_h + (status_h ? gap : 0);
        const int row_h = std::max(20, int(std::lround(24 * screen.scale)));
        const int visible = std::max(1, (height - row_top - footer - margin) / row_h);
        visible_rows_ = visible;
        scroll_ = clamp_int(scroll_, 0, std::max(0, 23 - visible));
        if (selected_binding_ < scroll_)
            scroll_ = selected_binding_;
        if (selected_binding_ >= scroll_ + visible)
            scroll_ = selected_binding_ - visible + 1;
        const int guest_w = width * 38 / 100;
        const int negative_w = width * 24 / 100;
        const int positive_w = width - margin * 2 - guest_w - negative_w - gap * 4;
        for (int row = scroll_; row < std::min(23, scroll_ + visible); ++row) {
            const int y = row_top + (row - scroll_) * row_h;
            const bool axis = row < 6;
            const int index = axis ? row : row < 19 ? row - 6 : row - 19;
            const int selected_part = selected_binding_ == row ? binding_part_ : -1;
            std::string title;
            if (axis)
                title = std::string(kAxisNames[index]) + " -B" +
                        guest_button_short(
                            profile_.axis_bindings[size_t(index)].negative_guest_buttons) +
                        " +B" +
                        guest_button_short(
                            profile_.axis_bindings[size_t(index)].positive_guest_buttons);
            else if (row < 19)
                title = kButtonNames[index];
            else
                title = kHatNames[index];
            add_key(margin, y, guest_w, row_h - gap, title, selected_binding_ == row, 300 + row);
            const int gx = margin + guest_w + gap;
            const int first_w = axis ? negative_w : negative_w + positive_w / 2;
            if (axis) {
                const AxisBinding &b = profile_.axis_bindings[size_t(index)];
                add_key(gx, y, first_w, row_h - gap,
                        std::string("Guest ") + kMapperAxes[int(b.guest_axis)] + " x" +
                            value_text(b.scale) + " " + (b.offset >= 0 ? "+" : "") +
                            value_text(b.offset),
                        selected_part == 0, 1000 + row * 10);
                add_key(gx + first_w + gap, y, positive_w / 2, row_h - gap,
                        std::string("- ") + key_name(b.negative_key), selected_part == 1,
                        2000 + row * 10 + 1);
                add_key(gx + first_w + gap + positive_w / 2 + gap, y, positive_w / 2, row_h - gap,
                        std::string("+ ") + key_name(b.positive_key), selected_part == 2,
                        2000 + row * 10 + 2);
            } else {
                const ButtonBinding &b =
                    row < 19 ? profile_.buttons[size_t(index)] : profile_.hat[size_t(index)];
                add_key(gx, y, first_w, row_h - gap, std::string("Guest ") + guest_button_name(b),
                        selected_part == 0, 1000 + row * 10);
                add_key(gx + first_w + gap, y, width - margin - (gx + first_w + gap), row_h - gap,
                        std::string("Key ") + key_name(b.key), selected_part == 1,
                        2000 + row * 10 + 1);
            }
        }
        const int base_y = height - footer + gap;
        const int btn_h = std::max(16, (footer - gap * 4) / 3);
        const int action_w = (width - margin * 2 - gap * 2) / 3;
        const char *part_label = selected_binding_ >= 6
                                     ? (binding_part_ == 0 ? "Part guest" : "Part key")
                                     : (binding_part_ == 0   ? "Part guest"
                                        : binding_part_ == 1 ? "Part - key"
                                        : binding_part_ == 2 ? "Part + key"
                                        : binding_part_ == 3 ? "Part scale"
                                        : binding_part_ == 4 ? "Part offset"
                                        : binding_part_ == 5 ? "Part -button"
                                                             : "Part +button");
        add_key(margin, base_y, action_w, btn_h, part_label, false, 201);
        add_key(margin + action_w + gap, base_y, action_w, btn_h, "-", false, 202);
        add_key(margin + (action_w + gap) * 2, base_y, action_w, btn_h, "+", false, 203);
        const int second_y = base_y + btn_h + gap;
        add_key(margin, second_y, action_w, btn_h, "Capture key", false, 501);
        add_key(margin + action_w + gap, second_y, action_w, btn_h, "Clear", false, 502);
        add_key(margin + (action_w + gap) * 2, second_y, action_w, btn_h,
                profile_.legacy_mapped ? "Default keys on" : "Default keys off",
                profile_.legacy_mapped, 205);
        const int third_y = second_y + btn_h + gap;
        add_key(margin, third_y, action_w, btn_h, "Apply", false, 600);
        add_key(margin + action_w + gap, third_y, action_w, btn_h, "Cancel", false, 601);
        add_key(margin + (action_w + gap) * 2, third_y, action_w, btn_h,
                profile_.native_enabled ? "Joystick on" : "Joystick off", profile_.native_enabled,
                204);
    } else {
        const int status_h =
            visible_status.empty() ? 0 : std::max(16, int(std::lround(18 * screen.scale)));
        if (status_h)
            add_key(margin, margin + top + gap, width - margin * 2, status_h, visible_status, false,
                    999);
        const int row_top = margin + top + gap + status_h + (status_h ? gap : 0);
        const int available_h = std::max(0, height - row_top - footer - margin);
        const int row_h = std::max(17, available_h / 7);
        const int visible_axes = std::max(1, std::min(int(kSdlAxisCount), available_h / row_h - 1));
        visible_rows_ = visible_axes;
        scroll_ = clamp_int(scroll_, 0, int(kSdlAxisCount) - visible_axes);
        const bool actual_seen = guest_actual.api != 0;
        const std::string api_name = !actual_seen            ? "No guest read yet"
                                     : guest_actual.api == 1 ? "DirectInput read"
                                                             : "XInput read";
        add_key(margin, row_top, width - margin * 2, row_h - gap,
                "Game starts after configuration; actual = last guest read / " + api_name +
                    (profile_.native_enabled ? " / preview is mapper output"
                                             : " / Joystick output off; preview is disabled"),
                false, 700);
        for (int i = scroll_; i < std::min(int(kSdlAxisCount), scroll_ + visible_axes); ++i) {
            const int y = row_top + row_h + (i - scroll_) * row_h;
            const int label_w = std::max(48, int(std::lround(58 * screen.scale)));
            const int meter_w = (width - margin * 2 - label_w - gap * 3) / 2;
            add_key(margin, y, label_w, row_h - gap, kGuestAxisNames[i], false, 900 + i);
            const float preview = axis_value(guest_preview, i);
            char actual_label[48];
            float actual_norm = 0;
            const int actual_axis = guest_actual.api == 1 ? native_axis_slot(i) : i;
            char slot[24];
            if (guest_actual.api == 2)
                snprintf(slot, sizeof slot, "XInput %s",
                         actual_axis == 0   ? "LX"
                         : actual_axis == 1 ? "LY"
                         : actual_axis == 2 ? "RX"
                         : actual_axis == 3 ? "RY"
                         : actual_axis == 4 ? "LT"
                                            : "RT");
            else
                snprintf(slot, sizeof slot, "DI %s", native_axis_name(actual_axis));
            if (actual_seen && guest_actual.present[actual_axis] &&
                guest_actual.maximum[actual_axis] != guest_actual.minimum[actual_axis]) {
                actual_norm = float((double(guest_actual.axes[actual_axis]) -
                                     double(guest_actual.minimum[actual_axis])) /
                                    (double(guest_actual.maximum[actual_axis]) -
                                     double(guest_actual.minimum[actual_axis])));
                actual_norm = std::max(0.0f, std::min(1.0f, actual_norm));
                if (i < 4)
                    actual_norm = actual_norm * 2.0f - 1.0f;
                snprintf(actual_label, sizeof actual_label, "%s %d [%d..%d]", slot,
                         guest_actual.axes[actual_axis], guest_actual.minimum[actual_axis],
                         guest_actual.maximum[actual_axis]);
            } else {
                snprintf(actual_label, sizeof actual_label, "ACT unavailable");
            }
            add_meter(margin + label_w + gap, y, meter_w, row_h - gap,
                      "Preview " + value_text(preview), preview, i < 4);
            add_meter(margin + label_w + gap * 2 + meter_w, y, meter_w, row_h - gap, actual_label,
                      actual_norm, i < 4);
        }
        const int info_y = height - footer + gap;
        char state_text[120];
        snprintf(state_text, sizeof state_text, "Buttons %04X  POV %04X  Packet %u",
                 guest_actual.buttons, guest_actual.pov, guest_actual.packet);
        const int btn_h = std::max(18, (footer - gap * 3) / 2);
        add_key(margin, info_y, width - margin * 2, btn_h, state_text, false, 701);
        const int btn_w = (width - margin * 2 - gap * 2) / 3;
        add_key(margin, info_y + btn_h + gap, btn_w, btn_h,
                guest_api_ == 0   ? "API auto"
                : guest_api_ == 1 ? "API DI"
                                  : "API XInput",
                false, 702);
        add_key(margin + btn_w + gap, info_y + btn_h + gap, btn_w, btn_h, "Apply", false, 600);
        add_key(margin + (btn_w + gap) * 2, info_y + btn_h + gap, btn_w, btn_h, "Cancel", false,
                601);
    }

    frame_.revision = revision_;
    frame_.layers[0].revision = revision_;
    return frame_;
}

void PadMapperEditor::pointer(double px, double py, int state) {
    if (!open_)
        return;
    if (state == 1) {
        pointer_down_ = true;
        for (const auto &widget : widget_ids_)
            if (widget.first.contains(px, py)) {
                activate(widget.second);
                return;
            }
    } else if (state < 0) {
        pointer_down_ = false;
    }
}

void PadMapperEditor::wheel(double notches) {
    if (!open_)
        return;
    int count = page_ == Page::Bindings ? 23 : int(kSdlAxisCount);
    const int visible = std::max(1, visible_rows_);
    scroll_ = clamp_int(scroll_ - int(std::lround(notches * 3)), 0,
                        std::max(0, count - std::min(count, visible)));
    if (page_ == Page::Calibration)
        selected_axis_ = clamp_int(selected_axis_, scroll_,
                                   std::min(int(kSdlAxisCount) - 1, scroll_ + visible - 1));
    changed();
}

void PadMapperEditor::escape() {
    if (!open_)
        return;
    if (capture_ != Capture::None) {
        capture_ = Capture::None;
        changed();
        return;
    }
    action_ = PadMapperEditorAction::Cancel;
}

void PadMapperEditor::key(int scancode, bool down) {
    if (!open_ || !down)
        return;
    if (capture_ != Capture::None) {
        if (scancode == kScanEscape) {
            capture_ = Capture::None;
        } else {
            const int key = (scancode == kScanBackspace || scancode == kScanDelete) ? 0 : scancode;
            if (key && !scancode_name(key)) {
                validation_error_ = "Unsupported key; press a supported key or Esc";
                changed();
                return;
            }
            if (capture_ == Capture::AxisNegative)
                profile_.axis_bindings[size_t(selected_binding_)].negative_key = key;
            else if (capture_ == Capture::AxisPositive)
                profile_.axis_bindings[size_t(selected_binding_)].positive_key = key;
            else if (selected_binding_ < 19)
                profile_.buttons[size_t(selected_binding_ - 6)].key = key;
            else
                profile_.hat[size_t(selected_binding_ - 19)].key = key;
            capture_ = Capture::None;
            validation_error_.clear();
        }
        changed();
        return;
    }
    if (scancode == kScanEscape) {
        escape();
        return;
    }
    if (scancode == kScan1)
        page_ = Page::Calibration;
    else if (scancode == kScan2)
        page_ = Page::Bindings;
    else if (scancode == kScan3)
        page_ = Page::GameView;
    else if (scancode == kScanReturn && page_ == Page::Bindings) {
        if (binding_part_ == 1 && selected_binding_ < 6)
            capture_ = Capture::AxisNegative;
        else if (binding_part_ == 2 && selected_binding_ < 6)
            capture_ = Capture::AxisPositive;
        else if (binding_part_ == 1)
            capture_ = Capture::ButtonKey;
    } else if (scancode == kScanTab && page_ == Page::Bindings) {
        binding_part_ = (binding_part_ + 1) % (selected_binding_ < 6 ? 7 : 2);
    } else if (scancode == kScanUp) {
        move_selection(-1);
    } else if (scancode == kScanDown) {
        move_selection(1);
    } else if (scancode == kScanLeft) {
        if (page_ == Page::Calibration)
            adjust(-1);
        else if (page_ == Page::Bindings)
            adjust(-1);
        else
            guest_api_ = (guest_api_ + 2) % 3;
    } else if (scancode == kScanRight) {
        if (page_ == Page::Calibration)
            adjust(1);
        else if (page_ == Page::Bindings)
            adjust(1);
        else
            guest_api_ = (guest_api_ + 1) % 3;
    } else if (scancode == kScanReturn && page_ == Page::Calibration) {
        adjust(1);
    } else if (scancode == kScanA) {
        action_ = PadMapperEditorAction::Apply;
    } else if (scancode == kScanP) {
        activate(800);
    }
    changed();
}

void PadMapperEditor::move_selection(int delta) {
    if (page_ == Page::Calibration) {
        if (sweep_) {
            profile_.axes[size_t(selected_axis_)].minimum = sweep_old_min_;
            profile_.axes[size_t(selected_axis_)].maximum = sweep_old_max_;
            profile_.axes[size_t(selected_axis_)].center = sweep_old_center_;
            sweep_ = false;
        }
        selected_axis_ = (selected_axis_ + delta + kSdlAxisCount) % kSdlAxisCount;
        const int available = std::max(1, visible_rows_);
        if (selected_axis_ < scroll_)
            scroll_ = selected_axis_;
        if (selected_axis_ >= scroll_ + available)
            scroll_ = selected_axis_ - available + 1;
    } else if (page_ == Page::Bindings) {
        selected_binding_ = (selected_binding_ + delta + 23) % 23;
        if (selected_binding_ < 6)
            binding_part_ = std::min(binding_part_, 6);
        else
            binding_part_ = std::min(binding_part_, 1);
        if (selected_binding_ < scroll_)
            scroll_ = selected_binding_;
        if (selected_binding_ >= scroll_ + visible_rows_)
            scroll_ = selected_binding_ - visible_rows_ + 1;
    }
}

void PadMapperEditor::adjust(int delta) {
    if (page_ == Page::Calibration) {
        AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
        switch (axis_field_) {
        case AxisField::Deadzone:
            c.deadzone =
                std::clamp(c.deadzone + delta * 0.01f, 0.0f, std::min(0.95f, c.saturation - 0.01f));
            break;
        case AxisField::Saturation:
            c.saturation = std::clamp(c.saturation + delta * 0.01f, c.deadzone + 0.01f, 1.0f);
            break;
        case AxisField::Curve:
            c.curve = std::clamp(c.curve + delta * 0.1f, 0.1f, 5.0f);
            break;
        case AxisField::Inverted:
            c.inverted = !c.inverted;
            break;
        case AxisField::Mode:
            c.mode = c.mode == MapperAxisMode::Centered ? MapperAxisMode::Pedal
                                                        : MapperAxisMode::Centered;
            if (c.mode == MapperAxisMode::Pedal) {
                c.minimum = selected_axis_ >= 4 ? 0.0f : -1.0f;
                c.center = c.minimum;
            } else if (selected_axis_ >= 4) {
                c.minimum = 0.0f;
                c.center = 0.5f;
            } else {
                c.minimum = -1.0f;
                c.center = 0.0f;
            }
            c.maximum = 1;
            break;
        case AxisField::StickDeadzone:
            profile_.stick_deadzone =
                std::clamp(profile_.stick_deadzone + delta * 0.01f, 0.0f, 0.95f);
            break;
        }
    } else if (page_ == Page::Bindings) {
        if (selected_binding_ < 6 && binding_part_ == 0) {
            AxisBinding &b = profile_.axis_bindings[size_t(selected_binding_)];
            int axis = int(b.guest_axis);
            axis = (axis + delta + kMapperAxisCount) % kMapperAxisCount;
            b.guest_axis = MapperAxis(axis);
        } else if (selected_binding_ < 6 && binding_part_ == 3) {
            AxisBinding &b = profile_.axis_bindings[size_t(selected_binding_)];
            b.scale = std::clamp(b.scale + delta * 0.05f, -4.0f, 4.0f);
        } else if (selected_binding_ < 6 && binding_part_ == 4) {
            AxisBinding &b = profile_.axis_bindings[size_t(selected_binding_)];
            b.offset = std::clamp(b.offset + delta * 0.05f, -1.0f, 1.0f);
        } else if (selected_binding_ < 6 && (binding_part_ == 5 || binding_part_ == 6)) {
            AxisBinding &b = profile_.axis_bindings[size_t(selected_binding_)];
            uint16_t &bits =
                binding_part_ == 5 ? b.negative_guest_buttons : b.positive_guest_buttons;
            int target = 0;
            for (int i = 0; i < 13; ++i)
                if (bits == kButtonBits[i])
                    target = i + 1;
            target = (target + delta + 14) % 14;
            bits = target ? kButtonBits[target - 1] : 0;
        } else if (selected_binding_ >= 6 && binding_part_ == 0) {
            ButtonBinding &b = selected_binding_ < 19
                                   ? profile_.buttons[size_t(selected_binding_ - 6)]
                                   : profile_.hat[size_t(selected_binding_ - 19)];
            int target = 0;
            if (b.guest_buttons) {
                for (int i = 0; i < 13; ++i)
                    if (b.guest_buttons == kButtonBits[i])
                        target = i + 1;
            } else if (b.guest_hat) {
                for (int i = 0; i < 4; ++i)
                    if (b.guest_hat == kHatBits[i])
                        target = i + 14;
            }
            target = (target + delta + 18) % 18;
            b.guest_buttons = target >= 1 && target <= 13 ? kButtonBits[target - 1] : 0;
            b.guest_hat = target >= 14 && target <= 17 ? kHatBits[target - 14] : 0;
        }
    }
    changed();
}

void PadMapperEditor::activate(int id) {
    if (id != 600)
        validation_error_.clear();
    if (id >= 1 && id <= 3) {
        if (sweep_) {
            profile_.axes[size_t(selected_axis_)].minimum = sweep_old_min_;
            profile_.axes[size_t(selected_axis_)].maximum = sweep_old_max_;
            profile_.axes[size_t(selected_axis_)].center = sweep_old_center_;
            sweep_ = false;
        }
        page_ = Page(id - 1);
        scroll_ = 0;
    } else if (id >= 100 && id < 106) {
        if (sweep_) {
            profile_.axes[size_t(selected_axis_)].minimum = sweep_old_min_;
            profile_.axes[size_t(selected_axis_)].maximum = sweep_old_max_;
            profile_.axes[size_t(selected_axis_)].center = sweep_old_center_;
            sweep_ = false;
        }
        center_capture_ = false;
        selected_axis_ = id - 100;
    } else if (id == 200) {
        axis_field_ = AxisField((int(axis_field_) + 1) % 6);
    } else if (id == 202 || id == 203) {
        adjust(id == 202 ? -1 : 1);
    } else if (id == 204) {
        profile_.native_enabled = !profile_.native_enabled;
    } else if (id == 205) {
        profile_.legacy_mapped = !profile_.legacy_mapped;
    } else if (id == 206) {
        center_capture_ = !center_capture_;
        center_sum_ = 0;
        center_samples_ = 0;
    } else if (id == 207) {
        if (sweep_) {
            sweep_ = false;
            AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
            c.minimum = c.mode == MapperAxisMode::Pedal
                            ? std::max(selected_axis_ >= 4 ? 0.0f : -1.0f, sweep_min_)
                            : sweep_min_;
            c.maximum = std::max(c.minimum + 0.001f, sweep_max_);
            if (c.mode == MapperAxisMode::Pedal)
                c.center = c.minimum;
        } else {
            const AxisCalibration &c = profile_.axes[size_t(selected_axis_)];
            sweep_old_min_ = c.minimum;
            sweep_old_max_ = c.maximum;
            sweep_old_center_ = c.center;
            sweep_min_ = sweep_max_ = axis_value(last_raw_, selected_axis_);
            sweep_ = true;
        }
    } else if (id == 208) {
        if (sweep_) {
            sweep_ = false;
            profile_.axes[size_t(selected_axis_)].minimum = sweep_old_min_;
            profile_.axes[size_t(selected_axis_)].maximum = sweep_old_max_;
            profile_.axes[size_t(selected_axis_)].center = sweep_old_center_;
        }
        center_capture_ = false;
    } else if (id == 300 || (id >= 300 && id < 323)) {
        const int row = id - 300;
        if (row < 23) {
            selected_binding_ = row;
            binding_part_ = 0;
            if (row < scroll_)
                scroll_ = row;
        }
    } else if (id >= 1000 && id < 1230) {
        const int row = (id - 1000) / 10;
        const int part = (id - 1000) % 10;
        if (row < 23) {
            selected_binding_ = row;
            binding_part_ = part;
            if (part == 0)
                adjust(1);
        }
    } else if (id >= 2000 && id < 2230) {
        const int row = (id - 2000) / 10;
        const int part = (id - 2000) % 10;
        if (row < 23) {
            selected_binding_ = row;
            binding_part_ = part;
            if (row < 6)
                capture_ = part == 1 ? Capture::AxisNegative : Capture::AxisPositive;
            else
                capture_ = Capture::ButtonKey;
        }
    } else if (id == 501) {
        if (selected_binding_ < 6 && binding_part_ == 1)
            capture_ = Capture::AxisNegative;
        else if (selected_binding_ < 6 && binding_part_ == 2)
            capture_ = Capture::AxisPositive;
        else if (selected_binding_ >= 6 && binding_part_ == 1)
            capture_ = Capture::ButtonKey;
        else
            validation_error_ = "Choose a key field with Part before capture";
    } else if (id == 502) {
        if (selected_binding_ < 6) {
            AxisBinding &b = profile_.axis_bindings[size_t(selected_binding_)];
            if (binding_part_ == 2)
                b.positive_key = 0;
            else if (binding_part_ == 1)
                b.negative_key = 0;
            else if (binding_part_ == 5)
                b.negative_guest_buttons = 0;
            else if (binding_part_ == 6)
                b.positive_guest_buttons = 0;
        } else if (selected_binding_ < 19) {
            profile_.buttons[size_t(selected_binding_ - 6)].key = 0;
        } else {
            profile_.hat[size_t(selected_binding_ - 19)].key = 0;
        }
    } else if (id == 600) {
        if (sweep_ || center_capture_) {
            validation_error_ = "Finish or cancel calibration before applying";
            changed();
            return;
        }
        action_ = PadMapperEditorAction::Apply;
    } else if (id == 601) {
        action_ = PadMapperEditorAction::Cancel;
    } else if (id == 700 || id == 702) {
        guest_api_ = (guest_api_ + 1) % 3;
    } else if (id == 800) {
        preset_ = (preset_ + 1) % 5;
        switch (preset_) {
        case 0:
            profile_ = pad_mapper_native_profile();
            break;
        case 1:
            profile_ = pad_mapper_centered_sticks();
            break;
        case 2:
            profile_ = pad_mapper_separate_pedals();
            break;
        case 3:
            profile_ = pad_mapper_combined_pedals();
            break;
        case 4: {
            profile_ = pad_mapper_default_profile();
            profile_.native_enabled = false;
            profile_.legacy_mapped = true;
            break;
        }
        }
    }
    if (id == 201) {
        const int count = selected_binding_ < 6 ? 7 : 2;
        binding_part_ = (binding_part_ + 1) % count;
    }
    changed();
}

PadMapperEditorAction PadMapperEditor::take_action() {
    const PadMapperEditorAction action = action_;
    action_ = PadMapperEditorAction::None;
    if (action == PadMapperEditorAction::Apply) {
        std::string error;
        if (!validate_pad_mapper_profile(profile_, &error)) {
            validation_error_ = std::move(error);
            changed();
            return PadMapperEditorAction::None;
        }
        validation_error_.clear();
    } else if (action == PadMapperEditorAction::Cancel) {
        open_ = false;
    }
    return action;
}

} // namespace controls
