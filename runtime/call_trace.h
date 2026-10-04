// call_trace.h - env-gated guest entry/return tracing.
//
// RECOMP_TRACE_CALLS=<item>[,<item>...] arms a hook on each translated entry
// named. An item is:
//
//   [<module>!]<hex>[:<label>][{<dump>[,<dump>...]}]
//
// <module> matches a registered auxiliary module by name or by its key without
// the extension (`ScriptLibraryR` or `ScriptLibraryR.dll`); without it the
// address is a main-image entry. <dump> is:
//
//   arg0..arg3[+<hex>][.b]  or  eax|ecx|edx|ebx|esp|ebp|esi|edi[+<hex>][.b]
//
// and names a guest word read at that stack argument or register plus offset;
// `.b` reads one byte instead of the default dword. An unmapped address is
// skipped. The hook logs the caller, the call arguments, the dumped words and
// the return value to stderr, then runs the original. It is a diagnostic for
// "which function is the guest stuck in, and with what": it does not change
// guest state and does nothing when the variable is unset.
//
// The tracer uses the generated hook tables (x86.h "hook dispatch"), so it sees
// direct C entry calls and recomp_call/recomp_jump alike. Each armed address
// gets its own hook stub, so a main-image index and a module-local index cannot
// collide; installation goes into whichever table owns the address. It chains
// to whatever hook was already published for the address, so a mod hook under
// it still runs. Addresses are supplied only through the environment; the kit
// contains no game addresses.
//
// Rate limiting: the first RECOMP_TRACE_CALLS_FULL (default 20) calls of an
// address log in full; after that only a call whose (arguments, EAX, dumped
// words) signature changed is logged, so a per-turn call stays readable and a
// task that stops advancing still shows the change. RECOMP_TRACE_CALLS_PERIOD
// (seconds, default 5) bounds the periodic count summary, which prints the last
// dumped values.
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Parse RECOMP_TRACE_CALLS and install the hooks. Idempotent: a second call
// after a successful install does nothing. A no-op when the variable is unset
// or empty.
void recomp_trace_calls_init(void);

// Print the count summary now. Called on exit and every period; safe to call
// when the tracer is not armed.
void recomp_trace_calls_flush(void);

// Per-address counters for tests and diagnostics; all zero for an address the
// tracer did not arm.
uint64_t recomp_trace_calls_total(uint32_t addr);
uint64_t recomp_trace_calls_logged(uint32_t addr);
uint64_t recomp_trace_calls_suppressed(uint32_t addr);

#ifdef __cplusplus
}
#endif
