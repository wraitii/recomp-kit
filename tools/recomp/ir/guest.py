"""Guest register identities, aliases and linkage, without backend storage syntax."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Register:
    name: str
    width: int
    roles: frozenset[str]


@dataclass(frozen=True)
class Alias:
    name: str
    parent: str
    offset: int
    width: int


@dataclass(frozen=True)
class Linkage:
    stack_pointer: str
    frame_pointer: str
    instruction_pointer: str
    address_width: int
    return_slot_bytes: int
    continuation: str
    call_effect: str
    return_effect: str


@dataclass(frozen=True)
class GuestState:
    language: str
    registers: tuple[Register, ...]
    aliases: tuple[Alias, ...]
    linkage: Linkage

    def names(self, role):
        return tuple(r.name for r in self.registers if role in r.roles)

    def validate(self, lifter):
        if lifter.language != self.language:
            raise ValueError("guest state %s cannot describe %s" % (self.language, lifter.language))
        def register(name):
            try:
                return lifter.register(name)
            except KeyError as error:
                raise ValueError("guest state %s lacks register %s" % (self.language, name)) from error

        registers = {r.name: r for r in self.registers}
        for r in self.registers:
            if register(r.name)[2] * 8 != r.width:
                raise ValueError("guest register width mismatch: %s" % r.name)
        for alias in self.aliases:
            parent = register(alias.parent)
            actual = register(alias.name)
            if (alias.offset + alias.width > registers[alias.parent].width
                    or actual[1] != parent[1] + alias.offset // 8
                    or actual[2] * 8 != alias.width):
                raise ValueError("guest register alias mismatch: %s" % alias.name)


_X86_GPRS = ("EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI")
_X86_FLAGS = ("CF", "PF", "AF", "ZF", "SF", "OF")
_X86_ROLES = {"ESP": {"stack", "mandatory"}, "EBP": {"frame", "mandatory"}}
X86_32 = GuestState(
    "x86:LE:32:default",
    tuple(Register(name, 32, frozenset({"gpr", "contract"} | _X86_ROLES.get(name, set())))
          for name in _X86_GPRS)
    + (Register("EIP", 32, frozenset({"instruction", "mandatory"})),
       Register("FS_OFFSET", 32, frozenset({"segment_base"})))
    + tuple(Register(name, 8, frozenset({"arithmetic_flag", "contract"})) for name in _X86_FLAGS)
    + (Register("DF", 8, frozenset({"direction_flag"})),),
    tuple(Alias(name, parent, 0, 16) for name, parent in zip(
        ("AX", "CX", "DX", "BX", "SP", "BP", "SI", "DI"), _X86_GPRS))
    + tuple(Alias(name, parent, offset, 8) for offset, names in (
        (0, ("AL", "CL", "DL", "BL")), (8, ("AH", "CH", "DH", "BH")))
        for name, parent in zip(names, _X86_GPRS)),
    Linkage("ESP", "EBP", "EIP", 32, 4, "instruction_fallthrough",
            "push_continuation", "pop_continuation_and_adjust_stack"),
)
