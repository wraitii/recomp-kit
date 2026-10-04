/* Synchronous memory boundaries for the isolated LLVM experiment. These are
 * compiled separately without LTO: the optimizer must treat CPU state as
 * observable by each access, even when capture is disabled for timing. */
#include "access.h"
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>

#define MAX_EVENTS 32
#define SCRATCH_BASE 0x10000
#define SCRATCH_SIZE 256

typedef struct MemoryEvent {
    X86 cpu;
    uint8_t scratch[SCRATCH_SIZE];
    uint32_t address, width, write;
    uint64_t payload;
} MemoryEvent;
static MemoryEvent reference[MAX_EVENTS];
static unsigned reference_count, event_count, active, compare_mode, stop_at;
static uint64_t checked_events;
static jmp_buf fault_return;

/* Observe after store conversion but before memory changes. Failure is an
 * injected synchronous early exit, not an OS signal/SEH emulation. */
static void event(X86 *c, uint32_t address, uint32_t width, uint32_t write, uint64_t payload) {
    if (!active)
        return;
    if (event_count == MAX_EVENTS) {
        fprintf(stderr, "memory trace exceeds bounded fixture capacity\n");
        abort();
    }
    MemoryEvent current;
    memset(&current, 0, sizeof current);
    memcpy(&current.cpu, c, sizeof *c);
    memcpy(current.scratch, g_mem + SCRATCH_BASE, SCRATCH_SIZE);
    current.address = address;
    current.width = width;
    current.write = write;
    current.payload = payload;
    if (!compare_mode && !stop_at) {
        reference[event_count] = current;
    } else if (event_count >= reference_count ||
               memcmp(&current, &reference[event_count], sizeof current)) {
        fprintf(stderr, "memory boundary mismatch: mode=%u event=%u address=%x write=%u\n",
                compare_mode, event_count, address, write);
        abort();
    } else if (!stop_at) {
        ++checked_events;
    }
    ++event_count;
    if (event_count == stop_at)
        longjmp(fault_return, 1);
}

float rk_access_f32(X86 *c, uint32_t address) {
    event(c, address, 4, 0, 0);
    return rdf32(address);
}
double rk_access_f64(X86 *c, uint32_t address) {
    event(c, address, 8, 0, 0);
    return rdf64(address);
}
uint32_t rk_access_u32(X86 *c, uint32_t address) {
    event(c, address, 4, 0, 0);
    return rd32(address);
}
void rk_access_store32(X86 *c, uint32_t address, float value) {
    uint32_t bits;
    memcpy(&bits, &value, sizeof bits);
    event(c, address, 4, 1, bits);
    wrf32(address, value);
}
void rk_memory_begin(unsigned mode) {
    event_count = 0;
    stop_at = 0;
    compare_mode = mode;
    active = mode < 3;
}
void rk_memory_end(unsigned mode) {
    if (mode == 0) {
        if (!event_count) {
            fprintf(stderr, "memory fixture executed no accesses\n");
            abort();
        }
        reference_count = event_count;
    } else if (mode < 3 && event_count != reference_count) {
        fprintf(stderr, "memory boundary count mismatch\n");
        abort();
    }
    active = 0;
}
unsigned rk_memory_count(void) {
    return reference_count;
}

/* setjmp's frame stays live. The caller owns the X86 object outside this
 * frame, so its contents remain defined after longjmp. No C++ unwinding. */
int rk_memory_fault(void (*fn)(X86 *), X86 *c, unsigned access) {
    if (!access || access > MAX_EVENTS)
        abort();
    active = 1;
    compare_mode = 4; /* Fault replay also checks the normal reference trace prefix. */
    event_count = 0;
    stop_at = access;
    if (setjmp(fault_return) == 0) {
        fn(c);
        active = 0;
        stop_at = 0;
        return 0;
    }
    active = 0;
    stop_at = 0;
    return 1;
}
void rk_memory_report(const char *name) {
    printf("MEMORY %s %llu ordered boundaries checked (raw + lifted): full CPU, "
           "scratch, address, width, direction, store payload\n",
           name, (unsigned long long)checked_events);
    checked_events = 0;
}
