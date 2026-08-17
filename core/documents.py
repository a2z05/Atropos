"""Office document read/create/edit — DOCX, XLSX, PPTX (ZIP+XML) and PDF text.

Ported from hermes-agent/tools/read_extract.py (docx/xlsx/ipynb extraction,
including the cell-type semantics and row/col caps), with PPTX extraction
following the same ZIP+XML walk over the presentation namespace, and PDF
text extraction over the standard content-stream operators (no external PDF
library). Generators for the ZIP-based formats produce real files that the
extractors round-trip. Every entry point degrades to {'ok': ..., 'error':
...} instead of raising. Since the Feishu path (feishu_doc_tool.py) needs
the lark_oapi SDK, it is represented here by the local-file path only.

Doc formats handled: docx (Word), xlsx (Excel), pptx (PowerPoint), pdf,
ipynb (Jupyter — read-only, from read_extract.py).
"""
from __future__ import annotations

import json
import posixpath
import re
import zlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = [
    "FORMAT_EXTS", "EXTRACTABLE_EXTENSIONS", "ExtractionError",
    "is_extractable_document", "extract_document_text",
    "documents_read", "documents_create", "documents_edit",
    "documents_manifest", "format_for_path",
]

# Extension -> format. ".pdf" is deliberately excluded from the binary list
# in hermes-agent/tools/binary_extensions.py ("text-based, agents may want to
# inspect") — mirrored here by claiming it in FORMAT_EXTS as readable text.
FORMAT_EXTS = {
    ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".pdf": "pdf", ".ipynb": "ipynb",
}

# The formats read_extract.py can render to text (its read path).
EXTRACTABLE_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx"})

MAX_XLSX_BYTES = 50 * 1024 * 1024   # read_extract.MAX_XLSX_BYTES
_MAX_XLSX_ROWS_PER_SHEET = 5000     # read_extract cap (rows per sheet)
_MAX_XLSX_COLS = 256                # read_extract cap (cols per sheet)

# Cap returned text so a pathological document cannot blow the context
# window. read_extract.py has no cap (its callers truncate); Atropos entry
# points apply the cap centrally on the way out.
DEFAULT_MAX_TEXT_CHARS = 1_000_000

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_REL = _NS_R  # read_extract.py names this constant _NS_REL
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_WORD_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_SHEET_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_PPT_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
_WORD_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_SHEET_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
_PPT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"

_PDF_SIGNATURE = re.compile(rb"^%PDF-\d\.\d")


class ExtractionError(Exception):
    """Raised when a supported-looking document cannot be rendered as text."""


def format_for_path(path: str) -> str:
    """Return the doc format for a path (lowercased extension), or ''."""
    return FORMAT_EXTS.get(Path(path).suffix.lower(), "")


def is_extractable_document(path: str) -> bool:
    return Path(path).suffix.lower() in EXTRACTABLE_EXTENSIONS


# ── extraction core (faithful to read_extract.py) ─────────────────────────

def _extension(path: str) -> str:
    ext = Path(path).suffix.lower()
    return ext if ext in EXTRACTABLE_EXTENSIONS else ""


def extract_document_text(path: str) -> str:
    """Render a supported document to plain text. Raises ExtractionError."""
    ext = _extension(path)
    if ext == ".ipynb":
        return _extract_notebook(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".xlsx":
        return _extract_xlsx(path)
    raise ExtractionError(f"Unsupported document type: {path!r}")


def _source_text(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(item for item in source if isinstance(item, str))
    return ""


def _extract_notebook(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nb = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"Not a valid notebook: {exc}") from exc
    if not isinstance(nb, dict):
        raise ExtractionError("Notebook root is not an object")

    cells = nb.get("cells")
    if not isinstance(cells, list):
        cells = [
            cell
            for ws in nb.get("worksheets", [])
            if isinstance(ws, dict)
            for cell in ws.get("cells", [])
        ]
    if not cells:
        raise ExtractionError("Notebook contains no cells")

    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}
    out: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        typ = cell.get("cell_type")
        if typ not in labels:
            continue
        counts[typ] += 1
        suffix = f" {counts[typ]}" if typ != "raw" else ""
        out.extend((f"# ── {labels[typ]} cell{suffix} ──",
                    _source_text(cell.get("source", "")).rstrip("\n"), ""))
    if not out:
        raise ExtractionError("Notebook contains no readable cells")
    return "\n".join(out).rstrip("\n") + "\n"


def _zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(zf.read(name))
    except KeyError as exc:
        raise ExtractionError(f"Missing {name}") from exc
    except ET.ParseError as exc:
        raise ExtractionError(f"Malformed XML in {name}: {exc}") from exc


def _extract_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            root = _zip_xml(zf, "word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid DOCX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    w = f"{{{_NS_W}}}"
    lines: list[str] = []
    for para in root.iter(f"{w}p"):
        buf: list[str] = []
        for node in para.iter():
            if node.tag == f"{w}t":
                buf.append(node.text or "")
            elif node.tag == f"{w}tab":
                buf.append("\t")
            elif node.tag in {f"{w}br", f"{w}cr"}:
                buf.append("\n")
        lines.extend("".join(buf).split("\n"))
    if not any(line.strip() for line in lines):
        raise ExtractionError("DOCX contains no extractable text")
    return "\n".join(lines).rstrip("\n") + "\n"


def _extract_xlsx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            shared = _shared_strings(zf, names)
            sheets = _workbook_sheets(zf)
            rels = _workbook_rels(zf, names)
            out: list[str] = []
            for name, state, rid in sheets:
                if state in {"hidden", "veryHidden"}:
                    continue
                part = _sheet_part(rels.get(rid, ""))
                if part not in names:
                    continue
                try:
                    rows = _sheet_rows(zf.read(part), shared)
                except ET.ParseError:
                    continue
                out.append(f"# ── Sheet: {name} ──")
                out.extend("\t".join(row) for row in rows)
                if not rows:
                    out.append("(empty)")
                out.append("")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid XLSX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    if not out:
        raise ExtractionError("XLSX has no visible sheets with content")
    return "\n".join(out).rstrip("\n") + "\n"


def _shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    s = f"{{{_NS_S}}}"
    return ["".join(t.text or "" for t in item.iter(f"{s}t")) for item in root.iter(f"{s}si")]


def _workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str, str]]:
    root = _zip_xml(zf, "xl/workbook.xml")
    s, r = f"{{{_NS_S}}}", f"{{{_NS_REL}}}"
    return [
        (sheet.get("name", "Sheet"), sheet.get("state", "visible"), sheet.get(f"{r}id", ""))
        for sheet in root.iter(f"{s}sheet")
    ]


def _workbook_rels(zf: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    try:
        root = ET.fromstring(zf.read(rels_path))
    except ET.ParseError:
        return {}
    rel_tag = f"{{{_NS_PKG_REL}}}Relationship"
    return {rel.get("Id", ""): rel.get("Target", "") for rel in root.iter(rel_tag) if rel.get("Id")}


def _sheet_part(target: str) -> str:
    target = target.lstrip("/")
    return posixpath.normpath(target if target.startswith("xl/") else f"xl/{target}")


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return max(idx - 1, 0)


def _sheet_rows(xml_bytes: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml_bytes)
    s = f"{{{_NS_S}}}"
    rows: list[list[str]] = []
    for row in root.iter(f"{s}row"):
        if len(rows) >= _MAX_XLSX_ROWS_PER_SHEET:
            break
        cells: dict[int, str] = {}
        max_col = -1
        for cell in row.iter(f"{s}c"):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col >= _MAX_XLSX_COLS:
                continue
            cells[col] = _cell_value(cell, shared, s)
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)] if max_col >= 0 else [])
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    return rows


def _cell_value(cell: ET.Element, shared: list[str], s: str) -> str:
    value = cell.findtext(f"{s}v") or ""
    typ = cell.get("t", "")
    if typ == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if typ == "inlineStr":
        inline = cell.find(f"{s}is")
        return "" if inline is None else "".join(t.text or "" for t in inline.iter(f"{s}t"))
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    if typ == "e":
        return value or "#ERROR"
    return value


# ── pptx extraction (ZIP+XML walk, presentation namespace) ────────────────

def _extract_pptx(path: str) -> str:
    """Render a PPTX to text: slide title first, then per-slide body runs.

    Slide text lives in slideN.xml; every ``<a:t>`` under a shape is a text
    run. The first non-empty shape of the slide is conventionally the title
    (pptx schema orders it first) — rendered as a heading line, mirroring
    how read_extract.py renders xlsx sheet names.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            rels: dict[str, str] = {}
            if "ppt/_rels/presentation.xml.rels" in names:
                try:
                    root = ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))
                    rel_tag = f"{{{_NS_PKG_REL}}}Relationship"
                    rels = {rel.get("Id", ""): rel.get("Target", "")
                            for rel in root.iter(rel_tag) if rel.get("Id")}
                except ET.ParseError:
                    rels = {}
            try:
                pres = _zip_xml(zf, "ppt/presentation.xml")
            except ExtractionError:
                pres = None
            order: list[str] = []
            seen: set[str] = set()
            if pres is not None:
                for sld in pres.iter(f"{{{_NS_P}}}sldId"):
                    rid = sld.get(f"{{{_NS_R}}}id", "")
                    target = rels.get(rid, "").lstrip("/")
                    if not target.startswith("ppt/"):
                        target = f"ppt/{target}"
                    part = posixpath.normpath(target)
                    if part not in names or part in seen:
                        continue
                    seen.add(part)
                    order.append(part)
            # Fallback: slideN.xml files not listed in presentation.xml
            # (malformed or minimal decks) are appended in numeric order.
            for slide in sorted(names):
                if re.match(r"^ppt/slides/slide\d+\.xml$", slide) and slide not in seen:
                    seen.add(slide)
                    order.append(slide)
            lines: list[str] = []
            for part in order:
                try:
                    lines.extend(_pptx_slide_lines(zf.read(part)))
                except (ET.ParseError, KeyError):
                    continue
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid PPTX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    if not lines:
        raise ExtractionError("PPTX contains no extractable text")
    return "\n".join(lines).rstrip("\n") + "\n"


def _pptx_slide_lines(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    a = f"{{{_NS_A}}}"
    out: list[str] = []
    first = True
    for sp in root.iter(f"{{{_NS_P}}}sp"):
        runs: list[str] = []
        for t in sp.iter(f"{a}t"):
            runs.append(t.text or "")
        text = "".join(runs).strip()
        if not text:
            continue
        if first:
            out.append(f"# ── Slide: {text} ──")
            first = False
        else:
            out.append(text)
    return out


# ── PDF text extraction (uncompressed + FlateDecode streams only) ─────────

def _extract_pdf(path: str) -> str:
    """Extract text from a PDF via its content streams.

    Locates the xref table from ``startxref``, resolves indirect object
    offsets, reads every ``N 0 obj ... endobj`` whose stream survives, and
    decodes FlateDecode (zlib) streams. Pages whose xref entries are missing
    are skipped silently; the text of each object is collected with
    :func:`_pdf_operator_text`. Pure text extraction — no page layout
    reconstruction, matching the scope of the docs entry point.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if not _PDF_SIGNATURE.match(data[:1024]):
        raise ExtractionError("Not a valid PDF (missing %PDF header)")
    offsets, scan = _pdf_locate_xref(data)
    if offsets is None or scan is None:
        raise ExtractionError("PDF has no usable xref table")

    out: list[str] = []
    seen: set[int] = set()
    for num in offsets:
        if num in seen:
            continue
        seen.add(num)
        try:
            obj = _pdf_parse_object(data, offsets[num])
        except (ValueError, IndexError):
            continue
        if obj is not None:
            text = _pdf_operator_text(obj.get("stream"))
            if text:
                out.append(text)
    if not out:
        raise ExtractionError("PDF contains no extractable text streams")
    return "\n".join(out).rstrip("\n") + "\n"


def _pdf_locate_xref(data: bytes):
    """Return (offsets, scan_start) for the file's last xref table.

    ``offsets`` maps object numbers to absolute byte offsets; ``scan_start``
    is where the xref table begins (used by callers that fall back to a
    trailer-object scan). Every entry is bounds-checked against the file
    size so a corrupt xref cannot crash the parse.
    """
    tail = data[-2048:]
    m = re.search(rb"startxref\r?\n\s*(\d+)", tail)
    if not m:
        return None, None
    start = int(m.group(1))
    if start < 0 or start >= len(data):
        return None, None

    first = data.find(b"xref", start - 32, start + 32)
    if first == -1:
        return None, start

    # ISO 32000-1 §7.5.4: after the ``xref`` keyword come subsections
    # "N COUNT" (object number N of the first entry in the subsection,
    # COUNT entries following), each entry being
    # "NNNNNNNNNN GGGGG n|f" (10-digit byte offset, 5-digit generation,
    # in-use/free flag). ``trailer`` follows after a blank line.
    offsets: dict[int, int] = {}
    pointer = first + len(b"xref")
    trailer_at = data.find(b"trailer", pointer)
    if trailer_at == -1:
        trailer_at = len(data)
    block_end = trailer_at
    next_num = -1
    expected = 0
    while pointer < block_end:
        line_end = data.find(b"\n", pointer)
        if line_end == -1 or line_end > block_end:
            line_end = block_end
        line = data[pointer:line_end].strip()
        pointer = line_end + 1
        if not line:
            continue
        if expected > 0:
            # xref entry line: offset, generation, flag
            m2 = re.match(rb"(\d+)\s+(\d+)\s+(n|f)\b", line)
            if m2:
                off = int(m2.group(1))
                if m2.group(3) == b"n" and off >= 0 and off < len(data):
                    offsets[next_num] = off
                next_num += 1
                expected -= 1
            continue
        m3 = re.match(rb"(\d+)\s+(\d+)\b", line)
        if m3:
            # subsection header: first object number + count
            next_num = int(m3.group(1))
            expected = int(m3.group(2))
    return offsets, start


def _skip_ws(data: bytes, i: int) -> int:
    while i < len(data) and data[i] in b"\x00\t\n\f\r ":
        i += 1
    return i


def _scan_dict_body(body: bytes):
    """Return (start, end) indices of a leading ``<< ... >>`` dict, or None."""
    if not body.startswith(b"<<"):
        return None
    depth = 0
    i = 0
    while i < len(body):
        if body[i:i + 2] == b"<<":
            depth += 1
            i += 2
            continue
        if body[i:i + 2] == b">>":
            depth -= 1
            i += 2
            if depth <= 0:
                return (2, i)
            continue
        i += 1
    return None


def _pdf_stream_length(d: dict, body: bytes, stream_pos: int, data: bytes,
                       pos: int) -> int:
    """Resolve a stream's /Length: literal int > the dict, else scan.

    The dict body barely ends before ``stream``; the /Length entry may be an
    indirect reference (ignored here) or a literal. When neither is usable,
    scan forward for ``endstream`` — correct for well-formed files where the
    stream is followed immediately by that keyword.
    """
    length = d.get("Length")
    if isinstance(length, int) and length >= 0:
        return length
    try:
        m = re.search(rb"/Length\s+(\d+)", body)
        if m:
            return int(m.group(1))
    except (TypeError, ValueError):
        pass
    end_marker = data.find(b"endstream", stream_pos, stream_pos + 4 * 1024 * 1024)
    if end_marker == -1:
        return 0
    # strip the trailing EOL that belongs to the marker
    j = end_marker
    if j > stream_pos and data[j - 1] in b"\r\n":
        j -= 1
        if j > stream_pos and data[j - 1] in b"\r\n":
            j -= 1
    return max(0, j - stream_pos)


def _pdf_decode_stream(raw: bytes, d: dict) -> bytes:
    """Apply FlateDecode when declared; otherwise return the raw stream.

    /Filter carries a name or an array of names. Each filter we cannot apply
    renders the stream undecodable as a whole (graceful: the object yields
    no text rather than crashing the read).
    """
    f = d.get("Filter")
    if isinstance(f, bytes):
        try:
            return zlib.decompress(raw) if f == b"FlateDecode" else raw
        except zlib.error:
            return raw
    if isinstance(f, list):
        if f == [b"FlateDecode"]:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return raw
        return raw  # multi-stage filters unsupported — keep raw text
    return raw


def _unescape_pdf_string(s: str) -> str:
    """Resolve PDF literal-string escapes (\\n \\r \\t \\b \\f \\ddd \\\\) and
    line continuations (backslash + EOL) per ISO 32000-1 §7.3.4.2."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break
        nxt = s[i + 1]
        if nxt in "\r\n":
            i += 2
            if nxt == "\r" and i < n and s[i] == "\n":
                i += 1
            continue
        if nxt.isdigit():
            digits = s[i + 1:i + 4]
            code = 0
            for d in digits[:3]:
                code = code * 8 + (ord(d) - 48)
            out.append(chr(code))
            i += 1 + len(digits[:3])
            continue
        i += 2
        if nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "b":
            out.append("\b")
        elif nxt == "f":
            out.append("\f")
        else:
            out.append(nxt)  # \\, \(, \), others → literal char
    return "".join(out)


def _pdf_stream_text(stream: bytes) -> str:
    """Collect text-showing operands from a decoded content stream.

    Handles ``Tj``, ``TJ`` (arrays), ``'`` and ``"`` (next-line show) with
    parenthesised literal strings; the stream is decoded latin-1 so byte
    offsets stay stable. Whitespace between runs is collapsed. This is the
    standard minimal extraction set — enough for plain-text PDFs.
    """
    try:
        text = stream.decode("latin-1")
    except (UnicodeDecodeError, AttributeError):
        return ""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "(":
            i += 1
            continue
        j = i + 1
        buf: list[str] = []
        depth = 1
        while j < n and depth:
            ch = text[j]
            if ch == "\\":
                buf.append(ch)
                buf.append(text[j + 1] if j + 1 < n else "")
                j += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    buf.append(ch)
                    j += 1
                    break
            buf.append(ch)
            j += 1
        literal = "".join(buf)
        if literal.endswith(")") and literal.startswith("\\(") is False:
            literal = literal[:-1]
        k = j
        while k < n and text[k] in " \t\r\n":
            k += 1
        if text[k:k + 2] in ("Tj", "TJ") and literal:
            literal = literal[1:] if literal.startswith("(") else literal
            out.append(_unescape_pdf_string(literal))
            i = k + 2
            continue
        if text[k:k + 1] in ("'", '"') and literal:
            literal = literal[1:] if literal.startswith("(") else literal
            out.append(_unescape_pdf_string(literal))
            i = k + 1
            continue
        i = j
    text_runs = [part for part in out if part]
    if not text_runs:
        return ""
    return "\n".join(text_runs)


def _pdf_parse_object(data: bytes, off: int) -> dict | None:
    """Parse one ``N 0 obj ... endobj`` at byte offset ``off``.

    Returns {'num': N, 'dict': {...}, 'stream': bytes|None} or None for
    free-object entries (which have no body).
    """
    off = _skip_ws(data, off)
    m = re.match(rb"(\d+)\s+(\d+)\s+obj\b", data[off:off + 32])
    if not m:
        return None
    num = int(m.group(1))
    pos = off + m.end()
    pos = _skip_ws(data, pos)  # writers put a EOL after ``obj``; the body
    # scan expects the ``<<`` to be the very first bytes
    end = data.find(b"endobj", pos)
    if end == -1:
        end = len(data)
    body = data[pos:end]

    dm = _scan_dict_body(body)
    d: dict = {}
    stream_pos = None
    if dm is not None:
        d = _pdf_dict_parse(body[dm[0]:dm[1]].strip(b"<> "))
        sm = re.search(rb"stream\r?\n", body[dm[1]:dm[1] + 64])
        if sm:
            stream_pos = pos + dm[1] + sm.end()
    stream = None
    if stream_pos is not None and stream_pos < len(data):
        length = _pdf_stream_length(d, body, stream_pos, data, pos)
        raw = data[stream_pos:stream_pos + length]
        stream = _pdf_decode_stream(raw, d)
    return {"num": num, "dict": d, "stream": stream}


def _pdf_dict_parse(inner: bytes) -> dict:
    """Parse the inside of a ``<< ... >>`` dict into a plain dict.

    Values: names (bytes), integers, booleans, arrays (lists), and the
    literal string type only when it immediately follows ``/Length``.
    Nested dicts are skipped (returned as None) — only /Length and /Filter
    drive stream decoding.
    """
    d: dict = {}
    tokens = re.findall(
        rb"/([\w.]+)|(\d+)|(true|false)|(\[)|(\])|\(((?:\\.|[^\\()])*)\)|(<<)|(>>)",
        inner,
    )
    key = None
    for m in tokens:
        name, num, boolean, lb, rb, lit, dlb, drb = m
        if name:
            if key is None:
                key = name.decode("latin-1")
            else:
                # Name directly after a name: a bare flag key (e.g.
                # ``/Filter /FlateDecode``) — the previous key resolves to
                # the name itself, which the stream decoder compares
                # against b"FlateDecode".
                d[key] = name
                key = None
            continue
        if key is None:
            continue
        if num:
            d[key] = int(num)
            key = None
        elif boolean:
            d[key] = boolean == b"true"
            key = None
        elif lit:
            d[key] = lit
            key = None
        elif lb:
            d[key] = []
            key = None
        elif dlb:
            key = None
        # "]"/">>"/names inside arrays are ignored for our two keys
    if key is not None:
        # Trailing bare flag (``... /Filter /FlateDecode >>`` ends with a
        # name, not a value): resolve it to True so the decoder can use it.
        d[key] = True
    return d


def _pdf_operator_text(stream) -> str:
    if not stream:
        return ""
    return _pdf_stream_text(stream)


# ── entry points (graceful {ok, ...} envelopes) ───────────────────────────

def _cap_text(text: str, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text


def documents_read(path: str, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> dict:
    """Read a supported document and return its text.

    Format dispatch mirrors read_extract.py (docx/xlsx/ipynb) plus the pdf
    and pptx extractors above. Returns {'ok': True, 'text': ..., 'format':
    ...} or {'ok': False, 'error': ...}. ``max_chars=0`` disables the cap.
    """
    fmt = format_for_path(path)
    if not fmt:
        return {"ok": False, "error": f"unsupported document format: {path!r} "
                                      "(supported: docx, xlsx, pptx, pdf, ipynb)"}
    try:
        if fmt == "pdf":
            text = _extract_pdf(path)
        elif fmt == "pptx":
            text = _extract_pptx(path)
        else:
            text = extract_document_text(path)
    except ExtractionError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if max_chars:
        text = _cap_text(text, max_chars)
    return {"ok": True, "text": text, "format": fmt}


def documents_create(format: str, path: str, content) -> dict:
    """Create a zip-based office document. content: str (docx/pptx) or a
    rows-table (xlsx: list[list[str]] or list[dict] keyed by 'cells')."""
    fmt = (format or "").lower().strip(".")
    if fmt not in {"docx", "xlsx", "pptx"}:
        return {"ok": False, "error": f"cannot create format {format!r} "
                                      "(docx, xlsx, pptx only)"}
    try:
        if fmt == "docx":
            _write_docx(path, content if isinstance(content, str) else str(content))
        elif fmt == "pptx":
            _write_pptx(path, content if isinstance(content, str) else str(content))
        else:
            _write_xlsx(path, _normalize_rows(content))
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "format": fmt, "path": str(Path(path).resolve())}


def _normalize_rows(content) -> list[list]:
    """Coerce xlsx content (rows of cells) into a list-of-lists."""
    rows: list = []
    if isinstance(content, str):
        rows = [[cell.strip() for cell in line.split("\t")] for line in content.splitlines() if line.strip()]
    elif isinstance(content, (list, tuple)):
        for row in content:
            if isinstance(row, dict):
                rows.append([str(row.get("cells", []))])
            elif isinstance(row, (list, tuple)):
                rows.append([str(c) for c in row])
            else:
                rows.append([str(row)])
    return rows


def documents_edit(path: str, changes: dict) -> dict:
    """Apply a small supported change set to a zip-based document.

    docx changes: {'replace': [{'old': str, 'new': str}, ...]} — walk text
    nodes. xlsx/pptx changes: currently unsupported and reported (graceful
    degradation; editing sheets/slides in place is a large surface that the
    Hermes sources do not cover either — their file tools only read).
    """
    fmt = format_for_path(path)
    if fmt not in {"docx", "xlsx", "pptx"}:
        return {"ok": False, "error": f"edit unsupported for format {fmt!r}"}
    if not Path(path).exists():
        return {"ok": False, "error": f"file not found: {path}"}
    if fmt == "docx":
        return _edit_docx(path, changes)
    if fmt == "xlsx":
        return _edit_xlsx(path, changes)
    return _edit_pptx(path, changes)


def _edit_xlsx(path: str, changes: dict) -> dict:
    if changes and any(changes.get(k) for k in ("replace", "delete", "append")):
        return {"ok": False, "error": "xlsx edit not implemented — read/create "
                                      "only (parity with Hermes file tools)"}
    return {"ok": True, "changed": 0, "note": "no-op: no supported changes"}


def _edit_pptx(path: str, changes: dict) -> dict:
    if changes and any(changes.get(k) for k in ("replace", "delete", "append")):
        return {"ok": False, "error": "pptx edit not implemented — read/create "
                                      "only (parity with Hermes file tools)"}
    return {"ok": True, "changed": 0, "note": "no-op: no supported changes"}


def _edit_docx(path: str, changes: dict) -> dict:
    replace = changes.get("replace") or []
    if not replace:
        return {"ok": True, "changed": 0, "note": "no-op: no supported changes"}
    try:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {"ok": False, "error": str(exc)}

    w = f"{{{_NS_W}}}"
    changed = 0
    for para in root.iter(f"{w}p"):
        buf: list[str] = []
        nodes: list[tuple] = []
        for node in para.iter():
            if node.tag == f"{w}t":
                buf.append(node.text or "")
                nodes.append((node, len(buf)))
        full = "".join(buf)
        new_full = full
        for spec in replace:
            old, new = spec.get("old", ""), spec.get("new", "")
            if not old:
                continue
            new_full = new_full.replace(old, new)
        if new_full != full:
            # Rewrite text across runs: first run takes leading text, the
            # rest becomes empty — simplest faithful run merge.
            cursor = 0
            for node, end_pos in nodes:
                start = 0 if not cursor else cursor
                seg = new_full[start:end_pos]
                node.text = seg
                cursor = end_pos
            # Any trailing text beyond the last run gets appended there.
            last = nodes[-1][0] if nodes else None
            if last is not None and len(new_full) > cursor:
                last.text = (last.text or "") + new_full[cursor:]
            changed += 1
    if changed == 0:
        return {"ok": True, "changed": 0, "note": "no matching text to replace"}
    try:
        _write_zip_xml(path, names, "word/document.xml", root)
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": f"failed to rewrite {path}: {exc}"}
    return {"ok": True, "changed": changed}


def _write_zip_xml(path: str, names: list[str], entry: str, root: ET.Element) -> None:
    """Rewrite one XML part of a zip in place, preserving other members.

    Reads all members into memory (documents are small in practice),
    replaces the edited part, and rewrites the archive with fixed member
    names + compression. This is the standard minimal ZIP-editing approach
    and keeps external references intact.
    """
    new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp = Path(path).with_suffix(".docx.tmp")
    try:
        with zipfile.ZipFile(path) as zfin:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zfout:
                for name in names:
                    data = new_xml if name == entry else zfin.read(name)
                    zfout.writestr(name, data)
        import os as _os
        _os.replace(str(tmp), path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ── generators: minimal-but-real zip packages ─────────────────────────────

def _content_types(overrides: dict[str, str]) -> bytes:
    """[Content_Types].xml with the given extension→content-type overrides."""
    defaults = "\n".join(
        f'<Default Extension="{ext}" ContentType="{ct}"/>'
        for ext, ct in {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "xml": "application/xml",
        }.items()
    )
    overrides_xml = "\n".join(
        f'<Override PartName="/{name}" ContentType="{ct}"/>'
        for name, ct in overrides.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}">{defaults}
{overrides_xml}</Types>
""".encode("utf-8")


def _rels_xml(entries: list[tuple[str, str, str]]) -> bytes:
    """Relationships part: [(Id, Type, Target), ...]."""
    items = "\n".join(
        f'<Relationship Id="{rid}" Type="{typ}" Target="{tgt}"/>'
        for rid, typ, tgt in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_PKG_REL}">{items}
</Relationships>
""".encode("utf-8")


def _doc_xml(paragraphs: list[str]) -> bytes:
    """word/document.xml body with one w:p per paragraph."""
    body = "\n".join(
        f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_NS_W}"><w:body>{body}
<w:sectPr/></w:body></w:document>
""".encode("utf-8")


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _write_docx(path: str, content: str) -> None:
    paragraphs = [p for p in content.splitlines()] if content else [" "]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types({"/word/document.xml": _WORD_CT}))
        zf.writestr("_rels/.rels", _rels_xml([
            ("rId1", _WORD_REL, "word/document.xml"),
        ]))
        zf.writestr("word/document.xml", _doc_xml(paragraphs))


def _sheet_xml(rows: list[list[str]]) -> bytes:
    """xl/worksheets/sheet1.xml with a row per row; cells carry r + t attrs
    so the extractor's cell-value path (inline strings) round-trips."""
    row_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = _col_letter(c_idx) + str(r_idx)
            if value == "":
                continue
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>')
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<worksheet xmlns="{_NS_S}"><sheetData>{"".join(row_xml)}</sheetData></worksheet>').encode("utf-8")


def _col_letter(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _write_xlsx(path: str, rows: list[list]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types({
            "/xl/workbook.xml": _SHEET_CT,
            "/xl/worksheets/sheet1.xml":
                "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        }))
        zf.writestr("_rels/.rels", _rels_xml([("rId1", _SHEET_REL, "xl/workbook.xml")]))
        zf.writestr("xl/workbook.xml", (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS_S}" xmlns:r="{_NS_R}">'
            f'<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ).encode("utf-8"))
        zf.writestr("xl/_rels/workbook.xml.rels", _rels_xml([
            ("rId1", _SHEET_REL, "worksheets/sheet1.xml"),
        ]))
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))


def _slide_xml(title: str, bullets: list[str]) -> bytes:
    """One pptx slide: a title shape plus one body shape with a:t runs."""
    a = _NS_A
    p = _NS_P
    esc = _xml_escape
    body_shapes = "\n".join(
        f'<p:sp><p:nvSpPr><p:cNvPr id="{i + 2}" name="body{i + 1}"/>'
        f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>'
        f'<a:p><a:r><a:t>{esc(b)}</a:t></a:r></a:p></p:txBody></p:sp>'
        for i, b in enumerate(bullets)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{a}" xmlns:r="{_NS_R}" xmlns:p="{p}">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr/>
<p:sp><p:nvSpPr><p:cNvPr id="2" name="title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>
<a:p><a:r><a:t>{esc(title)}</a:t></a:r></a:p></p:txBody></p:sp>
{body_shapes}
</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>
""".encode("utf-8")


def _write_pptx(path: str, content: str) -> None:
    """Create a PPTX from text: the first line is the title of slide 1, the
    rest become body runs on the same slide."""
    lines = [line for line in content.splitlines() if line.strip()]
    title = lines[0] if lines else "Slide 1"
    bullets = lines[1:] if lines else []
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types({
            "/ppt/presentation.xml": _PPT_CT,
            "/ppt/slides/slide1.xml":
                "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        }))
        zf.writestr("_rels/.rels", _rels_xml([("rId1", _PPT_REL, "ppt/presentation.xml")]))
        zf.writestr("ppt/presentation.xml", (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:p="{_NS_P}" xmlns:r="{_NS_R}">'
            f'<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
            f'<p:sldSz cx="9144000" cy="6858000"/></p:presentation>'
        ).encode("utf-8"))
        zf.writestr("ppt/_rels/presentation.xml.rels", _rels_xml([
            ("rId1",
             "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
             "slides/slide1.xml"),
        ]))
        zf.writestr("ppt/slides/slide1.xml", _slide_xml(title, bullets))


def documents_manifest() -> dict:
    """Describe the formats this module can read/create/edit.

    Mirrors the tools.py docs() note that real editing was unavailable — the
    manifest advertises the actual built-in capability (zip-based formats
    fully; pdf/ipynb read-only).
    """
    return {
        "ok": True,
        "read": sorted(FORMAT_EXTS.values()),
        "create": ["docx", "xlsx", "pptx"],
        "edit": ["docx"],
        "note": "pure stdlib (zipfile+xml.etree); pdf is text-extraction only",
    }