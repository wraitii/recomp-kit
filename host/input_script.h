// Timed, game-independent input subset of the smoke script language.
#pragma once
#include "script.h"
#include <functional>
#include <string>
#include <vector>

class HostInputScript {
  public:
    bool parse(const char *text, std::string &error);
    void tick(uint32_t ms, uint32_t presents, uint32_t clock_step,
              const std::function<void(const HostScriptStep &)> &deliver);
    bool finished() const {
        return next_ == steps_.size() && !held_;
    }

  private:
    std::vector<HostScriptStep> steps_;
    size_t next_ = 0;
    bool started_ = false, held_ = false;
    uint32_t start_ = 0, release_ = 0, hold_start_ = 0;
    HostScriptStep held_step_{};
};

// Called before boot and under the guest scheduler baton, respectively.
bool host_input_script_load();
void host_input_script_tick(uint32_t presents);
