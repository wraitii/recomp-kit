#!/usr/bin/env python3
"""Run translated game functions and the original bytes under Unicorn on the same random inputs.

    run.py --game-dir /abs/game --func 0x401000 [--func ...]
    run.py --game-dir /abs/game --changed      # functions whose generated C changed since the last run
    run.py --game-dir /abs/game --sample 2000  # random functions
    run.py --game-dir /abs/game --all

Uses the archive from the last `tools/build.py` build. Inputs whose original
execution faults, calls an import or runs too long are discarded; a function is
reported when the translation returns different registers, flags, x87 state or
memory, or escapes where the original returned.
"""

import argparse
import ctypes as C
import hashlib
import importlib.util
import json
import os
import platform
import random
import re
import select
import signal
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pefile
from unicorn import (Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_HOOK_INTR, UC_HOOK_MEM_INVALID,
                     UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, UC_PROT_ALL)
from unicorn.x86_const import (
    UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_EBX, UC_X86_REG_ESP,
    UC_X86_REG_EBP, UC_X86_REG_ESI, UC_X86_REG_EDI, UC_X86_REG_EFLAGS, UC_X86_REG_EIP,
    UC_X86_REG_FS, UC_X86_REG_SS, UC_X86_REG_DS, UC_X86_REG_ES, UC_X86_REG_GDTR)

KIT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(KIT / "tools"))
import game_config  # noqa: E402

spec = importlib.util.spec_from_file_location("build_py", KIT / "tools/build.py")
build_py = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_py)

SCRATCH, SCRATCH_SIZE = 0x0E000000, 0x00100000
STACK, STACK_SIZE = 0x0EF00000, 0x00100000
ESP_INIT = STACK + STACK_SIZE - 0x1000
ARG_BYTES = 0x100
CAVE = 0x0DEAC000
MAGIC_RET = CAVE
FNSAVE_CODE = CAVE + 0x20
FNSAVE_SLOT = CAVE + 0x200
FPU_RESET = CAVE + 0x40
FPU_CW_SLOT = CAVE + 0x300
IMPORT_TRAP = 0x0DF00000
TEB = 0x0DEB0000
GDT = 0x0DEB1000

REGS = [UC_X86_REG_EAX, UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_EBX,
        UC_X86_REG_ESP, UC_X86_REG_EBP, UC_X86_REG_ESI, UC_X86_REG_EDI]
REG_NAMES = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"]
FLAGS = {"CF": 0x001, "PF": 0x004, "ZF": 0x040, "SF": 0x080, "DF": 0x400, "OF": 0x800}
FLAG_MASK = sum(FLAGS.values())
FPU_CWS = (0x037F, 0x027F, 0x007F)


class State(C.Structure):
    _fields_ = [("r", C.c_uint32 * 8), ("eflags", C.c_uint32),
                ("fpu_cw", C.c_uint16), ("fpu_sw", C.c_uint16), ("fpu_tag", C.c_uint16),
                ("fpu_top", C.c_uint32), ("st0", C.c_double), ("escape_addr", C.c_uint32)]


def build_harness(cfg, build_root, out):
    archive = build_py.archive_path(build_root)
    gen = build_root / "recomp/gen"
    if not archive.exists():
        sys.exit("%s is missing: build the game first" % archive)
    sources = [KIT / "tools/recomp/tests/harness.c", Path(__file__).with_name("host.c"),
               KIT / "platform/os_posix.cpp"]
    inputs = sources + [archive, KIT / "runtime/x86.h", gen / "table.c"]
    if out.exists() and out.stat().st_mtime > max(p.stat().st_mtime for p in inputs):
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    darwin = platform.system() == "Darwin"
    cc = ["xcrun", "clang"] if darwin else ["clang"]
    os_obj = out.with_name("os_posix.o")
    subprocess.check_call(cc + ["-x", "c++", "-std=c++17", "-O1", "-fno-exceptions", "-fPIC",
                                "-I", str(KIT), "-c", str(sources[2]), "-o", str(os_obj)])
    whole = (["-dynamiclib", "-Wl,-force_load," + str(archive)] if darwin else
             ["-shared", "-Wl,--whole-archive", str(archive), "-Wl,--no-whole-archive", "-lm"])
    subprocess.check_call(cc + ["-O1", "-ffp-contract=off", "-std=c11", "-w", "-fPIC",
                                "-I", str(gen), "-I", str(KIT), "-I", str(KIT / "runtime"),
                                str(sources[0]), str(sources[1]), str(os_obj)] + whole +
                          ["-o", str(out)])


def function_bodies(gen):
    header = re.compile(r"^/\* \S+\s+\d+ insns\s+([0-9a-f]{8})\.\.[0-9a-f]{8} \*/$", re.M)
    bodies = {}
    for chunk in sorted(gen.glob("chunk_*.c")):
        text = chunk.read_text()
        marks = list(header.finditer(text))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            bodies[int(m.group(1), 16)] = hashlib.sha1(text[m.start():end].encode()).hexdigest()
    return bodies


class Image:
    def __init__(self, exe):
        pe = pefile.PE(str(exe))
        self.base = pe.OPTIONAL_HEADER.ImageBase
        self.size = (pe.OPTIONAL_HEADER.SizeOfImage + 0xFFF) & ~0xFFF
        image = bytearray(pe.get_memory_mapped_image()[:self.size])
        image += bytes(self.size - len(image))
        slot = 0
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            for imp in entry.imports:
                struct.pack_into("<I", image, imp.address - self.base, IMPORT_TRAP + 16 * slot)
                slot += 1
        self.bytes = bytes(image)
        self.sections = []
        self.writable = []
        self.bss = []
        for s in pe.sections:
            start = self.base + s.VirtualAddress
            size = (max(s.Misc_VirtualSize, s.SizeOfRawData) + 0xFFF) & ~0xFFF
            perms = UC_PROT_READ
            if s.Characteristics & 0x20000000:
                perms |= UC_PROT_EXEC
            if s.Characteristics & 0x80000000:
                perms |= UC_PROT_WRITE
                self.writable.append((start, size))
                if s.Misc_VirtualSize > s.SizeOfRawData:
                    self.bss.append((start + (s.SizeOfRawData + 3 & ~3),
                                     s.Misc_VirtualSize - s.SizeOfRawData & ~3))
            self.sections.append((start, size, perms))


class Native:
    def __init__(self, dylib, image):
        self.lib = C.CDLL(str(dylib))
        self.lib.harness_mem.restype = C.c_void_p
        self.lib.diff_run.restype = C.c_char_p
        self.lib.diff_run.argtypes = [C.c_uint32, C.POINTER(State), C.c_uint32]
        self.base = self.lib.harness_mem()
        self.write(image.base, image.bytes)

    def write(self, addr, data):
        C.memmove(self.base + addr, data, len(data))

    def read(self, addr, n):
        return C.string_at(self.base + addr, n)


class Emu:
    def __init__(self, image):
        self.u = Uc(UC_ARCH_X86, UC_MODE_32)
        self.u.mem_map(image.base, 0x1000, UC_PROT_READ)
        self.u.mem_write(image.base, image.bytes[:0x1000])
        for start, size, perms in image.sections:
            self.u.mem_map(start, size, perms)
            self.u.mem_write(start, image.bytes[start - image.base:start - image.base + size])
        self.u.mem_map(SCRATCH, SCRATCH_SIZE, UC_PROT_READ | UC_PROT_WRITE)
        self.u.mem_map(STACK, STACK_SIZE, UC_PROT_READ | UC_PROT_WRITE)
        self.u.mem_map(CAVE, 0x1000, UC_PROT_ALL)
        self.u.mem_write(FNSAVE_CODE, b"\xdd\x35" + struct.pack("<I", FNSAVE_SLOT))
        self.u.mem_write(FPU_RESET, b"\xdb\xe3\xd9\x2d" + struct.pack("<I", FPU_CW_SLOT))
        self.u.mem_map(TEB, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
        self.u.mem_map(GDT, 0x1000, UC_PROT_READ)
        self.u.mem_write(GDT + 8, segment(0, 0xFFFFF, flags=0xC) + segment(TEB, 0xFFF))
        self.u.reg_write(UC_X86_REG_GDTR, (0, GDT, 0x17, 0))
        for reg in (UC_X86_REG_SS, UC_X86_REG_DS, UC_X86_REG_ES):
            self.u.reg_write(reg, 1 << 3)
        self.u.reg_write(UC_X86_REG_FS, 2 << 3)
        self.u.hook_add(UC_HOOK_INTR, lambda uc, n, _: uc.emu_stop())
        self.fault = None
        self.u.hook_add(UC_HOOK_MEM_INVALID, self.on_fault)
        self.image = image

    def on_fault(self, uc, access, addr, size, value, _):
        self.fault = addr
        return False

    def fault_kind(self):
        a = self.fault
        if a is None:
            return "exception"
        if a < 0x10000:
            return "null page"
        if a >= IMPORT_TRAP and a < IMPORT_TRAP + 0x100000:
            return "import"
        if self.image.base <= a < self.image.base + self.image.size:
            return "image"
        if TEB <= a < TEB + 0x1000 or SCRATCH <= a < SCRATCH + SCRATCH_SIZE or STACK <= a < STACK + STACK_SIZE:
            return "protection"
        return "wild"


def segment(base, limit, access=0x93, flags=0x4):
    return struct.pack("<Q", (limit & 0xFFFF) | (base & 0xFFFFFF) << 16 | access << 40 |
                       (limit >> 16 & 0xF) << 48 | flags << 52 | (base >> 24 & 0xFF) << 56)


def teb():
    blob = bytearray(0x1000)
    struct.pack_into("<III", blob, 0, 0xFFFFFFFF, STACK + STACK_SIZE, STACK)
    struct.pack_into("<I", blob, 0x18, TEB)
    return bytes(blob)


class Inputs:
    def __init__(self, seed, image, cws):
        self.cws = cws
        self.rng = random.Random(seed)
        self.np = np.random.default_rng(seed)
        self.image = image

    def pointer(self):
        return SCRATCH + (self.rng.randrange(SCRATCH_SIZE * 3 // 4) & ~3)

    def word(self):
        k = self.rng.random()
        if k < 0.5:
            return self.pointer()
        if k < 0.75:
            return self.rng.randrange(256)
        if k < 0.9:
            return struct.unpack("<I", struct.pack("<f", self.rng.uniform(-1000, 1000)))[0]
        return self.rng.getrandbits(32)

    def words(self, size):
        n = size // 4
        kind = self.np.random(n)
        pointers = SCRATCH + self.np.integers(0, SCRATCH_SIZE * 3 // 16, n) * 4
        small = self.np.integers(0, 256, n)
        floats = self.np.uniform(-1000, 1000, n).astype(np.float32).view(np.uint32)
        noise = self.np.integers(0, 1 << 32, n, dtype=np.uint64)
        words = np.select([kind < 0.55, kind < 0.7, kind < 0.9], [pointers, small, floats], noise)
        return words.astype("<u4").tobytes()

    def make(self):
        regs = [self.word() for _ in range(8)]
        regs[4] = ESP_INIT
        stack = struct.pack("<I", MAGIC_RET) + b"".join(
            struct.pack("<I", self.word()) for _ in range(ARG_BYTES // 4 - 1))
        eflags = 0x202 | (self.rng.getrandbits(12) & (FLAG_MASK & ~FLAGS["DF"]))
        return (regs, eflags, self.rng.choice(self.cws), stack,
                [(SCRATCH, self.words(SCRATCH_SIZE)), (TEB, teb())] + [(a, self.words(n)) for a, n in self.image.bss])


def ext80(b):
    mant, se = struct.unpack("<QH", b)
    sign = -1.0 if se & 0x8000 else 1.0
    exp = se & 0x7FFF
    if exp == 0x7FFF:
        return float("nan") if mant << 1 & ((1 << 64) - 1) else sign * float("inf")
    if exp == 0 and mant == 0:
        return sign * 0.0
    try:
        return sign * float(mant) * 2.0 ** (exp - 16383 - 63)
    except OverflowError:
        return sign * float("inf")


def close(a, b, tol):
    if a == b or (a != a and b != b):
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b))


def memory_diff(want, got, base):
    hard, fp = [], []
    w = np.frombuffer(want, dtype="<u4")
    g = np.frombuffer(got, dtype="<u4")
    for i in np.nonzero(w != g)[0][:64]:
        a, b = int(w[i]), int(g[i])
        fa, fb = (struct.unpack("<f", struct.pack("<I", v))[0] for v in (a, b))
        entry = "%08x: %08x != %08x" % (base + 4 * int(i), a, b)
        (fp if close(fa, fb, 1e-6) or ((i & 1) and close_double(w, g, i)) else hard).append(entry)
    return hard, fp


def close_double(w, g, i):
    a = struct.unpack("<d", struct.pack("<II", int(w[i - 1]), int(w[i])))[0]
    b = struct.unpack("<d", struct.pack("<II", int(g[i - 1]), int(g[i])))[0]
    return close(a, b, 1e-12)


def regions(image):
    return list(image.writable) + [(SCRATCH, SCRATCH_SIZE), (ESP_INIT, ARG_BYTES), (TEB, 0x1000)]


def run_function(addr, native, emu, image, args):
    inputs = Inputs(args.seed ^ addr, image, args.fpu_cw)
    u = emu.u
    data = [(s, image.bytes[s - image.base:s - image.base + n]) for s, n in image.writable]
    skipped = Counter()
    compared = 0
    for it in range(args.iterations):
        regs, eflags, cw, stack, fills = inputs.make()
        for start, blob in data:
            u.mem_write(start, blob)
            native.write(start, blob)
        for target in (u.mem_write, native.write):
            for start, blob in fills:
                target(start, blob)
            target(ESP_INIT, stack)
        for r, v in zip(REGS, regs):
            u.reg_write(r, v)
        u.mem_write(FPU_CW_SLOT, struct.pack("<H", cw))
        u.emu_start(FPU_RESET, FPU_RESET + 8)
        u.reg_write(UC_X86_REG_EFLAGS, eflags)
        emu.fault = None
        try:
            u.emu_start(addr, MAGIC_RET, count=args.max_insns)
        except UcError:
            skipped[emu.fault_kind()] += 1
            continue
        if u.reg_read(UC_X86_REG_EIP) != MAGIC_RET:
            skipped["original did not return"] += 1
            continue
        want_regs = [u.reg_read(r) for r in REGS]
        want_flags = u.reg_read(UC_X86_REG_EFLAGS)
        want_mem = [bytes(u.mem_read(s, n)) for s, n in regions(image)]
        u.mem_write(FNSAVE_SLOT, bytes(108))
        u.emu_start(FNSAVE_CODE, FNSAVE_CODE + 6)
        fnsave = bytes(u.mem_read(FNSAVE_SLOT, 108))

        st = State()
        st.r[:] = regs
        st.eflags = eflags
        st.fpu_cw = cw
        escape = native.lib.diff_run(addr, C.byref(st), TEB)
        compared += 1
        problems = []
        if escape:
            problems.append("translation escaped (%s %08x) where the original returned"
                            % (escape.decode(), st.escape_addr))
        else:
            for name, a, b in zip(REG_NAMES, want_regs, st.r):
                if a != b:
                    problems.append("%s %08x != %08x" % (name, a, b))
            flags = [n for n, bit in FLAGS.items() if (want_flags ^ st.eflags) & bit]
            want_tag = struct.unpack_from("<H", fnsave, 8)[0]
            want_top = (struct.unpack_from("<H", fnsave, 4)[0] >> 11) & 7
            if want_top != st.fpu_top:
                problems.append("x87 TOP %d != %d" % (want_top, st.fpu_top))
            elif (want_tag >> (2 * want_top)) & 3 != 3:
                want_st0 = ext80(fnsave[28:38])
                if not close(want_st0, st.st0, 1e-9):
                    problems.append("ST0 %r != %r" % (want_st0, st.st0))
            fp = []
            for (start, n), want in zip(regions(image), want_mem):
                hard, soft = memory_diff(want, native.read(start, n), start)
                problems += hard
                fp += soft
            if flags and not problems:
                problems.append("flags " + " ".join(flags))
            if fp and not problems:
                problems.append("float only: " + "; ".join(fp[:4]))
        if problems:
            return {"addr": addr, "status": "diverged", "iteration": it, "compared": compared,
                    "input": {"regs": dict(zip(REG_NAMES, ("%08x" % v for v in regs))),
                              "eflags": "%08x" % eflags, "fpu_cw": "%04x" % cw,
                              "args": [("%08x" % v) for v in struct.unpack("<8I", stack[4:36])]},
                    "problems": problems[:24]}
    return {"addr": addr, "status": "ok" if compared else "unreached", "compared": compared,
            "skipped": dict(skipped)}


def run_all(addrs, args, dylib, image):
    native = Native(dylib, image)
    emu = Emu(image)
    pending = list(addrs)
    running = {}
    results = []
    started = time.time()
    while pending or running:
        while pending and len(running) < args.jobs:
            addr = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    result = run_function(addr, native, emu, image, args)
                except Exception as e:  # noqa: BLE001
                    result = {"addr": addr, "status": "error", "problems": [repr(e)]}
                os.write(w, json.dumps(result).encode())
                os._exit(0)
            os.close(w)
            running[r] = (pid, addr, time.time(), bytearray())
        ready, _, _ = select.select(list(running), [], [], 0.5)
        for fd in ready:
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[fd][3].extend(chunk)
                continue
            pid, addr, _, out = running.pop(fd)
            os.close(fd)
            _, status = os.waitpid(pid, 0)
            if out:
                results.append(json.loads(out))
            else:
                results.append({"addr": addr, "status": "crashed",
                                "problems": ["translation crashed (signal %d)" % os.WTERMSIG(status)
                                             if os.WIFSIGNALED(status) else "worker exited"]})
            report(results[-1], len(results), len(addrs))
        now = time.time()
        for fd, (pid, addr, begun, _) in list(running.items()):
            if now - begun > args.timeout:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(fd)
                del running[fd]
                results.append({"addr": addr, "status": "hung",
                                "problems": ["no result after %ds" % args.timeout]})
                report(results[-1], len(results), len(addrs))
    print("%d functions in %.0fs" % (len(results), time.time() - started))
    return results


def report(result, done, total):
    if result["status"] in ("ok", "unreached"):
        if sys.stdout.isatty():
            print("\r%d/%d" % (done, total), end="", flush=True)
        return
    print("\r%08x %s" % (result["addr"], result["status"]))
    if "input" in result:
        print("    input %s" % json.dumps(result["input"]))
    for p in result.get("problems", []):
        print("    " + p)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game-dir", type=Path, required=True)
    pick = parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--func", type=lambda v: int(v, 16), action="append")
    pick.add_argument("--changed", action="store_true")
    pick.add_argument("--sample", type=int)
    pick.add_argument("--all", action="store_true")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--max-insns", type=int, default=200000)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--fpu-cw", type=lambda v: int(v, 16), action="append",
                        help="initial x87 control words, default %s" % " ".join("%04x" % w for w in FPU_CWS))
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    args.fpu_cw = args.fpu_cw or list(FPU_CWS)

    game_dir = args.game_dir.resolve()
    cfg = game_config.load(game_dir)
    build_root = build_py.build_root_for(game_dir)
    out = build_root / "recomp/diff"
    gen = build_root / "recomp/gen"
    dylib = out / ("harness.dylib" if platform.system() == "Darwin" else "harness.so")
    build_harness(cfg, build_root, dylib)

    symbols = json.loads((gen / "symbols.json").read_text())
    entries = sorted(int(f["addr"], 16) for f in symbols["functions"] if f["kind"] == "entry")
    bodies = function_bodies(gen)
    snapshot = out / "bodies.json"
    if args.func:
        addrs = args.func
    elif args.changed:
        if not snapshot.exists():
            sys.exit("No previous run to compare with: run once before changing the translator")
        before = {int(k, 16): v for k, v in json.loads(snapshot.read_text()).items()}
        addrs = [a for a in entries if bodies.get(a) != before.get(a)]
        print("%d functions changed" % len(addrs))
    elif args.sample:
        addrs = sorted(random.Random(args.seed).sample(entries, min(args.sample, len(entries))))
    else:
        addrs = entries

    results = run_all(addrs, args, dylib, Image(cfg["developer_exe_path"]))
    snapshot.write_text(json.dumps({"%08x" % a: h for a, h in bodies.items()}))
    (out / "report.json").write_text(json.dumps(results, indent=1))
    counts = Counter(r["status"] for r in results)
    print(", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())))
    print("report: %s" % (out / "report.json"))
    sys.exit(1 if counts.keys() - {"ok", "unreached"} else 0)


if __name__ == "__main__":
    main()
