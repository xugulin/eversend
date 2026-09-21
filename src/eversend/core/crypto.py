"""Identity, authenticated key agreement and frame encryption.

Security model
--------------
Every installation owns a persistent **Ed25519** key pair (its identity) and an
**X25519** key pair (for key agreement).  The device id shown in the UI is a
truncated hash of the Ed25519 public key, so a device cannot forge another
device's id without breaking Ed25519.

A session is established in one round trip:

1. the initiator sends ``HELLO`` carrying its X25519 public key and a random
   ``nonce_c``;
2. the responder answers with ``HELLO_ACK`` carrying its own X25519 public key
   and ``nonce_s``;
3. both compute ``X25519(own_secret, peer_public)`` and run it through HKDF
   with ``salt = nonce_c || nonce_s`` to obtain a 32-byte session key.

Because the key depends on both nonces, a replayed handshake yields a
different key.  The identity keys authenticate that exchange: ``HELLO`` and
``HELLO_ACK`` are signed with Ed25519 over a transcript that binds both
nonces, both public keys and both device ids.  An attacker who can relay
traffic but not forge signatures therefore cannot impersonate a peer -- the
worst it can do is a transparent relay, which the **6-digit short
authentication string** (derived from the session key and shown on both
screens) exposes immediately.

Payloads are sealed with an AEAD in counter mode: a 96-bit nonce built from a
per-direction constant and a 64-bit counter, so nonces are never reused as
long as a session key is used once.  AES-256-GCM is preferred where the CPU
has AES acceleration (essentially all x86-64 and ARM64 chips) and
ChaCha20-Poly1305 otherwise.

If the ``cryptography`` package is missing the engine still runs, in plaintext
with CRC32 integrity only, and says so loudly.  A file-transfer tool that
refuses to start because an optional wheel is absent is worse than one that
transfers with a warning.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Optional backend
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised by presence/absence of the wheel
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover
    CRYPTO_AVAILABLE = False


NONCE_SIZE = 12
TAG_SIZE = 16
KEY_SIZE = 32

#: Prefix bytes of the AEAD nonce for each direction.  Combined with a 64-bit
#: counter this guarantees uniqueness per key.
_DIRECTION_INITIATOR = b"\x00\x00\x00\x01"
_DIRECTION_RESPONDER = b"\x00\x00\x00\x02"


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Identity:
    """The persistent cryptographic identity of this installation."""

    device_id: str
    ed25519_private: bytes
    ed25519_public: bytes
    x25519_private: bytes
    x25519_public: bytes
    name: str = ""
    created: float = field(default_factory=time.time)

    # Cached key objects (cryptography primitives are not picklable and are
    # recreated lazily after a load).
    _ed_priv: Any = field(default=None, repr=False, compare=False)
    _ed_pub: Any = field(default=None, repr=False, compare=False)
    _x_priv: Any = field(default=None, repr=False, compare=False)
    _x_pub: Any = field(default=None, repr=False, compare=False)

    @property
    def ed25519_pub_b64(self) -> str:
        return b64e(self.ed25519_public)

    @property
    def x25519_pub_b64(self) -> str:
        return b64e(self.x25519_public)

    @property
    def short_id(self) -> str:
        """A short, human-friendly form of the device id."""
        return self.device_id[:8]

    # -- key objects -------------------------------------------------------

    def _load_keys(self) -> None:
        if not CRYPTO_AVAILABLE:
            return
        if self._ed_priv is None:
            self._ed_priv = ed25519.Ed25519PrivateKey.from_private_bytes(self.ed25519_private)
            self._ed_pub = self._ed_priv.public_key()
        if self._x_priv is None:
            self._x_priv = x25519.X25519PrivateKey.from_private_bytes(self.x25519_private)
            self._x_pub = self._x_priv.public_key()

    def sign(self, message: bytes) -> bytes:
        self._load_keys()
        if self._ed_priv is None:
            return b""
        return self._ed_priv.sign(message)

    def agree(self, peer_public: bytes) -> bytes:
        """Raw X25519 shared secret with a peer's public key."""
        self._load_keys()
        if self._x_priv is None:
            return b""
        return self._x_priv.exchange(x25519.X25519PublicKey.from_public_bytes(peer_public))

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "ed25519Private": b64e(self.ed25519_private),
            "ed25519Public": b64e(self.ed25519_public),
            "x25519Private": b64e(self.x25519_private),
            "x25519Public": b64e(self.x25519_public),
            "name": self.name,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Identity:
        return cls(
            device_id=str(data["deviceId"]),
            ed25519_private=b64d(data["ed25519Private"]),
            ed25519_public=b64d(data["ed25519Public"]),
            x25519_private=b64d(data["x25519Private"]),
            x25519_public=b64d(data["x25519Public"]),
            name=str(data.get("name", "")),
            created=float(data.get("created", time.time())),
        )

    @classmethod
    def generate(cls, name: str = "") -> Identity:
        if CRYPTO_AVAILABLE:
            ed_priv = ed25519.Ed25519PrivateKey.generate()
            ed_pub = ed_priv.public_key()
            x_priv = x25519.X25519PrivateKey.generate()
            x_pub = x_priv.public_key()
            ed_priv_bytes = ed_priv.private_bytes_raw()
            ed_pub_bytes = ed_pub.public_bytes_raw()
            x_priv_bytes = x_priv.private_bytes_raw()
            x_pub_bytes = x_pub.public_bytes_raw()
        else:  # pragma: no cover - degraded mode
            ed_priv_bytes = secrets.token_bytes(32)
            ed_pub_bytes = hashlib.blake2b(ed_priv_bytes, digest_size=32).digest()
            x_priv_bytes = secrets.token_bytes(32)
            x_pub_bytes = hashlib.blake2b(x_priv_bytes, digest_size=32).digest()

        return cls(
            device_id=derive_device_id(ed_pub_bytes),
            ed25519_private=ed_priv_bytes,
            ed25519_public=ed_pub_bytes,
            x25519_private=x_priv_bytes,
            x25519_public=x_pub_bytes,
            name=name,
        )


def derive_device_id(ed25519_public: bytes) -> str:
    """A stable, collision-resistant device id from an Ed25519 public key."""
    return hashlib.blake2b(ed25519_public, digest_size=16).hexdigest()


def load_or_create_identity(path: str, name: str = "") -> Identity:
    """Read the identity from ``path``, creating (and persisting) it if absent.

    The file is written with ``0600`` and via a temporary file + rename so a
    crash during creation cannot leave a half-written identity behind.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        identity = Identity.from_dict(data)
        if name and identity.name != name:
            identity.name = name
        return identity
    except (OSError, ValueError, KeyError):
        pass

    identity = Identity.generate(name)
    save_identity(identity, path)
    return identity


def save_identity(identity: Identity, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    payload = json.dumps(identity.to_dict(), indent=2).encode("utf-8")
    if os.name == "posix":
        # Text content, so text mode is harmless -- but be explicit so a
        # later reader cannot be surprised by platform-dependent endings.
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600
        )
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        with open(tmp, "wb") as fh:
            fh.write(payload)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Session keys
# ---------------------------------------------------------------------------


def derive_session_key(
    shared_secret: bytes,
    nonce_initiator: bytes,
    nonce_responder: bytes,
    *,
    context: bytes = b"eversend/v1/session",
) -> bytes:
    """HKDF-SHA256 over the X25519 secret, bound to both handshake nonces."""
    salt = hashlib.sha256(nonce_initiator + nonce_responder).digest()
    if CRYPTO_AVAILABLE:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_SIZE,
            salt=salt,
            info=context,
        )
        return hkdf.derive(shared_secret)

    # Fallback: HKDF implemented directly on hmac (still correct, just slower).
    prk = hmac.new(salt, shared_secret, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < KEY_SIZE:
        block = hmac.new(prk, block + context + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:KEY_SIZE]


def handshake_transcript(
    initiator_id: str,
    responder_id: str,
    initiator_x25519: bytes,
    responder_x25519: bytes,
    nonce_initiator: bytes,
    nonce_responder: bytes,
) -> bytes:
    """The exact byte string both sides sign and verify.

    Binding every element of the exchange into one transcript is what stops a
    peer from replaying somebody else's handshake parameters.
    """
    return b"|".join(
        (
            b"eversend/v1/handshake",
            initiator_id.encode("utf-8", "replace"),
            responder_id.encode("utf-8", "replace"),
            initiator_x25519,
            responder_x25519,
            nonce_initiator,
            nonce_responder,
        )
    )


def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool:
    if not CRYPTO_AVAILABLE:
        return False
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except Exception:
        return False


def short_auth_string(session_key: bytes) -> str:
    """Six digits derived from the session key.

    Both users see the same number; if it differs, somebody is in the middle.
    This is the same idea as Signal's safety number, reduced to something a
    person can compare in two seconds.
    """
    digest = hashlib.sha256(b"eversend/v1/sas" + session_key).digest()
    value = int.from_bytes(digest[:4], "big") % 1_000_000
    return f"{value:06d}"


# ---------------------------------------------------------------------------
# AEAD
# ---------------------------------------------------------------------------


def _prefer_aesgcm() -> bool:
    """Whether AES-256-GCM is likely to be the faster AEAD on this CPU.

    AES-GCM runs at multiple GB/s with AES-NI (every x86-64 chip since ~2010,
    every ARMv8 chip with the crypto extensions).  Without it, AES-GCM is
    table-driven and slow, and ChaCha20-Poly1305 wins comfortably.
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "i386", "i686"):
        return True
    return machine in ("aarch64", "arm64", "armv8", "armv8l")


class Cipher:
    """Seals and opens frame payloads for one direction of one session.

    The seal side is internally locked.  The connection layer already holds its
    own send lock around sealing, so this is a second line of defence rather
    than the primary one -- but a nonce is a counter, two threads sealing at
    once would reuse one, and nonce reuse is the single AEAD mistake that
    breaks confidentiality rather than merely integrity.  That is worth a lock
    that costs nothing next to the encryption itself.  Opening stays
    single-threaded: exactly one reader owns a connection.
    """

    __slots__ = ("_aead", "_nonce_prefix", "_counter", "_algorithm", "_lock")

    def __init__(self, key: bytes, direction: bytes, algorithm: str) -> None:
        self._algorithm = algorithm
        prefix = _DIRECTION_INITIATOR if direction == b"i" else _DIRECTION_RESPONDER
        self._nonce_prefix = prefix
        self._counter = 0
        self._lock = threading.Lock()
        if algorithm == "aes256gcm":
            self._aead = AESGCM(key)
        else:
            self._aead = ChaCha20Poly1305(key)

    @property
    def algorithm(self) -> str:
        return self._algorithm

    def _next_nonce(self) -> bytes:
        nonce = self._nonce_prefix + struct.pack("!Q", self._counter)
        self._counter += 1
        return nonce

    def seal(self, plaintext: bytes | memoryview, aad: bytes = b"") -> bytes:
        """Encrypt and authenticate; returns ciphertext||tag."""
        with self._lock:
            return self._aead.encrypt(self._next_nonce(), bytes(plaintext), aad)

    def open(self, ciphertext: bytes | memoryview, aad: bytes = b"") -> bytes:
        """Verify and decrypt.  Raises ``InvalidTag`` on tampering."""
        return self._aead.decrypt(self._next_nonce(), bytes(ciphertext), aad)

    def seal_into(self, plaintext: bytes | memoryview, aad: bytes = b"") -> bytes:
        return self.seal(plaintext, aad)

    @staticmethod
    def overhead() -> int:
        return TAG_SIZE


class NullCipher:
    """Pass-through used when encryption is disabled or unavailable."""

    __slots__ = ()

    algorithm = "none"

    def seal(self, plaintext: bytes | memoryview, aad: bytes = b"") -> bytes:
        return bytes(plaintext)

    def open(self, ciphertext: bytes | memoryview, aad: bytes = b"") -> bytes:
        return bytes(ciphertext)

    @staticmethod
    def overhead() -> int:
        return 0


class SessionCrypto:
    """The two directions of an established session."""

    __slots__ = ("send", "recv", "key", "sas", "peer_id", "algorithm")

    def __init__(
        self,
        send: Any,
        recv: Any,
        key: bytes,
        peer_id: str,
        algorithm: str,
    ) -> None:
        self.send = send
        self.recv = recv
        self.key = key
        self.sas = short_auth_string(key)
        self.peer_id = peer_id
        self.algorithm = algorithm


def establish_session(
    identity: Identity,
    peer_id: str,
    peer_x25519_public_b64: str,
    nonce_initiator: bytes,
    nonce_responder: bytes,
    *,
    initiator: bool,
    algorithm: str | None = None,
) -> SessionCrypto:
    """Derive the session keys for one side of a handshake."""
    if algorithm is None:
        algorithm = "aes256gcm" if _prefer_aesgcm() else "chacha20poly1305"

    if not CRYPTO_AVAILABLE:
        return SessionCrypto(NullCipher(), NullCipher(), b"", peer_id, "none")

    peer_public = b64d(peer_x25519_public_b64)
    shared = identity.agree(peer_public)
    key = derive_session_key(shared, nonce_initiator, nonce_responder)

    # The initiator encrypts with "i" and decrypts with "r"; the responder
    # does the opposite, so the two directions can never share a nonce.
    if initiator:
        send = Cipher(key, b"i", algorithm)
        recv = Cipher(key, b"r", algorithm)
    else:
        send = Cipher(key, b"r", algorithm)
        recv = Cipher(key, b"i", algorithm)

    return SessionCrypto(send, recv, key, peer_id, algorithm)


NULL_SESSION_ALGORITHM = "none"


def new_nonce() -> bytes:
    """A fresh 16-byte handshake nonce."""
    return secrets.token_bytes(16)


__all__ = [
    "CRYPTO_AVAILABLE",
    "Cipher",
    "Identity",
    "NULL_SESSION_ALGORITHM",
    "NullCipher",
    "SessionCrypto",
    "b64d",
    "b64e",
    "derive_device_id",
    "derive_session_key",
    "establish_session",
    "handshake_transcript",
    "load_or_create_identity",
    "new_nonce",
    "save_identity",
    "short_auth_string",
    "verify_signature",
]
