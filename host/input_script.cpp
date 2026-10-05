#include "input_script.h"

bool HostInputScript::parse(const char *text, std::string &error) {
    *this = HostInputScript{};
    error.clear();
    steps_.resize(4096);
    char diagnostic[256];
    int count =
        host_script_parse(text, steps_.data(), (int)steps_.size(), diagnostic, sizeof diagnostic);
    if (count < 0) {
        error = diagnostic;
        steps_.clear();
        return false;
    }
    steps_.resize(count);
    for (const auto &step : steps_) {
        switch (step.op) {
        case HOST_SCRIPT_MOVE:
        case HOST_SCRIPT_MOVEBY:
        case HOST_SCRIPT_CLICK:
        case HOST_SCRIPT_GUESTCLICK:
        case HOST_SCRIPT_BUTTON:
        case HOST_SCRIPT_KEY:
        case HOST_SCRIPT_QUIT:
            break;
        default:
            error = "unsupported input script operation at step " +
                    std::to_string(&step - steps_.data() + 1);
            steps_.clear();
            return false;
        }
    }
    return true;
}

// A held click blocks later steps. Its time is removed from the script clock,
// preserving the waits after it even if several steps were overdue at a tick.
// Release needs completed presents, so a clock stall breaker cannot swallow it.
void HostInputScript::tick(uint32_t ms, uint32_t presents, uint32_t clock_step,
                           const std::function<void(const HostScriptStep &)> &deliver) {
    if (!started_) {
        started_ = true;
        start_ = ms;
    }
    if (held_) {
        if ((int32_t)(presents - release_) < 0)
            return;
        held_ = false;
        held_step_.down = 0;
        deliver(held_step_);
        start_ += ms - hold_start_;
        return;
    }
    if (next_ >= steps_.size() || ms - start_ < steps_[next_].at_ms)
        return;
    auto step = steps_[next_++];
    if (step.op == HOST_SCRIPT_CLICK || step.op == HOST_SCRIPT_GUESTCLICK) {
        step.down = 1;
        held_step_ = step;
        held_ = true;
        hold_start_ = ms;
        release_ = presents +
                   host_script_input_hold_frames(step.press_ms ? step.press_ms : 120, clock_step);
    }
    deliver(step);
    if (step.op == HOST_SCRIPT_QUIT)
        next_ = steps_.size();
}
