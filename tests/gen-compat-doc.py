#!/usr/bin/env python3
"""Generate docs/COMPATIBILITY.md from the lindos-gaming compat matrix.

SPEC §10 / §12: ``/usr/share/lindos/compat-matrix.json`` (shipped by
lindos-gaming, source path ``packages/lindos-gaming/root/usr/share/lindos/
compat-matrix.json``) is the single source of truth for game compatibility.
This script renders it to Markdown; CI runs ``--check`` to make sure the
committed document is up to date.

JSON schema (SPEC §10)::

    {"entries": [
        {"game": "Valorant", "status": "not possible", "how": "-",
         "anticheat": "Riot Vanguard (kernel driver)",
         "reason": "Vanguard has no Linux build ...",
         "link": "https://areweanticheatyet.com/game/valorant"},
        ...
    ]}

A bare list of entries is accepted too.  Recognised statuses (normalised,
case-insensitive, spaces/underscores → hyphen): ``native``, ``works``,
``partial``, ``broken``, ``not-possible``; common synonyms are mapped
(``platinum``/``gold`` → works, ``silver``/``bronze`` → partial,
``borked`` → broken, ``impossible``/``no`` → not-possible).  Anything else
lands in an "Unverified" section so it is visible rather than hidden.

Exit codes: 0 ok / up to date · 1 out of date (``--check``) or write error ·
2 usage error / source missing / source invalid.

Usage::

    python3 tests/gen-compat-doc.py            # (re)write docs/COMPATIBILITY.md
    python3 tests/gen-compat-doc.py --check    # exit 1 if the doc is stale
    python3 tests/gen-compat-doc.py --stdout   # print the rendered Markdown
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOG = logging.getLogger("gen-compat-doc")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, ".."))
SOURCE_REL = os.path.join("packages", "lindos-gaming", "root", "usr", "share", "lindos",
                          "compat-matrix.json")
OUTPUT_REL = os.path.join("docs", "COMPATIBILITY.md")
DEFAULT_SOURCE = os.path.join(REPO_ROOT, SOURCE_REL)
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, OUTPUT_REL)

STATUS_ORDER: Sequence[str] = ("native", "works", "partial", "broken", "not-possible", "unknown")

STATUS_TITLES: Dict[str, str] = {
    "native": "Native Linux builds",
    "works": "Works (Proton / Wine / Linux launcher)",
    "partial": "Partial (works with caveats)",
    "broken": "Currently broken",
    "not-possible": "Not possible on Linux",
    "unknown": "Unverified",
}

STATUS_BLURBS: Dict[str, str] = {
    "native": "The developer ships a Linux build. No compatibility layer involved.",
    "works": "Runs through Proton (Steam / umu-launcher / Heroic), Wine (Lutris) or a "
             "dedicated Linux client. Performance is near-native; expect the occasional "
             "launcher quirk.",
    "partial": "Runs, but with known limitations (missing features, unstable launcher, "
               "third-party tooling, or online modes that need verifying).",
    "broken": "Does not currently work for a technical reason that could change "
              "(regressions, launcher updates). Not an anti-cheat block.",
    "not-possible": "**Will not run on any Linux distribution, including Lindos.** The "
                    "publisher either uses a kernel-level anti-cheat with no Linux build or "
                    "has explicitly disabled the Linux support of their anti-cheat. Nothing "
                    "in Lindos can change this; only the publisher can.",
    "unknown": "Entries whose status string is not one of the recognised values. Treat as "
               "unverified.",
}

STATUS_LABELS: Dict[str, str] = {
    "native": "Native",
    "works": "Works",
    "partial": "Partial",
    "broken": "Broken",
    "not-possible": "Not possible",
    "unknown": "Unverified",
}

_SYNONYMS: Dict[str, str] = {
    "native": "native", "linux-native": "native", "linux": "native",
    "works": "works", "runs": "works", "playable": "works", "ok": "works", "yes": "works",
    "gold": "works", "platinum": "works", "verified": "works", "supported": "works",
    "works-via-sober": "works",
    "partial": "partial", "silver": "partial", "bronze": "partial", "caveats": "partial",
    "mixed": "partial", "limited": "partial",
    "broken": "broken", "borked": "broken", "unstable": "broken",
    "not-possible": "not-possible", "notpossible": "not-possible", "impossible": "not-possible",
    "no": "not-possible", "blocked": "not-possible", "unsupported": "not-possible",
    "never": "not-possible", "not-supported": "not-possible",
    "unknown": "unknown", "untested": "unknown", "unverified": "unknown", "": "unknown",
}

HONESTY_NOTE = (
    "> **Reality check.** Lindos runs Windows games through Wine/Proton, a translation layer, "
    "not through Windows. Whether a *multiplayer* game runs is decided by its **anti-cheat**, "
    "and that is the publisher's decision, not ours: **Valorant (Vanguard), Fortnite (Epic "
    "disabled EAC-Linux), League of Legends (Vanguard), Apex Legends (disabled Nov 2024), "
    "Rainbow Six Siege, Destiny 2 and PUBG do not run on any Linux, including Lindos.** "
    "Roblox works through **Sober** (a Linux runtime for the Android build), not the Windows "
    "client. Minecraft Java is native. For everything else the authoritative, always-current "
    "sources are [ProtonDB](https://www.protondb.com/) and "
    "[Are We Anti-Cheat Yet?](https://areweanticheatyet.com/) — statuses below are a snapshot "
    "and can change with a single publisher update."
)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
class MatrixError(Exception):
    """Raised for a missing or malformed compat matrix."""


def normalise_status(raw: Any) -> str:
    """Map a free-form status string to one of STATUS_ORDER."""
    text = str(raw or "").strip().casefold()
    text = " ".join(text.split())
    key = text.replace("_", "-").replace(" ", "-")
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    # "works via sober", "works (proton)", "not possible (vanguard)" …
    for prefix, target in (("not-possible", "not-possible"), ("native", "native"),
                           ("partial", "partial"), ("broken", "broken"), ("works", "works")):
        if key.startswith(prefix):
            return target
    return "unknown"


def load_matrix(path: str) -> List[Dict[str, Any]]:
    """Load and validate the compat matrix, returning the entry list."""
    if not os.path.isfile(path):
        raise MatrixError(
            f"compat matrix not found: {path}\n"
            f"  (expected at {SOURCE_REL.replace(os.sep, '/')} - shipped by the "
            "lindos-gaming package). Nothing to generate/check."
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise MatrixError(f"compat matrix is not valid JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise MatrixError(f"cannot read compat matrix {path}: {exc}") from exc

    if isinstance(data, dict):
        entries = data.get("entries")
        if entries is None:
            entries = data.get("games")
    else:
        entries = data
    if not isinstance(entries, list):
        raise MatrixError(
            f"compat matrix {path}: expected an object with an \"entries\" list "
            "(or a bare list), got " + type(entries).__name__
        )

    problems: List[str] = []
    clean: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"entry #{idx}: not an object")
            continue
        game = str(entry.get("game") or entry.get("name") or "").strip()
        if not game:
            problems.append(f"entry #{idx}: missing \"game\"")
            continue
        if "status" not in entry or str(entry.get("status") or "").strip() == "":
            problems.append(f"entry #{idx} ({game}): missing \"status\"")
            continue
        key = game.casefold()
        if key in seen:
            problems.append(f"entry #{idx} ({game}): duplicate of entry #{seen[key]}")
            continue
        seen[key] = idx
        routes = entry.get("routes")
        clean.append(
            {
                "game": game,
                "status_raw": str(entry.get("status")).strip(),
                "status": normalise_status(entry.get("status")),
                "how": str(entry.get("how") or entry.get("launcher") or "").strip(),
                "anticheat": str(entry.get("anticheat") or entry.get("anti_cheat") or "").strip(),
                "reason": str(entry.get("reason") or entry.get("notes") or "").strip(),
                "link": str(entry.get("link") or entry.get("url") or "").strip(),
                # SPEC-WINDOWS §30.1 "routes" (cloud/windows/vm/verified) — optional, only
                # present on some (typically not-possible) entries; None when absent/malformed.
                "routes": routes if isinstance(routes, dict) else None,
            }
        )
    if problems:
        raise MatrixError(
            f"compat matrix {path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems)
        )
    if not clean:
        raise MatrixError(f"compat matrix {path} contains no entries")
    return clean


def load_cloud_providers(path: str) -> Dict[str, Any]:
    """Best-effort read of the top-level ``cloud_providers`` object (SPEC-WINDOWS §30.1).

    Never raises: a missing/invalid file or an absent/malformed key just means no "Other ways
    to play" provider table is rendered (the per-title route bullets still are, from
    :func:`load_matrix`'s ``routes`` field).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("cloud_providers"), dict):
        return data["cloud_providers"]
    return {}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    text = " ".join(str(text or "").split())  # collapse newlines / runs of spaces
    text = text.replace("|", "\\|")
    return text if text else "—"


def _link_cell(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "—"
    label = url
    for prefix in ("https://", "http://"):
        if label.startswith(prefix):
            label = label[len(prefix):]
    if label.startswith("www."):
        label = label[4:]
    host = label.split("/", 1)[0]
    return f"[{_cell(host)}]({url})"


def _group(entries: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {s: [] for s in STATUS_ORDER}
    for entry in entries:
        groups[entry["status"]].append(entry)
    for lst in groups.values():
        lst.sort(key=lambda e: (e["game"].casefold(), e["game"]))
    return groups


_PROVIDER_LINUX_LABELS: Dict[str, str] = {
    "official-app": "Official Linux app/client",
    "browser-unofficial": "Chrome/Edge in a browser — Linux not officially listed",
}


def _provider_availability(p: Dict[str, Any]) -> str:
    if isinstance(p.get("regions"), list) and p["regions"]:
        return "Only " + ", ".join(str(r) for r in p["regions"])
    excluded = p.get("regions_excluded")
    if isinstance(excluded, list) and excluded:
        return "Everywhere except " + ", ".join(str(r) for r in excluded)
    return "No region restriction recorded"


def _render_cloud_providers_table(providers: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    add = lines.append
    add("**Cloud providers Lindos knows about** (official apps/pages only; a provider's own real "
        "Linux support is shown as-is, never oversold):")
    add("")
    add("| Provider | Linux support | Availability | Subscription |")
    add("|---|---|---|---|")
    for pid in sorted(providers):
        p = providers[pid]
        if not isinstance(p, dict):
            continue
        linux = _PROVIDER_LINUX_LABELS.get(str(p.get("linux", "")), str(p.get("linux", "—")))
        add(
            "| "
            + " | ".join(
                (
                    f"[{_cell(p.get('name', pid))}]({p.get('url', '')})",
                    _cell(linux),
                    _cell(_provider_availability(p)),
                    _cell(p.get("subscription", "")),
                )
            )
            + " |"
        )
    add("")
    return lines


def _render_other_ways_to_play(entries: List[Dict[str, Any]], providers: Dict[str, Any]) -> List[str]:
    """SPEC-WINDOWS §30.1: an "Other ways to play" section with ``routes.verified`` shown."""
    routed = [e for e in entries if e["status"] == "not-possible" and e.get("routes")]
    if not routed:
        return []
    lines: List[str] = []
    add = lines.append
    add("## Other ways to play")
    add("")
    add(
        "Every **Not possible** title above still has the honest routes Lindos knows about: "
        "official cloud streaming — only where the provider actually carries that title and only "
        "when it is offered in your region — and a one-click restart into a Windows install "
        "already on the machine. **Never** a spoofer, and **never** the Lindos VM: every anti-cheat "
        "below also blocks virtual machines, so a VM would not make the game work, only risk a "
        "hardware ban (see [ANTI-CHEAT.md](ANTI-CHEAT.md)). Region comes from your locale, "
        "timezone or `~/.config/lindos/config.json`, or an explicit `--region` — **never** IP "
        "geolocation. Run `lindos-game route <title>` for a live check against your own machine "
        "(installed clients, dual-boot / Secure-Boot / TPM status)."
    )
    add("")
    if providers:
        lines.extend(_render_cloud_providers_table(providers))
    routed = sorted(routed, key=lambda e: (e["game"].casefold(), e["game"]))
    for e in routed:
        routes = e["routes"]
        add(f"### {e['game']}")
        add("")
        requires = routes.get("windows_requires") or []
        req_txt = f" — needs {', '.join(str(r) for r in requires)} enabled in Windows" if requires else ""
        add(f"- **Restart into Windows**{req_txt}. Boots only a Windows Boot Manager entry the "
            "firmware already has (`lindos-dualboot`); never edits Windows, BCD, Secure-Boot keys "
            "or firmware settings.")
        cloud = routes.get("cloud") or []
        if cloud:
            for c in cloud:
                pid = c.get("provider")
                pname = providers.get(pid, {}).get("name", pid) if isinstance(providers.get(pid), dict) else pid
                tier = f" ({c['tier']})" if c.get("tier") else ""
                note = f" — {c['note']}" if c.get("note") else ""
                url = c.get("url", "")
                add(f"- **{pname}**{tier}: [{_cell(url.replace('https://', '').replace('http://', ''))}]({url}){note}")
        else:
            add("- No cloud-streaming route known for this title.")
        add(f"- The Lindos Windows VM is never offered here (`vm: {str(bool(routes.get('vm'))).lower()}`).")
        verified = routes.get("verified")
        if verified:
            add(f"- _Routes verified {verified}._")
        add("")
    return lines


def render(entries: List[Dict[str, Any]], source_rel: str = SOURCE_REL,
          providers: Optional[Dict[str, Any]] = None) -> str:
    """Render the Markdown document (deterministic; LF newlines)."""
    groups = _group(entries)
    total = len(entries)
    lines: List[str] = []
    add = lines.append

    add("<!-- GENERATED FILE — DO NOT EDIT BY HAND.")
    add(f"     Source : {source_rel.replace(os.sep, '/')}")
    add("     Rebuild: python3 tests/gen-compat-doc.py        (CI runs: --check)")
    add("     Edit the JSON, then regenerate; tests/run.sh fails if this file is stale. -->")
    add("")
    add("# Lindos game compatibility matrix")
    add("")
    add(HONESTY_NOTE)
    add("")
    add("This page is generated from the same data Lindos Settings shows in **Gaming → "
        "Compatibility** (`/usr/share/lindos/compat-matrix.json`). To correct or add an "
        "entry, edit the JSON in `packages/lindos-gaming` and run `python3 "
        "tests/gen-compat-doc.py`.")
    add("")
    add("## Legend")
    add("")
    add("| Status | Meaning |")
    add("|---|---|")
    for status in STATUS_ORDER:
        add(f"| **{STATUS_LABELS[status]}** | {STATUS_BLURBS[status]} |")
    add("")
    add("## Summary")
    add("")
    counts = ", ".join(
        f"{STATUS_LABELS[s]}: {len(groups[s])}" for s in STATUS_ORDER if groups[s]
    )
    add(f"{total} titles — {counts}.")
    add("")
    add("Columns: **How** = launcher / runtime used on Lindos · **Anti-cheat** = system the "
        "game uses (blank if none) · **Reason / notes** = why it has this status and what to "
        "expect · **Link** = ProtonDB / Are We Anti-Cheat Yet / project page.")
    add("")

    for status in STATUS_ORDER:
        rows = groups[status]
        if not rows:
            continue
        add(f"## {STATUS_TITLES[status]} ({len(rows)})")
        add("")
        add(STATUS_BLURBS[status])
        add("")
        add("| Game | How | Anti-cheat | Reason / notes | Link |")
        add("|---|---|---|---|---|")
        for e in rows:
            add(
                "| "
                + " | ".join(
                    (
                        f"**{_cell(e['game'])}**",
                        _cell(e["how"]),
                        _cell(e["anticheat"]),
                        _cell(e["reason"]),
                        _link_cell(e["link"]),
                    )
                )
                + " |"
            )
        add("")

    lines.extend(_render_other_ways_to_play(entries, providers or {}))

    add("## How to read a status that is not listed here")
    add("")
    add("1. Search the game on [ProtonDB](https://www.protondb.com/) — Platinum/Gold "
        "generally means *Works*, Silver/Bronze *Partial*, Borked *Broken*.")
    add("2. If it is multiplayer, check [Are We Anti-Cheat Yet?](https://areweanticheatyet.com/) "
        "— *Denied* or *Broken* there means *Not possible* here, whatever ProtonDB says.")
    add("3. Try it: `lindos-run game.exe` (Proton-GE via umu-launcher) or enable Steam Play "
        "for all titles in Steam → Settings → Compatibility. Report results with "
        "`lindos-tune report`.")
    add("")
    add(f"_Generated from `{source_rel.replace(os.sep, '/')}` — {total} entries._")
    add("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# file handling
# --------------------------------------------------------------------------- #
def _normalise_text(text: str) -> List[str]:
    """Line list tolerant to CRLF checkouts and trailing whitespace."""
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def is_up_to_date(rendered: str, output_path: str) -> Tuple[bool, str]:
    if not os.path.isfile(output_path):
        return False, f"{output_path} does not exist"
    with open(output_path, "r", encoding="utf-8", errors="replace") as fh:
        current = fh.read()
    want = _normalise_text(rendered)
    have = _normalise_text(current)
    # Ignore trailing blank lines on either side.
    while want and want[-1] == "":
        want.pop()
    while have and have[-1] == "":
        have.pop()
    if want == have:
        return True, "up to date"
    for i, (w, h) in enumerate(zip(want, have), start=1):
        if w != h:
            return False, f"first difference at line {i}:\n  expected: {w!r}\n  found:    {h!r}"
    return False, f"line count differs (expected {len(want)}, found {len(have)})"


def write_output(rendered: str, output_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    os.replace(tmp, output_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gen-compat-doc.py",
        description="Render docs/COMPATIBILITY.md from the lindos-gaming compat matrix JSON.",
    )
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"compat-matrix.json path (default: {SOURCE_REL})")
    p.add_argument("--output", default=DEFAULT_OUTPUT,
                   help=f"Markdown output path (default: {OUTPUT_REL})")
    p.add_argument("--source-label", default=None,
                   help="path shown in the generated header (default: --source relative "
                        "to the repository root)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="do not write; exit 1 if the output file is out of date")
    mode.add_argument("--stdout", action="store_true",
                      help="print the rendered Markdown instead of writing the file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        entries = load_matrix(args.source)
    except MatrixError as exc:
        LOG.error("%s", exc)
        return 2
    providers = load_cloud_providers(args.source)

    if args.source_label:
        source_rel = args.source_label
    else:
        try:
            source_rel = os.path.relpath(os.path.abspath(args.source), REPO_ROOT)
        except ValueError:  # different drive on Windows
            source_rel = args.source
        if source_rel.startswith(".."):
            source_rel = args.source
    rendered = render(entries, source_rel=source_rel, providers=providers)
    LOG.debug("rendered %d entries from %s", len(entries), args.source)

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        ok, why = is_up_to_date(rendered, args.output)
        if ok:
            LOG.info("%s is up to date (%d entries)", args.output, len(entries))
            return 0
        LOG.error("%s is OUT OF DATE: %s\n  run: python3 tests/gen-compat-doc.py",
                  args.output, why)
        return 1

    try:
        write_output(rendered, args.output)
    except OSError as exc:
        LOG.error("cannot write %s: %s", args.output, exc)
        return 1
    LOG.info("wrote %s (%d entries)", args.output, len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
