#!/usr/bin/env python3
"""Decode and diff RECOMP_TRACE_LINEAR binary traces.

    trace_diff.py A.trace B.trace [--context N] [--thread TID]
    trace_diff.py --dump A.trace [--thread TID] [--max-events N]

The diff reports the first divergent event for each guest thread, the open
call stack at that point, and every differing field. Values compare exactly;
the report does not assume that a differing pointer is noise.
"""
import argparse
import mmap
import struct
import sys
from array import array

HEADER = struct.Struct("<8s7I")
RECORD = struct.Struct("<BBH12IiIQ")
MAGIC = b"RCTRACE1"
VERSION = 1
RECORD_SIZE = 68
ENTRY_FIELDS = (("depth", 4), ("ret_addr", 6), ("esp", 7), ("ecx", 8), ("edx", 9),
                ("arg0", 10), ("arg1", 11), ("arg2", 12), ("arg3", 13))
RETURN_FIELDS = (("depth", 4), ("eax", 14), ("edx", 9), ("esp_delta", 15),
                 ("x87_top", 16), ("st0_valid", 1))
HEX_FIELDS = {"addr", "ret_addr", "esp", "ecx", "edx", "arg0", "arg1", "arg2",
              "arg3", "eax", "st0_bits"}


class Trace:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "rb")
        self.data = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        if len(self.data) < HEADER.size:
            raise ValueError(f"{path}: not a trace file")
        magic, version, record_size, _flags, start, start_n, lo, hi = HEADER.unpack_from(self.data)
        if magic != MAGIC:
            raise ValueError(f"{path}: bad magic {magic!r}")
        if version != VERSION or record_size != RECORD_SIZE:
            raise ValueError(f"{path}: unsupported version {version} record {record_size}")
        self.start, self.start_n, self.lo, self.hi = start, start_n, lo, hi
        self.offsets = {}
        end = len(self.data)
        offset = HEADER.size
        while offset + RECORD_SIZE <= end:
            tid = struct.unpack_from("<I", self.data, offset + 4)[0]
            self.offsets.setdefault(tid, array("Q")).append(offset)
            offset += RECORD_SIZE
        if offset != end:
            print(f"{path}: ignoring {end - offset} trailing bytes", file=sys.stderr)

    def record(self, offset):
        return RECORD.unpack_from(self.data, offset)

    def close(self):
        self.data.close()
        self.file.close()


def kind_name(kind):
    return "entry" if kind == 1 else "return" if kind == 2 else f"kind{kind}"


def format_field(name, value):
    if name == "kind":
        return kind_name(value)
    if name == "st0_bits":
        return f"0x{value:016x}"
    if name in HEX_FIELDS:
        return f"0x{value & 0xffffffff:08x}"
    return str(value)


def format_event(index, rec):
    kind = rec[0]
    parts = [f"{index + 1:>8} {kind_name(kind):6} 0x{rec[5]:08x} tid={rec[3]} depth={rec[4]}"]
    if kind == 1:
        parts.append(f"ret=0x{rec[6]:08x} esp=0x{rec[7]:08x} ecx=0x{rec[8]:08x}")
        parts.append("args=[" + " ".join(f"0x{rec[i]:08x}" for i in range(10, 14)) + "]")
    else:
        parts.append(f"eax=0x{rec[14]:08x} edx=0x{rec[9]:08x} de={rec[15]:+d}")
        valid = " st0=" if rec[1] & 1 else " st0=-"
        parts.append(f"{valid}0x{rec[17]:016x} top={rec[16] & 7}")
    return " ".join(parts)


IGNORED = set()


def differing_fields(a, b):
    if a[0] != b[0]:
        return None
    fields = ENTRY_FIELDS if a[0] == 1 else RETURN_FIELDS
    found = []
    for name, index in fields:
        if name == "st0_valid":
            va, vb = a[1] & 1, b[1] & 1
        else:
            va, vb = a[index], b[index]
        if va != vb and name not in IGNORED:
            found.append((name, va, vb))
    if a[0] == 2 and (a[1] & 1) and (b[1] & 1) and a[17] != b[17] and "st0_bits" not in IGNORED:
        found.append(("st0_bits", a[17], b[17]))
    return found


def apply_event(stack, rec):
    kind, depth, addr = rec[0], rec[4], rec[5]
    if kind == 1:
        if depth and depth - 1 <= len(stack):
            del stack[depth - 1:]
        stack.append(addr)
    elif kind == 2:
        if depth and depth - 1 <= len(stack):
            del stack[depth - 1:]
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == addr:
                    del stack[i:]
                    break


def context(trace, offsets, index, before, after):
    lo = max(0, index - before)
    hi = min(len(offsets), index + after + 1)
    return [(i, trace.record(offsets[i])) for i in range(lo, hi)]


def report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, index, reason, da, db,
                  before, after):
    print(f"\nthread {tid}: first divergence at event {index + 1} "
          f"(A {len(offs_a)} events, B {len(offs_b)} events)")
    print(f"  {reason}")
    if da is not None and db is not None and da[0] == db[0]:
        print("  differing fields:")
        for name, va, vb in differing_fields(da, db) or []:
            print(f"    {name}: A={format_field(name, va)} B={format_field(name, vb)}")
        if da[5] != db[5]:
            print(f"    callee: A=0x{da[5]:08x} B=0x{db[5]:08x}")
    for label, trace, offsets in (("A", a, offs_a), ("B", b, offs_b)):
        print(f"  context {label} (trace {trace.path}):")
        for i, rec in context(trace, offsets, index, before, after):
            marker = ">" if i == index else " "
            print(f"   {marker} {format_event(i, rec)}")
    if da is not None and da[0] == 1:
        stack_a = stack_a + [da[5]]
        stack_b = stack_b + [db[5]] if db is not None and db[0] == 1 else stack_b
    print("  open calls A: " + (" -> ".join(f"0x{x:08x}" for x in stack_a) or "(none)"))
    print("  open calls B: " + (" -> ".join(f"0x{x:08x}" for x in stack_b) or "(none)"))


def compare(a, b, before, after, only_tid=None):
    tids = sorted(set(a.offsets) | set(b.offsets))
    if only_tid is not None:
        tids = [tid for tid in tids if tid == only_tid]
    divergences = 0
    for tid in tids:
        offs_a = a.offsets.get(tid, array("Q"))
        offs_b = b.offsets.get(tid, array("Q"))
        stack_a, stack_b = [], []
        limit = max(len(offs_a), len(offs_b))
        for i in range(limit):
            rec_a = a.record(offs_a[i]) if i < len(offs_a) else None
            rec_b = b.record(offs_b[i]) if i < len(offs_b) else None
            if rec_a is None:
                report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, i,
                              "A has no event here", rec_a, rec_b, before, after)
                divergences += 1
                break
            if rec_b is None:
                report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, i,
                              "B has no event here", rec_a, rec_b, before, after)
                divergences += 1
                break
            if rec_a[0] != rec_b[0]:
                report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, i,
                              "event kind differs", rec_a, rec_b, before, after)
                divergences += 1
                break
            if rec_a[5] != rec_b[5]:
                report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, i,
                              "callee sequence differs", rec_a, rec_b, before, after)
                divergences += 1
                break
            diffs = differing_fields(rec_a, rec_b)
            if diffs:
                report_thread(tid, a, b, offs_a, offs_b, stack_a, stack_b, i,
                              "call matches but fields differ", rec_a, rec_b, before, after)
                divergences += 1
                break
            apply_event(stack_a, rec_a)
            apply_event(stack_b, rec_b)
    if not divergences:
        print("no divergence")
    return 1 if divergences else 0


def dump(trace, only_tid=None, max_events=None):
    print(f"trace {trace.path}: start=0x{trace.start:08x}#{trace.start_n} "
          f"range=0x{trace.lo:08x}:0x{trace.hi:08x}")
    tids = [only_tid] if only_tid is not None else sorted(trace.offsets)
    shown = 0
    for tid in tids:
        offsets = trace.offsets.get(tid, ())
        for i, offset in enumerate(offsets):
            if max_events is not None and shown >= max_events:
                return 0
            print(format_event(i, trace.record(offset)))
            shown += 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("traces", nargs="+", help="A.trace B.trace, or one trace with --dump")
    parser.add_argument("--context", type=int, default=3, help="events before/after (default 3)")
    parser.add_argument("--thread", type=lambda x: int(x, 0), help="only this guest thread id")
    parser.add_argument("--dump", action="store_true", help="decode one trace instead of diffing")
    parser.add_argument("--max-events", type=int, help="limit decoded events with --dump")
    parser.add_argument("--ignore", default="", help="comma-separated fields to ignore, e.g. ecx,edx")
    args = parser.parse_args()
    if args.dump:
        if len(args.traces) != 1:
            parser.error("--dump takes exactly one trace")
    elif len(args.traces) != 2:
        parser.error("diff takes exactly two traces")

    IGNORED.update(name for name in args.ignore.split(",") if name)
    traces = [Trace(path) for path in args.traces]
    try:
        if args.dump:
            return dump(traces[0], args.thread, args.max_events)
        return compare(traces[0], traces[1], args.context, args.context, args.thread)
    finally:
        for trace in traces:
            trace.close()


if __name__ == "__main__":
    sys.exit(main())
