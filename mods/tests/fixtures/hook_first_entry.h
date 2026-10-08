#ifndef RECOMP_MOD_TEST_HOOK_FIRST_ENTRY_H
#define RECOMP_MOD_TEST_HOOK_FIRST_ENTRY_H

#include "pop_mod_api.h"

static inline PopModStatus fixture_hook_first_entry(const PopModApi *api, PopHookFn callback,
                                                    int32_t mode, uint32_t *out_addr,
                                                    uint32_t *out_id) {
    uint32_t addrs[1024];
    uint32_t count = 0;
    PopModStatus status = api->symbols_matching(api, "", addrs, 1024, &count);
    if (status != POP_OK)
        return status;
    if (count > 1024)
        count = 1024;
    for (uint32_t i = 0; i < count; ++i) {
        status = api->hook_install(api, addrs[i], callback, mode, 0, out_id);
        if (status == POP_OK) {
            *out_addr = addrs[i];
            return POP_OK;
        }
        if (status != POP_E_NOSYMBOL)
            return status;
    }
    return POP_E_NOSYMBOL;
}

#endif
