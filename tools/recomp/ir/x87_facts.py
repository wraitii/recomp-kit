"""Validated compiler-facing x87 control-word facts.

The loaded result is frozen and typed: instruction modes, entry conditions and
return relations each reference one shared immutable provenance record. Only
instruction modes reach emission; entry and return facts are retained for
reporting and never enable optimization. The offline control-word graph builds
the same records through the validated constructor used by load().
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from program import TranslateError
from .x87_cw import CW_PC_RC, ModeCondition

FORMAT = "x87-compiler-facts-v1"
PRODUCER = "recomp-kit-x87-analysis-v1"
MODE_MASK = CW_PC_RC
FUNCTION_SCOPE = "all_invocations_joined"
RETURN_SCOPE = "all_mapped_entries"
CONTRACT_SCOPE = "all_invocations"
_IDENTITY_KEYS = ("executable_sha256", "producer", "producer_sha256", "analysis_inputs_sha256")
_CONTRACT_KEYS = frozenset(("function", "mask", "value", "scope", "verify", "evidence"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tree_digest(root, patterns):
    files = set(p for pattern in patterns for p in root.rglob(pattern) if p.is_file()
                and not {"build", "analysis", ".git", "__pycache__"}.intersection(p.relative_to(root).parts))
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(files)])


NON_PRODUCING = frozenset(("tests", "diff", "corpus"))
RE_RUN_MODULES = frozenset((
    "ir/lowering.py", "ir/c_boundaries.py", "ir/call_values.py",
    "ir/emit_c.py", "output.py",
))


def producer_digest(kit):
    """The analysis producer and its semantic dependencies, not the whole tree."""
    recomp = kit / "tools/recomp"
    files = set(recomp.rglob("*.py"))
    game_config = kit / "tools/game_config.py"
    if game_config.is_file():
        files.add(game_config)
    entries = []
    for path in sorted(files):
        relative = path.relative_to(recomp) if recomp in path.parents else Path(path.name)
        if NON_PRODUCING.intersection(relative.parts) or "__pycache__" in relative.parts:
            continue
        if str(relative) in RE_RUN_MODULES:
            continue
        entries.append((str(path.relative_to(kit)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return digest(entries)


def identity(settings):
    kit = Path(__file__).resolve().parents[3]
    policy = {key: value for key, value in settings.cfg["translate"].items()
              if key not in ("x87_facts", "code_map")}
    boundary = settings.x87_mode_metadata
    return {"executable_sha256": settings.sha256.lower(),
            "producer": PRODUCER,
            "producer_sha256": producer_digest(kit),
            "analysis_inputs_sha256": digest({
                "code_map": tree_digest(settings.code_map, ("*",)),
                "policy": policy,
                "entry_point": settings.cfg["game"]["entry_point"],
                "image_base": settings.base,
                "configured_entries": sorted(settings.configured_entries),
                "boundaries": hashlib.sha256(boundary.read_bytes()).hexdigest() if boundary else None,
                "native": {name: tree_digest(kit / name, ("*.c", "*.cpp", "*.h"))
                           for name in ("runtime", "dx", "mods")},
                "game_native": tree_digest(settings.game_dir, ("*.c", "*.cpp", "*.h"))})}


@dataclass(frozen=True)
class EntryContract:
    function: int
    mask: int
    value: int
    scope: str
    verify: bool | None
    evidence: str

    @property
    def condition(self):
        return ModeCondition(self.mask, self.value)

    @classmethod
    def from_row(cls, row):
        if not isinstance(row, dict) or not _CONTRACT_KEYS >= set(row):
            raise ValueError("entry contract has an unexpected shape")
        function, mask, value = row["function"], row["mask"], row["value"]
        scope, verify, evidence = row["scope"], row.get("verify"), row["evidence"]
        if (type(function) is not int or not 0 <= function <= 0xffffffff
                or type(mask) is not int or not 0 < mask <= 0xffff
                or type(value) is not int or value & ~mask
                or scope != CONTRACT_SCOPE or (verify is not None and type(verify) is not bool)
                or not isinstance(evidence, str) or not evidence):
            raise ValueError("invalid entry contract")
        return cls(function, mask, value, scope, verify, evidence)

    def to_payload(self):
        row = {"function": self.function, "mask": self.mask, "value": self.value,
               "scope": self.scope}
        if self.verify is not None:
            row["verify"] = self.verify
        row["evidence"] = self.evidence
        return row


@dataclass(frozen=True)
class Provenance:
    """Shared immutable identity and assumptions every fact references."""

    executable_sha256: str
    producer: str
    producer_sha256: str
    analysis_inputs_sha256: str
    imports_preserve_pc_rc: bool
    entry_contracts: tuple[EntryContract, ...]

    @classmethod
    def from_settings(cls, settings):
        ident = identity(settings)
        return cls(ident["executable_sha256"], ident["producer"], ident["producer_sha256"],
                   ident["analysis_inputs_sha256"], settings.x87_imports_preserve_pc_rc,
                   tuple(EntryContract.from_row(row) for row in settings.x87_entry_contracts))

    @classmethod
    def from_payload(cls, data):
        if not isinstance(data, dict) or set(_IDENTITY_KEYS) - set(data):
            raise ValueError("missing compiler-fact identity")
        assumptions = data.get("assumptions")
        if (not isinstance(assumptions, dict)
                or set(assumptions) != {"imports_preserve_pc_rc", "entry_contracts"}
                or type(assumptions["imports_preserve_pc_rc"]) is not bool
                or not isinstance(assumptions["entry_contracts"], list)):
            raise ValueError("invalid compiler-fact assumptions")
        return cls(data["executable_sha256"], data["producer"], data["producer_sha256"],
                   data["analysis_inputs_sha256"], assumptions["imports_preserve_pc_rc"],
                   tuple(EntryContract.from_row(row) for row in assumptions["entry_contracts"]))

    def identity_payload(self):
        return {"executable_sha256": self.executable_sha256, "producer": self.producer,
                "producer_sha256": self.producer_sha256,
                "analysis_inputs_sha256": self.analysis_inputs_sha256}

    def assumptions_payload(self):
        return {"imports_preserve_pc_rc": self.imports_preserve_pc_rc,
                "entry_contracts": [contract.to_payload() for contract in self.entry_contracts]}


@dataclass(frozen=True)
class InstructionFact:
    function: int
    site: int
    mode: ModeCondition
    scope: str
    provenance: Provenance


@dataclass(frozen=True)
class EntryCondition:
    function: int
    mask: int
    value: int
    scope: str
    provenance: Provenance

    @property
    def condition(self):
        return ModeCondition(self.mask, self.value)


@dataclass(frozen=True)
class FunctionFacts:
    function: int
    scope: str
    entry: EntryCondition
    instructions: tuple[InstructionFact, ...]
    provenance: Provenance


@dataclass(frozen=True)
class ReturnRelation:
    function: int
    scope: str
    entries: tuple[int, ...]
    preserved_mask: int
    known_mask: int
    known_value: int
    provenance: Provenance


@dataclass(frozen=True)
class X87Facts:
    enabled: bool
    provenance: Provenance | None
    functions: tuple[FunctionFacts, ...]
    returns: tuple[ReturnRelation, ...]

    @classmethod
    def disabled(cls):
        return cls(False, None, (), ())

    @classmethod
    def from_payload(cls, provenance, payload, ctx, prepared, opaque):
        def body_for(function):
            body = prepared.get(function)
            if function in opaque or body is None:
                raise ValueError("fact crosses an opaque, replacement or unsupported body at %08x"
                                 % function)
            return body

        functions, seen_functions = [], set()
        for row in payload["functions"]:
            if not isinstance(row, dict):
                raise ValueError("function fact must be an object")
            function = int(row["function"], 16)
            if function in seen_functions or row["scope"] != FUNCTION_SCOPE:
                raise ValueError("duplicate function or incompatible instruction scope")
            body = body_for(function)
            mask, value = int(row["entry_mask"], 16), int(row["entry_value"], 16)
            if mask & ~MODE_MASK or value & ~mask:
                raise ValueError("invalid entry mode bits")
            instructions, seen_sites = [], set()
            for item in row["sites"]:
                try:
                    site, smask, svalue = item
                except (TypeError, ValueError):
                    raise ValueError("invalid, duplicated or out-of-scope instruction fact")
                site, smask, svalue = int(site, 16), int(smask, 16), int(svalue, 16)
                if (site in seen_sites or site not in body.sites
                        or ctx.program.containing(site) != function
                        or not smask or smask & ~MODE_MASK or svalue & ~smask):
                    raise ValueError("invalid, duplicated or out-of-scope instruction fact")
                seen_sites.add(site)
                instructions.append(InstructionFact(
                    function, site, ModeCondition(smask, svalue), FUNCTION_SCOPE, provenance))
            seen_functions.add(function)
            functions.append(FunctionFacts(
                function, FUNCTION_SCOPE,
                EntryCondition(function, mask, value, FUNCTION_SCOPE, provenance),
                tuple(instructions), provenance))

        returns, seen_returns = [], set()
        for row in payload["returns"]:
            if not isinstance(row, dict):
                raise ValueError("return fact must be an object")
            function = int(row["function"], 16)
            if function in seen_returns or row["scope"] != RETURN_SCOPE:
                raise ValueError("duplicate return relation or incompatible entry scope")
            body = body_for(function)
            entries = row["entries"]
            if entries != ["%08x" % body.sites[i] for i in body.entries]:
                raise ValueError("duplicate return relation or incompatible entry scope")
            preserved, known, value = (int(row[k], 16) for k in
                                       ("preserved_mask", "known_mask", "known_value"))
            if (preserved | known) & ~0xffff or preserved & known or value & ~known:
                raise ValueError("invalid return control-word relation")
            seen_returns.add(function)
            returns.append(ReturnRelation(function, RETURN_SCOPE,
                                          tuple(int(entry, 16) for entry in entries),
                                          preserved, known, value, provenance))
        return cls(True, provenance, tuple(functions), tuple(returns))

    def mode_sites(self):
        return {facts.function: {item.site: item for item in facts.instructions}
                for facts in self.functions}

    def report(self):
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "functions": len(self.functions),
                "instruction_sites": sum(len(facts.instructions) for facts in self.functions),
                "entry_modes": {"%08x" % facts.function:
                                ["%04x" % facts.entry.mask, "%04x" % facts.entry.value]
                                for facts in self.functions},
                "return_relations": len(self.returns),
                "identity": self.provenance.identity_payload()}

    def to_payload(self):
        return {"format": FORMAT, **self.provenance.identity_payload(),
                "assumptions": self.provenance.assumptions_payload(),
                "functions": [{"function": "%08x" % facts.function, "scope": facts.scope,
                               "entry_mask": "%04x" % facts.entry.mask,
                               "entry_value": "%04x" % facts.entry.value,
                               "sites": [["%08x" % item.site,
                                          "%04x" % item.mode.mask,
                                          "%04x" % item.mode.value]
                                         for item in facts.instructions]}
                              for facts in self.functions],
                "returns": [{"function": "%08x" % relation.function, "scope": relation.scope,
                             "entries": ["%08x" % entry for entry in relation.entries],
                             "preserved_mask": "%04x" % relation.preserved_mask,
                             "known_mask": "%04x" % relation.known_mask,
                             "known_value": "%04x" % relation.known_value}
                            for relation in self.returns]}

    def to_json(self):
        return json.dumps(self.to_payload(), separators=(",", ":"))


def build(ctx, prepared, summaries, analyses, entries, opaque):
    provenance = Provenance.from_settings(ctx.settings)
    functions = []
    for addr, result in sorted(analyses.items()):
        if addr in opaque:
            continue
        sites = [["%08x" % site, "%04x" % (fact.known_mask & MODE_MASK),
                  "%04x" % (fact.known_value & MODE_MASK)]
                 for site, fact in sorted(result.sites.items()) if fact.known_mask & MODE_MASK]
        if sites:
            fact = entries.get(addr)
            functions.append({"function": "%08x" % addr, "scope": FUNCTION_SCOPE,
                              "entry_mask": "%04x" % (fact.known_mask & MODE_MASK) if fact else "0000",
                              "entry_value": "%04x" % (fact.known_value & MODE_MASK) if fact else "0000",
                              "sites": sites})
    selected = {int(row["function"], 16) for row in functions}
    relations = [{"function": "%08x" % addr, "scope": RETURN_SCOPE,
                  "entries": ["%08x" % body.sites[i] for i in body.entries],
                  "preserved_mask": "%04x" % summary.preserved_mask,
                  "known_mask": "%04x" % summary.known_mask,
                  "known_value": "%04x" % summary.known_value}
                 for addr, summary in sorted(summaries.items())
                 if addr in selected and (body := prepared.get(addr)) and addr not in opaque
                 and (summary.preserved_mask or summary.known_mask)]
    return X87Facts.from_payload(provenance, {"functions": functions, "returns": relations},
                                 ctx, prepared, opaque)


def load(path, ctx, prepared, opaque):
    if path is None:
        return X87Facts.disabled()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("compiler facts must be an object")
        if data.get("format") != FORMAT or any(data.get(k) != v for k, v in identity(ctx.settings).items()):
            raise ValueError("schema, producer or analysis-input identity mismatch")
        expected = {"imports_preserve_pc_rc": ctx.settings.x87_imports_preserve_pc_rc,
                    "entry_contracts": ctx.settings.x87_entry_contracts}
        if data.get("assumptions") != expected:
            raise ValueError("assumption dependencies do not match selected production policy")
        provenance = Provenance.from_payload(data)
        return X87Facts.from_payload(provenance, data, ctx, prepared, opaque)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise TranslateError("x87 compiler facts rejected: %s" % error) from error
