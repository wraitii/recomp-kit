#define RECOMP_GUEST_MEMORY_OWNER 1 /* defines and maps g_mem */
#include "../platform/os.h"
// interp_tests.cpp - the heap-code interpreter (runtime/interp.cpp).
//
//   interp_tests               the checks below
//   interp_tests --run HEX     runs HEX as a routine from a fixed state and
//                              prints the final state
#include "interp.h"

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#ifndef _WIN32
#include <unistd.h>
#endif

uint8_t *g_mem;
// This test links the interpreter alone, so it owns the definitions the
// runtime would otherwise supply: guest memory, and the dirty-region list
// x86.h's stores record.
RecompDirty g_dirty[RECOMP_DIRTY_SLOTS];
uint32_t g_dirty_count, g_store_hook;
uint32_t g_watch_base, g_watch_len; // the memory watch, never armed here
extern "C" void recomp_watch_hit(uint32_t, uint32_t, uint64_t) {}

// The interpreter tests link neither cpu.cpp nor a guest with handlers, so a
// null dereference has nowhere to raise to: report it and stop, which is what
// the real one does when the guest has no handler either.
extern "C" void recomp_null_access(uint32_t addr, int write) {
    fprintf(stderr, "null %s of %08x\n", write ? "write" : "read", addr);
    abort();
}

static int g_checks = 0, g_failures = 0;
#define CHECK(cond)                                                                                \
    do {                                                                                           \
        ++g_checks;                                                                                \
        if (!(cond)) {                                                                             \
            ++g_failures;                                                                          \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
        }                                                                                          \
    } while (0)

const uint32_t CODE = 0x01000000, HELPER = 0x01001000, DATA = 0x02000000, STACK = 0x0e000000;
const uint32_t NATIVE = 0x00401000, RETURN = 0x00400ff0;
static uint32_t g_native_arg = 0, g_native_calls = 0;

// The generated table's stand-in: NATIVE is a "translated" cdecl function
// that returns its argument plus one; heap addresses go to the interpreter.
extern "C" void recomp_call(X86 *c, uint32_t target) {
    if (target == NATIVE) {
        ++g_native_calls;
        g_native_arg = rd32(c->r[R_ESP] + 4);
        c->r[R_EAX] = g_native_arg + 1;
        c->eip = rd32(c->r[R_ESP]);
        c->r[R_ESP] += 4;
        return;
    }
    if (!interp_call(c, target)) {
        fprintf(stderr, "recomp_call %08x: %s\n", target, interp_last_error());
        c->eip = rd32(c->r[R_ESP]);
        c->r[R_ESP] += 4;
        c->r[R_EAX] = 0;
    }
}

// NATIVE is the one "translated" entry the table knows, so a JMP to it is a
// tail call the interpreter hands back rather than decoding into.
extern "C" int recomp_thunk_target_kind(uint32_t target) {
    return target == NATIVE ? 1 : 0;
}

extern "C" void recomp_jump(X86 *c, uint32_t target) {
    recomp_call(c, target);
}

static void put(uint32_t at, const std::vector<uint8_t> &b) {
    memcpy(g_mem + at, b.data(), b.size());
}
static void put_call(uint32_t at, uint32_t target) {
    g_mem[at] = 0xe8;
    wr32(at + 1, target - (at + 5));
}

static X86 fresh() {
    X86 c{};
    c.eflags_misc = 0x202;
    c.r[R_ESP] = STACK - 0x100;
    return c;
}

static void test_bank_routine() {
    // The shape of an audio bank routine: fetch values through helpers and
    // store them into the parameter block the caller passes.
    std::vector<uint8_t> code = {
        0x56,                               // push esi
        0x8b, 0x74, 0x24, 0x08,             // mov esi, [esp+8]
        0x56,                               // push esi
        0xe8, 0,    0,    0,    0,          // call NATIVE
        0x89, 0x86, 0x88, 0x00, 0x00, 0x00, // mov [esi+0x88], eax
        0x83, 0xc6, 0x14,                   // add esi, 0x14
        0x56,                               // push esi
        0xe8, 0,    0,    0,    0,          // call HELPER
        0x89, 0x46, 0x5c,                   // mov [esi+0x5c], eax
        0x8b, 0x46, 0x04,                   // mov eax, [esi+4]
        0x8b, 0x4e, 0x08,                   // mov ecx, [esi+8]
        0x3b, 0xc1,                         // cmp eax, ecx
        0x7c, 0x02,                         // jl +2
        0x8b, 0xc1,                         // mov eax, ecx
        0x0f, 0xaf, 0x46, 0x04,             // imul eax, [esi+4]
        0x2b, 0x46, 0x08,                   // sub eax, [esi+8]
        0x89, 0x46, 0x90,                   // mov [esi-0x70], eax
        0xc6, 0x46, 0x00, 0x07,             // mov byte [esi], 7
        0x83, 0xc4, 0x08,                   // add esp, 8
        0x5e,                               // pop esi
        0xc3,                               // ret
    };
    put(CODE, code);
    put_call(CODE + 6, NATIVE);
    put_call(CODE + 21, HELPER);
    // The helper, also heap code: returns [arg] * 3 through LEA.
    put(HELPER, {0x8b, 0x44, 0x24, 0x04, // mov eax, [esp+4]
                 0x8b, 0x00,             // mov eax, [eax]
                 0x8d, 0x04, 0x40,       // lea eax, [eax+eax*2]
                 0xc2, 0x00, 0x00});     // ret 0 (cdecl)
    const uint32_t block = DATA + 0x100;
    memset(g_mem + block, 0, 0x200);
    wr32(block + 0x14, 5);     // helper input
    wr32(block + 0x14 + 4, 9); // eax
    wr32(block + 0x14 + 8, 4); // ecx: 9 < 4 is false, so eax = 4
    X86 c = fresh();
    c.r[R_ESI] = 0x1234;
    const uint32_t esp = c.r[R_ESP];
    wr32(esp - 4, block);
    wr32(esp - 8, RETURN);
    c.r[R_ESP] = esp - 8;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(c.r[R_ESP] == esp - 4); // the RET popped the return address only
    CHECK(c.eip == RETURN);
    CHECK(c.r[R_ESI] == 0x1234);
    CHECK(g_native_calls == 1 && g_native_arg == block);
    CHECK(rd32(block + 0x88) == block + 1);
    CHECK(rd32(block + 0x14 + 0x5c) == 15);
    CHECK(rd32(block + 0x14 - 0x70) == 4 * 9 - 4);
    CHECK(rd8(block + 0x14) == 7);
    CHECK(rd8(block + 0x15) == 0); // one byte only
    CHECK(c.r[R_ECX] == 4);

    // Rewritten code is decoded again.
    g_mem[HELPER + 6] = 0x90; // lea -> nop nop nop: the helper returns [arg]
    g_mem[HELPER + 7] = 0x90;
    g_mem[HELPER + 8] = 0x90;
    c = fresh();
    wr32(c.r[R_ESP] - 4, block);
    wr32(c.r[R_ESP] - 8, RETURN);
    c.r[R_ESP] -= 8;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(rd32(block + 0x14 + 0x5c) == 7); // the byte the first run stored
}

// A lone JMP to a translated entry - the shape of a compiler's thunk, and of
// the jump-table slots a listing misses - is dispatched, not decoded through.
static void test_tail_call_out() {
    uint32_t stack = STACK - 0x40;
    put(CODE, {0xe9, 0, 0, 0, 0}); // jmp NATIVE
    wr32(CODE + 1, NATIVE - (CODE + 5));
    X86 c = fresh();
    c.r[R_ESP] = stack;
    wr32(stack, RETURN);     // the caller's return address
    wr32(stack + 4, 0x1234); // its cdecl argument, which NATIVE reads
    uint32_t before = g_native_calls;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(g_native_calls == before + 1);
    CHECK(c.r[R_EAX] == 0x1234 + 1);
    CHECK(c.eip == RETURN);
}

static void test_refusals() {
    // An instruction outside the set: nothing runs.
    put(CODE, {0x56, 0xd9, 0x46, 0x04, 0x5e, 0xc3}); // push esi; fld [esi+4]; pop esi; ret
    X86 c = fresh();
    X86 before = c;
    CHECK(interp_call(&c, CODE) == 0);
    CHECK(memcmp(&c, &before, sizeof c) == 0);
    CHECK(strstr(interp_last_error(), "unsupported") != nullptr);
    // A branch into the middle of an instruction.
    put(CODE, {0x74, 0x01, 0xb8, 1, 0, 0, 0, 0xc3});
    CHECK(interp_call(&c, CODE) == 0);
    CHECK(strstr(interp_last_error(), "inside") != nullptr);
    // A backward branch that leaves the routine.
    put(CODE, {0xeb, 0xfc, 0xc3});
    CHECK(interp_call(&c, CODE) == 0);
    // A forward branch past the first RET keeps the decoder going.
    put(CODE, {0x85, 0xc0, 0x74, 0x01, 0xc3, 0x40, 0xc3}); // test eax,eax; jz; ret; inc eax; ret
    c = fresh();
    wr32(c.r[R_ESP] - 4, RETURN);
    c.r[R_ESP] -= 4;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(c.r[R_EAX] == 1);
    // A fault part way through returns zero to the caller with its stack intact.
    put(CODE, {0x56, 0x56, 0x8b, 0x05, 0xff, 0xff, 0xff, 0xff, 0x5e, 0x5e,
               0xc3}); // mov eax, [0xffffffff]
    c = fresh();
    const uint32_t esp = c.r[R_ESP];
    wr32(esp - 4, RETURN);
    c.r[R_ESP] = esp - 4;
    c.r[R_EAX] = 5;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(c.r[R_EAX] == 0 && c.r[R_ESP] == esp && c.eip == RETURN);
}

// A routine that never reaches its RET has to stop on the step budget. Every
// check here would hang, not fail, if the budget were taken back out, so the
// suite is run under a watchdog: see main().
static void test_step_budget() {
    // jmp $ - the shape a data file's garbage decodes into. The decoder
    // accepts it (the JMP is the routine's last instruction and nothing
    // branches past it), so only the budget ends the run.
    put(CODE, {0xeb, 0xfe});
    X86 c = fresh();
    const uint32_t esp = c.r[R_ESP];
    wr32(esp - 4, RETURN);
    wr32(esp - 8, 0xaabbccdd);
    c.r[R_ESP] = esp - 4;
    c.r[R_EAX] = 5;
    CHECK(interp_call(&c, CODE) == 1); // ran, then faulted: not a refusal
    CHECK(c.r[R_EAX] == 0 && c.r[R_ESP] == esp && c.eip == RETURN);
    CHECK(rd32(esp - 8) == 0xaabbccdd); // the loop wrote nothing
    CHECK(strstr(interp_last_error(), "no RET within 1048576 instructions") != nullptr);
    CHECK(strstr(interp_last_error(), "01000000") != nullptr); // the guest address

    // A loop that pushes stops the same way, and the fault path puts ESP back
    // where the caller's return address left it however far the loop walked
    // it down. push imm32; jmp $.
    put(CODE, {0x68, 0x44, 0x33, 0x22, 0x11, 0xeb, 0xfe});
    c = fresh();
    wr32(esp - 4, RETURN);
    c.r[R_ESP] = esp - 4;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(c.r[R_ESP] == esp && c.eip == RETURN);
    CHECK(strstr(interp_last_error(), "no RET within") != nullptr);

    // A conditional loop on a register the caller never set counts the same.
    // dec eax; test eax,eax; jnz <start>; ret
    put(CODE, {0x48, 0x85, 0xc0, 0x75, 0xfb, 0xc3});
    c = fresh();
    wr32(c.r[R_ESP] - 4, RETURN);
    c.r[R_ESP] -= 4;
    c.r[R_EAX] = 0; // 0 -> -1 -> ... : 2^32 iterations, well past the budget
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(strstr(interp_last_error(), "no RET within") != nullptr);

    // The budget is per interp_call, so the next call gets a whole one: a
    // routine that stopped once must not poison the routines after it.
    put(CODE, {0x40, 0xc3}); // inc eax; ret
    c = fresh();
    wr32(c.r[R_ESP] - 4, RETURN);
    c.r[R_ESP] -= 4;
    c.r[R_EAX] = 7;
    CHECK(interp_call(&c, CODE) == 1);
    CHECK(c.r[R_EAX] == 8 && c.eip == RETURN);
}

// ---- lazy arithmetic flag descriptor (x86.h) --------------------------
//
// x86_cc_settle materialises a pending ADD/SUB/LOGIC/INC/DEC descriptor. Its
// recipes must match the eager ones the decoded emitter and the interpreter
// use, for every operation and operand width. Compare them over random
// operands rather than the fixed register table the interpreter fixtures use.
struct EagerFlags {
    uint32_t cf, pf, af, zf, sf, of;
};

static uint32_t width_mask(int bits) {
    return bits == 32 ? 0xffffffffu : (1u << bits) - 1u;
}

// CMP and TEST share the SUB and LOGIC descriptor kinds, so this covers all
// six integer flag producers. Flags an operation does not define keep the
// incoming field, matching the eager publication.
static EagerFlags eager_flags(int op, int bits, uint32_t a, uint32_t b, const EagerFlags &before) {
    const uint32_t m = width_mask(bits), sign = 1u << (bits - 1);
    EagerFlags f = before;
    a &= m;
    b &= m;
    uint32_t r = 0;
    switch (op) {
    case X86_CC_ADD:
        r = (a + b) & m;
        f.cf = r < a;
        f.of = (((a ^ r) & (b ^ r)) >> (bits - 1)) & 1u;
        f.af = ((a ^ b ^ r) >> 4) & 1u;
        f.zf = r == 0;
        f.sf = (r >> (bits - 1)) & 1u;
        f.pf = parity8(r);
        break;
    case X86_CC_SUB:
        r = (a - b) & m;
        f.cf = a < b;
        f.of = (((a ^ b) & (a ^ r)) >> (bits - 1)) & 1u;
        f.af = ((a ^ b ^ r) >> 4) & 1u;
        f.zf = r == 0;
        f.sf = (r >> (bits - 1)) & 1u;
        f.pf = parity8(r);
        break;
    case X86_CC_LOGIC:
        r = a & b;
        f.cf = 0;
        f.of = 0; // AF is preserved.
        f.zf = r == 0;
        f.sf = (r >> (bits - 1)) & 1u;
        f.pf = parity8(r);
        break;
    case X86_CC_INC:
        r = (a + 1u) & m;
        f.of = r == sign;
        f.af = ((a ^ r) >> 4) & 1u; // CF is preserved.
        f.zf = r == 0;
        f.sf = (r >> (bits - 1)) & 1u;
        f.pf = parity8(r);
        break;
    case X86_CC_DEC:
        r = (a - 1u) & m;
        f.of = r == (sign - 1u);
        f.af = ((a ^ r) >> 4) & 1u; // CF is preserved.
        f.zf = r == 0;
        f.sf = (r >> (bits - 1)) & 1u;
        f.pf = parity8(r);
        break;
    default:
        break;
    }
    return f;
}

static uint32_t g_flag_seed = 0x9e3779b9u;
static uint32_t flag_random(void) {
    g_flag_seed ^= g_flag_seed << 13;
    g_flag_seed ^= g_flag_seed >> 17;
    g_flag_seed ^= g_flag_seed << 5;
    return g_flag_seed;
}

static void test_lazy_flags() {
    static const uint8_t ops[] = {X86_CC_ADD, X86_CC_SUB, X86_CC_LOGIC, X86_CC_INC, X86_CC_DEC};
    static const uint8_t masks[] = {0x3fu, 0x3fu, 0x3bu, 0x3eu, 0x3eu};
    for (int bits = 8; bits <= 32; bits *= 2) {
        for (unsigned oi = 0; oi < sizeof ops / sizeof *ops; ++oi) {
            for (int t = 0; t < 4096; ++t) {
                EagerFlags before = {flag_random() & 1u, flag_random() & 1u, flag_random() & 1u,
                                     flag_random() & 1u, flag_random() & 1u, flag_random() & 1u};
                X86 c;
                memset(&c, 0, sizeof c);
                c.eflags_cf = before.cf;
                c.eflags_pf = before.pf;
                c.eflags_af = before.af;
                c.eflags_zf = before.zf;
                c.eflags_sf = before.sf;
                c.eflags_of = before.of;
                const uint32_t a = flag_random();
                const uint32_t b = flag_random();
                const uint32_t m = width_mask(bits);
                const uint32_t xa = a & m;
                const uint32_t xb = (ops[oi] == X86_CC_INC || ops[oi] == X86_CC_DEC) ? 1u : (b & m);
                c.cc_op = ops[oi];
                c.cc_size = (uint8_t)(bits / 8);
                c.cc_mask = masks[oi];
                c.cc_a = a;
                c.cc_b = (ops[oi] == X86_CC_INC || ops[oi] == X86_CC_DEC) ? 1u : b;
                if (ops[oi] == X86_CC_ADD)
                    c.cc_res = (xa + xb) & m;
                else if (ops[oi] == X86_CC_SUB)
                    c.cc_res = (xa - xb) & m;
                else if (ops[oi] == X86_CC_LOGIC)
                    c.cc_res = xa & xb;
                else if (ops[oi] == X86_CC_INC)
                    c.cc_res = (xa + 1u) & m;
                else
                    c.cc_res = (xa - 1u) & m;
                // Capture the expected result before x86_cc_settle clears the
                // descriptor fields it was built from.
                const EagerFlags want = eager_flags(ops[oi], bits, a, c.cc_b, before);
                x86_cc_settle(&c);
                CHECK(c.cc_op == X86_CC_NONE);
                CHECK(c.eflags_cf == want.cf && c.eflags_pf == want.pf && c.eflags_af == want.af &&
                      c.eflags_zf == want.zf && c.eflags_sf == want.sf && c.eflags_of == want.of);
            }
        }
    }
    // A partial mask writes only the named fields and leaves the rest.
    X86 c;
    memset(&c, 0, sizeof c);
    c.eflags_cf = 1;
    c.eflags_af = 1;
    c.eflags_of = 1;
    c.eflags_sf = 1;
    c.cc_op = X86_CC_SUB;
    c.cc_size = 4;
    c.cc_mask = X86_CCF_ZF;
    c.cc_a = 5;
    c.cc_b = 5;
    c.cc_res = 0;
    x86_cc_settle(&c);
    CHECK(c.eflags_zf == 1 && c.eflags_cf == 1 && c.eflags_af == 1 && c.eflags_of == 1 &&
          c.eflags_sf == 1 && c.eflags_pf == 0);
    // x86_get_eflags settles first, so PUSHFD cannot read a stale field while a
    // descriptor is pending.
    memset(&c, 0, sizeof c);
    c.eflags_misc = 0x202;
    c.cc_op = X86_CC_SUB;
    c.cc_size = 4;
    c.cc_mask = 0x3fu;
    c.cc_a = 5;
    c.cc_b = 7;
    c.cc_res = 5u - 7u;
    const EagerFlags want = eager_flags(X86_CC_SUB, 32, c.cc_a, c.cc_b, EagerFlags{});
    const uint32_t eflags = x86_get_eflags(&c);
    CHECK(c.cc_op == X86_CC_NONE);
    CHECK(((eflags >> 0) & 1u) == want.cf);
    CHECK(((eflags >> 2) & 1u) == want.pf);
    CHECK(((eflags >> 4) & 1u) == want.af);
    CHECK(((eflags >> 6) & 1u) == want.zf);
    CHECK(((eflags >> 7) & 1u) == want.sf);
    CHECK(((eflags >> 11) & 1u) == want.of);
}

// The state test_interp_unicorn.py mirrors: registers from a fixed table,
// ESI and EBX pointing into a data page filled with a pattern.
static int run_hex(const char *hex) {
    std::vector<uint8_t> code;
    for (const char *p = hex; p[0] && p[1]; p += 2)
        code.push_back((uint8_t)strtoul(std::string(p, 2).c_str(), nullptr, 16));
    put(CODE, code);
    for (uint32_t i = 0; i < 0x1000; ++i)
        g_mem[DATA + i] = (uint8_t)(i * 37 + 11);
    X86 c = fresh();
    const uint32_t regs[8] = {0x12345678, 0x80000000, 0x7fffffff,   DATA + 0x800,
                              0,          0xfffffffe, DATA + 0x400, 3};
    for (int i = 0; i < 8; ++i)
        if (i != R_ESP)
            c.r[i] = regs[i];
    c.r[R_ESP] = STACK - 0x100;
    wr32(c.r[R_ESP], RETURN);
    if (!interp_call(&c, CODE)) {
        printf("error %s\n", interp_last_error());
        return 1;
    }
    printf("eax=%08x ecx=%08x edx=%08x ebx=%08x esp=%08x ebp=%08x esi=%08x edi=%08x eip=%08x\n",
           c.r[0], c.r[1], c.r[2], c.r[3], c.r[4], c.r[5], c.r[6], c.r[7], c.eip);
    printf("cf=%u zf=%u sf=%u of=%u\n", c.eflags_cf, c.eflags_zf, c.eflags_sf, c.eflags_of);
    printf("data=");
    for (uint32_t i = 0; i < 0x1000; ++i)
        printf("%02x", g_mem[DATA + i]);
    printf("\n");
    return 0;
}

#ifndef _WIN32
extern "C" void watchdog(int) {
    const char msg[] = "FAIL interp_tests hung: a routine ran without a step budget\n";
    ssize_t ignored = write(2, msg, sizeof msg - 1);
    (void)ignored;
    _exit(1);
}
#endif

int main(int argc, char **argv) {
    g_mem = (uint8_t *)os_vm_reserve_at(RECOMP_ARENA, GUEST_SIZE);
    if (!g_mem)
        return 1;
    if (argc == 3 && !strcmp(argv[1], "--run"))
        return run_hex(argv[2]);
    // Without the interpreter's step budget the checks in test_step_budget do
    // not fail, they never come back; the watchdog turns that into a failure.
    // The whole suite inside the budget is a fraction of a second.
#ifndef _WIN32
    signal(SIGALRM, watchdog);
    alarm(30);
#endif
    test_bank_routine();
    test_tail_call_out();
    test_refusals();
    test_step_budget();
    test_lazy_flags();
    printf("interp: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
