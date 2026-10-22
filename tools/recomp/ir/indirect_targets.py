import re

from program import TranslateError

FORMAT = "recomp-indirect-targets-v1"
HEADER = "site\tkind\ttargets"
KINDS = {"call": 2, "jump": 4}
_HEX = re.compile(r"[0-9a-f]{8}")


def instruction_kind(image, site):
    opcode, modrm = image.bytes_at(site, 2)
    return next((kind for kind, field in KINDS.items()
                 if opcode == 0xFF and (modrm >> 3) & 7 == field), None)


def load(settings, program):
    if getattr(settings, "analysis_targets", None) is not None:
        facts = {}
        owners = program.owners()
        for site, (kind, targets) in sorted(settings.analysis_targets.items()):
            if site not in owners:
                raise TranslateError("%s: site %08x is not an instruction start in the code map"
                                     % (settings.analysis_facts, site))
            if instruction_kind(program.pe, site) != kind:
                raise TranslateError("%s: site %08x is not an indirect %s"
                                     % (settings.analysis_facts, site, kind))
            for target in targets:
                if target not in program.functions:
                    raise TranslateError("%s: site %08x target %08x is not a function entry"
                                         % (settings.analysis_facts, site, target))
            facts[site] = (kind, tuple(targets))
        return facts
    if settings.indirect_targets is None:
        return {}
    path = settings.indirect_targets
    rows = path.read_text().splitlines()
    head = rows[0].split("\t") if rows else []
    if len(head) != 2 or head[0] != FORMAT or head[1] != settings.sha256.lower():
        raise TranslateError("%s: first line must be '%s' and the executable SHA-256" % (path, FORMAT))
    if len(rows) < 2 or rows[1] != HEADER:
        raise TranslateError("%s: second line must be '%s'" % (path, HEADER.replace("\t", " ")))
    owners = program.owners()
    facts = {}
    for row in rows[2:]:
        fields = row.split("\t")
        if len(fields) != 3:
            raise TranslateError("%s: row must have three tab-separated fields: %r" % (path, row))
        site, kind, targets = fields
        if not _HEX.fullmatch(site):
            raise TranslateError("%s: invalid site address %r" % (path, site))
        addr = int(site, 16)
        if addr in facts:
            raise TranslateError("%s: site %08x repeats" % (path, addr))
        if kind not in KINDS:
            raise TranslateError("%s: site %08x has unknown kind %r" % (path, addr, kind))
        if addr not in owners:
            raise TranslateError("%s: site %08x is not an instruction start in the code map" % (path, addr))
        if instruction_kind(program.pe, addr) != kind:
            raise TranslateError("%s: site %08x is not an indirect %s" % (path, addr, kind))
        parsed = []
        for target in targets.split(","):
            if not _HEX.fullmatch(target):
                raise TranslateError("%s: site %08x has invalid target %r" % (path, addr, target))
            value = int(target, 16)
            if value not in program.functions:
                raise TranslateError("%s: site %08x target %08x is not a function entry" % (path, addr, value))
            parsed.append(value)
        facts[addr] = (kind, tuple(sorted(set(parsed))))
    return facts
