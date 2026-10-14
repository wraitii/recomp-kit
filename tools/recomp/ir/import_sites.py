from collections import namedtuple

import capstone

CALLBACK_IMPORTS = frozenset((
    "kernel32.dll!CreateThread", "kernel32.dll!ResumeThread", "kernel32.dll!RaiseException",
    "kernel32.dll!RtlUnwind", "kernel32.dll!LoadLibraryA", "kernel32.dll!LoadLibraryW",
    "kernel32.dll!LoadLibraryExW", "kernel32.dll!FreeLibrary", "kernel32.dll!EnumResourceNamesW",
    "kernel32.dll!EnumCalendarInfoW", "kernel32.dll!EnumSystemLocalesW",
    "user32.dll!CreateWindowExA", "user32.dll!CreateWindowExW",
    "user32.dll!DefWindowProcA", "user32.dll!DefWindowProcW", "user32.dll!DestroyWindow",
    "user32.dll!DispatchMessageA", "user32.dll!DispatchMessageW", "user32.dll!GetMessageA",
    "user32.dll!GetMessageW", "user32.dll!PeekMessageA", "user32.dll!PeekMessageW",
    "user32.dll!SendMessageA", "user32.dll!SendMessageW", "user32.dll!CallWindowProcW",
    "user32.dll!SetWindowPos", "user32.dll!UpdateWindow", "user32.dll!EnumWindows",
    "user32.dll!EnumChildWindows", "user32.dll!EnumThreadWindows", "user32.dll!EnumDisplayMonitors",
    "imm32.dll!ImmIsUIMessageA", "uxtheme.dll!DrawThemeParentBackground",
    "winmm.dll!timeSetEvent", "winmm.dll!midiOutOpen", "winmm.dll!midiOutLongMsg", "winmm.dll!waveOutOpen",
    "gdi32.dll!EnumFontFamiliesExW", "ddraw.dll!DirectDrawEnumerateA", "ddraw.dll!DirectDrawEnumerateW",
    "ddraw.dll!DirectDrawEnumerateExA", "ddraw.dll!DirectDrawEnumerateExW", "dsound.dll!DirectSoundEnumerateA",
    "ole32.dll!CoCreateInstance", "dinput.dll!DirectInputCreateA",
    "mss32.dll!_AIL_mem_alloc_lock@4", "mss32.dll!_AIL_mem_free_lock@4",
    "qmixer.dll!QSWaveMixActivate", "qmixer.dll!QSWaveMixCloseSession", "qmixer.dll!QSWaveMixFlushChannel",
    "qmixer.dll!QSWaveMixFreeWave", "qmixer.dll!QSWaveMixPlayEx", "qmixer.dll!QSWaveMixPump",
    "qmixer.dll!QSWaveMixStopChannel",
))

EMPTY_EFFECTS = {"sites": {}, "imports": {}}

_Body = namedtuple("_Body", ("sites", "order", "succ", "preds", "entries", "calls"))


def cached_imports(body, decode, slots, import_name):
    decoded = {i: decode(body.sites[i]) for i in body.order}
    calls = {i for i, _, _ in body.calls}
    incoming, outgoing = {}, {}
    work, queued = list(body.entries), set(body.entries)
    while work:
        i = work.pop()
        queued.discard(i)
        states = [outgoing[p] for p in body.preds[i] if p in outgoing]
        if i in body.entries:
            states.append({})
        state = dict(states[0])
        for other in states[1:]:
            state = {reg: name for reg, name in state.items() if other.get(reg) == name}
        if incoming.get(i) == state:
            continue
        incoming[i] = state
        state = dict(state)
        ins = decoded[i]
        if ins is None:
            state.clear()
        elif ins.mnemonic in ("call", "jmp") and i in calls:
            name = import_name(ins, ins.mnemonic)
            if (len(ins.operands) == 1 and ins.operands[0].type == capstone.x86.X86_OP_REG):
                name = state.get(ins.reg_name(ins.operands[0].reg))
            if name:
                state = {reg: value for reg, value in state.items()
                         if reg in ("ebx", "esi", "edi", "ebp")}
            else:
                state.clear()
        else:
            name = None
            operands = ins.operands
            if (ins.mnemonic == "mov" and len(operands) == 2
                    and operands[0].type == capstone.x86.X86_OP_REG and operands[0].size == 4):
                source = operands[1]
                if (source.type == capstone.x86.X86_OP_MEM and source.size == 4
                        and not source.mem.base and not source.mem.index and not source.mem.segment):
                    name = slots.get(source.mem.disp & 0xffffffff)
                elif source.type == capstone.x86.X86_OP_REG and source.size == 4:
                    name = state.get(ins.reg_name(source.reg))
            _, writes = ins.regs_access()
            for reg in writes:
                reg = ins.reg_name(reg)
                aliases = {"al": "eax", "ah": "eax", "ax": "eax", "bl": "ebx", "bh": "ebx",
                           "bx": "ebx", "cl": "ecx", "ch": "ecx", "cx": "ecx", "dl": "edx",
                           "dh": "edx", "dx": "edx", "si": "esi", "di": "edi", "bp": "ebp"}
                state.pop(aliases.get(reg, reg), None)
            if name:
                state[ins.reg_name(operands[0].reg)] = name
        if outgoing.get(i) != state:
            outgoing[i] = state
            for target in body.succ[i]:
                if target not in queued:
                    work.append(target)
                    queued.add(target)
    return {body.sites[i]: incoming[i][ins.reg_name(ins.operands[0].reg)]
            for i, ins in decoded.items() if i in incoming and ins is not None
            and ins.mnemonic == "call" and len(ins.operands) == 1
            and ins.operands[0].type == capstone.x86.X86_OP_REG
            and ins.reg_name(ins.operands[0].reg) in incoming[i]}


def resolve(fir, image):
    """{call site: "dll!name"} for CALL sites that provably call an IAT slot."""
    if not any(op.opc in ("CALL", "CALLIND") for ins in fir.insns for op in ins.ops):
        return {}
    slots = image.iat_keys
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    decoder.detail = True
    decoded = {ins.addr: next(decoder.disasm(ins.raw or b"", ins.addr), None) for ins in fir.insns}
    sites = tuple(ins.addr for ins in fir.insns)
    preds = [[] for _ in sites]
    for i, targets in enumerate(fir.succ):
        for j in targets:
            preds[j].append(i)
    body = _Body(sites, range(len(sites)), fir.succ, preds,
                 [i for i, ins in enumerate(fir.insns) if ins.addr == fir.addr or ins.addr in fir.entries],
                 [(i, ins.addr, None) for i, ins in enumerate(fir.insns)
                  if any(op.opc in ("CALL", "CALLIND") for op in ins.ops)])

    def import_name(ins, mnemonic):
        if ins is None or ins.mnemonic != mnemonic or len(ins.operands) != 1:
            return None
        op = ins.operands[0]
        if op.type != capstone.x86.X86_OP_MEM or op.mem.base or op.mem.index:
            return None
        return slots.get(op.mem.disp & 0xffffffff)

    found = cached_imports(body, decoded.get, slots, import_name)
    out = {}
    for _, site, _ in body.calls:
        name = found.get(site) or import_name(decoded[site], "call")
        if name:
            out[site] = name
    return out
