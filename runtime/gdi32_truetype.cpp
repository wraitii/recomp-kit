// TrueType faces from AddFontMemResourceEx, measured and rasterized with
// stb_truetype. The data is the program's own - a font resource in its
// executable - and is copied, since the caller may free its buffer. The kit's
// bundled Open Sans stands in for Windows' own interface faces.
#include "gdi32_truetype.h"
#include "game_config.h"
#include "win32.h"
#include "../platform/os.h"
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <tuple>
#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Weverything"
#elif defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wall"
#pragma GCC diagnostic ignored "-Wextra"
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wsign-conversion"
#pragma GCC diagnostic ignored "-Wshadow"
#endif
#define STB_TRUETYPE_IMPLEMENTATION
#define STBTT_STATIC
#include "../third_party/stb/stb_truetype.h"
#if defined(__clang__)
#pragma clang diagnostic pop
#elif defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

namespace gdi {
extern const unsigned char bundled_sans_regular[];
extern const size_t bundled_sans_regular_size;
extern const unsigned char bundled_sans_semibold[];
extern const size_t bundled_sans_semibold_size;
struct TrueTypeFace {
    std::shared_ptr<std::vector<uint8_t>> data;
    const uint8_t *bytes = nullptr;
    size_t size = 0;
    stbtt_fontinfo info{};
    std::string family; // lower case
    std::string style, full_name;
};
namespace {
std::vector<std::unique_ptr<TrueTypeFace>> &faces() {
    static std::vector<std::unique_ptr<TrueTypeFace>> list;
    return list;
}
std::string lower_ascii(std::string s) {
    for (char &ch : s)
        if (ch >= 'A' && ch <= 'Z')
            ch = char(ch - 'A' + 'a');
    return s;
}
std::string name_of(const stbtt_fontinfo &info, int id) {
    int length = 0;
    if (const char *utf16 =
            stbtt_GetFontNameString(&info, &length, STBTT_PLATFORM_ID_MICROSOFT,
                                    STBTT_MS_EID_UNICODE_BMP, STBTT_MS_LANG_ENGLISH, id)) {
        std::string name;
        for (int i = 0; i + 1 < length; i += 2) {
            uint16_t unit = uint16_t((uint8_t(utf16[i]) << 8) | uint8_t(utf16[i + 1]));
            name.push_back(unit < 128 ? char(unit) : '?');
        }
        return name;
    }
    if (const char *roman = stbtt_GetFontNameString(
            &info, &length, STBTT_PLATFORM_ID_MAC, STBTT_MAC_EID_ROMAN, STBTT_MAC_LANG_ENGLISH, id))
        return std::string(roman, size_t(length));
    return {};
}
} // namespace

uint32_t truetype_add_memory(const uint8_t *data, size_t size) {
    if (!data || size < 12 || size > (64u << 20))
        return 0;
    auto copy = std::make_shared<std::vector<uint8_t>>(data, data + size);
    int count = stbtt_GetNumberOfFonts(copy->data());
    uint32_t added = 0;
    for (int i = 0; i < count; ++i) {
        int offset = stbtt_GetFontOffsetForIndex(copy->data(), i);
        if (offset < 0 || size_t(offset) >= size)
            continue;
        auto face = std::make_unique<TrueTypeFace>();
        face->data = copy;
        face->bytes = copy->data();
        face->size = copy->size();
        if (!stbtt_InitFont(&face->info, copy->data(), offset))
            continue;
        face->family = lower_ascii(name_of(face->info, 1));
        face->style = name_of(face->info, 2);
        face->full_name = name_of(face->info, 4);
        if (face->family.empty())
            continue;
        faces().push_back(std::move(face));
        ++added;
    }
    return added;
}

const TrueTypeFace *truetype_find(const std::string &family, int32_t weight, bool italic) {
    static const bool loaded = [] {
        const char *const paths[] = RECOMP_FONT_FILES;
        for (const char *path : paths) {
            if (!path)
                break;
            const std::string host = win32_host_path(std::string(RECOMP_GUEST_ROOT) + "\\" + path);
            int fd = os_fd_open(host.c_str(), OS_O_RDONLY);
            OsStat stat{};
            std::vector<uint8_t> bytes;
            if (fd >= 0 && os_fd_stat(fd, &stat) == 0 && stat.size >= 12 &&
                stat.size <= (64u << 20)) {
                bytes.resize(size_t(stat.size));
                size_t read = 0;
                while (read < bytes.size()) {
                    int64_t count = os_fd_read(fd, bytes.data() + read, bytes.size() - read);
                    if (count <= 0)
                        break;
                    read += size_t(count);
                }
                if (read != bytes.size())
                    bytes.clear();
            }
            if (fd >= 0)
                os_fd_close(fd);
            if (!truetype_add_memory(bytes.data(), bytes.size())) {
                LOGW("gdi: cannot load configured font \"%s\"", path);
                abort();
            }
        }
        return true;
    }();
    (void)loaded;
    const std::string key = lower_ascii(family);
    const TrueTypeFace *best = nullptr;
    int best_score = 3;
    for (auto it = faces().rbegin(); it != faces().rend(); ++it) // the latest registration wins
        if ((*it)->family == key) {
            const auto &face = **it;
            const uint8_t style = face.info.data[face.info.head + 45];
            const int score =
                int(bool(style & 1) != (weight >= 600)) + int(bool(style & 2) != italic);
            if (score < best_score) {
                best = &face;
                best_score = score;
            }
        }
    return best;
}

namespace {
// A bundled face, read once from the embedded bytes. The data is the kit's
// own and lives as long as the process, so it is not copied.
std::unique_ptr<TrueTypeFace> bundled_face(const unsigned char *data, size_t size) {
    auto face = std::make_unique<TrueTypeFace>();
    face->bytes = data;
    face->size = size;
    const int offset = stbtt_GetFontOffsetForIndex(data, 0);
    if (offset < 0 || size_t(offset) >= size || !stbtt_InitFont(&face->info, data, offset))
        return nullptr;
    face->family = lower_ascii(name_of(face->info, 1));
    face->style = name_of(face->info, 2);
    face->full_name = name_of(face->info, 4);
    return face;
}
} // namespace

const TrueTypeFace *truetype_windows_substitute(const std::string &family, int32_t weight) {
    // The sans-serif interface faces a Windows 7 installation has, and the
    // names that resolve to them: the dialog aliases, the raster MS Sans
    // Serif, and Helvetica through FontSubstitutes. Serif, fixed-pitch and
    // the system raster fonts (System, Fixedsys, Terminal) are not here -
    // the 8x16 cells are the closer stand-in for those.
    static const char *const windows_sans[] = {
        "segoe ui",      "tahoma",       "microsoft sans serif",
        "ms sans serif", "ms shell dlg", "ms shell dlg 2",
        "arial",         "verdana",      "calibri",
        "trebuchet ms",  "helvetica",
    };
    const std::string key = lower_ascii(family);
    if (std::find(std::begin(windows_sans), std::end(windows_sans), key) == std::end(windows_sans))
        return nullptr;
    static const std::unique_ptr<TrueTypeFace> regular =
        bundled_face(bundled_sans_regular, bundled_sans_regular_size);
    static const std::unique_ptr<TrueTypeFace> semibold =
        bundled_face(bundled_sans_semibold, bundled_sans_semibold_size);
    const TrueTypeFace *face = weight >= 600 && semibold ? semibold.get() : regular.get();
    return face;
}

double truetype_scale(const TrueTypeFace *face, int32_t lf_height) {
    if (lf_height < 0)
        return stbtt_ScaleForMappingEmToPixels(&face->info, -float(lf_height));
    return stbtt_ScaleForPixelHeight(&face->info, lf_height ? float(lf_height) : 16.0f);
}

TrueTypeMetrics truetype_metrics(const TrueTypeFace *face, double scale) {
    int ascent = 0, descent = 0, gap = 0;
    stbtt_GetFontVMetrics(&face->info, &ascent, &descent, &gap);
    TrueTypeMetrics m;
    m.ascent = int32_t(std::lround(ascent * scale));
    m.descent = int32_t(std::lround(-descent * scale));
    m.height = m.ascent + m.descent;
    const uint16_t units_per_em = uint16_t((face->info.data[face->info.head + 18] << 8) |
                                           face->info.data[face->info.head + 19]);
    m.internal = std::max(0, m.height - int32_t(std::lround(units_per_em * scale)));
    m.average = truetype_advance(face, scale, 'x');
    for (uint32_t ch = 32; ch < 127; ++ch)
        m.maximum = std::max(m.maximum, truetype_advance(face, scale, ch));
    return m;
}

TrueTypeOutlineMetrics truetype_outline_metrics(const TrueTypeFace *face, double scale) {
    auto u16 = [](const uint8_t *p) { return uint32_t(p[0]) << 8 | p[1]; };
    auto s16 = [&](const uint8_t *p) { return int32_t(int16_t(u16(p))); };
    auto u32 = [](const uint8_t *p) {
        return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
    };
    const uint8_t *data = face->bytes;
    TrueTypeOutlineMetrics m;
    if (face->info.head < 0 || face->info.hhea < 0 || size_t(face->info.head) + 54 > face->size ||
        size_t(face->info.hhea) + 36 > face->size)
        return m;
    const uint8_t *head = data + face->info.head;
    const uint8_t *hhea = data + face->info.hhea;
    auto table = [&](const char *tag, size_t &length) -> const uint8_t * {
        if (face->info.fontstart < 0 || size_t(face->info.fontstart) + 12 > face->size)
            return nullptr;
        const uint8_t *directory = data + face->info.fontstart + 12;
        const uint32_t count = u16(data + face->info.fontstart + 4);
        if (count > (face->size - size_t(directory - data)) / 16)
            return nullptr;
        for (uint32_t i = 0; i < count; ++i, directory += 16) {
            if (memcmp(directory, tag, 4) != 0)
                continue;
            const uint32_t offset = u32(directory + 8), bytes = u32(directory + 12);
            if (offset > face->size || bytes > face->size - offset)
                return nullptr;
            length = bytes;
            return data + offset;
        }
        return nullptr;
    };
    m.em_square = u16(head + 18);
    m.minimum_ppem = u16(head + 46);
    m.ascent = int32_t(std::lround(s16(hhea + 4) * scale));
    m.descent = int32_t(std::lround(s16(hhea + 6) * scale));
    m.line_gap = int32_t(std::lround(s16(hhea + 8) * scale));
    m.slope_rise = s16(hhea + 18);
    m.slope_run = s16(hhea + 20);
    m.x_min = int32_t(std::lround(s16(head + 36) * scale));
    m.y_min = int32_t(std::lround(s16(head + 38) * scale));
    m.x_max = int32_t(std::lround(s16(head + 40) * scale));
    m.y_max = int32_t(std::lround(s16(head + 42) * scale));
    m.mac_ascent = m.ascent;
    m.mac_descent = m.descent;
    m.mac_line_gap = m.line_gap;
    size_t length = 0;
    if (const uint8_t *os2 = table("OS/2", length); os2 && length >= 74) {
        m.type = u16(os2 + 8) & 0x30e;
        m.ascent = int32_t(std::lround(s16(os2 + 68) * scale));
        m.descent = int32_t(std::lround(s16(os2 + 70) * scale));
        m.line_gap = int32_t(std::lround(s16(os2 + 72) * scale));
        m.subscript_x_size = int32_t(std::lround(s16(os2 + 10) * scale));
        m.subscript_y_size = int32_t(std::lround(s16(os2 + 12) * scale));
        m.subscript_x_offset = int32_t(std::lround(s16(os2 + 14) * scale));
        m.subscript_y_offset = int32_t(std::lround(s16(os2 + 16) * scale));
        m.superscript_x_size = int32_t(std::lround(s16(os2 + 18) * scale));
        m.superscript_y_size = int32_t(std::lround(s16(os2 + 20) * scale));
        m.superscript_x_offset = int32_t(std::lround(s16(os2 + 22) * scale));
        m.superscript_y_offset = int32_t(std::lround(s16(os2 + 24) * scale));
        m.strikeout_size = int32_t(std::lround(s16(os2 + 26) * scale));
        m.strikeout_position = int32_t(std::lround(s16(os2 + 28) * scale));
        memcpy(m.panose, os2 + 32, sizeof(m.panose));
        m.selection = u16(os2 + 62);
    }
    if (const uint8_t *post = table("post", length); post && length >= 12) {
        int32_t angle = int32_t(u32(post + 4));
        m.italic_angle = int32_t(std::lround(double(angle) * 10.0 / 65536.0));
        m.underline_position = int32_t(std::lround(s16(post + 8) * scale));
        m.underline_size = int32_t(std::lround(s16(post + 10) * scale));
    }
    return m;
}

const std::string &truetype_family(const TrueTypeFace *face) {
    return face->family;
}
const std::string &truetype_style(const TrueTypeFace *face) {
    return face->style;
}
const std::string &truetype_full_name(const TrueTypeFace *face) {
    return face->full_name;
}

int32_t truetype_advance(const TrueTypeFace *face, double scale, uint32_t codepoint) {
    int advance = 0, bearing = 0;
    stbtt_GetCodepointHMetrics(&face->info, int(codepoint), &advance, &bearing);
    return int32_t(std::lround(advance * scale));
}

const TrueTypeGlyph &truetype_glyph(const TrueTypeFace *face, double scale, uint32_t codepoint) {
    static std::map<std::tuple<const TrueTypeFace *, double, uint32_t>, TrueTypeGlyph> cache;
    auto key = std::make_tuple(face, scale, codepoint);
    auto it = cache.find(key);
    if (it != cache.end())
        return it->second;
    if (cache.size() >
        4096) // repaints redraw the same few captions; a bound, not an eviction policy
        cache.clear();
    TrueTypeGlyph glyph;
    int x0 = 0, y0 = 0, x1 = 0, y1 = 0;
    stbtt_GetCodepointBitmapBox(&face->info, int(codepoint), float(scale), float(scale), &x0, &y0,
                                &x1, &y1);
    if (x1 > x0 && y1 > y0 && int64_t(x1 - x0) * (y1 - y0) <= (1 << 22)) {
        glyph.w = x1 - x0;
        glyph.h = y1 - y0;
        glyph.x = x0;
        glyph.y = y0;
        glyph.coverage.assign(size_t(glyph.w) * size_t(glyph.h), 0);
        stbtt_MakeCodepointBitmap(&face->info, glyph.coverage.data(), glyph.w, glyph.h, glyph.w,
                                  float(scale), float(scale), int(codepoint));
    }
    return cache.emplace(key, std::move(glyph)).first->second;
}
} // namespace gdi
