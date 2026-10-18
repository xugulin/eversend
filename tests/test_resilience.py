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
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import peak_rss_mib, scratch, use_utf8_console  # noqa: E402

from eversend.core.chunkstore import ResumeIndex  # noqa: E402
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


def disk_usage(path: Path) -> int:
    """Bytes a directory really occupies (allocated blocks, not apparent size).

    The bug being measured is a preallocated ``.eversend.part``: its *size* is
    the whole file from the start, and so is the space it occupies, so only a
    block-based figure can show the space coming back.
    """
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if not entry.is_file():
                continue
            info = entry.stat()
        except OSError:
            continue
        total += (info.st_blocks * 512) if info.st_blocks else info.st_size
    return total


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
        #: One shared rate budget for every pump (see :meth:`_throttle`).
        self._rate_lock = threading.Lock()
        self._rate_next = 0.0

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

    def _throttle(self, count: int) -> None:
        """Charge ``count`` bytes against one budget shared by all pumps.

        Sleeping ``count / throttle_bps`` inside each pump looks right and is
        not: the transfer runs four streams, so the "8 MB/s" link silently
        became 32 MB/s and the test's own assertion (``throughput < 20``) went
        red on a fast CI runner while nothing about the product had changed.
        A single budget is what "an 8 MB/s link" actually means.
        """
        charge = count / self.throttle_bps
        with self._rate_lock:
            now = time.monotonic()
            self._rate_next = max(now, self._rate_next) + charge
            wait = self._rate_next - now
        if wait > 0:
            time.sleep(wait)

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
                self._throttle(count)
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


def test_peer_vanishes_before_answering() -> None:
    """A peer that resets the connection instead of answering the offer.

    ``send()`` is called by the desktop window, by the CLI and by the web UI's
    upload worker, and all three expect a *boolean* -- not an exception.  When
    something on the path resets the connection after it is established but
    before the peer answers the offer, the reset used to escape as
    ``ConnectionResetError`` and take the caller down with it.  This is
    deterministic where the hostile proxy was only occasionally lucky: the
    listener accepts the connection and immediately RSTs it.
    """
    print("\n[5] The peer resets the connection instead of answering the offer")
    with scratch() as root:
        rig = Rig(root, streams=2)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def hang_up() -> None:
            while not stop.is_set():
                try:
                    listener.settimeout(0.2)
                    conn, _ = listener.accept()
                except (OSError, socket.timeout):
                    continue
                # SO_LINGER=0 makes close() send a RST rather than a FIN.
                conn.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                conn.close()

        try:
            source = root / "A" / "vanishing.bin"
            source.write_bytes(os.urandom(2 * 1024 * 1024))
            worker = threading.Thread(target=hang_up, daemon=True)
            worker.start()

            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=port)
            try:
                ok = rig.engine_a.send(peer, [str(source)])
                raised = ""
            except Exception as exc:  # noqa: BLE001 - that is the bug
                ok, raised = False, repr(exc)

            check("send() reports failure instead of raising", not raised, raised)
            check("send() says the transfer did not happen", ok is False, str(ok))
            finished = [
                event
                for event in rig.engine_a.events.recent
                if event.get("kind") == "send_finished"
            ]
            check(
                "the failure is visible as an event with a reason",
                bool(finished) and bool(finished[-1].get("error")),
                str(finished[-1:]),
            )
        finally:
            stop.set()
            try:
                listener.close()
            except OSError:
                pass
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


def test_cancel_reclaims_disk() -> None:
    """Audit #3, reproduced at the audit's own timing.

    A cancelled receive must not leave the preallocated part file behind: the
    part is the size of the *whole* file from the first moment, so cancelling a
    10 GB transfer used to eat 10 GB that no screen ever showed.
    """
    print("\n[6] Cancelling a 200 MiB receive gives the disk back")
    with scratch() as root:
        rig = Rig(root, streams=4)
        recv_dir = root / "B" / "recv"
        try:
            source = root / "A" / "大文件.bin"
            size = 200 * 1024 * 1024
            with open(source, "wb") as fh:
                fh.truncate(size)  # sparse: the content is irrelevant here
            before = disk_usage(recv_dir)
            part = recv_dir / "大文件.bin.eversend.part"

            result: list[bool] = []
            peer = Peer(info=rig.engine_b.info, address="127.0.0.1", port=rig.engine_b.port)
            thread = threading.Thread(
                target=lambda: result.append(rig.engine_a.send(peer, [str(source)])),
                daemon=True,
            )
            thread.start()

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not (
                part.exists() and rig.engine_b.active_transfers()
            ):
                time.sleep(0.02)
            reserved = 0
            if part.exists():
                info = part.stat()
                reserved = (info.st_blocks * 512) if info.st_blocks else info.st_size
            active = rig.engine_b.active_transfers()
            check("取消时接收正在跑，.part 已按整份文件预分配", bool(active) and reserved >= size // 2,
                  f"active={bool(active)} 实占={reserved / 1048576:.0f} MiB")
            if active:
                rig.engine_b.cancel(active[0].transfer_id, "复核：用户取消")
            thread.join(timeout=20)

            # The audit looked 1.2 s after the cancel and still found a full-size
            # part file; that window is the assertion.
            time.sleep(1.2)
            leftovers = sorted(
                p.name for p in recv_dir.iterdir()
                if p.name.endswith((".eversend.part", ".eversend.journal"))
            )
            after = disk_usage(recv_dir)
            check("取消后接收目录里没有满尺寸 .part 残片",
                  not [name for name in leftovers if name.endswith(".eversend.part")],
                  f"目录={leftovers}")
            check("取消后目录占用回到取消前",
                  after <= before + 1024 * 1024,
                  f"取消前 {before} 字节 → 峰值 {reserved} → 1.2s 后 {after} 字节")
            print(
                f"      实测：.part 实占 {reserved / 1048576:.0f} MiB，"
                f"取消 1.2s 后目录占用 {after / 1048576:.2f} MiB（取消前 {before} 字节）"
            )

            # And the engine's own start-up cleanup must reclaim anything a
            # crash does leave behind, in the receive folder *and* the data dir.
            guarded = recv_dir / "崩溃遗留.bin.eversend.part"
            guarded.write_bytes(b"\0" * (16 * 1024 * 1024))
            (recv_dir / "崩溃遗留.bin.eversend.journal").write_bytes(b"SWDJ")
            old = time.time() - 30 * 24 * 3600
            os.utime(guarded, (old, old))
            os.utime(recv_dir / "崩溃遗留.bin.eversend.journal", (old, old))
            swept = ResumeIndex(str(rig.engine_b.config.data_dir)).sweep(max_age_seconds=0)
            check("sweep 能收掉接收目录里的 .part/.journal（不是只扫 data_dir）",
                  not guarded.exists() and any("接收" in p or "recv" in p for p in swept),
                  f"删除={swept}")
        finally:
            rig.close()


def test_leftovers_are_swept_not_resumed_away() -> None:
    """The cleanup must scan both folders, and must not eat a resumable file."""
    print("\n[7] 启动清理：扫接收目录和 data_dir，但不碰还能续传的残片")
    with scratch() as root:
        data_dir = root / "state"
        recv_dir = root / "recv"
        data_dir.mkdir(parents=True, exist_ok=True)
        recv_dir.mkdir(parents=True, exist_ok=True)
        month_ago = time.time() - 30 * 24 * 3600

        def leftover(directory: Path, name: str, age: float) -> tuple[Path, Path]:
            part = directory / (name + ".eversend.part")
            journal = directory / (name + ".eversend.journal")
            # Real allocation, like a preallocated part file.
            part.write_bytes(b"\0" * (8 * 1024 * 1024))
            journal.write_bytes(b"SWDJ")
            os.utime(part, (age, age))
            os.utime(journal, (age, age))
            return part, journal

        stale_recv = leftover(recv_dir, "上个月取消的.bin", month_ago)
        stale_data = leftover(data_dir, "数据目录里的幽灵.bin", month_ago)
        fresh_recv = leftover(recv_dir, "暂停后要继续的.bin", time.time())
        orphan = recv_dir / "孤立.bin.eversend.part"
        orphan.write_bytes(b"\0" * (4 * 1024 * 1024))
        os.utime(orphan, (month_ago, month_ago))

        engine = Engine(
            EngineConfig(
                data_dir=str(data_dir),
                receive_dir=str(recv_dir),
                name="清理机",
                tcp_port=0,
                discovery_port=free_port(),
                auto_accept_all=True,
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
            )
        )
        engine.start()
        try:
            check("启动清理收掉了接收目录里的陈年残片（.part 和 journal 一起）",
                  not stale_recv[0].exists() and not stale_recv[1].exists(),
                  str([p.name for p in stale_recv if p.exists()]))
            check("启动清理也扫 data_dir",
                  not stale_data[0].exists() and not stale_data[1].exists(),
                  str([p.name for p in stale_data if p.exists()]))
            check("没有 journal 的孤立 .part 也收掉（它本来就续不了）",
                  not orphan.exists(), str(orphan))
            check("刚暂停、还能续传的残片必须留着",
                  fresh_recv[0].exists() and fresh_recv[1].exists(),
                  str([p.name for p in fresh_recv if not p.exists()]))

            # A bare ResumeIndex(data_dir) -- what the start-up path and the
            # audit's recheck both use -- must know about the receive folder.
            ghost = recv_dir / "幽灵.bin.eversend.journal"
            ghost.write_bytes(b"x")
            removed = ResumeIndex(str(data_dir)).sweep(max_age_seconds=0)
            check("拿 data_dir 建索引也能扫到接收目录的 journal",
                  not ghost.exists(), f"sweep 删除={removed}")
        finally:
            engine.stop()


def test_open_failure_is_visible() -> None:
    """Audit #4: an opener that fails must not look like a success."""
    print("\n[8] 「打开」失败能被调用方知道（并拿到给用户看的理由）")
    from eversend.core import platform_open as po

    if not po.IS_LINUX:
        print("      (not Linux: xdg-open stub does not apply)")
        return
    with scratch() as root:
        stub_dir = root / "bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "xdg-open"
        stub.write_text("#!/bin/sh\necho 'no method available' >&2\nexit 3\n", encoding="utf-8")
        stub.chmod(0o755)
        probe = root / "未知类型.zzz"
        probe.write_bytes(b"hello")

        old_path = os.environ.get("PATH", "")
        shown: list[str] = []
        po.set_failure_handler(shown.append)
        try:
            os.environ["PATH"] = f"{stub_dir}{os.pathsep}{old_path}"
            ok = po.open_path(str(probe))
            check("xdg-open 退出码 3 时 open_path 返回 False（不再谎报成功）", ok is False, f"返回={ok}")
            check("失败原因可读（含退出码/错误行）",
                  "退出码 3" in po.last_open_error(), repr(po.last_open_error()))
            check("装了 handler 后失败消息交给它 —— 前端据此在状态行提示用户",
                  bool(shown) and "退出码 3" in shown[-1], str(shown[-1:]))
            ok2, why = po.open_path_report(str(probe))
            check("open_path_report 给出 (False, 原因)", ok2 is False and bool(why), f"{ok2!r} {why!r}")

            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            check("能打开的路径仍然返回 True（不是无脑 False）",
                  po.open_path(str(probe)) is True, repr(po.last_open_error()))

            gone = root / "已经被移走的文件.zzz"
            check("文件不在时返回 False 且有原因",
                  po.open_path(str(gone)) is False and bool(po.last_open_error()),
                  repr(po.last_open_error()))
        finally:
            os.environ["PATH"] = old_path
            po.set_failure_handler(None)


def main() -> int:
    use_utf8_console()
    print("EverSend resilience tests")
    print("=" * 66)
    tests = [
        test_connection_kills,
        test_kill_all_early,
        test_throttled_link,
        test_large_file,
        test_peer_vanishes_before_answering,
        test_cancel_reclaims_disk,
        test_leftovers_are_swept_not_resumed_away,
        test_open_failure_is_visible,
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
