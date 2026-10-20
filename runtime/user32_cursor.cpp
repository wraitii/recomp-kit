#include "user32_internal.h"
#include "gdi_image.h"
#include "memory.h"
#include "win32.h"
#include "imports.h"
#include "resources.h"
#include "../platform/os.h"
#include <algorithm>
#include <map>
#include <numeric>

namespace {
std::map<uint32_t, HostCursor> cursors;
uint64_t cursor_revision = 1;
uint32_t next_cursor = 0x000a0000;

bool decode_cursor(uint32_t p, uint32_t bytes, HostCursorFrame *frame) {
    if (bytes < 22 || rd16(p) || rd16(p + 2) != 2 || !rd16(p + 4) || rd16(p + 4) > (bytes - 6) / 16)
        return false;
    uint32_t size = rd32(p + 14), offset = rd32(p + 18);
    if (offset > bytes || size > bytes - offset || size < 40)
        return false;
    uint32_t dib = p + offset, header = rd32(dib);
    int32_t width = int32_t(rd32(dib + 4)), height = int32_t(rd32(dib + 8));
    uint32_t bpp = rd16(dib + 14), compression = rd32(dib + 16);
    if (header < 40 || header > size || width <= 0 || width > 256 || height <= 0 || height > 512 ||
        height % 2 || compression != 0)
        return false;
    height /= 2;
    uint32_t colors = bpp <= 8 ? (rd32(dib + 32) ? rd32(dib + 32) : 1u << bpp) : 0;
    uint64_t xor_stride = ((uint64_t(width) * bpp + 31) / 32) * 4;
    uint64_t mask_stride = ((uint64_t(width) + 31) / 32) * 4;
    uint64_t mask_offset = uint64_t(header) + colors * 4 + xor_stride * height;
    if (mask_offset + mask_stride * height > size)
        return false;
    uint32_t copy = heap_alloc(size);
    if (!copy)
        return false;
    memcpy(g_mem + copy, g_mem + dib, size);
    wr32(copy + 8, height);
    GdiImage image;
    bool ok = gdi_decode_image(copy, size, &image, 0, true);
    heap_free(copy);
    if (!ok)
        return false;
    bool alpha = bpp == 32 && std::any_of(image.pixels.begin(), image.pixels.end(),
                                          [](uint32_t pixel) { return (pixel >> 24) != 0; });
    for (int y = 0; y < height; ++y)
        for (int x = 0; x < width; ++x) {
            uint32_t &pixel = image.pixels[size_t(y) * width + x];
            bool masked = rd8(dib + uint32_t(mask_offset + mask_stride * (height - y - 1)) +
                              uint32_t(x / 8)) &
                          (0x80 >> (x % 8));
            if (!alpha)
                pixel = masked ? 0 : pixel | 0xff000000;
        }
    frame->width = width;
    frame->height = height;
    frame->hot_x = rd16(p + 10);
    frame->hot_y = rd16(p + 12);
    frame->pixels = std::move(image.pixels);
    return frame->hot_x < width && frame->hot_y < height;
}

bool decode_animation(uint32_t p, uint32_t bytes, HostCursor *cursor) {
    if (bytes < 12 || rd32(p) != 0x46464952 || rd32(p + 8) != 0x4e4f4341 || rd32(p + 4) < 4)
        return false;
    bytes = uint32_t(std::min<uint64_t>(bytes, uint64_t(rd32(p + 4)) + 8));
    uint32_t frames = 0, steps = 0, rate = 0, flags = 0;
    for (uint32_t offset = 12; offset + 8 <= bytes;) {
        uint32_t chunk = p + offset, size = rd32(chunk + 4);
        if (size > bytes - offset - 8)
            return false;
        if (rd32(chunk) == 0x68696e61) {
            if (size < 36 || rd32(chunk + 8) != 36)
                return false;
            frames = rd32(chunk + 12);
            steps = rd32(chunk + 16);
            rate = rd32(chunk + 36);
            flags = rd32(chunk + 40);
        } else if (rd32(chunk) == 0x65746172 || rd32(chunk) == 0x20716573) {
            if (size % 4)
                return false;
            auto &values = rd32(chunk) == 0x65746172 ? cursor->rates : cursor->sequence;
            for (uint32_t i = 0; i < size; i += 4)
                values.push_back(rd32(chunk + 8 + i));
        } else if (rd32(chunk) == 0x5453494c && size >= 4 && rd32(chunk + 8) == 0x6d617266) {
            for (uint32_t i = 4; i + 8 <= size;) {
                uint32_t item = chunk + 8 + i, count = rd32(item + 4);
                if (count > size - i - 8 || rd32(item) != 0x6e6f6369)
                    return false;
                HostCursorFrame frame;
                if (!decode_cursor(item + 8, count, &frame))
                    return false;
                cursor->frames.push_back(std::move(frame));
                i += 8 + count + (count & 1);
            }
        }
        offset += 8 + size + (size & 1);
    }
    if (!(flags & 1) || (flags & ~3u) || !frames || frames != cursor->frames.size() || !steps)
        return false;
    if (!(flags & 2)) {
        if (steps != frames)
            return false;
        cursor->sequence.resize(steps);
        std::iota(cursor->sequence.begin(), cursor->sequence.end(), 0);
    }
    if (cursor->rates.empty())
        cursor->rates.assign(steps, rate);
    return cursor->sequence.size() == steps && cursor->rates.size() == steps &&
           std::all_of(cursor->sequence.begin(), cursor->sequence.end(),
                       [frames](uint32_t frame) { return frame < frames; }) &&
           std::all_of(cursor->rates.begin(), cursor->rates.end(),
                       [](uint32_t value) { return value > 0; });
}
} // namespace

void win32_cursor_changed() {
    ++cursor_revision;
}

bool host_cursor_snapshot(uint64_t *revision, HostCursor *cursor) {
    if (*revision == cursor_revision)
        return false;
    *revision = cursor_revision;
    auto it = cursors.find(user32::g_cursor);
    *cursor = it == cursors.end() ? HostCursor{} : it->second;
    cursor->handle = user32::g_cursor;
    cursor->visible = user32::g_cursor != 0 && host_cursor_visible();
    return true;
}

uint32_t win32_load_cursor_file(X86 *c, const std::string &name) {
    std::string path = win32_host_path(name);
    int fd = path.empty() ? -1 : os_fd_open(path.c_str(), OS_O_RDONLY);
    if (fd < 0) {
        LOGW("LoadCursorFromFile: cannot open '%s'", name.c_str());
        set_last_error(2);
        return 0;
    }
    OsStat stat{};
    bool ok = os_fd_stat(fd, &stat) == 0 && stat.size >= 22 && stat.size <= 16 * 1024 * 1024;
    uint32_t buffer = ok ? heap_alloc(uint32_t(stat.size)) : 0;
    uint32_t read = 0;
    while (buffer && read < stat.size) {
        int64_t count = os_fd_read(fd, g_mem + buffer + read, size_t(stat.size - read));
        if (count <= 0)
            break;
        read += uint32_t(count);
    }
    os_fd_close(fd);
    HostCursor cursor;
    if (buffer && read == stat.size) {
        if (rd32(buffer) == 0x46464952)
            ok = decode_animation(buffer, read, &cursor);
        else {
            HostCursorFrame frame;
            ok = decode_cursor(buffer, read, &frame);
            if (ok) {
                cursor.frames.push_back(std::move(frame));
                cursor.sequence = {0};
                cursor.rates = {1};
            }
        }
    } else
        ok = false;
    if (buffer)
        heap_free(buffer);
    if (!ok) {
        LOGW("LoadCursorFromFile: unsupported cursor data in '%s'", name.c_str());
        imports_unsupported(c);
        return 0;
    }
    uint32_t handle = next_cursor++;
    cursors[handle] = std::move(cursor);
    return handle;
}

// A cursor group from a module's resources. RT_CURSOR data is the hotspot
// followed by the DIB, so it is framed as a one-image .cur for decode_cursor.
// Windows shares a loaded resource cursor, so a repeated load returns its handle.
uint32_t win32_load_cursor_resource(uint32_t module, uint32_t id) {
    static std::map<std::pair<uint32_t, uint32_t>, uint32_t> loaded;
    auto known = loaded.find({module, id});
    if (known != loaded.end())
        return known->second;
    uint32_t bytes = 0,
             group = resource_data(resource_find(12, id, nullptr, module), &bytes, nullptr, module);
    if (!group || bytes < 20 || rd16(group + 2) != 2 || !rd16(group + 4))
        return 0;
    uint32_t data =
        resource_data(resource_find(1, rd16(group + 18), nullptr, module), &bytes, nullptr, module);
    if (!data || bytes < 4 + 40)
        return 0;
    uint32_t size = bytes - 4, file = heap_alloc(22 + size, true);
    if (!file)
        return 0;
    wr16(file + 2, 2);
    wr16(file + 4, 1);
    wr16(file + 10, rd16(data));
    wr16(file + 12, rd16(data + 2));
    wr32(file + 14, size);
    wr32(file + 18, 22);
    memcpy(g_mem + file + 22, g_mem + data + 4, size);
    HostCursor cursor;
    HostCursorFrame frame;
    bool ok = decode_cursor(file, 22 + size, &frame);
    heap_free(file);
    if (!ok)
        return 0;
    cursor.frames.push_back(std::move(frame));
    cursor.sequence = {0};
    cursor.rates = {1};
    uint32_t handle = next_cursor++;
    cursors[handle] = std::move(cursor);
    loaded[{module, id}] = handle;
    return handle;
}

void win32_cursor_from_icon(uint32_t handle, uint32_t hot_x, uint32_t hot_y) {
    GdiImage image;
    if (!gdi_read_icon(handle, &image))
        return;
    HostCursor cursor;
    cursor.frames.push_back(
        {image.width, image.height, int(hot_x), int(hot_y), std::move(image.pixels)});
    cursor.sequence = {0};
    cursor.rates = {1};
    cursors[handle] = std::move(cursor);
}

bool win32_read_cursor(uint32_t handle, GdiImage *image, uint32_t *hot_x, uint32_t *hot_y) {
    auto it = cursors.find(handle);
    if (it == cursors.end() || it->second.frames.empty())
        return false;
    const auto &frame = it->second.frames[it->second.sequence.front()];
    *image = {frame.width, frame.height, frame.pixels};
    if (hot_x)
        *hot_x = frame.hot_x;
    if (hot_y)
        *hot_y = frame.hot_y;
    return true;
}

void win32_copy_cursor(uint32_t source, uint32_t destination, int width, int height) {
    auto it = cursors.find(source);
    if (!destination || it == cursors.end())
        return;
    HostCursor cursor = it->second;
    for (auto &frame : cursor.frames) {
        int w = width ? width : frame.width, h = height ? height : frame.height;
        std::vector<uint32_t> pixels(size_t(w) * h);
        for (int y = 0; y < h; ++y)
            for (int x = 0; x < w; ++x)
                pixels[size_t(y) * w + x] =
                    frame.pixels[size_t(int64_t(y) * frame.height / h) * frame.width +
                                 int64_t(x) * frame.width / w];
        frame.hot_x = int64_t(frame.hot_x) * w / frame.width;
        frame.hot_y = int64_t(frame.hot_y) * h / frame.height;
        frame.width = w;
        frame.height = h;
        frame.pixels = std::move(pixels);
    }
    cursors[destination] = std::move(cursor);
}

void win32_delete_cursor(uint32_t handle) {
    cursors.erase(handle);
    gdi_delete_icon(handle);
    if (user32::g_cursor == handle) {
        user32::g_cursor = 0;
        win32_cursor_changed();
    }
}
