import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diff.run import (Emu, FLAGS, FLAG_MASK, ESP_INIT, MAGIC_RET, UC_PROT_ALL,
                      emulate_original, compare_states)


def emulator(code):
    image = SimpleNamespace(base=0x400000, size=0x2000,
                            bytes=bytes(0x1000) + code + bytes(0x1000 - len(code)),
                            sections=[(0x401000, 0x1000, UC_PROT_ALL)])
    return Emu(image)


class DiffOracleTests(unittest.TestCase):
    def test_multiply_compares_carry_and_overflow_but_not_undefined_flags(self):
        emu = emulator(bytes.fromhex("6b c0 03"))
        emu.u.emu_start(0x401000, 0x401003)
        undefined = FLAGS["PF"] | FLAGS["ZF"] | FLAGS["SF"]
        self.assertEqual(emu.flag_mask, FLAG_MASK & ~undefined)
        oracle = dict(regs=[0] * 8, eflags=0, flag_mask=emu.flag_mask,
                      fpu_top=0, st0=None, mem=[])
        native = dict(oracle, eflags=undefined)
        self.assertEqual(compare_states(oracle, native, []), [])
        native["eflags"] |= FLAGS["CF"]
        self.assertEqual(compare_states(oracle, native, []), ["flags CF"])

    def test_later_compare_defines_flags_again(self):
        code = bytes.fromhex("6b c0 03 83 f8 00")
        emu = emulator(code)
        emu.u.emu_start(0x401000, 0x401000 + len(code))
        self.assertEqual(emu.flag_mask, FLAG_MASK)

    def test_x87_status_effects_do_not_define_integer_flags(self):
        code = bytes.fromhex("6b c0 03 d9 ee df e0 dd d8")
        emu = emulator(code)
        emu.u.emu_start(0x401000, 0x401000 + len(code))
        undefined = FLAGS["PF"] | FLAGS["ZF"] | FLAGS["SF"]
        self.assertEqual(emu.flag_mask, FLAG_MASK & ~undefined)

    def test_empty_x87_operands_are_reported_as_original_stack_faults(self):
        for code, expected in ((bytes.fromhex("de d9 c3"), "original x87 stack fault"),
                               (bytes.fromhex("d9 ee d9 ee de d9 c3"), None)):
            with self.subTest(code=code.hex()):
                emu = emulator(code)
                regs = [0] * 8
                regs[4] = ESP_INIT
                reason, _ = emulate_original(
                    emu.u, emu, 0x401000, [], [], SimpleNamespace(max_insns=20),
                    regs, 0x202, 0x037f, struct.pack("<I", MAGIC_RET), [])
                self.assertEqual(reason, expected)


if __name__ == "__main__":
    unittest.main()
