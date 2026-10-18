#!/usr/bin/env python3
"""真机 GUI 显示准确性探针：传输过程中同时记录「PC 侧真值」与「手机屏幕」。

为什么需要它
------------
用户要的是「GUI 界面显示的准确度」：手机/桌面显示的进度、速度、剩余时间、状态，
必须和后台真值一致。只靠最终 sha256 判不出"显示撒谎"（A2 曾经 100% 完成但卡片
还写着「接收中…」）。这个探针在**真机真系统**跑一次现成的真机用例（默认 A2：
电脑发 100 MiB 给手机），期间：

* 每 ~1.2s 记一次 PC 侧真值（``/api/state`` 的 shares.sentBytes / transfers）；
* 每 ~1.2s 抓一张手机截图（``adb exec-out screencap``）；
* 把「时间 → 真值 → 截图文件」对齐成时间线，落盘成 JSON+Markdown，供人工/下次
  自动化比对界面显示值（截图里能看到进度条百分比与卡片状态）。

用法（在 eversend/ 下）::

    EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real \
    DISPLAY=:99 QT_QPA_PLATFORM=xcb \
    python3 tools/realdevice_probe.py --only A2 --label gui-before

注意：探针**不驱动手机 UI**（键盘/tap 都归用例自己），只读截图与 PC API，
所以不会和 test_real_device.py 抢手机控制权；但同一时刻只能有一个电脑端实例
（默认端口 52117/52119），跑之前确认没有别的实例在跑。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORTS = REPO / "tests" / "reports"
OPT = REPORTS / "opt-1017"
SHOTS = OPT / "gui-probe-shots"


def http_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception:
        return None


def adb(serial: str, *args: str, binary: bool = False):
    command = ["adb", "-s", serial, *args]
    proc = subprocess.run(command, capture_output=True, timeout=30)
    return proc.stdout if binary else (proc.stdout or b"").decode("utf-8", "replace")


def free_ports() -> bool:
    import socket

    for port, kind in ((52117, socket.SOCK_STREAM), (52119, socket.SOCK_STREAM),
                       (52118, socket.SOCK_DGRAM)):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机 GUI 显示准确性探针")
    parser.add_argument("--only", default="A2", help="传给 test_real_device.py 的 --only")
    parser.add_argument("--label", default="gui", help="报告/截图前缀")
    parser.add_argument("--interval", type=float, default=1.2, help="采样间隔秒")
    parser.add_argument("--timeout", type=float, default=600.0, help="整体超时秒")
    args = parser.parse_args(argv)

    serial = os.environ.get("EVERSEND_ADB_SERIAL", "")
    if not serial:
        print("缺少 EVERSEND_ADB_SERIAL", file=sys.stderr)
        return 2
    if not free_ports():
        print("默认端口 52117/52119/52118 被占用：先停掉别的电脑端实例", file=sys.stderr)
        return 2

    for directory in (REPORTS, OPT, SHOTS):
        directory.mkdir(parents=True, exist_ok=True)
    run_shots = SHOTS / args.label
    if run_shots.exists():
        shutil.rmtree(run_shots)
    run_shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("EVERSEND_ADB_SERIAL", serial)
    env.setdefault("EVERSEND_REAL_WORK", "/home/xgl/qa-real")
    env.setdefault("DISPLAY", ":99")
    env.setdefault("QT_QPA_PLATFORM", "xcb")

    harness_log = run_shots / "harness.log"
    print(f"启动真机用例 --only {args.only}（日志 {harness_log}）…", flush=True)
    log_handle = open(harness_log, "wb")
    proc = subprocess.Popen(
        [sys.executable, "tests/test_real_device.py", "--only", args.only,
         "--label", f"probe-{args.label}"],
        cwd=str(REPO), env=env, stdout=log_handle, stderr=subprocess.STDOUT,
    )

    samples: list[dict] = []
    started = time.monotonic()
    index = 0
    try:
        while proc.poll() is None and time.monotonic() - started < args.timeout:
            now = time.monotonic() - started
            truth = http_json("http://127.0.0.1:52119/api/state")
            entry: dict = {"t": round(now, 2), "shot": "", "truth": None}
            if truth:
                shares = [
                    {
                        "name": str(item.get("name") or ""),
                        "sentBytes": int(item.get("sentBytes") or 0),
                        "size": int(item.get("size") or 0),
                        "downloaded": bool(item.get("downloaded")),
                    }
                    for item in (truth.get("shares") or [])
                ]
                transfers = [
                    {
                        "name": str(item.get("name") or ""),
                        "done": int(item.get("done") or 0),
                        "total": int(item.get("total") or 0),
                        "percent": item.get("percent"),
                        "direction": item.get("direction"),
                    }
                    for item in (truth.get("transfers") or [])
                ]
                entry["truth"] = {"shares": shares, "transfers": transfers}
            shot = run_shots / f"{index:04d}.png"
            payload = adb(serial, "exec-out", "screencap", "-p", binary=True)
            if payload:
                shot.write_bytes(payload)
                entry["shot"] = str(shot)
            samples.append(entry)
            index += 1
            time.sleep(args.interval)
    finally:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()

    # 用例自己的报告（真机验收结论）
    harness_report = REPORTS / f"real_device_probe-{args.label}.md"
    harness_text = harness_report.read_text("utf-8", "replace") if harness_report.exists() else ""
    harness_summary = ""
    match = re.search(r"\| [A-Z0-9]+ \|.*?\n", harness_text)
    for line in harness_text.splitlines():
        if line.startswith("| ") and ("PASS" in line or "FAIL" in line):
            harness_summary += line + "\n"

    payload = {
        "label": args.label,
        "only": args.only,
        "interval": args.interval,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": {
            "adb": serial,
            "work": env.get("EVERSEND_REAL_WORK"),
            "loadavg": os.getloadavg(),
        },
        "harness_exit": proc.returncode,
        "harness_report": str(harness_report),
        "harness_summary": harness_summary,
        "samples": samples,
    }
    json_path = OPT / f"gui-probe-{args.label}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    lines = [
        f"# 真机 GUI 显示准确性探针 · {args.label}",
        "",
        f"- 时间：{payload['when']}　·　用例：`--only {args.only}`　·　harness exit={proc.returncode}",
        f"- 采样：{len(samples)} 帧 / {args.interval}s　·　截图：`{run_shots}`",
        f"- 负载：{os.getloadavg()}",
        f"- 用例报告：`{harness_report}`",
        "",
        "## 用例结论",
        "",
        "```",
        harness_summary.strip() or "（没读到用例报告）",
        "```",
        "",
        "## 时间线（真值 vs 手机截图）",
        "",
        "| t(s) | 截图 | 真值：share sentBytes / total / done | 活动 transfers |",
        "|---|---|---|---|",
    ]
    for entry in samples:
        truth = entry.get("truth") or {}
        shares = truth.get("shares") or []
        share_text = "；".join(
            f"{s['name'][:18]} {s['sentBytes']}/{s['size']}{'✓' if s['downloaded'] else ''}"
            for s in shares
        ) or "—"
        transfer_text = "；".join(
            f"{t['name'][:14]}:{t['percent']}%" for t in (truth.get("transfers") or [])
        ) or "—"
        shot_name = Path(entry["shot"]).name if entry.get("shot") else ""
        lines.append(f"| {entry['t']} | {shot_name} | {share_text} | {transfer_text} |")
    md_path = OPT / f"gui-probe-{args.label}.md"
    md_path.write_text("\n".join(lines) + "\n", "utf-8")

    print(f"探针报告：{md_path}")
    print(f"原始数据：{json_path}")
    print(f"截图目录：{run_shots}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
