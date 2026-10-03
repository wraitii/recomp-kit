"""Emit a game-free fixture using the production chunk and entry emitters."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate as T


def write(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    # These are synthetic guest addresses, not any game's entry points.
    addresses = [0x0D001000, 0x0D001010, 0x0D001020, 0x0D001030]
    functions = [SimpleNamespace(addr=a, name="fixture", insns=[SimpleNamespace(addr=a)])
                 for a in addresses]
    bodies = {
        addresses[0]: ["void fn_0d001000(X86 *c) { c->r[0] += 1; }"],
        addresses[1]: ["void fn_0d001010(X86 *c) { CALL_FN(0d001000); c->r[1]++; }"],
        addresses[2]: ["void fn_0d001020(X86 *c) { CALL_FN(0d001000); return; }"],
        addresses[3]: ["void fn_0d001030(X86 *c) { CALL_FN(0d001010); }"],
    }
    T.emit_body_chunks(out, functions, bodies, {})
    T.emit_entry_chunks(out, addresses, "recomp_")
    (out / "fixture_table.c").write_text('''#include "x86.h"
void fn_0d001000(X86 *c);
void fn_0d001010(X86 *c);
void fn_0d001020(X86 *c);
void fn_0d001030(X86 *c);
void fixture_native(X86 *c);
const uint32_t recomp_func_addrs[] = {0x0d001000, 0x0d001010, 0x0d001020, 0x0d001030};
const uint32_t recomp_func_count = 4;
void (*const recomp_base_ptrs[])(X86 *) = {
    fixture_native, fn_0d001010, fn_0d001020, fn_0d001030
};
void (*const recomp_raw_ptrs[])(X86 *) = {
    fn_0d001000, fn_0d001010, fn_0d001020, fn_0d001030
};
uint8_t recomp_hooked[4];
RecompHookFn recomp_hook_ptrs[4];
''' + T.emit_entry_dispatch("recomp_"))


if __name__ == "__main__":
    write(sys.argv[1])
