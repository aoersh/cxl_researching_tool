/* lat.c — 随机单圈指针追逐；用法: lat <MB> <秒> */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/mman.h>

static double now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static unsigned long long rs = 88172645463325252ULL;
static unsigned long long xs(void) {
    rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return rs;
}

int main(int argc, char **argv) {
    long mb = argc > 1 ? atol(argv[1]) : 256;
    double secs = argc > 2 ? atof(argv[2]) : 5.0;
    long n = mb * 1024L * 1024L / 64;
    long bytes = n * 64;
    char *buf;
    long *idx;

    if (posix_memalign((void **)&buf, 64, bytes)) { perror("alloc"); return 1; }
    madvise(buf, bytes, MADV_HUGEPAGE);
    memset(buf, 1, bytes);                       /* 落实页 */

    idx = malloc(n * sizeof(long));
    if (!idx) { perror("idx"); return 1; }
    for (long i = 0; i < n; i++) idx[i] = i;
    for (long i = n - 1; i > 0; i--) {           /* Fisher-Yates 洗牌 */
        long j = (long)(xs() % (unsigned long long)(i + 1));
        long t = idx[i]; idx[i] = idx[j]; idx[j] = t;
    }
    for (long i = 0; i < n; i++)                 /* 串成随机单圈 */
        *(long *)(buf + idx[i] * 64) = (long)(buf + idx[(i + 1) % n] * 64);
    free(idx);

    volatile long *p = (volatile long *)buf;
    for (long i = 0; i < 100000; i++) p = (volatile long *)*p;   /* 预热 */

    long cnt = 0;
    double t0 = now(), t1;
    do {
        for (long k = 0; k < 10000; k++) p = (volatile long *)*p;
        cnt += 10000;
        t1 = now();
    } while (t1 - t0 < secs);

    printf("latency = %.2f ns/access   (%ld MB, %ld 次访问)\n",
           (t1 - t0) * 1e9 / cnt, mb, cnt);
    return 0;
}
