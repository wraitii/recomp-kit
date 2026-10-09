"""Ghidra-native program model: what is code, where it enters, where it flows.

The code map names function spans, jump tables, interior entries and
non-returning functions; the PE supplies bytes and imports; SLEIGH supplies
instruction effects. Nothing here decodes with Capstone or scans for code.
Unknown control flow fails with a named diagnostic: fix Ghidra and re-export.
"""
from bisect import bisect_right
import hashlib

import code_map
from ir.cfg import FunctionIR
from ir.lift import Lifter


class TranslateError(Exception):
    pass


class PE(object):
    """Mapped image bytes, executable ranges and import slots."""

    def __init__(self, path, base=None):
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
        preferred = pe.OPTIONAL_HEADER.ImageBase
        self.base = preferred if base is None else base
        self.size = pe.OPTIONAL_HEADER.SizeOfImage
        data = bytes(pe.get_memory_mapped_image())
        self.data = bytearray((data + bytes(max(0, self.size - len(data))))[:self.size])
        self.end = self.base + self.size
        reloc = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        self.reloc_dir = (reloc.VirtualAddress, reloc.Size)
        delta = self.base - preferred
        if delta:
            if not reloc.VirtualAddress or not reloc.Size:
                raise TranslateError("%s: rebased image has no relocation directory" % path)
            off, end = reloc.VirtualAddress, reloc.VirtualAddress + reloc.Size
            while off + 8 <= end:
                page = int.from_bytes(self.data[off:off + 4], "little")
                block = int.from_bytes(self.data[off + 4:off + 8], "little")
                if block < 8 or off + block > end:
                    raise TranslateError("%s: malformed relocation block at %x" % (path, off))
                for slot in range(off + 8, off + block, 2):
                    entry = int.from_bytes(self.data[slot:slot + 2], "little")
                    if entry >> 12 != 3:
                        continue
                    site = page + (entry & 0xfff)
                    if site + 4 > self.size:
                        raise TranslateError("%s: relocation outside image at %x" % (path, site))
                    value = int.from_bytes(self.data[site:site + 4], "little")
                    self.data[site:site + 4] = ((value + delta) & 0xffffffff).to_bytes(4, "little")
                off += block
        self.data = bytes(self.data)
        self.exec_ranges = [
            (self.base + s.VirtualAddress,
             self.base + s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData))
            for s in pe.sections if s.Characteristics & 0x20000000]
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        self.iat_names = {imp.address + delta: imp.name.decode("ascii", "replace")
                          for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
                          for imp in entry.imports if imp.name}
        pe.close()

    def is_exec(self, va):
        return any(lo <= va < hi for lo, hi in self.exec_ranges)

    def rd8(self, va):
        return self.data[va - self.base] if self.base <= va < self.end else None

    def rd32(self, va):
        if not (self.base <= va and va + 4 <= self.end):
            return None
        return int.from_bytes(self.data[va - self.base:va - self.base + 4], "little")

    def relocated_pointers(self):
        """{address a HIGHLOW base relocation rewrites: where it is stored}."""
        rva, size = self.reloc_dir
        out = {}
        off, end = rva, rva + size
        while size and off + 8 <= end:
            page = int.from_bytes(self.data[off:off + 4], "little")
            block = int.from_bytes(self.data[off + 4:off + 8], "little")
            if block < 8 or off + block > end:
                break
            for k in range(off + 8, off + block, 2):
                entry = int.from_bytes(self.data[k:k + 2], "little")
                site = page + (entry & 0xfff)
                if entry >> 12 == 3 and site + 4 <= self.size:
                    out.setdefault(int.from_bytes(self.data[site:site + 4], "little"), self.base + site)
            off += block
        return out

    def bytes_at(self, va, length):
        lo = va - self.base
        if lo < 0 or lo + length > self.size:
            raise TranslateError("%08x: outside the image" % va)
        return self.data[lo:lo + length]


class Function(object):
    __slots__ = ("addr", "spans", "noreturn")

    def __init__(self, addr, spans, noreturn):
        self.addr = addr
        self.spans = spans
        self.noreturn = noreturn


class Body(object):
    """A function lifted from bytes.

    `ir` is the CFG over in-body instructions. `exits` lists (instruction index,
    target) for flow that leaves the body: tail jumps, table targets in other
    functions, and the last instruction falling out of its span. `dynamic`
    indexes computed jumps with no recovered table, which dispatch at run time.
    """
    __slots__ = ("ir", "exits", "dynamic")

    def __init__(self, ir, exits, dynamic):
        self.ir = ir
        self.exits = exits
        self.dynamic = dynamic


class Program(object):
    def __init__(self, settings):
        root = settings.code_map
        self.pe = PE(settings.exe, settings.base)
        self.metadata, spans = code_map.read_map(root)
        actual_hash = hashlib.sha256(settings.exe.read_bytes()).hexdigest()
        expected_hash = settings.sha256
        if actual_hash != expected_hash or self.metadata.get("executable_sha256") != actual_hash:
            raise TranslateError("code-map, game.toml and executable SHA-256 do not match")
        if (self.pe.base != settings.base or
                self.metadata.get("image_base") != "%08x" % self.pe.base):
            raise TranslateError("code-map, game.toml and executable image bases do not match")
        self.tables, self.interior, noreturn, self.noreturn_calls = code_map.read_program(root)
        self.functions = {addr: Function(addr, rows, addr in noreturn)
                          for addr, rows in spans.items()}
        self.configured_entries = settings.configured_entries
        self.noreturn = frozenset(noreturn)
        self.lifter = Lifter()
        self._instructions = {}
        self._owner = None
        self._entries = None
        self._spans = sorted((start, start + size, addr)
                             for addr, fn in self.functions.items() for start, size, _ in fn.spans)

    def instructions(self, addr):
        """[(address, length)] of one function, in address order."""
        cached = self._instructions.get(addr)
        if cached is not None:
            return cached
        out = []
        for start, size, lengths in self.functions[addr].spans:
            data = self.pe.bytes_at(start, size)
            if lengths:
                sizes = [int(c, 16) for c in lengths]
            else:
                sizes = self.lifter.lengths(start, data)
            at = start
            for n in sizes:
                out.append((at, n))
                at += n
        out.sort()
        self._instructions[addr] = out
        return out

    def owners(self):
        """{instruction address: function}. Covers every function; decodes them all."""
        if self._owner is None:
            self._owner = {at: addr for addr in self.functions
                           for at, _ in self.instructions(addr)}
        return self._owner

    def containing(self, addr):
        """The function whose span holds `addr`, or None."""
        k = bisect_right(self._spans, (addr, 1 << 32, 0)) - 1
        if k >= 0 and addr < self._spans[k][1]:
            return self._spans[k][2]
        return None

    def interior_entries(self):
        """{function: sorted entries inside it}: every entry that is not a function start."""
        found = {}
        for addr in self.entries():
            if addr in self.functions:
                continue
            owner = self.containing(addr)
            if owner is None:
                raise TranslateError("%08x: entry outside every mapped function; fix Ghidra and re-export" % addr)
            found.setdefault(owner, []).append(addr)
        return {owner: sorted(addrs) for owner, addrs in found.items()}

    def entries(self):
        """Every address generated code can be entered at, with why.

        Function starts, jump-table targets, interior entries Ghidra found
        referenced from elsewhere, and the game's configured entries."""
        if self._entries is not None:
            return self._entries
        found = {addr: "function" for addr in self.functions}
        for targets in self.tables.values():
            for target in targets:
                found.setdefault(target, "table")
        for addr, (_owner, kind) in self.interior.items():
            found.setdefault(addr, kind)
        for addr in self.configured_entries:
            found.setdefault(addr, "config")
        self._entries = found
        return found

    def body(self, addr):
        """Lift one function from the image bytes."""
        insns = [self.lifter.lift(at, self.pe.bytes_at(at, n)) for at, n in self.instructions(addr)]
        index = {ins.addr: k for k, ins in enumerate(insns)}
        noreturn = self.noreturn
        succ, exits, tables, dynamic = [], [], [], []
        for k, ins in enumerate(insns):
            out, falls = [], True
            leave = []
            if not ins.internal_flow:
                for op in ins.ops:
                    if op.opc in ("BRANCH", "CBRANCH") and op.ins[0][0] == "ram":
                        target = op.ins[0][1]
                        if target in index:
                            out.append(index[target])
                        else:
                            leave.append(target)
                        if op.opc == "BRANCH":
                            falls = False
                    elif op.opc == "RETURN":
                        falls = False
                    elif op.opc == "BRANCHIND":
                        falls = False
                        listed = self.tables.get((addr, ins.addr))
                        if listed is None:
                            dynamic.append(k)
                            continue
                        out.extend(index[t] for t in listed if t in index)
                        leave.extend(t for t in listed if t not in index)
                        if not leave:
                            tables.append(k)
                    elif op.opc in ("CALL", "CALLIND") and (
                            ins.addr in self.noreturn_calls or op.ins[0][0] == "ram"
                            and op.ins[0][1] in noreturn):
                        falls = False
            if falls:
                if k + 1 < len(insns) and insns[k + 1].addr == ins.addr + ins.length:
                    out.append(k + 1)
                else:
                    leave.append(ins.addr + ins.length)
            succ.append(sorted(set(out)))
            exits.extend((k, t) for t in leave)
        return Body(FunctionIR(addr, insns, succ, tables), exits, dynamic)
