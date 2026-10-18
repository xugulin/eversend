#!/usr/bin/env python3
"""传输记录账本的自检脚本：先复现「同一个文件占两行」，再验收修好之后的行为。

两种用法
--------
1. **回归自检（默认）** —— 在一个临时 ``EVERSEND_HOME`` 里把用户投诉的场景重放一遍：

       .venv/bin/python tools/records_data_check.py

   每一项都是"能先失败"的断言。修之前跑 → 红（红的就是用户看到的那两行）；修之后
   跑 → 全绿。脚本自己打印红/绿，退出码 0 表示全绿。

2. **真实数据自检 / 修复** —— 扫一份真实的数据目录（chat.db + 三个身份 JSON）：

       .venv/bin/python tools/records_data_check.py --data-dir data            # 只看
       .venv/bin/python tools/records_data_check.py --data-dir data --apply    # 真修（先备份）

判据（宪章 C7/C8）
------------------
* 去重键 = ``(direction, peer_identity, content_identity)``；同键的行**永远只有一行**。
* 同一次投递的"进行中"行与"已完成"行收敛成一行（``attempts`` 仍是 1）。
* 重复发送同一个文件 = 同一行 + ``attempts`` 累加（宪章 C8：合并成一行并累加）。
* 保留策略：默认最近 500 条 / 30 天（常量在 ``core/constants.py``，可配）。
* ``清空记录`` 一次清干净：记录、chunkstore journal、孤儿 ``.part``、空会话、空设备行。

注意：本脚本只用 ``EVERSEND_HOME`` 指定的临时目录，不碰仓库里的 ``data/``
（``--data-dir`` 是显式指定的，且 ``--apply`` 前会先备份）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from eversend.core import chat as chat_mod  # noqa: E402
from eversend.core.chat import ChatStore, direct_conversation_id  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []
_checks = 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  [{detail}]"))
    if not condition:
        _failures.append(name)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n[{title}]")


def tmpdir(prefix: str) -> Path:
    base = os.environ.get("EVERSEND_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


# ---------------------------------------------------------------------------
# 1. 去重键
# ---------------------------------------------------------------------------


def test_record_key() -> None:
    section("1 去重键：确定性 + 只认身份"
    )
    key_of = getattr(chat_mod, "transfer_record_key", None)
    if key_of is None:
        check("transfer_record_key() 存在", False, "core/chat.py 还没有这个函数")
        return
    a = key_of("out", "android:phone-1", "d:abc123")
    b = key_of("out", "android:phone-1", "d:abc123")
    check("同样的输入得到同一个键", a == b, f"{a} != {b}")
    check("方向不同就是不同的键", a != key_of("in", "android:phone-1", "d:abc123"))
    check("对端不同就是不同的键", a != key_of("out", "android:phone-2", "d:abc123"))
    check("内容不同就是不同的键", a != key_of("out", "android:phone-1", "d:def456"))
    check("键里不出现原始身份（长度固定、可入库）", len(a) == 32 and "phone-1" not in a, a)


# ---------------------------------------------------------------------------
# 2/3/4/5. 账本：一次投递一行、重发累加、同名不同内容不合并
# ---------------------------------------------------------------------------


def _write(store, conv, path, state, **extra):
    """走"传输记录"这一条路写一行（没有新 API 时退回老的 add_message）。"""
    record = getattr(store, "record_transfer", None)
    if record is not None:
        return record(
            conv,
            direction="out",
            peer="android:phone-1",
            path=str(path),
            name=path.name,
            size=path.stat().st_size,
            state=state,
            **extra,
        )
    return store.add_message(
        conv,
        kind="file",
        media_name=path.name,
        media_size=path.stat().st_size,
        media_source=str(path),
        direction="out",
        sender="me",
        state=state,
        text="",
        **{k: v for k, v in extra.items() if k in ("media_rel", "media_mime", "duration_ms")},
    )


def test_one_delivery_one_row() -> None:
    section("2 一次投递：进行中 + 已完成 ⇒ 一行")
    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            conv = direct_conversation_id("me", "android:phone-1")
            store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
            payload = root + "/报告.pdf"
            Path(payload).write_bytes(b"%PDF-1.4 " + b"x" * 4096)
            _write(store, conv, Path(payload), "sending")
            _write(store, conv, Path(payload), "sent")
            rows = store.messages(conv, limit=200)
            check("一个文件只占一行（进行中与已完成收敛）", len(rows) == 1,
                  f"记录里 {len(rows)} 行：" + "、".join(f"{r['mediaName']}/{r['state']}" for r in rows))
            if len(rows) == 1:
                check("留下的是完成态", rows[0]["state"] == "sent", rows[0]["state"])
                check("同一次投递不算重复发送（attempts=1）",
                      int(rows[0].get("attempts") or 1) == 1, str(rows[0].get("attempts")))
        finally:
            store.close()


def test_repeat_send_merges() -> None:
    section("3 重复发送同一个文件：一行 + 次数累加")
    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            conv = direct_conversation_id("me", "android:phone-1")
            store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
            payload = root + "/假期.jpg"
            Path(payload).write_bytes(b"\xff\xd8\xff" + b"y" * 2048)
            for _ in range(2):
                _write(store, conv, Path(payload), "sending")
                _write(store, conv, Path(payload), "sent")
            rows = store.messages(conv, limit=200)
            check("发两次仍然只有一行", len(rows) == 1,
                  f"{len(rows)} 行：" + "、".join(f"{r['mediaName']}/{r.get('attempts')}" for r in rows))
            if len(rows) == 1:
                check("次数累加到 2", int(rows[0].get("attempts") or 0) == 2, str(rows[0].get("attempts")))
                check("状态是最后一次的结果", rows[0]["state"] == "sent", rows[0]["state"])
        finally:
            store.close()


def test_same_name_different_bytes() -> None:
    section("4 同名不同内容：不许合并（合并就是丢文件）")
    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            conv = direct_conversation_id("me", "android:phone-1")
            store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
            one = Path(root) / "a" / "报告.pdf"
            two = Path(root) / "b" / "报告.pdf"
            one.parent.mkdir(parents=True, exist_ok=True)
            two.parent.mkdir(parents=True, exist_ok=True)
            one.write_bytes(b"A" * 1024)
            two.write_bytes(b"B" * 1024)
            _write(store, conv, one, "sent")
            _write(store, conv, two, "sent")
            rows = store.messages(conv, limit=200)
            check("两个内容不同的同名文件各占一行", len(rows) == 2,
                  f"{len(rows)} 行：" + "、".join(str(r.get("attempts")) for r in rows))
        finally:
            store.close()


def test_legacy_double_write_converges() -> None:
    section("5 老写入路径（add_message 两连写）也不再出现两行")
    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            conv = direct_conversation_id("me", "android:phone-1")
            store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
            payload = root + "/安装包.apk"
            Path(payload).write_bytes(b"PK\x03\x04" + b"z" * 8192)
            # 这就是"传输页那一行"+"会话里那条记录"今天各自写一遍的样子：
            # 两个不同的 message id、同一个文件、同一台设备。
            common = dict(
                kind="file", media_name="安装包.apk", media_size=8188,
                media_source=payload, direction="out", sender="me", text="",
            )
            store.add_message(conv, state="sending", **common)
            store.add_message(conv, state="sent", **common)
            rows = store.messages(conv, limit=200)
            check("同一次投递不会留下两行", len(rows) == 1,
                  f"{len(rows)} 行：" + "、".join(f"{r['state']}" for r in rows))
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 6. 保留策略
# ---------------------------------------------------------------------------


def test_retention() -> None:
    section("6 保留策略：最近 500 条 / 30 天")
    from eversend.core import constants

    check("默认条数常量是 500", int(getattr(constants, "RECORD_KEEP_COUNT", 0)) == 500,
          str(getattr(constants, "RECORD_KEEP_COUNT", None)))
    check("默认天数是 30", float(getattr(constants, "RECORD_KEEP_DAYS", 0)) == 30.0,
          str(getattr(constants, "RECORD_KEEP_DAYS", None)))

    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            conv = direct_conversation_id("me", "android:phone-1")
            store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
            now = time.time()
            db = store._db  # noqa: SLF001 - 这一节就是在造历史数据
            for index in range(620):
                db.execute(
                    "INSERT INTO messages (id, conv, sender, sender_name, kind, text, media_name,"
                    " media_rel, media_size, media_mime, media_source, duration_ms, ts, direction, state)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"old-{index}", conv, "me", "我", "file", "", f"f{index}.bin", "", 10, "",
                     "", 0, now - 3600 - index, "out", "sent"),
                )
            # 一条 31 天前的，一条刚发的
            db.execute(
                "INSERT INTO messages (id, conv, sender, sender_name, kind, text, media_name,"
                " media_rel, media_size, media_mime, media_source, duration_ms, ts, direction, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ancient", conv, "me", "我", "file", "", "很旧.bin", "", 10, "", "", 0,
                 now - 31 * 86400, "out", "sent"),
            )
            db.commit()
            prune = getattr(store, "prune_records", None)
            if prune is None:
                check("prune_records() 存在", False, "core/chat.py 还没有这个函数")
                return
            removed = prune()
            kept = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            check("写/启动时自动修剪到 500 条以内", int(kept) <= 500, f"还剩 {kept} 条（删了 {removed}）")
            check("超过 30 天的被删掉",
                  db.execute("SELECT COUNT(*) FROM messages WHERE id='ancient'").fetchone()[0] == 0)
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 7. 清空记录一次干净
# ---------------------------------------------------------------------------


def test_purge() -> None:
    section("7 清空记录：记录 + journal + 孤儿 .part + 空会话 + __pending__")
    from eversend.core.chunkstore import JOURNAL_SUFFIX, PART_SUFFIX

    root = tmpdir("eversend-purge-")
    try:
        store = ChatStore(str(root))
        receive = root / "received"
        receive.mkdir(parents=True, exist_ok=True)
        conv = direct_conversation_id("me", "android:phone-1")
        store.upsert_conversation(conv, kind="direct", members=["me", "android:phone-1"])
        store.upsert_conversation("d:me|ghost", kind="direct", members=["me", "ghost"])
        store.add_message(conv, kind="file", media_name="大文件.iso", media_size=999, direction="out",
                          state="sending")
        store.add_message(conv, kind="file", media_name="大文件.iso", media_size=999, direction="out",
                          state="sent")
        store.add_message(conv, sender="me", text="__pending__1790000000-abc", direction="out")
        leftovers = [
            receive / ("半截.bin" + PART_SUFFIX),
            receive / ("半截.bin" + JOURNAL_SUFFIX),
        ]
        for path in leftovers:
            path.write_bytes(b"x" * 64)
        store.close()

        store = ChatStore(str(root))
        purge = getattr(store, "purge_records", None)
        if purge is None:
            check("purge_records() 存在", False, "core/chat.py 还没有这个函数")
            store.close()
            return
        purge(media_root=str(receive))
        db = sqlite3.connect(root / "chat.db")
        left = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        convs = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM messages WHERE text LIKE '%__pending__%'"
        ).fetchone()[0]
        db.close()
        check("媒体/传输记录被清掉（含那两行重复）", left <= 1, f"还剩 {left} 条")
        check("空会话被收走", convs <= 1, f"还剩 {convs} 段会话")
        check("没有 __pending__ 泄漏", pending == 0, f"{pending} 条")
        check("孤儿 .part 被清掉", not leftovers[0].exists())
        check("chunkstore journal 被清掉", not leftovers[1].exists())
        store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. 数据自检
# ---------------------------------------------------------------------------


def _seed_garbage(path: Path) -> None:
    """造一份"用户投诉现场"：孤儿消息、空会话、空名字、缺附件、0 宽度行、卡住的进行中。"""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            members TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
            last_text TEXT NOT NULL DEFAULT '', unread INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, conv TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'text',
            text TEXT NOT NULL DEFAULT '', media_name TEXT NOT NULL DEFAULT '',
            media_rel TEXT NOT NULL DEFAULT '', media_size INTEGER NOT NULL DEFAULT 0,
            media_mime TEXT NOT NULL DEFAULT '', media_source TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL,
            direction TEXT NOT NULL DEFAULT 'out', state TEXT NOT NULL DEFAULT 'sent');
        """
    )
    now = time.time()
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)",
               ("d:me|peer", "direct", "", '["me","peer"]', now, now, "", 0))
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)",
               ("d:me|nobody", "direct", "", '["me"]', now, now, "", 0))  # 空设备行
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("orphan-1", "d:gone|peer", "peer", "手机", "text", "孤儿消息", "", "", 0, "", "", 0,
                now, "in", "received"))
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("stuck-1", "d:me|peer", "me", "我", "file", "", "卡住.iso", "", 1024, "", "", 0,
                now - 3600, "out", "sending"))
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("missing-1", "d:me|peer", "peer", "手机", "file", "", "早没了.zip",
                "韧传聊天/deadbeef/早没了.zip", 2048, "", "", 0, now - 60, "in", "received"))
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("blank-1", "d:me|peer", "me", "我", "file", "", "", "", 0, "", "", 0, now, "out", "sent"))
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("pending-1", "d:me|peer", "me", "我", "file", "", "__pending__1-abc.bin", "", 5, "",
                "", 0, now, "out", "sending"))
    db.commit()
    db.close()


def test_audit_and_repair() -> None:
    section("8 数据自检：找出并修掉产生垃圾的代码路径")
    root = tmpdir("eversend-audit-")
    try:
        _seed_garbage(root / "chat.db")
        store = ChatStore(str(root))
        audit = getattr(store, "audit_records", None)
        repair = getattr(store, "repair_records", None)
        if audit is None or repair is None:
            check("audit_records()/repair_records() 存在", False, "core/chat.py 还没有这两个函数")
            store.close()
            return
        # 开库那一下就该把"卡在进行中、再也传不完"的行收敛掉（进程刚起来，
        # 本机不可能有还在飞的传输）。
        converged = store._db.execute(
            "SELECT state FROM messages WHERE id = 'stuck-1'"
        ).fetchone()[0]
        check("开库自动收敛卡住的进行中行", converged == "failed", str(converged))
        # 再塞一条（模拟运行中才出现的），自检必须找得到、修复必须收得掉。
        store._db.execute(
            "INSERT INTO messages (id, conv, sender, sender_name, kind, text, media_name,"
            " media_rel, media_size, media_mime, media_source, duration_ms, ts, direction, state,"
            " record_key, attempts) VALUES ('stuck-2','d:me|peer','me','我','file','',"
            "'又卡住.bin','',10,'','',0,?, 'out','sending','',1)",
            (time.time() - 3600,),
        )
        store._db.commit()
        report = audit(media_root=str(root / "received"))
        for key, label in (
            ("orphan_messages", "孤儿 message 的 conversation"),
            ("empty_conversations", "空会话 / 空设备行"),
            ("missing_attachments", "指向不存在文件的附件"),
            ("blank_records", "0 宽度行（没有任何可显示内容）"),
            ("stale_records", "卡在「进行中」再也不会动的行"),
            ("pending_leaks", "__pending__ 泄漏"),
        ):
            check(f"自检找到：{label}", int(report.get(key) or 0) > 0, json.dumps(report, ensure_ascii=False)[:200])
        repair(media_root=str(root / "received"))
        after = audit(media_root=str(root / "received"))
        check("修完自检干净（全 0）",
              all(int(after.get(k) or 0) == 0 for k in
                  ("orphan_messages", "empty_conversations", "blank_records", "stale_records", "pending_leaks")),
              json.dumps(after, ensure_ascii=False)[:300])
        # 从源头堵住孤儿：往一个不存在的会话里写消息，会话要跟着建出来
        ghost_conv = "d:me|never-seen"
        store.add_message(ghost_conv, sender="me", text="落在不存在会话里的消息", direction="out")
        check("往不存在的会话里写消息不会产生孤儿",
              store.audit_records()["orphan_messages"] == 0,
              str(store.audit_records()["orphan_messages"]))
        store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. 身份注册表去重（真实样本）
# ---------------------------------------------------------------------------


#: 真实 ``data/web_clients.json`` 里的一对记录：同一台安卓手机登记成两条 ——
#: 一条是 ``android:instrumented``（带别名），另一条是设备号本身。用户在设备列表
#: 里看到的就是同一台手机两行。
REAL_DUPLICATE_CLIENTS = [
    {
        "key": "android:instrumented",
        "deviceId": "instrumented",
        "label": "安卓 App",
        "name": "安卓 App",
        "kind": "app",
        "version": "1.0.0",
        "address": "192.168.110.127",
        "firstSeen": 1790076826.7,
        "lastSeen": 1790269890.2,
        "talked": True,
        "aliases": ["android:b696fdc5-6f04-4faf-a65f-9e4a3cbb0b2c", "127.0.0.1|d2445430"],
        "aliasLabels": {
            "android:b696fdc5-6f04-4faf-a65f-9e4a3cbb0b2c": {
                "label": "sdk_gphone64_x86_64", "kind": "app", "version": "1.0.0",
                "address": "127.0.0.1",
            },
            "127.0.0.1|d2445430": {"label": "安卓 App", "kind": "", "version": "", "address": "127.0.0.1"},
        },
    },
    {
        "key": "android:b696fdc5-6f04-4faf-a65f-9e4a3cbb0b2c",
        "deviceId": "b696fdc5-6f04-4faf-a65f-9e4a3cbb0b2c",
        "label": "sdk_gphone64_x86_64",
        "name": "sdk_gphone64_x86_64",
        "kind": "app",
        "version": "1.0.0",
        "address": "192.168.110.127",
        "firstSeen": 1790269884.0,
        "lastSeen": 1790269909.0,
        "talked": True,
    },
]


def test_identity_registry() -> None:
    section("9 身份注册表：同一台设备只留一条，aliases 可回溯")
    from eversend.core import model as model_mod

    merge = getattr(chat_mod, "merge_device_records", None) or getattr(model_mod, "merge_device_records", None)
    if merge is None:
        check("merge_device_records() 存在", False, "core/model.py 还没有这个函数")
        return
    merged, report = merge([dict(item) for item in REAL_DUPLICATE_CLIENTS])
    check("同一台设备的两条记录并成一条", len(merged) == 1,
          f"{len(merged)} 条：" + "、".join(str(m.get("key")) for m in merged))
    if len(merged) == 1:
        row = merged[0]
        survivor = str(row.get("key") or "")
        folded = [c["key"] for c in REAL_DUPLICATE_CLIENTS if c["key"] != survivor]
        aliases = set(row.get("aliases") or [])
        check("被并掉的旧键进 aliases（老会话还查得到名字）",
              all(key in aliases for key in folded), f"aliases={sorted(aliases)} 少了 {folded}")
        labels = row.get("aliasLabels") or {}
        check("旧的显示名 aliasLabels 没丢",
              all(str((labels.get(key) or {}).get("label") or "")
                  == str(next(c for c in REAL_DUPLICATE_CLIENTS if c["key"] == key).get("label"))
                  for key in folded),
              json.dumps(labels, ensure_ascii=False)[:200])
        check("留下的是最新的那个名字", row.get("label") == "sdk_gphone64_x86_64", str(row.get("label")))
        check("两份记录都消失（没有重复身份）",
              len([m for m in merged if m.get("deviceId") == "b696fdc5-6f04-4faf-a65f-9e4a3cbb0b2c"]) == 1)
    check("报告里说清了并掉了什么", int(report.get("merged") or 0) >= 1,
          json.dumps(report, ensure_ascii=False)[:200])


# ---------------------------------------------------------------------------
# 10. 设备行：同名消歧 + 幽灵行回收（Lead 实测：一台机器 3 台 RealProbe）
# ---------------------------------------------------------------------------


def test_device_rows() -> None:
    section("10 设备行：同名消歧 + 幽灵行回收")
    from eversend.core import model as model_mod

    names = getattr(model_mod, "disambiguate_names", None)
    prune = getattr(model_mod, "prune_device_records", None)
    if names is None or prune is None:
        check("disambiguate_names()/prune_device_records() 存在", False,
              "core/model.py 还没有这两个函数")
        return

    # Lead 在真机上量到的：同一台机器上跑出来的 3 个临时实例（deviceId 各不相同）
    live = [
        {"deviceId": "aaa", "name": "RealProbe", "address": "127.0.0.1", "port": 38275,
         "platform": "linux", "lastSeen": 100.0, "online": True},
        {"deviceId": "bbb", "name": "RealProbe", "address": "127.0.0.1", "port": 33817,
         "platform": "linux", "lastSeen": 200.0, "online": True},
        {"deviceId": "ccc", "name": "RealProbe", "address": "127.0.0.1", "port": 38907,
         "platform": "linux", "lastSeen": 300.0, "online": True},
        {"deviceId": "ddd", "name": "我的手机", "address": "192.168.1.9", "port": 52117,
         "platform": "android", "lastSeen": 50.0, "online": False},
    ]
    mapped = names(live)
    check("三台同名设备被消歧成不同的名字",
          len({mapped["aaa"], mapped["bbb"], mapped["ccc"]}) == 3,
          str([mapped["aaa"], mapped["bbb"], mapped["ccc"]]))
    check("最新的那台保留原名字（用户最可能指的就是它）", mapped["ccc"] == "RealProbe",
          str(mapped["ccc"]))
    check("其它两台带序号", mapped["bbb"] == "RealProbe #2" and mapped["aaa"] == "RealProbe #3",
          f"{mapped['bbb']} / {mapped['aaa']}")
    check("名字本来就不同的设备不动它", mapped["ddd"] == "我的手机", str(mapped["ddd"]))

    now = 1_000_000.0
    ttl = 86400.0
    rows = [
        # 同一台机器重装：地址+端口被新实例接管，旧行已经离线
        {"key": "android:old", "deviceId": "old", "label": "xgl-pc", "address": "10.0.0.5",
         "port": 52117, "platform": "linux", "lastSeen": now - 3 * ttl},
        {"key": "android:new", "deviceId": "new", "label": "xgl-pc", "address": "10.0.0.5",
         "port": 52117, "platform": "linux", "lastSeen": now - 10},
        # 同一台机器换数据目录：端口变了，但同地址同平台且旧行离线超过 TTL
        {"key": "android:ghost", "deviceId": "ghost", "label": "xgl-pc", "address": "10.0.0.5",
         "port": 40001, "platform": "linux", "lastSeen": now - 4 * ttl},
        # 同一地址上的另一台设备（平台不同）—— 不许并
        {"key": "android:mac", "deviceId": "mac", "label": "另一台", "address": "10.0.0.5",
         "port": 52117, "platform": "macos", "lastSeen": now - 2 * ttl},
        # 同一地址上两个不同浏览器（没有端口、没有平台）—— 不许并（真实数据实测过）
        {"key": "web:browser-1", "deviceId": "browser-1", "label": "Linux 上的 Chrome",
         "address": "127.0.0.1", "lastSeen": now - 5 * ttl},
        {"key": "web:browser-2", "deviceId": "browser-2", "label": "Linux 上的 Chrome",
         "address": "127.0.0.1", "lastSeen": now - 6 * ttl},
    ]
    kept, report = prune(rows, now=now, offline_ttl=ttl, limit=0)
    kept_keys = {str(r["key"]) for r in kept}
    dropped_keys = {str(r["key"]) for r in report["reasons"]}
    check("同一地址+端口的旧实例被回收", "android:old" in dropped_keys, str(sorted(dropped_keys)))
    check("同地址同平台、离线超过 TTL 的幽灵行被回收", "android:ghost" in dropped_keys,
          str(sorted(dropped_keys)))
    check("刚才还在的设备行不动", "android:new" in kept_keys, str(sorted(kept_keys)))
    check("同一地址上的另一台设备（平台不同）保留 —— 不过度合并",
          "android:mac" in kept_keys, str(sorted(kept_keys)))
    check("两个浏览器都在同一地址、没有平台 —— 都保留",
          {"web:browser-1", "web:browser-2"} <= kept_keys, str(sorted(kept_keys)))
    check("每一行回收都有理由与幸存者",
          all(r.get("reason") and r.get("survivor") for r in report["reasons"]),
          json.dumps(report["reasons"], ensure_ascii=False)[:200])


# ---------------------------------------------------------------------------
# 11. 群聊场景：同一文件发到同一个群 = 合并；发到群 vs 发到个人 = 两行
# ---------------------------------------------------------------------------


def test_group_records() -> None:
    section("11 群聊：同一文件发同一个群 = 一行；群 vs 个人 = 两行")
    from eversend.core.chat import new_group_id

    with tempfile.TemporaryDirectory() as root:
        store = ChatStore(root)
        try:
            group = new_group_id()
            store.upsert_conversation(group, kind="group", title="项目群",
                                      members=["me", "peer-a", "peer-b"])
            direct = direct_conversation_id("me", "peer-a")
            store.upsert_conversation(direct, kind="direct", members=["me", "peer-a"])
            payload = root + "/周报.pdf"
            Path(payload).write_bytes(b"%PDF-1.4 " + b"g" * 2048)
            # 同一个文件发给同一个群两次（每次都是 sending → sent）
            for _ in range(2):
                store.record_transfer(group, direction="out", peer=group, path=payload,
                                      name="周报.pdf", state="sending")
                store.record_transfer(group, direction="out", peer=group, path=payload,
                                      name="周报.pdf", state="sent")
            rows = store.transfer_records(conv_id=group)
            check("群里同一个文件只占一行", len(rows) == 1,
                  f"{len(rows)} 行：" + "、".join(f"{r['mediaName']}/{r['state']}" for r in rows))
            if len(rows) == 1:
                check("群里重发也算重复投递（attempts 累加到 2）",
                      int(rows[0].get("attempts") or 0) == 2, str(rows[0].get("attempts")))
            # 同一个文件再单发给群里的一位成员：另一段会话、另一个对端身份
            store.record_transfer(direct, direction="out", peer="peer-a", path=payload,
                                  name="周报.pdf", state="sent")
            check("发到群与发到个人各占一行（对端身份不同）",
                  len(store.transfer_records(conv_id=direct)) == 1
                  and len(store.transfer_records(conv_id=group)) == 1,
                  str(len(store.transfer_records(conv_id=direct))))
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 真实数据自检 / 修复
# ---------------------------------------------------------------------------


def audit_real(data_dir: str, apply: bool) -> int:
    from eversend.core.chat import audit_identity_files, repair_identity_files

    store = ChatStore(data_dir)
    print(f"数据目录：{os.path.abspath(data_dir)}")
    before = store.audit_records()
    print("chat.db 自检（修前）：")
    for key in sorted(before):
        print(f"  {key}: {before[key]}")
    files_before = audit_identity_files(data_dir)
    print("身份 JSON 自检（修前）：")
    for key in sorted(files_before):
        value = files_before[key]
        print(f"  {key}: {value if not isinstance(value, list) else len(value)}")

    if not apply:
        store.close()
        print("\n（只读模式；加 --apply 才真修，修理前会自动备份）")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name in ("chat.db", "web_clients.json", "known_devices.json", "identity.json"):
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.records-bak-{stamp}")
    fixed = store.repair_records()
    store.close()
    files_fixed = repair_identity_files(data_dir)
    verify = ChatStore(data_dir)
    after = verify.audit_records()
    verify.close()
    print("\nchat.db 修了什么：")
    for key in sorted(fixed):
        print(f"  {key}: {fixed[key]}")
    print("chat.db 自检（修后）：")
    for key in sorted(after):
        print(f"  {key}: {after[key]}")
    print("身份 JSON 修了什么：")
    for key in sorted(files_fixed):
        print(f"  {key}: {files_fixed[key]}")
    print("身份 JSON 自检（修后）：")
    for key in sorted(audit_identity_files(data_dir)):
        value = audit_identity_files(data_dir)[key]
        print(f"  {key}: {value if not isinstance(value, list) else len(value)}")
    print(f"\n备份后缀：.records-bak-{stamp}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="传输记录账本自检（红→绿）与真实数据审计")
    parser.add_argument("--data-dir", default="", help="真实数据目录（默认跑回归自检）")
    parser.add_argument("--apply", action="store_true", help="配合 --data-dir：真的修复（先备份）")
    args = parser.parse_args()
    if args.data_dir:
        return audit_real(args.data_dir, args.apply)

    print("EverSend 传输记录账本自检（C7 垃圾记录堆积 / C8 同一文件两行）")
    print("=" * 68)
    os.environ.setdefault("EVERSEND_HOME", tempfile.gettempdir())
    for test in (
        test_record_key,
        test_one_delivery_one_row,
        test_repeat_send_merges,
        test_same_name_different_bytes,
        test_legacy_double_write_converges,
        test_retention,
        test_purge,
        test_audit_and_repair,
        test_identity_registry,
        test_device_rows,
        test_group_records,
    ):
        try:
            test()
        except Exception:
            import traceback

            traceback.print_exc()
            _failures.append(f"{test.__name__} raised")
    print("\n" + "=" * 68)
    if _failures:
        print(f"\033[31mRED\033[0m — {len(_failures)}/{_checks} 项未通过：")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"\033[32mGREEN\033[0m — {_checks} 项全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
