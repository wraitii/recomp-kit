"""Audited native CW relations, separate from translated instruction proofs."""
import json
from pathlib import Path

import capstone

from .import_sites import cached_imports
from .x87_cw import (CW_D3D8_PRESERVED, CW_D3D8_SETUP_MASK, CW_D3D8_SETUP_VALUE,
                     CW_MASK, CW_PC_RC, ReturnSummary)

PRESERVE = ReturnSummary(CW_MASK, 0, 0)
D3D8_CREATE = ReturnSummary(CW_D3D8_PRESERVED, 0, 0)
D3D8_SETUP = ReturnSummary(CW_D3D8_PRESERVED, CW_D3D8_SETUP_MASK, CW_D3D8_SETUP_VALUE)
EFFECTS = {"preserve": PRESERVE, "d3d8_create_no_preserve": D3D8_CREATE}


def load(path, prepared, image, imports_preserve_pc_rc=False):
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
                calls[site] = EFFECTS[imports[name]["effect"]]
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
        calls[site] = EFFECTS[row["effect"]]
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
