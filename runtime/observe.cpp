#ifndef RECOMP_NO_OBSERVE
#include "observe.h"

#include "../platform/os.h"
#include "imports.h"

#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>
#include <tuple>

extern "C" {
uint8_t recomp_observe_on = 0;
uint32_t recomp_observe_site = 0;
}

namespace {
typedef std::tuple<uint32_t, uint32_t, std::string> ObserveKey;
std::mutex g_observe_mutex;
std::map<ObserveKey, uint64_t> g_observe_counts;
const char *g_observe_path = nullptr;

void write_locked() {
    FILE *f = std::fopen(g_observe_path, "w");
    if (!f)
        return;
    for (const auto &e : g_observe_counts) {
        const std::string &kind = std::get<2>(e.first);
        const char *desc = kind == "shim" ? imports_describe(std::get<1>(e.first)) : nullptr;
        std::fprintf(f, "%08x %08x %s %llu %s\n", std::get<0>(e.first), std::get<1>(e.first),
                     kind.c_str(), (unsigned long long)e.second, desc ? desc : "");
    }
    std::fclose(f);
}

struct ObserveEnable {
    ObserveEnable() {
        const char *path = recomp_env("OBSERVE");
        if (!path || !*path)
            return;
        g_observe_path = path;
        recomp_observe_on = 1;
        std::atexit(observe_write);
    }
} g_observe_enable;
} // namespace

extern "C" void observe_record(uint32_t site, uint32_t target, const char *kind) {
    std::lock_guard<std::mutex> lock(g_observe_mutex);
    ++g_observe_counts[ObserveKey(site, target, kind)];
}

extern "C" void observe_write(void) {
    if (!recomp_observe_on)
        return;
    std::lock_guard<std::mutex> lock(g_observe_mutex);
    write_locked();
}
#endif
