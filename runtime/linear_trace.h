// linear_trace.h - exhaustive, ordered main-image entry/return tracing.
//
// RECOMP_TRACE_LINEAR=<path> writes a binary record for every translated
// main-image entry call, paired with the return when it completes. The format
// is decoded by tools/recomp/trace_diff.py, which also diffs two traces:
//
//   RECOMP_TRACE_LINEAR=/tmp/python.trace ./game
//   RECOMP_TRACE_LINEAR=/tmp/rust.trace ./game
//   tools/recomp/trace_diff.py /tmp/python.trace /tmp/rust.trace
//
//   RECOMP_TRACE_LINEAR_FROM=<hex addr>[:<n>]
//       begin recording at the n-th entry to addr (default n = 1)
//   RECOMP_TRACE_LINEAR_MAX=<events>
//       stop after this many records (default 2000000; 0 means no cap)
//   RECOMP_TRACE_LINEAR_RANGE=<hex lo>[:<hex hi>]
//       only record entries inside the inclusive address range
//
// Entry records carry guest thread id, call depth, callee, return address,
// ESP, ECX, EDX and the first four stack words. Return records carry EAX, EDX,
// ESP delta and the x87 TOP/ST0 bits. Hooks chain under any existing hook and
// set recomp_hooks_ever, so specialised call sites fall back through the
// ordinary entry. Auxiliary-module entries are not traced.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void recomp_linear_trace_init(void);
void recomp_linear_trace_on_fault(void);

#ifdef __cplusplus
}
#endif
