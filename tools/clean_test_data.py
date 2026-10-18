#!/usr/bin/env python3
"""找出（并可选地清掉）**测试跑出来的**聊天记录。

为什么要这个工具：仪器测试和手工联调时起的电脑端如果没有设 `EVERSEND_HOME`，
`--cli serve` 会把数据目录算成 `<仓库>/data` —— 于是测试消息、测试群聊、测试设备
全都写进了用户真实的那份库里（审计员量到：445 条消息里 219 条、60 段会话里 48 段
是 `g:instrumented*` 测试群聊）。

用法：
    # 只看会删什么（默认，不动数据）
    .venv/bin/python tools/clean_test_data.py
    # 真的删（先自动备份 chat.db 到 .bak-<时间戳>）
    .venv/bin/python tools/clean_test_data.py --apply
    # 换一份库看看
    .venv/bin/python tools/clean_test_data.py --db /tmp/copy/chat.db

判据（都可打印出来核对，不做"猜"）：
  · 会话 id 以 ``g:instrumented`` 开头 —— 仪器测试建的群聊；
  · 消息文本命中仪器测试里那几句固定文案；
  · 附件名命中测试用的固定文件名（``ui-图片.png`` 等）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

#: 仪器测试写进去的固定文案（EverSendInstrumentedTest.kt 里那几句）。
TEST_TEXTS = (
    "界面测试消息",
    "来自安卓 App 的问候",
    "群聊建好了",
    "会话名字自检",
    "手机说的一句话",
    "手机发来的一句话",
    "真实设置下的一句话",
)
#: 仪器测试用的固定附件名（前缀匹配）。
TEST_FILES = (
    "ui-图片.png",
    "preview-图片.png",
    "instrumented-图片.png",
    "eversend-instrumented-图片.png",
    "发送页-真机-",
)


def default_db() -> Path:
    root = os.environ.get("EVERSEND_HOME") or Path.cwd()
    return Path(root) / "data" / "chat.db"


def analyse(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    conversations = [dict(row) for row in con.execute("SELECT * FROM conversations")]
    messages = [dict(row) for row in con.execute("SELECT * FROM messages")]

    test_convs: set[str] = set()
    for row in conversations:
        conv_id = str(row.get("id") or "")
        title = str(row.get("title") or "")
        if conv_id.startswith("g:instrumented") or "instrumented" in conv_id:
            test_convs.add(conv_id)
        elif "instrumented" in title.lower():
            test_convs.add(conv_id)

    def is_test_message(row: dict) -> bool:
        if str(row.get("conv") or "") in test_convs:
            return True
        text = str(row.get("text") or "")
        if any(marker in text for marker in TEST_TEXTS):
            return True
        name = str(row.get("media_name") or "")
        return any(name.startswith(prefix) for prefix in TEST_FILES)

    test_messages = [row for row in messages if is_test_message(row)]
    for row in test_messages:
        test_convs.add(str(row.get("conv") or ""))

    # 只剩测试消息的会话：删掉它们才叫"清干净"；还有真实消息的会话保留。
    keep_convs = {
        str(row.get("conv"))
        for row in messages
        if str(row.get("conv") or "") not in {
            str(m.get("conv")) for m in test_messages
        }
    }
    removable_convs = sorted(conv for conv in test_convs if conv and conv not in keep_convs)
    return {
        "db": str(db),
        "conversations_total": len(conversations),
        "messages_total": len(messages),
        "test_conversations": sorted(test_convs),
        "removable_conversations": removable_convs,
        "test_messages": len(test_messages),
        "keep_conversations": len(keep_convs),
        "samples": [
            {"conv": str(m.get("conv"))[:24], "text": str(m.get("text"))[:24],
             "media": str(m.get("media_name") or "")[:24]}
            for m in test_messages[:8]
        ],
    }


def apply_cleanup(db: Path, plan: dict) -> dict:
    backup = db.with_suffix(f".db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, backup)
    con = sqlite3.connect(db)
    try:
        removed_messages = 0
        for row in con.execute("SELECT id, conv, text, media_name FROM messages"):
            message_id, conv, text, media = row
            hit = conv in plan["test_conversations"] or any(
                marker in str(text or "") for marker in TEST_TEXTS
            ) or any(str(media or "").startswith(prefix) for prefix in TEST_FILES)
            if hit:
                con.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                removed_messages += 1
        removed_conversations = 0
        for conv_id in plan["removable_conversations"]:
            left = con.execute(
                "SELECT COUNT(*) FROM messages WHERE conv = ?", (conv_id,)
            ).fetchone()[0]
            if left:
                continue  # 里面还有真实消息，留着
            con.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            removed_conversations += 1
        con.commit()
    finally:
        con.close()
    return {
        "backup": str(backup),
        "removed_messages": removed_messages,
        "removed_conversations": removed_conversations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="chat.db 的路径")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认只看）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    db = args.db or default_db()
    if not db.exists():
        print(f"找不到聊天库：{db}", file=sys.stderr)
        return 2

    plan = analyse(db)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"聊天库：{plan['db']}")
        print(f"  会话 {plan['conversations_total']} 段 / 消息 {plan['messages_total']} 条")
        print(f"  判定为测试产生：消息 {plan['test_messages']} 条，"
              f"会话 {len(plan['test_conversations'])} 段（其中 {len(plan['removable_conversations'])} 段整段可删）")
        for row in plan["samples"]:
            print(f"    样本: {row['conv']:26s} {row['text']:26s} {row['media']}")
    if not args.apply:
        print("\n（默认只看：加 --apply 才会真删，删前会自动备份 chat.db）")
        return 0

    result = apply_cleanup(db, plan)
    print(f"\n已删除：消息 {result['removed_messages']} 条、"
          f"会话 {result['removed_conversations']} 段")
    print(f"备份：{result['backup']}")
    print("提示：下次起测试服务端请设 EVERSEND_HOME=/tmp/<目录>，别写进真实数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
