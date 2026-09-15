#!/usr/bin/env python3
"""Bounded live validation on an isolated local API; uses real OpenAI calls.

python tools/validate_live.py --output artifacts/audit-verification --smoke
python tools/validate_live.py --output artifacts/audit-verification
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from aerlink.config import load_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--smoke', action='store_true')
    modes.add_argument('--preview-only', action='store_true', help='Validate all cases in preview without repeating successful live write checks')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = load_config()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = 'http://127.0.0.1:{}'.format(port)
    environment = dict(os.environ, OPS_HOST='127.0.0.1', OPS_PORT=str(port),
                       OPS_BASE_URL=base, OPS_API_KEY=config.ops_api_key)
    python = getattr(sys, '_base_executable', sys.executable)
    log_path = output / ('smoke-server.log' if args.smoke else 'server.log')
    with log_path.open('w', encoding='utf-8') as log:
        server = subprocess.Popen([python, str(ROOT / 'env/ops_server.py')], cwd=ROOT,
            env=environment, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    def audit():
        request = urllib.request.Request(base + '/_audit', headers={'X-Ops-Key': config.ops_api_key})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    def worker(name, source, *extra):
        destination = output / name
        command = [sys.executable, str(ROOT / 'run.py'), *source, '--output', str(destination),
                   '--run-ceiling-usd', '0.25', '--journal-namespace', 'audit-' + name, *extra]
        result = subprocess.run(command, cwd=ROOT, env=environment, timeout=1200)
        if result.returncode:
            raise RuntimeError('{} worker exited {}'.format(name, result.returncode))
        summary = json.loads((destination / 'batch-summary.json').read_text(encoding='utf-8'))
        if summary['model_usage']['unresolved_calls'] or summary['model_usage']['calls'] == 0:
            raise RuntimeError('{} did not establish successful billable model use'.format(name))
        check = subprocess.run([sys.executable, 'tools/inspect_run.py', str(destination)],
                               cwd=ROOT, env=environment, timeout=120)
        if check.returncode:
            raise RuntimeError('{} inspection failed'.format(name))
        return summary
    try:
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError('Isolated operations server exited')
            try:
                with urllib.request.urlopen(base + '/health', timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        if args.smoke:
            worker('smoke', ['--inbound', 'cases/case-09/inbound.txt', '--meta', 'cases/case-09/meta.json'], '--dry-run')
            assert all(not rows for rows in audit()['writes'].values())
            return 0
        if args.preview_only:
            before = audit()['writes']
            summary = worker('dry-run', ['--cases', 'cases'], '--dry-run')
            after = audit()['writes']
            assert after == before, 'Dry run changed server state'
            (output / 'validation.json').write_text(json.dumps({
                'dry_run_added_zero_writes': True,
                'runs': {'dry-run': summary['model_usage']},
            }, indent=2), encoding='utf-8')
            print('All preview checks passed; evidence at {}'.format(output))
            return 0
        results = {}
        results['full-run'] = worker('full-run', ['--cases', 'cases'], '--reset-ops')
        before = audit()['writes']
        results['repeat-run'] = worker('repeat-run', ['--cases', 'cases'])
        assert audit()['writes'] == before, 'Repeat run created new writes'
        results['unseen'] = worker('unseen', ['--inbound', 'examples/unseen-case/inbound.txt',
            '--meta', 'examples/unseen-case/meta.json', '--case-id', 'unseen-audit'])
        before = audit()['writes']
        results['dry-run'] = worker('dry-run', ['--cases', 'cases'], '--dry-run')
        assert audit()['writes'] == before, 'Dry run changed server state'
        (output / 'validation.json').write_text(json.dumps({
            'repeat_added_zero_writes': True, 'dry_run_added_zero_writes': True,
            'runs': {name: summary['model_usage'] for name, summary in results.items()},
        }, indent=2), encoding='utf-8')
        print('All live checks passed; evidence at {}'.format(output))
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
