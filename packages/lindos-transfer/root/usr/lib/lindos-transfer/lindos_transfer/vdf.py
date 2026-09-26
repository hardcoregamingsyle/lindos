"""Valve KeyValues ("VDF") text reader and writer (``libraryfolders.vdf``, ``appmanifest_*.acf``).

Grammar: ``"key" "value"`` pairs and ``"key" { ... }`` blocks; tokens may be quoted (escapes
``\\\\ \\" \\n \\t``) or bare; ``//`` comments; ``[$WIN32]``-style conditionals after a token are
ignored.  Keys are case-insensitive in Steam's own reader -- use :func:`get`.  Input is bounded
(size, nesting); anything malformed raises :class:`VdfError`.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple, Union

from . import secrets

__all__ = ["VdfError", "loads", "load", "dumps", "get", "MAX_VDF_BYTES", "MAX_DEPTH"]

MAX_VDF_BYTES = 16 << 20
MAX_DEPTH = 64
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}

VdfDict = Dict[str, Any]


class VdfError(ValueError):
    """Not a readable Valve KeyValues text file."""


def _tokens(text: str) -> List[Tuple[str, str]]:
    """``[(kind, value)]`` with kind in ``str``, ``{``, ``}``."""
    out: List[Tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            out.append((c, c))
            i += 1
        elif c == "[":
            j = text.find("]", i)
            if j < 0:
                raise VdfError("unterminated conditional")
            i = j + 1  # platform conditional: ignored
        elif c == '"':
            i += 1
            buf: List[str] = []
            while True:
                if i >= n:
                    raise VdfError("unterminated string")
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], "\\" + text[i + 1]))
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            out.append(("str", "".join(buf)))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            out.append(("str", text[i:j]))
            i = j
    return out


def loads(text: str) -> VdfDict:
    """Parse KeyValues text into nested dicts (a repeated key keeps its last value)."""
    if len(text) > MAX_VDF_BYTES:
        raise VdfError("file too large")
    toks = _tokens(text)
    root: VdfDict = {}
    stack: List[VdfDict] = [root]
    i = 0
    while i < len(toks):
        kind, value = toks[i]
        if kind == "}":
            if len(stack) == 1:
                raise VdfError("unexpected '}'")
            stack.pop()
            i += 1
            continue
        if kind == "{":
            raise VdfError("block without a name")
        if i + 1 >= len(toks):
            raise VdfError(f"key {value!r} has no value")
        nkind, nvalue = toks[i + 1]
        if nkind == "{":
            if len(stack) > MAX_DEPTH:
                raise VdfError("nested too deeply")
            child: VdfDict = {}
            stack[-1][value] = child
            stack.append(child)
            i += 2
        elif nkind == "str":
            stack[-1][value] = nvalue
            i += 2
        else:
            raise VdfError(f"key {value!r} has no value")
    if len(stack) != 1:
        raise VdfError("missing '}'")
    return root


def load(path: Union[str, "os.PathLike[str]"]) -> VdfDict:
    """Read a VDF/ACF file through the secrets gate."""
    raw = secrets.read_bytes(path, MAX_VDF_BYTES)
    return loads(raw.decode("utf-8", errors="replace"))


def _quote(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(data: VdfDict, _indent: int = 0) -> str:
    """Serialise like Steam does (tab indentation, quoted keys and values)."""
    lines: List[str] = []
    pad = "\t" * _indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{_quote(key)}")
            lines.append(f"{pad}{{")
            inner = dumps(value, _indent + 1)
            if inner:
                lines.append(inner.rstrip("\n"))
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{_quote(key)}\t\t{_quote(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def get(data: Optional[VdfDict], key: str, default: Any = None) -> Any:
    """Case-insensitive lookup."""
    if not isinstance(data, dict):
        return default
    if key in data:
        return data[key]
    low = key.lower()
    for k, v in data.items():
        if k.lower() == low:
            return v
    return default
