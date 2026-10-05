"""Bounded scalar x87 lowering for the C emitter.

Ordinary integer accesses retain their ordering inside regions. Direct branches
and joins may share scalar slots when TOP agrees on every incoming edge. Calls,
alternate entries, observers and unsupported instructions publish full state. Values, tags, TOP and integer metadata are committed before
the boundary, including contents of popped slots. Arithmetic and status helpers
are copied from the ordinary emitter, not reimplemented here.

Arena accesses do not observe x87 state in the current runtime. On an interior
fault, x87 state may still reflect the preceding published boundary. Fatal
host fault diagnostics read EIP/ESP/EBP, which this pass leaves eager. The pass
is opt-in: runtimes exposing x87 at asynchronous/access boundaries must disable
it. CPU contexts must be outside the guest arena, as in the kit runtime.

DIVERGENCE(original): [x87-local-status] control and status stay in a private
helper context within a region. Guest FNSTSW reads the current local status;
every exit publishes it. Interior fault diagnostics can see the preceding
published status, matching the pass's deferred stack-state observation policy.

DIVERGENCE(original): [x87-binary32] under PC=00, arithmetic with proven
binary32 operands uses a separate native float operation. This shares the
existing runtime's float exponent range rather than x87's wider range. Other
precision settings and unproven operands retain the double-backed helper.
Operation order, rounding points and canonical invalid status are retained.
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
PUSH_ST = re.compile(r"fpush_st\(c, (\d+)\);")
COPY = re.compile(r"fcopy\(c, (\d+), (\d+)\);")
LOAD = re.compile(r"double v_ = (.*);")
# These helpers access only CW/SW, never registers, tags or stack values. A
# separate nonescaping object lets the C compiler keep both fields in native
# registers despite guest accesses and eager integer CPU writes in the region.
ENV_HELPERS = re.compile(r"\b(fx87|fx87_exact|fcom|fucom|fto_float)\(c,")


def environment():
    return ["X86 x87_env_;",
            "x87_env_.fpu_cw = c->fpu_cw;",
            "x87_env_.fpu_sw = c->fpu_sw;"]


def eligible(ins, parse_operand):
    """Allow ordered arena accesses, refusing segments and observer helpers."""
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
        return all(o.kind in ("reg", "imm", "mem") for o in ops)
    if ins.mnem == "FNSTSW":
        return len(ops) == 1 and ops[0].kind == "reg" and ops[0].reg == 0 and ops[0].size == 16
    if ins.mnem == "FLD":
        return len(ops) == 1 and (ops[0].kind == "st" or
                                 (ops[0].kind == "mem" and ops[0].size in (32, 64)))
    if ins.mnem in ("FST", "FSTP"):
        return len(ops) == 1 and (ops[0].kind == "st" or
                                 (ops[0].kind == "mem" and ops[0].size in (32, 64)))
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
        self.single = set()

    def binary32(self, expr):
        """Prove representability on PC=00, without assuming incoming widths."""
        expr = expr.strip()
        while expr.startswith("(") and expr.endswith(")"):
            depth = 0
            for i, ch in enumerate(expr):
                depth += (ch == "(") - (ch == ")")
                if depth == 0:
                    break
            if i != len(expr) - 1:
                break
            expr = expr[1:-1].strip()
        return (expr.startswith("(double)rdf32(") or expr in ("0.0", "1.0") or
                any(expr == self.values[s] for s in self.single))

    def arithmetic(self, expr):
        """Use one binary32 operation only for two evidenced binary32 inputs.

        The alternative retains the original double operation and helper. No
        reassociation or contraction is introduced; NaNs still use the runtime
        canonicalization/status helper. The selector is invariant in a region.
        """
        prefix = "fx87(c, "
        if not expr.startswith(prefix) or not expr.endswith(")"):
            return expr, self.binary32(expr)
        body = expr[len(prefix):-1]
        depth = 0
        for i, ch in enumerate(body):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and ch in "+-*" and body[i-1:i] == " " and body[i+1:i+2] == " ":
                lhs, rhs = body[:i].strip(), body[i+1:].strip()
                if self.binary32(lhs) and self.binary32(rhs):
                    expr = ("((x87_env_.fpu_cw & 0x300u) == 0u ? "
                            f"fx87_exact(c, (double)((float)({lhs}) {ch} (float)({rhs}))) : {expr})")
                break
        # Even with wider operands, the existing PC=00 helper rounds its
        # result to float (or returns a canonical NaN) for the next instruction.
        return expr, True

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

    def copy(self, src, dst):
        """Copy a locally defined float, including its possibly empty tag.

        Straight-line locals have zero integer bits/exactness. Incoming slots
        still cut the region; never substitute fset's tag classification for
        the register move's tag copy. Capture before writing for FLD ST7.
        """
        if src not in self.values:
            raise ValueError("incoming x87 copy")
        expr, tag, single = self.values[src], self.tags[src], src in self.single
        line = self.value(expr, dst)
        self.tags[dst] = tag
        self.single.discard(dst)
        if single:
            self.single.add(dst)
        return line

    def append(self, i, body, floating=False):
        """Use the baseline's expressions; roll back on an incoming operand."""
        if any("recomp_" in line or "CALL_FN(" in line for line in body):
            return False
        saved = self.top, self.values.copy(), self.tags.copy(), self.serial, self.writes, self.single.copy()
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
                push_st, copy = PUSH_ST.fullmatch(stripped), COPY.fullmatch(stripped)
                if push_st or copy:
                    if push_st:
                        src = (self.top + int(push_st[1])) & 7
                        dst = (self.top - 1) & 7
                    else:
                        src = (self.top + int(copy[2])) & 7
                        dst = (self.top + int(copy[1])) & 7
                    line = self.copy(src, dst) + suffix
                    if push_st:
                        self.top = dst
                elif push:
                    expr = ST.sub(self.read, push[1])
                    single = self.binary32(expr)
                    self.top = (self.top - 1) & 7
                    line = " " * (len(line) - len(line.lstrip())) + self.value(expr, self.top) + suffix
                    self.single.discard(self.top)
                    if single:
                        self.single.add(self.top)
                elif assign:
                    expr = ST.sub(self.read, assign[2])
                    if source is not None:
                        expr = re.sub(r"\bv_\b", lambda _: f"({source})", expr)
                    slot = (self.top + int(assign[1])) & 7
                    expr, single = self.arithmetic(expr)
                    line = " " * (len(line) - len(line.lstrip())) + self.value(expr, slot) + suffix
                    self.single.discard(slot)
                    if single:
                        self.single.add(slot)
                elif stripped == "fdrop(c);" or stripped == "fdrop(c); fdrop(c);":
                    for _ in range(stripped.count("fdrop")):
                        if self.top not in self.values:
                            raise ValueError("incoming x87 pop")
                        lines.extend(self.drop())
                    continue
                else:
                    line = ST.sub(self.read, line)
                    # FNSTSW observes virtual TOP, but needs no physical stack.
                    status = ("(uint16_t)((c->fpu_sw & (uint16_t)~0x3800u) | "
                              f"(((x87_top_ + {self.top}u) & 7u) << 11))")
                    line = line.replace("fstsw(c)", status)
                line = ENV_HELPERS.sub(r"\1(&x87_env_,", line)
                line = line.replace("c->fpu_sw", "x87_env_.fpu_sw")
                lines.append(line)
        except ValueError:
            self.top, self.values, self.tags, self.serial, self.writes, self.single = saved
            return False
        if lines and instruction_comment:
            lines[0] += instruction_comment
        self.lines.extend(lines)
        self.indices.append(i)
        return True

    def drop(self):
        self.tags[self.top] = "FTAG_EMPTY"
        self.top = (self.top + 1) & 7
        return []

    def emit(self, entry):
        lines = [f"{{ /* {entry:08x} local x87 values; full state at region exit */",
                 "    const unsigned x87_top_ = c->fpu_top;"]
        lines += ["    " + line for line in environment()]
        lines += ["    " + line for line in self.lines]
        for slot, value in sorted(self.values.items()):
            phys = f"((x87_top_ + {slot}u) & 7u)"
            lines += [f"    c->st[{phys}] = {value};",
                      f"    c->st_bits[{phys}] = 0;",
                      f"    c->st_exact[{phys}] = 0;",
                      f"    ftag_put(c, {phys}, {self.tags[slot]});"]
        if self.top:
            lines.append(f"    c->fpu_top = (x87_top_ + {self.top}u) & 7u;")
        lines.append("    c->fpu_sw = x87_env_.fpu_sw;")
        lines.append("}")
        return lines


def lower_regions(fn, bodies, labels, dead, parse_operand, protected=(), cfg=None):
    """Replace supported regions without moving accesses or crossing entries.

    Result maps original instruction indices to output; interior indices of an
    emitted region become empty. Label/gap handling remains in the C driver.
    Tiny regions keep baseline output to avoid extra loads and final stores.
    """
    if not any(ins.mnem in FLOAT_OPS for ins in fn.insns):
        return dict(bodies), 0, 0
    result = dict(bodies)
    consumed = set()
    count = instructions = 0
    if cfg is not None:
        consumed, count = lower_branch_regions(fn, result, labels, dead, parse_operand, protected, *cfg)
        instructions = len(consumed)
    region = Region()

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
        if ins.addr in labels or i in dead or i in consumed or ins.addr in protected:
            finish()
        if i in dead or i in consumed:
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


class BranchRegion(Region):
    """Mutable scalar slots for a single-entry CFG with statically known TOP.

    Each path retains popped values and incoming integer metadata. Tags are
    classified only when publishing: marker 4 means a newly written float.
    Incoming tags (including empty) remain unchanged on untouched paths.
    """
    def __init__(self):
        super().__init__()
        self.values = {s: f"x87_s{s}_" for s in range(8)}
        self.used = set()
        self.modified = set()

    def read(self, match):
        slot = (self.top + int(match[1])) & 7
        self.used.add(slot)
        return self.values[slot]

    def copy(self, src, dst):
        # CFG locals can contain incoming exact integers or path-dependent
        # metadata. Keep copies eager until all four fields are tracked here.
        raise ValueError("CFG x87 register copy")

    def value(self, expr, slot):
        self.used.add(slot)
        self.modified.add(slot)
        self.writes += 1
        return (f"x87_s{slot}_ = {expr};\n"
                f"x87_bits{slot}_ = 0; x87_exact{slot}_ = 0; x87_tag{slot}_ = 4;")

    def drop(self):
        slot = self.top
        self.used.add(slot)
        self.modified.add(slot)
        self.top = (slot + 1) & 7
        return [f"x87_exact{slot}_ = 0; x87_tag{slot}_ = FTAG_EMPTY;"]

    def commit(self, top):
        lines = ["c->fpu_sw = x87_env_.fpu_sw;"]
        for slot in sorted(self.modified):
            phys = f"((x87_top_ + {slot}u) & 7u)"
            lines += [f"c->st[{phys}] = x87_s{slot}_;",
                      f"c->st_bits[{phys}] = x87_bits{slot}_;",
                      f"c->st_exact[{phys}] = x87_exact{slot}_;",
                      f"ftag_put(c, {phys}, x87_tag{slot}_ == 4 ? ftag_classify(x87_s{slot}_) : x87_tag{slot}_);"]
        lines.append(f"c->fpu_top = (x87_top_ + {top}u) & 7u;")
        return lines

    def declarations(self, entry):
        lines = [f"{{ /* {entry:08x} local x87 CFG; full state at exits */",
                 "const unsigned x87_top_ = c->fpu_top;"]
        lines += environment()
        for slot in sorted(self.used):
            phys = f"((x87_top_ + {slot}u) & 7u)"
            lines.append(f"double x87_s{slot}_ = c->st[{phys}];")
            if slot in self.modified:
                lines += [f"uint64_t x87_bits{slot}_ = c->st_bits[{phys}];",
                          f"uint8_t x87_exact{slot}_ = c->st_exact[{phys}];",
                          f"unsigned x87_tag{slot}_ = ftag_of(c, {phys});"]
        return lines


def lower_branch_regions(fn, bodies, labels, dead, parse_operand, protected,
                         successors, branch_target, conditional, entries):
    """Lower contiguous single-entry CFGs, refusing inconsistent TOP joins.

    External/indirect transfers, listing gaps and helper bodies are barriers.
    Internal labels move inside the region; its public entry stays before the
    initialization, while loop backedges target a separate internal label.
    Every edge leaving the region publishes full state before transferring.
    """
    n = len(fn.insns)
    succ = [successors(fn, i) for i in range(n)]
    preds = [set() for _ in range(n)]
    for i, targets in enumerate(succ):
        for j in targets:
            preds[j].add(i)
    for addr in (fn.addr, *entries, *fn.pushed_continuations):
        if addr in fn.index:
            preds[fn.index[addr]].add(-1)

    def allowed(i):
        ins = fn.insns[i]
        if i in dead or ins.addr in protected or not fn.contiguous[i]:
            return False
        if any("recomp_" in line or "CALL_FN(" in line for line in bodies[i]):
            return False
        if ins.mnem in conditional or ins.mnem == "JMP":
            return branch_target(ins) in fn.index
        if not eligible(ins, parse_operand):
            return False
        # Register transfers are currently straight-line-only. Keep their
        # previous CFG boundaries rather than admitting one and rejecting the
        # whole surrounding interval later, losing unrelated branch lowering.
        return not (ins.mnem in ("FLD", "FST", "FSTP") and any(
            parse_operand(o).kind == "st" for o in ins.ops))

    ranges = []
    start = None
    for i in range(n + 1):
        if i < n and allowed(i):
            if start is None:
                start = i
        elif start is not None:
            ranges.append((start, i))
            start = None
    # Splitting an interval can expose another external predecessor; revisit
    # both pieces until only each piece's first instruction can be entered.
    work = ranges
    ranges = []
    while work:
        a, b = work.pop()
        split = next((i for i in range(a + 1, b)
                      if any(j < a or j >= b for j in preds[i])), None)
        if split is None:
            ranges.append((a, b))
        else:
            work.extend(((a, split), (split, b)))

    consumed = set()
    count = 0
    for a, b in sorted(ranges):
        if not any(fn.insns[i].mnem in conditional or fn.insns[i].mnem == "JMP"
                   for i in range(a, b)):
            continue
        delta = {i: sum(line.count("fdrop(c)") - line.count("fpush(c,")
                        for line in bodies[i]) for i in range(a, b)}
        tops = {a: 0}
        queue = [a]
        consistent = True
        while queue and consistent:
            i = queue.pop()
            top = (tops[i] + delta[i]) & 7
            for j in succ[i]:
                if not a <= j < b:
                    continue
                if j in tops:
                    if tops[j] != top:
                        consistent = False
                        break
                else:
                    tops[j] = top
                    queue.append(j)
        if not consistent or len(tops) != b - a:
            continue
        region = BranchRegion()
        rewritten = {}
        for i in range(a, b):
            region.top = tops[i]
            region.lines = []
            # Prove widths locally within each basic block. A label or branch
            # cuts provenance, so joins/backedges never inherit another path's
            # speculative input widths. Local values themselves remain live.
            if (fn.insns[i].addr in labels or i == a or
                    fn.insns[i-1].mnem in conditional or fn.insns[i-1].mnem == "JMP"):
                region.single.clear()
            if not region.append(i, bodies[i], fn.insns[i].mnem in FLOAT_OPS):
                consistent = False
                break
            rewritten[i] = region.lines
        if not consistent or region.writes < 3:
            continue
        lines = region.declarations(fn.insns[a].addr)
        for i in range(a, b):
            addr = fn.insns[i].addr
            if addr in labels or i == a:
                lines.append(f"L_x87_{addr:08x}: ;")
            top = (tops[i] + delta[i]) & 7
            for line in rewritten[i]:
                def transfer(match):
                    target = int(match[1], 16)
                    if a <= fn.index[target] < b:
                        return f"goto L_x87_{target:08x};"
                    return "{ " + " ".join(region.commit(top)) + f" goto L_{target:08x}; }}"
                lines.append(re.sub(r"goto L_([0-9a-f]{8});", transfer, line))
            # The only implicit exit is fall-through past the last instruction.
            if i == b - 1 and fn.insns[i].mnem != "JMP":
                lines.extend(region.commit(top))
        lines.append("}")
        bodies[a] = lines
        for i in range(a + 1, b):
            bodies[i] = []
            labels.discard(fn.insns[i].addr)
        consumed.update(range(a, b))
        count += 1
    return consumed, count
