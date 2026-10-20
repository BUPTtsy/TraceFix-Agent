#!/usr/local/bin/python
"""Container-side Docker CLI replacement using the restricted file broker."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    root = Path('/agent-run/broker') / uuid.uuid4().hex
    root.mkdir()

    def atomic(path, data):
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)

    def cancel(*_):
        (root / 'cancel').touch()
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    atomic(root / 'request.json', json.dumps({'arguments': arguments, 'stream': '-i' in arguments}).encode())

    def forward_input():
        index = 0
        while chunk := os.read(sys.stdin.fileno(), 65_536):
            atomic(root / f'in-{index:08d}', chunk)
            index += 1
        (root / 'stdin-closed').touch()

    threading.Thread(target=forward_input, daemon=True).start()
    index = 0
    deadline = time.monotonic() + int(os.getenv('TRACEFIX_BROKER_TIMEOUT', '3600'))
    while time.monotonic() < deadline:
        path = root / f'out-{index:08d}'
        if path.is_file():
            sys.stdout.buffer.write(path.read_bytes())
            sys.stdout.buffer.flush()
            index += 1
            continue
        if (root / 'error.json').is_file():
            sys.stderr.write('Docker broker rejected or failed request\n')
            return 125
        if (root / 'done.json').is_file():
            result = json.loads((root / 'done.json').read_text())
            if index >= result.get('output_chunks', 0):
                return result['returncode']
        time.sleep(0.02)
    (root / 'cancel').touch()
    return 124


if __name__ == '__main__':
    sys.exit(main())
