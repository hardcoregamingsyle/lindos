"""Browser choice: Microsoft Edge / Google Chrome / Mozilla Firefox (SPEC §4.4).

Honesty note (SPEC §0.1): Edge and Chrome are *not* on the ISO — their licences forbid
redistribution.  They are downloaded from Microsoft's / Google's official apt repositories
by ``install-browser.sh`` (root, via the helper action ``install-browser``).  Firefox ships on
the ISO as Mint's ``.deb`` (no snap).
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from typing import Any, Callable, Dict, List, Optional

from . import config as lconfig
from . import helper as lhelper
from . import paths

log = logging.getLogger("lindos.browsers")

BROWSERS: Dict[str, Dict[str, Any]] = {
    "edge": {
        "name": "Microsoft Edge",
        "package": "microsoft-edge-stable",
        "desktop": "microsoft-edge.desktop",
        "repo": "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main",
        "key_url": "https://packages.microsoft.com/keys/microsoft.asc",
        "list": "/etc/apt/sources.list.d/microsoft-edge.list",
    },
    "chrome": {
        "name": "Google Chrome",
        "package": "google-chrome-stable",
        "desktop": "google-chrome.desktop",
        "repo": "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main",
        "key_url": "https://dl.google.com/linux/linux_signing_key.pub",
        "list": "/etc/apt/sources.list.d/google-chrome.list",
    },
    "firefox": {
        "name": "Mozilla Firefox",
        "package": "firefox",
        "desktop": "firefox.desktop",
        "repo": None,
        "key_url": None,
        "list": None,
    },
}

#: executables that prove a browser is present even without dpkg
_BINARIES: Dict[str, List[str]] = {
    "edge": ["microsoft-edge-stable", "microsoft-edge"],
    "chrome": ["google-chrome-stable", "google-chrome"],
    "firefox": ["firefox", "firefox-esr"],
}

#: xfce4 "preferred applications" helper ids (``~/.config/xfce4/helpers.rc``)
_XFCE_HELPER_IDS: Dict[str, str] = {"edge": "microsoft-edge", "chrome": "google-chrome", "firefox": "firefox"}

_ONLINE_PROBES = (("packages.microsoft.com", 443), ("dl.google.com", 443), ("deb.debian.org", 443), ("1.1.1.1", 53))

LogFn = Callable[[str], None]


def _check(browser_id: str) -> Dict[str, Any]:
    if browser_id not in BROWSERS:
        raise KeyError(f"unknown browser {browser_id!r} (choose from {', '.join(BROWSERS)})")
    return BROWSERS[browser_id]


def _run(cmd: List[str], timeout: float = 30) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def dpkg_installed(package: str) -> bool:
    """True when dpkg reports *package* as installed (False on non-Debian systems)."""
    dpkg = shutil.which("dpkg-query")
    if not dpkg:
        return False
    proc = _run([dpkg, "-W", "-f=${Status}", package], timeout=20)
    return bool(proc and proc.returncode == 0 and "install ok installed" in (proc.stdout or ""))


def is_installed(bid: str) -> bool:
    """Is browser *bid* installed?  (dpkg status, then executables on PATH; guarded.)"""
    info = _check(bid)
    if dpkg_installed(info["package"]):
        return True
    return any(shutil.which(b) for b in _BINARIES.get(bid, []))


def online(timeout: float = 3.0) -> bool:
    """Cheap connectivity probe (TCP connect to a couple of well-known hosts)."""
    if os.environ.get("LINDOS_FORCE_OFFLINE", "").strip().lower() in ("1", "true", "yes"):
        return False
    for host, port in _ONLINE_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def install_preflight(bid: str, log: LogFn = print) -> Optional[bool]:
    """The unprivileged half of :func:`install`: ``True`` = already installed (nothing to do),
    ``False`` = cannot be installed now (offline; the reason is logged), ``None`` = the privileged
    ``install-browser`` helper action has to run.  Lets a caller that batches several privileged
    steps into one helper run (``lindos.helper.run_privileged_batch``) decide whether to include it."""
    info = _check(bid)
    if is_installed(bid):
        log(f"{info['name']} is already installed")
        return True
    if bid != "firefox" and not online():
        log(f"offline: {info['name']} is downloaded from the vendor's apt repository — connect to the "
            f"internet and run 'lindos-browser install {bid}' later. Firefox is available meanwhile.")
        return False
    return None


def install(bid: str, log: LogFn = print) -> bool:
    """Install *bid* through the helper action ``install-browser``.

    Returns True when the browser ends up installed.  Offline → logs a clear notice and
    returns False (Firefox on the ISO is the fallback; the setup wizard tells the user how to
    finish later: ``lindos-browser install <id>``).
    """
    info = _check(bid)
    ready = install_preflight(bid, log)
    if ready is not None:
        return ready
    log(f"installing {info['name']} (this needs administrator rights)…")
    res = lhelper.run_privileged("install-browser", {"browser": bid}, log=log)
    if not res.ok:
        log(f"install failed: {res.message}")
        return False
    return is_installed(bid) or res.ok


def _xfce_helper_dirs() -> List[str]:
    dirs = [os.path.join(paths.resolve("~/.local/share"), "xfce4", "helpers")]
    for base in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if base:
            dirs.append(os.path.join(base, "xfce4", "helpers"))
    return dirs


def _ensure_xfce_helper(bid: str) -> Optional[str]:
    """Make sure an xfce4 WebBrowser helper exists for *bid*; return its id."""
    helper_id = _XFCE_HELPER_IDS[bid]
    info = BROWSERS[bid]
    for d in _xfce_helper_dirs():
        if os.path.isfile(os.path.join(d, helper_id + ".desktop")):
            return helper_id
    binary = next((b for b in _BINARIES[bid] if shutil.which(b)), _BINARIES[bid][0])
    user_dir = _xfce_helper_dirs()[0]
    content = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Encoding=UTF-8\n"
        "Type=X-XFCE-Helper\n"
        f"Name={info['name']}\n"
        f"Icon={info['desktop'][:-8]}\n"
        "X-XFCE-Category=WebBrowser\n"
        f"X-XFCE-Commands={binary}\n"
        f"X-XFCE-CommandsWithParameter={binary} \"%s\"\n"
        f"X-XFCE-Binaries={';'.join(_BINARIES[bid])};\n"
    )
    try:
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, helper_id + ".desktop"), "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        log.warning("cannot write xfce helper for %s: %s", bid, exc)
        return None
    return helper_id


def _write_helpers_rc(helper_id: str) -> bool:
    rc = paths.resolve("~/.config/xfce4/helpers.rc")
    lines: List[str] = []
    try:
        if os.path.isfile(rc):
            with open(rc, "r", encoding="utf-8", errors="replace") as fh:
                lines = [ln.rstrip("\n") for ln in fh]
        lines = [ln for ln in lines if not ln.startswith("WebBrowser=")]
        lines.append(f"WebBrowser={helper_id}")
        os.makedirs(os.path.dirname(rc), exist_ok=True)
        with open(rc, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return True
    except OSError as exc:
        log.warning("cannot write %s: %s", rc, exc)
        return False


def set_default(bid: str) -> bool:
    """Make *bid* the default browser (xdg-settings + xdg-mime + xfce4 helpers.rc + config).

    Returns True when at least the config was written and every available tool succeeded.
    """
    info = _check(bid)
    desktop = info["desktop"]
    ok = True
    xdg_settings = shutil.which("xdg-settings")
    if xdg_settings:
        proc = _run([xdg_settings, "set", "default-web-browser", desktop], timeout=30)
        if not proc or proc.returncode != 0:
            log.warning("xdg-settings failed: %s", (proc.stderr if proc else "not run").strip())
            ok = False
    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime:
        proc = _run([xdg_mime, "default", desktop, "x-scheme-handler/http", "x-scheme-handler/https",
                     "text/html", "application/xhtml+xml"], timeout=30)
        if not proc or proc.returncode != 0:
            log.warning("xdg-mime failed: %s", (proc.stderr if proc else "not run").strip())
            ok = False
    helper_id = _ensure_xfce_helper(bid)
    if helper_id:
        _write_helpers_rc(helper_id)
    try:
        cfg = lconfig.Config.load()
        cfg["browser"] = bid
        cfg.save()
    except OSError as exc:
        log.warning("cannot save config: %s", exc)
        return False
    return ok


def default_browser() -> Optional[str]:
    """Browser id currently reported by ``xdg-settings`` (None when unknown/unavailable)."""
    xdg_settings = shutil.which("xdg-settings")
    if xdg_settings:
        proc = _run([xdg_settings, "get", "default-web-browser"], timeout=20)
        if proc and proc.returncode == 0:
            desktop = (proc.stdout or "").strip()
            for bid, info in BROWSERS.items():
                if desktop == info["desktop"]:
                    return bid
            if desktop:
                return desktop
    return None


def list_browsers() -> List[Dict[str, Any]]:
    """Metadata + ``installed``/``on_iso`` flags for the UI/CLI."""
    out = []
    for bid, info in BROWSERS.items():
        entry = dict(info)
        entry["id"] = bid
        entry["installed"] = is_installed(bid)
        entry["on_iso"] = bid == "firefox"
        entry["note"] = ("Ships on the ISO" if bid == "firefox"
                         else "Downloaded from the vendor's official apt repository (needs internet)")
        out.append(entry)
    return out


__all__ = ["BROWSERS", "is_installed", "install", "install_preflight", "set_default", "online", "dpkg_installed",
           "default_browser", "list_browsers"]
