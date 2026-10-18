#!/usr/bin/env python3
"""真机「上传断点续传」端到端探针（opt-1017 阶段 3）。

背景：Linux 端实现了 `/api/upload` 断点续传（202/409/status + 幂等，见
`tests/reports/opt-1017/http-upload-resume-api.md`），安卓 v2+ 实现了客户端。
协议层已有 `tests/test_http_resume.py` 20 项断言全绿；本探针要证明**真机真系统**
上「手机发大文件 → 中途断网 → 自动从断点续传」这条链路。

做法：
1. 起真电脑端（复用 tests/test_real_device.py 的 start_pc / adb 驱动函数）；
2. 生成一张 ~24 MiB 的合法 PNG（DocumentsUI 的「图片」分类才列得出来），adb push 到手机；
3. 真 UI：点「📎 传输文件」→ 图片分类 → 选文件 → 发送；
4. 监听电脑端 `.eversend-uploads/resume-*` spool 增长；到达阈值后用 iptables
   REJECT 手机→电脑 52119 入向流量 4 秒再放开（模拟断网）；
5. 断言：最终电脑接收目录出现文件、sha256 与源一致；记录 spool 是否**从同一 offset
   续上**（而不是从 0 重来）、手机 logcat 的续传日志、总耗时。
6. 无论成败，finally 里删除 iptables 规则。

用法（在 eversend/ 下，需要免密 sudo 的 iptables）：

    EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real \
    DISPLAY=:99 QT_QPA_PLATFORM=xcb \
    python3 tools/realdevice_upload_resume_probe.py --size-mib 24 --label v3

注意：同一时刻只能一个电脑端实例（默认端口），跑之前确认 52117/52119 空闲、
手机屏幕常亮。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO / "src"))

OPT = REPO / "tests" / "reports" / "opt-1017"
SHOTS = OPT / "resume-probe-shots"

PC_NAME = "续传探针"


def fast_big_png(path: Path, side: int = 2400) -> str:
    """生成 side×side RGB 噪声 PNG（不可压缩，~side*side*3 字节），返回 sha256。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    compressor = zlib.compressobj(6)
    parts: list[bytes] = []
    row_bytes = side * 3
    for _ in range(side):
        parts.append(compressor.compress(b"\x00"))
        parts.append(compressor.compress(os.urandom(row_bytes)))
    parts.append(compressor.flush())
    idat = b"".join(parts)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return hashlib.sha256(png).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def spool_state(receive_dir: Path) -> tuple[int, list[tuple[str, int]]]:
    """(spool 文件总字节, [(name,size)…]) —— 续传 spool 在 .eversend-uploads/resume-*。"""
    spool_dir = receive_dir / ".eversend-uploads"
    total = 0
    items: list[tuple[str, int]] = []
    try:
        for entry in spool_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            items.append((entry.name, size))
            total += size
    except OSError:
        pass
    return total, items


def iptables_reject(phone_ip: str, port: int, enable: bool) -> None:
    rule = [
        "iptables", "-I", "INPUT", "-s", phone_ip, "-p", "tcp", "--dport", str(port),
        "-j", "REJECT",
    ] if enable else [
        "iptables", "-D", "INPUT", "-s", phone_ip, "-p", "tcp", "--dport", str(port),
        "-j", "REJECT",
    ]
    subprocess.run(["sudo", "-n", *rule], capture_output=True, timeout=15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机上传断点续传端到端探针")
    parser.add_argument("--size-mib", type=float, default=24.0, help="测试 PNG 实测大小（约）")
    parser.add_argument("--label", default="resume", help="报告/截图前缀")
    parser.add_argument("--cut-fraction", type=float, default=0.35, help="传到此比例时断网")
    parser.add_argument("--cut-seconds", type=float, default=4.0, help="断网时长")
    args = parser.parse_args(argv)

    serial = os.environ.get("EVERSEND_ADB_SERIAL", "")
    if not serial:
        print("缺少 EVERSEND_ADB_SERIAL", file=sys.stderr)
        return 2
    OPT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    import test_real_device as rt  # noqa: E402  (需要 env 已就绪)

    if not all(rt.ports_free().values()):
        print("默认端口被占用，先停掉其它电脑端实例", file=sys.stderr)
        return 2
    phone_ip = ""
    try:
        match = __import__("re").search(r"inet (\d+\.\d+\.\d+\.\d+)/", rt.adb("shell", "ip", "addr", "show", "wlan0"))
        phone_ip = match.group(1) if match else ""
    except Exception:
        pass
    if not phone_ip:
        print("读不到手机 wlan0 IP", file=sys.stderr)
        return 2
    print(f"手机 IP={phone_ip} 目标大小≈{args.size_mib} MiB")

    bundle = rt.start_pc(PC_NAME)
    rt._PC["bundle"] = bundle
    receive_dir = Path(bundle["receive_dir"])
    timeline: list[dict] = []
    cut_done = False
    rule_on = False
    result: dict = {"label": args.label, "phone_ip": phone_ip, "size_mib": args.size_mib}
    source = Path("/home/xgl/qa-real/files") / f"resume-{args.label}.png"
    try:
        rt.ensure_phone_app_ready()
        peer = rt.ensure_phone_connected(bundle)
        result["connected"] = peer is not None
        if peer is None:
            print("手机没连上电脑端", file=sys.stderr)
            return 3
        rt.ensure_phone_chat_page()

        # 生成目标大小的 PNG：side≈sqrt(bytes/3)
        side = max(256, int((args.size_mib * 1024 * 1024 / 3) ** 0.5))
        digest = fast_big_png(source, side)
        total = source.stat().st_size
        result.update({"file": source.name, "size": total, "sha256": digest, "png_side": side})
        print(f"测试文件 {source.name} {total/2**20:.1f} MiB sha256={digest[:16]}…")

        rt.adb("shell", "rm", "-f", f"{rt.PHONE_DOWNLOAD}/{source.name}", timeout=30)
        rt.adb("push", str(source), f"{rt.PHONE_DOWNLOAD}/{source.name}", timeout=300)
        rt.adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
               "-d", f"file://{rt.PHONE_DOWNLOAD}/{source.name}", timeout=30)
        time.sleep(1.0)

        nodes = rt.phone_ui()
        candidates = [n for n in nodes if n.text and "传输文件" in n.text]
        if not candidates:
            print("找不到「传输文件」按钮", file=sys.stderr)
            return 3
        rt.phone_tap(*max(candidates, key=lambda n: n.bounds[1]).center)
        time.sleep(2.0)
        chip = next((n for n in rt.phone_ui() if n.text == "图片"), None)
        if chip is not None:
            rt.phone_tap(*chip.center)
            time.sleep(2.5)
        picked = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = next((n for n in rt.phone_ui() if n.text == source.name), None)
            if row is not None:
                rt.phone_tap(*row.center)
                picked = True
                break
            time.sleep(1.0)
        result["picked"] = picked
        if not picked:
            print("没在选择器里选中文件", file=sys.stderr)
            return 3
        time.sleep(2.5)
        nodes = rt.phone_ui()
        if any(n.text == "选择" for n in nodes):
            rt.phone_tap_label(nodes, "选择")
            time.sleep(2.0)
        staged = any(source.name in (t or "") for t in rt.phone_ui_texts())
        result["staged"] = staged
        if not staged:
            print("文件没进待发区", file=sys.stderr)
            return 3

        # 只勾目标设备
        for _ in range(5):
            nodes = rt.phone_ui()
            rows = [n for n in nodes if n.text and n.text.strip().startswith(("☐", "☑"))]
            if not rows:
                break
            target = next((n for n in rows if PC_NAME in n.text), None)
            wrong = [n for n in rows if n.text.strip().startswith("☑") and PC_NAME not in n.text]
            target_ok = target is not None and target.text.strip().startswith("☑")
            if target_ok and not wrong:
                break
            if wrong:
                rt.phone_tap(*wrong[0].center)
            elif target is not None:
                rt.phone_tap(*target.center)
            time.sleep(1.2)

        before = set(p.name for p in receive_dir.iterdir()) if receive_dir.exists() else set()
        sent = rt.phone_tap_label(rt.phone_ui(), "发送")
        result["sent_clicked"] = sent is not None
        if not sent:
            print("没点到发送", file=sys.stderr)
            return 3
        send_at = time.monotonic()
        print("已点发送，开始监听续传 spool …")

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - send_at
            spool_bytes, items = spool_state(receive_dir)
            timeline.append({"t": round(elapsed, 2), "spool": spool_bytes,
                             "items": items[-2:] if items else []})
            if not cut_done and spool_bytes >= total * args.cut_fraction:
                print(f"  {elapsed:.1f}s spool={spool_bytes}/{total}，制造断网 {args.cut_seconds}s")
                iptables_reject(phone_ip, rt.PC_WEB_PORT, True)
                rule_on = True
                cut_done = True
                time.sleep(args.cut_seconds)
                iptables_reject(phone_ip, rt.PC_WEB_PORT, False)
                rule_on = False
                print("  断网结束，观察是否从断点续上")
            landed = sorted(receive_dir.glob(f"{source.stem}*{source.suffix}"))
            if landed and landed[-1].stat().st_size == total:
                time.sleep(0.5)
                result["landed"] = str(landed[-1])
                result["landed_sha256"] = sha256_file(landed[-1])
                result["elapsed_s"] = round(time.monotonic() - send_at, 2)
                result["resume_ok"] = result["landed_sha256"] == digest
                print(f"  落地 {landed[-1].name} sha256={'一致' if result['resume_ok'] else '不一致'} "
                      f"耗时 {result['elapsed_s']}s")
                break
            time.sleep(0.25)
        else:
            result["landed"] = ""
            result["resume_ok"] = False

        try:
            logs = rt.adb("logcat", "-d", "-s", "EverSend:V")
            result["app_log"] = [
                line for line in logs.splitlines()
                if any(key in line for key in ("上传", "续传", "偏移", "已发给", "发送失败"))
            ][-20:]
        except Exception:
            result["app_log"] = []
        try:
            bundle["window"].grab().save(str(SHOTS / f"{args.label}-desktop.png"), "PNG")
        except Exception:
            pass
        rt.phone_screenshot(SHOTS / f"{args.label}-phone.png")
    finally:
        if rule_on:
            iptables_reject(phone_ip, rt.PC_WEB_PORT, False)
        result["timeline"] = timeline
        result["cut_done"] = cut_done
        json_path = OPT / f"resume-probe-{args.label}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        lines = [
            f"# 真机上传断点续传探针 · {args.label}",
            "",
            f"- 文件：{result.get('file')} {result.get('size', 0)/2**20:.1f} MiB　·　sha256={result.get('sha256','')[:16]}…",
            f"- 断网：传至 {int(args.cut_fraction*100)}% 时 REJECT 手机→电脑 {args.cut_seconds:.0f}s",
            f"- 结果：**{'PASS' if result.get('resume_ok') else 'FAIL'}**（落地 sha256 {'一致' if result.get('resume_ok') else '不一致'}，耗时 {result.get('elapsed_s')}s）",
            "",
            "## spool 时间线（续传证据：断网后从同一 offset 继续，而不是从 0）",
            "",
            "| t(s) | spool 字节 | 文件 |",
            "|---|---|---|",
        ]
        for item in timeline[::max(1, len(timeline)//40)]:
            names = "；".join(f"{n}:{s}" for n, s in item["items"])
            lines.append(f"| {item['t']} | {item['spool']} | {names} |")
        if result.get("app_log"):
            lines += ["", "## 手机日志（续传相关）", "", "```", *result["app_log"], "```"]
        (OPT / f"resume-probe-{args.label}.md").write_text("\n".join(lines) + "\n", "utf-8")
        try:
            bundle["ui"].stop()
            bundle["engine"].stop()
            bundle["window"].close()
        except Exception:
            pass
        print(f"报告：{OPT / ('resume-probe-' + args.label + '.md')}")
    return 0 if result.get("resume_ok") else 1


if __name__ == "__main__":
    sys.exit(main())
