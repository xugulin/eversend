#!/usr/bin/env python3
"""A stand-in computer that answers EverSend discovery probes.

The Android CI job runs the emulator behind a NAT: its broadcasts never reach
the host, so "search for the computer" cannot be tested against the real
desktop there.  Unicast in both directions *does* cross that NAT (it is how the
app talks HTTP to 10.0.2.2), and unicast is exactly the path the app now uses
first -- it sweeps every host in its subnet with a small UDP probe.

So the job starts this responder on the host, points the app at its port with
``-e udpPort``, and the test then proves the whole chain on a real device:
the probe leaves the phone, crosses the network, and the announcement that
comes back is parsed into a usable address.

Run::

    python3 tools/ci_udp_responder.py --port 53999 --name CI-宿主电脑
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="EverSend discovery responder for CI")
    parser.add_argument("--port", type=int, default=53999)
    parser.add_argument("--name", default="CI-宿主电脑")
    parser.add_argument("--web-port", type=int, default=53119)
    parser.add_argument("--seconds", type=float, default=180.0, help="how long to stay up")
    parser.add_argument("--ready-file", default="", help="touch this file once bound")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)
    announcement = json.dumps(
        {
            "t": "eversend/1",
            "id": "ci-host-responder",
            "n": args.name,
            "k": "desktop",
            "p": "linux",
            "v": "1.0.0",
            "port": 53117,
            "web": args.web_port,
            "ts": int(time.time()),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    print(f"responder listening on udp/{args.port} as {args.name!r}", flush=True)
    if args.ready_file:
        try:
            open(args.ready_file, "w").write("ready\n")
        except OSError:
            pass

    deadline = time.monotonic() + args.seconds
    seen = 0
    while time.monotonic() < deadline:
        try:
            data, address = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if b"eversend/1" not in data:
            continue
        seen += 1
        if seen <= 3:  # one line per probe is noise; the first few are the proof
            print(f"probe #{seen} from {address}", flush=True)
        try:
            sock.sendto(announcement, address)
        except OSError as exc:
            print(f"reply failed: {exc}", file=sys.stderr, flush=True)
    print(f"responder saw {seen} probe(s)", flush=True)
    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
