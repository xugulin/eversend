"""PySide6 desktop front-end (Linux / Windows / macOS)."""

from __future__ import annotations

__all__ = ["main"]


def main(argv=None):
    from .app import main as _main

    return _main(argv)
