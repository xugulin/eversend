"""The browser (mobile) front end of EverSend.

PySide6 cannot run on Android, so a phone drives the engine through this
package instead: :class:`~eversend.web.server.WebUI` serves a mobile-first
single-page app plus a JSON/SSE API on
:data:`~eversend.core.constants.DEFAULT_WEB_PORT`, and every call it makes
goes through the same :class:`~eversend.core.engine.Engine` object the desktop
UI uses.  There is no second implementation of the protocol to keep in sync.

Typical use::

    from eversend.core.engine import Engine, EngineConfig
    from eversend.web import create_web_ui

    engine = Engine(EngineConfig(data_dir="...", receive_dir="..."))
    engine.start()
    ui = create_web_ui(engine)          # binds 0.0.0.0:52119
    print(ui.url)                       # the URL to encode in a QR code
    ...
    ui.stop()
    engine.stop()
"""

from __future__ import annotations

from typing import Any

from ..core.engine import Engine
from .server import (
    DEFAULT_MAX_UPLOAD,
    MAX_JSON_BODY,
    SSE_HEARTBEAT,
    TOKEN_HEADER,
    WebUI,
    device_dict,
    display_address,
    event_dict,
    local_addresses,
    peer_dict,
    serve,
    transfer_dict,
)

__all__ = [
    "DEFAULT_MAX_UPLOAD",
    "MAX_JSON_BODY",
    "SSE_HEARTBEAT",
    "TOKEN_HEADER",
    "WebUI",
    "create_web_ui",
    "device_dict",
    "display_address",
    "event_dict",
    "local_addresses",
    "peer_dict",
    "serve",
    "transfer_dict",
]


def create_web_ui(engine: Engine, **kwargs: Any) -> WebUI:
    """Build (but do not start) the web UI for ``engine``.

    A factory rather than a bare class so callers -- the desktop app, a
    headless ``eversend --web`` mode, the self-test -- have one obvious entry
    point, and so the constructor's options can grow without breaking them.

    Keyword arguments are :class:`WebUI`'s: ``host``, ``port``, ``asset_dir``,
    ``max_upload_bytes``, ``keep_uploads``, ``extra_hosts``, ``advertise`` and
    ``log_requests``.
    """
    return WebUI(engine, **kwargs)
