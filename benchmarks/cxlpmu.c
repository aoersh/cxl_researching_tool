/* cxlpmu.c — 直接驱动 CXL CPMU，不依赖 perf 二进制
 * 用法: ./cxlpmu <pmu> <秒> <event> [event ...]                              */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>

#define SYSDIR "/sys/bus/event_source/devices"
#define MAXEV 64

static int read_type(const char *pmu)
{
    char p[512]; int t = -1; FILE *f;
    snprintf(p, sizeof p, SYSDIR "/%s/type", pmu);
    if (!(f = fopen(p, "r"))) { fprintf(stderr, "打不开 %s: %s\n", p, strerror(errno)); exit(1); }
    if (fscanf(f, "%d", &t) != 1) { fprintf(stderr, "type 解析失败\n"); exit(1); }
    fclose(f); return t;
}

static unsigned long long read_config(const char *pmu, const char *ev)
{
    char p[512], line[256]; unsigned long long cfg, v; int got = 0; FILE *f;
    snprintf(p, sizeof p, SYSDIR "/%s/events/%s", pmu, ev);
    if (!(f = fopen(p, "r"))) { fprintf(stderr, "打不开 %s: %s\n", p, strerror(errno)); exit(1); }
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "config=%llx", &v) == 1) { cfg = v; got = 1; }
    fclose(f);
    if (!got) { fprintf(stderr, "%s 里没有 config= 字段\n", p); exit(1); }
    return cfg;
}

int main(int argc, char **argv)
{
    if (argc < 4) { fprintf(stderr, "用法: %s <pmu> <秒> <event> [event ...]\n", argv[0]); return 1; }
    const char *pmu = argv[1];
    int secs = atoi(argv[2]), n = argc - 3, i;
    int type = read_type(pmu), fd[MAXEV];
    if (n > MAXEV) { fprintf(stderr, "事件太多\n"); return 1; }

    for (i = 0; i < n; i++) {
        struct perf_event_attr a;
        memset(&a, 0, sizeof a);
        a.type     = type;
        a.size     = sizeof a;
        a.config   = read_config(pmu, argv[3 + i]);
        a.disabled = 1;
        a.inherit  = 0;
        /* 设备型 PMU 必须系统级采集：pid=-1, cpu=0 */
        fd[i] = (int)syscall(__NR_perf_event_open, &a, -1, 0, -1, 0);
        if (fd[i] < 0)
            fprintf(stderr, "%-22s perf_event_open 失败: %s (type=%d config=0x%llx)\n",
                    argv[3 + i], strerror(errno), type, a.config);
    }
    for (i = 0; i < n; i++) if (fd[i] >= 0) ioctl(fd[i], PERF_EVENT_IOC_RESET,  0);
    for (i = 0; i < n; i++) if (fd[i] >= 0) ioctl(fd[i], PERF_EVENT_IOC_ENABLE, 0);
    sleep(secs);
    for (i = 0; i < n; i++) if (fd[i] >= 0) ioctl(fd[i], PERF_EVENT_IOC_DISABLE, 0);

    printf("%s  (窗口 %d 秒)\n", pmu, secs);
    for (i = 0; i < n; i++) {
        unsigned long long c = 0;
        if (fd[i] < 0) continue;
        if (read(fd[i], &c, sizeof c) == sizeof c) printf("  %-22s %llu\n", argv[3 + i], c);
        close(fd[i]);
    }
    return 0;
}
