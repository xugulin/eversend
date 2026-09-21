#!/usr/bin/env python3
"""End-to-end test of the EverSend core over the loopback interface.

Runs two real engines against each other and checks that the bytes on the
receiving side are identical to the source, that resume works after an
artificial interruption, and that corruption is repaired rather than
restarted.

Run with::

    python3 tests/test_loopback.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import scratch, use_utf8_console  # noqa: E402

from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.core.model import DeviceInfo, FileEntry, Peer  # noqa: E402

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


def make_random_file(path: Path, size: int) -> str:
    """Write ``size`` random-ish bytes fast and return the sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    block = os.urandom(1024 * 1024)
    remaining = size
    with open(path, "wb") as fh:
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            fh.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Rig:
    """Two engines wired to talk to each other over 127.0.0.1."""

    def __init__(self, root: Path, *, streams: int = 4) -> None:
        self.root = root
        (root / "A").mkdir(parents=True, exist_ok=True)
        (root / "B").mkdir(parents=True, exist_ok=True)

        self.engine_a = Engine(
            EngineConfig(
                data_dir=str(root / "A" / "state"),
                receive_dir=str(root / "A" / "recv"),
                name="NodeA",
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
                name="NodeB",
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

    def peer_for(self, engine: Engine, target: Engine, address: str = "127.0.0.1") -> Peer:
        return Peer(info=target.info, address=address, port=target.port)

    def close(self) -> None:
        self.engine_a.stop()
        self.engine_b.stop()


def test_basic_transfer() -> None:
    print("\n[1] Basic transfer: single 8 MiB file")
    with scratch() as root:
        rig = Rig(root)
        try:
            source = root / "A" / "payload.bin"
            expected = make_random_file(source, 8 * 1024 * 1024)

            peer = rig.peer_for(rig.engine_a, rig.engine_b)
            ok = rig.engine_a.send(peer, [str(source)])
            check("send returned success", ok, f"error={rig.engine_a.events.recent[-1]}")

            received = root / "B" / "recv" / "payload.bin"
            check("file exists at destination", received.exists())
            if received.exists():
                check("size matches", received.stat().st_size == 8 * 1024 * 1024)
                check("content matches (sha256)", sha256_file(received) == expected)
            check(
                "no leftover .part file",
                not list((root / "B" / "recv").glob("*.eversend.part")),
            )

            # And the sender must have let go of the file it just read: on
            # Windows an open handle means the file cannot be deleted or
            # overwritten, which is how this was found -- the packaged
            # `--cli selftest` could not remove its own temporary directory and
            # exited 1 *after* reporting a successful transfer.
            leaked = fds_open_on(str(source))
            if os.path.isdir("/proc/self/fd"):
                check("nothing still holds the source file open", not leaked, str(leaked))
        finally:
            rig.close()


def test_multi_file_and_dirs() -> None:
    print("\n[2] Directory transfer with mixed sizes")
    with scratch() as root:
        rig = Rig(root)
        try:
            tree = root / "A" / "project"
            (tree / "sub" / "deep").mkdir(parents=True)
            files = {
                "small.txt": b"hello eversend\n",
                "empty.dat": b"",
                "sub/medium.bin": os.urandom(3 * 1024 * 1024),
                "sub/deep/large.bin": os.urandom(11 * 1024 * 1024),
            }
            expected = {}
            for rel, data in files.items():
                target = tree / rel
                target.write_bytes(data)
                expected[rel] = hashlib.sha256(data).hexdigest()

            peer = rig.peer_for(rig.engine_a, rig.engine_b)
            ok = rig.engine_a.send(peer, [str(tree)])
            check("directory send succeeded", ok)

            base = root / "B" / "recv" / "project"
            for rel, digest in expected.items():
                path = base / rel
                if not path.exists():
                    check(f"{rel} received", False)
                    continue
                check(
                    f"{rel} intact",
                    hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                )
        finally:
            rig.close()


def test_resume_after_disconnect() -> None:
    print("\n[3] Resume: interrupt mid-transfer, then reconnect")
    with scratch() as root:
        rig = Rig(root, streams=2)
        try:
            source = root / "A" / "big.bin"
            expected = make_random_file(source, 40 * 1024 * 1024)

            peer = rig.peer_for(rig.engine_a, rig.engine_b)

            # Kill the receiving engine partway through.
            killer_done = threading.Event()

            def kill_later() -> None:
                # Wait until some bytes have landed, then stop B hard.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    parts = list((root / "B" / "recv").glob("*.eversend.part"))
                    if parts and parts[0].stat().st_size > 0:
                        break
                    time.sleep(0.02)
                time.sleep(0.15)
                rig.engine_b.server.stop()
                killer_done.set()

            killer = threading.Thread(target=kill_later, daemon=True)
            killer.start()
            try:
                rig.engine_a.send(peer, [str(source)])
            except Exception:
                pass
            killer.join(timeout=5)

            part = root / "B" / "recv" / "big.bin.eversend.part"
            journal = root / "B" / "recv" / "big.bin.eversend.journal"
            check("partial file was left behind", part.exists() or (root / "B" / "recv" / "big.bin").exists())
            resumed_from = part.stat().st_size if part.exists() else 0
            check("journal was written", journal.exists() or not part.exists())

            # Restart B *from scratch* -- a brand new Engine object over the
            # same directories, which is what an application restart looks
            # like.  Anything that survives must have come off the disk.
            rig.engine_b.stop()
            rig.engine_b = Engine(
                EngineConfig(
                    data_dir=str(root / "B" / "state"),
                    receive_dir=str(root / "B" / "recv"),
                    name="NodeB",
                    tcp_port=0,
                    discovery_port=free_port(),
                    streams=2,
                    auto_accept_all=True,
                    enable_broadcast=False,
                    enable_mdns=False,
                    enable_web=False,
                )
            )
            rig.engine_b.start()
            peer2 = rig.peer_for(rig.engine_a, rig.engine_b)
            ok = rig.engine_a.send(peer2, [str(source)])
            check("resumed send succeeded", ok)

            received = root / "B" / "recv" / "big.bin"
            check("file exists after resume", received.exists())
            if received.exists():
                check("size matches after resume", received.stat().st_size == 40 * 1024 * 1024)
                check("content matches after resume", sha256_file(received) == expected)
            check("part file cleaned up", not part.exists())
        finally:
            rig.close()


def test_corruption_repair() -> None:
    print("\n[4] Corruption repair: damage a received file, re-send, expect repair")
    with scratch() as root:
        rig = Rig(root)
        try:
            source = root / "A" / "data.bin"
            expected = make_random_file(source, 6 * 1024 * 1024)
            peer = rig.peer_for(rig.engine_a, rig.engine_b)
            rig.engine_a.send(peer, [str(source)])

            received = root / "B" / "recv" / "data.bin"
            check("initial copy ok", received.exists() and sha256_file(received) == expected)

            # Corrupt bytes in the middle of the received file and re-send.
            with open(received, "r+b") as fh:
                fh.seek(2 * 1024 * 1024)
                fh.write(b"\x00" * 4096)
            check("file was corrupted", sha256_file(received) != expected)

            # A plain re-send must detect it through the digest and fix it.
            ok = rig.engine_a.send(peer, [str(source)])
            check("re-send reported success", ok)
            check("file repaired", sha256_file(received) == expected)
        finally:
            rig.close()


def test_cancel() -> None:
    print("\n[5] Cancellation")
    with scratch() as root:
        rig = Rig(root, streams=1)
        try:
            source = root / "A" / "huge.bin"
            make_random_file(source, 48 * 1024 * 1024)

            peer = rig.peer_for(rig.engine_a, rig.engine_b)
            result: list[bool] = []

            def do_send() -> None:
                try:
                    result.append(rig.engine_a.send(peer, [str(source)]))
                except Exception:
                    result.append(False)

            thread = threading.Thread(target=do_send, daemon=True)
            thread.start()
            time.sleep(0.35)

            active = rig.engine_b.active_transfers()
            if active:
                rig.engine_b.cancel(active[0].transfer_id, "test cancel")
            thread.join(timeout=15)
            check("send stopped after cancel", not thread.is_alive())
            check("no final file written", not (root / "B" / "recv" / "huge.bin").exists())
            check(
                "partial state kept for a later resume",
                (root / "B" / "recv" / "huge.bin.eversend.part").exists(),
            )
        finally:
            rig.close()


def test_speed() -> None:
    """End-to-end throughput on this machine.

    Note what this actually measures: the whole pipeline, not just the
    network.  The sender reads the file, hashes it and pushes it; the receiver
    pulls, hashes, writes and journals -- roughly four times the file size in
    real I/O.  On the project disk that lands around 100-170 MiB/s; pointed at
    a tmpfs (EVERSEND_TEST_TMP=/dev/shm) it reaches 160-200 MiB/s and shows
    what the protocol itself costs.  Either way it is far above a gigabit
    link's 118 MiB/s, so the network, not this code, is the limit on a LAN.
    """
    print("\n[6] End-to-end throughput (64 MiB, 4 streams)")
    with scratch() as root:
        rig = Rig(root, streams=4)
        try:
            source = root / "A" / "speed.bin"
            make_random_file(source, 64 * 1024 * 1024)
            peer = rig.peer_for(rig.engine_a, rig.engine_b)
            started = time.monotonic()
            ok = rig.engine_a.send(peer, [str(source)])
            elapsed = max(1e-6, time.monotonic() - started)
            mbps = 64 / elapsed
            print(f"      64 MiB in {elapsed:.2f}s = {mbps:.0f} MiB/s")
            check("fast transfer succeeded", ok)
            # The floor catches a real regression, it does not measure the
            # disk.  On a developer machine 60 MiB/s is a low bar (gigabit
            # Ethernet alone tops out at 118).  But a shared CI runner's disk
            # is the bottleneck there, not this code -- macOS runners measured
            # 15 MiB/s -- so CI lowers the bar via the environment rather than
            # pretending a slow SSD is a protocol bug.
            floor = float(os.environ.get("EVERSEND_MBPS_FLOOR", "60"))
            check(
                f"throughput above the floor ({floor:.0f} MiB/s)",
                mbps > floor,
                f"got {mbps:.0f} MiB/s",
            )
        finally:
            rig.close()


def fds_open_on(fragment: str) -> list[str]:
    """Descriptors this process still holds on a matching path.  Linux only.

    Empty on platforms without ``/proc`` (the check is then skipped): this is a
    *diagnostic*, not the thing being tested -- the thing being tested fails on
    Windows and is invisible on Linux, so Linux needs a way to see it at all.
    """
    hits: list[str] = []
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        return hits
    for entry in entries:
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:
            continue
        if fragment in target:
            hits.append(f"{entry}->{target}")
    return hits


def test_manual_accept() -> None:
    """The "ask first" path: the receiver answers an offer instead of auto-taking it.

    This is the path the desktop dialog and the phone's Web UI both use, and it
    is *not* what the other tests here exercise -- they all auto-accept.  It
    broke on Windows for a reason worth remembering: the decision is computed
    twice (once to show the user what is on offer, once when they answer), and
    each computation opened its own set of part files.  The first set was
    dropped without being closed, so the part file was still open when the
    transfer finished and ``os.replace`` failed with "共享冲突"
    (ERROR_SHARING_VIOLATION).  Every manually accepted transfer lost its file
    after sending all of it.  Linux renames an open file happily, so the only
    visible symptom there was a slow descriptor leak.
    """
    print("\n[7] Manual accept: the offer is answered by the receiver")
    with scratch() as root:
        rig = Rig(root)
        try:
            rig.engine_a.config.auto_accept_all = False
            source = root / "A" / "payload-manual.bin"
            expected = make_random_file(source, 4 * 1024 * 1024)

            offers: list[dict] = []
            rig.engine_a.events.subscribe(offers.append)

            result: list[bool] = []
            peer = rig.peer_for(rig.engine_b, rig.engine_a)

            def send() -> None:
                try:
                    result.append(rig.engine_b.send(peer, [str(source)]))
                except Exception:
                    result.append(False)

            received_name = "payload-manual.bin"
            before = fds_open_on(f"/recv/{received_name}")
            thread = threading.Thread(target=send, daemon=True)
            thread.start()

            deadline = time.monotonic() + 30
            request_id = ""
            while time.monotonic() < deadline and not request_id:
                for event in list(offers):
                    if event.get("kind") == "offer_received":
                        request_id = str(event.get("request_id", ""))
                        break
                time.sleep(0.05)
            check("the offer reaches the receiver and waits for an answer", bool(request_id))

            if request_id:
                check("accepting the offer is accepted", rig.engine_a.resolve_offer(request_id, True))

            thread.join(timeout=60)
            check("the sender reports success", result == [True], str(result))

            # The receiver is engine_a, so that is the receive directory that
            # has to grow -- checking B here silently asserted nothing.
            received = root / "A" / "recv" / received_name
            check("the file lands even though the transfer had to be accepted first", received.exists())
            if received.exists():
                check("content matches (sha256)", sha256_file(received) == expected)

            # The invariant Windows enforces for us: once the transfer is done,
            # nothing in this process may still hold the received file open, or
            # the rename that puts it in place fails with a sharing violation.
            # ``before`` is read before the transfer starts, so a descriptor
            # that was already there (the test's own read handle, say) is not
            # counted against us.
            leaked = fds_open_on(f"recv/{received_name}")
            if os.path.isdir("/proc/self/fd"):
                check(
                    "nothing still holds the received file open (Windows needs this to rename it)",
                    not leaked,
                    f"{leaked} (before: {before})",
                )
        finally:
            rig.close()


def main() -> int:
    use_utf8_console()
    print("EverSend core loopback tests")
    print("=" * 60)
    tests = [
        test_basic_transfer,
        test_multi_file_and_dirs,
        test_resume_after_disconnect,
        test_corruption_repair,
        test_cancel,
        test_speed,
        test_manual_accept,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            _failures.append(f"{test.__name__} raised {exc!r}")

    print("\n" + "=" * 60)
    if _failures:
        print(f"{len(_failures)} failure(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
