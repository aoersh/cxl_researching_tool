#!/usr/bin/env python3
import collections
import csv
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


ROOT = Path(__file__).resolve().parent
SAMPLE_PATTERN = re.compile(
    r'([\d.]+):\s+([0-9a-f]+)\s+([0-9a-f]+)\s+(\|.*?)\s+'
    r'(\d+)\s+([0-9a-f]+)\s+(\S+)\s+([0-9a-f]+)\s*$'
)
LABELS = {0: '本地 DRAM', 1: '跨路 DRAM', 2: '本地 CXL', 3: '跨路 CXL'}
CONDITIONS = {'idle': '空载', 'read4': '同节点 4 线程读',
              'write4': '同节点 4 线程非临时写', 'other_dram_read4': '本地 DRAM 4 线程读'}


def dump_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def pebs_summary(records):
    summaries = []
    rows = []
    for record in records:
        if not record.get('pebs') or record['status'] != 'ok':
            continue
        directory = ROOT / 'runs' / record['run']
        datafile = directory / 'perf.data'
        result = subprocess.run(
            ['perf', 'script', '-i', str(datafile), '-F',
             'time,ip,sym,addr,phys_addr,weight,data_src'],
            text=True, capture_output=True, check=True)
        (directory / 'samples.txt').write_text(result.stdout)
        (directory / 'decode-stderr.txt').write_text(result.stderr)
        for command, name in (
            (['perf', 'evlist', '-v', '-i', str(datafile)], 'events.txt'),
            (['perf', 'mem', 'report', '--stdio', '-i', str(datafile),
              '--sort=mem,snoop,tlb'], 'mem-report.txt'),
        ):
            decoded = subprocess.run(command, text=True, capture_output=True, check=True)
            (directory / name).write_text(decoded.stdout + decoded.stderr)
        sources = collections.Counter()
        weights = []
        missing_physical = 0
        other_region = 0
        region0 = 0
        parsed = 0
        for line in result.stdout.splitlines():
            match = SAMPLE_PATTERN.fullmatch(line.strip())
            if not match:
                raise ValueError(f'Unrecognized perf sample: {line}')
            timestamp, address, source, decoded, weight, instruction, symbol, physical = match.groups()
            parsed += 1
            if symbol != 'chase':
                continue
            physical_value = int(physical, 16)
            weight_value = int(weight)
            sources[re.search(r'LVL (.*?)\|', decoded).group(1)] += 1
            weights.append(weight_value)
            missing_physical += physical_value == 0
            in_region = 0x1080000000 <= physical_value < 0x2080000000
            region0 += in_region
            other_region += physical_value != 0 and not in_region
            rows.append({'run': record['run'], 'condition': record['condition'],
                         'time_seconds': timestamp, 'virtual_address': address,
                         'physical_address': physical, 'region0': in_region,
                         'weight_cycles': weight_value, 'ip': instruction,
                         'symbol': symbol, 'data_src_hex': source, 'decoded': decoded})
        lost = re.search(r'Total Lost Samples:\s*(\d+)', (directory / 'mem-report.txt').read_text())
        raw = subprocess.run(['perf', 'script', '-D', '-i', str(datafile)],
                             text=True, capture_output=True, check=True)
        summaries.append({
            'run': record['run'], 'condition': record['condition'], 'all_samples': parsed,
            'chase_samples': len(weights), 'region0': region0,
            'physical_zero': missing_physical, 'other_region': other_region,
            'sources': dict(sources), 'weight_p50_cycles': percentile(weights, .50),
            'weight_p95_cycles': percentile(weights, .95),
            'lost_samples': int(lost.group(1)) if lost else None,
            'throttle_records': len(re.findall(r'PERF_RECORD_THROTTLE', raw.stdout)),
        })
    write_csv(ROOT / 'pebs-chase-samples.csv', rows)
    dump_json(ROOT / 'pebs-summary.json', summaries)
    return summaries


def quality(records):
    results = []
    for record in records:
        directory = ROOT / 'runs' / record['run']
        history = json.loads((directory / 'probe-snapshots.json').read_text())
        target = record['node']
        buffer_pages = 32768 if record['pebs'] is not None else 65536
        valid = []
        affinity = collections.Counter()
        huge_kib = []
        for snapshot in history:
            correct = False
            for line in snapshot['numa_maps'].splitlines():
                pages = {int(key): int(value) for key, value in re.findall(r'\bN(\d+)=(\d+)', line)}
                if pages.get(target, 0) >= buffer_pages:
                    correct |= sum(pages.values()) == pages[target]
            if correct:
                valid.append(snapshot)
            match = re.search(r'AnonHugePages:\s*(\d+)', snapshot['smaps_rollup'])
            if match:
                huge_kib.append(int(match.group(1)))
            for thread in snapshot['threads'].values():
                match = re.search(r'Cpus_allowed_list:\s*(.*)', thread['status'])
                if match:
                    affinity[match.group(1)] += 1
        row = {'run': record['run'], 'valid_placement_snapshots': len(valid),
               'total_snapshots': len(history), 'max_anonhuge_kib': max(huge_kib or [0]),
               'thread_affinities': dict(affinity)}
        if valid:
            last = valid[-1]
            row['last_major_faults'] = int(last['stat'].split()[11])
            row['last_nonvoluntary_switches'] = int(
                re.search(r'nonvoluntary_ctxt_switches:\s*(\d+)', last['status']).group(1))
        if record['pebs'] is None:
            software = {}
            for line in (directory / 'software.csv').read_text().splitlines():
                fields = line.split(';')
                if len(fields) > 4 and fields[0] and not fields[0].startswith('#'):
                    software[fields[2]] = float(fields[0])
            row['software_counters_including_initialization'] = software
        before = dict(line.split() for line in (directory / 'vmstat-before.txt').read_text().splitlines())
        after = dict(line.split() for line in (directory / 'vmstat-after.txt').read_text().splitlines())
        row['system_vmstat_delta'] = {
            key: int(after[key]) - int(before[key]) for key in
            ('pgmajfault', 'pgmigrate_success', 'numa_pages_migrated', 'numa_hint_faults',
             'pgscan_direct', 'allocstall_movable') if key in before
        }
        results.append(row)
    dump_json(ROOT / 'quality.json', results)
    assert all(item['valid_placement_snapshots'] for item in results)
    assert all(item['max_anonhuge_kib'] == 0 for item in results)
    assert all(item['last_major_faults'] == 0 for item in results)
    return results


def main():
    records = [json.loads(path.read_text()) for path in sorted((ROOT / 'runs').glob('*/run.json'))]
    assert len(records) == 54 and all(record['status'] == 'ok' for record in records)
    groups = collections.defaultdict(list)
    for record in records:
        if record['run'].startswith('matrix'):
            groups[record['node'], record['condition']].append(record)
    aggregate = []
    for (node, condition), runs in sorted(groups.items()):
        assert len(runs) == 3
        results = [run['result'] for run in runs]
        row = {'node': node, 'memory': LABELS[node], 'condition': condition,
               'repeats': len(runs), 'samples_total': sum(item['samples'] for item in results),
               'overflow_total': sum(item['overflow_count'] for item in results),
               'mean_ns_median': statistics.median(item['mean_ns'] for item in results)}
        for label in ('p50', 'p99', 'p999', 'p9999'):
            for end in ('low', 'high'):
                row[f'{label}_{end}_ns_median'] = statistics.median(item[f'{label}_{end}_ns'] for item in results)
            row[f'{label}_low_ns_min'] = min(item[f'{label}_low_ns'] for item in results)
            row[f'{label}_high_ns_max'] = max(item[f'{label}_high_ns'] for item in results)
        for end in ('low', 'high'):
            row[f'gt1000_pct_{end}_median'] = statistics.median(item[f'gt1000_pct_{end}'] for item in results)
        rates = []
        for run in runs:
            load = ROOT / 'runs' / run['run'] / 'load.txt'
            if load.exists():
                rates.extend(float(value) for value in re.findall(
                    r'^\s*00000\s+[\d.]+\s+([\d.]+)', load.read_text(), re.MULTILINE))
        row['load_mlc_mb_s_median'] = statistics.median(rates) if rates else None
        aggregate.append(row)
    write_csv(ROOT / 'group-summary.csv', aggregate)
    dump_json(ROOT / 'group-summary.json', aggregate)
    pebs = pebs_summary(records)
    quality_results = quality(records)
    counts = collections.Counter()
    for item in pebs:
        for key in ('all_samples', 'chase_samples', 'region0', 'physical_zero', 'other_region', 'lost_samples'):
            counts[key] += item[key]
    print('runs:', len(records), 'histogram observations:', sum(row['samples_total'] for row in aggregate))
    print('PEBS:', dict(counts))
    print('All runs have target-node placement; max huge pages and major faults are zero.')
    lines = [
        '| 内存 / CPU0 发起 | 背景负载 | 平均 ns | P99 ns | P99.9 ns | >1µs 占比 |',
        '|---|---|---:|---:|---:|---:|',
    ]
    for row in aggregate:
        interval = lambda label: f"{row[label + '_low_ns_median']:.0f}–{row[label + '_high_ns_median']:.0f}"
        lines.append(f"| node{row['node']} {row['memory']} | {CONDITIONS[row['condition']]} | "
                     f"{row['mean_ns_median']:.1f} | {interval('p99')} | {interval('p999')} | "
                     f"{row['gt1000_pct_low_median']:.3f}–{row['gt1000_pct_high_median']:.3f}% |")
    (ROOT / 'results-table.md').write_text('\n'.join(lines) + '\n')
    overhead = []
    for condition in ('idle', 'read4'):
        for repeat in range(1, 4):
            paired = {record['pebs']: record['result']['mean_ns'] for record in records
                      if record['run'].startswith(f'pebs-r{repeat}-{condition}-')}
            overhead.append({'condition': condition, 'repeat': repeat,
                             'unobserved_mean_ns': paired[False], 'sampled_mean_ns': paired[True],
                             'change_pct': 100 * (paired[True] / paired[False] - 1)})
    write_csv(ROOT / 'pebs-overhead.csv', overhead)
    assert len(quality_results) == 54


if __name__ == '__main__':
    main()
