"""EverSend (韧传): fast, resumable, cross-platform file transfer.

The public surface is deliberately small -- everything a front-end needs is on
:class:`eversend.core.engine.Engine`.
"""

from __future__ import annotations

from .core.constants import APP_NAME, APP_NAME_CN, APP_VERSION

__version__ = APP_VERSION

__all__ = ["APP_NAME", "APP_NAME_CN", "APP_VERSION", "__version__"]
