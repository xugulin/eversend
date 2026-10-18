#!/usr/bin/env python3
"""分享列表持久化回归测试（opt-1017 补丁）。

背景：手机下载大文件走 HTTP Range 断点续传。电脑端进程一重启，原来的分享表只在
内存里 → 手机带着 Range 回来续传时服务端回 404「这个文件已经不在分享列表里了」，
用户看到的是"传到一半白传"。修复：分享表原子落盘到 ``data_dir/shares.json``，
启动时恢复，并只保留文件仍存在的条目。

本测试起**真实** WebUI（不 mock），验证：
1. 分享 → 落盘 → 同一 data_dir 重新构造 WebUI（模拟进程重启）→ 分享 id 仍在、
   文件大小正确、GET /api/share/<id> 可用、带 Range 的续传返回 206 + 正确尾部字节；
2. 文件被删除后重启 → 该条目被丢弃（不会给手机一个"已失效但还在列表里"的死链接）；
3. clear_shares() 之后重启 → 分享表为空（用户清空要真的清干净）。

Run from the repository root::

    PYTHONPATH=src python3 -u tests/test_share_persistence.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.web import create_web_ui  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f"  ← {detail}" if detail else ""))
        _failures.append(name)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0):
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get_json(url: str) -> dict:
    status, _headers, body = get(url)
    return json.loads(body) if body else {}


def make_ui(engine, data_dir: Path):
    ui = create_web_ui(engine, host="127.0.0.1", port=0, keep_uploads=True)
    port = ui.start()
    return ui, f"http://127.0.0.1:{port}"


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="share-persist-", dir=str(REPO / ".testscratch")))
    engine = None
    ui = None
    try:
        data_dir = root / "state"
        receive_dir = root / "recv"
        engine = Engine(
            EngineConfig(
                data_dir=str(data_dir),
                receive_dir=str(receive_dir),
                name="SharePersistTest",
                tcp_port=0,
                discovery_port=free_port(),
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
                auto_accept_all=True,
            )
        )
        engine.start()
        receive_dir.mkdir(parents=True, exist_ok=True)

        source = root / "分享测试.bin"
        payload = os.urandom(1024 * 1024 + 4321)
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        # ---- 1. 第一次进程：分享 + 落盘 -----------------------------------
        print("\n[1] 分享并落盘")
        ui, base = make_ui(engine, data_dir)
        entries = ui.share_files([str(source)])
        check("1.1 分享成功", len(entries) == 1, str(entries))
        share_id = str(entries[0]["id"]) if entries else ""
        shares_file = data_dir / "shares.json"
        check("1.2 shares.json 已落盘", shares_file.exists(), str(shares_file))
        if shares_file.exists():
            saved = json.loads(shares_file.read_text("utf-8"))
            check("1.3 落盘包含该分享 id", any(s.get("id") == share_id for s in saved.get("shares", [])),
                  str(saved)[:200])
        status, _headers, body = get(base + "/api/share/" + share_id)
        check("1.4 重启前整文件下载正常", status == 200 and hashlib.sha256(body).hexdigest() == digest,
              f"status={status} len={len(body)}")
        ui.stop()
        ui = None

        # ---- 2. 模拟进程重启：同一 data_dir 新 WebUI -----------------------
        print("\n[2] 模拟电脑端重启后续传")
        ui, base = make_ui(engine, data_dir)
        state = get_json(base + "/api/state")
        recovered = {s.get("id"): s for s in state.get("shares", [])}
        check("2.1 重启后分享 id 仍在列表里", share_id in recovered, str(list(recovered))[:200])
        check("2.2 大小正确", recovered.get(share_id, {}).get("size") == len(payload),
              str(recovered.get(share_id)))
        offset = len(payload) // 2
        status, headers, body = get(
            base + "/api/share/" + share_id,
            headers={"Range": f"bytes={offset}-"},
        )
        check("2.3 Range 续传返回 206", status == 206, f"status={status}")
        check("2.4 续传尾部字节与源一致（sha256）",
              hashlib.sha256(body).hexdigest() == hashlib.sha256(payload[offset:]).hexdigest(),
              f"len={len(body)} want={len(payload) - offset}")
        check("2.5 Content-Range 正确",
              headers.get("Content-Range") == f"bytes {offset}-{len(payload) - 1}/{len(payload)}",
              str(headers.get("Content-Range")))
        ui.stop()
        ui = None

        # ---- 3. 文件被删 → 重启后条目被丢弃 --------------------------------
        print("\n[3] 文件不存在时丢弃条目")
        source.unlink()
        ui, base = make_ui(engine, data_dir)
        state = get_json(base + "/api/state")
        check("3.1 文件被删后分享表不再列出它", share_id not in {s.get("id") for s in state.get("shares", [])},
              str([s.get("id") for s in state.get("shares", [])])[:200])
        status, _headers, body = get(base + "/api/share/" + share_id)
        check("3.2 取被删文件的分享返回 404", status == 404, f"status={status} body={body[:120]}")
        ui.stop()
        ui = None

        # ---- 4. clear_shares 也要落盘 -------------------------------------
        print("\n[4] 清空分享表")
        source2 = root / "第二条.bin"
        source2.write_bytes(os.urandom(2048))
        ui, base = make_ui(engine, data_dir)
        second = ui.share_files([str(source2)])
        check("4.1 重新分享成功", len(second) == 1, str(second))
        ui.clear_shares()
        ui.stop()
        ui = None
        ui, base = make_ui(engine, data_dir)
        state = get_json(base + "/api/state")
        check("4.2 清空后重启仍为空", state.get("shares") == [], str(state.get("shares"))[:200])
        ui.stop()
        ui = None

        # ---- 5. 聊天消息要带上分享 id（手机 PG-1/PG-2 修复的前提） ----------
        print("\n[5] /api/chat 下发 shareId")
        source3 = root / "聊天分享.bin"
        source3.write_bytes(os.urandom(4096))
        ui, base = make_ui(engine, data_dir)
        shared3 = ui.share_files([str(source3)])
        share_id3 = str(shared3[0]["id"]) if shared3 else ""
        conv_id = "d:testconv|web:unit"
        engine.chat.upsert_conversation(conv_id, title="单元测试", members=["testconv", "web:unit"])
        engine.chat.add_message(
            conv_id,
            sender="testconv",
            sender_name="单元测试",
            kind="file",
            media_name=source3.name,
            media_size=source3.stat().st_size,
            media_source=str(source3),
            direction="out",
            state="sent",
            record=False,
        )
        status, _headers, body = get(base + "/api/chat?" + "conv=" + conv_id)
        payload = json.loads(body) if body else {}
        messages = payload.get("messages") or []
        target = next((m for m in messages if m.get("mediaName") == source3.name), None)
        check("5.1 消息里带上了服务端算出的 shareId",
              target is not None and target.get("shareId") == share_id3,
              str(target)[:220])
        check("5.2 本机 mediaSource 路径没有下发给手机",
              target is not None and "mediaSource" not in target,
              str(sorted((target or {}).keys()))[:220])
        ui.stop()
        ui = None

        print("\n" + "=" * 60)
        if _failures:
            print(f"{FAIL} {len(_failures)} 项失败：")
            for item in _failures:
                print("   -", item)
            return 1
        print(f"{PASS} 全部通过")
        return 0
    finally:
        try:
            if ui is not None:
                ui.stop()
        except Exception:
            pass
        try:
            if engine is not None:
                engine.stop()
        except Exception:
            pass
        time.sleep(0.2)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
