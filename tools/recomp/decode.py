"""Capstone decoding and immutable source-byte evidence for x86 guests."""

from collections import namedtuple

from program import PE, TranslateError
from x86_frontend import STRING_MNEM, MSVC_SEH_PROLOG, MSVC_SEH_EPILOG, MSVC_EH3_FRAME, MSVC_EH3_HANDLER


DecodeOperand = namedtuple(
    "DecodeOperand",
    "kind width reg immediate base index scale displacement segment",
)
DecodeRecord = namedtuple(
    "DecodeRecord",
    "address source_bytes opcode_id opcode raw_mnemonic rep operands address_size length",
)


class Insn(object):
    __slots__ = ("addr", "mnem", "rep", "ops", "text", "raw", "decode")

    def __init__(self, addr, mnem, rep, opstrs, raw, decode=None):
        self.addr = addr
        self.mnem = mnem
        self.rep = rep
        self.ops = opstrs
        self.raw = raw
        self.decode = decode

    def __repr__(self):
        return "%08x %s %s" % (self.addr, self.mnem, ",".join(self.ops))


def operand_record(kind, width, reg=None, immediate=None, base=None,
                   index=None, scale=1, displacement=0, segment=None):
    """Build a value-only operand description with no Capstone object state."""
    return DecodeOperand(kind, width, reg, immediate, base, index, scale,
                         displacement, segment)


class Image(PE):
    """The program's bytes with a Capstone decoder over them."""

    def __init__(self, path, base=None):
        super().__init__(path, base)
        import capstone
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True
        self._decode_records = {}
        self._decode_records_by_addr = {}
        self._DecodeRecord = DecodeRecord
        self._operand_record = operand_record

    def import_thunk_name(self, va):
        if not hasattr(self, "_import_thunk_cache"):
            self._import_thunk_cache = {}
        if va not in self._import_thunk_cache:
            raw = self.bytes_at(va, 6) if self.is_exec(va) and va + 6 <= self.end else b""
            slot = int.from_bytes(raw[2:], "little") if raw[:2] == b"\xff\x25" else None
            self._import_thunk_cache[va] = self.iat_names.get(slot) if slot is not None else None
        return self._import_thunk_cache[va]

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







    def code_matches(self, va, pattern):
        """Do the executable bytes at va equal pattern, with None as one wildcard byte?"""
        if not self.is_exec(va) or not self.is_exec(va + len(pattern) - 1):
            return False
        code = self.data[va - self.base:va - self.base + len(pattern)]
        return len(code) == len(pattern) and all(p is None or p == b
                                                 for p, b in zip(pattern, code))

    def msvc_seh_prolog(self, va):
        """Return the handler pushed by MSVC's __SEH_prolog, which links the
        caller's EH3 record through EAX and returns with it published."""
        if self.code_matches(va, MSVC_SEH_PROLOG):
            handler = self.rd32(va + 1)
            if self.is_exec(handler):
                return handler
        return None

    def msvc_seh_epilog(self, va):
        return self.code_matches(va, MSVC_SEH_EPILOG)

    def msvc_unwind_only(self, handler):
        """Is handler a C++ EH thunk whose FuncInfo has no try blocks, so it never resumes its frame?"""
        if handler is None or not self.code_matches(handler, (0xb8,) + (None,) * 4 + (0xe9,) + (None,) * 4):
            return False
        info = self.rd32(handler + 1)
        return (self.rd32(info) in (0x19930520, 0x19930521, 0x19930522)
                and self.rd32(info + 12) == 0)

    def msvc_eh3_finally_only(self, va, levels):
        """Does va open an __except_handler3 frame whose scopes at `levels`, and those
        enclosing them, have no filter? The handler then only runs __finally blocks
        while unwinding and never resumes the frame."""
        if (levels is None or not self.code_matches(va, MSVC_EH3_FRAME)
                or not self.code_matches(self.rd32(va + 11), MSVC_EH3_HANDLER)):
            return False
        scope, seen, todo = self.rd32(va + 6), set(), list(levels)
        while todo:
            level = todo.pop()
            if level == 0xffffffff or level in seen:
                continue
            seen.add(level)
            if level >= 0x100 or self.rd32(scope + 12 * level + 4) != 0:
                return False
            todo.append(self.rd32(scope + 12 * level))
        return True







    #: x87 mnemonics whose memory operand is an integer or a control/status
    #: word rather than a float, so its width names itself (`word ptr`) instead
    #: of being spelled `float`/`double`/`extended double`.
    X87_INT = frozenset(("FILD", "FIST", "FISTP", "FIADD", "FISUB", "FISUBR",
                         "FIMUL", "FIDIV", "FIDIVR", "FICOM", "FICOMP",
                         "FNSTSW", "FSTSW", "FNSTCW", "FSTCW", "FLDCW",
                         "FNSTENV", "FLDENV", "FNSAVE", "FSAVE", "FRSTOR"))
    STRING_OPS = frozenset(("MOVS", "STOS", "LODS", "CMPS", "SCAS", "INS", "OUTS"))
    LEGACY_PREFIXES = frozenset((0xF0, 0xF2, 0xF3, 0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65, 0x66, 0x67))
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

    def _op_text(self, record, op):
        mnem = record.opcode
        if op.reg is not None:
            return op.reg
        if op.kind == "imm":
            return "-0x%x" % -op.immediate if op.immediate < 0 else "0x%x" % op.immediate
        if op.kind == "mem":
            word = self._size_word(op.width // 8, mnem)
            # Environment/state instructions have structured 14/28/94/108-byte
            # operands. Their mnemonic determines the layout, not a scalar width.
            structured = mnem in ("FNSTENV", "FSTENV", "FLDENV", "FNSAVE", "FSAVE", "FRSTOR")
            if word is None and not structured:
                raise TranslateError("%08x: unknown operand width %d for %s" %
                                     (record.address, op.width // 8, mnem))
            parts = []
            if op.base:
                parts.append(op.base)
            if op.index:
                parts.append("%s*0x%x" % (op.index, op.scale))
            if op.displacement or not parts:
                parts.append("-0x%x" % -op.displacement if op.displacement < 0
                             else "0x%x" % op.displacement)
            inner = " + ".join(parts)
            seg = op.segment or ""
            return "%s%s[%s]" % ("" if structured else word + " ptr ",
                                  seg + ":" if seg else "", inner)
        raise TranslateError("unknown decoded operand kind %s" % op.kind)

    def decode_record(self, ci, source_bytes=None, address=None, mnem_override=None):
        """Return immutable decode evidence, cached by address and exact bytes."""
        raw = bytes(ci.bytes if source_bytes is None else source_bytes)
        addr = ci.address if address is None else address
        instruction_bytes = bytes(ci.bytes)
        if (not raw or (addr == ci.address and raw != instruction_bytes)
                or (addr != ci.address and
                    (addr + len(raw) != ci.address + ci.size
                     or not raw.endswith(instruction_bytes)))):
            raise TranslateError("%08x: Capstone decode does not match source-byte identity" % addr)
        key = (addr, raw)
        found = self._decode_records.get(key)
        if found is not None and mnem_override is None:
            return found
        words = ci.mnemonic.split()
        rep = None
        if words[0] in self.REP_PREFIX:
            rep = self.REP_PREFIX[words[0]]
            words = words[1:]
        mnem = words[-1].upper()
        mnem = self.MNEM_ALIAS.get(mnem, mnem)
        if mnem_override is not None:
            mnem = mnem_override
        operands = []
        import capstone.x86_const as X
        for op in ci.operands:
            if op.type == X.X86_OP_REG:
                name = ci.reg_name(op.reg).upper()
                if name.startswith("ST(") and name.endswith(")"):
                    name = "ST" + name[3:-1]
                kind = ("xmm" if name.startswith("XMM") else
                        "ymm" if name.startswith("YMM") else
                        "mm" if name.startswith("MM") else
                        "st" if name.startswith("ST") else
                        "sreg" if name in ("CS", "DS", "ES", "FS", "GS", "SS") else "reg")
                item = self._operand_record(kind, op.size * 8, reg=name)
            elif op.type == X.X86_OP_IMM:
                item = self._operand_record("imm", op.size * 8, immediate=op.imm)
            elif op.type == X.X86_OP_MEM:
                mem = op.mem
                item = self._operand_record(
                    "mem", op.size * 8,
                    base=ci.reg_name(mem.base).upper() if mem.base else None,
                    index=ci.reg_name(mem.index).upper() if mem.index else None,
                    scale=mem.scale, displacement=mem.disp,
                    segment=ci.reg_name(mem.segment).upper() if mem.segment else None)
            else:
                raise TranslateError("unknown capstone operand type %d" % op.type)
            operands.append(item)
        # A MOVSD/MOVSS/CMPSD with an XMM operand is SSE, not a string
        # instruction, so its mandatory F2/F3 is not a repeat prefix.
        if (rep is None and mnem[:-1] in self.STRING_OPS
                and not any(op.kind == "xmm" for op in operands)):
            for byte in instruction_bytes:
                if byte not in self.LEGACY_PREFIXES:
                    break
                if byte in (0xF2, 0xF3):
                    rep = "REPNE" if byte == 0xF2 else "REP"
        record = self._DecodeRecord(
            addr, raw, ci.id, mnem, ci.mnemonic, rep, tuple(operands), ci.addr_size, len(raw))
        self._decode_records[key] = record
        self._decode_records_by_addr[addr] = record
        return record

    def to_insn(self, ci, source_bytes=None, address=None, mnem_override=None):
        """Build a listing Insn from a capstone instruction or cached record."""
        try:
            record = ci if isinstance(ci, self._DecodeRecord) else self.decode_record(
                ci, source_bytes=source_bytes, address=address, mnem_override=mnem_override)
            ops = [self._op_text(record, op) for op in record.operands]
        except TranslateError as error:
            if not getattr(self, "allow_unmodelled", False):
                raise
            addr = ci.address if address is None else address
            mnemonic = ci.raw_mnemonic if isinstance(ci, self._DecodeRecord) else ci.mnemonic
            operands = "" if isinstance(ci, self._DecodeRecord) else ci.op_str
            return Insn(addr, "UNMODELLED", None, [],
                        "%08x  %s %s: %s" % (addr, mnemonic.upper(), operands, error))
        mnem, rep = record.opcode, record.rep
        if rep or (mnem in STRING_MNEM
                   and not any(op.kind == "xmm" for op in record.operands)):
            ops = []            # implicit ES:EDI / ESI operands carry nothing
        text = "%08x  %s%s%s" % (record.address, mnem, "." + rep if rep else "",
                                 (" " + ",".join(ops)) if ops else "")
        return Insn(record.address, mnem, rep, ops, text, record)

    def decode_span_record(self, addr, raw):
        """Decode one mapped instruction, including Ghidra's folded WAIT form."""
        key = (addr, bytes(raw))
        record = self._decode_records.get(key)
        if record is None:
            decoded = list(self.md.disasm(raw, addr))
            if len(decoded) == 1 and decoded[0].size == len(raw):
                record = self.decode_record(decoded[0], source_bytes=raw)
            elif (len(decoded) == 2 and decoded[0].mnemonic in ("wait", "fwait")
                  and decoded[1].mnemonic.startswith("fn")
                  and sum(ci.size for ci in decoded) == len(raw)):
                ci = decoded[1]
                suffix = ci.mnemonic[2:].upper()
                mnem = self.MNEM_ALIAS.get("F" + suffix, "F" + suffix)
                record = self.decode_record(ci, source_bytes=raw, address=addr, mnem_override=mnem)
            else:
                return None
        # The old mapped-code path canonicalized XLATB for the emitter; the
        # general Capstone view keeps its original spelling.
        if record.opcode == "XLATB":
            record = record._replace(opcode="XLAT")
        if record.opcode == "XCHG" and raw and 0x90 <= raw[0] <= 0x97 and len(record.operands) == 2:
            record = record._replace(operands=tuple(reversed(record.operands)))
        return record

    def decode_span_instruction(self, addr, raw):
        record = self.decode_span_record(addr, raw)
        return self.to_insn(record) if record is not None else None


    def insn_end(self, va, mnem):
        """Address just past the instruction at `va`, or None if unknown.

        The Ghidra listing folds a WAIT prefix into the following x87
        mnemonic; capstone reports the two separately, so skip a leading
        WAIT when the listing calls the instruction something else.
        """
        if self.md is None or not (self.base <= va and va + 16 <= self.end):
            return None
        record = self._decode_records_by_addr.get(va)
        if record is not None and record.opcode == mnem:
            return va + record.length
        o = va - self.base
        got = list(self.md.disasm(self.data[o:o + 16], va, count=2))
        if not got:
            return None
        if (got[0].mnemonic in ("wait", "fwait") and mnem != "WAIT"
                and mnem.startswith("F")):
            raw = self.data[o:o + got[0].size + (got[1].size if len(got) > 1 else 0)]
            if len(got) > 1:
                self.decode_record(got[1], source_bytes=raw, address=va,
                                   mnem_override=mnem)
            return got[1].address + got[1].size if len(got) > 1 else None
        self.decode_record(got[0])
        return got[0].address + got[0].size
