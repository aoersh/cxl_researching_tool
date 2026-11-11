#!/usr/bin/env python3
import json
import os
from pathlib import Path
import random
import time

import run_experiment as runner


def main():
    os.sched_setaffinity(0, {8})
    runner.ROOT = Path(__file__).resolve().parent / 'expansion-20261111'
    runner.ROOT.mkdir(exist_ok=True)
    cases = [(node, cpu, 'idle', ()) for node in (2, 3) for cpu in (0, 16)]
    cases += [(2, 0, 'read4', tuple(range(1, threads + 1))) for threads in (1, 2, 4)]
    cases += [(2, 0, 'write4', (1, 2, 3, 4))]
    jobs = []
    for repeat in range(1, 4):
        block = list(cases)
        random.Random(20261111 + repeat).shuffle(block)
        for node, cpu, condition, load_cpus in block:
            run_id = f'r{repeat}-n{node}-cpu{cpu}-{condition}-t{len(load_cpus)}'
            jobs.append((run_id, node, cpu, condition, load_cpus))
    runner.write_json(runner.ROOT / 'schedule.json', jobs)
    if not (runner.ROOT / 'metadata-start.json').exists():
        runner.write_json(runner.ROOT / 'metadata-start.json', runner.metadata())
    for index, (run_id, node, cpu, condition, load_cpus) in enumerate(jobs, 1):
        path = runner.ROOT / 'runs' / run_id / 'run.json'
        if path.exists():
            if json.loads(path.read_text())['status'] != 'ok':
                raise RuntimeError(f'Inspect failed run before resuming: {path}')
            continue
        print(f'[{index}/{len(jobs)}] {run_id} {runner.stamp()["wall"]}', flush=True)
        record = runner.execute_run(run_id, node, condition, probe_cpu=cpu,
                                    load_cpus=load_cpus)
        print(json.dumps(record['result']), flush=True)
        runner.summarize()
        time.sleep(.5)
    runner.write_json(runner.ROOT / 'metadata-end.json', runner.metadata())
    print('COMPLETE', flush=True)


if __name__ == '__main__':
    main()
