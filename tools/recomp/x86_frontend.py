"""x86 operand identities and structural function flow, independent of C emission."""
import re

from ir.lift import LiftError
from program import TranslateError


REG32 = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"]
REG16 = ["AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI"]
REG8L = ["AL", "CL", "DL", "BL"]
REG8H = ["AH", "CH", "DH", "BH"]

R_EAX, R_ECX, R_EDX, R_EBX, R_ESP, R_EBP, R_ESI, R_EDI = range(8)

JCC = frozenset(('JA', 'JAE', 'JB', 'JBE', 'JC', 'JE', 'JECXZ', 'JCXZ', 'JG', 'JGE', 'JL', 'JLE', 'JNA', 'JNAE', 'JNB', 'JNBE', 'JNC', 'JNE', 'JNG', 'JNGE', 'JNL', 'JNLE', 'JNO', 'JNP', 'JNS', 'JNZ', 'JO', 'JP', 'JPE', 'JPO', 'JS', 'JZ', 'LOOP'))

STRING_MNEM = frozenset(("STOSB", "STOSW", "STOSD", "MOVSB", "MOVSW", "MOVSD",
                         "LODSB", "LODSW", "LODSD", "SCASB", "SCASW", "SCASD",
                         "CMPSB", "CMPSW", "CMPSD",
                         # The port forms. A user-mode guest never reaches
                         # one; they turn up where a listing misdecodes
                         # data as code, and refusing them would fail a
                         # whole build over a byte nothing executes.
                         "INSB", "INSW", "INSD", "OUTSB", "OUTSW", "OUTSD"))

class Op(object):
    __slots__ = ("kind", "size", "reg", "part", "imm",
                 "base", "index", "scale", "disp", "seg", "sti")

    def __init__(self, kind, **kw):
        self.kind = kind
        self.size = kw.get("size")
        self.reg = kw.get("reg")
        self.part = kw.get("part")
        self.imm = kw.get("imm")
        self.base = kw.get("base")
        self.index = kw.get("index")
        self.scale = kw.get("scale", 1)
        self.disp = kw.get("disp", 0)
        self.seg = kw.get("seg")
        self.sti = kw.get("sti")

    def __repr__(self):
        return "Op(%s,%s)" % (self.kind, self.size)


PTR_SIZE = {"byte": 8, "word": 16, "dword": 32, "qword": 64, "tword": 80,
            "float": 32, "double": 64, "extended double": 80, "xmmword": 128}

MEM_RE = re.compile(
    r"^(?:(byte|word|dword|qword|tword|xmmword|float|double|extended double) ptr )?"
    r"(?:([CDEFGS]S):)?"
    r"\[([^\]]*)\]$")

IMM_RE = re.compile(r"^-?0x[0-9a-fA-F]+$|^-?[0-9]+$")
#: Segment selectors as a 32-bit Windows process sees them: the flat code
#: and data selectors, FS for the TEB.  Read-only here; the game never
#: reloads a segment register.
SEGMENT_SELECTOR = {"CS": 0x1b, "DS": 0x23, "ES": 0x23, "SS": 0x23, "FS": 0x3b, "GS": 0x00}
ST_RE = re.compile(r"^ST([0-7])$")
MM_RE = re.compile(r"^MM([0-7])$")

#: A vector instruction this translator does not model. CPUID advertises
#: neither SSE nor SSE2, so a guest that checks (the CRT's own probe) never
#: runs one; each becomes a recomp_unmodelled trap rather than a translation
#: failure, which would lose the whole function. Anything naming an MMn,
#: XMMn or YMMn register counts, plus the extension instructions that name
#: none. AVX is included for the same reason: CPUID advertises it no more than
#: SSE, and a runtime's AVX path (Delphi's Move has one) is never taken.
VECTOR_REG_RE = re.compile(r"\b(?:[XY]?MM[0-7]|[xy]mmword)\b")
#: AVX names registers and an operand size this translator does not parse,
#: so it has to be recognised before the operands are.
AVX_OPERAND_RE = re.compile(r"\b(?:YMM[0-7]|ymmword)\b")
VECTOR_MNEM = frozenset((
    "STMXCSR", "LDMXCSR", "FXSAVE", "FXRSTOR", "SFENCE", "LFENCE", "MFENCE",
    "PREFETCHNTA", "PREFETCHT0", "PREFETCHT1", "PREFETCHT2", "MOVNTI", "CLFLUSH",
))


def is_vector_insn(mnem, ops):
    """True for an MMX/SSE/SSE2 instruction (see VECTOR_REG_RE).

    EMMS and FEMMS are not vector traps here: they name no register and the
    x87 model handles their media-state effects directly.
    """
    if mnem in ("EMMS", "FEMMS"):
        return False
    return mnem in VECTOR_MNEM or any(VECTOR_REG_RE.search(o) for o in ops)


XMM_RE = re.compile(r"^XMM([0-7])$")


def names_an_xmm(ins):
    """MOVSD and CMPSD name a string instruction and an SSE scalar one, and
    only the operands tell them apart: the string forms take ES:EDI and ESI,
    the SSE forms an XMM register."""
    return any(XMM_RE.match(o) for o in ins.ops)


def parse_reg(text):
    """Return (index, size, part) or None."""
    if text in REG32:
        return (REG32.index(text), 32, None)
    if text in REG16:
        return (REG16.index(text), 16, None)
    if text in REG8L:
        return (REG8L.index(text), 8, "l")
    if text in REG8H:
        return (REG8H.index(text), 8, "h")
    return None


def parse_imm(text):
    if text.startswith("-"):
        return -int(text[1:], 0)
    return int(text, 0)


def parse_mem_expr(expr):
    """`EAX*0x4 + 0x1234` -> (base, index, scale, disp)."""
    base = index = None
    scale = 1
    disp = 0
    for term in [t.strip() for t in expr.split("+")]:
        if not term:
            continue
        if "*" in term:
            rname, sc = term.split("*", 1)
            r = parse_reg(rname.strip())
            if r is None or r[1] != 32:
                raise TranslateError("bad index register %r" % term)
            index, scale = r[0], parse_imm(sc.strip())
        else:
            r = parse_reg(term)
            if r is not None:
                if r[1] != 32:
                    raise TranslateError("bad base register %r" % term)
                if base is None:
                    base = r[0]
                elif index is None:
                    index, scale = r[0], 1
                else:
                    raise TranslateError("too many registers in %r" % expr)
            elif IMM_RE.match(term):
                disp += parse_imm(term)
            else:
                raise TranslateError("bad memory term %r" % term)
    return base, index, scale, disp


def parse_operand(text):
    text = text.strip()
    r = parse_reg(text)
    if r is not None:
        return Op("reg", reg=r[0], size=r[1], part=r[2])
    m = XMM_RE.match(text)
    if m:
        return Op("xmm", reg=int(m.group(1)), size=128)
    m = ST_RE.match(text)
    if m:
        return Op("st", sti=int(m.group(1)))
    m = MM_RE.match(text)
    if m:
        return Op("mm", reg=int(m.group(1)), size=64)
    if text in SEGMENT_SELECTOR:
        return Op("sreg", size=16, imm=SEGMENT_SELECTOR[text])
    if IMM_RE.match(text):
        return Op("imm", imm=parse_imm(text))
    m = MEM_RE.match(text)
    if m:
        sz = PTR_SIZE[m.group(1)] if m.group(1) else None
        base, index, scale, disp = parse_mem_expr(m.group(3))
        return Op("mem", size=sz, base=base, index=index, scale=scale,
                  disp=disp, seg=m.group(2))
    raise TranslateError("unparsed operand %r" % text)

def operand_size(ops, hint=None):
    """Infer the operation width from a register operand or an explicit ptr."""
    for o in ops:
        if o.kind == "reg":
            return o.size
    for o in ops:
        if o.kind == "mem" and o.size:
            return o.size
    if hint:
        return hint
    raise TranslateError("ambiguous operand size")

class Function(object):
    def __init__(self, addr, name, size, insns):
        self.addr = addr
        self.name = name
        self.size = size
        self.insns = insns
        self.addrs = {i.addr for i in insns}
        self.end = max(addr + size, insns[-1].addr + 1) if insns else addr
        # filled in by measure(): contiguous[i] is True when insn i+1 in the
        # listing really is insn i's fall-through successor.
        self.contiguous = [True] * len(insns)
        self.fallthrough = [None] * len(insns)
        self.index = None
        self.pushed_continuations = set()
        self.seh_sites = set()
        self.seh_restores = set()
        self.return_jumps = set()
        self.seh_escapes = set()
        self.dead_addrs = set()

    def measure(self, image):
        """Find listing gaps: places where the next listed instruction is not
        where the current one actually ends."""
        for i, ins in enumerate(self.insns):
            e = image.insn_end(ins.addr, ins.mnem)
            self.fallthrough[i] = e
            nxt = self.insns[i + 1].addr if i + 1 < len(self.insns) else self.end
            if e is not None and e != nxt:
                self.contiguous[i] = False


TERMINATORS = frozenset(("RET", "JMP"))


def seh_chain_operand(op, zero_base=0):
    """Only the Delphi chain-head spellings, not other fields in the TEB."""
    return (op.kind == "mem" and op.size == 32 and op.seg == "FS"
            and op.base in (None, zero_base) and op.index is None and op.disp == 0)


def seh_zero_base(fn, i, reg):
    """Prove a zero FS base through a short straight-line register-preserving span."""
    if reg is None:
        return True
    if reg == R_ESP:
        return False
    targets = {ControlFlow.branch_target(ins) for ins in fn.insns
               if ins.mnem in JCC or ins.mnem == "JMP"}
    for j in range(i - 1, max(-1, i - 16), -1):
        if not fn.contiguous[j] or fn.insns[j + 1].addr in targets:
            return False
        ins = fn.insns[j]
        if ins.mnem == "XOR" and len(ins.ops) == 2:
            a, b = [parse_operand(o) for o in ins.ops]
            if a.kind == b.kind == "reg" and a.reg == b.reg == reg and a.size == b.size == 32:
                return True
        if (ins.mnem not in ("MOV", "LEA", "POP", "PUSH", "ADD", "SUB", "AND", "OR",
                              "XOR", "TEST", "CMP", "NOP")
                or ControlFlow.writes_reg32(ins, reg)):
            return False
    return False


def seh_restore_sites(fn):
    """Recognize unlink-only helpers as well as restores in establishing bodies."""
    result = set()
    for i, ins in enumerate(fn.insns):
        if ins.mnem not in ("POP", "MOV") or not ins.ops:
            continue
        dst = parse_operand(ins.ops[0])
        if not seh_chain_operand(dst, dst.base) or not seh_zero_base(fn, i, dst.base):
            continue
        if ins.mnem == "POP":
            result.add(i)
        elif len(ins.ops) == 2:
            src = parse_operand(ins.ops[1])
            if src.kind == "reg" and src.size == 32 and src.reg != R_ESP:
                result.add(i)
    return result


def seh_frame_sites(fn, image=None):
    """Map establishing MOV/helper CALL indices to their handler addresses."""
    sites = {}
    prolog = getattr(image, "msvc_seh_prolog", None)
    handler = prolog(fn.addr) if prolog is not None else None
    if handler is not None:
        publish = fn.addr + len(MSVC_SEH_PROLOG) - 7
        sites.update({i: handler for i, ins in enumerate(fn.insns) if ins.addr == publish})
    helper = getattr(image, "seh_constructor_helper", None)
    if helper is not None:
        stub = helper(fn.addr)
        if stub is not None:
            # The verified allocator helper publishes caller-reserved words
            # through ECX. Keep its checkpoint live until its return, then
            # let the caller adopt it; never leave setjmp in a dead helper.
            for i, ins in enumerate(fn.insns):
                if ins.mnem == "MOV" and len(ins.ops) == 2:
                    dst, src = [parse_operand(op) for op in ins.ops]
                    if (seh_chain_operand(dst, 2) and dst.base == 2
                            and src.kind == "reg" and src.reg == 1):
                        sites[i] = stub
        for i, ins in enumerate(fn.insns):
            if not i or ins.mnem != "CALL" or not fn.contiguous[i - 1]:
                continue
            target = ControlFlow.branch_target(ins)
            reserve = fn.insns[i - 1]
            if target is None or reserve.mnem not in ("ADD", "SUB") or len(reserve.ops) != 2:
                continue
            dst, amount = [parse_operand(op) for op in reserve.ops]
            if (dst.kind != "reg" or dst.reg != 4 or dst.size != 32 or amount.kind != "imm"
                    or amount.imm & 0xffffffff != (0xfffffff0 if reserve.mnem == "ADD" else 16)):
                continue
            stub = helper(target)
            if stub is not None:
                sites[i] = stub
    for i in range(2, len(fn.insns)):
        mov, push = fn.insns[i], fn.insns[i - 1]
        if mov.mnem != "MOV" or push.mnem != "PUSH" or len(mov.ops) != 2:
            continue
        try:
            dst, src = [parse_operand(o) for o in mov.ops]
            # The same frame idiom can zero another register before its
            # three PUSHes. Prove that spelling locally; an arbitrary FS
            # register operand can address a different TEB field.
            zero_base = dst.base if seh_zero_base(fn, i, dst.base) else 0
            if (not seh_chain_operand(dst, zero_base) or src.kind != "reg" or src.reg != 4
                    or src.size != 32 or not fn.contiguous[i - 1]):
                continue
            pushed = parse_operand(push.ops[0])
            handler_index = i - 2
            if not seh_chain_operand(pushed, zero_base):
                # MSVC also loads the old chain head before pushing it. Prove
                # the adjacent load/push pair; an arbitrary pushed register
                # is not evidence that this publishes a registration record.
                load = fn.insns[i - 2]
                if (pushed.kind != "reg" or pushed.size != 32 or
                        load.mnem != "MOV" or len(load.ops) != 2 or
                        not fn.contiguous[i - 2]):
                    continue
                loaded, head = [parse_operand(o) for o in load.ops]
                if head.kind == "mem" and head.size is None:
                    head.size = loaded.size  # MOV's register fixes the load width.
                if (loaded.kind != "reg" or loaded.size != 32 or
                        loaded.reg != pushed.reg or loaded.reg == R_ESP or
                        not seh_chain_operand(head, zero_base)):
                    continue
                handler_index -= 1
            for j in range(handler_index, max(-1, handler_index - 2), -1):
                prev = fn.insns[j]
                if not fn.contiguous[j]:
                    break
                if prev.mnem == "PUSH" and prev.ops:
                    imm = parse_operand(prev.ops[0])
                    if imm.kind == "imm":
                        sites[i] = imm.imm
                    break
        except TranslateError:
            continue
    return sites

MSVC_SEH_PROLOG = (b"\x68" + b"\0" * 4 + bytes.fromhex(
    "64a100000000508b442410896c24108d6c24102be05356578b45f88965e8508b45fc"
    "c745fcffffffff8945f88d45f064a300000000c3"))
MSVC_SEH_PROLOG = tuple(None if 1 <= i <= 4 else b for i, b in enumerate(MSVC_SEH_PROLOG))
MSVC_SEH_EPILOG = tuple(bytes.fromhex("8b4df064890d00000000595f5e5bc951c3"))
# PUSH EBP; MOV EBP,ESP; PUSH -1; PUSH scope; PUSH handler; MOV EAX,FS:[0]; PUSH EAX; MOV FS:[0],ESP
MSVC_EH3_FRAME = tuple(None if 6 <= i < 10 or 11 <= i < 15 else b for i, b in enumerate(bytes.fromhex(
    "558bec6aff68000000006800000000" "64a100000000" "50" "648925" "00000000")))
MSVC_EH3_HANDLER = tuple(bytes.fromhex("558bec83ec0853565755fc8b5d0c8b4508f74004060000000f85"))


EXPLICIT_DESTINATION = frozenset(("MOV", "LEA", "ADD", "SUB", "AND", "OR", "XOR", "INC", "DEC",
                                  "NEG", "NOT", "SHL", "SHR", "SAR", "MOVZX", "MOVSX", "POP",
                                  "PUSH", "CMP", "TEST", "JMP", "NOP")) | JCC - {"LOOP"}


def register_constants(fn, successors):
    """Constant 32-bit registers before each instruction, joined over the body's flow.

    Only XOR r,r and MOV r,imm create constants. Instructions outside
    EXPLICIT_DESTINATION and x87 forget every register; a direct call forgets
    EAX, ECX and EDX and keeps the callee-saved registers the compiled caller
    relies on."""
    before = {fn.index[fn.addr]: {}}
    work = [fn.index[fn.addr]]
    while work:
        i = work.pop()
        ins, state = fn.insns[i], dict(before[i])
        mnem = ins.mnem
        if mnem == "CALL":
            for reg in (R_EAX, R_ECX, R_EDX):
                state.pop(reg, None)
        elif mnem in EXPLICIT_DESTINATION or mnem.startswith("F"):
            try:
                ops = [parse_operand(o) for o in ins.ops]
            except TranslateError:
                ops = None
            if ops is None:
                state = {}
            elif ops and ops[0].kind == "reg" and mnem not in ("PUSH", "CMP", "TEST"):
                reg = ops[0].reg
                state.pop(reg, None)
                if ops[0].size == 32 and len(ops) == 2:
                    if mnem == "XOR" and ops[1].kind == "reg" and ops[1].reg == reg and ops[1].size == 32:
                        state[reg] = 0
                    elif mnem == "MOV" and ops[1].kind == "imm":
                        state[reg] = ops[1].imm & 0xffffffff
        else:
            state = {}
        for j in successors(fn, i):
            joined = state if j not in before else {r: v for r, v in before[j].items() if state.get(r) == v}
            if before.get(j) != joined:
                before[j] = joined
                work.append(j)
    return before


def eh3_trylevels(fn, successors):
    """The constant trylevels an EBP-framed EH3 body stores to [EBP-4]; None if another write reaches it."""
    levels, constants = set(), None
    for i, ins in enumerate(fn.insns):
        if not ins.ops or ins.mnem in ("CMP", "TEST", "PUSH"):
            continue
        try:
            dst = parse_operand(ins.ops[0])
            src = parse_operand(ins.ops[1]) if len(ins.ops) == 2 else None
        except TranslateError:
            continue
        if (dst.kind != "mem" or dst.base != R_EBP or dst.index is not None or dst.seg is not None
                or dst.disp & 0xffffffff != 0xfffffffc):
            continue
        value = src.imm & 0xffffffff if src is not None and src.kind == "imm" else None
        if ins.mnem == "MOV" and src is not None and src.kind == "reg" and src.size == 32:
            constants = constants or register_constants(fn, successors)
            value = constants.get(i, {}).get(src.reg)
            if value is None:
                return None
            levels.add(value)
        elif ins.mnem == "MOV" and value is not None:
            levels.add(value)
        elif ins.mnem == "AND" and value == 0:
            levels.add(0)
        elif ins.mnem == "OR" and value == 0xffffffff:
            levels.add(value)
        else:
            return None
    return levels


class ControlFlow(object):
    STRING_MNEM = STRING_MNEM

    def __init__(self, image, func_addrs, allow_unmodelled=None):
        self.image = image
        self.func_addrs = func_addrs
        self.allow_unmodelled = allow_unmodelled
        self.unmodelled = []
        self.call_targets = set()
        self.jumptables = {}
        self.internal_entries = {}
        self.noreturn_callees = set()
        self.noreturn_sites = set()
        self.seh_helpers = set()

    def seh_escaping_returns(self, fn):
        """Return paths with an established chain record but no matching unlink.

        Begin at the normal entry, not at exception landing aliases. A helper
        can return by RET or a proven jump through its popped return register.
        Calls to already marked helpers propagate ownership to their caller.
        """
        if not fn.seh_sites and not any(ins.mnem == "CALL" and
                                       self.branch_target(ins) in self.seh_helpers
                                       for ins in fn.insns):
            return set()
        returns = self.popped_return_jumps(fn, ())
        work, seen, escapes = [(fn.index[fn.addr], False)], set(), set()
        while work:
            i, active = work.pop()
            if (i, active) in seen:
                continue
            seen.add((i, active))
            ins = fn.insns[i]
            if i in fn.seh_sites or (ins.mnem == "CALL" and
                                    self.branch_target(ins) in self.seh_helpers):
                active = True
            elif i in fn.seh_restores or self.calls_seh_epilog(ins):
                active = False
            if i in returns or (ins.mnem == "RET" and self.push_ret_target(fn, i) is None):
                if active:
                    escapes.add(i)
                if i in returns:
                    continue
            work.extend((j, active) for j in self.successors(fn, i))
        return escapes

    def calls_seh_epilog(self, ins):
        if ins.mnem != "CALL":
            return False
        target = self.branch_target(ins)
        return target is not None and self.image.msvc_seh_epilog(target)

    @staticmethod
    def seh_except_entry(ins):
        """MSVC EH3/EH4 __except blocks begin by restoring ESP from the frame."""
        if ins.mnem != "MOV" or len(ins.ops) != 2 or ins.ops[0] != "ESP":
            return False
        src = parse_operand(ins.ops[1])
        return (src.kind == "mem"
                and src.base == R_EBP and src.index is None and src.seg is None
                and src.disp & 0xffffffff == 0xffffffe8)

    def seh_effects(self, fn):
        """SEH runtime effects per instruction, as the decoded emitter attaches them."""
        for i in fn.seh_sites:
            if fn.insns[i].mnem != "MOV":
                raise LiftError("%08x: SEH frame site is not a MOV" % fn.insns[i].addr)
        effects = {}
        unlinks = bool(fn.seh_sites) or self.image.msvc_seh_epilog(fn.addr)
        for i, ins in enumerate(fn.insns):
            if self.seh_except_entry(ins):
                effects[i] = ("landing",)
            elif ins.mnem == "MOV" and unlinks and len(ins.ops) == 2:
                dst, src = [parse_operand(o) for o in ins.ops]
                if ((seh_chain_operand(dst) or i in fn.seh_sites)
                        and src.kind == "reg" and src.size == 32):
                    effects[i] = ("enter" if i in fn.seh_sites else "leave",)
            elif ins.mnem == "POP" and i in fn.seh_restores:
                effects[i] = ("leave",)
            elif ins.mnem == "CALL" and self.branch_target(ins) in self.seh_helpers:
                effects[i] = ("adopt",)
            elif ins.mnem == "RET" and i in fn.seh_escapes:
                effects[i] = ("orphan",)
        return effects


    def never_returns(self, ins):
        """Is `ins` a direct CALL to a callee the listings show never returning?"""
        if ins.mnem != "CALL":
            return False
        t = self.branch_target(ins)
        return ins.addr in self.noreturn_sites or t is not None and t in self.noreturn_callees


    # -- control flow ------------------------------------------------------

    @staticmethod
    def pushed_continuations(fn):
        """Instruction boundaries in this body that PUSH names as continuations."""
        targets = set()
        for ins in fn.insns:
            if ins.mnem != "PUSH" or not ins.ops:
                continue
            try:
                op = parse_operand(ins.ops[0])
            except TranslateError:
                continue  # not a PUSH imm32; it cannot name a continuation
            if op.kind == "imm" and operand_size([op], hint=32) == 32:
                target = op.imm & 0xffffffff
                if target in fn.addrs:
                    targets.add(target)
        return targets


    def push_ret_target(self, fn, i):
        """An adjacent PUSH imm32 / RET is a jump, including Delphi epilogues.

        Lower it on the PUSH path only: another entry at the RET still returns
        to its own caller (finally handlers share that instruction).
        """
        ins = fn.insns[i]
        if (ins.mnem == "PUSH" and i + 1 < len(fn.insns) and fn.contiguous[i]
                and fn.insns[i + 1].mnem == "RET" and not fn.insns[i + 1].ops):
            try:
                op = parse_operand(ins.ops[0])
            except TranslateError:
                return None  # not a PUSH imm32, so not this pattern
            if op.kind == "imm" and operand_size([op], hint=32) == 32:
                return op.imm & 0xffffffff
        return None

    def popped_return_jumps(self, fn, entries):
        """Prove JMPs through the caller's return slot without runtime lookup.

        Facts are (ESP delta, EBP delta, registers popped at delta zero).
        Joins retain only agreeing facts; alternate entries start independent
        call frames. CALL is stack-neutral here, including cleanup calls after
        a saved return has been popped. Explicit/implicit register writes kill
        that register's fact. Unknown instructions or indirect destinations
        discard facts rather than guessing that a computed target is a return.
        """
        if not any(ins.mnem == "POP" for ins in fn.insns):
            return set()
        candidates = {i for i, ins in enumerate(fn.insns)
                      if ins.mnem == "JMP" and ins.ops and ins.ops[0] in REG32}
        if not candidates:
            return set()
        unknown = (None, None, frozenset())
        states, pending = {}, []

        def merge(i, state):
            old = states.get(i)
            if old is not None:
                state = (old[0] if old[0] == state[0] else None,
                         old[1] if old[1] == state[1] else None, old[2] & state[2])
            if state != old:
                states[i] = state
                pending.append(i)

        def transfer(ins, state):
            delta, frame, saved = state
            m = ins.mnem
            try:
                ops = ([parse_operand(o) for o in ins.ops]
                       if m not in self.STRING_MNEM or names_an_xmm(ins) else [])
            except TranslateError:
                # An operand this translator cannot even spell is the strongest
                # unmodelled form there is, and the rule above already covers
                # it: the proof does not survive one. The instruction itself
                # becomes a trap wherever it is emitted, so the only thing
                # raising here would change is which pass reports it - and in
                # an entry stub whose listing runs off into padding, that is
                # the difference between a translated image and no image.
                return unknown
            writes = set()
            # Read-only instructions and calls do not explicitly redefine a
            # saved return register. All unmodelled forms invalidate the proof.
            readonly = {"CMP", "TEST", "PUSH", "CALL", "JMP", "RET", "NOP", "WAIT",
                        "PAUSE", "CLC", "STC", "CMC", "CLD", "STD", "SAHF", "CLI", "STI"}
            dest_write = {"MOV", "MOVZX", "MOVSX", "LEA", "POP", "ADD", "ADC", "SUB",
                          "SBB", "AND", "OR", "XOR", "INC", "DEC", "NEG", "NOT", "SHL",
                          "SHR", "SAR", "ROL", "ROR", "RCL", "RCR", "SHLD", "SHRD",
                          "BSWAP", "BSF", "BSR", "BTS", "BTR", "BTC", "IMUL"}
            if m in readonly or m in JCC or m.startswith("F"):
                if m in ("FNSTSW", "FSTSW"):
                    writes.add(R_EAX)
            elif m in dest_write or m.startswith("SET") or m.startswith("CMOV"):
                if ops and ops[0].kind == "reg":
                    writes.add(ops[0].reg)
            elif m in ("XCHG", "XADD", "CMPXCHG"):
                writes.update(o.reg for o in ops if o.kind == "reg")
                if m == "CMPXCHG":
                    writes.add(R_EAX)
            elif m in ("PUSHAD", "PUSHFD", "PUSHF", "POPFD", "POPF"):
                writes.add(R_ESP)
            elif m == "LEAVE":
                writes.update((R_ESP, R_EBP))
            elif m in ("MUL", "DIV", "IDIV", "RDTSC"):
                writes.update((R_EAX, R_EDX))
            elif m in ("CDQ", "CWD"):
                writes.add(R_EDX)
            elif m in ("CBW", "CWDE", "LAHF", "XLAT"):
                writes.add(R_EAX)
            else:
                return unknown
            if m == "IMUL" and len(ops) == 1:
                writes.update((R_EAX, R_EDX))
            if m.startswith("LOOP"):
                writes.add(R_ECX)
            result = set(saved) - writes
            if (m == "POP" and delta == 0 and ops[0].kind == "reg"
                    and ops[0].size == 32 and ops[0].reg != R_ESP):
                result.add(ops[0].reg)
            adjustment = None
            if m in ("PUSH", "POP"):
                adjustment = operand_size(ops, hint=32) // 8 * (-1 if m == "PUSH" else 1)
            elif m in ("PUSHAD", "PUSHFD", "PUSHF", "POPFD", "POPF"):
                adjustment = {"PUSHAD": -32, "PUSHFD": -4, "PUSHF": -2,
                              "POPFD": 4, "POPF": 2}[m]
            elif m == "RET":
                adjustment = 4 + (parse_imm(ins.ops[0]) if ins.ops else 0)
            elif (m in ("ADD", "SUB") and ops[0].kind == "reg"
                  and ops[0].reg == R_ESP and ops[0].size == 32 and ops[1].kind == "imm"):
                immediate = ops[1].imm & 0xffffffff
                if immediate >= 0x80000000:
                    immediate -= 0x100000000
                adjustment = immediate * (-1 if m == "SUB" else 1)
            if m == "LEAVE":
                new_delta = frame + 4 if frame is not None else None
            elif m == "POP" and ops[0].kind == "reg" and ops[0].reg == R_ESP:
                new_delta = None
            elif adjustment is not None:
                new_delta = delta + adjustment if delta is not None else None
            else:
                new_delta = None if R_ESP in writes else delta
            new_frame = None if R_EBP in writes else frame
            if (m == "MOV" and ins.ops == ["EBP", "ESP"]):
                new_frame = delta
            return new_delta, new_frame, frozenset(result)

        for addr in (fn.addr, *entries):
            merge(fn.index[addr], (0, None, frozenset()))
        unknown_indirect_seen = False
        while pending:
            i = pending.pop()
            ins, state = fn.insns[i], states[i]
            if i in candidates and parse_operand(ins.ops[0]).reg in state[2]:
                continue
            out = transfer(ins, state)
            if ins.mnem == "JMP" and self.branch_target(ins) is None:
                # A non-return computed jump can enter anywhere. Seed unknown
                # facts once, avoiding a quadratic all-to-all propagation.
                if not unknown_indirect_seen:
                    for j in range(len(fn.insns)):
                        merge(j, unknown)
                    unknown_indirect_seen = True
                continue
            for j in self.successors(fn, i):
                merge(j, out)
        return {i for i in candidates if i in states
                and parse_operand(fn.insns[i].ops[0]).reg in states[i][2]}

    def successors(self, fn, i):
        """Indices reachable from insn i, and whether flags escape the function."""
        ins = fn.insns[i]
        m = ins.mnem
        t = self.push_ret_target(fn, i)
        if t is not None and t not in fn.pushed_continuations:
            return [fn.index[t]] if t in fn.index else []
        nxt = i + 1 if (i + 1 < len(fn.insns) and fn.contiguous[i]) else None
        if m == "INT3":
            # MSVC pads between functions with INT3, and a listing cut at a
            # call that never returns runs that padding into the next
            # function. Nothing falls out of the padding: the byte after it
            # belongs to whatever comes next, and is not this block's
            # successor. The instruction itself still traps at run time.
            return []
        if m == "RET":
            return [fn.index[t] for t in sorted(fn.pushed_continuations)]
        if m in JCC:
            out = [nxt] if nxt is not None else []
            t = self.branch_target(ins)
            if t is not None and t in fn.index:
                out.append(fn.index[t])
            return out
        if m == "JMP":
            if self.import_tail_slot(ins) is not None:
                return []
            if ins.ops and ins.ops[0].startswith("0x"):
                t = int(ins.ops[0], 16)
                return [fn.index[t]] if t in fn.index else []
            tgts = self.jumptables.get((fn.addr, ins.addr)) or fn.addrs
            return [fn.index[t] for t in tgts if t in fn.index]
        return [nxt] if nxt is not None else []

    @staticmethod
    def popped_jump(fn, i):
        """POP reg; JMP reg: Delphi leaving a finally block for a continuation
        it pushed, or a return through a popped return address."""
        ins = fn.insns[i]
        return (ins.mnem == "JMP" and bool(ins.ops) and ins.ops[0] in REG32
                and i > 0 and fn.contiguous[i - 1] and fn.insns[i - 1].mnem == "POP"
                and fn.insns[i - 1].ops == ins.ops)

    def dead_after_noreturn(self, fn, entries):
        """Instructions after a call that never returns that nothing reaches.

        Ghidra does not know _Halt0 or ExitProcess never return and lists on
        through the bytes behind the call, and Delphi keeps string literals
        there: Siege of Avalon's single-instance check ends CALL _Halt0 and is
        followed by L"DigitalTomeSiegeOfAvalon", whose 16-bit addressing the
        emitter refuses. Only such a tail is dropped - a run behind a call
        that never returns, reached from no entry, pushed continuation or
        branch. A computed jump other than POP reg; JMP reg may land anywhere
        (RTL fill routines compute addresses into unrolled code), so a body
        holding one keeps everything. POP reg; JMP reg goes where a
        continuation was pushed, or out of the function.
        """
        if not any(self.never_returns(ins) for ins in fn.insns):
            return set()
        live = self.reached(fn, {fn.addr} | set(entries))
        dead, after_noreturn = set(), False
        for i, ins in enumerate(fn.insns):
            if i in live:
                after_noreturn = self.never_returns(ins)
            elif after_noreturn:
                dead.add(i)
        return dead

    def reached(self, fn, roots):
        """Indices control flow reaches from `roots` and the pushed
        continuations; every index if the body has a computed jump that is
        not POP reg; JMP reg. A call that never returns ends a path."""
        if any(ins.mnem == "JMP" and self.branch_target(ins) is None
               and not self.jumptables.get((fn.addr, ins.addr))
               and not self.popped_jump(fn, i) for i, ins in enumerate(fn.insns)):
            return set(range(len(fn.insns)))
        roots = set(roots) | set(fn.pushed_continuations)
        work = [fn.index[a] for a in roots if a in fn.index]
        live = set()
        while work:
            i = work.pop()
            if i in live:
                continue
            live.add(i)
            if self.never_returns(fn.insns[i]):
                continue
            if self.popped_jump(fn, i):
                work.extend(fn.index[t] for t in fn.pushed_continuations)
                continue
            if fn.insns[i].mnem not in TERMINATORS and not fn.contiguous[i]:
                # A listing gap: emission jumps to the fall-through address.
                t = fn.fallthrough[i]
                if t in fn.index:
                    work.append(fn.index[t])
                continue
            work.extend(j for j in self.successors(fn, i) if j is not None)
        return live

    @staticmethod
    def branch_target(ins):
        if ins.ops and ins.ops[0].startswith("0x"):
            return int(ins.ops[0], 16)
        return None

    def import_tail_slot(self, ins):
        raw = ins.decode.source_bytes if ins.decode is not None else b''
        if ins.mnem == 'JMP' and len(raw) == 6 and raw[:2] == b'\xff\x25':
            slot = int.from_bytes(raw[2:], 'little')
            if slot in self.image.iat_names:
                return slot
        return None


    # -- jump tables -------------------------------------------------------


    #: Guards that branch TO a jump when the index is in range: `JC` after
    #: `CMP idx,N` leaves 0..N-1 at the target, after `SUB idx,N` -N..-1.
    RANGE_GUARDS = frozenset(("JC", "JB", "JNAE"))


    #: How many values a byte can take at the jump, given the guards in front
    #: of it.  `OR CL,CL; JS` sends the negative half elsewhere.
    BYTE_ROWS = 256
    BYTE_ROWS_SIGNED = 128


    #: How many index values reach the table, given `CMP idx,N` and the guard
    #: that follows it.  Only a guard that branches AWAY on out-of-range values
    #: bounds the fall-through path the JMP sits on: JA leaves 0..N (N+1
    #: values) and JAE/JNC leave 0..N-1 (N).  JBE/JB/JC/JNA branch away on
    #: in-range values, so reaching the JMP by falling through them means the
    #: index is out of range and the compare bounds nothing.
    GUARD_BOUND = {"JA": 1, "JNBE": 1, "JAE": 0, "JNC": 0, "JNB": 0}


    #: x87 instructions that write EFLAGS (the rest only touch x87 state).
    EFLAGS_X87 = frozenset(("FCOMI", "FCOMIP", "FUCOMI", "FUCOMIP"))


    @staticmethod
    def writes_reg32(ins, reg):
        if not ins.ops:
            return False
        if ins.mnem == "LOOP":
            return reg == 1                # ECX
        if ins.mnem in ("CMP", "TEST", "PUSH", "JMP") or ins.mnem in JCC:
            return False
        try:
            d = parse_operand(ins.ops[0])
        except TranslateError:
            return True
        return d.kind == "reg" and d.reg == reg

    def prepare(self, fn):
        """Index the function and its SEH frame sites; jump tables come from the code map."""
        fn.index = {ins.addr: k for k, ins in enumerate(fn.insns)}
        fn.pushed_continuations = self.pushed_continuations(fn)
        fn.seh_sites = seh_frame_sites(fn, self.image)
        fn.seh_restores = seh_restore_sites(fn)

    def analyze(self, fn, entries):
        fn.return_jumps = self.popped_return_jumps(fn, entries)
        fn.seh_escapes = self.seh_escaping_returns(fn)
        fn.dead_addrs = {fn.insns[i].addr for i in self.dead_after_noreturn(fn, entries)}
