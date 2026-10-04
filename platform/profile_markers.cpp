// profile_markers.cpp - samply marker-file spans; see profile_markers.h.
#include "platform/profile_markers.h"
#include "platform/os.h"

#include <mutex>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace {

std::mutex g_mutex;
FILE *g_file = nullptr;
bool g_enabled = false;
uint64_t g_last_frame_ns = 0;
unsigned g_unflushed = 0;

void close_file() {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_file) {
        fclose(g_file);
        g_file = nullptr;
    }
}

bool open_file() {
    const char *rc = recomp_env("PROFILE_MARKERS");
    const bool forced = rc && *rc && strcmp(rc, "0") != 0;
    if (!forced && !getenv("SAMPLY_BOOTSTRAP_SERVER_NAME"))
        return false;
    char path[4200];
    snprintf(path, sizeof path, "%s/marker-%lld.txt", os_temp_dir(), (long long)os_process_id());
    // The preload hooks open/fopen and reads the file name, so this call is
    // what registers the file with samply.
    g_file = fopen(path, "w");
    if (!g_file)
        return false;
    atexit(close_file);
    return true;
}

void write_span(const char *name, uint64_t start_ns, uint64_t end_ns) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_file)
        return;
    fprintf(g_file, "%llu %llu %s\n", (unsigned long long)start_ns, (unsigned long long)end_ns,
            name);
    // Samply reads the file when the process ends, but a crash should still
    // leave most of it behind.
    if (++g_unflushed >= 32) {
        fflush(g_file);
        g_unflushed = 0;
    }
}

} // namespace

extern "C" {

int profile_markers_enabled(void) {
    static const bool once = (g_enabled = open_file());
    (void)once;
    return g_enabled;
}

uint64_t profile_marker_now(void) {
    return profile_markers_enabled() ? os_monotonic_ns() : 0;
}

void profile_marker_end(const char *name, uint64_t start_ns) {
    if (!start_ns)
        return;
    write_span(name, start_ns, os_monotonic_ns());
}

void profile_marker_frame(void) {
    if (!profile_markers_enabled())
        return;
    const uint64_t now = os_monotonic_ns();
    if (g_last_frame_ns)
        write_span("frame", g_last_frame_ns, now);
    g_last_frame_ns = now;
}

} // extern "C"
