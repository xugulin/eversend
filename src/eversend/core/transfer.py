"""Send and receive sessions: the actual file transfer logic.

Design notes
------------

**The receiver drives the transfer.**  Every chunk is fetched because the
*receiver* asked for it, not because the sender decided to push it.  Three
properties fall out of that choice, and all three matter:

* **Resume is free.**  The receiver owns the resume journal, so after any
  interruption it simply does not ask for the chunks it already has.  No
  negotiation, no bitmap exchange, no "start over from zero".
* **Work is balanced automatically.**  Streams pull from one shared queue, so
  a fast connection takes more chunks than a slow one without any tuning.
  A stream that dies leaves its in-flight chunks to be re-queued and picked up
  by the others.
* **The sender stays stateless.**  It just answers questions about its file.
  It holds one chunk in memory at a time no matter how many streams exist.

**Chunks are handed out in file order.**  A shared counter produces ascending
chunk indices, so parallel streams collectively write the file front to back.
That turns random writes into (mostly) sequential ones -- which is the
difference between 120 MB/s and 20 MB/s on a spinning disk -- and lets the
:class:`~eversend.core.hashing.IncrementalFileHasher` fold bytes into the
whole-file digest as they arrive instead of re-reading the file at the end.

**Failure is expected, not exceptional.**  A chunk that times out is put back
on the queue.  A stream that dies is dropped and its work redistributed.  A
file whose digest mismatches is repaired chunk by chunk rather than resent.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import connection as conn_mod
from . import crypto
from .chunkstore import ChunkStoreError, PartFile
from .connection import ChunkFrame, Connection
from .constants import (
    DEFAULT_STREAMS,
    DEFAULT_WINDOW_BYTES,
    MAX_STREAMS,
    MSG_ATTACH,
    MSG_CANCEL,
    CHUNK_DEADLINE_FLOOR_BPS,
    CHUNK_REQUEST_TIMEOUT,
    MSG_CHUNK_REQ,
    MSG_DIGEST_REQ,
    MSG_ERROR,
    MSG_FILE_DIGEST,
    MSG_FILE_DONE,
    MSG_FILE_VERIFIED,
    MSG_NEED_STREAMS,
    MSG_OFFER,
    MSG_OFFER_ACK,
    MSG_OFFER_REJECT,
    MSG_PROGRESS,
    MSG_TRANSFER_DONE,
    OFFER_TIMEOUT,
    STREAM_IDLE_TIMEOUT,
)
from .fileio import PositionedFile
from .framing import ConnectionClosed, Frame, ProtocolError
from .hashing import IncrementalFileHasher, crc32
from .model import (
    DeviceInfo,
    FileEntry,
    TransferItem,
    TransferStats,
    new_transfer_id,
    safe_join,
    unique_path,
)


class TransferCancelled(Exception):
    """Raised inside a session when the user or the peer aborted it."""


class TransferFailed(Exception):
    """Raised when a transfer cannot proceed (disk full, I/O error, ...)."""


@dataclass
class OfferDecision:
    """What the receiving side decided to do with an offer."""

    accepted: list[int]
    rejected: list[tuple[int, str]] = field(default_factory=list)
    streams: int = DEFAULT_STREAMS
    save_dir: str = ""
    pin_ok: bool = True
    reason: str = ""


# ---------------------------------------------------------------------------
# Chunk reading (sender side)
# ---------------------------------------------------------------------------


class FileReader:
    """Reads chunks out of one source file with minimal allocation.

    Reads go through :mod:`~eversend.core.fileio`, which fills a preallocated
    buffer so a chunk costs one kernel-to-user copy and **no** Python
    allocation (unlike reading into a fresh multi-megabyte ``bytes`` object
    per chunk -- tens of thousands of them for a large transfer) and, crucially,
    works on Windows as well as POSIX.

    One reader serves every stream that is pulling from the same file, so the
    read buffer **must** be thread local: two streams reading different chunks
    into one shared buffer would each send whatever the other wrote last, which
    is silent data corruption.  ``preadv`` itself is thread safe (it carries
    its own offset), so no lock is needed.
    """

    __slots__ = ("path", "size", "chunk_size", "_file", "_local")

    def __init__(self, path: str, size: int, chunk_size: int) -> None:
        self.path = path
        self.size = size
        self.chunk_size = chunk_size
        # Positioned reads rather than a shared descriptor: on Windows the
        # standard library has neither pread nor preadv, and the naive
        # seek+read substitution would have every stream trampling the one
        # file position.
        self._file = PositionedFile(path)
        self._local = threading.local()

    def read_chunk(self, index: int, length: int) -> memoryview:
        """Return a view of chunk ``index`` inside this thread's buffer.

        The view stays valid until the next call **on the same thread**, which
        is exactly the lifetime the stream loops need: read, hash, send.
        """
        if length <= 0:
            return memoryview(b"")
        offset = index * self.chunk_size
        buffer = getattr(self._local, "buffer", None)
        if buffer is None or len(buffer) < length:
            buffer = bytearray(max(length, self.chunk_size))
            self._local.buffer = buffer
        view = memoryview(buffer)[:length]
        got = self._file.read_into(offset, view)
        if got != length:
            raise TransferFailed(
                f"{os.path.basename(self.path)}: short read at chunk {index} "
                f"({got}/{length} bytes)"
            )
        return view

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Chunk scheduling
# ---------------------------------------------------------------------------


class ChunkQueue:
    """A shared, ordered work queue of chunk indices.

    Thread safe.  Missing chunks are pushed in ascending order once; streams
    pop from the front, so the file is written front to back.  Chunks that fail
    are pushed to the back so they are retried only after everything else has
    been attempted -- a systematically bad chunk then costs one retry rather
    than stalling the whole transfer.

    Accounting is explicit because "is the transfer finished?" must be
    answerable while chunks are in flight: a chunk that has been handed to a
    stream is neither pending nor done, and treating it as done would end the
    transfer early while treating it as pending would end it never.
    """

    __slots__ = ("_items", "_cond", "_in_flight", "_closed", "_retries", "_attempts", "max_attempts")

    def __init__(self, indices: list[int], max_attempts: int = 6) -> None:
        self._items = deque(indices)
        self._cond = threading.Condition(threading.Lock())
        self._in_flight = 0
        self._closed = False
        self._retries = 0
        self._attempts: dict[int, int] = {}
        self.max_attempts = max_attempts

    # -- introspection -----------------------------------------------------

    @property
    def pending(self) -> int:
        """Chunks not yet handed to a stream."""
        with self._cond:
            return len(self._items)

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def outstanding(self) -> int:
        with self._cond:
            return len(self._items) + self._in_flight

    @property
    def exhausted(self) -> bool:
        """No work left to hand out and nothing in flight."""
        with self._cond:
            return not self._items and self._in_flight == 0

    @property
    def retries(self) -> int:
        return self._retries

    def __len__(self) -> int:
        return self.outstanding

    # -- work distribution -------------------------------------------------

    def get(self, timeout: float | None = None) -> int | None:
        """Take the next chunk index, or ``None`` when there is nothing to do.

        ``None`` means "nothing available right now"; callers decide whether
        that is the end of the transfer by checking :attr:`exhausted`.
        """
        with self._cond:
            if not self._items:
                if self._closed or self._in_flight == 0:
                    return None
                self._cond.wait(timeout)
                if not self._items:
                    return None
            index = self._items.popleft()
            self._in_flight += 1
            self._attempts[index] = self._attempts.get(index, 0) + 1
            return index

    def done(self, index: int) -> None:
        """Mark a chunk as successfully stored."""
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._attempts.pop(index, None)
            self._cond.notify_all()

    def requeue(self, index: int, *, front: bool = False) -> bool:
        """Return a failed chunk to the queue.

        Returns ``False`` when the chunk has exhausted its attempts, which is
        what stops a permanently unreadable chunk from spinning forever.
        """
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._attempts.get(index, 0) >= self.max_attempts:
                self._attempts.pop(index, None)
                self._cond.notify_all()
                return False
            if front:
                self._items.appendleft(index)
            else:
                self._items.append(index)
                self._retries += 1
            self._cond.notify_all()
            return True

    def give_up(self, index: int) -> None:
        """Drop a chunk from the accounting entirely (unrecoverable)."""
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._attempts.pop(index, None)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# Receive session
# ---------------------------------------------------------------------------


class ReceiveSession:
    """Receives one transfer: many files, many parallel streams."""

    def __init__(
        self,
        *,
        control: Connection,
        offer: dict[str, Any],
        identity: crypto.Identity,
        save_dir: str,
        events: "EventSink",
        accept_pin: str = "",
        resume: bool = True,
    ) -> None:
        self.control = control
        self.offer = offer
        self.identity = identity
        self.save_dir = save_dir
        self.events = events
        self.accept_pin = accept_pin
        self.use_resume = resume

        self.transfer_id = str(offer.get("transferId", ""))
        self.peer: DeviceInfo = control.peer.info if control.peer else DeviceInfo("", "unknown")
        self.entries = [FileEntry.from_dict(d) for d in offer.get("files", [])]
        self.total_bytes = sum(e.size for e in self.entries)

        self.stats = TransferStats(
            total_bytes=self.total_bytes, total_files=len(self.entries)
        )
        self.items: dict[int, TransferItem] = {
            e.index: TransferItem(entry=e) for e in self.entries
        }

        self._streams: "queue.Queue[Connection]" = queue.Queue()
        #: Data connections that are still usable.  They survive from one file
        #: to the next; only the session tears them down, because reconnecting
        #: between files would cost a handshake per file and lose the pipeline.
        self._live: list[Connection] = []
        self._live_lock = threading.Lock()
        self._stream_threads: list[threading.Thread] = []
        self._control_thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: Set by :meth:`cancel` (or when the sender cancels): the transfer is
        #: reported as ``cancelled`` so the UI can say 「已取消」 instead of
        #: colouring a deliberate stop as a failure.
        self._cancelled = False
        self._lock = threading.RLock()
        self._parts: dict[int, PartFile] = {}
        self._hashers: dict[int, IncrementalFileHasher] = {}
        self._done_bytes = 0
        self._t0 = time.monotonic()
        self._expected_streams = 0
        self._last_progress_sent = 0.0
        self.error = ""
        #: Whole-file digests the sender supplied on demand, by file index.
        self._digests: dict[int, str] = {}
        self._digest_waiters: dict[int, threading.Event] = {}
        #: Cached :meth:`decide` result: see the note there about opening the
        #: part files exactly once.
        self._decision: OfferDecision | None = None

    # -- properties --------------------------------------------------------

    @property
    def done_bytes(self) -> int:
        return self._done_bytes

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    # -- setup -------------------------------------------------------------

    def decide(self) -> OfferDecision:
        """Compute the accept/reject decision, resolving resume state.

        Called before OFFER_ACK is sent.  It touches the filesystem (it has to:
        the decision depends on which chunks are already on disk), so it runs
        before the sender starts pushing anything.

        The answer is cached, and the cache is what makes the manual-accept
        path work at all.  ``decide`` opens a :class:`PartFile` per accepted
        file, and it runs twice: once when the offer arrives (the UI needs the
        resume state to show "还有 3.2 GB 要传") and once when the user answers.
        Rebuilding the decision opened a *second* set of part files and threw
        the first away without closing it, which on Windows meant the part file
        was still open when the transfer ended -- so ``os.replace`` failed with
        "共享冲突" (ERROR_SHARING_VIOLATION) and **every manually accepted
        transfer lost its file after transferring all of it**.  POSIX lets a
        rename succeed with the file open, so this was invisible on Linux; the
        only trace there was one leaked descriptor per accepted file.
        """
        if self._decision is not None:
            return self._decision

        accepted: list[int] = []
        rejected: list[tuple[int, str]] = []

        requested = {int(i) for i in self.offer.get("accept", [])} if "accept" in self.offer else None
        pin = str(self.offer.get("pin", "") or "")
        pin_ok = (not self.accept_pin) or (pin == self.accept_pin)

        for entry in self.entries:
            if requested is not None and entry.index not in requested:
                rejected.append((entry.index, "declined"))
                continue
            try:
                target = safe_join(self.save_dir, entry.rel_dir, entry.name)
            except ValueError as exc:
                rejected.append((entry.index, str(exc)))
                continue

            if os.path.exists(target) and not self.use_resume:
                target = unique_path(target)

            try:
                part = PartFile(
                    target,
                    entry.size,
                    entry.digest,
                    entry.chunk_size,
                    mtime_ns=entry.mtime_ns,
                    resume=self.use_resume,
                )
            except (OSError, ChunkStoreError) as exc:
                rejected.append((entry.index, f"cannot open destination: {exc}"))
                continue

            self._parts[entry.index] = part
            self._hashers[entry.index] = IncrementalFileHasher()
            self.items[entry.index].path = target
            if part.received_bytes:
                self.items[entry.index].done_bytes = part.received_bytes
            accepted.append(entry.index)

        self._done_bytes = sum(
            self._parts[i].received_bytes for i in self._parts
        )

        # Fewer streams for a spinning disk: parallel random writes on a HDD
        # are far slower than a single sequential pass.
        streams = int(self.offer.get("streams", DEFAULT_STREAMS) or DEFAULT_STREAMS)
        streams = max(1, min(MAX_STREAMS, streams))
        from .sockutil import is_rotational_disk

        if is_rotational_disk(self.save_dir):
            streams = min(streams, 2)

        reason = ""
        if not pin_ok:
            reason = "PIN required or incorrect"
        elif not accepted:
            reason = "no files accepted"

        self._decision = OfferDecision(
            accepted=accepted,
            rejected=rejected,
            streams=streams,
            save_dir=self.save_dir,
            pin_ok=pin_ok,
            reason=reason,
        )
        return self._decision

    def reject(self, reason: str, status: str = "rejected") -> None:
        """Tell the sender the offer was declined and clean up."""
        try:
            self.control.send_json(
                MSG_OFFER_REJECT, {"transferId": self.transfer_id, "reason": reason}
            )
        except Exception:
            pass
        for part in self._parts.values():
            part.discard()
        self._parts.clear()
        self.events.emit("transfer_rejected", transfer_id=self.transfer_id, reason=reason)

    def attach(self, conn: Connection) -> None:
        """Hand a freshly attached data connection to the stream pool."""
        if self._stop.is_set():
            conn.abort()
            return
        self._streams.put(conn)

    # -- main loop ---------------------------------------------------------

    def run(self, decision: OfferDecision, expected_streams: int) -> bool:
        """Execute the transfer.  Returns True when every accepted file landed."""
        try:
            self._run_inner(decision, expected_streams)
            return not self.error
        finally:
            self._stop.set()
            for thread in self._stream_threads:
                thread.join(timeout=2.0)
            for part in self._parts.values():
                try:
                    part.close()
                except Exception:
                    pass

    def _run_inner(self, decision: OfferDecision, expected_streams: int) -> None:
        # 1. Order the files so the largest goes first: a long pole at the end
        #    of a transfer wastes the parallelism built up before it.
        order = sorted(
            decision.accepted,
            key=lambda i: (-self._parts[i].size, self.items[i].entry.name),
        )

        # 2. Send OFFER_ACK so the sender can attach its streams.
        self.control.send_json(
            MSG_OFFER_ACK,
            {
                "transferId": self.transfer_id,
                "accepted": decision.accepted,
                "rejected": [{"i": i, "why": w} for i, w in decision.rejected],
                "streams": expected_streams,
                "resumeBytes": self._done_bytes,
                "totalBytes": self.total_bytes,
                "saveDir": self.save_dir,
            },
        )
        self.events.emit(
            "transfer_accepted",
            transfer_id=self.transfer_id,
            peer=self.peer,
            files=len(decision.accepted),
            resume_bytes=self._done_bytes,
        )

        # 2b. Start reading the control connection.  The receiver drives chunk
        #     requests, but the sender still needs to push digests and
        #     cancellations back, and those arrive here.
        self._control_thread = threading.Thread(
            target=self._control_loop, name=f"recv-ctl-{self.transfer_id[:8]}", daemon=True
        )
        self._control_thread.start()

        # 3. Wait for the data streams to arrive.
        streams = self._collect_streams(expected_streams)
        if not streams:
            self.error = "no data streams were established"
            self._finish("failed")
            return
        self._live = list(streams)
        self._expected_streams = max(1, expected_streams) if expected_streams else max(1, len(streams))
        self._debug(f"开始传输：收到 {len(streams)} 条流（期望 {expected_streams}）")
        self.events.emit("transfer_started", transfer_id=self.transfer_id, streams=len(streams))

        # 4. Send each file, in order.
        ok = True
        for file_index in order:
            if self._stop.is_set():
                ok = False
                break
            if not self._send_file(file_index):
                ok = False
                break

        self._debug(f"文件循环结束，live 流还有 {len(self._live)} 条")

        # 5. Report first, then tear the data connections down.  If the
        #    control connection died, a data stream is the only way the
        #    sender can learn the outcome -- closing them first would leave it
        #    waiting out its grace period and reporting a failure for a
        #    transfer that actually succeeded.
        self._finish("done" if ok else ("cancelled" if self._cancelled else "failed"))
        for conn in list(self._live):
            conn.abort()

    def _control_loop(self) -> None:
        """Read control messages from the sender for the whole session."""
        try:
            self.control.sock.settimeout(None)
            while not self._stop.is_set():
                try:
                    frame = self.control.recv()
                except (ConnectionClosed, ProtocolError, OSError):
                    return
                try:
                    self._handle_control(frame, self.control)
                except TransferCancelled:
                    return
        finally:
            # Unblock anybody waiting on a digest that will never arrive.
            for waiter in list(self._digest_waiters.values()):
                waiter.set()

    def _collect_streams(
        self, expected: int, timeout: float = 20.0, *, at_least: int | None = None
    ) -> list[Connection]:
        """Gather data connections, waiting for up to ``expected`` of them.

        ``at_least`` lets a recovery path stop waiting as soon as the transfer
        can make progress again: one healthy stream is enough to finish a
        transfer, so blocking for the full complement would only add latency
        to a link that is already struggling.
        """
        streams: list[Connection] = []
        deadline = time.monotonic() + timeout
        floor = expected if at_least is None else max(1, at_least)
        while len(streams) < expected and time.monotonic() < deadline:
            if self._stop.is_set():
                break
            try:
                streams.append(self._streams.get(timeout=0.2))
            except queue.Empty:
                if len(streams) >= floor and time.monotonic() > deadline - timeout + 1.5:
                    # Enough to work with, and we gave the peer a moment to
                    # send more; get moving rather than idling.
                    break
                continue
            if len(streams) >= floor and len(streams) >= expected:
                break
        return streams

    # -- per-file transfer -------------------------------------------------

    def _send_file(self, file_index: int) -> bool:
        part = self._parts[file_index]
        item = self.items[file_index]
        entry = item.entry

        if part.complete and part.received_bytes == part.size:
            # Everything is already on disk (typical for a full resume).
            return self._finalize_file(file_index, part, item, [])

        missing = part.missing_chunks()
        item.status = "active"
        self.events.emit(
            "file_started",
            transfer_id=self.transfer_id,
            index=file_index,
            name=entry.name,
            size=entry.size,
            chunks=len(missing),
            resumed=part.received_bytes,
        )

        work = ChunkQueue(missing)
        stop = self._stop
        errors: list[str] = []
        error_lock = threading.Lock()

        def stream_worker(conn: Connection, worker_id: int) -> None:
            window = max(
                2,
                min(8, DEFAULT_WINDOW_BYTES // max(1, part.chunk_size)),
            )
            outstanding: deque[int] = deque()
            failed = False
            # A chunk must come back within a time that scales with its size,
            # assuming a modest throughput floor.  The socket timeout is the
            # only thing that ends a read on a connection the network killed
            # silently, so it must be tight enough to be a latency bound
            # rather than a liveness backstop.
            chunk_deadline = max(
                CHUNK_REQUEST_TIMEOUT, part.chunk_size / CHUNK_DEADLINE_FLOOR_BPS
            )
            try:
                conn.sock.settimeout(chunk_deadline)
                while not stop.is_set():
                    # Fill the pipeline: keep up to ``window`` requests in
                    # flight so the stream never idles waiting for a reply.
                    while len(outstanding) < window:
                        index = work.get(timeout=0.05)
                        if index is None:
                            break
                        outstanding.append(index)
                        try:
                            conn.send_json(
                                MSG_CHUNK_REQ,
                                {
                                    "t": self.transfer_id,
                                    "f": file_index,
                                    "i": index,
                                },
                            )
                        except ConnectionClosed:
                            work.requeue(index, front=True)
                            outstanding.pop()
                            raise

                    if not outstanding:
                        if work.exhausted:
                            return
                        continue

                    expected_index = outstanding[0]
                    try:
                        frame = conn.recv_data()
                    except socket.timeout:
                        raise TimeoutError(
                            f"no reply to chunk {expected_index} within {chunk_deadline:.0f}s"
                        )
                    except (ConnectionClosed, ProtocolError, OSError) as exc:
                        raise ConnectionError(str(exc)) from exc

                    if isinstance(frame, Frame):
                        self._handle_control(frame, conn)
                        continue

                    assert isinstance(frame, ChunkFrame)
                    outstanding.popleft()
                    if frame.index != expected_index:
                        # The peer answered a different chunk than we asked
                        # for: the stream desynced.  Put both back and drop
                        # this stream rather than guess which is which.
                        work.requeue(expected_index, front=True)
                        work.requeue(frame.index, front=True)
                        raise ProtocolError(
                            f"chunk {frame.index} arrived where {expected_index} was expected"
                        )

                    try:
                        self._store_chunk(part, item, file_index, frame)
                    except ProtocolError:
                        # Bad CRC: retry the chunk on another stream.
                        work.requeue(frame.index)
                        continue
                    work.done(frame.index)
                    if work.exhausted and not outstanding:
                        return
            except (ConnectionError, ProtocolError, TimeoutError, ConnectionClosed, OSError) as exc:
                failed = True
                with error_lock:
                    errors.append(str(exc))
                # Everything this stream had in flight goes back on the queue
                # so the surviving streams can finish the file.
                for index in outstanding:
                    work.requeue(index, front=True)
            finally:
                # A stream that ended normally stays open for the next file;
                # only a broken one is dropped so its work can be taken over.
                if failed:
                    self._drop_stream(conn)

        # ---- run the file, replacing dead streams as needed ---------------
        #
        # The budget is a wall-clock deadline rather than a retry counter.
        # Counting attempts conflates two very different things: "these chunks
        # keep failing" (a real problem, and a few tries is plenty) and "the
        # network keeps dropping my connections" (not the file's fault, and a
        # hostile link can burn six attempts in two seconds).  A deadline
        # tolerates the second case while still bounding the first.
        attempt = 0
        recovery_deadline = time.monotonic() + self.FILE_RECOVERY_BUDGET
        while (
            attempt < self.MAX_FILE_ATTEMPTS
            and time.monotonic() < recovery_deadline
            and not self._stop.is_set()
            and not part.complete
        ):
            attempt += 1
            streams = self._live_streams()
            if not streams:
                # Every stream is gone.  Ask the sender for replacements; that
                # is what keeps a transfer alive across a Wi-Fi dropout.  The
                # request itself may have nowhere to travel when every
                # connection failed at once, in which case the sender's own
                # supervisor is reconnecting in parallel, so the ask is best
                # effort and the wait below covers the other case.
                #
                # `_request_streams` already registers whatever arrives, so
                # re-reading the pool is enough -- collecting a second time
                # would wait for connections that have already been taken.
                wanted = self._expected_streams or DEFAULT_STREAMS
                self._debug(f"file {file_index}: stream pool empty, asking for {wanted}")
                if not self._request_streams(wanted):
                    # The request had nowhere to go, which happens when the
                    # control connection and every data connection died
                    # together.  The sender is reconnecting on its own, so wait
                    # for the ATTACH to arrive instead of declaring failure the
                    # instant the pool is empty.
                    arrived = self._collect_streams(1, timeout=8.0, at_least=1)
                    with self._live_lock:
                        for conn in arrived:
                            if conn not in self._live:
                                self._live.append(conn)
                streams = self._live_streams()
                self._debug(f"file {file_index}: after recovery ask, live streams = {len(streams)}")
                if not streams:
                    break
                continue

            self._debug(f"round {attempt}: starting {len(streams)} stream(s)")
            errors.clear()
            self._stream_threads = []
            for worker_id, conn in enumerate(streams):
                thread = threading.Thread(
                    target=stream_worker,
                    args=(conn, worker_id),
                    name=f"recv-{self.transfer_id[:8]}-{worker_id}",
                    daemon=True,
                )
                thread.start()
                self._stream_threads.append(thread)
            for thread in self._stream_threads:
                thread.join()
            work.close()
            self._debug(
                f"round {attempt}: joined, have {part.received_bytes}/{part.size} bytes"
            )

            if part.complete:
                break
            # Streams died with work left: ask for reinforcements and retry the
            # chunks that are still missing before giving up on the file.
            if self._stop.is_set():
                break
            missing_now = part.missing_chunks()
            if not missing_now:
                break
            self.events.emit(
                "transfer_recovering",
                transfer_id=self.transfer_id,
                index=file_index,
                missing=len(missing_now),
            )
            work = ChunkQueue(missing_now)

        self._debug(f"file loop finished after {attempt} round(s)")
        if self._stop.is_set():
            return False

        if not part.complete:
            remaining = len(part.missing_chunks())
            self.error = (
                f"{item.entry.name}: {remaining} chunk(s) could not be received"
                + (f" ({errors[0]})" if errors else "")
            )
            item.status = "failed"
            item.error = self.error
            self.stats.failed_files += 1
            self.events.emit("file_failed", transfer_id=self.transfer_id, index=file_index, error=self.error)
            return False

        return self._finalize_file(file_index, part, item, errors)

    #: How long one file may keep recovering from lost connections before the
    #: transfer is declared failed.  Generous: a Wi-Fi roam or a VPN rekey can
    #: take tens of seconds, and giving up early throws away a partial file
    #: that resume would otherwise have finished for free.
    FILE_RECOVERY_BUDGET = 180.0

    #: Hard cap on recovery rounds for one file, so a peer that accepts and
    #: instantly drops connections cannot spin forever.
    MAX_FILE_ATTEMPTS = 60

    # -- stream pool -------------------------------------------------------

    def _debug(self, message: str) -> None:
        """Emit a diagnostic only when EVERSEND_DEBUG is set."""
        if os.environ.get("EVERSEND_DEBUG"):
            print(
                f"[recv {self.transfer_id[:8]} +{time.monotonic() - self._t0:6.2f}s] {message}",
                file=sys.stderr,
                flush=True,
            )

    def _send_control(self, mtype: int, payload: dict[str, Any]) -> bool:
        """Send a control message, falling back to a data stream.

        The control connection is just another TCP connection and can be the
        one a flaky network drops.  Nothing about the protocol requires
        control messages to travel on it -- the peer dispatches by message
        type, not by connection -- so once it is gone we keep talking over any
        surviving data stream.  Without this, losing one connection would
        abandon a transfer that is 99% complete with every chunk stream still
        perfectly healthy.
        """
        control = self.control
        if control is not None and not control.closed:
            try:
                control.send_json(mtype, payload)
                return True
            except Exception:
                pass
        for conn in self._live_streams():
            try:
                conn.send_json(mtype, payload)
                return True
            except Exception:
                continue
        return False

    def _live_streams(self) -> list[Connection]:
        with self._live_lock:
            self._live = [c for c in self._live if not c.closed]
            return list(self._live)

    def _drop_stream(self, conn: Connection) -> None:
        try:
            conn.abort()
        except Exception:
            pass
        with self._live_lock:
            if conn in self._live:
                self._live.remove(conn)

    def _request_streams(self, count: int, timeout: float = 3.0) -> bool:
        """Ask the sender for ``count`` replacement data connections."""
        if self._stop.is_set():
            return False
        before = len(self._live_streams())
        wanted_for_request = max(1, min(MAX_STREAMS, count))
        if not self._send_control(
            MSG_NEED_STREAMS, {"t": self.transfer_id, "n": wanted_for_request}
        ):
            return False
        wanted = max(1, min(MAX_STREAMS, count))
        # Wait for the whole pool (or the timeout): recovery is about getting
        # throughput back, and a single stream on a lossy link will just be
        # killed again before it has moved anything useful.
        new = self._collect_streams(wanted, timeout=timeout)
        with self._live_lock:
            for conn in new:
                if conn not in self._live:
                    self._live.append(conn)
        return len(self._live) > before

    def _store_chunk(
        self, part: PartFile, item: TransferItem, file_index: int, frame: ChunkFrame
    ) -> None:
        """Verify and persist one received chunk."""
        actual_crc = crc32(frame.data)
        if actual_crc != frame.crc:
            # Corruption in flight (only reachable without AEAD, since the
            # AEAD tag already covers this).  Re-queue rather than write.
            raise ProtocolError(
                f"{item.entry.name}: chunk {frame.index} failed CRC "
                f"({actual_crc:#010x} != {frame.crc:#010x})"
            )
        if frame.offset != frame.index * part.chunk_size:
            raise ProtocolError(
                f"{item.entry.name}: chunk {frame.index} claims offset {frame.offset}"
            )

        part.write_chunk(frame.index, frame.data, frame.crc)

        with self._lock:
            self._done_bytes += len(frame.data)
            item.done_bytes += len(frame.data)
            self.stats.record(self._done_bytes)

        # Tell the sender where we are.  This is the only place that knows the
        # true figure, and without it every progress bar on the sending side
        # sits at zero for the whole transfer (the receiver used to compute
        # progress and never report it).
        self.progress_report()

        hasher = self._hashers.get(file_index)
        if hasher is not None and not hasher.overflowing:
            hasher.update(frame.offset, frame.data)

    def _finalize_file(
        self,
        file_index: int,
        part: PartFile,
        item: TransferItem,
        errors: list[str],
    ) -> bool:
        """Verify the whole file, repair if needed, and move it into place.

        The expected digest is fetched from the sender *now* rather than being
        carried in the offer: the sender hashes the file while it is already
        reading it for the transfer, so no separate pre-pass is needed and a
        multi-gigabyte send starts instantly.
        """
        entry = item.entry
        self.events.emit(
            "file_verifying", transfer_id=self.transfer_id, index=file_index, name=entry.name
        )

        self._debug(f"file {file_index}: requesting digest")
        expected = self._request_digest(file_index)
        self._debug(f"file {file_index}: digest {'received' if expected else 'MISSING'}")
        hasher = self._hashers.get(file_index)
        digest_ok = False
        actual = ""

        # Fast path: the incremental hasher usually already covers the file
        # because chunks were handed out in order, which makes verification
        # effectively free.
        if expected and hasher is not None and hasher.can_finish_incrementally(part.size):
            actual = hasher.finish()
            digest_ok = actual == expected.lower()
        else:
            actual = _digest_fd(part)
            digest_ok = (not expected) or actual == expected.lower()

        if not digest_ok and expected:
            # Repair round: find the chunks that rotted and fetch only those.
            if self._repair(file_index, part, item, errors):
                digest_ok = True
                actual = expected.lower()
                hasher = None

        if not digest_ok and expected:
            part.flush()
            self.error = (
                f"{entry.name}: checksum mismatch "
                f"(expected {expected[:16]}..., got {actual[:16]}...)"
            )
            item.status = "failed"
            item.error = self.error
            self.stats.failed_files += 1
            self.events.emit("file_failed", transfer_id=self.transfer_id, index=file_index, error=self.error)
            return False

        if not expected:
            # The sender could not produce a digest (it may have been unable
            # to re-read the file).  The per-chunk CRCs still guarantee every
            # byte arrived intact; say so rather than pretending to have
            # verified end to end.
            self.events.emit(
                "file_unverified", transfer_id=self.transfer_id, index=file_index, name=entry.name
            )

        try:
            part.finalize(verify=False)  # already verified above
        except ChunkStoreError as exc:
            self.error = str(exc)
            item.status = "failed"
            item.error = self.error
            self.stats.failed_files += 1
            return False

        part.set_times(entry.mtime_ns)
        item.status = "done"
        item.done_bytes = entry.size
        self.stats.done_files += 1
        self.events.emit(
            "file_done",
            transfer_id=self.transfer_id,
            index=file_index,
            name=entry.name,
            path=part.target_path,
            size=entry.size,
        )
        self._send_control(
            MSG_FILE_DONE,
            {
                "t": self.transfer_id,
                "i": file_index,
                "ok": True,
                "path": os.path.basename(part.target_path),
            },
        )
        return True

    #: How long to wait for a whole-file digest.
    #:
    #: The sender answers from a hasher it fed while sending, so this is
    #: normally instantaneous; the timeout only matters when its hasher saw
    #: chunks out of order and it has to re-read the file.
    DIGEST_TIMEOUT = 30.0

    def _request_digest(self, file_index: int) -> str:
        """Ask the sender for a file's whole-file digest and wait for it.

        By the time this runs the chunk workers have exited, so nothing else is
        reading the data streams.  The reply is therefore read **on the
        connection the request was sent on, by the same thread** -- if it were
        left to the control-channel reader, a reply arriving on a data stream
        would sit unread until the timeout expired, which turned a completed
        transfer into a 60-second stall before it finalised.
        """
        known = self._digests.get(file_index)
        if known:
            return known

        deadline = time.monotonic() + self.DIGEST_TIMEOUT
        request = {"t": self.transfer_id, "i": file_index}

        # 1. A data stream: we own it here, so read the reply ourselves.
        for conn in self._live_streams():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                conn.send_json(MSG_DIGEST_REQ, request)
            except Exception:
                continue
            try:
                conn.sock.settimeout(remaining)
            except OSError:
                continue
            while time.monotonic() < deadline:
                try:
                    frame = conn.recv()
                except (socket.timeout, ConnectionClosed, ProtocolError, OSError):
                    break
                if frame.type == MSG_FILE_DIGEST:
                    self._store_digest(frame)
                    return self._digests.get(file_index, "")
                try:
                    self._handle_control(frame, conn)
                except TransferCancelled:
                    return ""

        # 2. Nothing else is available: use the control channel, where the
        #    dedicated reader thread will deliver the reply to our waiter.
        known = self._digests.get(file_index)
        if known:
            return known
        waiter = threading.Event()
        self._digest_waiters[file_index] = waiter
        if not self._send_control(MSG_DIGEST_REQ, request):
            self._digest_waiters.pop(file_index, None)
            return ""
        remaining = max(0.1, deadline - time.monotonic())
        waiter.wait(remaining)
        self._digest_waiters.pop(file_index, None)
        return self._digests.get(file_index, "")

    def _store_digest(self, frame: Frame) -> None:
        """Record a FILE_DIGEST message and wake anyone waiting for it."""
        try:
            info = json.loads(frame.payload.decode("utf-8"))
            index = int(info.get("i", -1))
            digest = str(info.get("h", ""))
        except (ValueError, UnicodeDecodeError):
            return
        self._digests[index] = digest
        waiter = self._digest_waiters.pop(index, None)
        if waiter is not None:
            waiter.set()

    def _repair(
        self,
        file_index: int,
        part: PartFile,
        item: TransferItem,
        errors: list[str],
    ) -> bool:
        """Re-fetch only the chunks that fail their recorded CRC.

        This is what turns "the 100 GB transfer failed its checksum" into
        "4 MB were re-sent".
        """
        self.events.emit(
            "file_repairing", transfer_id=self.transfer_id, index=file_index, name=item.entry.name
        )
        bad = part.repair_scan()
        if not bad:
            return False

        # Everything is suspect if the CRCs all pass but the digest differs.
        for index in bad:
            part.mark_lost(index)

        work = ChunkQueue(bad)
        stop = self._stop
        errors.clear()

        collected = self._live_streams()
        if not collected:
            if not self._request_streams(1):
                return False
            collected = self._live_streams()
        if not collected:
            return False

        def worker(conn: Connection) -> None:
            try:
                conn.sock.settimeout(
                    max(CHUNK_REQUEST_TIMEOUT, part.chunk_size / CHUNK_DEADLINE_FLOOR_BPS)
                )
            except OSError:
                return
            outstanding: deque[int] = deque()
            try:
                while not stop.is_set():
                    while len(outstanding) < 2:
                        index = work.get(timeout=0.05)
                        if index is None:
                            break
                        outstanding.append(index)
                        conn.send_json(
                            MSG_CHUNK_REQ,
                            {"t": self.transfer_id, "f": file_index, "i": index},
                        )
                    if not outstanding:
                        if len(work) == 0:
                            return
                        continue
                    expected_index = outstanding[0]
                    frame = conn.recv_data()
                    if isinstance(frame, Frame):
                        self._handle_control(frame, conn)
                        continue
                    outstanding.popleft()
                    if frame.index != expected_index:
                        work.requeue(expected_index, front=True)
                        continue
                    self._store_chunk(part, item, file_index, frame)
            except Exception as exc:
                errors.append(str(exc))
                for index in outstanding:
                    work.requeue(index, front=True)
                self._drop_stream(conn)

        threads = [
            threading.Thread(target=worker, args=(c,), daemon=True) for c in collected
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        work.close()

        if part.complete:
            part.flush()
            actual = _digest_fd(part)
            if not item.entry.digest or actual == item.entry.digest.lower():
                return True
        return False

    # -- misc --------------------------------------------------------------

    def _handle_control(self, frame: Frame, conn: Connection) -> None:
        """React to a control message that arrived on a data stream."""
        if frame.type == MSG_FILE_DIGEST:
            self._store_digest(frame)
            return
        if frame.type == MSG_CANCEL:
            self._cancelled = True
            self._stop.set()
            raise TransferCancelled("发送方取消了这次传输")
        if frame.type == MSG_ERROR:
            try:
                message = json.loads(frame.payload.decode("utf-8")).get("message", "peer error")
            except Exception:
                message = "peer error"
            self.error = message
            self._stop.set()
            raise TransferCancelled(message)

    def _finish(self, status: str) -> None:
        for part in self._parts.values():
            try:
                part.close()
            except Exception:
                pass
        total = self._done_bytes
        self.events.emit(
            "transfer_finished",
            transfer_id=self.transfer_id,
            peer=self.peer,
            status=status,
            bytes=total,
            files=self.stats.done_files,
            error=self.error,
        )
        report = {
            "t": self.transfer_id,
            "status": status,
            "bytes": total,
            "files": self.stats.done_files,
            "error": self.error,
        }
        self._debug(f"finish({status}): broadcasting report")
        self._broadcast_report(report)
        self._debug("finish: report broadcast done")

    #: How long the outcome is repeated across every available channel.
    REPORT_WINDOW = 3.0

    #: Spacing between repeats.
    REPORT_INTERVAL = 0.4

    def _broadcast_report(self, report: dict[str, Any]) -> None:
        """Deliver the final outcome to the sender, reliably.

        A single send is not enough.  A connection that the network has already
        killed can still accept a write into its socket buffer -- the RST comes
        back afterwards -- so "the send did not raise" proves nothing, and a
        lost report leaves the sender waiting out its grace period and
        reporting a failure for a transfer that actually succeeded.

        So the report is repeated, on every channel, for a fixed short window.
        It is tiny and idempotent (the sender acts on the first copy and
        ignores the rest), and the window is long enough to cover a stream the
        peer's supervisor is reconnecting right now.  Retrying unconditionally
        is deliberate: there is no reliable local signal for "this write really
        arrived".
        """
        deadline = time.monotonic() + self.REPORT_WINDOW
        delivered = False
        while True:
            # Adopt any connection that attached while we were finishing.
            while True:
                try:
                    conn = self._streams.get_nowait()
                except queue.Empty:
                    break
                with self._live_lock:
                    if not conn.closed:
                        self._live.append(conn)

            channels = [self.control] if self.control is not None else []
            channels.extend(self._live_streams())
            for conn in channels:
                if conn is None or conn.closed:
                    continue
                try:
                    conn.send_json(MSG_TRANSFER_DONE, report)
                    delivered = True
                except Exception:
                    continue

            if time.monotonic() >= deadline:
                break
            # Staggered so repeats land on connections that are up by then.
            time.sleep(self.REPORT_INTERVAL)

        if not delivered:
            self.events.emit(
                "transfer_report_undelivered", transfer_id=self.transfer_id, status=status_of(report)
            )

    def cancel(self, reason: str = "cancelled by user") -> None:
        """Abort from the receiving side."""
        self._cancelled = True
        self.error = reason
        self._stop.set()
        self._send_control(MSG_CANCEL, {"t": self.transfer_id, "reason": reason})
        self.events.emit("transfer_cancelled", transfer_id=self.transfer_id, reason=reason)

    def progress_report(self) -> None:
        """Push a PROGRESS message if enough time has passed.

        Called from the chunk-store hot path, so the interval check happens
        before anything else -- at 16 MiB chunks and 10 Gbps this would
        otherwise try to send thousands of messages per second.
        """
        now = time.monotonic()
        if now - self._last_progress_sent < 0.2:
            return
        self._last_progress_sent = now
        self._send_control(
            MSG_PROGRESS,
            {
                "t": self.transfer_id,
                "bytes": self._done_bytes,
                "total": self.total_bytes,
                "files": self.stats.done_files,
            },
        )


def status_of(report: dict[str, Any]) -> str:
    """The status field of a transfer report."""
    return str(report.get("status", ""))


def _digest_fd(part: PartFile) -> str:
    """Whole-file digest of a :class:`PartFile` without disturbing its offset."""
    from .hashing import file_digest_positioned

    return file_digest_positioned(part._file, part.size)  # noqa: SLF001 - same package


# ---------------------------------------------------------------------------
# Send session
# ---------------------------------------------------------------------------


class SendSession:
    """Sends one transfer to a peer, serving chunk requests from many streams."""

    def __init__(
        self,
        *,
        control: Connection,
        identity: crypto.Identity,
        entries: list[FileEntry],
        sources: dict[int, str],
        events: "EventSink",
        pin: str = "",
        streams: int = DEFAULT_STREAMS,
        compress: bool = False,
        cancel_event: threading.Event | None = None,
        digest_cache: Any = None,
    ) -> None:
        self.control = control
        self.identity = identity
        self.entries = entries
        self.sources = sources
        self.events = events
        self.pin = pin
        self.requested_streams = max(1, min(MAX_STREAMS, streams))
        self.cancel_event = cancel_event or threading.Event()
        self._digest_cache = digest_cache
        self._digests: dict[int, str] = {}

        self.transfer_id = new_transfer_id(entries, identity.device_id)
        self.total_bytes = sum(e.size for e in entries)
        self.stats = TransferStats(total_bytes=self.total_bytes, total_files=len(entries))
        self.items: dict[int, TransferItem] = {
            e.index: TransferItem(entry=e, path=sources.get(e.index, "")) for e in entries
        }
        self.peer = control.peer.info if control.peer else DeviceInfo("", "unknown")
        self.accepted: list[int] = []
        self.rejected: list[tuple[int, str]] = []
        self.error = ""
        self._readers: dict[int, FileReader] = {}
        #: Set once the readers are closed for good; see :meth:`_close_readers`.
        self._readers_closed = False
        #: Guards "check the flag, then maybe open a reader" against the
        #: cleanup that sets it, so no reader can slip in after the sweep.
        self._reader_lock = threading.Lock()
        #: Whole-file hashers fed from the chunks as they are read, so the
        #: digest costs no extra pass over the file in the normal case.
        self._hashers: dict[int, IncrementalFileHasher] = {}
        self._digest_cache: Any = None
        self._data_conns: list[Connection] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        #: Set by :meth:`cancel`.  Without it a cancelled transfer was reported
        #: as ``failed`` with the reason as its error, so the desktop showed
        #: 「用户取消」 in red under a 失败 headline -- and the row could not tell
        #: "the user stopped this" from "the network broke".
        self._cancelled = False
        self._done_bytes = 0
        self._lock = threading.RLock()
        #: Opens additional data connections on demand (set by the engine).
        self.stream_opener: Any = None
        self._next_stream_index = 0
        #: Set when the receiver reports the transfer finished, on whatever
        #: connection that report happens to arrive.
        self._finished = threading.Event()
        self._finish_status = ""
        #: How many data streams this transfer is supposed to have.  The
        #: supervisor restores the pool to this size when connections die.
        self._target_streams = 0
        self._reconnects = 0

    # -- offer -------------------------------------------------------------

    def offer(self, *, accept_all: bool = True) -> dict[str, Any]:
        """Send the OFFER and wait for the receiver's answer."""
        self.control.send_json(
            MSG_OFFER,
            {
                "transferId": self.transfer_id,
                "files": [e.to_dict() for e in self.entries],
                "totalBytes": self.total_bytes,
                "streams": self.requested_streams,
                "pin": self.pin,
                "sender": self.identity.device_id,
                "compress": False,
            },
        )

        self.control.sock.settimeout(OFFER_TIMEOUT)
        try:
            while True:
                frame = self.control.recv()
                if frame.type == MSG_OFFER_ACK:
                    answer = json.loads(frame.payload.decode("utf-8"))
                    self.accepted = [int(i) for i in answer.get("accepted", [])]
                    self.rejected = [
                        (int(r.get("i", -1)), str(r.get("why", "")))
                        for r in answer.get("rejected", [])
                    ]
                    return answer
                if frame.type == MSG_OFFER_REJECT:
                    answer = json.loads(frame.payload.decode("utf-8"))
                    raise TransferFailed(str(answer.get("reason", "the receiver declined")))
                if frame.type == MSG_ERROR:
                    raise TransferFailed(_frame_error(frame))
                if frame.type == MSG_CANCEL:
                    self._cancelled = True
                    raise TransferCancelled("接收方取消了这次传输")
        finally:
            self.control.sock.settimeout(None)

    # -- data streams ------------------------------------------------------

    def attach_stream(self, conn: Connection, index: int) -> None:
        """Send ATTACH on a data connection and start serving it."""
        conn.send_json(
            MSG_ATTACH,
            {"t": self.transfer_id, "i": index, "sender": self.identity.device_id},
        )
        self._data_conns.append(conn)

    #: Upper bound on automatic stream reconnects for one transfer.  Without a
    #: cap, a peer that accepts and immediately drops connections would make
    #: the sender dial forever.
    MAX_RECONNECTS = 40

    #: How often the supervisor checks the health of the stream pool.
    SUPERVISE_INTERVAL = 1.0

    def serve(self) -> bool:
        """Serve chunk requests until the receiver reports the transfer done."""
        self._target_streams = max(1, len(self._data_conns))
        for index, conn in enumerate(self._data_conns):
            self._next_stream_index = index + 1
            thread = threading.Thread(
                target=self._serve_stream,
                args=(conn, index),
                name=f"send-{self.transfer_id[:8]}-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        supervisor = threading.Thread(
            target=self._supervise_streams,
            name=f"send-sup-{self.transfer_id[:8]}",
            daemon=True,
        )
        supervisor.start()

        ok = True
        self.control.sock.settimeout(None)
        control_lost: str = ""
        try:
            while not self._stop.is_set() and not self.cancel_event.is_set():
                try:
                    frame = self.control.recv()
                except (ConnectionClosed, ProtocolError, OSError) as exc:
                    control_lost = str(exc)
                    break
                if not self._handle_control(frame, self.control):
                    break
        finally:
            # The control connection is only a convenience: it can be the one
            # connection a flaky network drops while every chunk stream is
            # still healthy.  Keep serving the data streams and wait for the
            # receiver to report the outcome over one of them, rather than
            # abandoning a transfer that is nearly finished.
            #
            # The wait is bounded by both a deadline and the survival of the
            # data streams: once none is left, no report can still arrive, and
            # an unreported transfer counts as a failure.  Success is only ever
            # claimed on an explicit "done", because a false success loses
            # data while a false failure merely costs a retry (which resume
            # turns into a no-op).
            if control_lost and not self._stop.is_set() and not self.cancel_event.is_set():
                deadline = time.monotonic() + self.CONTROL_LOSS_GRACE
                while (
                    not self._finished.is_set()
                    and not self._stop.is_set()
                    and not self.cancel_event.is_set()
                    and time.monotonic() < deadline
                    and self._reconnects < self.MAX_RECONNECTS
                ):
                    # Do NOT bail out merely because no stream is alive this
                    # instant: a failure that kills the control connection
                    # usually kills the data streams with it, and the
                    # supervisor is reconnecting them right now.  Give up only
                    # when its budget is exhausted or the deadline passes.
                    self._finished.wait(0.5)
                if not self._finished.is_set():
                    self.error = (
                        f"control connection lost and the peer stopped responding "
                        f"({control_lost})"
                    )
            self._stop.set()
            for conn in self._data_conns:
                conn.abort()
            for thread in self._threads:
                thread.join(timeout=2.0)
            self._close_readers()

        if self._finish_status:
            ok = self._finish_status == "done"
        else:
            # No report ever arrived, so the outcome is unknown -- and unknown
            # must not be reported as success.
            ok = False
            if not self.error:
                self.error = "the receiver never reported the transfer result"
        if self.error and self._finish_status == "done":
            ok = False
        if ok:
            status = "done"
        elif self._cancelled:
            status = "cancelled"
            # The reason is already on screen as 「已取消」; repeating it as an
            # error would put the same sentence in a red failure line.
            self.error = ""
        else:
            status = "failed"
        self.events.emit(
            "send_finished",
            transfer_id=self.transfer_id,
            peer=self.peer,
            status=status,
            bytes=self._done_bytes,
            error=self.error,
        )
        return ok

    #: How long to keep serving data streams after the control connection
    #: dies, waiting for the receiver to report the transfer's outcome.
    CONTROL_LOSS_GRACE = 120.0

    def _supervise_streams(self) -> None:
        """Keep the data-stream pool at its intended size.

        The receiver asks for replacements when it notices a stream has died,
        but that request needs a working connection to travel over -- and the
        failure that killed the streams may well have killed the control
        connection too.  So the sender also watches its own pool and reconnects
        by itself, which is what makes a transfer survive losing *every*
        connection at once (a Wi-Fi roam, a VPN rekey, a sleeping radio).
        """
        backoff = self.SUPERVISE_INTERVAL
        while not self._stop.is_set() and not self.cancel_event.is_set():
            if self._finished.wait(backoff):
                return
            if self._stop.is_set() or self.cancel_event.is_set():
                return

            live = sum(1 for thread in list(self._threads) if thread.is_alive())
            if live >= self._target_streams:
                backoff = self.SUPERVISE_INTERVAL
                continue
            if self._reconnects >= self.MAX_RECONNECTS:
                return

            missing = self._target_streams - live
            try:
                self._open_replacement_streams(missing)
            except Exception as exc:
                self.error = str(exc)
            self._reconnects += 1
            # Gentle backoff only.  A peer that was moving data a moment ago
            # is reached again in milliseconds once the link is back, and the
            # reconnect budget already bounds how long we keep trying, so a
            # steep backoff would only add dead time to every recovery.
            backoff = min(2.0, backoff * 1.25)

    def _has_live_streams(self) -> bool:
        """Whether any data-stream thread can still deliver a message."""
        return any(
            thread.is_alive()
            for thread in self._threads
            if thread is not threading.current_thread()
        )

    def _serve_stream(self, conn: Connection, index: int) -> None:
        """Answer CHUNK_REQ messages on one data connection."""
        try:
            conn.sock.settimeout(120.0)
        except OSError:
            # The connection was torn down before this thread got going.
            self._log(f"stream {index}: socket already closed, nothing to serve")
            return
        self._log(f"stream {index}: serving")
        served = 0
        try:
            while not self._stop.is_set():
                try:
                    frame = conn.recv_data()
                except socket.timeout:
                    self._log(f"stream {index}: idle timeout after {served} request(s)")
                    break
                if isinstance(frame, ChunkFrame):
                    continue  # the sender never receives chunks
                served += 1
                if not self._handle_control(frame, conn):
                    break
        except (ConnectionClosed, ProtocolError, OSError, AttributeError) as exc:
            # Never let a stream die silently: a stream that stops answering
            # leaves the receiver waiting for a chunk that will never come, and
            # all it can report is "no reply within Ns" -- which says nothing
            # about why, and cost several CI rounds to pin down.
            #
            # But do not cry wolf during an orderly shutdown.  serve() sets
            # _stop and then closes the data connections, which wakes every
            # stream thread parked in recv() with a socket error (WSAENOTSOCK
            # on Windows).  That is the shutdown working, not a failure, and
            # reporting it as one buries the real errors.
            if self._stop.is_set() or self.cancel_event.is_set():
                self._log(f"stream {index}: closed during shutdown (served {served})")
            else:
                self._log(
                    f"stream {index}: died after {served} request(s): "
                    f"{type(exc).__name__}: {exc}"
                )
        finally:
            self._log(f"stream {index}: exiting (served {served})")
            try:
                conn.abort()
            except Exception:
                pass

    def _log(self, message: str) -> None:
        """Diagnostics for the sending side, gated like the receiving side's."""
        if os.environ.get("EVERSEND_DEBUG"):
            print(f"[send {self.transfer_id[:8]}] {message}", file=sys.stderr, flush=True)

    def _handle_control(self, frame: Frame, conn: Connection) -> bool:
        """Process a control frame on the sender side.  False = stop."""
        if frame.type == MSG_NEED_STREAMS:
            try:
                info = json.loads(frame.payload.decode("utf-8"))
                count = int(info.get("n", 1))
            except (ValueError, UnicodeDecodeError):
                count = 1
            self._open_replacement_streams(max(1, min(MAX_STREAMS, count)))
            return True

        if frame.type == MSG_DIGEST_REQ:
            try:
                request = json.loads(frame.payload.decode("utf-8"))
                file_index = int(request["i"])
            except (ValueError, KeyError, UnicodeDecodeError):
                return True
            digest = self._digest_for(file_index)
            try:
                conn.send_json(MSG_FILE_DIGEST, {"t": self.transfer_id, "i": file_index, "h": digest})
            except Exception:
                pass
            self.events.emit(
                "send_file_verified" if digest else "send_file_unverified",
                transfer_id=self.transfer_id,
                index=file_index,
            )
            return True

        if frame.type == MSG_CHUNK_REQ:
            try:
                request = json.loads(frame.payload.decode("utf-8"))
                file_index = int(request["f"])
                chunk_index = int(request["i"])
            except (ValueError, KeyError, UnicodeDecodeError):
                return True
            self._send_one_chunk(conn, file_index, chunk_index)
            return True

        if frame.type == MSG_PROGRESS:
            try:
                info = json.loads(frame.payload.decode("utf-8"))
                done = int(info.get("bytes", 0))
            except (ValueError, UnicodeDecodeError):
                return True
            with self._lock:
                self._done_bytes = done
                self.stats.record(done)
            self.events.emit(
                "send_progress",
                transfer_id=self.transfer_id,
                bytes=done,
                total=self.total_bytes,
            )
            return True

        if frame.type == MSG_FILE_DONE:
            try:
                info = json.loads(frame.payload.decode("utf-8"))
                file_index = int(info.get("i", -1))
            except (ValueError, UnicodeDecodeError):
                return True
            item = self.items.get(file_index)
            if item is not None:
                item.status = "done"
                item.done_bytes = item.entry.size
                self.stats.done_files += 1
            self.events.emit("send_file_done", transfer_id=self.transfer_id, index=file_index)
            return True

        if frame.type == MSG_TRANSFER_DONE:
            try:
                info = json.loads(frame.payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                info = {}
            status = str(info.get("status", "done"))
            self._finish_status = status
            if status != "done":
                self.error = str(info.get("error", "")) or "the receiver reported a failure"
            # Whichever connection carried the report ends the session, which
            # also unblocks a serve() loop waiting out a lost control channel.
            self._finished.set()
            return False

        if frame.type == MSG_CANCEL:
            self._cancelled = True
            self.error = _frame_error(frame) or "接收方取消了这次传输"
            self._finished.set()
            return False

        if frame.type == MSG_ERROR:
            # A late error must not overwrite a result that already arrived.
            # The receiver forgets a finished session immediately, so a
            # replacement stream opened by the supervisor in the same instant
            # as completion is legitimately rejected as an unknown transfer --
            # that race says nothing about the transfer's outcome.
            if self._finish_status or self._finished.is_set():
                return True
            self.error = _frame_error(frame)
            self._finished.set()
            return False

        return True

    def _open_replacement_streams(self, count: int) -> None:
        """Open extra data connections because the receiver lost some.

        A dropped Wi-Fi link or a NAT rebinding kills TCP connections; without
        this the transfer would fail with most of the work already done.
        """
        if self.stream_opener is None:
            return
        try:
            conns = self.stream_opener(count)
        except Exception as exc:
            self.error = f"could not open replacement streams: {exc}"
            return
        for conn in conns:
            index = self._next_stream_index
            self._next_stream_index += 1
            try:
                self.attach_stream(conn, index)
            except Exception:
                conn.abort()
                continue
            thread = threading.Thread(
                target=self._serve_stream,
                args=(conn, index),
                name=f"send-{self.transfer_id[:8]}-r{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        if conns:
            self.events.emit(
                "send_streams_added", transfer_id=self.transfer_id, count=len(conns)
            )

    def _digest_for(self, file_index: int) -> str:
        """Whole-file digest, computed without an extra read where possible."""
        known = self._digests.get(file_index)
        if known:
            return known

        entry = next((e for e in self.entries if e.index == file_index), None)
        if entry is None:
            return ""
        source = self.sources.get(file_index, "")

        # 1. The incremental hasher has almost always seen every byte already,
        #    because chunks are handed out in file order.
        hasher = self._hashers.get(file_index)
        if hasher is not None and hasher.can_finish_incrementally(entry.size):
            digest = hasher.finish()
        elif self._digest_cache is not None and source:
            # 2. Fall back to the on-disk cache, then to a fresh read.
            try:
                digest = self._digest_cache.digest_for(source, entry.size, entry.mtime_ns)
            except OSError:
                digest = ""
        elif source:
            from .hashing import file_digest

            try:
                digest = file_digest(source)
            except OSError:
                digest = ""
        else:
            digest = ""

        if digest:
            self._digests[file_index] = digest
            if self._digest_cache is not None and source:
                self._digest_cache.put(source, entry.size, entry.mtime_ns, digest)
        return digest

    def _close_readers(self) -> None:
        """Close every source reader, and refuse to open another one.

        The stream threads are joined with a *timeout*, so one of them can
        still be on its way into :meth:`_send_one_chunk` when the session tears
        down.  If it then found the cache empty it would open a second reader
        for a file that nobody will ever close again -- because the cleanup has
        already run and the session is finished.

        On POSIX that is one leaked descriptor.  On Windows the source file
        stays **locked**, and that is how this was found: the packaged
        ``--cli selftest`` could not delete its temporary directory and exited
        1 with "The process cannot access the file because it is being used by
        another process" -- a green transfer and a red exit code.
        """
        with self._reader_lock:
            self._readers_closed = True
            readers = list(self._readers.values())
            self._readers.clear()
        for reader in readers:
            try:
                reader.close()
            except Exception:  # pragma: no cover - closing must never raise
                pass

    def _send_one_chunk(self, conn: Connection, file_index: int, chunk_index: int) -> None:
        """Read one chunk from the source file and push it down the stream.

        The chunk is answered on the connection that asked for it, which is
        what keeps load balanced across streams without a central scheduler.
        """
        if file_index not in self.accepted:
            self._log(
                f"chunk {file_index}/{chunk_index}: file not in accepted list "
                f"{sorted(self.accepted)} -- 忽略"
            )
            return
        with self._reader_lock:
            if self._readers_closed:
                # The session is over; a reader opened now would never be
                # closed again.  See :meth:`_close_readers`.
                return
            reader = self._readers.get(file_index)
            if reader is None:
                entry = next((e for e in self.entries if e.index == file_index), None)
                source = self.sources.get(file_index)
                if entry is None or not source:
                    return
                try:
                    reader = FileReader(source, entry.size, entry.chunk_size)
                except OSError as exc:
                    self.error = f"cannot read {source}: {exc}"
                    return
                self._readers[file_index] = reader

        entry = next(e for e in self.entries if e.index == file_index)
        from .constants import chunk_range

        _offset, length = chunk_range(chunk_index, entry.chunk_size, entry.size)
        try:
            view = reader.read_chunk(chunk_index, length)
        except (OSError, TransferFailed, ValueError) as exc:
            # Tell the peer.  Returning silently leaves the receiver waiting
            # for a chunk that will never come, and all it can report is a
            # timeout -- which says nothing about the actual problem (a file
            # that vanished or became unreadable mid-transfer).  ``ValueError``
            # is in the list because a session that is tearing down closes its
            # readers, and reading from a closed one says exactly that.
            self.error = str(exc)
            try:
                conn.send_json(MSG_ERROR, {"message": f"读取 {entry.name} 失败：{exc}"})
            except Exception:
                pass
            return

        crc = crc32(view)
        hasher = self._hashers.get(file_index)
        if hasher is None:
            hasher = IncrementalFileHasher()
            self._hashers[file_index] = hasher
        if not hasher.overflowing:
            hasher.update(chunk_index * entry.chunk_size, view)
        conn.send_chunk(file_index, chunk_index, chunk_index * entry.chunk_size, view, crc)

    # -- lifecycle ---------------------------------------------------------

    def cancel(self, reason: str = "cancelled by user") -> None:
        self._cancelled = True
        self.error = reason
        self._stop.set()
        self.cancel_event.set()
        try:
            self.control.send_json(MSG_CANCEL, {"t": self.transfer_id, "reason": reason})
        except Exception:
            pass


def _frame_error(frame: Frame) -> str:
    try:
        return str(json.loads(frame.payload.decode("utf-8")).get("message", "peer error"))
    except Exception:
        return "peer error"


class EventSink:
    """Callable event sink; the engine supplies a real one."""

    def emit(self, kind: str, **payload: Any) -> None:  # pragma: no cover - interface
        raise NotImplementedError


__all__ = [
    "ChunkQueue",
    "EventSink",
    "FileReader",
    "OfferDecision",
    "ReceiveSession",
    "SendSession",
    "TransferCancelled",
    "TransferFailed",
]
