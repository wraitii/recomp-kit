"""Bounded scalar x87 lowering for the C emitter.

Only inline floating helpers and pure register instructions may share a region.
Integer memory accesses, calls, branches, alternate entries and unsupported
instructions end it. Values, tags, TOP and integer metadata are committed before
the boundary, including contents of popped slots. Arithmetic and status helpers
are copied from the ordinary emitter, not reimplemented here.

Floating arena accesses do not observe CPU state in the current runtime. Fatal
host fault diagnostics read EIP/ESP/EBP, which this pass leaves eager. The pass
is opt-in: runtimes exposing x87 at asynchronous/access boundaries must disable
it. CPU contexts must be outside the guest arena, as in the kit runtime.
"""
import re


FLOAT_OPS = frozenset((
    "FLD", "FLD1", "FLDZ", "FST", "FSTP", "FADD", "FSUB", "FSUBR", "FMUL",
    "FADDP", "FSUBP", "FSUBRP", "FMULP", "FCOM", "FCOMP", "FCOMPP",
    "FUCOM", "FUCOMP", "FUCOMPP", "FNSTSW",
))
REGISTER_OPS = frozenset((
    "MOV", "MOVZX", "MOVSX", "LEA", "NOP", "ADD", "SUB", "INC", "DEC",
    "CMP", "TEST", "AND", "OR", "XOR", "NOT", "NEG", "SHL", "SHR", "SAR",
))
ST = re.compile(r"ST\(c, (\d+)\)")
PUSH = re.compile(r"fpush\(c, (.*)\);")
SET = re.compile(r"fset\(c, (\d+), (.*)\);")
LOAD = re.compile(r"double v_ = (.*);")


def eligible(ins, parse_operand):
    """Refuse observer/helper paths before inspecting generated expressions."""
    if ins.rep or ins.mnem not in FLOAT_OPS | REGISTER_OPS:
        return False
    try:
        ops = [parse_operand(o) for o in ins.ops]
    except Exception:
        # The parser's TranslateError lives in the driver; any refusal here
        # keeps its already-emitted baseline diagnostic/body intact.
        return False
    if any(o.kind not in ("reg", "imm", "mem", "st") or
           (o.kind == "mem" and o.seg) for o in ops):
        return False
    if ins.mnem in REGISTER_OPS:
        # LEA's memory-shaped source is an address calculation, not an access.
        return all(o.kind in ("reg", "imm") for o in ops) or (
            ins.mnem == "LEA" and len(ops) == 2 and ops[0].kind == "reg" and ops[1].kind == "mem")
    if ins.mnem == "FNSTSW":
        return len(ops) == 1 and ops[0].kind == "reg" and ops[0].reg == 0 and ops[0].size == 16
    if ins.mnem == "FLD":
        return len(ops) == 1 and ops[0].kind == "mem" and ops[0].size in (32, 64)
    if ins.mnem in ("FST", "FSTP"):
        return len(ops) == 1 and ops[0].kind == "mem" and ops[0].size in (32, 64)
    return all(o.kind != "mem" or o.size in (32, 64) for o in ops)


class Region:
    """Track locally defined physical slots relative to entry TOP.

    Unknown incoming slots cause a cut instead of guessed values or metadata.
    Slot numbers wrap just as the runtime does; a pop retains the last value
    and integer bits while marking its tag empty and clearing exactness.
    """
    def __init__(self):
        self.top = 0
        self.values = {}
        self.tags = {}
        self.serial = 0
        self.lines = []
        self.indices = []
        self.writes = 0

    def read(self, match):
        slot = (self.top + int(match[1])) & 7
        if slot not in self.values:
            raise ValueError("incoming x87 value")
        return self.values[slot]

    def value(self, expr, slot):
        name = f"x87_v{self.serial}_"
        self.serial += 1
        self.values[slot] = name
        self.tags[slot] = f"ftag_classify({name})"
        self.writes += 1
        return f"double {name} = {expr};"

    def append(self, i, body, floating=False):
        """Use the baseline's expressions; roll back on an incoming operand."""
        if any("recomp_" in line or "CALL_FN(" in line for line in body):
            return False
        saved = self.top, self.values.copy(), self.tags.copy(), self.serial, self.writes
        lines = []
        source = None
        instruction_comment = ""
        # Baseline x87 instruction scopes only contain a load, arithmetic and
        # a pop. Scalar values live in the enclosing region instead; inline
        # the one-use memory operand without moving it past another access.
        if floating and body[0].startswith("{"):
            if "/*" in body[0]:
                instruction_comment = "  " + body[0][body[0].index("/*"):]
            body = [line.strip() for line in body[1:-1]]
        try:
            for line in body:
                code, separator, comment = line.partition("  /*")
                stripped = code.strip()
                suffix = separator + comment
                load = LOAD.fullmatch(stripped) if floating else None
                if load:
                    source = load[1]
                    continue
                push, assign = PUSH.fullmatch(stripped), SET.fullmatch(stripped)
                if push:
                    expr = ST.sub(self.read, push[1])
                    self.top = (self.top - 1) & 7
                    line = " " * (len(line) - len(line.lstrip())) + self.value(expr, self.top) + suffix
                elif assign:
                    expr = ST.sub(self.read, assign[2])
                    if source is not None:
                        expr = re.sub(r"\bv_\b", lambda _: f"({source})", expr)
                    slot = (self.top + int(assign[1])) & 7
                    line = " " * (len(line) - len(line.lstrip())) + self.value(expr, slot) + suffix
                elif stripped == "fdrop(c);" or stripped == "fdrop(c); fdrop(c);":
                    for _ in range(stripped.count("fdrop")):
                        if self.top not in self.values:
                            raise ValueError("incoming x87 pop")
                        self.tags[self.top] = "FTAG_EMPTY"
                        self.top = (self.top + 1) & 7
                    continue
                else:
                    line = ST.sub(self.read, line)
                    # FNSTSW observes virtual TOP, but needs no physical stack.
                    status = ("(uint16_t)((c->fpu_sw & (uint16_t)~0x3800u) | "
                              f"(((x87_top_ + {self.top}u) & 7u) << 11))")
                    line = line.replace("fstsw(c)", status)
                lines.append(line)
        except ValueError:
            self.top, self.values, self.tags, self.serial, self.writes = saved
            return False
        if lines and instruction_comment:
            lines[0] += instruction_comment
        self.lines.extend(lines)
        self.indices.append(i)
        return True

    def emit(self, entry):
        lines = [f"{{ /* {entry:08x} local x87 values; full state at region exit */",
                 "    const unsigned x87_top_ = c->fpu_top;"]
        lines += ["    " + line for line in self.lines]
        for slot, value in sorted(self.values.items()):
            phys = f"((x87_top_ + {slot}u) & 7u)"
            lines += [f"    c->st[{phys}] = {value};",
                      f"    c->st_bits[{phys}] = 0;",
                      f"    c->st_exact[{phys}] = 0;",
                      f"    ftag_put(c, {phys}, {self.tags[slot]});"]
        if self.top:
            lines.append(f"    c->fpu_top = (x87_top_ + {self.top}u) & 7u;")
        lines.append("}")
        return lines


def lower_regions(fn, bodies, labels, dead, parse_operand, protected=()):
    """Replace supported regions without moving accesses or crossing entries.

    Result maps original instruction indices to output; interior indices of an
    emitted region become empty. Label/gap handling remains in the C driver.
    Tiny regions keep baseline output to avoid extra loads and final stores.
    """
    result = dict(bodies)
    region = Region()
    count = instructions = 0

    def finish():
        nonlocal region, count, instructions
        if region.writes >= 3:
            result[region.indices[0]] = region.emit(fn.insns[region.indices[0]].addr)
            for i in region.indices[1:]:
                result[i] = []
            count += 1
            instructions += len(region.indices)
        region = Region()

    for i, ins in enumerate(fn.insns):
        if ins.addr in labels or i in dead or ins.addr in protected:
            finish()
        if i in dead:
            continue
        if ins.addr in protected or not eligible(ins, parse_operand):
            finish()
        elif not region.append(i, bodies[i], ins.mnem in FLOAT_OPS):
            finish()
            region.append(i, bodies[i], ins.mnem in FLOAT_OPS)
        if not fn.contiguous[i]:
            finish()
    finish()
    return result, count, instructions
