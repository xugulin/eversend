"""A self-signed certificate, generated on first run.

Why this exists
---------------
A browser only hands over the microphone in a **secure context**.  A phone
opening ``http://192.168.1.5:52119/`` is not one, so ``getUserMedia`` is simply
unavailable and voice messages are impossible -- no flag, no workaround.  Serving
the same page over HTTPS is the only way, and on a LAN with no domain name that
means a self-signed certificate.

What that costs, honestly: the phone shows "not private / continue?" the first
time.  That is expected for a self-signed certificate and the page says so.  The
certificate is generated **locally** and never sent anywhere; the private key
stays in the data directory with the identity key.  Nothing here protects
against someone already on the LAN -- the transfer protocol's own X25519/AEAD
handshake does that, and it is unaffected by this.

``cryptography`` is already part of the green pack (it is what the transfer
protocol encrypts with), so this adds no dependency.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import os
import socket
from typing import Iterable

#: How long a generated certificate is valid.  Self-signed and pinned by the
#: user's click-through, so a long life is convenience, not a security claim.
VALID_DAYS = 3650

CERT_NAME = "eversend-cert.pem"
KEY_NAME = "eversend-key.pem"


class TlsUnavailable(RuntimeError):
    """Raised when a certificate cannot be produced (no ``cryptography``)."""


def _sans(extra: Iterable[str]) -> tuple[list, list]:
    """Split names into IP and DNS subjectAltNames."""
    ips, names = [], []
    candidates: list[str] = ["127.0.0.1", "::1", "localhost", socket.gethostname()]
    try:
        from .sockutil import list_interfaces

        candidates += [i.address for i in list_interfaces(include_loopback=True)]
    except Exception:  # pragma: no cover - interface listing is best effort
        pass
    candidates += [str(x) for x in extra if x]
    host = socket.gethostname()
    if host:
        candidates += [f"{host}.local"]

    for raw in candidates:
        text = str(raw).strip()
        if not text or text in ("0.0.0.0", "::"):
            continue
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            if text not in names:
                names.append(text)
        else:
            if ip not in ips:
                ips.append(ip)
    return ips, names


def ensure_certificate(data_dir: str, *, extra_names: Iterable[str] = ()) -> tuple[str, str]:
    """Return ``(cert_path, key_path)``, creating them once if needed.

    Regenerated when the machine's addresses change, because a certificate whose
    SANs do not cover the address the phone dials produces a *different* warning
    (and some browsers refuse outright).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except Exception as exc:  # pragma: no cover - exercised without the wheel
        raise TlsUnavailable(f"需要 cryptography 才能生成证书：{exc}") from exc

    directory = os.path.join(str(data_dir), "tls")
    os.makedirs(directory, exist_ok=True)
    cert_path = os.path.join(directory, CERT_NAME)
    key_path = os.path.join(directory, KEY_NAME)
    ips, names = _sans(extra_names)

    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            with open(cert_path, "rb") as handle:
                existing = x509.load_pem_x509_certificate(handle.read())
            have = {
                str(entry.value)
                for entry in existing.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
            }
            wanted = {str(ip) for ip in ips} | set(names)
            if wanted <= have and existing.not_valid_after_utc > _dt.datetime.now(
                _dt.timezone.utc
            ):
                return cert_path, key_path
        except Exception:
            pass  # unreadable or malformed: just make a new one

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "eversend"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "韧传 EverSend"),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    alt = [x509.IPAddress(ip) for ip in ips] + [x509.DNSName(name) for name in names]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(key_path, 0o600)
    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def ssl_context(data_dir: str, *, extra_names: Iterable[str] = ()):
    """An ``SSLContext`` for the local certificate, or ``None`` if unavailable.

    Returning ``None`` rather than raising keeps the HTTP interface usable on a
    machine without ``cryptography``: voice messages are then simply not
    available, and the UI says so.
    """
    import ssl

    try:
        cert_path, key_path = ensure_certificate(data_dir, extra_names=extra_names)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        return context
    except (TlsUnavailable, OSError, ValueError, ssl.SSLError):
        return None


__all__ = ["TlsUnavailable", "ensure_certificate", "ssl_context", "CERT_NAME", "KEY_NAME"]
