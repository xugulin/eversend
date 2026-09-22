"""Chat: conversations and messages, kept in SQLite.

Why a database and not a JSON file
---------------------------------
A chat log grows by one row per message and is read back newest-first, paginated
and filtered by conversation.  ``sqlite3`` is in the standard library (so the
green pack still needs nothing extra), it is crash-safe with a journal, and it
gives pagination for free.  A JSON file rewritten on every keystroke would be
both slower and easy to corrupt.

Two kinds of conversation
------------------------
* **direct** — id derived from the two members, so both sides compute the same
  id without any negotiation: ``d:<id-a>|<id-b>`` (sorted).
* **group** — id minted by whoever created it: ``g:<uuid4>``.  The member list
  travels in every message, so a device that was offline when the group was
  made still learns about it from the next message.

Attachments
-----------
Media is *not* stored in the database.  A message carries ``media_rel``: a path
relative to the receive directory, e.g. ``韧传聊天/d:aa|bb/photo.jpg``.  Two
reasons: a 4 GB video must never be copied into a BLOB, and the web layer can
then serve it through the same range-capable download path as everything else.
The sending side moves the file with the normal transfer engine into exactly
that relative folder, so both ends agree on the path without extra protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

#: Relative folder (inside the receive directory) where chat media lands.  Kept
#: as a constant because both ends must compute the identical path.
MEDIA_DIRNAME = "韧传聊天"

#: Message kinds.  ``system`` is used for "X 加入了群聊" style notes.
KINDS = ("text", "image", "video", "voice", "file", "system")

#: Which kinds carry an attachment.
MEDIA_KINDS = ("image", "video", "voice", "file")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    members   TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    last_text TEXT NOT NULL DEFAULT '',
    unread    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    conv        TEXT NOT NULL,
    sender      TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'text',
    text        TEXT NOT NULL DEFAULT '',
    media_name  TEXT NOT NULL DEFAULT '',
    media_rel   TEXT NOT NULL DEFAULT '',
    media_size  INTEGER NOT NULL DEFAULT 0,
    media_mime  TEXT NOT NULL DEFAULT '',
    media_source TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ts          REAL NOT NULL,
    direction   TEXT NOT NULL DEFAULT 'out',
    state       TEXT NOT NULL DEFAULT 'sent'
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conv, ts);
"""


def direct_conversation_id(one: str, other: str) -> str:
    """The id both sides of a 1:1 chat compute for themselves."""
    pair = sorted([str(one), str(other)])
    return "d:" + "|".join(pair)


def new_group_id() -> str:
    return "g:" + uuid.uuid4().hex[:16]


def conversation_dirname(conv_id: str) -> str:
    """A filesystem-safe folder name for a conversation.

    A conversation id is ``d:<id>|<id>`` or ``g:<uuid>`` -- it contains ``:``
    and ``|``, which the receiving side sanitises away when it builds the path
    from the wire.  Hashing keeps both ends computing the *same* folder without
    either of them having to sanitise identically: a mismatch here means the
    message points at a file that is not where it says it is.
    """
    digest = hashlib.sha1(str(conv_id).encode("utf-8", "replace")).hexdigest()
    return f"{MEDIA_DIRNAME}/{digest[:16]}"


def media_relpath(conv_id: str, name: str) -> str:
    """Where an attachment lives, relative to the receive directory.

    The file name is sanitised with the *receiver's* own rules (``safe_join``
    does exactly this), so this returns the path the file will really have.
    """
    from .model import sanitize_component

    safe = sanitize_component(os.path.basename(str(name))) or "file"
    return f"{conversation_dirname(conv_id)}/{safe}"


def _decode_members(raw: Any) -> list[str]:
    """Read the member list, tolerating rows written by an older build.

    Members are stored as JSON, not as a ``|``-joined string: a member id can
    itself contain ``|`` (a phone is ``web:<address>|<ua-hash>``), and the
    separator then split one phone into two members -- which showed up in the
    UI as a conversation with a member literally named "abc12345".
    """
    text = str(raw or "")
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [m for m in text.split("|") if m]
    if isinstance(parsed, list):
        return [str(m) for m in parsed if str(m)]
    return []


class ChatStore:
    """Conversations and messages for one installation."""

    def __init__(self, data_dir: str, *, limit_messages: int = 5000) -> None:
        self.data_dir = str(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.path = os.path.join(self.data_dir, "chat.db")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        #: Newest messages kept per conversation; a chat is not an archive.
        self.limit_messages = int(limit_messages)
        self._db  # noqa: B018 - opens (and validates) the database now

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: the engine writes from transfer threads and
        # the UI reads from the Qt thread; every statement below is short and
        # serialised by _lock, which is what SQLite needs.
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        # ``media_source`` arrived later than the first release.  A user's
        # existing chat.db must keep working (and keep its history), so the
        # column is added here instead of asking anyone to delete the file.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
        if "media_source" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN media_source TEXT NOT NULL DEFAULT ''")
        db.commit()
        return db

    @property
    def _db(self) -> sqlite3.Connection:
        """The live connection, reopened on demand.

        Reopening matters because :meth:`~eversend.core.engine.Engine.stop`
        closes it: on Windows a file with an open handle cannot be deleted, so a
        stopped engine that left ``chat.db`` open made ``--cli selftest`` fail
        to remove its own temporary directory -- a green self-test and a red
        exit code.  Keeping the handle open *and* being able to close it is what
        the property buys.
        """
        with self._lock:
            if self._connection is None:
                self._connection = self._connect()
            return self._connection

    # -- conversations -----------------------------------------------------

    def upsert_conversation(
        self,
        conv_id: str,
        *,
        kind: str = "direct",
        title: str = "",
        members: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Create the conversation, or update what we now know about it."""
        member_list = [str(m) for m in members if str(m)]
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            encoded = json.dumps(member_list, ensure_ascii=False)
            if row is None:
                self._db.execute(
                    "INSERT INTO conversations (id, kind, title, members, created, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (conv_id, kind, title, encoded, now, now),
                )
            else:
                # Never lose members we already knew: the sender may be telling
                # us about a subset (it only knows who *it* can reach).
                known = _decode_members(row["members"])
                merged = known + [m for m in member_list if m not in known]
                self._db.execute(
                    "UPDATE conversations SET kind = ?, title = ?, members = ? WHERE id = ?",
                    (kind or row["kind"], title or row["title"],
                     json.dumps(merged, ensure_ascii=False), conv_id),
                )
            self._db.commit()
        return self.conversation(conv_id) or {}

    def conversation(self, conv_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
        return self._conversation_dict(row) if row is not None else None

    def conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM conversations ORDER BY updated DESC"
            ).fetchall()
        return [self._conversation_dict(row) for row in rows]

    def members(self, conv_id: str) -> list[str]:
        conv = self.conversation(conv_id)
        return list(conv.get("members", [])) if conv else []

    def add_member(self, conv_id: str, member: str) -> None:
        conv = self.conversation(conv_id)
        if conv is None:
            return
        members = list(conv["members"])
        if member and member not in members:
            members.append(member)
            self.upsert_conversation(
                conv_id, kind=conv["kind"], title=conv["title"], members=members
            )

    def remove_conversation(self, conv_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE conv = ?", (conv_id,))
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._db.commit()

    # -- messages ----------------------------------------------------------

    def add_message(
        self,
        conv_id: str,
        *,
        sender: str = "",
        sender_name: str = "",
        kind: str = "text",
        text: str = "",
        media_name: str = "",
        media_rel: str = "",
        media_size: int = 0,
        media_mime: str = "",
        media_source: str = "",
        duration_ms: int = 0,
        direction: str = "out",
        state: str = "sent",
        message_id: str = "",
        ts: float | None = None,
    ) -> dict[str, Any]:
        """Append one message and bump its conversation."""
        if kind not in KINDS:
            kind = "text"
        # One shape everywhere: the dict this returns is the same one the UI,
        # the web layer and the wire format read (camelCase), so a field can
        # never be silently dropped between here and the other end.  (It was:
        # the wire format asked for ``mediaRel`` while this returned
        # ``media_rel``, and every attachment arrived with an empty path.)
        entry = {
            "id": message_id or uuid.uuid4().hex[:20],
            "conv": conv_id,
            "sender": str(sender),
            "senderName": str(sender_name),
            "kind": kind,
            "text": str(text),
            "mediaName": str(media_name),
            "mediaRel": str(media_rel),
            "mediaSize": int(media_size or 0),
            "mediaMime": str(media_mime),
            # Where *this* machine keeps the file it sent.  Local bookkeeping
            # only -- it never goes on the wire (see chat_wire_payload) and is
            # stripped from the HTTP API, because a path like
            # /home/me/相册/假期.jpg says a lot about this computer.
            "mediaSource": str(media_source),
            "durationMs": int(duration_ms or 0),
            "ts": float(ts if ts is not None else time.time()),
            "direction": "in" if direction == "in" else "out",
            "state": str(state),
        }
        row = {
            "id": entry["id"],
            "conv": entry["conv"],
            "sender": entry["sender"],
            "sender_name": entry["senderName"],
            "kind": entry["kind"],
            "text": entry["text"],
            "media_name": entry["mediaName"],
            "media_rel": entry["mediaRel"],
            "media_size": entry["mediaSize"],
            "media_mime": entry["mediaMime"],
            "media_source": entry["mediaSource"],
            "duration_ms": entry["durationMs"],
            "ts": entry["ts"],
            "direction": entry["direction"],
            "state": entry["state"],
        }
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO messages (id, conv, sender, sender_name, kind, text,"
                " media_name, media_rel, media_size, media_mime, media_source, duration_ms, ts,"
                " direction, state)"
                " VALUES (:id, :conv, :sender, :sender_name, :kind, :text, :media_name, :media_rel,"
                " :media_size, :media_mime, :media_source, :duration_ms, :ts, :direction, :state)",
                row,
            )
            preview = entry["text"] or {
                "image": "[图片]",
                "video": "[视频]",
                "voice": "[语音]",
                "file": "[文件]",
            }.get(entry["kind"], "")
            unread = 0 if entry["direction"] == "out" else 1
            self._db.execute(
                "UPDATE conversations SET updated = ?, last_text = ?,"
                " unread = unread + ? WHERE id = ?",
                (entry["ts"], preview[:200], unread, conv_id),
            )
            self._db.commit()
            self._trim(conv_id)
        return entry

    def set_state(self, message_id: str, state: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE messages SET state = ? WHERE id = ?", (str(state), message_id)
            )
            self._db.commit()

    def message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM messages WHERE id = ?", (str(message_id),)
            ).fetchone()
        return self._message_dict(row) if row is not None else None

    def messages(
        self, conv_id: str, *, limit: int = 200, before: float | None = None
    ) -> list[dict[str, Any]]:
        """Newest ``limit`` messages, returned oldest-first for display."""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if before is None:
                rows = self._db.execute(
                    "SELECT * FROM messages WHERE conv = ? ORDER BY ts DESC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM messages WHERE conv = ? AND ts < ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (conv_id, float(before), limit),
                ).fetchall()
        return [self._message_dict(row) for row in reversed(rows)]

    def unread_total(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT SUM(unread) AS n FROM conversations").fetchone()
        return int(row["n"] or 0) if row is not None else 0

    def mark_read(self, conv_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET unread = 0 WHERE id = ?", (conv_id,))
            self._db.commit()

    # -- internals ---------------------------------------------------------

    def _trim(self, conv_id: str) -> None:
        """Keep a conversation from growing without bound."""
        if self.limit_messages <= 0:
            return
        self._db.execute(
            "DELETE FROM messages WHERE conv = ? AND id NOT IN ("
            " SELECT id FROM messages WHERE conv = ? ORDER BY ts DESC LIMIT ?)",
            (conv_id, conv_id, self.limit_messages),
        )
        self._db.commit()

    @staticmethod
    def _conversation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "members": _decode_members(row["members"]),
            "created": row["created"],
            "updated": row["updated"],
            "lastText": row["last_text"],
            "unread": int(row["unread"] or 0),
        }

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conv": row["conv"],
            "sender": row["sender"],
            "senderName": row["sender_name"],
            "kind": row["kind"],
            "text": row["text"],
            "mediaName": row["media_name"],
            "mediaRel": row["media_rel"],
            "mediaSize": int(row["media_size"] or 0),
            "mediaMime": row["media_mime"],
            # Local-only: the bubble previews the file you sent from wherever
            # you sent it, instead of looking for it in the receive folder.
            "mediaSource": row["media_source"] if "media_source" in row.keys() else "",
            "durationMs": int(row["duration_ms"] or 0),
            "ts": row["ts"],
            "direction": row["direction"],
            "state": row["state"],
        }

    def close(self) -> None:
        """Release the file handle (and allow a later reopen)."""
        with self._lock:
            if self._connection is None:
                return
            try:
                self._connection.close()
            except Exception:  # pragma: no cover - closing must never raise
                pass
            self._connection = None


__all__ = [
    "ChatStore",
    "conversation_dirname",
    "KINDS",
    "MEDIA_DIRNAME",
    "MEDIA_KINDS",
    "direct_conversation_id",
    "media_relpath",
    "new_group_id",
]
