#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('condition', choices=('idle', 'read4', 'write4'))
    args = parser.parse_args()
    os.sched_setaffinity(0, {8})
    source = Path(__file__).resolve().with_name('run_experiment.py')
    spec = importlib.util.spec_from_file_location('experiment', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = args.directory.resolve()
    args.directory.mkdir(parents=True, exist_ok=True)
    module.write_json(args.directory / 'metadata.json', module.metadata())
    result = module.execute_run('node2-' + args.condition, 2, args.condition)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
