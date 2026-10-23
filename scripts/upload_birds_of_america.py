#!/usr/bin/env python3
"""Upload a frozen artwork plan and independently verify every public byte.

Uses the operator's existing Wrangler authentication without reading or printing
credentials. Only the dedicated bundle bucket and checksum-addressed PNG/JSON
namespace are writable. Existing objects must match before being reused.
"""
import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import time
import tempfile
from pathlib import Path

ORIGIN = 'https://avianvisitors.com/api/bundles/'
BUCKET = 'avianvisitors-bundles'


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class TransportError(RuntimeError):
    pass


def public_check(item):
    for attempt in range(3):
        try:
            return public_check_once(item)
        except TransportError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def public_check_once(item):
    url = ORIGIN + item['kind'] + '/' + item['sha256'] + item['suffix']
    # curl uses the host's working TLS/proxy transport. Stream the artwork
    # into a digest rather than retaining another download on disk.
    with tempfile.TemporaryDirectory(prefix='avian-artwork-check-') as directory:
        headers = Path(directory) / 'headers'
        process = subprocess.Popen(['curl', '--http1.1', '--silent', '--show-error', '--max-time', '300',
                                    '--connect-timeout', '20', '--speed-limit', '1024', '--speed-time', '30',
                                    '--proto', '=https', '--dump-header', str(headers), url],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        actual, size = hashlib.sha256(), 0
        while chunk := process.stdout.read(1024 * 1024):
            actual.update(chunk)
            size += len(chunk)
            if size > item['bytes']:
                process.kill()
                process.communicate()
                raise RuntimeError('Remote object exceeds frozen size')
        _, error = process.communicate()
        if process.returncode:
            raise TransportError('Public artwork transport failed')
        header = headers.read_text().lower()
        status = [line.split()[1] for line in header.splitlines() if line.startswith('http/')][-1]
        if status == '404':
            return False
        if status in {'429', '500', '502', '503', '504'}:
            raise TransportError(f'Public artwork returned HTTP {status}')
        if status != '200':
            raise RuntimeError(f'Public artwork returned HTTP {status}')
        if size != item['bytes'] or actual.hexdigest() != item['sha256']:
            raise RuntimeError('Remote object differs from frozen artwork')
        if 'access-control-allow-origin: *' not in header:
            raise RuntimeError('Public artwork lacks anonymous CORS')
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--wrangler', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--resume-verified', action='store_true', help='Reuse prior exact anonymous-download receipts for this frozen plan')
    parser.add_argument('--workers', type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    raw = args.plan.read_bytes()
    plan = json.loads(raw)
    config = args.config.read_text()
    if '557e541291a977fc1683c53376eb56a0' not in config or 'name = "avianvisitors-bundles"' not in config:
        raise ValueError('Wrong publication account or Worker')
    seen = set()
    for item in plan:
        if (item['kind'], item['suffix']) not in {('objects', '.png'), ('manifests', '.json')}:
            raise ValueError('Unsupported publication namespace')
        path = Path(item['path'])
        if path.is_symlink() or path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
            raise ValueError(f'Frozen input changed: {path.name}')
        key = (item['kind'], item['sha256'])
        if key in seen:
            raise ValueError('Duplicate publication object')
        seen.add(key)
    print(json.dumps({'plan_sha256': hashlib.sha256(raw).hexdigest(), 'files': len(plan), 'bytes': sum(i['bytes'] for i in plan)}), flush=True)
    if not args.apply and not args.verify_only:
        return
    verified_before = set()
    if args.resume_verified and args.receipt.exists() and not args.verify_only:
        expected = {(i['kind'], i['sha256']): i['bytes'] for i in plan}
        for line in args.receipt.read_text().splitlines():
            row = json.loads(line)
            key = (row['kind'], row['sha256'])
            if key in expected and row['bytes'] == expected[key] and isinstance(row.get('verified_at'), int):
                verified_before.add(key)
        print(f'Resuming {len(verified_before)} previously checksum-verified public files', flush=True)

    def transfer(item):
        exists = public_check(item)
        if not exists:
            if args.verify_only:
                raise RuntimeError('Frozen publication is missing a public object')
            key = f"bundles/{item['kind']}/sha256/{item['sha256'][:2]}/{item['sha256']}{item['suffix']}"
            command = ['node', str(args.wrangler), 'r2', 'object', 'put', f'{BUCKET}/{key}',
                       '--file', item['path'], '--remote', '--config', str(args.config),
                       '--content-type', 'image/png' if item['kind'] == 'objects' else 'application/json',
                       '--cache-control', 'public, max-age=31536000, immutable']
            for attempt in range(3):
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode == 0:
                    break
                # An uncertain upload may have succeeded. Verify before retrying.
                if public_check(item):
                    break
                if attempt == 2:
                    # Do not echo CLI diagnostics that could include operator data.
                    raise RuntimeError(f"Upload failed for {item['sha256']} (Wrangler exit {result.returncode})")
                time.sleep(2 ** (attempt + 1))
            if not public_check(item):
                raise RuntimeError('Uploaded object did not become publicly readable')
        return {'kind': item['kind'], 'sha256': item['sha256'], 'bytes': item['bytes'], 'verified_at': int(time.time()), 'reused': exists}

    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open('a') as receipt:
        # Publish manifests last, after all referenced artwork has passed its
        # independent anonymous download. Catalog discovery is a separate step.
        for kind in ('objects', 'manifests'):
            items = [i for i in plan if i['kind'] == kind and (kind, i['sha256']) not in verified_before]
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
            futures = [executor.submit(transfer, item) for item in items]
            try:
                for number, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    verified = future.result()
                    receipt.write(json.dumps(verified, sort_keys=True) + '\n')
                    receipt.flush()
                    if number % 20 == 0 or number == len(items):
                        print(f'{kind}: verified {number}/{len(items)}', flush=True)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    main()
