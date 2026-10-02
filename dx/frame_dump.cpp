#include "frame_dump.h"
#include "../platform/os.h"
#include "../third_party/miniz/miniz.h"

#include <cstdio>
#include <cstdlib>
#include <limits>
#include <string>
#include <vector>

void dx_dump_frame_rgba(const uint8_t *rgba, uint32_t width, uint32_t height) {
    static const std::string directory = [] {
        const char *value = getenv("RECOMP_DUMP_FRAME_DIR");
        std::string dir = value ? value : "";
        if (!dir.empty()) {
            std::string prefix;
            for (size_t i = 0; i <= dir.size(); ++i) {
                if ((i == dir.size() || dir[i] == '/') && !prefix.empty())
                    os_mkdir(prefix.c_str());
                if (i < dir.size())
                    prefix.push_back(dir[i]);
            }
            printf("[frame-dump] writing PNGs to %s\n", dir.c_str());
        }
        return dir;
    }();
    if (directory.empty())
        return;
    if (!rgba || !width || !height || width > INT32_MAX || height > INT32_MAX ||
        uint64_t(width) * height > std::numeric_limits<size_t>::max() / 4)
        return;
    const size_t pixels = size_t(width) * height;
    std::vector<uint8_t> rgb(pixels * 3);
    for (size_t i = 0; i < pixels; ++i) {
        rgb[i * 3] = rgba[i * 4];
        rgb[i * 3 + 1] = rgba[i * 4 + 1];
        rgb[i * 3 + 2] = rgba[i * 4 + 2];
    }
    size_t size = 0;
    void *png =
        tdefl_write_image_to_png_file_in_memory(rgb.data(), int(width), int(height), 3, &size);
    static uint32_t serial = 0; // Called on the guest scheduler baton.
    const std::string path = directory + "/frame_" + [&] {
        char number[32];
        snprintf(number, sizeof number, "%05u", serial++);
        return std::string(number);
    }() + ".png";
    FILE *file = png ? fopen(path.c_str(), "wb") : nullptr;
    bool ok = file && fwrite(png, 1, size, file) == size;
    if (file && fclose(file) != 0)
        ok = false;
    mz_free(png);
    if (!ok)
        fprintf(stderr, "[frame-dump] cannot write %s\n", path.c_str());
}
