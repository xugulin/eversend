"""Socket tuning and network-interface helpers.

Throughput of a single TCP connection is bounded by ``window / RTT``.  On a
LAN the RTT is well under a millisecond so the default buffers are plenty, but
the moment a transfer crosses a VPN, a relay or a Wi-Fi hop the default
autotuning is not enough and a single stream stalls around 100-300 Mbps.  The
fix is to ask for large buffers **before** connecting (Linux freezes the
autotuning ceiling at connect time) and to run several streams in parallel.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import struct
import subprocess
import sys
import time

from .constants import (
    SOCKET_BUFFER_SIZE,
    TCP_KEEPALIVE_COUNT,
    TCP_KEEPALIVE_IDLE,
    TCP_KEEPALIVE_INTERVAL,
)

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_ANDROID = sys.platform == "android" or "ANDROID_ROOT" in os.environ


# ---------------------------------------------------------------------------
# Interface enumeration
# ---------------------------------------------------------------------------


class Interface:
    """A usable local network interface address."""

    __slots__ = ("name", "address", "netmask", "is_ipv6", "index", "is_loopback", "is_virtual", "speed_mbps")

    def __init__(
        self,
        name: str,
        address: str,
        netmask: str | None = None,
        index: int = 0,
        is_loopback: bool = False,
        speed_mbps: int = 0,
    ) -> None:
        self.name = name
        self.address = address
        self.netmask = netmask
        self.is_ipv6 = ":" in address
        self.index = index
        self.is_loopback = is_loopback
        self.speed_mbps = speed_mbps
        self.is_virtual = _looks_virtual(name)

    @property
    def broadcast(self) -> str | None:
        """Directed broadcast address for IPv4 interfaces."""
        if self.is_ipv6 or not self.netmask:
            return None
        try:
            net = ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)
            return str(net.broadcast_address)
        except ValueError:
            return None

    @property
    def network(self) -> ipaddress.IPv4Network | None:
        if self.is_ipv6 or not self.netmask:
            return None
        try:
            return ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)
        except ValueError:
            return None

    def __repr__(self) -> str:  # pragma: no cover
        flag = " virtual" if self.is_virtual else ""
        return f"<Interface {self.name} {self.address}/{self.netmask or '-'}{flag}>"


#: Interface name fragments that almost always mean "not a real LAN link".
#: Announcements sent on these reach nobody but cost a socket each, and
#: subnet-scanning a Docker bridge is pure waste.
_VIRTUAL_HINTS = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vmnet",
    "vboxnet",
    "vethernet",
    "hyper-v",
    "tailscale",
    "zt",  # ZeroTier
    "wg",  # WireGuard
    "tun",
    "tap",
    "utun",
    "loopback",
    "bluetooth",
    "ham",
    "dummy",
    "sit",
    "isatap",
    "teredo",
)

#: Interface names that are virtual but still perfectly usable for LAN
#: discovery when nothing better exists (e.g. a laptop whose only link is
#: ZeroTier).  They are kept but sorted last.
_VIRTUAL_REAL_HINTS = ("tailscale", "zt", "wg", "tun", "tap", "utun")


def _looks_virtual(name: str) -> bool:
    low = name.lower()
    for hint in _VIRTUAL_HINTS:
        if low.startswith(hint) or hint in low:
            return not any(real in low for real in _VIRTUAL_REAL_HINTS)
    return False


#: Short-lived cache of the interface list.  The GUI asks for it on every
#: header refresh and every QR dialog; re-enumerating each time is wasteful and
#: on some platforms genuinely slow.
_INTERFACE_CACHE: tuple[float, list["Interface"]] | None = None
_INTERFACE_CACHE_TTL = 5.0


def list_interfaces(
    include_virtual: bool = True,
    include_loopback: bool = False,
    *,
    use_cache: bool = True,
) -> list[Interface]:
    """Enumerate local interfaces with addresses.

    Uses :mod:`socket` + platform specific ioctls rather than a third-party
    dependency so the portable build stays dependency-free for this part.
    """
    global _INTERFACE_CACHE
    if use_cache and _INTERFACE_CACHE is not None:
        stamp, cached = _INTERFACE_CACHE
        if time.monotonic() - stamp < _INTERFACE_CACHE_TTL:
            return _filter_interfaces(cached, include_virtual, include_loopback)

    found: list[Interface] = []
    seen: set[str] = set()

    def add(name: str, addr: str, mask: str | None, index: int = 0, loopback: bool = False) -> None:
        if addr in seen:
            return
        seen.add(addr)
        iface = Interface(name, addr, mask, index, loopback)
        iface.speed_mbps = _interface_speed(name)
        found.append(iface)

    # getaddrinfo with AI_PASSIVE gives no interface list; use the platform
    # specific sources instead.
    if IS_WINDOWS:
        found.extend(_windows_interfaces())
    else:
        try:
            for index, name in socket.if_nameindex():
                addrs = _unix_if_addrs(name, index)
                for addr, mask, loop in addrs:
                    add(name, addr, mask, index, loop)
        except (OSError, AttributeError):
            pass

    if not found:
        # Last-resort fallback: whatever the default route gives us.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 53))
                add("default", probe.getsockname()[0], "255.255.255.0")
            finally:
                probe.close()
        except OSError:
            pass

    _INTERFACE_CACHE = (time.monotonic(), list(found))
    return _filter_interfaces(found, include_virtual, include_loopback)


def _filter_interfaces(
    interfaces: list[Interface], include_virtual: bool, include_loopback: bool
) -> list[Interface]:
    result = []
    for iface in interfaces:
        if iface.is_loopback and not include_loopback:
            continue
        if iface.is_virtual and not include_virtual:
            continue
        result.append(iface)

    # Real, routable, non-virtual links first; then by speed.
    result.sort(key=lambda i: (i.is_virtual, i.is_loopback, -i.speed_mbps, i.name))
    return result


def _unix_if_addrs(name: str, index: int) -> list[tuple[str, str | None, bool]]:
    """Return ``[(address, netmask, is_loopback)]`` for a Unix interface."""
    out: list[tuple[str, str | None, bool]] = []
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-Unix
        return out

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # IPv4 address
        try:
            packed = struct.pack("256s", name[:15].encode())
            addr = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, packed)[20:24])  # SIOCGIFADDR
        except OSError:
            addr = None
        if addr:
            try:
                packed = struct.pack("256s", name[:15].encode())
                mask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x891B, packed)[20:24])  # SIOCGIFNETMASK
            except OSError:
                mask = "255.255.255.0"
            try:
                packed = struct.pack("256s", name[:15].encode())
                flags = struct.unpack("H", fcntl.ioctl(probe.fileno(), 0x8913, packed)[16:18])[0]  # SIOCGIFFLAGS
                loopback = bool(flags & 0x8)  # IFF_LOOPBACK
                up = bool(flags & 0x1)  # IFF_UP
            except OSError:
                loopback, up = False, True
            if up:
                out.append((addr, mask, loopback))
    finally:
        probe.close()

    out.extend((address, None, loopback) for address in _ipv6_addresses(name))
    return out


def _ipv6_addresses(name: str) -> list[str]:
    """Global-unicast IPv6 addresses of an interface.

    Deliberately does **not** use ``socket.getaddrinfo(name, ...)``.  Passing an
    interface name such as ``wlan0`` to the resolver makes it attempt a DNS
    lookup for that name, which on a network without a fast resolver stalls for
    the full resolver timeout -- measured at **12 seconds** for a single
    interface here.  Reading the kernel's own table is instant and exact.

    Link-local addresses (``fe80::/10``) are skipped: they are only reachable
    together with a scope id that the peer cannot guess.
    """
    result: list[str] = []

    if IS_LINUX:
        try:
            with open("/proc/net/if_inet6", "r", encoding="ascii") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 6 or parts[5] != name:
                        continue
                    packed = bytes.fromhex(parts[0])
                    address = str(ipaddress.IPv6Address(packed))
                    if address.startswith("fe80"):
                        continue
                    result.append(address)
            return result
        except (OSError, ValueError):
            return result

    if IS_MACOS:
        # macOS has no /proc; ifconfig is fast and always present.
        try:
            output = subprocess.run(
                ["ifconfig", name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            for line in output.splitlines():
                stripped = line.strip()
                if not stripped.startswith("inet6 "):
                    continue
                address = stripped.split()[1].split("%")[0]
                if address.startswith("fe80") or address == "::1":
                    continue
                result.append(address)
        except (OSError, subprocess.SubprocessError):
            pass
        return result

    return result


def _windows_interfaces() -> list[Interface]:
    """Enumerate interfaces on Windows without third-party modules."""
    out: list[Interface] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            out.append(Interface("win", info[4][0], "255.255.255.0"))
    except (OSError, socket.gaierror):
        pass
    # getaddrinfo only returns the primary address on some setups; gethostbyname_ex
    # returns all of them.
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            out.append(Interface("win", ip, "255.255.255.0"))
    except (OSError, socket.gaierror):
        pass
    # De-duplicate by address, keeping the first.
    uniq: dict[str, Interface] = {}
    for iface in out:
        uniq.setdefault(iface.address, iface)
    return list(uniq.values())


#: Cache of ``interface -> Mbps``.  Reading the kernel's speed file has to ask
#: the driver, and on an interface that is administratively down that call can
#: block for seconds; the value never changes for a given interface anyway.
_SPEED_CACHE: dict[str, int] = {}


def _interface_speed(name: str) -> int:
    """Best-effort link speed in Mbps (0 when unknown)."""
    if not IS_LINUX:
        return 0
    cached = _SPEED_CACHE.get(name)
    if cached is not None:
        return cached

    value = 0
    try:
        # Only ask the driver when the link is actually up: on a down
        # interface this read is the one that blocks.
        with open(f"/sys/class/net/{name}/operstate", "rb") as fh:
            if fh.read().strip() == b"up":
                with open(f"/sys/class/net/{name}/speed", "rb") as speed_file:
                    raw = int(speed_file.read().strip())
                value = raw if raw > 0 else 0
    except (OSError, ValueError):
        value = 0

    _SPEED_CACHE[name] = value
    return value


def is_rotational_disk(path: str) -> bool:
    """Whether the filesystem holding ``path`` is on a spinning disk.

    Used to reduce the number of parallel writers: random writes from many
    threads destroy throughput on a HDD, while on an SSD they are free.
    """
    if not IS_LINUX:
        return False
    try:
        dev = os.stat(path).st_dev
        major = os.major(dev)
        minor = os.minor(dev)
        # Resolve the device name from /sys/dev/block.
        link = os.readlink(f"/sys/dev/block/{major}:{minor}")
        name = os.path.basename(link)
        # Walk up in case of a partition (sda1 -> sda).
        for candidate in (name, name.rstrip("0123456789")):
            try:
                with open(f"/sys/block/{candidate}/queue/rotational", "rb") as fh:
                    return fh.read().strip() == b"1"
            except OSError:
                continue
    except (OSError, ValueError):
        pass
    return False


# ---------------------------------------------------------------------------
# Socket configuration
# ---------------------------------------------------------------------------


def tune_socket(
    sock: socket.socket,
    *,
    buffers: int = SOCKET_BUFFER_SIZE,
    keepalive: bool = True,
    nodelay: bool = True,
) -> None:
    """Apply throughput- and reliability-oriented options to ``sock``.

    Must be called *before* ``connect()`` on the client side for the send
    buffer to take effect on Linux: once the connection is established the
    kernel freezes the autotuning ceiling at whatever it decided.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffers)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffers)
    except OSError:
        pass
    if nodelay:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
    if keepalive:
        enable_keepalive(sock)
    # Do not let a stalled peer pin a file descriptor forever: the engine's
    # own idle timeouts normally fire first, this is the backstop.
    try:
        sock.settimeout(None)
    except OSError:
        pass


def enable_keepalive(sock: socket.socket) -> None:
    """Turn on TCP keepalive with aggressive, portable timings."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return

    # Linux
    for opt_name, value in (
        ("TCP_KEEPIDLE", TCP_KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", TCP_KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", TCP_KEEPALIVE_COUNT),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass

    if IS_MACOS:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, 0x10, TCP_KEEPALIVE_IDLE)  # TCP_KEEPALIVE
        except OSError:
            pass

    if IS_WINDOWS:
        # Windows uses a single keepalive timeout in milliseconds plus an
        # interval, configured through SIO_KEEPALIVE_VALS.
        try:
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, TCP_KEEPALIVE_IDLE * 1000, TCP_KEEPALIVE_INTERVAL * 1000),
            )
        except (OSError, AttributeError):
            pass


def actual_socket_buffers(sock: socket.socket) -> tuple[int, int]:
    """Return the effective ``(SO_SNDBUF, SO_RCVBUF)``.

    Linux doubles the requested value, so callers should not assume the
    value they asked for is the value they got.
    """
    snd = rcv = 0
    try:
        snd = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    except OSError:
        pass
    try:
        rcv = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError:
        pass
    return snd, rcv


def create_multicast_listener(
    group: str,
    port: int,
    interfaces: list[Interface],
    *,
    reuse_port: bool,
) -> list[socket.socket]:
    """Bind one multicast receiver per interface.

    One socket per interface is required because a multicast socket only
    receives the group on the interface it last joined, and because a single
    socket that joined several groups on the same host would deliver
    duplicates.

    ``SO_REUSEADDR`` plus ``SO_REUSEPORT`` are set so several EverSend
    instances (or a restart racing the old process) can share the port.
    """
    sockets: list[socket.socket] = []
    is_v6 = ":" in group

    for iface in interfaces:
        if iface.is_ipv6 != is_v6:
            continue
        fam = socket.AF_INET6 if is_v6 else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if reuse_port and hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            if is_v6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass

            if is_v6:
                sock.bind((group, port, 0, iface.index))
                membership = struct.pack("@16sI", socket.inet_pton(socket.AF_INET6, group), iface.index)
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, membership)
            else:
                sock.bind(("", port))
                membership = socket.inet_aton(group) + socket.inet_aton(iface.address)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)

            sock.settimeout(0.5)
            sockets.append(sock)
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
    return sockets


def create_multicast_sender(group: str, port: int, interface: Interface) -> socket.socket | None:
    """Create a UDP socket that sends to ``group`` out of ``interface``."""
    is_v6 = ":" in group
    fam = socket.AF_INET6 if is_v6 else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_DGRAM)
    try:
        if is_v6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, interface.index)
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
            except OSError:
                pass
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface.address))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            # Loopback on so a second instance on the same host is discovered.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def create_broadcast_sender(interface: Interface) -> socket.socket | None:
    """Create a UDP socket that can send to ``255.255.255.255`` from ``interface``."""
    if interface.is_ipv6:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((interface.address, 0))
        sock.settimeout(0.5)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def create_broadcast_listener(port: int) -> socket.socket | None:
    """Bind a UDP socket that receives broadcasts on ``port``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", port))
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.settimeout(0.5)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def set_reuse(sock: socket.socket) -> None:
    """Allow immediate rebinding of a listening socket after a restart."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    if not IS_WINDOWS and hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass


def is_dual_stack_available() -> bool:
    """Whether an IPv6 socket can be created at all."""
    if not socket.has_ipv6:
        return False
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.close()
        return True
    except OSError:
        return False


def create_listener(host: str, port: int, *, dual_stack: bool = True) -> list[socket.socket]:
    """Bind TCP listeners, preferring a dual-stack IPv6 socket.

    Returns a list because the caller may have to fall back to separate IPv4
    and IPv6 sockets when the platform refuses ``IPV6_V6ONLY=0``.
    """
    listeners: list[socket.socket] = []
    if dual_stack and is_dual_stack_available():
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            set_reuse(sock)
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
            sock.bind(("::", port))
            sock.listen(128)
            sock.settimeout(0.5)
            return [sock]
        except OSError:
            try:
                sock.close()
            except OSError:
                pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    set_reuse(sock)
    sock.bind((host, port))
    sock.listen(128)
    sock.settimeout(0.5)
    listeners.append(sock)
    return listeners


def errno_name(exc: OSError) -> str:
    """Human readable errno for logging."""
    if exc.errno is None:
        return str(exc)
    try:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    except Exception:  # pragma: no cover
        return str(exc.errno)


__all__ = [
    "IS_ANDROID",
    "IS_LINUX",
    "IS_MACOS",
    "IS_WINDOWS",
    "Interface",
    "actual_socket_buffers",
    "create_broadcast_listener",
    "create_broadcast_sender",
    "create_listener",
    "create_multicast_listener",
    "create_multicast_sender",
    "enable_keepalive",
    "errno_name",
    "is_dual_stack_available",
    "is_rotational_disk",
    "list_interfaces",
    "set_reuse",
    "tune_socket",
]
