/* memtraffic.c — 在指定节点上持续读、再持续写；用法: memtraffic <MB> <每段秒数> */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    long mb = argc > 1 ? atol(argv[1]) : 512;
    double s = argc > 2 ? atof(argv[2]) : 5.0;
    long n = mb * 1024L * 1024L / 64, bytes = n * 64;
    char *buf;
    volatile long sink = 0;
    long pass; double t0, t1;

    if (posix_memalign((void **)&buf, 64, bytes)) { perror("alloc"); return 1; }
    memset(buf, 1, bytes);

    pass = 0; t0 = now();
    do {
        for (long i = 0; i < n; i++) sink += *(volatile long *)(buf + i * 64);
        pass++; t1 = now();
    } while (t1 - t0 < s);
    double rbw = (double)pass * n * 64 / (t1 - t0) / 1e9;

    pass = 0; t0 = now();
    do {
        for (long i = 0; i < n; i++) {
            volatile long *q = (volatile long *)(buf + i * 64);
            for (int k = 0; k < 8; k++) q[k] = sink;      /* 整行写 */
        }
        sink++; pass++; t1 = now();
    } while (t1 - t0 < s);
    double wbw = (double)pass * n * 64 / (t1 - t0) / 1e9;

    printf("read = %.1f GB/s   write = %.1f GB/s   (%ld MB, 每段 %g 秒)\n", rbw, wbw, mb, s);
    return 0;
}
