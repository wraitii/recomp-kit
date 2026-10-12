"""Backend-neutral SSA preparation, finalization and semantic plans."""
from dataclasses import dataclass

from .lift import Lifter
from .ssa import SSAError, MEMORY, build
from .cfg import FunctionIR
from .guest import X86_32
from .simplify import canonicalize, simplify, state_roots, EFFECTS, EXITS
from .publication import analyze as publication_analysis
from . import flag_region, operations
from . import transfer_plan, x87_plan
from .call_contracts import GPRS as CONTRACT_GPRS, ARITH_FLAGS as CONTRACT_FLAGS
from .contract_variants import (output_mask as contract_output_mask, PrivateLayout,
                                ProvisionalBindings, FinalBindings)
from .body import (BodyRequest, BodyVariant, EntryForm, ReturnForm, EmissionPolicy,
                   FrozenMap, PreparedOptions)
from . import x87_mode
from .x87_facts import InstructionFact
from .invocation import (EntryBinding, binding_invocation,
                         private_invocation, tail_invocation)
from . import call_values, lowering
from .normalize import codegen_ir, CC_DEFINES
from .x87_cw import CW_FAST, CW_PC_RC, ModeCondition, condition_of, proven_fact, Dynamic, Guarded


# Arithmetic flags tracked by the calling-convention census.
FLAG_FIELDS = frozenset(X86_32.names("arithmetic_flag"))

# Call-contract fields.
CONTRACT_FIELDS = frozenset(CONTRACT_GPRS) | frozenset(CONTRACT_FLAGS)
#: The guest frame pointer and stack pointer are call-helper state even when a
#: contract says the callee overwrites them; never drop their publication.
CONTRACT_NEVER_SKIP = frozenset(X86_32.names("stack") + X86_32.names("frame"))


CC_BIT_VALUES = {"cf": 1, "pf": 2, "af": 4, "zf": 8, "sf": 0x10, "of": 0x20}
#: Seams whose observer may read the guest's fields directly, so flags stay eager.
CC_EAGER_EVENTS = operations.FLAG_EAGER_CODEGEN


def normalize_bindings(mapping, label, allow_checked=True):
    """Validate a target-to-binding map, preserving literal custom symbols."""
    try:
        items = dict(mapping or {}).items()
    except (TypeError, ValueError):
        raise SSAError("invalid %s target map" % label)
    result = {}
    for target, binding in items:
        if type(target) is not int or not 0 <= target <= 0xffffffff:
            raise SSAError("invalid %s target %r" % (label, target))
        if target in result:
            raise SSAError("duplicate %s target %08x" % (label, target))
        if isinstance(binding, EntryBinding):
            if binding.target != target:
                raise SSAError("binding target %08x does not match %s target %08x"
                               % (binding.target, label, target))
            if binding.checked and not allow_checked:
                raise SSAError("%s target %08x cannot bind a checked entry" % (label, target))
        elif not (isinstance(binding, str) and binding.isidentifier() and binding.isascii()):
            raise SSAError("invalid %s symbol %r for target %08x" % (label, binding, target))
        result[target] = binding
    return result



#: Codegen ops whose seams materialise flags eagerly before or after running.
#: They end the region as observers, so the settle is kept.
_SSA_EAGER_OPS = operations.FLAG_EAGER_OBSERVERS


def call_cc_action(summary, live):
    """The post-call action for a region summary and the flags read from the fields.

    A descriptor whose `cc_mask` misses every needed flag leaves those fields
    current: with no write in the region it stays pending, and once every
    flag it defines is overwritten before the next boundary it is dropped.
    """
    if summary is None:
        return flag_region.SETTLE
    reads, writes, must = summary
    need = reads | live if not writes else reads | live | (flag_region.ALL_FLAGS - must)
    if not need:
        return flag_region.REMOVE if not writes else flag_region.DROP
    if need == flag_region.ALL_FLAGS:
        return flag_region.SETTLE
    return ("x86_cc_settle_mask" if not writes else "x86_cc_settle_or_drop", need)


def ssa_settle_plan(cgi, flag_off_name):
    """Per-site lazy-flag decisions for one SSA body.

    Returns ``(entry, calls)``: the decision at the body entry and a mapping
    from each call instruction index to the post-call region summary
    (`flag_region.summarize`), which `call_cc_action` turns into a decision.
    The classification is over the codegen ops, so it matches what the
    emitter actually materialises: an eager seam is an observer, a call is a
    normal region boundary, and unknown forms settle.
    """
    n = len(cgi.insns)

    def access(i):
        ins = cgi.insns[i]
        if ins.mnem.upper() in ("CALL", "RET"):
            return (frozenset(), frozenset())
        if any(op.opc in _SSA_EAGER_OPS for op in ins.ops):
            return None
        reads, writes = set(), set()
        for op in ins.ops:
            if op.out is not None and op.out[0] == "register":
                for k in range(op.out[2]):
                    name = flag_off_name.get(op.out[1] + k)
                    if name:
                        writes.add(name)
            for v in op.ins:
                if v is not None and v[0] == "register":
                    for k in range(v[2]):
                        name = flag_off_name.get(v[1] + k)
                        if name:
                            reads.add(name)
        return (frozenset(reads), frozenset(writes))

    def successors(i):
        # Out-of-body targets stay in the list: the analysis treats them as
        # observers rather than silently ending the path.
        return list(cgi.succ[i])

    def is_end(i):
        return cgi.insns[i].mnem.upper() in ("CALL", "RET")

    def summarize(start):
        return flag_region.summarize(start, successors, access, is_end,
                                     lambda i: False, n)

    indices = {ins.addr: k for k, ins in enumerate(cgi.insns)}
    start = indices.get(cgi.addr)
    entry = flag_region.SETTLE
    if start is not None:
        found = summarize(start)
        entry = flag_region.SETTLE if found is None else flag_region.decision(*found)
    calls = {}
    for i, ins in enumerate(cgi.insns):
        if ins.mnem.upper() != "CALL":
            continue
        after = successors(i)
        calls[i] = summarize(after[0]) if after else None
    return entry, calls


def live_flag_reload_calls(s, live, flag_off_name):
    """Block index -> flags whose post-call reload is live.

    The SSA builder replaces every tracked flag with a fresh ``CALL_RELOAD``
    of the callee's fields after a call.  ``simplify`` keeps only the live,
    un-forwarded reloads, and the emitter lowers each surviving one to a
    direct read of the flag fields right after the site's settle.  A reload
    can be live because a guest instruction later reads it, because a RET
    publishes it, or because a following call's publication snapshot (a
    callee that reads or preserves flags) roots it.  Only the call that owns
    such a reload needs its flags materialised; its neighbours keep their
    region decision.
    """
    calls = {}
    for index, block in s.blocks.items():
        if not any(v.opc in ("CALL", "CALLIND") for v in block.ops):
            continue
        for v in block.ops:
            if (v.opc == "CALL_RELOAD" and v.data[0] == "register" and v.data[1] in flag_off_name
                    and v.id in live and s.resolve(v) is v):
                calls.setdefault(index, set()).add(flag_off_name[v.data[1]])
    return calls


@dataclass
class PreparedFunction:
    options: PreparedOptions
    ir: FunctionIR
    ssa: object
    live: set
    fields: list
    mapping: dict
    flag_keys: set
    kept_calls: dict
    transfers: transfer_plan.TransferPlan
    cc_entry: object
    cc_calls: dict
    seh_mark: bool
    arithmetic: x87_plan.X87Plan
    report_calls: bool
    state_groups: tuple
    provisional: ProvisionalBindings | None = None
    final: FinalBindings | None = None
    transport: lowering.CPUFieldTransport | lowering.ValueTransport | None = None

    @property
    def invocations(self):
        return (self.final or self.provisional).invocations

    def _contract_calls(self):
        for b in self.ssa.blocks.values():
            for v in b.ops:
                if v.opc == "CALL" and v.data in self.options.request.contract_calls:
                    call = self.transfers.calls[v.id]
                    if call.required_outputs is not None:
                        yield b, v, call

    @property
    def requests(self):
        return {(v.data, contract_output_mask(call.required_outputs))
                for _b, v, call in self._contract_calls()}

    @property
    def bound_requests(self):
        return {(inv.target, inv.output_mask) for inv in self.invocations.values()
                if inv.form == "private"}

    def _call_invocation(self, b, v, call, layouts):
        options = self.options
        target = v.data
        expected = b.insn.addr + b.insn.length
        binding = options.call_symbols.get(target)
        if binding is None:
            raise SSAError("%08x: no C symbol bound for call target %08x"
                           % (b.insn.addr, target))
        if target in options.request.contract_calls and call.required_outputs is not None:
            mask = layouts[target].output_mask if target in layouts else contract_output_mask(call.required_outputs)
            proof = proven_fact(options.mode_sites.get(b.insn.addr), CW_FAST)
            return private_invocation(target, binding, expected, proof, mask)
        form = "checked" if target in options.request.checked_calls else "ordinary"
        return binding_invocation(target, binding, form, expected)

    def _tail_invocation(self, v):
        target = v.args[0].data
        binding = self.options.tail_symbols.get(target)
        if binding is None:
            raise SSAError("no C symbol bound for tail target %08x" % target)
        return tail_invocation(target, binding)

    def _bind(self, layouts):
        invocations = {}
        for b in self.ssa.blocks.values():
            for v in b.ops:
                if v.opc == "CALL":
                    invocations[v.id] = self._call_invocation(b, v, self.transfers.calls[v.id], layouts)
                elif v.opc == "TAIL":
                    invocations[v.id] = self._tail_invocation(v)
        return FrozenMap.of(invocations)

    def bind_provisional(self):
        self.provisional = ProvisionalBindings(self._bind({}))

    def finalize(self, layouts):
        for _b, v, call in self._contract_calls():
            if v.data not in layouts or not call.required_outputs <= set(layouts[v.data].outputs):
                raise SSAError("shared contract outputs do not cover the call continuation")
            contract = self.options.request.call_contracts.get(v.data)
            if contract is not None and not contract.reads <= set(layouts[v.data].inputs):
                raise SSAError("shared contract inputs do not cover the callee reads")
        invocations = self._bind(layouts)
        values = call_values.bind(self, invocations, layouts)
        final = FinalBindings(invocations, FrozenMap.of(layouts), values)
        if self.final is not None and self.final != final:
            raise SSAError("body already finalized with a different layout")
        boundaries = lowering.plan(self, invocations)
        transport = (lowering.select_values(self, boundaries, values)
                     if self.options.request.policy.private_value_transport
                     else lowering.select_cpu_fields(boundaries, values))
        self.final, self.transport = final, transport

    def decision_rows(self):
        if not self.report_calls:
            return []
        options = self.options
        contracts = options.request.call_contracts
        lane_field = options.lane_field
        mapping = self.mapping
        fir = self.ir

        def field_names(keys):
            return sorted({lane_field.get(key, mapping[key][0])
                           for key in keys if key in mapping})

        rows = []
        for b in self.ssa.blocks.values():
            for v in b.ops:
                if v.opc != "CALL":
                    continue
                call = self.transfers.calls[v.id]
                invocation = self.invocations[v.id]
                private = invocation.form == "private"
                reason = None if private else (
                    "caller has alternate entries or SEH" if fir.entries or fir.seh
                    else "target is not an admitted checked leaf")
                contract = contracts.get(v.data)
                values = self.transport.calls.get(v.id) if isinstance(self.transport, lowering.ValueTransport) else None
                fast_publication = field_names(call.publication.required)
                removed = []
                if values is not None:
                    removed = [field for field in fast_publication if field in values.layout.inputs
                               and field in CONTRACT_GPRS and field not in CONTRACT_NEVER_SKIP]
                    fast_publication = sorted(set(fast_publication) - set(removed))
                rows.append({
                    "caller": "%08x" % fir.addr, "site": "%08x" % b.insn.addr,
                    "target": "%08x" % v.data,
                    "specialization": invocation.form,
                    "callee_reads": sorted(contract.reads) if contract is not None else None,
                    "fast_publication": fast_publication,
                    "transport": "values" if values is not None else "cpu_fields",
                    "argument_fields": list(values.layout.inputs) if values is not None else [],
                    "result_fields": list(values.layout.outputs) if values is not None else [],
                    "cpu_input_stores_removed": removed,
                    "outputs": sorted(call.required_outputs) if private else None,
                    "entry_output_mask": "%04x" % invocation.output_mask if private else None,
                    "ordinary_fallback": field_names(call.fallback.required) if private else None,
                    "fallback_flag_mask": call.fallback.flags.mask if call.fallback.flags else None,
                    "reason": reason,
                })
        return rows


def validate_operations(s):
    supported = frozenset((
        "MEMORY", "X87_REG", "X87_MEM", "CALL", "CALL_RELOAD", "CALL_KEEP", "CALLIND",
        "STRINGOP", "LOAD", "STORE", "DIV32", "IDIV32", "RETURN", "SEH", "TRAP", "TAIL",
        "BRANCHIND", "BRANCH", "CBRANCH", "PACK", "BYTE", "COPY", "INT_ZEXT", "INT_SEXT",
        "INT_ADD", "INT_SUB", "INT_MULT", "INT_AND", "INT_OR", "INT_XOR", "INT_EQUAL",
        "INT_NOTEQUAL", "INT_LESS", "INT_LESSEQUAL", "BOOL_AND", "BOOL_OR", "BOOL_XOR",
        "INT_SLESS", "INT_SLESSEQUAL", "INT_LEFT", "INT_RIGHT", "INT_SRIGHT", "INT_CARRY",
        "INT_SCARRY", "INT_SBORROW", "BOOL_NEGATE", "INT_NEGATE", "INT_2COMP", "POPCOUNT",
        "SUBPIECE", "PIECE"))
    for b in s.blocks.values():
        for v in b.ops:
            if v.opc not in supported:
                operations.describe(v.opc)
                raise SSAError("unsupported integer operation %s" % v.opc)


def _validate_mode_sites(mode_sites, boundaries, function):
    for site, fact in mode_sites.items():
        if not isinstance(fact, InstructionFact):
            raise SSAError("%08x: control-word mode requires a typed InstructionFact record" % site)
        if fact.site != site or fact.function != function or site not in boundaries:
            raise SSAError("%08x: control-word mode fact does not belong to this body" % site)
        condition = fact.mode
        if (not isinstance(condition, ModeCondition) or condition.mask & ~CW_PC_RC
                or condition.value & ~condition.mask):
            raise SSAError("%08x: invalid control-word mode metadata" % site)


def prepare(fir, request, *, lifter=None, normalized=None):
    """Prepare variant-local SSA; immutable frontend artifacts may be supplied."""
    symbol, variant, policy = request.symbol, request.variant, request.policy
    if (variant.entry == EntryForm.MULTIPLE) != bool(fir.entries) and not variant.checked:
        raise SSAError("body variant disagrees with alternate entries")
    call_symbols, tail_symbols = request.call_symbols, request.tail_symbols
    call_contracts = request.call_contracts
    checked_return = variant.checked
    return_fields = None if request.layout is None else frozenset(request.layout.outputs)
    checked_calls, contract_calls = request.checked_calls, request.contract_calls
    x87_mode_sites, x87_entry_verify = request.mode_sites, request.entry_verify
    x87_known_mode = variant.known_mode
    x87_scalar_strict, local_state = policy.x87_scalar_strict, policy.local_state
    msvc_convention, lazy_flags = policy.msvc_convention, policy.lazy_flags
    indirect_call_symbol, x87_cw_clone = policy.indirect_call_symbol, policy.x87_cw_clone
    lifter = lifter if lifter is not None else Lifter(fir.language)
    if fir.language != lifter.language:
        raise SSAError("function language %s disagrees with lifter %s" % (fir.language, lifter.language))
    try:
        X86_32.validate(lifter)
    except ValueError as error:
        raise SSAError(str(error)) from error
    x87_mode_sites = (x87_mode_sites or {}) if not x87_scalar_strict else {}
    _validate_mode_sites(x87_mode_sites, {ins.addr for ins in fir.insns}, fir.addr)
    if x87_known_mode:
        if (return_fields is None or x87_scalar_strict or not x87_cw_clone
                or not x87_mode.mode_preserving_leaf(x87_mode.prepare(fir))):
            raise SSAError("known x87 mode requires a guarded private control-word-preserving leaf")
    report_calls = request.report_calls
    checked_calls = frozenset(checked_calls)
    contract_calls = frozenset(contract_calls)
    if not contract_calls <= checked_calls:
        raise SSAError("state contracts require checked calls")
    cgi = normalized if normalized is not None else codegen_ir(fir, lifter, tail_symbols)
    if return_fields is not None and not x87_known_mode and any(
            op.opc in ("X87_REG", "X87_MEM", "X87_OPAQUE")
            for ins in cgi.insns for op in ins.ops):
        raise SSAError("private x87 entry requires guarded D3D8 arithmetic")
    if return_fields is not None and (not checked_return or any(
            op.opc in (operations.X87_RESET_OPS | frozenset(("SEH",)))
            for ins in cgi.insns for op in ins.ops)):
        raise SSAError("private state entry requires a checked leaf without opaque observers")
    if checked_return and (fir.entries or fir.seh or fir.exits):
        raise SSAError("checked return requires a single-entry closed body without SEH")
    if not symbol.isidentifier() or not symbol.isascii():
        raise SSAError("invalid C symbol")
    # The register model and operand corrections are immutable across bodies.
    # Reuse the image's SLEIGH context instead of reparsing its specification
    # for every function.
    call_symbols = normalize_bindings(call_symbols, "direct-call")
    if not checked_calls <= call_symbols.keys():
        raise SSAError("checked calls require bound direct-call symbols")
    for target, binding in call_symbols.items():
        if isinstance(binding, EntryBinding) and binding.checked != (target in checked_calls):
            raise SSAError("direct-call binding %08x disagrees with its checked class" % target)
    if indirect_call_symbol is not None and (
            not isinstance(indirect_call_symbol, str)
            or not indirect_call_symbol.isidentifier()
            or not indirect_call_symbol.isascii()):
        raise SSAError("invalid indirect call symbol %r" % (indirect_call_symbol,))
    # DIVERGENCE(original): [ssa-x87-convention] FINCSTP/FDECSTP leave a tagged
    # register above TOP, so those functions keep the exact x87 flush.
    x87_convention = msvc_convention and not any(
        ins.mnem.upper().removeprefix("WAIT ") in ("FINCSTP", "FDECSTP") for ins in fir.insns)
    # Lazy NaN needs per-op deferral, so it is off for the exact flush (which
    # resets at edges) and strict x87.
    defer_ie = not x87_scalar_strict and msvc_convention
    # Each SLEIGH register byte must map to a known runtime field.
    fields = []
    for index, name in enumerate(X86_32.names("gpr")):
        fields.append((lifter.register(name), name))
    fields.append((lifter.register("EIP"), "EIP"))
    # FS-relative accesses (the SEH chain head, TLS reads) lift to FS_OFFSET plus
    # the displacement. It is read-only guest state: no instruction here writes it.
    fields.append((lifter.register("FS_OFFSET"), "FS_OFFSET"))
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF", "DF"):
        fields.append((lifter.register(name), name))
    mapping = {}
    for (_, off, size), field in fields:
        for n in range(size):
            mapping[("register", off + n)] = (field, n)
    flag_off_name = {}
    flag_first_key = {}
    for name in ("CF", "PF", "AF", "ZF", "SF", "OF"):
        _, off, size = lifter.register(name)
        flag_first_key[name.lower()] = ("register", off)
        for n in range(size):
            flag_off_name[off + n] = name.lower()
    flag_keys = {key for key, (field, _) in mapping.items() if field in FLAG_FIELDS}
    groups = [[("register", off + n) for n in range(size)]
              for (_, off, size), _ in fields]
    lane_field, field_lanes = {}, {}
    for name in CONTRACT_GPRS:
        _, off, size = lifter.register(name)
        for lane in range(size):
            key = ("register", off + lane)
            lane_field[key] = name
            field_lanes.setdefault(name, set()).add(key)
    for name in CONTRACT_FLAGS:
        _, off, size = lifter.register(name)
        for n in range(size):
            key = ("register", off + n)
            lane_field[key] = name
            field_lanes.setdefault(name, set()).add(key)
    kept_fields = {}
    if call_contracts and not fir.seh:
        for target, contract in call_contracts.items():
            fields_ = {field for field in CONTRACT_FIELDS
                       if field not in CONTRACT_NEVER_SKIP and field not in contract.reads
                       and field not in contract.writes}
            if not set(CONTRACT_FLAGS) <= fields_:
                fields_ -= set(CONTRACT_FLAGS)
            if fields_:
                kept_fields[target] = frozenset(
                    key for field in fields_ for key in field_lanes[field])
    tail_symbols = normalize_bindings(tail_symbols, "tail", allow_checked=False)
    seh_mark = any("orphan" in kinds for kinds in fir.seh.values())
    fir = cgi
    # Per-site lazy-flag settle decisions over the same codegen ops the
    # emitter lowers, so an eager seam or a call boundary is classified the
    # way the generated body actually behaves.
    cc_entry, cc_calls = flag_region.SETTLE, {}
    if lazy_flags:
        cc_entry, cc_calls = ssa_settle_plan(cgi, flag_off_name)
    s = build(cgi,
              register_groups=groups,
              call_targets=call_symbols, indirect_call_symbol=indirect_call_symbol,
              flag_off_name=flag_off_name, kept_calls=kept_fields)
    if any(key != MEMORY and key not in mapping for key in s.inputs):
        raise SSAError("unmapped runtime register")
    canonicalize(s)
    # DIVERGENCE(original): [ssa-state-locals] guest load/store faults
    # and store watch callbacks may observe earlier GPR/flag values
    # under the agreed performance-mode policy. Keep diagnostic
    # EIP/ESP/EBP eager; division, string helpers, calls and returns
    # still publish every required field. The strict path keeps all
    # pre-access snapshots.
    mandatory_registers = {lifter.register(name) for name in X86_32.names("mandatory")}
    mandatory_fields = {field for register, field in fields if register in mandatory_registers}
    access_fields = ({key for key, (field, _) in mapping.items() if field in mandatory_fields}
                     if local_state else None)
    # Compiler conventions do not cover every binary boundary: CRT
    # assembly helpers can return flags or consume incoming flags.
    # Keep flag publication at calls/returns until actual call
    # summaries prove which fields a boundary does not observe.
    kept_calls = {v.id: kept_fields[v.data] for b in s.blocks.values() for v in b.ops
                  if v.opc == "CALL" and v.data in kept_fields}
    transported = {key for key, field in lane_field.items() if field in CONTRACT_GPRS}
    value_calls = {v.id for b in s.blocks.values() for v in b.ops
                   if v.opc == "CALL" and v.data in contract_calls} if policy.private_value_transport else set()
    nonresident_inputs = (set(transported) if return_fields is not None and policy.private_value_transport else set())
    publication_facts = publication_analysis(s, fir.succ, groups, access_fields=access_fields,
                                             reloaded=set(mapping) - flag_keys, kept=kept_calls,
                                             nonresident_inputs=nonresident_inputs, value_calls=value_calls,
                                             transported=transported)
    transfers = transfer_plan.build(s, groups, publication_facts,
                                    call_contracts or {}, kept_calls, lane_field, CONTRACT_NEVER_SKIP,
                                    contract_calls, return_fields, flag_keys, flag_first_key,
                                    CC_DEFINES, CC_BIT_VALUES, lazy_flags, CC_EAGER_EVENTS)
    if policy.private_value_transport:
        for b in s.blocks.values():
            for v in b.ops:
                if v.id in value_calls or (v.opc == "RETURN" and return_fields is not None):
                    state = b.exit if v.opc == "RETURN" else b.snapshots[v.id]
                    keys = [key for key in state if key in lane_field]
                    transfers.roots.extend(state_roots(s, state, keys, groups))
    live = simplify(s, transfers.live_publications, canonical=False, extra_roots=transfers.roots,
                    removable=frozenset(("CALL_RELOAD", "CALL_KEEP")), groups=groups)
    reads_entry_flags = any(
        v.opc == "INPUT" and v.data in flag_keys and v.id in live and s.resolve(v) is v
        for v in s.values)
    if lazy_flags:
        # A post-call reload reads the flag fields directly, right after the
        # site's action, so the action materialises a descriptor that defines
        # one of them. A reload can be live only as a following call's
        # publication root: a callee that reads or preserves flags forces the
        # caller to publish the fields before that call, so the earlier reload
        # must not read a stale field. A call whose flag reload is dead
        # (shadowed by a later call or overwritten) keeps its region decision.
        reloads = live_flag_reload_calls(s, live, flag_off_name)
        cc_calls = {i: call_cc_action(summary, frozenset(reloads.get(i, ())))
                    for i, summary in cc_calls.items()}
    if reads_entry_flags or fir.entries:
        # The SSA builder loads live entry flag INPUTs straight from the fields,
        # immediately after the entry settle.  Those loads are not codegen ops,
        # so the region walk cannot see them: it may have classified the entry
        # region as REMOVE or DROP while a caller descriptor is still pending,
        # and the later guest read would use a stale field.  Settle first.
        cc_entry = flag_region.SETTLE
    arithmetic_plan = x87_plan.prepare(s, fir, live, strict=x87_scalar_strict,
                                       convention=x87_convention, defer_ie=defer_ie,
                                       known_mode=x87_known_mode, cw_clone=x87_cw_clone,
                                       mode_sites=x87_mode_sites)
    options = PreparedOptions(request, FrozenMap.of(call_symbols), FrozenMap.of(tail_symbols),
                              FrozenMap.of(x87_mode_sites), FrozenMap.of(lane_field))
    prepared = PreparedFunction(options, fir, s, live, fields, mapping, flag_keys,
                                kept_calls, transfers, cc_entry, cc_calls, seh_mark,
                                arithmetic_plan, report_calls,
                                tuple((name, tuple(keys)) for name, keys in zip(
                                    (*X86_32.names("gpr"), "EIP", "FS_OFFSET", "CF", "PF", "AF", "ZF", "SF", "OF", "DF"), groups)))
    prepared.bind_provisional()
    validate_operations(s)
    return prepared

