#include <stdint.h>

extern "C" void harness_escape(const char *reason, uint32_t addr);

void guest_sleep_ms(uint32_t ms) {
    harness_escape("sleep", ms);
}
