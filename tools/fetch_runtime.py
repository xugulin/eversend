#!/usr/bin/env python3
"""Fetch the relocatable CPython runtime and every wheel EverSend needs.

EverSend ships as a "green" (portable) application: the user unzips a single
``.zip`` onto a USB stick, double-clicks a launcher and everything runs out of
that folder.  Nothing may be installed and nothing may be downloaded at first
run, so the build has to bake in:

* a **relocatable** CPython interpreter -- python-build-standalone's
  ``install_only`` tarballs are built exactly for that (their ``sys.prefix`` is
  derived from the executable location, there is no hard-coded build prefix);
* every **wheel** the application imports, unzipped into a ``site-packages``
  shaped directory.

Why download wheels by hand instead of ``pip install``?

* the build machine has no ``pip`` (Arch keeps it in a separate package) and we
  do not want the build to depend on the developer's environment at all;
* a wheel is just a zip, and a *binary* wheel is already laid out for
  ``site-packages`` (extension modules sit at their final import path), so
  unzipping is the whole installation step -- provided we handle the three
  details that make naive unzipping wrong (see :func:`install_wheel`):
  ``.data/purelib`` remapping, symlink entries, and RECORD verification.

Everything is cached under ``tools/.cache/`` so a rebuild is offline and fast.

Usage::

    python3 tools/fetch_runtime.py --platform linux  --out tools/.cache/stage-linux
    python3 tools/fetch_runtime.py --platform windows --out tools/.cache/stage-windows
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import email.parser
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

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


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent
CACHE_DIR = TOOLS_DIR / ".cache"
DOWNLOAD_DIR = CACHE_DIR / "downloads"
HTTP_CACHE_DIR = CACHE_DIR / "http"

PBS_REPO = "astral-sh/python-build-standalone"
#: Default release tag.  Pinned so a build is reproducible; override with
#: ``--tag`` (the release is always *resolved* through the GitHub API, so a
#: stale asset name inside a tag cannot silently break the build).
DEFAULT_TAG = "20260901"
DEFAULT_PYTHON_VERSION = "3.14.7"

USER_AGENT = "eversend-green-build/1.0 (+https://example.invalid/eversend)"

#: ``--manylinux`` default order.  manylinux_2_34 is what the task specified;
#: see docs/PACKAGING.md for the portability trade-off (2_34 raises the glibc
#: floor to 2.34 = Ubuntu 22.04 / Debian 12, while 2_17 would accept much older
#: distributions but is not published for every project).
MANYLINUX_PREFERENCE = ("manylinux_2_34", "manylinux_2_28", "manylinux_2_17", "manylinux2014")

#: Timeouts.  PyPI's *project* level JSON endpoint takes ~200 s for projects
#: with thousands of releases (cryptography), while the version-pinned one
#: answers in ~1 s -- hence the two-stage resolution in :class:`PyPIIndex`.
HTTP_TIMEOUT = 60.0
JSON_TIMEOUT = 420.0
#: Per-read stall timeout for bulk downloads.  urllib applies the socket
#: timeout to every read, so a connection that opens but never delivers data
#: (which is exactly what ``objects.githubusercontent.com`` does from mainland
#: China) fails after this many seconds instead of hanging the build.
STALL_TIMEOUT = 30.0

#: Transport mirrors, tried in order when the primary URL does not deliver.
#:
#: GitHub's release CDN (``objects.githubusercontent.com``) is frequently
#: unreachable from China -- ``api.github.com`` answers in ~18 s while the
#: asset URL never sends a single byte.  These mirrors only ever act as a
#: *transport*: the SHA-256 digest always comes from the GitHub API (or from
#: PyPI), so a mirror cannot substitute different bytes for the real thing.
#: ``{repo}``/``{tag}``/``{asset}``/``{url}`` are filled in per candidate.
PBS_MIRRORS = (
    "https://mirror.nju.edu.cn/github-release/{repo}/{tag}/{asset}",
    "https://mirrors.ustc.edu.cn/github-release/{repo}/{tag}/{asset}",
    "https://gh-proxy.com/{url}",
    "https://ghproxy.net/{url}",
)

#: Same idea for wheels: Tsinghua and Aliyun mirror the ``/packages`` tree of
#: ``files.pythonhosted.org`` verbatim.
PYPI_MIRRORS = (
    "https://pypi.tuna.tsinghua.edu.cn/packages/{path}",
    "https://mirrors.aliyun.com/pypi/packages/{path}",
)


@dataclass(frozen=True)
class PlatformSpec:
    """Everything that differs between the two build targets."""

    name: str
    #: python-build-standalone target triple.
    pbs_triple: str
    #: Path inside the tarball that must exist (sanity check after download).
    pbs_probe_member: str
    #: Path of the interpreter relative to the unpacked ``runtime/``.
    interpreter_rel: str
    #: Platform tag preference list for wheel selection, best first.
    wheel_platforms: tuple[str, ...]
    #: PEP 508 marker environment *of the target*, not of the build host.
    marker_env: dict[str, str]


LINUX = PlatformSpec(
    name="linux",
    pbs_triple="x86_64-unknown-linux-gnu",
    pbs_probe_member="python/bin/python3",
    interpreter_rel="bin/python3",
    wheel_platforms=(
        *[f"{m}_x86_64" for m in MANYLINUX_PREFERENCE],
        "linux_x86_64",
        "any",
    ),
    marker_env={
        "os_name": "posix",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "x86_64",
        "platform_release": "",
        "platform_version": "",
        "python_version": "3.14",
        "python_full_version": DEFAULT_PYTHON_VERSION,
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "implementation_version": DEFAULT_PYTHON_VERSION,
        "extra": "",
    },
)

WINDOWS = PlatformSpec(
    name="windows",
    pbs_triple="x86_64-pc-windows-msvc",
    pbs_probe_member="python/python.exe",
    interpreter_rel="python.exe",
    wheel_platforms=("win_amd64", "any"),
    marker_env={
        "os_name": "nt",
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "platform_release": "",
        "platform_version": "",
        "python_version": "3.14",
        "python_full_version": DEFAULT_PYTHON_VERSION,
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "implementation_version": DEFAULT_PYTHON_VERSION,
        "extra": "",
    },
)

PLATFORMS = {LINUX.name: LINUX, WINDOWS.name: WINDOWS}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def human_size(num: float) -> str:
    """Format a byte count the way a human reads it."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024.0 or unit == "GiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GiB"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


class FetchError(RuntimeError):
    """Raised when a download or a verification step cannot be completed."""


def http_get(url: str, *, timeout: float = HTTP_TIMEOUT, headers: dict[str, str] | None = None) -> bytes:
    """GET ``url`` with retries, returning the body.

    Retries matter here because both GitHub and PyPI occasionally reset a
    connection mid-body; a build that fails on one flaky packet would be
    infuriating.
    """
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        merged.update(headers)
    last: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=merged)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # 4xx other than 429 will not get better by retrying.
            if exc.code not in (429, 500, 502, 503, 504):
                raise FetchError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
            last = exc
        except Exception as exc:  # noqa: BLE001 - network errors are open-ended
            last = exc
        if attempt < 3:
            delay = 2.0 * (attempt + 1)
            log(f"    ! {type(last).__name__}: {last} -- retrying in {delay:.0f}s")
            time.sleep(delay)
    raise FetchError(f"failed to GET {url}: {last}")


def github_url_candidates(browser_download_url: str, tag: str, asset_name: str) -> list[str]:
    """Primary GitHub URL first, then the mirrors."""
    candidates = [browser_download_url]
    for template in PBS_MIRRORS:
        candidates.append(
            template.format(repo=PBS_REPO, tag=tag, asset=asset_name, url=browser_download_url)
        )
    return candidates


def pypi_url_candidates(url: str) -> list[str]:
    """Primary PyPI URL first, then the package mirrors."""
    candidates = [url]
    marker = "files.pythonhosted.org/packages/"
    if marker in url:
        path = url.split(marker, 1)[1]
        for template in PYPI_MIRRORS:
            candidates.append(template.format(path=path))
    return candidates


def order_urls(urls: Sequence[str], mode: str) -> list[str]:
    """Apply ``--source``: ``auto`` keeps the primary first, ``mirror`` skips it.

    ``github.com`` itself is TCP-blackholed on plenty of Chinese networks
    (connect times out after minutes, while ``api.github.com`` answers fine),
    so waiting for the primary URL is pure waste there -- ``--source mirror``
    goes straight to a mirror.  The digest check makes both paths equivalent
    in trust.
    """
    if mode == "primary":
        return [urls[0]]
    if mode == "mirror":
        return list(urls[1:]) or list(urls)
    return list(urls)


def download_file(
    urls: str | Sequence[str],
    dest: Path,
    *,
    expect_sha256: str | None = None,
    expect_size: int | None = None,
    force: bool = False,
    timeout: float = STALL_TIMEOUT,
) -> Path:
    """Download from the first URL that delivers, verifying before publishing.

    The file only appears under its final name once it has been fully written
    *and* verified, so an interrupted build can never leave a half-downloaded
    artifact behind that a later run would happily reuse.  Every candidate URL
    is measured against the same digest, so falling back to a mirror does not
    weaken the guarantee.
    """
    candidates = [urls] if isinstance(urls, str) else list(urls)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        if expect_size is not None and dest.stat().st_size != expect_size:
            log(f"    cached {dest.name} has the wrong size, re-downloading")
        elif expect_sha256 is not None and sha256_file(dest) != expect_sha256:
            log(f"    cached {dest.name} has the wrong digest, re-downloading")
        else:
            log(f"    cached: {dest.name} ({human_size(dest.stat().st_size)})")
            return dest

    part = dest.with_name(dest.name + ".part")
    errors: list[str] = []
    for candidate_index, url in enumerate(candidates):
        for attempt in range(2):
            part.unlink(missing_ok=True)
            log(f"    GET {url}")
            started = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            digest = hashlib.sha256()
            written = 0
            last_report = 0.0
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response, part.open("wb") as fh:
                    total = int(response.headers.get("Content-Length") or 0)
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        fh.write(block)
                        digest.update(block)
                        written += len(block)
                        now = time.monotonic()
                        if now - last_report > 5.0:
                            last_report = now
                            pct = f"{written * 100 / total:5.1f}%" if total else "  ?  "
                            rate = written / max(1e-6, now - started)
                            print(
                                f"\r      {pct} {human_size(written)} @ {human_size(rate)}/s",
                                end="",
                                flush=True,
                            )
                if written:
                    print("\r" + " " * 60 + "\r", end="", flush=True)
            except Exception as exc:  # noqa: BLE001
                part.unlink(missing_ok=True)
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                log(f"    ! {type(exc).__name__}: {exc}")
                continue

            elapsed = time.monotonic() - started
            if expect_size is not None and written != expect_size:
                part.unlink(missing_ok=True)
                errors.append(f"{url}: expected {expect_size} bytes, got {written}")
                log(f"    ! size mismatch ({written} != {expect_size})")
                continue
            actual = digest.hexdigest()
            if expect_sha256 is not None and actual != expect_sha256:
                part.unlink(missing_ok=True)
                errors.append(f"{url}: sha256 mismatch ({actual} != {expect_sha256})")
                log(f"    ! digest mismatch for {dest.name}")
                continue
            part.replace(dest)
            source = "primary" if candidate_index == 0 else f"mirror #{candidate_index}"
            log(
                f"    ok: {dest.name} {human_size(written)} in {elapsed:.1f}s "
                f"via {source} (sha256 {actual[:16]}...)"
            )
            return dest
    raise FetchError(
        "all download sources failed for "
        + dest.name
        + "\n    "
        + "\n    ".join(errors[-4:])
        + f"\n    hint: download the file manually into {dest.parent} and re-run with --offline"
    )


# --------------------------------------------------------------------------
# PEP 425 wheel tags
# --------------------------------------------------------------------------

WHEEL_NAME_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)


@dataclass(frozen=True)
class WheelName:
    filename: str
    name: str
    version: str
    python: str
    abi: str
    platform: str

    @classmethod
    def parse(cls, filename: str) -> WheelName | None:
        match = WHEEL_NAME_RE.match(filename)
        if not match:
            return None
        groups = match.groupdict()
        return cls(
            filename=filename,
            name=groups["name"],
            version=groups["version"],
            python=groups["python"],
            abi=groups["abi"],
            platform=groups["platform"],
        )


def _interpreter_rank(python_tag: str, abi_tag: str) -> int | None:
    """Rank a ``{python}-{abi}`` pair for a CPython 3.14 target; lower is better.

    ``None`` means "not usable at all".  an ``abi3`` wheel builds against the
    stable ABI from some floor version ``cp3X`` onwards and therefore works for
    every later CPython -- that is why PySide6 ships ``cp310-abi3`` and runs on
    the 3.14 interpreter.
    """
    target_major, target_minor = 3, 14
    for py in python_tag.split("."):
        if py.startswith("cp3"):
            minor_text = py[3:]
            if not minor_text.isdigit():
                continue
            minor = int(minor_text)
            if abi_tag == "abi3" or abi_tag.startswith("abi3"):
                if minor <= target_minor:
                    # Prefer the newest abi3 floor we can use.
                    return 10 + (target_minor - minor)
                continue
            if minor == target_minor and abi_tag in ("cp314", f"cp3{minor}"):
                return 0
            continue
        if py.startswith("py3"):
            if abi_tag in ("none", "abi3"):
                return 50
        elif py.startswith("py2.py3"):
            if abi_tag == "none":
                return 51
    return None


def wheel_score(wheel: WheelName, spec: PlatformSpec) -> tuple[int, int] | None:
    """Return a sort key for ``wheel`` on ``spec``, or ``None`` if unusable."""
    platforms = [p.strip() for p in wheel.platform.split(".")]
    platform_rank: int | None = None
    for candidate in platforms:
        if candidate in spec.wheel_platforms:
            platform_rank = spec.wheel_platforms.index(candidate)
            break
    if platform_rank is None:
        return None
    ranks = [
        rank
        for rank in (
            _interpreter_rank(py.strip(), abi.strip())
            for py in wheel.python.split(".")
            for abi in wheel.abi.split(".")
        )
        if rank is not None
    ]
    if not ranks:
        return None
    return (min(ranks), platform_rank)


def pick_wheel(files: Sequence[dict[str, Any]], spec: PlatformSpec) -> dict[str, Any]:
    """Choose the best wheel for ``spec`` out of one release's file list."""
    scored: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for item in files:
        if item.get("packagetype") != "bdist_wheel" and not str(item.get("filename", "")).endswith(".whl"):
            continue
        parsed = WheelName.parse(str(item["filename"]))
        if parsed is None or parsed.name.endswith(".data"):
            continue
        score = wheel_score(parsed, spec)
        if score is not None:
            scored.append((score, item))
    if not scored:
        return {}
    scored.sort(key=lambda pair: (pair[0], pair[1]["filename"]))
    return scored[0][1]


# --------------------------------------------------------------------------
# PEP 508 markers
# --------------------------------------------------------------------------

_MARKER_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<op>===|==|!=|<=|>=|~=|<|>)
      | (?P<str>'[^']*'|"[^"]*")
      | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
      | (?P<lparen>\()
      | (?P<rparen>\))
    )""",
    re.VERBOSE,
)


class MarkerError(ValueError):
    """Raised when a marker cannot be parsed."""


def _tokenize_marker(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _MARKER_TOKEN_RE.match(text, pos)
        if not match:
            raise MarkerError(f"cannot tokenize marker at {text[pos:pos + 20]!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        value = match.group(kind)
        tokens.append((kind, value))
    return tokens


class _MarkerParser:
    """Recursive-descent parser for the subset of PEP 508 markers in the wild.

    We only ever *evaluate* markers, never re-serialise them, and the grammar
    used by real wheels is small: comparisons of variables/strings/literals
    combined with ``and``/``or``/``not`` and parentheses.
    """

    def __init__(self, tokens: list[tuple[str, str]], env: dict[str, str]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.env = env

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise MarkerError("unexpected end of marker")
        self.pos += 1
        return token

    def parse(self) -> bool:
        value = self.parse_or()
        if self.peek() is not None:
            raise MarkerError(f"trailing tokens in marker: {self.tokens[self.pos:]!r}")
        return value

    def parse_or(self) -> bool:
        value = self.parse_and()
        while True:
            token = self.peek()
            if token and token[0] == "name" and token[1] == "or":
                self.next()
                value = self.parse_and() or value
            else:
                return value

    def parse_and(self) -> bool:
        value = self.parse_atom()
        while True:
            token = self.peek()
            if token and token[0] == "name" and token[1] == "and":
                self.next()
                value = self.parse_atom() and value
            else:
                return value

    def parse_atom(self) -> bool:
        token = self.peek()
        if token is None:
            raise MarkerError("unexpected end of marker")
        if token[0] == "name" and token[1] == "not":
            self.next()
            return not self.parse_atom()
        if token[0] == "lparen":
            self.next()
            value = self.parse_or()
            closing = self.next()
            if closing[0] != "rparen":
                raise MarkerError("missing closing parenthesis")
            return value
        return self.parse_comparison()

    def parse_comparison(self) -> bool:
        left = self.parse_operand()
        token = self.peek()
        if token is None:
            raise MarkerError("expected a comparison operator")
        if token[0] == "name" and token[1] == "in":
            self.next()
            right = self.parse_operand()
            return left in right
        if token[0] == "name" and token[1] == "not":
            self.next()
            keyword = self.next()
            if keyword[1] != "in":
                raise MarkerError("expected 'in' after 'not'")
            right = self.parse_operand()
            return left not in right
        if token[0] != "op":
            raise MarkerError(f"expected an operator, got {token!r}")
        self.next()
        right = self.parse_operand()
        return _compare(token[1], left, right)

    def parse_operand(self) -> str:
        token = self.next()
        if token[0] == "str":
            return token[1][1:-1]
        if token[0] == "name":
            return self.env.get(token[1], "")
        raise MarkerError(f"unexpected operand {token!r}")


def _version_key(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts[:4]) if parts else (0,)


def _compare(operator: str, left: str, right: str) -> bool:
    if operator in ("==", "==="):
        return left == right
    if operator == "!=":
        return left != right
    left_key, right_key = _version_key(left), _version_key(right)
    if operator == "<":
        return left_key < right_key
    if operator == "<=":
        return left_key <= right_key
    if operator == ">":
        return left_key > right_key
    if operator == ">=":
        return left_key >= right_key
    if operator == "~=":
        return left_key >= right_key
    raise MarkerError(f"unsupported operator {operator!r}")


def marker_applies(marker: str, env: dict[str, str]) -> bool:
    """Evaluate a PEP 508 marker string against ``env``.

    A marker we cannot parse is treated as *True* (keep the dependency): a
    missing wheel at runtime is fatal, a spare wheel only costs disk space.
    """
    try:
        return _MarkerParser(_tokenize_marker(marker), env).parse()
    except MarkerError as exc:
        log(f"    ! unparsable marker {marker!r} ({exc}); keeping the dependency")
        return True


# --------------------------------------------------------------------------
# Wheel metadata
# --------------------------------------------------------------------------


@dataclass
class Requirement:
    raw: str
    name: str
    extras: tuple[str, ...]
    specifier: str
    marker: str


def parse_requires_dist(text: str) -> Requirement | None:
    """Parse one ``Requires-Dist`` line into its parts."""
    text = text.strip()
    if not text:
        return None
    marker = ""
    if ";" in text:
        head, _, marker = text.partition(";")
        text, marker = head.strip(), marker.strip()
    match = re.match(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<extras>\[[^\]]*\])?\s*(?P<spec>[^;]*)$", text)
    if not match:
        return None
    extras_text = (match.group("extras") or "").strip("[]")
    extras = tuple(part.strip() for part in extras_text.split(",") if part.strip())
    return Requirement(
        raw=text,
        name=match.group("name"),
        extras=extras,
        specifier=(match.group("spec") or "").strip(),
        marker=marker,
    )


def read_wheel_metadata(wheel: Path) -> tuple[dict[str, str], list[str]]:
    """Return ``(METADATA headers, Requires-Dist lines)`` for a wheel."""
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if not names:
            raise FetchError(f"{wheel.name} has no .dist-info/METADATA")
        raw = archive.read(names[0]).decode("utf-8", "replace")
    message = email.parser.Parser().parsestr(raw)
    headers = {key: message.get(key, "") for key in ("Name", "Version", "Requires-Python")}
    return headers, message.get_all("Requires-Dist") or []


def specifier_allows(specifier: str, version: str) -> bool:
    """Very small PEP 440 specifier evaluator (``==``, ``>=``, ``<``, ``!=``)."""
    if not specifier:
        return True
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(===|==|!=|<=|>=|<|>|~=)\s*(.+)$", clause)
        if not match:
            continue
        operator, wanted = match.group(1), match.group(2).strip().rstrip(".*")
        if not _compare(operator if operator != "===" else "==", version, wanted):
            return False
    return True


# --------------------------------------------------------------------------
# PyPI
# --------------------------------------------------------------------------


class PyPIIndex:
    """Minimal PyPI JSON API client with an on-disk cache.

    Two endpoints are used:

    * ``/pypi/<project>/json``          -- to learn the current version.  Slow
      for big projects, so the answer is cached forever.
    * ``/pypi/<project>/<version>/json`` -- small and fast; this is what we use
      whenever the version is already known (which is the normal case, because
      transitive dependencies arrive with an exact pin from ``Requires-Dist``).
    """

    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        HTTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cached_json(self, url: str, *, timeout: float) -> dict[str, Any]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        path = HTTP_CACHE_DIR / f"{key}.json"
        if path.exists():
            try:
                return json.loads(path.read_text("utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt cache entry is not fatal
                path.unlink(missing_ok=True)
        if self.offline:
            raise FetchError(f"offline mode and no cached response for {url}")
        started = time.monotonic()
        log(f"    API {url}")
        payload = json.loads(http_get(url, timeout=timeout, headers={"Accept": "application/json"}))
        path.write_text(json.dumps(payload), "utf-8")
        log(f"    API ok in {time.monotonic() - started:.1f}s")
        return payload

    def latest_version(self, project: str) -> str:
        payload = self._cached_json(f"https://pypi.org/pypi/{project}/json", timeout=JSON_TIMEOUT)
        return str(payload["info"]["version"])

    def release(self, project: str, version: str | None = None) -> dict[str, Any]:
        if version is None:
            payload = self._cached_json(f"https://pypi.org/pypi/{project}/json", timeout=JSON_TIMEOUT)
        else:
            payload = self._cached_json(f"https://pypi.org/pypi/{project}/{version}/json", timeout=HTTP_TIMEOUT)
        files = [f for f in payload.get("urls", []) if f.get("filename", "").endswith(".whl")]
        if not files:
            raise FetchError(f"no wheels published for {project} {version or '(latest)'}")
        return {"version": payload["info"]["version"], "files": files}


# --------------------------------------------------------------------------
# Interpreter (python-build-standalone)
# --------------------------------------------------------------------------


@dataclass
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None


def resolve_release(tag: str, python_version: str) -> dict[str, Any]:
    """Fetch a python-build-standalone release through the GitHub API."""
    url = f"https://api.github.com/repos/{PBS_REPO}/releases/tags/{tag}"
    cache = HTTP_CACHE_DIR / f"gh-{tag}.json"
    if cache.exists():
        log(f"    release metadata cached: {cache.name}")
        return json.loads(cache.read_text("utf-8"))
    log(f"    API {url}")
    try:
        payload = json.loads(
            http_get(url, headers={"Accept": "application/vnd.github+json"}, timeout=HTTP_TIMEOUT)
        )
    except FetchError as exc:
        raise FetchError(
            f"cannot resolve release {tag!r} via the GitHub API ({exc}).\n"
            f"    If assets are already in {DOWNLOAD_DIR}, re-run with --offline."
        ) from exc
    HTTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload), "utf-8")
    return payload


def pick_interpreter_asset(release: dict[str, Any], spec: PlatformSpec, python_version: str) -> Asset:
    """Choose the ``install_only`` tarball for ``spec``.

    Preference: the requested version (else the newest 3.14.x present), and the
    ``_stripped`` flavour when it exists -- stripping removes the test suite,
    ``ensurepip`` and shipped ``__pycache__``, which is pure win for a runtime
    that only has to *run* an application (36 MiB instead of 126 MiB on Linux).
    """
    candidates: list[tuple[tuple[int, ...], int, dict[str, Any]]] = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if not name.endswith(".tar.gz") or "install_only" not in name:
            continue
        if "freethreaded" in name:
            continue
        if f"-{spec.pbs_triple}-" not in name:
            continue
        version_match = re.match(r"^cpython-(\d+\.\d+\.\d+)\+", name)
        if not version_match:
            continue
        version = version_match.group(1)
        if python_version and version != python_version:
            continue
        # Sort key: version first, then the "stripped" flag, because we take
        # the *last* element -- so for one and the same version the stripped
        # tarball (35.9 MiB vs 119.7 MiB on Linux) wins.
        stripped = 1 if name.endswith("_stripped.tar.gz") else 0
        candidates.append((_version_key(version), stripped, asset))
    if not candidates:
        available = sorted(
            {
                re.match(r"^cpython-(\d+\.\d+\.\d+)\+", str(a.get("name", ""))).group(1)
                for a in release.get("assets", [])
                if re.match(r"^cpython-(\d+\.\d+\.\d+)\+", str(a.get("name", "")))
                and f"-{spec.pbs_triple}-" in str(a.get("name", ""))
                and "install_only" in str(a.get("name", ""))
            }
        )
        raise FetchError(
            f"no install_only tarball for {spec.pbs_triple} at version {python_version!r}.\n"
            f"    versions available in this release: {', '.join(available) or 'none'}"
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    best = candidates[-1][2]
    digest = str(best.get("digest") or "")
    return Asset(
        name=str(best["name"]),
        url=str(best["browser_download_url"]),
        size=int(best.get("size") or 0),
        sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
    )


def verify_tarball(path: Path, probe_member: str) -> None:
    """Prove the download really is a gzip tarball containing an interpreter.

    A captive portal or a proxy error page downloaded with HTTP 200 would
    otherwise only explode much later, inside ``tarfile``.
    """
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic != b"\x1f\x8b":
        raise FetchError(f"{path.name} is not gzip data (magic {magic!r})")
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
    except tarfile.TarError as exc:
        raise FetchError(f"{path.name} is not a readable tar.gz: {exc}") from exc
    if probe_member not in names:
        raise FetchError(
            f"{path.name} does not contain {probe_member!r}; first entries: {names[:5]}"
        )


def safe_members(archive: tarfile.TarFile, dest: Path) -> Iterator[tarfile.TarInfo]:
    """Yield members that stay inside ``dest`` (path traversal guard)."""
    root = dest.resolve()
    for member in archive:
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(root)):
            raise FetchError(f"refusing to extract {member.name!r}: escapes {dest}")
        if member.isdev():
            continue
        yield member


def extract_runtime(tarball: Path, dest: Path) -> Path:
    """Unpack ``python/...`` from the tarball into ``dest`` (the ``python/`` prefix is dropped)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as archive:
        for member in safe_members(archive, dest):
            parts = Path(member.name).parts
            if parts and parts[0] == "python":
                member.name = str(Path(*parts[1:])) if len(parts) > 1 else ""
            if not member.name or member.name == ".":
                continue
            archive.extract(member, dest, filter="fully_trusted")
    return dest


# --------------------------------------------------------------------------
# Wheel installation
# --------------------------------------------------------------------------


@dataclass
class InstallReport:
    wheel: str
    files: int = 0
    links: int = 0
    skipped: int = 0
    bytes: int = 0
    record_checked: int = 0
    record_mismatch: list[str] = field(default_factory=list)


def _safe_relpath(name: str) -> str | None:
    """Return ``name`` as a safe relative path, or ``None`` if it must be skipped."""
    if not name or name.endswith("/"):
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def install_wheel(
    wheel: Path,
    site_dir: Path,
    *,
    materialize_symlinks: bool = True,
    verify_record: bool = True,
) -> InstallReport:
    """Unzip a wheel into ``site_dir`` (a ``site-packages`` root).

    Three things make a naive ``extractall`` wrong, and all three are handled:

    1. **``.data`` directories.**  ``foo-1.0.data/purelib/...`` and
       ``.../platlib/...`` belong at the site-packages root, not in a
       subdirectory.  ``scripts``/``data``/``headers`` are build-machine
       concerns (console scripts and man pages) and are skipped.
    2. **Symlink entries.**  A wheel may store a symlink as a zip entry with
       ``S_IFLNK`` in the external attributes.  ``extractall`` would write a
       *text file* holding the link target, which then fails to load as a
       shared library.  We recreate it -- or, with ``materialize_symlinks``
       (the default), copy the target's bytes instead, because the artifact is
       going onto FAT32/exFAT USB sticks where symlinks do not exist and the
       zip format cannot represent them faithfully anyway.
    3. **Trust.**  Every extracted file is hashed and compared against the
       wheel's own ``RECORD``, which is what turns "we unzipped a binary blob
       into site-packages" into a verified operation.
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    report = InstallReport(wheel=wheel.name)
    digests: dict[str, str] = {}

    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                continue
            rel = _safe_relpath(name)
            if rel is None:
                report.skipped += 1
                continue

            target_rel = rel
            if ".data/" in rel:
                head, _, tail = rel.partition(".data/")
                category, _, rest = tail.partition("/")
                if category in ("purelib", "platlib") and rest:
                    target_rel = rest
                else:
                    report.skipped += 1
                    continue

            target = site_dir / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0xFFFF
            raw = archive.read(info)
            digests[rel] = hashlib.sha256(raw).hexdigest()

            if stat.S_ISLNK(mode):
                link_target = raw.decode("utf-8", "replace")
                report.links += 1
                if materialize_symlinks:
                    source = (target.parent / link_target).resolve()
                    if source.is_file():
                        shutil.copyfile(source, target)
                        report.files += 1
                        report.bytes += target.stat().st_size
                    else:
                        target.write_bytes(raw)
                        report.files += 1
                else:
                    target.unlink(missing_ok=True)
                    os.symlink(link_target, target)
                continue

            with target.open("wb") as fh:
                fh.write(raw)
            wanted_mode = mode & 0o777
            if wanted_mode:
                try:
                    os.chmod(target, wanted_mode)
                except OSError:
                    pass
            report.files += 1
            report.bytes += len(raw)

        if verify_record:
            records = [n for n in archive.namelist() if n.endswith(".dist-info/RECORD")]
            if records:
                for line in archive.read(records[0]).decode("utf-8", "replace").splitlines():
                    fields = line.split(",")
                    if len(fields) < 2 or not fields[1].startswith("sha256="):
                        continue
                    rel = _safe_relpath(fields[0])
                    if rel is None or rel not in digests:
                        continue
                    report.record_checked += 1
                    # RECORD stores the digest as unpadded URL-safe base64 of the
                    # raw sha256 bytes (PEP 376 / wheel spec), not as hex.
                    expected = (
                        base64.urlsafe_b64encode(bytes.fromhex(digests[rel])).decode().rstrip("=")
                    )
                    if fields[1][len("sha256=") :] != expected:
                        report.record_mismatch.append(rel)
    return report


# --------------------------------------------------------------------------
# Dependency resolution
# --------------------------------------------------------------------------


@dataclass
class ResolvedWheel:
    project: str
    version: str
    filename: str
    url: str
    size: int
    sha256: str | None
    required_by: list[str] = field(default_factory=list)
    path: Path | None = None


def resolve_requirements(
    roots: Sequence[str],
    spec: PlatformSpec,
    index: PyPIIndex,
    *,
    pins: dict[str, str] | None = None,
    force: bool = False,
    source: str = "auto",
) -> list[ResolvedWheel]:
    """Breadth-first resolution of ``roots`` and their transitive dependencies.

    Dependencies come from each wheel's own ``METADATA`` (``Requires-Dist``),
    which is authoritative and -- unlike the JSON API -- tells us *why* a
    dependency is needed, so markers such as
    ``cffi>=1.14; platform_python_implementation != "PyPy"`` can be evaluated
    against the **target** platform and foreign-platform extras can be dropped.
    """
    pins = {k.lower(): v for k, v in (pins or {}).items()}
    resolved: dict[str, ResolvedWheel] = {}
    #: project -> exact version pinned by a parent's Requires-Dist
    pending: list[tuple[str, str | None, str]] = [
        (name, pins.get(name.lower()), "requested") for name in roots
    ]

    while pending:
        project, pinned_version, required_by = pending.pop(0)
        key = project.lower()
        if key in resolved:
            if required_by not in resolved[key].required_by:
                resolved[key].required_by.append(required_by)
            continue

        version = pinned_version
        release = index.release(project, version)
        version = release["version"]
        choice = pick_wheel(release["files"], spec)
        if not choice:
            available = ", ".join(sorted(f["filename"] for f in release["files"])[:6])
            raise FetchError(
                f"no wheel of {project} {version} matches {spec.name} "
                f"(looked for {spec.wheel_platforms[:2]}...)\n    published: {available}"
            )

        digest = (choice.get("digests") or {}).get("sha256")
        entry = ResolvedWheel(
            project=project,
            version=version,
            filename=str(choice["filename"]),
            url=str(choice["url"]),
            size=int(choice.get("size") or 0),
            sha256=str(digest) if digest else None,
            required_by=[required_by],
        )
        entry.path = download_file(
            order_urls(pypi_url_candidates(entry.url), source),
            DOWNLOAD_DIR / entry.filename,
            expect_sha256=entry.sha256,
            expect_size=entry.size or None,
            force=force,
        )
        resolved[key] = entry

        headers, requires = read_wheel_metadata(entry.path)
        log(f"    {project} {version}: {len(requires)} Requires-Dist line(s)")
        for line in requires:
            requirement = parse_requires_dist(line)
            if requirement is None:
                continue
            if requirement.marker and not marker_applies(requirement.marker, spec.marker_env):
                log(f"      - skip (other platform): {line.strip()}")
                continue
            # Precedence: an exact ``==`` pin in Requires-Dist, then a
            # user supplied --pin, then "whatever is newest" (which costs a
            # slow PyPI project-level JSON call).
            pinned = None
            exact = re.match(r"^==\s*([^,;\s]+)$", requirement.specifier)
            if exact:
                pinned = exact.group(1)
            if pinned is None:
                pinned = pins.get(requirement.name.lower())
            pending.append((requirement.name, pinned, f"{project} {version}"))

    return list(resolved.values())


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_runtime.py",
        description="Download the relocatable CPython runtime and the wheels EverSend vendors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="linux")
    parser.add_argument("--out", type=Path, default=None, help="staging directory (default tools/.cache/stage-<platform>)")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"python-build-standalone release tag (default {DEFAULT_TAG})")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION, help="CPython version to embed")
    parser.add_argument("--force", action="store_true", help="re-download everything, ignoring the cache")
    parser.add_argument("--offline", action="store_true", help="use only cached downloads (no network)")
    parser.add_argument(
        "--source",
        choices=("auto", "primary", "mirror"),
        default="auto",
        help="transport preference for bulk downloads (auto: primary then mirrors; "
        "mirror: skip the primary, e.g. when github.com is blocked)",
    )
    parser.add_argument("--skip-interpreter", action="store_true")
    parser.add_argument("--skip-wheels", action="store_true")
    parser.add_argument(
        "--wheels",
        default="PySide6-Essentials,cryptography",
        help="comma separated root projects to vendor (default: PySide6-Essentials,cryptography)",
    )
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="PROJECT=VERSION",
        help="pin any project (including a transitive dependency) to an exact version, "
        "which skips PyPI's slow project-level JSON endpoint; repeatable",
    )
    parser.add_argument("--pyside-version", default=None, help="pin PySide6-Essentials instead of taking the latest")
    parser.add_argument("--cryptography-version", default=None, help="pin cryptography instead of taking the latest")
    parser.add_argument(
        "--symlinks",
        choices=("materialize", "preserve"),
        default="materialize",
        help="how to treat symlink entries inside wheels (default materialize: FAT32/exFAT safe)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    spec = PLATFORMS[args.platform]
    out_dir: Path = (args.out or (CACHE_DIR / f"stage-{spec.name}")).resolve()
    runtime_dir = out_dir / "runtime"
    site_dir = out_dir / "site"
    out_dir.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "platform": spec.name,
        "pbs_triple": spec.pbs_triple,
        "tag": args.tag,
        "python_version": args.python_version,
        "interpreter": None,
        "wheels": [],
        "symlinks": args.symlinks,
    }

    log(f"== EverSend green runtime fetch ==")
    log(f"platform : {spec.name} ({spec.pbs_triple})")
    log(f"out      : {out_dir}")

    if not args.skip_interpreter:
        log(f"\n[1/2] embedded CPython {args.python_version} from {PBS_REPO}@{args.tag}")
        if args.offline:
            # Offline: pick the cached tarball that matches the target triple.
            cached = sorted(DOWNLOAD_DIR.glob(f"cpython-{args.python_version}+*-{spec.pbs_triple}-install_only*.tar.gz"))
            if not cached:
                raise FetchError(f"--offline: no cached interpreter for {spec.pbs_triple} in {DOWNLOAD_DIR}")
            tarball = cached[0]
            log(f"    offline: using {tarball.name}")
            asset = Asset(name=tarball.name, url=str(tarball), size=tarball.stat().st_size, sha256=None)
        else:
            release = resolve_release(args.tag, args.python_version)
            asset = pick_interpreter_asset(release, spec, args.python_version)
            log(f"    asset: {asset.name} ({human_size(asset.size)})")
            tarball = download_file(
                order_urls(github_url_candidates(asset.url, args.tag, asset.name), args.source),
                DOWNLOAD_DIR / asset.name,
                expect_sha256=asset.sha256,
                expect_size=asset.size or None,
                force=args.force,
            )
        verify_tarball(tarball, spec.pbs_probe_member)
        log(f"    verified gzip tarball containing {spec.pbs_probe_member}")
        extract_runtime(tarball, runtime_dir)
        interpreter = runtime_dir / spec.interpreter_rel
        if not interpreter.exists():
            raise FetchError(f"interpreter missing after extraction: {interpreter}")
        if spec.name == "linux":
            interpreter.chmod(interpreter.stat().st_mode | 0o755)
        # NB: exclude symlinks -- `Path.is_file()` follows them, which would
        # count `bin/python3 -> python3.14` and `libpython3.14.so` twice.
        total = sum(
            p.stat().st_size for p in runtime_dir.rglob("*") if p.is_file() and not p.is_symlink()
        )
        links = sum(1 for p in runtime_dir.rglob("*") if p.is_symlink())
        log(
            f"    runtime/: {human_size(total)} in {sum(1 for _ in runtime_dir.rglob('*'))} entries "
            f"({links} symlink(s) to be materialised later)"
        )
        manifest["interpreter"] = {
            "asset": asset.name,
            "url": asset.url,
            "sha256": asset.sha256 or sha256_file(tarball),
            "size": tarball.stat().st_size,
            "unpacked_bytes": total,
        }

    if not args.skip_wheels:
        log(f"\n[2/2] wheels for {spec.name}")
        index = PyPIIndex(offline=args.offline)
        pins = {
            "PySide6-Essentials": args.pyside_version,
            "cryptography": args.cryptography_version,
        }
        for item in args.pin:
            name, _, version = item.partition("=")
            if not name or not version:
                raise FetchError(f"--pin expects PROJECT=VERSION, got {item!r}")
            pins[name.strip()] = version.strip()
        roots = [name.strip() for name in args.wheels.split(",") if name.strip()]
        wheels = resolve_requirements(roots, spec, index, pins=pins, force=args.force, source=args.source)
        if site_dir.exists():
            shutil.rmtree(site_dir)
        site_dir.mkdir(parents=True)
        for entry in sorted(wheels, key=lambda w: w.project.lower()):
            assert entry.path is not None
            report = install_wheel(
                entry.path,
                site_dir,
                materialize_symlinks=args.symlinks == "materialize",
            )
            flags = f"{report.files} files"
            if report.links:
                flags += f", {report.links} symlink(s) {args.symlinks}"
            if report.skipped:
                flags += f", {report.skipped} skipped (.data payload)"
            if report.record_checked:
                flags += f", RECORD verified {report.record_checked}"
            if report.record_mismatch:
                raise FetchError(
                    f"{entry.filename}: {len(report.record_mismatch)} file(s) do not match RECORD: "
                    f"{report.record_mismatch[:3]}"
                )
            log(f"    installed {entry.project} {entry.version}: {flags}")
            manifest["wheels"].append(
                {
                    "project": entry.project,
                    "version": entry.version,
                    "filename": entry.filename,
                    "url": entry.url,
                    "sha256": entry.sha256,
                    "size": entry.size,
                    "required_by": entry.required_by,
                    "files": report.files,
                    "record_verified": report.record_checked,
                    "unpacked_bytes": report.bytes,
                }
            )
        total = sum(p.stat().st_size for p in site_dir.rglob("*") if p.is_file() and not p.is_symlink())
        log(f"    site/: {human_size(total)}")
        manifest["site_bytes"] = total

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    log(f"\nmanifest: {manifest_path}")
    log("done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FetchError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
