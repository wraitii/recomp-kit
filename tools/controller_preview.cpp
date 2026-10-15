#include "host/controls/overlay_paint.h"
#include "host/controls/pad_mapper_editor.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: controller_preview OUTPUT.ppm "
                        "overview|mapping|axis|advanced|calibration|response|presets|discard|"
                        "disconnected [width height scale]\n");
        return 2;
    }
    const std::string page = argv[2];
    const int width = argc > 3 ? std::max(640, atoi(argv[3])) : 800;
    const int height = argc > 4 ? std::max(520, atoi(argv[4])) : 620;
    const double scale = argc > 5 ? std::max(0.75, atof(argv[5])) : 1.0;
    controls::Screen screen{width, height, scale, {0, 0, width, height}, {}};
    controls::PadMapperEditor editor;
    editor.open(controls::pad_mapper_native_profile(), screen);
    editor.set_device("Example USB Controller", page != "disconnected");
    controls::PadState raw, calibrated, output;
    raw.lx = 0.42f;
    raw.ly = -0.28f;
    raw.r2 = 0.75f;
    raw.buttons = controls::kPadCross;
    auto view = [&] {
        controls::PadMapper mapper;
        mapper.set_profile(editor.profile());
        std::vector<TouchAction> ignored;
        mapper.process(raw, &calibrated, &output, &ignored);
        return editor.view(screen, raw, calibrated, output, "");
    };
    controls::ControlsView frame = view();
    auto click = [&](const std::string &label) {
        const auto &items = frame.picker.empty() ? frame.toolbar : frame.picker_rows;
        auto it = std::find_if(items.begin(), items.end(),
                               [&](const auto &d) { return d.label == label; });
        if (it == items.end()) {
            fprintf(stderr, "preview: control not visible: %s\n", label.c_str());
            return false;
        }
        const double x = it->rect.x + it->rect.w / 2.0, y = it->rect.y + it->rect.h / 2.0;
        editor.pointer(x, y, 1);
        editor.pointer(x, y, -1);
        frame = view();
        return true;
    };
    if (page == "mapping" || page == "axis" || page == "advanced") {
        if (!click("Mapping"))
            return 1;
        if (page == "axis" && !click("Left stick horizontal"))
            return 1;
        if (page == "advanced" && !click("More options..."))
            return 1;
    }
    if (page == "calibration" || page == "response") {
        if (!click("Calibration"))
            return 1;
        if (page == "response") {
            for (int i = 0; i < 8; ++i) {
                if (std::any_of(frame.toolbar.begin(), frame.toolbar.end(),
                                [](const auto &d) { return d.label == "More options..."; }))
                    break;
                if (!click("Scroll down"))
                    return 1;
            }
            if (!click("More options..."))
                return 1;
            for (int i = 0; i < 6; ++i)
                if (!click("Scroll down"))
                    return 1;
        }
    }
    if (page == "presets" && !click("Presets..."))
        return 1;
    if (page == "discard") {
        if (!click("Calibration") || !click("+") || !click("Cancel"))
            return 1;
    }
    if (page == "disconnected") {
        raw = {};
        frame = view();
    }
    std::vector<uint8_t> pixels(size_t(width) * size_t(height) * 4);
    controls::Canvas canvas(pixels, width, height);
    controls::Paint background;
    background.c0 = {16, 18, 24, 255};
    canvas.rect(0, 0, width, height, background);
    controls::paint_editor(canvas, frame, {0, 0, width, height});
    FILE *file = fopen(argv[1], "wb");
    if (!file)
        return 1;
    fprintf(file, "P6\n%d %d\n255\n", width, height);
    for (size_t i = 0; i < pixels.size(); i += 4) {
        const int alpha = 255 - pixels[i + 3];
        pixels[i] = uint8_t(std::min(255, pixels[i] + 16 * alpha / 255));
        pixels[i + 1] = uint8_t(std::min(255, pixels[i + 1] + 18 * alpha / 255));
        pixels[i + 2] = uint8_t(std::min(255, pixels[i + 2] + 24 * alpha / 255));
        fwrite(pixels.data() + i, 1, 3, file);
    }
    return fclose(file) == 0 ? 0 : 1;
}
