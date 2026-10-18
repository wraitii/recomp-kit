#include "user32_internal.h"

#include <cstdint>
#include <cstdio>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {

constexpr uint32_t IDOK = 1;
constexpr uint32_t IDCANCEL = 2;
constexpr uint32_t IDABORT = 3;
constexpr uint32_t IDRETRY = 4;
constexpr uint32_t IDIGNORE = 5;
constexpr uint32_t IDYES = 6;
constexpr uint32_t IDNO = 7;
constexpr uint32_t IDTRYAGAIN = 10;
constexpr uint32_t IDCONTINUE = 11;
constexpr uint32_t WS_DISABLED = 0x08000000u;
constexpr uint32_t closed_button = 0xffffffffu;

struct Button {
    uint32_t id;
    const char *text;
};

struct ButtonSet {
    std::vector<Button> buttons;
};

struct Request {
    uint64_t id = 0;
    std::string title;
    std::string text;
    uint32_t icon = 0;
    bool has_owner = false;
    std::vector<Button> buttons;
    std::vector<std::string> labels;
    uint32_t default_id = IDOK;
    uint32_t escape_id = 0;
};

struct Result {
    bool ready = false;
    uint32_t button = 0;
    uint32_t error = 0;
};

std::mutex g_lock;
std::deque<Request> g_requests;
std::map<uint64_t, Result> g_results;
uint64_t g_next_id = 1;
HostMessageBoxPresenter g_presenter = nullptr;

ButtonSet button_set(uint32_t type) {
    static const Button ok{IDOK, "OK"};
    static const Button cancel{IDCANCEL, "Cancel"};
    static const Button abort{IDABORT, "Abort"};
    static const Button retry{IDRETRY, "Retry"};
    static const Button ignore{IDIGNORE, "Ignore"};
    static const Button yes{IDYES, "Yes"};
    static const Button no{IDNO, "No"};
    static const Button try_again{IDTRYAGAIN, "Try Again"};
    static const Button continue_button{IDCONTINUE, "Continue"};
    switch (type & 0x0000000fu) {
    case 0:
        return {{ok}};
    case 1:
        return {{ok, cancel}};
    case 2:
        return {{abort, retry, ignore}};
    case 3:
        return {{yes, no, cancel}};
    case 4:
        return {{yes, no}};
    case 5:
        return {{retry, cancel}};
    case 6:
        return {{cancel, try_again, continue_button}};
    default:
        return {};
    }
}

uint32_t icon_flags(uint32_t type) {
    switch (type & 0x000000f0u) {
    case 0x10:
        return HOST_MESSAGEBOX_ERROR;
    case 0x20:
        return HOST_MESSAGEBOX_QUESTION;
    case 0x30:
        return HOST_MESSAGEBOX_WARNING;
    case 0x40:
        return HOST_MESSAGEBOX_INFORMATION;
    default:
        return 0;
    }
}

const char *icon_name(uint32_t type) {
    switch (type & 0x000000f0u) {
    case 0x10:
        return "MB_ICONERROR";
    case 0x20:
        return "MB_ICONQUESTION";
    case 0x30:
        return "MB_ICONWARNING";
    case 0x40:
        return "MB_ICONINFORMATION";
    default:
        return "no icon";
    }
}

struct NamedFlag {
    uint32_t bit;
    const char *name;
};

constexpr NamedFlag unsupported_flag_names[] = {
    {0x00001000u, "MB_SYSTEMMODAL"},   {0x00002000u, "MB_TASKMODAL"},
    {0x00004000u, "MB_HELP"},          {0x00008000u, "MB_NOFOCUS"},
    {0x00010000u, "MB_SETFOREGROUND"}, {0x00020000u, "MB_DEFAULT_DESKTOP_ONLY"},
    {0x00040000u, "MB_TOPMOST"},       {0x00080000u, "MB_RIGHT"},
    {0x00100000u, "MB_RTLREADING"},    {0x00200000u, "MB_SERVICE_NOTIFICATION"},
};

std::string unsupported_flags(uint32_t flags) {
    std::string names;
    uint32_t known = 0;
    for (const NamedFlag &flag : unsupported_flag_names) {
        known |= flag.bit;
        if (!(flags & flag.bit))
            continue;
        if (!names.empty())
            names += ", ";
        names += flag.name;
    }
    const uint32_t unknown = flags & ~known;
    if (unknown) {
        char text[48];
        snprintf(text, sizeof text, "%s0x%x", names.empty() ? "" : ", ", unknown);
        names += text;
    }
    return names;
}

bool prepare_request(uint32_t type, Request &request) {
    ButtonSet set = button_set(type);
    if (set.buttons.empty()) {
        LOGW("MessageBox: unsupported button type 0x%x; returning zero", type & 0x0000000fu);
        return false;
    }

    const uint32_t icon = type & 0x000000f0u;
    if (icon && icon != 0x10 && icon != 0x20 && icon != 0x30 && icon != 0x40) {
        if (icon == 0x80)
            LOGW("MessageBox: MB_USERICON is unsupported; returning zero");
        else
            LOGW("MessageBox: unsupported icon flags 0x%x; returning zero", icon);
        return false;
    }

    const uint32_t def = type & 0x00000f00u;
    if (def > 0x00000300u) {
        LOGW("MessageBox: unsupported MB_DEFBUTTON flags 0x%x; returning zero", def);
        return false;
    }
    const uint32_t default_index = def >> 8;
    if (default_index >= set.buttons.size()) {
        LOGW("MessageBox: MB_DEFBUTTON%u is unavailable; returning zero", default_index + 1);
        return false;
    }

    const uint32_t other = type & ~(0x0000000fu | 0x000000f0u | 0x00000f00u);
    if (other) {
        const std::string names = unsupported_flags(other);
        LOGW("MessageBox: unsupported flags %s; returning zero", names.c_str());
        return false;
    }

    request.buttons = std::move(set.buttons);
    request.labels.reserve(request.buttons.size());
    for (const Button &button : request.buttons)
        request.labels.emplace_back(button.text);
    request.default_id = request.buttons[default_index].id;
    for (const Button &button : request.buttons)
        if (button.id == IDCANCEL)
            request.escape_id = IDCANCEL;
    if (!request.escape_id)
        for (const Button &button : request.buttons)
            if (button.id == IDOK)
                request.escape_id = IDOK;
    request.icon = icon_flags(type);
    if (icon == 0x20)
        log_once("messagebox-question-icon",
                 "DIVERGENCE(original): MessageBox MB_ICONQUESTION has no SDL equivalent; "
                 "using the information icon");
    LOGV("MessageBox: %s, %zu button(s), default %u", icon_name(type), request.buttons.size(),
         request.default_id);
    return true;
}

bool closed_result(const Request &request, uint32_t *button) {
    if (request.escape_id) {
        *button = request.escape_id;
        return true;
    }
    log_once("messagebox-close-ignored",
             "MessageBox: SDL closed a box with no Cancel or OK button; keeping it pending");
    return false;
}

bool offered(const Request &request, uint32_t button) {
    for (const Button &candidate : request.buttons)
        if (candidate.id == button)
            return true;
    return false;
}

bool take_result(uint64_t id, uint32_t *button, uint32_t *error) {
    std::lock_guard<std::mutex> held(g_lock);
    auto it = g_results.find(id);
    if (it == g_results.end() || !it->second.ready)
        return false;
    *button = it->second.button;
    *error = it->second.error;
    g_results.erase(it);
    return true;
}

bool disable_owner(X86 *c, uint32_t owner) {
    user32::Window *window = user32::find_window(owner);
    if (!window || !window->enabled)
        return false;
    const bool wide = window->unicode;
    window->enabled = false;
    window->style |= WS_DISABLED;
    user32::deliver_message(c, owner, 0x000a, 0, 0, wide);
    return true;
}

void restore_owner(X86 *c, uint32_t owner) {
    user32::Window *window = user32::find_window(owner);
    if (!window || window->enabled)
        return;
    const bool wide = window->unicode;
    window->enabled = true;
    window->style &= ~WS_DISABLED;
    user32::deliver_message(c, owner, 0x000a, 1, 0, wide);
}

} // namespace

void host_set_message_box_presenter(HostMessageBoxPresenter presenter) {
    std::lock_guard<std::mutex> held(g_lock);
    g_presenter = presenter;
}

bool host_message_box_pending() {
    std::lock_guard<std::mutex> held(g_lock);
    return !g_requests.empty();
}

void host_message_box_service() {
    HostMessageBoxPresenter presenter;
    Request request;
    {
        std::lock_guard<std::mutex> held(g_lock);
        presenter = g_presenter;
        if (!presenter || g_requests.empty())
            return;
        request = std::move(g_requests.front());
        g_requests.pop_front();
    }

    std::vector<HostMessageBoxButton> buttons;
    buttons.reserve(request.buttons.size());
    for (size_t i = 0; i < request.buttons.size(); ++i) {
        uint32_t flags = 0;
        if (request.buttons[i].id == request.default_id)
            flags |= HOST_MESSAGEBOX_BUTTON_DEFAULT;
        if (request.buttons[i].id == request.escape_id)
            flags |= HOST_MESSAGEBOX_BUTTON_ESCAPE;
        buttons.push_back({request.buttons[i].id, request.labels[i].c_str(), flags});
    }

    HostMessageBoxRequest host_request{
        request.title.c_str(), request.text.c_str(),     request.icon,
        request.has_owner,     uint32_t(buttons.size()), buttons.data()};
    uint32_t button = closed_button;
    const int status = presenter(&host_request, &button);

    std::lock_guard<std::mutex> held(g_lock);
    if (status != 0) {
        LOGW("MessageBox: host failed to show the box; returning zero");
        g_results[request.id] = {true, 0, 31};
    } else if (button == closed_button) {
        uint32_t result = 0;
        if (closed_result(request, &result))
            g_results[request.id] = {true, result, 0};
        else
            g_requests.push_front(std::move(request));
    } else if (!offered(request, button)) {
        LOGW("MessageBox: host returned button %u which the box did not offer; returning zero",
             button);
        g_results[request.id] = {true, 0, 31};
    } else {
        g_results[request.id] = {true, button, 0};
    }
}

void host_message_box_cancel_all() {
    std::lock_guard<std::mutex> held(g_lock);
    if (g_requests.empty())
        return;
    log_once("messagebox-shutdown",
             "MessageBox: host shutdown cancelled pending boxes; returning zero");
    for (const Request &request : g_requests)
        g_results[request.id] = {true, 0, 31};
    g_requests.clear();
}

uint32_t user32_message_box_show(X86 *c, uint32_t owner, const std::string &title,
                                 const std::string &text, uint32_t type) {
    if (owner && !user32::find_window(owner)) {
        LOGW("MessageBox: invalid owner window %08x; returning zero", owner);
        set_last_error(1400);
        return 0;
    }

    Request request;
    request.title = title;
    request.text = text;
    request.has_owner = owner != 0;
    if (!prepare_request(type, request)) {
        set_last_error(87);
        return 0;
    }

    {
        std::lock_guard<std::mutex> held(g_lock);
        if (!g_presenter) {
            LOGW("MessageBox: no host message-box presenter; returning zero");
            set_last_error(1400);
            return 0;
        }
    }

    const bool owner_disabled = disable_owner(c, owner);
    uint64_t id = 0;
    bool missing_presenter = false;
    {
        std::lock_guard<std::mutex> held(g_lock);
        if (!g_presenter) {
            missing_presenter = true;
        } else {
            id = g_next_id++;
            request.id = id;
            g_requests.push_back(std::move(request));
        }
    }
    if (missing_presenter) {
        LOGW("MessageBox: no host message-box presenter; returning zero");
        if (owner_disabled)
            restore_owner(c, owner);
        set_last_error(1400);
        return 0;
    }

    uint32_t button = 0;
    uint32_t error = 0;
    for (;;) {
        if (take_result(id, &button, &error)) {
            if (owner_disabled)
                restore_owner(c, owner);
            if (error)
                set_last_error(error);
            return button;
        }
        if (process_exited()) {
            if (owner_disabled)
                restore_owner(c, owner);
            set_last_error(31);
            return 0;
        }
        host_guest_yield();
        guest_sleep_ms(1);
    }
}
