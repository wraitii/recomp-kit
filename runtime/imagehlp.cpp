// imagehlp.cpp - IMAGEHLP (DbgHelp) shims for LHLogR's crash logging.
//
// The recompiled guest runs against this runtime, not a real DbgHelp stack
// walker. These handlers answer the documented failure the caller can already
// handle (no symbols, no walk) and log once, so an unimplemented call is
// visible rather than silently returning zero. They are a host-boundary
// approximation, not a reconstruction of the original DbgHelp behaviour: a
// stack trace through them will be empty. Keep the names and arity exact; a
// real handler can replace one without changing the table.
#include "imports.h"

namespace {
void i_SymSetOptions(X86 *c) {
    // DbgHelp returns the options now in effect; the argument is what it sets.
    set_eax(c, arg(c, 0));
}
void i_SymInitialize(X86 *c) {
    log_once("imagehlp:SymInitialize",
             "IMAGEHLP.dll!SymInitialize: no symbol handler in the host; reporting success "
             "with no symbols");
    set_eax(c, 1);
}
void i_SymCleanup(X86 *c) {
    set_eax(c, 1);
}
void i_SymLoadModule(X86 *c) {
    log_once("imagehlp:SymLoadModule", "IMAGEHLP.dll!SymLoadModule: no load address");
    set_eax(c, 0);
}
void i_SymGetModuleBase(X86 *c) {
    set_eax(c, 0); // no module table
}
void i_SymFunctionTableAccess(X86 *c) {
    set_eax(c, 0); // no FPO/PDB table
}
void i_StackWalk(X86 *c) {
    log_once("imagehlp:StackWalk",
             "IMAGEHLP.dll!StackWalk: no unwind support; reporting end of stack");
    set_eax(c, 0);
}
void i_SymGetSymFromAddr(X86 *c) {
    set_eax(c, 0); // no symbol name
}
} // namespace

const ImportShim g_imagehlp_shims[] = {
    {"IMAGEHLP.dll", "SymSetOptions", 1, i_SymSetOptions},
    {"IMAGEHLP.dll", "SymInitialize", 3, i_SymInitialize},
    {"IMAGEHLP.dll", "SymCleanup", 1, i_SymCleanup},
    {"IMAGEHLP.dll", "SymLoadModule", 6, i_SymLoadModule},
    {"IMAGEHLP.dll", "SymGetModuleBase", 2, i_SymGetModuleBase},
    {"IMAGEHLP.dll", "SymFunctionTableAccess", 2, i_SymFunctionTableAccess},
    {"IMAGEHLP.dll", "StackWalk", 9, i_StackWalk},
    {"IMAGEHLP.dll", "SymGetSymFromAddr", 4, i_SymGetSymFromAddr},
};
const size_t g_imagehlp_shim_count = sizeof(g_imagehlp_shims) / sizeof(g_imagehlp_shims[0]);

void imagehlp_register() {
    imports_register(g_imagehlp_shims, g_imagehlp_shim_count);
}
