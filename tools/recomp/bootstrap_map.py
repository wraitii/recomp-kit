import argparse
from array import array
import bisect
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

import capstone
import capstone.x86_const as x86
import pefile
import pypcode

import code_map
from program import PE, TranslateError


class Recovery:
    def __init__(self, executable, base):
        self.pe = pefile.PE(str(executable))
        self.image = PE(executable, base)
        self.delta = self.image.base - self.pe.OPTIONAL_HEADER.ImageBase
        self.original = self.pe.get_memory_mapped_image()
        self.relocations = [(self.image.rd32(self.image.base + entry.rva), self.image.base + entry.rva)
                            for block in getattr(self.pe, "DIRECTORY_ENTRY_BASERELOC", ())
                            for entry in block.entries if entry.type == 3]
        self.reloc_slots = {slot for _, slot in self.relocations}
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True
        self.sleigh = pypcode.Context("x86:LE:32:default")
        self.instructions = {}
        self.covered = {}
        self.speculative = set()
        self.trusted_seeds = set()
        self.ghidra_spans = []
        self.ghidra_entries = {}
        self.seeds = defaultdict(set)
        self.pending = deque()
        self.roots = set()
        self.tables = []
        self.gaps = []
        self.traps = {}
        self.data_ranges = []
        self.excluded_bytes = 0
        self.known_mask = bytearray(self.pe.OPTIONAL_HEADER.SizeOfImage)
        self.has_evidence = False
        self.data_mask = None
        self.data_owner = None
        self.conflicts = set()
        self.function_ranges = []
        self.function_starts = []
        for index, directory in enumerate(self.pe.OPTIONAL_HEADER.DATA_DIRECTORY):
            if index != 4 and directory.VirtualAddress and directory.Size:
                self.data_ranges.append((self.image.base + directory.VirtualAddress,
                                         self.image.base + directory.VirtualAddress + directory.Size, "PE directory"))
        for entry in getattr(self.pe, "DIRECTORY_ENTRY_DEBUG", ()):
            if entry.struct.AddressOfRawData and entry.struct.SizeOfData:
                self.data_ranges.append((self.image.base + entry.struct.AddressOfRawData,
                                         self.image.base + entry.struct.AddressOfRawData + entry.struct.SizeOfData, "PE debug data"))
        self.add_import_data_ranges()

    def add_import_data_ranges(self):
        def add(rva, size):
            if not rva or size <= 0:
                return
            low, high = self.image.base + rva, self.image.base + rva + size
            for start, end in self.image.exec_ranges:
                if max(low, start) < min(high, end):
                    self.data_ranges.append((max(low, start), min(high, end), "PE import data"))

        for descriptor in getattr(self.pe, "DIRECTORY_ENTRY_IMPORT", ()):
            count = len(descriptor.imports)
            add(descriptor.struct.Name, len(descriptor.dll) + 1)
            for array in (descriptor.struct.OriginalFirstThunk, descriptor.struct.FirstThunk):
                add(array, 4 * (count + 1))
            for entry in descriptor.imports:
                if entry.name and entry.hint_name_table_rva:
                    add(entry.hint_name_table_rva, 2 + len(entry.name) + 1)
        for descriptor in getattr(self.pe, "DIRECTORY_ENTRY_DELAY_IMPORT", ()):
            count = len(descriptor.imports)
            add(descriptor.struct.szName, len(descriptor.dll) + 1)
            for array in (descriptor.struct.pINT, descriptor.struct.pIAT, descriptor.struct.pBoundIAT, descriptor.struct.pUnloadIAT):
                add(array, 4 * (count + 1))
            add(descriptor.struct.phmod, 4)
            for entry in descriptor.imports:
                if entry.name and entry.hint_name_table_rva:
                    add(entry.hint_name_table_rva, 2 + len(entry.name) + 1)

    def load_evidence(self, path):
        if not path:
            return
        self.has_evidence = True
        evidence = json.loads(Path(path).read_text())
        for start, end in evidence.get("data_ranges", ()):
            self.data_ranges.append((self.image.base + start, self.image.base + end, "PDB data"))
            self.known_mask[start:end] = b"\x01" * (end - start)
        for rva in evidence.get("entries", ()):
            self.seed(self.image.base + rva, "PDB entry", True)
        self.function_ranges = [(self.image.base + lo, self.image.base + hi, self.image.base + entry)
                                for lo, hi, entry in evidence.get("functions", ())]
        self.function_starts = [lo for lo, _, _ in self.function_ranges]

    def function_of(self, at):
        index = bisect.bisect_right(self.function_starts, at) - 1
        if index >= 0 and at < self.function_ranges[index][1]:
            return self.function_ranges[index][2]
        return None

    def at_known_data(self, at):
        offset = at - self.image.base
        return 0 <= offset < len(self.known_mask) and self.known_mask[offset]

    def build_data_mask(self):
        size = self.pe.OPTIONAL_HEADER.SizeOfImage
        self.data_mask = bytearray(size)
        self.data_owner = array("i", [-1]) * size
        for index, (lo, hi, _) in enumerate(self.data_ranges):
            for offset in range(max(0, lo - self.image.base), min(size, hi - self.image.base)):
                if not self.data_mask[offset]:
                    self.data_mask[offset] = 1
                    self.data_owner[offset] = index

    def data_containing(self, at):
        offset = at - self.image.base
        if not 0 <= offset < len(self.data_mask) or not self.data_mask[offset]:
            return None
        return self.data_ranges[self.data_owner[offset]]

    def first_data_start(self, at, size):
        offset = at - self.image.base
        for step in range(1, size):
            if 0 <= offset + step < len(self.data_mask) and self.data_mask[offset + step]:
                return at + step
        return None

    def seed(self, target, evidence, root=False, speculative=False):
        if not self.image.is_exec(target):
            return
        if evidence.startswith("direct:") and self.at_known_data(target):
            owner = self.ghidra_owner(target)
            if owner not in (None, target):
                self.ghidra_entries[target] = owner
            return
        if root:
            self.roots.add(target)
        first = target not in self.seeds
        self.seeds[target].add(evidence)
        if not speculative:
            self.trusted_seeds.add(target)
        owner = self.covered.get(target)
        if evidence.startswith("direct:") and not speculative and owner not in (None, target):
            while owner not in (None, target):
                self.retract(owner)
                owner = self.covered.get(target)
            first = True
        if owner in self.speculative and (owner != target or not speculative):
            self.retract(owner)
            first = True
        elif owner is not None and owner != target:
            if not speculative:
                self.conflicts.add((target, owner, evidence))
            return
        if first:
            self.pending.append(target)

    def ghidra_owner(self, target):
        for start, end, owner in self.ghidra_spans:
            if start <= target < end:
                at = start
                while at < target:
                    ins = self.decode(at)
                    if ins is None:
                        return None
                    at += ins.size
                return owner if at == target else None
        return None

    def retract(self, address):
        self.speculative.discard(address)
        size = self.instructions.pop(address).size
        for at in range(address, address + size):
            if self.covered.get(at) == address:
                del self.covered[at]

    def decode(self, address):
        end = next(hi for lo, hi in self.image.exec_ranges if lo <= address < hi)
        raw = self.image.bytes_at(address, min(15, end - address))
        ins = next(self.md.disasm(raw, address, count=1), None)
        if ins is None:
            return None
        try:
            decoded = self.sleigh.disassemble(raw, address, max_instructions=1).instructions
        except Exception:
            return None
        if decoded and decoded[0].length != ins.size and ins.mnemonic == "wait":
            folded = list(self.md.disasm(raw[:decoded[0].length], address))
            if len(folded) == 2 and folded[1].mnemonic.startswith("fn") and sum(i.size for i in folded) == decoded[0].length:
                ins = SimpleNamespace(address=address, size=decoded[0].length, mnemonic=folded[1].mnemonic,
                                      operands=folded[1].operands, group=folded[1].group)
        if not decoded or decoded[0].length != ins.size:
            self.conflicts.add((address, address, "SLEIGH/Capstone boundary disagreement"))
            return None
        return ins

    def pointer_run_at(self, at):
        for step in range(2):
            slot = at + 4 * step
            if slot not in self.reloc_slots or not self.image.is_exec(self.image.rd32(slot)):
                return False
        return True

    def record(self, ins, speculative=False):
        if self.pointer_run_at(ins.address):
            return False
        span = range(ins.address, ins.address + ins.size)
        if any(self.at_known_data(at) for at in span):
            return False
        for at in span:
            owner = self.covered.get(at)
            if owner is not None and owner != ins.address and (speculative or owner not in self.speculative):
                if not speculative:
                    self.conflicts.add((ins.address, owner, "overlapping decode"))
                return False
            if at != ins.address and at in self.seeds and (speculative or at in self.trusted_seeds):
                if not speculative:
                    self.conflicts.add((at, ins.address, "; ".join(sorted(self.seeds[at]))))
                return False
        if not speculative:
            for at in span:
                owner = self.covered.get(at)
                if owner in self.speculative and owner != ins.address:
                    self.retract(owner)
        self.instructions[ins.address] = ins
        self.covered.update((at, ins.address) for at in span)
        if speculative:
            self.speculative.add(ins.address)
        if ins.group(capstone.CS_GRP_CALL) or ins.group(capstone.CS_GRP_JUMP):
            if ins.operands and ins.operands[0].type == x86.X86_OP_IMM:
                target = ins.operands[0].imm & 0xffffffff
                self.seed(target, "direct:%08x" % ins.address, ins.group(capstone.CS_GRP_CALL) and not speculative, speculative)
        return True

    def trace(self):
        while self.pending:
            at = self.pending.popleft()
            speculative = at not in self.trusted_seeds
            while self.image.is_exec(at):
                if self.at_known_data(at):
                    owner = self.ghidra_owner(at)
                    if owner not in (None, at):
                        self.ghidra_entries[at] = owner
                    break
                if at in self.covered:
                    owner = self.covered[at]
                    if speculative or owner not in self.speculative:
                        if owner != at and not speculative:
                            self.conflicts.add((at, owner, "entry alignment"))
                        break
                    self.retract(owner)
                    continue
                ins = self.decode(at)
                if ins is None or not self.record(ins, speculative):
                    break
                if ins.group(capstone.CS_GRP_RET) or ins.group(capstone.CS_GRP_IRET) or ins.mnemonic in ("jmp", "ljmp", "int3", "ud2"):
                    break
                at += ins.size

    def initial_seeds(self):
        self.seed(self.image.base + self.pe.OPTIONAL_HEADER.AddressOfEntryPoint, "PE entry", True)
        for symbol in getattr(getattr(self.pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", ()):
            if not symbol.forwarder:
                self.seed(self.image.base + symbol.address, "export:%d" % symbol.ordinal, True)
        for target, slot in self.relocations:
            if not self.image.is_exec(slot) and not self.at_known_data(target):
                self.seed(target, "relocation:%08x" % slot, False, True)
        tls = getattr(self.pe, "DIRECTORY_ENTRY_TLS", None)
        if tls and tls.struct.AddressOfCallBacks:
            at = tls.struct.AddressOfCallBacks + self.delta
            while self.image.rd32(at):
                self.seed(self.image.rd32(at), "TLS:%08x" % at, True)
                at += 4
        self.trace()
        for target, slot in self.relocations:
            owner = self.covered.get(slot)
            ins = self.instructions.get(owner)
            if ins and any(op.type == x86.X86_OP_IMM and op.imm & 0xffffffff == target for op in ins.operands):
                if not self.at_known_data(target):
                    self.seed(target, "relocated-immediate:%08x" % owner, False, True)
        self.trace()

    def pointer_tables(self):
        for section in self.pe.sections:
            if not section.Characteristics & 0x40000000:
                continue
            start = self.image.base + section.VirtualAddress
            end = start + section.SizeOfRawData
            at = start
            while at + 8 <= end:
                slots = []
                while at + 4 <= end and not any(p in self.covered for p in range(at, at + 4)):
                    offset = at - self.image.base
                    original_target = int.from_bytes(self.original[offset:offset + 4], "little") + self.delta
                    target = self.image.rd32(at)
                    if target != original_target or not self.image.is_exec(target):
                        break
                    slots.append((at, target))
                    at += 4
                if len(slots) >= 2:
                    self.tables.append((slots[0][0], at))
                    for slot, target in slots:
                        if not self.at_known_data(target):
                            self.seed(target, "pointer-run:%08x" % slot, False, True)
                if len(slots) < 2:
                    at = slots[0][0] + 1 if slots else at + 1
        self.trace()

    def index_tables(self):
        ordered = sorted(self.instructions)
        pointer_tables = dict(self.tables)
        for position, at in enumerate(ordered):
            ins = self.instructions[at]
            if ins.mnemonic != "movzx" or len(ins.operands) != 2:
                continue
            dest, source = ins.operands
            if dest.type != x86.X86_OP_REG or source.type != x86.X86_OP_MEM or source.size != 1:
                continue
            memory = source.mem
            index = memory.base or memory.index
            if not index or memory.base and memory.index or memory.scale != 1:
                continue
            lo = memory.disp & 0xffffffff
            jump = self.instructions.get(at + ins.size)
            if not jump or jump.mnemonic != "jmp" or jump.operands[0].type != x86.X86_OP_MEM:
                continue
            dispatch = jump.operands[0].mem
            table = dispatch.disp & 0xffffffff
            if dispatch.base or dispatch.index != dest.reg or dispatch.scale != 4 or table not in pointer_tables:
                continue
            bound = None
            for previous in reversed(ordered[max(0, position - 5):position]):
                compare = self.instructions[previous]
                if compare.mnemonic == "cmp" and len(compare.operands) == 2:
                    left, right = compare.operands
                    if left.type == x86.X86_OP_REG and left.reg == index and right.type == x86.X86_OP_IMM:
                        bound = right.imm
                    break
                if compare.mnemonic not in ("ja", "push", "nop"):
                    break
            if bound is None or not 0 <= bound < 65536 or not self.image.is_exec(lo):
                count = (pointer_tables[table] - table) // 4
                if (self.has_evidence and bound is None and self.image.is_exec(lo)
                        and lo < at and not self.at_known_data(lo)
                        and all(value < count for value in self.image.bytes_at(lo, at - lo))):
                    self.data_ranges.append((lo, at, "switch byte table"))
                continue
            hi = lo + bound + 1
            count = (pointer_tables[table] - table) // 4
            if not self.image.is_exec(hi - 1) or any(p in self.covered for p in range(lo, hi)):
                continue
            if any(value >= count for value in self.image.bytes_at(lo, hi - lo)):
                continue
            self.data_ranges.append((lo, hi, "switch byte table"))

    def sweep(self):
        tables = dict(self.tables)
        table_starts = sorted(tables)
        self.build_data_mask()
        for start, end in self.image.exec_ranges:
            at = start
            while at < end:
                if at in self.covered:
                    at = self.covered[at] + self.instructions[self.covered[at]].size
                    continue
                data = self.data_containing(at)
                if data:
                    _, hi, kind = data
                    self.gaps.append((at, min(hi, end), kind))
                    at = min(hi, end)
                    continue
                if at in tables:
                    self.gaps.append((at, tables[at], "pointer-table"))
                    at = tables[at]
                    continue
                ins = self.decode(at)
                if ins is None:
                    self.gaps.append((at, at + 1, "undecodable"))
                    at += 1
                    continue
                position = bisect.bisect_right(table_starts, at)
                data_start = table_starts[position] if position < len(table_starts) and table_starts[position] < at + ins.size else None
                if data_start is None:
                    data_start = self.first_data_start(at, ins.size)
                if data_start is not None:
                    self.gaps.append((at, data_start, "data-boundary"))
                    at = data_start
                    continue
                if any(inner in self.covered or inner in self.seeds for inner in range(at + 1, at + ins.size)):
                    self.gaps.append((at, at + 1, "sweep-padding"))
                    at += 1
                    continue
                if not self.record(ins, True):
                    at += 1
                    continue
                self.trace()
                at += ins.size
        for ins in self.instructions.values():
            if ins.address not in self.speculative and (ins.group(capstone.CS_GRP_CALL) or ins.group(capstone.CS_GRP_JUMP)) and ins.operands and ins.operands[0].type == x86.X86_OP_IMM:
                target = ins.operands[0].imm & 0xffffffff
                if target not in self.instructions:
                    self.traps[ins.address] = "direct transfer to unmapped instruction %08x" % target
        for target, evidence in self.seeds.items():
            if target in self.trusted_seeds and target not in self.instructions and not self.at_known_data(target):
                self.conflicts.add((target, self.covered.get(target, target), "; ".join(sorted(evidence))))

    def report(self):
        return {
            "instructions": len(self.instructions), "entries": len(self.seeds), "excluded_bytes": self.excluded_bytes,
            "pointer_runs": [{"start": "%08x" % a, "end": "%08x" % b} for a, b in self.tables],
            "gaps": [{"start": "%08x" % a, "end": "%08x" % b, "kind": kind} for a, b, kind in self.gaps],
            "traps": {"%08x" % at: reason for at, reason in sorted(self.traps.items())},
            "conflicts": [{"target": "%08x" % a, "instruction": "%08x" % b, "evidence": evidence,
                           "kind": "shared" if a in self.instructions else "rejected"}
                          for a, b, evidence in sorted(self.conflicts)],
            "seeds": {"%08x" % at: sorted(evidence) for at, evidence in sorted(self.seeds.items())},
        }

    def code_map_rows(self, body_bytes=4096):
        groups = defaultdict(list)
        owned = {entry for _, _, entry in self.function_ranges}
        window = previous = None
        flows_into = None
        for at, ins in sorted(self.instructions.items()):
            if (at != flows_into and at not in self.seeds and self.function_of(at) is None
                    and set(ins.bytes) <= {0x90, 0xcc}):
                continue
            flows_into = None if ins.group(capstone.CS_GRP_RET) or ins.mnemonic in ("jmp", "ljmp", "int3", "ud2") else at + ins.size
            owner = self.function_of(at)
            if owner is None and previous and previous[0] == at and previous[1] in owned and at not in self.roots:
                owner = previous[1]
            if owner is None:
                if (window is None or previous[1] != window or at in self.roots
                        or at >= window + body_bytes):
                    window = at
                owner = window
            groups[owner].append((at, ins.size))
            previous = (at + ins.size, owner)
        for owner, group in groups.items():
            if owner not in self.instructions or self.function_of(owner) not in (None, owner):
                raise TranslateError("function %08x has no instruction at its entry" % owner)
        functions = {}
        entries = {}
        for owner, group in sorted(groups.items()):
            runs = []
            for at, size in group:
                if not runs or at != runs[-1][0] + sum(runs[-1][1]):
                    runs.append((at, []))
                runs[-1][1].append(size)
                if at != owner:
                    entries[at] = (owner, "branch")
            functions[owner] = [(start, sum(lengths), "".join("%x" % n for n in lengths)) for start, lengths in runs]
        for target, owner in self.ghidra_entries.items():
            entries.setdefault(target, (owner, "branch"))
        return functions, entries

    def publish(self, executable, output, body_bytes):
        if output.exists():
            raise TranslateError("Output exists: %s" % output)
        functions, entries = self.code_map_rows(body_bytes)
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".bootstrap-map-", dir=output.parent))
        try:
            spans = [code_map.SPANS_HEADER]
            for owner, rows in functions.items():
                for start, size, lengths in rows:
                    spans.append("%s\t%08x\t%d\t%s" % ("" if start == owner else "%08x" % owner, start, size, lengths))
            entry_rows = [code_map.ENTRIES_HEADER] + ["%08x\t%08x\t%s" % (at, owner, kind) for at, (owner, kind) in entries.items()]
            metadata = {
                "format": code_map.FORMAT, "status": "complete", "provenance": "provisional-sweep",
                "emitter": "decoded", "trap_instructions": ",".join("%08x" % at for at in sorted(self.traps)), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "image_base": "%08x" % self.image.base, "functions": str(len(functions)),
                "instructions": str(len(self.instructions)),
                "instruction_bytes": str(sum(ins.size for ins in self.instructions.values())),
            }
            (staging / "metadata.txt").write_text("".join("%s=%s\n" % item for item in metadata.items()))
            (staging / "spans.tsv").write_text("\n".join(spans) + "\n")
            (staging / "entries.tsv").write_text("\n".join(entry_rows) + "\n")
            (staging / "tables.tsv").write_text(code_map.TABLES_HEADER + "\n")
            (staging / "noreturn.tsv").write_text(code_map.NORETURN_HEADER + "\n")
            code_map.read_map(staging)
            code_map.read_program(staging)
            staging.rename(output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return len(functions)


def build(executable, base, evidence, extra_data, exclude_map=None):
    recovery = Recovery(executable, base)
    recovery.load_evidence(evidence)
    excluded = [] if exclude_map is None else [(start, start + size, "Ghidra code map")
                                               for spans in code_map.read_map(exclude_map)[1].values()
                                               for start, size, _ in spans]
    recovery.excluded_bytes = sum(hi - lo for lo, hi, _ in excluded)
    if exclude_map is not None:
        recovery.ghidra_spans = sorted((start, start + size, owner)
                                       for owner, spans in code_map.read_map(exclude_map)[1].items()
                                       for start, size, _ in spans)
    for lo, hi, kind in list(extra_data) + excluded:
        recovery.data_ranges.append((lo, hi, kind))
        recovery.known_mask[lo - recovery.image.base:hi - recovery.image.base] = b"\x01" * (hi - lo)
    recovery.initial_seeds()
    recovery.pointer_tables()
    recovery.index_tables()
    recovery.sweep()
    recovery.index_tables()
    return recovery


def discover(executable, base, exclude_map=None):
    return build(executable, base, None, [], exclude_map)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--base", type=lambda value: int(value, 0))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--body-bytes", type=int, default=4096)
    parser.add_argument("--evidence", type=Path, default=None,
                        help="JSON with data_ranges and entries (RVAs) from a PDB")
    parser.add_argument("--exclude-map", type=Path, default=None,
                        help="Ghidra code map directory whose spans are excluded from discovery")
    args = parser.parse_args()
    if args.body_bytes <= 0:
        parser.error("--body-bytes must be positive")
    recovery = build(args.exe, args.base, args.evidence, [], args.exclude_map)
    if args.evidence:
        extra = set()
        while True:
            found = {(lo, hi, kind) for lo, hi, kind in recovery.data_ranges
                     if kind == "switch byte table"}
            if not found - extra:
                break
            extra |= found
            recovery = build(args.exe, args.base, args.evidence, sorted(extra), args.exclude_map)
    report = recovery.report()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    try:
        groups = recovery.publish(args.exe, args.out, args.body_bytes)
    except TranslateError as error:
        parser.exit(1, str(error) + "; see " + str(args.report) + "\n")
    print("Published %d provisional bodies, %d instructions, %d pointer runs" %
          (groups, len(recovery.instructions), len(recovery.tables)))


if __name__ == "__main__":
    main()
