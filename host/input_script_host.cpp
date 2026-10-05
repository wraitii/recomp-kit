#include "input_script.h"
#include "input.h"
#include "input_gate.h"
#include "boot.h"
#include "../runtime/win32.h"
#include "../platform/os.h"
#include <cstdio>
#include <fstream>
#include <iterator>

namespace {
HostInputScript script;
bool enabled = false;
int32_t x = 0, y = 0;
void deliver(const HostScriptStep &step) {
    printf("[input-script] op=%d at=%u x=%d y=%d down=%d key=%s\n", step.op, step.at_ms, step.x,
           step.y, step.down, step.name);
    switch (step.op) {
    case HOST_SCRIPT_MOVE:
    case HOST_SCRIPT_MOVEBY: {
        int32_t dx =
            step.op == HOST_SCRIPT_MOVEBY ? step.x : (int32_t)((uint32_t)step.x - (uint32_t)x);
        int32_t dy =
            step.op == HOST_SCRIPT_MOVEBY ? step.y : (int32_t)((uint32_t)step.y - (uint32_t)y);
        x = step.op == HOST_SCRIPT_MOVEBY ? (int32_t)((uint32_t)x + dx) : step.x;
        y = step.op == HOST_SCRIPT_MOVEBY ? (int32_t)((uint32_t)y + dy) : step.y;
        if (!host_gate_motion(x, y, dx, dy)) {
            host_input_motion(x, y, dx, dy);
            host_post_client_mouse_message(host_main_window(), 0x200, 0, (int16_t)x, (int16_t)y);
        }
        break;
    }
    case HOST_SCRIPT_CLICK:
    case HOST_SCRIPT_GUESTCLICK:
        // Ordinary click supplies relative motion as a physical mouse does;
        // guestclick uses the existing absolute guest-coordinate injection.
        if (step.down && step.op == HOST_SCRIPT_CLICK &&
            !host_gate_motion(step.x, step.y, (int32_t)((uint32_t)step.x - (uint32_t)x),
                              (int32_t)((uint32_t)step.y - (uint32_t)y)))
            host_input_motion(step.x, step.y, (int32_t)((uint32_t)step.x - (uint32_t)x),
                              (int32_t)((uint32_t)step.y - (uint32_t)y));
        x = step.x;
        y = step.y;
        host_gate_inject_guest_click(x, y, step.button, step.down != 0);
        break;
    case HOST_SCRIPT_BUTTON:
        host_gate_inject_guest_click(x, y, step.button, step.down != 0);
        break;
    case HOST_SCRIPT_KEY:
        host_key_event(host_key_mapping_for_dik(step.dik).mac, step.down != 0, 0);
        break;
    case HOST_SCRIPT_QUIT:
        host_input_release_all();
        boot_request_close("input script quit");
        break;
    }
    fflush(stdout);
}
} // namespace

bool host_input_script_load() {
    const char *path = recomp_env("INPUT_SCRIPT");
    if (!path || !*path)
        return true;
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        fprintf(stderr, "input-script: cannot open %s\n", path);
        return false;
    }
    std::string text((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    std::string error;
    if (!script.parse(text.c_str(), error)) {
        fprintf(stderr, "input-script: %s: %s\n", path, error.c_str());
        return false;
    }
    enabled = true;
    printf("[input-script] loaded %s\n", path);
    return true;
}

void host_input_script_tick(uint32_t presents) {
    if (enabled && !boot_close_requested())
        script.tick(boot_guest_millis(), presents, host_pinned_clock_step(), deliver);
}
