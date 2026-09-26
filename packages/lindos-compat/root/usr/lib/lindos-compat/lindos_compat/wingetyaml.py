"""Strict YAML reader for winget manifests (SPEC-WINDOWS §28.10).

winget manifests (the files in ``microsoft/winget-pkgs`` and the "merged" manifests and
``versionData`` lists on Microsoft's winget CDN) use a small part of YAML.  winget itself reads
them with libyaml and treats **every scalar as a string** -- ``PackageVersion: 1.10`` must stay
``"1.10"`` (not the float 1.1) and ``ReleaseDate: 2024-11-12`` must stay text.

:func:`load` is what the rest of Lindos calls:

* when ``python3-yaml`` is installed it composes the document with ``yaml.CBaseLoader`` (or the
  pure-Python ``BaseLoader``) -- both keep every scalar a string -- and then applies the same
  refusals as the fallback parser;
* otherwise it uses :func:`parse`, a stdlib-only parser for exactly the subset winget uses.

Supported by :func:`parse`: block mappings and sequences (sequences written at the parent key's
indentation *and* indented), ``#`` comments (full-line and trailing), plain scalars folded over
several lines, single-quoted (``''`` escape) and double-quoted scalars with the full YAML escape
table (``\\xNN``, ``\\uNNNN``, ``\\UNNNNNNNN``, ``\\N``, ``\\_``, escaped line breaks ...),
``|``/``>`` block scalars with chomping (``-``/``+``) and indentation indicators, one-line flow
lists/maps of scalars (``[]``, ``[x64, arm64]``, ``{}``), CRLF line ends and a UTF-8 BOM, an
optional ``---`` start and ``...`` end marker.

Refused (``YamlError``), like winget does: anchors ``&``, aliases ``*``, tags ``!``, complex
``?`` keys, multi-document streams, duplicate keys, tab indentation, nested or multi-line flow
collections, non-printable characters, and inputs larger than :data:`MAX_BYTES`.

Stdlib only; importable on any OS.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

__all__ = ["YamlError", "MAX_BYTES", "MAX_DEPTH", "parse", "load", "libyaml_available"]

#: Refuse documents larger than this (the biggest winget manifest is well below 1 MiB).
MAX_BYTES = 16 << 20
#: Maximum nesting depth of mappings/sequences.
MAX_DEPTH = 64

# YAML 1.2 "printable" set (the same check libyaml/PyYAML's reader applies).
_NON_PRINTABLE_RE = re.compile("[^\x09\x0a\x0d\x20-\x7e\x85\xa0-퟿-�\U00010000-\U0010ffff]")

_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": " ", "P": " ",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_DEFAULT_TAGS = ("tag:yaml.org,2002:str", "tag:yaml.org,2002:seq", "tag:yaml.org,2002:map")


class YamlError(ValueError):
    """The text is not valid YAML, or uses a YAML feature winget manifests never use."""

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


# ---------------------------------------------------------------------------
# input normalisation
# ---------------------------------------------------------------------------


def _to_text(data: Union[str, bytes, bytearray]) -> str:
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_BYTES:
            raise YamlError(f"document too large ({len(data)} bytes; limit {MAX_BYTES})")
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise YamlError(f"not UTF-8 text ({exc.reason} at byte {exc.start})") from None
    elif isinstance(data, str):
        text = data
    else:
        raise TypeError("YAML input must be str or bytes")
    if len(text) > MAX_BYTES:
        raise YamlError(f"document too large ({len(text)} characters; limit {MAX_BYTES})")
    if text.startswith("﻿"):
        text = text[1:]
    bad = _NON_PRINTABLE_RE.search(text)
    if bad:
        line = text.count("\n", 0, bad.start()) + 1
        raise YamlError(f"non-printable character U+{ord(bad.group()):04X}", line)
    return text


def _spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_seq_entry(content: str) -> bool:
    return content == "-" or (content[:1] == "-" and content[1:2] in (" ", "\t"))


def _is_doc_marker(line: str, marker: str) -> bool:
    return line.startswith(marker) and (len(line) == 3 or line[3] in " \t")


def _strip_comment(text: str) -> Tuple[str, bool]:
    """Cut a trailing `` #comment`` off a plain-scalar line; returns (text, had_comment)."""
    for i, ch in enumerate(text):
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip(" \t"), True
    return text.rstrip(" \t"), False


def _has_mapping_indicator(text: str) -> bool:
    """``a: b`` / ``a:`` inside a plain scalar (YAML: "mapping values are not allowed here")."""
    return ": " in text or ":\t" in text or text.endswith(":")


def _check_plain_start(text: str, line: int) -> None:
    c = text[:1]
    if c in ("&", "*", "!"):
        what = {"&": "anchors (&)", "*": "aliases (*)", "!": "tags (!)"}[c]
        raise YamlError(f"YAML {what} are not supported in winget manifests", line)
    if c in ("@", "`", "%"):
        raise YamlError(f"a plain value cannot start with {c!r} (quote it)", line)
    if c == "?" and text[1:2] in ("", " ", "\t"):
        raise YamlError("complex mapping keys ('? ') are not supported", line)
    if c in (",", "]", "}"):
        raise YamlError(f"unexpected {c!r}", line)
    if _is_seq_entry(text):
        raise YamlError("a list cannot start on the same line as its key", line)


# ---------------------------------------------------------------------------
# scalar decoding helpers
# ---------------------------------------------------------------------------


def _decode_dq(segment: str, line: int) -> Tuple[List[Tuple[str, bool]], bool]:
    """Decode one line of a double-quoted scalar.

    Returns ``(pieces, escaped_break)`` where ``pieces`` is a list of ``(text, escaped)`` so that
    folding can strip only *unescaped* white space, and ``escaped_break`` says the line ended in a
    backslash (an escaped line break: no folding space is inserted).
    """
    pieces: List[Tuple[str, bool]] = []
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch != "\\":
            pieces.append((ch, False))
            i += 1
            continue
        if i + 1 >= n:
            return pieces, True
        code = segment[i + 1]
        if code in _ESCAPES:
            pieces.append((_ESCAPES[code], True))
            i += 2
            continue
        if code in _HEX_ESCAPES:
            width = _HEX_ESCAPES[code]
            digits = segment[i + 2:i + 2 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                raise YamlError(f"bad \\{code} escape in double-quoted text", line)
            value = int(digits, 16)
            if value > 0x10FFFF:
                raise YamlError(f"escape \\{code}{digits} is outside Unicode", line)
            pieces.append((chr(value), True))
            i += 2 + width
            continue
        raise YamlError(f"unknown escape \\{code} in double-quoted text", line)
    return pieces, False


def _join_surrogates(text: str) -> str:
    """Combine ``\\uD83D\\uDE00`` style pairs; replace lone surrogates with U+FFFD."""
    if not re.search("[\ud800-\udfff]", text):
        return text
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if "\ud800" <= ch <= "\udbff" and i + 1 < len(text) and "\udc00" <= text[i + 1] <= "\udfff":
            out.append(chr(0x10000 + ((ord(ch) - 0xD800) << 10) + (ord(text[i + 1]) - 0xDC00)))
            i += 2
            continue
        out.append("�" if "\ud800" <= ch <= "\udfff" else ch)
        i += 1
    return "".join(out)


def _fold(lines: List[Tuple[str, bool, bool]]) -> str:
    """Flow-scalar line folding.

    ``lines`` holds ``(text, escaped_break, blank)`` per source line, already stripped.  A single
    line break becomes a space, each empty line becomes ``\\n``; an escaped break joins directly.
    """
    result = lines[0][0]
    prev_escaped = lines[0][1]
    blank_run = 0
    last = len(lines) - 1
    for idx in range(1, len(lines)):
        text, escaped, blank = lines[idx]
        if prev_escaped:
            result += text
            prev_escaped = escaped
            continue
        if blank and idx < last:
            blank_run += 1
            continue
        result += ("\n" * blank_run) if blank_run else " "
        blank_run = 0
        result += text
        prev_escaped = escaped
    return result


def _strip_pieces(pieces: List[Tuple[str, bool]], *, left: bool, right: bool) -> str:
    start, end = 0, len(pieces)
    if left:
        while start < end and not pieces[start][1] and pieces[start][0] in " \t":
            start += 1
    if right:
        while end > start and not pieces[end - 1][1] and pieces[end - 1][0] in " \t":
            end -= 1
    return "".join(p[0] for p in pieces[start:end])


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.final_newline = text.endswith("\n")
        lines = text.split("\n")
        if self.final_newline:
            lines.pop()
        self.lines: List[str] = lines
        self.pos = 0

    # -- line helpers ------------------------------------------------------------

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.lines)

    def _lineno(self, idx: Optional[int] = None) -> int:
        return (self.pos if idx is None else idx) + 1

    def _has_newline(self, idx: int) -> bool:
        return idx < len(self.lines) - 1 or self.final_newline

    def _indent(self, idx: int) -> int:
        line = self.lines[idx]
        ind = _spaces(line)
        if ind < len(line) and line[ind] == "\t":
            raise YamlError("tabs cannot be used for indentation (use spaces)", idx + 1)
        return ind

    def _skip(self) -> None:
        """Advance over blank and comment-only lines."""
        while self.pos < len(self.lines):
            stripped = self.lines[self.pos].strip(" \t")
            if stripped == "" or stripped.startswith("#"):
                self.pos += 1
                continue
            break

    def _at_marker(self) -> bool:
        line = self.lines[self.pos]
        return _is_doc_marker(line, "---") or _is_doc_marker(line, "...")

    # -- document ----------------------------------------------------------------

    def parse(self) -> Any:
        # prologue: comments, %directives, an optional '---'
        while not self.eof:
            line = self.lines[self.pos]
            stripped = line.strip(" \t")
            if stripped == "" or stripped.startswith("#") or line.startswith("%"):
                self.pos += 1
                continue
            if _is_doc_marker(line, "---"):
                rest = line[3:].strip(" \t")
                if rest and not rest.startswith("#"):
                    raise YamlError("content on the '---' line is not supported", self._lineno())
                self.pos += 1
            break
        self._skip()
        if self.eof:
            return None
        if _is_doc_marker(self.lines[self.pos], "..."):
            node: Any = None
        else:
            node = self._node(-1, 0)
        self._skip()
        if not self.eof and _is_doc_marker(self.lines[self.pos], "..."):
            self.pos += 1
            self._skip()
        if not self.eof:
            if _is_doc_marker(self.lines[self.pos], "---"):
                raise YamlError("more than one YAML document in one file is not supported", self._lineno())
            raise YamlError("unexpected text (check the indentation)", self._lineno())
        return node

    # -- nodes -------------------------------------------------------------------

    def _node(self, parent: int, depth: int) -> Any:
        """Parse the node starting at the current line, which must be indented more than ``parent``."""
        if depth > MAX_DEPTH:
            raise YamlError(f"nesting deeper than {MAX_DEPTH} levels", self._lineno())
        self._skip()
        if self.eof:
            return ""
        ind = self._indent(self.pos)
        if ind <= parent or (ind == 0 and self._at_marker()):
            return ""
        content = self.lines[self.pos][ind:]
        if _is_seq_entry(content):
            return self._sequence(ind, depth)
        if self._split_key(content, self.pos) is not None:
            return self._mapping(ind, depth)
        return self._value(content, parent, depth)

    def _split_key(self, content: str, idx: int) -> Optional[Tuple[str, str]]:
        """``key: rest`` -> (key, rest) when ``content`` starts a mapping entry, else None."""
        line = idx + 1
        first = content[:1]
        if first in ("'", '"'):
            end = self._find_closing(content, 1, first)
            if end is None:
                return None  # a (possibly multi-line) quoted scalar, not a key
            after = content[end + 1:].lstrip(" \t")
            if after[:1] == ":" and after[1:2] in ("", " ", "\t"):
                raw = content[1:end]
                if first == "'":
                    key = raw.replace("''", "'")
                else:
                    pieces, _ = _decode_dq(raw, line)
                    key = _join_surrogates("".join(p[0] for p in pieces))
                return key, after[1:]
            return None
        if first in ("[", "{"):
            return None
        if first == "?" and content[1:2] in ("", " ", "\t"):
            raise YamlError("complex mapping keys ('? ') are not supported", line)
        for i, ch in enumerate(content):
            if ch == "#" and i > 0 and content[i - 1] in " \t":
                return None
            if ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
                key = content[:i].rstrip(" \t")
                if not key:
                    raise YamlError("empty mapping key", line)
                if key[0] in ("&", "*", "!", "|", ">", "@", "`", "%"):
                    _check_plain_start(key, line)
                    raise YamlError(f"a key cannot start with {key[0]!r} (quote it)", line)
                return key, content[i + 1:]
        return None

    @staticmethod
    def _find_closing(text: str, start: int, quote: str) -> Optional[int]:
        i = start
        n = len(text)
        while i < n:
            ch = text[i]
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                return i
            i += 1
        return None

    def _mapping(self, ind: int, depth: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            self._skip()
            if self.eof:
                break
            li = self._indent(self.pos)
            if li < ind or (li == 0 and self._at_marker()):
                break
            if li > ind:
                raise YamlError("unexpected indentation", self._lineno())
            content = self.lines[self.pos][ind:]
            if _is_seq_entry(content):
                raise YamlError("a list item was found where a 'key: value' line was expected", self._lineno())
            kv = self._split_key(content, self.pos)
            if kv is None:
                raise YamlError("expected a 'key: value' line", self._lineno())
            key, rest = kv
            if key in result:
                raise YamlError(f"duplicate key {key!r}", self._lineno())
            rest_s = rest.strip(" \t")
            if rest_s == "" or (rest_s.startswith("#") and rest[:1] in (" ", "\t")):
                self.pos += 1
                self._skip()
                value: Any = ""
                if not self.eof:
                    ni = self._indent(self.pos)
                    if ni > ind and not (ni == 0 and self._at_marker()):
                        value = self._node(ind, depth + 1)
                    elif ni == ind and _is_seq_entry(self.lines[self.pos][ni:]):
                        value = self._sequence(ind, depth + 1)
            else:
                value = self._value(rest.lstrip(" \t"), ind, depth + 1)
            result[key] = value
        return result

    def _sequence(self, ind: int, depth: int) -> List[Any]:
        items: List[Any] = []
        while True:
            self._skip()
            if self.eof:
                break
            li = self._indent(self.pos)
            if li < ind or (li == 0 and self._at_marker()):
                break
            if li > ind:
                raise YamlError("unexpected indentation", self._lineno())
            content = self.lines[self.pos][ind:]
            if not _is_seq_entry(content):
                break
            after = content[1:]
            after_s = after.lstrip(" \t")
            if after_s == "" or after_s.startswith("#"):
                self.pos += 1
                self._skip()
                item: Any = ""
                if not self.eof and self._indent(self.pos) > ind:
                    item = self._node(ind, depth + 1)
            else:
                # compact form ("- key: v", "- - x", "- value"): re-read the rest of the line as a
                # node that starts at its own column, so following lines align with it.
                col = ind + 1 + (len(after) - len(after_s))
                self.lines[self.pos] = " " * col + after_s
                item = self._node(ind, depth + 1)
            items.append(item)
        return items

    # -- scalars -----------------------------------------------------------------

    def _value(self, text: str, parent: int, depth: int) -> Any:
        """A value that starts at ``text`` on the current line (consumes its lines)."""
        line = self._lineno()
        first = text[:1]
        if first in ("|", ">"):
            return self._block_scalar(text, parent)
        if first in ('"', "'"):
            return self._quoted(text)
        if first in ("[", "{"):
            return self._flow(text)
        _check_plain_start(text, line)
        return self._plain(text, parent)

    def _plain(self, text: str, parent: int) -> str:
        first, had_comment = _strip_comment(text)
        if _has_mapping_indicator(first):
            raise YamlError("':' followed by a space inside a plain value (quote the value)", self._lineno())
        self.pos += 1
        parts: List[Tuple[int, str]] = []
        blank_run = 0
        while not had_comment and not self.eof:
            raw = self.lines[self.pos]
            stripped = raw.strip(" \t")
            if stripped == "":
                blank_run += 1
                self.pos += 1
                continue
            if _spaces(raw) <= parent or stripped.startswith("#"):
                break
            if _spaces(raw) == 0 and self._at_marker():
                break
            piece, had_comment = _strip_comment(stripped)
            if _has_mapping_indicator(piece):
                raise YamlError("':' followed by a space inside a plain value (quote the value)", self._lineno())
            parts.append((blank_run, piece))
            blank_run = 0
            self.pos += 1
        result = first
        for blanks, piece in parts:
            result += ("\n" * blanks) if blanks else " "
            result += piece
        return result

    def _quoted(self, text: str) -> str:
        quote = text[0]
        start_line = self._lineno()
        segments: List[str] = []
        buf = text[1:]
        while True:
            end = self._find_closing(buf, 0, quote)
            if end is not None:
                segments.append(buf[:end])
                after = buf[end + 1:]
                break
            segments.append(buf)
            self.pos += 1
            if self.eof:
                raise YamlError("unterminated quoted text", start_line)
            buf = self.lines[self.pos]
            if _spaces(buf) == 0 and self._at_marker():
                raise YamlError("unterminated quoted text", start_line)
        after_s = after.strip(" \t")
        if after_s and not (after_s.startswith("#") and after[:1] in (" ", "\t")):
            raise YamlError(f"unexpected text after the quoted value: {after_s[:20]!r}", self._lineno())
        self.pos += 1
        last = len(segments) - 1
        folded: List[Tuple[str, bool, bool]] = []
        for idx, seg in enumerate(segments):
            blank = seg.strip(" \t") == ""
            if quote == "'":
                piece = seg.replace("''", "'")
                if idx > 0:
                    piece = piece.lstrip(" \t")
                if idx < last:
                    piece = piece.rstrip(" \t")
                folded.append((piece, False, blank))
            else:
                pieces, escaped = _decode_dq(seg, start_line + idx)
                piece = _strip_pieces(pieces, left=idx > 0, right=idx < last and not escaped)
                folded.append((piece, escaped, blank and not escaped))
        return _join_surrogates(_fold(folded))

    def _block_scalar(self, text: str, parent: int) -> str:
        line = self._lineno()
        style = text[0]
        header = text[1:]
        chomp: Optional[bool] = None  # None = clip, False = strip, True = keep
        increment: Optional[int] = None
        i = 0
        while i < len(header) and i < 2 and header[i] in "+-123456789":
            ch = header[i]
            if ch in "+-":
                if chomp is not None:
                    raise YamlError("two chomping indicators in a block scalar header", line)
                chomp = ch == "+"
            else:
                if increment is not None:
                    raise YamlError("two indentation indicators in a block scalar header", line)
                increment = int(ch)
            i += 1
        tail = header[i:]
        if tail.strip(" \t") and not (tail[:1] in (" ", "\t") and tail.strip(" \t").startswith("#")):
            raise YamlError(f"unexpected text after {style!r}", line)
        self.pos += 1
        min_indent = max(parent + 1, 1)
        n = len(self.lines)
        if increment is not None:
            indent = min_indent + increment - 1
        else:
            max_indent = 0
            j = self.pos
            while j < n:
                sp = _spaces(self.lines[j])
                max_indent = max(max_indent, sp)
                if sp < len(self.lines[j]):
                    break
                j += 1
            indent = max(min_indent, max_indent)

        def blank(idx: int) -> bool:
            ln = self.lines[idx]
            return ln.strip(" ") == "" and len(ln) <= indent

        def content_at(idx: int) -> bool:
            return idx < n and not blank(idx) and _spaces(self.lines[idx]) >= indent

        chunks: List[str] = []
        line_break = ""
        j = self.pos
        breaks = 0
        while j < n and blank(j):
            if self._has_newline(j):
                breaks += 1
            j += 1
        folded = style == ">"
        while content_at(j):
            chunks.append("\n" * breaks)
            body = self.lines[j][indent:]
            leading_non_space = body[:1] not in (" ", "\t")
            chunks.append(body)
            line_break = "\n" if self._has_newline(j) else ""
            j += 1
            breaks = 0
            while j < n and blank(j):
                if self._has_newline(j):
                    breaks += 1
                j += 1
            if content_at(j):
                nxt = self.lines[j][indent:]
                if folded and line_break == "\n" and leading_non_space and nxt[:1] not in (" ", "\t"):
                    if not breaks:
                        chunks.append(" ")
                else:
                    chunks.append(line_break)
            else:
                break
        if chomp is not False:
            chunks.append(line_break)
        if chomp is True:
            chunks.append("\n" * breaks)
        self.pos = j
        return "".join(chunks)

    def _flow(self, text: str) -> Any:
        line = self._lineno()
        value, end = _FlowReader(text, line).read()
        rest = text[end:]
        rest_s = rest.strip(" \t")
        if rest_s and not (rest_s.startswith("#") and rest[:1] in (" ", "\t")):
            raise YamlError(f"unexpected text after a [ ] or {{ }} value: {rest_s[:20]!r}", line)
        self.pos += 1
        return value


class _FlowReader:
    """One-line ``[a, 'b', "c"]`` / ``{k: v}`` of scalars (winget's ``[]`` and friends)."""

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line
        self.i = 0

    def _ws(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t":
            self.i += 1

    def _unterminated(self) -> YamlError:
        return YamlError("[ ] or { } values must be complete on one line", self.line)

    def read(self) -> Tuple[Any, int]:
        opener = self.text[0]
        self.i = 1
        closer = "]" if opener == "[" else "}"
        seq: List[str] = []
        mapping: Dict[str, str] = {}
        while True:
            self._ws()
            if self.i >= len(self.text):
                raise self._unterminated()
            if self.text[self.i] == closer:
                self.i += 1
                return (seq if opener == "[" else mapping), self.i
            item = self._scalar(closer)
            if opener == "{":
                self._ws()
                if self.text[self.i:self.i + 1] != ":":
                    raise YamlError("expected ':' in a { } value", self.line)
                self.i += 1
                self._ws()
                value = self._scalar(closer)
                if item in mapping:
                    raise YamlError(f"duplicate key {item!r}", self.line)
                mapping[item] = value
            else:
                seq.append(item)
            self._ws()
            if self.i >= len(self.text):
                raise self._unterminated()
            ch = self.text[self.i]
            if ch == ",":
                self.i += 1
                continue
            if ch != closer:
                raise YamlError(f"unexpected {ch!r} in a [ ] or {{ }} value", self.line)

    def _scalar(self, closer: str) -> str:
        if self.i >= len(self.text):
            raise self._unterminated()
        ch = self.text[self.i]
        if ch in ("[", "{"):
            raise YamlError("nested [ ] / { } values are not supported in winget manifests", self.line)
        if ch in ('"', "'"):
            end = _Parser._find_closing(self.text, self.i + 1, ch)
            if end is None:
                raise self._unterminated()
            raw = self.text[self.i + 1:end]
            self.i = end + 1
            if ch == "'":
                return raw.replace("''", "'")
            pieces, _ = _decode_dq(raw, self.line)
            return _join_surrogates("".join(p[0] for p in pieces))
        if ch in (",", closer):
            raise YamlError("empty item in a [ ] or { } value", self.line)
        _check_plain_start(self.text[self.i:], self.line)
        start = self.i
        while self.i < len(self.text):
            c = self.text[self.i]
            if c in ",[]{}":
                break
            if c == ":" and closer == "}" and self.text[self.i + 1:self.i + 2] in (" ", "\t", ",", "}", ""):
                break
            if c == "#" and self.text[self.i - 1] in " \t":
                raise self._unterminated()
            self.i += 1
        return self.text[start:self.i].strip(" \t")


def parse(text: Union[str, bytes]) -> Any:
    """Parse a winget YAML document with the stdlib subset parser.

    Returns nested ``dict``/``list``/``str`` (every scalar is a ``str``; an empty value is ``""``),
    or ``None`` for an empty document.  Raises :class:`YamlError`.
    """
    return _Parser(_to_text(text)).parse()


# ---------------------------------------------------------------------------
# python3-yaml (libyaml) path
# ---------------------------------------------------------------------------


def _yaml_module() -> Any:
    """``yaml`` (python3-yaml) when importable, else None.  Tests monkeypatch this."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    if not hasattr(yaml, "compose") or not hasattr(yaml, "BaseLoader"):
        return None
    return yaml


def libyaml_available() -> bool:
    """True when python3-yaml is importable (then :func:`load` uses it)."""
    return _yaml_module() is not None


def _from_node(yaml: Any, node: Any, seen: set, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise YamlError(f"nesting deeper than {MAX_DEPTH} levels")
    line = getattr(getattr(node, "start_mark", None), "line", None)
    lineno = line + 1 if isinstance(line, int) else None
    if id(node) in seen:
        raise YamlError("YAML aliases (*) are not supported in winget manifests", lineno)
    seen.add(id(node))
    if node.tag not in _DEFAULT_TAGS:
        raise YamlError(f"YAML tags ({node.tag}) are not supported in winget manifests", lineno)
    if isinstance(node, yaml.ScalarNode):
        return node.value
    if isinstance(node, yaml.SequenceNode):
        return [_from_node(yaml, item, seen, depth + 1) for item in node.value]
    if isinstance(node, yaml.MappingNode):
        out: Dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise YamlError("complex mapping keys are not supported", lineno)
            key = _from_node(yaml, key_node, seen, depth + 1)
            if key in out:
                raise YamlError(f"duplicate key {key!r}", lineno)
            out[key] = _from_node(yaml, value_node, seen, depth + 1)
        return out
    raise YamlError("unsupported YAML node", lineno)


def _load_with_libyaml(yaml: Any, text: str) -> Any:
    if "&" in text or "!" in text:
        _refuse_anchor_and_tag_tokens(yaml, text)
    loader = getattr(yaml, "CBaseLoader", None) or yaml.BaseLoader
    try:
        node = yaml.compose(text, Loader=loader)
    except yaml.YAMLError as exc:
        raise YamlError(f"invalid YAML: {exc}") from None
    if node is None:
        return None
    return _from_node(yaml, node, set(), 0)


def _refuse_anchor_and_tag_tokens(yaml: Any, text: str) -> None:
    """Refuse real anchor/tag tokens (a '&' or '!' inside a value is fine)."""
    loader = getattr(yaml, "CBaseLoader", None) or yaml.BaseLoader
    try:
        for token in yaml.scan(text, Loader=loader):
            mark = getattr(token, "start_mark", None)
            lineno = mark.line + 1 if mark is not None else None
            if isinstance(token, yaml.AnchorToken):
                raise YamlError("YAML anchors (&) are not supported in winget manifests", lineno)
            if isinstance(token, yaml.TagToken):
                raise YamlError("YAML tags (!) are not supported in winget manifests", lineno)
    except yaml.YAMLError as exc:
        raise YamlError(f"invalid YAML: {exc}") from None


def load(data: Union[str, bytes], *, use_libyaml: Optional[bool] = None) -> Any:
    """Load a winget manifest: python3-yaml (Base loader) when available, else :func:`parse`.

    Both paths return the same shapes (strings only) and apply the same refusals.
    ``use_libyaml=False`` forces the stdlib parser.
    """
    text = _to_text(data)
    if use_libyaml is not False:
        yaml = _yaml_module()
        if yaml is not None:
            return _load_with_libyaml(yaml, text)
    return _Parser(text).parse()
