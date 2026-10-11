from dataclasses import dataclass

from . import x87
from .ssa import SSAError
from .x87_scalar import X87Scalar, PART_TYPES, UNSAFE, carry_var, ordered_parts
from .x87_carry import assert_covers

CW_CLONE_MIN_OPS = 4
CW_CLONE_DENSITY = 8
CW_CLONE_MAX_GUARDS = 1


@dataclass
class X87Plan:
    strict: bool
    convention: bool
    defer_ie: bool
    known_mode: bool
    clones: tuple
    binary32: set
    indices: dict
    predecessors: dict
    carry_mode: bool
    entry_shape: dict | None
    declared: set
    linear_prev: dict
    activations: tuple = ()

    def scalar(self):
        scalar = X87Scalar(observe_loads=self.strict, convention=self.convention,
                           lazy_nan=self.defer_ie)
        scalar.known_mode = self.known_mode
        scalar.mode_hook_guard = True
        return scalar

    def declarations(self):
        return ["%s %s;" % (PART_TYPES[part], carry_var(position, part))
                for position, part in sorted(self.declared)]

    def seed_block(self, scalar, shape, lines):
        if shape is None or shape is UNSAFE or not shape.active:
            scalar.reset()
            return
        exprs = {}
        for position, slot in shape.slots:
            agreements = dict(slot.const)
            for part in ordered_parts(slot.parts):
                if part not in agreements:
                    exprs[(position, part)] = scalar._temp(
                        carry_var(position, part), lines, PART_TYPES[part])
        scalar.seed(shape, exprs)

    def transition(self, scalar, source, targets, lines):
        active = []
        fallback = False
        # Deferred NaNs never cross a non-linear internal edge: fold IE and
        # canonicalise the carried value so the planned shape is unchanged.
        scalar.fold_pending(lines)
        live = scalar.snapshot()
        for target in targets:
            shape = self.entry_shape.get(target)
            if shape is None or shape is UNSAFE:
                fallback = True
            elif shape.active:
                active.append((target, shape))
            else:
                assert_covers(shape, live, "B%d->B%d" % (source, target))
        if active:
            scalar.normalize(lines)
            # Check every target against the edge state before any
            # materialization read: reading a slot another predecessor
            # dirtied raises the tracker's `high` mark, which is bookkeeping
            # for this edge, not state the other target inherits.
            live = scalar.snapshot()
            for target, shape in active:
                assert_covers(shape, live, "B%d->B%d" % (source, target))
            # Both CBRANCH targets read the same tracker, so one canonical
            # assignment per (position, part) serves every carried target.
            assignments = {}
            for target, shape in active:
                for position, slot in shape.slots:
                    agreements = dict(slot.const)
                    for part in ordered_parts(slot.parts):
                        name = carry_var(position, part)
                        if part not in agreements and name not in assignments:
                            assignments[name] = scalar._read(position, part, lines)
            for name, expr in assignments.items():
                lines.append("%s = %s;" % (name, expr))
        if fallback:
            lines.extend(scalar.flush())

    def replay(self, s, fir, mode_sites):
        scalar = self.scalar()
        counts = []
        for clone in self.clones:
            scalar.reset()
            scalar.clone, scalar.activations = clone, 0
            previous = None
            for i, b in s.blocks.items():
                scalar.proven_cw = mode_sites.get(b.insn.addr)
                if self.carry_mode:
                    if i not in self.linear_prev:
                        self.seed_block(scalar, self.entry_shape.get(i), [])
                elif (previous is None or set(fir.succ[previous]) != {i}
                      or self.predecessors[i] != {previous}):
                    scalar.reset()
                for v in b.ops:
                    if v.opc in ("X87_REG", "X87_MEM"):
                        scalar.binary32 = v.id in self.binary32
                        scalar.statements(v.data, "planning_address", "planning_result")
                    elif v.opc in ("CALL", "CALLIND", "STRINGOP", "DIV32", "IDIV32"):
                        scalar.flush()
                        scalar.reset()
                    elif v.opc in ("LOAD", "STORE"):
                        if scalar.observe_loads:
                            scalar.flush()
                    elif v.opc in ("RETURN", "TAIL"):
                        scalar.flush()
                    elif v.opc in ("BRANCH", "CBRANCH", "BRANCHIND"):
                        if self.carry_mode:
                            if v.opc == "BRANCHIND":
                                targets = sorted(set(fir.succ[i]))
                            else:
                                targets = [self.indices[v.args[0].data]]
                                if v.opc == "CBRANCH":
                                    targets.append(self.indices[b.insn.addr + b.insn.length])
                            self.transition(scalar, i, targets, [])
                            if v.opc == "BRANCHIND":
                                scalar.flush()
                        else:
                            scalar.flush()
                if not any(v.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP")
                           for v in b.ops):
                    target = fir.succ[i][0]
                    if self.carry_mode:
                        if self.linear_prev.get(target) != i:
                            self.transition(scalar, i, [target], [])
                    elif self.predecessors[target] != {i} or target != i + 1:
                        scalar.flush()
                previous = i
            counts.append(scalar.activations)
        if len(set(counts)) > 1:
            raise SSAError("x87 specializations activate at different points: %r" % counts)
        return tuple(counts)


def prepare(s, fir, live, *, strict, convention, defer_ie, known_mode, cw_clone, mode_sites):
    scalar = X87Scalar(observe_loads=strict, convention=convention, lazy_nan=defer_ie)
    # Fast CW clone: worth its size only when enough operations read PC/RC.
    cw_sensitive = 0
    if cw_clone and not scalar.observe_loads:
        for v in s.values:
            if v.opc not in ("X87_REG", "X87_MEM") or v.id not in live:
                continue
            m = v.data["mnem"]
            if (m.rstrip("P") in x87.ARITH or m.rstrip("P") in x87.INTEGER_ARITH
                    or m in ("FSQRT", "FRNDINT", "FIST", "FISTP")
                    or (v.opc == "X87_MEM" and m in ("FST", "FSTP"))):
                cw_sensitive += 1
    # The clone duplicates the integer code too; a mostly-integer body
    # (ProjectionMeshBuilder::Build: 25 x87 of ~300 instructions) doubled in
    # size for a 3% corpus gain, so also require a minimum x87 density.
    proven_mode = all(mode_sites.get(b.insn.addr) == (0x0f00, 0)
                      for b in s.blocks.values() for v in b.ops
                      if v.opc in ("X87_REG", "X87_MEM") and v.id in live)
    clones = (("fast",) if known_mode else ("fast", "general")
              if not proven_mode and cw_sensitive >= CW_CLONE_MIN_OPS
              and cw_sensitive * CW_CLONE_DENSITY >= len(fir.insns)
              else (None,))
    indices = {b.insn.addr: i for i, b in s.blocks.items()}
    predecessors = {i: set() for i in s.blocks}
    for e in [s.entry] + s.alternates:
        predecessors[e].add(-1)
    for i in s.blocks:
        for j in set(fir.succ[i]):
            predecessors[j].add(i)
    scalar_binary32 = set()
    if not scalar.observe_loads:
        # A one-operation read/modify/store run does not amortize the precision
        # selector and its extra live representations. Keep its double recipe.
        # Longer linear arithmetic runs retain the proven binary32 path. This
        # is a codegen-cost heuristic, never a relaxation of operand provenance.
        arithmetic = []

        def finish_scalar_run():
            if len(arithmetic) >= 2:
                scalar_binary32.update(arithmetic)
            arithmetic.clear()

        prev = None
        for i, b in s.blocks.items():
            if prev is not None and (set(fir.succ[prev]) != {i} or predecessors[i] != {prev}):
                finish_scalar_run()
            for v in b.ops:
                if v.opc in ("X87_REG", "X87_MEM"):
                    m = v.data["mnem"]
                    if m.rstrip("P") in ("FADD", "FSUB", "FSUBR", "FMUL"):
                        arithmetic.append(v.id)
                    if m in ("FXAM", "FIST", "FISTP", "FLDCW", "FNINIT", "FINIT") or (
                            v.opc == "X87_MEM" and m in ("FST", "FSTP", "FNSTSW", "FNSTCW")):
                        finish_scalar_run()
                elif v.opc in ("STORE", "DIV32", "IDIV32", "CALL", "CALLIND", "STRINGOP",
                               "RETURN", "TAIL", "TRAP", "BRANCH", "CBRANCH", "BRANCHIND"):
                    finish_scalar_run()
            prev = i
        finish_scalar_run()
    # Exact-flush functions (msvc_convention=False or FINCSTP/FDECSTP) keep the
    # per-edge flush. Carry only under the MSVC convention, where popped residue
    # may be relaxed; x87_scalar.snapshot() carries all popped parts otherwise.
    carry_mode = not scalar.observe_loads and scalar.convention
    entry_shape, declared, linear_prev = None, set(), {}
    if carry_mode:
        from .x87_carry import analyze as analyze_carry

        def carry_factory():
            return X87Scalar(observe_loads=False, convention=convention,
                             lazy_nan=defer_ie)

        # The fixed point is needed because a loop header's shape depends on
        # its own backedge; see x87_carry.py.
        entry_shape, _exit_shape = analyze_carry(s, fir, carry_factory, scalar_binary32)
        declared = set()
        for i, b in s.blocks.items():
            shape = entry_shape.get(i)
            if shape is None or shape is UNSAFE or not shape.active:
                continue
            for position, slot in shape.slots:
                for part in ordered_parts(slot.parts):
                    if part not in dict(slot.const):
                        declared.add((position, part))
        # Only non-linear edges cut the tracker. A plain fallthrough from the
        # previous instruction keeps the live scalar locals and needs neither
        # canonical assignment nor entry reload.
        linear_prev = {}
        for i in s.blocks:
            preds = predecessors[i]
            if len(preds) != 1:
                continue
            prev = next(iter(preds))
            if prev == -1:
                continue
            shape = entry_shape.get(i)
            if shape is None or shape is UNSAFE:
                # The successor must reset, so this edge is a real cut and
                # must publish before it even though it looks like a
                # fallthrough.
                continue
            if (i == prev + 1 and set(fir.succ[prev]) == {i}
                    and not any(op.opc in ("BRANCH", "CBRANCH", "RETURN", "BRANCHIND", "TAIL", "TRAP")
                                for op in s.blocks[prev].ops)):
                linear_prev[i] = prev

    plan = X87Plan(strict, convention, defer_ie, known_mode, clones, scalar_binary32,
                   indices, predecessors, carry_mode, entry_shape, declared, linear_prev)
    plan.activations = plan.replay(s, fir, mode_sites)
    if not known_mode and len(clones) > 1 and plan.activations[0] > CW_CLONE_MAX_GUARDS:
        plan.clones = (None,)
        plan.activations = plan.replay(s, fir, mode_sites)
    return plan
