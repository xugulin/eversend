#!/usr/bin/env python3
"""Linux ↔ Windows 真机互传测试（同一台机器上跑，Wine 承载 Windows 端）。

为什么要有这个文件
------------------
三平台的单元测试都只在本机回环上跑：Linux 端和 Linux 端说话，Windows 端和
Windows 端说话。**它们证明不了 Linux 和 Windows 能互相说话。** 而跨系统的差异
恰恰是最容易出事的地方，实测已经抓到两个，都是"本机测试永远是绿的"那种：

1. ``os.pread`` / ``os.pwrite`` 是 POSIX 专有，Windows 上根本没有这两个函数
   ——接收端写第一个分片就会 ``AttributeError``，Windows 版一个字都收不了。
2. Windows 的 ``os.open`` 默认是**文本模式**，会把每个 ``0x0A`` 悄悄改写成
   ``0x0D 0x0A``。落盘的文件比源文件大几千字节，而程序自己的校验和居然发现不了
   ——因为读的时候文本模式又原样翻译了回来。

怎么在没有 Windows 机器的情况下测
--------------------------------
GitHub 的 Windows runner 和 Linux runner 互相连不上（都没有公网入口），所以这里
换一个思路：**在同一台 Linux 机器上，用 Wine 跑真正的 Windows CPython 和
win_amd64 轮子**，让它成为一个真实的 Windows 协议对端。这不是模拟——跑的是
Windows 解释器、Windows 二进制扩展、Windows 的系统调用语义，和 runner 上完全一致。

两个方向都测：Linux 发 → Windows 收，Windows 发 → Linux 收。只测一个方向会漏掉
发送路径和接收路径各自一半的代码。

用法::

    python3 tests/test_interop_wine.py --stage <fetch_runtime 的产物目录>

``--stage`` 指向 ``tools/.cache/stage-windows``（内含 ``runtime/`` 与 ``site/``）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _scratch import scratch  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name} {detail}")
        _failures.append(name)


def wine_path(posix_path: str) -> str:
    """Translate a POSIX path into the ``Z:`` drive path Wine exposes."""
    return "Z:" + str(posix_path).replace("/", "\\")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, timeout: float) -> bool:
    """Wait until something actually accepts on ``port``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


class Peer:
    """One side of the transfer: a native peer or a Wine-hosted Windows peer."""

    def __init__(self, *, wine: bool, stage: Path | None, src: Path, home: Path, name: str) -> None:
        self.wine = wine
        self.stage = stage
        self.src = src
        self.home = home
        self.name = name

    def _command(self, args: list[str]) -> tuple[list[str], dict[str, str]]:
        env = dict(os.environ)
        env["WINEDEBUG"] = "-all"
        # Force UTF-8 so the peer's Chinese output can be decoded here rather
        # than arriving in the console's ANSI code page (GBK on a zh-CN host).
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        if self.wine:
            assert self.stage is not None
            env["PYTHONPATH"] = f"{wine_path(self.src)};{wine_path(self.stage / 'site')}"
            env["EVERSEND_HOME"] = wine_path(self.home)
            python = str(self.stage / "runtime" / "python.exe")
            return ["wine", python, *args], env

        env["PYTHONPATH"] = str(self.src)
        env["EVERSEND_HOME"] = str(self.home)
        return [sys.executable, *args], env

    def serve(self, port: int, receive_dir: Path, log_path: Path | None = None) -> subprocess.Popen:
        cmd, env = self._command(
            [
                "-m", "eversend", "--cli", "serve",
                "--auto-accept", "--name", self.name,
                "--port", str(port),
                "--receive", wine_path(receive_dir) if self.wine else str(receive_dir),
                "--no-broadcast", "--no-mdns",
            ]
        )
        # Write the receiver's output to a file rather than a pipe.  Under
        # Wine a piped stdout is buffered inside the Windows process and is
        # simply lost when the process is killed -- which is precisely when
        # you need it.  A file always has whatever was written.
        if log_path is not None:
            handle = open(log_path, "wb")
            return subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT)
        return subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )

    def send(self, source: Path, port: int, timeout: float = 240.0) -> subprocess.CompletedProcess:
        path = wine_path(source) if self.wine else str(source)
        cmd, env = self._command(
            ["-m", "eversend", "--cli", "send", path,
             "--to", f"127.0.0.1:{port}", "--name", self.name]
        )
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              errors="replace", timeout=timeout)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_direction(
    *,
    label: str,
    receiver: Peer,
    sender: Peer,
    root: Path,
    size_mib: int,
) -> None:
    """One direction: ``sender`` pushes a file to ``receiver``."""
    print(f"\n[{label}]")
    payload_path = root / f"{label}-payload.bin"
    payload = os.urandom(size_mib * 1024 * 1024)
    payload_path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    receive_dir = root / f"{label}-recv"
    receive_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()

    log_path = root / f"{label.replace(' ', '').replace('→', '-')}-receiver.log"
    server = receiver.serve(port, receive_dir, log_path)
    try:
        # Wine needs several seconds to warm up its prefix; wait for the port
        # rather than sleeping a fixed amount, so a slow CI box does not flake.
        ready = wait_for_port(port, timeout=120)
        check(f"{label}: 接收端已就绪", ready)
        if not ready:
            # Say why.  "not ready" alone sent me looking at the wrong end of
            # the transfer twice: the receiver had simply failed to start, and
            # its exit code and output were the only things that showed it.
            print(f"      接收端进程退出码: {server.poll()}")
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            print(f"      接收端输出（{len(text)} 字节）:")
            for line in (text.strip().splitlines() or ["（空）"])[-20:]:
                print(f"      {line}")
            return
        # Give the process a moment past the TCP bind so the handshake handler
        # is actually installed, not merely listening.
        time.sleep(1.5)

        started = time.monotonic()
        result = sender.send(payload_path, port)
        elapsed = max(1e-6, time.monotonic() - started)

        if result.returncode != 0:
            tail = "\n".join(
                line for line in (result.stdout or "").replace("\r", "\n").splitlines()
                if line.strip() and not line.strip().startswith("0 B")
            )[-600:]
            print(f"      发送端返回码 {result.returncode}")
            if tail:
                print(f"      发送端输出: {tail}")
            if result.stderr.strip():
                print(f"      发送端 stderr: {result.stderr[-800:]}")
        check(f"{label}: 发送端返回 0", result.returncode == 0)

        landed = receive_dir / payload_path.name
        check(f"{label}: 文件已落地", landed.exists())
        if landed.exists():
            actual = sha256_of(landed)
            check(f"{label}: 大小一致", landed.stat().st_size == len(payload),
                  f"{landed.stat().st_size} != {len(payload)}")
            check(f"{label}: SHA-256 逐字节一致", actual == expected,
                  f"\n      期望 {expected}\n      实得 {actual}")
            print(f"      {size_mib} MiB 用时 {elapsed:.1f}s = {size_mib / elapsed:.0f} MiB/s")
    finally:
        try:
            server.send_signal(signal.SIGINT)
        except Exception:
            pass
        time.sleep(2)
        server.kill()
        try:
            server.wait(timeout=10)
        except Exception:
            pass
        # Always show what the receiver said.  When the Wine side rejects an
        # offer, its own output is the only place the reason appears -- the
        # sender just reports "returned 1".
        try:
            output = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        print(f"      ── 接收端输出（{log_path.name}）──")
        for line in (output.strip().splitlines() or ["（空）"])[-14:]:
            print(f"      {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux <-> Windows 真机互传测试")
    parser.add_argument("--stage", required=True, help="stage-windows 目录（含 runtime/ 与 site/）")
    parser.add_argument("--size", type=int, default=24, help="每个方向传多少 MiB")
    args = parser.parse_args()

    stage = Path(args.stage).resolve()
    src = Path(__file__).resolve().parent.parent / "src"

    print("Linux ↔ Windows 真机互传测试")
    print("=" * 66)

    if shutil.which("wine") is None:
        print("  找不到 wine。这个测试需要 Wine 来承载 Windows 端。")
        print("  Ubuntu: sudo apt-get install -y wine64   （或 wine）")
        return 2
    if not (stage / "runtime" / "python.exe").is_file():
        print(f"  在 {stage} 下找不到 runtime/python.exe")
        print("  先跑： python3 tools/fetch_runtime.py --platform windows --out <stage>")
        return 2

    with scratch("interop-") as root:
        linux = Peer(wine=False, stage=None, src=src, home=root / "home-linux", name="Linux-Peer")
        windows = Peer(wine=True, stage=stage, src=src, home=root / "home-windows", name="Windows-Peer")
        for peer in (linux, windows):
            peer.home.mkdir(parents=True, exist_ok=True)

        run_direction(label="Linux → Windows", receiver=windows, sender=linux,
                      root=root, size_mib=args.size)
        run_direction(label="Windows → Linux", receiver=linux, sender=windows,
                      root=root, size_mib=args.size)

    print("\n" + "=" * 66)
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("两个方向全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
