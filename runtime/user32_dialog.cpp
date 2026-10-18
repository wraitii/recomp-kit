#include "user32_internal.h"
#include "gdi32_truetype.h"
#include "loader.h"
#include "memory.h"
#include "resources.h"
#include <algorithm>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace user32 {
namespace {
constexpr uint32_t RT_DIALOG = 5;
constexpr uint32_t DS_SETFONT = 0x40;
constexpr uint32_t WS_CHILD = 0x40000000u;
constexpr uint32_t WS_VISIBLE = 0x10000000u;
constexpr uint32_t WS_DISABLED = 0x08000000u;
constexpr uint32_t WS_TABSTOP = 0x00010000u;
constexpr uint32_t WM_INITDIALOG = 0x0110;
constexpr uint32_t WM_NCCREATE = 0x0081;
constexpr uint32_t WM_CREATE = 0x0001;
constexpr uint32_t WM_NCDESTROY = 0x0082;

struct TemplateItem {
    uint32_t style = 0, exstyle = 0, id = 0, title_ordinal = 0;
    int16_t x = 0, y = 0, cx = 0, cy = 0;
    std::string cls, title;
};

struct Template {
    bool extended = false;
    uint32_t style = 0, exstyle = 0;
    int16_t x = 0, y = 0, cx = 0, cy = 0;
    std::string title;
    bool font = false;
    uint16_t point_size = 0, weight = 400;
    uint8_t italic = 0, charset = 0;
    std::string face, problem;
    std::vector<TemplateItem> items;
};

struct DialogState {
    uint32_t proc = 0, param = 0, owner = 0, first_tab = 0, font = 0, brush = 0, msg_result = 0;
};

std::map<uint32_t, DialogState> &dialogs() {
    static std::map<uint32_t, DialogState> value;
    return value;
}

struct Reader {
    const uint8_t *data = nullptr;
    size_t size = 0, pos = 0;
    bool bad = false;
    bool need(size_t n) {
        if (bad || pos > size || n > size - pos) {
            bad = true;
            return false;
        }
        return true;
    }
    uint8_t u8() {
        if (!need(1))
            return 0;
        return data[pos++];
    }
    uint16_t u16() {
        if (!need(2))
            return 0;
        uint16_t v = uint16_t(data[pos] | data[pos + 1] << 8);
        pos += 2;
        return v;
    }
    int16_t s16() {
        return int16_t(u16());
    }
    uint32_t u32() {
        if (!need(4))
            return 0;
        uint32_t v = uint32_t(data[pos]) | uint32_t(data[pos + 1]) << 8 |
                     uint32_t(data[pos + 2]) << 16 | uint32_t(data[pos + 3]) << 24;
        pos += 4;
        return v;
    }
    void align4() {
        while (!bad && (pos & 3))
            u8();
    }
    bool var(std::string *text, uint32_t *ordinal) {
        if (text)
            text->clear();
        if (ordinal)
            *ordinal = 0;
        uint16_t first = u16();
        if (bad)
            return false;
        if (!first)
            return true;
        if (first == 0xffff) {
            uint16_t id = u16();
            if (ordinal)
                *ordinal = id;
            return !bad;
        }
        pos -= 2;
        for (size_t units = 0; units < 0x10000; ++units) {
            uint16_t ch = u16();
            if (bad)
                return false;
            if (!ch)
                return true;
            if (ch < 0x80)
                text->push_back(char(ch));
            else if (ch < 0x800) {
                text->push_back(char(0xc0 | (ch >> 6)));
                text->push_back(char(0x80 | (ch & 0x3f)));
            } else {
                text->push_back(char(0xe0 | (ch >> 12)));
                text->push_back(char(0x80 | ((ch >> 6) & 0x3f)));
                text->push_back(char(0x80 | (ch & 0x3f)));
            }
        }
        bad = true;
        return false;
    }
};

bool parse(const uint8_t *data, uint32_t size, Template *out) {
    if (!data || size < 18)
        return false;
    Reader r{data, size};
    uint16_t version = r.u16(), signature = r.u16();
    out->extended = version == 1 && signature == 0xffff;
    if (out->extended) {
        r.u32();
        out->exstyle = r.u32();
        out->style = r.u32();
    } else {
        r.pos = 0;
        out->style = r.u32();
        out->exstyle = r.u32();
    }
    uint16_t count = r.u16();
    out->x = r.s16();
    out->y = r.s16();
    out->cx = r.s16();
    out->cy = r.s16();
    uint32_t ordinal = 0, menu_ordinal = 0, class_ordinal = 0;
    std::string menu, window_class;
    r.var(&menu, &menu_ordinal);
    r.var(&window_class, &class_ordinal);
    r.var(&out->title, &ordinal);
    if (!menu.empty() || menu_ordinal)
        out->problem = "dialog menu is unsupported";
    if (!window_class.empty() || class_ordinal)
        out->problem = "dialog window class is unsupported";
    if (out->style & DS_SETFONT) {
        out->font = true;
        out->point_size = r.u16();
        if (out->extended) {
            out->weight = r.u16();
            out->italic = r.u8();
            out->charset = r.u8();
        }
        r.var(&out->face, nullptr);
    }
    r.align4();
    if (r.bad || count > 1024)
        return false;
    out->items.reserve(count);
    for (uint16_t i = 0; i < count; ++i) {
        r.align4();
        TemplateItem item;
        if (out->extended) {
            r.u32();
            item.exstyle = r.u32();
            item.style = r.u32();
        } else {
            item.style = r.u32();
            item.exstyle = r.u32();
        }
        item.x = r.s16();
        item.y = r.s16();
        item.cx = r.s16();
        item.cy = r.s16();
        item.id = out->extended ? r.u32() : r.u16();
        uint32_t cls_ordinal = 0;
        r.var(&item.cls, &cls_ordinal);
        r.var(&item.title, &item.title_ordinal);
        if (item.cls.empty() && !cls_ordinal)
            out->problem = "empty control class is unsupported";
        uint16_t extra = r.u16();
        if (!r.need(extra))
            return false;
        if (extra)
            out->problem = "control creation data is unsupported";
        r.pos += extra;
        if (r.bad)
            return false;
        if (cls_ordinal == 0x80)
            item.cls = "button";
        else if (cls_ordinal == 0x81)
            item.cls = "edit";
        else if (cls_ordinal == 0x82)
            item.cls = "static";
        else if (cls_ordinal)
            out->problem =
                "control class ordinal " + std::to_string(cls_ordinal) + " is unsupported";
        out->items.push_back(std::move(item));
    }
    return !r.bad;
}

int32_t muldiv(int32_t value, int32_t base, int32_t divisor) {
    int64_t n = int64_t(value) * base;
    if (n >= 0)
        return int32_t((n + divisor / 2) / divisor);
    return int32_t(-((-n + divisor / 2) / divisor));
}

struct FontScale {
    uint32_t font = 0;
    int32_t base_x = 8, base_y = 16;
};

void face_units(const std::string &family, uint16_t point_size, uint16_t weight, uint8_t italic,
                int32_t *base_x, int32_t *base_y) {
    std::string face = family.empty() ? "MS Shell Dlg" : family;
    const auto *found = gdi::truetype_find(face, int32_t(weight), italic != 0);
    if (!found) {
        found = gdi::truetype_windows_substitute(face, int32_t(weight));
        if (found)
            log_once(("dialog-font:" + face).c_str(),
                     "DIVERGENCE(original): dialog font %s uses bundled substitute metrics",
                     face.c_str());
    }
    if (!found) {
        log_once(("dialog-font:" + face).c_str(),
                 "DIVERGENCE(original): dialog font \"%s\" uses 8x16 fallback metrics",
                 face.c_str());
        return;
    }
    const int32_t height = -muldiv(point_size ? int32_t(point_size) : 10, 96, 72);
    const double scale = gdi::truetype_scale(found, height);
    const gdi::TrueTypeMetrics metrics = gdi::truetype_metrics(found, scale);
    int32_t extent = 0;
    for (uint32_t ch = 'A'; ch <= 'Z'; ++ch)
        extent += gdi::truetype_advance(found, scale, ch);
    for (uint32_t ch = 'a'; ch <= 'z'; ++ch)
        extent += gdi::truetype_advance(found, scale, ch);
    const int32_t alphabet = (extent / 26 + 1) / 2;
    *base_x = std::max(1, alphabet ? alphabet : metrics.average);
    *base_y = std::max(1, metrics.height);
}

FontScale create_font(const Template &t) {
    FontScale result;
    face_units(t.face, t.point_size, t.weight, t.italic, &result.base_x, &result.base_y);
    gdi::Object font;
    font.kind = gdi::Object::Font;
    int32_t font_height = -muldiv(t.point_size ? t.point_size : 10, 96, 72);
    memcpy(font.logfont.data(), &font_height, sizeof font_height);
    int32_t weight = t.weight;
    memcpy(font.logfont.data() + 16, &weight, sizeof weight);
    font.logfont[20] = t.italic;
    font.logfont[23] = t.charset;
    const std::string face = t.face.empty() ? "MS Shell Dlg" : t.face;
    for (size_t i = 0; i < 32 && i < face.size(); ++i) {
        font.logfont[28 + i * 2] = uint8_t(face[i]);
        font.logfont[29 + i * 2] = 0;
    }
    result.font = gdi::make_object(font);
    return result;
}

uint32_t text_buffer(const std::string &text) {
    uint32_t p = heap_alloc(uint32_t(text.size()) + 1, true);
    if (p)
        gm_put_str(p, text.c_str(), uint32_t(text.size()) + 1);
    return p;
}

uint32_t create_child(X86 *c, uint32_t parent, const TemplateItem &item, const FontScale &scale,
                      uint32_t hinst) {
    if (item.cls.empty()) {
        log_once("dialog-empty-class", "dialog: control %u has no supported class", item.id);
        return 0;
    }
    uint32_t cls = text_buffer(item.cls);
    uint32_t title = text_buffer(item.title);
    if (!cls || !title) {
        if (cls)
            heap_free(cls);
        if (title)
            heap_free(title);
        return 0;
    }
    const int32_t x = muldiv(item.x, scale.base_x, 4), y = muldiv(item.y, scale.base_y, 8);
    int32_t w = muldiv(item.cx, scale.base_x, 4), h = muldiv(item.cy, scale.base_y, 8);
    if (item.cls == "static" && (item.style & 0x1f) == 0x0e && item.title_ordinal &&
        !(item.style & (0x0200 | 0x0040))) {
        uint32_t bytes = 0;
        uint32_t module = hinst == loader_image_base() ? 0 : hinst;
        uint32_t data =
            resource_data(resource_find(2, item.title_ordinal, nullptr, module), &bytes);
        GdiImage image;
        if (data && gdi_decode_image(data, bytes, &image)) {
            w = image.width;
            h = image.height;
        }
    }
    uint32_t target = imports_resolve("USER32.dll", "CreateWindowExA");
    uint32_t args[12] = {item.exstyle, cls,         title,  item.style, uint32_t(x), uint32_t(y),
                         uint32_t(w),  uint32_t(h), parent, item.id,    hinst,       0};
    uint32_t hwnd = guest_call(c, target, args, 12);
    if (hwnd && item.title_ordinal)
        if (Window *window = find_window(hwnd))
            window->title_ordinal = item.title_ordinal;
    if (hwnd && scale.font)
        deliver_message(c, hwnd, 0x0030, scale.font, 0, false);
    heap_free(cls);
    heap_free(title);
    return hwnd;
}

uint32_t first_tab(const std::vector<TemplateItem> &items) {
    for (const auto &item : items)
        if ((item.style & WS_TABSTOP) && !(item.style & WS_DISABLED))
            return item.id;
    return 0;
}

bool direct_result(uint32_t msg) {
    return msg == 0x0110 || msg == 0x002e || msg == 0x002f || msg == 0x0037 || msg == 0x0039 ||
           (msg >= 0x0132 && msg <= 0x0138);
}

void dialog_proc(X86 *c) {
    const uint32_t hwnd = arg(c, 0), msg = arg(c, 1);
    auto found = dialogs().find(hwnd);
    if (msg == WM_NCDESTROY) {
        if (found != dialogs().end()) {
            const DialogState state = found->second;
            guest_call(c, state.proc, hwnd, msg, arg(c, 2), arg(c, 3));
            if ((found = dialogs().find(hwnd)) != dialogs().end()) {
                if (found->second.font)
                    gdi::objects().erase(found->second.font);
                if (found->second.brush)
                    gdi::objects().erase(found->second.brush);
                dialogs().erase(found);
            }
        }
        def_window_proc(c, false);
        return;
    }
    if (found == dialogs().end() || msg == WM_NCCREATE || msg == WM_CREATE) {
        def_window_proc(c, false);
        return;
    }
    DialogState state = found->second;
    state.msg_result = 0;
    found->second.msg_result = 0;
    uint32_t result = guest_call(c, state.proc, hwnd, msg, arg(c, 2), arg(c, 3));
    if (msg == WM_INITDIALOG) {
        if (result && find_window(hwnd) && find_window(state.first_tab))
            guest_call(c, imports_resolve("USER32.dll", "SetFocus"), state.first_tab);
        set_eax(c, result);
        return;
    }
    if (msg >= 0x0132 && msg <= 0x0138 && !result) {
        if (auto *dc = gdi::dc_of(arg(c, 2))) {
            dc->text_color = 0;
            dc->bk_color = 0xc8c8c8;
        }
        set_eax(c, state.brush);
        return;
    }
    if (result) {
        if (direct_result(msg)) {
            set_eax(c, result);
            return;
        }
        auto current = dialogs().find(hwnd);
        set_eax(c, current == dialogs().end() ? 0 : current->second.msg_result);
        return;
    }
    if (Window *window = find_window(hwnd))
        def_window_proc(c, window->unicode);
    else
        set_eax(c, 0);
}
} // namespace

void dialog_set_msg_result(uint32_t hwnd, uint32_t value) {
    auto found = dialogs().find(hwnd);
    if (found != dialogs().end())
        found->second.msg_result = value;
}

uint32_t dialog_get_msg_result(uint32_t hwnd) {
    auto found = dialogs().find(hwnd);
    return found == dialogs().end() ? 0 : found->second.msg_result;
}

bool is_dialog(uint32_t hwnd) {
    return dialogs().count(hwnd) != 0;
}

void ensure_dialog_classes() {
    if (classes().count("#32770"))
        return;
    WndClass wc;
    wc.style = 0x4088;
    wc.wndproc = imports_alloc_trampoline("USER32.dll", "Dialog window procedure", dialog_proc, 4);
    wc.name = "#32770";
    wc.unicode = false;
    wc.atom = 0xc000 + uint32_t(classes().size());
    classes()["#32770"] = wc;
    classes()["#atom" + std::to_string(wc.atom)] = wc;
}

uint32_t create_dialog_param(X86 *c, bool wide) {
    (void)wide;
    uint32_t hinst = arg(c, 0), name = arg(c, 1), owner = arg(c, 2), proc = arg(c, 3),
             param = arg(c, 4);
    if (!proc) {
        LOGW("CreateDialogParamA: dialog procedure is null");
        set_last_error(87);
        return 0;
    }
    uint32_t bytes = 0;
    uint32_t module = hinst && hinst != loader_image_base() ? hinst : 0;
    uint32_t data = resource_data(resource_find(RT_DIALOG, name, nullptr, module), &bytes);
    Template dialog;
    if (!data || !parse(g_mem + data, bytes, &dialog)) {
        LOGW("CreateDialogParamA: dialog template %08x is missing or malformed", name);
        set_last_error(1813);
        return 0;
    }
    if (!dialog.problem.empty()) {
        LOGW("CreateDialogParamA: dialog template %08x: %s", name, dialog.problem.c_str());
        set_last_error(1813);
        return 0;
    }
    ensure_dialog_classes();
    ensure_system_classes();
    ensure_progress_class();
    const FontScale font = create_font(dialog);
    int32_t x = muldiv(dialog.x, font.base_x, 4), y = muldiv(dialog.y, font.base_y, 8);
    const int32_t w = muldiv(dialog.cx, font.base_x, 4), h = muldiv(dialog.cy, font.base_y, 8);
    if (dialog.style & 0x0800) {
        const Window *bounds = find_window(desktop_handle);
        if (bounds) {
            int32_t bx = 0, by = 0;
            client_origin(bounds->hwnd, &bx, &by);
            x = bx + (bounds->w - w) / 2;
            y = by + (bounds->h - h) / 2;
        }
    }
    uint32_t cls = text_buffer("#32770");
    uint32_t title = text_buffer(dialog.title);
    uint32_t args[12] = {dialog.exstyle, cls,         title,       dialog.style & ~WS_VISIBLE,
                         uint32_t(x),    uint32_t(y), uint32_t(w), uint32_t(h),
                         owner,          0,           hinst,       0};
    uint32_t hwnd = cls && title
                        ? guest_call(c, imports_resolve("USER32.dll", "CreateWindowExA"), args, 12)
                        : 0;
    if (cls)
        heap_free(cls);
    if (title)
        heap_free(title);
    if (!hwnd) {
        if (font.font)
            gdi::objects().erase(font.font);
        LOGW("CreateDialogParamA: dialog window creation failed for template %08x", name);
        return 0;
    }
    DialogState state;
    state.proc = proc;
    state.param = param;
    state.owner = owner;
    state.font = font.font;
    state.first_tab = first_tab(dialog.items);
    gdi::Object brush;
    brush.kind = gdi::Object::Brush;
    brush.color = 0xc8c8c8;
    state.brush = gdi::make_object(brush);
    dialogs()[hwnd] = state;
    if (Window *window = find_window(hwnd))
        window->font = font.font;
    for (const auto &item : dialog.items) {
        if (!create_child(c, hwnd, item, font, hinst)) {
            LOGW("CreateDialogParamA: control %u could not be created", item.id);
            guest_call(c, imports_resolve("USER32.dll", "DestroyWindow"), hwnd);
            if (dialogs().count(hwnd)) {
                if (dialogs()[hwnd].font)
                    gdi::objects().erase(dialogs()[hwnd].font);
                if (dialogs()[hwnd].brush)
                    gdi::objects().erase(dialogs()[hwnd].brush);
                dialogs().erase(hwnd);
            }
            set_last_error(1813);
            return 0;
        }
    }
    uint32_t first = find_dlg_item(hwnd, state.first_tab);
    if (!find_window(hwnd) || dialogs().count(hwnd) == 0) {
        set_last_error(1813);
        return 0;
    }
    state.first_tab = first;
    dialogs()[hwnd].first_tab = first;
    uint32_t result = guest_call(c, proc, hwnd, WM_INITDIALOG, first, param);
    if (result && first && find_window(hwnd) && find_window(first))
        guest_call(c, imports_resolve("USER32.dll", "SetFocus"), first);
    if ((dialog.style & WS_VISIBLE) && find_window(hwnd))
        guest_call(c, imports_resolve("USER32.dll", "ShowWindow"), hwnd, 5);
    return hwnd;
}
} // namespace user32
