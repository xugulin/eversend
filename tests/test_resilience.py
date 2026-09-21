#!/usr/bin/env python3
"""Adversarial tests: prove transfers survive a network that actively breaks.

The loopback tests in ``test_loopback.py`` run over a perfect link.  Real
networks are not perfect, and "传输一定要稳定" is the requirement that matters
most, so these tests put a hostile proxy between the two engines:

* it kills a random live connection every few hundred milliseconds,
* it throttles throughput to a fraction of loopback,
* it introduces latency and jitter.

A transfer must still finish, and the bytes must still be identical.  This is
what exercises stream replacement (``NEED_STREAMS``), chunk re-queueing and
resume all at once.

Run with::

    python3 tests/test_resilience.py
"""

from __future__ import annotations

import hashlib
import os
import random
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import peak_rss_mib, scratch, use_utf8_console  # noqa: E402

from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.core.model import Peer  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name} {detail}")
        _failures.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class HostileProxy:
    """A TCP proxy that sabotages the connections passing through it.

    Killing a connection is the interesting failure: it is exactly what a
    Wi-Fi drop, a NAT rebinding or a flaky VPN does, and unlike a slow link it
    cannot be papered over by TCP retransmission.
    """

    def __init__(
        self,
        target_port: int,
        *,
        kill_interval: float = 0.4,
        max_kills: int = 6,
        throttle_bps: int = 0,
        latency: float = 0.0,
        jitter: float = 0.0,
        seed: int = 1234,
    ) -> None:
        self.target_port = target_port
        self.kill_interval = kill_interval
        self.max_kills = max_kills
        self.throttle_bps = throttle_bps
        self.latency = latency
        self.jitter = jitter
        self.random = random.Random(seed)

        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(0.3)
        self.port = self._server.getsockname()[1]

        self._pairs: list[tuple[socket.socket, socket.socket]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.kills = 0
        self.bytes_forwarded = 0

    def start(self) -> None:
        self._stop.clear()
        self._spawn(self._accept_loop)
        self._spawn(self._sabotage_loop)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        with self._lock:
            pairs = list(self._pairs)
            self._pairs.clear()
        for a, b in pairs:
            for sock in (a, b):
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=1.0)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
            except OSError:
                client.close()
                continue
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._pairs.append((client, upstream))
            self._spawn(self._pump, client, upstream)
            self._spawn(self._pump, upstream, client)

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        """Forward bytes one way, applying latency and throttling."""
        source.settimeout(0.5)
        buffer = bytearray(256 * 1024)
        view = memoryview(buffer)
        while not self._stop.is_set():
            try:
                count = source.recv_into(view)
            except socket.timeout:
                continue
            except OSError:
                break
            if count == 0:
                break
            if self.latency or self.jitter:
                time.sleep(max(0.0, self.latency + self.random.uniform(0, self.jitter)))
            if self.throttle_bps:
                # Sleep proportionally to the block just read.
                time.sleep(count / self.throttle_bps)
            try:
                sink.sendall(view[:count])
                self.bytes_forwarded += count
            except OSError:
                break
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _sabotage_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.kill_interval):
                return
            if self.kills >= self.max_kills:
                continue
            with self._lock:
                if not self._pairs:
                    continue
                victim = self.random.choice(self._pairs)
                self._pairs.remove(victim)
            self.kills += 1
            for sock in victim:
                # SO_LINGER 0 makes close() send RST: an abrupt, realistic
                # failure rather than a graceful shutdown the peer can foresee.
                try:
                    import struct as _struct

                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _struct.pack("ii", 1, 0))
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass


class Rig:
    def __init__(self, root: Path, streams: int = 4) -> None:
        (root / "A").mkdir(parents=True, exist_ok=True)
        (root / "B").mkdir(parents=True, exist_ok=True)
        self.engine_a = Engine(
            EngineConfig(
                data_dir=str(root / "A" / "state"),
                receive_dir=str(root / "A" / "recv"),
                name="Sender",
                tcp_port=0,
                discovery_port=free_port(),
                streams=streams,
                auto_accept_all=True,
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
            )
        )
        self.engine_b = Engine(
            EngineConfig(
                data_dir=str(root / "B" / "state"),
                receive_dir=str(root / "B" / "recv"),
                name="Receiver",
                tcp_port=0,
                discovery_port=free_port(),
                streams=streams,
                auto_accept_all=True,
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
            )
        )
        self.engine_a.start()
        self.engine_b.start()

    def close(self) -> None:
        self.engine_a.stop()
        self.engine_b.stop()


def test_connection_kills() -> None:
    print("\n[1] Connections killed mid-transfer (simulates Wi-Fi drops / NAT rebinding)")
    with scratch() as root:
        rig = Rig(root, streams=4)
        # Throttle as well as kill: an unthrottled loopback transfer finishes
        # in a fraction of a second and would never meet the saboteur, making
        # the test pass without proving anything.
        proxy = HostileProxy(
            rig.engine_b.port,
            kill_interval=0.35,
            max_kills=8,
            throttle_bps=6 * 1024 * 1024,
        )
        proxy.start()
        try:
            source = root / "A" / "payload.bin"
            payload = os.urandom(24 * 1024 * 1024)
            source.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()

            # Dial through the proxy so every connection is fair game.
            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=proxy.port)
            started = time.monotonic()
            ok = rig.engine_a.send(peer, [str(source)])
            elapsed = time.monotonic() - started

            received = root / "B" / "recv" / "payload.bin"
            errors = [
                e for e in rig.engine_b.events.recent
                if e.get("kind") in ("transfer_finished", "file_failed") and e.get("error")
            ]
            detail = ""
            if not ok and errors:
                detail = f"receiver said: {errors[-1].get('error')}"
            check("transfer completed despite kills", ok, detail or f"proxy killed {proxy.kills}")
            check(
                "proxy killed several connections during the transfer",
                proxy.kills >= 3,
                f"only {proxy.kills} kill(s) landed -- the test is not hostile enough",
            )
            check("file landed", received.exists(), "receiver did not finalise the file")
            if received.exists():
                check("bytes identical", sha256_file(received) == expected)
                check("size correct", received.stat().st_size == len(payload))
            else:
                parts = list((root / "B" / "recv").glob("*.part"))
                if parts:
                    print(f"      partial file kept: {parts[0].stat().st_size} of {len(payload)} bytes (resumable)")
            print(f"      24 MiB through a hostile proxy in {elapsed:.1f}s ({proxy.kills} kills)")
        finally:
            proxy.stop()
            rig.close()


def test_kill_all_early() -> None:
    print("\n[2] Every stream killed repeatedly, then a clean retry")
    with scratch() as root:
        rig = Rig(root, streams=2)
        proxy = HostileProxy(rig.engine_b.port, kill_interval=0.15, max_kills=12)
        proxy.start()
        try:
            source = root / "A" / "torture.bin"
            payload = os.urandom(16 * 1024 * 1024)
            source.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()

            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=proxy.port)
            first = rig.engine_a.send(peer, [str(source)])

            # Whether or not the first attempt finished under that abuse, a
            # second attempt over a working link must produce a correct file,
            # resuming rather than restarting.
            proxy.stop()
            proxy = HostileProxy(rig.engine_b.port, kill_interval=999, max_kills=0)
            proxy.start()
            peer2 = Peer(info=rig.engine_b.info, address="127.0.0.1", port=proxy.port)
            second = rig.engine_a.send(peer2, [str(source)])

            received = root / "B" / "recv" / "torture.bin"
            check("recovery attempt succeeded", second)
            check("file landed", received.exists())
            if received.exists():
                check("bytes identical after abuse", sha256_file(received) == expected)
            print(f"      first attempt ok={first}, recovery ok={second}")
        finally:
            proxy.stop()
            rig.close()


def test_throttled_link() -> None:
    print("\n[3] Throttled link (8 MB/s) with latency and jitter — integrity under backpressure")
    with scratch() as root:
        rig = Rig(root, streams=4)
        proxy = HostileProxy(
            rig.engine_b.port,
            kill_interval=999,
            max_kills=0,
            throttle_bps=8 * 1024 * 1024,
            latency=0.001,
            jitter=0.004,
        )
        proxy.start()
        try:
            source = root / "A" / "slow.bin"
            payload = os.urandom(12 * 1024 * 1024)
            source.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()

            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=proxy.port)
            started = time.monotonic()
            ok = rig.engine_a.send(peer, [str(source)])
            elapsed = time.monotonic() - started
            throughput = 12 / max(1e-6, elapsed)

            received = root / "B" / "recv" / "slow.bin"
            check("throttled transfer succeeded", ok)
            check("bytes identical under backpressure", received.exists() and sha256_file(received) == expected)
            check(
                "throughput respects the throttle",
                throughput < 20,
                f"got {throughput:.1f} MiB/s, throttle was 8 MiB/s",
            )
            print(f"      12 MiB at {throughput:.1f} MiB/s through an 8 MiB/s throttle")
        finally:
            proxy.stop()
            rig.close()


def test_large_file() -> None:
    print("\n[4] Large file (512 MiB) — sustained transfer, bounded memory")
    with scratch() as root:
        rig = Rig(root, streams=4)
        try:
            source = root / "A" / "large.bin"
            size = 512 * 1024 * 1024
            block = os.urandom(1024 * 1024)
            digest = hashlib.sha256()
            with open(source, "wb") as fh:
                written = 0
                while written < size:
                    fh.write(block)
                    digest.update(block)
                    written += len(block)
            expected = digest.hexdigest()

            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=rig.engine_b.port)
            peak_rss = {"value": 0.0}
            stop_monitor = threading.Event()

            def monitor() -> None:
                # peak_rss_mib handles the three platform differences: no
                # `resource` on Windows, and ru_maxrss in bytes on macOS but
                # kilobytes on Linux.
                while not stop_monitor.is_set():
                    peak_rss["value"] = max(peak_rss["value"], peak_rss_mib())
                    time.sleep(0.1)

            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()

            started = time.monotonic()
            ok = rig.engine_a.send(peer, [str(source)])
            elapsed = max(1e-6, time.monotonic() - started)
            stop_monitor.set()
            watcher.join(timeout=1)

            received = root / "B" / "recv" / "large.bin"
            check("512 MiB transfer succeeded", ok)
            check("512 MiB bytes identical", received.exists() and sha256_file(received) == expected)
            check(
                "memory stayed bounded",
                # A machine that cannot report memory must not fail the test;
                # 0 means "unknown" here, not "used nothing".
                peak_rss["value"] == 0.0 or peak_rss["value"] < 900,
                f"peak RSS was {peak_rss['value']:.0f} MiB",
            )
            print(
                f"      512 MiB in {elapsed:.1f}s = {512 / elapsed:.0f} MiB/s, "
                f"peak RSS {peak_rss['value']:.0f} MiB"
            )
        finally:
            rig.close()


def main() -> int:
    use_utf8_console()
    print("EverSend resilience tests")
    print("=" * 66)
    tests = [
        test_connection_kills,
        test_kill_all_early,
        test_throttled_link,
        test_large_file,
    ]
    for test in tests:
        try:
            test()
        except Exception:
            import traceback

            traceback.print_exc()
            _failures.append(f"{test.__name__} raised")

    print("\n" + "=" * 66)
    if _failures:
        print(f"{len(_failures)} failure(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("All resilience checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
