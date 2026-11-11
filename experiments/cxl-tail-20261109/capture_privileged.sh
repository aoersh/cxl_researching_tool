#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
    echo "Run: sudo bash $0"
    echo 'Collects three node2 cases with PCM, scheduler/IRQ traces, and CPU frequency logs.'
    echo 'Results are written into a new /tmp/cxl-tail-followup.* directory.'
    exit 0
fi
if (( EUID != 0 )); then
    echo "Run: sudo bash $0" >&2
    exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
result_dir=$(mktemp -d /tmp/cxl-tail-followup.XXXXXX)
trace_root=/sys/kernel/tracing
if [[ ! -r "$trace_root/events/sched/sched_switch/id" ]]; then
    trace_root=/sys/kernel/debug/tracing
fi
if [[ ! -r "$trace_root/events/sched/sched_switch/id" ]]; then
    echo 'The sched_switch tracepoint is unavailable; inspect tracefs configuration.' >&2
    exit 1
fi
finish() {
    if [[ -n ${frequency_pid:-} ]]; then
        kill "$frequency_pid" 2>/dev/null || true
        wait "$frequency_pid" 2>/dev/null || true
    fi
    if [[ -n ${SUDO_UID:-} && -n ${SUDO_GID:-} ]]; then
        chown -R -- "$SUDO_UID:$SUDO_GID" "$result_dir"
    fi
    echo "Results: $result_dir"
}
trap finish EXIT

modprobe msr
date -Is > "$result_dir/start.txt"
uname -a > "$result_dir/kernel.txt"
numactl --hardware > "$result_dir/numa.txt"
cxl list -M -H > "$result_dir/cxl-health.txt" 2>&1 || true
daxctl list > "$result_dir/dax.txt" 2>&1 || true
lspci -vv -s 33:00.0 > "$result_dir/pci-mem0.txt" 2>&1
lspci -vv -s b4:00.0 > "$result_dir/pci-mem1.txt" 2>&1
dmesg --ctime > "$result_dir/dmesg-before.txt" 2>&1 || true
events=(sched:sched_switch sched:sched_wakeup irq:irq_handler_entry irq:irq_handler_exit
        irq:softirq_entry irq:softirq_exit exceptions:page_fault_user migrate:mm_migrate_pages)
trace_args=()
for event in "${events[@]}"; do
    event_path=${event/:/\/}
    if [[ -r "$trace_root/events/$event_path/id" ]]; then
        trace_args+=(-e "$event")
        echo "$event" >> "$result_dir/trace-events.txt"
    fi
done

for condition in idle read4 write4; do
    case_dir="$result_dir/$condition"
    mkdir -p -- "$case_dir"
    cat /proc/interrupts > "$case_dir/interrupts-before.txt"
    cat /proc/softirqs > "$case_dir/softirqs-before.txt"
    cat /proc/vmstat > "$case_dir/vmstat-before.txt"
    turbostat --quiet --interval 1 --num_iterations 38 --out "$case_dir/frequency.txt" \
        > "$case_dir/turbostat-stdout.txt" 2> "$case_dir/turbostat-stderr.txt" &
    frequency_pid=$!
    perf record -a -k mono -m 256 -o "$case_dir/scheduler.data" "${trace_args[@]}" -- \
        /usr/local/bin/pcm-memory 0.5 "-csv=$case_dir/pcm.csv" -- \
        /usr/bin/python3 "$script_dir/capture_case.py" "$case_dir" "$condition" \
        > "$case_dir/capture.txt" 2>&1
    wait "$frequency_pid" || true
    frequency_pid=
    perf script --ns -i "$case_dir/scheduler.data" > "$case_dir/scheduler.txt" \
        2> "$case_dir/scheduler-decode-stderr.txt"
    perf report --stdio --header-only -i "$case_dir/scheduler.data" \
        > "$case_dir/trace-header.txt" 2>&1
    cat /proc/interrupts > "$case_dir/interrupts-after.txt"
    cat /proc/softirqs > "$case_dir/softirqs-after.txt"
    cat /proc/vmstat > "$case_dir/vmstat-after.txt"
done
dmesg --ctime > "$result_dir/dmesg-after.txt" 2>&1 || true
date -Is > "$result_dir/end.txt"
