#!/usr/bin/env python3
"""可续传上传（opt-1017）HTTP 回归测试。

背景：手机→电脑的大文件以前是"一次 POST 传完"，97MB 传到 73MiB 断网就全废，
重发从 0 开始。opt-1017 给 ``/api/upload`` 加了**断点续传**：

* 客户端生成 ``uploadId``（16-64 位十六进制），请求带
  ``?uploadId=&offset=<已确认字节>``，请求头 ``X-EverSend-Total: <文件总字节>``，
  body 是"从 offset 起的剩余字节"；
* 服务端把字节追加到固定名字的 spool，返回
  ``202 {"complete": false, "offset": <服务端已有>}``；收满 total 才定稿；
* 偏移对不上回 ``409 {"offset": <服务端已有>}``，客户端据此重发；
* 已经完成的 uploadId 重发最后一段 → 幂等返回原响应，不会重复落盘；
* **没有 uploadId 的旧客户端完全走原路径**（回归保护）。

Run from the repository root::

    PYTHONPATH=src python3 -u tests/test_http_resume.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.web import create_web_ui  # noqa: E402
from eversend.web.server import TOKEN_HEADER  # noqa: E402

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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cast(
    base: str,
    token: str,
    *,
    name: str,
    device_id: str,
    data: bytes,
    upload_id: str = "",
    offset: int | None = None,
    total: int | None = None,
) -> tuple[int, dict]:
    """One ``/api/upload`` POST; returns (status, parsed JSON)."""
    query: dict[str, str] = {"name": name, "deviceId": device_id}
    if upload_id:
        query["uploadId"] = upload_id
        query["offset"] = str(offset or 0)
    url = base + "/api/upload?" + urllib.parse.urlencode(query)
    headers = {
        TOKEN_HEADER: token,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
    }
    if total is not None:
        headers["X-EverSend-Total"] = str(total)
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except ValueError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


def get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="http-resume-", dir=str(REPO / ".testscratch")))
    engine = None
    ui = None
    try:
        engine = Engine(
            EngineConfig(
                data_dir=str(root / "state"),
                receive_dir=str(root / "recv"),
                name="ResumeTest",
                tcp_port=0,
                discovery_port=free_port(),
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
                auto_accept_all=True,
            )
        )
        engine.start()
        ui = create_web_ui(engine, host="127.0.0.1", port=0, keep_uploads=True)
        port = ui.start()
        base = f"http://127.0.0.1:{port}"
        token = ui.token
        own_id = str(engine.info.device_id)
        receive = Path(engine.config.receive_dir)
        receive.mkdir(parents=True, exist_ok=True)

        # ---- 0. 能力宣告：客户端据此决定启用续传 ---------------------------
        print("\n[0] 能力宣告")
        state = get_json(base + "/api/state")
        caps = state.get("caps") or {}
        check("0.1 /api/state 宣告 caps.uploadResume=true", caps.get("uploadResume") is True, str(caps))
        check(
            "0.2 宣告 caps.uploadMaxBytes 为正整数",
            isinstance(caps.get("uploadMaxBytes"), int) and caps["uploadMaxBytes"] > 0,
            str(caps),
        )
        check("0.3 旧字段还在（device/transfers/shares）",
              all(k in state for k in ("device", "transfers", "shares")), str(sorted(state))[:120])

        # ---- 1. 三段续传：模拟两次断线 -----------------------------------
        print("\n[1] 分段续传 happy path")
        source = root / "source.bin"
        payload = os.urandom(2 * 1024 * 1024 + 12345)
        source.write_bytes(payload)
        total = len(payload)
        digest = sha256_file(source)
        resume_id = secrets.token_hex(16)
        name = "续传-三段测.bin"

        seg1 = payload[: 500 * 1024]
        status, body = cast(base, token, name=name, device_id=own_id, data=seg1,
                            upload_id=resume_id, offset=0, total=total)
        check("1.1 第一段返回 202 且未完成", status == 202 and body.get("complete") is False,
              f"status={status} body={body}")
        check("1.2 服务端报告已收到 500KiB", body.get("offset") == len(seg1), f"offset={body.get('offset')}")

        seg2_start = len(seg1)
        seg2 = payload[seg2_start : seg2_start + 1024 * 1024]
        status, body = cast(base, token, name=name, device_id=own_id, data=seg2,
                            upload_id=resume_id, offset=seg2_start, total=total)
        check("1.3 第二段续传返回 202 且未完成", status == 202 and body.get("complete") is False,
              f"status={status} body={body}")
        check("1.4 offset 单调递增", body.get("offset") == seg2_start + len(seg2),
              f"offset={body.get('offset')} expected={seg2_start + len(seg2)}")

        seg3_start = seg2_start + len(seg2)
        seg3 = payload[seg3_start:]
        status, body = cast(base, token, name=name, device_id=own_id, data=seg3,
                            upload_id=resume_id, offset=seg3_start, total=total)
        check("1.5 收满后返回完成", status in (200, 202) and body.get("complete") is True,
              f"status={status} body={body}")
        check("1.6 完成响应带 resumeId", body.get("resumeId") == resume_id, str(body.get("resumeId")))
        landed = receive / name
        check("1.7 文件落到接收目录", landed.exists(), str(list(receive.name for _ in [])))
        check("1.8 续传拼接后 sha256 与源一致", landed.exists() and sha256_file(landed) == digest,
              f"landed={sha256_file(landed) if landed.exists() else '缺'} want={digest}")
        check("1.9 spool 已清理（resume- 开头文件不再占位）",
              not [p.name for p in (receive / ".eversend-uploads").glob("resume-*")],
              str(list((receive / ".eversend-uploads").glob("*"))[:5])
              if (receive / ".eversend-uploads").exists() else "")

        # ---- 2. 偏移对不上 → 409 + 正确 offset ----------------------------
        print("\n[2] 偏移不匹配的恢复")
        resume_id2 = secrets.token_hex(16)
        name2 = "偏移-恢复.bin"
        first = payload[: 300 * 1024]
        status, body = cast(base, token, name=name2, device_id=own_id, data=first,
                            upload_id=resume_id2, offset=0, total=total)
        check("2.1 先传 300KiB", status == 202 and body.get("offset") == len(first), f"status={status}")
        # 故意报错偏移：客户端以为从 0 重来，服务端已有 300KiB
        status, body = cast(base, token, name=name2, device_id=own_id, data=payload[: 100 * 1024],
                            upload_id=resume_id2, offset=0, total=total)
        check("2.2 偏移不符返回 409", status == 409, f"status={status} body={body}")
        check("2.3 409 告诉客户端服务端真实偏移", body.get("offset") == len(first),
              f"offset={body.get('offset')}")
        server_offset = int(body.get("offset") or 0)
        rest = payload[server_offset:]
        status, body = cast(base, token, name=name2, device_id=own_id, data=rest,
                            upload_id=resume_id2, offset=server_offset, total=total)
        check("2.4 按 409 的 offset 重发后完成", status in (200, 202) and body.get("complete") is True,
              f"status={status} body={body}")
        landed2 = receive / name2
        check("2.5 最终文件 sha256 一致", landed2.exists() and sha256_file(landed2) == digest,
              str(sha256_file(landed2) if landed2.exists() else "缺"))

        # ---- 3. 幂等重放：完成后重发最后一段不重复落盘 --------------------
        print("\n[3] 完成后的幂等重放")
        before = sorted(p.name for p in receive.iterdir() if p.is_file())
        status, body = cast(base, token, name=name, device_id=own_id, data=b"",
                            upload_id=resume_id, offset=total, total=total)
        after = sorted(p.name for p in receive.iterdir() if p.is_file())
        check("3.1 重发返回完成", status in (200, 202) and body.get("complete") is True,
              f"status={status} body={body}")
        check("3.2 接收目录没有多出重复文件", before == after, f"before={before} after={after}")

        # ---- 4. 旧路径（无 uploadId）完全不变 -----------------------------
        print("\n[4] 旧客户端回归（无 uploadId）")
        legacy = os.urandom(256 * 1024)
        legacy_name = "旧路径-一次性.bin"
        status, body = cast(base, token, name=legacy_name, device_id=own_id, data=legacy)
        landed3 = receive / legacy_name
        check("4.1 旧一次性上传仍成功", status in (200, 202) and landed3.exists(),
              f"status={status} body={body}")
        check("4.2 旧路径内容 sha256 一致",
              landed3.exists() and sha256_file(landed3) == hashlib.sha256(legacy).hexdigest())
        check("4.3 旧路径响应不带续传字段（不改变旧客户端语义）", "resumeId" not in body, str(body))

        # ---- 5. 非法 uploadId / 缺 total 被拒 -----------------------------
        print("\n[5] 参数校验")
        status, body = cast(base, token, name="bad.bin", device_id=own_id, data=b"x",
                            upload_id="../../etc/passwd", offset=0, total=1)
        check("5.1 非法 uploadId 返回 400", status == 400, f"status={status} body={body}")
        status, body = cast(base, token, name="bad2.bin", device_id=own_id, data=b"x",
                            upload_id=secrets.token_hex(16), offset=0, total=None)
        check("5.2 续传缺 X-EverSend-Total 返回 400", status == 400, f"status={status} body={body}")
        status, body = cast(base, token, name="bad3.bin", device_id=own_id, data=b"xx",
                            upload_id=secrets.token_hex(16), offset=0, total=1)
        check("5.3 声明总长 < body 返回 400", status == 400, f"status={status} body={body}")

        # ---- 6. 状态查询接口（断线后客户端问"服务端已有几字节"）----------
        print("\n[6] /api/upload/status")
        resume_id3 = secrets.token_hex(16)
        first3 = payload[: 128 * 1024]
        cast(base, token, name="状态查询.bin", device_id=own_id, data=first3,
             upload_id=resume_id3, offset=0, total=total)
        status_info = get_json(base + "/api/upload/status?uploadId=" + resume_id3)
        check("6.1 状态查询返回服务端已有字节", status_info.get("offset") == len(first3),
              str(status_info))
        check("6.2 未完成时 complete=false", status_info.get("complete") is False, str(status_info))
        done_state = get_json(base + "/api/upload/status?uploadId=" + resume_id)
        check("6.3 已完成的 uploadId 查得到完成态",
              done_state.get("complete") is True and done_state.get("offset") == total,
              str(done_state))
        unknown = get_json(base + "/api/upload/status?uploadId=" + secrets.token_hex(16))
        check("6.4 未知 uploadId 返回 offset=0", unknown.get("offset") == 0, str(unknown))

        # ---- 7. 进程重启：完成快照落盘 → 幂等重放不重复落盘 --------------
        print("\n[7] 进程重启后的幂等重放（完成快照落盘）")
        resume_id4 = secrets.token_hex(16)
        name4 = "重启-已完成.bin"
        first4 = payload[: 400 * 1024]
        cast(base, token, name=name4, device_id=own_id, data=first4,
             upload_id=resume_id4, offset=0, total=total)
        rest4 = payload[len(first4):]
        status, body = cast(base, token, name=name4, device_id=own_id, data=rest4,
                            upload_id=resume_id4, offset=len(first4), total=total)
        check("7.1 重启前完成上传", status in (200, 202) and body.get("complete") is True,
              f"status={status} body={body}")
        done_file = receive / name4
        check("7.2 重启前文件已落地且 sha256 一致",
              done_file.exists() and sha256_file(done_file) == digest,
              str(done_file.exists()))

        # 模拟电脑端进程重启：同一个 engine / data_dir 上重新创建一个 WebUI。
        ui.stop()
        ui = create_web_ui(engine, host="127.0.0.1", port=0, keep_uploads=True)
        port = ui.start()
        base = f"http://127.0.0.1:{port}"
        token = ui.token
        files_before = sorted(p.name for p in receive.iterdir() if p.is_file())
        status, body = cast(base, token, name=name4, device_id=own_id, data=b"",
                            upload_id=resume_id4, offset=total, total=total)
        files_after = sorted(p.name for p in receive.iterdir() if p.is_file())
        check("7.3 重启后重放最后一段仍返回完成",
              status in (200, 202) and body.get("complete") is True,
              f"status={status} body={body}")
        check("7.4 重启后重放不再落重复文件（不出现 (1)）", files_before == files_after,
              f"before={files_before[-3:]} after={files_after[-3:]}")

        # 半途重启：spool 已收字节必须保留，客户端按服务端 offset 继续。
        resume_id5 = secrets.token_hex(16)
        name5 = "重启-半途.bin"
        half = 600 * 1024
        cast(base, token, name=name5, device_id=own_id, data=payload[:half],
             upload_id=resume_id5, offset=0, total=total)
        ui.stop()
        ui = create_web_ui(engine, host="127.0.0.1", port=0, keep_uploads=True)
        port = ui.start()
        base = f"http://127.0.0.1:{port}"
        token = ui.token
        status_info = get_json(base + "/api/upload/status?uploadId=" + resume_id5)
        check("7.5 重启后仍查得到已收字节", status_info.get("offset") == half, str(status_info))
        status, body = cast(base, token, name=name5, device_id=own_id,
                            data=payload[: 100 * 1024], upload_id=resume_id5,
                            offset=0, total=total)
        check("7.6 重启后用旧偏移续传返回 409", status == 409,
              f"status={status} body={body}")
        offset5 = int(body.get("offset") or 0)
        status, body = cast(base, token, name=name5, device_id=own_id,
                            data=payload[offset5:], upload_id=resume_id5,
                            offset=offset5, total=total)
        landed5 = receive / name5
        check("7.7 重启后按正确 offset 续传完成且 sha256 一致",
              status in (200, 202) and body.get("complete") is True
              and landed5.exists() and sha256_file(landed5) == digest,
              f"status={status} landed={landed5.exists()}")
        done_snapshot = root / "state" / "resume_done.json"
        check("7.8 完成快照已落盘（下次重启仍可幂等重放）", done_snapshot.exists(),
              str(done_snapshot))

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
