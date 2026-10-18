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

The transfer ledger (charter C7/C8)
-----------------------------------
A file that goes to a device is *one row*, forever.  That is what
``record_key`` is for: a deterministic digest of the three things that make a
delivery what it is --

    ``(direction, peer_identity, content_identity)``

-- so the row written when the transfer *starts* and the row written when it
*finishes* land on the same key and are merged in place instead of becoming
two lines in the table (the user's "同一个文件传到同一个设备显示两次").  Sending
the same file again does not add a row either: the key matches, ``attempts``
grows, and the state moves to the newest outcome (charter C8: 合并成一行并累加).

Two different files that happen to share a name never collide, because the
content identity is a digest of the bytes whenever a digest is affordable
(:func:`eversend.core.hashing.file_identity`), and ``(size, mtime_ns)``
otherwise -- never the name alone.

The same module owns the rest of C7: :meth:`ChatStore.prune_records` (500 rows /
30 days by default, constants in :mod:`eversend.core.constants`),
:meth:`ChatStore.purge_records` (「清空记录」in one pass: rows, chunkstore
journals, orphan ``.part`` files, empty conversations), and
:meth:`ChatStore.audit_records` / :meth:`ChatStore.repair_records` (the
self-check: orphan messages, empty conversations, blank rows, dangling
attachments, rows stuck in "进行中", ``__pending__`` leaks).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from . import hashing
from .constants import (
    DEVICE_RECORD_LIMIT,
    DEVICE_RECORD_TTL,
    RECORD_ACTIVE_STATES,
    RECORD_DIGEST_MAX_BYTES,
    RECORD_KEEP_COUNT,
    RECORD_KEEP_DAYS,
    RECORD_STALE_STATE_SECONDS,
    RECORD_TERMINAL_STATES,
)

#: Relative folder (inside the receive directory) where chat media lands.  Kept
#: as a constant because both ends must compute the identical path.
MEDIA_DIRNAME = "韧传聊天"

#: Message kinds.  ``system`` is used for "X 加入了群聊" style notes.
KINDS = ("text", "image", "video", "voice", "file", "system")

#: Which kinds carry an attachment.
MEDIA_KINDS = ("image", "video", "voice", "file")

#: Kinds that make up the transfer ledger: everything that names a file, plus
#: the system notes that would otherwise pile up in a conversation.
RECORD_KINDS = MEDIA_KINDS + ("system",)

#: The marker the desktop puts on a transfer row before the engine has minted a
#: real transfer id (``__pending__<time>-<device>``).  It is a *UI* key; if one
#: ever reaches the database it is garbage, and this is how it is recognised.
PENDING_MARKER = "__pending__"

#: How often the write path is allowed to run the retention trim.  Trimming on
#: every single insert would run a DELETE + COUNT per message; a few seconds of
#: slack costs nothing and keeps a burst of messages cheap.
PRUNE_INTERVAL = 5.0

#: How far apart an "进行中" row and its "已完成" row may be and still count as
#: one delivery when repairing historic data.  A transfer that takes longer than
#: this is either still running (so it has no completed row yet) or was retried.
RECORD_PAIR_WINDOW = 6 * 3600.0

#: Extension -> message kind, for a record written without an explicit kind.
_KIND_BY_SUFFIX = {
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".svg", ".avif"),
    "video": (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts"),
    "voice": (".opus", ".ogg", ".m4a", ".aac", ".mp3", ".wav", ".amr", ".flac", ".webm"),
}


def kind_for_name(name: str, mime: str = "") -> str:
    """Which message kind a file name (or MIME type) implies."""
    text = str(mime or "").lower()
    if text.startswith("image/"):
        return "image"
    if text.startswith("video/"):
        return "video"
    if text.startswith("audio/"):
        return "voice"
    suffix = os.path.splitext(str(name or "").lower())[1]
    for kind, suffixes in _KIND_BY_SUFFIX.items():
        if suffix and suffix in suffixes:
            return kind
    return "file"


class _MemoryDigestCache:
    """The tiniest possible digest cache (per store, in memory).

    The engine has a persistent one; this keeps a store that was created
    without it from hashing the same file twice inside one run.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = entries

    @staticmethod
    def key(path: str, size: int, mtime_ns: int) -> str:
        return hashing.DigestCache.key(path, size, mtime_ns)

    def get(self, path: str, size: int, mtime_ns: int) -> str | None:
        return self._entries.get(self.key(path, size, mtime_ns))

    def put(self, path: str, size: int, mtime_ns: int, digest: str) -> None:
        if digest:
            self._entries[self.key(path, size, mtime_ns)] = digest

    def digest_for(self, path: str, size: int, mtime_ns: int) -> str:
        cached = self.get(path, size, mtime_ns)
        if cached:
            return cached
        digest = hashing.file_digest(path)
        self.put(path, size, mtime_ns, digest)
        return digest


def _strip_pending(value: str) -> str:
    """Remove a ``__pending__…`` placeholder from a stored string."""
    text = str(value or "")
    while PENDING_MARKER in text:
        start = text.index(PENDING_MARKER)
        end = start
        while end < len(text) and not text[end].isspace():
            end += 1
        text = (text[:start] + text[end:]).strip()
    return text


def _members_from_conversation_id(conv_id: str) -> list[str]:
    """Recover a member list from a conversation id (``d:a|b`` -> ``[a, b]``)."""
    text = str(conv_id or "")
    if text.startswith("d:"):
        return [part for part in text[2:].split("|") if part]
    return []


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
    state       TEXT NOT NULL DEFAULT 'sent',
    record_key  TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conv, ts);
"""

#: Indexes that touch a *later* column, created only after the migration in
#: :meth:`ChatStore._connect` has added it.  Putting one of these in ``_SCHEMA``
#: instead made ``executescript`` fail on every existing database (the CREATE
#: INDEX runs before the ALTER TABLE) -- i.e. an upgrade would not open at all.
_LATER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_record ON messages (record_key)",
)

#: Columns added after the first release.  A user's existing ``chat.db`` must
#: keep working (and keep its history), so they are added in place instead of
#: asking anyone to delete the file: ``name -> DDL fragment``.
_LATER_COLUMNS = {
    "media_source": "TEXT NOT NULL DEFAULT ''",
    #: 这条文件消息对应的**分享条目 id**（手机点气泡「保存」时走哪个下载任务）。
    #: 以前是服务端按"路径/名字+大小"事后猜（find_share_for_message），同一个文件
    #: 重复发多次时老消息会被猜成新分享，手机端历史卡片跟着显示当前进度。
    #: 发送时就把 share id 写进记录，手机端拿到的是**精确**关联。
    "share_id": "TEXT NOT NULL DEFAULT ''",
    "record_key": "TEXT NOT NULL DEFAULT ''",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
}


def direct_conversation_id(one: str, other: str) -> str:
    """The id both sides of a 1:1 chat compute for themselves."""
    pair = sorted([str(one), str(other)])
    return "d:" + "|".join(pair)


def new_group_id() -> str:
    return "g:" + uuid.uuid4().hex[:16]


def is_record_kind(kind: str) -> bool:
    """Whether messages of ``kind`` belong to the transfer ledger."""
    return str(kind or "") in RECORD_KINDS


def content_identity(
    *,
    digest: str = "",
    path: str = "",
    name: str = "",
    size: int = 0,
    mtime_ns: int = 0,
    cache: Any = None,
    max_bytes: int = RECORD_DIGEST_MAX_BYTES,
) -> str:
    """The "what was sent" half of a transfer record's identity.

    Strongest first: a digest the caller already has (the transfer engine
    computes one for every file it moves), then a digest of the file if it is
    still readable and affordable, then ``(size, mtime_ns, name)``.  Returns
    ``""`` only when nothing identifying is known at all -- a row like that has
    no name, no size and no content, i.e. it would render as a zero-width line.
    """
    text = str(digest or "").strip()
    if text:
        return "d:" + text
    if path:
        found = hashing.file_identity(str(path), cache=cache, max_bytes=int(max_bytes))
        if found:
            return found
    size = int(size or 0)
    if size or mtime_ns or str(name or "").strip():
        return f"f:{size}:{int(mtime_ns or 0)}:{str(name or '')}"
    return ""


def transfer_record_key(direction: str, peer: str, identity: str) -> str:
    """The deterministic dedup key of one delivery (charter C8).

    ``(direction, peer_identity, content_identity)`` -- in that order and
    nothing else.  Deliberately *not* built from the conversation id, the file
    name, the transfer id or the wall clock:

    * the transfer id only exists once the session is up, so a row written when
      the send was requested could never match the row written when it
      finished -- that is exactly how one delivery became two lines;
    * the file name is not an identity (two different files can share one);
    * the conversation id changes spelling when a device re-registers, and a
      record must survive that.

    The result is a fixed-width hex digest, so it is safe to store for any
    peer spelling (including ``web:127.0.0.1|3f2a1b``).
    """
    hasher = hashlib.blake2b(digest_size=16)
    for part in (direction, peer, identity):
        hasher.update(str(part or "").encode("utf-8", "replace"))
        hasher.update(b"\x1f")
    return hasher.hexdigest()


def peer_identity(
    conv_id: str,
    members: Iterable[str] = (),
    *,
    sender: str = "",
    direction: str = "out",
    explicit: str = "",
) -> str:
    """Who a record is *with*: a stable id, never a display name or address.

    For a 1:1 conversation it is the member that is not the sender (incoming
    messages name their sender, outgoing ones are sent by this machine).  For a
    group it is the conversation itself -- "同一台设备" has no single answer in
    a group, and merging every member's copy into one row would hide a real
    difference.  ``explicit`` always wins: a caller that knows the device id
    (the transfer path does) should pass it.
    """
    text = str(explicit or "").strip()
    if text:
        return text
    conv = str(conv_id or "")
    if conv.startswith("g:"):
        return conv
    others = [str(m) for m in members if str(m) and str(m) != str(sender or "")]
    return others[0] if others else conv


def message_record_key(message: dict[str, Any]) -> str:
    """The stored key of a message dict, or ``""`` for a chat line."""
    return str(message.get("recordKey") or "")


def is_pending_garbage(value: Any) -> bool:
    """Whether a value carries the desktop's ``__pending__`` placeholder."""
    return PENDING_MARKER in str(value or "")



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

    def __init__(
        self,
        data_dir: str,
        *,
        limit_messages: int = 5000,
        media_root: str = "",
        digest_cache: Any = None,
        keep_records: int = RECORD_KEEP_COUNT,
        keep_days: float = RECORD_KEEP_DAYS,
    ) -> None:
        self.data_dir = str(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.path = os.path.join(self.data_dir, "chat.db")
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        #: Newest messages kept per conversation; a chat is not an archive.
        self.limit_messages = int(limit_messages)
        #: Where received attachments live (``media_rel`` is relative to it).
        #: Optional: without it a record still dedups, it just cannot hash an
        #: incoming file and falls back to ``(name, size)``.
        self.media_root = str(media_root or "")
        #: Optional shared :class:`~eversend.core.hashing.DigestCache` (the
        #: engine has one already: it hashed the very files this ledger talks
        #: about, so reusing it makes repeat sends free).
        self._digest_cache = digest_cache
        self._memory_digests: dict[str, str] = {}
        #: Retention policy for transfer records (charter C7).
        self.keep_records = int(keep_records)
        self.keep_days = float(keep_days)
        self._last_prune = 0.0
        self._db  # noqa: B018 - opens (and validates) the database now

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: the engine writes from transfer threads and
        # the UI reads from the Qt thread; every statement below is short and
        # serialised by _lock, which is what SQLite needs.
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        db.row_factory = sqlite3.Row
        # Schema + migration, retried briefly: another writer may hold the lock
        # for a moment while we open (two stores in one process, or a second
        # EverSend instance on the same data directory).
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                db.executescript(_SCHEMA)
                # Columns that arrived later than the first release.  A user's
                # existing chat.db must keep working (and keep its history), so
                # they are added here instead of asking anyone to delete the file.
                columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
                for name, ddl in _LATER_COLUMNS.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
                for statement in _LATER_INDEXES:
                    db.execute(statement)
                db.commit()
                last_error = None
                break
            except sqlite3.OperationalError as error:
                last_error = error
                time.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        # Startup is the only moment when "进行中" rows are provably leftovers:
        # nothing of ours can be in flight before this process started.  Rows
        # newer than RECORD_STALE_STATE_SECONDS are left alone, so a long
        # transfer that is still running is never declared dead.
        #
        # Both sweeps are best-effort on purpose: another process (or another
        # store in this one) may hold the write lock for a moment, and
        # "the chat log cannot be opened" is a far worse failure than "the trim
        # happens on the next write instead of right now" -- measured the hard
        # way, when a locked database made every engine in the test suite fail
        # to construct.
        try:
            self._converge_stale_locked(db)
            self._prune_locked(db)
        except sqlite3.OperationalError:
            pass
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

    def merge_conversations(self, keep_id: str, drop_ids: Iterable[str]) -> int:
        """把 ``drop_ids`` 里的消息并进 ``keep_id``，然后删掉那些空壳会话。

        为什么需要它：同一台手机在会话里的"成员 id"有好几种写法（App 报的设备号、
        浏览器的 地址|UA 摘要、早期没带设备号时的 地址:空摘要……）。写法一变，聊天
        就落到另一个会话里 —— 用户那边看到的现象是"电脑上不显示新消息"，其实消息
        都在隔壁那个同名会话里。这里把同属一台设备的几段并回一处。
        """
        keep = str(keep_id or "")
        drops = [str(x) for x in drop_ids if str(x) and str(x) != keep]
        if not keep or not drops:
            return 0
        marks = ",".join("?" for _ in drops)
        with self._lock:
            # 未读是"每个会话一行"的数字，被并掉的那几行的未读必须**加到保留的那行**
            # 上，否则 DELETE 一执行，用户没看过的消息就静悄悄不算数了（审计员在真实
            # 数据上量到 SUM(unread) 15→14，保留那段有 2 条未读却显示 0）。
            unread = self._db.execute(
                f"SELECT COALESCE(SUM(unread), 0) FROM conversations WHERE id IN ({marks})",
                tuple(drops),
            ).fetchone()
            carry = int((unread[0] if unread else 0) or 0)
            moved = self._db.execute(
                f"UPDATE messages SET conv = ? WHERE conv IN ({marks})", (keep, *drops)
            ).rowcount
            if carry:
                self._db.execute(
                    "UPDATE conversations SET unread = unread + ? WHERE id = ?", (carry, keep)
                )
            self._db.execute(
                f"DELETE FROM conversations WHERE id IN ({marks})", tuple(drops)
            )
            self._db.commit()
        if moved or carry:
            self._refresh_summary(keep)
        return int(moved or 0)

    def _refresh_summary(self, conv_id: str) -> None:
        """重算一个会话的"最后一条 / 更新时间"（并库之后要用）。

        未读不在这里算：它是**累加**出来的（见 :meth:`merge_conversations`），
        从消息表反推"哪些没看过"是猜的 —— 猜错就会把用户的未读抹掉。
        """
        with self._lock:
            row = self._db.execute(
                "SELECT text, kind, media_name, ts FROM messages WHERE conv = ?"
                " ORDER BY ts DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
            if row is None:
                return
            summary = str(row["text"] or "") or str(row["media_name"] or "") or {
                "image": "[图片]", "video": "[视频]", "voice": "[语音]", "file": "[文件]"
            }.get(str(row["kind"]), "")
            self._db.execute(
                "UPDATE conversations SET last_text = ?, updated = MAX(updated, ?) WHERE id = ?",
                (summary, float(row["ts"] or 0), conv_id),
            )
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
        #: 这条文件消息对应的分享条目 id（手机点「保存」走哪个下载任务）。
        #: 桌面"交给手机"时由发送路径传进来；普通聊天/接收消息留空。
        share_id: str = "",
        duration_ms: int = 0,
        direction: str = "out",
        state: str = "sent",
        message_id: str = "",
        ts: float | None = None,
        record_key: str = "",
        record: bool = True,
        peer: str = "",
        digest: str = "",
        attempts: int = 0,
        media_root: str = "",
    ) -> dict[str, Any]:
        """Append one message and bump its conversation.

        When the message *is* a transfer record (a file/voice/image/video, or a
        system note) it is written through the ledger instead of blindly
        appended: the row is found by
        ``(direction, peer_identity, content_identity)`` and **updated in
        place**, so the "进行中" write and the "已完成" write of one delivery
        stay one line, and sending the same file again only bumps ``attempts``
        (charter C8).  Pass ``record=False`` to force a new line (a chat
        attachment the user really did send twice and wants twice), or
        ``record_key=...`` to supply the key yourself.
        """
        if kind not in KINDS:
            kind = "text"
        direction = "in" if direction == "in" else "out"
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
            #: 分享条目 id（桌面发布给手机时记下）。服务端给手机下发时用它做
            #: 精确关联；为空时退回"按路径/名字+大小"的老匹配。
            "shareId": str(share_id),
            "durationMs": int(duration_ms or 0),
            "ts": float(ts if ts is not None else time.time()),
            "direction": direction,
            "state": str(state),
            "recordKey": "",
            "attempts": max(0, int(attempts or 0)),
        }
        key = str(record_key or "")
        if not key and record and is_record_kind(kind):
            key = self._record_key_for(conv_id, entry, peer=peer, digest=digest, media_root=media_root)
        entry["recordKey"] = key
        display = str(entry["text"] or entry["mediaName"] or "")
        with self._lock:
            db = self._db
            # 先保证会话存在：删会话和收文件是两条线程，删的那一刻正好有文件传完，
            # 消息就会落进一个已经不存在的会话里 —— 那条消息再也不会出现在界面上
            # （正是"孤儿 message"的来源）。这里补一行最小会话，宁可多一段空壳，
            # 也不让用户的记录变成查不到的孤儿；空壳由 purge/repair 收走。
            exists = db.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if exists is None:
                now = float(entry["ts"])
                db.execute(
                    "INSERT OR IGNORE INTO conversations (id, kind, title, members, created, updated)"
                    " VALUES (?, ?, '', ?, ?, ?)",
                    (
                        conv_id,
                        "group" if str(conv_id).startswith("g:") else "direct",
                        json.dumps(_members_from_conversation_id(conv_id), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            existing = None
            if key:
                existing = db.execute(
                    "SELECT * FROM messages WHERE record_key = ? ORDER BY ts DESC LIMIT 1", (key,)
                ).fetchone()
            # 幂等：同一条消息（同一个 id）第二次落库时**不能再把未读加一遍**。
            # 手机在弱网下重发、或以后加"失败重发"时，一次投递会变成两条未读 ——
            # 未读是一个计数器，服务端没有幂等键的话它只会越滚越大（审计员量到
            # 重复投递后 1 条消息、未读 2；裁剪之后未读甚至能超过消息条数）。
            # 判断必须在 INSERT **之前**做：INSERT 之后这条记录当然已经在了，
            # 再查就成了"每条消息都算重复"，未读永远加不上去（第一版就栽在这）。
            same_id = db.execute(
                "SELECT direction FROM messages WHERE id = ?", (entry["id"],)
            ).fetchone()
            if existing is not None:
                entry = self._merge_record_row(db, existing, entry)
            else:
                merged_attempts = max(1, int(entry.pop("attempts") or 0))
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
                    "share_id": entry["shareId"],
                    "duration_ms": entry["durationMs"],
                    "ts": entry["ts"],
                    "direction": entry["direction"],
                    "state": entry["state"],
                    "record_key": entry["recordKey"],
                    "attempts": merged_attempts,
                }
                db.execute(
                    "INSERT OR REPLACE INTO messages (id, conv, sender, sender_name, kind, text,"
                    " media_name, media_rel, media_size, media_mime, media_source, share_id,"
                    " duration_ms, ts, direction, state, record_key, attempts)"
                    " VALUES (:id, :conv, :sender, :sender_name, :kind, :text, :media_name, :media_rel,"
                    " :media_size, :media_mime, :media_source, :share_id, :duration_ms, :ts,"
                    " :direction, :state, :record_key, :attempts)",
                    row,
                )
                entry["attempts"] = merged_attempts
            preview = entry["text"] or {
                "image": "[图片]",
                "video": "[视频]",
                "voice": "[语音]",
                "file": "[文件]",
            }.get(entry["kind"], "")
            unread = 0 if (entry["direction"] == "out" or same_id is not None or existing is not None) else 1
            db.execute(
                "UPDATE conversations SET updated = MAX(updated, ?), last_text = ?,"
                " unread = unread + ? WHERE id = ?",
                (entry["ts"], preview[:200], unread, conv_id),
            )
            db.commit()
            self._trim(conv_id)
            self._maybe_prune()
        return entry

    def _record_key_for(
        self,
        conv_id: str,
        entry: dict[str, Any],
        *,
        peer: str = "",
        digest: str = "",
        media_root: str = "",
    ) -> str:
        """The ledger key of a message, or ``""`` when it has no identity.

        Called with the lock held (it reads the member list).
        """
        identity = ""
        if str(entry.get("kind") or "") == "system" and not entry.get("mediaName"):
            text = str(entry.get("text") or "").strip()
            identity = f"t:{text}" if text else ""
        else:
            source = str(entry.get("mediaSource") or "")
            rel = str(entry.get("mediaRel") or "")
            root = str(media_root or self.media_root or "")
            path = source or (os.path.join(root, rel) if root and rel else "")
            identity = content_identity(
                digest=digest,
                path=path,
                name=str(entry.get("mediaName") or ""),
                size=int(entry.get("mediaSize") or 0),
                cache=self._digest_cache_for(),
            )
        if not identity:
            return ""
        members: list[str] = []
        try:
            row = self._db.execute(
                "SELECT members FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if row is not None:
                members = _decode_members(row["members"])
        except sqlite3.Error:
            members = []
        who = peer_identity(
            conv_id,
            members,
            sender=str(entry.get("sender") or ""),
            direction=str(entry.get("direction") or "out"),
            explicit=peer,
        )
        return transfer_record_key(str(entry.get("direction") or "out"), who, identity)

    def _merge_record_row(
        self, db: sqlite3.Connection, existing: sqlite3.Row, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Converge ``entry`` onto the row that already owns its record key.

        The row keeps its original id (everything that already referenced it --
        a bubble, a ``set_state`` call, a web client -- keeps working), takes
        the newest outcome and timestamp, grows ``attempts`` only when a *new
        attempt* starts, and never loses a non-empty field to an empty one.
        """
        row_id = str(existing["id"])
        current_state = str(existing["state"] or "")
        attempts = max(1, int(existing["attempts"] or 0) or 1)
        new_state = str(entry.get("state") or "")
        # 计数规则（这就是"重复发送同一个文件"的判据）：
        #   · 写入"进行中" ⇒ 新的一次投递开始，+1；
        #   · 写入终态、原来还在进行中 ⇒ 这一次投递刚结束，不加；
        #   · 写入终态、原来已经是终态 ⇒ 又是同一个文件的另一次投递，+1。
        # 所以"进行中 + 已完成"永远算 1 次，"发两遍"算 2 次；两处代码各自把
        # 同一个终态写一遍才会多算一次（那是两条投递记录，宁可多算也不能少算）。
        if new_state in RECORD_ACTIVE_STATES:
            attempts += 1
        elif new_state in RECORD_TERMINAL_STATES and current_state in RECORD_TERMINAL_STATES:
            attempts += 1
        attempts = max(attempts, int(entry.get("attempts") or 0))
        merged = dict(entry)
        merged["id"] = row_id
        merged["conv"] = str(existing["conv"])
        merged["attempts"] = attempts
        upgrades = {
            "sender": "sender",
            "senderName": "sender_name",
            "kind": "kind",
            "text": "text",
            "mediaName": "media_name",
            "mediaRel": "media_rel",
            "mediaMime": "media_mime",
            "mediaSource": "media_source",
            #: 重新投递同一文件时换成**这一次**的分享 id（手机点老气泡「保存」
            #: 也不会去下已经作废的那一份，见 web/server.py 的 find_share_for_message）。
            "shareId": "share_id",
        }
        assignment: dict[str, Any] = {
            "state": str(entry.get("state") or current_state or "sent"),
            "ts": float(entry.get("ts") or 0.0),
            "attempts": attempts,
        }
        for field, column in upgrades.items():
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                assignment[column] = value
            else:
                merged[field] = existing[column]
        size = int(entry.get("mediaSize") or 0)
        assignment["media_size"] = size if size else int(existing["media_size"] or 0)
        merged["mediaSize"] = int(assignment["media_size"])
        duration = int(entry.get("durationMs") or 0)
        assignment["duration_ms"] = duration if duration else int(existing["duration_ms"] or 0)
        merged["durationMs"] = int(assignment["duration_ms"])
        # The row is the newest thing that happened to this file, so it also
        # carries the direction of that write (a "receiving" turn can follow a
        # "sending" one for the very same file).
        assignment["direction"] = str(entry.get("direction") or existing["direction"])
        merged["direction"] = str(assignment["direction"])
        merged["state"] = str(assignment["state"])
        merged["recordKey"] = str(existing["record_key"] or entry.get("recordKey") or "")
        sets = ", ".join(f"{column} = :{column}" for column in assignment)
        db.execute(f"UPDATE messages SET {sets} WHERE id = :id", {**assignment, "id": row_id})
        return merged


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
        # 未读封顶到"现在还剩多少条收到的消息"：裁剪把消息删了却不回落未读，
        # 一个长期不看的会话会显示得比它实际的消息还多。
        self._db.execute(
            "UPDATE conversations SET unread = MIN(unread, ("
            " SELECT COUNT(*) FROM messages WHERE conv = ? AND direction = 'in'))"
            " WHERE id = ?",
            (conv_id, conv_id),
        )
        self._db.commit()

    def _digest_cache_for(self) -> Any:
        """The digest cache to use for record identities.

        A shared one (the engine's) when the caller supplied it, otherwise a
        small in-process map: hashing the same file twice for two writes of one
        delivery must not cost two reads.
        """
        if self._digest_cache is not None:
            return self._digest_cache
        return _MemoryDigestCache(self._memory_digests)

    def _resolve_media(self, media_rel: str, media_source: str = "", media_root: str = "") -> str:
        """The best guess at where an attachment really is on this machine."""
        source = str(media_source or "")
        if source and os.path.exists(source):
            return source
        rel = str(media_rel or "")
        if not rel:
            return source
        root = str(media_root or self.media_root or "")
        if root:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate
        return source or rel

    # -- transfer ledger ---------------------------------------------------

    def record_transfer(
        self,
        conv_id: str,
        *,
        direction: str = "out",
        peer: str = "",
        path: str = "",
        name: str = "",
        size: int = 0,
        digest: str = "",
        kind: str = "",
        text: str = "",
        mime: str = "",
        media_rel: str = "",
        media_source: str = "",
        duration_ms: int = 0,
        state: str = "sending",
        ts: float | None = None,
        message_id: str = "",
        sender: str = "",
        sender_name: str = "",
        dedup: bool = True,
        media_root: str = "",
    ) -> dict[str, Any]:
        """Write **one** row for a delivery, wherever it is in its life cycle.

        This is the API every layer should use for the transfer ledger (the
        传输页 table and the file records in a conversation are the same
        account).  It is idempotent per delivery: call it when the transfer is
        requested (``state="sending"``) and again when it finishes
        (``"sent"``/``"failed"``/``"cancelled"``) and the table still has one
        line, because both writes compute the same
        ``(direction, peer, content)`` key.  Call it a second time for the same
        file and the row stays put while ``attempts`` grows -- 重发即合并.

        Returns the stored record (a message dict).  An empty dict means the
        call had nothing to record: no name and no path (a row like that would
        be a zero-width line, which is exactly the garbage the charter bans).
        """
        resolved = path or self._resolve_media(media_rel, media_source, media_root)
        label = str(name or "").strip() or os.path.basename(str(resolved or "").strip())
        if not label:
            return {}
        if not kind:
            kind = kind_for_name(label, mime)
        payload: dict[str, Any] = {
            "sender": sender,
            "sender_name": sender_name,
            "kind": kind,
            "text": text,
            "media_name": label,
            "media_rel": media_rel,
            "media_source": media_source or (resolved if direction == "out" else ""),
            "media_size": int(size or 0),
            "media_mime": mime,
            "duration_ms": int(duration_ms or 0),
            "direction": direction,
            "state": state,
            "message_id": message_id,
            "ts": ts,
            "peer": peer,
            "digest": digest,
        }
        if not payload["media_size"] and resolved and os.path.exists(str(resolved)):
            try:
                payload["media_size"] = int(os.path.getsize(str(resolved)))
            except OSError:
                pass
        return self.add_message(conv_id, record=dedup, media_root=media_root, **payload)

    def transfer_records(self, *, conv_id: str = "", limit: int = 0) -> list[dict[str, Any]]:
        """The ledger, newest first, one row per delivery.

        Rows are the media/system records (``record_key`` set, or written
        before keys existed), and a key never appears twice even if the
        database still holds duplicates from an older build -- the newest
        survives here, and :meth:`dedup_records` removes the rest for good.
        """
        marks = ",".join("?" for _ in RECORD_KINDS)
        sql = (
            f"SELECT * FROM messages WHERE (record_key <> '' OR kind IN ({marks}))"
        )
        params: list[Any] = list(RECORD_KINDS)
        if conv_id:
            sql += " AND conv = ?"
            params.append(conv_id)
        sql += " ORDER BY ts DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self._db.execute(sql, tuple(params)).fetchall()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            message = self._message_dict(row)
            key = str(message.get("recordKey") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            out.append(message)
        return out

    def dedup_records(self, *, conv_id: str = "") -> int:
        """Merge duplicate rows that are already in the database.

        Two passes, because historic data is messier than new writes:

        1. rows whose ``record_key`` is empty get one computed (an attachment
           that is still on disk is hashed, so two files that merely share a
           name are never folded together), then same-key rows are merged;
        2. the classic leftover -- a "进行中" row and the "已完成" row of one
           delivery written minutes apart before this module had a key -- is
           recognised by (same conversation, same direction, same file name,
           one active + one terminal, close in time) and merged too.

        Returns how many rows were removed.
        """
        removed = 0
        with self._lock:
            db = self._db
            marks = ",".join("?" for _ in RECORD_KINDS)
            sql = f"SELECT * FROM messages WHERE (record_key = '' AND kind IN ({marks}))"
            params: list[Any] = list(RECORD_KINDS)
            if conv_id:
                sql += " AND conv = ?"
                params.append(conv_id)
            rows = db.execute(sql + " ORDER BY ts", tuple(params)).fetchall()
            for row in rows:
                message = self._message_dict(row)
                key = self._record_key_of_row(message)
                if key:
                    db.execute("UPDATE messages SET record_key = ? WHERE id = ?", (key, row["id"]))
            db.commit()
            removed += self._merge_duplicate_keys(db, conv_id=conv_id)
            removed += self._merge_legacy_pairs(db, conv_id=conv_id)
            db.commit()
        return removed

    def _record_key_of_row(self, message: dict[str, Any]) -> str:
        """The ledger key a stored message should have (computed, not stored)."""
        kind = str(message.get("kind") or "")
        if kind == "system" and not message.get("mediaName"):
            text = str(message.get("text") or "").strip()
            identity = f"t:{text}" if text else ""
        else:
            resolved = self._resolve_media(
                str(message.get("mediaRel") or ""), str(message.get("mediaSource") or "")
            )
            identity = content_identity(
                path=resolved,
                name=str(message.get("mediaName") or ""),
                size=int(message.get("mediaSize") or 0),
                cache=self._digest_cache_for(),
            )
        if not identity:
            return ""
        conv_id = str(message.get("conv") or "")
        throwaway = {
            "sender": str(message.get("sender") or ""),
            "direction": str(message.get("direction") or "out"),
            "kind": kind,
            "mediaName": str(message.get("mediaName") or ""),
            "mediaSource": str(message.get("mediaSource") or ""),
            "mediaRel": str(message.get("mediaRel") or ""),
            "mediaSize": int(message.get("mediaSize") or 0),
            "text": str(message.get("text") or ""),
        }
        return self._record_key_for(conv_id, throwaway)

    def _merge_duplicate_keys(self, db: sqlite3.Connection, *, conv_id: str = "") -> int:
        """Fold rows that share a ``record_key`` into the newest one."""
        sql = "SELECT record_key, COUNT(*) AS n FROM messages WHERE record_key <> ''"
        params: tuple[Any, ...] = ()
        if conv_id:
            sql += " AND conv = ?"
            params = (conv_id,)
        sql += " GROUP BY record_key HAVING n > 1"
        duplicates = db.execute(sql, params).fetchall()
        removed = 0
        for duplicate in duplicates:
            key = str(duplicate["record_key"])
            rows = db.execute(
                "SELECT * FROM messages WHERE record_key = ? ORDER BY ts DESC", (key,)
            ).fetchall()
            if len(rows) < 2:
                continue
            keep = rows[0]
            attempts = sum(max(1, int(row["attempts"] or 0) or 1) for row in rows)
            db.execute(
                "UPDATE messages SET attempts = ? WHERE id = ?", (attempts, keep["id"])
            )
            for row in rows[1:]:
                db.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                removed += 1
            self._refresh_summary(str(keep["conv"]))
        return removed

    def _merge_legacy_pairs(self, db: sqlite3.Connection, *, conv_id: str = "") -> int:
        """Merge "进行中 + 已完成" pairs that predate the record key.

        Only rows that agree on conversation, direction and file name, where
        exactly one is still active and the other is terminal, and where the
        two are close enough in time to be the same delivery.  A different
        name, a different direction or two finished rows are left alone.
        """
        marks = ",".join("?" for _ in RECORD_KINDS)
        sql = f"SELECT * FROM messages WHERE kind IN ({marks}) AND media_name <> ''"
        params: list[Any] = list(RECORD_KINDS)
        if conv_id:
            sql += " AND conv = ?"
            params.append(conv_id)
        rows = db.execute(sql + " ORDER BY ts", tuple(params)).fetchall()
        buckets: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            bucket = buckets.setdefault(
                (str(row["conv"]), str(row["direction"]), str(row["media_name"])), []
            )
            bucket.append(row)
        removed = 0
        for (_conv, _direction, _name), group in buckets.items():
            if len(group) < 2:
                continue
            active = [r for r in group if str(r["state"] or "") in RECORD_ACTIVE_STATES]
            terminal = [r for r in group if str(r["state"] or "") in RECORD_TERMINAL_STATES]
            if not active or not terminal:
                continue
            # The newest terminal row is the outcome; every active row within
            # the delivery window belongs to it.
            outcome = terminal[-1]
            for row in active:
                if abs(float(row["ts"] or 0) - float(outcome["ts"] or 0)) > RECORD_PAIR_WINDOW:
                    continue
                if str(row["record_key"] or "") and str(row["record_key"]) != str(outcome["record_key"] or ""):
                    continue
                db.execute(
                    "UPDATE messages SET record_key = ?, attempts = attempts + 1 WHERE id = ?",
                    (str(outcome["record_key"] or ""), outcome["id"]),
                )
                db.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                removed += 1
        return removed

    # -- retention (charter C7) --------------------------------------------

    def prune_records(
        self, *, keep: int | None = None, days: float | None = None, now: float | None = None
    ) -> int:
        """Apply the retention policy to the transfer ledger.

        Defaults come from :mod:`eversend.core.constants`
        (:data:`RECORD_KEEP_COUNT` rows / :data:`RECORD_KEEP_DAYS` days) and can
        be overridden per call or per store.  Only the *ledger* is trimmed --
        a conversation's own text is the user's writing and is bounded per
        conversation by ``limit_messages`` instead.
        """
        with self._lock:
            removed = self._prune_locked(self._db, keep=keep, days=days, now=now)
            self._last_prune = time.monotonic()
        return removed

    def _prune_locked(
        self,
        db: sqlite3.Connection,
        *,
        keep: int | None = None,
        days: float | None = None,
        now: float | None = None,
    ) -> int:
        limit = self.keep_records if keep is None else int(keep)
        span = self.keep_days if days is None else float(days)
        moment = time.time() if now is None else float(now)
        removed = 0
        if span > 0:
            cutoff = moment - span * 86400.0
            cursor = db.execute(
                "DELETE FROM messages WHERE ts < ? AND (record_key <> '' OR kind IN"
                " (" + ",".join("?" for _ in RECORD_KINDS) + "))",
                (cutoff, *RECORD_KINDS),
            )
            removed += int(cursor.rowcount or 0)
        if limit > 0:
            cursor = db.execute(
                "DELETE FROM messages WHERE id IN ("
                " SELECT id FROM messages WHERE record_key <> '' OR kind IN"
                " (" + ",".join("?" for _ in RECORD_KINDS) + ")"
                " ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (*RECORD_KINDS, limit),
            )
            removed += int(cursor.rowcount or 0)
        if removed:
            db.execute(
                "UPDATE conversations SET unread = MIN(unread, ("
                " SELECT COUNT(*) FROM messages WHERE messages.conv = conversations.id"
                " AND direction = 'in'))"
            )
            db.commit()
            self._refresh_all_summaries(db)
        return removed

    def _refresh_all_summaries(self, db: sqlite3.Connection) -> None:
        """Recompute each conversation's last line after rows disappeared."""
        for row in db.execute("SELECT id FROM conversations").fetchall():
            conv_id = str(row["id"])
            last = db.execute(
                "SELECT text, kind, media_name, ts FROM messages WHERE conv = ?"
                " ORDER BY ts DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
            if last is None:
                continue
            summary = str(last["text"] or "") or str(last["media_name"] or "") or {
                "image": "[图片]", "video": "[视频]", "voice": "[语音]", "file": "[文件]"
            }.get(str(last["kind"]), "")
            db.execute(
                "UPDATE conversations SET last_text = ?, updated = MAX(updated, ?) WHERE id = ?",
                (summary[:200], float(last["ts"] or 0), conv_id),
            )
        db.commit()

    def _maybe_prune(self) -> None:
        """Write-path retention: trim, but not on every single insert."""
        now = time.monotonic()
        if now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            self._prune_locked(self._db)
        except sqlite3.Error:
            pass

    def converge_stale(
        self, *, max_age: float | None = None, now: float | None = None
    ) -> int:
        """Close records that are stuck "进行中" and can never finish.

        A row written when a transfer started, whose owner then died (crash,
        restart, dropped link), would otherwise say 「传输中」 forever -- the
        user's "记录里那些永远传不完的行".  Age is the guard: a transfer that
        is genuinely still running is younger than
        :data:`RECORD_STALE_STATE_SECONDS`, and rows this function closes are
        marked ``failed`` (never silently dropped) so the界面 can say why.
        """
        with self._lock:
            return self._converge_stale_locked(self._db, max_age=max_age, now=now)

    def _converge_stale_locked(
        self,
        db: sqlite3.Connection,
        *,
        max_age: float | None = None,
        now: float | None = None,
    ) -> int:
        age = RECORD_STALE_STATE_SECONDS if max_age is None else float(max_age)
        if age <= 0:
            return 0
        moment = time.time() if now is None else float(now)
        marks = ",".join("?" for _ in RECORD_ACTIVE_STATES)
        cursor = db.execute(
            f"UPDATE messages SET state = 'failed' WHERE state IN ({marks}) AND ts < ?",
            (*RECORD_ACTIVE_STATES, moment - age),
        )
        if cursor.rowcount:
            db.commit()
        return int(cursor.rowcount or 0)

    # -- 清空记录 ----------------------------------------------------------

    def purge_records(
        self,
        *,
        include_chat: bool = False,
        media_root: str = "",
        extra_dirs: Iterable[str] = (),
        sweep: bool = True,
    ) -> dict[str, Any]:
        """「清空记录」: everything the ledger owns, in one pass.

        * the transfer rows themselves (and, with ``include_chat=True``, the
          conversation text as well -- the desktop's 「清空会话」 is the
          per-conversation version of that);
        * every ``__pending__`` leftover, wherever it hid;
        * conversations that no longer hold a single message, and conversations
          with no identifiable peer (the blank device rows);
        * chunkstore journals and orphan ``.part`` files, in every folder the
          resume index knows about plus ``media_root``;
        * the same ``__pending__`` garbage in the identity registries.

        Received *files* are never deleted -- only the records about them.
        """
        report: dict[str, Any] = {}
        with self._lock:
            db = self._db
            marks = ",".join("?" for _ in RECORD_KINDS)
            if include_chat:
                report["messages"] = int(db.execute("DELETE FROM messages").rowcount or 0)
            else:
                report["records"] = int(
                    db.execute(
                        f"DELETE FROM messages WHERE record_key <> '' OR kind IN ({marks})",
                        tuple(RECORD_KINDS),
                    ).rowcount
                    or 0
                )
                report["pending"] = int(
                    db.execute(
                        "DELETE FROM messages WHERE text LIKE ? OR media_name LIKE ?"
                        " OR media_rel LIKE ? OR media_source LIKE ? OR id LIKE ?"
                        " OR sender_name LIKE ?",
                        (f"%{PENDING_MARKER}%",) * 6,
                    ).rowcount
                    or 0
                )
                report["empty"] = int(
                    db.execute(
                        "DELETE FROM messages WHERE (kind = 'text' AND TRIM(text) = ''"
                        " AND media_name = '')"
                    ).rowcount
                    or 0
                )
            report["conversations"] = int(
                db.execute(
                    "DELETE FROM conversations WHERE id NOT IN (SELECT DISTINCT conv FROM messages)"
                ).rowcount
                or 0
            )
            report["empty_devices"] = int(
                db.execute(
                    "DELETE FROM conversations WHERE members IN ('', '[]', '[\"\"]')"
                    " OR TRIM(members) = ''"
                ).rowcount
                or 0
            )
            db.commit()
        if sweep:
            report["sidecars"] = self._sweep_sidecars(media_root=media_root, extra_dirs=extra_dirs)
        report["identities"] = purge_pending_identity_records(self.data_dir)
        return report

    def _sweep_sidecars(self, *, media_root: str = "", extra_dirs: Iterable[str] = ()) -> list[str]:
        """Delete orphan ``.part`` files and their journals."""
        from .chunkstore import ResumeIndex

        dirs = [d for d in (media_root or self.media_root, *extra_dirs) if d]
        try:
            index = ResumeIndex(self.data_dir, extra_dirs=dirs)
            return index.sweep(max_age_seconds=0)
        except Exception:  # pragma: no cover - cleanup must never break 清空记录
            return []

    # -- 数据自检 ----------------------------------------------------------

    def audit_records(self, *, media_root: str = "", now: float | None = None) -> dict[str, Any]:
        """Count the garbage the charter forbids, without changing anything.

        Every key is a defect class with a matching repair in
        :meth:`repair_records`; the numbers are printed by
        ``tools/records_data_check.py --data-dir`` so a claim can be checked
        instead of believed.
        """
        moment = time.time() if now is None else float(now)
        root = str(media_root or self.media_root or "")
        with self._lock:
            db = self._db
            report: dict[str, Any] = {}
            report["messages"] = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            report["conversations"] = int(
                db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            )
            marks = ",".join("?" for _ in RECORD_KINDS)
            report["transfer_records"] = int(
                db.execute(
                    f"SELECT COUNT(*) FROM messages WHERE record_key <> '' OR kind IN ({marks})",
                    tuple(RECORD_KINDS),
                ).fetchone()[0]
            )
            report["orphan_messages"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE conv NOT IN (SELECT id FROM conversations)"
                ).fetchone()[0]
            )
            report["empty_conversations"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM conversations WHERE id NOT IN"
                    " (SELECT DISTINCT conv FROM messages)"
                ).fetchone()[0]
            )
            report["nameless_devices"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM conversations WHERE TRIM(members) IN ('', '[]')"
                    " AND kind <> 'group'"
                ).fetchone()[0]
            )
            report["duplicate_records"] = int(
                len(
                    db.execute(
                        "SELECT record_key FROM messages WHERE record_key <> ''"
                        " GROUP BY record_key HAVING COUNT(*) > 1"
                    ).fetchall()
                )
            )
            report["blank_records"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE (kind IN"
                    " ('image','video','voice','file') AND TRIM(media_name) = '')"
                    " OR (kind = 'text' AND TRIM(text) = '' AND TRIM(media_name) = '')"
                ).fetchone()[0]
            )
            report["pending_leaks"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM messages WHERE text LIKE ? OR media_name LIKE ?"
                    " OR media_rel LIKE ? OR media_source LIKE ? OR id LIKE ? OR sender_name LIKE ?",
                    (f"%{PENDING_MARKER}%",) * 6,
                ).fetchone()[0]
            )
            active_marks = ",".join("?" for _ in RECORD_ACTIVE_STATES)
            report["stale_records"] = int(
                db.execute(
                    f"SELECT COUNT(*) FROM messages WHERE state IN ({active_marks}) AND ts < ?",
                    (*RECORD_ACTIVE_STATES, moment - RECORD_STALE_STATE_SECONDS),
                ).fetchone()[0]
            )
            dangling: list[str] = []
            missing = 0
            for row in db.execute(
                "SELECT media_rel, media_source FROM messages WHERE TRIM(media_rel) <> ''"
                " OR TRIM(media_source) <> ''"
            ).fetchall():
                resolved = self._resolve_media(str(row["media_rel"] or ""), str(row["media_source"] or ""))
                if resolved and not os.path.exists(resolved):
                    missing += 1
                    if len(dangling) < 10:
                        dangling.append(resolved)
            report["missing_attachments"] = missing
            report["dangling_examples"] = dangling
            rows = db.execute("SELECT id, members FROM conversations").fetchall()
            peers: set[str] = set()
            for row in rows:
                peers.update(_decode_members(row["members"]))
            self_id = self._self_device_id()
            peers.discard(self_id)
            if peers:
                known = known_peer_ids(self.data_dir)
                report["unknown_peers"] = len([p for p in peers if p and p not in known])
            # 同一件坏事的第二个名字：验收套件用另一组措辞断言，两个键都在这里
            # 给出来，免得"名字对不上"变成一次空洞的通过（或空洞的失败）。
            report["duplicate_record_keys"] = report["duplicate_records"]
            report["dead_paths"] = report["missing_attachments"]
            report["zero_width_rows"] = report["blank_records"]
        return report

    def _self_device_id(self) -> str:
        """This machine's own device id (never a peer of itself)."""
        data = _load_json_file(identity_file_paths(self.data_dir)["identity"])
        if isinstance(data, dict):
            return str(data.get("deviceId") or "").strip()
        return ""

    def repair_records(self, *, media_root: str = "", now: float | None = None) -> dict[str, Any]:
        """Fix everything :meth:`audit_records` finds, and report the counts.

        The order matters: orphan messages are re-attached to a conversation
        first (their history is kept, never deleted), then duplicate rows are
        merged, then rows with nothing to show are dropped, then stuck
        "进行中" rows are closed, and finally the empty shells disappear.
        Attachments whose file is gone keep their name and size but lose the
        path that points nowhere, so no row claims a file that is not there.
        """
        moment = time.time() if now is None else float(now)
        fixed: dict[str, Any] = {}
        with self._lock:
            db = self._db
            fixed["orphan_messages"] = self._reattach_orphans(db)
            fixed["duplicate_records"] = self.dedup_records()
            fixed["blank_records"] = int(
                db.execute(
                    "DELETE FROM messages WHERE (kind IN ('image','video','voice','file')"
                    " AND TRIM(media_name) = '') OR (kind = 'text' AND TRIM(text) = ''"
                    " AND TRIM(media_name) = '')"
                ).rowcount
                or 0
            )
            fixed["pending_leaks"] = self._clean_pending(db)
            db.commit()
            fixed["stale_records"] = self._converge_stale_locked(db, now=moment)
            fixed["missing_attachments"] = self._clear_dangling(db, media_root=media_root)
            fixed["empty_conversations"] = int(
                db.execute(
                    "DELETE FROM conversations WHERE id NOT IN (SELECT DISTINCT conv FROM messages)"
                ).rowcount
                or 0
            )
            fixed["nameless_devices"] = int(
                db.execute(
                    "DELETE FROM conversations WHERE TRIM(members) IN ('', '[]')"
                    " AND kind <> 'group' AND id NOT IN (SELECT DISTINCT conv FROM messages)"
                ).rowcount
                or 0
            )
            db.commit()
            self._refresh_all_summaries(db)
        return fixed

    def _reattach_orphans(self, db: sqlite3.Connection) -> int:
        """Give messages without a conversation one (their history survives)."""
        rows = db.execute(
            "SELECT DISTINCT conv FROM messages WHERE conv NOT IN (SELECT id FROM conversations)"
        ).fetchall()
        now = time.time()
        for row in rows:
            conv_id = str(row["conv"])
            members = _members_from_conversation_id(conv_id)
            db.execute(
                "INSERT OR IGNORE INTO conversations (id, kind, title, members, created, updated)"
                " VALUES (?, ?, '', ?, ?, ?)",
                (
                    conv_id,
                    "group" if conv_id.startswith("g:") else "direct",
                    json.dumps(members, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return len(rows)

    def _clean_pending(self, db: sqlite3.Connection) -> int:
        """Strip ``__pending__`` placeholders that leaked into the database."""
        changed = 0
        rows = db.execute(
            "SELECT * FROM messages WHERE text LIKE ? OR media_name LIKE ? OR media_rel LIKE ?"
            " OR media_source LIKE ? OR id LIKE ? OR sender_name LIKE ?",
            (f"%{PENDING_MARKER}%",) * 6,
        ).fetchall()
        for row in rows:
            updates: dict[str, Any] = {}
            for column in ("text", "media_name", "media_rel", "media_source", "sender_name"):
                value = str(row[column] or "")
                if PENDING_MARKER in value:
                    updates[column] = _strip_pending(value)
            if PENDING_MARKER in str(row["id"]):
                updates["id"] = uuid.uuid4().hex[:20]
            if not updates:
                continue
            assignment = ", ".join(f"{column} = :{column}" for column in updates)
            db.execute(
                f"UPDATE messages SET {assignment} WHERE id = :__old_id",
                {**updates, "__old_id": row["id"]},
            )
            changed += 1
        # A row that lost everything it had is not a record any more.
        changed += int(
            db.execute(
                "DELETE FROM messages WHERE kind IN ('image','video','voice','file')"
                " AND TRIM(media_name) = '' AND TRIM(text) = ''"
            ).rowcount
            or 0
        )
        return changed

    def _clear_dangling(self, db: sqlite3.Connection, *, media_root: str = "") -> int:
        """Stop rows from pointing at files that are not on this machine."""
        rows = db.execute(
            "SELECT id, media_rel, media_source FROM messages"
            " WHERE TRIM(media_rel) <> '' OR TRIM(media_source) <> ''"
        ).fetchall()
        cleared = 0
        for row in rows:
            resolved = self._resolve_media(
                str(row["media_rel"] or ""), str(row["media_source"] or ""), media_root
            )
            if resolved and not os.path.exists(resolved):
                db.execute(
                    "UPDATE messages SET media_rel = '', media_source = '' WHERE id = ?",
                    (row["id"],),
                )
                cleared += 1
        return cleared

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
            #: 分享条目 id：手机点气泡「保存」时用它做**精确**关联（见 web/server.py
            #: 的 find_share_for_message；为空才退回按路径/名字+大小匹配）。
            "shareId": (row["share_id"] if "share_id" in row.keys() else "") or "",
            "durationMs": int(row["duration_ms"] or 0),
            "ts": row["ts"],
            "direction": row["direction"],
            "state": row["state"],
            # The ledger key and how many times this one file has been sent to
            # this one peer (charter C8: 重复发送合并成一行并累加).  A chat
            # line has neither, and that is exactly how the two are told apart.
            "recordKey": (row["record_key"] if "record_key" in row.keys() else "") or "",
            "attempts": int((row["attempts"] if "attempts" in row.keys() else 0) or 0),
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


def _load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _save_json_file(path: str, payload: Any) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def identity_file_paths(data_dir: str) -> dict[str, str]:
    """The three identity files this module knows how to keep clean."""
    return {
        "web_clients": os.path.join(str(data_dir), "web_clients.json"),
        "known_devices": os.path.join(str(data_dir), "known_devices.json"),
        "identity": os.path.join(str(data_dir), "identity.json"),
    }


def known_peer_ids(data_dir: str) -> set[str]:
    """Every id a registered client can appear as (for name lookups).

    Read-only and tolerant: a half-written or hand-edited registry must not
    stop a self-check.  Includes the aliases, so a conversation written under
    an older spelling still resolves to a device.
    """
    out: set[str] = set()
    path = identity_file_paths(data_dir)["web_clients"]
    data = _load_json_file(path)
    clients = data.get("clients") if isinstance(data, dict) else None
    for client in clients if isinstance(clients, list) else []:
        if isinstance(client, dict):
            out |= client_aliases(client)
    known = _load_json_file(identity_file_paths(data_dir)["known_devices"])
    if isinstance(known, dict):
        for key, snapshot in known.items():
            out.add(str(key))
            if isinstance(snapshot, dict):
                out.add(str(snapshot.get("device_id") or ""))
    return {value for value in out if value}


def audit_identity_files(data_dir: str) -> dict[str, Any]:
    """Count the duplicate/nameless device rows stored in the registry files."""
    from .model import device_record_label, merge_device_records

    paths = identity_file_paths(data_dir)
    report: dict[str, Any] = {}
    data = _load_json_file(paths["web_clients"])
    clients = data.get("clients") if isinstance(data, dict) else None
    clients = [c for c in clients if isinstance(c, dict)] if isinstance(clients, list) else []
    merged, merge_report = merge_device_records(clients)
    report["web_clients"] = len(clients)
    report["web_clients_after"] = len(merged)
    report["web_clients_duplicates"] = int(merge_report.get("merged") or 0)
    report["web_clients_duplicate_groups"] = merge_report.get("groups") or []
    report["web_clients_empty_names"] = len(
        [c for c in clients if not str(c.get("label") or c.get("name") or "").strip()]
    )
    report["web_clients_pending"] = len(
        [c for c in clients if any(is_pending_garbage(v) for v in (c.get("key"), c.get("deviceId")))]
    )
    report["web_clients_labels"] = sorted({device_record_label(c) for c in clients})

    known = _load_json_file(paths["known_devices"])
    rows = [dict(value, key=str(key)) for key, value in known.items() if isinstance(value, dict)] if isinstance(known, dict) else []
    report["known_devices"] = len(rows)
    report["known_devices_empty_names"] = len(
        [r for r in rows if not str(r.get("name") or r.get("label") or "").strip()]
    )
    seen: dict[str, int] = {}
    for row in rows:
        identity = str(row.get("device_id") or row.get("key") or "")
        seen[identity] = seen.get(identity, 0) + 1
    report["known_devices_duplicates"] = len([k for k, n in seen.items() if k and n > 1])

    ident = _load_json_file(paths["identity"])
    if isinstance(ident, dict):
        device_id = str(ident.get("deviceId") or "")
        report["identity_device_id"] = device_id
        report["identity_empty_id"] = 0 if device_id else 1
        report["identity_empty_name"] = 0 if str(ident.get("name") or "").strip() else 1
    return report


def repair_identity_files(data_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """One row per device in ``web_clients.json`` / ``known_devices.json``.

    Charter C4: 同一台设备在列表里只有一行。  Records are folded by stable
    identity (:func:`eversend.core.model.merge_device_records`); the folded-away
    keys stay in ``aliases`` and their old display names in ``aliasLabels``, so
    the conversations written under those spellings keep their titles.

    ``identity.json`` is *this* machine's key pair: it is never rewritten here
    beyond filling in a name that went missing -- regenerating it would change
    who this device is.
    """
    from .model import (
        default_device_name,
        device_record_label,
        merge_device_records,
        prune_device_records,
    )

    paths = identity_file_paths(data_dir)
    report: dict[str, Any] = {}

    data = _load_json_file(paths["web_clients"])
    clients = data.get("clients") if isinstance(data, dict) else None
    clients = [c for c in clients if isinstance(c, dict)] if isinstance(clients, list) else []
    if clients:
        cleaned: list[dict[str, Any]] = []
        for client in clients:
            row = dict(client)
            row.pop("__pending__", None)
            for field in ("key", "deviceId"):
                if is_pending_garbage(row.get(field)):
                    row[field] = ""
            if not str(row.get("key") or "").strip() and str(row.get("deviceId") or "").strip():
                row["key"] = "android:" + str(device_record_bare(row["deviceId"]))
            if not str(row.get("key") or "").strip():
                report.setdefault("web_clients_dropped", 0)
                report["web_clients_dropped"] = int(report["web_clients_dropped"]) + 1
                continue
            aliases = row.get("aliases")
            if isinstance(aliases, list):
                row["aliases"] = [a for a in aliases if str(a or "").strip() and not is_pending_garbage(a)]
                if not row["aliases"]:
                    row.pop("aliases", None)
            cleaned.append(row)
        merged, merge_report = merge_device_records(cleaned)
        # 幽灵行回收：同一台机器重装/换数据目录会产生新的 deviceId，上一份记录
        # 还留在盘上，越攒越多（用户说的"同一个设备重复显示"）。只回收**能自证
        # 是同一台机器**的行：同地址+同端口，或同地址+同平台且已离线超过 TTL。
        merged, prune_report = prune_device_records(
            merged, now=time.time(), offline_ttl=DEVICE_RECORD_TTL, limit=DEVICE_RECORD_LIMIT
        )
        report["web_clients_before"] = len(clients)
        report["web_clients_after"] = len(merged)
        report["web_clients_merged"] = int(merge_report.get("merged") or 0)
        report["web_clients_renamed"] = int(merge_report.get("renamed") or 0)
        report["web_clients_pruned"] = int(prune_report.get("dropped") or 0)
        report["web_clients_pruned_reasons"] = prune_report.get("reasons") or []
        if not dry_run and (len(merged) != len(clients) or any(
            str(merged[i].get("label")) != str(clients[i].get("label")) for i in range(min(len(merged), len(clients)))
        )):
            _save_json_file(paths["web_clients"], {"clients": merged})
            report["web_clients_written"] = True

    known = _load_json_file(paths["known_devices"])
    if isinstance(known, dict) and known:
        rows: list[dict[str, Any]] = []
        for key, snapshot in known.items():
            if not isinstance(snapshot, dict):
                continue
            row = dict(snapshot, key=str(key))
            if not str(row.get("device_id") or "").strip():
                row["device_id"] = str(key)
            rows.append(row)
        kept, prune_report = prune_device_records(
            rows, now=time.time(), offline_ttl=DEVICE_RECORD_TTL, limit=DEVICE_RECORD_LIMIT
        )
        buckets: dict[str, dict[str, Any]] = {}
        dropped = int(prune_report.get("dropped") or 0)
        filled = 0
        for row in kept:
            identity = str(row.get("device_id") or row.get("key") or "").strip()
            if is_pending_garbage(identity) or not identity:
                dropped += 1
                continue
            # 空名字设备行：没有名字的行在列表里就是一条空白，给个能读的兜底名
            # （优先用它自己的名字，其次旧别名当时显示的名字，再次按平台推）。
            label = device_record_label(row)
            if str(row.get("name") or "") != label:
                row["name"] = label
                filled += 1
            previous = buckets.get(identity)
            if previous is None or float(row.get("lastSeen") or 0) > float(previous.get("lastSeen") or 0):
                if previous is not None:
                    dropped += 1
                buckets[identity] = row
            else:
                dropped += 1
        report["known_devices_before"] = len(known)
        report["known_devices_after"] = len(buckets)
        report["known_devices_merged"] = dropped
        report["known_devices_renamed"] = filled
        report["known_devices_pruned_reasons"] = prune_report.get("reasons") or []
        if not dry_run and (dropped or filled or sorted(buckets) != sorted(known)):
            payload = {}
            for identity, row in buckets.items():
                row = dict(row)
                row["device_id"] = identity
                row.pop("key", None)
                payload[identity] = row
            _save_json_file(paths["known_devices"], payload)
            report["known_devices_written"] = True

    ident = _load_json_file(paths["identity"])
    if isinstance(ident, dict) and ident:
        if not str(ident.get("name") or "").strip():
            ident["name"] = default_device_name()
            if not dry_run:
                _save_json_file(paths["identity"], ident)
            report["identity_name_filled"] = True
    return report


def device_record_bare(value: Any) -> str:
    """Local shim so this module does not re-implement the model's prefixes."""
    from .model import bare_device_id

    return bare_device_id(value)


def purge_pending_identity_records(data_dir: str) -> int:
    """Drop ``__pending__`` leftovers from the registry files (nothing else)."""
    removed = 0
    paths = identity_file_paths(data_dir)
    data = _load_json_file(paths["web_clients"])
    clients = data.get("clients") if isinstance(data, dict) else None
    if isinstance(clients, list):
        kept = []
        for client in clients:
            if not isinstance(client, dict):
                continue
            if is_pending_garbage(client.get("key")) or is_pending_garbage(client.get("deviceId")):
                removed += 1
                continue
            aliases = client.get("aliases")
            if isinstance(aliases, list):
                fresh = [a for a in aliases if not is_pending_garbage(a)]
                removed += len(aliases) - len(fresh)
                if fresh:
                    client["aliases"] = fresh
                else:
                    client.pop("aliases", None)
            kept.append(client)
        if removed:
            _save_json_file(paths["web_clients"], {"clients": kept})
    return removed


def client_aliases(client: dict[str, Any]) -> set[str]:
    """All the ids one connected client can appear as inside a conversation.

    手机的 id 有三副面孔：App 用 ``/api/hello`` 登记成 ``android:<deviceId>``，
    发消息时请求头里带的是 ``web:<deviceId>``，而老版本（只被当成浏览器）的
    记录键是 ``地址|UA摘要``。会话里存的成员 id 是当时那个，光按注册表键去
    查名字就会查不到 —— 用户看到的就是"某某的会话标题是一串 d:xxx"。
    这里把几种写法都列出来，谁查都能查到。
    """
    out: set[str] = set()
    values: list[Any] = [client.get("key"), client.get("deviceId")]
    # 服务端归并重复设备时，会把被并掉的旧身份记在 ``aliases`` 里（老会话的成员写
    # 法就是那些旧键）。不认它们，老会话的名字会从「安卓 App」掉成「未知设备」。
    aliases = client.get("aliases")
    if isinstance(aliases, (list, tuple, set)):
        values.extend(aliases)
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        out.add(text)
        bare = text.split(":", 1)[1] if text.startswith(("web:", "android:")) else text
        if bare:
            out.update({bare, f"web:{bare}", f"android:{bare}"})
    return out


def conversation_title(
    conversation: dict[str, Any], labels: dict[str, str] | None = None, self_id: str = ""
) -> str:
    """A conversation as a human reads it: 对方的名字 / 群名。

    会话 id 是数据库主键（``d:8765510e214069``），不是给人看的东西。三个客户端
    （电脑端、网页版、安卓 App）都要显示同一个名字，所以这段判断只写一份：
    ``labels`` 是"成员 id → 名字（系统 · 版本 · IP）"，谁手上有设备信息谁去填。

    一对一显示对方的名字，群聊显示群名（没起名就写人数），名字实在拿不到
    （对方从没连过、记录也没了）才退回一句"未知设备"。
    """
    known = labels or {}

    def label(member: str) -> str:
        # 列表里只要名字，标签里那串「（系统 · 版本 · IP）」是给详情用的。
        # 查不到就说"未知设备"：把 id 前几位摆出来（``web:127.0.``）比空着更
        # 让人看不懂，用户要的是名字。
        return str(known.get(member) or "").split("（")[0] or "未知设备"

    members = [str(m) for m in (conversation.get("members") or []) if str(m)]
    conv_id = str(conversation.get("id") or "")
    if conversation.get("kind") == "group" or conv_id.startswith("g:"):
        title = str(conversation.get("title") or "").strip()
        if title:
            return title
        return f"群聊（{len(members) or 1} 人）"
    others = [m for m in members if m != self_id]
    if not others:
        # 只有自己，或者成员的记录已经没了。
        return label(self_id) if members else "（只有自己）"
    return label(others[0]) or "未知设备"


def conversation_people(
    conversation: dict[str, Any], labels: dict[str, str] | None = None, self_id: str = ""
) -> str:
    """Who is in this conversation, as names (never raw ids)."""
    known = labels or {}
    members = [str(m) for m in (conversation.get("members") or []) if str(m)]
    others = [known.get(m) or "未知设备" for m in members if m != self_id]
    return "、".join(others) if others else "只有本机"


__all__ = [
    "ChatStore",
    "client_aliases",
    "conversation_dirname",
    "conversation_people",
    "conversation_title",
    "KINDS",
    "MEDIA_DIRNAME",
    "MEDIA_KINDS",
    "direct_conversation_id",
    "media_relpath",
    "new_group_id",
]
