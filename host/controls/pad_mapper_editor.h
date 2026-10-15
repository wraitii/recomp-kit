#pragma once

#include "overlay.h"
#include "pad_mapper.h"

#include <string>
#include <utility>
#include <vector>

struct HostPadGuestState;

namespace controls {

enum class PadMapperEditorAction { None, Apply, Cancel };

class PadMapperEditor {
  public:
    void open(const PadMapperProfile &profile, const Screen &screen);
    void close();
    void set_screen(const Screen &screen);
    bool is_open() const;
    const PadMapperProfile &profile() const;
    ControlsView view(const Screen &screen, const PadState &raw, const PadState &calibrated,
                      const PadState &guest_preview, const ::HostPadGuestState &guest_actual,
                      const std::string &status);
    void pointer(double px, double py, int state); // -1 up, 0 move, 1 down
    void wheel(double notches);
    void escape();
    void key(int scancode, bool down);
    PadMapperEditorAction take_action();
    uint32_t guest_api() const;

  private:
    enum class Page { Calibration, Bindings, GameView };
    enum class Capture { None, AxisNegative, AxisPositive, ButtonKey };
    enum class AxisField { Deadzone, Saturation, Curve, Inverted, Mode, StickDeadzone };

    void changed();
    void activate(int id);
    void move_selection(int delta);
    void adjust(int delta);
    void add_key(int x, int y, int w, int h, const std::string &label, bool lit, int id);
    void add_meter(int x, int y, int w, int h, const std::string &label, float value, bool bipolar);

    PadMapperProfile profile_;
    Screen screen_;
    Page page_ = Page::Calibration;
    AxisField axis_field_ = AxisField::Deadzone;
    Capture capture_ = Capture::None;
    PadMapperEditorAction action_ = PadMapperEditorAction::None;
    std::string validation_error_;
    bool open_ = false;
    bool pointer_down_ = false;
    bool sweep_ = false;
    bool center_capture_ = false;
    double center_sum_ = 0;
    unsigned center_samples_ = 0;
    float sweep_min_ = 0, sweep_max_ = 0;
    float sweep_old_min_ = 0, sweep_old_max_ = 1;
    float sweep_old_center_ = 0;
    int selected_axis_ = 0;
    int selected_binding_ = 0;
    int binding_part_ = 0;
    int scroll_ = 0;
    int visible_rows_ = 1;
    uint32_t guest_api_ = 0;
    int preset_ = -1;
    uint64_t last_guest_view_hash_ = 0;
    bool have_telemetry_ = false;
    PadState last_raw_, last_calibrated_, last_guest_;
    uint64_t revision_ = 1;
    ControlsView frame_;
    std::vector<std::pair<Rect, int>> widget_ids_;
};

} // namespace controls
