"""Shared analysis inputs, contracts, boundaries and reports."""
import hashlib
import json
from pathlib import Path

import capstone
import game_config
from settings import Settings
from ir import x87
from ir.import_sites import cached_imports
from ir.x87_cw import (CW_D3D8_PRESERVED, CW_D3D8_SETUP_MASK, CW_D3D8_SETUP_VALUE,
                       CW_MASK, CW_PC_RC, ReturnSummary, fact_dict)

MASK = CW_PC_RC


def settings_for(args):
    game_config.resolve_game_args(args, default=None)
    settings = Settings(game_config.load(args.game_dir, args.build_root), allow_unmodelled="offline CW analysis")
    if hashlib.sha256(Path(settings.exe).read_bytes()).hexdigest() != settings.sha256.lower():
        raise ValueError("original executable SHA-256 does not match game configuration")
    return settings



def apply_contracts(prepared, boundaries, image, rows):
    prepared = dict(prepared)
    boundaries = {**boundaries, "calls": dict(boundaries["calls"])}
    for row in rows:
        addr, site = int(row["function"], 16), int(row["site"], 16)
        if not row.get("evidence") or not row.get("assumption"):
            raise ValueError("analysis contracts require evidence and an explicit assumption")
        raw = bytes.fromhex(row["bytes"])
        if image.bytes_at(site, len(raw)) != raw:
            raise ValueError("%08x: analysis contract disagrees with original bytes" % site)
        if row["kind"] == "terminate_function":
            if site != addr:
                raise ValueError("termination contract bytes must identify the function entry")
            sites = [{**row, "kind": "terminate_call", "function": "%08x" % owner,
                      "site": "%08x" % call_site,
                      "bytes": image.bytes_at(call_site, 5).hex()}
                     for owner, body in prepared.items() if body
                     for _, call_site, target in body.calls if target == addr]
            prepared, boundaries = apply_contracts(prepared, boundaries, image, sites)
            continue
        body = prepared[addr]
        if body is None or site not in body.sites:
            raise ValueError("%08x: analysis contract has no prepared instruction" % site)
        index = body.sites.index(site)
        if row["kind"] == "preserve_call":
            if site not in {s for _, s, _ in body.calls}:
                raise ValueError("%08x: expected a prepared call" % site)
            boundaries["calls"][site] = ReturnSummary(MASK, 0, 0)
            continue
        succ = list(body.succ)
        if row["kind"] == "terminate_call":
            if site not in {s for _, s, _ in body.calls}:
                raise ValueError("%08x: expected a prepared call" % site)
            succ[index] = ()
        elif row["kind"] == "select_edge":
            target = body.sites.index(int(row["target"], 16))
            if target not in succ[index]:
                raise ValueError("%08x: selected edge is absent" % site)
            succ[index] = (target,)
        else:
            raise ValueError("unknown analysis contract kind: " + row["kind"])
        seen, todo = set(), list(body.entries)
        while todo:
            i = todo.pop()
            if i not in seen:
                seen.add(i)
                todo.extend(succ[i])
        succ = tuple(tuple(j for j in targets if j in seen) if i in seen else ()
                     for i, targets in enumerate(succ))
        preds = [[] for _ in body.sites]
        for i, targets in enumerate(succ):
            for j in targets:
                preds[j].append(i)
        prepared[addr] = body._replace(
            order=tuple(i for i in body.order if i in seen), succ=succ,
            preds=tuple(tuple(p) for p in preds),
            effects=tuple(e for e in body.effects if e[0] in seen),
            calls=tuple(c for c in body.calls if c[0] in seen
                        and not (row["kind"] == "terminate_call" and c[0] == index)),
            returns=tuple(i for i in body.returns if i in seen
                          and not (row["kind"] == "terminate_call" and i == index)),
            word_relations={i: r for i, r in body.word_relations.items() if i in seen})
    return prepared, boundaries


def preserving(summary):
    return summary.preserved_mask & MASK == MASK


def coverage(results, mnemonics):
    arithmetic = []
    known_functions = set()
    sites = 0
    for addr, result in results.items():
        for site, fact in result.sites.items():
            if not fact.known_mask & MASK:
                continue
            sites += 1
            known_functions.add(addr)
            if mnemonics.get(site) in x87.ARITH:
                arithmetic.append({"function": "%08x" % addr, "site": "%08x" % site,
                                   **fact_dict(fact)})
    return {"functions_with_known_sites": len(known_functions), "known_sites": sites,
            "arithmetic_sites": len(arithmetic),
            "arithmetic_functions": len({row["function"] for row in arithmetic}),
            "arithmetic": arithmetic}



def context_contracts(path, ctx):
    contracts = json.loads(path.read_text()) if path else []
    for row in contracts:
        site = int(row.get('site', row['function']), 16)
        raw = bytes.fromhex(row['bytes'])
        if not raw or ctx.image.bytes_at(site, len(raw)) != raw:
            raise ValueError('contract bytes disagree at %08x' % site)
        if row['kind'] not in ('frame_call', 'terminate_function', 'terminate_call') or not row.get('assumption') or not row.get('evidence'):
            raise ValueError('contract must name its kind, evidence and assumption')
        if row['kind'] == 'frame_call' and (row.get('stack_pop', -1) < 0
                or row.get('foreign_eax') and row.get('foreign_size', 0) <= 0):
            raise ValueError('frame contract requires stack cleanup and a bounded foreign object')
        if row.get('require_imports_preserve_pc_rc') and not ctx.settings.x87_imports_preserve_pc_rc:
            raise ValueError('frame contract requires the configured import preservation contract')
        if row['kind'] == 'terminate_call' and ctx.program.containing(site) != int(row['function'], 16):
            raise ValueError('terminating call does not belong to its stated owner')
    return contracts


PRESERVE = ReturnSummary(CW_MASK, 0, 0)
D3D8_CREATE = ReturnSummary(CW_D3D8_PRESERVED, 0, 0)
D3D8_SETUP = ReturnSummary(CW_D3D8_PRESERVED, CW_D3D8_SETUP_MASK, CW_D3D8_SETUP_VALUE)
BOUNDARY_EFFECTS = {"preserve": PRESERVE, "d3d8_create_no_preserve": D3D8_CREATE}


def load_boundaries(path, prepared, image, imports_preserve_pc_rc=False):
    data = json.loads(Path(path).read_text()) if path else {"boundaries": []}
    calls, edges, metadata = {}, {}, []
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    decoder.detail = True
    imports = {row["name"]: row for row in data.get("imports", [])}
    def decode(site):
        return next(decoder.disasm(image.data[site - image.base:site - image.base + 15], site), None)

    def import_name(ins, mnemonic, thunk=True):
        if ins is None or ins.mnemonic != mnemonic or len(ins.operands) != 1:
            return None
        op = ins.operands[0]
        if op.type == capstone.x86.X86_OP_IMM and thunk:
            return import_name(decode(op.imm), "jmp", False)
        if op.type != capstone.x86.X86_OP_MEM or op.mem.base or op.mem.index:
            return None
        return image.iat_names.get(op.mem.disp & 0xffffffff)

    for addr, body in prepared.items():
        if body is None:
            continue
        cached = cached_imports(body, decode, image.iat_names, import_name)
        for _, site, _ in body.calls:
            ins = decode(site)
            name = cached.get(site) or (import_name(ins, ins.mnemonic)
                   if ins and ins.mnemonic in ("call", "jmp") else None)
            if name in imports:
                calls[site] = BOUNDARY_EFFECTS[imports[name]["effect"]]
                source = "audited_native"
            elif name and imports_preserve_pc_rc:
                calls[site] = ReturnSummary(CW_PC_RC, 0, 0)
                source = "configuration_assumption"
            else:
                continue
            metadata.append({"function": "%08x" % addr, "site": "%08x" % site,
                             "import": name, "source": source,
                             "target_evidence": "original-byte CFG register provenance" if site in cached else "original-byte IAT operand",
                             "preserved_mask": "%04x" % calls[site].preserved_mask})
    for row in data["boundaries"]:
        addr, site = int(row["function"], 16), int(row["site"], 16)
        body = prepared.get(addr)
        if body is None or site not in {s for _, s, _ in body.calls}:
            raise ValueError("%08x: audited CW boundary has no prepared call" % site)
        ins = next(decoder.disasm(image.data[site - image.base:site - image.base + 15], site))
        operands = ins.operands
        if "import" in row:
            source = decode(int(row["load_site"], 16))
            if (ins.mnemonic != "call" or len(operands) != 1
                    or operands[0].type != capstone.x86.X86_OP_REG
                    or ins.reg_name(operands[0].reg) != row["register"]
                    or source is None or source.mnemonic != "mov" or len(source.operands) != 2
                    or source.operands[0].type != capstone.x86.X86_OP_REG
                    or source.reg_name(source.operands[0].reg) != row["register"]
                    or source.operands[1].type != capstone.x86.X86_OP_MEM
                    or source.operands[1].mem.base or source.operands[1].mem.index
                    or image.iat_names.get(source.operands[1].mem.disp & 0xffffffff) != row["import"]):
                raise ValueError("%08x: audited cached import disagrees with original bytes" % site)
            if imports_preserve_pc_rc:
                calls[site] = ReturnSummary(CW_PC_RC, 0, 0)
            continue
        if (ins.mnemonic != "call" or len(operands) != 1
                or operands[0].type != capstone.x86.X86_OP_MEM
                or operands[0].mem.disp != row["vtable_offset"]):
            raise ValueError("%08x: audited CW boundary disagrees with original call" % site)
        if any(int(item["site"], 16) == site for item in data["boundaries"] if item is not row):
            raise ValueError("%08x: duplicate CW boundary" % site)
        calls[site] = BOUNDARY_EFFECTS[row["effect"]]
        if row["effect"] == "d3d8_create_no_preserve":
            test, branch = list(decoder.disasm(image.data[site + ins.size - image.base:site + ins.size - image.base + 8], site + ins.size))[:2]
            target = int(row["success"], 16)
            if (test.mnemonic != "test" or test.op_str != "eax, eax"
                    or branch.mnemonic != "jge" or branch.operands[0].imm != target):
                raise ValueError("%08x: audited device success edge disagrees with original bytes" % site)
            indices = {s: i for i, s in enumerate(body.sites)}
            if target not in body.sites or indices[target] not in body.succ[indices[branch.address]]:
                raise ValueError("%08x: device success edge missing from CFG" % site)
            edges[(branch.address, target)] = D3D8_SETUP
    roots = {}
    for row in data.get("roots", []):
        addr = int(row["function"], 16)
        mask, value = int(row.get("mask", "0000"), 16), int(row.get("value", "0000"), 16)
        name = row["name"]
        if (addr not in prepared or name in roots or mask & ~CW_MASK or value & ~mask
                or ("site" in row and (prepared[addr] is None
                    or int(row["site"], 16) not in prepared[addr].sites))):
            raise ValueError("%08x: invalid contextual CW root" % addr)
        roots[name] = row
    return {"calls": calls, "edges": edges, "metadata": data["boundaries"] + data.get("imports", []) + metadata, "roots": roots}
