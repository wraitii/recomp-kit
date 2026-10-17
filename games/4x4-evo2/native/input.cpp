#include "frame_pacing.h"
#include "platform/os.h"
#include "win32.h"
#include <cstring>

extern "C" void fn_004823d0(X86 *c);

// BUGFIX(original): 0x00481760 caches the button count before 0x004823d0
// restores bindings. Match 0x004839c0's recount after a successful load;
// traces show D-pad slots 13..16 skipped by 0x0048ac00 with cached count 13.
void evo2_load_input_bindings(X86 *c) {
    fn_004823d0(c);
    static const bool fix = [] {
        const char *value = recomp_env("INPUT_FIXUP");
        return !value || std::strcmp(value, "0") != 0;
    }();
    if (!fix || !(c->r[R_EAX] & 0xffu))
        return;
    uint32_t count = 0;
    for (uint32_t i = 0; i < 32; ++i)
        count += rd32(0x00aa5324 + i * 4) != 0;
    wr32(0x00aa5430, count);
}
