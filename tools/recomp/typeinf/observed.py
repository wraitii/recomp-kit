from dataclasses import dataclass, field

SHIM = "shim"
TARGET_KINDS = frozenset(("known", "module", SHIM))
CALL_REG = 2


@dataclass
class ObservedEdges:
    targets: dict = field(default_factory=dict)
    rejected: set = field(default_factory=set)

    def targets_at(self, site):
        return frozenset(self.targets.get(site, ()))


def parse_line(line):
    fields = line.split()
    if len(fields) < 4 or fields[2] not in TARGET_KINDS:
        raise ValueError("malformed observe line: %r" % line)
    return int(fields[0], 16), int(fields[1], 16), fields[2]


def is_indirect_call(pe, site):
    if not (pe.base <= site and site + 2 <= pe.end):
        return False
    opcode, modrm = pe.bytes_at(site, 2)
    return opcode == 0xFF and (modrm >> 3) & 7 == CALL_REG


def load_observed(path, program):
    edges = ObservedEdges()
    checked = {}
    with open(path) as lines:
        for line in lines:
            if not line.strip():
                continue
            site, target, kind = parse_line(line)
            if site not in checked:
                checked[site] = is_indirect_call(program.pe, site)
            if not checked[site] or kind == SHIM or not program.pe.is_exec(target):
                continue
            if target in program.functions:
                edges.targets.setdefault(site, set()).add(target)
            else:
                edges.rejected.add((site, target))
    return edges
