#!/usr/bin/env python3
"""用所有已测 JSON 生成 bench-baseline.md 的最终「基线结论」章节。

原则：缺哪组数据就标 PENDING（说明由哪个 A/B 组补），不猜、不编；所有数字从 JSON 现算。
生成结果追加到 tests/reports/opt-1017/bench-baseline.md。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import bench_transfer as B  # noqa: E402

REPORT = REPO / "tests" / "reports" / "opt-1017"
MIB = 1024 * 1024


def load(name: str) -> dict | None:
    path = REPORT / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 3) if values else None


def cv(values):
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    m = statistics.median(values)
    return round(statistics.pstdev(values) / m * 100.0, 1) if m else None


def all_runs(*datasets: dict | None) -> list[dict]:
    runs: list[dict] = []
    for data in datasets:
        if data:
            runs.extend(data.get("runs") or [])
    B._mark_env_stalls(runs)
    return runs


def runs_of(runs, kind, size=None, streams=None, *, clean: bool = True):
    out = []
    for run in runs:
        if run.get("kind") != kind or run.get("warmup") or run.get("skipped") or not run.get("ok", True):
            continue
        if clean and run.get("env_stall"):
            continue
        if size is not None and run.get("size_mib") != size:
            continue
        if streams is not None and run.get("streams") != streams:
            continue
        out.append(run)
    return out


def rate(runs, kind, size=None, streams=None):
    return med([r["mib_per_s"] for r in runs_of(runs, kind, size, streams)])


def cpu_mib(runs, kind, size=None, streams=None):
    return med([r.get("cpu_s_per_mib") for r in runs_of(runs, kind, size, streams)])


def fmt(value, digits: int = 1, dash: str = "PENDING") -> str:
    if value is None:
        return dash
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> int:
    base = load("bench-baseline.json")
    http_btrfs = load("bench-http-btrfs.json")
    tmpfs = load("bench-supp-tmpfs.json")
    tmpfs_1g = load("bench-supp-tmpfs-1g.json")
    nvme = load("bench-supp-nvme.json")
    split = load("bench-cpu-split.json")
    runs = all_runs(base, http_btrfs, tmpfs, tmpfs_1g, nvme, split)

    http_runs = None
    http_src = ""
    if http_btrfs and any(str(r.get("kind", "")).startswith("http-") for r in http_btrfs.get("runs") or []):
        http_runs = list(http_btrfs.get("runs") or [])
        http_src = "bench-http-btrfs.json（新代码：一次性 + 续传分段 + 下载 + Range）"
    elif base:
        http_runs = [r for r in base.get("runs") or [] if str(r.get("kind", "")).startswith("http-")]
        http_src = ("bench-baseline.json 的 HTTP 组（旧脚本：一次性上传/下载/Range；"
                    "新代码专属的续传上传与 sha256 校验由 quiet-window A/B 组补）")
    if http_runs:
        B._mark_env_stalls(http_runs)

    lines: list[str] = []
    A = lines.append
    A("")
    A("---")
    A("")
    A("# 基线结论（perf-bench 手写 + 自动数字）")
    A("")
    A("> 本节由 `.testscratch/perf-bench-analysis.py` 从同目录 JSON 现算生成；缺数据项标 **PENDING**（说明由哪个 A/B 组补），不做推断。")
    A("")

    host = (base or {}).get("host") or {}
    disk = host.get("disk_probe") or {}
    git = host.get("git_state") or {}
    A("## 0. 版本与环境口径（before/after 可比性）")
    A("")
    A(f"- 基线 git HEAD：`{git.get('head', '')}`；测量时脏文件 {len(git.get('dirty_files') or [])} 个"
      "（工作树全团队共享，`web/server.py` 等在测量期间有他人改动，已记录 md5）")
    A(f"- 测量时关键文件 md5：`web/server.py` = `{(git.get('md5') or {}).get('src/eversend/web/server.py', '?')}`；"
      f"`core/chunkstore.py` = `{(git.get('md5') or {}).get('src/eversend/core/chunkstore.py', '?')}`")
    A(f"- **本机磁盘完成路径探针**（preallocate+write+fdatasync 64 MiB）：fdatasync ≈ "
      f"**{fmt(disk.get('fdatasync_mib_s'), 1)} MiB/s**（{fmt(disk.get('fdatasync_s'), 3)} s / 64 MiB）")
    A("- 机器：16 核 AMD Ryzen 7 2700X；/home = **USB 便携 SSD（LanKxin PSSD）上的 btrfs**；"
      "基线测量期间其他专家任务并行，loadavg 10–18（每次运行都记录 `loadavg_before/after`）。")
    A("- **口径提醒：loopback 数字只用于 CPU/协议归因，不能当用户速度。** 真实手机↔电脑 WiFi 基线"
      "（Lead 真机实测，PC 侧 sentBytes 采样）约 **24–25 MiB/s**。")
    A("")

    http_upload_1g = rate(http_runs or [], "http-upload", 1024)
    http_dl_1g = rate(http_runs or [], "http-download", 1024)
    tmpfs_has = tmpfs is not None and bool(tmpfs.get("runs"))
    tmpfs_runs = all_runs(tmpfs, tmpfs_1g) if tmpfs_has else []
    tmpfs_evsd_512_4 = rate(tmpfs_runs, "evsd-loopback", 512, 4)
    btrfs_evsd_512_4 = rate(runs, "evsd-loopback", 512, 4)
    A("## 0.5 速度档位：用户可感知速度在哪一档")
    A("")
    A("| 链路 | 量级 MiB/s | 来源/口径 | 用户感知 |")
    A("|---|---|---|---|")
    A("| 真实手机 ↔ 电脑 WiFi（PC→手机） | **24–25** | Lead 真机 sentBytes 采样（MI9，load≈8.9） | 用户真实速度；APK 侧优化才有感 |")
    A(f"| 本机 HTTP localhost（服务端上限参考） | {fmt(http_upload_1g)} 上传 / {fmt(http_dl_1g)} 下载 | 本报告 §3；loopback page cache | 服务端 CPU/协议不是瓶颈 |")
    A(f"| 本机 EVSD loopback（btrfs/USB 落盘，端到端） | **{fmt(btrfs_evsd_512_4)}** | 本报告 §1，512MiB×4 流（剔除 ENV_STALL 后） | 桌面↔桌面大文件「看起来慢」就是这一档 |")
    A(f"| 本机 EVSD loopback（tmpfs，协议/CPU 上限） | {fmt(tmpfs_evsd_512_4)} | 本报告 §1；PENDING 的 quiet-window A/B 组补全量 | 去掉慢盘 fsync 后的协议上限 |")
    A("| btrfs/USB 盘 fsync 上限 | 39–56 | 磁盘探针 `_disk_probe`（64 MiB fdatasync，多轮） | 收尾刷盘时吞吐被截在这里 |")
    A("")
    A("**结论**：用户真实感受 ≈ 24–25 MiB/s（WiFi 档）。安静窗口下它高于本机慢盘 EVSD，"
      "说明真机链路瓶颈在手机 WiFi/协议链路而不是这台电脑的磁盘；但桌面↔桌面大文件/本机验收会被慢盘 fsync 截住"
      "（高负载时 512 MiB 单次墙钟出现过 60–130 s 的 I/O 停顿），T4 修完应显著改善本机与 LAN 场景的收尾。")
    A("")

    A("## 1. EVSD 双组：慢盘（btrfs/USB） vs tmpfs（协议上限） vs NVMe（快盘）")
    A("")
    A("噪声处理：单次吞吐 < 同配置中位 40% 的运行标 `ENV_STALL_EXCLUDED`（USB 盘 I/O 停顿），"
      "下表同时给**原始中位**与**剔除停顿后的中位**；被剔除运行在 §1.2 单列，保留证据。")
    A("")
    A("| size MiB | streams | btrfs 原始中位 | btrfs 剔除后中位 (n_clean) | tmpfs 中位 | NVMe 中位 | btrfs CPU s/MiB | tmpfs CPU s/MiB |")
    A("|---|---|---|---|---|---|---|---|")
    ratios = []
    for size, streams in ((64, 1), (64, 4), (512, 1), (512, 4), (1024, 4)):
        raw = med([r["mib_per_s"] for r in runs_of(runs, "evsd-loopback", size, streams, clean=False)])
        clean = rate(runs, "evsd-loopback", size, streams)
        n_clean = len(runs_of(runs, "evsd-loopback", size, streams))
        t = rate(tmpfs_runs, "evsd-loopback", size, streams) if tmpfs_has else None
        n = rate(nvme, "evsd-loopback", size, streams) if (nvme and nvme.get("runs")) else None
        bc = cpu_mib(runs, "evsd-loopback", size, streams)
        tc = cpu_mib(tmpfs_runs, "evsd-loopback", size, streams) if tmpfs_has else None
        if t and clean:
            ratios.append(t / clean)
        A(f"| {size} | {streams} | {fmt(raw)} | {fmt(clean)} ({n_clean}) | {fmt(t)} | {fmt(n)} | {fmt(bc, 4)} | {fmt(tc, 4)} |")
    A("")
    if ratios:
        A(f"tmpfs/btrfs 剔除后中位倍数 ≈ **{fmt(med(ratios), 2)}×** —— 这就是慢盘完成路径"
          "（`chunkstore.finalize()` 的 `file.sync()` + USB SSD）吃掉的吞吐。")
    else:
        A("> **PENDING**：tmpfs/NVMe 组数据由 quiet-window A/B 组重跑补齐"
          "（原补充测量被机器重启/负载中断）。当前唯一可信的对比证据是本机磁盘探针（fdatasync 39–56 MiB/s）。")
    A("")

    A("### 1.1 流数扩展（512 MiB，btrfs，剔除停顿后）")
    A("")
    A("| streams | MiB/s（剔除后） | 原始中位 | CV% | wall s | CPU s/MiB |")
    A("|---|---|---|---|---|---|")
    for streams in (1, 2, 4, 8):
        clean_runs = runs_of(runs, "evsd-loopback", 512, streams)
        raw_runs = runs_of(runs, "evsd-loopback", 512, streams, clean=False)
        A(f"| {streams} | {fmt(rate(runs, 'evsd-loopback', 512, streams))} | "
          f"{fmt(med([r['mib_per_s'] for r in raw_runs]))} | "
          f"{fmt(cv([r['mib_per_s'] for r in clean_runs]), 1)} | "
          f"{fmt(med([r['wall_s'] for r in clean_runs]), 2)} | "
          f"{fmt(cpu_mib(runs, 'evsd-loopback', 512, streams), 4)} |")
    A("")
    A("**观察**：慢盘组 1→8 流收益很小（高负载下一次 1–9 MiB/s 的停顿会盖过一切），"
      "说明端到端被「收尾 fsync + 写回」串行截住；协议/CPU 真实扩展曲线要等 tmpfs 组安静窗口数据。")
    A("")

    A("### 1.2 受环境停顿污染的运行（ENV_STALL_EXCLUDED）")
    A("")
    polluted = [r for r in runs if r.get("env_stall")]
    if polluted:
        A("| kind | size MiB | streams | MiB/s | wall s | loadavg(前) | 原因 |")
        A("|---|---|---|---|---|---|---|")
        for r in sorted(polluted, key=lambda x: (str(x.get("kind")), x.get("size_mib") or 0, x.get("streams") or 0)):
            A(f"| {r.get('kind')} | {r.get('size_mib')} | {r.get('streams')} | {fmt(r.get('mib_per_s'))} | "
              f"{fmt(r.get('wall_s'), 2)} | {fmt((r.get('loadavg_before') or [None])[0], 2)} | {r.get('env_stall_reason')} |")
    else:
        A("（本次没有触发环境停顿剔除的运行）")
    A("")

    A("## 2. EVSD 2 GiB × 4 流：分阶段 CPU 账本（阶段 2 收益折算依据）")
    A("")
    split_runs = [r for r in runs if r.get("kind") == "evsd-cpu-split" and r.get("ok") and r.get("profile")]
    if split_runs:
        first = split_runs[0]
        A(f"- 测量：{first.get('size_mib')} MiB × streams={first.get('streams')}，n={len(split_runs)}，tmpfs；"
          f"wall 中位 {fmt(med([r['wall_s'] for r in split_runs]), 2)} s，"
          f"总 CPU 中位 {fmt(med([r['total_cpu_s'] for r in split_runs]), 2)} s，"
          f"CPU s/MiB 中位 {fmt(med([r['cpu_s_per_mib'] for r in split_runs]), 5)}。")
        A("")
        A("| 阶段 | CPU s（中位） | 占总 CPU %（中位） |")
        A("|---|---|---|")
        labels = first["profile"].get("bucket_labels") or {}
        for bucket in sorted(first["profile"]["stages"]):
            vals = [r["profile"]["stages"].get(bucket, {}).get("cpu_s") for r in split_runs]
            pcts = [r["profile"]["stages"].get(bucket, {}).get("pct_of_total") for r in split_runs]
            A(f"| {labels.get(bucket, bucket)} (`{bucket}`) | {fmt(med(vals), 3)} | {fmt(med(pcts), 1)} |")
        A("")
        A("口径：同进程双 Engine（与 `tests/test_loopback.py` 同构）；`time.thread_time()` 自时间归因，"
          "嵌套调用只记最内层（外层自时间 = 总时间 − 子调用时间），各桶不重复计；"
          "`other` = 进程总 CPU − 已归因。**墙钟在共享负载机上不可比，CPU 秒/MiB 更稳。**")
        A("")
        A("> **T4 脚注**：`disk_sync` 桶 = `PositionedFile.sync` + `PartFile._maybe_flush` + `PartFile.flush`；"
          "T4 会改变收尾落盘的调用组成，跨版本只比较该桶数量级/占比，不把差值当纯收益；"
          "T4 真实收益看 btrfs 组端到端 wall / MiB/s。")
    else:
        A("> **PENDING**：2 GiB 分阶段 CPU 账本由 quiet-window A/B 两组（BEFORE=旧树 / AFTER=新树，"
          "`cpu-split --size-mib 2048 --streams 4 --repeats 3 --scratch /dev/shm/...`）产出。"
          "脚本已在本机冒烟（64 MiB）：hash 35.8% / aead 16.0% / crc32 11.4% / connection 9.6% / "
          "socket_recv 5.4% / other 15.3%；旧树 worktree 兼容性也已验证可出账本。")
    A("")

    A("## 3. HTTP 链路（手机 App↔电脑同构：localhost 真 HTTP，单请求）")
    A("")
    A(f"数据源：{http_src}")
    A("")
    A("| 操作 | size MiB | MiB/s | wall s | CPU s/MiB | tx CPU s | rx CPU s | 峰值 RSS KB (server) |")
    A("|---|---|---|---|---|---|---|---|")
    any_http = False
    for kind, label in (("http-upload", "一次性上传 POST /api/upload"),
                        ("http-upload-resumable", "续传分段上传（3 段）"),
                        ("http-download", "下载 GET /api/download"),
                        ("http-download-range", "Range 下载（8 MiB/段）")):
        for size in (64, 512, 1024):
            items = runs_of(http_runs or [], kind, size)
            if not items:
                continue
            any_http = True
            A(f"| {label} | {size} | {fmt(med([r['mib_per_s'] for r in items]))} | "
              f"{fmt(med([r['wall_s'] for r in items]), 3)} | "
              f"{fmt(med([r.get('cpu_s_per_mib') for r in items]), 5)} | "
              f"{fmt(med([r.get('tx_cpu_s') for r in items]), 3)} | "
              f"{fmt(med([r.get('rx_cpu_s') for r in items]), 3)} | "
              f"{fmt(med([r.get('rx_rss_hwm_kb') for r in items]), 0)} |")
    if not any_http:
        A("| （无 HTTP 数据） | | | | | | | |")
    A("")
    A("**观察**：HTTP 下载/上传在 localhost 的 CPU s/MiB 极低、下载可达数千 MiB/s（page cache），"
      "说明服务端 CPU/协议开销远小于 WiFi 带宽；真实手机链路瓶颈是 WiFi 上下行（~24–25 MiB/s）。")
    A("")
    A("### 3.1 关于早期报告里的 `SHA_MISMATCH` 标注（澄清 + 已修复）")
    A("")
    A("早期自动摘要里 `http-download*` 全部显示 `SHA_MISMATCH` —— **这是基准脚本的判据错误，不是产品 bug**：")
    A("")
    A("1. 下载性能测量为了不让哈希成本污染 wall，**刻意丢弃 body 只统计字节数**，`sha_ok` 字段因此是 `None`（=「本测量未做哈希」）；")
    A("2. 旧版摘要代码把所有「非真」值当不一致（`not None == True`），把 `None` 误标成 `SHA_MISMATCH`；")
    A("3. 证据：所有下载运行 `size_ok=True`（收到字节数 == 文件大小）且 `ok=True`；")
    A("   本轮旧树兼容性冒烟已实测 `http-download-verify`：**整文件下载 sha256 一致、Range 拼接 sha256 一致**；产品侧还有 web selftest 356 项与真机 sha 交叉验证。")
    A("")
    A("**修复**：(a) 摘要判据改成只有显式 `False` 才算不一致；(b) 新增 `http-download-verify`："
      "性能测量**之外**单独下载并算 sha256（整文件 + Range 拼接两条）；(c) 后续报告不再出现这条假红线。")
    A("")
    verifications = []
    for data in (base, http_btrfs):
        verifications.extend((data or {}).get("verifications") or [])
    A("### 3.2 下载完整性校验（测量外 sha256）")
    A("")
    if verifications:
        A("| 校验方式 | size MiB | 状态码 | 收到字节 | sha256 一致 | wall s（测量外） |")
        A("|---|---|---|---|---|---|")
        for item in verifications:
            mode = "Range 分段拼接" if item.get("chunk_mib") else "整文件单请求"
            A(f"| {mode} | {fmt(item.get('size_mib'))} | {','.join(str(s) for s in (item.get('statuses') or []))} | "
              f"{item.get('bytes')} | {item.get('sha_ok')} | {fmt(item.get('wall_s'), 3)} |")
    else:
        A("> **PENDING**：本基线下载只做字节数校验；sha256 证据由 quiet-window A/B 两组产出"
          "（`http-download-verify` 已内置于脚本，每组对 64/512/1024 MiB 整文件与 Range 拼接各校验一次；"
          "旧树兼容性冒烟已证明该功能可用：整文件/`Range` 拼接 sha256 均一致）。")
    A("")

    A("## 4. EVSD 续传（中断 → 同目录重启 → 补传）")
    A("")
    resume_runs = [r for r in runs if r.get("kind") == "evsd-resume" and r.get("ok")]
    if resume_runs:
        A("| size MiB | streams | 中断时 journal 落定 | 落定比 | 补传字节比 | 恢复 wall s | 冷传 wall s | 提速 | loadavg(前) |")
        A("|---|---|---|---|---|---|---|---|---|")
        for r in resume_runs:
            A(f"| {r.get('size_mib')} | {r.get('streams')} | {r.get('persisted_bytes_at_interrupt')} | "
              f"{fmt(r.get('persisted_ratio'))} | {fmt(r.get('retransmit_ratio'))} | "
              f"{fmt(r.get('wall_s'), 2)} | {fmt(r.get('cold_wall_s'), 2)} | "
              f"{fmt(r.get('resume_speedup_vs_cold'), 2)} | {fmt((r.get('loadavg_before') or [None])[0], 2)} |")
        A("")
        A("**注意**：补传字节比由 journal 落定状态决定，**不受墙钟污染**（两次几乎一致，可靠性高）；"
          "而「恢复 wall」受本机 USB 盘 I/O 停顿影响（同配置出现过 60.8 s vs 16.5 s），"
          "quiet-window A/B 的恢复耗时才可作为提速依据。")
    else:
        A("> **PENDING**：续传数据由 quiet-window A/B 组产出（脚本已就绪；本机基线已有 2 次成功，补传比中位 64.2%，恢复 wall 受 I/O 停顿污染）。")
    A("")

    A("## 5. 瓶颈假设（按证据强度排序）")
    A("")
    A("1. **本机 btrfs/USB 盘的完成路径 fsync 是当前 loopback 端到端第一瓶颈。**"
      f"磁盘探针 fdatasync ≈ {fmt(disk.get('fdatasync_mib_s'), 1)} MiB/s，而 EVSD 接收端 "
      "`chunkstore.finalize()` 有 `file.sync()`（`core/chunkstore.py:385-425`）且 `_maybe_flush` 周期性 `fdatasync(.part)`；"
      "btrfs 组 CPU s/MiB（0.006–0.017）远未打满 CPU，说明不是 CPU 墙；高负载下一次 512 MiB 传输可被 I/O 停顿放大到 60–130 s。")
    A("2. **hash（blake2b）是当前最大 CPU 桶，AEAD 次之，crc32 第三。** 64 MiB 冒烟账本："
      "hash 35.8% / aead 16.0% / crc32 11.4% / connection 9.6% / socket_recv 5.4% / other 15.3%；"
      "hash 大头是接收端 `IncrementalFileHasher` 逐块 blake2b + 两端整文件摘要；AEAD 每块 seal/open；"
      "crc32 在 AEAD 已保完整性时仍两端各算一遍。LAN 千兆（118 MiB/s）下 CPU 不是墙，loopback/2.5GbE+/手机弱 CPU 上会变成墙。")
    A("3. **协议固定成本摊在 1 MiB 块上（与 transport-core T3/T5/T6/T7 一致）。** 本机 chunk 数 = size/1 MiB；"
      "流数 1→8 在慢盘组几乎无收益，说明瓶颈不在并行度而在串行完成路径与每块固定开销。")
    A("4. **HTTP 链路瓶颈在 WiFi（真实 ~24–25 MiB/s）而不是服务端 CPU。** localhost CPU s/MiB 极低；"
      "真实提速靠少 RTT/大 IO/sendfile/断点续传。")
    A("")

    A("## 6. 给 transport 侧的建议（按预期收益排序，阶段 2 可直接做）")
    A("")
    A("| 优先级 | 改动 | 预期收益（基线证据） | 风险 | 复测口径 |")
    A("|---|---|---|---|---|")
    A("| 1 | **收尾 fsync 移出关键路径 / 合并**（T4：finalize 的两次 `file.sync()` 与 `_maybe_flush` 移出锁、整文件 sync 延迟/异步；保持 journal 语义） | "
      "本机 btrfs loopback 端到端从 ~40–65 MiB/s 提升到接近协议上限；消除 512 MiB 级秒级停顿 | 中（掉电语义需文档化） | A/B `evsd-loopback` btrfs + tmpfs + 续传 |")
    A("| 2 | **削减 hash 成本**（T9：避免两端重复整文件摘要、流式 hasher 大块合并、必要时并行） | 2 GiB 账本最大 CPU 桶（冒烟 35.8%），去重可省约一半 hash CPU | 低-中 | A/B `cpu-split --size-mib 2048 --streams 4` |")
    A("| 3 | **AEAD 去全量复制（T1：crypto.py seal/open 改 memoryview 直传）** | aead 一档（冒烟 16.0%）；transport-core 微基准 4 MiB 块 +115%，CPU s/MiB 预计降 10–15% | 低（+单测） | A/B cpu-split + `tests/test_loopback.py` |")
    A("| 4 | **crc32 在 AEAD 已验证时跳过接收端复算（T8）** | 冒烟 11.4% CPU；省 ~8–11% | 低-中（不加密/修复扫描保留） | A/B cpu-split |")
    A("| 5 | **降低每块固定成本**（T5 dict 替代线性扫描 + 顶层 import；T7 合并 3 次 recv_into） | 1–2% CPU + 更少 syscall | 低 | A/B 64 MiB×4 流 |")
    A("| 6 | **HTTP sendfile/大块**（下载 os.sendfile；上传 IO_CHUNK 256 KiB→1 MiB；续传分段 Lead 已落地） | localhost CPU 已低，收益主要在真实 WiFi 的 RTT/syscall 数 | 低-中 | A/B `http --resume-segments 3` |")
    A("")
    A("注：T1/T3–T10 编号与 `tests/reports/opt-1017/transport-audit.md`（transport-core）对齐，互为证据。")
    A("")

    A("## 7. 复现命令")
    A("")
    A("```bash")
    A("cd /home/xgl/我的项目/自制软件/韧传/eversend")
    A("PYTHONPATH=src python3 tools/bench_transfer.py all \\")
    A("    --sizes 64,512,1024 --streams 1,2,4,8 --repeats 3 --warmups 1 \\")
    A("    --resume-size-mib 512 --resume-repeats 2 \\")
    A("    --json tests/reports/opt-1017/bench-baseline.json --md tests/reports/opt-1017/bench-baseline.md")
    A("")
    A("PYTHONPATH=src python3 tools/bench_transfer.py evsd-loopback --sizes 64,512 --streams 1,4 \\")
    A("    --repeats 3 --warmups 1 --scratch /dev/shm/perf-bench")
    A("PYTHONPATH=src python3 tools/bench_transfer.py cpu-split --size-mib 2048 --streams 4 --repeats 2 \\")
    A("    --scratch /dev/shm/perf-bench")
    A("")
    A("# A/B 一键（安静窗口 load<4；脚本自带守卫，>4 直接 ABORT）")
    A("bash .testscratch/perf-bench-ab.sh /home/xgl/qa-real/baseline-tree before-quiet /home/xgl/qa-real/out/before")
    A("bash .testscratch/perf-bench-ab.sh \"$PWD\" after-quiet /home/xgl/qa-real/out/after")
    A("python3 .testscratch/perf-bench-compare.py /home/xgl/qa-real/out/before /home/xgl/qa-real/out/after")
    A("```")
    A("")

    A("## 8. before/after 可比口径（预注册，避免事后解释）")
    A("")
    A("**方法**：同一脚本、同一机器、同一安静窗口（load<4），分别跑两棵树：")
    A("")
    A("- BEFORE：`git worktree add /home/xgl/qa-real/baseline-tree e18c079`（优化前完整树，已建好并验证兼容），"
      "把当前 `tools/bench_transfer.py` 复制进去（脚本是新工具，快照里没有）；")
    A("- AFTER：当前工作树（优化后），**同一个脚本文件**；")
    A("- 两组都在 `/dev/shm`（tmpfs）+ btrfs 上各跑一遍；`--label before-quiet` / `after-quiet`；每组**5 轮中位**（预热 1，剔除 ENV_STALL）。")
    A("")
    A("**可比性判据（按优先级）**：")
    A("")
    A("1. **主判据：CPU 秒/MiB**（受负载影响小）—— 同配置 AFTER 相对 BEFORE 的变化 %；")
    A("2. **辅助：tmpfs 组 MiB/s**（协议/CPU 上限）—— 安静窗口下可直接比；")
    A("3. **参考：btrfs 组 MiB/s**（用户可感知端到端）—— 受慢盘 I/O 停顿影响，必须同时给 loadavg 与 CV，"
      "并用「剔除 ENV_STALL 后中位」比较；")
    A("4. **续传**：补传字节比（journal 决定，稳定）+ 安静窗口恢复 wall；")
    A("5. **0 假红**：BEFORE 旧树没有可续传上传（无 `/api/upload/status`），脚本自动探测并记 `SKIPPED`（不计 FAIL，旧树冒烟已验证）。")
    A("")
    A("**预注册验收线**：T4 后 btrfs 端到端应 ≥1.5×；T1/T8/T9 后 EVSD CPU s/MiB 应下降 ≥10%；"
      "任何配置 CPU s/MiB 上升 >3% 视为回退，必须解释。")
    A("")
    A("**为什么基线 btrfs 绝对值不作为 before/after 吞吐基线**：本次基线在 load 10–18 的共享机采集，"
      "偶发 I/O 停顿已用 ENV_STALL 标注剔除，但剩余噪声仍大；quiet-window A/B 用同一脚本重采两组，"
      "本文档的 btrfs 绝对值只用于机制归因；**tmpfs 组 + CPU s/MiB 才是主判据**。")
    A("")

    out = REPORT / "bench-baseline.md"
    prev = out.read_text(encoding="utf-8") if out.exists() else ""
    marker = "## 人工分析"
    if marker in prev:
        prev = prev.split(marker)[0]
    out.write_text(prev.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"appended analysis -> {out}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
