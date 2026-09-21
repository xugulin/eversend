#!/usr/bin/env python3
"""先确认 Wine 里的 Windows Python 和加密库真的能用，再跑互传。

单独成一步的理由：互传失败时，你想立刻知道是"Wine 环境没搭起来"还是"协议实现
有问题"。混在一个步骤里，两种失败长得一模一样，都得翻完整日志才知道。

检查三件事：Windows 解释器起得来、报告的确实是 Windows/AMD64、win_amd64 的
cryptography 能加载（它的 OpenSSL 是打包在轮子里的 DLL，最容易出问题的一环）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def wine_path(posix_path: str) -> str:
    return "Z:" + str(posix_path).replace("/", "\\")


def run_wine_python(python: str, site: Path, code: str, timeout: int = 240) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WINEDEBUG"] = "-all"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = wine_path(site)
    return subprocess.run(
        ["wine", python, "-c", code],
        env=env, capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    args = parser.parse_args()

    stage = Path(args.stage).resolve()
    python = str(stage / "runtime" / "python.exe")
    site = stage / "site"

    if shutil.which("wine") is None:
        print("  ✗ 找不到 wine（Ubuntu: sudo apt-get install -y wine64）")
        return 1
    if not Path(python).is_file():
        print(f"  ✗ 找不到 {python}")
        return 1

    print("Wine 环境检查")
    print("=" * 60)

    probe = (
        "import sys, platform;"
        "print('python', sys.version.split()[0]);"
        "print('platform', platform.system(), platform.machine());"
        "print('executable', sys.executable)"
    )
    result = run_wine_python(python, site, probe)
    print(result.stdout.strip() or result.stderr.strip()[-800:])
    if result.returncode != 0 or "Windows" not in result.stdout:
        print("  ✗ Windows 解释器没能正常工作")
        return 1
    print("  ✓ Windows 解释器可用")

    crypto = (
        "import cryptography;"
        "from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey;"
        "from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305, AESGCM;"
        "X25519PrivateKey.generate();"
        "AESGCM(bytes(32)).encrypt(bytes(12), b'probe', b'');"
        "print('cryptography', cryptography.__version__, 'ok')"
    )
    result = run_wine_python(python, site, crypto)
    print(result.stdout.strip() or result.stderr.strip()[-800:])
    if result.returncode != 0 or "ok" not in result.stdout:
        print("  ✗ Windows 版 cryptography 不可用（协议就握不上手）")
        return 1
    print("  ✓ Windows 版加密库可用（X25519 + AES-GCM 都跑通了）")
    print("=" * 60)
    print("Wine 环境就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
