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
import time
from typing import Any, Callable, Dict, List, Optional

from . import config as lconfig
from . import helper as lhelper
from . import installstate
from . import paths
from . import session as lsession

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
    returns False (Firefox on the ISO is the fallback; Settings > Apps offers "Install now" and
    ``lindos-browser install <id>`` finishes it later).
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


# --- follow the choice made in Setup once the browser has landed (SPEC §4.4, §17) ---------------------
#: how often :func:`sync_default` looks again while it waits, and for how long a login session waits at most
SYNC_POLL_SECONDS = 30.0
SYNC_MAX_WAIT_SECONDS = 7200.0

#: outcomes of :func:`sync_default`; the first two mean "look again later" while a wait budget lasts
SYNC_SETUP_PENDING = "setup-pending"      # Lindos Setup has not finished: the choice is not made yet
SYNC_PENDING = "pending"                  # chosen, not installed yet, and the silent retry may still add it
SYNC_UNAVAILABLE = "unavailable"          # chosen, not installed, and nothing will install it
SYNC_ALREADY = "already"                  # the user's XFCE preferred browser already is the chosen one
SYNC_PERSONAL = "personal"                # the user already has a personal choice: left alone
SYNC_LIVE = "live"                        # live USB session / the temporary oem account: nothing to do
SYNC_APPLIED = "applied"                  # made the default (xdg + xfce4 helpers.rc + config)
SYNC_PARTIAL = "partial"                  # made the default, but a tool reported a problem (see the log)

_HTTP_KEYS = ("x-scheme-handler/https", "x-scheme-handler/http")


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.strip() for ln in fh.read().splitlines()]
    except OSError:
        return []


def user_helper_choice() -> Optional[str]:
    """``WebBrowser=`` in the user's ``~/.config/xfce4/helpers.rc`` (XFCE's preferred web browser); None if unset.

    Only the user's own file counts: the system-wide one (the base system's says ``firefox``) is the *default*, not a choice."""
    value: Optional[str] = None
    for line in _read_lines(paths.resolve("~/.config/xfce4/helpers.rc")):
        if line.startswith("WebBrowser=") and line.split("=", 1)[1].strip():
            value = line.split("=", 1)[1].strip()
    return value


def user_mime_choice() -> Optional[str]:
    """The web browser the user's own ``~/.config/mimeapps.list`` names for https/http (first desktop id); None if unset."""
    section = ""
    found: Dict[str, str] = {}
    for line in _read_lines(paths.resolve("~/.config/mimeapps.list")):
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Default Applications]" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            first = next((v.strip() for v in value.split(";") if v.strip()), "")
            if key.strip() in _HTTP_KEYS and first:
                found[key.strip()] = first
    for key in _HTTP_KEYS:
        if key in found:
            return found[key]
    return None


def _retry_expected(bid: str) -> bool:
    """True when something is still going to install *bid* by itself.

    Only Chrome has a silent start-up retry (``lindos-browser-firstboot.service``), and only while the installer's
    'browser' step is pending/failed/unknown; Edge is never installed by Lindos and Firefox is on the ISO."""
    if bid != "chrome":
        return False
    return installstate.status("browser") not in installstate.TERMINAL


def _sync_once(log: LogFn) -> str:
    if lsession.is_live_session() or lsession.is_oem_temp_user():
        return SYNC_LIVE
    if not lconfig.is_setup_done():
        return SYNC_SETUP_PENDING
    bid = lconfig.effective_browser()
    if bid not in BROWSERS:
        return SYNC_UNAVAILABLE
    # a choice the user already has always wins - and needs no waiting for the install
    helper_choice = user_helper_choice()
    if helper_choice == _XFCE_HELPER_IDS[bid]:
        return SYNC_ALREADY
    mime_choice = user_mime_choice()
    if helper_choice or (mime_choice and mime_choice != BROWSERS[bid]["desktop"]):
        log(f"the user already has a personal web browser choice ({helper_choice or mime_choice}); left alone")
        return SYNC_PERSONAL
    if not is_installed(bid):
        return SYNC_PENDING if _retry_expected(bid) else SYNC_UNAVAILABLE
    log(f"{BROWSERS[bid]['name']} is installed and is the browser chosen in Setup: making it the default")
    return SYNC_APPLIED if set_default(bid) else SYNC_PARTIAL


def sync_default(wait: float = 0.0, poll: float = SYNC_POLL_SECONDS, *, log: LogFn = lambda msg: None,
                 sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> str:
    """Make the browser the user chose in Lindos Setup the default once it is installed; returns an ``SYNC_*`` outcome.

    Why: Chrome is downloaded from Google's repository, so an offline install (or a failed one) leaves it *pending*
    and Setup can only remember the choice.  The silent retry later installs it and points ``/etc/xdg/mimeapps.list``
    at it, but XFCE's own "preferred web browser" (``exo-open``, the keyboard shortcut for the browser, the menu's web
    search) reads ``xfce4/helpers.rc`` - and the system-wide one that ships with the base says Firefox.  A root job
    cannot write into the user's home, so this runs as the user, at every login (an autostart entry) and, with
    *wait*, keeps looking every *poll* seconds for at most *wait* seconds while Setup or the retry is still to come.

    A personal choice always wins: a ``WebBrowser=`` line in the user's own ``helpers.rc``, or a different https/http
    handler in the user's own ``mimeapps.list`` (whatever Setup, Settings, XFCE or Firefox wrote there), is never
    overwritten.  Nothing happens in the live session, in the temporary ``oem`` account, or before Setup finished.
    """
    deadline = clock() + max(0.0, float(wait))
    while True:
        outcome = _sync_once(log)
        if outcome not in (SYNC_SETUP_PENDING, SYNC_PENDING):
            return outcome
        remaining = deadline - clock()
        if remaining <= 0:
            return outcome
        sleep(min(float(poll), remaining))


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
           "default_browser", "list_browsers", "sync_default", "user_helper_choice", "user_mime_choice",
           "SYNC_POLL_SECONDS", "SYNC_MAX_WAIT_SECONDS", "SYNC_SETUP_PENDING", "SYNC_PENDING", "SYNC_UNAVAILABLE",
           "SYNC_ALREADY", "SYNC_PERSONAL", "SYNC_LIVE", "SYNC_APPLIED", "SYNC_PARTIAL"]
