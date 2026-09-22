"""Protocol-wide constants for EverSend.

Everything that both peers must agree on numerically lives here so the wire
format has exactly one source of truth.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Protocol identity
# ---------------------------------------------------------------------------

#: Magic bytes that open every frame.  Chosen so a stray service probing the
#: port is rejected immediately instead of being parsed as a frame.
MAGIC = b"EVSD"

#: Wire protocol version.  Bump on any incompatible framing/message change.
PROTOCOL_VERSION = 1

#: Human readable version of the application.
APP_NAME = "EverSend"
APP_NAME_CN = "韧传"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

#: Default TCP port for the data/control plane.
DEFAULT_TCP_PORT = 52117

#: Default UDP port used for multicast/broadcast announcements.
DEFAULT_DISCOVERY_PORT = 52118

#: Default HTTP port for the browser (mobile) interface.
DEFAULT_WEB_PORT = 52119

#: IPv4 multicast group for announcements.
#:
#: 239.255.83.68 is in the administratively scoped range (239.0.0.0/8), which
#: routers never forward beyond the local network.  It is deliberately
#: different from LocalSend's 224.0.0.167 so the two applications can share a
#: LAN without feeding on each other's traffic.
MULTICAST_GROUP_V4 = "239.255.83.68"

#: IPv6 multicast group (link-local scope) for announcements.
MULTICAST_GROUP_V6 = "ff12::5357:6472"  # ff12::EVSD

#: mDNS group/port, used as an additional discovery channel because some
#: consumer access points forward mDNS reliably while dropping arbitrary
#: multicast groups.
MDNS_GROUP_V4 = "224.0.0.251"
MDNS_GROUP_V6 = "ff02::fb"
MDNS_PORT = 5353

#: The service type advertised/queried over mDNS.
MDNS_SERVICE_TYPE = "_eversend._tcp.local"

# ---------------------------------------------------------------------------
# Frame layout
# ---------------------------------------------------------------------------

#: ``magic(4) type(1) flags(1) stream(2) length(4)`` -> 12 bytes.
FRAME_HEADER = "!4sBBHI"
FRAME_HEADER_SIZE = 12

#: Hard ceiling for a single frame payload.  Anything larger is treated as a
#: protocol violation and the connection is dropped -- this is what keeps a
#: hostile/corrupt peer from making us allocate unbounded memory.
#:
#: 64 MiB leaves room for a 16 MiB chunk plus AEAD tag and future control
#: payloads, while staying far below "accidentally OOM the receiver".
MAX_FRAME_PAYLOAD = 64 * 1024 * 1024

#: Ceiling for control (JSON) frames specifically.  Control messages are
#: small; a large one means something is wrong.
MAX_CONTROL_PAYLOAD = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

#: Smallest chunk size (1 MiB).
MIN_CHUNK_SIZE = 1 * 1024 * 1024

#: Largest chunk size (16 MiB).
MAX_CHUNK_SIZE = 16 * 1024 * 1024

#: Chunk sizes are rounded down to a multiple of this, which keeps every
#: chunk (and therefore every disk write) aligned.
CHUNK_ALIGN = 64 * 1024

#: Target number of chunks per file.  The chunk size is picked so a file ends
#: up with roughly this many chunks, clamped to [MIN_CHUNK_SIZE, MAX_CHUNK_SIZE].
#:
#: ~4096 chunks means a bitmap of 512 bytes per file: cheap to exchange, and
#: small enough that per-chunk bookkeeping never dominates.
TARGET_CHUNKS_PER_FILE = 4096

#: Overhead of the chunk sub-header inside a CHUNK frame payload
#: ``seq(4) index(4) offset(8) length(4) crc(4)`` = 24 bytes.
CHUNK_HEADER = "!IIQII"
CHUNK_HEADER_SIZE = 24


def pick_chunk_size(file_size: int) -> int:
    """Return the chunk size to use for a file of ``file_size`` bytes.

    The goal is a chunk count near :data:`TARGET_CHUNKS_PER_FILE` while
    staying inside the allowed range and keeping chunks aligned.  A single
    formula is used by both peers, so they always agree without negotiating.
    """
    if file_size <= 0:
        return MIN_CHUNK_SIZE

    raw = file_size // TARGET_CHUNKS_PER_FILE
    # Round down to alignment, then clamp.
    aligned = max(CHUNK_ALIGN, (raw // CHUNK_ALIGN) * CHUNK_ALIGN)
    return max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, aligned))


def chunk_count(file_size: int, chunk_size: int) -> int:
    """Number of chunks a file of ``file_size`` bytes is split into.

    An empty file still has exactly one (empty) chunk so that every file has
    a well defined "all chunks present" state.
    """
    if file_size <= 0:
        return 1
    return (file_size + chunk_size - 1) // chunk_size


def chunk_range(index: int, chunk_size: int, file_size: int) -> tuple[int, int]:
    """Return ``(offset, length)`` of chunk ``index`` within a file."""
    offset = index * chunk_size
    if offset >= file_size:
        return offset, 0
    return offset, min(chunk_size, file_size - offset)


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

# Control plane (JSON payloads)
MSG_HELLO = 0x01
MSG_HELLO_ACK = 0x02
MSG_AUTH = 0x03
MSG_AUTH_OK = 0x04

#: Sent by the sender on an extra connection to bind it to a transfer.
MSG_ATTACH = 0x05
#: Receiver asks the sender for replacement data streams after some died.
MSG_NEED_STREAMS = 0x06

MSG_OFFER = 0x10
MSG_OFFER_ACK = 0x11
MSG_OFFER_REJECT = 0x12

MSG_CHUNK_REQ = 0x20
MSG_CHUNK = 0x21
MSG_CHUNK_BATCH_REQ = 0x22

MSG_FILE_DONE = 0x30
MSG_FILE_VERIFIED = 0x31
MSG_TRANSFER_DONE = 0x32
#: Receiver asks the sender for a file's whole-file digest once every chunk
#: has landed.  Requesting it on demand (instead of putting it in the OFFER)
#: is what lets the sender start pushing bytes immediately: it computes the
#: digest while the transfer runs, so no pre-pass over the file is needed.
MSG_DIGEST_REQ = 0x33
MSG_FILE_DIGEST = 0x34
MSG_FILE_READY = 0x35

MSG_PROGRESS = 0x38
MSG_CANCEL = 0x40
MSG_ERROR = 0x41
MSG_PING = 0x50
MSG_PONG = 0x51
MSG_BYE = 0x60

# Chat.  One message per short-lived connection: the sender connects, hands the
# message over, waits for the ack and hangs up.  A chat line is a few hundred
# bytes, so a connection per message costs one round trip on the LAN and saves
# every peer from keeping a session (and its failure modes) alive forever.
MSG_CHAT = 0x70
MSG_CHAT_ACK = 0x71

#: Message types whose payload is JSON text.
CONTROL_TYPES = frozenset(
    {
        MSG_ATTACH,
        MSG_HELLO,
        MSG_NEED_STREAMS,
        MSG_HELLO_ACK,
        MSG_AUTH,
        MSG_AUTH_OK,
        MSG_OFFER,
        MSG_OFFER_ACK,
        MSG_OFFER_REJECT,
        MSG_CHUNK_REQ,
        MSG_CHUNK_BATCH_REQ,
        MSG_DIGEST_REQ,
        MSG_FILE_DIGEST,
        MSG_FILE_DONE,
        MSG_FILE_READY,
        MSG_FILE_VERIFIED,
        MSG_CHAT,
        MSG_CHAT_ACK,
        MSG_TRANSFER_DONE,
        MSG_PROGRESS,
        MSG_CANCEL,
        MSG_ERROR,
        MSG_PING,
        MSG_PONG,
        MSG_BYE,
    }
)

MSG_NAMES = {
    MSG_ATTACH: "ATTACH",
    MSG_NEED_STREAMS: "NEED_STREAMS",
    MSG_HELLO: "HELLO",
    MSG_HELLO_ACK: "HELLO_ACK",
    MSG_AUTH: "AUTH",
    MSG_AUTH_OK: "AUTH_OK",
    MSG_OFFER: "OFFER",
    MSG_OFFER_ACK: "OFFER_ACK",
    MSG_OFFER_REJECT: "OFFER_REJECT",
    MSG_CHUNK_REQ: "CHUNK_REQ",
    MSG_CHUNK: "CHUNK",
    MSG_CHUNK_BATCH_REQ: "CHUNK_BATCH_REQ",
    MSG_DIGEST_REQ: "DIGEST_REQ",
    MSG_FILE_DIGEST: "FILE_DIGEST",
    MSG_FILE_DONE: "FILE_DONE",
    MSG_FILE_READY: "FILE_READY",
    MSG_FILE_VERIFIED: "FILE_VERIFIED",
    MSG_TRANSFER_DONE: "TRANSFER_DONE",
    MSG_PROGRESS: "PROGRESS",
    MSG_CANCEL: "CANCEL",
    MSG_ERROR: "ERROR",
    MSG_PING: "PING",
    MSG_PONG: "PONG",
    MSG_BYE: "BYE",
    MSG_CHAT: "CHAT",
    MSG_CHAT_ACK: "CHAT_ACK",
}

# Frame flags
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED = 0x02
FLAG_LAST = 0x04

# ---------------------------------------------------------------------------
# Roles / connection kinds
# ---------------------------------------------------------------------------

#: A connection that carries only control messages for a transfer.
ROLE_CONTROL = 0
#: A connection that carries chunk data (and control messages when needed).
ROLE_DATA = 1

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

#: TCP connect timeout when dialling a peer (seconds).
CONNECT_TIMEOUT = 6.0

#: How long to wait for HELLO/HELLO_ACK during the handshake.
HANDSHAKE_TIMEOUT = 10.0

#: How long a data stream waits for a *requested* chunk before giving up on
#: the request, re-queueing it and dropping the stream.
#:
#: This is deliberately much shorter than :data:`STREAM_IDLE_TIMEOUT`.  A
#: connection that the network killed without sending a reset looks perfectly
#: healthy to a blocked reader, so the only thing that ends the wait is a
#: timeout -- and sitting on the long idle timeout turns a two-second outage
#: into a two-minute stall.  Dropping the stream early is cheap: the chunks it
#: had in flight go back on the shared queue and another stream takes them.
CHUNK_REQUEST_TIMEOUT = 8.0

#: The throughput floor assumed when sizing a chunk deadline, in bytes/second.
#: A link slower than this is better served by retrying the chunk elsewhere
#: than by holding a connection open indefinitely.
CHUNK_DEADLINE_FLOOR_BPS = 512 * 1024

#: Idle timeout for a data connection: if no frame at all arrives within this
#: window the stream is considered dead and its work is re-queued.
STREAM_IDLE_TIMEOUT = 30.0

#: Interval between keepalive PINGs on an idle control connection.
KEEPALIVE_INTERVAL = 15.0

#: How long to wait for an OFFER to be answered by the user before giving up.
OFFER_TIMEOUT = 300.0

#: Announcement burst offsets (seconds).  Repeated sends make discovery
#: survive a single lost datagram and give peers that just booted time to
#: start listening.
ANNOUNCE_DELAYS = (0.0, 0.25, 1.0, 2.5)

#: How often a device re-announces itself while running, in seconds.
ANNOUNCE_INTERVAL = 30.0

#: A discovered device is considered gone after this many seconds without an
#: announcement.
DEVICE_TTL = 90.0

# ---------------------------------------------------------------------------
# Networking tuning
# ---------------------------------------------------------------------------

#: Desired socket send/receive buffer.  Must be requested *before* connect()
#: on Linux for the kernel to honour it, otherwise autotuning caps the window
#: and single-stream throughput collapses on high-latency links.
SOCKET_BUFFER_SIZE = 8 * 1024 * 1024

#: Default number of parallel data connections.
DEFAULT_STREAMS = 4

#: Upper bound when the engine auto-tunes the stream count.
MAX_STREAMS = 16

#: Bytes allowed in flight per data stream.  This is the flow-control window:
#: the receiver will not request more than this much outstanding data on one
#: connection, which bounds memory while still filling a fat pipe.
DEFAULT_WINDOW_BYTES = 8 * 1024 * 1024

#: Listen backlog.
LISTEN_BACKLOG = 128

#: TCP keepalive probing so half-open connections are detected instead of
#: hanging a transfer forever.
TCP_KEEPALIVE_IDLE = 20
TCP_KEEPALIVE_INTERVAL = 5
TCP_KEEPALIVE_COUNT = 4

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Maximum simultaneous transfers served at once.
MAX_ACTIVE_TRANSFERS = 8

#: Maximum simultaneous control connections being handshaked.
MAX_PENDING_HANDSHAKES = 32

#: Chunk index bitmap width in bytes for a given chunk count.
def bitmap_size(nchunks: int) -> int:
    return (nchunks + 7) // 8
