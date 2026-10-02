"""Cross-surface guard for the anti-cheat "Not supported yet" disclaimer (SPEC 0.1, SPEC-KERNEL 14).

Lindos may say a game is *not supported yet* and that its publisher decides; it must never promise
what only a publisher can deliver ("coming soon", a date). This test keeps every user-visible surface
that names those games honest, and keeps docs/ANTI-CHEAT.md word-for-word in step with the matrix.

Two layers: the explicit SURFACES / STATES_REALITY lists below (whole-file checks), and a repo-wide,
per-block scan of every shipped file (see "Repo-wide guard") so a new or forgotten surface cannot carry
the old unqualified wording ("do not run on any Linux") without failing here.
"""
from __future__ import annotations

import ast
import functools
import html
import json
import re
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json"
APP_MAP = REPO / "packages/lindos-transfer/root/usr/share/lindos/transfer/app-map.json"

#: user-visible surfaces that name the blocked games (relative to the repo root)
SURFACES = [
    "README.md", "docs/FAQ.md", "docs/GAMING.md", "docs/ANTI-CHEAT.md", "docs/MODES.md", "docs/COMPATIBILITY.md",
    "docs/DUALBOOT.md", "docs/TRANSFER.md", "docs/WINDOWS-APPS.md",
    "packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json",
    "packages/lindos-gaming/root/usr/share/lindos/gaming/launchers.json",
    "packages/lindos-gaming/root/usr/share/lindos/gaming/profiles/valorant.json",
    "packages/lindos-gaming/root/usr/bin/lindos-game",
    "packages/lindos-gaming/DEBIAN/control",
    "packages/lindos-meta/DEBIAN/control", "packages/lindos-compat/DEBIAN/control",
    "packages/lindos-transfer/DEBIAN/control",
    "packages/lindos-compat/root/usr/share/lindos/recipes/riot-client.json",
    "packages/lindos-compat/root/usr/share/lindos/recipes/epic-games-launcher.json",
    "packages/lindos-compat/root/usr/share/lindos/compat/README.md",
    "packages/lindos-compat/root/usr/lib/lindos-compat/lindos_compat/cli_run.py",
    "packages/lindos-transfer/root/usr/lib/lindos-transfer/lindos_transfer/gui.py",
    "packages/lindos-transfer/root/usr/lib/lindos-transfer/lindos_transfer/steam.py",
    "packages/lindos-setup/root/usr/lib/lindos-setup/lindos_setup/pages.py",
    "packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/model.py",
    "packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/pages/gaming.py",
    "packages/lindos-transfer/root/usr/share/lindos/transfer/app-map.json",
    "packages/lindos-core/root/usr/share/lindos/modes/gaming/mode.json",
    "packages/lindos-desktop/root/etc/skel/.config/lindos/README",
    "build/installer/slideshow/index.html",
    "tests/gen-compat-doc.py",
]
#: CONTRIBUTING.md and SPEC*.md are deliberately absent: they quote the banned phrases to forbid them
PROMISES = ("coming soon", "will be coming", "will be supported", "arriving soon", "support is coming",
            "in the near future", "guaranteed to")

#: surfaces that state the reality in a sentence of their own and so must carry the qualifier
STATES_REALITY = [
    "README.md", "docs/FAQ.md", "docs/ANTI-CHEAT.md", "docs/GAMING.md",
    "packages/lindos-setup/root/usr/lib/lindos-setup/lindos_setup/pages.py",
    "packages/lindos-settings/root/usr/lib/lindos-settings/lindos_settings/model.py",
    "build/installer/slideshow/index.html",
    "packages/lindos-gaming/DEBIAN/control",
    "docs/MODES.md", "docs/WINDOWS-APPS.md",
    "packages/lindos-meta/DEBIAN/control", "packages/lindos-compat/DEBIAN/control",
    "packages/lindos-compat/root/usr/share/lindos/recipes/riot-client.json",
    "packages/lindos-compat/root/usr/share/lindos/recipes/epic-games-launcher.json",
    "packages/lindos-compat/root/usr/share/lindos/compat/README.md",
    "packages/lindos-core/root/usr/share/lindos/modes/gaming/mode.json",
    "packages/lindos-desktop/root/etc/skel/.config/lindos/README",
]

#: Surfaces owned by another workstream that still carry the old wording.  They stay in the lists above so
#: the guard sees them; strict xfail means the day their owner rewords them the test XPASSes and fails,
#: which is the reminder to delete the entry here.
PENDING_ELSEWHERE: Dict[str, str] = {}


def _params(rels: List[str]) -> list:
    return [pytest.param(r, marks=pytest.mark.xfail(strict=True, reason=PENDING_ELSEWHERE[r]))
            if r in PENDING_ELSEWHERE else r for r in rels]


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _flat(rel: str) -> str:
    return re.sub(r"\s+", " ", _text(rel).lower())


@pytest.mark.parametrize("rel", SURFACES)
def test_no_surface_promises_support_or_a_date(rel: str) -> None:
    low = _text(rel).lower()
    for phrase in PROMISES:
        assert phrase not in low, f"{rel}: promises what only a publisher can deliver: {phrase!r}"


@pytest.mark.parametrize("rel", _params(STATES_REALITY))
def test_surface_says_not_supported_yet_and_that_the_publisher_decides(rel: str) -> None:
    low = _flat(rel)
    assert "not supported" in low and " yet" in low, rel
    assert "publisher" in low, rel


@pytest.mark.parametrize("rel", ["build/installer/slideshow/index.html",
                                 "packages/lindos-setup/root/usr/lib/lindos-setup/lindos_setup/pages.py"])
def test_the_first_run_surfaces_also_say_there_is_no_date(rel: str) -> None:
    assert "no date" in _flat(rel), rel


def test_anti_cheat_doc_quotes_the_matrix_disclaimer_verbatim() -> None:
    long_text = json.loads(MATRIX.read_text(encoding="utf-8"))["disclaimer"]["long"]
    doc = _text("docs/ANTI-CHEAT.md")
    start = doc.index("\n## 0. ")
    m = re.search(r"((?:^>[^\n]*\n)+)", doc[start:], re.M)   # the first blockquote of section 0
    assert m, "docs/ANTI-CHEAT.md section 0 must quote the disclaimer as a blockquote"
    quoted = " ".join(line.lstrip("> ").rstrip() for line in m.group(1).splitlines())
    quoted = re.sub(r"\s+", " ", quoted.replace("`", "")).strip()   # the doc may format the command as code
    assert quoted == re.sub(r"\s+", " ", long_text).strip()


def test_the_generated_compatibility_doc_carries_the_same_paragraph() -> None:
    long_text = json.loads(MATRIX.read_text(encoding="utf-8"))["disclaimer"]["long"]
    assert long_text in _text("docs/COMPATIBILITY.md")


def _github_slug(heading: str) -> str:
    return re.sub(r" ", "-", re.sub(r"[^\w\- ]", "", heading.strip().lower()))


def test_links_to_the_anti_cheat_section_zero_resolve() -> None:
    headings = {_github_slug(h) for h in re.findall(r"^#{1,6} (.+)$", _text("docs/ANTI-CHEAT.md"), re.M)}
    linked = set()
    for rel in ("README.md", "docs/FAQ.md", "docs/GAMING.md", "docs/ANTI-CHEAT.md"):
        linked |= set(re.findall(r"(?:ANTI-CHEAT\.md)?#(0-what-not-supported[\w-]*)\)", _text(rel)))
    assert linked, "expected at least one link to ANTI-CHEAT.md section 0"
    assert linked <= headings, linked - headings


def test_transfer_notes_are_honest_and_the_xbox_app_is_not_a_yet() -> None:
    """The Steam-library/app list note: anti-cheat games say 'not supported yet', the Xbox app does not."""
    data = json.loads(APP_MAP.read_text(encoding="utf-8"))
    blocked = {a["id"]: a for entry in data["apps"] for a in entry.get("actions", []) if a.get("type") == "not_possible"}
    assert len(blocked) == 15
    for aid, action in blocked.items():
        note = action["note"]
        assert "lindos-game route" in note, aid
        low = note.lower()
        for phrase in PROMISES:
            assert phrase not in low, (aid, phrase)
        if aid == "xbox-app":
            assert "yet" not in low and "microsoft" in low and "store" in low
        else:
            assert low.startswith("not supported on lindos yet"), aid
            assert "publisher" in low and "no date" in low, aid
            assert "kernel-mode anti-cheat with no linux support" not in low, aid


# ==============================================================================================
# Repo-wide guard: every shipped file, not only the SURFACES above.
#
# A sentence that names a disclaimed title (or a kernel-driver anti-cheat) and says it cannot run
# must carry the pairing -- "not supported ... yet" and who decides ("publisher") -- and no block that
# names one may promise anything ("soon", "will run", a date).  Blocks are paragraphs, JSON string
# values, Python string constants, HTML elements or table rows, so one stray "publisher" elsewhere in
# a long file cannot vouch for a bare "do not run on any Linux".
# ==============================================================================================
SHIPPED_GLOBS = ("packages/*/DEBIAN/control", "packages/*/root/**/*", "build/installer/**/*", "README.md", "docs/**/*.md")
#: anti-cheats that are Windows kernel drivers with no Linux build (EAC/BattlEye work on Linux when a publisher opts in)
ANTI_CHEAT_NAMES = ("Vanguard", "Ricochet", "Javelin")
TITLE_ALIASES = ("GTA Online", "Rainbow Six", "Teamfight Tactics", "TFT")
#: case-sensitive because they are ordinary words or acronyms otherwise
CASE_SENSITIVE = {"Rust", "TFT"}

_NON_TEXT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".gz", ".xz", ".zip", ".woff", ".woff2", ".ttf",
                      ".otf", ".pyc", ".mo", ".deb"}


def _matrix_titles() -> List[str]:
    entries = json.loads(MATRIX.read_text(encoding="utf-8"))["entries"]
    return [re.split(r"\s*[(:]", e["game"])[0].strip() for e in entries if e.get("unsupported_kind")]


ALL_NAMES = sorted(set(_matrix_titles()) | set(TITLE_ALIASES) | set(ANTI_CHEAT_NAMES), key=len, reverse=True)


def _names_regex() -> "re.Pattern[str]":
    parts = []
    for name in ALL_NAMES:
        body = r"\b" + re.escape(name).replace(r"\ ", r"\s+") + r"\b"
        parts.append(f"(?-i:{body})" if name in CASE_SENSITIVE else body)
    return re.compile("|".join(parts), re.I)


NAMES_RE = _names_regex()
#: what the old, unqualified wording looked like: a flat statement that the title cannot run
UNSUPPORTED_RE = re.compile(
    r"any linux|(?:cannot|can't|can not|will not|won't|does not|do not|did not|never) (?:even )?run\b|not run on|"
    r"\bimpossible\b|\bnot possible\b|no linux (?:version|build|support)|(?:cannot|can't) (?:work|be played)|"
    r"(?:does|do) not work|won't work|not supported|unsupported|(?:does|do) not support", re.I)
#: promises only a publisher can keep; "will list" is the established formula and is deliberately not here
PROMISE_RE = re.compile(
    r"coming soon|will be coming|will be supported|will be added|will be available|will be playable|arriving soon|"
    r"support is coming|in the near future|guaranteed to|\bsoon\b|\beventually\b|\bone day\b|\bsomeday\b|"
    r"\bin the future\b|\bupcoming\b|\bplanned for\b|"
    r"\bwill (?:also |soon |eventually |then |definitely |finally )?(?:run|work|support|launch|arrive|land|come|be able)\b|"
    r"\b(?:by|before|until|starting|from|later in|end of)\s+(?:(?:early|mid|late) )?20(?:2[6-9]|[3-9]\d)\b|"
    r"\b(?:in|during|as of)\s+(?:(?:early|mid|late) |q[1-4] |h[12] )?20(?:2[7-9]|[3-9]\d)\b|"
    r"\b(?:q[1-4]|h[12])\s*20(?:2[6-9]|[3-9]\d)\b|"
    r"\b(?:next|this) (?:year|month|summer|winter|spring|autumn|fall|quarter)\b", re.I)

#: (path, distinctive text of the block) -> why that block may name a title without the pairing.  Explicit on
#: purpose: every entry must still match a real block (test_the_allow_list_has_no_stale_entries).
_VM_ONLY = ("says only that VM-blocking anti-cheat refuses to start inside the Lindos VM (VM-specific and still "
            "true, docs/VM.md); it makes no claim about Linux support, so it takes no 'yet'")
ALLOWED_UNPAIRED: Dict[Tuple[str, str], str] = {
    ("packages/lindos-vm/DEBIAN/control", "will NOT run in this VM"): _VM_ONLY,
    ("packages/lindos-vm/root/usr/bin/lindos-vm", "will NOT run here"): _VM_ONLY,
    ("packages/lindos-vm/root/usr/bin/lindos-vm", "Note: VM-blocking anti-cheat (Vanguard etc.) will NOT run in this VM"):
        _VM_ONLY,
}

#: what pairs a block: prose needs "not supported ... yet" and the publisher; a table row needs the badge text; a
#: matrix entry with `unsupported_kind` is rendered with the badge, so the data field itself is the pairing
PROSE, ROW, BADGED = "prose", "row", "badged"
Block = Tuple[str, str]   # (text, PROSE | ROW | BADGED)


def _json_blocks(node: object, kind: str = PROSE) -> Iterator[Block]:
    if isinstance(node, str):
        yield from ((b, kind) for b in re.split(r"\n\s*\n|\n", node))
    elif isinstance(node, dict):
        kind = BADGED if node.get("unsupported_kind") else kind
        for value in node.values():
            yield from _json_blocks(value, kind)
    elif isinstance(node, list):
        for value in node:
            yield from _json_blocks(value, kind)


def _python_blocks(text: str) -> "List[Block] | None":
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: List[Block] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out += [(b, PROSE) for b in re.split(r"\n\s*\n", node.value)]
    return out


def _html_blocks(text: str) -> List[Block]:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)</?(?:p|div|li|ul|ol|h[1-6]|section|article|br|tr|td|th|table)\b[^>]*>", "\n\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return [(b, PROSE) for b in re.split(r"\n\s*\n", text)]


def _text_blocks(rel: str, text: str) -> List[Block]:
    sep = r"\n \.\n|\n\s*\n" if rel.endswith("/control") else r"\n\s*\n"   # Debian descriptions use " ." lines
    out: List[Block] = []
    for para in re.split(sep, text):
        if rel.endswith(".md") and para.lstrip().startswith("|"):
            out += [(line, ROW) for line in para.splitlines()]
        else:
            out.append((para, PROSE))
    return out


def _blocks(rel: str, text: str) -> List[Block]:
    if rel.endswith(".json"):
        try:
            return list(_json_blocks(json.loads(text)))
        except ValueError:
            pass
    if rel.endswith(".py") or text.startswith("#!") and "python" in text.split("\n", 1)[0]:
        found = _python_blocks(text)
        if found is not None:
            return found
    if rel.endswith((".html", ".htm")):
        return _html_blocks(text)
    return _text_blocks(rel, text)


@functools.lru_cache(maxsize=None)
def _shipped_files() -> Tuple[str, ...]:
    found = set()
    for pattern in SHIPPED_GLOBS:
        for path in REPO.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() not in _NON_TEXT_SUFFIXES:
                found.add(path.relative_to(REPO).as_posix())
    return tuple(sorted(found))


@functools.lru_cache(maxsize=None)
def _read_shipped(rel: str) -> "str | None":
    try:
        text = (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    return None if "\0" in text else text


def _norm(block: str) -> str:
    block = re.sub(r"(?m)^[ \t]*(?:>[ \t]*)+", "", block)   # a Markdown quote marker must not split a phrase
    return re.sub(r"\s+", " ", block).strip()


def _carries_pairing(flat: str, kind: str) -> bool:
    if kind == BADGED:
        return True
    low = flat.lower()
    says = re.search(r"not supported|(?:does|do) not support", low) and re.search(r"(?<![a-z])yet(?![a-z])", low)
    return bool(says and (kind == ROW or "publisher" in low))


def _unpaired(block: str, kind: str = PROSE) -> bool:
    flat = _norm(block)
    return bool(NAMES_RE.search(flat) and UNSUPPORTED_RE.search(flat) and not _carries_pairing(flat, kind))


def _promises(block: str) -> "re.Match[str] | None":
    flat = _norm(block)
    return PROMISE_RE.search(flat) if NAMES_RE.search(flat) else None


@functools.lru_cache(maxsize=None)
def _naming_files() -> Tuple[str, ...]:
    """The shipped files that name a disclaimed title (a cheap substring pass first: the regex is slow on big files)."""
    out = []
    for rel in _shipped_files():
        text = _read_shipped(rel)
        flat = _norm(text) if text is not None else ""
        low = flat.lower()
        if any((n in flat) if n in CASE_SENSITIVE else (n.lower() in low) for n in ALL_NAMES) and NAMES_RE.search(flat):
            out.append(rel)
    return tuple(out)


def _allowed(rel: str, flat: str) -> bool:
    return any(rel == a_rel and a_text in flat for (a_rel, a_text) in ALLOWED_UNPAIRED)


def _unpaired_blocks(rel: str) -> List[str]:
    text = _read_shipped(rel) or ""
    flats = [(_norm(block), kind) for block, kind in _blocks(rel, text)]
    return [flat[:200] for flat, kind in flats if _unpaired(flat, kind) and not _allowed(rel, flat)]


def test_the_scan_sees_the_files_and_titles_it_is_meant_to_guard() -> None:
    files = _naming_files()
    assert len(files) >= 25, files
    for expected in ("README.md", "docs/ANTI-CHEAT.md", "docs/COMPATIBILITY.md", "packages/lindos-meta/DEBIAN/control",
                     "packages/lindos-compat/root/usr/share/lindos/recipes/riot-client.json",
                     "packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json"):
        assert expected in files, expected
    titles = _matrix_titles()
    assert len(titles) == 14 and "Valorant" in titles and "Rust" in titles and "Xbox app" not in " ".join(titles)


@pytest.mark.parametrize("rel", _params(list(_naming_files())))
def test_every_shipped_statement_that_a_title_cannot_run_carries_the_pairing(rel: str) -> None:
    bad = _unpaired_blocks(rel)
    assert not bad, (f"{rel}: names a disclaimed title as unsupported without 'not supported ... yet' and who "
                     f"decides (the publisher): {bad}")


@pytest.mark.parametrize("rel", _naming_files())
def test_no_shipped_block_naming_a_title_promises_anything(rel: str) -> None:
    text = _read_shipped(rel) or ""
    low = text.lower()
    for phrase in PROMISES:
        assert phrase not in low, f"{rel}: promises what only a publisher can deliver: {phrase!r}"
    for block, _kind in _blocks(rel, text):
        hit = _promises(block)
        assert hit is None, f"{rel}: promise {hit.group(0)!r} in a block that names a disclaimed title: {_norm(block)[:200]}"


def test_the_allow_list_has_no_stale_entries() -> None:
    for (rel, needle), why in ALLOWED_UNPAIRED.items():
        assert why.strip(), (rel, needle)
        text = _read_shipped(rel)
        assert text is not None, f"allow-listed file is gone: {rel}"
        assert any(_unpaired(b, kind) and needle in _norm(b) for b, kind in _blocks(rel, text)), (
            f"{rel}: '{needle}' no longer matches an unpaired block - remove the entry")


@pytest.mark.parametrize("sample", [
    "Valorant and Fortnite do not run on any Linux.",
    "Valorant and League of Legends cannot run on Lindos or any Linux.",
    "Fortnite stays impossible (EAC-Linux disabled by Epic).",
    "Not possible on any Linux: Riot Vanguard is a Windows-only kernel anti-cheat.",
    "Valorant (Vanguard) will NOT run on Lindos.",
    "Games whose anti-cheat does not run on any Linux (Valorant, Call of Duty) have a route.",
    "Valorant does not run on any Linux and is not supported on Lindos yet.",      # 'yet' but nobody decides
    "Valorant does not run on any Linux and is not supported. Up to its publisher.",   # a publisher but no 'yet'
    "| Valorant | Does not run on any Linux distribution |",                     # a table row with no badge
])
def test_the_guard_flags_the_old_unqualified_wording(sample: str) -> None:
    assert _unpaired(sample), sample


@pytest.mark.parametrize("sample", [
    "Valorant does not run on any Linux today, so it is not supported on Lindos yet; that is up to its publisher, "
    "no date is given.",
    "Fortnite and Apex Legends do not run on any Linux today, so they are not supported on Lindos yet: that is up "
    "to their publishers.",
    "| **Valorant** · _Not supported yet_ | Vanguard has no Linux build. Does not run on any Linux distribution |",
    "Counter-Strike 2 runs natively; Elden Ring works through Proton.",         # names no disclaimed title
    "Wine cannot run kernel drivers.",                                          # names no disclaimed title
])
def test_the_guard_accepts_the_established_formula_and_unrelated_text(sample: str) -> None:
    assert not _unpaired(sample, ROW if sample.startswith("|") else PROSE), sample


@pytest.mark.parametrize("sample", [
    "Valorant is coming soon to Lindos.",
    "Fortnite will be supported once Epic agrees.",
    "Fortnite will run on Lindos.",
    "Valorant will also work on Lindos.",
    "Apex Legends support arrives in Q3 2027.",
    "We expect League of Legends by 2027.",
    "Destiny 2 will be playable in 2028.",
    "Rust support is planned for next year.",
    "Support for Valorant will be added eventually.",
    "Fortnite could work one day, and soon.",
])
def test_the_guard_flags_promises_and_dates_next_to_a_title(sample: str) -> None:
    assert _promises(sample), sample


@pytest.mark.parametrize("sample", [
    "Valorant will not run on Lindos; Lindos will list it once its publisher enables Linux and it has been tested.",
    "Valorant does not run on any Linux today; no date is given.",
    "League of Legends ran under Wine until Riot made Vanguard mandatory in 2024.",
    "Tarkov 1.0 released on Steam on 2025-11-15; Apex Legends moves to Javelin on 2026-09-29.",
    "Coming soon: a faster installer.",                                          # names no disclaimed title
])
def test_the_guard_leaves_honest_wording_and_history_alone(sample: str) -> None:
    assert not _promises(sample), sample


def test_the_modes_doc_quotes_the_gaming_mode_notes_verbatim() -> None:
    notes = json.loads(_text("packages/lindos-core/root/usr/share/lindos/modes/gaming/mode.json"))["notes"]
    doc = re.sub(r"\s+", " ", _text("docs/MODES.md"))
    assert re.sub(r"\s+", " ", notes).strip() in doc.replace("* ", "").replace("*", ""), (
        "docs/MODES.md must quote the gaming mode.json notes word for word")
