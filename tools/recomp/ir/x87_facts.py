"""Validated compiler facts, separate from offline discovery reports."""
import hashlib
import json
from pathlib import Path

from program import TranslateError

FORMAT = "x87-compiler-facts-v1"
PRODUCER = "recomp-kit-x87-analysis-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tree_digest(root, patterns):
    files = set(p for pattern in patterns for p in root.rglob(pattern) if p.is_file()
                and not {"build", "analysis", ".git", "__pycache__"}.intersection(p.relative_to(root).parts))
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(files)])


def identity(settings):
    kit = Path(__file__).resolve().parents[3]
    tools = kit / "tools"
    policy = {key: value for key, value in settings.cfg["translate"].items()
              if key not in ("x87_facts", "code_map")}
    boundary = settings.x87_mode_metadata
    return {"executable_sha256": settings.sha256.lower(),
            "producer": PRODUCER,
            "producer_sha256": tree_digest(tools, ("*.py",)),
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


def document(ctx, prepared, summaries, analyses, entries, opaque):
    functions = []
    for addr, result in sorted(analyses.items()):
        if addr in opaque:
            continue
        sites = [["%08x" % site, "%04x" % (fact.known_mask & 0x0f00),
                  "%04x" % (fact.known_value & 0x0f00)]
                 for site, fact in sorted(result.sites.items()) if fact.known_mask & 0x0f00]
        if sites:
            fact = entries.get(addr)
            functions.append({"function": "%08x" % addr, "scope": "all_invocations_joined",
                              "entry_mask": "%04x" % (fact.known_mask & 0x0f00) if fact else "0000",
                              "entry_value": "%04x" % (fact.known_value & 0x0f00) if fact else "0000",
                              "sites": sites})
    selected = {int(row["function"], 16) for row in functions}
    relations = [{"function": "%08x" % addr, "scope": "all_mapped_entries",
                  "entries": ["%08x" % body.sites[i] for i in body.entries],
                  "preserved_mask": "%04x" % summary.preserved_mask,
                  "known_mask": "%04x" % summary.known_mask,
                  "known_value": "%04x" % summary.known_value}
                 for addr, summary in sorted(summaries.items())
                 if addr in selected and (body := prepared.get(addr)) and addr not in opaque
                 and (summary.preserved_mask or summary.known_mask)]
    return {"format": FORMAT, **identity(ctx.settings),
            "assumptions": {"imports_preserve_pc_rc": ctx.settings.x87_imports_preserve_pc_rc,
                            "entry_contracts": ctx.settings.x87_entry_contracts},
            "functions": functions, "returns": relations}


def load(path, ctx, prepared, opaque):
    if path is None:
        return {}, {"enabled": False}
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
        facts, entries = {}, {}
        def body_for(row):
            addr = int(row["function"], 16)
            body = prepared.get(addr)
            if addr in opaque or body is None:
                raise ValueError("fact crosses an opaque, replacement or unsupported body at %08x" % addr)
            return addr, body
        for row in data["functions"]:
            addr, body = body_for(row)
            if addr in facts or row["scope"] != "all_invocations_joined":
                raise ValueError("duplicate function or incompatible instruction scope")
            mask, value = int(row["entry_mask"], 16), int(row["entry_value"], 16)
            if mask & ~0x0f00 or value & ~mask:
                raise ValueError("invalid entry mode bits")
            entries[addr] = (mask, value)
            sites = {}
            for site, mask, value in row["sites"]:
                site, mask, value = int(site, 16), int(mask, 16), int(value, 16)
                if (site in sites or site not in body.sites or ctx.program.containing(site) != addr
                        or not mask or mask & ~0x0f00 or value & ~mask):
                    raise ValueError("invalid, duplicated or out-of-scope instruction fact")
                sites[site] = (mask, value)
            facts[addr] = sites
        seen = set()
        for row in data["returns"]:
            addr, body = body_for(row)
            if (addr in seen or row["scope"] != "all_mapped_entries"
                    or row["entries"] != ["%08x" % body.sites[i] for i in body.entries]):
                raise ValueError("duplicate return relation or incompatible entry scope")
            seen.add(addr)
            preserved, known, value = (int(row[k], 16) for k in ("preserved_mask", "known_mask", "known_value"))
            if (preserved | known) & ~0xffff or preserved & known or value & ~known:
                raise ValueError("invalid return control-word relation")
        return facts, {"enabled": True, "functions": len(facts),
                       "instruction_sites": sum(map(len, facts.values())),
                       "entry_modes": {"%08x" % a: ["%04x" % m, "%04x" % v] for a, (m, v) in entries.items()},
                       "return_relations": len(seen), "identity": identity(ctx.settings)}
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise TranslateError("x87 compiler facts rejected: %s" % error) from error
