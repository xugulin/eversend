#!/usr/bin/env python3
"""PG-3 证据抓取：传输中把会话列表滚到顶部，拍下「电脑发来 N 个文件」批量卡片。

背景：手机下载时 LazyColumn 会自动滚到最后一条，批量卡片（第 0 项）被滚出屏幕，
常规 GUI 探针截图覆盖不到它。本脚本在真机传输进行中（服务端 sentBytes > 0）
立刻 swipe 到顶部并截图，同时 dump UI 文本做机器可读断言。

用法：DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c \
      python3 tools/capture_batch_card.py --size-mib 100 --label v11c
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

import test_real_device as rt  # noqa: E402

OUT = REPO / "tests" / "reports" / "opt-1017" / "gui-probe-shots" / "pg3-batch"
FILES = rt.WORK / "files"


def shares_snapshot(bundle) -> list[dict]:
    try:
        return list(bundle["ui"].shares())
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=int, default=100)
    parser.add_argument("--label", default="v11c")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--shots", type=int, default=5)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(parents=True, exist_ok=True)
    source = FILES / f"B-批次卡片-{args.size_mib * 1024 * 1024}.bin"
    if not source.exists() or source.stat().st_size != args.size_mib * 1024 * 1024:
        with open(source, "wb") as handle:
            chunk = b"\xa5" * (1024 * 1024)
            for _ in range(args.size_mib):
                handle.write(chunk)

    captured: list[dict] = []
    bundle = rep = None
    try:
        print("启动电脑端…", flush=True)
        bundle = rt.start_pc(rt.PC_NAME)
        print("等手机连上…", flush=True)
        phone = rt.ensure_phone_connected(bundle, timeout=120.0)
        if phone is None:
            print("手机没连上", flush=True)
            return 2
        rt.ensure_phone_chat_page(timeout=60.0)

        window = bundle["window"]
        window.sidebar.set_devices([phone], {})
        window._select_device_row(phone)
        rt.qt_app().processEvents()
        window._add_paths([str(source)])
        rt.qt_app().processEvents()
        window.send_button.click()
        rt.qt_app().processEvents()
        print(f"已发送 {source.name}，等字节开始流动…", flush=True)

        started = time.monotonic()
        while time.monotonic() - started < args.timeout:
            items = shares_snapshot(bundle)
            share = next((s for s in items if s.get("name") == source.name), None)
            if share and int(share.get("sentBytes") or 0) > 0:
                break
            time.sleep(0.15)
        else:
            print("等不到传输开始", flush=True)
            return 3

        # 传输开始：会话列表很长（历史消息多），单次 swipe 到不了顶 —— 连滑 12 次
        # 快速滚到最顶（批量卡片是第 0 项），再截图；之后每次补滑 3 次再拍。
        for index in range(1, args.shots + 1):
            swipes = 12 if index == 1 else 3
            for _ in range(swipes):
                rt.adb("shell", "input", "swipe", "540", "450", "540", "2200", "80", timeout=20)
            time.sleep(0.2)
            shot = OUT / f"{args.label}-{index}.png"
            raw = rt.adb("exec-out", "screencap", "-p", binary=True, timeout=30)
            shot.write_bytes(raw)
            texts = [t for t in rt.phone_ui_texts() if t]
            items = shares_snapshot(bundle)
            share = next((s for s in items if s.get("name") == source.name), None)
            captured.append({
                "shot": str(shot),
                "sentBytes": int((share or {}).get("sentBytes") or 0),
                "total": int((share or {}).get("size") or 0),
                "texts": texts[:40],
                "batch_visible": any("电脑发来" in t or "正在接收" in t for t in texts),
                "progress_text": next((t for t in texts if "接收中" in t or "正在接收" in t), ""),
            })
            print(f"截图 {shot.name}: 真值 {captured[-1]['sentBytes']}/{captured[-1]['total']} "
                  f"批量卡片可见={captured[-1]['batch_visible']} 进度文本={captured[-1]['progress_text']!r}",
                  flush=True)

        report = OUT / f"batch-card-{args.label}.json"
        report.write_text(json.dumps({"label": args.label, "source": str(source),
                                      "captured": captured}, ensure_ascii=False, indent=2), "utf-8")
        md = OUT / f"batch-card-{args.label}.md"
        lines = [f"# 批量卡片（PG-3）真机证据 · {args.label}", ""]
        for item in captured:
            pct = (item["sentBytes"] / item["total"] * 100) if item["total"] else 0
            lines.append(f"- 截图 `{Path(item['shot']).name}`：PC 真值 **{pct:.0f}%**"
                         f"（{item['sentBytes']}/{item['total']}）；批量卡片可见={item['batch_visible']}；"
                         f"进度文本={item['progress_text']!r}")
        md.write_text("\n".join(lines) + "\n", "utf-8")
        print(f"报告：{md}", flush=True)
        return 0
    finally:
        try:
            if bundle is not None:
                bundle["ui"].stop()
                bundle["engine"].stop()
                bundle["window"].close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
