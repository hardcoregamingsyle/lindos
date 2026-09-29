#!/usr/bin/env python3
"""rebrand-base.py - keep the Linux Mint base from showing through the Lindos desktop.

Lindos is a remaster of Linux Mint, so the base packages ship menu entries, autostarts, release
files and browser defaults that say "Linux Mint".  This script (stdlib only, offline, idempotent)
puts Lindos over the *visible* parts.  It edits files that other packages own, so it runs again
after every apt run (/etc/apt/apt.conf.d/99lindos-branding -> apply-branding.sh --files-only).

Steps, all driven by /usr/share/lindos/branding/base-sweep.json:
  applications  /usr/share/applications/*.desktop: hide the Mint duplicates of Lindos tools, swap
                Mint-looking icons, say "Lindos" where a visible text says "Linux Mint", hide
                entries that only open Linux Mint web pages
  autostart     /etc/xdg/autostart/*.desktop: hide the Mint Welcome window (lindos-setup replaces it)
  files         KEY=value files (/etc/lsb-release, /etc/linuxmint/info, /etc/casper.conf): display
                fields only - IDs, release numbers and codenames are never touched
  firefox       homepage / welcome-page prefs, enterprise policies and distribution.ini that point
                at Linux Mint's own pages

Never touched: ID / ID_LIKE / codenames, apt sources, package and executable names.  Every edit is
guarded (a missing file is skipped), backed up once under /var/lib/lindos/rebrand/orig and can be
undone with --revert.  Exit status is 0 unless the command line is wrong: this must never fail a
package operation.

Usage: rebrand-base.py [--root DIR] [--data FILE] [--fragment FILE] [--steps a,b] [--dry-run]
                       [--quiet] [--revert] [--audit]
Env:   LINDOS_ROOT  prefix for every system path (tests); --root wins.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional, Tuple

DATA_PATH = "/usr/share/lindos/branding/base-sweep.json"
FRAGMENT_PATH = "/usr/share/lindos/os-release.d/lindos.conf"
BACKUP_DIR = "/var/lib/lindos/rebrand/orig"
MARKER = "X-Lindos-Rebranded"
STEPS = ("applications", "autostart", "files", "firefox")
MAX_TEXT_BYTES = 512 * 1024

MINT_RE = re.compile(r"linux[ _-]?mint", re.IGNORECASE)
MINT_ANY_RE = re.compile(r"linux[ _-]?mint|linuxmint", re.IGNORECASE)
MINT_THEME_RE = re.compile(r"\bMint-[XYL]\b|\bmint-[xyl]-icons\b|\bMint-[XY]-[A-Z][a-z]+\b")
_KEY_RE = re.compile(r"^([A-Za-z0-9-]+)(\[[^\]]*\])?(\s*=\s*)(.*)$")
_KV_RE = re.compile(r"^(?P<pre>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.*?)(?P<post>\s*)$")
VISIBLE_KEYS = ("Name", "GenericName", "Comment", "Keywords")
LOCALISED_KEYS = ("Name", "GenericName", "Comment", "Keywords")


# --------------------------------------------------------------------------- context / io
class Ctx:
    """Where to work (a root prefix for tests) and how loudly."""

    def __init__(self, root: str = "", dry_run: bool = False, quiet: bool = False) -> None:
        self.root = root.rstrip("/\\") if root and root not in ("/", "\\") else ""
        self.dry_run = dry_run
        self.quiet = quiet
        self.changes = 0
        self.problems = 0

    def path(self, absolute: str) -> str:
        return self.root + absolute

    def log(self, msg: str) -> None:
        if not self.quiet:
            sys.stderr.write("rebrand-base: %s\n" % msg)

    def warn(self, msg: str) -> None:
        self.problems += 1
        sys.stderr.write("rebrand-base: WARNING: %s\n" % msg)


def read_text(path: str) -> Optional[str]:
    """The file's text (bytes preserved), or None for a missing/symlinked/huge/unreadable file."""
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            return fh.read()
    except OSError:
        return None


def _write_raw(path: str, text: str) -> None:
    tmp = path + ".lindos-tmp"
    try:
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(text)
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def backup_file(ctx: Ctx, rel: str) -> str:
    return ctx.path(BACKUP_DIR) + "/" + urllib.parse.quote(rel, safe="")


def save_backup(ctx: Ctx, rel: str, text: str, refresh: bool) -> None:
    """Keep the untouched original once (refresh=True: it is the base's new, not yet branded version)."""
    dst = backup_file(ctx, rel)
    if ctx.dry_run or (os.path.exists(dst) and not refresh):
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _write_raw(dst, text)
    except OSError as exc:
        ctx.warn("cannot save a backup of %s: %s" % (rel, exc))


def commit(ctx: Ctx, rel: str, old: str, new: str, backup_refresh: bool) -> bool:
    """Write *new* over *rel* (after saving *old*); True when the file now holds *new*."""
    if new == old:
        return False
    ctx.changes += 1
    if ctx.dry_run:
        ctx.log("would change %s" % rel)
        return True
    save_backup(ctx, rel, old, backup_refresh)
    try:
        _write_raw(ctx.path(rel), new)
    except OSError as exc:
        ctx.warn("cannot write %s: %s" % (rel, exc))
        return False
    ctx.log("changed %s" % rel)
    return True


# --------------------------------------------------------------------------- .desktop entries
def _entry_range(lines: List[str]) -> Tuple[int, int]:
    start = -1
    for i, ln in enumerate(lines):
        if ln.strip() == "[Desktop Entry]":
            start = i
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("[") and s.endswith("]"):
            end = i
            break
    return start, end


def get_key(text: str, key: str) -> Optional[str]:
    """Value of an un-localised key in the [Desktop Entry] group."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    start, end = _entry_range(lines)
    if start < 0:
        return None
    for ln in lines[start + 1:end]:
        m = _KEY_RE.match(ln)
        if m and m.group(1) == key and not m.group(2):
            return m.group(4).strip()
    return None


def is_marked(text: str) -> bool:
    return re.search(r"^%s\s*=\s*true\s*$" % re.escape(MARKER), text, flags=re.M) is not None


def patch_desktop_entry(text: str, *, hide: Optional[str] = None, set_keys: Optional[Dict[str, str]] = None,
                        scrub: bool = True, brand: str = "Lindos") -> Tuple[str, bool]:
    """Return (new_text, changed) for one .desktop file.

    hide      "NoDisplay" (menu entries) or "Hidden" (autostart) -> that key becomes true
    set_keys  KEY -> value replaced/added in [Desktop Entry] (localised variants of KEY are dropped)
    scrub     rewrite "Linux Mint" to *brand* in the visible text keys of every group
    A changed file also gets X-Lindos-Rebranded=true.  Running the result through again is a no-op.
    """
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    start, end = _entry_range(lines)
    if start < 0:
        return text, False
    changed = False

    if scrub:
        for i, ln in enumerate(lines):
            m = _KEY_RE.match(ln)
            if m and m.group(1) in VISIBLE_KEYS and MINT_RE.search(m.group(4)):
                lines[i] = ln[:m.start(4)] + MINT_RE.sub(brand, m.group(4))
                changed = True

    def find_key(key: str) -> int:
        for i in range(start + 1, end):
            m = _KEY_RE.match(lines[i])
            if m and m.group(1) == key and not m.group(2):
                return i
        return -1

    def set_key(key: str, value: str) -> None:
        nonlocal end, changed
        i = find_key(key)
        if i >= 0:
            m = _KEY_RE.match(lines[i])
            assert m is not None
            if m.group(4).strip() == value:
                return
            lines[i] = "%s%s%s%s" % (m.group(1), m.group(2) or "", m.group(3), value)
            changed = True
            return
        last = end - 1
        while last > start and not lines[last].strip():
            last -= 1
        lines.insert(last + 1, "%s=%s" % (key, value))
        end += 1
        changed = True

    if hide:
        set_key(hide, "true")
    for key, value in (set_keys or {}).items():
        set_key(key, value)
        if key in LOCALISED_KEYS:
            for i in range(end - 1, start, -1):
                m = _KEY_RE.match(lines[i])
                if m and m.group(1) == key and m.group(2):
                    del lines[i]
                    end -= 1
                    changed = True

    if changed and not any(re.match(r"^%s\s*=" % re.escape(MARKER), ln) for ln in lines[start + 1:end]):
        lines.insert(start + 1, "%s=true" % MARKER)
    return nl.join(lines), changed


def sweep_desktop_dir(ctx: Ctx, section: Dict[str, Any], brand: str, kind: str) -> int:
    """Apply the rules of one section ("applications" | "autostart") to every entry in its dirs."""
    hide_key = "Hidden" if kind == "autostart" else "NoDisplay"
    hide_names = set(section.get("hide") or [])
    hide_res = [re.compile(p, re.IGNORECASE) for p in (section.get("hide_if_exec_matches") or [])]
    set_map = section.get("set") or {}
    skip = tuple(section.get("skip_prefixes") or [])
    done = 0
    for d in section.get("dirs") or []:
        try:
            names = sorted(os.listdir(ctx.path(d)))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop") or (skip and name.startswith(skip)):
                continue
            rel = d.rstrip("/") + "/" + name
            text = read_text(ctx.path(rel))
            if text is None:
                continue
            exec_line = get_key(text, "Exec") or ""
            hide = name in hide_names or any(r.search(exec_line) for r in hide_res)
            new, changed = patch_desktop_entry(text, hide=hide_key if hide else None,
                                               set_keys=set_map.get(name), scrub=True, brand=brand)
            if changed and commit(ctx, rel, text, new, backup_refresh=not is_marked(text)):
                done += 1
    return done


# --------------------------------------------------------------------------- KEY=value files
def patch_kv(text: str, updates: Dict[str, str]) -> Tuple[str, bool]:
    """Set existing KEY=value lines (optional 'export '; the line's own quoting is kept as it is, other
    parsers already cope with it); keys that are absent stay absent."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    changed = False
    for i, ln in enumerate(lines):
        m = _KV_RE.match(ln)
        if not m or m.group("key") not in updates:
            continue
        val = m.group("val")
        quote = val[0] if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0] else ""
        current = val[1:-1] if quote else val
        want = updates[m.group("key")]
        if current == want:
            continue
        lines[i] = "%s%s=%s%s%s%s" % (m.group("pre"), m.group("key"), quote, want, quote, m.group("post"))
        changed = True
    return nl.join(lines), changed


def sweep_files(ctx: Ctx, rules: Dict[str, Any], brand: Dict[str, str]) -> int:
    done = 0
    for spec in rules.get("files") or []:
        rel = spec.get("path") or ""
        updates = {k: str(v).format_map(brand) for k, v in (spec.get("set") or {}).items()}
        if not rel or not updates:
            continue
        text = read_text(ctx.path(rel))
        if text is None:
            continue
        new, changed = patch_kv(text, updates)
        if changed and commit(ctx, rel, text, new, backup_refresh=MINT_ANY_RE.search(text) is not None):
            done += 1
    return done


# --------------------------------------------------------------------------- Firefox
_PREF_RE = re.compile(
    r"""^(?P<head>\s*(?:user_pref|pref|defaultPref|lockPref|sticky_pref)\(\s*(?P<q1>["'])(?P<name>[^"']+)(?P=q1)\s*,\s*)"""
    r"""(?P<q2>["'])(?P<val>.*?)(?P=q2)(?P<tail>\s*\).*)$""")
_FF_EMPTY_PREFS = ("startup.homepage_welcome_url", "startup.homepage_welcome_url.additional",
                   "startup.homepage_override_url")


def _is_mint(value: Any) -> bool:
    return isinstance(value, str) and MINT_ANY_RE.search(value) is not None


def patch_prefs_js(text: str, homepage: str) -> Tuple[str, bool]:
    """Point Firefox's homepage / welcome-page prefs away from linuxmint.com (only those prefs)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    changed = False
    for i, ln in enumerate(lines):
        m = _PREF_RE.match(ln)
        if not m or not _is_mint(m.group("val")):
            continue
        name = m.group("name")
        if name == "browser.startup.homepage":
            new_val = homepage
        elif name in _FF_EMPTY_PREFS:
            new_val = ""
        else:
            continue
        q = m.group("q2")
        lines[i] = "%s%s%s%s%s" % (m.group("head"), q, new_val, q, m.group("tail"))
        changed = True
    return nl.join(lines), changed


def _filter_bookmarks(items: Any) -> Tuple[Any, bool]:
    if not isinstance(items, list):
        return items, False
    out: List[Any] = []
    changed = False
    for it in items:
        if isinstance(it, dict):
            if _is_mint(it.get("URL")) or _is_mint(it.get("url")) or _is_mint(it.get("Title")) or _is_mint(it.get("name")):
                changed = True
                continue
            if "children" in it:
                kids, kid_changed = _filter_bookmarks(it["children"])
                if kid_changed:
                    it = dict(it, children=kids)
                    changed = True
        out.append(it)
    return out, changed


def patch_policies(text: str, homepage: str) -> Tuple[str, bool]:
    """Remove Linux Mint's start page, bookmarks and search engine from a policies.json (invalid JSON: untouched)."""
    try:
        data = json.loads(text)
    except ValueError:
        return text, False
    pol = data.get("policies") if isinstance(data, dict) else None
    if not isinstance(pol, dict):
        return text, False
    changed = False
    hp = pol.get("Homepage")
    if isinstance(hp, dict):
        if _is_mint(hp.get("URL")):
            hp["URL"] = homepage
            changed = True
        if isinstance(hp.get("Additional"), list):
            keep = [u for u in hp["Additional"] if not _is_mint(u)]
            if len(keep) != len(hp["Additional"]):
                hp["Additional"] = keep
                changed = True
    for key in ("Bookmarks", "ManagedBookmarks"):
        if key in pol:
            new, did = _filter_bookmarks(pol[key])
            if did:
                pol[key] = new
                changed = True
    for key in ("OverrideFirstRunPage", "OverridePostUpdatePage"):
        if _is_mint(pol.get(key)):
            pol[key] = ""
            changed = True
    se = pol.get("SearchEngines")
    if isinstance(se, dict):
        if isinstance(se.get("Add"), list):
            keep = [e for e in se["Add"] if not (isinstance(e, dict) and (_is_mint(e.get("Name")) or _is_mint(e.get("URLTemplate"))))]
            if len(keep) != len(se["Add"]):
                se["Add"] = keep
                changed = True
        if _is_mint(se.get("Default")):
            del se["Default"]
            changed = True
    if not changed:
        return text, False
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", True


def patch_distribution_ini(text: str, homepage: str, brand: str) -> Tuple[str, bool]:
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    changed = False
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*about\s*=\s*)(.*)$", ln)
        if m and MINT_RE.search(m.group(2)):
            lines[i] = m.group(1) + MINT_RE.sub(brand, m.group(2))
            changed = True
            continue
        m = re.match(r"^(\s*(browser\.startup\.homepage|startup\.homepage_welcome_url(?:\.additional)?|startup\.homepage_override_url)\s*=\s*)(.*)$", ln)
        if m and _is_mint(m.group(3)):
            lines[i] = m.group(1) + (homepage if m.group(2) == "browser.startup.homepage" else "")
            changed = True
    return nl.join(lines), changed


def _firefox_files(ctx: Ctx, dirs: List[str]) -> Iterator[Tuple[str, str]]:
    """(relative path, kind) of the small config files Firefox reads its defaults from."""
    for d in dirs:
        base = ctx.path(d)
        if not os.path.isdir(base):
            continue
        for cur, subdirs, files in os.walk(base):
            rel_dir = os.path.relpath(cur, base).replace("\\", "/")
            if rel_dir != "." and len(rel_dir.split("/")) >= 5:
                subdirs[:] = []
            subdirs.sort()
            parent = os.path.basename(cur)
            for name in sorted(files):
                rel = d.rstrip("/") + "/" + os.path.relpath(os.path.join(cur, name), base).replace("\\", "/")
                if name == "policies.json":
                    yield rel, "policies"
                elif name == "distribution.ini":
                    yield rel, "ini"
                elif name.endswith((".js", ".cfg")) and (parent in ("preferences", "pref", "defaults")
                                                         or name in ("syspref.js", "mozilla.cfg", "autoconfig.js")):
                    yield rel, "prefs"


def sweep_firefox(ctx: Ctx, rules: Dict[str, Any], brand: Dict[str, str]) -> int:
    section = rules.get("firefox") or {}
    homepage = section.get("homepage") or "about:home"
    done = 0
    for rel, kind in _firefox_files(ctx, section.get("dirs") or []):
        text = read_text(ctx.path(rel))
        if text is None or not MINT_ANY_RE.search(text):
            continue
        if kind == "policies":
            new, changed = patch_policies(text, homepage)
        elif kind == "ini":
            new, changed = patch_distribution_ini(text, homepage, brand["name"])
        else:
            new, changed = patch_prefs_js(text, homepage)
        if changed and commit(ctx, rel, text, new, backup_refresh=True):
            done += 1
    return done


# --------------------------------------------------------------------------- brand + rules
def _parse_kv_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    text = read_text(path)
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_brand(ctx: Ctx, fragment: Optional[str]) -> Dict[str, str]:
    """Display strings for templates, from the os-release fragment apply-branding.sh already uses."""
    frag = _parse_kv_file(fragment or ctx.path(FRAGMENT_PATH))
    name = frag.get("NAME") or "Lindos"
    version = frag.get("LINDOS_VERSION") or "1.0.0"
    short = ".".join(version.split(".")[:2])
    return {
        "name": name,
        "version": version,
        "version_short": short,
        "title": "%s %s" % (name, short),
        "description": frag.get("PRETTY_NAME") or "%s %s (%s)" % (name, short, frag.get("LINDOS_CODENAME") or "Aurora"),
    }


def load_rules(ctx: Ctx, data_path: Optional[str]) -> Optional[Dict[str, Any]]:
    path = data_path or ctx.path(DATA_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        ctx.warn("cannot read the sweep rules %s: %s" % (path, exc))
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- revert + audit
def revert(ctx: Ctx) -> int:
    """Put back every file this script saved a backup of (desktop entries only while still branded)."""
    bdir = ctx.path(BACKUP_DIR)
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        ctx.log("nothing to revert")
        return 0
    restored = 0
    for name in names:
        rel = urllib.parse.unquote(name)
        target = ctx.path(rel)
        saved = read_text(os.path.join(bdir, name))
        if saved is None:
            continue
        current = read_text(target)
        ours = current is not None and (not rel.endswith(".desktop") or is_marked(current))
        if ours and current != saved:
            if not ctx.dry_run:
                try:
                    _write_raw(target, saved)
                except OSError as exc:
                    ctx.warn("cannot restore %s: %s" % (rel, exc))
                    continue
            restored += 1
            ctx.log("restored %s" % rel)
        if not ctx.dry_run:
            try:
                os.remove(os.path.join(bdir, name))
            except OSError:
                pass
    ctx.log("reverted %d file(s)" % restored)
    return restored


def audit(ctx: Ctx, rules: Optional[Dict[str, Any]]) -> int:
    """Print (stdout) every place that still says Linux Mint, plus Mint-named art/themes; returns the count."""
    found = 0

    def hit(rel: str, why: str) -> None:
        nonlocal found
        found += 1
        sys.stdout.write("audit: %s: %s\n" % (rel, why.strip()[:140]))

    def scan_file(rel: str) -> None:
        if rel.endswith(".lindos-orig"):        # the base's originals that lindos-desktop diverted away
            return
        text = read_text(ctx.path(rel))
        if text is None:
            return
        for ln in text.splitlines():
            if (MINT_ANY_RE.search(ln) or MINT_THEME_RE.search(ln)) and not ln.lstrip().startswith(("#", "//")):
                hit(rel, ln)
                return

    for d in ("/usr/share/applications", "/etc/xdg/autostart"):
        try:
            names = sorted(os.listdir(ctx.path(d)))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".desktop"):
                continue
            text = read_text(ctx.path(d + "/" + n))
            if text is None:
                continue
            if re.search(r"^(NoDisplay|Hidden)\s*=\s*true\s*$", text, flags=re.M | re.I):
                continue
            for ln in text.splitlines():
                m = _KEY_RE.match(ln)
                if m and ((m.group(1) in VISIBLE_KEYS and re.search(r"\bmint\b", m.group(4), re.I))
                          or (m.group(1) == "Icon" and m.group(4).lower().startswith("mint"))):
                    hit(d + "/" + n, ln)
                    break

    for rel in ("/etc/issue", "/etc/issue.net", "/etc/motd", "/etc/lsb-release", "/etc/os-release", "/usr/lib/os-release",
                "/etc/linuxmint/info", "/etc/casper.conf", "/etc/default/grub", "/etc/hostname"):
        scan_file(rel)
    for d in ("/etc/update-motd.d", "/etc/default/grub.d", "/etc/lightdm", "/etc/skel", "/etc/xdg", "/etc/gtk-3.0", "/etc/dconf",
              "/usr/lib/firefox/distribution", "/usr/lib/firefox/defaults", "/usr/lib/firefox/browser/defaults", "/etc/firefox"):
        base = ctx.path(d)
        for cur, subdirs, files in os.walk(base):
            subdirs.sort()
            for f in sorted(files):
                scan_file(os.path.join(cur, f)[len(ctx.root):].replace("\\", "/"))
    # GSettings defaults the base ships (theme / wallpaper names); Flatpak and GNOME-style apps read these
    schemas = "/usr/share/glib-2.0/schemas"
    try:
        overrides = sorted(n for n in os.listdir(ctx.path(schemas)) if n.endswith(".override"))
    except OSError:
        overrides = []
    for n in overrides:
        scan_file(schemas + "/" + n)
    for d in ("/usr/share/backgrounds", "/usr/share/plymouth/themes", "/usr/share/icons", "/usr/share/themes",
              "/usr/share/pixmaps", "/usr/share/slick-greeter", "/usr/share/lightdm"):
        try:
            names = sorted(os.listdir(ctx.path(d)))
        except OSError:
            continue
        for n in names:
            if re.search(r"mint", n, re.I):
                hit(d + "/" + n, "name mentions mint (art/theme kept on disk)")
    sys.stdout.write("audit: %d item(s) still mention Linux Mint (see docs/BUILDING.md, 'Mint sweep')\n" % found)
    return found


# --------------------------------------------------------------------------- main
def run_steps(ctx: Ctx, rules: Dict[str, Any], brand: Dict[str, str], steps: List[str]) -> None:
    for step in steps:
        try:
            if step == "applications":
                n = sweep_desktop_dir(ctx, rules.get("applications") or {}, brand["name"], "applications")
            elif step == "autostart":
                n = sweep_desktop_dir(ctx, rules.get("autostart") or {}, brand["name"], "autostart")
            elif step == "files":
                n = sweep_files(ctx, rules, brand)
            elif step == "firefox":
                n = sweep_firefox(ctx, rules, brand)
            else:
                continue
            ctx.log("%s: %d file(s) %s" % (step, n, "would change" if ctx.dry_run else "changed"))
        except Exception as exc:  # noqa: BLE001 - one broken step must not stop the others
            ctx.warn("step %s failed: %s" % (step, exc))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="rebrand-base.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=None, help="prefix for every system path (default: $LINDOS_ROOT or none)")
    ap.add_argument("--data", default=None, help="sweep rules JSON (default: %s)" % DATA_PATH)
    ap.add_argument("--fragment", default=None, help="os-release fragment for the brand strings")
    ap.add_argument("--steps", default=",".join(STEPS), help="comma separated subset of: " + ", ".join(STEPS))
    ap.add_argument("--dry-run", action="store_true", help="change nothing, say what would change")
    ap.add_argument("--quiet", "-q", action="store_true")
    ap.add_argument("--revert", action="store_true", help="restore the saved originals")
    ap.add_argument("--audit", action="store_true", help="list what still says Linux Mint (changes nothing)")
    args = ap.parse_args(argv)

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in steps if s not in STEPS]
    if bad:
        ap.error("unknown step(s): %s" % ", ".join(bad))

    root = args.root if args.root is not None else os.environ.get("LINDOS_ROOT", "")
    ctx = Ctx(root, dry_run=args.dry_run, quiet=args.quiet)
    try:
        if args.revert:
            revert(ctx)
            return 0
        rules = load_rules(ctx, args.data)
        if args.audit:
            audit(ctx, rules)
            return 0
        if rules is None:
            return 0
        run_steps(ctx, rules, load_brand(ctx, args.fragment), steps)
        ctx.log("done: %d change(s)%s" % (ctx.changes, ", %d problem(s)" % ctx.problems if ctx.problems else ""))
    except Exception as exc:  # noqa: BLE001 - never fail a package operation
        ctx.warn("unexpected error: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
