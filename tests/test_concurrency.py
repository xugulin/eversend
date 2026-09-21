#!/usr/bin/env python3
"""并发与短写正确性测试：三个只有真跑才暴露的缺陷。

为什么单独有这个文件
--------------------
前两个是 CI 上抓到的间歇性失败，本机复现不了，三轮都卡在"只有一句超时报错、
两边都没有更多线索"上。它们都属于"在完美的本机环境里永远是绿的"那一类。

**一、AEAD nonce 竞态。** ``Cipher`` 的 nonce 是个普通整数计数器，而 ``seal()``
原本在发送锁**外面**调用::

    线程 A: seal() 拿到 nonce 7 ──────────────┐
    线程 B: seal() 拿到 nonce 8 ─────┐        │
    线程 B: 抢到锁先写出 nonce 8     │        │
    线程 A: 再抢到锁写出 nonce 7     ┘        │
                                     ↑ 对端按自己的计数器解，标签全错

后果比"校验失败"严重：两条不同的帧用了同一个 nonce，那是 AEAD 唯一一种会破坏
**机密性**而不只是完整性的错误。

**二、``sendmsg`` 短写。** ``socket.sendmsg`` 的语义和 ``send`` 一样——返回实际写
出去多少字节，**不会**像 ``sendall`` 那样自己循环。而套接字一旦设了超时（我们给
数据流设了 120 s），CPython 就用非阻塞 + select 实现它，此时 ``sendmsg`` 只写内核
缓冲装得下的部分就返回。实测同一个 512 KiB 的帧::

    纯阻塞套接字        → 一次写完 524288 字节
    设了 settimeout 之后 → 只写出 32741 字节，其余 491547 字节无声消失

对端于是永远等下去，而没有任何一层会报错。本机之所以测不出来，是因为发送缓冲
开到 8 MiB，整块通常一次装得下。

**三、nonce 不能复用**（第一条的直接推论，单独把 Cipher 本身再测一遍）。
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
from eversend.core.connection import ChunkFrame, Connection  # noqa: E402
from eversend.core.constants import MSG_PROGRESS  # noqa: E402
from eversend.core.framing import ConnectionClosed  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    if not condition:
        _failures.append(name)


def make_pair(key: bytes):
    """Two Connections over a real TCP pair, session already established."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    client = socket.socket()
    client.connect(("127.0.0.1", listener.getsockname()[1]))
    server, _ = listener.accept()
    listener.close()

    identity = crypto.Identity.generate("concurrency")
    a = Connection(client, identity, stream_id=0, encrypt=False)
    b = Connection(server, identity, stream_id=0, encrypt=False)
    # Install matching session ciphers directly: the handshake is not what
    # these tests are about.
    a._session = crypto.SessionCrypto(
        crypto.Cipher(key, b"i", "aes256gcm"), crypto.Cipher(key, b"r", "aes256gcm"),
        key, "peer", "aes256gcm",
    )
    b._session = crypto.SessionCrypto(
        crypto.Cipher(key, b"r", "aes256gcm"), crypto.Cipher(key, b"i", "aes256gcm"),
        key, "peer", "aes256gcm",
    )
    return a, b


def test_concurrent_seal_order() -> None:
    print("\n[1] 多线程在同一条连接上并发发送（nonce 顺序必须与上线顺序一致）")
    key = os.urandom(32)
    sender, receiver = make_pair(key)
    total = 400
    threads = 8
    payloads: dict[int, bytes] = {}
    received: list[bytes] = []
    errors: list[str] = []

    def reader() -> None:
        try:
            for _ in range(total):
                frame = receiver.recv()
                received.append(bytes(frame.payload))
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
    check(
        "每个载荷都完整且唯一（无 nonce 复用导致的丢失）",
        set(received) == set(payloads.values()),
        f"缺 {len(set(payloads.values()) - set(received))} 个",
    )
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
            # Go through the public API: sealing is where the nonce is handed
            # out, and if that is not atomic two threads walk away with the
            # same one.  Distinct plaintext makes identical ciphertext a
            # reliable signal of reuse.
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


def test_gather_write_is_complete() -> None:
    """小块发送缓冲 + socket 超时下，拼装写必须把整帧写完。

    Two conditions make ``sendmsg`` short-write, and both are how the engine
    really uses it:

    * the socket has a timeout (data streams set 120 s), which CPython
      implements as non-blocking + select, so ``sendmsg`` returns once the
      kernel buffer is full;
    * the send buffer is small (a slow reader, or a low ``net.core.wmem_max``).

    A real TCP pair is required.  ``socketpair()`` on Linux is AF_UNIX, which
    does not take this path -- an earlier version of this test used one and
    passed even with the fix reverted.
    """
    print("\n[3] 小块发送缓冲 + socket 超时下，整帧必须写完")
    chunk = os.urandom(512 * 1024)
    received: list[bytes] = []
    errors: list[str] = []

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    client = socket.socket()
    client.connect(("127.0.0.1", listener.getsockname()[1]))
    server, _ = listener.accept()
    listener.close()

    identity = crypto.Identity.generate("gather")
    sender = Connection(client, identity, stream_id=1, encrypt=False)
    receiver = Connection(server, identity, stream_id=1, encrypt=False)

    # Shrink the buffers *after* the Connections exist.  Connection.__init__
    # calls tune_socket(), which asks for 8 MiB -- setting the small value
    # first would simply be overwritten and this test would pass no matter what.
    #
    # A small buffer is the realistic case, not a contrived one: a stock GitHub
    # Ubuntu runner has net.core.wmem_max = 212992 (208 KB), so a 1 MiB chunk
    # never fits and every chunk short-writes there.  On a developer machine
    # wmem_max is often 4 MiB, the whole chunk fits, and the bug is invisible.
    client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    client.settimeout(120.0)   # also what turns sendmsg into a partial write
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sender._session = crypto.SessionCrypto(
        crypto.NullCipher(), crypto.NullCipher(), b"", "peer", "none"
    )
    receiver._session = crypto.SessionCrypto(
        crypto.NullCipher(), crypto.NullCipher(), b"", "peer", "none"
    )

    def reader() -> None:
        try:
            frame = receiver.recv_data()
            if isinstance(frame, ChunkFrame):
                received.append(bytes(frame.data))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    # Let the reader drain once so the buffer starts empty; otherwise the first
    # sendmsg blocks instead of short-writing.
    time.sleep(0.3)
    try:
        sender.send_chunk(0, 0, 0, chunk, 0)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"send: {type(exc).__name__}: {exc}")
    thread.join(timeout=20)

    check("没有异常", not errors, str(errors[:2]))
    check(
        "整块都收到了（没有被静默截断）",
        len(received) == 1 and received[0] == chunk,
        f"收到 {len(received[0]) if received else 0} / 期望 {len(chunk)} 字节",
    )
    sender.abort()
    receiver.abort()


def main() -> int:
    use_utf8_console()
    print("EverSend 并发与短写正确性测试")
    print("=" * 66)

    if not crypto.CRYPTO_AVAILABLE:
        # 前两项测的是 AEAD 路径，没有 cryptography 就无从测起。以前这会以
        # "NameError: name 'AESGCM' is not defined" 收场，看起来像代码坏了，
        # 其实只是这台机器没装那个可选的加密轮子。CI 一定会装上（工作流里
        # 有一步专门钉住它必须可用），所以这里的跳过不会让加密路径失去覆盖。
        print("跳过：[1][2] 需要 cryptography，这台机器没装，加密路径无法测试。")
        print("      装上再跑：pip install cryptography")
        print("      仍然会测：[3] 多线程整帧写入（不依赖加密）。\n")
        tests = (test_gather_write_is_complete,)
    else:
        tests = (
            test_concurrent_seal_order,
            test_nonce_never_repeats,
            test_gather_write_is_complete,
        )

    for test in tests:
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
    if not crypto.CRYPTO_AVAILABLE:
        print("（部分测试被跳过：本机没有 cryptography。）")
        return 0
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
