import argparse
import bisect
import json
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import pefile

SC_RE = re.compile(r"SC\[(\S+)\]\s+\|\s+mod = \d+,\s+(\d+):(\d+),\s+size = (\d+)")
FPO_ROW_RE = re.compile(r"^\s*([0-9A-Fa-f]{8})\s*\|")

IMAGE_SCN_CNT_CODE = "IMAGE_SCN_CNT_CODE"
IMAGE_SCN_MEM_EXECUTE = 0x20000000
SECTION_HEADER_SIZE = 40


def pdbutil(pdbutil_path, pdb, mode):
    result = subprocess.run([pdbutil_path, "dump", mode, str(pdb)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("llvm-pdbutil dump %s failed:\n%s" % (mode, result.stderr))
    return result.stdout


def stream_index(pdbutil_path, pdb, label):
    match = re.search(r"Stream\s+(\d+) \(\s*\d+ bytes\): \[%s\]" % re.escape(label),
                      pdbutil(pdbutil_path, pdb, "--streams"))
    if not match:
        raise SystemExit("PDB has no %s stream" % label)
    return int(match.group(1))


def export_stream(pdbutil_path, pdb, label):
    stream = stream_index(pdbutil_path, pdb, label)
    handle, name = tempfile.mkstemp(prefix="recomp-pdb-stream-")
    os.close(handle)
    result = subprocess.run([pdbutil_path, "export", "--stream=%d" % stream,
                             "--out=%s" % name, str(pdb)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("llvm-pdbutil export stream %d failed:\n%s" % (stream, result.stderr))
    data = Path(name).read_bytes()
    Path(name).unlink()
    return data


def parse_original_sections(data):
    sections = []
    for offset in range(0, len(data) - SECTION_HEADER_SIZE + 1, SECTION_HEADER_SIZE):
        name = data[offset:offset + 8].split(b"\0")[0].decode(errors="replace")
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, offset + 8)
        characteristics = struct.unpack_from("<I", data, offset + 36)[0]
        sections.append({"name": name, "vsize": vsize, "va": va, "raw_size": raw_size,
                         "raw_ptr": raw_ptr, "characteristics": characteristics})
    return sections


def parse_omap(data):
    pairs = []
    for offset in range(0, len(data) - 8 + 1, 8):
        pairs.append(struct.unpack_from("<II", data, offset))
    pairs.sort()
    return pairs


def omap_translate(omap, omap_sources, rva):
    index = bisect.bisect_right(omap_sources, rva) - 1
    if index < 0:
        return None
    source, destination = omap[index]
    if destination == 0:
        return None
    return destination + (rva - source)


def merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def parse_contributions(text, sections):
    contributions = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = SC_RE.search(line)
        if not match:
            continue
        section = int(match.group(2))
        offset = int(match.group(3))
        size = int(match.group(4))
        characteristics = next((following for following in lines[index + 1:] if following.strip()), "")
        if not 1 <= section <= len(sections):
            raise SystemExit("contribution names section %d outside the original image" % section)
        header = sections[section - 1]
        contributions.append({
            "start": header["va"] + offset,
            "size": size,
            "code": IMAGE_SCN_CNT_CODE in characteristics,
            "exec": bool(header["characteristics"] & IMAGE_SCN_MEM_EXECUTE),
        })
    contributions.sort(key=lambda entry: entry["start"])
    return contributions


def project(contributions, omap):
    starts = [entry["start"] for entry in contributions]
    code = []
    data = []
    for index in range(len(omap)):
        source, destination = omap[index]
        if destination == 0:
            continue
        source_next = omap[index + 1][0] if index + 1 < len(omap) else None
        if source_next is None or source_next <= source:
            continue
        position = bisect.bisect_right(starts, source) - 1
        if position < 0:
            continue
        entry = contributions[position]
        if not entry["start"] <= source < entry["start"] + entry["size"]:
            continue
        piece_end = min(source_next, entry["start"] + entry["size"])
        if piece_end <= source:
            continue
        final = (destination, destination + (piece_end - source))
        if entry["code"]:
            code.append(final)
        elif entry["exec"]:
            data.append(final)
    return merge(code), merge(data)


def fpo_functions(text):
    functions = {}
    mode = None
    for line in text.splitlines():
        if "Old FPO Data" in line:
            mode = "old"
            continue
        if "New FPO Data" in line:
            mode = "new"
            continue
        match = FPO_ROW_RE.match(line)
        if not match:
            continue
        fields = [field.strip() for field in line.split("|")]
        if mode == "old" or len(fields) > 9 and fields[9] == "true":
            start = int(match.group(1), 16)
            functions[start] = max(functions.get(start, 0), int(fields[1]))
    return functions


def function_ranges(functions, omap):
    starts = sorted(functions)
    sources = [source for source, _ in omap]
    ranges = []
    for index, (source, destination) in enumerate(omap):
        source_next = omap[index + 1][0] if index + 1 < len(omap) else None
        if destination == 0 or source_next is None or source_next <= source:
            continue
        position = max(0, bisect.bisect_right(starts, source) - 1)
        while position < len(starts) and starts[position] < source_next:
            start = starts[position]
            lo, hi = max(source, start), min(source_next, start + functions[start])
            entry = omap_translate(omap, sources, start)
            if lo < hi and entry is not None:
                ranges.append([destination + lo - source, destination + hi - source, entry])
            position += 1
    merged = []
    for lo, hi, entry in sorted(ranges):
        if merged and merged[-1][1] == lo and merged[-1][2] == entry:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi, entry])
    return merged


def export_addresses(pe):
    addresses = set()
    directory = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
    if directory:
        for symbol in directory.symbols:
            if symbol.address:
                addresses.add(symbol.address)
    return addresses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--pdbutil", default="llvm-pdbutil")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    pe = pefile.PE(str(args.exe))
    sections = parse_original_sections(export_stream(args.pdbutil, args.pdb, "Section Header Original Data"))
    omap = parse_omap(export_stream(args.pdbutil, args.pdb, "Omap From Source Data"))
    omap_sources = [source for source, _ in omap]
    contributions = parse_contributions(
        pdbutil(args.pdbutil, args.pdb, "--section-contribs"), sections)
    _, data = project(contributions, omap)

    functions = fpo_functions(pdbutil(args.pdbutil, args.pdb, "--fpo"))
    entries = set(export_addresses(pe))
    for start in functions:
        final = omap_translate(omap, omap_sources, start)
        if final is not None:
            entries.add(final)
    ranges = function_ranges(functions, omap)
    evidence = {
        "source": args.pdb.name,
        "data_ranges": [[start, end] for start, end in data],
        "entries": sorted(entries),
        "functions": ranges,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=1) + "\n")
    print("%d omap pairs, %d data ranges, %d entries, %d function ranges -> %s"
          % (len(omap), len(data), len(entries), len(ranges), args.out))


if __name__ == "__main__":
    main()
