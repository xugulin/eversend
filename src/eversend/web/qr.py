"""QR codes for the browser UI.

The encoder itself lives in :mod:`eversend.core.qr`: it is protocol-adjacent
functionality that the desktop UI needs as well (to draw the "scan to connect"
code), and having two independent encoders was a real duplication.  The core
one was verified module-for-module against a reference implementation across
every version, error-correction level and mask, and decoded back by an
independent scanner, so it is the one that survived.

This module re-exports it so existing imports keep working::

    from eversend.web import qr
    matrix, version, level, mask = qr.encode("http://192.168.1.5:52119/")
"""

from __future__ import annotations

from ..core.qr import (  # noqa: F401
    MAX_VERSION,
    _REMAINDER_BITS,
    QrError,
    data_capacity,
    encode,
    free_module_count,
    iter_matrix,
    matrix_text,
    png,
    raw_data_modules,
    svg,
    symbol_capacity_codewords,
    total_codewords,
)

__all__ = [
    "MAX_VERSION",
    "_REMAINDER_BITS",
    "QrError",
    "data_capacity",
    "encode",
    "free_module_count",
    "iter_matrix",
    "matrix_text",
    "png",
    "raw_data_modules",
    "svg",
    "symbol_capacity_codewords",
    "total_codewords",
]
