/* Corpus-only return seam: unexpected dispatch and callbacks fail by name. */
#include "x86.h"
#include "corpus-config.h"
#include <stdio.h>
#include <stdlib.h>
const int recomp_resumable_stacks = 0;
static void unexpected(const char *name, uint32_t address) {
    fprintf(stderr, "corpus: unsupported %s at %08x\n", name, address);
    abort();
}
int recomp_is_call_return(uint32_t address) {
    if (address != CORPUS_RETURN)
        unexpected("return", address);
    return 1;
}
int recomp_module_is_call_return(uint32_t address) {
    unexpected("module return", address);
    return 0;
}
int32_t recomp_index_of(uint32_t address) {
    unexpected("dispatch", address);
    return -1;
}
int32_t recomp_module_lookup(uint32_t address) {
    unexpected("module dispatch", address);
    return -1;
}
void recomp_call(X86 *c, uint32_t address) {
    (void)c;
    unexpected("call", address);
}
void recomp_callback_return(X86 *c) {
    unexpected("callback", c->eip);
}
void recomp_div_error(X86 *c, uint32_t address) {
    (void)c;
    unexpected("integer division fault", address);
}
