#ifndef RECOMP_NO_OBSERVE
#include "observe.h"

#include "../platform/os.h"
#include "imports.h"

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

void put_hex8(char *&p, uint32_t v) {
    static const char digits[] = "0123456789abcdef";
    for (int shift = 28; shift >= 0; shift -= 4)
        *p++ = digits[(v >> shift) & 0xf];
}

void put_dec(char *&p, uint64_t v) {
    char tmp[20];
    int n = 0;
    do {
        tmp[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n)
        *p++ = tmp[--n];
}

void put_str(char *&p, const char *s) {
    while (*s)
        *p++ = *s++;
}

// Raw descriptor writes, so a fatal-signal handler can use this without stdio.
void write_locked() {
    int fd = os_fd_open(g_observe_path, OS_O_WRONLY | OS_O_CREAT | OS_O_TRUNC);
    if (fd < 0)
        return;
    char line[512];
    for (const auto &e : g_observe_counts) {
        const std::string &kind = std::get<2>(e.first);
        const char *desc = kind == "shim" ? imports_describe(std::get<1>(e.first)) : nullptr;
        char *p = line;
        put_hex8(p, std::get<0>(e.first));
        *p++ = ' ';
        put_hex8(p, std::get<1>(e.first));
        *p++ = ' ';
        put_str(p, kind.c_str());
        *p++ = ' ';
        put_dec(p, e.second);
        *p++ = ' ';
        if (desc)
            put_str(p, desc);
        *p++ = '\n';
        os_fd_write(fd, line, (size_t)(p - line));
    }
    os_fd_close(fd);
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

extern "C" void observe_write_on_fault(void) {
    if (!recomp_observe_on)
        return;
    std::unique_lock<std::mutex> lock(g_observe_mutex, std::try_to_lock);
    if (!lock.owns_lock())
        return;
    write_locked();
}
#endif
