"""Whole-image census of absolute global reads, writes and escapes."""
import argparse
import json
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import numpy as np

KIT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(KIT / "tools"), str(KIT / "tools" / "recomp")]

from .dispatch import Image, ProofError  # noqa: E402
from .facts import cached, collect, contracts, default_jobs, source_key  # noqa: E402

FRESH_IMPORTS = frozenset(("HeapAlloc", "VirtualAlloc", "GlobalAlloc", "LocalAlloc"))


def _operand_escape(image, addr, value):
    owner, start = image.instruction(addr)
    if start is None:
        return "value"
    length = next((n for at, n in image.starts[owner] if at == start), None)
    if length is None:
        return "value"
    raw = image.program.pe.bytes_at(start, length)
    record = image.program.pe.decode_span_record(start, raw)
    if record is None:
        return "value"
    exact = False
    for operand in record.operands:
        if operand.kind == "imm" and operand.immediate == value:
            return "value"
        if operand.kind == "mem" and operand.displacement == value:
            if operand.base is not None or operand.index is not None:
                return "indexed"
            exact = True
    return None if exact else "value"


def _window_escapes(image, globals_, window):
    if not window:
        return {}, {}
    values = sorted(image.code_values)
    candidates = set()
    for addr in globals_:
        candidates.update(values[bisect_left(values, addr - window):bisect_left(values, addr)])
    if not candidates:
        return {}, {}
    wanted = np.array(sorted(candidates), dtype=np.uint32)
    words = image.words
    positions = np.searchsorted(wanted, words)
    valid = (positions < len(wanted)) & (wanted[np.minimum(positions, len(wanted) - 1)] == words)
    hits = np.flatnonzero(valid & image.executable[:len(words)])
    occurrences, escaped = defaultdict(list), {}
    for at in hits.tolist():
        value = int(words[at])
        addr = image.program.pe.base + at
        if not image._operand(addr, value):
            continue
        kind = _operand_escape(image, addr, value)
        occurrences[value].append(addr)
        if kind is not None:
            escaped.setdefault(value, kind)
    found = {}
    for addr in globals_:
        reasons = []
        for value in values[bisect_left(values, addr - window):bisect_left(values, addr)]:
            if value in escaped and value in occurrences:
                reasons.append((value, escaped[value], occurrences[value]))
        if reasons:
            found[addr] = reasons
    return found, escaped


def census(image, facts, escape_window=0x1000):
    reads, writes, recognised = defaultdict(list), defaultdict(list), defaultdict(set)
    for writer, found in facts.items():
        for read in found.global_reads:
            reads[read.addr].append((writer, read))
            recognised[read.addr].add(read.site)
        for write in found.global_writes:
            writes[write.addr].append((writer, write))
            recognised[write.addr].add(write.site)
    global_addrs = sorted(set(reads) | set(writes))
    below, _ = _window_escapes(image, global_addrs, escape_window)
    report = []
    for addr in global_addrs:
        escapes = []
        for at, code in image.literals(addr):
            if code:
                _, start = image.instruction(at)
                if start is None or start not in recognised[addr]:
                    escapes.append((at, code))
            else:
                escapes.append((at, code))
        rows, dependencies = [], set()
        for writer, write in writes[addr]:
            value = write.value
            local = _dependencies(image, writer, write)
            dependencies |= local
            rows.append({"site": write.site, "writer": writer, "size": write.size, "value": value,
                         "class": _value_text(value, local)})
        reasons = ["%s literal at 0x%08x is not a lifted global access" % (
            "code" if code else "data", at) for at, code in escapes]
        reasons += ["%s literal 0x%08x below the global at 0x%08x is not an exact access" % (
            kind, value, at) for value, kind, sites in below.get(addr, ())
            for at in sites[:1]]
        for writer, write in writes[addr]:
            value = write.value
            if value[0] == "arg":
                local = _dependencies(image, writer, write)
                if local:
                    reasons += ["write at 0x%08x takes argument %d through unbounded slot 0x%08x+%#x" % (
                        write.site, value[1], table, offset) for table, offset in sorted(local)]
                else:
                    reasons.append("write at 0x%08x takes argument %d (arguments not linked)" % (
                        write.site, value[1]))
            elif value[0] == "arg_modified":
                reasons.append("write at 0x%08x takes argument %d after a store to its slot" % (
                    write.site, value[1]))
            elif _unlinked(value):
                reasons.append("write at 0x%08x has unlinked value %s" % (write.site, _value_text(value, ())))
        report.append({
            "addr": addr,
            "reads": [{"site": read.site, "reader": writer, "size": read.size} for writer, read in reads[addr]],
            "writes": rows,
            "escapes": [at for at, _ in escapes],
            "below_literals": [{"literal": value, "kind": kind, "distance": addr - value,
                                "sites": sites} for value, kind, sites in below.get(addr, ())],
            "dependencies": [{"table": table, "offset": offset} for table, offset in sorted(dependencies)],
            "open": bool(reasons),
            "reasons": reasons,
        })
    return report


def _dependencies(image, writer, write):
    if write.value[0] != "arg" or image.callers(writer):
        return set()
    found = set()
    for at, code in image.literals(writer):
        if code:
            continue
        try:
            table = image.table_of(at)
            if at - table in image.extent(table):
                found.add((table, at - table))
        except ProofError:
            pass
    return found


def _unlinked(value):
    kind = value[0]
    if kind == "phi":
        return any(_unlinked(item) for item in value[1])
    if kind == "import":
        return value[2].rsplit("!", 1)[-1] not in FRESH_IMPORTS
    if kind == "offset":
        return _unlinked(value[1])
    return kind in ("reg", "field", "stack", "other", "arg_modified") or (kind == "call" and value[2] is None)


def _value_text(value, dependencies):
    kind = value[0]
    if kind == "null":
        return "null"
    if kind == "const":
        return "constant 0x%08x" % value[1]
    if kind == "call":
        return "post-call 0x%08x -> 0x%08x (%s)" % (value[1], value[2] or 0, value[3])
    if kind == "import":
        return "import %s (%s)" % (value[2], value[3])
    if kind == "offset":
        return "offset of %s%+#x" % (_value_text(value[1], ()), value[2])
    if kind in ("arg", "arg_modified"):
        suffix = "".join(" via slot 0x%08x+%#x" % dependency for dependency in sorted(dependencies))
        return "argument %d%s%s" % (value[1], " (modified)" if kind == "arg_modified" else "", suffix)
    if kind == "reg":
        return "entry register %s" % value[1]
    if kind == "global":
        return "global load 0x%08x" % value[1]
    if kind == "field":
        return "field load [%s+%#x]" % (_value_text(value[1], ()), value[2])
    if kind == "phi":
        return "phi(%s)" % ", ".join(_value_text(item, ()) for item in value[1])
    if kind == "stack":
        return "stack value %#x" % value[1]
    if kind == "arg":
        return "argument %d" % value[1]
    return "unknown %s" % value[1]


def main():
    import game_config
    from program import Program
    from settings import Settings
    parser = argparse.ArgumentParser(description="Census absolute global reads, writes and escapes")
    game_config.add_game_args(parser)
    parser.add_argument("--jobs", type=int, default=default_jobs())
    parser.add_argument("--out", type=Path)
    parser.add_argument("--explain", type=lambda value: int(value, 16), action="append", default=[])
    parser.add_argument("--no-abi", action="store_true")
    args = parser.parse_args()
    game_config.resolve_game_args(args)
    settings = Settings(game_config.load(args.game_dir, args.build_root), None, args.jobs, None)
    started = time.monotonic()
    cache = Path(args.build_root) / "typeinf"
    key = source_key(settings)
    preserved = cached(cache / "contracts.pickle", key, lambda: contracts(settings, args.jobs))
    facts = cached(cache / "dispatch-facts.pickle", (key, not args.no_abi),
                   lambda: collect(settings, preserved, args.jobs, abi=not args.no_abi))
    report = census(Image(Program(settings)), facts)
    stats = {"globals": len(report), "open": sum(1 for row in report if row["open"]),
             "writes": sum(len(row["writes"]) for row in report),
             "reads": sum(len(row["reads"]) for row in report),
             "seconds": round(time.monotonic() - started, 1)}
    output = {"stats": stats, "globals": report}
    if args.out:
        args.out.write_text(json.dumps(output, indent=1, default=_plain))
    for addr in args.explain:
        row = next((row for row in report if row["addr"] == addr), None)
        print(json.dumps(row, indent=1, default=_plain) if row else "no global 0x%08x" % addr)
    print(json.dumps(stats, indent=1))


def _plain(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value))


if __name__ == "__main__":
    main()
