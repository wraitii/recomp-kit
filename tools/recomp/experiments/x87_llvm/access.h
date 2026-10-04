#pragma once
#include "x86.h"

/* Kept opaque to the generated C and LLVM modules (separate TU, no LTO).
 * An access may read complete CPU state or abandon the call before touching
 * memory. On success it changes only its specified guest bytes. */
float rk_access_f32(X86 *c, uint32_t address);
double rk_access_f64(X86 *c, uint32_t address);
uint32_t rk_access_u32(X86 *c, uint32_t address);
void rk_access_store32(X86 *c, uint32_t address, float value);

/* Focused replay hooks; mode 3 retains its older final-state-only contract. */
void rk_memory_begin(unsigned mode);
void rk_memory_end(unsigned mode);
unsigned rk_memory_count(void);
int rk_memory_fault(void (*fn)(X86 *), X86 *c, unsigned access);
void rk_memory_report(const char *name);
