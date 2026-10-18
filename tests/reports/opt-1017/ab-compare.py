#!/usr/bin/env python3
"""A/B 对比：读 BEFORE/AFTER 两个输出目录的 JSON，生成 bench-before-after.md。

主判据：CPU 秒/MiB（受负载影响小）；辅判据：tmpfs 组 MiB/s（协议上限）。
btrfs 组 MiB/s 作为「用户可感知端到端」参考，必须标注 quiet-window load。
用法: python3 .testscratch/perf-bench-compare.py <before-dir> <after-dir> [out.md]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_dir(path: Path) -> dict[str, dict]:
    data: dict[str, dict] = {}
    for file in sorted(path.glob("*.json")):
        try:
            data[file.stem] = json.loads(file.read_text())
        except ValueError:
            pass
    return data


def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 4) if values else None


def collect(datasets: dict[str, dict]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for data in datasets.values():
        for run in data.get("runs") or []:
            if run.get("warmup") or run.get("skipped") or not run.get("ok", True):
                continue
            if run.get("env_stall"):
                continue
            key = (run.get("kind"), run.get("size_mib"), run.get("streams"))
            groups.setdefault(key, []).append(run)
    return groups


def metrics(items: list[dict]) -> dict:
    return {
        "rate": med([i.get("mib_per_s") for i in items]),
        "wall": med([i.get("wall_s") for i in items]),
        "cpu_mib": med([i.get("cpu_s_per_mib") for i in items]),
        "n": len(items),
    }


def main() -> int:
    before_dir = Path(sys.argv[1])
    after_dir = Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else REPO / "tests/reports/opt-1017/bench-before-after.md"
    before = collect(load_dir(before_dir))
    after = collect(load_dir(after_dir))
    lines = ["# 优化前后 A/B 对比（同一脚本 / 同机 / 同安静窗口）", ""]
    lines.append(f"- BEFORE: `{before_dir}`")
    lines.append(f"- AFTER:  `{after_dir}`")
    lines.append("- 主判据 CPU 秒/MiB（负载敏感低）；辅助判据 tmpfs 组 MiB/s；btrfs 组 MiB/s 为用户可感知端到端参考。")
    lines.append("- 每次运行都剔除 ENV_STALL（吞吐 < 同配置中位 40%）与 warmup/skipped。")
    lines.append("")
    lines.append("| 链路 | size MiB | streams | BEFORE MiB/s | AFTER MiB/s | Δ% | BEFORE CPU s/MiB | AFTER CPU s/MiB | CPU Δ% | n(b/a) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    keys = sorted(set(before) | set(after), key=lambda k: tuple(str(x) for x in k))
    summary_rows = []
    for key in keys:
        b = metrics(before.get(key, []))
        a = metrics(after.get(key, []))
        if b["rate"] is None or a["rate"] is None:
            continue
        delta = (a["rate"] / b["rate"] - 1.0) * 100.0 if b["rate"] else None
        cpu_delta = (
            (a["cpu_mib"] / b["cpu_mib"] - 1.0) * 100.0
            if (a["cpu_mib"] and b["cpu_mib"]) else None
        )
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | {b['rate']} | {a['rate']} | "
            f"{f'{delta:+.1f}' if delta is not None else '—'} | {b['cpu_mib']} | {a['cpu_mib']} | "
            f"{f'{cpu_delta:+.1f}' if cpu_delta is not None else '—'} | {b['n']}/{a['n']} |"
        )
        summary_rows.append((key, delta, cpu_delta))
    lines.append("")
    # CPU 账本对比（若两边都有 cpu-split）
    def cpu_ledger(datasets):
        for data in datasets.values():
            for run in data.get("runs") or []:
                if run.get("kind") == "evsd-cpu-split" and run.get("ok") and run.get("profile"):
                    return run
        return None
    b_led = cpu_ledger(load_dir(before_dir))
    a_led = cpu_ledger(load_dir(after_dir))
    if b_led and a_led:
        lines.append("## 2 GiB 分阶段 CPU 账本对比（tmpfs）")
        lines.append("")
        lines.append("| 阶段 | BEFORE CPU s | AFTER CPU s | Δ% | BEFORE % | AFTER % |")
        lines.append("|---|---|---|---|---|---|")
        labels = b_led["profile"].get("bucket_labels") or {}
        buckets = sorted(set(b_led["profile"]["stages"]) | set(a_led["profile"]["stages"]))
        for bucket in buckets:
            b_slot = b_led["profile"]["stages"].get(bucket, {})
            a_slot = a_led["profile"]["stages"].get(bucket, {})
            b_cpu, a_cpu = b_slot.get("cpu_s"), a_slot.get("cpu_s")
            delta = (a_cpu / b_cpu - 1.0) * 100.0 if (a_cpu is not None and b_cpu) else None
            lines.append(
                f"| {labels.get(bucket, bucket)} (`{bucket}`) | {b_cpu} | {a_cpu} | "
                f"{f'{delta:+.1f}' if delta is not None else '—'} | "
                f"{b_slot.get('pct_of_total')} | {a_slot.get('pct_of_total')} |"
            )
        lines.append("")
        lines.append(
            "> **脚注（T4 后的语义变化）**：`disk_sync` 桶 = `PositionedFile.sync` + "
            "`PartFile._maybe_flush` + `PartFile.flush`。T4 改变了收尾 fsync 的路径与时机，"
            "该桶的跨版本差值不可直接当优化收益；T4 的真实收益看 btrfs 组端到端 wall / MiB/s。"
        )
        lines.append("")
    lines.append("## 结论（自动判据）")
    lines.append("")
    improved = [row for row in summary_rows if row[2] is not None and row[2] < -3.0]
    regressed = [row for row in summary_rows if row[2] is not None and row[2] > 3.0]
    lines.append(f"- CPU s/MiB 下降 >3% 的配置：{len(improved)} 个；上升 >3% 的配置：{len(regressed)} 个。")
    lines.append("- 目标：T1/T4/T5/T8 改动后 EVSD 的 CPU s/MiB 明显下降；tmpfs MiB/s 上升；btrfs 端到端因 T4 应显著上升。")
    lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    for row in summary_rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
