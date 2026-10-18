#include <stdint.h>

#include <string>

#include "runtime/x86.h"

extern "C" void harness_escape(const char *reason, uint32_t addr);

void guest_sleep_ms(uint32_t ms) {
    harness_escape("sleep", ms);
}

bool guest_key_names_system() {
    return false;
}

std::string guest_key_name(uint32_t dik) {
    (void)dik;
    return std::string();
}

extern "C" uint8_t recomp_observe_on = 0;
extern "C" uint32_t recomp_observe_site = 0;
extern "C" uint32_t recomp_dispatch_epoch = 1;
extern "C" void observe_record(uint32_t site, uint32_t target, const char *kind) {
    (void)site;
    (void)target;
    (void)kind;
}
extern "C" void observe_write(void) {}
extern "C" void recomp_icall_slow(X86 *c, uint32_t target, RecompICache *ic, uint32_t site) {
    (void)ic;
    (void)site;
    recomp_unknown_call(c, target);
}
extern "C" void recomp_module_invoke(X86 *c, const RecompModule *m, int32_t i) {
    recomp_unknown_call(c, m->func_addrs[i]);
}
