#!/usr/bin/env python3
"""Bind UDP, print every datagram with ISO-8601 timestamp prefix.

Use to capture decoded MeshCore JSON frames emitted by `demodmeshcore`
when configured with `sendJsonViaUdp=1`.

Usage:
    python3 udp_dump.py --port 9999 > tmp/rx.log
"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timezone


def main():
    p = argparse.ArgumentParser(
        description="Bind UDP, print every datagram with ISO-8601 timestamp prefix.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--bufsize", type=int, default=65536)
    args = p.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((args.host, args.port))
    print(f"listening on {args.host}:{args.port}", file=sys.stderr, flush=True)

    while True:
        data, addr = s.recvfrom(args.bufsize)
        ts = datetime.now(timezone.utc).isoformat()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = repr(data)
        print(f"{ts} {addr[0]}:{addr[1]} {text}", flush=True)


if __name__ == "__main__":
    main()
