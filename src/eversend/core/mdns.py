"""A very small mDNS (DNS-SD) implementation.

Only what discovery needs: build a PTR/SRV/TXT/A announcement, parse incoming
records (including DNS name compression, which every real responder uses), and
turn a TXT record back into an :class:`~eversend.core.discovery.Announcement`.

Deliberately not a general DNS library -- there are no dependencies to pull in
and the whole thing is about 200 lines.
"""

from __future__ import annotations

import socket
import struct
import time

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
TYPE_AAAA = 28
TYPE_ANY = 255

CLASS_IN = 1
#: mDNS sets the top bit of the class to mean "cache flush".
CLASS_FLUSH = 0x8000

_FLAGS_RESPONSE = 0x8400


class DnsRecord:
    """One parsed resource record."""

    __slots__ = ("name", "rtype", "rclass", "ttl", "rdata", "target", "priority", "weight", "port")

    def __init__(
        self,
        name: str,
        rtype: int,
        rclass: int,
        ttl: int,
        rdata: bytes,
        target: str = "",
        priority: int = 0,
        weight: int = 0,
        port: int = 0,
    ) -> None:
        self.name = name
        self.rtype = rtype
        self.rclass = rclass
        self.ttl = ttl
        self.rdata = rdata
        self.target = target
        self.priority = priority
        self.weight = weight
        self.port = port

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DnsRecord {self.name} type={self.rtype} len={len(self.rdata)}>"


def encode_name(name: str) -> bytes:
    """Encode a domain name as a sequence of length-prefixed labels."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")[:63]
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def _read_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    """Decode a (possibly compressed) name, returning it and the next offset."""
    if depth > 12:
        raise ValueError("DNS name compression loop")

    labels: list[str] = []
    position = offset
    next_offset = -1

    while True:
        if position >= len(data):
            raise ValueError("truncated DNS name")
        length = data[position]

        if length == 0:
            position += 1
            break
        if length & 0xC0 == 0xC0:
            if position + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | data[position + 1]
            if next_offset < 0:
                next_offset = position + 2
            suffix, _ = _read_name(data, pointer, depth + 1)
            if suffix:
                labels.append(suffix)
            break
        position += 1
        labels.append(data[position : position + length].decode("utf-8", "replace"))
        position += length

    name = ".".join(labels)
    return name, (next_offset if next_offset >= 0 else position)


def parse_questions(data: bytes) -> list[tuple[str, int]]:
    """The question section: ``(name, qtype)`` pairs, lower-cased names.

    Needed to *answer* queries.  The desktop already knew how to build an
    announcement (``build_announcement``) but nothing ever called it, so other
    mDNS implementations -- including Android's own ``NsdManager`` -- could
    only find this program by waiting for its 30-second broadcast.  A phone
    that just tapped 「搜索电脑」 will not wait that long.
    """
    if len(data) < 12:
        return []
    _ident, _flags, qdcount, _an, _ns, _ar = struct.unpack_from("!HHHHHH", data, 0)
    offset = 12
    questions: list[tuple[str, int]] = []
    for _ in range(qdcount):
        try:
            name, offset = _read_name(data, offset)
        except ValueError:
            return questions
        if offset + 4 > len(data):
            return questions
        qtype, _qclass = struct.unpack_from("!HH", data, offset)
        offset += 4
        questions.append((name.lower(), qtype))
    return questions


def parse_records(data: bytes) -> list[DnsRecord]:
    """Parse the answer/authority/additional sections of a DNS message."""
    if len(data) < 12:
        return []
    _ident, _flags, qdcount, ancount, nscount, arcount = struct.unpack_from("!HHHHHH", data, 0)
    offset = 12

    for _ in range(qdcount):
        try:
            _name, offset = _read_name(data, offset)
        except ValueError:
            return []
        offset += 4  # QTYPE + QCLASS
        if offset > len(data):
            return []

    records: list[DnsRecord] = []
    for _ in range(ancount + nscount + arcount):
        try:
            name, offset = _read_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, rclass, ttl, rdlength = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            rdata = data[offset : offset + rdlength]
            if len(rdata) < rdlength:
                break
            offset += rdlength
        except (ValueError, struct.error):
            break

        record = DnsRecord(name, rtype, rclass & ~CLASS_FLUSH, ttl, rdata)
        if rtype == TYPE_SRV and len(rdata) >= 6:
            record.priority, record.weight, record.port = struct.unpack_from("!HHH", rdata, 0)
            try:
                record.target, _ = _read_name(data, offset - rdlength + 6)
            except ValueError:
                record.target = ""
        elif rtype == TYPE_PTR:
            try:
                record.target, _ = _read_name(data, offset - rdlength)
            except ValueError:
                record.target = ""
        records.append(record)
    return records


def parse_txt(rdata: bytes) -> dict[str, str]:
    """Decode a TXT record into a key/value map."""
    out: dict[str, str] = {}
    position = 0
    while position < len(rdata):
        length = rdata[position]
        position += 1
        entry = rdata[position : position + length]
        position += length
        try:
            text = entry.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "=" in text:
            key, value = text.split("=", 1)
            out[key] = value
        elif text:
            out[text] = ""
    return out


def announcement_from_txt(rdata: bytes, address: str):
    """Build an :class:`Announcement` from a EverSend TXT record."""
    from .discovery import Announcement

    fields = parse_txt(rdata)
    device_id = fields.get("id", "")
    if not device_id:
        return None
    return Announcement(
        device_id=device_id,
        name=fields.get("n", "unknown"),
        kind=fields.get("k", "desktop"),
        platform=fields.get("p", "unknown"),
        version=fields.get("v", ""),
        port=int(fields.get("port", 0) or 0),
        web_port=int(fields.get("web", 0) or 0),
        timestamp=time.time(),
    )


def build_announcement(
    *,
    instance: str,
    service_type: str,
    host: str,
    port: int,
    txt: dict[str, str],
    address: str = "",
    ttl: int = 120,
) -> bytes:
    """Build an mDNS response advertising one service instance.

    Emits PTR (service -> instance), SRV (instance -> host:port), TXT (our
    metadata) and, when ``address`` is given, an A record.
    """
    service = service_type
    instance_fqdn = f"{instance}.{service}"
    host_fqdn = host if host.endswith(".") else host + "."

    records: list[bytes] = []

    def rr(name: str, rtype: int, rdata: bytes, flush: bool = False) -> bytes:
        rclass = CLASS_IN | (CLASS_FLUSH if flush else 0)
        header = encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata))
        return header + rdata

    # PTR: service type -> instance (shared, so no cache-flush bit).
    records.append(rr(service, TYPE_PTR, encode_name(instance_fqdn)))
    # SRV: instance -> host + port (unique, cache-flush on).
    srv_rdata = struct.pack("!HHH", 0, 0, port) + encode_name(host_fqdn)
    records.append(rr(instance_fqdn, TYPE_SRV, srv_rdata, flush=True))
    # TXT: our metadata.
    txt_rdata = bytearray()
    for key, value in txt.items():
        entry = f"{key}={value}".encode("utf-8")[:255]
        txt_rdata.append(len(entry))
        txt_rdata.extend(entry)
    if not txt_rdata:
        txt_rdata.append(0)
    records.append(rr(instance_fqdn, TYPE_TXT, bytes(txt_rdata), flush=True))
    # A: the address to dial.
    if address:
        try:
            records.append(rr(host_fqdn, TYPE_A, socket.inet_aton(address), flush=True))
        except OSError:
            pass

    header = struct.pack("!HHHHHH", 0, _FLAGS_RESPONSE, 0, len(records), 0, 0)
    return header + b"".join(records)


def build_query(service_type: str) -> bytes:
    """Build a PTR query for ``service_type``."""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question = encode_name(service_type) + struct.pack("!HH", TYPE_PTR, CLASS_IN)
    return header + question


__all__ = [
    "parse_questions",
    "CLASS_IN",
    "DnsRecord",
    "TYPE_A",
    "TYPE_PTR",
    "TYPE_SRV",
    "TYPE_TXT",
    "announcement_from_txt",
    "build_announcement",
    "build_query",
    "encode_name",
    "parse_records",
    "parse_txt",
]
