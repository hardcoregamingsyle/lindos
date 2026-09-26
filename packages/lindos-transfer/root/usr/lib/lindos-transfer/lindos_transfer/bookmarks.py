"""Bookmarks: models, readers and the Netscape bookmark HTML writer (SPEC-WINDOWS §29.8).

* Chromium family (Chrome, Edge, Brave, Opera, Vivaldi): ``Bookmarks`` / ``AccountBookmarks`` JSON
  (``roots.bookmark_bar|other|synced``; dates are microseconds since 1601-01-01 -> Unix seconds).
  Encrypted bookmark files are never opened (deny-listed).
* Firefox: ``places.sqlite`` read from a *temporary copy* (with its ``-wal``), ``moz_bookmarks`` joined
  with ``moz_places`` (PRTime = microseconds since 1970).
* Internet Explorer / Edge Legacy *Favorites*: ``.url`` files (``[InternetShortcut] URL=``).
* Output: the Netscape bookmark file every browser imports ("Import bookmarks from HTML file");
  the Chromium bookmarks bar / Firefox toolbar is marked ``PERSONAL_TOOLBAR_FOLDER="true"``.

Parsers are bounded (size, depth, node count) and never trust input.
"""

from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from . import secrets
from .copyengine import CopyEngine, copy_to_scratch

__all__ = [
    "BookmarkError",
    "Link",
    "Separator",
    "Folder",
    "Node",
    "webkit_to_unix",
    "prtime_to_unix",
    "chromium_roots",
    "read_chromium_file",
    "firefox_roots",
    "parse_url_file",
    "favorites_roots",
    "netscape_html",
    "count_links",
    "MAX_NODES",
    "MAX_DEPTH",
]

log = logging.getLogger("lindos-transfer.bookmarks")

WEBKIT_EPOCH_OFFSET = 11644473600          # seconds between 1601-01-01 and 1970-01-01
MAX_JSON_BYTES = 64 << 20
MAX_NODES = 500_000
MAX_DEPTH = 100
MAX_URL_FILE = 64 << 10
_SAFE_FAVORITE_SCHEMES = ("http://", "https://", "ftp://", "mailto:")


class BookmarkError(ValueError):
    """The bookmark data cannot be read."""


@dataclass
class Link:
    title: str
    url: str
    add_date: Optional[int] = None


@dataclass
class Separator:
    pass


@dataclass
class Folder:
    title: str
    children: List["Node"] = field(default_factory=list)
    add_date: Optional[int] = None
    last_modified: Optional[int] = None
    toolbar: bool = False


Node = Union[Link, Separator, Folder]


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
def webkit_to_unix(value: Any) -> Optional[int]:
    """Chromium time (microseconds since 1601, string or int) -> Unix seconds (``None`` if unset/bad)."""
    try:
        micros = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    secs = micros // 1_000_000 - WEBKIT_EPOCH_OFFSET
    return secs if 0 < secs < 32503680000 else None


def prtime_to_unix(value: Any) -> Optional[int]:
    """Firefox PRTime (microseconds since 1970) -> Unix seconds."""
    try:
        micros = int(value)
    except (TypeError, ValueError):
        return None
    secs = micros // 1_000_000
    return secs if 0 < secs < 32503680000 else None


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0

    def take(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise BookmarkError("too many bookmarks (the file looks damaged)")


# --------------------------------------------------------------------------- #
# Chromium
# --------------------------------------------------------------------------- #
def _chromium_node(node: Any, budget: _Budget, depth: int) -> Optional[Node]:
    if not isinstance(node, dict):
        return None
    if depth > MAX_DEPTH:
        raise BookmarkError("bookmark folders nested too deeply")
    budget.take()
    kind = node.get("type")
    title = str(node.get("name") or "")
    if kind == "url":
        url = str(node.get("url") or "")
        if not url:
            return None
        return Link(title or url, url, webkit_to_unix(node.get("date_added")))
    if kind == "folder":
        folder = Folder(title, add_date=webkit_to_unix(node.get("date_added")),
                        last_modified=webkit_to_unix(node.get("date_modified")))
        for child in node.get("children") or []:
            conv = _chromium_node(child, budget, depth + 1)
            if conv is not None:
                folder.children.append(conv)
        return folder
    return None  # unknown node types are skipped (as Chromium does)


_CHROMIUM_ROOTS = (("bookmark_bar", "Bookmarks bar", True), ("other", "Other bookmarks", False),
                   ("synced", "Mobile bookmarks", False))


def chromium_roots(data: Any) -> List[Folder]:
    """Top-level folders from a parsed Chromium ``Bookmarks`` JSON (empty folders dropped)."""
    if not isinstance(data, dict) or not isinstance(data.get("roots"), dict):
        raise BookmarkError("not a Chromium bookmarks file")
    if data.get("version") not in (None, 1):
        log.info("unexpected Chromium bookmarks version %r; reading anyway", data.get("version"))
    roots = data["roots"]
    budget = _Budget()
    out: List[Folder] = []
    for key, title, toolbar in _CHROMIUM_ROOTS:
        conv = _chromium_node(roots.get(key), budget, 0)
        if isinstance(conv, Folder):
            conv.title = conv.title or title
            conv.toolbar = toolbar
            if conv.children:
                out.append(conv)
    return out


def read_chromium_file(path: Union[str, os.PathLike]) -> List[Folder]:
    """Read ``Bookmarks``/``AccountBookmarks`` through the secrets gate."""
    try:
        raw = secrets.read_bytes(path, MAX_JSON_BYTES)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BookmarkError(f"cannot read {os.path.basename(os.fspath(path))}: {exc}") from exc
    return chromium_roots(data)


# --------------------------------------------------------------------------- #
# Firefox
# --------------------------------------------------------------------------- #
_FIREFOX_ROOTS = (("toolbar_____", "Bookmarks Toolbar", True), ("menu________", "Bookmarks Menu", False),
                  ("unfiled_____", "Other Bookmarks", False), ("mobile______", "Mobile Bookmarks", False))


def _firefox_rows(db: Path) -> List[sqlite3.Row]:
    # The temporary copy is ours: with a -wal file open it normally so SQLite applies the log;
    # without one, ?immutable=1 reads it without any locking or journal files.
    wal = db.with_name(db.name + "-wal")
    uri = db.resolve().as_uri() if wal.exists() else db.resolve().as_uri() + "?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.row_factory = sqlite3.Row
        return list(con.execute(
            "SELECT b.id AS id, b.type AS type, b.parent AS parent, b.position AS position, "
            "b.title AS title, b.dateAdded AS added, b.lastModified AS modified, b.guid AS guid, "
            "p.url AS url FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk = p.id"))
    finally:
        con.close()


def firefox_roots(places: Union[str, os.PathLike], *, scratch: Optional[Path] = None) -> List[Folder]:
    """Bookmarks from a Firefox ``places.sqlite`` (copied with its ``-wal`` to a temporary folder first)."""
    src = Path(places)
    with tempfile.TemporaryDirectory(prefix="lindos-places-", dir=scratch) as tmp:
        tmpdb = Path(tmp) / "places.sqlite"
        try:
            copy_to_scratch(src, tmpdb)
            wal = src.with_name(src.name + "-wal")
            if os.path.lexists(wal):
                copy_to_scratch(wal, Path(tmp) / "places.sqlite-wal")
            rows = _firefox_rows(tmpdb)
        except (OSError, sqlite3.Error) as exc:
            raise BookmarkError(f"cannot read Firefox bookmarks: {exc}") from exc
    by_parent: Dict[int, List[sqlite3.Row]] = {}
    by_guid: Dict[str, sqlite3.Row] = {}
    for r in rows:
        by_parent.setdefault(int(r["parent"] or 0), []).append(r)
        if r["guid"]:
            by_guid[str(r["guid"])] = r
    budget = _Budget()
    seen: set = set()

    def build(row: sqlite3.Row, depth: int) -> Optional[Node]:
        rid = int(row["id"])
        if rid in seen:
            return None
        seen.add(rid)
        if depth > MAX_DEPTH:
            raise BookmarkError("bookmark folders nested too deeply")
        budget.take()
        kind = int(row["type"] or 0)
        if kind == 1:
            url = str(row["url"] or "")
            if not url or url.startswith("place:"):
                return None
            return Link(str(row["title"] or url), url, prtime_to_unix(row["added"]))
        if kind == 3:
            return Separator()
        if kind == 2:
            folder = Folder(str(row["title"] or ""), add_date=prtime_to_unix(row["added"]),
                            last_modified=prtime_to_unix(row["modified"]))
            for child in sorted(by_parent.get(rid, []), key=lambda c: int(c["position"] or 0)):
                conv = build(child, depth + 1)
                if conv is not None:
                    folder.children.append(conv)
            return folder
        return None

    out: List[Folder] = []
    for guid, title, toolbar in _FIREFOX_ROOTS:
        row = by_guid.get(guid)
        if row is None:
            continue
        conv = build(row, 0)
        if isinstance(conv, Folder) and conv.children:
            conv.title = title
            conv.toolbar = toolbar
            out.append(conv)
    return out


# --------------------------------------------------------------------------- #
# Internet Explorer / Edge Legacy favorites
# --------------------------------------------------------------------------- #
def parse_url_file(data: bytes) -> Optional[str]:
    """The ``URL=`` of an ``[InternetShortcut]`` file (ANSI, UTF-8 or UTF-16 with BOM)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "internetshortcut" and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().lower() == "url":
                return value.strip() or None
    return None


def favorites_roots(folder: Path, *, engine: Optional[CopyEngine] = None) -> List[Folder]:
    """Favorites (``.url`` files, sub-folders kept) as one "Favorites" folder."""
    engine = engine or CopyEngine(use_default_xattr=False)
    top = Folder("Favorites")
    index: Dict[str, Folder] = {"": top}
    budget = _Budget()
    for kind, path, rel, _info in engine.walk(Path(folder)):
        if kind != "file" or not rel.lower().endswith(".url"):
            continue
        budget.take()
        try:
            url = parse_url_file(secrets.read_bytes(path, MAX_URL_FILE))
        except OSError:
            continue
        if not url or not url.lower().startswith(_SAFE_FAVORITE_SCHEMES):
            continue
        parts = rel.split("/")
        parent = top
        for i in range(len(parts) - 1):
            key = "/".join(parts[: i + 1]).lower()
            if key not in index:
                sub = Folder(parts[i])
                index["/".join(parts[:i]).lower()].children.append(sub)
                index[key] = sub
            parent = index[key]
        parent.children.append(Link(parts[-1][:-4], url))
    return [top] if count_links([top]) else []


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #
def count_links(nodes: Iterable[Node]) -> int:
    total = 0
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, Link):
            total += 1
        elif isinstance(n, Folder):
            stack.extend(n.children)
    return total


def _attr_dates(add: Optional[int], mod: Optional[int] = None) -> str:
    out = ""
    if add:
        out += f' ADD_DATE="{int(add)}"'
    if mod:
        out += f' LAST_MODIFIED="{int(mod)}"'
    return out


def netscape_html(roots: Sequence[Node], *, title: str = "Bookmarks") -> str:
    """Serialise *roots* as a Netscape bookmark file (UTF-8 text)."""
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file.",
        "     It will be read and overwritten.",
        "     DO NOT EDIT! -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        f"<TITLE>{esc(title)}</TITLE>",
        f"<H1>{esc(title)}</H1>",
        "<DL><p>",
    ]

    def emit(node: Node, depth: int) -> None:
        pad = "    " * depth
        if isinstance(node, Link):
            lines.append(f'{pad}<DT><A HREF="{esc(node.url)}"{_attr_dates(node.add_date)}>{esc(node.title)}</A>')
        elif isinstance(node, Separator):
            lines.append(f"{pad}<HR>")
        elif isinstance(node, Folder):
            toolbar = ' PERSONAL_TOOLBAR_FOLDER="true"' if node.toolbar else ""
            lines.append(f"{pad}<DT><H3{_attr_dates(node.add_date, node.last_modified)}{toolbar}>"
                         f"{esc(node.title)}</H3>")
            lines.append(f"{pad}<DL><p>")
            for child in node.children:
                emit(child, depth + 1)
            lines.append(f"{pad}</DL><p>")

    for node in roots:
        emit(node, 1)
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"
