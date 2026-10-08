// The mods test builder globs tests/*.cpp and is the only POPM_TESTING build.
#ifdef POPM_TESTING
#include "../native/replay.cpp"
#include "../native/tests/replay_tests.cpp"
#include "../../runtime/guest.h"
#include "../mods_internal.h"
#include "../../runtime/win32.h"
#include "../../runtime/memory.h"
#include "../native/page_track.h"
#include "../native/shim_capture.h"
#include <utility>
#include "../../platform/os.h"
namespace {
char capture_last_log[256];
PopModStatus capture_log(const PopModApi *, const char *message) {
    std::snprintf(capture_last_log, sizeof capture_last_log, "%s", message ? message : "");
    return POP_OK;
}
} // namespace
// Every configuration the capture fixture cannot trust must end with it
// refusing to install, because a hook that installed and then wrote nothing
// would look like a candidate that was never called.
MOD_TEST_SUITE(capture_fixture_fails_closed) {
    void *library = os_dlopen((mods_test_build_path("recomp/mods-fixtures/capture_mod") +
                               std::string(os_plugin_extension()))
                                  .c_str());
    MOD_CHECK(library != nullptr);
    if (!library)
        return;
    auto init =
        reinterpret_cast<PopModStatus (*)(const PopModApi *)>(os_dlsym(library, "pop_mod_init"));
    MOD_CHECK(init != nullptr);
    if (!init) {
        os_dlclose(library);
        return;
    }
    PopModApi api{};
    api.log = capture_log;

    // A host that was not built for testing: refused before anything is read.
    const char *saved = recomp_env("TESTING");
    std::string keep = saved ? saved : "";
    os_unsetenv("RECOMP_TESTING");
    os_setenv("RECOMP_CAPTURE_TARGET", "0x12340000");
    os_setenv("RECOMP_CAPTURE_OUT", mods_test_build_path("recomp/should-not-exist.json").c_str());
    capture_last_log[0] = 0;
    MOD_CHECK_EQ(init(&api), POP_E_STATE);
    MOD_CHECK(strstr(capture_last_log, "RECOMP_TESTING") != nullptr);

    // Configured for testing but told neither what to capture nor where.
    os_setenv("RECOMP_TESTING", keep.empty() ? "1" : keep.c_str());
    os_unsetenv("RECOMP_CAPTURE_TARGET");
    os_unsetenv("RECOMP_CAPTURE_OUT");
    capture_last_log[0] = 0;
    MOD_CHECK_EQ(init(&api), POP_E_STATE);
    MOD_CHECK(strstr(capture_last_log, "RECOMP_CAPTURE_TARGET") != nullptr);

    // A target that is neither an address nor a symbol. api.symbol is null
    // here, so the address form is the one this case can reach; the symbol
    // form is exercised by the capture run itself.
    os_setenv("RECOMP_CAPTURE_TARGET", "0xnot-an-address");
    os_setenv("RECOMP_CAPTURE_OUT", mods_test_build_path("recomp/should-not-exist.json").c_str());
    capture_last_log[0] = 0;
    MOD_CHECK_EQ(init(&api), POP_E_STATE);
    MOD_CHECK(strstr(capture_last_log, "not an address") != nullptr);
    os_unsetenv("RECOMP_CAPTURE_TARGET");
    os_unsetenv("RECOMP_CAPTURE_OUT");

    MOD_CHECK_EQ(os_dlclose(library), 0);
}
#endif
#ifdef POPM_TESTING
#include "../native/tests/page_track_tests.cpp"
#include "../native/tests/shim_capture_tests.cpp"
#include <algorithm>
// The corpus writer. Until this suite existed the only thing that exercised it
// was a live capture run, which is a poor place to discover that a field is
// missing or that a rejected capture left a file behind.
extern "C" {
int pop_capture_available(void);
int pop_capture_begin(uint32_t max_pages, uint32_t max_calls);
int pop_capture_write(const char *path, uint32_t target, const pop_cpu_v1 *entry,
                      const pop_cpu_v1 *exit_state, uint32_t live_flags, const char **why);
}
namespace {
std::string slurp(const char *path) {
    FILE *f = std::fopen(path, "rb");
    if (!f)
        return {};
    std::string out;
    char buf[4096];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, f)) > 0)
        out.append(buf, n);
    std::fclose(f);
    return out;
}
} // namespace
MOD_TEST_SUITE(capture_corpus_has_every_field_replay_needs) {
    os_setenv("RECOMP_TESTING", "1");
    mem_init();
    imports_init();
    MOD_CHECK(pop_capture_available() == 1);
    // A scratch directory of this suite's own, created empty for it, so the
    // corpus cannot be a leftover from an earlier run of the same test.
    std::string out_path = std::string(mod_test_dir("capture_corpus")) + "/corpus.json";
    const char *out = out_path.c_str();

    uint32_t tid = imports_resolve("KERNEL32.dll", "GetCurrentThreadId");
    MOD_CHECK(tid != 0);
    if (!tid)
        return;
    const size_t page = pop_pagetrack::page_size();
    const uint32_t written = 0x04000000u & ~(uint32_t)(page - 1);
    const uint32_t read_only = written + (uint32_t)page;
    // Seeded before the window, so the entry snapshot has something in it that
    // is not zero and the exit snapshot has something different.
    wr32(read_only, 0xfeedface);
    wr32(written, 0x11111111);

    pop_cpu_v1 entry;
    pop_cpu_v1_init(&entry);
    entry.target = 0x12340000;
    entry.esp = 0x03100000;

    MOD_CHECK(pop_capture_begin(64, 8) == 1);
    wr32(written, 0x22222222);
    volatile uint32_t seen = rd32(read_only);
    (void)seen;
    X86 c{};
    c.r[R_ESP] = entry.esp;
    wr32(entry.esp, 0x12345678);
    imports_dispatch(&c, tid);

    pop_cpu_v1 exit_state = entry;
    exit_state.eax = c.r[R_EAX];
    const char *why = "unset";
    MOD_CHECK_EQ(pop_capture_write(out, entry.target, &entry, &exit_state, 0, &why), 1);
    MOD_CHECK(why != nullptr && !*why);

    const std::string text = slurp(out);
    MOD_CHECK(!text.empty());
    // Everything pop_replay::validate and the replay itself read back.
    MOD_CHECK(text.find("\"version\": 1") != std::string::npos);
    MOD_CHECK(text.find("\"target\": 305397760") != std::string::npos);
    MOD_CHECK(text.find("\"arena_size\": 268435456") != std::string::npos);
    MOD_CHECK(text.find("\"live_flags\": 0") != std::string::npos);
    MOD_CHECK(text.find("\"entry\": {") != std::string::npos);
    MOD_CHECK(text.find("\"exit\": {") != std::string::npos);
    MOD_CHECK(text.find("\"pages\": [") != std::string::npos);
    MOD_CHECK(text.find("\"written\": true") != std::string::npos);
    MOD_CHECK(text.find("\"read\": true") != std::string::npos);
    MOD_CHECK(text.find("KERNEL32.dll!GetCurrentThreadId") != std::string::npos);
    // The FPU registers are written as hex floats, so they survive the trip.
    MOD_CHECK(text.find("\"0x0p+0\"") != std::string::npos);

    // And a rejected capture leaves no file at all, which is the property the
    // capture driver relies on to tell refusal from success.
    std::remove(out);
    uint32_t bad = imports_alloc_trampoline("test.dll", "corpus_side_effect", nullptr, 0);
    MOD_CHECK(pop_capture_begin(64, 8) == 1);
    c = X86{};
    c.r[R_ESP] = entry.esp;
    imports_dispatch(&c, bad);
    why = "unset";
    MOD_CHECK_EQ(pop_capture_write(out, entry.target, &entry, &exit_state, 0, &why), 0);
    MOD_CHECK(why != nullptr && strstr(why, "side-effecting") != nullptr);
    MOD_CHECK(slurp(out).empty());
}
#endif
#ifdef POPM_TESTING
// The whole loop, through a file, with shim calls in it.
//
// The review's point was that replay was disconnected at both ends: nothing
// read a serialized corpus, the only translated adapter hard-coded one address
// and ignored Seams, and every recorded seam in a test was fed in by hand. So
// this captures a run that makes real shim calls through imports_dispatch,
// writes the corpus to a file, loads it back with pop_replay::load, and
// replays it with the calls served out of the record at the dispatcher, so the
// shims themselves never run again.
//
// TWO DIFFERENT seams, not the same one twice. Two calls to one seam with the
// same arguments are indistinguishable in either order, so a reordering test
// built on them could not fail whatever the code did.
MOD_TEST_SUITE(capture_to_file_and_replay_with_intercepted_shims) {
    os_setenv("RECOMP_TESTING", "1");
    sched_set_guest_thread(true);
    mods_hooks_reset();
    mem_init();
    imports_init();

    uint32_t tid = imports_resolve("KERNEL32.dll", "GetCurrentThreadId");
    uint32_t ticks = imports_resolve("KERNEL32.dll", "GetTickCount");
    MOD_CHECK(tid != 0);
    MOD_CHECK(ticks != 0);
    if (!tid || !ticks)
        return;

    const size_t page = pop_pagetrack::page_size();
    const uint32_t stack = 0x07000000u & ~(uint32_t)(page - 1);
    pop_cpu_v1 entry;
    pop_cpu_v1_init(&entry);
    entry.target = 0x12340000;
    entry.esp = stack + (uint32_t)page / 2;
    wr32(entry.esp, 0x12345678);

    // Capture: the two seams, in this order, through the real dispatcher.
    MOD_CHECK_EQ(pop_capture_begin(64, 8), 1);
    X86 c{};
    c.r[R_ESP] = entry.esp;
    imports_dispatch(&c, tid);
    const uint32_t got_tid = c.r[R_EAX];
    c.r[R_ESP] = entry.esp;
    imports_dispatch(&c, ticks);
    const uint32_t got_ticks = c.r[R_EAX];
    pop_cpu_v1 exit_state = entry;
    exit_state.eax = got_tid + got_ticks;

    std::string out = std::string(mod_test_dir("capture_roundtrip")) + "/corpus.json";
    const char *why = "unset";
    MOD_CHECK_EQ(pop_capture_write(out.c_str(), entry.target, &entry, &exit_state, 0, &why), 1);

    // Everything replay needs has to survive the file.
    pop_replay::Capture loaded;
    std::string bad = pop_replay::load(out, &loaded);
    if (!bad.empty())
        std::fprintf(stderr, "load said: %s\n", bad.c_str());
    MOD_CHECK(bad.empty());
    MOD_CHECK_EQ(loaded.calls.size(), 2u);
    MOD_CHECK(!loaded.pages.empty());
    MOD_CHECK_EQ(loaded.entry.size, (uint32_t)sizeof(pop_cpu_v1));
    MOD_CHECK_EQ(loaded.page_size, (uint32_t)page);
    if (loaded.calls.size() != 2)
        return;
    MOD_CHECK(loaded.calls[0].function == "KERNEL32.dll!GetCurrentThreadId");
    MOD_CHECK(loaded.calls[1].function == "KERNEL32.dll!GetTickCount");
    MOD_CHECK_EQ(loaded.calls[0].result, (uint64_t)got_tid);
    MOD_CHECK_EQ(loaded.calls[1].result, (uint64_t)got_ticks);

    // Results the real shims never return, so a replay that reached the real
    // shim instead of the record would not produce this exit value.
    loaded.calls[0].result = 0x11110000u;
    loaded.calls[1].result = 0x00002222u;
    loaded.exit.eax = 0x11112222u;

    // A candidate that makes the same two calls, through the same dispatcher,
    // with the record served in the shims' place.
    auto order = [](uint32_t first, uint32_t second) {
        return [first, second](pop_cpu_v1 &cpu, uint8_t *arena, size_t arena_size,
                               pop_replay::Seams &seams) {
            std::string failure;
            uint32_t a = 0, b = 0;
            {
                pop_replay::ServeRecordedShims serving(seams);
                recomp_arena_swap(arena, arena_size);
                X86 x{};
                x.r[R_ESP] = cpu.esp;
                imports_dispatch(&x, first);
                a = x.r[R_EAX];
                x.r[R_ESP] = cpu.esp;
                imports_dispatch(&x, second);
                b = x.r[R_EAX];
                recomp_arena_swap(arena, arena_size);
                failure = serving.failed();
            }
            cpu.eax = a + b;
            if (!failure.empty())
                throw std::runtime_error(failure);
        };
    };

    const std::string right = pop_replay::run(loaded, order(tid, ticks));
    if (!right.empty())
        std::fprintf(stderr, "replay said: %s\n", right.c_str());
    MOD_CHECK(right.empty());

    // The same two calls in the other order: the sum is identical, so only the
    // sequence check can catch it. It must.
    const std::string wrong = pop_replay::run(loaded, order(ticks, tid));
    MOD_CHECK(!wrong.empty());
    MOD_CHECK(wrong.find("mismatch") != std::string::npos);

    // And a candidate that makes one call too few fails on the unused record.
    auto once = [tid](pop_cpu_v1 &cpu, uint8_t *arena, size_t arena_size,
                      pop_replay::Seams &seams) {
        pop_replay::ServeRecordedShims serving(seams);
        recomp_arena_swap(arena, arena_size);
        X86 x{};
        x.r[R_ESP] = cpu.esp;
        imports_dispatch(&x, tid);
        cpu.eax = 0x11112222u;
        recomp_arena_swap(arena, arena_size);
    };
    MOD_CHECK(!pop_replay::run(loaded, once).empty());
}
#endif
#ifdef POPM_TESTING

namespace {
std::pair<std::string, std::string> loader_rule_corpus(const char *suite) {
    os_setenv("RECOMP_TESTING", "1");
    sched_set_guest_thread(true);
    mods_hooks_reset();
    mem_init();
    imports_init();
    std::string path = std::string(mod_test_dir(suite)) + "/corpus.json";
    pop_cpu_v1 cpu;
    pop_cpu_v1_init(&cpu);
    cpu.target = cpu.eip = 0x12340000u;
    cpu.st[0] = -1.5;
    MOD_CHECK_EQ(pop_capture_begin(8, 8), 1);
    wr32(0x08000000u, 0x1234u);
    const char *why = nullptr;
    MOD_CHECK_EQ(pop_capture_write(path.c_str(), cpu.target, &cpu, &cpu, 0, &why), 1);
    pop_replay::Capture capture;
    MOD_CHECK(pop_replay::load(path, &capture).empty());
    MOD_CHECK_EQ(capture.entry.st[0] * 2, -3);
    return {path, slurp(path.c_str())};
}

std::string load_edited_corpus(const std::string &path, const std::string &text) {
    FILE *file = std::fopen(path.c_str(), "wb");
    MOD_CHECK(file != nullptr);
    if (!file)
        return "could not write test input";
    MOD_CHECK_EQ(std::fwrite(text.data(), 1, text.size(), file), text.size());
    std::fclose(file);
    pop_replay::Capture capture;
    return pop_replay::load(path, &capture);
}

void replace_loader_text(std::string &text, const std::string &from, const std::string &to) {
    const auto at = text.find(from);
    MOD_CHECK(at != std::string::npos);
    if (at != std::string::npos)
        text.replace(at, from.size(), to);
}
} // namespace

MOD_TEST_SUITE(replay_loader_requires_every_cpu_field) {
    auto [path, text] = loader_rule_corpus("loader_full_cpu");
    for (const char *cpu : {"entry", "exit"}) {
        const std::string key = std::string("\"") + cpu + "\": {";
        const auto start = text.find(key) + key.size();
        const auto end = text.find('}', start);
        std::string edited = text;
        edited.replace(start, end - start, "\"size\": " + std::to_string(sizeof(pop_cpu_v1)));
        MOD_CHECK(load_edited_corpus(path, edited).find("missing required fields") !=
                  std::string::npos);
        // Missing a single late field must also fail, not only size-only CPU.
        edited = text;
        const auto field = edited.find("\"fpu_tag\":", start);
        edited.erase(field, edited.find(',', field) + 1 - field);
        MOD_CHECK(load_edited_corpus(path, edited).find("missing required fields") !=
                  std::string::npos);
        edited = text;
        const auto st = edited.find(",\n    \"st\":", start);
        edited.erase(st, edited.find(']', st) + 1 - st);
        MOD_CHECK(load_edited_corpus(path, edited).find("missing required fields") !=
                  std::string::npos);
    }
}

MOD_TEST_SUITE(replay_loader_rejects_decimal_fpu_strings) {
    auto [path, text] = loader_rule_corpus("loader_hex_fpu");
    for (const char *bad : {"1.5", "0x1p+0junk", ""}) {
        std::string edited = text;
        replace_loader_text(edited, "\"-0x1.8p+0\"", std::string("\"") + bad + "\"");
        MOD_CHECK(load_edited_corpus(path, edited).find("not a hex float") != std::string::npos);
    }
}

MOD_TEST_SUITE(replay_loader_rejects_negative_numbers) {
    auto [path, text] = loader_rule_corpus("loader_unsigned");
    for (const char *bad : {"-1", "-0", "0.5", "1e0"}) {
        std::string edited = text;
        replace_loader_text(edited, "\"eax\": 0", std::string("\"eax\": ") + bad);
        MOD_CHECK(!load_edited_corpus(path, edited).empty());
    }
    replace_loader_text(text, "\"calls\": [",
                        "\"calls\": [{\"function\": \"KERNEL32.dll!GetTickCount\", \"arguments\": "
                        "[], \"result\": -1}");
    MOD_CHECK(load_edited_corpus(path, text).find("unsigned integer") != std::string::npos);
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_cpu_scalars) {
    auto [path, text] = loader_rule_corpus("loader_cpu_range");
    for (const char *snapshot : {"entry", "exit"}) {
        const auto start = text.find(std::string("\"") + snapshot + "\": {");
        const std::string field = "\"eax\": 0";
        const auto at = text.find(field, start);
        MOD_CHECK(at != std::string::npos);
        if (at == std::string::npos)
            continue;
        std::string edited = text;
        edited.replace(at, field.size(), "\"eax\": 4294967295");
        MOD_CHECK(load_edited_corpus(path, edited).empty());
        edited = text;
        edited.replace(at, field.size(), "\"eax\": 4294967296");
        MOD_CHECK(load_edited_corpus(path, edited) ==
                  "a CPU scalar exceeds the 32-bit unsigned range");
    }
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_page_addresses) {
    auto [path, text] = loader_rule_corpus("loader_address_range");
    // The low 32 bits still name the valid captured page, so geometry
    // validation cannot catch this after a truncating cast.
    replace_loader_text(text, "\"address\": 134217728", "\"address\": 4429185024");
    MOD_CHECK(load_edited_corpus(path, text) == "a page address exceeds the 32-bit unsigned range");
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_geometry) {
    auto [path, text] = loader_rule_corpus("loader_geometry_range");
    for (const auto &[key, value] :
         {std::pair<const char *, uint64_t>{"arena_size", GUEST_SIZE},
          std::pair<const char *, uint64_t>{"page_size", pop_pagetrack::page_size()}}) {
        std::string edited = text;
        const std::string field = std::string("\"") + key + "\": ";
        replace_loader_text(edited, field + std::to_string(value),
                            field + std::to_string(value + (uint64_t{1} << 32)));
        MOD_CHECK(load_edited_corpus(path, edited) ==
                  std::string(key) + " exceeds the 32-bit unsigned range");
    }
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_target_and_live_flags) {
    auto [path, text] = loader_rule_corpus("loader_metadata_range");
    for (const auto &[key, value] : {std::pair<const char *, uint64_t>{"target", 0x12340000u},
                                     std::pair<const char *, uint64_t>{"live_flags", 0}}) {
        std::string edited = text;
        const std::string field = std::string("\"") + key + "\": ";
        replace_loader_text(edited, field + std::to_string(value),
                            field + std::to_string(value + (uint64_t{1} << 32)));
        MOD_CHECK(load_edited_corpus(path, edited) ==
                  std::string(key) + " exceeds the 32-bit unsigned range");
    }
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_shim_arguments) {
    auto [path, text] = loader_rule_corpus("loader_argument_range");
    replace_loader_text(text, "\"calls\": [",
                        "\"calls\": [{\"function\": \"KERNEL32.dll!GetTickCount\", "
                        "\"arguments\": [4294967295], \"result\": 0}");
    MOD_CHECK(load_edited_corpus(path, text).empty());
    replace_loader_text(text, "[4294967295]", "[4294967296]");
    MOD_CHECK(load_edited_corpus(path, text) ==
              "a shim argument exceeds the 32-bit unsigned range");
}

MOD_TEST_SUITE(replay_loader_rejects_oversized_shim_results) {
    auto [path, text] = loader_rule_corpus("loader_result_range");
    replace_loader_text(text, "\"calls\": [",
                        "\"calls\": [{\"function\": \"KERNEL32.dll!GetTickCount\", "
                        "\"arguments\": [], \"result\": 4294967295}");
    MOD_CHECK(load_edited_corpus(path, text).empty());
    replace_loader_text(text, "\"result\": 4294967295", "\"result\": 4294967296");
    MOD_CHECK(load_edited_corpus(path, text) == "a shim result exceeds the 32-bit unsigned range");
}

MOD_TEST_SUITE(replay_loader_rejects_trailing_bytes) {
    auto [path, text] = loader_rule_corpus("loader_trailing");
    MOD_CHECK(load_edited_corpus(path, text + " \t\r\n").empty());
    for (const std::string suffix :
         {std::string("garbage"), std::string("{}"), std::string(1, '\0')})
        MOD_CHECK(load_edited_corpus(path, text + suffix) ==
                  "trailing bytes after the corpus object");
}
#endif
