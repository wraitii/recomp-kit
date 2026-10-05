#pragma once
// Profiler time spans for samply (https://github.com/mstange/samply).
//
// Samply's macOS preload notices a process opening `marker-<pid>.txt` and
// reads it as `<start_ns> <end_ns> <name>` lines on the mach uptime clock,
// which is what os_monotonic_ns returns on Darwin. The spans then show in the
// Firefox Profiler marker chart and table, so a sampled profile carries real
// frame boundaries (frame count and time are readable per selection).
//
// Off unless running under samply (SAMPLY_BOOTSTRAP_SERVER_NAME is set) or
// RECOMP_PROFILE_MARKERS=1. When off, every call is a flag test.
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int profile_markers_enabled(void);
// os_monotonic_ns when enabled, else 0: spans started while off are dropped.
uint64_t profile_marker_now(void);
// A span that started at start_ns (from profile_marker_now) and ends now.
void profile_marker_end(const char *name, uint64_t start_ns);
// Marks a frame boundary: closes the span "frame" opened by the previous call.
void profile_marker_frame(void);

#ifdef __cplusplus
}
#endif
