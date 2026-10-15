#pragma once

#include "binding.h"
#include "vpad.h"

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace controls {

enum class MapperAxis : uint8_t { None, LX, LY, RX, RY, L2, R2 };
enum class MapperAxisMode : uint8_t { Centered, Pedal };

struct AxisCalibration {
    float minimum = -1;
    float center = 0;
    float maximum = 1;
    float deadzone = 0;
    float saturation = 1;
    float curve = 1;
    bool inverted = false;
    MapperAxisMode mode = MapperAxisMode::Centered;
};

struct AxisBinding {
    MapperAxis guest_axis = MapperAxis::None;
    int negative_key = 0;
    int positive_key = 0;
    float scale = 1;
    float offset = 0;
    uint16_t negative_guest_buttons = 0;
    uint16_t positive_guest_buttons = 0;
};

struct ButtonBinding {
    uint16_t guest_buttons = 0;
    uint8_t guest_hat = 0;
    int key = 0;
};

struct PadMapperProfile {
    uint32_t version = 1;
    bool legacy_mapped = false;
    bool native_enabled = true;
    float stick_deadzone = 0.08f;
    std::array<AxisCalibration, kSdlAxisCount> axes{};
    std::array<AxisBinding, kSdlAxisCount> axis_bindings{};
    std::array<ButtonBinding, 13> buttons{};
    std::array<ButtonBinding, 4> hat{}; // up, right, down, left
};

PadMapperProfile pad_mapper_default_profile();
PadMapperProfile pad_mapper_centered_sticks();
PadMapperProfile pad_mapper_separate_pedals();
PadMapperProfile pad_mapper_combined_pedals();
PadMapperProfile pad_mapper_keyboard_profile(const MappedTable &legacy_defaults);
PadMapperProfile pad_mapper_native_profile();

void set_pad_mapper_runtime_mode(int mode);
int pad_mapper_runtime_mode(int fallback);

bool validate_pad_mapper_profile(const PadMapperProfile &profile, std::string *error = nullptr);
std::string serialize_pad_mapper_profile(const PadMapperProfile &profile,
                                         std::string *error = nullptr);
bool parse_pad_mapper_profile(const std::string &text, PadMapperProfile *profile,
                              std::string *error = nullptr);

class PadMapper {
  public:
    PadMapper();

    bool set_profile(const PadMapperProfile &profile, std::string *error = nullptr);
    PadMapperProfile snapshot() const;

    // `input` is canonical SDL-free raw physical state from the host.
    // Emits key press/release edges in TouchAction form.
    void process(const PadState &input, PadState *calibrated, PadState *guest,
                 std::vector<TouchAction> *key_edges);
    void release(std::vector<TouchAction> *key_edges);

  private:
    mutable std::mutex mutex_;
    PadMapperProfile profile_;
    std::vector<int> held_keys_;
};

} // namespace controls
