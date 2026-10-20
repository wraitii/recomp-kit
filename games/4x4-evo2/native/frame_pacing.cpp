#include "frame_pacing.h"
#include "platform/os.h"
#include "win32.h"
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>

// 0x00495d10 completes hardware/software frames and pumps messages. Its
// gameplay caller samples getTime (0x0060b020) immediately afterward at
// 0x004639a7. DIVERGENCE(original): pace this boundary to protect
// frame-sensitive physics. RECOMP_EVO2_FPS=0 retains the translated original
// without pacing.
extern "C" void fn_00495d10(X86 *c);

static uint32_t frame_rate() {
    const char *value = recomp_env("EVO2_FPS");
    if (!value)
        value = "60";
    char *end;
    errno = 0;
    long rate = std::strtol(value, &end, 10);
    if (errno || end == value || *end || rate < 0 || rate > 1000) {
        std::fprintf(stderr, "evo2: RECOMP_EVO2_FPS must be an integer from 0 to 1000\n");
        std::abort();
    }
    std::fprintf(stderr, "[recomp] evo2: guest frame cap %ld FPS (0 means uncapped)\n", rate);
    return uint32_t(rate);
}

void evo2_complete_frame(X86 *c) {
    static const uint32_t rate = frame_rate();
    static uint64_t deadline;
    if (!rate) {
        fn_00495d10(c);
        return;
    }
    const uint64_t period = 1000000000ull / rate;
    uint64_t now = os_monotonic_ns();
    if (!deadline || now > deadline + period)
        deadline = now + period;
    else
        deadline = std::min(deadline + period, now + period);
    const uint64_t target = deadline;
    fn_00495d10(c);
    while ((now = os_monotonic_ns()) < target)
        guest_sleep_ms(uint32_t(std::min<uint64_t>((target - now + 999999) / 1000000, 100)));
}
