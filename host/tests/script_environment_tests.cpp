// CPU-only parser checks, executable without Metal or CoreAudio.
#include "../script.h"
#include <cassert>
#include <cstdio>
#include <string>

int main() {
    HostScriptStep steps[32]{};
    char error[256]{};
    auto parse = [&](const char *text) {
        return host_script_parse(text, steps, 32, error, sizeof error);
    };
    assert(parse("dumpat c840 command_frame>=840\ndumpat command_frame>=880 c880\n") == 2);
    for (int i = 0; i < 2; ++i) {
        assert(steps[i].op == HOST_SCRIPT_DUMPAT && steps[i].at_least);
        assert(std::string(steps[i].name) == "command_frame");
        assert(steps[i].threshold == (i ? 880 : 840));
        assert(std::string(steps[i].text) == (i ? "c880" : "c840"));
    }
    assert(parse("wait 10\nmove 2 3\nclick left 2 3\n") == 3);
    for (const char *game_specific :
         {"camera 1 2\n", "viewmove 1 2\n", "watch 0 1\n", "landmark 1 expect hidden\n",
          "simdump frame\n", "click entity 1\n", "click world 1 2 3\n"}) {
        assert(parse(game_specific) == -1);
        assert(std::string(error).find("unsupported game-specific") != std::string::npos);
    }
    puts("generic smoke parser checks passed");
}
