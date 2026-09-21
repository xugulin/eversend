#!/usr/bin/env python3
"""并发正确性测试：AEAD nonce 不能被两条线程同时拿到。

为什么单独有这个文件
--------------------
CI 上出现过一次 `chunk authentication failed`，而且是间歇性的 —— 本机复现不了。
根因是 `Cipher` 的 nonce 计数器不是原子的，而 `seal()` 在发送锁**外面**调用：

    线程 A: seal() 拿到 nonce 7  ────────────────┐
    线程 B: seal() 拿到 nonce 8  ──────┐         │
    线程 B: 抢到锁，先写出 nonce 8 的帧  │         │
    线程 A: 再抢到锁，写出 nonce 7 的帧  ┘         │
                                      ↑ 对端按自己的计数器解，全错

后果比"校验失败"严重得多：**两条不同的帧用了同一个 nonce**，那是 AEAD 唯一一种
会破坏机密性而不只是完整性的错误。

这个测试用真 socketpair 起两条线程往同一条连接上狂发，逐帧解密并核对序号 ——
只要 nonce 分配顺序和上线顺序有任何一次不一致，它就会失败。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _scratch import use_utf8_console  # noqa: E402

from eversend.core import crypto  # noqa: E402
from eversend.core.connection import Connection  # noqa: E402
from eversend.core.constants import MSG_PING, MSG_PROGRESS  # noqa: E402
from eversend.core.framing import ConnectionClosed  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    if not condition:
        _failures.append(name)


def make_pair(key: bytes):
    """Two Connections over a real socketpair, session already established."""
    # A UNIX socketpair is enough: this test is about ordering inside the
    # Connection, not about TCP.  (TCP_NODELAY would raise ENOTSUP here.)
    left, right = socket.socketpair()

    identity = crypto.Identity.generate("concurrency")
    a = Connection(left, identity, stream_id=0, encrypt=False)
    b = Connection(right, identity, stream_id=0, encrypt=False)
    # Install matching session ciphers directly: the handshake is not what this
    # test is about.
    a._session = crypto.SessionCrypto(crypto.Cipher(key, b"i", "aes256gcm"),
                                      crypto.Cipher(key, b"r", "aes256gcm"),
                                      key, "peer", "aes256gcm")
    b._session = crypto.SessionCrypto(crypto.Cipher(key, b"r", "aes256gcm"),
                                      crypto.Cipher(key, b"i", "aes256gcm"),
                                      key, "peer", "aes256gcm")
    return a, b


def test_concurrent_seal_order() -> None:
    print("\n[1] 多条线程在同一条连接上并发发送（nonce 顺序必须与上线顺序一致）")
    key = os.urandom(32)
    sender, receiver = make_pair(key)
    total = 400
    threads = 8
    payloads = {}

    received: list[tuple[int, bytes]] = []
    errors: list[str] = []

    def reader() -> None:
        try:
            for _ in range(total):
                frame = receiver.recv()
                received.append((frame.type, bytes(frame.payload)))
        except (ConnectionClosed, OSError) as exc:
            errors.append(str(exc))

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def writer(start: int) -> None:
        for i in range(start, total, threads):
            body = f"frame-{i:04d}-{'x' * 40}".encode()
            payloads[i] = body
            try:
                sender.send(MSG_PROGRESS, body)
            except ConnectionClosed:
                return

    writers = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(threads)]
    started = time.monotonic()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join(timeout=30)
    reader_thread.join(timeout=30)

    check("全部帧都被解密（没有 authentication failed）", not errors, str(errors[:2]))
    check("收到的帧数正确", len(received) == total, f"{len(received)}/{total}")

    # Each payload carries its own index; the set must match exactly, which is
    # only possible if no two frames were sealed with the same nonce.
    got = {payload for _type, payload in received}
    check("每个载荷都完整且唯一（无 nonce 复用导致的丢失）",
          got == set(payloads.values()), f"缺 {len(set(payloads.values()) - got)} 个")

    sender.abort()
    receiver.abort()
    print(f"      {total} 帧 / {threads} 线程，用时 {time.monotonic() - started:.2f}s")


def test_nonce_never_repeats() -> None:
    print("\n[2] Cipher 本身：并发 seal 不产生重复 nonce")
    key = os.urandom(32)
    cipher = crypto.Cipher(key, b"i", "aes256gcm")
    seen: list[bytes] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(200):
            # Go through the public API.  Sealing is where the nonce is handed
            # out, and if that is not atomic two threads walk away with the
            # same one -- the case this test exists to catch.  The ciphertext
            # is distinct per call, so identical ciphertext would also reveal
            # a reused nonce.
            blob = cipher.seal(b"payload-" + os.urandom(8))
            with lock:
                seen.append(blob[:12])

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    check("密文数量正确", len(seen) == 1600, str(len(seen)))
    check("密文无重复（nonce 未被复用）", len(set(seen)) == len(seen),
          f"{len(seen) - len(set(seen))} 个重复")


def main() -> int:
    use_utf8_console()
    print("EverSend 并发正确性测试")
    print("=" * 66)
    for test in (test_concurrent_seal_order, test_nonce_never_repeats):
        try:
            test()
        except Exception:
            import traceback

            traceback.print_exc()
            _failures.append(test.__name__)
    print("\n" + "=" * 66)
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
