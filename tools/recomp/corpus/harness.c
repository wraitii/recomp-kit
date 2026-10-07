/* Mapped normal-exit comparisons. Translation variants match full CPU and the
 * row's declared guest memory ranges; the native reference matches only the
 * fixture's declared observable results. Timings include a common entry reset,
 * indirect call and native adapter. Optional boundary hooks bracket every
 * variant and every timed workload for the fixture's coverage accounting. */
#include "x86.h"
#include "platform/os.h"
#include "corpus-config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Experimental ceiling column only (see CORPUS_MODE_CEILING below). A fixture's
 * boundary hooks may consult these: while corpus_relaxed_boundaries is set they
 * count disagreements in corpus_relaxed_boundary_mismatches instead of aborting. */
int corpus_relaxed_boundaries;
unsigned corpus_relaxed_boundary_mismatches;

/* msvc_x87_convention: the combined SSA variant leaves
 * popped x87 residue unpublished at calls and returns. That mode, and boundary
 * hooks while it runs, compare canonical copies with the value/bits/exact of
 * empty registers and the bits of registers whose exact flag is clear (no
 * helper reads them). Flags, TOP, tags, status and every live register still
 * compare. -1 when not relaxed. */
#if defined(CORPUS_MODE_CONVENTION)
int corpus_convention_mode = CORPUS_MODE_CONVENTION;
#else
int corpus_convention_mode = -1;
#endif
void corpus_convention_canonical(X86 *c) {
    for (unsigned i = 0; i < 8; ++i) {
        if (ftag_of(c, i) == FTAG_EMPTY) {
            c->st[i] = 0;
            c->st_exact[i] = 0;
        }
        if (!c->st_exact[i])
            c->st_bits[i] = 0;
    }
}
uint8_t *g_mem;
uint32_t g_watch_base, g_watch_len, g_dirty_count, g_store_hook;
RecompDirty g_dirty[RECOMP_DIRTY_SLOTS];
void recomp_watch_hit(uint32_t a, uint32_t n, uint64_t v) {
    (void)a;
    (void)n;
    (void)v;
    abort();
}

#if defined(CORPUS_BOUNDARY_HOOKS)
void corpus_variant_begin(unsigned fixture, unsigned mode, int checking);
void corpus_variant_end(unsigned fixture);
void corpus_validation_end(unsigned fixture, unsigned checks);
#define CORPUS_HOOKS 1
#else
#define CORPUS_HOOKS 0
#endif

static volatile uint32_t checksum;

static void fail(unsigned row, unsigned input, unsigned mode, const char *reason) {
    fprintf(stderr, "%s input %u mode %u: %s\n", corpus_names[row], input, mode, reason);
    exit(1);
}

static size_t range_total(unsigned row) {
    size_t total = 0;
    for (unsigned i = 0; i < corpus_memory_range_counts[row]; ++i) {
        const struct corpus_range *range = &corpus_memory_ranges[row][i];
        if (range->start > CORPUS_MEMORY_SIZE || range->size > CORPUS_MEMORY_SIZE - range->start)
            fail(row, 0, 0, "snapshot range outside guest arena");
        total += range->size;
    }
    return total;
}

/* Snapshot the concatenated per-row guest ranges in declaration order. */
static void snapshot(unsigned row, uint8_t *out) {
    size_t offset = 0;
    const struct corpus_range *ranges = corpus_memory_ranges[row];
    for (unsigned i = 0; i < corpus_memory_range_counts[row]; ++i) {
        memcpy(out + offset, g_mem + ranges[i].start, ranges[i].size);
        offset += ranges[i].size;
    }
}

/* First differing CPU bytes and guest-range bytes on a translation mismatch.
 * This is a diagnostic only; the checks themselves remain full memcmp. */
static void report_diff(const X86 *a, const X86 *b, unsigned row, const uint8_t *want) {
    const unsigned char *pa = (const unsigned char *)a, *pb = (const unsigned char *)b;
    unsigned shown = 0;
    for (size_t i = 0; i < sizeof *a && shown < 8; ++i) {
        if (pa[i] != pb[i]) {
            fprintf(stderr, "  cpu+0x%03zx: eager=%02x actual=%02x\n", i, pa[i], pb[i]);
            ++shown;
        }
    }
    size_t offset = 0;
    const struct corpus_range *ranges = corpus_memory_ranges[row];
    for (unsigned i = 0; i < corpus_memory_range_counts[row] && shown < 16; ++i) {
        for (uint32_t j = 0; j < ranges[i].size && shown < 16; ++j) {
            if (want[offset + j] != g_mem[ranges[i].start + j]) {
                fprintf(stderr, "  guest+0x%08x: eager=%02x actual=%02x\n", ranges[i].start + j,
                        want[offset + j], g_mem[ranges[i].start + j]);
                ++shown;
            }
        }
        offset += ranges[i].size;
    }
}

#if defined(CORPUS_MODE_CEILING)
/* UNPROVEN SSA ceiling column (corpus-only; see tools/recomp/ir/ceiling.py).
 * Full-state equality with eager is expected to fail, so the variant is judged
 * only on declared observations and mismatches are counted, never fatal.
 * Native rows use the fixture's declared native observations; translation-only
 * rows compare the declared guest ranges (excluding stack residue below the
 * final ESP), EAX and the ST0 return against eager. */
#ifndef CORPUS_CEILING_STACK_WINDOW
#define CORPUS_CEILING_STACK_WINDOW 0x1000u
#endif
struct ceiling_row {
    unsigned checked, skipped, observation, memory, eax, st0, boundary;
    int first_input;
    char reason[112];
    int bench_invalid;
};
static struct ceiling_row ceiling_rows[CORPUS_COUNT];

static void ceiling_note(struct ceiling_row *r, unsigned input, const char *what) {
    if (r->first_input < 0) {
        r->first_input = (int)input;
        snprintf(r->reason, sizeof r->reason, "%s", what);
    }
}

static int same_double(double a, double b) {
    return !memcmp(&a, &b, sizeof a) || (a != a && b != b);
}

static void ceiling_check(unsigned row, unsigned input, const X86 *entry, const X86 *eager,
                          const uint8_t *expected, uint8_t *actual_mem) {
    struct ceiling_row *r = &ceiling_rows[row];
    unsigned fixture = corpus_fixture_ids[row];
    X86 actual;
#if defined(CORPUS_CEILING_E_DOMAIN)
    /* Relaxation E assumes PC=00, RC=nearest, masked exceptions. */
    if ((entry->fpu_cw & 0x3fu) != 0x3fu || (entry->fpu_cw & 0x0f00u)) {
        r->skipped++;
        return;
    }
#endif
    corpus_relaxed_boundaries = 1;
    corpus_relaxed_boundary_mismatches = 0;
#if CORPUS_HOOKS
    corpus_variant_begin(fixture, CORPUS_MODE_CEILING, 1);
#endif
    corpus_setup(fixture, input, &actual);
    corpus_functions[row][CORPUS_MODE_CEILING](&actual);
#if CORPUS_HOOKS
    corpus_variant_end(fixture);
#endif
    corpus_relaxed_boundaries = 0;
    r->checked++;
    if (corpus_relaxed_boundary_mismatches) {
        r->boundary++;
        ceiling_note(r, input, "call boundary state/count differs");
    }
    if (corpus_has_native[row]) {
        if (!corpus_observable_equal(fixture, eager, &actual, expected)) {
            r->observation++;
            ceiling_note(r, input, "declared native observation differs");
        }
        return;
    }
    snapshot(row, actual_mem);
    uint32_t final_esp = eager->r[R_ESP];
    uint32_t low = entry->r[R_ESP] > CORPUS_CEILING_STACK_WINDOW
                       ? entry->r[R_ESP] - CORPUS_CEILING_STACK_WINDOW
                       : 0;
    size_t offset = 0;
    int memory_bad = 0;
    const struct corpus_range *ranges = corpus_memory_ranges[row];
    for (unsigned i = 0; i < corpus_memory_range_counts[row] && !memory_bad; ++i) {
        for (uint32_t j = 0; j < ranges[i].size; ++j) {
            uint32_t address = ranges[i].start + j;
            if (address >= low && address < final_esp)
                continue; /* stack residue below the final ESP */
            if (expected[offset + j] != actual_mem[offset + j]) {
                char what[112];
                snprintf(what, sizeof what, "guest memory %08x eager=%02x ceiling=%02x", address,
                         expected[offset + j], actual_mem[offset + j]);
                ceiling_note(r, input, what);
                memory_bad = 1;
                break;
            }
        }
        offset += ranges[i].size;
    }
    r->memory += memory_bad;
    if (eager->r[R_EAX] != actual.r[R_EAX]) {
        r->eax++;
        ceiling_note(r, input, "EAX differs");
    }
    if (eager->fpu_top != actual.fpu_top) {
        r->st0++;
        ceiling_note(r, input, "x87 TOP differs");
    } else if (((entry->fpu_top - eager->fpu_top) & 7u) == 1u &&
               !same_double(eager->st[eager->fpu_top], actual.st[actual.fpu_top])) {
        r->st0++;
        ceiling_note(r, input, "ST0 return differs");
    }
}
#endif

static void validate(unsigned count) {
    /* corpus_is_boundary is an annotation for readers of the generated config;
     * boundary hooks bracket every variant regardless of the row's kind. */
    (void)corpus_is_boundary;
    /* Snapshot windows are game-sized; keep them off the stack. */
    uint8_t *expected = malloc(CORPUS_MAX_SNAPSHOT);
    uint8_t *actual_mem = malloc(CORPUS_MAX_SNAPSHOT);
    if (!expected || !actual_mem) {
        fprintf(stderr, "corpus: snapshot allocation failed\n");
        exit(1);
    }
#if defined(CORPUS_MODE_CEILING)
    for (unsigned row = 0; row < CORPUS_COUNT; ++row)
        ceiling_rows[row].first_input = -1;
#endif
    for (unsigned row = 0; row < CORPUS_COUNT; ++row) {
        unsigned fixture = corpus_fixture_ids[row];
        unsigned modes = corpus_row_modes[row];
        unsigned native = corpus_has_native[row];
        size_t bytes = range_total(row);
        for (unsigned input = 0; input < count; ++input) {
            X86 eager, actual, entry;
#if CORPUS_HOOKS
            /* The variant mode must be current before setup: setup calls
             * corpus_boundary_case_begin and expects the active mode. */
            corpus_variant_begin(fixture, 0, 1);
#endif
            corpus_setup(fixture, input, &eager);
            entry = eager;
            corpus_functions[row][0](&eager);
#if CORPUS_HOOKS
            corpus_variant_end(fixture);
#endif
            snapshot(row, expected);
            for (unsigned mode = 1; mode < modes; ++mode) {
#if defined(CORPUS_MODE_CEILING)
                if (mode == CORPUS_MODE_CEILING) {
                    ceiling_check(row, input, &entry, &eager, expected, actual_mem);
                    continue;
                }
#endif
#if CORPUS_HOOKS
                corpus_variant_begin(fixture, mode, 1);
#endif
                corpus_setup(fixture, input, &actual);
                corpus_functions[row][mode](&actual);
#if CORPUS_HOOKS
                corpus_variant_end(fixture);
#endif
                if (native && mode == CORPUS_MODE_NATIVE) {
                    if (!corpus_observable_equal(fixture, &eager, &actual, expected))
                        fail(row, input, mode, "native observable result mismatch");
                } else {
                    snapshot(row, actual_mem);
                    X86 want = eager;
                    if ((int)mode == corpus_convention_mode) {
                        corpus_convention_canonical(&want);
                        corpus_convention_canonical(&actual);
                    }
                    if (memcmp(&want, &actual, sizeof actual) ||
                        memcmp(expected, actual_mem, bytes)) {
                        report_diff(&want, &actual, row, expected);
                        fail(row, input, mode, "translated full CPU/memory mismatch");
                    }
                }
            }
        }
#if CORPUS_HOOKS
        corpus_validation_end(fixture, count);
#endif
        printf("CHECK %u %u\n", row, count);
#if defined(CORPUS_MODE_CEILING)
        printf("CEILING %u %u %u %u %u %u %u %u %d %s\n", row, ceiling_rows[row].checked,
               ceiling_rows[row].skipped, ceiling_rows[row].observation, ceiling_rows[row].memory,
               ceiling_rows[row].eax, ceiling_rows[row].st0, ceiling_rows[row].boundary,
               ceiling_rows[row].first_input, ceiling_rows[row].reason);
#endif
    }
    free(expected);
    free(actual_mem);
}

static double trial(unsigned row, unsigned mode, unsigned calls) {
    X86 c;
    unsigned fixture = corpus_fixture_ids[row];
    void (*function)(X86 *) = corpus_functions[row][mode];
#if CORPUS_HOOKS
    corpus_variant_begin(fixture, mode, 0);
#endif
    corpus_setup(fixture, CORPUS_BENCH_INPUT, &c);
    /* Warm the exact workload; setup is untimed and repeated for each trial. */
    for (unsigned n = 0; n < 1000; ++n) {
        corpus_reset_entry(fixture, &c);
        function(&c);
    }
    corpus_setup(fixture, CORPUS_BENCH_INPUT, &c);
    clock_t start = clock();
    for (unsigned n = 0; n < calls; ++n) {
        corpus_reset_entry(fixture, &c);
        function(&c);
    }
    clock_t end = clock();
#if CORPUS_HOOKS
    corpus_variant_end(fixture);
#endif
    checksum ^= c.r[R_EAX] ^ rd32(CORPUS_SCRATCH);
    if (!corpus_bench_valid(fixture, &c)) {
#if defined(CORPUS_MODE_CEILING)
        if (mode == CORPUS_MODE_CEILING) {
            /* Experimental column: record, do not abort the whole run. */
            fprintf(stderr, "%s: ceiling post-timing sanity check failed\n", corpus_names[row]);
            ceiling_rows[row].bench_invalid = 1;
            return (double)(end - start) * 1e9 / CLOCKS_PER_SEC / calls;
        }
#endif
        fail(row, 0, mode, "post-timing sanity check failed");
    }
    return (double)(end - start) * 1e9 / CLOCKS_PER_SEC / calls;
}

/* Pick one call count per row so every variant and trial runs the same work.
 * The eager variant is the slowest translated variant, so doubling its batch
 * until it takes at least the budget leaves every other variant at or above
 * the budget too. Calibration is untimed for reporting and runs before any
 * trial of the row; a clock too coarse to see the batch just keeps doubling. */
#define CORPUS_CALIBRATION_START 1024u
#define CORPUS_CALIBRATION_LIMIT (1u << 30)

static unsigned calibrate(unsigned row, double budget_ms) {
    double budget_ns = budget_ms * 1e6;
    unsigned calls = CORPUS_CALIBRATION_START;
    while (calls < CORPUS_CALIBRATION_LIMIT && trial(row, 0, calls) * calls < budget_ns)
        calls *= 2;
    return calls;
}

int main(int argc, char **argv) {
    if (argc != 6)
        return 2;
    g_mem = os_vm_reserve_at(RECOMP_ARENA, CORPUS_MEMORY_SIZE);
    if (!g_mem)
        return 2;
    FILE *image = fopen(argv[1], "rb");
    size_t image_size = strtoul(argv[2], NULL, 10);
    if (!image || CORPUS_IMAGE_BASE + image_size > CORPUS_MEMORY_SIZE ||
        fread(g_mem + CORPUS_IMAGE_BASE, 1, image_size, image) != image_size)
        return 2;
    fclose(image);
    unsigned checks = strtoul(argv[3], NULL, 10);
    double trial_ms = strtod(argv[4], NULL); /* 0 = correctness only, no timing */
    unsigned trials = strtoul(argv[5], NULL, 10);
    validate(checks);
    if (trial_ms > 0)
        for (unsigned row = 0; row < CORPUS_COUNT; ++row) {
            unsigned calls = calibrate(row, trial_ms);
            printf("CALLS %u %u\n", row, calls);
            unsigned modes = corpus_row_modes[row];
            unsigned total = modes + (corpus_has_native[row] ? 1 : 0);
            unsigned fixture = corpus_fixture_ids[row];
            for (unsigned t = 0; t < trials; ++t)
                for (unsigned offset = 0; offset < total; ++offset) {
                    unsigned mode = (offset + t) % total;
                    double ns;
                    if (mode == modes) {
#if CORPUS_HOOKS
                        corpus_variant_begin(fixture, mode, 0);
#endif
                        corpus_native_trial(fixture, 1000);
                        ns = corpus_native_trial(fixture, calls);
#if CORPUS_HOOKS
                        corpus_variant_end(fixture);
#endif
                    } else
                        ns = trial(row, mode, calls);
                    printf("TIME %u %u %u %.6f\n", row, mode, t, ns);
                }
        }
#if defined(CORPUS_MODE_CEILING)
    for (unsigned row = 0; row < CORPUS_COUNT; ++row)
        printf("CEILING_BENCH %u %d\n", row, ceiling_rows[row].bench_invalid ? 0 : 1);
#endif
    os_vm_release(g_mem, CORPUS_MEMORY_SIZE);
    return 0;
}
