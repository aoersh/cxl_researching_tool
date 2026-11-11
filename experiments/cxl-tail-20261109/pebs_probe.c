#define _GNU_SOURCE
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>

struct cache_line {
    uint64_t next;
    unsigned char padding[56];
};

static double seconds_now(void)
{
    struct timespec timestamp;
    clock_gettime(CLOCK_MONOTONIC, &timestamp);
    return timestamp.tv_sec + timestamp.tv_nsec * 1e-9;
}

__attribute__((noinline))
static uint64_t chase(volatile struct cache_line *nodes, uint64_t current)
{
    for (uint64_t step = 0; step < 65536; ++step) {
        current = nodes[current].next;
    }
    return current;
}

int main(int argument_count, char **arguments)
{
    const double duration = argument_count > 1 ? atof(arguments[1]) : 2.0;
    const size_t allocation_size = 128UL * 1024 * 1024;
    const size_t node_count = allocation_size / sizeof(struct cache_line);
    struct cache_line *nodes = mmap(NULL, allocation_size,
        PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    uint32_t *order = malloc(node_count * sizeof(*order));
    if (nodes == MAP_FAILED || order == NULL) {
        perror("allocation");
        return 1;
    }
    madvise(nodes, allocation_size, MADV_NOHUGEPAGE);
    for (size_t position = 0; position < node_count; ++position) {
        order[position] = (uint32_t)position;
    }
    uint64_t random_state = 0x9e3779b97f4a7c15ULL;
    for (size_t remaining = node_count; remaining > 1; --remaining) {
        random_state ^= random_state << 13;
        random_state ^= random_state >> 7;
        random_state ^= random_state << 17;
        const size_t selected = random_state % remaining;
        const uint32_t previous = order[remaining - 1];
        order[remaining - 1] = order[selected];
        order[selected] = previous;
    }
    for (size_t position = 0; position < node_count; ++position) {
        nodes[order[position]].next = order[(position + 1) % node_count];
    }
    FILE *numa_map = fopen("/proc/self/numa_maps", "r");
    if (numa_map != NULL) {
        char mapping_line[4096];
        while (fgets(mapping_line, sizeof(mapping_line), numa_map)) {
            uintptr_t mapping_address;
            if (sscanf(mapping_line, "%" SCNxPTR, &mapping_address) == 1 &&
                mapping_address == (uintptr_t)nodes) {
                printf("allocation: %s", mapping_line);
            }
        }
        fclose(numa_map);
    }
    uint64_t current = order[0];
    uint64_t loads = 0;
    const double started = seconds_now();
    do {
        current = chase(nodes, current);
        loads += 65536;
    } while (seconds_now() - started < duration);
    printf("loads=%" PRIu64 " seconds=%.3f final_index=%" PRIu64 "\n",
        loads, seconds_now() - started, current);
    free(order);
    munmap(nodes, allocation_size);
    return 0;
}
