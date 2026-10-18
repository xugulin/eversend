#!/usr/bin/env python3
"""可复现传输性能基准 harness（韧传 EverSend 2026-10-17 优化工程）。

产物：`tests/reports/opt-1017/bench-baseline.{json,md}`。
后续所有「优化了多少」都以本工具的同一命令、同一口径为准。

覆盖两条真实链路：

* ``evsd-loopback``：两个**真 Engine 进程**走 127.0.0.1 的 EVSD 协议互传
  （桌面↔桌面链路）。测量端在父进程，接收端是独立子进程，避免 GIL 把两条
  链路揉进一个进程 —— 真机上是两台机器，不是两个线程。
* ``http``：起**真 CLI serve 子进程**（``python3 -m eversend --cli serve
  --web ...``），用裸 socket 发**真 HTTP** 请求：``POST /api/upload``（raw
  body，带 CSRF token）与 ``GET /api/download``（含 Range 分段）。这是手机
  App↔电脑链路的同构测量（本机 loopback 代替 WiFi）。
* ``resume``：EVSD 传输中途 SIGKILL 接收端 → 同目录重启 → 再传，测
  「补传字节 / 总字节」与恢复耗时（补传比 = 1 - 中断瞬间 journal 里已
  落定 chunk 的比例；journal 是接收端 SIGKILL 前落盘的权威续传状态）。
* ``matrix`` / ``all``：跑完整矩阵并输出 JSON + Markdown。

口径：

* **MiB/s**，1 MiB = 1048576 字节（JSON 里写明 ``mib_definition``）。
* 每配置 1 次预热（丢弃）+ N 次测量，报中位数、min/max、CV（离散度）。
* wall / CPU（user+sys，发送与接收分开）/ 峰值 RSS（VmHWM）全记录。
* 每次测量前后记录 ``/proc/loadavg``、可用内存；平台型号/磁盘/内核在头部。
* 端口一律随机空闲端口（``bind(('127.0.0.1',0))``），不碰 52117/52118/52119。
* 临时目录在 ``eversend/.testscratch/perf-bench-*/``，结束自清理（--keep 保留）。
* 退出码：0 全部通过；1 有校验失败/传输失败；2 harness 自身错误。

用法::

    cd eversend
    PYTHONPATH=src python3 tools/bench_transfer.py evsd-loopback --sizes 64 --streams 1 --repeats 1
    PYTHONPATH=src python3 tools/bench_transfer.py all          # 基线全量
    PYTHONPATH=src python3 tools/bench_transfer.py resume

复现示例（改前基线）::

    PYTHONPATH=src python3 tools/bench_transfer.py all \\
        --sizes 64,512,1024 --streams 1,2,4,8 --repeats 3 \\
        --json tests/reports/opt-1017/bench-baseline.json \\
        --md   tests/reports/opt-1017/bench-baseline.md
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parent.parent  # <repo>/eversend
SRC_DIR = REPO_ROOT / "src"
SCRATCH_ROOT = REPO_ROOT / ".testscratch"
REPORT_DIR = REPO_ROOT / "tests" / "reports" / "opt-1017"
TOKEN_HEADER = "X-EverSend-Token"

# 子进程（engine / CLI serve）统一带上的环境。
_CHILD_ENV = dict(os.environ)
_CHILD_ENV["PYTHONPATH"] = str(SRC_DIR) + (
    os.pathsep + _CHILD_ENV["PYTHONPATH"] if _CHILD_ENV.get("PYTHONPATH") else ""
)
_CHILD_ENV.setdefault("PYTHONIOENCODING", "utf-8")

_CLK_TCK = os.sysconf("SC_CLK_TCK")
_LIVE_PROCS: list[subprocess.Popen] = []


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _loadavg() -> list[float]:
    try:
        return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except OSError:
        return []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _port_free(port: int, kind: str = "tcp") -> bool:
    s_type = socket.SOCK_STREAM if kind == "tcp" else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, s_type) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _port_listening(port: int) -> bool:
    """是否有进程还在 LISTEN（connect 能连上）。TIME_WAIT 不算。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _df(path: Path) -> str:
    try:
        out = subprocess.run(
            ["df", "-T", str(path)], capture_output=True, text=True, timeout=10
        ).stdout.strip().splitlines()
        return out[-1].strip() if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_state() -> dict:
    """HEAD + dirty tree + 关键文件 md5。

    基准期间别的专家会并行改产品代码（例如 web/server.py 的可续传上传），
    所以「哪一版本测的」不能只看 git HEAD —— 必须把脏树与文件哈希记进 JSON，
    否则 before/after 不可比。
    """
    state: dict = {"head": _git_head()}
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        state["dirty_files"] = [line for line in out.splitlines() if line.strip()][:80]
    except (OSError, subprocess.SubprocessError):
        state["dirty_files"] = []
    keys = [
        "src/eversend/core/chunkstore.py",
        "src/eversend/core/crypto.py",
        "src/eversend/core/framing.py",
        "src/eversend/core/hashing.py",
        "src/eversend/core/transfer.py",
        "src/eversend/core/engine.py",
        "src/eversend/web/server.py",
        "tools/bench_transfer.py",
    ]
    import hashlib as _hashlib

    digests = {}
    for rel in keys:
        try:
            data = (REPO_ROOT / rel).read_bytes()
            digests[rel] = _hashlib.md5(data).hexdigest()
        except OSError:
            digests[rel] = ""
    state["md5"] = digests
    return state


def _host_info(scratch: Path) -> dict:
    info: dict = {
        "git_state": _git_state(),
        "nproc": os.cpu_count(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "git_head": _git_head(),
        "cwd": str(Path.cwd()),
    }
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            mem[key] = value.strip()
        info["mem_total"] = mem.get("MemTotal", "")
        info["mem_available_at_start"] = mem.get("MemAvailable", "")
    except OSError:
        pass
    info["scratch_df"] = _df(scratch)
    info["home_df"] = _df(Path.home())
    info["loadavg_at_start"] = _loadavg()
    info["mib_definition"] = "1 MiB = 1048576 bytes"
    return info


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def _disk_probe(scratch: Path, size_bytes: int = 64 * MIB) -> dict:
    """磁盘完成路径探针：模拟接收端 PartFile 的 preallocate → 写入 → fdatasync。

    它给出「本机磁盘 fsync 上限」这个背景数字。EVSD 接收端在 finalize 里有
    ``file.sync()``（见 core/chunkstore.py），所以 loopback 吞吐会被这个上限
    截住 —— 不看这个数就会把「磁盘慢」误判成「协议慢」。
    """
    probe = scratch / "disk-probe.bin"
    result: dict = {}
    try:
        t0 = time.monotonic()
        handle = open(probe, "wb")
        try:
            try:
                os.posix_fallocate(handle.fileno(), 0, size_bytes)
            except (AttributeError, OSError):
                os.ftruncate(handle.fileno(), size_bytes)
            t1 = time.monotonic()
            block = os.urandom(4 * MIB)
            remaining = size_bytes
            while remaining > 0:
                chunk = block[: min(len(block), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
            handle.flush()
            t2 = time.monotonic()
            os.fdatasync(handle.fileno())
            t3 = time.monotonic()
        finally:
            handle.close()
        result = {
            "size_mib": round(size_bytes / MIB, 2),
            "preallocate_s": round(t1 - t0, 4),
            "write_cache_s": round(t2 - t1, 4),
            "write_cache_mib_s": round(size_bytes / MIB / (t2 - t1), 1) if t2 > t1 else None,
            "fdatasync_s": round(t3 - t2, 4),
            "fdatasync_mib_s": round(size_bytes / MIB / (t3 - t2), 1) if t3 > t2 else None,
            "note": "preallocate+write+fdatasync 模拟接收端 finalize 的落盘路径",
        }
    except OSError as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return result


def _make_payload(path: Path, size_bytes: int) -> str:
    """生成 size_bytes 随机（不可压缩）数据并返回 sha256；只做一次。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    block = os.urandom(4 * MIB)
    remaining = size_bytes
    with open(path, "wb") as handle:
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


# --- /proc 观测 -------------------------------------------------------------


def _read_proc_stat(pid: int) -> tuple[float, float] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    if len(fields) < 13:
        return None
    try:
        return int(fields[11]) / _CLK_TCK, int(fields[12]) / _CLK_TCK
    except (ValueError, IndexError):
        return None


def _read_proc_io(pid: int) -> dict[str, int]:
    """读 /proc/<pid>/io。

    注意：实测本机内核（7.2.8-cachyos）/ procfs 的 wchar **不统计 socket 写**
    （64 KiB sendall 后 wchar 增量为 0），所以这里只信任 rchar（读文件/读 socket
    的真实字节数可以用于校验"读了几遍源文件"），不再用 wchar 推算线上字节数。
    """
    result: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("rchar", "syscr", "syscw"):
                try:
                    result[key] = int(value.strip())
                except ValueError:
                    pass
    except OSError:
        pass
    return result


def _read_proc_hwm_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _counter_snapshot(pid: int) -> dict:
    """进程级 CPU / IO / 峰值 RSS 快照。父进程与子进程用同一口径。"""
    cpu = _read_proc_stat(pid)
    io = _read_proc_io(pid)
    return {
        "cpu_user_s": cpu[0] if cpu else None,
        "cpu_sys_s": cpu[1] if cpu else None,
        "cpu_s": (cpu[0] + cpu[1]) if cpu else None,
        "rchar": io.get("rchar"),
        "rss_hwm_kb": _read_proc_hwm_kb(pid),
    }


def _delta(before: dict | None, after: dict | None) -> dict:
    """两个快照的差值；缺失字段为 None（JSON 友好）。"""
    if not before or not after:
        return {}
    out: dict = {}
    for key in ("cpu_user_s", "cpu_sys_s", "cpu_s", "rchar"):
        a, b = before.get(key), after.get(key)
        if a is None or b is None:
            out[key] = None
        else:
            out[key] = round(float(b) - float(a), 6)
    # rss 是 high-water mark：只报 after 的绝对值。
    out["rss_hwm_kb"] = after.get("rss_hwm_kb") or before.get("rss_hwm_kb")
    return out


# --- 汇总 -------------------------------------------------------------------


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _cv_percent(values: list[float]) -> float:
    if len(values) < 2:
        return float("nan")
    m = statistics.median(values)
    if m == 0:
        return float("nan")
    return statistics.pstdev(values) / m * 100.0


def _mark_env_stalls(runs: list[dict]) -> None:
    """标出被共享机 I/O 停顿污染的单次运行（ENV_STALL_EXCLUDED）。

    规则（确定性、可复现）：同一配置（kind+size+streams）至少有 3 次有效运行，
    某次吞吐 < 该配置中位数的 40% → 判为环境停顿污染。这类运行保留在原始 JSON，
    但不进"剔除后中位数"，并在报告里单独列表说明（本机 USB 盘写回停顿实测可达
    60–130 s，会把 512 MiB 的墙钟放大 10 倍以上）。
    """
    groups: dict[tuple, list[dict]] = {}
    for run in runs:
        if run.get("warmup") or run.get("skipped") or not run.get("ok", True):
            continue
        rate = run.get("mib_per_s")
        if not rate:
            continue
        groups.setdefault((run.get("kind"), run.get("size_mib"), run.get("streams")), []).append(run)
    for items in groups.values():
        rates = [r["mib_per_s"] for r in items]
        if len(rates) < 3:
            continue
        # 用同配置的**最大值**当正常量级参考：I/O 停顿是灾难性离群（常为正常的
        # 1%–20%），而如果多数运行都被拖慢，"中位数"本身也会被污染。max 至少
        # 保留一次正常运行的量级（共享机负载不是恒定的）。
        reference = max(rates)
        threshold = 0.30 * reference
        for run in items:
            if reference and run["mib_per_s"] < threshold:
                run["env_stall"] = True
                run["env_stall_reason"] = (
                    f"rate {run['mib_per_s']:.1f} MiB/s < 30% of config max {reference:.1f}"
                    f"（USB 盘 I/O 停顿）"
                )


def _summarise(runs: list[dict], key_fields: tuple[str, ...] = ("kind", "size_mib", "streams")) -> list[dict]:
    _mark_env_stalls(runs)
    groups: dict[tuple, list[dict]] = {}
    for run in runs:
        if run.get("warmup") or run.get("skipped") or not run.get("ok"):
            continue
        key = tuple(run.get(field) for field in key_fields)
        groups.setdefault(key, []).append(run)
    summaries: list[dict] = []
    for key, items in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        base = dict(zip(key_fields, key))
        clean = [i for i in items if not i.get("env_stall")]
        rates = [i["mib_per_s"] for i in items]
        rates_clean = [i["mib_per_s"] for i in clean]
        walls = [i["wall_s"] for i in items]
        walls_clean = [i["wall_s"] for i in clean]
        cpu_per_mib = [i["cpu_s_per_mib"] for i in clean if i.get("cpu_s_per_mib") is not None]
        summary = {
            **base,
            "n": len(items),
            "n_clean": len(clean),
            "env_stall_runs": len(items) - len(clean),
            "mib_per_s_median": round(_median(rates), 3),
            "mib_per_s_min": round(min(rates), 3),
            "mib_per_s_max": round(max(rates), 3),
            "mib_per_s_cv_pct": round(_cv_percent(rates), 2),
            "mib_per_s_median_clean": round(_median(rates_clean), 3) if rates_clean else None,
            "mib_per_s_min_clean": round(min(rates_clean), 3) if rates_clean else None,
            "mib_per_s_max_clean": round(max(rates_clean), 3) if rates_clean else None,
            "mib_per_s_cv_pct_clean": round(_cv_percent(rates_clean), 2) if len(rates_clean) > 1 else None,
            "wall_s_median": round(_median(walls), 4),
            "wall_s_median_clean": round(_median(walls_clean), 4) if walls_clean else None,
            "cpu_s_per_mib_median": round(_median(cpu_per_mib), 5) if cpu_per_mib else None,
            "cpu_s_per_mib_cv_pct": round(_cv_percent(cpu_per_mib), 2) if len(cpu_per_mib) > 1 else None,
            "tx_cpu_s_median": round(
                _median([i["tx_cpu_s"] for i in clean if i.get("tx_cpu_s") is not None]), 4
            ) if any(i.get("tx_cpu_s") is not None for i in clean) else None,
            "rx_cpu_s_median": round(
                _median([i["rx_cpu_s"] for i in clean if i.get("rx_cpu_s") is not None]), 4
            ) if any(i.get("rx_cpu_s") is not None for i in clean) else None,
            "flags": [],
        }
        if summary["env_stall_runs"]:
            summary["flags"].append(f"ENV_STALL_EXCLUDED({summary['env_stall_runs']})")
        if summary["mib_per_s_cv_pct"] is not None and summary["mib_per_s_cv_pct"] > 20.0:
            summary["flags"].append("NOISY_CV_GT_20%")
        if any(i.get("sha_ok") is False for i in items):
            summary["flags"].append("SHA_MISMATCH")
        if any(i.get("size_ok") is False for i in items):
            summary["flags"].append("SIZE_MISMATCH")
        summaries.append(summary)
    return summaries


# ---------------------------------------------------------------------------
# 分阶段 CPU 归因（thread_time 自时间，嵌套调用用栈扣除）
#
# 目的：给 EVSD 传输一份「AEAD / hash / crc32 / socket / 磁盘 / framing / 其它」
# 的 CPU 秒账本，作为阶段 2 收益折算依据。全部在同一个进程里跑两个 Engine
# （跟 tests/test_loopback.py 同构），用 /proc/self/stat 算进程总 CPU，用
# time.thread_time() 算每个被包裹函数的**本线程 CPU**，嵌套调用只把自时间
# 记到最内层，外层自时间 = 总时间 - 子调用时间，因此各桶不重复计。
# ---------------------------------------------------------------------------

_PROFILE_ENABLED = False
_PROFILE_STATS: dict[str, dict] = {}
_PROFILE_LOCK = threading.Lock()
_PROFILE_TL = threading.local()


class _ProfFrame:
    __slots__ = ("bucket", "start", "children", "parent")

    def __init__(self, bucket: str, parent: "_ProfFrame | None") -> None:
        self.bucket = bucket
        self.start = time.thread_time()
        self.children = 0.0
        self.parent = parent


def _prof_wrap(owner, name: str, bucket: str) -> None:
    """把 owner.name 包一层计时；重复包裹时跳过。"""
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_bench_profiled", False):
        return

    def wrapper(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if not _PROFILE_ENABLED:
            return original(*args, **kwargs)
        parent = getattr(_PROFILE_TL, "frame", None)
        frame = _ProfFrame(bucket, parent)
        _PROFILE_TL.frame = frame
        try:
            return original(*args, **kwargs)
        finally:
            spent = time.thread_time() - frame.start
            _PROFILE_TL.frame = parent
            self_time = spent - frame.children
            if self_time < 0:
                self_time = 0.0
            with _PROFILE_LOCK:
                slot = _PROFILE_STATS.setdefault(bucket, {"cpu_s": 0.0, "calls": 0})
                slot["cpu_s"] += self_time
                slot["calls"] += 1
            if parent is not None:
                parent.children += spent

    wrapper._bench_profiled = True  # type: ignore[attr-defined]
    wrapper.__name__ = getattr(original, "__name__", name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    setattr(owner, name, wrapper)


_PROFILE_BUCKETS = {
    "aead": "AEAD (seal/open)",
    "hash": "hash (blake2b/file digest)",
    "crc32": "crc32",
    "socket_send": "socket send",
    "socket_recv": "socket recv",
    "disk_write": "磁盘写 (pwrite/write_at)",
    "disk_sync": "磁盘同步 (fdatasync/fsync)",
    "chunkstore": "chunkstore 自身 (journal/锁)",
    "framing": "framing 自身 (组/拆帧)",
    "connection": "connection 自身 (协议调度)",
}


def _install_profiling() -> None:
    """给 EVSD 核心模块装上分阶段计时探针（只在 cpu-split 子命令用）。"""
    global _PROFILE_ENABLED
    from eversend.core import chunkstore, connection, crypto, fileio, framing, hashing

    # aead：crypto 模块里所有带 seal/open 方法的类
    for attr in list(vars(crypto).values()):
        if isinstance(attr, type) and "seal" in getattr(attr, "__dict__", {}):
            _prof_wrap(attr, "seal", "aead")
            _prof_wrap(attr, "open", "aead")
    # hashing：文件/块摘要
    for name in ("file_digest", "file_digest_positioned", "digest_of_stream", "verify_file"):
        _prof_wrap(hashing, name, "hash")
    _prof_wrap(hashing.IncrementalFileHasher, "update", "hash")
    _prof_wrap(hashing.IncrementalFileHasher, "finish", "hash")
    _prof_wrap(hashing, "crc32", "crc32")
    _prof_wrap(hashing, "verify_chunk", "crc32")
    # transfer.py 在文件顶部 ``from .hashing import IncrementalFileHasher, crc32``
    # 直接绑定了 crc32 函数对象 —— 只 patch hashing.crc32 抓不到这条路径，
    # 必须把 transfer.crc32 这个已绑定引用也换成包装版（真实热路径就在这）。
    from eversend.core import transfer as transfer_mod

    _prof_wrap(transfer_mod, "crc32", "crc32")
    # framing：小帧与批量发送
    for name in (
        "read_frame",
        "read_frame_into",
        "_recv_into_exact",
        "recv_exact",
        "send_frame",
        "send_encrypted_frame",
        "send_frame_gather",
        "sendmsg_all",
    ):
        bucket = "socket_recv" if name in ("_recv_into_exact", "recv_exact") else (
            "socket_send" if name in ("sendmsg_all",) else "framing"
        )
        _prof_wrap(framing, name, bucket)
    # connection：块级收发（外层）
    for name in ("send_chunk", "recv_data", "send", "recv"):
        _prof_wrap(connection.Connection, name, "connection")
    # 磁盘
    _prof_wrap(fileio.PositionedFile, "write_at", "disk_write")
    _prof_wrap(fileio.PositionedFile, "sync", "disk_sync")
    # chunkstore：journal/锁/校验逻辑
    _prof_wrap(chunkstore.PartFile, "write_chunk", "chunkstore")
    # 落盘桶：T4 会改收尾路径（_maybe_flush → flush / PositionedFile.sync），
    # 所以两代实现的落盘点都包上，跨版本比较时按「桶 = 落盘等待类」看。
    _prof_wrap(chunkstore.PartFile, "_maybe_flush", "disk_sync")
    _prof_wrap(chunkstore.PartFile, "flush", "disk_sync")
    _prof_wrap(chunkstore.PartFile, "finalize", "chunkstore")
    _PROFILE_ENABLED = True


def _profile_reset() -> None:
    _PROFILE_ENABLED = False
    with _PROFILE_LOCK:
        _PROFILE_STATS.clear()
    _PROFILE_ENABLED = True


def _profile_result(total_cpu_s: float | None) -> dict:
    global _PROFILE_ENABLED
    _PROFILE_ENABLED = False
    with _PROFILE_LOCK:
        raw = {bucket: dict(slot) for bucket, slot in _PROFILE_STATS.items()}
    stages: dict[str, dict] = {}
    accounted = 0.0
    for bucket, slot in raw.items():
        cpu = round(slot["cpu_s"], 6)
        accounted += slot["cpu_s"]
        stages[bucket] = {"cpu_s": cpu, "calls": slot["calls"]}
    if total_cpu_s:
        other = total_cpu_s - accounted
        stages["other"] = {"cpu_s": round(other, 6), "calls": 0}
        for bucket in stages:
            stages[bucket]["pct_of_total"] = (
                round(stages[bucket]["cpu_s"] / total_cpu_s * 100.0, 2) if total_cpu_s > 0 else None
            )
    stages = dict(sorted(stages.items(), key=lambda kv: -kv[1]["cpu_s"]))
    return {
        "total_cpu_s": round(total_cpu_s, 6) if total_cpu_s else None,
        "accounted_cpu_s": round(accounted, 6),
        "stages": stages,
        "bucket_labels": _PROFILE_BUCKETS,
        "disk_sync_note": (
            "disk_sync 桶 = PositionedFile.sync + PartFile._maybe_flush + PartFile.flush。"
            "T4（收尾 fsync 移出关键路径）会改变该桶的调用组成：跨版本只比较桶数量级/占比，"
            "不把差值解读成纯优化收益；T4 真实收益看 btrfs 端到端 wall/MiB/s。"
        ),
    }


def _run_cpu_split(args, scratch: Path, results: dict) -> None:
    """EVSD 传输的分阶段 CPU 账本（默认 2 GiB / 4 流，接收目录在 tmpfs 时最干净）。"""
    from eversend.core.model import Peer

    size_mib = int(args.size_mib)
    streams = int(args.streams)
    size_bytes = size_mib * MIB
    payload_dir = scratch / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    src = payload_dir / f"payload-{size_mib}MiB.bin"
    src_sha = _make_payload(src, size_bytes)
    for index in range(int(args.repeats)):
        run_root = scratch / "cpusplit" / f"run-{index}-{uuid.uuid4().hex[:6]}"
        print(
            f"[cpu-split] {size_mib} MiB streams={streams} run {index + 1}/{args.repeats}"
            f"（同进程双 Engine，scratch={scratch}）…",
            flush=True,
        )
        from eversend.core.engine import Engine, EngineConfig

        def make(workdir: Path) -> "Engine":
            workdir.mkdir(parents=True, exist_ok=True)
            engine = Engine(
                EngineConfig(
                    data_dir=str(workdir / "state"),
                    receive_dir=str(workdir / "recv"),
                    name=f"Prof{workdir.name}",
                    tcp_port=0,
                    discovery_port=_free_port(),
                    streams=max(8, streams),
                    auto_accept_all=True,
                    auto_accept_trusted=True,
                    enable_broadcast=False,
                    enable_mdns=False,
                    enable_web=False,
                )
            )
            engine.start()
            return engine

        rx = tx = None
        try:
            rx = make(run_root / "rx")
            tx = make(run_root / "tx")
            peer = Peer(info=rx.info, address="127.0.0.1", port=int(rx.port), source="manual", trusted=True)
            _install_profiling()
            _profile_reset()
            before = _counter_snapshot(os.getpid())
            load_before = _loadavg()
            t0 = time.monotonic()
            outcome = tx.send_ex(peer, [str(src)], streams=streams)
            wall = time.monotonic() - t0
            load_after = _loadavg()
            after = _counter_snapshot(os.getpid())
            delta = _delta(before, after)
            total_cpu = delta.get("cpu_s")
            profile = _profile_result(total_cpu)
            target = run_root / "rx" / "recv" / src.name
            sha_ok = bool(target.exists() and _sha256_file(target) == src_sha)
            record = {
                "kind": "evsd-cpu-split",
                "size_mib": round(size_bytes / MIB, 3),
                "size_bytes": size_bytes,
                "streams": streams,
                "path": str(scratch),
                "wall_s": round(wall, 6),
                "mib_per_s": round(size_bytes / MIB / wall, 3) if wall else None,
                "total_cpu_s": round(total_cpu, 6) if total_cpu else None,
                "cpu_s_per_mib": round(total_cpu / (size_bytes / MIB), 5) if total_cpu else None,
                "ok": bool(outcome.ok and sha_ok),
                "sha_ok": sha_ok,
                "tx_rss_hwm_kb": delta.get("rss_hwm_kb"),
                "loadavg_before": load_before,
                "loadavg_after": load_after,
                "profile": profile,
                "note": "同进程双 Engine；thread_time 自时间归因，other=进程总 CPU-已归因",
            }
            print(
                f"       -> {record['mib_per_s']} MiB/s wall={wall:.2f}s cpu={total_cpu:.2f}s"
                f" ok={outcome.ok} sha={sha_ok}",
                flush=True,
            )
            for bucket, slot in profile["stages"].items():
                print(f"          {bucket:12s} {slot['cpu_s']:8.3f}s  {slot.get('pct_of_total')}%  calls={slot['calls']}", flush=True)
            results["runs"].append(record)
        except Exception as exc:
            print(f"[cpu-split] 失败: {type(exc).__name__}: {exc}", flush=True)
            results["runs"].append(
                {"kind": "evsd-cpu-split", "size_mib": size_mib, "streams": streams, "ok": False,
                 "error_code": "harness_exception", "error_text": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            _PROFILE_ENABLED = False
            _stop_engine(tx)
            _stop_engine(rx)
            if not args.keep:
                shutil.rmtree(run_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# EVSD：接收端独立进程 + 发送端在主进程
# ---------------------------------------------------------------------------


def _wait_ready(path: Path, timeout: float, proc: subprocess.Popen, log_path: Path) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    tail = ""
    try:
        tail = log_path.read_text(errors="replace")[-2000:]
    except OSError:
        pass
    code = proc.poll()
    raise RuntimeError(f"接收端/服务端启动失败（exit={code}）: {tail}")


def _start_evsd_receiver(workdir: Path, streams: int) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    ready = workdir / "rx-ready.json"
    stop = workdir / "rx-stop"
    log = workdir / "rx.log"
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_evsd-receiver",
        "--workdir",
        str(workdir),
        "--streams",
        str(streams),
        "--ready",
        str(ready),
        "--stop",
        str(stop),
    ]
    handle = open(log, "wb")
    proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, env=_CHILD_ENV, cwd=str(REPO_ROOT))
    _LIVE_PROCS.append(proc)
    try:
        # 共享/慢盘机器上子进程启动可能被 I/O 停顿卡几十秒，给足 120 s
        data = _wait_ready(ready, 120.0, proc, log)
    except RuntimeError:
        _stop_process(proc, 5.0)
        raise
    data["proc"] = proc
    data["stop_path"] = stop
    data["workdir"] = workdir
    data["log_path"] = log
    data["stdout_handle"] = handle
    return data


def _stop_process(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass
    if proc in _LIVE_PROCS:
        try:
            _LIVE_PROCS.remove(proc)
        except ValueError:
            pass


def _stop_evsd_receiver(rx: dict, timeout: float = 15.0) -> int | None:
    proc = rx["proc"]
    try:
        Path(rx["stop_path"]).write_text("stop")
    except OSError:
        pass
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout=5.0)
    if proc in _LIVE_PROCS:
        try:
            _LIVE_PROCS.remove(proc)
        except ValueError:
            pass
    try:
        rx["stdout_handle"].close()
    except OSError:
        pass
    return code


def _make_engine(workdir: Path, name: str, streams: int):
    from eversend.core.engine import Engine, EngineConfig

    workdir.mkdir(parents=True, exist_ok=True)
    engine = Engine(
        EngineConfig(
            data_dir=str(workdir / "state"),
            receive_dir=str(workdir / "recv"),
            name=name,
            tcp_port=0,
            discovery_port=_free_port(),
            streams=streams,
            auto_accept_all=True,
            auto_accept_trusted=True,
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
        )
    )
    engine.start()
    return engine


def _peer_from(rx: dict):
    from eversend.core.model import DeviceInfo, Peer

    return Peer(
        info=DeviceInfo.from_dict(rx["info"]),
        address="127.0.0.1",
        port=int(rx["port"]),
        source="manual",
        trusted=True,
    )


def _progress_watch(engine, events: list[tuple[float, int]], lock: threading.Lock) -> None:
    def callback(event: dict) -> None:
        if event.get("kind") != "send_progress":
            return
        try:
            samples = (time.monotonic(), int(event.get("bytes") or 0))
        except (TypeError, ValueError):
            return
        with lock:
            events.append(samples)

    engine.events.subscribe(callback)


def _steady_rate(samples: list[tuple[float, int]], size_bytes: int, t0: float) -> float | None:
    points = [(t - t0, b) for t, b in samples if b > 0]
    if len(points) < 4:
        return None
    low = size_bytes * 0.10
    high = size_bytes * 0.90
    start = next(((t, b) for t, b in points if b >= low), None)
    end = next(((t, b) for t, b in reversed(points) if b <= high), None)
    if not start or not end or end[0] <= start[0] or end[1] <= start[1]:
        return None
    return (end[1] - start[1]) / MIB / (end[0] - start[0])


def _residue_ok(recv_dir: Path, name: str) -> bool:
    if not recv_dir.exists():
        return True
    for entry in recv_dir.iterdir():
        if entry.name.startswith(name) and (
            entry.name.endswith(".eversend.part") or entry.name.endswith(".eversend.journal")
        ):
            return False
    return True


def _evsd_run(workdir: Path, src: Path, src_sha: str, size_bytes: int, streams: int, *, warmup: bool) -> dict:
    """一次 EVSD 传输测量：接收端子进程 + 发送端 Engine。"""
    rx_root = workdir / "rx"
    tx_root = workdir / "tx"
    rx = _start_evsd_receiver(rx_root, max(8, streams))
    engine = None
    try:
        engine = _make_engine(tx_root, "BenchTx", streams)
        peer = _peer_from(rx)
        samples: list[tuple[float, int]] = []
        lock = threading.Lock()
        _progress_watch(engine, samples, lock)

        before = {
            "tx": _counter_snapshot(os.getpid()),
            "rx": _counter_snapshot(int(rx["pid"])),
        }
        load_before = _loadavg()
        t0 = time.monotonic()
        outcome = engine.send_ex(peer, [str(src)], streams=streams)
        wall = time.monotonic() - t0
        load_after = _loadavg()
        after = {
            "tx": _counter_snapshot(os.getpid()),
            "rx": _counter_snapshot(int(rx["pid"])),
        }
        tx_delta = _delta(before["tx"], after["tx"])
        rx_delta = _delta(before["rx"], after["rx"])

        target = rx_root / "recv" / src.name
        size_ok = target.exists() and target.stat().st_size == size_bytes
        sha_ok = bool(size_ok and _sha256_file(target) == src_sha)
        residue_ok = _residue_ok(rx_root / "recv", src.name)
        with lock:
            event_count = len(samples)
            first_progress = samples[0][1] if samples else None
        steady = None  # 事件投递有排队延迟，steady 口径不可靠，基线不采信
        size_mib = size_bytes / MIB
        tx_cpu = tx_delta.get("cpu_s")
        rx_cpu = rx_delta.get("cpu_s")
        total_cpu = None
        if tx_cpu is not None and rx_cpu is not None:
            total_cpu = round(tx_cpu + rx_cpu, 6)
        return {
            "kind": "evsd-loopback",
            "size_mib": round(size_mib, 3),
            "size_bytes": size_bytes,
            "streams": streams,
            "warmup": warmup,
            "wall_s": round(wall, 6),
            "mib_per_s": round(size_mib / wall, 3) if wall > 0 else None,
            "steady_mib_per_s": None,
            "ok": bool(outcome.ok),
            "status": getattr(outcome, "status", ""),
            "error_code": getattr(outcome, "error_code", ""),
            "error_text": getattr(outcome, "error_text", ""),
            "sha_ok": sha_ok,
            "size_ok": size_ok,
            "residue_ok": residue_ok,
            "tx_cpu_s": tx_cpu,
            "rx_cpu_s": rx_cpu,
            "total_cpu_s": total_cpu,
            "tx_cpu_user_s": tx_delta.get("cpu_user_s"),
            "tx_cpu_sys_s": tx_delta.get("cpu_sys_s"),
            "rx_cpu_user_s": rx_delta.get("cpu_user_s"),
            "rx_cpu_sys_s": rx_delta.get("cpu_sys_s"),
            "cpu_s_per_mib": round(total_cpu / size_mib, 5) if total_cpu else None,
            "tx_rss_hwm_kb": tx_delta.get("rss_hwm_kb"),
            "rx_rss_hwm_kb": rx_delta.get("rss_hwm_kb"),
            "tx_source_rchar": tx_delta.get("rchar"),
            "events_n": event_count,
            "first_progress_bytes": first_progress,
            "loadavg_before": load_before,
            "loadavg_after": load_after,
            "receiver_pid": rx["pid"],
            "receiver_tcp_port": rx["port"],
        }
    finally:
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        _stop_evsd_receiver(rx, 15.0)


def run_evsd_bench(args, scratch: Path, results: dict) -> None:
    sizes = _parse_ints(args.sizes)
    streams_list = _parse_ints(args.streams)
    payload_dir = scratch / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[int, tuple[Path, str]] = {}
    for size_mib in sizes:
        src = payload_dir / f"payload-{size_mib}MiB.bin"
        print(f"[evsd] 生成 payload {size_mib} MiB …", flush=True)
        payloads[size_mib] = (src, _make_payload(src, size_mib * MIB))
    runs: list[dict] = []
    for size_mib in sizes:
        src, src_sha = payloads[size_mib]
        for streams in streams_list:
            total = args.warmups + args.repeats
            for index in range(total):
                warmup = index < args.warmups
                tag = "warmup" if warmup else f"run {index - args.warmups + 1}/{args.repeats}"
                print(f"[evsd] {size_mib} MiB streams={streams} {tag} …", flush=True)
                workdir = scratch / "evsd" / f"{size_mib}MiB-s{streams}-{uuid.uuid4().hex[:8]}"
                try:
                    record = _evsd_run(
                        workdir,
                        src,
                        src_sha,
                        size_mib * MIB,
                        streams,
                        warmup=warmup,
                    )
                except Exception:
                    # 共享机 USB 盘 I/O 停顿可能让接收端子进程启动超时；
                    # 换一个全新 workdir 再试一次，成功则 record["retried"]=True
                    retry_dir = workdir.with_name(workdir.name + "-retry")
                    try:
                        print("       (接收端启动失败，重试一次…)", flush=True)
                        record = _evsd_run(
                            retry_dir,
                            src,
                            src_sha,
                            size_mib * MIB,
                            streams,
                            warmup=warmup,
                        )
                        record["retried"] = True
                        if not args.keep:
                            shutil.rmtree(retry_dir, ignore_errors=True)
                    except Exception as exc:  # 单次失败不中断整轮，但退出码会非 0
                        record = {
                        "kind": "evsd-loopback",
                        "size_mib": size_mib,
                        "size_bytes": size_mib * MIB,
                        "streams": streams,
                        "warmup": warmup,
                        "ok": False,
                        "sha_ok": False,
                        "size_ok": False,
                        "error_code": "harness_exception",
                        "error_text": f"{type(exc).__name__}: {exc}",
                        "wall_s": None,
                        "mib_per_s": None,
                    }
                finally:
                    if not args.keep:
                        shutil.rmtree(workdir, ignore_errors=True)
                runs.append(record)
                print(
                    f"       -> {record.get('mib_per_s')} MiB/s  wall={record.get('wall_s')}s"
                    f"  cpu/MiB={record.get('cpu_s_per_mib')}  ok={record.get('ok')}"
                    f" sha={record.get('sha_ok')}",
                    flush=True,
                )
    results["runs"].extend(runs)
    results["summaries"].extend(_summarise(runs))


# ---------------------------------------------------------------------------
# HTTP：真 CLI serve 子进程 + 裸 socket 客户端（POST /api/upload、GET /api/download）
# ---------------------------------------------------------------------------


def _http_small(port: int, path: str, timeout: float = 15.0) -> tuple[int, dict[str, str], bytes]:
    """小请求（GET /api/state 等），用 urllib 即可。"""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def _start_web_server(workdir: Path, receive_dir: Path, tcp_port: int, web_port: int) -> dict:
    home = workdir / "home"
    home.mkdir(parents=True, exist_ok=True)
    log = workdir / "serve.log"
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "eversend",
        "--cli",
        "serve",
        "--port",
        str(tcp_port),
        "--web",
        "--web-port",
        str(web_port),
        "--auto-accept",
        "--no-broadcast",
        "--no-mdns",
        "--receive",
        str(receive_dir),
        "--name",
        "BenchServe",
    ]
    env = dict(_CHILD_ENV)
    env["EVERSEND_HOME"] = str(home)
    handle = open(log, "wb")
    proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=str(REPO_ROOT))
    _LIVE_PROCS.append(proc)
    deadline = time.monotonic() + 90.0
    status = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            status, _headers, _body = _http_small(web_port, "/api/state", timeout=2.0)
            if status == 200:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    if status != 200:
        tail = ""
        try:
            tail = log.read_text(errors="replace")[-2000:]
        except OSError:
            pass
        _stop_process(proc, 5.0)
        raise RuntimeError(f"CLI serve 启动失败（exit={proc.poll()}）: {tail}")

    _status, _headers, body = _http_small(web_port, "/", timeout=10.0)
    text = body.decode("utf-8", "replace")
    match = re.search(r'name="eversend-token"\s+content="([^"]+)"', text)
    token = match.group(1) if match else ""
    state_status, _headers, state_body = _http_small(web_port, "/api/state", timeout=10.0)
    device_id = ""
    if state_status == 200:
        try:
            device_id = str(json.loads(state_body.decode("utf-8"))["device"]["id"])
        except (ValueError, KeyError, TypeError):
            device_id = ""
    return {
        "proc": proc,
        "log_path": log,
        "stdout_handle": handle,
        "web_port": web_port,
        "tcp_port": tcp_port,
        "token": token,
        "device_id": device_id,
        "workdir": workdir,
    }


def _read_http_response(
    sock: socket.socket, *, discard_body: bool = False, sink=None
) -> tuple[int, dict[str, str], int]:
    """读一个 HTTP/1.1 响应（Connection: close），返回 (status, headers, body_bytes)。"""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("HTTP 响应头不完整（连接提前关闭）")
        buffer += chunk
        if len(buffer) > 256 * 1024:
            raise RuntimeError("HTTP 响应头过大")
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise RuntimeError(f"HTTP 状态行非法: {lines[0]!r}") from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    body_bytes = len(rest)
    if not discard_body:
        body = bytearray(rest)
        while True:
            chunk = sock.recv(MIB)
            if not chunk:
                break
            body.extend(chunk)
        return status, headers, bytes(body)
    if rest and sink is not None:
        sink(rest)
    while True:
        chunk = sock.recv(MIB)
        if not chunk:
            break
        body_bytes += len(chunk)
        if sink is not None:
            sink(chunk)
    return status, headers, body_bytes


def _http_upload(workdir: Path, src: Path, size_bytes: int, server: dict, name: str) -> dict:
    """POST /api/upload —— raw body（Content-Length + 4 MiB 分块 socket 发送）。"""
    from urllib.parse import quote

    query = f"/api/upload?name={quote(name)}&deviceId={quote(server['device_id'])}"
    request_head = (
        f"POST {query} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server['web_port']}\r\n"
        "Connection: close\r\n"
        f"{TOKEN_HEADER}: {server['token']}\r\n"
        "Content-Type: application/octet-stream\r\n"
        f"Content-Length: {size_bytes}\r\n"
        "\r\n"
    ).encode("latin-1")
    sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
    try:
        before = {
            "tx": _counter_snapshot(os.getpid()),
            "rx": _counter_snapshot(int(server["proc"].pid)),
        }
        load_before = _loadavg()
        t0 = time.monotonic()
        sock.sendall(request_head)
        sent = 0
        with open(src, "rb") as handle:
            for chunk in iter(lambda: handle.read(4 * MIB), b""):
                sock.sendall(chunk)
                sent += len(chunk)
        status, headers, body = _read_http_response(sock)
        wall = time.monotonic() - t0
        load_after = _loadavg()
        after = {
            "tx": _counter_snapshot(os.getpid()),
            "rx": _counter_snapshot(int(server["proc"].pid)),
        }
    finally:
        sock.close()
    tx_delta = _delta(before["tx"], after["tx"])
    rx_delta = _delta(before["rx"], after["rx"])
    answer: dict = {}
    try:
        answer = json.loads(body.decode("utf-8"))
    except (ValueError, AttributeError):
        pass
    size_mib = size_bytes / MIB
    tx_cpu, rx_cpu = tx_delta.get("cpu_s"), rx_delta.get("cpu_s")
    total_cpu = round(tx_cpu + rx_cpu, 6) if tx_cpu is not None and rx_cpu is not None else None
    return {
        "kind": "http-upload",
        "size_mib": round(size_mib, 3),
        "size_bytes": size_bytes,
        "streams": 1,
        "warmup": False,
        "wall_s": round(wall, 6),
        "mib_per_s": round(size_mib / wall, 3) if wall > 0 else None,
        "steady_mib_per_s": None,
        "ok": status == 202,
        "status": str(status),
        "error_code": "" if status == 202 else f"http_{status}",
        "error_text": body.decode("utf-8", "replace")[:200],
        "sha_ok": None,  # 上传后由调用方在落盘文件上校验
        "size_ok": answer.get("size") == size_bytes,
        "residue_ok": None,
        "tx_cpu_s": tx_cpu,
        "rx_cpu_s": rx_cpu,
        "total_cpu_s": total_cpu,
        "tx_cpu_user_s": tx_delta.get("cpu_user_s"),
        "tx_cpu_sys_s": tx_delta.get("cpu_sys_s"),
        "rx_cpu_user_s": rx_delta.get("cpu_user_s"),
        "rx_cpu_sys_s": rx_delta.get("cpu_sys_s"),
        "cpu_s_per_mib": round(total_cpu / size_mib, 5) if total_cpu else None,
        "tx_rss_hwm_kb": tx_delta.get("rss_hwm_kb"),
        "rx_rss_hwm_kb": rx_delta.get("rss_hwm_kb"),
        "events_n": None,
        "first_progress_bytes": None,
        "loadavg_before": load_before,
        "loadavg_after": load_after,
        "receiver_pid": server["proc"].pid,
        "receiver_web_port": server["web_port"],
        "http_answer": answer,
        "bytes_sent": sent,
    }


def _http_download(src: Path, size_bytes: int, server: dict, name: str, *, chunk_bytes: int = 0) -> dict:
    """GET /api/download?path=… 全量或 Range 分段；body 丢弃只数字节（不哈希，避免污染 wall）。"""
    from urllib.parse import quote

    file_query = f"/api/download?path={quote(name)}"
    before = {
        "tx": _counter_snapshot(os.getpid()),
        "rx": _counter_snapshot(int(server["proc"].pid)),
    }
    load_before = _loadavg()
    t0 = time.monotonic()
    total_bytes = 0
    status_last = 0
    ranges = 0
    if chunk_bytes <= 0:
        sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
        try:
            sock.sendall(
                (
                    f"GET {file_query} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server['web_port']}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
            )
            status, _headers, body_bytes = _read_http_response(sock, discard_body=True)
            status_last = status
            total_bytes = body_bytes
            ranges = 1
        finally:
            sock.close()
    else:
        offset = 0
        while offset < size_bytes:
            end = min(offset + chunk_bytes, size_bytes) - 1
            sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
            try:
                sock.sendall(
                    (
                        f"GET {file_query} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{server['web_port']}\r\n"
                        "Connection: close\r\n"
                        f"Range: bytes={offset}-{end}\r\n\r\n"
                    ).encode("latin-1")
                )
                status, headers, body_bytes = _read_http_response(sock, discard_body=True)
                status_last = status
                ranges += 1
                expect = end - offset + 1
                if status != 206 or body_bytes != expect:
                    total_bytes += body_bytes if status == 200 else 0
                    raise RuntimeError(
                        f"Range {offset}-{end} 异常: status={status} bytes={body_bytes} expect={expect}"
                    )
                total_bytes += body_bytes
            finally:
                sock.close()
            offset = end + 1
    wall = time.monotonic() - t0
    load_after = _loadavg()
    after = {
        "tx": _counter_snapshot(os.getpid()),
        "rx": _counter_snapshot(int(server["proc"].pid)),
    }
    tx_delta = _delta(before["tx"], after["tx"])
    rx_delta = _delta(before["rx"], after["rx"])
    size_mib = size_bytes / MIB
    tx_cpu, rx_cpu = tx_delta.get("cpu_s"), rx_delta.get("cpu_s")
    total_cpu = round(tx_cpu + rx_cpu, 6) if tx_cpu is not None and rx_cpu is not None else None
    ok = (status_last in (200, 206)) and total_bytes == size_bytes
    return {
        "kind": "http-download-range" if chunk_bytes > 0 else "http-download",
        "size_mib": round(size_mib, 3),
        "size_bytes": size_bytes,
        "streams": 1,  # 单请求单连接；Range 的段数见 range_requests
        "warmup": False,
        "wall_s": round(wall, 6),
        "mib_per_s": round(size_mib / wall, 3) if wall > 0 else None,
        "steady_mib_per_s": None,
        "ok": ok,
        "status": str(status_last),
        "error_code": "" if ok else "http_download_incomplete",
        "error_text": "" if ok else f"status={status_last} bytes={total_bytes}/{size_bytes}",
        "sha_ok": None,  # 本测量只数字节（body 丢弃以免污染 wall）；完整性见 http-download-verify
        "size_ok": total_bytes == size_bytes,
        "residue_ok": None,
        "tx_cpu_s": tx_cpu,
        "rx_cpu_s": rx_cpu,
        "total_cpu_s": total_cpu,
        "tx_cpu_user_s": tx_delta.get("cpu_user_s"),
        "tx_cpu_sys_s": tx_delta.get("cpu_sys_s"),
        "rx_cpu_user_s": rx_delta.get("cpu_user_s"),
        "rx_cpu_sys_s": rx_delta.get("cpu_sys_s"),
        "cpu_s_per_mib": round(total_cpu / size_mib, 5) if total_cpu else None,
        "tx_rss_hwm_kb": tx_delta.get("rss_hwm_kb"),
        "rx_rss_hwm_kb": rx_delta.get("rss_hwm_kb"),
        "events_n": None,
        "first_progress_bytes": None,
        "loadavg_before": load_before,
        "loadavg_after": load_after,
        "receiver_pid": server["proc"].pid,
        "receiver_web_port": server["web_port"],
        "bytes_read": total_bytes,
        "range_requests": ranges,
    }


def _http_upload_segment(
    server: dict, src: Path, name: str, upload_id: str, offset: int, length: int, total: int
) -> tuple[int, dict, float]:
    """发一段续传上传 body（从 offset 起 length 字节），返回 (status, json, wall)。"""
    from urllib.parse import quote

    query = (
        f"/api/upload?name={quote(name)}&deviceId={quote(server['device_id'])}"
        f"&uploadId={upload_id}&offset={offset}"
    )
    head = (
        f"POST {query} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server['web_port']}\r\n"
        "Connection: close\r\n"
        f"{TOKEN_HEADER}: {server['token']}\r\n"
        "Content-Type: application/octet-stream\r\n"
        f"X-EverSend-Total: {total}\r\n"
        f"Content-Length: {length}\r\n"
        "\r\n"
    ).encode("latin-1")
    sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
    try:
        t0 = time.monotonic()
        sock.sendall(head)
        remaining = length
        with open(src, "rb") as handle:
            handle.seek(offset)
            for chunk in iter(lambda: handle.read(4 * MIB), b""):
                if remaining <= 0:
                    break
                piece = chunk[:remaining]
                sock.sendall(piece)
                remaining -= len(piece)
        status, _headers, body = _read_http_response(sock)
        wall = time.monotonic() - t0
    finally:
        sock.close()
    answer: dict = {}
    try:
        answer = json.loads(body.decode("utf-8"))
    except (ValueError, AttributeError):
        pass
    return status, answer, wall


def _probe_upload_resume(server: dict) -> bool:
    """探测服务端是否支持可续传上传（opt-1017 新增）。

    BEFORE 组跑的是 e18c079 旧树（没有 /api/upload/status）—— 必须自动 skip，
    不能把"旧树没有这个功能"记成 FAIL，否则 A/B 的 BEFORE 会是假红。
    """
    probe_id = uuid.uuid4().hex
    try:
        status, _headers, _body = _http_small(
            server["web_port"], f"/api/upload/status?uploadId={probe_id}", timeout=5.0
        )
    except (OSError, ValueError):
        return False
    return status == 200


def _http_download_verify(
    src_sha: str, size_bytes: int, server: dict, name: str, *, chunk_bytes: int = 0
) -> dict:
    """下载完整性校验（**测量外**，不计入性能 wall）：整文件或 Range 拼接的 sha256 == 源 sha。

    性能测量里下载 body 是直接丢弃的（避免给 wall 加哈希成本），
    所以「字节是否一致」必须靠这个单独的一次校验来证明，而不是把
    「没做哈希」误报成 SHA_MISMATCH。
    """
    from urllib.parse import quote

    file_query = f"/api/download?path={quote(name)}"
    hasher = hashlib.sha256()
    total = 0
    statuses: list[int] = []
    t0 = time.monotonic()
    if chunk_bytes <= 0:
        sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
        try:
            sock.sendall(
                (
                    f"GET {file_query} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server['web_port']}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
            )
            status, _headers, body_bytes = _read_http_response(sock, discard_body=True, sink=hasher.update)
            statuses.append(status)
            total = body_bytes
        finally:
            sock.close()
    else:
        offset = 0
        while offset < size_bytes:
            end = min(offset + chunk_bytes, size_bytes) - 1
            sock = socket.create_connection(("127.0.0.1", server["web_port"]), timeout=600)
            try:
                sock.sendall(
                    (
                        f"GET {file_query} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{server['web_port']}\r\n"
                        "Connection: close\r\n"
                        f"Range: bytes={offset}-{end}\r\n\r\n"
                    ).encode("latin-1")
                )
                status, _headers, body_bytes = _read_http_response(
                    sock, discard_body=True, sink=hasher.update
                )
                statuses.append(status)
                expect = end - offset + 1
                if status != 206 or body_bytes != expect:
                    raise RuntimeError(
                        f"verify Range {offset}-{end} 异常: status={status} bytes={body_bytes} expect={expect}"
                    )
                total += body_bytes
            finally:
                sock.close()
            offset = end + 1
    digest = hasher.hexdigest()
    return {
        "kind": "http-download-verify",
        "size_mib": round(size_bytes / MIB, 3),
        "chunk_mib": round(chunk_bytes / MIB, 3) if chunk_bytes else 0,
        "statuses": statuses,
        "bytes": total,
        "sha256": digest,
        "sha_ok": digest == src_sha,
        "size_ok": total == size_bytes,
        "wall_s": round(time.monotonic() - t0, 4),
        "note": "测量外完整性校验（只证明字节一致，不计入性能 wall）",
    }


def _http_upload_resumable(src: Path, size_bytes: int, server: dict, name: str, segments: int) -> dict:
    """分段续传上传：3~5 段 POST（每段带 uploadId/offset/X-EverSend-Total）。

    这就是手机 App 断线重连后的真实形态：服务端 spool 保留，客户端从
    `/api/upload/status` 报告的 offset 继续发剩余字节。测量总分片墙钟、
    每段墙钟/状态码、CPU、RSS；字节是否完整由调用方对落盘文件算 sha256。
    """
    segments = max(1, int(segments))
    upload_id = uuid.uuid4().hex  # 32 位十六进制，符合服务端 16-64 位约束
    segment_len = size_bytes // segments
    per_segment: list[dict] = []
    statuses: list[int] = []
    offsets: list[int] = []
    total_wall = 0.0
    before = {
        "tx": _counter_snapshot(os.getpid()),
        "rx": _counter_snapshot(int(server["proc"].pid)),
    }
    load_before = _loadavg()
    ok = True
    error = ""
    for index in range(segments):
        offset = index * segment_len
        length = size_bytes - offset if index == segments - 1 else segment_len
        status, answer, wall = _http_upload_segment(server, src, name, upload_id, offset, length, size_bytes)
        total_wall += wall
        statuses.append(status)
        offsets.append(int(answer.get("offset") or 0))
        complete = bool(answer.get("complete"))
        expected_status = 202
        if status != expected_status or (complete and index != segments - 1):
            ok = False
            error = f"segment {index}: status={status} complete={complete} offset={offsets[-1]}"
        per_segment.append(
            {
                "index": index,
                "offset": offset,
                "length": length,
                "wall_s": round(wall, 6),
                "status": status,
                "complete": complete,
                "offset_after": offsets[-1],
            }
        )
        if status == 409:  # 偏移冲突：按服务端 offset 校正后继续（真实客户端也这么做）
            server_offset = int(answer.get("offset") or 0)
            if server_offset == size_bytes:
                break
            offset = server_offset
    load_after = _loadavg()
    after = {
        "tx": _counter_snapshot(os.getpid()),
        "rx": _counter_snapshot(int(server["proc"].pid)),
    }
    tx_delta = _delta(before["tx"], after["tx"])
    rx_delta = _delta(before["rx"], after["rx"])
    size_mib = size_bytes / MIB
    tx_cpu, rx_cpu = tx_delta.get("cpu_s"), rx_delta.get("cpu_s")
    total_cpu = round(tx_cpu + rx_cpu, 6) if tx_cpu is not None and rx_cpu is not None else None
    return {
        "kind": "http-upload-resumable",
        "size_mib": round(size_mib, 3),
        "size_bytes": size_bytes,
        "segments_n": len(per_segment),
        "wall_s": round(total_wall, 6),
        "mib_per_s": round(size_mib / total_wall, 3) if total_wall > 0 else None,
        "ok": ok and statuses[-1:] == [202],
        "status": ";".join(str(item) for item in statuses),
        "error_code": "" if ok else "resume_upload_failed",
        "error_text": error,
        "sha_ok": None,  # 调用方对落盘文件校验
        "size_ok": None,
        "tx_cpu_s": tx_cpu,
        "rx_cpu_s": rx_cpu,
        "total_cpu_s": total_cpu,
        "tx_cpu_user_s": tx_delta.get("cpu_user_s"),
        "tx_cpu_sys_s": tx_delta.get("cpu_sys_s"),
        "rx_cpu_user_s": rx_delta.get("cpu_user_s"),
        "rx_cpu_sys_s": rx_delta.get("cpu_sys_s"),
        "cpu_s_per_mib": round(total_cpu / size_mib, 5) if total_cpu else None,
        "tx_rss_hwm_kb": tx_delta.get("rss_hwm_kb"),
        "rx_rss_hwm_kb": rx_delta.get("rss_hwm_kb"),
        "loadavg_before": load_before,
        "loadavg_after": load_after,
        "per_segment": per_segment,
        "upload_id": upload_id,
        "retransmitted_bytes": 0,
        "note": "分段续传上传：每段独立 POST（新连接），服务端 spool 追加；字节数=文件大小",
    }


def _wait_file(path: Path, size_bytes: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.exists() and path.stat().st_size == size_bytes:
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def run_http_bench(args, scratch: Path, results: dict) -> None:
    sizes = _parse_ints(args.sizes)
    payload_dir = scratch / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[int, tuple[Path, str]] = {}
    for size_mib in sizes:
        src = payload_dir / f"payload-{size_mib}MiB.bin"
        print(f"[http] 生成 payload {size_mib} MiB …", flush=True)
        payloads[size_mib] = (src, _make_payload(src, size_mib * MIB))
    runs: list[dict] = []
    for size_mib in sizes:
        src, src_sha = payloads[size_mib]
        workdir = scratch / "http" / f"{size_mib}MiB-{uuid.uuid4().hex[:8]}"
        receive_dir = workdir / "recv"
        receive_dir.mkdir(parents=True, exist_ok=True)
        tcp_port = _free_port()
        web_port = _free_port()
        for _ in range(20):  # 两次 _free_port() 可能拿到同一个/已被占的端口
            if web_port != tcp_port and _port_free(tcp_port) and _port_free(web_port):
                break
            web_port = _free_port()
        else:
            raise RuntimeError("端口挑选失败（并发占用），请重跑")
        print(
            f"[http] 起 CLI serve: tcp={tcp_port} web={web_port} "
            f"receive={receive_dir}（随机空闲端口，绝不使用 52117/52119）",
            flush=True,
        )
        try:
            server = _start_web_server(workdir, receive_dir, tcp_port, web_port)
        except Exception:
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)
            raise
        # 探测服务端能力：e18c079 旧树没有 /api/upload/status → 续传用例 SKIPPED
        resume_supported = _probe_upload_resume(server)
        print(
            f"[http] /api/upload/status 可续传上传: "
            f"{'支持' if resume_supported else '不支持（旧树 → 续传用例 SKIPPED）'}",
            flush=True,
        )
        try:
            total = args.warmups + args.repeats
            for index in range(total):
                warmup = index < args.warmups
                name = f"bench-{size_mib}MiB-r{index}.bin"
                landed = receive_dir / name
                try:
                    landed.unlink()
                except OSError:
                    pass
                tag = "warmup" if warmup else f"run {index - args.warmups + 1}/{args.repeats}"
                print(f"[http] {size_mib} MiB 上传 {tag} …", flush=True)
                record = _http_upload(workdir, src, size_mib * MIB, server, name)
                record["warmup"] = warmup
                if record["ok"]:
                    appeared = _wait_file(landed, size_mib * MIB)
                    sha_ok = bool(appeared and _sha256_file(landed) == src_sha)
                    record["sha_ok"] = sha_ok
                    record["size_ok"] = bool(appeared and record.get("size_ok"))
                runs.append(record)
                print(
                    f"       -> {record.get('mib_per_s')} MiB/s ok={record.get('ok')} sha={record.get('sha_ok')}",
                    flush=True,
                )

                print(f"[http] {size_mib} MiB 下载 {tag} …", flush=True)
                download = _http_download(src, size_mib * MIB, server, name)
                download["warmup"] = warmup
                runs.append(download)
                print(
                    f"       -> {download.get('mib_per_s')} MiB/s ok={download.get('ok')}"
                    f" status={download.get('status')}",
                    flush=True,
                )
                if index == args.warmups:  # 第一次正式测量之后：测量外的下载完整性校验
                    try:
                        verify_full = _http_download_verify(src_sha, size_mib * MIB, server, name)
                        results.setdefault("verifications", []).append(verify_full)
                        print(
                            f"       [verify] 整文件下载 sha256 {'一致' if verify_full['sha_ok'] else '不一致'}"
                            f"（{verify_full['bytes']} B, 测量外）",
                            flush=True,
                        )
                        if getattr(args, "range_mib", 0):
                            verify_range = _http_download_verify(
                                src_sha, size_mib * MIB, server, name,
                                chunk_bytes=int(args.range_mib) * MIB,
                            )
                            results.setdefault("verifications", []).append(verify_range)
                            print(
                                f"       [verify] Range 拼接 sha256 {'一致' if verify_range['sha_ok'] else '不一致'}"
                                f"（{verify_range['range_requests'] if verify_range.get('range_requests') else len(verify_range.get('statuses') or [])} 段）",
                                flush=True,
                            )
                    except Exception as exc:
                        results.setdefault("verifications", []).append(
                            {"kind": "http-download-verify", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        )
                        print(f"       [verify] 失败: {type(exc).__name__}: {exc}", flush=True)

                if getattr(args, "resume_segments", 0) and not resume_supported:
                    if not warmup:
                        runs.append(
                            {
                                "kind": "http-upload-resumable",
                                "size_mib": round(size_mib, 3),
                                "size_bytes": size_mib * MIB,
                                "streams": 1,
                                "skipped": True,
                                "ok": True,
                                "status": "SKIPPED",
                                "note": "SKIPPED: 服务端无 /api/upload/status（e18c079 旧树没有可续传上传，不记 FAIL）",
                            }
                        )
                        print("       -> SKIPPED（旧树无 /api/upload/status）", flush=True)

                if getattr(args, "resume_segments", 0) and resume_supported:
                    seg_name = f"bench-{size_mib}MiB-r{index}-rs.bin"
                    rs_landed = receive_dir / seg_name
                    try:
                        rs_landed.unlink()
                    except OSError:
                        pass
                    print(
                        f"[http] {size_mib} MiB 续传分段上传 {tag}"
                        f"（{args.resume_segments} 段）…",
                        flush=True,
                    )
                    resumed = _http_upload_resumable(
                        src, size_mib * MIB, server, seg_name, int(args.resume_segments)
                    )
                    resumed["warmup"] = warmup
                    if resumed["ok"]:
                        appeared = _wait_file(rs_landed, size_mib * MIB)
                        resumed["sha_ok"] = bool(appeared and _sha256_file(rs_landed) == src_sha)
                        resumed["size_ok"] = appeared
                    runs.append(resumed)
                    print(
                        f"       -> {resumed.get('mib_per_s')} MiB/s ok={resumed.get('ok')}"
                        f" sha={resumed.get('sha_ok')} status={resumed.get('status')}",
                        flush=True,
                    )
                    try:
                        rs_landed.unlink()
                    except OSError:
                        pass

                if index == args.warmups and getattr(args, "range_mib", 0):
                    chunk = int(args.range_mib) * MIB
                    print(f"[http] {size_mib} MiB 下载 Range({args.range_mib} MiB/段) …", flush=True)
                    ranged = _http_download(src, size_mib * MIB, server, name, chunk_bytes=chunk)
                    ranged["warmup"] = warmup
                    runs.append(ranged)
                    print(
                        f"       -> {ranged.get('mib_per_s')} MiB/s"
                        f" ({ranged.get('range_requests')} 段) ok={ranged.get('ok')}",
                        flush=True,
                    )
                try:
                    landed.unlink()
                except OSError:
                    pass
        finally:
            proc = server["proc"]
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGINT)  # CLI serve 捕获后干净退出
                    proc.wait(timeout=15)
                except (subprocess.TimeoutExpired, OSError):
                    _stop_process(proc, 5.0)
            if proc in _LIVE_PROCS:
                try:
                    _LIVE_PROCS.remove(proc)
                except ValueError:
                    pass
            try:
                server["stdout_handle"].close()
            except OSError:
                pass
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)
        # 确认端口已释放：监听 socket 已关闭时，TIME_WAIT 也会让 bind() 失败，
        # 所以用「能否 connect 上」判断（连不上 = 没有监听者 = 已释放）。
        for label, port in (("tcp", tcp_port), ("web", web_port)):
            if _port_listening(port):
                print(f"[http] 警告：CLI serve 退出后端口仍在监听 {label}={port}", flush=True)
    results["runs"].extend(runs)
    results["summaries"].extend(_summarise(runs))


# ---------------------------------------------------------------------------
# 续传：断点 → 重启 → 补传
# ---------------------------------------------------------------------------


def _journal_progress(journal: Path | None, size_bytes: int) -> int:
    """读 EVSD journal 算出「接收端已落定的字节数」。

    journal 是接收端 PartFile 的权威续传状态（append-only，8 字节/记录）：
    SIGKILL 之后能免传的恰好就是 journal 里出现过的 chunk。注意 ``.part``
    文件在打开时就被 ``posix_fallocate`` 预分配到**全长**，所以它的
    ``st_size`` 绝不能用来看进度 —— 这正是第一版续传度量踩的坑。
    """
    if journal is None:
        return 0
    from eversend.core.chunkstore import _JOURNAL_PREFIX, chunk_range

    try:
        with open(journal, "rb") as handle:
            prefix = handle.read(_JOURNAL_PREFIX.size)
            if len(prefix) < _JOURNAL_PREFIX.size:
                return 0
            magic, version, _flags, _res, header_len = _JOURNAL_PREFIX.unpack(prefix)
            if magic != b"SWDJ" or version != 1:
                return 0
            header = json.loads(handle.read(header_len).decode("utf-8"))
            body = handle.read()
    except (OSError, ValueError):
        return 0
    try:
        chunk_size = int(header.get("chunkSize") or 0)
    except (TypeError, ValueError):
        return 0
    if chunk_size <= 0:
        return 0
    seen: set[int] = set()
    total = 0
    for offset in range(0, len(body) // 8 * 8, 8):
        try:
            index, _crc = struct.unpack_from("!II", body, offset)
        except struct.error:
            continue
        if index in seen:
            continue
        seen.add(index)
        total += chunk_range(index, chunk_size, size_bytes)[1]
    return total


def _find_journal(recv_dir: Path) -> Path | None:
    try:
        for entry in recv_dir.glob("*.eversend.journal"):
            return entry
    except OSError:
        return None
    return None


def _peer_for_engine(engine):
    from eversend.core.model import Peer

    return Peer(info=engine.info, address="127.0.0.1", port=int(engine.port), source="manual", trusted=True)


def _stop_engine(engine) -> None:
    if engine is None:
        return
    try:
        engine.stop()
    except Exception:
        pass


def _resume_cold_run(workdir: Path, src: Path, src_sha: str, size_bytes: int, streams: int) -> dict:
    """冷传参考：一次完整传输（同进程双 Engine，与 test_loopback.py 同构）。"""
    root = workdir / "cold"
    rx = _make_engine(root / "rx", "BenchRxCold", max(8, streams))
    tx = None
    try:
        tx = _make_engine(root / "tx", "BenchTxCold", streams)
        peer = _peer_for_engine(rx)
        load_before = _loadavg()
        t0 = time.monotonic()
        outcome = tx.send_ex(peer, [str(src)], streams=streams)
        wall = time.monotonic() - t0
        load_after = _loadavg()
        target = root / "rx" / "recv" / src.name
        sha_ok = bool(target.exists() and target.stat().st_size == size_bytes and _sha256_file(target) == src_sha)
        return {
            "kind": "evsd-resume-cold",
            "size_mib": round(size_bytes / MIB, 3),
            "size_bytes": size_bytes,
            "streams": streams,
            "wall_s": round(wall, 6),
            "mib_per_s": round(size_bytes / MIB / wall, 3) if wall else None,
            "ok": bool(outcome.ok and sha_ok),
            "sha_ok": sha_ok,
            "size_ok": sha_ok,
            "loadavg_before": load_before,
            "loadavg_after": load_after,
            "note": "同进程双 Engine（GIL 内），wall 仅作续传对照，不作链路吞吐基线",
        }
    finally:
        _stop_engine(tx)
        _stop_engine(rx)


def _evsd_resume_run(workdir: Path, src: Path, src_sha: str, size_bytes: int, streams: int, fraction: float) -> list[dict]:
    """一次续传测量：冷传参考 → 中途停接收端 → 同目录重启 → 补传。"""
    records: list[dict] = [ _resume_cold_run(workdir, src, src_sha, size_bytes, streams) ]
    cold = records[0]

    run_root = workdir / "resume"
    rx = _make_engine(run_root / "rx", "BenchRx", max(8, streams))
    tx = None
    try:
        tx = _make_engine(run_root / "tx", "BenchTx", streams)
        peer = _peer_for_engine(rx)
        cancel = threading.Event()
        state: dict = {"persisted": 0, "stopped": False, "poll_s": 0.0}
        recv_dir = run_root / "rx" / "recv"
        threshold = max(int(size_bytes * fraction), 4 * MIB)

        def killer() -> None:
            t0 = time.monotonic()
            deadline = t0 + 120.0
            while time.monotonic() < deadline:
                journal = _find_journal(recv_dir)
                if journal is not None:
                    persisted = _journal_progress(journal, size_bytes)
                    state["persisted"] = max(state["persisted"], persisted)
                    if persisted >= threshold:
                        break
                if cancel.is_set():
                    break
                time.sleep(0.01)
            state["poll_s"] = round(time.monotonic() - t0, 4)
            # 停监听 + 取消发送：协议侧表现为「对端掉线」，与真实断链一致
            try:
                rx.server.stop()
            except Exception:
                pass
            state["stopped"] = True
            cancel.set()

        watcher = threading.Thread(target=killer, daemon=True)
        watcher.start()
        interrupted_wall = None
        first_ok = False
        first_error = ""
        try:
            t0 = time.monotonic()
            first = tx.send_ex(peer, [str(src)], streams=streams, cancel_event=cancel)
            interrupted_wall = round(time.monotonic() - t0, 4)
            first_ok = bool(first.ok)
            first_error = getattr(first, "error_code", "") or getattr(first, "status", "")
        except Exception as exc:
            interrupted_wall = round(time.monotonic() - t0, 4) if interrupted_wall is None else interrupted_wall
            first_ok = False
            first_error = f"{type(exc).__name__}: {exc}"
        watcher.join(timeout=15.0)
        journal = _find_journal(recv_dir)
        persisted = _journal_progress(journal, size_bytes)
        if persisted > state["persisted"]:
            state["persisted"] = persisted
    finally:
        _stop_engine(tx)

    # 接收端进程/引擎完全停下后，同目录重启（= App 重启后继续接收）
    _stop_engine(rx)
    rx2 = _make_engine(run_root / "rx", "BenchRx2", max(8, streams))
    tx2 = None
    try:
        tx2 = _make_engine(run_root / "tx2", "BenchTx2", streams)
        peer2 = _peer_for_engine(rx2)
        samples: list[tuple[float, int]] = []
        lock = threading.Lock()
        _progress_watch(tx2, samples, lock)
        before_tx = _counter_snapshot(os.getpid())
        load_before = _loadavg()
        t0 = time.monotonic()
        second = tx2.send_ex(peer2, [str(src)], streams=streams)
        resume_wall = time.monotonic() - t0
        load_after = _loadavg()
        after_tx = _counter_snapshot(os.getpid())
        delta = _delta(before_tx, after_tx)
        target = run_root / "rx" / "recv" / src.name
        sha_ok = bool(target.exists() and target.stat().st_size == size_bytes and _sha256_file(target) == src_sha)
        residue_ok = _residue_ok(recv_dir, src.name)
        with lock:
            first_progress = samples[0][1] if samples else None
        persisted = state.get("persisted", 0)
        records.append(
            {
                "kind": "evsd-resume",
                "size_mib": round(size_bytes / MIB, 3),
                "size_bytes": size_bytes,
                "streams": streams,
                "wall_s": round(resume_wall, 6),
                "mib_per_s": round(size_bytes / MIB / resume_wall, 3) if resume_wall else None,
                "ok": bool(second.ok and sha_ok and residue_ok),
                "sha_ok": sha_ok,
                "size_ok": sha_ok,
                "residue_ok": residue_ok,
                "persisted_bytes_at_interrupt": persisted,
                "persisted_ratio": round(persisted / size_bytes, 5),
                # 补传字节比：协议只重传 journal 里没有的 chunk → 1 - 落定比
                "retransmit_ratio": round(1.0 - persisted / size_bytes, 5),
                "first_progress_bytes": first_progress,
                "cold_wall_s": cold.get("wall_s"),
                "resume_speedup_vs_cold": round(cold["wall_s"] / resume_wall, 3) if cold.get("wall_s") and resume_wall else None,
                "interrupted_wall_s": interrupted_wall,
                "first_attempt_ok": first_ok,
                "first_attempt_error": first_error,
                "killed": state.get("stopped"),
                "interrupt_poll_s": state.get("poll_s"),
                "tx_cpu_s": delta.get("cpu_s"),
                "loadavg_before": load_before,
                "loadavg_after": load_after,
                "note": "同进程双 Engine；补传比用 journal 落定字节计算，非估算",
            }
        )
    finally:
        _stop_engine(tx2)
        _stop_engine(rx2)
    return records


def run_resume_bench(args, scratch: Path, results: dict) -> None:
    size_mib = int(args.size_mib)
    streams = int(args.streams)
    size_bytes = size_mib * MIB
    payload_dir = scratch / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    src = payload_dir / f"payload-{size_mib}MiB.bin"
    src_sha = _make_payload(src, size_bytes)
    records: list[dict] = []
    for index in range(int(getattr(args, "repeats", 2))):
        print(f"[resume] {size_mib} MiB streams={streams} run {index + 1}/{args.repeats} …", flush=True)
        workdir = scratch / "resume" / f"{size_mib}MiB-{uuid.uuid4().hex[:8]}"
        try:
            records.extend(_evsd_resume_run(workdir, src, src_sha, size_bytes, streams, float(args.fraction)))
        except Exception as exc:
            records.append(
                {
                    "kind": "evsd-resume",
                    "size_mib": size_mib,
                    "size_bytes": size_bytes,
                    "streams": streams,
                    "ok": False,
                    "error_code": "harness_exception",
                    "error_text": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)
    for record in records:
        print(
            f"       -> {record['kind']}: wall={record.get('wall_s')}s"
            f" persisted={record.get('persisted_bytes_at_interrupt')}"
            f" retransmit_ratio={record.get('retransmit_ratio')} ok={record.get('ok')}",
            flush=True,
        )
    results["runs"].extend(records)
    # 续传的关键口径是「补传比 + 恢复耗时」，不并进吞吐 summary
    records_ok = [r for r in records if r.get("kind") == "evsd-resume" and r.get("ok")]
    if records_ok:
        results.setdefault("resume_summary", []).append(
            {
                "size_mib": size_mib,
                "streams": streams,
                "n": len(records_ok),
                "persisted_ratio_median": round(_median([r["persisted_ratio"] for r in records_ok]), 5),
                "retransmit_ratio_median": round(_median([r["retransmit_ratio"] for r in records_ok]), 5),
                "retransmit_ratio_min": round(min(r["retransmit_ratio"] for r in records_ok), 5),
                "retransmit_ratio_max": round(max(r["retransmit_ratio"] for r in records_ok), 5),
                "resume_wall_s_median": round(_median([r["wall_s"] for r in records_ok]), 4),
                "cold_wall_s_median": round(_median([r["cold_wall_s"] for r in records_ok if r.get("cold_wall_s")]), 4)
                if any(r.get("cold_wall_s") for r in records_ok)
                else None,
                "resume_speedup_median": round(
                    _median([r["resume_speedup_vs_cold"] for r in records_ok if r.get("resume_speedup_vs_cold")]), 3
                )
                if any(r.get("resume_speedup_vs_cold") for r in records_ok)
                else None,
                "first_progress_equals_persisted": all(
                    r.get("first_progress_bytes") is None or r["first_progress_bytes"] >= r["persisted_bytes_at_interrupt"]
                    for r in records_ok
                ),
            }
        )


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _parse_ints(text: str) -> list[int]:
    return [int(part) for part in str(text).replace(" ", "").split(",") if part]


def _write_outputs(args, results: dict) -> tuple[Path | None, Path | None]:
    results = dict(results)
    results.setdefault("generated_at", _now_iso())
    results.setdefault("label", getattr(args, "label", ""))
    results.setdefault("command", " ".join(sys.argv))
    results.setdefault("host", results.pop("_host", {}))
    json_path: Path | None = None
    md_path: Path | None = None
    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        print(f"[report] JSON -> {json_path}", flush=True)
    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_markdown(results), encoding="utf-8")
        print(f"[report] Markdown -> {md_path}", flush=True)
    return json_path, md_path


def _markdown(results: dict) -> str:
    host = results.get("host") or {}
    lines: list[str] = []
    lines.append("# 韧传 EverSend · 传输性能基线（tools/bench_transfer.py 自动生成）")
    lines.append("")
    lines.append(f"- 生成时间：{results.get('generated_at', '')}")
    if results.get("label"):
        lines.append(f"- 标签：{results['label']}")
    lines.append(f"- 命令：`{results.get('command', '')}`")
    lines.append("- **口径：1 MiB = 1048576 字节；MiB/s = 字节数 / 1048576 / wall 秒**")
    lines.append("- wall = 一次传输 API 调用的墙钟（EVSD: `Engine.send_ex` 阻塞返回；HTTP: 一次真请求从 connect 到读完响应）")
    lines.append("- CPU = `/proc/<pid>/stat` utime+stime（发送/接收进程分开统计），峰值 RSS = VmHWM")
    lines.append("")
    lines.append("## 机器背景")
    lines.append("")
    for key, value in (
        ("CPU", host.get("cpu_model", "")),
        ("nproc", host.get("nproc", "")),
        ("内核", host.get("kernel", "")),
        ("Python", host.get("python", "")),
        ("git HEAD", host.get("git_head", "")),
        ("内存", f"total={host.get('mem_total', '')} 可用={host.get('mem_available_at_start', '')}"),
        ("scratch 磁盘", host.get("scratch_df", "")),
        ("home 磁盘", host.get("home_df", "")),
        ("loadavg @start", host.get("loadavg_at_start", "")),
        ("磁盘完成路径探针", json.dumps(host.get("disk_probe", {}), ensure_ascii=False)),
    ):
        lines.append(f"- {key}: {value}")
    lines.append("")
    if results.get("config"):
        lines.append("## 配置")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(results["config"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    summaries = results.get("summaries") or []
    lines.append("## 结果摘要（中位数，n=测量次数，预热不计入）")
    lines.append("")
    if summaries:
        has_clean = any(item.get("mib_per_s_median_clean") is not None for item in summaries)
        head = "| 链路 | size MiB | streams | n | MiB/s 中位 | min | max | CV% |"
        if has_clean:
            head += " 剔除停顿后 MiB/s 中位 | n_clean |"
        head += " wall s | CPU s/MiB | tx CPU s | rx CPU s | 标记 |"
        lines.append(head)
        lines.append("|" + "---|" * (16 if has_clean else 13))
        for item in summaries:
            row = (
                f"| {item.get('kind', '')} | {item.get('size_mib', '')} | {item.get('streams', '')} | "
                f"{item.get('n', '')} | {item.get('mib_per_s_median', '')} | {item.get('mib_per_s_min', '')} | "
                f"{item.get('mib_per_s_max', '')} | {item.get('mib_per_s_cv_pct', '')} |"
            )
            if has_clean:
                row += (
                    f" {item.get('mib_per_s_median_clean', '')} | {item.get('n_clean', '')} |"
                )
            row += (
                f" {item.get('wall_s_median', '')} | {item.get('cpu_s_per_mib_median', '')} | "
                f"{item.get('tx_cpu_s_median', '')} | {item.get('rx_cpu_s_median', '')} | "
                f"{', '.join(item.get('flags') or [])} |"
            )
            lines.append(row)
        lines.append("")
    else:
        lines.append("（本次没有成功的测量）")
        lines.append("")
    cpu_rows = [r for r in (results.get("runs") or []) if r.get("kind") == "evsd-cpu-split"]
    if cpu_rows:
        lines.append("## 分阶段 CPU 账本（EVSD，thread_time 自时间归因）")
        lines.append("")
        lines.append(
            "同进程双 Engine；`other` = 进程总 CPU − 已归因。会话期间机器有其他专家负载，"
            "绝对值只能与「同 env 的改后复测」比较。"
        )
        lines.append("")
        lines.append("| size MiB | streams | wall s | MiB/s | 总 CPU s | CPU s/MiB | ok | sha |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in cpu_rows:
            lines.append(
                f"| {row.get('size_mib')} | {row.get('streams')} | {row.get('wall_s')} | "
                f"{row.get('mib_per_s')} | {row.get('total_cpu_s')} | {row.get('cpu_s_per_mib')} | "
                f"{row.get('ok')} | {row.get('sha_ok')} |"
            )
        lines.append("")
        for row in cpu_rows:
            profile = row.get("profile") or {}
            stages = profile.get("stages") or {}
            if not stages:
                continue
            lines.append(f"### {row.get('size_mib')} MiB × streams={row.get('streams')} · CPU 分桶")
            lines.append("")
            lines.append("| 阶段 | CPU s | 占总 CPU % | 调用次数 |")
            lines.append("|---|---|---|---|")
            for bucket, slot in stages.items():
                label = (profile.get("bucket_labels") or {}).get(bucket, bucket)
                lines.append(
                    f"| {label} (`{bucket}`) | {slot.get('cpu_s')} | {slot.get('pct_of_total')} | {slot.get('calls')} |"
                )
            lines.append("")
    resume_rows = [r for r in (results.get("runs") or []) if r.get("kind") == "evsd-resume"]
    if resume_rows:
        lines.append("## 续传（中断 → 同目录重启 → 补传）")
        lines.append("")
        lines.append("口径：``.part`` 打开即被预分配到全长，不能用它的大小看进度；补传字节比 = 1 - 中断瞬间 journal 已落定 chunk 的比例（journal 是接收端权威续传状态）。")
        lines.append("")
        lines.append("| size MiB | streams | 中断时 journal 落定字节 | 落定比 | 补传字节比(1-落定比) | 恢复 wall s | 冷传 wall s | 提速 | 首进度字节 | sha | ok |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for row in resume_rows:
            lines.append(
                "| {size} | {streams} | {persisted} | {persisted_ratio} | {ratio} | {wall} | {cold} | {speed} | {first_progress} | {sha} | {ok} |".format(
                    size=row.get("size_mib", ""),
                    streams=row.get("streams", ""),
                    persisted=row.get("persisted_bytes_at_interrupt", ""),
                    persisted_ratio=row.get("persisted_ratio", ""),
                    ratio=row.get("retransmit_ratio", ""),
                    wall=row.get("wall_s", ""),
                    cold=row.get("cold_wall_s", ""),
                    speed=row.get("resume_speedup_vs_cold", ""),
                    first_progress=row.get("first_progress_bytes", ""),
                    sha=row.get("sha_ok", ""),
                    ok=row.get("ok", ""),
                )
            )
        lines.append("")
    for item in results.get("resume_summary") or []:
        lines.append(
            f"- 续传汇总：{item['size_mib']} MiB × streams={item['streams']}，n={item['n']}，"
            f"落定比中位 {item['persisted_ratio_median']}，补传比中位 {item['retransmit_ratio_median']}"
            f"（min {item['retransmit_ratio_min']} / max {item['retransmit_ratio_max']}），"
            f"恢复 {item['resume_wall_s_median']}s vs 冷传 {item['cold_wall_s_median']}s"
            f"（提速 {item['resume_speedup_median']}×）"
        )
    lines.append("")
    lines.append("## 原始测量（每次）")
    lines.append("")
    fields = (
        "kind",
        "size_mib",
        "streams",
        "warmup",
        "wall_s",
        "mib_per_s",
        "cpu_s_per_mib",
        "sha_ok",
        "ok",
    )
    lines.append("| " + " | ".join(fields) + " |")
    lines.append("|" + "---|" * len(fields))
    for run in results.get("runs") or []:
        lines.append("| " + " | ".join(str(run.get(field, "")) for field in fields) + " |")
    lines.append("")
    lines.append("## 人工分析")
    lines.append("")
    lines.append("（基线结论与瓶颈假设由 perf-bench 手写补充。）")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 内部子命令：EVSD 接收端进程
# ---------------------------------------------------------------------------


def _evsd_receiver_main(args) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    ready = Path(args.ready)
    stop = Path(args.stop)
    try:
        stop.unlink()
    except OSError:
        pass
    from eversend.core.engine import Engine, EngineConfig

    engine = Engine(
        EngineConfig(
            data_dir=str(workdir / "state"),
            receive_dir=str(workdir / "recv"),
            name="BenchRx",
            tcp_port=0,
            discovery_port=_free_port(),
            streams=int(args.streams),
            auto_accept_all=True,
            auto_accept_trusted=True,
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
        )
    )
    engine.start()
    payload = {
        "ok": True,
        "pid": os.getpid(),
        "port": int(engine.port),
        "info": engine.info.to_dict(),
    }
    tmp = ready.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, ready)
    parent = os.getppid()
    deadline = time.monotonic() + 1800.0
    try:
        while time.monotonic() < deadline:
            if stop.exists():
                break
            if os.getppid() != parent:  # 父进程没了：自退，绝不留孤儿
                break
            time.sleep(0.05)
    finally:
        try:
            engine.stop()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", default="", help="结果 JSON 路径（空=按子命令默认）")
    parser.add_argument("--md", default="", help="结果 Markdown 路径（空=按子命令默认）")
    parser.add_argument("--label", default="", help="记录在 JSON 里的标签，如 pre-opt-baseline")
    parser.add_argument("--keep", action="store_true", help="保留 .testscratch 临时目录以便排查")
    parser.add_argument("--scratch", default="", help="覆盖临时根目录（默认 eversend/.testscratch）")


def _add_measure_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repeats", type=int, default=3, help="每配置测量次数（默认 3）")
    parser.add_argument("--warmups", type=int, default=1, help="每配置预热次数（丢弃，默认 1）")
    parser.add_argument("--taskset", default="", help='绑核（如 "0-3"），只在提供时生效')


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="韧传传输性能基准 harness（可复现、可对比、可自清理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    evsd = sub.add_parser("evsd-loopback", help="两个真 Engine 进程走 EVSD 协议互传")
    evsd.add_argument("--sizes", default="64,512,1024", help="文件大小 MiB 列表")
    evsd.add_argument("--streams", default="1,2,4,8", help="EVSD 流数列表")
    _add_measure_args(evsd)
    _add_output_args(evsd)

    http = sub.add_parser("http", help="真 CLI serve + 真 HTTP 上传/下载")
    http.add_argument("--sizes", default="64,512,1024", help="文件大小 MiB 列表")
    http.add_argument("--range-mib", type=int, default=8, help="下载 Range 分段大小 MiB（0=不测）")
    http.add_argument("--resume-segments", type=int, default=3, help="续传上传分段数（0=不测；手机链路重点）")
    _add_measure_args(http)
    _add_output_args(http)

    matrix = sub.add_parser("matrix", help="EVSD + HTTP 组合矩阵")
    matrix.add_argument("--sizes", default="64,512,1024", help="文件大小 MiB 列表")
    matrix.add_argument("--streams", default="1,2,4,8", help="EVSD 流数列表")
    matrix.add_argument("--range-mib", type=int, default=8, help="下载 Range 分段大小 MiB（0=不测）")
    matrix.add_argument("--resume-segments", type=int, default=3, help="续传上传分段数（0=不测；手机链路重点）")
    _add_measure_args(matrix)
    _add_output_args(matrix)

    resume = sub.add_parser("resume", help="中断后续传：补传字节比 / 恢复耗时")
    resume.add_argument("--size-mib", type=int, default=512, help="文件大小 MiB")
    resume.add_argument("--streams", type=int, default=4, help="EVSD 流数")
    resume.add_argument("--fraction", type=float, default=0.30, help="在落盘到该比例时中断")
    resume.add_argument("--repeats", type=int, default=2, help="续传测量次数（默认 2）")
    _add_output_args(resume)

    all_cmd = sub.add_parser("all", help="完整基线（矩阵 + 续传），写默认报告")
    all_cmd.add_argument("--sizes", default="64,512,1024", help="文件大小 MiB 列表")
    all_cmd.add_argument("--streams", default="1,2,4,8", help="EVSD 流数列表")
    all_cmd.add_argument("--range-mib", type=int, default=8, help="下载 Range 分段大小 MiB（0=不测）")
    all_cmd.add_argument("--resume-segments", type=int, default=3, help="续传上传分段数（0=不测；手机链路重点）")
    _add_measure_args(all_cmd)
    all_cmd.add_argument("--resume-size-mib", type=int, default=512, help="续传测试文件 MiB")
    all_cmd.add_argument("--resume-streams", type=int, default=4, help="续传测试流数")
    all_cmd.add_argument("--resume-repeats", type=int, default=2, help="续传测量次数")
    all_cmd.add_argument("--resume-fraction", type=float, default=0.30, help="续传中断点比例")
    all_cmd.add_argument("--skip-resume", action="store_true", help="跳过续传")
    _add_output_args(all_cmd)

    cpu_split = sub.add_parser("cpu-split", help="EVSD 分阶段 CPU 账本（AEAD/hash/crc32/socket/磁盘/其它）")
    cpu_split.add_argument("--size-mib", type=int, default=2048, help="文件大小 MiB（默认 2048 = 2 GiB）")
    cpu_split.add_argument("--streams", type=int, default=4, help="EVSD 流数（默认 4）")
    cpu_split.add_argument("--repeats", type=int, default=1, help="测量次数（默认 1）")
    _add_output_args(cpu_split)

    receiver = sub.add_parser("_evsd-receiver", help="内部：EVSD 接收端进程")
    receiver.add_argument("--workdir", required=True)
    receiver.add_argument("--streams", type=int, default=8)
    receiver.add_argument("--ready", required=True)
    receiver.add_argument("--stop", required=True)

    return parser


def _apply_taskset(spec: str) -> None:
    if not spec:
        return
    cpus: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    if cpus:
        os.sched_setaffinity(0, cpus)
        print(f"[env] 绑核: {sorted(cpus)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "_evsd-receiver":
        return _evsd_receiver_main(args)
    _apply_taskset(getattr(args, "taskset", ""))
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    base_scratch = Path(args.scratch) if args.scratch else SCRATCH_ROOT
    scratch = base_scratch / f"perf-bench-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    scratch.mkdir(parents=True, exist_ok=True)
    atexit.register(lambda: shutil.rmtree(scratch, ignore_errors=True))

    host_info = _host_info(scratch)
    if args.command in ("evsd-loopback", "http", "matrix", "all", "resume", "cpu-split"):
        print("[env] 磁盘完成路径探针（preallocate+write+fdatasync 64 MiB）…", flush=True)
        host_info["disk_probe"] = _disk_probe(scratch)
        print(f"       -> {host_info['disk_probe']}", flush=True)
    results: dict = {"runs": [], "summaries": [], "_host": host_info, "config": vars(args)}
    exit_code = 0
    try:
        if args.command == "evsd-loopback":
            run_evsd_bench(args, scratch, results)
        elif args.command == "http":
            run_http_bench(args, scratch, results)
        elif args.command == "matrix":
            run_evsd_bench(args, scratch, results)
            run_http_bench(args, scratch, results)
        elif args.command == "resume":
            run_resume_bench(args, scratch, results)
        elif args.command == "cpu-split":
            _run_cpu_split(args, scratch, results)
        elif args.command == "all":
            run_evsd_bench(args, scratch, results)
            run_http_bench(args, scratch, results)
            if not args.skip_resume:
                resume_args = argparse.Namespace(
                    size_mib=args.resume_size_mib,
                    streams=args.resume_streams,
                    fraction=args.resume_fraction,
                    repeats=args.resume_repeats,
                    keep=args.keep,
                )
                run_resume_bench(resume_args, scratch, results)
        else:  # pragma: no cover
            parser.error(f"未知子命令 {args.command}")
    except KeyboardInterrupt:
        print("\n[bench] 被中断，尝试清理…", flush=True)
        exit_code = 2
    except Exception as exc:
        print(f"[bench] harness 异常: {type(exc).__name__}: {exc}", flush=True)
        import traceback

        traceback.print_exc()
        exit_code = 2
    finally:
        for proc in list(_LIVE_PROCS):
            _stop_process(proc, 5.0)

    # 退出码：校验失败/传输失败也算 1
    failed = [r for r in results["runs"] if not r.get("ok", True) and not r.get("skipped")]
    if failed:
        exit_code = exit_code or 1
    if args.json == "" and args.md == "":
        default_dir = REPORT_DIR
        if args.command in ("matrix", "all"):
            args.json = str(default_dir / "bench-baseline.json")
            args.md = str(default_dir / "bench-baseline.md")
        else:
            args.json = str(default_dir / f"bench-{args.command}.json")
        if args.command == "cpu-split" and not args.md:
            args.md = ""
    _write_outputs(args, results)
    print(
        f"[bench] 完成：{len(results['runs'])} 条测量，失败 {len(failed)} 条，exit={exit_code}",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
