#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description='Verify original experiment files, including compressed traces.')
    parser.add_argument('--restore', action='store_true', help='Restore missing original scheduler files after verification.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'experiments' / 'cxl-tail-20261109'
    manifest = json.loads((root / 'source-manifest.json').read_text())['files']
    failures = []
    compressed_paths = []
    for relative, expected in manifest.items():
        path = root / relative
        if path.is_file():
            handle = path.open('rb')
        elif path.with_name(path.name + '.gz').is_file():
            compressed = path.with_name(path.name + '.gz')
            handle = gzip.open(compressed, 'rb')
            compressed_paths.append((compressed, path))
        else:
            failures.append(relative + ': missing')
            continue
        digest = hashlib.sha256()
        size = 0
        with handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
            failures.append(relative + ': checksum/size mismatch')
    if failures:
        raise SystemExit('\n'.join(failures))
    if args.restore:
        for compressed, path in compressed_paths:
            temporary = path.with_name(path.name + '.restoring')
            with gzip.open(compressed, 'rb') as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            os.replace(temporary, path)
            print('Restored:', path.relative_to(root))
    print(f'Verified {len(manifest)} original files; {len(compressed_paths)} read from gzip.')


if __name__ == '__main__':
    main()
