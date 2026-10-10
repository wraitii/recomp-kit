"""The decoded emitter: Capstone-decoded instructions lowered to plain C.

Used for the bodies the SSA emitter does not take and for the entry wrappers of
bodies whose extra entries are all switch cases. It also holds the structural
analyses SSA shares (SEH frame sites, popped return jumps, dead tails after
non-returning calls). Every instruction is emitted eagerly: no flag liveness and
no register or x87 locals.
"""

import re

from ir.cfg import FunctionIR
from ir.lift import LiftError
from program import PE, TranslateError

#: The import-shim trampoline range from x86.h; a literal dispatch into it is
#: an import, not a missing function.
GUEST_SHIM_BASE = 0x0FF00000
GUEST_SHIM_END = 0x10000000



# --------------------------------------------------------------- registers --

REG32 = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"]
REG16 = ["AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI"]
REG8L = ["AL", "CL", "DL", "BL"]
REG8H = ["AH", "CH", "DH", "BH"]

R_EAX, R_ECX, R_EDX, R_EBX, R_ESP, R_EBP, R_ESI, R_EDI = range(8)

#: MMX on the MMn registers, which the translator emits. The rest of the
#: vector set (SSE, SSE2, and the SSE extensions to MMX) stays a trap.
MMX_BINARY = {
    "PADDB": ("mmx_padd", 8), "PADDW": ("mmx_padd", 16), "PADDD": ("mmx_padd", 32),
    "PADDQ": ("mmx_padd", 64),
    "PSUBB": ("mmx_psub", 8), "PSUBW": ("mmx_psub", 16), "PSUBD": ("mmx_psub", 32),
    "PSUBQ": ("mmx_psub", 64),
    "PADDSB": ("mmx_padds", 8), "PADDSW": ("mmx_padds", 16),
    "PSUBSB": ("mmx_psubs", 8), "PSUBSW": ("mmx_psubs", 16),
    "PADDUSB": ("mmx_paddus", 8), "PADDUSW": ("mmx_paddus", 16),
    "PSUBUSB": ("mmx_psubus", 8), "PSUBUSW": ("mmx_psubus", 16),
    "PMULLW": ("mmx_pmullw", 0), "PMULHW": ("mmx_pmulhw", 0), "PMADDWD": ("mmx_pmaddwd", 0),
    "PCMPEQB": ("mmx_pcmpeq", 8), "PCMPEQW": ("mmx_pcmpeq", 16), "PCMPEQD": ("mmx_pcmpeq", 32),
    "PCMPGTB": ("mmx_pcmpgt", 8), "PCMPGTW": ("mmx_pcmpgt", 16), "PCMPGTD": ("mmx_pcmpgt", 32),
    "PACKSSWB": ("mmx_packsswb", 0), "PACKSSDW": ("mmx_packssdw", 0),
    "PACKUSWB": ("mmx_packuswb", 0),
    "PUNPCKLBW": ("mmx_punpckl", 8), "PUNPCKLWD": ("mmx_punpckl", 16),
    "PUNPCKLDQ": ("mmx_punpckl", 32),
    "PUNPCKHBW": ("mmx_punpckh", 8), "PUNPCKHWD": ("mmx_punpckh", 16),
    "PUNPCKHDQ": ("mmx_punpckh", 32),
    "PAND": ("mmx_pand", 0), "PANDN": ("mmx_pandn", 0), "POR": ("mmx_por", 0),
    "PXOR": ("mmx_pxor", 0),
}
MMX_SHIFT = {
    "PSRLW": ("mmx_psrl", 16), "PSRLD": ("mmx_psrl", 32), "PSRLQ": ("mmx_psrl", 64),
    "PSRAW": ("mmx_psra", 16), "PSRAD": ("mmx_psra", 32),
    "PSLLW": ("mmx_psll", 16), "PSLLD": ("mmx_psll", 32), "PSLLQ": ("mmx_psll", 64),
}
MMN_RE = re.compile(r"\bMM[0-7]\b")


def is_mmx_insn(mnem, ops):
    """An instruction the translator emits as MMX: an MMX mnemonic whose
    operands name an MMn register and no XMM one."""
    if mnem not in MMX_BINARY and mnem not in MMX_SHIFT and mnem not in ("MOVQ", "MOVD", "PSHUFW"):
        return False
    return (any(MMN_RE.search(o) for o in ops) and not any(XMM_RE.search(o) for o in ops))


def hexlit(v):
    return "0x%xu" % (v & 0xFFFFFFFF)


def mask_of(size):
    return {8: 0xFF, 16: 0xFFFF, 32: 0xFFFFFFFF}[size]


def utype(size):
    return {8: "uint8_t", 16: "uint16_t", 32: "uint32_t"}[size]


def stype(size):
    return {8: "int8_t", 16: "int16_t", 32: "int32_t"}[size]


# ---------------------------------------------------------------- operands --

class Op(object):
    __slots__ = ("kind", "size", "reg", "part", "imm",
                 "base", "index", "scale", "disp", "seg", "sti", "addr_c")

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
        self.addr_c = kw.get("addr_c")   # pre-computed address expression

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

    EMMS is not one of them here: it names no register and only marks every
    x87 register empty, which the x87 model can do. Codecs call a lone
    `emms; ret` helper unconditionally, whatever CPUID said, so trapping it
    stopped a game that never used MMX arithmetic at all.
    """
    if mnem == "EMMS":
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


# ------------------------------------------------------------ C generation --

def addr_expr(op):
    if op.addr_c is not None:
        return op.addr_c
    terms = []
    if op.seg == "FS":
        terms.append("c->fs_base")
    if op.base is not None:
        terms.append("c->r[%d]" % op.base)
    if op.index is not None:
        if op.scale == 1:
            terms.append("c->r[%d]" % op.index)
        else:
            terms.append("c->r[%d] * %du" % (op.index, op.scale))
    if op.disp or not terms:
        terms.append(hexlit(op.disp))
    if len(terms) == 1:
        return terms[0]
    return "(" + " + ".join(terms) + ")"


def reg_read(idx, size, part):
    if size == 32:
        return "c->r[%d]" % idx
    if size == 16:
        return "(uint16_t)c->r[%d]" % idx
    if part == "h":
        return "(uint8_t)(c->r[%d] >> 8)" % idx
    return "(uint8_t)c->r[%d]" % idx


def reg_write(idx, size, part, value):
    if size == 32:
        return "c->r[%d] = %s;" % (idx, value)
    if size == 16:
        return "c->r[%d] = (c->r[%d] & 0xffff0000u) | ((%s) & 0xffffu);" % (idx, idx, value)
    if part == "h":
        return ("c->r[%d] = (c->r[%d] & 0xffff00ffu) | (((%s) & 0xffu) << 8);"
                % (idx, idx, value))
    return "c->r[%d] = (c->r[%d] & 0xffffff00u) | ((%s) & 0xffu);" % (idx, idx, value)


def read_op(op, size):
    if op.kind == "reg":
        return reg_read(op.reg, op.size, op.part)
    if op.kind == "imm":
        return hexlit(op.imm) if size == 32 else "0x%xu" % (op.imm & mask_of(size))
    if op.kind == "mem":
        return "rd%d(%s)" % (size, addr_expr(op))
    if op.kind == "sreg":
        return "0x%xu" % op.imm
    raise TranslateError("cannot read operand %r" % op.kind)


def write_op(op, size, value):
    if op.kind == "reg":
        return reg_write(op.reg, op.size, op.part, value)
    if op.kind == "mem":
        return "wr%d(%s, (%s)(%s));" % (size, addr_expr(op), utype(size), value)
    raise TranslateError("cannot write operand %r" % op.kind)


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


# ----------------------------------------------------------------- parsing --


class Insn(object):
    __slots__ = ("addr", "mnem", "rep", "ops", "text", "raw")

    def __init__(self, addr, mnem, rep, opstrs, raw):
        self.addr = addr
        self.mnem = mnem
        self.rep = rep
        self.ops = opstrs
        self.raw = raw

    def __repr__(self):
        return "%08x %s %s" % (self.addr, self.mnem, ",".join(self.ops))


# ------------------------------------------------------------------- flags --

COND = {
    "Z":  ("c->eflags_zf", ("zf",)),
    "NZ": ("!c->eflags_zf", ("zf",)),
    "C":  ("c->eflags_cf", ("cf",)),
    "B":  ("c->eflags_cf", ("cf",)),
    "NC": ("!c->eflags_cf", ("cf",)),
    "AE": ("!c->eflags_cf", ("cf",)),
    "A":  ("(!c->eflags_cf && !c->eflags_zf)", ("cf", "zf")),
    "BE": ("(c->eflags_cf || c->eflags_zf)", ("cf", "zf")),
    "S":  ("c->eflags_sf", ("sf",)),
    "NS": ("!c->eflags_sf", ("sf",)),
    "O":  ("c->eflags_of", ("of",)),
    "NO": ("!c->eflags_of", ("of",)),
    "P":  ("c->eflags_pf", ("pf",)),
    "NP": ("!c->eflags_pf", ("pf",)),
    "G":  ("(!c->eflags_zf && c->eflags_sf == c->eflags_of)", ("zf", "sf", "of")),
    "GE": ("c->eflags_sf == c->eflags_of", ("sf", "of")),
    "L":  ("c->eflags_sf != c->eflags_of", ("sf", "of")),
    "LE": ("(c->eflags_zf || c->eflags_sf != c->eflags_of)", ("zf", "sf", "of")),
}
# capstone spells several conditions differently from Ghidra
for _dst, _src in (("E", "Z"), ("NE", "NZ"), ("NAE", "C"), ("NB", "NC"),
                   ("NBE", "A"), ("NA", "BE"), ("NG", "LE"), ("NGE", "L"),
                   ("NL", "GE"), ("NLE", "G"), ("PE", "P"), ("PO", "NP")):
    COND[_dst] = COND[_src]

JCC = {"J" + k: v for k, v in COND.items()}
JCC["JECXZ"] = ("c->r[1] == 0", ())
# LOOP decrements ECX and branches while it is not zero; the decrement is
# the condition, so it happens whichever way the branch goes.
JCC["LOOP"] = ("(c->r[1] = c->r[1] - 1u) != 0u", ())
SETCC = {"SET" + k: v for k, v in COND.items()}
CMOVCC = {"CMOV" + k: v for k, v in COND.items()}
# FCMOVcc tests the same EFLAGS bits as the integer forms but names parity U/NU.
FCMOVCC = {"FCMOV" + k: COND[v] for k, v in (
    ("B", "B"), ("E", "E"), ("BE", "BE"), ("U", "P"),
    ("NB", "NB"), ("NE", "NE"), ("NBE", "NBE"), ("NU", "NP"))}


# ------------------------------------------------------------------- image --

class Image(PE):
    """The program's bytes with a Capstone decoder over them."""

    def __init__(self, path, base=None):
        super().__init__(path, base)
        import capstone
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

    def seh_constructor_helper(self, va):
        """Return the handler stored by Delphi's returning constructor helper.

        The helper saves EDX/ECX/EBX, optionally calls the allocator, then
        fills the caller's 16-byte registration at ESP+16 through ECX. Match
        the whole sequence, including zeroing the FS base and restoring the
        saved registers. The handler immediate is image data, never a kit
        address. setjmp must live in the caller AFTER this helper returns.
        """
        if not hasattr(self, "_seh_constructor_cache"):
            self._seh_constructor_cache = {}
        if va not in self._seh_constructor_cache:
            prefix = bytes.fromhex("52515384d27c03ff50f431d28d4c2410648b1a8919896908c74104")
            suffix = bytes.fromhex("89410c64890a5b595ac3")
            size = len(prefix) + 4 + len(suffix)
            code = self.data[va - self.base:va - self.base + size] if self.is_exec(va) else b""
            stub = None
            if (len(code) == size and code.startswith(prefix) and code.endswith(suffix)
                    and self.is_exec(va + size - 1)):
                candidate = self.rd32(va + len(prefix))
                if self.is_exec(candidate):
                    stub = candidate
            self._seh_constructor_cache[va] = stub
        return self._seh_constructor_cache[va]







    #: x87 mnemonics whose memory operand is an integer or a control/status
    #: word rather than a float, so its width names itself (`word ptr`) instead
    #: of being spelled `float`/`double`/`extended double`.
    X87_INT = frozenset(("FILD", "FIST", "FISTP", "FIADD", "FISUB", "FISUBR",
                         "FIMUL", "FIDIV", "FIDIVR", "FICOM", "FICOMP",
                         "FNSTSW", "FSTSW", "FNSTCW", "FSTCW", "FLDCW",
                         "FNSTENV", "FLDENV", "FNSAVE", "FSAVE", "FRSTOR"))
    REP_PREFIX = {"rep": "REP", "repe": "REPE", "repz": "REPE",
                  "repne": "REPNE", "repnz": "REPNE"}
    #: Mnemonics capstone spells differently from the listing grammar.
    MNEM_ALIAS = {"POPAL": "POPAD", "PUSHAL": "PUSHAD",
                  "POPFL": "POPFD", "PUSHFL": "PUSHFD",
                  "CWTL": "CWDE", "CLTD": "CDQ", "CBTW": "CBW",
                  # Capstone names the popping compares FCOMPI/FUCOMPI;
                  # listings and the emitter use Intel FCOMIP/FUCOMIP.
                  "FCOMPI": "FCOMIP", "FUCOMPI": "FUCOMIP",
                  "IRETD": "IRET"}

    def _size_word(self, nbytes, mnem):
        if mnem.startswith("F") and mnem not in self.X87_INT:
            return {4: "float", 8: "double", 10: "extended double"}.get(nbytes)
        return {1: "byte", 2: "word", 4: "dword", 8: "qword",
                10: "extended double", 16: "xmmword"}.get(nbytes)

    def _op_text(self, ci, op, mnem):
        import capstone.x86_const as X
        if op.type == X.X86_OP_REG:
            name = ci.reg_name(op.reg).upper()
            if name.startswith("ST(") and name.endswith(")"):
                return "ST" + name[3:-1]        # capstone st(1) -> ST1
            return name
        if op.type == X.X86_OP_IMM:
            return "-0x%x" % -op.imm if op.imm < 0 else "0x%x" % op.imm
        if op.type == X.X86_OP_MEM:
            word = self._size_word(op.size, mnem)
            # Environment/state instructions have structured 14/28/94/108-byte
            # operands. Their mnemonic determines the layout, not a scalar width.
            structured = mnem in ("FNSTENV", "FSTENV", "FLDENV", "FNSAVE", "FSAVE", "FRSTOR")
            if word is None and not structured:
                raise TranslateError("%08x: unknown operand width %d for %s" %
                                     (ci.address, op.size, mnem))
            parts = []
            if op.mem.base:
                parts.append(ci.reg_name(op.mem.base).upper())
            if op.mem.index:
                parts.append("%s*0x%x" % (ci.reg_name(op.mem.index).upper(), op.mem.scale))
            if op.mem.disp or not parts:
                parts.append("-0x%x" % -op.mem.disp if op.mem.disp < 0
                             else "0x%x" % op.mem.disp)
            inner = " + ".join(parts)
            seg = ci.reg_name(op.mem.segment).upper() if op.mem.segment else ""
            return "%s%s[%s]" % ("" if structured else word + " ptr ",
                                  seg + ":" if seg else "", inner)
        raise TranslateError("unknown capstone operand type %d" % op.type)

    def to_insn(self, ci):
        """Build a listing Insn from a capstone instruction."""
        words = ci.mnemonic.split()
        rep = None
        if words[0] in self.REP_PREFIX:
            rep = self.REP_PREFIX[words[0]]
            words = words[1:]
        mnem = words[-1].upper()
        mnem = self.MNEM_ALIAS.get(mnem, mnem)
        ops = [self._op_text(ci, o, mnem) for o in ci.operands]
        if rep or mnem in Translator.STRING_MNEM:
            ops = []            # implicit ES:EDI / ESI operands carry nothing
        text = "%08x  %s%s%s" % (ci.address, mnem, "." + rep if rep else "",
                                 (" " + ",".join(ops)) if ops else "")
        return Insn(ci.address, mnem, rep, ops, text)


    def insn_end(self, va, mnem):
        """Address just past the instruction at `va`, or None if unknown.

        The Ghidra listing folds a WAIT prefix into the following x87
        mnemonic; capstone reports the two separately, so skip a leading
        WAIT when the listing calls the instruction something else.
        """
        if self.md is None or not (self.base <= va and va + 16 <= self.end):
            return None
        o = va - self.base
        got = list(self.md.disasm(self.data[o:o + 16], va, count=2))
        if not got:
            return None
        if (got[0].mnemonic in ("wait", "fwait") and mnem != "WAIT"
                and mnem.startswith("F")):
            return got[1].address + got[1].size if len(got) > 1 else None
        return got[0].address + got[0].size


# -------------------------------------------------------------- translator --

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
    targets = {Translator.branch_target(ins) for ins in fn.insns
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
                or Translator.writes_reg32(ins, reg)):
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
            target = Translator.branch_target(ins)
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


NORETURN_IMPORTS = frozenset(("RaiseException", "ExitProcess", "TerminateProcess",
                              "ExitThread", "FatalAppExitA", "FatalExit"))


class Translator(object):
    def __init__(self, image, func_addrs, allow_unmodelled=None):
        self.image = image
        self.func_addrs = func_addrs
        self.allow_unmodelled = allow_unmodelled
        self.unmodelled = []
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
            elif i in fn.seh_restores:
                active = False
            if i in returns or (ins.mnem == "RET" and self.push_ret_target(fn, i) is None):
                if active:
                    escapes.add(i)
                if i in returns:
                    continue
            work.extend((j, active) for j in self.successors(fn, i))
        return escapes

    def seh_effects(self, fn):
        """SEH runtime effects per instruction, as the decoded emitter attaches them."""
        for i in fn.seh_sites:
            if fn.insns[i].mnem != "MOV":
                raise LiftError("%08x: SEH frame site is not a MOV" % fn.insns[i].addr)
        effects = {}
        for i, ins in enumerate(fn.insns):
            if ins.mnem == "MOV" and fn.seh_sites and len(ins.ops) == 2:
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

    # -- emission ----------------------------------------------------------

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

    def translate(self, fn, entries=()):
        """Emit fn_ADDR, plus one thin wrapper per alternate entry point.

        With entries, the body becomes `static void body_ADDR(X86 *, uint32_t)`
        preceded by a dispatch switch and every entry is a one-line wrapper."""
        entries = sorted(set(entries))
        dead = {i for i, ins in enumerate(fn.insns) if ins.addr in fn.dead_addrs}
        live = [i for i in range(len(fn.insns)) if i not in dead]
        labels = set(fn.pushed_continuations) | set(entries)
        if any(ins.mnem == "JMP" and self.branch_target(ins) is None
               and not self.jumptables.get((fn.addr, ins.addr)) for ins in fn.insns):
            labels.update(fn.insns[i].addr for i in live)
        for i in live:
            ins = fn.insns[i]
            t = self.push_ret_target(fn, i)
            if t is not None and t in fn.index:
                labels.add(t)
            if ins.mnem in JCC or ins.mnem == "JMP":
                t = self.branch_target(ins)
                if t is not None and t in fn.index:
                    labels.add(t)
            labels.update(t for t in self.jumptables.get((fn.addr, ins.addr), []) if t in fn.index)
            if ins.mnem not in TERMINATORS and not fn.contiguous[i] and fn.fallthrough[i] in fn.index:
                labels.add(fn.fallthrough[i])
        last = fn.insns[live[-1]]
        if last.mnem not in TERMINATORS and (fn.fallthrough[live[-1]] or fn.end) in fn.index:
            labels.add(fn.fallthrough[live[-1]] or fn.end)

        head = fn.insns[0].addr != fn.addr
        if head:
            labels.add(fn.addr)
        out = []
        if entries:
            out.append("static void body_%08x(X86 *c, uint32_t entry_) {" % fn.addr)
            out.append("    x86_cc_settle(c);")
            if fn.seh_escapes:
                out.append("    uint64_t seh_mark_ = recomp_seh_frame_mark(c);")
            out.append("    switch (entry_) {")
            for e in entries:
                out.append("    case %s: goto L_%08x;" % (hexlit(e), e))
            out.append("    default: goto L_%08x;" % fn.addr if head else "    default: break;")
            out.append("    }")
        else:
            out.append("void fn_%08x(X86 *c) {" % fn.addr)
            out.append("    x86_cc_settle(c);")
            if fn.seh_escapes:
                out.append("    uint64_t seh_mark_ = recomp_seh_frame_mark(c);")
            if head:
                out.append("    goto L_%08x;" % fn.addr)
        for i in live:
            ins = fn.insns[i]
            if ins.addr in labels:
                out.append("L_%08x: ;" % ins.addr)
            out.extend("    " + line for line in self.emit(fn, i))
            if (not fn.contiguous[i] and ins.mnem not in TERMINATORS
                    and ins.mnem != "INT3" and not self.never_returns(ins)):
                t = fn.fallthrough[i]
                out.extend("    " + line for line in self.goto_target(fn, t, ins))
        if (last.mnem not in TERMINATORS and last.mnem != "INT3"
                and not self.never_returns(last)):
            out.append("    " + " ".join(self.goto_target(fn, fn.fallthrough[live[-1]] or fn.end, last)))
        out.append("}")
        if entries:
            out.append("void fn_%08x(X86 *c) { body_%08x(c, 0u); }" % (fn.addr, fn.addr))
            for e in entries:
                out.append("void fn_%08x(X86 *c) { body_%08x(c, %s); }" % (e, fn.addr, hexlit(e)))
        return out

    # ---- helpers used by emit -------------------------------------------

    def flags_arith(self, kind, size, carry=True, a="a_", b="b_", r="r_", rf="rf_"):
        """Flag stores for ADD/ADC/SUB/SBB/CMP/INC/DEC."""
        lines = []
        if carry and kind in ("add", "sub"):
            lines.append("c->eflags_cf = (uint32_t)((%s >> %d) & 1u);" % (rf, size))
        if kind == "add":
            lines.append("c->eflags_of = (uint32_t)((~(%s ^ %s) & (%s ^ %s)) >> %d) & 1u;"
                         % (a, b, a, r, size - 1))
        else:
            lines.append("c->eflags_of = (uint32_t)(((%s ^ %s) & (%s ^ %s)) >> %d) & 1u;"
                         % (a, b, a, r, size - 1))
        lines.append("c->eflags_af = ((%s ^ %s ^ %s) >> 4) & 1u;" % (a, b, r))
        lines.append("c->eflags_zf = (%s == 0);" % r)
        lines.append("c->eflags_sf = (%s >> %d) & 1u;" % (r, size - 1))
        lines.append("c->eflags_pf = parity8(%s);" % r)
        return lines

    def flags_logic(self, size, r="r_"):
        return ["c->eflags_cf = 0;", "c->eflags_of = 0;",
                "c->eflags_zf = (%s == 0);" % r,
                "c->eflags_sf = (%s >> %d) & 1u;" % (r, size - 1),
                "c->eflags_pf = parity8(%s);" % r]

    def rmw(self, op, size, lines):
        """For a memory destination, hoist the address into ad_ and return a
        substitute operand that reuses it."""
        if op.kind != "mem":
            return op
        lines.append("uint32_t ad_ = %s;" % addr_expr(op))
        return Op("mem", size=op.size, addr_c="ad_")

    # ---- the instruction dispatcher --------------------------------------

    def emit(self, fn, i):
        ins = fn.insns[i]
        m = ins.mnem
        # A recovered block can end on a CALL, with its last-byte estimate
        # four bytes short of the real rel32 continuation. Listing gaps can
        # also skip past it. The return address belongs to the instruction.
        nxt = fn.fallthrough[i] or (fn.insns[i + 1].addr if i + 1 < len(fn.insns) else fn.end)
        try:
            body = self._emit(fn, i, ins, m, nxt)
        except TranslateError as e:
            # An instruction this translator cannot model becomes a trap at its
            # own address rather than the end of the build. A listing routinely
            # decodes the data past a function's real last instruction as code
            # - sixteen-bit addressing and port instructions in a thirty-two-bit
            # user-mode image are the signature - and refusing the image over
            # bytes nothing executes helps nobody. Reaching one is still fatal,
            # loudly and with its address, which is the property that matters.
            if not self.allow_unmodelled:
                raise TranslateError("%08x %s: %s" % (ins.addr, ins.raw.split("  ", 1)[1], e))
            self.unmodelled.append((ins.addr, "%s: %s" % (ins.raw.split("  ", 1)[1], e)))
            body = ["recomp_unmodelled(c, 0x%08xu);" % ins.addr]
        declares = any(body[0].startswith(t) for t in
                       ("uint8_t ", "uint16_t ", "uint32_t ", "uint64_t ", "double "))
        if len(body) == 1 and not declares:
            return ["%s  /* %08x %s */" % (body[0], ins.addr, ins.raw.split("  ", 1)[1])]
        return (["{   /* %08x %s */" % (ins.addr, ins.raw.split("  ", 1)[1])]
                + ["    " + b for b in body] + ["}"])

    STRING_MNEM = frozenset(("STOSB", "STOSW", "STOSD", "MOVSB", "MOVSW", "MOVSD",
                             "LODSB", "LODSW", "LODSD", "SCASB", "SCASW", "SCASD",
                             "CMPSB", "CMPSW", "CMPSD",
                             # The port forms. A user-mode guest never reaches
                             # one; they turn up where a listing misdecodes
                             # data as code, and refusing them would fail a
                             # whole build over a byte nothing executes.
                             "INSB", "INSW", "INSD", "OUTSB", "OUTSW", "OUTSD"))

    def _emit(self, fn, i, ins, m, nxt):
        if any(AVX_OPERAND_RE.search(o) for o in ins.ops or ()):
            # Never modelled, and its operands do not parse: trap it here,
            # before parsing refuses the whole function over a path CPUID
            # keeps the guest from taking.
            return ["recomp_unmodelled(c, %s); return;" % hexlit(ins.addr)]
        if m in self.STRING_MNEM and not names_an_xmm(ins):
            # Ghidra prints the implicit ES:EDI / ESI operands; they carry no
            # information the mnemonic does not already imply.
            return self.emit_string(ins, m)
        ops = [parse_operand(o) for o in ins.ops] if ins.ops else []
        L = []

        # ---------------------------------------------------------- data --
        if m == "MOV":
            size = operand_size(ops)
            dst, src = ops
            if dst.kind == "sreg":
                # A segment load means nothing in the flat model the runtime
                # provides; a load of CS is an invalid opcode on the CPU too.
                # These come from data Ghidra decoded as code.
                return ["recomp_int(c, 6u);"] if dst.imm == SEGMENT_SELECTOR["CS"] else [";"]
            L.append(write_op(dst, size, read_op(src, size)))
            if (fn.seh_sites and (seh_chain_operand(dst) or i in fn.seh_sites)
                    and src.kind == "reg" and src.size == 32):
                L.append("c->eip = %s;" % hexlit(ins.addr))
                if i in fn.seh_sites:
                    L.append("{ jmp_buf *b_ = recomp_seh_frame_enter(c); "
                             "if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } }")
                else:
                    L.append("recomp_seh_frame_leave(c);")
            return L

        if m == "LEA":
            dst, src = ops
            if src.kind != "mem":
                raise TranslateError("LEA without memory operand")
            L.append(write_op(dst, 32, addr_expr(src)))
            return L

        if m in ("MOVSX", "MOVZX"):
            dst, src = ops
            ssize = src.size if src.kind == "mem" and src.size else (
                src.size if src.kind == "reg" else None)
            if ssize is None:
                raise TranslateError("%s: unknown source size" % m)
            if m == "MOVSX":
                val = "(uint32_t)(int32_t)(%s)(%s)" % (stype(ssize), read_op(src, ssize))
            else:
                val = "(uint32_t)(%s)" % read_op(src, ssize)
            L.append(write_op(dst, dst.size, val))
            return L

        if m == "XCHG":
            size = operand_size(ops)
            a, b = ops
            a = self.rmw(a, size, L)
            L.append("uint32_t t_ = %s;" % read_op(a, size))
            L.append(write_op(a, size, read_op(b, size)))
            L.append(write_op(b, size, "t_"))
            return L

        if m == "CMPXCHG":
            # The guest is single-threaded, so LOCK needs no host atomic.
            # Flags come from accumulator - destination on either path.
            size = operand_size(ops)
            dst, src = ops
            dst = self.rmw(dst, size, L)
            L.append("uint32_t a_ = c->r[0] & %s, b_ = %s;"
                     % (hexlit(mask_of(size)), read_op(dst, size)))
            L.append("uint64_t rf_ = (uint64_t)a_ - b_;")
            L.append("uint32_t r_ = (uint32_t)rf_ & %s;" % hexlit(mask_of(size)))
            L.append("if (a_ == b_) { %s }" % write_op(dst, size, read_op(src, size)))
            L.append("else { %s }" % write_op(Op("reg", reg=0, size=size, part=None), size, "b_"))
            L.extend(self.flags_arith("sub", size))
            return L

        if m == "CMPXCHG8B":
            # LOCK needs no host atomic under the cooperative guest scheduler.
            # Capture the guest address before the failure path changes EDX:EAX.
            L.append("uint32_t ad_ = %s;" % addr_expr(ops[0]))
            L.append("uint64_t dst_ = rd64(ad_);")
            L.append("uint64_t expected_ = ((uint64_t)c->r[2] << 32) | c->r[0];")
            L.append("c->eflags_zf = (expected_ == dst_);")
            L.append("if (c->eflags_zf) { wr64(ad_, ((uint64_t)c->r[1] << 32) | c->r[3]); }")
            L.append("else { c->r[0] = (uint32_t)dst_; c->r[2] = (uint32_t)(dst_ >> 32); }")
            return L

        if m == "XADD":
            size = operand_size(ops)
            dst, src = ops
            dst = self.rmw(dst, size, L)
            L.append("uint32_t a_ = %s, b_ = %s;" % (read_op(dst, size), read_op(src, size)))
            L.append("uint64_t rf_ = (uint64_t)a_ + b_;")
            L.append("uint32_t r_ = (uint32_t)rf_ & %s;" % hexlit(mask_of(size)))
            L.append(write_op(src, size, "a_"))
            L.append(write_op(dst, size, "r_"))
            L.extend(self.flags_arith("add", size))
            return L

        if m == "PUSH":
            size = operand_size(ops, hint=32)
            if size == 16:
                L.append("uint32_t v_ = %s;" % read_op(ops[0], 16))
                L.append("c->r[4] -= 2; wr16(c->r[4], (uint16_t)v_);")
                return L
            if size != 32:
                raise TranslateError("non-32-bit PUSH")
            L.append("uint32_t v_ = %s;" % read_op(ops[0], 32))
            L.append("c->r[4] -= 4; wr32(c->r[4], v_);")
            t = self.push_ret_target(fn, i)
            if t is not None and t not in fn.pushed_continuations:
                # Preserve the guest stack write even though the pair has no
                # net stack effect, then take the RET's guest continuation.
                L.append("c->eip = v_; c->r[4] += 4;")
                L.extend(self.goto_target(fn, t, ins))
            return L

        if m == "POP":
            size = operand_size(ops, hint=32)
            if size == 16:
                L.append("uint32_t v_ = rd16(c->r[4]); c->r[4] += 2;")
                L.append(write_op(ops[0], 16, "v_"))
                return L
            if size != 32:
                raise TranslateError("non-32-bit POP")
            L.append("uint32_t v_ = rd32(c->r[4]); c->r[4] += 4;")
            L.append(write_op(ops[0], 32, "v_"))
            if i in fn.seh_restores:
                L.append("c->eip = %s; recomp_seh_frame_leave(c);" % hexlit(ins.addr))
            return L

        if m == "PUSHAD":
            return ["x86_pushad(c);"]
        if m == "POPAD":
            return ["x86_popad(c);"]
        if m == "PUSHFD":
            return ["c->r[4] -= 4; wr32(c->r[4], x86_get_eflags(c));"]
        if m == "POPFD":
            return ["x86_set_eflags(c, rd32(c->r[4])); c->r[4] += 4;"]
        if m == "PUSHF":
            return ["c->r[4] -= 2; wr16(c->r[4], (uint16_t)x86_get_eflags(c));"]
        if m == "POPF":
            return ["x86_set_eflags(c, (x86_get_eflags(c) & 0xffff0000u) | rd16(c->r[4]));",
                    "c->r[4] += 2;"]
        if m == "LEAVE":
            return ["c->r[4] = c->r[5]; c->r[5] = rd32(c->r[4]); c->r[4] += 4;"]
        if m == "SAHF":
            return ["x86_sahf(c);"]
        if m == "LAHF":
            # AH = SF:ZF:0:AF:0:PF:1:CF
            return ["c->r[0] = (c->r[0] & 0xffff00ffu) | "
                    "(((x86_get_eflags(c) & 0xd5u) | 0x02u) << 8);"]
        if m == "XLAT":
            return ["xlat(c);"]
        if m == "BSWAP":
            return [write_op(ops[0], 32, "bswap32(%s)" % read_op(ops[0], 32))]

        if m == "CDQ":
            return ["c->r[2] = (uint32_t)((int32_t)c->r[0] >> 31);"]
        if m == "CWD":
            return ["c->r[2] = (c->r[2] & 0xffff0000u) | "
                    "((uint32_t)((int16_t)c->r[0] >> 15) & 0xffffu);"]
        if m == "CBW":
            return ["c->r[0] = (c->r[0] & 0xffff0000u) | "
                    "((uint32_t)(int32_t)(int8_t)c->r[0] & 0xffffu);"]
        if m == "CWDE":
            return ["c->r[0] = (uint32_t)(int32_t)(int16_t)c->r[0];"]
        if m in ("LDMXCSR", "STMXCSR"):
            # The SSE control word. This kit models one rounding mode and
            # masks every exception, which is what a guest sets it to; a store
            # hands back exactly that.
            if m == "STMXCSR":
                return [write_op(ops[0], 32, "0x1f80u")]
            return [";"]

        # ------------------------------------------------------- arith ----
        if m in ("ADD", "ADC", "SUB", "SBB", "CMP"):
            size = operand_size(ops)
            dst, src = ops
            dst = self.rmw(dst, size, L)
            L.append("uint32_t a_ = %s, b_ = %s;" % (read_op(dst, size), read_op(src, size)))
            if m in ("ADD", "ADC"):
                carry = " + c->eflags_cf" if m == "ADC" else ""
                L.append("uint64_t rf_ = (uint64_t)a_ + b_%s;" % carry)
                kind = "add"
            else:
                carry = " - c->eflags_cf" if m == "SBB" else ""
                L.append("uint64_t rf_ = (uint64_t)a_ - b_%s;" % carry)
                kind = "sub"
            L.append("uint32_t r_ = (uint32_t)rf_ & %s;" % hexlit(mask_of(size)))
            if m != "CMP":
                L.append(write_op(dst, size, "r_"))
            L += self.flags_arith(kind, size)
            return L

        if m in ("INC", "DEC"):
            size = operand_size(ops)
            dst = self.rmw(ops[0], size, L)
            L.append("uint32_t a_ = %s, b_ = 1u;" % read_op(dst, size))
            L.append("uint64_t rf_ = (uint64_t)a_ %s b_;" % ("+" if m == "INC" else "-"))
            L.append("uint32_t r_ = (uint32_t)rf_ & %s;" % hexlit(mask_of(size)))
            L.append(write_op(dst, size, "r_"))
            L += self.flags_arith("add" if m == "INC" else "sub", size, carry=False)
            return L

        if m == "NEG":
            size = operand_size(ops)
            dst = self.rmw(ops[0], size, L)
            L.append("uint32_t b_ = %s, a_ = 0u;" % read_op(dst, size))
            L.append("uint64_t rf_ = (uint64_t)a_ - b_;")
            L.append("uint32_t r_ = (uint32_t)rf_ & %s;" % hexlit(mask_of(size)))
            L.append(write_op(dst, size, "r_"))
            L += self.flags_arith("sub", size)
            return L

        if m == "NOT":
            size = operand_size(ops)
            dst = self.rmw(ops[0], size, L)
            L.append(write_op(dst, size, "~%s" % read_op(dst, size)))
            return L

        if m in ("AND", "OR", "XOR", "TEST"):
            size = operand_size(ops)
            dst, src = ops
            cop = {"AND": "&", "OR": "|", "XOR": "^", "TEST": "&"}[m]
            dst2 = self.rmw(dst, size, L) if m != "TEST" else dst
            L.append("uint32_t r_ = (%s %s %s) & %s;"
                     % (read_op(dst2, size), cop, read_op(src, size), hexlit(mask_of(size))))
            if m != "TEST":
                L.append(write_op(dst2, size, "r_"))
            L += self.flags_logic(size)
            return L

        # ------------------------------------------------- shift / rotate --
        if m in ("SHL", "SHR", "SAR", "ROL", "ROR", "RCL", "RCR"):
            size = ops[0].size or operand_size(ops, hint=32)
            dst = self.rmw(ops[0], size, L)
            cnt = read_op(ops[1], 32) if len(ops) > 1 else "1u"
            if len(ops) > 1 and ops[1].kind == "reg":
                cnt = "(uint32_t)%s" % read_op(ops[1], ops[1].size)
            fn_base = m.lower() + str(size)
            call = "%s_f(c, %s, %s)" % (fn_base, read_op(dst, size), cnt)
            L.append(write_op(dst, size, call))
            return L

        if m in ("SHLD", "SHRD"):
            size = operand_size(ops[:2])
            if size != 32:
                raise TranslateError("%s at width %d" % (m, size))
            dst = self.rmw(ops[0], 32, L)
            cnt = ("(uint32_t)%s" % read_op(ops[2], ops[2].size)
                   if ops[2].kind == "reg" else read_op(ops[2], 32))
            L.append(write_op(dst, 32, "%s32_f(c, %s, %s, %s)"
                              % (m.lower(), read_op(dst, 32), read_op(ops[1], 32), cnt)))
            return L

        # -------------------------------------------------- mul  /  div ---
        if m == "IMUL" and len(ops) >= 2:
            size = operand_size(ops[:2])
            if len(ops) == 2:
                a, b = read_op(ops[0], size), read_op(ops[1], size)
            else:
                a, b = read_op(ops[1], size), read_op(ops[2], size)
            L.append(write_op(ops[0], size, "imul2_%d_f(c, %s, %s)" % (size, a, b)))
            return L

        if m in ("MUL", "IMUL", "DIV", "IDIV"):
            size = operand_size(ops)
            src = read_op(ops[0], size)
            base = m.lower() + str(size)
            if m in ("DIV", "IDIV"):
                L.append("%s(c, (%s)(%s), %s);" % (base, utype(size), src, hexlit(ins.addr)))
            else:
                L.append("%s(c, (%s)(%s));" % (base, utype(size), src))
            return L

        # ------------------------------------------------------- bit ops --
        if m in ("BT", "BTS", "BTR", "BTC"):
            size = operand_size(ops, hint=32)
            dst, src = ops
            idx = read_op(src, size)
            if dst.kind == "mem":
                L.append("uint32_t bi_ = %s;" % idx)
                L.append("uint32_t ad_ = %s + 4u * (bi_ >> 5);" % addr_expr(dst))
                dst = Op("mem", size=32, addr_c="ad_")
                bit = "(bi_ & 31u)"
            else:
                L.append("uint32_t bi_ = (%s) & %du;" % (idx, size - 1))
                bit = "bi_"
            L.append("uint32_t v_ = %s;" % read_op(dst, size))
            L.append("c->eflags_cf = (v_ >> %s) & 1u;" % bit)
            if m == "BTS":
                L.append(write_op(dst, size, "v_ | (1u << %s)" % bit))
            elif m == "BTR":
                L.append(write_op(dst, size, "v_ & ~(1u << %s)" % bit))
            elif m == "BTC":
                L.append(write_op(dst, size, "v_ ^ (1u << %s)" % bit))
            return L

        if m in ("BSR", "BSF"):
            # With a zero source the destination is architecturally undefined;
            # unicorn (and AMD) leave it unchanged, so pass the old value in.
            L.append(write_op(ops[0], 32, "%s32_f(c, %s, %s)"
                              % (m.lower(), read_op(ops[0], 32), read_op(ops[1], 32))))
            return L

        if m in SETCC:
            cond = SETCC[m][0]
            L.append(write_op(ops[0], 8, "(%s) ? 1u : 0u" % cond))
            return L

        if m in CMOVCC:
            size = operand_size(ops)
            cond = CMOVCC[m][0]
            L.append("if (%s) { %s }" % (cond, write_op(ops[0], size,
                                                        read_op(ops[1], size))))
            return L

        if m == "CLD":
            return ["c->eflags_df = 0;"]
        if m == "STD":
            return ["c->eflags_df = 1;"]
        if m == "CLC":
            return ["c->eflags_cf = 0;"]
        if m == "STC":
            return ["c->eflags_cf = 1;"]
        if m == "CMC":
            return ["c->eflags_cf = !c->eflags_cf;"]

        # ---------------------------------------------------- control flow --
        if m in JCC:
            cond = JCC[m][0]
            t = self.branch_target(ins)
            if t is None:
                raise TranslateError("indirect conditional jump")
            return ["if (%s) { %s }" % (cond, " ".join(self.goto_target(fn, t, ins)))]

        if m == "JMP":
            t = self.branch_target(ins)
            if t is not None:
                return self.goto_target(fn, t, ins)
            return self.emit_indirect_jump(fn, i, ins, ops[0])

        if m == "CALL":
            if ins.ops and ins.ops[0].startswith("0x"):
                t = int(ins.ops[0], 16)
                L.append("c->r[4] -= 4; wr32(c->r[4], %s);" % hexlit(nxt))
                if t in self.func_addrs:
                    L.append("CALL_FN(%08x);" % t)
                else:
                    self.reject_offimage_call(t)
                    L.append("recomp_call(c, %s);" % hexlit(t))
                # An SSA callee may return with a pending flags descriptor; the
                # decoded caller reads the guest's fields directly.
                L.append("x86_cc_settle(c);")
                if t in self.seh_helpers:
                    L.append("c->eip = %s;" % hexlit(ins.addr))
                    L.append("{ jmp_buf *b_ = recomp_seh_frame_adopt(c); "
                             "if (b_) { if (RECOMP_SETJMP(*b_)) { recomp_seh_land(c); return; } } }")
                if t in self.noreturn_callees:
                    # The callee throws or exits; what follows is padding and
                    # tables, never code.  Reaching this line means it came
                    # back after all, which the runtime then reports by address.
                    L.append("recomp_unknown_call(c, %s); return;" % hexlit(nxt))
                return L
            L.append("uint32_t t_ = %s;" % read_op(ops[0], 32))
            L.append("c->r[4] -= 4; wr32(c->r[4], %s);" % hexlit(nxt))
            L.append("recomp_call(c, t_);")
            L.append("x86_cc_settle(c);")
            return L

        if m == "RET":
            n = parse_imm(ins.ops[0]) if ins.ops else 0
            orphan = "recomp_seh_frame_orphan(c, seh_mark_); " if i in fn.seh_escapes else ""
            if fn.pushed_continuations:
                # Normal finally cleanup stays in the establishing C frame.
                # An alternate entry called by the exception dispatcher has
                # its own return address and takes the default arm instead.
                L = ["uint32_t r_ = rd32(c->r[4]); c->r[4] += %du;" % (4 + n),
                     "switch (r_) {"]
                L.extend("case %s: goto L_%08x;" % (hexlit(t), t)
                         for t in sorted(fn.pushed_continuations))
                L.extend(["default: c->eip = r_; " + orphan + "recomp_return(c); return;", "}"])
                return L
            return ["c->eip = rd32(c->r[4]); c->r[4] += %du; " % (4 + n) +
                    orphan + "recomp_return(c); return;"]

        # ------------------------------------------------------------ SSE --
        # Data movement only, in dword lanes. A Delphi runtime's FillChar and
        # Move reach for these unconditionally - SSE2 predates every CPU the
        # compiler supports, so there is no feature test to fail - while the
        # AVX forms beside them are gated on a CPUID bit this kit does not
        # set, and stay traps nobody reaches.
        # MMX first: is_mmx_insn only matches an MMn operand with no XMM one,
        # so the SSE2 path below still takes every xmm form of MOVQ/MOVD.
        if is_mmx_insn(m, ins.ops):
            return self.emit_mmx(ins, m)
        # Lane-wise integer, logical and unpack forms. Every one of them has
        # the same shape: both operands are read in full before either lane of
        # the destination is written, because a destination is commonly also
        # the source and these are not sequences of independent moves.
        SSE_LANE_OPS = {"PXOR": "^", "XORPD": "^", "XORPS": "^", "PAND": "&", "ANDPD": "&",
                        "ANDPS": "&", "POR": "|", "ORPD": "|", "ORPS": "|"}
        SSE_LANE_FORMS = ("PANDN", "ANDNPD", "ANDNPS", "PCMPEQD", "PUNPCKLDQ", "PUNPCKHDQ",
                          "PUNPCKLQDQ", "UNPCKLPD", "UNPCKHPD", "MOVDDUP", "MOVLPD", "MOVLPS",
                          "MOVHPD", "MOVHPS", "MOVLHPS", "SHUFPS", "SHUFPD", "MOVAPD", "MOVUPD")
        if m in SSE_LANE_OPS or m in SSE_LANE_FORMS:
            dst, src = ops[0], ops[1]

            def lane(op, i):
                if op.kind == "xmm":
                    return "c->xmm[%d][%d]" % (op.reg, i)
                return "rd32(%s + %du)" % (addr_expr(op), 4 * i)

            def put(op, i, value):
                if op.kind == "xmm":
                    return "c->xmm[%d][%d] = %s;" % (op.reg, i, value)
                return "wr32(%s + %du, %s);" % (addr_expr(op), 4 * i, value)

            # d0_..d3_ and s0_..s3_ hold the operands as they were on entry.
            # A half-width form reads only the half it uses.
            halves = 2 if m in ("MOVLPD", "MOVLPS", "MOVHPD", "MOVHPS", "MOVLHPS") else 4
            L.extend("uint32_t d%d_ = %s;" % (i, lane(dst, i)) for i in range(halves))
            L.extend("uint32_t s%d_ = %s;" % (i, lane(src, i)) for i in range(halves))
            d = lambda i: "d%d_" % i
            sv = lambda i: "s%d_" % i

            if m in SSE_LANE_OPS:
                L.extend(put(dst, i, "%s %s %s" % (d(i), SSE_LANE_OPS[m], sv(i)))
                         for i in range(4))
            elif m in ("PANDN", "ANDNPD", "ANDNPS"):
                L.extend(put(dst, i, "(~%s) & %s" % (d(i), sv(i))) for i in range(4))
            elif m == "PCMPEQD":
                L.extend(put(dst, i, "%s == %s ? 0xffffffffu : 0u" % (d(i), sv(i)))
                         for i in range(4))
            elif m in ("MOVAPD", "MOVUPD"):   # MOVAPS and MOVUPS under another name
                L.extend(put(dst, i, sv(i)) for i in range(4))
            elif m == "PUNPCKLDQ":            # d0 s0 d1 s1
                L.extend([put(dst, 0, d(0)), put(dst, 1, sv(0)),
                          put(dst, 2, d(1)), put(dst, 3, sv(1))])
            elif m == "PUNPCKHDQ":            # d2 s2 d3 s3
                L.extend([put(dst, 0, d(2)), put(dst, 1, sv(2)),
                          put(dst, 2, d(3)), put(dst, 3, sv(3))])
            elif m in ("PUNPCKLQDQ", "UNPCKLPD"):   # d0 d1 s0 s1
                L.extend([put(dst, 2, sv(0)), put(dst, 3, sv(1))])
            elif m == "UNPCKHPD":                   # d2 d3 s2 s3
                L.extend([put(dst, 0, d(2)), put(dst, 1, d(3)),
                          put(dst, 2, sv(2)), put(dst, 3, sv(3))])
            elif m == "MOVDDUP":                    # s0 s1 s0 s1
                L.extend([put(dst, 0, sv(0)), put(dst, 1, sv(1)),
                          put(dst, 2, sv(0)), put(dst, 3, sv(1))])
            elif m == "MOVLHPS":
                # Raw low source lanes replace the high destination lanes.
                # Snapshot the source before stores, including dst == src.
                L.extend(put(dst, 2 + i, sv(i)) for i in range(2))
            elif m in ("MOVLPD", "MOVLPS", "MOVHPD", "MOVHPS"):
                # One 64-bit half moves; the other half keeps what it had.
                half = 0 if m in ("MOVLPD", "MOVLPS") else 2
                if dst.kind == "xmm":
                    L.extend(put(dst, half + i, sv(i)) for i in range(2))
                else:
                    L.extend(put(dst, i, "c->xmm[%d][%d]" % (src.reg, half + i))
                             for i in range(2))
            elif m == "SHUFPS":
                # Lanes 0 and 1 come from the destination, 2 and 3 from the source.
                sel = parse_imm(ins.ops[2])
                L.extend([put(dst, 0, d(sel & 3)), put(dst, 1, d((sel >> 2) & 3)),
                          put(dst, 2, sv((sel >> 4) & 3)), put(dst, 3, sv((sel >> 6) & 3))])
            else:  # SHUFPD: one double from each operand
                sel = parse_imm(ins.ops[2])
                lo, hi = 2 * (sel & 1), 2 * ((sel >> 1) & 1)
                L.extend([put(dst, 0, d(lo)), put(dst, 1, d(lo + 1)),
                          put(dst, 2, sv(hi)), put(dst, 3, sv(hi + 1))])
            return L

        # Scalar double and single forms. The host's double and float are the
        # same IEEE formats in the same rounding mode, and scalar SSE leaves
        # the lanes above its result alone, which is why nothing here clears
        # them except a load from memory, where the hardware does.
        SSE_SCALAR_MATH = {"ADDSD": "+", "SUBSD": "-", "MULSD": "*", "DIVSD": "/",
                           "ADDSS": "+", "SUBSS": "-", "MULSS": "*", "DIVSS": "/"}
        SSE_SCALAR_FORMS = ("MOVSD", "MOVSS", "SQRTSD", "SQRTSS", "MINSD", "MAXSD",
                            "MINSS", "MAXSS", "CVTSI2SD", "CVTSI2SS", "CVTTSD2SI",
                            "CVTTSS2SI", "CVTSD2SS", "CVTSS2SD", "COMISD", "UCOMISD",
                            "COMISS", "UCOMISS")
        # Only MOVSD and MOVSS are shared with another instruction; the rest
        # of these mnemonics are SSE whatever their operands are, and a
        # conversion's source or destination is often memory or a register.
        if (m in SSE_SCALAR_MATH
                or (m in SSE_SCALAR_FORMS
                    and (m not in ("MOVSD", "MOVSS") or names_an_xmm(ins)))):
            dst, src = ops[0], ops[1]
            # Which host type this form works in. The conversions name both,
            # so they spell their own operands out instead.
            single = m.endswith("SS") and m not in ("CVTSD2SS",)

            def scalar(op, as_single):
                if op.kind == "xmm":
                    return "xmm_f32(c, %d)" % op.reg if as_single else "xmm_f64(c, %d)" % op.reg
                return "rdf32(%s)" % addr_expr(op) if as_single else "rdf64(%s)" % addr_expr(op)

            def store_scalar(op, value, as_single):
                if op.kind == "xmm":
                    return ("xmm_set_f32(c, %d, %s);" if as_single else "xmm_set_f64(c, %d, %s);") \
                        % (op.reg, value)
                return ("wrf32(%s, %s);" if as_single else "wrf64(%s, %s);") \
                    % (addr_expr(op), value)

            if m in SSE_SCALAR_MATH:
                L.append(store_scalar(dst, "%s %s %s" % (scalar(dst, single), SSE_SCALAR_MATH[m],
                                                         scalar(src, single)), single))
            elif m in ("SQRTSD", "SQRTSS"):
                L.append(store_scalar(dst, "%s(%s)" % ("recomp_sse_sqrtf" if single
                                                       else "recomp_sse_sqrt",
                                                       scalar(src, single)), single))
            elif m in ("MINSD", "MAXSD", "MINSS", "MAXSS"):
                # The hardware returns its second operand when either is a NaN
                # or both are zero, which is what this spelling does.
                t = "float" if single else "double"
                keep = "<" if m in ("MINSD", "MINSS") else ">"
                L.append("{ %s a_ = %s, b_ = %s; %s }"
                         % (t, scalar(dst, single), scalar(src, single),
                            store_scalar(dst, "b_ %s a_ ? a_ : b_" % keep, single)))
            elif m in ("COMISD", "UCOMISD", "COMISS", "UCOMISS"):
                L.append("recomp_comis(c, %s, %s);" % (scalar(dst, single), scalar(src, single)))
            elif m in ("CVTSI2SD", "CVTSI2SS"):
                L.append(store_scalar(dst, "(%s)(int32_t)%s" % ("float" if single else "double",
                                                                read_op(src, 32)), single))
            elif m in ("CVTTSD2SI", "CVTTSS2SI"):
                L.append(write_op(dst, 32, "(uint32_t)(int32_t)%s" % scalar(src, single)))
            elif m == "CVTSD2SS":
                L.append(store_scalar(dst, "(float)%s" % scalar(src, False), True))
            elif m == "CVTSS2SD":
                L.append(store_scalar(dst, "(double)%s" % scalar(src, True), False))
            else:  # MOVSD, MOVSS between registers or memory
                L.append(store_scalar(dst, scalar(src, single), single))
                if dst.kind == "xmm" and src.kind != "xmm":
                    # A load clears what is above the value; a move between
                    # registers keeps it.
                    L.extend("c->xmm[%d][%d] = 0u;" % (dst.reg, i)
                             for i in range(1 if single else 2, 4))
            return L

        if m in ("MOVUPS", "MOVAPS", "MOVDQU", "MOVDQA", "MOVQ", "MOVD", "PSHUFD"):
            def lane(op, i):
                if op.kind == "xmm":
                    return "c->xmm[%d][%d]" % (op.reg, i)
                return "rd32(%s + %du)" % (addr_expr(op), 4 * i)

            def put(op, i, value):
                if op.kind == "xmm":
                    return "c->xmm[%d][%d] = %s;" % (op.reg, i, value)
                return "wr32(%s + %du, %s);" % (addr_expr(op), 4 * i, value)

            dst, src = ops[0], ops[1]
            if m == "PSHUFD":
                sel = parse_imm(ins.ops[2])
                # Read every lane before writing one: the destination is
                # commonly the source, and a shuffle is not a sequence of
                # independent moves.
                L.extend("uint32_t s%d_ = %s;" % (i, lane(src, i)) for i in range(4))
                L.extend(put(dst, i, "s%d_" % ((sel >> (2 * i)) & 3)) for i in range(4))
                return L
            if m == "MOVD":
                # A dword between an XMM lane and a general register or memory,
                # zeroing what is above it when the XMM side is written.
                if dst.kind == "xmm":
                    L.append(put(dst, 0, read_op(src, 32)))
                    L.extend(put(dst, i, "0u") for i in range(1, 4))
                    return L
                L.append(write_op(dst, 32, lane(src, 0)))
                return L
            lanes = 2 if m == "MOVQ" else 4
            L.extend("uint32_t s%d_ = %s;" % (i, lane(src, i)) for i in range(lanes))
            L.extend(put(dst, i, "s%d_" % i) for i in range(lanes))
            # MOVQ into a register clears the upper half; into memory it
            # writes eight bytes and stops.
            if m == "MOVQ" and dst.kind == "xmm":
                L.extend(put(dst, i, "0u") for i in range(2, 4))
            return L
        if m in ("SFENCE", "LFENCE", "MFENCE", "VZEROUPPER", "VZEROALL", "PREFETCHNTA",
                 "PREFETCHT0", "PREFETCHT1", "PREFETCHT2"):
            # Ordering and cache hints on a machine with one guest thread of
            # execution at a time, and no AVX state to clear.
            return [";"]

        # --------------------------------------------------------- system --
        if m == "RDTSC":
            return ["recomp_rdtsc(c);"]
        if m == "CPUID":
            return ["recomp_cpuid(c);"]
        if m == "CLI":
            return ["recomp_cli(c);"]
        if m == "STI":
            return ["recomp_sti(c);"]
        if m == "HLT":
            return ["recomp_hlt(c);"]
        if m == "INT":
            return ["recomp_int(c, %s);" % hexlit(parse_imm(ins.ops[0]))]
        if m == "INT3":
            return ["recomp_int(c, 3u);"]
        if m == "IN":
            size = operand_size(ops[:1], hint=32)
            port = read_op(ops[1], 32) if len(ops) > 1 else "0u"
            L.append(write_op(ops[0], size, "recomp_in(c, %s, %d)" % (port, size // 8)))
            return L
        if m == "OUT":
            size = operand_size(ops[1:], hint=8)
            port = read_op(ops[0], 32) if ops[0].kind == "reg" else read_op(ops[0], 32)
            L.append("recomp_out(c, %s, %s, %d);" % (port, read_op(ops[1], size), size // 8))
            return L
        if m in ("NOP", "WAIT", "PAUSE"):
            return [";"]
        if m == "EMMS":
            # Every x87 register empty; TOP and the values are left alone.
            # Codecs call a bare `emms; ret` whatever CPUID said.
            return ["c->fpu_tag = 0xffffu;"]
        if m == "STMXCSR":
            # No translated SSE arithmetic changes MXCSR, so expose its reset value.
            return ["wr32(%s, 0x1f80u);" % addr_expr(ops[0])]
        if m in ("FNCLEX", "FCLEX"):
            return ["c->fpu_sw &= (uint16_t)~0x80ffu;"]

        # ------------------------------------------------------------ x87 --
        if m.startswith("F"):
            return self.emit_x87(fn, ins, m, ops)

        if is_vector_insn(m, ins.ops or ()):
            # Not one of the forms modelled above: a trap says so if a guest
            # that skipped the CPUID check ever reaches it.
            return ["recomp_unmodelled(c, %s); return;" % hexlit(ins.addr)]
        raise TranslateError("unhandled mnemonic %s" % m)

    # ---- control-flow helpers -------------------------------------------

    def reject_offimage_call(self, t):
        """A direct CALL whose literal target is not in the image at all.

        No such instruction can be real: the bytes were decoded out of step
        with the stream, which is what a listing does to the padding and
        tables behind a function's last instruction. Under --allow-unmodelled
        it goes through the same door as any other instruction the translator
        cannot model - a trap at its own address - rather than surviving as a
        dispatch target that reaches no translated code and fails the build
        later, far from its cause. Without the switch the dangling-target
        check still reports it, which is the stricter reading and stays the
        default.

        Only a call. A conditional jump out of the image is how a bad
        speculative block gives itself away."""
        if not self.allow_unmodelled:
            return
        if self.image.end <= self.image.base:
            return  # a translator built without bytes, as the unit tests are
        if self.image.base <= t < self.image.end:
            return
        if GUEST_SHIM_BASE <= t < GUEST_SHIM_END:
            return
        raise TranslateError("call to %08x, which is outside the image" % t)

    def goto_target(self, fn, t, ins):
        if t in fn.index:
            return ["goto L_%08x;" % t]
        if t in self.func_addrs:
            return ["CALL_FN(%08x); return;" % t]
        return ["c->eip = %s; recomp_jump(c, %s); return;" % (hexlit(ins.addr), hexlit(t))]

    def emit_indirect_jump(self, fn, i, ins, op):
        if i in fn.return_jumps:
            orphan = ["recomp_seh_frame_orphan(c, seh_mark_);"] if i in fn.seh_escapes else []
            # A JMP through the entry stack slot is a RET written out longhand,
            # so it ends like one. Returning on the strength of the proof alone
            # assumes the slot held this host frame's return address, and the
            # proof only ever established where the value came from, not what
            # it is: a block recovered as its own function starts at delta zero
            # holding whatever its real caller pushed, and for Delphi's finally
            # idiom - PUSH resume; CALL cleanup; POP EAX; JMP EAX - that is a
            # continuation INTO the establishing body. Setting EIP and
            # returning drops it, and the establishing body's epilogue never
            # runs, so it never restores EBP; its caller then reads its own
            # locals through a frame pointer that moved, and the damage shows
            # up as a wrong value somewhere else entirely. recomp_return keeps
            # a genuine return as cheap as it was and dispatches the rest.
            return orphan + ["c->eip = %s; recomp_return(c); return;" % read_op(op, 32)]
        targets = self.jumptables.get((fn.addr, ins.addr))
        L = ["uint32_t t_ = %s;" % read_op(op, 32)]
        if not targets:
            # A table-less jump may enter any instruction of this body. Keep
            # that transfer in the current host frame; nonlocal targets and
            # holes in the listing retain the existing runtime dispatch.
            L.append("if (t_ >= %s && t_ < %s) {" % (hexlit(fn.insns[0].addr), hexlit(fn.end)))
            L.append("switch (t_) {")
            for t in sorted(fn.addrs - getattr(fn, "dead_addrs", set())):
                L.append("case %s: goto L_%08x;" % (hexlit(t), t))
            L.extend(["default: break;", "}", "}"])
            L.append("c->eip = %s; recomp_jump(c, t_); return;" % hexlit(ins.addr))
            return L
        L.append("switch (t_) {")
        for t in sorted(set(targets)):
            if t in fn.index:
                L.append("case %s: goto L_%08x;" % (hexlit(t), t))
            else:
                L.append("case %s: CALL_FN(%08x); return;" % (hexlit(t), t))
        L.append("default: c->eip = %s; recomp_jump(c, t_); return;" % hexlit(ins.addr))
        L.append("}")
        return L

    def emit_string(self, ins, m):
        suf = {"B": "b", "W": "w", "D": "d"}[m[-1]]
        kind = m[:-1].lower()
        if kind in ("scas", "cmps"):
            if ins.rep == "REPE" or ins.rep == "REP":
                return ["repe_%s(c);" % m.lower()]
            if ins.rep == "REPNE":
                return ["repne_%s(c);" % m.lower()]
            if ins.rep:
                raise TranslateError("prefix %s on %s" % (ins.rep, m))
            return ["%s(c);" % m.lower()]
        if ins.rep in ("REP", "REPE"):
            return ["rep_%s%s(c);" % (kind, suf)]
        if ins.rep:
            raise TranslateError("prefix %s on %s" % (ins.rep, m))
        return ["%s%s(c);" % (kind, suf)]


    # ---- x87 -------------------------------------------------------------

    X87_MEM_INT = {"FILD", "FISTP", "FIST", "FIADD", "FISUB", "FISUBR",
                   "FIMUL", "FIDIV", "FIDIVR", "FICOM", "FICOMP"}

    def x87_mem_value(self, op):
        """Value of an x87 memory source as a C double expression."""
        if op.size == 32:
            return "(double)rdf32(%s)" % addr_expr(op)
        if op.size == 64:
            return "rdf64(%s)" % addr_expr(op)
        if op.size == 80:
            return "rdf80(%s)" % addr_expr(op)
        raise TranslateError("bad x87 memory size %r" % op.size)

    def x87_int_value(self, op):
        return "(double)" + self.x87_signed_value(op)

    def x87_signed_value(self, op):
        if op.size in (16, 32, 64):
            return "(int%d_t)rd%d(%s)" % (op.size, op.size, addr_expr(op))
        raise TranslateError("bad x87 integer size %r" % op.size)

    def emit_mmx(self, ins, m):
        """MMX on the eight MMn registers, through runtime/x86.h's helpers."""
        ops = [parse_operand(o) for o in ins.ops]

        def src(op, width=64):
            if op.kind == "mm":
                return "c->mm[%d]" % op.reg
            if op.kind == "mem":
                return "rd64(%s)" % addr_expr(op) if width == 64 else "(uint64_t)rd32(%s)" % addr_expr(op)
            if op.kind == "imm":
                return "(uint64_t)0x%xu" % (op.imm & 0xff)
            if op.kind == "reg" and op.size == 32:
                return "(uint64_t)c->r[%d]" % op.reg
            raise TranslateError("MMX operand %r" % op.kind)

        if m == "MOVQ":
            dst, s = ops
            if dst.kind == "mm":
                return ["c->mm[%d] = %s;" % (dst.reg, src(s))]
            if dst.kind == "mem" and s.kind == "mm":
                return ["wr64(%s, c->mm[%d]);" % (addr_expr(dst), s.reg)]
            raise TranslateError("MOVQ form")
        if m == "MOVD":
            dst, s = ops
            if dst.kind == "mm":
                return ["c->mm[%d] = %s;" % (dst.reg, src(s, 32))]
            if s.kind == "mm":
                return [write_op(dst, 32, "(uint32_t)c->mm[%d]" % s.reg)]
            raise TranslateError("MOVD form")
        dst = ops[0]
        if dst.kind != "mm":
            raise TranslateError("%s destination is not an MMX register" % m)
        if m == "PSHUFW":
            return ["c->mm[%d] = mmx_pshufw(%s, %du);" % (dst.reg, src(ops[1]), ops[2].imm & 0xff)]
        if m in MMX_SHIFT:
            fn, bits = MMX_SHIFT[m]
            return ["c->mm[%d] = %s(c->mm[%d], %s, %du);" % (dst.reg, fn, dst.reg, src(ops[1]), bits)]
        fn, bits = MMX_BINARY[m]
        if bits:
            return ["c->mm[%d] = %s(c->mm[%d], %s, %du);" % (dst.reg, fn, dst.reg, src(ops[1]), bits)]
        return ["c->mm[%d] = %s(c->mm[%d], %s);" % (dst.reg, fn, dst.reg, src(ops[1]))]

    def emit_x87(self, fn, ins, m, ops):
        L = []
        st = lambda n: "ST(c, %d)" % n
        # Writing a register goes through fset so its tag follows the value.
        setst = lambda n, v: "fset(c, %d, %s);" % (n, v)

        if m == "FLD":
            if ops[0].kind == "st":
                L.append("fpush_st(c, %d);" % ops[0].sti)
            else:
                L.append("fpush(c, %s);" % self.x87_mem_value(ops[0]))
            return L
        if m == "FLD1":
            return ["fpush(c, 1.0);"]
        if m == "FLDZ":
            return ["fpush(c, 0.0);"]
        if m == "FLDPI":
            return ["fpush(c, 3.14159265358979323846);"]
        if m == "FLDLN2":
            return ["fpush(c, 0.69314718055994530942);"]
        if m == "FLDL2E":
            return ["fpush(c, 1.44269504088896340736);"]
        if m == "FLDLG2":
            return ["fpush(c, 0.30102999566398119521);"]
        if m == "FLDL2T":
            return ["fpush(c, 3.32192809488736234787);"]
        if m == "FBSTP":
            return ["wrbcd80(%s, fpop(c));" % addr_expr(ops[0])]
        if m == "FILD":
            return ["fpush_int(c, %s);" % self.x87_signed_value(ops[0])]

        if m in ("FST", "FSTP"):
            if ops and ops[0].kind == "st":
                if ops[0].sti != 0:
                    L.append("fcopy(c, %d, 0);" % ops[0].sti)
            elif ops:
                op = ops[0]
                if op.size == 32:
                    L.append("wrf32(%s, fto_float(c, %s));" % (addr_expr(op), st(0)))
                elif op.size == 64:
                    L.append("wrf64(%s, %s);" % (addr_expr(op), st(0)))
                elif op.size == 80:
                    L.append("wrf80(%s, %s);" % (addr_expr(op), st(0)))
                else:
                    raise TranslateError("bad %s size" % m)
            if m == "FSTP":
                L.append("fdrop(c);")
            return L

        if m in ("FIST", "FISTP"):
            op = ops[0]
            if op.size == 16:
                L.append("wr16(%s, (uint16_t)fist_i16(c));" % addr_expr(op))
            elif op.size == 32:
                L.append("wr32(%s, (uint32_t)fist_i32(c));" % addr_expr(op))
            elif op.size == 64:
                L.append("wr64(%s, (uint64_t)fist_i64(c));" % addr_expr(op))
            else:
                raise TranslateError("bad %s size" % m)
            if m == "FISTP":
                L.append("fdrop(c);")
            return L

        # arithmetic ------------------------------------------------------
        ARITH = {"FADD": "+", "FSUB": "-", "FMUL": "*", "FDIV": "/",
                 "FSUBR": "-", "FDIVR": "/"}

        def combine(lhs, op, rhs):
            """`lhs op rhs`, routing division through the #Z check."""
            if op == "/":
                return "fdivz(c, %s, %s)" % (lhs, rhs)
            return "%s %s %s" % (lhs, op, rhs)
        base = m[:-1] if m.endswith("P") and m[:-1] in ARITH else None
        if m in ARITH:                       # non-popping
            rev = m in ("FSUBR", "FDIVR")
            o = ARITH[m]
            if not ops:
                raise TranslateError("%s without operand" % m)
            if len(ops) == 2:
                d, s = ops[0].sti, ops[1].sti
                lhs, rhs = (st(s), st(d)) if rev else (st(d), st(s))
                L.append(setst(d, "fx87(c, %s)" % combine(lhs, o, rhs)))
            elif ops[0].kind == "st":
                # Ghidra can print both D8 (ST0 destination) and DC (STi
                # destination) as a single STi operand. Recover the direction
                # from the pinned instruction bytes; the short text alone is
                # ambiguous. Legacy prefixes do not change that direction.
                at = ins.addr
                while self.image.rd8(at) in (0x26, 0x2e, 0x36, 0x3e, 0x64,
                                              0x65, 0x66, 0x67, 0x9b):
                    at += 1
                d, s = (ops[0].sti, 0) if self.image.rd8(at) == 0xdc else (0, ops[0].sti)
                lhs, rhs = (st(s), st(d)) if rev else (st(d), st(s))
                L.append(setst(d, "fx87(c, %s)" % combine(lhs, o, rhs)))
            else:
                v = (self.x87_mem_value(ops[0]))
                L.append("double v_ = %s;" % v)
                lhs, rhs = ("v_", st(0)) if rev else (st(0), "v_")
                L.append(setst(0, "fx87(c, %s)" % combine(lhs, o, rhs)))
            return L

        if base:                             # FADDP/FSUBP/FMULP/FDIVP/...
            rev = base in ("FSUBR", "FDIVR")
            o = ARITH[base]
            d = ops[0].sti if ops else 1
            lhs, rhs = (st(0), st(d)) if rev else (st(d), st(0))
            L.append(setst(d, "fx87(c, %s)" % combine(lhs, o, rhs)))
            L.append("fdrop(c);")
            return L

        IARITH = {"FIADD": "+", "FISUB": "-", "FIMUL": "*", "FIDIV": "/",
                  "FISUBR": "-", "FIDIVR": "/"}
        if m in IARITH:
            rev = m in ("FISUBR", "FIDIVR")
            o = IARITH[m]
            L.append("double v_ = %s;" % self.x87_int_value(ops[0]))
            lhs, rhs = ("v_", st(0)) if rev else (st(0), "v_")
            L.append(setst(0, "fx87(c, %s)" % combine(lhs, o, rhs)))
            return L

        # comparison ------------------------------------------------------
        if m in ("FCOM", "FCOMP", "FUCOM", "FUCOMP"):
            # ops[-1] is the source: a two-operand form is `FCOM ST0,STi`, so
            # taking ops[0] would compare ST(0) with itself.
            if not ops:
                other = st(1)
            elif ops[-1].kind == "st":
                other = st(ops[-1].sti)
            else:
                other = self.x87_mem_value(ops[-1])
            # FUCOM raises the invalid exception only for a signalling NaN.
            L.append("%s(c, %s, %s);" % ("fucom" if m.startswith("FU") else "fcom",
                                         st(0), other))
            if m.endswith("P"):
                L.append("fdrop(c);")
            return L
        if m in ("FCOMPP", "FUCOMPP"):
            L.append("%s(c, %s, %s);" % ("fucom" if m.startswith("FU") else "fcom",
                                         st(0), st(1)))
            L.append("fdrop(c); fdrop(c);")
            return L
        if m in ("FICOM", "FICOMP"):
            L.append("fcom(c, %s, %s);" % (st(0), self.x87_int_value(ops[0])))
            if m == "FICOMP":
                L.append("fdrop(c);")
            return L
        if m in FCMOVCC:
            # FCMOVcc ST0,STi: copy STi into ST0 when the EFLAGS condition holds.
            L.append("if (%s) { %s }" % (FCMOVCC[m][0], setst(0, st(ops[-1].sti))))
            return L
        if m in ("FCOMI", "FCOMIP", "FUCOMI", "FUCOMIP"):
            other = st(ops[-1].sti) if ops else st(1)
            L.append("%s(c, %s, %s);" % ("fucomi" if m.startswith("FU") else "fcomi",
                                         st(0), other))
            if m.endswith("IP"):
                L.append("fdrop(c);")
            return L
        if m == "FTST":
            return ["fcom(c, %s, 0.0);" % st(0)]
        if m == "FXAM":
            return ["fxam(c);"]

        # transcendental / misc --------------------------------------------
        # Per the SDM, precision control affects only FADD/FSUB/FMUL/FDIV
        # (with their integer and popping forms) and FSQRT.  FRNDINT and the
        # transcendentals must keep the register's full precision, otherwise
        # PC=00 turns an exact 16777217 into 16777216.
        UNARY = {"FABS": "fabs(%s)", "FCHS": "-(%s)",
                 "FSQRT": "fx87(c, sqrt(%s))",
                 "FSIN": "fx87_exact(c, sin(%s))",
                 "FCOS": "fx87_exact(c, cos(%s))",
                 "FRNDINT": "fx87_exact(c, fround_cw(c, %s))"}
        if m in UNARY:
            return [setst(0, UNARY[m] % st(0))]
        if m == "FPTAN":
            L.append("double v_ = %s;" % st(0))
            L.append(setst(0, "fx87_exact(c, tan(v_))"))
            L.append("fpush(c, 1.0);")
            return L
        if m == "FSINCOS":
            L.append("double v_ = %s;" % st(0))
            L.append(setst(0, "fx87_exact(c, sin(v_))"))
            L.append("fpush(c, fx87_exact(c, cos(v_)));")
            return L
        if m == "FSCALE":
            return [setst(0, "fx87_exact(c, fscale(%s, %s))" % (st(0), st(1)))]
        if m == "FPATAN":
            L.append(setst(1, "fx87_exact(c, atan2(%s, %s))" % (st(1), st(0))))
            L.append("fdrop(c);")
            return L
        if m == "FYL2X":
            L.append(setst(1, "fx87_exact(c, %s * log2(%s))" % (st(1), st(0))))
            L.append("fdrop(c);")
            return L
        if m == "FYL2XP1":
            # log2(x+1) loses every significant bit for small x; log1p keeps
            # them.  With ST(0) = 2^-54 and ST(1) = 1 the naive form returns 0.
            L.append(setst(1, "fx87_exact(c, %s * log1p(%s) / M_LN2)"
                           % (st(1), st(0))))
            L.append("fdrop(c);")
            return L
        if m == "F2XM1":
            # Likewise 2^x - 1: expm1(x * ln 2) is the same value without the
            # cancellation.
            return [setst(0, "fx87_exact(c, expm1(%s * M_LN2))" % st(0))]
        if m in ("FPREM", "FPREM1"):
            return [setst(0, "fprem_common(c, %s, %s, %d)"
                          % (st(0), st(1), 1 if m == "FPREM1" else 0))]
        if m == "FXTRACT":
            L.append("double v_ = %s;" % st(0))
            L.append(setst(0, "fxtract_exponent(v_)"))
            L.append("fpush(c, fxtract_significand(v_));")
            return L
        if m == "FDECSTP":
            # Both rotate TOP and clear C1.
            return ["c->fpu_top = (c->fpu_top - 1u) & 7u;",
                    "c->fpu_sw &= (uint16_t)~0x0200u;"]
        if m == "FINCSTP":
            return ["c->fpu_top = (c->fpu_top + 1u) & 7u;",
                    "c->fpu_sw &= (uint16_t)~0x0200u;"]
        if m == "FNOP":
            return [";"]
        if m == "FXCH":
            # Capstone renders D9 C9 with the implicit ST(0) present, as
            # `FXCH ST0,ST1`, where the listing writes `FXCH ST1`.  Taking
            # ops[0] there selects ST(0) and swaps the register with itself.
            return ["fxch(c, %d);" % (ops[-1].sti if ops else 1)]
        if m in ("FNSTSW", "FSTSW"):
            if ops and ops[0].kind == "reg":
                return ["c->r[0] = (c->r[0] & 0xffff0000u) | fstsw(c);"]
            return ["wr16(%s, fstsw(c));" % addr_expr(ops[0])]
        if m in ("FSTCW", "FNSTCW"):
            return ["wr16(%s, c->fpu_cw);" % addr_expr(ops[0])]
        if m == "FLDCW":
            return ["x87_set_cw(c, rd16(%s));" % addr_expr(ops[0])]
        if m == "FFREE":
            return [";"]
        if m in ("FSAVE", "FNSAVE"):
            return ["x87_fnsave(c, %s);" % addr_expr(ops[0])]
        if m == "FRSTOR":
            return ["x87_frstor(c, %s);" % addr_expr(ops[0])]
        if m in ("FINIT", "FNINIT"):
            return ["x87_finit(c);"]
        if m in ("FNSTENV", "FSTENV"):
            return ["x87_fnstenv(c, %s);" % addr_expr(ops[0])]
        if m == "FLDENV":
            return ["x87_fldenv(c, %s);" % addr_expr(ops[0])]

        raise TranslateError("unhandled x87 mnemonic %s" % m)


def function_ir(tr, lifter, fn):
    """Lift `fn`'s listed instructions from the image bytes with the
    translator's successors."""
    image = tr.image
    if not hasattr(fn, "index"):
        tr.prepare(fn)
    insns = []
    for i, ins in enumerate(fn.insns):
        end = fn.fallthrough[i] if fn.fallthrough[i] is not None else image.insn_end(
            ins.addr, ins.mnem)
        if end is None or end <= ins.addr:
            raise LiftError("%08x: instruction length unknown" % ins.addr)
        lo = ins.addr - image.base
        insns.append(lifter.lift(ins.addr, bytes(image.data[lo:lo + end - ins.addr]),
                                 mnem=ins.mnem))
    succ = [tr.successors(fn, i) for i in range(len(fn.insns))]
    noreturn = {}
    for i, ins in enumerate(fn.insns):
        if tr.never_returns(ins):
            noreturn[i] = insns[i].addr + insns[i].length
            succ[i] = []
    return FunctionIR(fn.addr, insns, succ, table_jumps(tr, fn), external_exits(tr, fn),
                      noreturn=noreturn, seh=tr.seh_effects(fn))


def external_exits(tr, fn):
    """Transfers that leave the body, by instruction index.

    Mirrors `goto_target` and `emit_indirect_jump`: direct jumps, conditional
    jumps and decoded table cases outside the body, and a last instruction
    that is not a terminator falling out of the span. A computed jump without
    a decoded table may leave for any address and maps to an empty tuple.
    """
    exits = {}
    for i, ins in enumerate(fn.insns):
        m = ins.mnem
        if (m == "INT3" or tr.push_ret_target(fn, i) is not None or tr.never_returns(ins)
                or ins.addr in getattr(fn, "dead_addrs", ())):
            continue
        out = []
        if m == "JMP":
            t = tr.branch_target(ins)
            if t is not None:
                out.append(t)
            elif i not in getattr(fn, "return_jumps", ()):
                table = tr.jumptables.get((fn.addr, ins.addr))
                if not table:
                    exits[i] = ()
                    continue
                out.extend(table)
        elif m.startswith("J"):
            out.append(tr.branch_target(ins))
        if m not in ("RET", "JMP") and not (i + 1 < len(fn.insns) and fn.contiguous[i]):
            out.append(fn.fallthrough[i] or fn.end)
        out = sorted({t for t in out if t is not None and t not in fn.index})
        if out:
            exits[i] = tuple(out)
    return exits


def table_jumps(tr, fn):
    """Indices of table jumps the decoded emitter lowers to a pure in-body switch.

    Mirrors `emit_indirect_jump`: a JMP that is not a proven return jump. Without
    a decoded table every instruction of the body is a case. Cases outside this
    body are `external_exits`.
    """
    result = set()
    return_jumps = getattr(fn, "return_jumps", ())
    for i, ins in enumerate(fn.insns):
        if ins.mnem != "JMP" or not ins.ops or ins.ops[0].startswith("0x") or i in return_jumps:
            continue
        result.add(i)
    return result
