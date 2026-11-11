#!/usr/bin/env python3
import argparse
import csv
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import time


ROOT = Path(__file__).resolve().parent
MLC = Path('/home/hjy/projects/HotPageScheduling/third_party/intel-mlc/Linux/mlc')
PEBS_PROBE = ROOT / 'pebs_probe'
PMU_EVENTS = ','.join(
    f'cxl_pmu_mem{device}.0/{event}/'
    for device in (0, 1) for event in ('ddr_casrd', 'ddr_caswr')
)
SOFTWARE_EVENTS = 'task-clock,minor-faults,major-faults,context-switches,cpu-migrations'


def read_text(path):
    try:
        return Path(path).read_text()
    except (OSError, UnicodeError) as error:
        return f'UNAVAILABLE: {error}'


def stamp():
    return {'wall': datetime.datetime.now().astimezone().isoformat(),
            'monotonic_ns': time.monotonic_ns()}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def capture(command):
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=15)
        return {'command': command, 'returncode': result.returncode, 'output': result.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'command': command, 'error': str(error)}


def metadata():
    paths = [
        '/proc/sys/kernel/perf_event_paranoid', '/proc/sys/kernel/numa_balancing',
        '/proc/sys/kernel/nmi_watchdog', '/sys/kernel/mm/numa/demotion_enabled',
        '/sys/kernel/mm/transparent_hugepage/enabled',
        '/sys/kernel/mm/transparent_hugepage/defrag',
        '/sys/bus/event_source/devices/cpu/caps/max_precise',
        '/proc/loadavg', '/proc/meminfo', '/proc/cmdline',
    ]
    for cpu in (0, 1, 4, 16, 32):
        for name in ('scaling_governor', 'scaling_cur_freq', 'base_frequency'):
            paths.append(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/{name}')
    for node in range(4):
        paths.extend(f'/sys/devices/system/node/node{node}/{name}'
                     for name in ('cpulist', 'distance', 'meminfo'))
    for region in (0, 9):
        paths.extend(f'/sys/bus/cxl/devices/region{region}/{name}'
                     for name in ('resource', 'size', 'interleave_granularity', 'interleave_ways'))
    for device in (0, 1):
        for name in ('type', 'cpumask', 'events/ddr_casrd', 'events/ddr_caswr'):
            paths.append(f'/sys/bus/event_source/devices/cxl_pmu_mem{device}.0/{name}')
    return {
        'timestamp': stamp(), 'files': {path: read_text(path) for path in paths},
        'commands': [capture(command) for command in (
            ['uname', '-a'], ['lscpu'], ['lscpu', '-e=CPU,NODE,SOCKET,CORE,ONLINE'],
            ['numactl', '--hardware'], ['perf', '--version'],
            ['cxl', '--version'], ['cxl', 'list', '-M', '-H'], ['daxctl', 'list'],
        )],
        'binaries': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (MLC, PEBS_PROBE)},
    }


def descendants(pid):
    result = [pid]
    for parent in result:
        children = read_text(f'/proc/{parent}/task/{parent}/children')
        if not children.startswith('UNAVAILABLE'):
            result.extend(int(child) for child in children.split())
    return result


def snapshots(pid):
    result = []
    for process in descendants(pid):
        command = read_text(f'/proc/{process}/comm').strip()
        if command not in ('mlc', 'pebs_probe'):
            continue
        entry = {'pid': process, 'comm': command, 'timestamp': stamp()}
        for name in ('numa_maps', 'smaps_rollup', 'status', 'stat', 'schedstat', 'io'):
            entry[name] = read_text(f'/proc/{process}/{name}')
        entry['threads'] = {}
        try:
            for task in Path(f'/proc/{process}/task').iterdir():
                entry['threads'][task.name] = {
                    name: read_text(task / name) for name in ('status', 'stat', 'schedstat')
                }
        except OSError:
            pass
        result.append(entry)
    return result


def assigned_pages(process, node):
    total = 0
    for line in read_text(f'/proc/{process}/numa_maps').splitlines():
        match = re.search(rf'\bN{node}=(\d+)', line)
        if match:
            total += int(match.group(1))
    return total


def stop(process, interrupt=False):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT if interrupt else signal.SIGTERM)
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def parse_histogram(text):
    frequency = re.search(r'base freq of (\d+) MHz', text)
    average = re.search(r'Each iteration took ([\d.]+) base frequency clocks\s*\(\s*([\d.]+)\s*ns', text)
    bins = [(int(low), int(high), int(count)) for low, high, count in
            re.findall(r'^\[(\d+)-(\d+)\]\s+(\d+)', text, re.MULTILINE)]
    overflow = re.search(r'^\[>(\d+)\]\s+(\d+)', text, re.MULTILINE)
    if not frequency or not average or not bins or not overflow:
        raise ValueError('Incomplete MLC histogram')
    ghz = int(frequency.group(1)) / 1000
    overflow_limit, overflow_count = map(int, overflow.groups())
    samples = sum(count for low, high, count in bins) + overflow_count
    if not samples:
        raise ValueError('Empty MLC histogram')
    result = {'base_mhz': int(frequency.group(1)), 'mean_ns': float(average.group(2)),
              'samples': samples, 'overflow_count': overflow_count,
              'overflow_limit_ns': overflow_limit / ghz}
    for label, quantile in (('p50', .5), ('p95', .95), ('p99', .99),
                            ('p999', .999), ('p9999', .9999)):
        target, accumulated = math.ceil(samples * quantile), 0
        lower, upper = overflow_limit / ghz, None
        for low, high, count in bins:
            accumulated += count
            if accumulated >= target:
                lower, upper = low / ghz, high / ghz
                break
        result[f'{label}_low_ns'], result[f'{label}_high_ns'] = lower, upper
    for threshold in (1000, 5000, 10000):
        cutoff = threshold * ghz
        certain = sum(count for low, high, count in bins if low > cutoff)
        possible = sum(count for low, high, count in bins if high > cutoff)
        if overflow_limit >= cutoff:
            certain += overflow_count
        possible += overflow_count
        result[f'gt{threshold}_pct_low'] = 100 * certain / samples
        result[f'gt{threshold}_pct_high'] = 100 * possible / samples
    nonempty = [(low, high) for low, high, count in bins if count]
    result['max_bin_low_ns'] = (overflow_limit if overflow_count else nonempty[-1][0]) / ghz
    result['max_bin_high_ns'] = None if overflow_count else nonempty[-1][1] / ghz
    return result


def execute_run(run_id, node, condition, histogram=True, pebs=None, duration=3,
                probe_cpu=0, load_cpus=(1, 2, 3, 4), injection_delay=0):
    directory = ROOT / 'runs' / run_id
    directory.mkdir(parents=True, exist_ok=False)
    load = None
    observer = None
    probe = None
    handles = []
    record = {'run': run_id, 'node': node, 'condition': condition,
              'histogram': histogram, 'pebs': pebs, 'start': stamp(), 'cpu': probe_cpu,
              'load_cpus': list(load_cpus) if condition != 'idle' else [],
              'injection_delay': injection_delay}
    before = read_text('/proc/vmstat')
    try:
        if condition != 'idle':
            load_node = 0 if condition == 'other_dram_read4' else node
            pattern = 'W6' if condition == 'write4' else 'R'
            config = directory / 'load.cfg'
            cpu_list = ','.join(str(cpu) for cpu in load_cpus)
            config.write_text(f'{cpu_list} {pattern} seq 256m dram {load_node}\n')
            command = [str(MLC), '--loaded_latency', '-T', '-e', f'-o{config}',
                       f'-d{injection_delay}', '-t30']
            handle = (directory / 'load.txt').open('w')
            handles.append(handle)
            load = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            record['load_command'] = command
            deadline = time.monotonic() + 12
            page_kib = os.sysconf('SC_PAGE_SIZE') / 1024
            target_pages = len(load_cpus) * 256 * 1024 / page_kib
            while assigned_pages(load.pid, load_node) < target_pages * .90:
                if load.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Load memory did not reach target node before deadline')
                time.sleep(.1)
            time.sleep(1.5)
            record['load_ready'] = stamp()
            write_json(directory / 'load-placement.json', snapshots(load.pid))
        observer_command = ['perf', 'stat', '-a', '-I', '1000', '-x', ';',
                            '-e', PMU_EVENTS, '-o', str(directory / 'pmu.csv'), '--', 'sleep', '90']
        handle = (directory / 'pmu-stderr.txt').open('w')
        handles.append(handle)
        observer = subprocess.Popen(observer_command, stdout=handle, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        record['observer_command'] = observer_command
        if pebs is None:
            workload = [str(MLC), '--idle_latency', '-e', '-r', f'-c{probe_cpu}', f'-j{node}',
                        '-b256m', f'-t{duration}']
            if histogram:
                workload += ['-f4', '--lat_hist_max_us=100']
            command = ['perf', 'stat', '-x', ';', '-e', SOFTWARE_EVENTS,
                       '-o', str(directory / 'software.csv'), '--'] + workload
        else:
            workload = ['numactl', '--all', f'--physcpubind={probe_cpu}', f'--membind={node}',
                        str(PEBS_PROBE), str(duration)]
            command = workload
            if pebs:
                command = ['perf', 'record', '-e', 'cpu/mem-loads,ldlat=64/upp',
                           '-c', '257', '-d', '-W', '--phys-data',
                           '-o', str(directory / 'perf.data'), '--'] + workload
        record['command'] = command
        handle = (directory / 'probe.txt').open('w')
        handles.append(handle)
        record['probe_start'] = stamp()
        probe = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        history = []
        deadline = time.monotonic() + 45
        while probe.poll() is None:
            history.extend(snapshots(probe.pid))
            if load is not None and load.poll() is not None:
                raise RuntimeError('Background load ended before probe')
            if time.monotonic() >= deadline:
                raise RuntimeError('Probe timed out')
            time.sleep(.5)
        record['probe_end'] = stamp()
        record['probe_returncode'] = probe.returncode
        write_json(directory / 'probe-snapshots.json', history)
        if probe.returncode:
            raise RuntimeError(f'Probe exited {probe.returncode}')
        stop(observer, interrupt=True)
        record['observer_returncode'] = observer.returncode
        if load is not None:
            load.wait(timeout=35)
            record['load_end'] = stamp()
            record['load_returncode'] = load.returncode
            if load.returncode:
                raise RuntimeError(f'Load exited {load.returncode}')
        for handle in handles:
            handle.flush()
        text = (directory / 'probe.txt').read_text()
        if histogram and pebs is None:
            record['result'] = parse_histogram(text)
        elif pebs is None:
            match = re.search(r'Each iteration took.*?\(\s*([\d.]+)\s*ns', text)
            if not match:
                raise ValueError('MLC mean absent')
            record['result'] = {'mean_ns': float(match.group(1))}
        else:
            match = re.search(r'loads=(\d+) seconds=([\d.]+)', text)
            if not match:
                raise ValueError('Probe throughput absent')
            loads, elapsed = int(match.group(1)), float(match.group(2))
            record['result'] = {'loads': loads, 'seconds': elapsed,
                                'mean_ns': elapsed * 1e9 / loads}
        record['status'] = 'ok'
    except Exception as error:
        record['status'] = 'error'
        record['error'] = str(error)
        raise
    finally:
        stop(probe)
        stop(observer, interrupt=True)
        stop(load)
        for handle in handles:
            handle.close()
        (directory / 'vmstat-before.txt').write_text(before)
        (directory / 'vmstat-after.txt').write_text(read_text('/proc/vmstat'))
        record['end'] = stamp()
        write_json(directory / 'run.json', record)
    return record


def summarize():
    records = [json.loads(path.read_text()) for path in sorted((ROOT / 'runs').glob('*/run.json'))]
    write_json(ROOT / 'all-runs.json', records)
    rows = []
    for record in records:
        row = {key: record.get(key) for key in ('run', 'node', 'condition', 'histogram', 'pebs', 'status')}
        row.update(record.get('result', {}))
        rows.append(row)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (ROOT / 'summary.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    os.sched_setaffinity(0, {8})
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('matrix', 'control', 'pebs', 'summarize'))
    args = parser.parse_args()
    if args.phase == 'summarize':
        summarize()
        return
    if not (ROOT / 'metadata-start.json').exists():
        write_json(ROOT / 'metadata-start.json', metadata())
        for filename in ('pcm-cxl-node3.csv', 'pcm-cxl-check.csv'):
            source = Path('/tmp') / filename
            if source.exists():
                shutil.copyfile(source, ROOT / ('previous-' + filename))
    jobs = []
    for repeat in range(1, 4):
        if args.phase == 'matrix':
            block = [(node, condition) for node in range(4) for condition in ('idle', 'read4')]
            block += [(2, 'write4'), (2, 'other_dram_read4')]
            random.Random(20261109 + repeat).shuffle(block)
            for node, condition in block:
                jobs.append((f'matrix-r{repeat}-n{node}-{condition}', node, condition, True, None, 3))
        elif args.phase == 'control':
            nodes = list(range(4))
            random.Random(1026 + repeat).shuffle(nodes)
            for node in nodes:
                jobs.append((f'control-r{repeat}-n{node}', node, 'idle', False, None, 3))
        else:
            for condition in ('idle', 'read4'):
                for sampled in ((False, True) if repeat % 2 else (True, False)):
                    jobs.append((f'pebs-r{repeat}-{condition}-{int(sampled)}', 2,
                                 condition, False, sampled, 5))
    for index, job in enumerate(jobs, 1):
        existing = ROOT / 'runs' / job[0]
        if existing.exists() and json.loads((existing / 'run.json').read_text())['status'] != 'ok':
            archived = ROOT / 'pilot' / (job[0] + '-failed-' + str(time.time_ns()))
            existing.rename(archived)
        if existing.exists():
            print(f'SKIP existing {job[0]}', flush=True)
            continue
        print(f'[{index}/{len(jobs)}] {job[0]} {stamp()["wall"]}', flush=True)
        result = execute_run(*job)
        print(json.dumps(result['result']), flush=True)
        summarize()
        time.sleep(.5)
    write_json(ROOT / ('metadata-end-' + args.phase + '.json'), metadata())


if __name__ == '__main__':
    main()
