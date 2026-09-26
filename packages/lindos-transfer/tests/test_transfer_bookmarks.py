"""Tests for bookmark parsing/writing and browser discovery (SPEC-WINDOWS §29.8)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lindos_transfer.bookmarks import (BookmarkError, Folder, Link, Separator, chromium_roots,
                                       count_links, favorites_roots, firefox_roots, netscape_html,
                                       parse_url_file, prtime_to_unix, read_chromium_file,
                                       webkit_to_unix)
from lindos_transfer.browsers import (chromium_profiles, firefox_profiles, firefox_running,
                                      linux_firefox_root, register_firefox_profile, safe_filename)


# --------------------------------------------------------------------------- #
# date conversions
# --------------------------------------------------------------------------- #
def test_webkit_to_unix_known_value() -> None:
    # 2024-01-01 00:00:00 UTC is 1704067200 unix; Chromium stores microseconds since 1601-01-01.
    micros = (1704067200 + 11644473600) * 1_000_000
    assert webkit_to_unix(str(micros)) == 1704067200
    assert webkit_to_unix("0") is None
    assert webkit_to_unix("not a number") is None
    assert webkit_to_unix(None) is None


def test_prtime_to_unix() -> None:
    assert prtime_to_unix(1704067200 * 1_000_000) == 1704067200
    assert prtime_to_unix(0) is None
    assert prtime_to_unix("bad") is None


# --------------------------------------------------------------------------- #
# Chromium Bookmarks JSON
# --------------------------------------------------------------------------- #
def _chromium_json(bar_children=None, other_children=None) -> dict:
    return {"version": 1, "roots": {
        "bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": bar_children or []},
        "other": {"type": "folder", "name": "Other bookmarks", "children": other_children or []},
        "synced": {"type": "folder", "name": "Mobile bookmarks", "children": []},
    }}


def test_chromium_roots_basic_folder_and_link() -> None:
    data = _chromium_json(bar_children=[
        {"type": "url", "name": "Example", "url": "https://example.com", "date_added": "0"},
        {"type": "folder", "name": "Sub", "children": [
            {"type": "url", "name": "Inner", "url": "https://inner.example"}]},
    ])
    roots = chromium_roots(data)
    assert len(roots) == 1
    bar = roots[0]
    assert bar.toolbar is True
    assert count_links(roots) == 2


def test_chromium_roots_drops_empty_folders() -> None:
    data = _chromium_json()
    assert chromium_roots(data) == []


def test_chromium_roots_rejects_non_chromium_json() -> None:
    with pytest.raises(BookmarkError):
        chromium_roots({"not": "bookmarks"})


def test_chromium_roots_skips_unknown_node_types() -> None:
    data = _chromium_json(bar_children=[{"type": "weird", "name": "?"},
                                        {"type": "url", "name": "A", "url": "https://a.example"}])
    roots = chromium_roots(data)
    assert count_links(roots) == 1


def test_read_chromium_file_through_secrets_gate(tmp_path: Path) -> None:
    f = tmp_path / "Bookmarks"
    f.write_bytes(("﻿" + json.dumps(_chromium_json(bar_children=[
        {"type": "url", "name": "A", "url": "https://a.example"}]))).encode("utf-8"))
    roots = read_chromium_file(f)
    assert count_links(roots) == 1


def test_read_chromium_file_bad_json_raises_bookmarkerror(tmp_path: Path) -> None:
    f = tmp_path / "Bookmarks"
    f.write_text("not json")
    with pytest.raises(BookmarkError):
        read_chromium_file(f)


# --------------------------------------------------------------------------- #
# IE / Edge-legacy Favorites (.url files)
# --------------------------------------------------------------------------- #
def test_parse_url_file_ansi_and_utf16() -> None:
    ansi = b"[InternetShortcut]\r\nURL=https://example.com\r\n"
    assert parse_url_file(ansi) == "https://example.com"
    utf16 = ("[InternetShortcut]\r\nURL=https://example.com\r\n").encode("utf-16")  # codec adds its own BOM
    assert parse_url_file(utf16) == "https://example.com"
    assert parse_url_file(b"[Other]\r\nX=1\r\n") is None


def test_favorites_roots_builds_nested_folders(tmp_path: Path) -> None:
    fav = tmp_path / "Favorites"
    (fav / "News").mkdir(parents=True)
    (fav / "Example.url").write_bytes(b"[InternetShortcut]\r\nURL=https://example.com\r\n")
    (fav / "News" / "BBC.url").write_bytes(b"[InternetShortcut]\r\nURL=https://bbc.co.uk\r\n")
    (fav / "News" / "bad.url").write_bytes(b"[InternetShortcut]\r\nURL=javascript:alert(1)\r\n")
    roots = favorites_roots(fav)
    assert len(roots) == 1
    assert count_links(roots) == 2  # the javascript: URL is refused
    top = roots[0]
    names = {c.title for c in top.children}
    assert "Example" in names
    news = next(c for c in top.children if isinstance(c, Folder) and c.title == "News")
    assert {c.title for c in news.children} == {"BBC"}


def test_favorites_roots_empty_folder_returns_nothing(tmp_path: Path) -> None:
    fav = tmp_path / "Favorites"
    fav.mkdir()
    assert favorites_roots(fav) == []


# --------------------------------------------------------------------------- #
# Netscape HTML writer
# --------------------------------------------------------------------------- #
def test_netscape_html_escapes_and_marks_toolbar() -> None:
    roots = [Folder("Bookmarks bar", toolbar=True, children=[
        Link("A & B <script>", "https://example.com/?a=1&b=2"), Separator(),
        Folder("Sub", children=[Link("Inner", "https://inner.example")])])]
    html = netscape_html(roots)
    assert "<!DOCTYPE NETSCAPE-Bookmark-file-1>" in html
    assert 'PERSONAL_TOOLBAR_FOLDER="true"' in html
    assert "A &amp; B &lt;script&gt;" in html
    assert "https://example.com/?a=1&amp;b=2" in html
    assert "<HR>" in html
    assert "Inner" in html


def test_count_links_ignores_folders_and_separators() -> None:
    roots = [Folder("F", children=[Link("A", "https://a"), Separator(), Folder("G", children=[])])]
    assert count_links(roots) == 1


# --------------------------------------------------------------------------- #
# Firefox places.sqlite reader
# --------------------------------------------------------------------------- #
def _make_places(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);"
        "CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, parent INTEGER, "
        " position INTEGER, title TEXT, dateAdded INTEGER, lastModified INTEGER, guid TEXT, fk INTEGER);"
    )
    con.execute("INSERT INTO moz_places (id, url) VALUES (1, 'https://example.com')")
    con.execute("INSERT INTO moz_bookmarks (id,type,parent,position,title,dateAdded,guid,fk) "
               "VALUES (1,2,0,0,'root',0,'root________',NULL)")
    con.execute("INSERT INTO moz_bookmarks (id,type,parent,position,title,dateAdded,guid,fk) "
               "VALUES (2,2,1,0,'Toolbar',0,'toolbar_____',NULL)")
    con.execute("INSERT INTO moz_bookmarks (id,type,parent,position,title,dateAdded,fk) "
               "VALUES (3,1,2,0,'Example',1704067200000000,1)")
    con.commit()
    con.close()


def test_firefox_roots_reads_toolbar_bookmark(tmp_path: Path) -> None:
    places = tmp_path / "places.sqlite"
    _make_places(places)
    roots = firefox_roots(places, scratch=tmp_path)
    assert len(roots) == 1 and roots[0].toolbar is True
    assert count_links(roots) == 1
    link = roots[0].children[0]
    assert link.title == "Example" and link.url == "https://example.com"


def test_firefox_roots_copies_via_scratch_and_keeps_source_untouched(tmp_path: Path) -> None:
    places = tmp_path / "places.sqlite"
    _make_places(places)
    original = places.read_bytes()
    firefox_roots(places, scratch=tmp_path)
    assert places.read_bytes() == original


# --------------------------------------------------------------------------- #
# browsers.py: profile discovery, safe filenames, Firefox profile registration
# --------------------------------------------------------------------------- #
def test_safe_filename_strips_unsafe_characters() -> None:
    assert safe_filename('a/b:c*d?"e<f>g|h') == "a_b_c_d_e_f_g_h"
    assert safe_filename("  .leading.dots.  ") == "leading.dots"
    assert safe_filename("") == "profile"


def test_linux_firefox_root_prefers_existing_dot_mozilla(tmp_path: Path) -> None:
    (tmp_path / ".mozilla").mkdir()
    assert linux_firefox_root(tmp_path) == tmp_path / ".mozilla" / "firefox"


def test_linux_firefox_root_uses_xdg_config_when_no_dot_mozilla(tmp_path: Path) -> None:
    assert linux_firefox_root(tmp_path, {}) == tmp_path / ".config" / "mozilla" / "firefox"


def test_register_firefox_profile_creates_first_profile_as_default(tmp_path: Path) -> None:
    root = tmp_path / "firefox"
    name, made_default = register_firefox_profile(root, "abcd.windows-import")
    assert made_default is True
    text = (root / "profiles.ini").read_text()
    assert "[Profile0]" in text and "Default=1" in text and f"Path=abcd.windows-import" in text
    # a second registration is not made default and gets a unique name
    name2, made_default2 = register_firefox_profile(root, "efgh.windows-import")
    assert made_default2 is False
    assert name2 != name


def test_firefox_running_detects_lock_file(tmp_path: Path) -> None:
    root = tmp_path / "firefox"
    prof = root / "abcd.default-release"
    prof.mkdir(parents=True)
    assert firefox_running(root) is False
    (prof / "lock").write_text("")
    assert firefox_running(root) is True


def test_chromium_profiles_finds_default_and_reports_encrypted_only(tmp_path: Path, home) -> None:
    from lindos_transfer.plan import make_context
    from lindos_transfer.sources import open_source

    profile_dir = tmp_path / "profile"
    chrome = profile_dir / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    chrome.mkdir(parents=True)
    (chrome / "Bookmarks").write_text(json.dumps(_chromium_json()))
    edge = profile_dir / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data" / "Profile 1"
    edge.mkdir(parents=True)
    (edge / "EncryptedBookmarks2").write_bytes(b"enc")

    import types

    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                               user=types.SimpleNamespace(profile_dir=profile_dir))
    profiles = chromium_profiles(ctx)
    ids = {(p.browser, p.profile, p.encrypted_only) for p in profiles}
    assert ("chrome", "Default", False) in ids
    assert ("edge", "Profile 1", True) in ids


def test_firefox_profiles_reads_profiles_ini(tmp_path: Path) -> None:
    import types

    profile_dir = tmp_path / "profile"
    ff_root = profile_dir / "AppData" / "Roaming" / "Mozilla" / "Firefox"
    (ff_root).mkdir(parents=True)
    (ff_root / "profiles.ini").write_text(
        "[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default-release\nIsRelative=1\n"
        "Path=Profiles/abcd.default-release\nDefault=1\n")
    prof_dir = ff_root / "Profiles" / "abcd.default-release"
    prof_dir.mkdir(parents=True)
    (prof_dir / "places.sqlite").write_bytes(b"data")

    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False),
                               user=types.SimpleNamespace(profile_dir=profile_dir))
    profiles = firefox_profiles(ctx)
    assert len(profiles) == 1
    assert profiles[0].name == "default-release"
    assert profiles[0].path == prof_dir
