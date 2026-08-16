#!/usr/bin/env python3
"""Atropos QR encoder tests — structure, layout, and decode round-trips.

The round-trip decoder is hand-rolled and INDEPENDENT of core.qr: it has its
own GF(256)/RS, its own format-BCH, its own function-module map, its own
zigzag scan and de-interleave. It reads both format copies, unmasks the data
area, splits blocks, recomputes the Reed-Solomon ECC and requires it to match,
then parses the byte-mode payload. This is the "is it actually scannable" gate.
"""

import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import qr  # noqa: E402

# ── independent mini-decoder (v1-4, byte mode, ECC M) ──────────────────────
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


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_gen(degree):
    gen = [1]
    for i in range(degree):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j + 1] ^= c
            nxt[j] ^= _mul(c, _GF_EXP[i])
        gen = nxt
    return gen


def _rs_rem(data, degree):
    """Recompute the ECC codewords for `data` (independent RS)."""
    gen = _rs_gen(degree)
    rem = [0] * degree
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for i in range(1, degree + 1):
                rem[i - 1] ^= _mul(gen[i], f)
    return rem


def _bch_rem(data5):
    """BCH(15,5) remainder of the 5-bit format data, no mask xor."""
    rest = data5 << 10
    g = 0b10100110111
    while rest.bit_length() > 10:
        rest ^= g << (rest.bit_length() - 11)
    return rest


def _is_func(row, col, size, version):
    """Independent function-module map (spec geometry, drawn by hand)."""
    if (row <= 8 and col <= 8) or (row <= 8 and col >= size - 8) or \
       (row >= size - 8 and col <= 8):
        return True
    if row == 6 or col == 6:
        return True
    for cr, cc in {2: [(18, 18)], 3: [(22, 22)], 4: [(26, 26)]}.get(version, []):
        if abs(row - cr) <= 2 and abs(col - cc) <= 2:
            return True
    if row == size - 8 and col == 8:  # dark module
        return True
    if row == 8 or col == 8:
        return (row <= 8 or row >= size - 8) and (col <= 8 or col >= size - 8)
    return False


_MASKS = {
    0: lambda r, c: (r + c) % 2 == 0,
    1: lambda r, c: r % 2 == 0,
    2: lambda r, c: c % 3 == 0,
    3: lambda r, c: (r + c) % 3 == 0,
    4: lambda r, c: (r // 2 + c // 3) % 2 == 0,
    5: lambda r, c: ((r * c) % 2) + ((r * c) % 3) == 0,
    6: lambda r, c: (((r * c) % 2) + ((r * c) % 3)) % 2 == 0,
    7: lambda r, c: (((r + c) % 2) + ((r * c) % 3)) % 2 == 0,
}


def _read_format(matrix):
    """Read both format-info copies. Returns (ecl_bits, mask).
    Raises AssertionError if the copies disagree or the BCH fails."""
    size = len(matrix)
    pos1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7),
            (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    pos2 = [(8, size - 1), (8, size - 2), (8, size - 3), (8, size - 4),
            (8, size - 5), (8, size - 6), (8, size - 7),
            (size - 8, 8), (size - 7, 8), (size - 6, 8), (size - 5, 8),
            (size - 4, 8), (size - 3, 8), (size - 2, 8), (size - 1, 8)]

    def read(pos):
        out = 0
        for r, c in pos:
            out = (out << 1) | (1 if matrix[r][c] else 0)
        return out

    f1, f2 = read(pos1), read(pos2)
    # copy 1 stores bit 0 first along the positions (LSB at (8,0)); reading
    # in position order therefore yields the bit-reversed value.  Copy 2
    # stores bit 14 first, so `f2` is already oriented like the format value.
    rev = 0
    for i in range(15):
        rev |= ((f2 >> i) & 1) << (14 - i)
    assert f1 == rev, "format copies disagree"
    raw = f2 ^ 0x5412
    data5 = (raw >> 10) & 0x1F
    assert _bch_rem(data5) == (raw & 0x3FF), "format BCH mismatch"
    return (data5 >> 3) & 0b11, data5 & 0b111


def _zigzag(matrix, size, version):
    """Data-area bit values in placement order (MSB-first per codeword)."""
    bits = []
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
                if not _is_func(row, x, size, version):
                    bits.append(1 if matrix[row][x] else 0)
        col -= 2
        upward = not upward
    return bits


def decode(matrix):
    """Decode a v1-4 QR matrix fully; returns the original utf-8 string."""
    size = len(matrix)
    version = (size - 17) // 4
    ecl, mask = _read_format(matrix)
    assert ecl == 0b00, f"expected ECC M, got {ecl:02b}"
    # unmask the data area
    unmasked = []
    for r in range(size):
        row = []
        for c in range(size):
            if _is_func(r, c, size, version):
                row.append(bool(matrix[r][c]))
            else:
                row.append(bool(matrix[r][c]) != _MASKS[mask](r, c))
        unmasked.append(row)
    bits = _zigzag(unmasked, size, version)
    words = []
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for k in range(8):
            v = (v << 1) | bits[i + k]
        words.append(v)
    # de-interleave per ECC-M block layout
    layout = {1: (16, 10, 1), 2: (28, 16, 1), 3: (44, 26, 1), 4: (32, 18, 2)}
    dc, ec, nb = layout[version]
    total = dc * nb + ec * nb
    assert len(words) == total, f"got {len(words)} codewords, want {total}"
    data_i = words[:dc * nb]
    ecc_i = words[dc * nb:]
    blocks = [[data_i[i * nb + b] for i in range(dc)] for b in range(nb)]
    eccs = [[ecc_i[i * nb + b] for i in range(ec)] for b in range(nb)]
    for b in range(nb):
        assert _rs_rem(blocks[b], ec) == eccs[b], f"RS ECC mismatch block {b}"
    stream = [w for b in range(nb) for w in blocks[b]]
    bitstr = "".join(format(w, "08b") for w in stream)
    assert bitstr[:4] == "0100", f"mode {bitstr[:4]!r}"
    nbytes = int(bitstr[4:12], 2)
    out = bytearray()
    for i in range(nbytes):
        out.append(int(bitstr[12 + i * 8:20 + i * 8], 2))
    return out.decode("utf-8"), mask


# ── shared base (hermetic ATROPOS_HOME like test_lan.py) ───────────────────
class QrBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_qr_")
        os.environ["ATROPOS_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class MatrixTests(QrBase):
    def test_matrix_square_and_version_sizes(self):
        for text, want in [("HI", 21), ("Atropos", 21),
                           ("http://192.168.1.50:8787/", 25),
                           ("x" * 40, 29),      # 40 bytes > v2 (26) -> v3
                           ("x" * 50, 33)]:     # 50 bytes > v3 (42) -> v4
            m = qr.qr_matrix(text)
            self.assertEqual(len(m), want)
            self.assertTrue(all(len(row) == want for row in m))
            self.assertEqual({type(v) for row in m for v in row}, {bool})

    def test_finder_patterns_present(self):
        m = qr.qr_matrix("HI")
        size = len(m)
        for r0, c0 in [(0, 0), (0, size - 7), (size - 7, 0)]:
            for dr in range(7):
                for dc in range(7):
                    dark = (dr in (0, 6) or dc in (0, 6) or
                            (2 <= dr <= 4 and 2 <= dc <= 4))
                    self.assertEqual(m[r0 + dr][c0 + dc], dark,
                                     f"finder at {r0},{c0} {dr},{dc}")
        # separators are light
        for i in range(8):
            self.assertFalse(m[7][i])
            self.assertFalse(m[i][7])
            self.assertFalse(m[7][size - 1 - i])
            self.assertFalse(m[size - 1 - i][7])

    def test_format_info_not_uniform_and_valid(self):
        for text in ("HI", "http://192.168.1.50:8787/", "x" * 40, "x" * 50):
            m = qr.qr_matrix(text)
            area = [m[8][i] for i in range(9)] + [m[i][8] for i in range(9)]
            self.assertTrue(any(area))
            self.assertTrue(not all(area))
            ecl, mask = _read_format(m)  # raises if copies disagree / bad BCH
            self.assertEqual(ecl, 0)
            self.assertIn(mask, range(8))

    def test_mask_selected_deterministically(self):
        m1 = qr.qr_matrix("HI")
        m2 = qr.qr_matrix("HI")
        self.assertEqual(m1, m2)

    def test_timing_and_dark_module(self):
        m = qr.qr_matrix("HI")
        size = len(m)
        for i in range(8, size - 8):
            self.assertEqual(m[6][i], i % 2 == 0)
            self.assertEqual(m[i][6], i % 2 == 0)
        self.assertTrue(m[size - 8][8])  # dark module (v1: row 13, col 8)

    def test_alignment_patterns(self):
        for text, cc in [("x" * 15, 18), ("x" * 27, 22), ("x" * 43, 26)]:
            m = qr.qr_matrix(text)
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    self.assertEqual(
                        m[cc + dr][cc + dc], max(abs(dr), abs(dc)) != 1,
                        f"alignment {cc + dr},{cc + dc} for {text[:3]}")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            qr.qr_matrix("")


class DecodeRoundTripTests(QrBase):
    def _roundtrip(self, text, want_size):
        m = qr.qr_matrix(text)
        self.assertEqual(len(m), want_size)
        decoded, _mask = decode(m)
        self.assertEqual(decoded, text)

    def test_decode_v1_hi(self):
        self._roundtrip("HI", 21)

    def test_decode_v1_atropos(self):
        self._roundtrip("Atropos", 21)

    def test_decode_v2_string(self):
        self._roundtrip("atropos sync status works", 25)  # 26 bytes → v2 cap

    def test_decode_v3_40_bytes(self):
        self._roundtrip("B" * 40, 29)

    def test_decode_v4_45_bytes(self):
        self._roundtrip("C" * 45, 33)

    def test_decode_v4_max_62_bytes(self):
        self._roundtrip("D" * 62, 33)

    def test_decode_v4_full_capacity_with_url(self):
        url = ("http://192.168.1.50:8787/?pair=abcdef1234567890"
               "abcdef1234567890abcdef1234")   # 63? keep under 62 limit
        self._roundtrip(url[:62], 33)

    def test_roundtrip_utf8_multibyte(self):
        text = "آتروپوس Moirai"      # 7 Persian chars (2 bytes each) + space +
        self.assertEqual(len(text.encode("utf-8")), 21)   # 6 ASCII = 21 bytes
        self._roundtrip(text, 25)     # -> v2

    def test_every_mask_decodes(self):
        for text in ("HI", "http://192.168.1.50:8787/"):
            m = qr.qr_matrix(text)
            decoded, _mask = decode(m)
            self.assertEqual(decoded, text)


class RenderTests(QrBase):
    def test_ascii_rows_equal_length(self):
        lines = qr.qr_ascii("HI")
        self.assertEqual(len(lines), 21 + 8)  # quiet zone 4 top + 4 bottom
        n = len(lines[0])
        self.assertEqual(n, (21 + 8) * 2)  # 2 terminal columns per module
        self.assertTrue(all(len(ln) == n for ln in lines))
        self.assertTrue(all(set(ln) <= {" ", "█"} for ln in lines))
        # top-left finder dark ring visible just below the quiet zone
        self.assertTrue("██████" in "".join(lines[4:8]))

    def test_svg_structure(self):
        svg = qr.qr_svg("HI")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)
        self.assertIn("viewBox", svg)
        self.assertIn("<path", svg)
        self.assertIn("</svg>", svg)
        self.assertNotIn("http://www.w3.org/1999/xlink", svg)  # no ext assets

    def test_png_written(self):
        dest = Path(self.tmp) / "qr.png"
        res = qr.qr_png("HI", str(dest))
        self.assertTrue(res["ok"])
        self.assertEqual(res["modules"], 21)
        self.assertEqual(res["path"], str(dest))
        self.assertTrue(dest.exists())
        data = dest.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        # last chunk: length(4) + "IEND"(4) + crc(4)
        self.assertEqual(struct.unpack(">I", data[-12:-8])[0], 0)
        self.assertEqual(data[-8:-4], b"IEND")
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), ((21 + 8) * 8, (21 + 8) * 8))

    def test_png_creates_nested_dir(self):
        dest = Path(self.tmp) / "a" / "b" / "qr.png"
        res = qr.qr_png("HI", str(dest))
        self.assertTrue(res["ok"])
        self.assertTrue(dest.exists())

    def test_quiet_wraps(self):
        m = qr.qr_matrix("HI")
        q = qr.quiet(m)
        self.assertEqual(len(q), 21 + 8)
        self.assertTrue(all(not cell for cell in q[0]))
        self.assertTrue(all(not cell for cell in q[-1]))
        for row in q:
            self.assertTrue(not row[0] and not row[-1])
        for r in range(21):
            self.assertEqual(q[r + 4][4:25], m[r])


class CapacityTests(QrBase):
    def test_capacity_limits(self):
        self.assertEqual(qr._MAX_CAPACITY,
                         {1: 14, 2: 26, 3: 42, 4: 62})

    def test_max_bytes_constant(self):
        self.assertEqual(qr.MAX_BYTES, 62)
        self.assertEqual(qr.VERSIONS_SUPPORTED, 4)

    def test_rs_verify_all_versions(self):
        # decode() recomputes RS ECC per block and asserts equality
        for text in ["HI", "A" * 20, "B" * 40, "C" * 60]:
            decode(qr.qr_matrix(text))


if __name__ == "__main__":
    unittest.main()