"""Call-site rows and the resolver shared by call contracts and x87 propagation."""
from collections import namedtuple

from .import_sites import resolve as resolve_imports
from .x87_cw import instruction_mnemonic

Target = namedtuple("Target", "address provenance")
CallSite = namedtuple("CallSite", "owner site kind targets import_key")
Domain = namedtuple("Domain", "lookup join override import_value boundary indirect indirect_with_direct indirect_lookup",
                   defaults=(None, None, None, None, None, None, True, None))
MISSING = object()


def direct_target(ins):
    for op in ins.ops:
        if op.opc == "CALL" and op.ins:
            space, target, _size = op.ins[0]
            return target if space == "ram" else None
    return None


def indirect_jump(ins):
    return instruction_mnemonic(ins) == "JMP" and any(op.opc == "BRANCHIND" for op in ins.ops)


def build(fir, image=None):
    """Return instruction index -> CallSite for CALL/CALLIND and tail JMP sites."""
    rows = {}
    for i, ins in enumerate(fir.insns):
        if not fir.succ[i] and indirect_jump(ins):
            rows[i] = CallSite(fir.addr, ins.addr, "tail", (), None)
    for i, targets in fir.exits.items():
        ins = fir.insns[i]
        literal = [op.ins[0][1] for op in ins.ops
                   if op.opc == "BRANCH" and op.ins[0][0] == "ram"]
        if instruction_mnemonic(ins) != "JMP" or fir.succ[i]:
            continue
        if indirect_jump(ins):
            rows[i] = CallSite(fir.addr, ins.addr, "tail", (), None)
            continue
        if len(targets) != 1 or literal != list(targets):
            continue
        rows[i] = CallSite(fir.addr, ins.addr, "tail", (Target(literal[0], "tail"),), None)
    imports = resolve_imports(fir, image) if image is not None else {}
    for i, ins in enumerate(fir.insns):
        if not (ins.mnem.upper() == "CALL"
                or any(op.opc in ("CALL", "CALLIND") for op in ins.ops)):
            continue
        key = imports.get(ins.addr)
        target = direct_target(ins)
        targets = (Target(target, "direct_call"),) if target is not None else ()
        kind = ("import" if key is not None else "direct" if targets else
                "indirect" if any(op.opc == "CALLIND" for op in ins.ops) else "unknown")
        rows[i] = CallSite(fir.addr, ins.addr, kind, targets, key)
    return rows


def resolve(row, domain, facts):
    """Resolve one row in a caller-supplied value domain."""
    for source in (domain.boundary, domain.override):
        value = source(row) if source is not None else MISSING
        if value is not MISSING:
            return value
    if domain.indirect is not None and (domain.indirect_with_direct or not row.targets):
        found = domain.indirect(row, facts)
        if found is not MISSING:
            lookup = domain.indirect_lookup or domain.lookup
            values = [lookup(target) for target in found]
            return None if None in values else domain.join(values)
    value = domain.import_value(row) if domain.import_value is not None else MISSING
    if value is not MISSING:
        return value
    return domain.lookup(row.targets[0].address) if row.targets else None
