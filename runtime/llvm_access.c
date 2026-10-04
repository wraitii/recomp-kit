/* Opaque access ABI for bounded LLVM functions. Compile separately without LTO:
 * optimized code must expose complete CPU state before these calls. Successful
 * accesses preserve X86 and use the same guest arena and write hooks as C.
 * This does not introduce signal recovery or alter the runtime's fault policy. */
#include "x86.h"

float rk_access_f32(X86 *c, uint32_t address) {
    (void)c;
    return rdf32(address);
}
double rk_access_f64(X86 *c, uint32_t address) {
    (void)c;
    return rdf64(address);
}
uint32_t rk_access_u32(X86 *c, uint32_t address) {
    (void)c;
    return rd32(address);
}
void rk_access_store32(X86 *c, uint32_t address, float value) {
    (void)c;
    wrf32(address, value);
}
void rk_access_write32(X86 *c, uint32_t address, uint32_t value) {
    (void)c;
    wr32(address, value);
}
