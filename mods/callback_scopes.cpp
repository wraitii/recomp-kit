// Callback scopes are nested per guest thread so hooks can restore the exact
// outer scope after a nested call or an unwind.
#include <deque>
#include <stdint.h>
#include "mods_internal.h"

namespace {
// A deque keeps outer scopes in place while nested callbacks push their own.
thread_local std::deque<bool> scopes;
} // namespace

#ifdef POPM_TESTING
static thread_local uint64_t test_view_pushes = 0;
extern "C" uint64_t mods_view_test_push_count() {
    return test_view_pushes;
}
#endif

void mods_view_push() {
#ifdef POPM_TESTING
    ++test_view_pushes;
#endif
    scopes.push_back(true);
}

void mods_view_pop() {
    // Pop only the innermost scope; an empty stack is harmless during reset.
    if (!scopes.empty())
        scopes.pop_back();
}

void mods_view_reset() {
    scopes.clear();
}

uint32_t mods_view_depth() {
    return (uint32_t)scopes.size();
}

void mods_view_truncate(uint32_t depth) {
    // Guest unwinds restore a saved depth rather than blindly popping once.
    while (scopes.size() > depth)
        scopes.pop_back();
}

bool mods_view_active() {
    return !scopes.empty();
}
