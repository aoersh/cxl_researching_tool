#!/usr/bin/env python3
import collections
import csv
import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess

import run_experiment as runner


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'followup'
SOURCE = OUTPUT / 'raw'
TRACE = re.compile(r'\s*(.*?)\s+(-?\d+)\s+\[(\d+)\]\s+([\d.]+):\s+([\w:]+):\s*(.*)')
SWITCH = re.compile(r'prev_comm=(.*?) prev_pid=(\d+) prev_prio=\d+ prev_state=(.*?) ==> '
                    r'next_comm=(.*?) next_pid=(\d+)')


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write_csv(path, rows):
    with path.open('w', newline='') as handle:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def stats(values):
    ordered = sorted(values)
    if not ordered:
        return {'count': 0, 'sum': 0}
    return {'count': len(ordered), 'sum': sum(ordered), 'min': ordered[0],
            'median': statistics.median(ordered),
            'p99': ordered[math.ceil(.99 * len(ordered)) - 1], 'max': ordered[-1]}


def stable_snapshots(history, node):
    result = []
    for snapshot in history:
        for line in snapshot['numa_maps'].splitlines():
            pages = {int(key): int(value) for key, value in re.findall(r'\bN(\d+)=(\d+)', line)}
            if pages.get(node, 0) >= 65536 and sum(pages.values()) == pages[node]:
                result.append(snapshot)
                break
    return result


def ns(timestamp):
    seconds, fraction = timestamp.split('.')
    return int(seconds) * 10**9 + int(fraction.ljust(9, '0'))


def analyze_trace(directory, pid, start, end):
    events = []
    counts = collections.Counter()
    faults = []
    migrations = []
    target_cpus = collections.Counter()
    for line in (directory / 'scheduler.txt').open():
        match = TRACE.match(line)
        if not match:
            continue
        comm, event_pid, cpu, timestamp, event, detail = match.groups()
        event_pid, cpu, timestamp = int(event_pid), int(cpu), ns(timestamp)
        counts[event] += 1
        inside = start <= timestamp <= end
        if cpu == 0:
            events.append((timestamp, event, event_pid, detail))
        if inside and event_pid == pid:
            target_cpus[cpu] += 1
            if event == 'exceptions:page_fault_user':
                faults.append({'monotonic_ns': timestamp, 'cpu': cpu, 'detail': detail})
        if inside and event == 'migrate:mm_migrate_pages':
            migrations.append({'monotonic_ns': timestamp, 'pid': event_pid,
                               'comm': comm, 'detail': detail})
    events.sort(key=lambda event: event[0])
    switches = []
    for timestamp, event, event_pid, detail in events:
        if event == 'sched:sched_switch':
            match = SWITCH.search(detail)
            if not match:
                raise ValueError(f'Cannot decode switch: {detail}')
            prev_name, prev_pid, state, next_name, next_pid = match.groups()
            switches.append((timestamp, prev_name, int(prev_pid), state,
                             next_name, int(next_pid)))
    offcpu = []
    pending = None
    competitors = collections.Counter()
    for index, switch in enumerate(switches):
        timestamp, prev_name, prev_pid, state, next_name, next_pid = switch
        next_time = switches[index + 1][0] if index + 1 < len(switches) else end
        overlap = max(0, min(end, next_time) - max(start, timestamp))
        if overlap and next_pid != pid:
            competitors[f'{next_name}:{next_pid}'] += overlap
        if prev_pid == pid and next_pid != pid:
            if pending is not None:
                raise ValueError('Unpaired target switch-out')
            pending = (timestamp, state)
        if next_pid == pid and pending is not None:
            lower, upper = max(start, pending[0]), min(end, timestamp)
            if upper > lower:
                offcpu.append({'start_ns': lower, 'end_ns': upper,
                               'duration_us': (upper - lower) / 1000,
                               'prev_state': pending[1]})
            pending = None
    if pending is not None and pending[0] < end:
        offcpu.append({'start_ns': max(start, pending[0]), 'end_ns': end,
                       'duration_us': (end - max(start, pending[0])) / 1000,
                       'prev_state': pending[1]})
    softirq_pending = {}
    softirq = []
    hardware_irq_entries = 0
    for timestamp, event, event_pid, detail in events:
        if event == 'irq:irq_handler_entry' and start <= timestamp <= end:
            hardware_irq_entries += 1
        if event not in ('irq:softirq_entry', 'irq:softirq_exit'):
            continue
        match = re.search(r'vec=(\d+) \[action=(.*?)\]', detail)
        if not match:
            raise ValueError('Cannot decode softirq')
        vector, action = match.groups()
        if event == 'irq:softirq_entry':
            softirq_pending[vector] = (timestamp, action, event_pid)
        else:
            pending = softirq_pending.pop(vector, None)
            if pending:
                lower, upper = max(start, pending[0]), min(end, timestamp)
                if upper > lower:
                    softirq.append({'start_ns': lower, 'end_ns': upper,
                                    'duration_us': (upper - lower) / 1000,
                                    'action': action, 'context_pid': pending[2]})
    offcpu_stats = stats([entry['duration_us'] for entry in offcpu])
    result = {'whole_trace_event_counts': dict(counts), 'pid': pid,
              'stable_window_start_monotonic_ns': start,
              'stable_window_end_monotonic_ns': end, 'stable_window_seconds': (end - start) / 1e9,
              'offcpu_us': offcpu_stats,
              'offcpu_pct': offcpu_stats['sum'] * 1000 / (end - start) * 100,
              'competitor_cpu0_us': {name: duration / 1000 for name, duration in competitors.most_common()},
              'target_faults_in_window': faults, 'host_migrations_in_window': migrations,
              'target_trace_cpu_counts_in_window': dict(target_cpus),
              'cpu0_irq_handler_entries_in_window': hardware_irq_entries,
              'cpu0_softirq_us': stats([entry['duration_us'] for entry in softirq]),
              'target_context_softirq_us': stats([entry['duration_us'] for entry in softirq
                                                 if entry['context_pid'] == pid]),
              'cpu0_softirq_by_action_us': {
                  action: stats([entry['duration_us'] for entry in softirq if entry['action'] == action])
                  for action in sorted({entry['action'] for entry in softirq})}}
    write_csv(OUTPUT / f'{directory.name}-offcpu.csv', offcpu)
    write_csv(OUTPUT / f'{directory.name}-softirq.csv', softirq)
    output = subprocess.run(['perf', 'report', '--stdio', '--stats', '-i', str(directory / 'scheduler.data')],
                            capture_output=True, text=True, check=True)
    report = output.stdout + output.stderr
    (OUTPUT / f'{directory.name}-trace-stats.txt').write_text(report)
    expected_samples = int(re.search(r'^\s*SAMPLE events:\s+(\d+)', report, re.MULTILINE).group(1))
    assert sum(counts.values()) == expected_samples
    result['decoded_samples_match_perf_stats'] = True
    result['lost_records_in_perf_stats'] = sum(int(value) for value in re.findall(
        r'^\s*LOST(?:_SAMPLES)? events:\s+(\d+)', report, re.MULTILINE))
    result['decode_warnings'] = (directory / 'scheduler-decode-stderr.txt').read_text().strip()
    return result


def analyze_pcm(directory, start_wall, end_wall):
    rows = list(csv.reader((directory / 'pcm.csv').open()))
    keys = [f'{group.strip()}/{field.strip()}' for group, field in zip(rows[0], rows[1])]
    previous = None
    retained = []
    for row in rows[2:]:
        timestamp = datetime.datetime.fromisoformat(f'{row[0]}T{row[1]}+08:00')
        if previous is not None and previous >= start_wall and timestamp <= end_wall:
            values = {key: float(value) for key, value in zip(keys[2:], row[2:])}
            retained.append({'interval_start': previous.isoformat(), 'interval_end': timestamp.isoformat(), **values})
        previous = timestamp
    write_csv(OUTPUT / f'{directory.name}-pcm-window.csv', retained)
    return {'intervals': len(retained), 'columns_MB_s': {
        key: stats([row[key] for row in retained]) for key in keys[2:]
        if ('CXL.mem' in key or 'Mem Read' in key or 'Mem Write' in key)
        and any(row[key] > 0 for row in retained)}}


def analyze_frequency(directory):
    records = []
    header = None
    block = -1
    for line in (directory / 'frequency.txt').read_text().splitlines():
        fields = line.split()
        if fields[:3] == ['Package', 'Core', 'CPU']:
            header = fields
            block += 1
        elif header and len(fields) > 5 and fields[2] == '0':
            row = dict(zip(header, fields))
            records.append({'block_index': block, **row})
    write_csv(OUTPUT / f'{directory.name}-frequency-cpu0.csv', records)
    active = [row for row in records if float(row['Busy%']) >= 95]
    return {'total_blocks': len(records), 'cpu0_busy_ge95_blocks': len(active),
            'Bzy_MHz': stats([float(row['Bzy_MHz']) for row in active]),
            'CoreTmp_C': stats([float(row['CoreTmp']) for row in active]),
            'CoreThr_sum_active_blocks': sum(int(row['CoreThr']) for row in active),
            'SMI_sum_active_blocks': sum(int(row['SMI']) for row in active),
            'limitation': 'No timestamps in turbostat output; active blocks cannot be precisely aligned to histogram spikes.'}


def expansion():
    source = ROOT / 'expansion-20261111'
    groups = collections.defaultdict(list)
    quality = []
    for path in sorted((source / 'runs').glob('*/run.json')):
        record = json.loads(path.read_text())
        assert record['status'] == 'ok'
        history = json.loads((path.parent / 'probe-snapshots.json').read_text())
        valid = stable_snapshots(history, record['node'])
        assert valid
        affinity_sequence = [re.search(r'Cpus_allowed_list:\s*(.*)', entry['status']).group(1)
                             for entry in valid]
        affinity = sorted(set(affinity_sequence))
        first_pinned = affinity_sequence.index(str(record['cpu']))
        pinned_snapshots = valid[first_pinned:]
        huge_kib = max(int(re.search(r'AnonHugePages:\s*(\d+)', entry['smaps_rollup']).group(1))
                       for entry in valid)
        major_faults = max(int(entry['stat'].split()[11]) for entry in valid)
        assert all(cpu == str(record['cpu']) for cpu in affinity_sequence[first_pinned:])
        assert huge_kib == 0 and major_faults == 0
        quality.append({'run': record['run'], 'valid_snapshots': len(valid),
                        'affinities': affinity, 'max_anonhuge_kib': huge_kib,
                        'max_major_faults': major_faults,
                        'snapshots_before_requested_affinity': first_pinned,
                        'snapshots_at_requested_affinity': len(pinned_snapshots)})
        groups[record['node'], record['cpu'], record['condition'], len(record['load_cpus'])].append(record)
    assert sum(len(records) for records in groups.values()) == 24
    result = []
    for (node, cpu, condition, threads), records in sorted(groups.items()):
        assert len(records) == 3
        row = {'node': node, 'cpu': cpu, 'condition': condition, 'load_threads': threads,
               'repeats': 3, 'samples': sum(record['result']['samples'] for record in records),
               'overflow': sum(record['result']['overflow_count'] for record in records)}
        for metric in ('mean_ns', 'p99_low_ns', 'p99_high_ns', 'p999_low_ns', 'p999_high_ns'):
            values = [record['result'][metric] for record in records]
            row[metric + '_median'] = statistics.median(values)
            row[metric + '_min'] = min(values)
            row[metric + '_max'] = max(values)
        rates = []
        for record in records:
            path = source / 'runs' / record['run'] / 'load.txt'
            if path.exists():
                rates.extend(float(value) for value in re.findall(
                    r'^\s*00000\s+[\d.]+\s+([\d.]+)', path.read_text(), re.MULTILINE))
        row['load_mlc_MB_s_median'] = statistics.median(rates) if rates else None
        result.append(row)
    write_json(source / 'group-summary.json', result)
    write_csv(source / 'group-summary.csv', result)
    write_json(source / 'quality.json', quality)
    return result


def main():
    OUTPUT.mkdir(exist_ok=True)
    cases = []
    for condition in ('idle', 'read4', 'write4'):
        directory = SOURCE / condition
        run_dir = directory / 'runs' / f'node2-{condition}'
        record = json.loads((run_dir / 'run.json').read_text())
        assert record['status'] == 'ok'
        parsed = runner.parse_histogram((run_dir / 'probe.txt').read_text())
        assert parsed == record['result']
        history = stable_snapshots(json.loads((run_dir / 'probe-snapshots.json').read_text()), 2)
        assert history and len({snapshot['pid'] for snapshot in history}) == 1
        first, last = history[0], history[-1]
        trace = analyze_trace(directory, first['pid'], first['timestamp']['monotonic_ns'],
                              last['timestamp']['monotonic_ns'])
        pcm = analyze_pcm(directory, datetime.datetime.fromisoformat(first['timestamp']['wall']),
                           datetime.datetime.fromisoformat(last['timestamp']['wall']))
        cases.append({'condition': condition, 'latency': parsed, 'trace': trace, 'pcm': pcm,
                      'frequency': analyze_frequency(directory)})
    hardware = {}
    for address in ('0000:33:00.0', '0000:32:02.0', '0000:b4:00.0', '0000:b3:02.0'):
        directory = Path('/sys/bus/pci/devices') / address
        hardware[address] = {name: runner.read_text(directory / name).strip() for name in
                             ('current_link_speed', 'current_link_width', 'max_link_speed',
                              'max_link_width', 'aer_dev_correctable', 'aer_dev_nonfatal', 'aer_dev_fatal')}
    summary = {'analyzed_at': runner.stamp(), 'source_original': '/tmp/cxl-tail-followup.vE2UIq',
               'source_archived': str(SOURCE), 'cases': cases, 'current_pci_sysfs_snapshot': hardware,
               'cxl_health_at_capture_start': json.loads((SOURCE / 'cxl-health.txt').read_text()),
               'current_memdev_firmware': {f'mem{device}': runner.read_text(
                   f'/sys/bus/cxl/devices/mem{device}/firmware_version').strip() for device in (0, 1)},
               'dmesg_before_after_identical': (SOURCE / 'dmesg-before.txt').read_bytes() ==
                                              (SOURCE / 'dmesg-after.txt').read_bytes(),
               'expansion': expansion()}
    write_json(OUTPUT / 'analysis.json', summary)
    manifest = {}
    for path in sorted(SOURCE.rglob('*')):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            manifest[str(path.relative_to(SOURCE))] = {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}
    write_json(OUTPUT / 'raw-manifest.json', manifest)
    for case in cases:
        print(case['condition'], json.dumps({'offcpu_us': case['trace']['offcpu_us'],
              'offcpu_pct': case['trace']['offcpu_pct'], 'softirq_us': case['trace']['target_context_softirq_us'],
              'faults': len(case['trace']['target_faults_in_window']),
              'pcm': case['pcm']['columns_MB_s'].get('SKT0/CXL.mem_P1Read'),
              'pcm_write': case['pcm']['columns_MB_s'].get('SKT0/CXL.mem_P1Write')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
