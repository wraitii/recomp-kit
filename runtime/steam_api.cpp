// Mock for the GOG steam_api.dll shim shipped with Spore. It is not a Steam
// client: the packaged DLL itself only reports success and owns the game's app
// id, so this table reproduces those answers without loading Steam.
#include "imports.h"
#include "memory.h"

#include <cstdio>

namespace {

uint32_t g_mock_steam_apps;
uint32_t g_mock_steam_vtable;

void mock_steam_init(X86 *c) {
    LOGW("steam_api: mock GOG shim active; SteamAPI_Init reports success without a Steam client");
    set_eax(c, 1);
}

void mock_steam_shutdown(X86 *c) {
    set_eax(c, 0);
}

void mock_owns_app(X86 *c) {
    set_eax(c, 1);
}

void mock_steam_apps(X86 *c) {
    if (!g_mock_steam_apps || !heap_owns(g_mock_steam_apps) || !g_mock_steam_vtable ||
        !heap_owns(g_mock_steam_vtable)) {
        g_mock_steam_vtable = heap_alloc(8 * 4, true);
        g_mock_steam_apps = heap_alloc(4, true);
        uint32_t owns = imports_alloc_trampoline("steam_api.dll", "__recomp_mock_steam_apps_owns",
                                                 mock_owns_app, ARGC_CDECL);
        if (g_mock_steam_vtable && g_mock_steam_apps && owns) {
            wr32(g_mock_steam_vtable + 6 * 4, owns);
            wr32(g_mock_steam_apps, g_mock_steam_vtable);
        } else {
            g_mock_steam_apps = 0;
        }
    }
    set_eax(c, g_mock_steam_apps);
}

const ImportShim g_steam_api_shims[] = {
    {"steam_api.dll", "SteamAPI_Init", ARGC_CDECL, mock_steam_init},
    {"steam_api.dll", "SteamAPI_Shutdown", ARGC_CDECL, mock_steam_shutdown},
    {"steam_api.dll", "SteamApps", ARGC_CDECL, mock_steam_apps},
};

} // namespace

void steam_api_register() {
    imports_register(g_steam_api_shims, sizeof g_steam_api_shims / sizeof g_steam_api_shims[0]);
    imports_register_strict("steam_api.dll");
}
