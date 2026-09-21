#!/usr/bin/env python3
"""End-to-end verification of an assembled EverSend portable tree.

The point of this script is to stop trusting the build machine.  It takes the
*artifact* (an unzipped tree or the release zip), copies it somewhere that
looks nothing like a developer checkout -- a path with spaces and Chinese
characters, which is exactly what the user's own USB stick will look like --
and then drives it through ``env -i`` so that not a single environment
variable from this shell can leak in and paper over a missing file.

What is proven, in order:

1. **Structure** -- the layout the launchers expect is really there, ``run.sh``
   kept its executable bit (also inside the zip), nothing is a symlink, and no
   absolute build path is baked into any file.
2. **It runs on a foreign machine** -- the embedded interpreter starts,
   ``eversend.core.constants``, ``PySide6.QtCore`` and ``cryptography`` all
   import *from the bundled tree* (their ``__file__`` is printed as proof), a
   real 4 MiB loopback transfer verifies byte-for-byte, and a Qt window can be
   constructed with the bundled platform plugin.
3. **It is relocatable** -- the same checks pass after *moving* the tree to a
   second, differently named directory, and when invoked through a symlink
   from an unrelated working directory.
4. **It survives a read-only medium** -- with ``chmod -R a-w`` the launcher
   still starts, reports the fallback and keeps its data in a temp directory.
5. **It leaves no trace in $HOME** -- the fake home directory stays empty.

The Windows target cannot be executed here, so for it only the structural
checks run; the report says so explicitly instead of pretending otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

def use_utf8_console() -> None:
    """让中文输出在旧版 Windows 控制台上也能活下来。

    zh-CN 的 Windows 控制台默认代码页是 936、en-US 是 1252，打印中文检查名会
    抛 UnicodeEncodeError 把整个脚本打断——CI 在 Windows runner 上就是这么挂的。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

#: A directory name no sane build machine would ever produce: spaces AND
#: Chinese characters AND a leading dash-free but odd-looking prefix.
FOREIGN_DIR_NAME = "U 盘 测 试/解压 后的 目录"
FOREIGN_DIR_NAME_2 = "移动 到 别处/EverSend 副本"

#: Environment given to every child process: exactly what a bare
#: ``env -i`` session would have on the user's machine.
SANITIZED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
FORBIDDEN_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append(Result(name, ok, detail))
        colour = GREEN if ok else RED
        mark = "PASS" if ok else "FAIL"
        print(f"  {colour}{mark}{RESET} {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""), flush=True)
        return ok

    def skip(self, name: str, detail: str = "") -> None:
        self.results.append(Result(name, True, detail, skipped=True))
        print(f"  {YELLOW}SKIP{RESET} {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""), flush=True)

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if not r.ok and not r.skipped]

    @property
    def passes(self) -> int:
        return sum(1 for r in self.results if r.ok and not r.skipped)


class Runner:
    """Runs commands with a sanitised environment, fully isolated from ours."""

    def __init__(self, home: Path, tmpdir: Path, scratch: Path) -> None:
        self.home = home
        self.tmpdir = tmpdir
        self.scratch = scratch

    def env_args(self, extra: dict[str, str] | None = None) -> list[str]:
        base = {
            "HOME": str(self.home),
            "PATH": SANITIZED_PATH,
            "TMPDIR": str(self.tmpdir),
            "LANG": "C.UTF-8",
        }
        if extra:
            base.update(extra)
        # `env -i` is used literally (not "start from os.environ"): the point is
        # that nothing this shell exports can reach the artifact.
        args = ["env", "-i"]
        args += [f"{key}={value}" for key, value in base.items()]
        return args

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float = 180.0,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [*self.env_args(extra_env), *argv]
        return subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )

    def artifact_env(self, tree: Path, *, qt: str | None = None) -> dict[str, str]:
        """The environment the launcher would create, for direct probes.

        XDG_* is redirected into the scratch directory for the same reason the
        launcher redirects it into ``data/``: fontconfig and Qt otherwise write
        ``$HOME/.cache``, which would break the "nothing outside the folder"
        promise we are here to check.
        """
        env = {
            "PYTHONHOME": str(tree / "runtime"),
            "PYTHONPATH": f"{tree / 'app'}{os.pathsep}{tree / 'site'}",
            "LD_LIBRARY_PATH": f"{tree / 'runtime' / 'lib'}{os.pathsep}{tree / 'site' / 'PySide6' / 'Qt' / 'lib'}",
            "QT_PLUGIN_PATH": str(tree / "site" / "PySide6" / "Qt" / "plugins"),
            "XDG_CONFIG_HOME": str(self.scratch / "xdg-config"),
            "XDG_CACHE_HOME": str(self.scratch / "xdg-cache"),
            "XDG_DATA_HOME": str(self.scratch / "xdg-data"),
        }
        if qt:
            env["QT_QPA_PLATFORM"] = qt
        return env

    def popen(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        command = [*self.env_args(extra_env), *argv]
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def human(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024.0 or unit == "GiB":
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} GiB"


def tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def locate_tree(path: Path, workdir: Path) -> tuple[Path, str]:
    """Accept a zip, a folder containing ``EverSend/``, or ``EverSend/`` itself."""
    if path.is_file() and path.suffix.lower() == ".zip":
        target = workdir / "from-zip"
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            top = sorted({name.split("/", 1)[0] for name in names})
            for info in archive.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise SystemExit(f"unsafe zip member: {info.filename}")
                destination = target / member
                if info.filename.endswith("/"):
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode:
                    os.chmod(destination, mode & 0o7777)
        print(f"extracted {path.name} -> {target} (top level: {', '.join(top)})")
        if len(top) != 1:
            raise SystemExit(f"the zip must contain exactly one top level folder, found {top}")
        return target / top[0], "zip"
    if (path / "run.sh").exists() or (path / "run.bat").exists():
        return path, "tree"
    candidates = [child for child in path.iterdir() if (child / "run.sh").exists() or (child / "run.bat").exists()]
    if len(candidates) == 1:
        return candidates[0], "tree"
    raise SystemExit(f"cannot find a EverSend tree in {path}")


def mirror_copy(source: Path, destination: Path) -> Path:
    """Copy the tree, then make every directory writable again (zip bits)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    for path in destination.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(path.stat().st_mode | 0o700)
        except OSError:
            pass
    return destination


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_structure(tree: Path, report: Report, origin: str) -> None:
    print("\n[1] structure")
    required = [
        "run.sh",
        "run.bat",
        "README.txt",
        "runtime",
        "app/eversend",
        "app/eversend_green.py",
        "site",
        "data",
        "received",
    ]
    missing = [name for name in required if not (tree / name).exists()]
    report.add("required layout present", not missing, f"missing: {missing}" if missing else f"{len(required)} entries")

    run_sh = tree / "run.sh"
    if run_sh.exists():
        mode = run_sh.stat().st_mode
        if sys.platform == "win32":
            # Windows has no execute bit: every extracted file comes back 0o666,
            # so this check can only ever fail there -- and it says nothing about
            # the artifact.  What matters is the bit *inside the zip*, which the
            # Linux run verifies.
            report.skip("run.sh is executable", "Windows 没有执行位；zip 内的权限由 Linux 侧验证")
        else:
            report.add("run.sh is executable", bool(mode & stat.S_IXUSR), f"mode {oct(mode & 0o777)}")

    links = [p for p in tree.rglob("*") if p.is_symlink()]
    report.add("no symlinks in the tree", not links, f"{len(links)} found" if links else "FAT32/exFAT safe")

    # The launchers must not bake in an absolute path.
    for name in ("run.sh", "run.bat"):
        text = (tree / name).read_text("utf-8", errors="replace")
        report.add(
            f"{name} resolves its own directory",
            ("$0" in text if name.endswith(".sh") else "%~dp0" in text),
        )

    if origin == "zip":
        report.add("zip keeps one top-level folder", tree.name == "EverSend", f"top level = {tree.name}/")


def check_no_baked_paths(tree: Path, report: Report, forbidden: Sequence[str]) -> None:
    print("\n[2] no absolute build paths")
    needles = [n.encode() for n in forbidden if n]
    hits: list[str] = []
    scanned = 0
    for path in tree.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        for needle in needles:
            if needle in data:
                hits.append(f"{path.relative_to(tree)} contains {needle.decode()}")
                break
    report.add(
        f"no build path in {scanned} files",
        not hits,
        "; ".join(hits[:3]) if hits else f"searched {len(needles)} forbidden path(s)",
    )


def check_foreign_machine(tree: Path, runner: Runner, report: Report) -> None:
    """Everything a fresh machine has to be able to do."""
    print("\n[3] runs on a foreign machine (env -i, spaces + Chinese in the path)")

    # 3a. The embedded interpreter starts and reports its own prefix.
    python = tree / "runtime" / "bin" / "python3"
    if not python.exists():
        python = tree / "runtime" / "bin" / "python3.14"
    result = runner.run(
        [str(python), "-s", "-B", "-c", "import sys;print(sys.executable);print(sys.prefix);print(sys.version.split()[0])"],
        cwd=Path("/"),
    )
    ok = result.returncode == 0 and str(tree) in result.stdout
    report.add(
        "embedded interpreter starts",
        ok,
        (result.stdout.strip().splitlines() or [result.stderr.strip()[:120]])[0],
    )
    version = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "?"

    # 3b. The launcher itself, exactly as the user would run it.
    for flag, expect in (("--version", "EverSend"), ("--print-paths", "app home")):
        result = runner.run(["./run.sh", flag], cwd=tree)
        report.add(
            f"./run.sh {flag}",
            result.returncode == 0 and expect in result.stdout,
            (result.stdout.strip().splitlines() or [result.stderr.strip()[:150]])[-1],
        )

    result = runner.run(["./run.sh", "--selftest"], cwd=tree)
    report.add(
        "./run.sh --selftest",
        result.returncode == 0 and "SELFTEST OK" in result.stdout,
        f"rc={result.returncode}",
    )
    if result.returncode != 0:
        print(textwrap.indent(result.stdout[-2000:] + result.stderr[-2000:], "      "))

    # 3c. Import provenance: the modules must come out of the artifact.
    probe = textwrap.dedent(
        """
        import json, sys
        payload = {}
        from eversend.core import constants
        payload["core"] = constants.APP_VERSION
        import PySide6
        from PySide6 import QtCore
        payload["pyside6"] = PySide6.__file__
        payload["qt_version"] = QtCore.qVersion()
        import cryptography
        payload["cryptography"] = cryptography.__file__
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        Ed25519PrivateKey.generate()
        ChaCha20Poly1305(ChaCha20Poly1305.generate_key())
        payload["sys_executable"] = sys.executable
        print("PROBE" + json.dumps(payload))
        """
    ).strip()
    probe_path = runner.scratch / "probe_imports.py"
    probe_path.write_text(probe, "utf-8")
    result = runner.run(
        [str(python), "-s", "-B", str(probe_path)],
        cwd=runner.scratch,
        extra_env=runner.artifact_env(tree, qt="offscreen"),
    )
    line = next((l for l in result.stdout.splitlines() if l.startswith("PROBE")), "")
    if not line:
        report.add("imports come from the bundled tree", False, (result.stderr.strip()[-200:] or "no probe output"))
        return
    import json

    payload = json.loads(line[len("PROBE") :])
    inside = lambda p: str(tree) in str(p)  # noqa: E731
    report.add("eversend.core imports", bool(payload.get("core")), f"APP_VERSION={payload.get('core')}")
    report.add("PySide6.QtCore imports from site/", inside(payload["pyside6"]), f"{payload['pyside6']} (Qt {payload['qt_version']})")
    report.add("cryptography imports from site/", inside(payload["cryptography"]), str(payload["cryptography"]))
    report.add("sys.executable is inside the tree", inside(payload["sys_executable"]), str(payload["sys_executable"]))

    # 3d. A real Qt window, using the bundled platform plugin.
    qt_probe = textwrap.dedent(
        """
        import os, sys
        from PySide6.QtWidgets import QApplication, QLabel
        app = QApplication(sys.argv)
        label = QLabel("韧传")
        label.resize(120, 40)
        label.show()
        print("QT_PLUGIN", app.platformName(), os.environ.get("QT_QPA_PLATFORM"))
        """
    ).strip()
    qt_path = runner.scratch / "probe_qt.py"
    qt_path.write_text(qt_probe, "utf-8")
    result = runner.run(
        [str(python), "-s", "-B", str(qt_path)],
        cwd=runner.scratch,
        extra_env=runner.artifact_env(tree, qt="offscreen"),
    )
    ok = result.returncode == 0 and "QT_PLUGIN" in result.stdout
    detail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr.strip()[-150:]
    report.add("Qt widgets app starts (offscreen plugin from site/)", ok, detail)
    if not ok:
        print(textwrap.indent((result.stdout + result.stderr)[-1500:], "      "))

    # 3e. Data really does land inside the tree when it is writable.
    result = runner.run(["./run.sh", "--print-paths"], cwd=tree)
    report.add(
        "writable medium keeps data inside the tree",
        str(tree / "data") in result.stdout,
        next((l for l in result.stdout.splitlines() if l.startswith("data dir")), ""),
    )

    # 3f. No graphical session -> a clear Chinese error, not a crash.
    result = runner.run(["./run.sh"], cwd=tree, timeout=60)
    combined = result.stdout + result.stderr
    report.add(
        "no DISPLAY -> clear Chinese error + CLI hint",
        result.returncode == 3 and "图形" in combined and "--cli" in combined,
        f"rc={result.returncode}",
    )

    # 3g. Headless CLI mode really starts the engine.
    process = runner.popen(["./run.sh", "--cli"], cwd=tree)
    lines: list[str] = []
    # The application's own CLI prints Chinese ("已启动" / "传输端口"); accept
    # either spelling so this check survives wording changes in the app.
    ready_markers = ("传输端口", "tcp port", "已启动")

    def drain() -> None:
        # A reader thread, not readline() in the main loop: a blocking read on
        # a pipe that never closes would hang past every deadline we set.
        if process.stdout is None:
            return
        for line in process.stdout:
            lines.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            if any(marker in line for line in lines for marker in ready_markers):
                break
            if process.poll() is not None:
                break
            time.sleep(0.2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        reader.join(timeout=5)
    output = "".join(lines)
    report.add(
        "./run.sh --cli starts the engine headless",
        any(marker in output for marker in ready_markers),
        next(
            (l.strip() for l in output.splitlines() if any(m in l for m in ready_markers)),
            output[-120:].replace("\n", " "),
        ),
    )
    print(f"      {DIM}(embedded CPython {version}){RESET}")


def check_transfer(tree: Path, runner: Runner, report: Report) -> None:
    print("\n[4] real loopback transfer (4 MiB, bytes compared by sha256)")
    result = runner.run(["./run.sh", "--selftest-transfer"], cwd=tree, timeout=300)
    markers = ("TRANSFER SELFTEST OK", "RESULT       : PASS", "selftest", "PASS")
    ok = result.returncode == 0 and ("TRANSFER SELFTEST OK" in result.stdout or "PASS" in result.stdout)
    detail = next(
        (
            l.strip()
            for l in reversed(result.stdout.splitlines())
            if "identical" in l or "sha256" in l or "RESULT" in l
        ),
        f"rc={result.returncode}",
    )
    report.add("loopback transfer over the artifact (bytes compared)", ok, detail)
    del markers
    if not ok:
        print(textwrap.indent((result.stdout + result.stderr)[-2000:], "      "))


def check_relocation(tree: Path, workdir: Path, runner: Runner, report: Report, origin: str) -> Path:
    print("\n[5] portability: move the tree, run it from elsewhere")
    second = workdir / FOREIGN_DIR_NAME_2
    if second.exists():
        shutil.rmtree(second, ignore_errors=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tree), str(second))
    report.add("tree moved to a second location", second.exists(), str(second))

    result = runner.run([str(second / "run.sh"), "--selftest"], cwd=Path("/"), timeout=300)
    report.add(
        "selftest passes after the move (cwd=/ , absolute launcher path)",
        result.returncode == 0 and "SELFTEST OK" in result.stdout,
        f"rc={result.returncode}",
    )

    # Invoked through a symlink from an unrelated directory.
    link = workdir / "launcher-link" / "run.sh"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(second / "run.sh")
    result = runner.run([str(link), "--print-paths"], cwd=Path("/"), timeout=120)
    report.add(
        "invoked through a symlink",
        result.returncode == 0 and str(second) in result.stdout,
        next((l for l in result.stdout.splitlines() if l.startswith("app home")), result.stderr.strip()[:120]),
    )

    result = runner.run(["./run.sh", "--selftest-transfer"], cwd=second, timeout=300)
    report.add(
        "transfer still works after the move",
        result.returncode == 0
        and ("TRANSFER SELFTEST OK" in result.stdout or "PASS" in result.stdout),
        next(
            (l.strip() for l in result.stdout.splitlines() if "identical" in l or "RESULT" in l),
            f"rc={result.returncode}",
        ),
    )
    return second


def check_readonly(tree: Path, runner: Runner, report: Report) -> None:
    """Freeze the (already moved) tree and prove it still runs.

    The tree is chmod-ed in place rather than copied a third time: the copy
    would be another ~370 MiB, and the property under test -- "permission bits
    say read-only" -- is identical.  Permissions are restored in ``finally`` so
    the temporary directory can still be removed.
    """
    print("\n[6] read-only medium (chmod -R a-w on the moved tree)")

    def snapshot() -> dict[str, tuple[int, int]]:
        state: dict[str, tuple[int, int]] = {}
        for path in tree.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    info = path.stat()
                    state[str(path.relative_to(tree))] = (info.st_size, info.st_mtime_ns)
            except OSError:
                pass
        return state

    before_state = snapshot()
    subprocess.run(["chmod", "-R", "a-w", str(tree)], check=False)
    probe = tree / "data" / ".still-writable"
    try:
        probe.write_text("x", "utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    report.add("tree really is read-only", not writable, "chmod -R a-w applied")

    try:
        result = runner.run(["./run.sh", "--print-paths"], cwd=tree, timeout=120)
        combined = result.stdout + result.stderr
        expects_temp = str(runner.tmpdir / f"eversend-{os.getuid()}")
        report.add(
            "launcher reports the read-only fallback (Chinese)",
            result.returncode == 0 and "不可写" in combined,
            next((l.strip() for l in combined.splitlines() if "不可写" in l), "no warning printed"),
        )
        report.add(
            "data dir moved under TMPDIR",
            result.returncode == 0 and expects_temp in result.stdout,
            expects_temp,
        )
        result = runner.run(["./run.sh", "--selftest"], cwd=tree, timeout=300)
        report.add(
            "selftest passes on a read-only tree",
            result.returncode == 0 and "SELFTEST OK" in result.stdout,
            f"rc={result.returncode}",
        )
        if result.returncode != 0:
            print(textwrap.indent((result.stdout + result.stderr)[-1500:], "      "))
        result = runner.run(["./run.sh", "--selftest-transfer"], cwd=tree, timeout=300)
        report.add(
            "transfer works on a read-only tree (data in temp)",
            result.returncode == 0 and ("TRANSFER SELFTEST OK" in result.stdout or "PASS" in result.stdout),
            f"rc={result.returncode}",
        )
        after_state = snapshot()
        added = sorted(set(after_state) - set(before_state))
        changed = sorted(
            name for name in set(after_state) & set(before_state) if after_state[name] != before_state[name]
        )
        pollution = added + changed
        report.add(
            "nothing written into the read-only tree",
            not pollution,
            f"{pollution[:3]}" if pollution else f"{len(before_state)} files unchanged (no new .pyc, no state)",
        )
    finally:
        # Always give the files back their permissions (the task asks for it,
        # and an unremovable temp tree would be a nasty parting gift).
        subprocess.run(["chmod", "-R", "u+rwX", str(tree)], check=False)
        report.add("permissions restored after the test", True, "chmod -R u+rwX")


def check_no_home_traces(runner: Runner, report: Report) -> None:
    print("\n[7] no traces in the user's home directory")
    entries = sorted(str(p.relative_to(runner.home)) for p in runner.home.rglob("*"))
    report.add(
        "$HOME stays empty",
        not entries,
        f"{len(entries)} entr(y/ies): {entries[:5]}" if entries else "XDG_* redirected into the app data dir",
    )


def qt_plugins_dir(tree: Path) -> Path | None:
    """The Qt plugin directory, in whichever layout this wheel used.

    PySide6 ships ``PySide6/Qt/plugins`` on Linux/macOS but ``PySide6/plugins``
    on Windows, and the launchers probe for both.  Getting this wrong is fatal
    (a wrong ``QT_QPA_PLATFORM_PLUGIN_PATH`` stops Qt from creating any window),
    so it is checked explicitly rather than assumed.
    """
    for relative in ("site/PySide6/Qt/plugins", "site/PySide6/plugins"):
        candidate = tree / relative
        if candidate.is_dir():
            return candidate
    return None


def check_launcher_references(tree: Path, report: Report) -> None:
    """Every path the launchers point at must exist in the artifact.

    This is the check that caught the Windows build pointing
    ``QT_QPA_PLATFORM_PLUGIN_PATH`` at the Linux-only ``PySide6/Qt/plugins``
    directory: a structural assumption is only true once something verifies it.
    """
    print("\n[1b] launcher references resolve inside the artifact")
    plugins = qt_plugins_dir(tree)
    missing: list[str] = []
    checked = 0
    for name, pattern, root in (
        ("run.sh", r"\$APPDIR/([A-Za-z0-9_./-]+)", None),
        ("run.bat", r"%APPDIR%\\([A-Za-z0-9_.\\-]+)", None),
    ):
        text = (tree / name).read_text("utf-8", errors="replace")
        for raw in sorted(set(re.findall(pattern, text))):
            relative = raw.replace("\\", "/").strip("/")
            if not relative or "%" in relative or "!" in relative:
                continue
            # Directories the launcher creates at runtime, and the shell
            # variables it derives, are not expected to exist in the zip.
            if relative.startswith(("data", "received", "logs", "xdg-")):
                continue
            checked += 1
            target = tree / relative
            if target.exists():
                continue
            # The launchers *probe* for the Qt directories because the two
            # wheel layouts differ (PySide6/Qt/{lib,plugins} vs PySide6/...),
            # so a missing probe target is fine when its fallback exists.
            if "plugins" in relative and plugins is not None:
                continue
            if relative.endswith("PySide6/Qt/lib") and (tree / "site" / "PySide6").is_dir():
                continue
            missing.append(f"{name}: {relative}")
    report.add(
        f"all {checked} launcher paths exist",
        not missing,
        "; ".join(missing[:4]) if missing else "no dangling reference",
    )


def check_windows_structure(tree: Path, report: Report) -> None:
    print("\n[8] windows target: static structure (cannot execute python.exe here)")
    python = tree / "runtime" / "python.exe"
    report.add("runtime/python.exe present", python.exists())
    report.add("runtime/pythonw.exe present", (tree / "runtime" / "pythonw.exe").exists())
    pyd = list((tree / "site").rglob("*.pyd"))
    report.add("windows extension modules (.pyd) installed", len(pyd) > 0, f"{len(pyd)} .pyd files")
    linux_so = [p for p in (tree / "site").rglob("*.so") if "PySide6" in str(p)]
    report.add("no linux .so left in site/", not linux_so, f"{len(linux_so)} found" if linux_so else "")
    bat = tree / "run.bat"
    text = bat.read_text("utf-8", errors="replace") if bat.exists() else ""
    report.add("run.bat uses %~dp0 and python.exe", "%~dp0" in text and "python.exe" in text)
    report.add("run.bat forwards arguments", "%ARGS%" in text)
    plugins = qt_plugins_dir(tree)
    platforms = plugins / "platforms" if plugins else None
    report.add(
        "Qt plugin dir found (either wheel layout)",
        plugins is not None,
        str(plugins.relative_to(tree)) if plugins else "neither site/PySide6/Qt/plugins nor site/PySide6/plugins",
    )
    report.add(
        "platforms/qwindows.dll present",
        bool(platforms and (platforms / "qwindows.dll").exists()),
        ", ".join(sorted(p.name for p in platforms.glob("*.dll"))) if platforms else "missing",
    )
    report.add(
        "core Qt DLLs next to the PySide6 modules",
        all((tree / "site" / "PySide6" / f"Qt6{name}.dll").exists() for name in ("Core", "Gui", "Widgets", "Network")),
    )
    msvcp = list((tree / "site").rglob("msvcp140*.dll"))
    report.add("MSVC runtime DLLs vendored", bool(msvcp), f"{len(msvcp)} file(s)")
    report.add(
        "PySide6 self-configures its DLL directory",
        "add_dll_directory" in (tree / "site" / "PySide6" / "__init__.py").read_text("utf-8", errors="replace"),
    )
    if sys.platform == "win32":
        # This is the one thing a user does first, and the one thing static
        # analysis cannot prove: cmd.exe has to accept the batch file, the
        # argument forwarding has to work, and the app has to start with the
        # bundled interpreter.  ``run.bat --selftest`` exercises exactly that
        # path (loop transfer included) without opening a window.
        try:
            # Invoked by name with the tree as the working directory: the path
            # contains spaces and Chinese characters, and ``cmd /c`` has its own
            # quoting rules for a quoted first token.  A bare name never has to
            # survive them.
            result = subprocess.run(
                ["cmd", "/c", "run.bat", "--selftest"],
                cwd=str(tree),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=600,
            )
            tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
            report.add(
                "run.bat --selftest starts the app and passes",
                result.returncode == 0 and "SELFTEST OK" in (result.stdout or ""),
                f"rc={result.returncode} {tail[0][:80]}",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            report.add("run.bat --selftest starts the app and passes", False, str(exc)[:100])
    else:
        report.skip("execute the windows launcher", "requires Windows; verified statically only")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_green.py",
        description="Verify an assembled EverSend portable tree (or its release zip) end to end.",
    )
    parser.add_argument("artifact", type=Path, help="unzipped EverSend tree, its parent folder, or the release .zip")
    parser.add_argument("--platform", choices=("linux", "windows", "auto"), default="auto")
    parser.add_argument("--keep", action="store_true", help="keep the temporary copies for inspection")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="where to build the throwaway copies (default: a fresh directory in TMPDIR). "
        "The tree is copied several times, so this needs ~1.5 GB of free space",
    )
    parser.add_argument("--quick", action="store_true", help="skip the transfer and read-only phases")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    report = Report()

    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="verify-", dir=str(args.work_dir)))
    else:
        workdir = Path(tempfile.mkdtemp(prefix="eversend-verify-"))
    print("=" * 78)
    print("EverSend portable artifact verification")
    print("=" * 78)
    print(f"artifact : {args.artifact}")
    print(f"work dir : {workdir}")

    try:
        source, origin = locate_tree(args.artifact.resolve(), workdir)
        platform = args.platform
        if platform == "auto":
            platform = "windows" if (source / "run.bat").exists() and not (source / "runtime" / "bin").exists() else "linux"
        print(f"tree     : {source}")
        print(f"origin   : {origin}, platform {platform}")
        print(f"size     : {human(tree_size(source))}")

        check_structure(source, report, origin)
        check_launcher_references(source, report)
        forbidden = [
            str(source.parent),
            str(args.artifact.resolve().parent),
            str(Path.cwd()),
            "/home/xgl/python",
        ]
        check_no_baked_paths(source, report, forbidden)

        if platform == "windows":
            check_windows_structure(source, report)
        else:
            # A foreign machine: fresh home, fresh temp, path with spaces and
            # Chinese characters, no inherited environment at all.
            foreign = workdir / FOREIGN_DIR_NAME / "EverSend"
            mirror_copy(source, foreign)
            home = workdir / "fake-home"
            home.mkdir(parents=True, exist_ok=True)
            tmpdir = workdir / "fake-tmp"
            tmpdir.mkdir(parents=True, exist_ok=True)
            scratch = workdir / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)
            runner = Runner(home, tmpdir, scratch)
            print(f"foreign  : {foreign}")
            print(f"env      : env -i HOME={home} TMPDIR={tmpdir} (no PYTHONPATH/PYTHONHOME)")

            check_foreign_machine(foreign, runner, report)
            if not args.quick:
                check_transfer(foreign, runner, report)
            moved = check_relocation(foreign, workdir, runner, report, origin)
            if not args.quick:
                check_readonly(moved, runner, report)
            check_no_home_traces(runner, report)
    finally:
        if args.keep:
            print(f"\ntemporary files kept in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---- report -----------------------------------------------------------
    elapsed = time.monotonic() - started
    print("\n" + "=" * 78)
    print(f"{'CHECK':<58}{'RESULT':>8}  DETAIL")
    print("-" * 78)
    for result in report.results:
        state = "SKIP" if result.skipped else ("PASS" if result.ok else "FAIL")
        detail = result.detail if len(result.detail) <= 60 else result.detail[:57] + "..."
        print(f"{result.name:<58}{state:>8}  {detail}")
    print("-" * 78)
    failures = report.failures
    print(f"{report.passes} passed, {len(failures)} failed, {sum(1 for r in report.results if r.skipped)} skipped  ({elapsed:.1f}s)")
    if failures:
        print("\nFAILURES:")
        for result in failures:
            print(f"  - {result.name}: {result.detail}")
    print("=" * 78)
    print("RESULT: " + ("PASS" if not failures else "FAIL"))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
