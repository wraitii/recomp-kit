"""Ghidra-native program model: what is code, where it enters, where it flows.

The code map names function spans, jump tables, interior entries and
non-returning functions; the PE supplies bytes and imports; SLEIGH supplies
instruction effects. Nothing here decodes with Capstone or scans for code.
Unknown control flow fails with a named diagnostic: fix Ghidra and re-export.
"""
from pathlib import Path

import code_map
from ir.cfg import FunctionIR
from ir.lift import Lifter


class ProgramError(Exception):
    pass


class PE(object):
    """Mapped image bytes, executable ranges and import slots."""

    def __init__(self, path):
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
        self.base = pe.OPTIONAL_HEADER.ImageBase
        self.size = pe.OPTIONAL_HEADER.SizeOfImage
        data = bytes(pe.get_memory_mapped_image())
        self.data = (data + bytes(max(0, self.size - len(data))))[:self.size]
        self.end = self.base + self.size
        self.exec_ranges = [
            (self.base + s.VirtualAddress,
             self.base + s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData))
            for s in pe.sections if s.Characteristics & 0x20000000]
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        self.iat_names = {imp.address: imp.name.decode("ascii", "replace")
                          for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
                          for imp in entry.imports if imp.name}
        pe.close()

    def is_exec(self, va):
        return any(lo <= va < hi for lo, hi in self.exec_ranges)

    def bytes_at(self, va, length):
        lo = va - self.base
        if lo < 0 or lo + length > self.size:
            raise ProgramError("%08x: outside the image" % va)
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
    def __init__(self, cfg):
        translate = cfg.get("translate", {})
        root = Path(cfg["code_map_path"])
        self.pe = PE(cfg["developer_exe_path"])
        self.metadata, spans = code_map.read_map(root)
        self.tables, self.interior, noreturn, self.noreturn_calls = code_map.read_program(root)
        self.functions = {addr: Function(addr, rows, addr in noreturn)
                          for addr, rows in spans.items()}
        self.configured_entries = (frozenset(int(a) for a in translate.get("alternate_entries", ()))
                                   | frozenset(int(a) for a in translate.get("entry_points", ())))
        self.noreturn = frozenset(noreturn)
        self.lifter = Lifter()
        self._instructions = {}
        self._owner = None

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

    def entries(self):
        """Every address generated code can be entered at, with why.

        Function starts, jump-table targets, interior entries Ghidra found
        referenced from elsewhere, and the game's configured entries."""
        found = {addr: "function" for addr in self.functions}
        for targets in self.tables.values():
            for target in targets:
                found.setdefault(target, "table")
        for addr, (_owner, kind) in self.interior.items():
            found.setdefault(addr, kind)
        for addr in self.configured_entries:
            found.setdefault(addr, "config")
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
