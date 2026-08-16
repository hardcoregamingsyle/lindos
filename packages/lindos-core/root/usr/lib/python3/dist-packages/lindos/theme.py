"""Look & feel: dark/light, accent, wallpaper, font, taskbar (SPEC §4.7, §2).

All changes go through ``xfconf-query`` (guarded — returns False when unavailable) plus
plain files under ``~/.config`` (GTK 4 settings, accent CSS).  Every setter also records
the choice in the user config so ``apply_from_config()`` can restore it after login.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from . import config as lconfig
from . import paths

log = logging.getLogger("lindos.theme")

THEME_DARK = "Lindos-Dark"
THEME_LIGHT = "Lindos-Light"
ICON_THEME_DARK = "Lindos-dark"
ICON_THEME_LIGHT = "Lindos"
CURSOR_DARK = "Fluent-dark-cursors"
CURSOR_LIGHT = "Fluent-cursors"
XFWM_DARK = "Lindos-Dark"
XFWM_LIGHT = "Lindos-Light"
ACCENT_DARK = "#60CDFF"
ACCENT_LIGHT = "#0067C0"
DEFAULT_FONT = "Selawik 10"
DEFAULT_WALLPAPER = "/usr/share/backgrounds/lindos/aurora-dark.svg"
LIGHT_WALLPAPER = "/usr/share/backgrounds/lindos/aurora-light.svg"
SYSTEM_GTK_CSS = "/usr/share/lindos/gtk-3.0/lindos.css"

#: the eight accent swatches offered by the OOBE / settings (name, hex)
ACCENTS: List[Tuple[str, str]] = [
    ("Aurora Blue", "#60CDFF"),
    ("Windows Blue", "#0067C0"),
    ("Mint Green", "#3EB489"),
    ("Violet", "#B4A0FF"),
    ("Rose", "#FF99A4"),
    ("Amber", "#FFB900"),
    ("Teal", "#00B7C3"),
    ("Graphite", "#8E8E8E"),
]

#: xfce4-panel snap positions ("p=N" in /panels/panel-1/position): 6 = top-left, 8 = bottom-left
PANEL_POSITION_TOP = "p=6;x=0;y=0"
PANEL_POSITION_BOTTOM = "p=8;x=0;y=0"

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CSS_BEGIN = "/* lindos:begin — managed by lindos.theme, do not edit between markers */"
_CSS_END = "/* lindos:end */"


# --- xfconf plumbing ------------------------------------------------------------------------
def xfconf_available() -> bool:
    return shutil.which("xfconf-query") is not None


def xfconf_query(args: List[str], timeout: float = 15) -> Tuple[bool, str]:
    """Run ``xfconf-query <args>``; ``(ok, output)``.  Never raises."""
    tool = shutil.which("xfconf-query")
    if not tool:
        return False, "xfconf-query: not found"
    try:
        proc = subprocess.run([tool] + args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def xfconf_get(channel: str, prop: str) -> Optional[str]:
    ok, out = xfconf_query(["-c", channel, "-p", prop])
    return out if ok else None


def xfconf_set(channel: str, prop: str, value: Any, vtype: str = "string") -> bool:
    """Set (creating if needed) ``channel:prop`` to *value* of xfconf type *vtype*."""
    if isinstance(value, bool):
        text = "true" if value else "false"
        vtype = "bool"
    else:
        text = str(value)
    ok, out = xfconf_query(["-c", channel, "-p", prop, "-n", "-t", vtype, "-s", text])
    if not ok:
        log.debug("xfconf set %s %s failed: %s", channel, prop, out)
    return ok


def xfconf_list(channel: str, prefix: str = "/") -> List[str]:
    ok, out = xfconf_query(["-c", channel, "-p", prefix, "-l"])
    if not ok:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("/")]


def _gsettings(*args: str) -> bool:
    tool = shutil.which("gsettings")
    if not tool:
        return False
    try:
        proc = subprocess.run([tool] + list(args), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=15, check=False)
        return proc.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _save_config(**values: Any) -> None:
    try:
        cfg = lconfig.Config.load()
        cfg.update(values)
        cfg.save()
    except OSError as exc:
        log.warning("cannot save config: %s", exc)


def _write_text(path: str, content: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except OSError as exc:
        log.warning("cannot write %s: %s", path, exc)
        return False


# --- dark / light -----------------------------------------------------------------------------
def _write_gtk4_settings(dark: bool) -> bool:
    path = os.path.join(paths.gtk4_dir(), "settings.ini")
    lines: List[str] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [ln.rstrip("\n") for ln in fh]
        except OSError:
            lines = []
    lines = [ln for ln in lines if not ln.startswith("gtk-application-prefer-dark-theme")
             and not ln.startswith("gtk-theme-name") and not ln.startswith("gtk-icon-theme-name")
             and not ln.startswith("gtk-cursor-theme-name")]
    if "[Settings]" not in [ln.strip() for ln in lines]:
        lines.insert(0, "[Settings]")
    idx = [ln.strip() for ln in lines].index("[Settings]") + 1
    additions = [
        f"gtk-application-prefer-dark-theme={'1' if dark else '0'}",
        f"gtk-theme-name={THEME_DARK if dark else THEME_LIGHT}",
        f"gtk-icon-theme-name={ICON_THEME_DARK if dark else ICON_THEME_LIGHT}",
        f"gtk-cursor-theme-name={CURSOR_DARK if dark else CURSOR_LIGHT}",
    ]
    lines[idx:idx] = additions
    return _write_text(path, "\n".join(lines) + "\n")


def set_dark(dark: bool) -> bool:
    """Switch the whole desktop between Lindos-Dark and Lindos-Light.

    xfconf: xsettings ``/Net/ThemeName``, ``/Net/IconThemeName``, ``/Gtk/CursorThemeName``,
    xfwm4 ``/general/theme``; GTK 4 ``settings.ini``; gsettings ``color-scheme``; config.
    Returns True when the file-based parts succeeded and xfconf (if present) succeeded.
    """
    dark = bool(dark)
    ok = True
    if xfconf_available():
        ok &= xfconf_set("xsettings", "/Net/ThemeName", THEME_DARK if dark else THEME_LIGHT)
        ok &= xfconf_set("xsettings", "/Net/IconThemeName", ICON_THEME_DARK if dark else ICON_THEME_LIGHT)
        ok &= xfconf_set("xsettings", "/Gtk/CursorThemeName", CURSOR_DARK if dark else CURSOR_LIGHT)
        ok &= xfconf_set("xfwm4", "/general/theme", XFWM_DARK if dark else XFWM_LIGHT)
    ok &= _write_gtk4_settings(dark)
    _gsettings("set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark" if dark else "default")
    _gsettings("set", "org.gnome.desktop.interface", "gtk-theme", THEME_DARK if dark else THEME_LIGHT)
    _save_config(theme="dark" if dark else "light")
    return ok


def is_dark() -> bool:
    """True when the current GTK theme is a dark variant (xfconf, else config)."""
    name = xfconf_get("xsettings", "/Net/ThemeName")
    if name:
        return name.strip().lower().endswith(("-dark", "dark"))
    return lconfig.Config.load().get("theme", "dark") == "dark"


# --- accent -----------------------------------------------------------------------------------
def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def accent_css(hex_color: str) -> str:
    fg = "#000000" if _luminance(hex_color) > 0.4 else "#FFFFFF"
    return (
        "/* Generated by Lindos (lindos.theme.set_accent) — edit the accent in Lindos Settings instead */\n"
        f"@define-color lindos_accent {hex_color};\n"
        f"@define-color lindos_accent_fg {fg};\n"
        "@define-color accent_color @lindos_accent;\n"
        "@define-color accent_bg_color @lindos_accent;\n"
        "@define-color accent_fg_color @lindos_accent_fg;\n"
        "@define-color theme_selected_bg_color @lindos_accent;\n"
        "@define-color theme_selected_fg_color @lindos_accent_fg;\n"
        "@define-color selected_bg_color @lindos_accent;\n"
        "@define-color selected_fg_color @lindos_accent_fg;\n"
        "@define-color link_color @lindos_accent;\n"
        "@define-color focus_border_color @lindos_accent;\n"
    )


def _update_gtk_css(gtk_dir: str, imports: List[str]) -> bool:
    path = os.path.join(gtk_dir, "gtk.css")
    existing = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        except OSError:
            existing = ""
    if _CSS_BEGIN in existing and _CSS_END in existing:
        head, rest = existing.split(_CSS_BEGIN, 1)
        _, tail = rest.split(_CSS_END, 1)
        existing = head + tail.lstrip("\n")
    block = _CSS_BEGIN + "\n" + "".join(f'@import url("{imp}");\n' for imp in imports) + _CSS_END + "\n"
    return _write_text(path, block + existing)


def set_accent(hex_color: str) -> bool:
    """Write ``~/.config/gtk-3.0/lindos-accent.css`` (+ gtk-4.0) and import it from ``gtk.css``."""
    hex_color = hex_color.strip()
    if not _HEX_RE.match(hex_color):
        raise ValueError(f"accent must be #RRGGBB, got {hex_color!r}")
    hex_color = hex_color.upper()
    css = accent_css(hex_color)
    ok = True
    for gtk_dir in (paths.gtk3_dir(), paths.gtk4_dir()):
        ok &= _write_text(os.path.join(gtk_dir, "lindos-accent.css"), css)
        imports = []
        system_css = paths.resolve(SYSTEM_GTK_CSS)
        if gtk_dir == paths.gtk3_dir() and os.path.isfile(system_css):
            imports.append(SYSTEM_GTK_CSS)
        imports.append("lindos-accent.css")
        ok &= _update_gtk_css(gtk_dir, imports)
    _save_config(accent=hex_color)
    return ok


def current_accent() -> str:
    return str(lconfig.Config.load().get("accent", ACCENT_DARK))


# --- wallpaper ------------------------------------------------------------------------------------
def list_wallpapers() -> List[str]:
    """Wallpaper files shipped in ``/usr/share/backgrounds/lindos`` (sorted, absolute)."""
    base = paths.wallpapers_dir()
    files: List[str] = []
    for ext in ("svg", "png", "jpg", "jpeg", "webp"):
        files.extend(glob.glob(os.path.join(base, f"*.{ext}")))
    return sorted(set(files))


def _backdrop_props() -> List[str]:
    props = [p for p in xfconf_list("xfce4-desktop", "/backdrop")
             if re.match(r"^/backdrop/screen\d+/monitor[^/]+/workspace\d+/last-image$", p)]
    return props


def set_wallpaper(path: str) -> bool:
    """Set *path* on every monitor/workspace known to xfdesktop (xfconf) and remember it."""
    if not path:
        raise ValueError("wallpaper path required")
    ok = True
    if xfconf_available():
        props = _backdrop_props()
        if not props:
            monitors = ["monitor0"]
            try:
                from . import hardware
                outs = [o for o, info in hardware.refresh_rates().items() if info.get("connected")]
                if outs:
                    monitors = [f"monitor{o}" for o in outs]
            except Exception:
                pass
            props = [f"/backdrop/screen0/{m}/workspace0/last-image" for m in monitors]
        for prop in props:
            ok &= xfconf_set("xfce4-desktop", prop, path)
            base = prop.rsplit("/", 1)[0]
            xfconf_set("xfce4-desktop", base + "/image-style", 5, "int")   # zoomed
            xfconf_set("xfce4-desktop", base + "/color-style", 0, "int")
    _save_config(wallpaper=path)
    return ok


# --- fonts --------------------------------------------------------------------------------------
def set_font(name: str) -> bool:
    """Set the UI font (``"Selawik 10"``): xsettings ``/Gtk/FontName`` + xfwm4 title font."""
    name = name.strip()
    if not name:
        raise ValueError("font name required")
    ok = True
    m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)$", name)
    family, size = (m.group(1), m.group(2)) if m else (name, "10")
    if xfconf_available():
        ok &= xfconf_set("xsettings", "/Gtk/FontName", name)
        ok &= xfconf_set("xfwm4", "/general/title_font", f"{family} Bold {max(8, int(float(size)) - 1)}")
    _save_config(font=name)
    return ok


# --- taskbar ---------------------------------------------------------------------------------------
def _panel_plugin_ids(panel: str = "panel-1") -> List[int]:
    ok, out = xfconf_query(["-c", "xfce4-panel", "-p", f"/panels/{panel}/plugin-ids"])
    if not ok:
        return []
    return [int(tok) for tok in re.findall(r"^\s*(\d+)\s*$", out, re.M)]


def _plugin_type(plugin_id: int) -> str:
    return (xfconf_get("xfce4-panel", f"/plugins/plugin-{plugin_id}") or "").strip()


def set_taskbar_alignment(align: str) -> bool:
    """``"center"`` (Windows 11 style) or ``"left"``: toggles the leading expanding separator."""
    if align not in ("center", "left"):
        raise ValueError("alignment must be 'center' or 'left'")
    ok = True
    if xfconf_available():
        ids = _panel_plugin_ids()
        first_sep = next((pid for pid in ids if _plugin_type(pid) == "separator"), None)
        if first_sep is None:
            log.warning("no separator plugin found on panel-1; cannot change alignment")
            ok = False
        else:
            ok &= xfconf_set("xfce4-panel", f"/plugins/plugin-{first_sep}/expand", align == "center")
            # separator "style" is a uint in xfce4-panel (matches lindos-desktop's xfce4-panel.xml)
            xfconf_set("xfce4-panel", f"/plugins/plugin-{first_sep}/style", 0, "uint")
    _save_config(taskbar_alignment=align)
    return ok


def set_taskbar_position(pos: str) -> bool:
    """Move the main panel to ``"bottom"`` (default) or ``"top"``."""
    if pos not in ("bottom", "top"):
        raise ValueError("position must be 'bottom' or 'top'")
    ok = True
    if xfconf_available():
        value = PANEL_POSITION_TOP if pos == "top" else PANEL_POSITION_BOTTOM
        ok &= xfconf_set("xfce4-panel", "/panels/panel-1/position", value)
        xfconf_set("xfce4-panel", "/panels/panel-1/position-locked", True)
        xfconf_set("xfce4-panel", "/panels/panel-1/length", 100, "uint")
    _save_config(taskbar_position=pos)
    return ok


# --- restore -------------------------------------------------------------------------------------
def apply_from_config(cfg: Optional[lconfig.Config] = None) -> Dict[str, bool]:
    """Re-apply theme/accent/wallpaper/font/taskbar from the user config (login autostart)."""
    cfg = cfg or lconfig.Config.load()
    results: Dict[str, bool] = {}
    try:
        results["dark"] = set_dark(cfg.get("theme", "dark") == "dark")
    except Exception as exc:
        log.warning("theme: %s", exc)
        results["dark"] = False
    accent = str(cfg.get("accent", ACCENT_DARK))
    try:
        results["accent"] = set_accent(accent) if _HEX_RE.match(accent) else False
    except Exception as exc:
        log.warning("accent: %s", exc)
        results["accent"] = False
    wallpaper = str(cfg.get("wallpaper", DEFAULT_WALLPAPER))
    try:
        results["wallpaper"] = set_wallpaper(wallpaper) if wallpaper else False
    except Exception as exc:
        log.warning("wallpaper: %s", exc)
        results["wallpaper"] = False
    if cfg.get("font"):
        try:
            results["font"] = set_font(str(cfg["font"]))
        except Exception:
            results["font"] = False
    if cfg.get("taskbar_alignment") in ("center", "left"):
        results["taskbar_alignment"] = set_taskbar_alignment(str(cfg["taskbar_alignment"]))
    if cfg.get("taskbar_position") in ("bottom", "top"):
        results["taskbar_position"] = set_taskbar_position(str(cfg["taskbar_position"]))
    return results


__all__ = [
    "THEME_DARK", "THEME_LIGHT", "ICON_THEME_DARK", "ICON_THEME_LIGHT", "CURSOR_DARK", "CURSOR_LIGHT",
    "ACCENT_DARK", "ACCENT_LIGHT", "ACCENTS", "DEFAULT_FONT", "DEFAULT_WALLPAPER", "LIGHT_WALLPAPER",
    "PANEL_POSITION_TOP", "PANEL_POSITION_BOTTOM",
    "xfconf_available", "xfconf_query", "xfconf_get", "xfconf_set", "xfconf_list",
    "set_dark", "is_dark", "set_accent", "accent_css", "current_accent", "list_wallpapers", "set_wallpaper",
    "set_font", "set_taskbar_alignment", "set_taskbar_position", "apply_from_config",
]
