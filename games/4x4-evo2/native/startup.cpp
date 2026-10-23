#include "platform/os.h"
#include "x86.h"
#include <cstring>

extern "C" void fn_00491d00(X86 *c);

// 0x00491d00 shows the startup "4x4 Setup Options" menu. Its defaults come
// from system/metal.ini and are held in these globals. DIVERGENCE(original):
// lock the renderer to Direct3D8 Hardware TnL and the color depth to 32 while
// leaving the rest of the menu and the saved resolution unchanged. The outer
// jump table entries for Color Depth at 0x00491cd8 and Renderer at 0x00491cdc
// are routed to the common redraw path at 0x00492216. Set RECOMP_EVO2_GRAPHICS=0
// to keep the original behavior.

static bool forcing_graphics() {
    static const bool enabled = [] {
        const char *value = recomp_env("EVO2_GRAPHICS");
        return !value || std::strcmp(value, "0") != 0;
    }();
    return enabled;
}

static void copy_string(uint32_t address, const char *text) {
    do {
        wr8(address++, static_cast<uint8_t>(*text));
    } while (*text++);
}

extern "C" void evo2_force_startup_graphics(X86 *c) {
    if (!forcing_graphics()) {
        fn_00491d00(c);
        return;
    }

    wr32(0x00672a70, 32);
    wr32(0x00d44cac, 1);
    copy_string(0x0067ced0, "tridx8tl.dll");
    const uint32_t color_depth_case = rd32(0x00491cd8);
    const uint32_t renderer_case = rd32(0x00491cdc);
    wr32(0x00491cd8, 0x00492216);
    wr32(0x00491cdc, 0x00492216);
    fn_00491d00(c);
    wr32(0x00491cd8, color_depth_case);
    wr32(0x00491cdc, renderer_case);
}
