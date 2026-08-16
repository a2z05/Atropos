#!/usr/bin/env python3
"""Pure-stdlib QR Code encoder — real, scannable QR codes.

Versions 1-4, byte mode, ECC level M (25%). Produces QR matrices plus
terminal / SVG / PNG renderings. Only stdlib (zlib, struct, io).
"""

from __future__ import annotations

import binascii
import os
import struct
import zlib

# ── public caps ──────────────────────────────────────────────────────────
VERSIONS_SUPPORTED = 4

# module count per version: v1=21 .. v4=33
_SIZE = {1: 21, 2: 25, 3: 29, 4: 33}

# Byte-mode data capacity per version at ECC level M (ISO 18004 Table 7).
_MAX_CAPACITY = {1: 14, 2: 26, 3: 42, 4: 62}
MAX_BYTES = _MAX_CAPACITY[4]

# Block layout (data codewords, ECC codewords per block, number of blocks).
# ISO 18004 Table 9 (ECC level M):
#   v1: 1 block of (16 data + 10 ecc)  -> 26 codewords
#   v2: 1 block of (28 data + 16 ecc)  -> 44 codewords
#   v3: 1 block of (44 data + 26 ecc)  -> 70 codewords
#   v4: 2 blocks of (32 data + 18 ecc) -> 100 codewords
_LAYOUT = {
    1: (16, 10, 1),
    2: (28, 16, 1),
    3: (44, 26, 1),
    4: (32, 18, 2),
}

# Total codewords per version (data + ecc), incl. across blocks.
_TOTAL_CODEWORDS = {
    1: 26,   # 16 + 10
    2: 44,   # 28 + 16
    3: 70,   # 44 + 26
    4: 100,  # 2 * (32 + 18)
}

# Alignment pattern centre coordinates (ISO 18004 Annex E). v1 has none.
_ALIGNMENT = {
    1: [],
    2: [(18, 18)],
    3: [(22, 22)],
    4: [(26, 26)],
}

# ---- GF(256) Reed-Solomon over primitive polynomial 0x11D ────────────────
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree):
    """Generator polynomial of `degree` (list, highest power first)."""
    gen = [1]
    for i in range(degree):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j + 1] ^= c
            nxt[j] ^= _gf_mul(c, _GF_EXP[i])
        gen = nxt
    return gen


def _rs_remainder(data, gen):
    """ECC codewords for `data` (list of ints) given generator coeffs."""
    gl = len(gen)
    rem = [0] * gl
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for i in range(1, gl):
                rem[i - 1] ^= _gf_mul(gen[i], f)
    return rem


# ---- format / version info ────────────────────────────────────────────────
_FORMAT_MASK = 0x5412  # XOR mask on the BCH(15,5) format value


def _bch_format(data5):
    """BCH(15,5) of the 5-bit format value, xored with the 0x5412 mask."""
    rest = data5 << 10
    g = 0b10100110111  # x^10 + x^8 + x^5 + x^4 + x^2 + x + 1
    for _ in range(15):
        top = rest.bit_length() - 1
        if top < 10:
            break
        rest ^= g << (top - 10)
    return ((data5 << 10) | rest) ^ _FORMAT_MASK


def _format_bits(mask):
    """15-bit format info for ECC M (level indicator 00) + `mask`."""
    return _bch_format((0b00 << 3) | mask)


# ---- module placement ─────────────────────────────────────────────────────
def _version_for_size(size):
    for v, s in _SIZE.items():
        if s == size:
            return v
    return None


def _is_function(row, col, size):
    """True for finders, separators, timing, alignment, dark module, format."""
    v = _version_for_size(size)
    # finder patterns + separators (9x9 corner blocks)
    if (row <= 8 and col <= 8) or (row <= 8 and col >= size - 8) or \
       (row >= size - 8 and col <= 8):
        return True
    # timing patterns
    if row == 6 or col == 6:
        return True
    # alignment patterns
    for cr, cc in _ALIGNMENT.get(v, []):
        if abs(row - cr) <= 2 and abs(col - cc) <= 2:
            return True
    # dark module: row = size - 8, col 8
    if row == size - 8 and col == 8:
        return True
    # format info areas: row 8 / col 8 spans inside the corner blocks
    if row == 8 or col == 8:
        return (row <= 8 or row >= size - 8) and (col <= 8 or col >= size - 8)
    return False


def _module_coords(size, version):
    """(row, col) of every data bit position in standard fill order, in the
    exact order the bits must be laid down: column pairs are scanned
    right-to-left; within a pair the two columns are consumed row by row
    together, and the vertical direction flips per pair."""
    coords = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
            continue
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for j in range(2):
                x = col - j
                if not _is_function(row, x, size):
                    coords.append((row, x))
        col -= 2
        upward = not upward
    return coords


# ---- data bitstream ────────────────────────────────────────────────────────
def _char_count_bits(version):
    return 8  # byte mode, versions 1-9


def _encode_bytes(text: str):
    data = text.encode("utf-8")
    version = _min_version(len(data))
    cc = _char_count_bits(version)
    bits = "0100" + format(len(data), f"0{cc}b")
    for b in data:
        bits += format(b, "08b")
    capacity_bits = _TOTAL_CODEWORDS[version] * 8
    # terminator (up to 4 zero bits)
    bits += "0" * min(4, capacity_bits - len(bits))
    # pad to byte boundary
    if len(bits) % 8:
        bits += "0" * (8 - len(bits) % 8)
    # pad codewords 0xEC / 0x11 alternating
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < capacity_bits:
        bits += format(pad[i % 2], "08b")
        i += 1
    return bits, version


def _min_version(nbytes: int) -> int:
    for v in range(1, VERSIONS_SUPPORTED + 1):
        if nbytes <= _MAX_CAPACITY[v]:
            return v
    raise ValueError(f"text too long for QR version 1-4 ({nbytes} bytes > "
                     f"{MAX_BYTES})")


def _split_blocks(data: list, version: int):
    """Split the data codewords into the version's interleave blocks."""
    dc, ec, blocks = _LAYOUT[version]
    return [data[b * dc:(b + 1) * dc] for b in range(blocks)]


def _interleave(blocks, ec_degree):
    """Interleave data codewords across blocks, then ecc codewords."""
    interleaved = []
    max_len = max(len(b) for b in blocks)
    for i in range(max_len):
        for b in blocks:
            if i < len(b):
                interleaved.append(b[i])
    ecc_blocks = []
    gen = _rs_generator(ec_degree)
    for b in blocks:
        ecc_blocks.append(_rs_remainder(b, gen))
    for i in range(ec_degree):
        for eb in ecc_blocks:
            interleaved.append(eb[i])
    return interleaved


# ---- masking + penalty ─────────────────────────────────────────────────────
_N1, _N2, _N3, _N4 = 3, 3, 40, 10


def _apply_mask(matrix, mask):
    size = len(matrix)
    out = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if _is_function(r, c, size):
                continue
            invert = False
            if mask == 0:
                invert = (r + c) % 2 == 0
            elif mask == 1:
                invert = r % 2 == 0
            elif mask == 2:
                invert = c % 3 == 0
            elif mask == 3:
                invert = (r + c) % 3 == 0
            elif mask == 4:
                invert = (r // 2 + c // 3) % 2 == 0
            elif mask == 5:
                invert = ((r * c) % 2) + ((r * c) % 3) == 0
            elif mask == 6:
                invert = (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
            elif mask == 7:
                invert = (((r + c) % 2) + ((r * c) % 3)) % 2 == 0
            if invert:
                out[r][c] = not out[r][c]
    return out


def _is_finder_pattern(seven):
    """Match 1:1:3:1:1 dark·light·dark·light·dark."""
    return seven == [True, False, True, True, True, False, True]


def _penalty(matrix):
    size = len(matrix)
    score = 0

    def _run_penalties(line):
        """Yield the N1 penalty for each run of >= 5 identical modules."""
        runs = 1
        prev = line[0]
        for v in line[1:]:
            if v == prev:
                runs += 1
            else:
                if runs >= 5:
                    yield 3 + (runs - 5)
                runs = 1
                prev = v
        if runs >= 5:
            yield 3 + (runs - 5)

    # N1: runs of same colour, 5 or longer, in rows and columns
    for r in range(size):
        for s in _run_penalties(matrix[r]):
            score += s * _N1
    for c in range(size):
        col = [matrix[r][c] for r in range(size)]
        for s in _run_penalties(col):
            score += s * _N1
    # N2: 2x2 blocks of the same colour
    for r in range(size - 1):
        for c in range(size - 1):
            if matrix[r][c] == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += _N2
    # N3: finder-like 1:1:3:1:1 patterns with 4 light modules on either side
    for r in range(size):
        for c in range(size - 6):
            if _is_finder_pattern(matrix[r][c:c + 7]):
                if c >= 4 and not any(matrix[r][c - 4:c]):
                    score += _N3
                if c + 11 <= size and not any(matrix[r][c + 7:c + 11]):
                    score += _N3
    for c in range(size):
        for r in range(size - 6):
            col = [matrix[x][c] for x in range(r, r + 7)]
            if _is_finder_pattern(col):
                if r >= 4 and not any(matrix[x][c] for x in range(r - 4, r)):
                    score += _N3
                if r + 11 <= size and not any(matrix[x][c] for x in range(r + 7, r + 11)):
                    score += _N3
    # N4: proportion of dark modules deviating from 50%
    dark = sum(row.count(True) for row in matrix)
    percent = dark * 100 // (size * size)
    k = abs(percent - 50) // 5
    score += k * _N4
    return score


# ---- public API ────────────────────────────────────────────────────────────
def quiet(matrix):
    """Wrap a matrix in the 4-module quiet zone."""
    n = 4
    size = len(matrix)
    blank = [False] * (size + 2 * n)
    out = [blank[:] for _ in range(n)]
    for row in matrix:
        out.append([False] * n + row + [False] * n)
    out.extend(blank[:] for _ in range(n))
    return out


def qr_matrix(text: str) -> list:
    """Build a QR symbol (True = dark) for `text`, v1-4 byte mode ECC M."""
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    bits, version = _encode_bytes(text)
    size = _SIZE[version]

    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    dc, ec, nblocks = _LAYOUT[version]
    blocks = _split_blocks(data[:dc * nblocks], version)
    codewords = _interleave(blocks, ec)

    # build the base matrix with function patterns
    m = [[False] * size for _ in range(size)]

    def _finder(r0, c0):
        for r in range(7):
            for c in range(7):
                m[r0 + r][c0 + c] = (r in (0, 6) or c in (0, 6) or
                                     (2 <= r <= 4 and 2 <= c <= 4))

    _finder(0, 0)
    _finder(0, size - 7)
    _finder(size - 7, 0)
    # separators (one-module light ring around each finder)
    for i in range(8):
        m[7][i] = False
        m[i][7] = False
        m[7][size - 1 - i] = False
        m[size - 1 - i][7] = False
        m[size - 8][i] = False
        m[i][size - 8] = False
    # timing
    for i in range(8, size - 8):
        m[6][i] = (i % 2 == 0)
        m[i][6] = (i % 2 == 0)
    # alignment
    for cr, cc in _ALIGNMENT.get(version, []):
        for r in range(-2, 3):
            for c in range(-2, 3):
                m[cr + r][cc + c] = (max(abs(r), abs(c)) != 1)
    # dark module (row size-8, col 8)
    m[size - 8][8] = True

    # place data bits (MSB-first per codeword), in standard zigzag order
    coords = _module_coords(size, version)
    flat = []
    for b in codewords:
        flat.extend((b >> (7 - i)) & 1 for i in range(8))
    for idx, (r, c) in enumerate(coords):
        if idx < len(flat):
            m[r][c] = bool(flat[idx])

    # choose the best mask and re-draw format info for it
    best_mask = 0
    best_m = None
    best_score = None
    for mask in range(8):
        candidate = _apply_mask(m, mask)
        _place_format(candidate, _format_bits(mask), size)
        s = _penalty(candidate)
        if best_score is None or s < best_score:
            best_score = s
            best_mask = mask
            best_m = candidate
    _place_format(best_m, _format_bits(best_mask), size)
    return best_m


def _place_format(matrix, fmt15, size):
    """Place the 15 format bits (bit 0 = LSB) into the two format areas."""
    bits = [(fmt15 >> i) & 1 for i in range(15)]
    # copy 1: around the top-left finder
    pos = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7),
        (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    # copy 2: top-right (row 8) + bottom-left (col 8), mirrored order
    pos2 = [
        (8, size - 1), (8, size - 2), (8, size - 3), (8, size - 4),
        (8, size - 5), (8, size - 6), (8, size - 7),
        (size - 8, 8), (size - 7, 8), (size - 6, 8), (size - 5, 8),
        (size - 4, 8), (size - 3, 8), (size - 2, 8), (size - 1, 8),
    ]
    for i, (r, c) in enumerate(pos):
        matrix[r][c] = bool(bits[i])
    for i, (r, c) in enumerate(pos2):
        matrix[r][c] = bool(bits[14 - i])


def qr_ascii(text: str, light="  ", dark="██") -> list:
    """Render as text lines: every module occupies two terminal columns
    (dark = `██`, light = two spaces) so the aspect looks right, wrapped
    in a 4-module quiet zone. All lines have equal length."""
    matrix = qr_matrix(text)
    q = quiet(matrix)
    return ["".join(dark if cell else light for cell in row) for row in q]


def qr_svg(text: str, scale=4) -> str:
    """Inline SVG markup for the QR, viewBox covers modules + quiet zone."""
    matrix = qr_matrix(text)
    q = quiet(matrix)
    size = len(q)
    dim = size * scale
    dark = []
    for r, row in enumerate(q):
        for c, cell in enumerate(row):
            if cell:
                dark.append(f"M{c * scale} {r * scale}h{scale}v{scale}h-{scale}z")
    path = "".join(dark) or "M0 0h0z"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
            f'width="{dim}" height="{dim}" shape-rendering="crispEdges">'
            f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')


def qr_png(text: str, path) -> dict:
    """Write a PNG (RGBA, module scale 8px, quiet zone) for `text`.
    Returns {"ok": True, "path", "modules", "size"}."""
    try:
        matrix = qr_matrix(text)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    q = quiet(matrix)
    size = len(q)
    scale = 8
    dim = size * scale
    raw = bytearray()
    for y in range(dim):
        raw.append(0)  # filter type 0 per scanline
        for x in range(dim):
            if q[y // scale][x // scale]:
                raw += bytes((0, 0, 0, 255))
            else:
                raw += bytes((255, 255, 255, 255))

    def _chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        c += struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        return c

    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return {"ok": True, "path": str(path), "modules": len(matrix),
            "size": len(png)}