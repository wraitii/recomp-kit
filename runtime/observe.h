#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifdef RECOMP_NO_OBSERVE
#define recomp_observe_on 0
static inline void observe_indirect(uint32_t, uint32_t, const char *) {}
static inline void observe_pend(uint32_t) {}
static inline uint32_t observe_take(void) {
    return 0;
}
static inline void observe_write(void) {}
#else
extern uint8_t recomp_observe_on;
extern uint32_t recomp_observe_site;
void observe_record(uint32_t site, uint32_t target, const char *kind);
void observe_write(void);
static inline void observe_indirect(uint32_t site, uint32_t target, const char *kind) {
    if (recomp_observe_on && site)
        observe_record(site, target, kind);
}
static inline void observe_pend(uint32_t site) {
    if (recomp_observe_on)
        recomp_observe_site = site;
}
static inline uint32_t observe_take(void) {
    if (!recomp_observe_on)
        return 0;
    uint32_t site = recomp_observe_site;
    recomp_observe_site = 0;
    return site;
}
#endif

#ifdef __cplusplus
}
#endif
