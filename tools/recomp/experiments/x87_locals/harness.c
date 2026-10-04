/* Fragment microbenchmark, not a game replay or original-x86 oracle.
 * Inputs stay mapped; faults, callbacks and intermediate observers are excluded.
 * All variants are separately compiled from this harness, without LTO.
 */
#include "x86.h"
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include "fixtures.h"

uint8_t *g_mem;
uint32_t g_watch_base, g_watch_len, g_dirty_count, g_store_hook;
RecompDirty g_dirty[RECOMP_DIRTY_SLOTS];
void recomp_watch_hit(uint32_t a, uint32_t n, uint64_t v) {
    (void)a;
    (void)n;
    (void)v;
    abort();
}

#ifndef FIXTURE_MEMORY_SIZE
#define FIXTURE_MEMORY_SIZE 0x20000
#endif
#ifndef FIXTURE_SETUP
#define FIXTURE_SETUP(c, n) ((void)0)
#define FIXTURE_RESET(c) ((void)0)
#define FIXTURE_BEFORE(mode) ((void)0)
#define FIXTURE_AFTER(mode) ((void)0)
#endif
#ifndef FIXTURE_CUSTOM_INPUTS
#define FIXTURE_CUSTOM_INPUTS 0
#endif
#ifndef FIXTURE_FINISH
#define FIXTURE_FINISH() ((void)0)
#endif

static uint32_t seed = 123456789;
static uint32_t random_word(void) {
    seed ^= seed << 13;
    seed ^= seed >> 17;
    seed ^= seed << 5;
    return seed;
}

static void setup(X86 *c, unsigned n) {
    memset(c, 0, sizeof *c);
    for (unsigned j = 0; j < 8; ++j) {
        c->r[j] = random_word();
        c->st[j] = (double)(int32_t)random_word();
        c->st_bits[j] = ((uint64_t)random_word() << 32) | random_word();
        c->st_exact[j] = j & 1;
    }
    c->fpu_top = n & 7;
    c->fpu_cw = 0x7f | (((n >> 3) & 15) << 8);
    c->fpu_sw = (uint16_t)random_word();
    c->fpu_tag = 0xffff;
    c->r[R_ESI] = 0x10000;
    c->r[R_EDI] = 0x10040;
    /* Exercise output/input aliasing as well as unaligned destinations. */
    c->r[R_EBX] = (n & 1) ? 0x10000 : ((n & 2) ? 0x10081 : 0x10080);
    const uint32_t special[] = {0,          0x80000000, 0x7f800000, 0xff800000, 0x7fc12345,
                                0x7f812345, 1,          0x007fffff, 0x7f7fffff, 0x3f800000};
    memset(g_mem + 0x10000, 0, 256);
    for (unsigned j = 0; j < 7; ++j) {
        uint32_t a = j < 3 ? 0x10000 + 4 * j : 0x10040 + 4 * (j - 3);
        uint32_t bits;
        if ((n / 128) % 3 == 0) {
            bits = special[random_word() % (sizeof special / sizeof *special)];
        } else if ((n / 128) % 3 == 1) {
            float v = (float)(int32_t)random_word() / 1234567.0f;
            memcpy(&bits, &v, 4);
        } else {
            bits = random_word();
        }
        memcpy(g_mem + a, &bits, 4);
    }
    FIXTURE_SETUP(c, n);
}

/* Only the live/relaxed experimental contracts discard empty-slot contents.
 * Tags, TOP, status, exact-integer flags and all remaining state still compare.
 */
static void discard_empty_contents(X86 *c) {
    for (unsigned i = 0; i < 8; ++i)
        if (ftag_of(c, i) == FTAG_EMPTY) {
            c->st[i] = 0;
            c->st_bits[i] = 0;
        }
}

static int compare(void) {
    const unsigned cases = sizeof functions / sizeof *functions;
    for (unsigned f = 0; f < cases; ++f) {
        unsigned differences[4] = {0}, memory_differences = 0;
        unsigned relaxed_by_pc[4] = {0}, finite_differences = 0;
        for (unsigned n = 0; n < 24576; ++n) {
            X86 initial, expected;
            uint8_t input[256], output[256];
            setup(&initial, n);
            memcpy(input, g_mem + 0x10000, 256);
            expected = initial;
            FIXTURE_BEFORE(0);
            functions[f][0](&expected);
            FIXTURE_AFTER(0);
            memcpy(output, g_mem + 0x10000, 256);
            for (unsigned mode = 1; mode < 4; ++mode) {
                X86 actual = initial, reference = expected;
                memcpy(g_mem + 0x10000, input, 256);
                FIXTURE_BEFORE(mode);
                functions[f][mode](&actual);
                FIXTURE_AFTER(mode);
                int mem_diff = memcmp(output, g_mem + 0x10000, 256) != 0;
                if (normalize_empty_mask & (1u << mode)) {
                    discard_empty_contents(&actual);
                    discard_empty_contents(&reference);
                }
                int different = mem_diff || memcmp(&actual, &reference, sizeof actual);
                differences[mode] += !!different;
                if (mode == 3) {
                    memory_differences += mem_diff;
                    relaxed_by_pc[(initial.fpu_cw >> 8) & 3] += !!different;
                    if ((n / 128) % 3 == 1)
                        finite_differences += !!different;
                }
                if ((required_match_mask & (1u << mode)) && different) {
                    fprintf(stderr, "FAIL %s mode=%u seed=%u memory=%d\n", case_names[f], mode, n,
                            mem_diff);
                    return 1;
                }
            }
        }
        printf("CHECK %s 24576 inputs: %s=%u %s=%u %s=%u (last variant memory=%u; "
               "PC00/01/10/11=%u/%u/%u/%u) differences\n",
               case_names[f], mode_names[1], differences[1], mode_names[2], differences[2],
               mode_names[3], differences[3], memory_differences, relaxed_by_pc[0],
               relaxed_by_pc[1], relaxed_by_pc[2], relaxed_by_pc[3]);
        if (!FIXTURE_CUSTOM_INPUTS)
            printf("FINITE %s last variant differences=%u/8192 ordinary finite inputs\n",
                   case_names[f], finite_differences);
    }
    return 0;
}

static int order_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static void benchmark(void) {
    const unsigned iterations = 1000000, trials = 9;
    for (unsigned f = 0; f < sizeof functions / sizeof *functions; ++f) {
        for (unsigned pc = 0; pc <= 2; pc += 2) {
            double samples[4][9];
            for (unsigned trial = 0; trial < trials; ++trial) {
                for (unsigned k = 0; k < 4; ++k) {
                    unsigned mode = (k + trial) % 4;
                    X86 c;
                    setup(&c, 0);
                    c.fpu_cw = 0x7f | (pc << 8);
                    c.r[R_EBX] = 0x10080;
                    for (unsigned j = 0; j < 3; ++j)
                        wrf32(0x10000 + j * 4, (float)(j + 1) / 3);
                    for (unsigned j = 0; j < 4; ++j)
                        wrf32(0x10040 + j * 4, (float)(j + 1) / 7);
                    void (*fn)(X86 *) = functions[f][mode];
                    clock_t start = clock();
                    for (unsigned i = 0; i < iterations; ++i) {
                        /* Restore entry TOP after the live-output fragment. */
                        c.fpu_top = 0;
                        FIXTURE_RESET(&c);
                        fn(&c);
                    }
                    samples[mode][trial] =
                        (double)(clock() - start) * 1e9 / CLOCKS_PER_SEC / iterations;
                    /* Observable checksum outside the timing region. */
                    if (c.fpu_top > 7 || !isfinite(rdf32(0x10080)))
                        abort();
                }
            }
            for (unsigned mode = 0; mode < 4; ++mode) {
                qsort(samples[mode], trials, sizeof(double), order_double);
                printf("BENCH %s PC=%u %s median=%.3f min=%.3f max=%.3f ns/fragment\n",
                       case_names[f], pc, mode_names[mode], samples[mode][trials / 2],
                       samples[mode][0], samples[mode][trials - 1]);
            }
        }
    }
}

int main(void) {
    g_mem = calloc(1, FIXTURE_MEMORY_SIZE);
    if (!g_mem)
        return 2;
    if (compare())
        return 1;
    FIXTURE_FINISH();
    benchmark();
    free(g_mem);
    return 0;
}
