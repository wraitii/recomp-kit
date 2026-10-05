// key_names.h - Windows' key names for the US keyboard layout, by DirectInput
// scan code. One table serves DirectInput's DIPROP_KEYNAME (dx/dinput.cpp) and
// GetKeyNameTextA/W (runtime/user32.cpp), which name the same keys.
#pragma once
#include <stdint.h>

namespace {

// The name DirectInput reports for a keyboard object (DIPROP_KEYNAME) is the
// Windows key name for the scancode (GetKeyNameText), which depends on the
// installed keyboard layout. This is the US English table; extended keys are
// DIK codes with bit 7 set. A DIVERGENCE(original): a different layout on the
// original machine would give different names.
inline const char *dik_key_name(uint32_t dik) {
    static const char *const low[0x59] = {
        nullptr, "Esc",   "1",         "2",       "3",     "4",        "5",           "6",
        "7",     "8",     "9",         "0",       "-",     "=",        "Backspace",   "Tab",
        "Q",     "W",     "E",         "R",       "T",     "Y",        "U",           "I",
        "O",     "P",     "[",         "]",       "Enter", "Ctrl",     "A",           "S",
        "D",     "F",     "G",         "H",       "J",     "K",        "L",           ";",
        "'",     "`",     "Shift",     "\\",      "Z",     "X",        "C",           "V",
        "B",     "N",     "M",         ",",       ".",     "/",        "Right Shift", "Num *",
        "Alt",   "Space", "Caps Lock", "F1",      "F2",    "F3",       "F4",          "F5",
        "F6",    "F7",    "F8",        "F9",      "F10",   "Num Lock", "Scroll Lock", "Num 7",
        "Num 8", "Num 9", "Num -",     "Num 4",   "Num 5", "Num 6",    "Num +",       "Num 1",
        "Num 2", "Num 3", "Num 0",     "Num Del", nullptr, nullptr,    nullptr,       "F11",
        "F12"};
    if (dik < 0x59)
        return low[dik];
    switch (dik) {
    case 0x9c:
        return "Num Enter";
    case 0x9d:
        return "Right Ctrl";
    case 0xb5:
        return "Num /";
    case 0xb7:
        return "Prnt Scrn";
    case 0xb8:
        return "Right Alt";
    case 0xc5:
        return "Pause";
    case 0xc7:
        return "Home";
    case 0xc8:
        return "Up";
    case 0xc9:
        return "Page Up";
    case 0xcb:
        return "Left";
    case 0xcd:
        return "Right";
    case 0xcf:
        return "End";
    case 0xd0:
        return "Down";
    case 0xd1:
        return "Page Down";
    case 0xd2:
        return "Insert";
    case 0xd3:
        return "Delete";
    case 0xdb:
        return "Left Windows";
    case 0xdc:
        return "Right Windows";
    case 0xdd:
        return "Application";
    }
    return nullptr;
}

} // namespace
