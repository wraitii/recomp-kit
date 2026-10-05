/* Mapped normal-exit comparisons. Translation variants match full CPU/scratch;
 * the native reference matches only the fixture's declared observable results.
 * Timings include a common entry reset, indirect call and native adapter. */
#include "x86.h"
#include "corpus-config.h"
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

uint8_t *g_mem;
uint32_t g_watch_base, g_watch_len, g_dirty_count, g_store_hook;
RecompDirty g_dirty[RECOMP_DIRTY_SLOTS];
void recomp_watch_hit(uint32_t a, uint32_t n, uint64_t v) {
    (void)a;
    (void)n;
    (void)v;
    abort();
}
static volatile uint32_t checksum;
static void fail(unsigned row, unsigned input, unsigned mode, const char *reason) {
    fprintf(stderr, "%s input %u mode %u: %s\n", corpus_names[row], input, mode, reason);
    exit(1);
}
static void validate(unsigned count) {
    uint8_t expected[CORPUS_SCRATCH_SIZE];
    for (unsigned row = 0; row < CORPUS_COUNT; ++row) {
        for (unsigned input = 0; input < count; ++input) {
            X86 eager, actual;
            corpus_setup(corpus_fixture_ids[row], input, &eager);
            corpus_functions[row][0](&eager);
            memcpy(expected, g_mem + CORPUS_SCRATCH, sizeof expected);
            for (unsigned mode = 1; mode < CORPUS_MODES; ++mode) {
                corpus_setup(corpus_fixture_ids[row], input, &actual);
                corpus_functions[row][mode](&actual);
                if (mode + 1 == CORPUS_MODES) {
                    if (!corpus_observable_equal(corpus_fixture_ids[row], &eager, &actual,
                                                 expected))
                        fail(row, input, mode, "native observable result mismatch");
                } else if (memcmp(&eager, &actual, sizeof actual) ||
                           memcmp(expected, g_mem + CORPUS_SCRATCH, sizeof expected)) {
                    fail(row, input, mode, "translated full CPU/memory mismatch");
                }
            }
        }
        printf("CHECK %u %u\n", row, count);
    }
}
static double trial(unsigned row, unsigned mode, unsigned calls) {
    X86 c;
    unsigned fixture = corpus_fixture_ids[row];
    corpus_setup(fixture, CORPUS_BENCH_INPUT, &c);
    void (*function)(X86 *) = corpus_functions[row][mode];
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
    checksum ^= c.r[R_EAX] ^ rd32(CORPUS_SCRATCH);
    if (!corpus_bench_valid(fixture, &c))
        fail(row, 0, mode, "post-timing sanity check failed");
    return (double)(end - start) * 1e9 / CLOCKS_PER_SEC / calls;
}
int main(int argc, char **argv) {
    if (argc != 6)
        return 2;
    g_mem = calloc(1, CORPUS_MEMORY_SIZE);
    if (!g_mem)
        return 2;
    FILE *image = fopen(argv[1], "rb");
    size_t image_size = strtoul(argv[2], NULL, 10);
    if (!image || CORPUS_IMAGE_BASE + image_size > CORPUS_MEMORY_SIZE ||
        fread(g_mem + CORPUS_IMAGE_BASE, 1, image_size, image) != image_size)
        return 2;
    fclose(image);
    unsigned checks = strtoul(argv[3], NULL, 10);
    unsigned calls = strtoul(argv[4], NULL, 10);
    unsigned trials = strtoul(argv[5], NULL, 10);
    validate(checks);
    if (calls)
        for (unsigned row = 0; row < CORPUS_COUNT; ++row)
            for (unsigned t = 0; t < trials; ++t)
                for (unsigned offset = 0; offset <= CORPUS_MODES; ++offset) {
                    unsigned mode = (offset + t) % (CORPUS_MODES + 1);
                    double ns;
                    if (mode == CORPUS_MODES) {
                        corpus_native_trial(corpus_fixture_ids[row], 1000);
                        ns = corpus_native_trial(corpus_fixture_ids[row], calls);
                    } else
                        ns = trial(row, mode, calls);
                    printf("TIME %u %u %u %.6f\n", row, mode, t, ns);
                }
    free(g_mem);
    return 0;
}
