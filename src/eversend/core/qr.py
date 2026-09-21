"""A dependency-free QR Code encoder (ISO/IEC 18004, byte mode).

Why hand-rolled
---------------
The portable EverSend build vendors exactly two wheels (``cryptography`` and
PySide6) and the phone talks to a server that must also run on a bare
``python3`` install.  Pulling in ``qrcode`` + ``Pillow`` just to draw one code
would double the dependency surface of the whole application, and Pillow is a
large native wheel that we would then have to cross-compile for every target.

So the encoder below is pure stdlib and emits **SVG**, which is sharper than a
PNG on every phone (it scales to the physical pixel grid), needs no rasteriser,
and keeps the response a few kilobytes.

Scope: byte mode (the only mode a URL needs), all 40 versions, all four error
correction levels, automatic version and mask selection.  The tables are the
ones published in the standard; :mod:`eversend.web.selftest` checks the block
tables against the module-count identity and the symbol itself is verified
against an independent decoder during development.
"""

from __future__ import annotations

from typing import Iterator, Sequence

# ---------------------------------------------------------------------------
# Galois field GF(2^8), primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
# ---------------------------------------------------------------------------

_GF_EXP: list[int] = [0] * 512
_GF_LOG: list[int] = [0] * 256


def _init_gf() -> None:
    value = 1
    for power in range(255):
        _GF_EXP[power] = value
        _GF_LOG[value] = power
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for power in range(255, 512):
        _GF_EXP[power] = _GF_EXP[power - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(nsym: int) -> list[int]:
    """Generator polynomial ``(x - a^0)(x - a^1)...(x - a^(nsym-1))``.

    Highest-degree coefficient first, with a leading 1.
    """
    poly = [1]
    for i in range(nsym):
        factor = _GF_EXP[i]
        result = [0] * (len(poly) + 1)
        for j, coefficient in enumerate(poly):
            result[j] ^= coefficient
            result[j + 1] ^= _gf_mul(coefficient, factor)
        poly = result
    return poly


def _rs_encode(data: bytes, nsym: int) -> bytes:
    """Return the ``nsym`` Reed-Solomon check codewords for ``data``."""
    generator = _rs_generator(nsym)
    work = list(data) + [0] * nsym
    for i in range(len(data)):
        coefficient = work[i]
        if coefficient:
            for j in range(1, len(generator)):
                work[i + j] ^= _gf_mul(generator[j], coefficient)
    return bytes(work[len(data):])


# ---------------------------------------------------------------------------
# Standard tables
# ---------------------------------------------------------------------------

#: Error correction level -> the 2-bit field stored in the format information.
_EC_FORMAT_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

#: Per version, per level: ``(ec_codewords_per_block, (blocks, data_codewords),
#: (blocks, data_codewords))``.  The second group only exists for versions
#: whose codewords do not divide evenly over the block count; its blocks hold
#: one data codeword more than the first group's.
_BLOCKS: dict[int, dict[str, tuple[int, tuple[int, int], tuple[int, int]]]] = {
    1: {"L": (7, (1, 19), (0, 0)), "M": (10, (1, 16), (0, 0)), "Q": (13, (1, 13), (0, 0)), "H": (17, (1, 9), (0, 0))},
    2: {"L": (10, (1, 34), (0, 0)), "M": (16, (1, 28), (0, 0)), "Q": (22, (1, 22), (0, 0)), "H": (28, (1, 16), (0, 0))},
    3: {"L": (15, (1, 55), (0, 0)), "M": (26, (1, 44), (0, 0)), "Q": (18, (2, 17), (0, 0)), "H": (22, (2, 13), (0, 0))},
    4: {"L": (20, (1, 80), (0, 0)), "M": (18, (2, 32), (0, 0)), "Q": (26, (2, 24), (0, 0)), "H": (16, (4, 9), (0, 0))},
    5: {"L": (26, (1, 108), (0, 0)), "M": (24, (2, 43), (0, 0)), "Q": (18, (2, 15), (2, 16)), "H": (22, (2, 11), (2, 12))},
    6: {"L": (18, (2, 68), (0, 0)), "M": (16, (4, 27), (0, 0)), "Q": (24, (4, 19), (0, 0)), "H": (28, (4, 15), (0, 0))},
    7: {"L": (20, (2, 78), (0, 0)), "M": (18, (4, 31), (0, 0)), "Q": (18, (2, 14), (4, 15)), "H": (26, (4, 13), (1, 14))},
    8: {"L": (24, (2, 97), (0, 0)), "M": (22, (2, 38), (2, 39)), "Q": (22, (4, 18), (2, 19)), "H": (26, (4, 14), (2, 15))},
    9: {"L": (30, (2, 116), (0, 0)), "M": (22, (3, 36), (2, 37)), "Q": (20, (4, 16), (4, 17)), "H": (24, (4, 12), (4, 13))},
    10: {"L": (18, (2, 68), (2, 69)), "M": (26, (4, 43), (1, 44)), "Q": (24, (6, 19), (2, 20)), "H": (28, (6, 15), (2, 16))},
    11: {"L": (20, (4, 81), (0, 0)), "M": (30, (1, 50), (4, 51)), "Q": (28, (4, 22), (4, 23)), "H": (24, (3, 12), (8, 13))},
    12: {"L": (24, (2, 92), (2, 93)), "M": (22, (6, 36), (2, 37)), "Q": (26, (4, 20), (6, 21)), "H": (28, (7, 14), (4, 15))},
    13: {"L": (26, (4, 107), (0, 0)), "M": (22, (8, 37), (1, 38)), "Q": (24, (8, 20), (4, 21)), "H": (22, (12, 11), (4, 12))},
    14: {"L": (30, (3, 115), (1, 116)), "M": (24, (4, 40), (5, 41)), "Q": (20, (11, 16), (5, 17)), "H": (24, (11, 12), (5, 13))},
    15: {"L": (22, (5, 87), (1, 88)), "M": (24, (5, 41), (5, 42)), "Q": (30, (5, 24), (7, 25)), "H": (24, (11, 12), (7, 13))},
    16: {"L": (24, (5, 98), (1, 99)), "M": (28, (7, 45), (3, 46)), "Q": (24, (15, 19), (2, 20)), "H": (30, (3, 15), (13, 16))},
    17: {"L": (28, (1, 107), (5, 108)), "M": (28, (10, 46), (1, 47)), "Q": (28, (1, 22), (15, 23)), "H": (28, (2, 14), (17, 15))},
    18: {"L": (30, (5, 120), (1, 121)), "M": (26, (9, 43), (4, 44)), "Q": (28, (17, 22), (1, 23)), "H": (28, (2, 14), (19, 15))},
    19: {"L": (28, (3, 113), (4, 114)), "M": (26, (3, 44), (11, 45)), "Q": (26, (17, 21), (4, 22)), "H": (26, (9, 13), (16, 14))},
    20: {"L": (28, (3, 107), (5, 108)), "M": (26, (3, 41), (13, 42)), "Q": (30, (15, 24), (5, 25)), "H": (28, (15, 15), (10, 16))},
    21: {"L": (28, (4, 116), (4, 117)), "M": (26, (17, 42), (0, 0)), "Q": (28, (17, 22), (6, 23)), "H": (30, (19, 16), (6, 17))},
    22: {"L": (28, (2, 111), (7, 112)), "M": (28, (17, 46), (0, 0)), "Q": (30, (7, 24), (16, 25)), "H": (24, (34, 13), (0, 0))},
    23: {"L": (30, (4, 121), (5, 122)), "M": (28, (4, 47), (14, 48)), "Q": (30, (11, 24), (14, 25)), "H": (30, (16, 15), (14, 16))},
    24: {"L": (30, (6, 117), (4, 118)), "M": (28, (6, 45), (14, 46)), "Q": (30, (11, 24), (16, 25)), "H": (30, (30, 16), (2, 17))},
    25: {"L": (26, (8, 106), (4, 107)), "M": (28, (8, 47), (13, 48)), "Q": (30, (7, 24), (22, 25)), "H": (30, (22, 15), (13, 16))},
    26: {"L": (28, (10, 114), (2, 115)), "M": (28, (19, 46), (4, 47)), "Q": (28, (28, 22), (6, 23)), "H": (30, (33, 16), (4, 17))},
    27: {"L": (30, (8, 122), (4, 123)), "M": (28, (22, 45), (3, 46)), "Q": (30, (8, 23), (26, 24)), "H": (30, (12, 15), (28, 16))},
    28: {"L": (30, (3, 117), (10, 118)), "M": (28, (3, 45), (23, 46)), "Q": (30, (4, 24), (31, 25)), "H": (30, (11, 15), (31, 16))},
    29: {"L": (30, (7, 116), (7, 117)), "M": (28, (21, 45), (7, 46)), "Q": (30, (1, 23), (37, 24)), "H": (30, (19, 15), (26, 16))},
    30: {"L": (30, (5, 115), (10, 116)), "M": (28, (19, 47), (10, 48)), "Q": (30, (15, 24), (25, 25)), "H": (30, (23, 15), (25, 16))},
    31: {"L": (30, (13, 115), (3, 116)), "M": (28, (2, 46), (29, 47)), "Q": (30, (42, 24), (1, 25)), "H": (30, (23, 15), (28, 16))},
    32: {"L": (30, (17, 115), (0, 0)), "M": (28, (10, 46), (23, 47)), "Q": (30, (10, 24), (35, 25)), "H": (30, (19, 15), (35, 16))},
    33: {"L": (30, (17, 115), (1, 116)), "M": (28, (14, 46), (21, 47)), "Q": (30, (29, 24), (19, 25)), "H": (30, (11, 15), (46, 16))},
    34: {"L": (30, (13, 115), (6, 116)), "M": (28, (14, 46), (23, 47)), "Q": (30, (44, 24), (7, 25)), "H": (30, (59, 16), (1, 17))},
    35: {"L": (30, (12, 121), (7, 122)), "M": (28, (12, 47), (26, 48)), "Q": (30, (39, 24), (14, 25)), "H": (30, (22, 15), (41, 16))},
    36: {"L": (30, (6, 121), (14, 122)), "M": (28, (6, 47), (34, 48)), "Q": (30, (46, 24), (10, 25)), "H": (30, (2, 15), (64, 16))},
    37: {"L": (30, (17, 122), (4, 123)), "M": (28, (29, 46), (14, 47)), "Q": (30, (49, 24), (10, 25)), "H": (30, (24, 15), (46, 16))},
    38: {"L": (30, (4, 122), (18, 123)), "M": (28, (13, 46), (32, 47)), "Q": (30, (48, 24), (14, 25)), "H": (30, (42, 15), (32, 16))},
    39: {"L": (30, (20, 117), (4, 118)), "M": (28, (40, 47), (7, 48)), "Q": (30, (43, 24), (22, 25)), "H": (30, (10, 15), (67, 16))},
    40: {"L": (30, (19, 118), (6, 119)), "M": (28, (18, 47), (31, 48)), "Q": (30, (34, 24), (34, 25)), "H": (30, (20, 15), (61, 16))},
}

#: Alignment pattern centre coordinates per version (empty for version 1).
_ALIGNMENT: dict[int, tuple[int, ...]] = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
    11: (6, 30, 54), 12: (6, 32, 58), 13: (6, 34, 62), 14: (6, 26, 46, 66),
    15: (6, 26, 48, 70), 16: (6, 26, 50, 74), 17: (6, 30, 54, 78),
    18: (6, 30, 56, 82), 19: (6, 30, 58, 86), 20: (6, 34, 62, 90),
    21: (6, 28, 50, 72, 94), 22: (6, 26, 50, 74, 98), 23: (6, 30, 54, 78, 102),
    24: (6, 28, 54, 80, 106), 25: (6, 32, 58, 84, 110), 26: (6, 30, 58, 86, 114),
    27: (6, 34, 62, 90, 118), 28: (6, 26, 50, 74, 98, 122),
    29: (6, 30, 54, 78, 102, 126), 30: (6, 26, 52, 78, 104, 130),
    31: (6, 30, 56, 82, 108, 134), 32: (6, 34, 60, 86, 112, 138),
    33: (6, 30, 58, 86, 114, 142), 34: (6, 34, 62, 90, 118, 146),
    35: (6, 30, 54, 78, 102, 126, 150), 36: (6, 24, 50, 76, 102, 128, 154),
    37: (6, 28, 54, 80, 106, 132, 158), 38: (6, 32, 58, 84, 110, 136, 162),
    39: (6, 26, 54, 82, 110, 138, 166), 40: (6, 30, 58, 86, 114, 142, 170),
}

#: Extra all-zero bits appended after the interleaved codewords.
_REMAINDER_BITS: dict[int, int] = {}
for _v in range(1, 41):
    if _v == 1:
        _REMAINDER_BITS[_v] = 0
    elif _v <= 6:
        _REMAINDER_BITS[_v] = 7
    elif _v <= 13:
        _REMAINDER_BITS[_v] = 0
    elif _v <= 20:
        _REMAINDER_BITS[_v] = 3
    elif _v <= 27:
        _REMAINDER_BITS[_v] = 4
    elif _v <= 34:
        _REMAINDER_BITS[_v] = 3
    else:
        _REMAINDER_BITS[_v] = 0
del _v

MAX_VERSION = 40


def data_capacity(version: int, ec_level: str = "M") -> int:
    """Number of *data* codewords available in ``version`` at ``ec_level``."""
    first, second = _BLOCKS[version][ec_level][1], _BLOCKS[version][ec_level][2]
    return first[0] * first[1] + second[0] * second[1]


def total_codewords(version: int) -> int:
    """Total codewords (data + error correction) in one symbol.

    The block tables of all four levels must agree on this number, which is
    what makes it a useful cross-check of the tables themselves.
    """
    totals = set()
    for ec, first, second in _BLOCKS[version].values():
        totals.add(first[0] * (first[1] + ec) + second[0] * (second[1] + ec))
    if len(totals) != 1:  # pragma: no cover - internal invariant
        raise QrError(f"inconsistent block table for version {version}: {sorted(totals)}")
    return totals.pop()


def raw_data_modules(version: int) -> int:
    """Modules left for data once every function pattern has been drawn.

    This is the standard's closed form for the count.  It is derived from the
    symbol geometry alone, so comparing it with :func:`total_codewords` (which
    comes from the block table) catches a mistyped table entry without needing
    a reference implementation at runtime.
    """
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def symbol_capacity_codewords(version: int) -> int:
    """Total codewords the symbol physically holds (function patterns excluded)."""
    return raw_data_modules(version) // 8


def free_module_count(version: int) -> int:
    """Count the data modules by actually drawing the function patterns.

    Fully independent of :func:`raw_data_modules`, which is why the self-test
    can use the pair to prove both the tables and the drawing code.
    """
    size = version * 4 + 17
    matrix = [[False] * size for _ in range(size)]
    reserved = _draw_function_patterns(matrix, version)
    return sum(1 for line in reserved for module in line if not module)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


class QrError(ValueError):
    """The payload cannot be represented in a QR symbol."""


def encode(
    data: bytes | str,
    *,
    ec_level: str = "M",
    version: int | None = None,
    mask: int | None = None,
    encoding: str = "utf-8",
) -> tuple[list[list[bool]], int, str, int]:
    """Encode ``data`` and return ``(matrix, version, ec_level, mask)``.

    ``matrix[row][col]`` is ``True`` for a dark module, with no quiet zone.
    ``version``/``mask`` may be forced; both are chosen automatically when
    omitted.  A URL is pure ASCII, but anything else is encoded as UTF-8 --
    which is what phones expect for a scanned link.
    """
    level = ec_level.upper()
    if level not in _EC_FORMAT_BITS:
        raise QrError(f"unknown error correction level {ec_level!r}")
    payload = data.encode(encoding) if isinstance(data, str) else bytes(data)

    if version is None:
        version = _pick_version(payload, level)
    if not 1 <= version <= MAX_VERSION:
        raise QrError(f"version {version} out of range")

    capacity = data_capacity(version, level)
    codewords = _make_codewords(payload, version, level, capacity)
    blocks = _make_blocks(codewords, version, level)
    bits = _bit_stream(blocks, version, level)
    if len(bits) % 8 != _REMAINDER_BITS[version]:  # pragma: no cover - invariant
        raise QrError("bit stream does not match the symbol capacity")

    matrix: list[list[bool]] = [[False] * (version * 4 + 17) for _ in range(version * 4 + 17)]
    reserved = _draw_function_patterns(matrix, version)
    _place_bits(matrix, reserved, bits)

    if mask is None:
        # Try all eight masks and keep the one the standard scores lowest.
        # ``_draw_format_bits`` is part of the score because the format
        # information itself contributes to the penalty.
        best, best_penalty = 0, None
        for candidate in range(8):
            _apply_mask(matrix, reserved, candidate)
            _draw_format_bits(matrix, level, candidate)
            penalty = _penalty(matrix)
            if best_penalty is None or penalty < best_penalty:
                best, best_penalty = candidate, penalty
            _apply_mask(matrix, reserved, candidate)  # undo (XOR is its own inverse)
        mask = best

    _apply_mask(matrix, reserved, mask)
    _draw_format_bits(matrix, level, mask)
    return matrix, version, level, mask


def _pick_version(payload: bytes, level: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version <= 9 else 16
        needed_bits = 4 + count_bits + 8 * len(payload)
        if (needed_bits + 7) // 8 <= data_capacity(version, level):
            return version
    raise QrError(f"{len(payload)} bytes do not fit in any QR version at level {level}")


def _make_codewords(payload: bytes, version: int, level: str, capacity: int) -> bytes:
    count_bits = 8 if version <= 9 else 16
    bits: list[int] = []

    def push(value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)  # byte mode
    push(len(payload), count_bits)
    for byte in payload:
        push(byte, 8)

    # Terminator: up to four zero bits, then pad to the codeword boundary.
    limit = capacity * 8
    push(0, min(4, max(0, limit - len(bits))))
    while len(bits) % 8:
        bits.append(0)

    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i : i + 8]:
            byte = (byte << 1) | bit
        out.append(byte)

    # Alternating pad codewords, exactly as the standard prescribes.
    pads = (0xEC, 0x11)
    index = 0
    while len(out) < capacity:
        out.append(pads[index % 2])
        index += 1
    return bytes(out)


def _make_blocks(codewords: bytes, version: int, level: str) -> list[bytes]:
    """Split into blocks, append the EC codewords, then interleave."""
    ec_len, first, second = _BLOCKS[version][level]
    lengths = [first[1]] * first[0] + [second[1]] * second[0]
    if sum(lengths) != len(codewords):  # pragma: no cover - internal invariant
        raise QrError("block table does not match the data length")

    data_blocks: list[bytes] = []
    ec_blocks: list[bytes] = []
    offset = 0
    for length in lengths:
        block = codewords[offset : offset + length]
        offset += length
        data_blocks.append(block)
        ec_blocks.append(_rs_encode(block, ec_len))
    return data_blocks + ec_blocks


def _interleave(blocks: list[bytes], version: int, level: str) -> bytes:
    """Interleave data blocks, then EC blocks, then append remainder bits."""
    ec_len, first, second = _BLOCKS[version][level]
    nblocks = first[0] + second[0]
    data_blocks = blocks[:nblocks]
    ec_blocks = blocks[nblocks:]

    out = bytearray()
    longest = max(len(b) for b in data_blocks)
    for i in range(longest):
        for block in data_blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for block in ec_blocks:
            if i < len(block):
                out.append(block[i])
    return bytes(out)


def _bit_stream(blocks: list[bytes], version: int, level: str) -> list[int]:
    payload = _interleave(blocks, version, level)
    bits: list[int] = []
    for byte in payload:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    bits.extend([0] * _REMAINDER_BITS[version])
    return bits


def _draw_function_patterns(matrix: list[list[bool]], version: int) -> list[list[bool]]:
    """Draw everything that is not data and return the reserved-module mask."""
    size = len(matrix)
    reserved = [[False] * size for _ in range(size)]

    def set_module(row: int, col: int, dark: bool) -> None:
        matrix[row][col] = dark
        reserved[row][col] = True

    # Timing patterns first: the finder patterns below overwrite their ends.
    # The order matters -- drawing the timing last erases the finders' outer
    # rows and columns, which still *decodes* (the finders are recognised by
    # their 1:1:3:1:1 profile) but is not the symbol the standard defines.
    for i in range(size):
        set_module(6, i, i % 2 == 0)
        set_module(i, 6, i % 2 == 0)

    # Finder patterns, separators included (the radius-4 ring is the separator).
    for centre_row, centre_col in ((3, 3), (3, size - 4), (size - 4, 3)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                row, col = centre_row + dy, centre_col + dx
                if 0 <= row < size and 0 <= col < size:
                    set_module(row, col, max(abs(dy), abs(dx)) not in (2, 4))

    # Alignment patterns, skipping the three that would sit on a finder.
    positions = _ALIGNMENT[version]
    if positions:
        last = len(positions) - 1
        for i, row in enumerate(positions):
            for j, col in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        set_module(row + dy, col + dx, max(abs(dy), abs(dx)) != 1)

    # Reserve the format information areas (values are written after masking).
    for i in range(9):
        if not reserved[8][i]:
            set_module(8, i, False)
        if not reserved[i][8]:
            set_module(i, 8, False)
    for i in range(8):
        set_module(8, size - 1 - i, False)
        set_module(size - 1 - i, 8, False)

    # Version information (two 3x6 blocks) for versions 7 and up.  Like the
    # format information it is never masked, so it is written once here.
    if version >= 7:
        for i in range(18):
            row, col = size - 11 + i % 3, i // 3
            set_module(row, col, False)
            set_module(col, row, False)
        _draw_version_bits(matrix, version)

    return reserved


def _draw_format_bits(matrix: list[list[bool]], level: str, mask: int) -> None:
    size = len(matrix)
    data = (_EC_FORMAT_BITS[level] << 3) | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = ((data << 10) | remainder) ^ 0x5412

    def bit(index: int) -> bool:
        return ((bits >> index) & 1) != 0

    for i in range(6):
        matrix[i][8] = bit(i)
    matrix[7][8] = bit(6)
    matrix[8][8] = bit(7)
    matrix[8][7] = bit(8)
    for i in range(9, 15):
        matrix[8][14 - i] = bit(i)
    for i in range(8):
        matrix[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        matrix[size - 15 + i][8] = bit(i)
    matrix[size - 8][8] = True  # the always-dark module


def _draw_version_bits(matrix: list[list[bool]], version: int) -> None:
    if version < 7:
        return
    size = len(matrix)
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
    bits = (version << 12) | remainder
    for i in range(18):
        value = ((bits >> i) & 1) != 0
        row, col = size - 11 + i % 3, i // 3
        matrix[row][col] = value
        matrix[col][row] = value


def _place_bits(matrix: list[list[bool]], reserved: list[list[bool]], bits: Sequence[int]) -> None:
    """Walk the symbol in the standard two-column zigzag and drop bits in."""
    size = len(matrix)
    index = 0
    total = len(bits)
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # the vertical timing pattern column is skipped entirely
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for candidate in (col, col - 1):
                if reserved[row][candidate]:
                    continue
                matrix[row][candidate] = index < total and bits[index] == 1
                index += 1
        upward = not upward
        col -= 2


_MASK_FUNCS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row + col) % 2 + (row * col) % 3) % 2 == 0,
)


def _apply_mask(matrix: list[list[bool]], reserved: list[list[bool]], mask: int) -> None:
    func = _MASK_FUNCS[mask]
    for row, line in enumerate(matrix):
        for col in range(len(line)):
            if not reserved[row][col] and func(row, col):
                line[col] = not line[col]


def _penalty(matrix: list[list[bool]]) -> int:
    """Mask evaluation score; the standard's four rules, lower is better."""
    size = len(matrix)
    score = 0

    # Rule 1: runs of five or more same-coloured modules in a line.
    for line in list(matrix) + [list(column) for column in zip(*matrix)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # Rule 2: 2x2 blocks of one colour.
    for row in range(size - 1):
        for col in range(size - 1):
            first = matrix[row][col]
            if first == matrix[row][col + 1] == matrix[row + 1][col] == matrix[row + 1][col + 1]:
                score += 3

    # Rule 3: finder-like patterns (1:1:3:1:1 with four light modules).
    patterns = (
        (True, False, True, True, True, False, True, False, False, False, False),
        (False, False, False, False, True, False, True, True, True, False, True),
    )
    for line in list(matrix) + [list(column) for column in zip(*matrix)]:
        for i in range(size - 10):
            window = tuple(line[i : i + 11])
            if window == patterns[0] or window == patterns[1]:
                score += 40

    # Rule 4: deviation from a 50% dark ratio.
    dark = sum(1 for line in matrix for module in line if module)
    percent = dark * 100 / (size * size)
    score += 10 * (int(abs(percent - 50)) // 5)
    return score


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def matrix_text(matrix: list[list[bool]], quiet_zone: int = 0) -> str:
    """ASCII rendering of a matrix, used by the tests."""
    size = len(matrix)
    lines: list[str] = []
    for row in range(-quiet_zone, size + quiet_zone):
        chars = []
        for col in range(-quiet_zone, size + quiet_zone):
            inside = 0 <= row < size and 0 <= col < size
            chars.append("##" if inside and matrix[row][col] else "  ")
        lines.append("".join(chars))
    return "\n".join(lines)


def svg(
    data: bytes | str,
    *,
    ec_level: str = "M",
    scale: int = 8,
    quiet_zone: int = 4,
    dark: str = "#101418",
    light: str = "#ffffff",
    title: str = "EverSend",
) -> str:
    """Render ``data`` as a standalone SVG QR code.

    SVG is preferred over a raster image because the phone scales it to its
    own pixel grid: no blurry resampling, no Pillow dependency, and a few
    kilobytes instead of a base64 blob.
    """
    matrix, version, level, mask = encode(data, ec_level=ec_level)
    size = len(matrix)
    side = size + quiet_zone * 2
    pixel = side * scale

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{pixel}" height="{pixel}" viewBox="0 0 {side} {side}" '
            f'shape-rendering="crispEdges" role="img" '
            f'aria-label="{_escape(title)}" '
            f'data-version="{version}" data-ec="{level}" data-mask="{mask}">'
        ),
        f"<title>{_escape(title)}</title>",
        f'<rect width="{side}" height="{side}" fill="{light}"/>',
        f'<g fill="{dark}">',
    ]
    parts.extend(
        f'<rect x="{col + quiet_zone}" y="{row + quiet_zone}" width="1" height="1"/>'
        for row, line in enumerate(matrix)
        for col, module in enumerate(line)
        if module
    )
    parts.append("</g></svg>")
    return "".join(parts)


def png(data: bytes | str, *, ec_level: str = "M", scale: int = 6, quiet_zone: int = 4) -> bytes:
    """Render a 1-bit greyscale PNG (stdlib ``zlib`` only).

    Kept for clients that prefer an image URL over SVG -- notably older
    in-app browsers whose SVG support inside ``<img>`` is unreliable.
    """
    import struct
    import zlib

    matrix, _version, _level, _mask = encode(data, ec_level=ec_level)
    size = len(matrix)
    side = size + quiet_zone * 2
    width = height = side * scale

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        row = bytearray()
        for x in range(width):
            module_row = y // scale - quiet_zone
            module_col = x // scale - quiet_zone
            inside = 0 <= module_row < size and 0 <= module_col < size
            row.append(0 if (inside and matrix[module_row][module_col]) else 255)
        raw.extend(row)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def iter_matrix(data: bytes | str, *, ec_level: str = "M") -> Iterator[list[bool]]:
    """Yield the module rows of ``data`` without a quiet zone."""
    matrix, _version, _level, _mask = encode(data, ec_level=ec_level)
    return iter(matrix)


__all__ = [
    "MAX_VERSION",
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
