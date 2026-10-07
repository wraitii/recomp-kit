#include "../input_script.h"
#include <cstdio>

int main() {
    HostInputScript script;
    std::string error;
    int presses = 0, releases = 0, keys = 0;
    auto deliver = [&](const HostScriptStep &s) {
        if (s.op == HOST_SCRIPT_CLICK) {
            if (s.down)
                ++presses;
            else
                ++releases;
        } else if (s.op == HOST_SCRIPT_KEY)
            ++keys;
    };
    auto require = [](bool ok, const char *message) {
        if (!ok)
            fprintf(stderr, "%s\n", message);
        return ok;
    };
    if (!require(script.parse("wait 100\nclick left 20 30\nwait 200\nkey ESCAPE down\n", error),
                 "parse"))
        return 1;
    script.tick(1000, 0, 50, deliver);
    script.tick(1100, 1, 50, deliver);
    // A pinned clock's stall breaker can advance without any present.
    script.tick(5000, 1, 50, deliver);
    if (!require(presses == 1 && releases == 0 && keys == 0,
                 "clock polls must not release a click"))
        return 1;
    script.tick(5100, 5, 50, deliver);
    script.tick(5299, 6, 50, deliver);
    if (!require(releases == 1 && keys == 0, "wait after delayed release must be retained"))
        return 1;
    script.tick(5300, 7, 50, deliver);
    if (!require(keys == 1 && script.finished(), "complete after wait"))
        return 1;
    if (!require(!script.parse("expect textures>0\n", error), "reject unsupported operations"))
        return 1;
    if (!require(!script.parse("key NONEXISTENT down\n", error), "reject malformed input"))
        return 1;
    // Unsigned clocks and present counters may wrap.
    if (!script.parse("click left 1 2\n", error))
        return 1;
    script.tick(0xfffffff0u, 0xfffffffeu, 50, deliver);
    script.tick(100, 1, 50, deliver);
    if (!require(!script.finished(), "four presents across wrap"))
        return 1;
    script.tick(101, 2, 50, deliver);
    if (!require(script.finished(), "release across wrap"))
        return 1;
    puts("input script tests passed");
}
