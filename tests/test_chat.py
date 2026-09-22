#!/usr/bin/env python3
"""Chat tests: the store, and two engines actually talking to each other.

What is being checked, and why it is worth a test
-------------------------------------------------
* A brand-new 1:1 conversation has no members stored yet -- the caller supplies
  the peer.  Getting that wrong made ``send_chat`` report "sent" while delivering
  nothing (``reachable 0 / delivered 0``), which is the worst possible failure
  mode: the UI says it worked.
* An attachment must land at *exactly* the path the message advertises.  The
  first version used the raw conversation id as a folder name; the receiver
  sanitises path components (``d:aa|bb`` -> ``d_aa_bb``), so the message pointed
  at a file that was somewhere else.  Both ends now derive the folder from a
  hash of the conversation id.
* Group chat is a fan-out: one message, N recipients, and each of them learns
  the conversation even if it had never heard of the group before.
* A member that is not reachable must not be reported as delivered.

Usage::

    python3 tests/test_chat.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _scratch import scratch, use_utf8_console  # noqa: E402

from eversend.core.chat import (  # noqa: E402
    ChatStore,
    conversation_dirname,
    direct_conversation_id,
    media_relpath,
    new_group_id,
)
from eversend.core.engine import Engine, EngineConfig  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    if not condition:
        _failures.append(name)
    return bool(condition)


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def make_engine(root: Path, name: str) -> Engine:
    engine = Engine(
        EngineConfig(
            data_dir=str(root / name / "state"),
            receive_dir=str(root / name / "recv"),
            name=name,
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
            auto_accept_all=True,
        )
    )
    engine.start()
    return engine


def introduce(one: Engine, other: Engine) -> None:
    """Let two engines see each other without waiting for discovery."""
    one.discovery.peers.add_direct(other.info, "127.0.0.1", other.port)
    other.discovery.peers.add_direct(one.info, "127.0.0.1", one.port)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_store() -> None:
    print("\n[1] The store: conversations, messages, attachments, unread")
    with scratch() as root:
        store = ChatStore(str(root))
        try:
            direct = direct_conversation_id("bbb", "aaa")
            check("a 1:1 id is the same from both sides", direct == direct_conversation_id("aaa", "bbb"), direct)
            store.upsert_conversation(direct, kind="direct", title="和 手机", members=["aaa", "bbb"])
            check("the conversation is stored", store.conversation(direct) is not None)
            check("members survive", store.conversation(direct)["members"] == ["aaa", "bbb"])

            # A member id may contain the separator the first version used.
            phone = "web:192.168.1.9|a4ed7fe8"
            conv = direct_conversation_id("aaa", phone)
            store.upsert_conversation(conv, kind="direct", members=["aaa", phone])
            check("a member id containing '|' stays one member",
                  store.conversation(conv)["members"] == ["aaa", phone],
                  str(store.conversation(conv)["members"]))

            store.add_message(direct, sender="aaa", sender_name="我", text="你好 👋", direction="out")
            store.add_message(
                direct,
                sender="bbb",
                sender_name="手机",
                kind="image",
                media_name="a.png",
                media_rel=media_relpath(direct, "a.png"),
                media_size=99,
                direction="in",
            )
            messages = store.messages(direct)
            check("both messages are there, oldest first",
                  [m["kind"] for m in messages] == ["text", "image"], str([m["kind"] for m in messages]))
            check("an incoming message counts as unread", store.unread_total() == 1, str(store.unread_total()))
            check("the list preview shows the attachment", store.conversation(direct)["lastText"] == "[图片]",
                  store.conversation(direct)["lastText"])
            store.mark_read(direct)
            check("marking read clears the badge", store.unread_total() == 0)

            check("the attachment folder is filesystem safe",
                  ":" not in conversation_dirname(direct) and "|" not in conversation_dirname(direct),
                  conversation_dirname(direct))
            check("the same conversation always maps to the same folder",
                  conversation_dirname(direct) == conversation_dirname(direct))
            check("a nasty file name is sanitised the receiver's way",
                  ":" not in media_relpath(direct, "a:b?.png") and "?" not in media_relpath(direct, "a:b?.png"),
                  media_relpath(direct, "a:b?.png"))

            group = new_group_id()
            store.upsert_conversation(group, kind="group", title="群", members=["aaa", "bbb", "ccc"])
            store.add_message(group, sender="aaa", text="大家好", direction="out")
            check("groups are separate conversations", len(store.conversations()) == 3, str(len(store.conversations())))
            check("the newest conversation comes first", store.conversations()[0]["id"] == group)
        finally:
            store.close()


def test_direct_chat() -> None:
    print("\n[2] Two computers, one conversation")
    with scratch() as root:
        a = make_engine(root, "甲")
        b = make_engine(root, "乙")
        try:
            introduce(a, b)
            check("甲 sees 乙", any(p.info.name == "乙" for p in a.devices()))
            conv = direct_conversation_id(a.info.device_id, b.info.device_id)

            result = a.send_chat(conv, kind="text", text="你好 👋", to=b.info.device_id, title="和 乙 的对话")
            check("the message was delivered", result["delivered"] == 1, str(result)[:120])
            check("delivery state is 'sent'", result["message"]["state"] == "sent", result["message"]["state"])
            check("乙 received it", wait_for(lambda: bool(b.chat.messages(conv))))
            if b.chat.messages(conv):
                got = b.chat.messages(conv)[0]
                check("text arrived intact with the emoji", got["text"] == "你好 👋", got["text"])
                check("it arrives as incoming", got["direction"] == "in", got["direction"])
                check("the sender name came along", got["senderName"] == "甲", got["senderName"])
            check("the conversation exists on both sides",
                  bool(a.chat.conversation(conv)) and bool(b.chat.conversation(conv)))

            # A message to a device that is not there must be honest about it.
            missing = a.send_chat(
                direct_conversation_id(a.info.device_id, "deadbeef"),
                kind="text",
                text="有人吗",
                to="deadbeef",
            )
            check("an unreachable member is reported as failed",
                  missing["message"]["state"] == "failed", str(missing["message"]["state"]))
        finally:
            a.stop()
            b.stop()


def test_attachment() -> None:
    print("\n[3] An attachment lands where the message says it does")
    with scratch() as root:
        a = make_engine(root, "甲")
        b = make_engine(root, "乙")
        try:
            introduce(a, b)
            conv = direct_conversation_id(a.info.device_id, b.info.device_id)
            payload = bytes(range(256)) * 40
            photo = root / "photo.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)

            result = a.send_chat(conv, kind="image", media_path=str(photo), media_mime="image/png", to=b.info.device_id)
            rel = result["message"]["mediaRel"]
            check("the message carries a relative media path", bool(rel), str(result["message"])[:120])
            landed = Path(b.config.receive_dir) / rel
            check("the file exists at that path", wait_for(landed.exists, timeout=8.0), str(landed))
            if landed.exists():
                check("the bytes are identical", landed.read_bytes() == photo.read_bytes())
            check("乙's copy of the message points at the same path",
                  wait_for(lambda: any(m["mediaRel"] == rel for m in b.chat.messages(conv))),
                  str([m["mediaRel"] for m in b.chat.messages(conv)])[:120])
            if b.chat.messages(conv):
                incoming = [m for m in b.chat.messages(conv) if m["kind"] == "image"]
                check("the receiver knows the size and mime",
                      bool(incoming) and incoming[0]["mediaSize"] == photo.stat().st_size
                      and incoming[0]["mediaMime"] == "image/png",
                      str(incoming)[:140])

            voice = root / "voice.webm"
            voice.write_bytes(b"\x1a\x45\xdf\xa3" + b"x" * 512)
            a.send_chat(
                conv, kind="voice", media_path=str(voice), media_mime="audio/webm", duration_ms=3100, to=b.info.device_id
            )
            check(
                "a voice note carries its duration",
                wait_for(lambda: any(m["kind"] == "voice" and m["durationMs"] == 3100 for m in b.chat.messages(conv))),
                str([(m["kind"], m["durationMs"]) for m in b.chat.messages(conv)]),
            )
        finally:
            a.stop()
            b.stop()


def test_group_chat() -> None:
    print("\n[4] Group chat: one message, every member")
    with scratch() as root:
        a = make_engine(root, "甲")
        b = make_engine(root, "乙")
        c = make_engine(root, "丙")
        try:
            introduce(a, b)
            introduce(a, c)
            group = new_group_id()
            members = [a.info.device_id, b.info.device_id, c.info.device_id]
            result = a.send_chat(group, kind="text", text="群里好 🎉", title="测试群", members=members)
            check("both members got it", result["delivered"] == 2, str(result["delivered"]))
            check("乙 learned the group", wait_for(lambda: bool(b.chat.conversation(group))))
            check("丙 learned the group", wait_for(lambda: bool(c.chat.conversation(group))))
            check("乙 has the message", wait_for(lambda: [m["text"] for m in b.chat.messages(group)] == ["群里好 🎉"]),
                  str([m["text"] for m in b.chat.messages(group)]))
            check("丙 has the message", wait_for(lambda: [m["text"] for m in c.chat.messages(group)] == ["群里好 🎉"]),
                  str([m["text"] for m in c.chat.messages(group)]))
            check("the group keeps its name on the receivers",
                  b.chat.conversation(group)["title"] == "测试群" and b.chat.conversation(group)["kind"] == "group")
            check("everyone is listed as a member",
                  len(b.chat.conversation(group)["members"]) == 3, str(b.chat.conversation(group)["members"]))
        finally:
            a.stop()
            b.stop()
            c.stop()


def main() -> int:
    use_utf8_console()
    print("EverSend chat tests")
    print("=" * 60)
    for test in (test_store, test_direct_chat, test_attachment, test_group_chat):
        try:
            test()
        except Exception:
            import traceback

            traceback.print_exc()
            _failures.append(f"{test.__name__} raised")
    print("\n" + "=" * 60)
    if _failures:
        print(f"{len(_failures)} failure(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("All chat checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
