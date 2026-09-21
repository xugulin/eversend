"""Headless command line interface.

Useful for scripting, for servers without a display, and as the smoke test the
packaging toolchain runs against a freshly built artifact.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Sequence

from .core.constants import APP_NAME, APP_VERSION, DEFAULT_WEB_PORT
from .core.engine import Engine, EngineConfig, build_file_entries
from .core.model import default_device_name, human_bytes, human_speed
from .core.platform_open import default_download_dir
from .core.sockutil import list_interfaces


def _default_config(args: argparse.Namespace) -> EngineConfig:
    root = os.environ.get("EVERSEND_HOME") or os.getcwd()
    data_dir = os.path.join(root, "data")
    receive_dir = args.receive or os.path.join(root, "received")
    if not os.access(root, os.W_OK):
        import getpass
        import tempfile

        who = getpass.getuser()
        data_dir = os.path.join(tempfile.gettempdir(), f"eversend-{who}")
        receive_dir = args.receive or os.path.join(default_download_dir(), "EverSend")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(receive_dir, exist_ok=True)

    return EngineConfig(
        data_dir=data_dir,
        receive_dir=receive_dir,
        name=args.name or default_device_name(),
        tcp_port=args.port,
        streams=args.streams,
        pin=args.pin or "",
        auto_accept_all=args.auto_accept,
        auto_accept_trusted=True,
        enable_web=args.web,
        web_port=getattr(args, "web_port", DEFAULT_WEB_PORT) or DEFAULT_WEB_PORT,
        enable_broadcast=not args.no_broadcast,
        enable_mdns=not args.no_mdns,
    )


def _print_devices(engine: Engine) -> None:
    devices = engine.devices()
    if not devices:
        print("   （还没有发现设备）")
        return
    for index, peer in enumerate(devices):
        trusted = " [已信任]" if peer.trusted else ""
        print(
            f"   [{index}] {peer.info.name:<20} {peer.address}:{peer.port:<6} "
            f"{peer.info.platform}{trusted}"
        )


def _start_web_ui(engine: Engine, args: argparse.Namespace):
    """Bring up the browser interface when asked, or explain why not.

    ``--web`` originally only set a configuration flag and nothing ever read
    it, so ``--cli serve --web`` ran happily while serving no pages at all --
    the phone just got a connection refused.  Starting it here is what makes
    the documented headless-plus-phone mode real.
    """
    if not getattr(args, "web", False):
        return None
    port = int(getattr(args, "web_port", DEFAULT_WEB_PORT) or DEFAULT_WEB_PORT)
    try:
        from .web import create_web_ui

        ui = create_web_ui(engine, port=port)
        bound = ui.start(port)
        engine.info.web_port = bound
        print(f"   手机访问 : {ui.url}")
        for extra in ui.urls()[1:3]:
            print(f"              {extra}")
        return ui
    except Exception as exc:
        print(f"   手机访问 : 启动失败 —— {exc}", file=sys.stderr)
        return None


def _cmd_serve(args: argparse.Namespace) -> int:
    config = _default_config(args)
    engine = Engine(config)
    engine.start()
    web_ui = _start_web_ui(engine, args)
    print(f"{APP_NAME} {APP_VERSION} 已启动")
    print(f"   设备名称 : {engine.info.name}")
    print(f"   设备 ID  : {engine.identity.device_id}")
    print(f"   传输端口 : {engine.port}")
    print(f"   接收目录 : {config.receive_dir}")
    for iface in list_interfaces(include_virtual=False):
        host = f"[{iface.address}]" if iface.is_ipv6 else iface.address
        print(f"   本机地址 : {host}:{engine.port}")
    print()
    print("按 Ctrl+C 退出。")

    try:
        while True:
            time.sleep(1.0)
            for transfer in engine.active_transfers():
                stats = transfer.stats
                sys.stdout.write(
                    f"\r   {transfer.direction} {transfer.peer.name}: "
                    f"{human_bytes(stats.done_bytes)}/{human_bytes(stats.total_bytes)} "
                    f"{human_speed(stats.instant_speed_bps)}   "
                )
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        if web_ui is not None:
            try:
                web_ui.stop()
            except Exception:
                pass
        engine.stop()
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    config = _default_config(args)
    engine = Engine(config)
    engine.start()
    print("正在搜索同一局域网内的设备…")
    for _ in range(args.seconds):
        time.sleep(1.0)
        sys.stdout.write(".")
        sys.stdout.flush()
    print()
    _print_devices(engine)
    engine.stop()
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    config = _default_config(args)
    engine = Engine(config)
    engine.start()

    if args.to:
        host, _, port_text = args.to.partition(":")
        port = int(port_text) if port_text else engine.port
        peer = engine.add_manual_device(host, port)
    else:
        print("正在搜索接收设备…")
        deadline = time.monotonic() + args.wait
        peer = None
        while time.monotonic() < deadline:
            devices = engine.devices()
            if devices:
                peer = devices[0]
                break
            time.sleep(0.5)
        if peer is None:
            print("没有发现任何设备。请用 --to <IP[:端口]> 手动指定。", file=sys.stderr)
            engine.stop()
            return 1

    entries, _sources = build_file_entries(args.paths)
    if not entries:
        print("没有可发送的文件。", file=sys.stderr)
        engine.stop()
        return 1
    total = sum(entry.size for entry in entries)
    print(f"发送 {len(entries)} 个文件（{human_bytes(total)}）到 {peer.info.name}…")

    queue = engine.events.subscribe()
    result = {"ok": False}
    import threading

    def run() -> None:
        try:
            result["ok"] = engine.send(peer, args.paths, pin=args.pin or "")
        except Exception as exc:
            print(f"\n发送失败：{exc}", file=sys.stderr)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    started = time.monotonic()
    last = 0.0
    stats = None
    while thread.is_alive():
        try:
            queue.get(timeout=0.3)
        except Exception:
            pass
        now = time.monotonic()
        if now - last >= 0.25:
            last = now
            active = engine.active_transfers()
            if active:
                stats = active[0].stats
                sys.stdout.write(
                    f"\r   {human_bytes(stats.done_bytes)}/{human_bytes(stats.total_bytes)} "
                    f"{human_speed(stats.instant_speed_bps)}   "
                )
                sys.stdout.flush()
    thread.join(timeout=5)
    elapsed = max(1e-6, time.monotonic() - started)

    # A final summary, because a progress line that stops updating is not an
    # answer: the user needs to know it finished, how long it took and how
    # fast it went.
    #
    # On success the summary uses the *offered* total rather than the last
    # progress sample: progress arrives in coarse jumps (the receiver throttles
    # its reports), so the final sample routinely lags the real figure and the
    # user would be told a 762 MB transfer delivered 635 MB.
    if result["ok"]:
        done = total
    else:
        done = stats.done_bytes if stats is not None else 0
    print(
        f"\r   {human_bytes(done)} 完成，用时 {elapsed:.1f}s，"
        f"平均 {human_speed(done / elapsed)}          "
    )
    engine.stop()
    return 0 if result["ok"] else 1


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Quick sanity check used by the packaging toolchain."""
    print(f"{APP_NAME} {APP_VERSION} self-test")
    ok = True

    from .core import crypto

    print(f"   cryptography : {'available' if crypto.CRYPTO_AVAILABLE else 'MISSING'}")
    if not crypto.CRYPTO_AVAILABLE:
        ok = False

    config = _default_config(args)
    engine = Engine(config)
    try:
        engine.start()
    except Exception as exc:
        print(f"   cannot start engine: {exc}")
        return 1
    print(f"   listening    : port {engine.port}")
    print(f"   device id    : {engine.identity.device_id}")
    print(f"   data dir     : {config.data_dir}")
    print(f"   receive dir  : {config.receive_dir}")

    # Loopback transfer: proves the whole pipeline in this very process.
    import hashlib
    import socket
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "selftest.bin")
        payload = os.urandom(3 * 1024 * 1024)
        with open(source, "wb") as fh:
            fh.write(payload)

        receiver_config = EngineConfig(
            data_dir=os.path.join(tmp, "state"),
            receive_dir=os.path.join(tmp, "recv"),
            name="selftest-receiver",
            tcp_port=0,
            discovery_port=_free_port(),
            auto_accept_all=True,
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
        )
        receiver = Engine(receiver_config)
        receiver.start()
        # Collect both sides' events.  Without this a failed loop transfer
        # prints `sent=False` and nothing else -- which is exactly what
        # happened on the CI Linux runner, and it took a second round trip
        # through the logs to find out the receiver had simply never answered.
        sender_events = engine.events.subscribe()
        receiver_events = receiver.events.subscribe()
        try:
            peer = engine.add_manual_device("127.0.0.1", receiver.port, "selftest")
            transferred = engine.send(peer, [source])
            target = os.path.join(receiver_config.receive_dir, "selftest.bin")
            landed = os.path.exists(target)
            same = landed and hashlib.sha256(open(target, "rb").read()).hexdigest() == hashlib.sha256(payload).hexdigest()
            print(f"   loop transfer: sent={transferred} landed={landed} identical={same}")
            if not (transferred and landed and same):
                print(f"   发送端: {engine.info.name} 端口 {engine.port}")
                print(f"   接收端: {receiver.info.name} 端口 {receiver.port} 目录 {receiver_config.receive_dir}")
                for label, queue_ in (("发送端事件", sender_events), ("接收端事件", receiver_events)):
                    interesting = []
                    while not queue_.empty():
                        event = queue_.get_nowait()
                        if event.get("kind") in (
                            "send_finished", "transfer_finished", "transfer_rejected",
                            "offer_received", "file_failed", "warning", "peer_rejected",
                        ):
                            interesting.append({k: v for k, v in event.items() if k not in ("ts", "peer")})
                    print(f"   {label}: {interesting[-4:] if interesting else '（无）'}")
            ok = ok and transferred and landed and same
        finally:
            receiver.stop()

    engine.stop()
    print("   RESULT       :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eversend",
        description=f"{APP_NAME} — 跨平台局域网文件互传",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--name", help="本机显示名称")
    common.add_argument("--port", type=int, default=52117, help="传输端口（默认 52117）")
    common.add_argument("--streams", type=int, default=4, help="并行连接数（默认 4）")
    common.add_argument("--pin", help="接收 PIN")
    common.add_argument("--receive", help="接收目录")
    common.add_argument("--web", action="store_true", help="启用手机浏览器界面")
    common.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT,
                        help=f"浏览器界面端口（默认 {DEFAULT_WEB_PORT}）")
    common.add_argument("--no-broadcast", action="store_true", help="禁用 UDP 广播发现")
    common.add_argument("--no-mdns", action="store_true", help="禁用 mDNS 发现")

    subs = parser.add_subparsers(dest="command")

    serve = subs.add_parser("serve", parents=[common], help="常驻运行，接收别人发来的文件")
    serve.add_argument("--auto-accept", action="store_true", help="自动接收，无需确认")
    serve.set_defaults(func=_cmd_serve)

    discover = subs.add_parser("discover", parents=[common], help="搜索局域网内的设备")
    discover.add_argument("--seconds", type=int, default=3)
    discover.set_defaults(func=_cmd_discover)

    send = subs.add_parser("send", parents=[common], help="发送文件")
    send.add_argument("paths", nargs="+", help="要发送的文件或目录")
    send.add_argument("--to", help="目标 IP[:端口]，省略则自动选择第一台发现的设备")
    send.add_argument("--wait", type=float, default=5.0, help="等待发现设备的秒数")
    send.set_defaults(func=_cmd_send)

    selftest = subs.add_parser("selftest", parents=[common], help="自检")
    selftest.set_defaults(func=_cmd_selftest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Line-buffer stdout.  When output is a pipe or a file -- which is exactly
    # what `run.sh --cli serve > log` does -- Python block-buffers it, so a
    # long-running server prints nothing at all until 8 KiB accumulates.  A
    # user who starts the server and sees an empty terminal will assume it
    # failed.  Reconfiguring is cheap and fixes every entry point at once.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    parser = build_parser()
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))
    if not getattr(args, "command", None):
        # No subcommand: behave like "serve" so `run.sh --cli` just works.
        args = parser.parse_args(["serve", *(argv if argv is not None else sys.argv[1:])])
    args.auto_accept = getattr(args, "auto_accept", False)
    return args.func(args)


__all__ = ["build_parser", "main"]
