#!/usr/bin/env python3
"""panel-profile-pack.py — build xfce4-panel-profiles tarballs from xfconf XML (Lindos).

xfce4-panel-profiles stores a profile as a ``.tar.bz2`` containing

* ``config.txt`` — one line per xfconf property of the ``xfce4-panel`` channel,
  ``"<property path> <value>"`` where the value is the GVariant *text* form exactly as
  ``str(GLib.Variant)`` prints it (type-annotated, e.g. ``uint32 48``, ``'p=10;x=0;y=0'``,
  ``true``, ``[<1>, <2>]``); it is read back with ``GLib.Variant.parse()``;
* plugin rc files at the archive root, named ``<plugin>-<id>.rc`` (``whiskermenu-1.rc``,
  ``docklike-2.rc`` …), restored into ``~/.config/xfce4/panel/`` on load;
* launcher desktop files as ``launcher-<id>/<file>.desktop``.

This tool produces that layout from a ``panel/`` directory holding an xfconf channel dump
``xfce4-panel.xml`` plus the rc files — pure stdlib, so it runs on the build host, in the
chroot and on Windows (tests).  When PyGObject is importable it additionally round-trips
every value through ``GLib.Variant.parse`` (``--verify``) to prove the text is parseable.

Usage
    panel-profile-pack.py pack  <panel_dir> <out.tar.bz2> [--verify] [--quiet]
    panel-profile-pack.py dump  <panel_dir>                       # print config.txt
    panel-profile-pack.py check <profile.tar.bz2>                 # validate a tarball
    panel-profile-pack.py all   <modes_dir> [--verify] [--quiet]  # every <mode>/panel/ → <mode>/panel.tar.bz2

Exit codes: 0 ok, 1 error, 2 usage.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import tarfile
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("panel-profile-pack")

CHANNEL = "xfce4-panel"
CONFIG_TXT = "config.txt"

# xfconf XML "type" attribute → GVariant type string
XFCONF_TO_GVARIANT: Dict[str, str] = {
    "string": "s",
    "bool": "b",
    "int": "i",
    "uint": "u",
    "int64": "x",
    "uint64": "t",
    "double": "d",
    "float": "d",      # xfconf has no float; treat like double
    "int16": "n",
    "uint16": "q",
    "char": "y",
    "uchar": "y",
}

# Type annotations GLib emits when type_annotate=TRUE (int32/bool/double/string need none)
ANNOTATED = {"u": "uint32", "x": "int64", "t": "uint64", "n": "int16", "q": "uint16", "y": "byte"}

# Panel plugins that keep their settings in ~/.config/xfce4/panel/<name>-<id>.rc (not xfconf);
# a missing rc for one of these is worth a warning.  Clock/pulseaudio/systray/etc. use xfconf.
RC_PLUGINS = {"whiskermenu", "docklike", "genmon", "xkb", "weather", "places", "cpugraph",
              "netload", "battery", "datetime", "verve", "timer", "diskperf", "fsguard",
              "mailwatch", "smartbookmark", "systemload", "wavelan", "sensors", "eyes",
              "mount", "cpufreq", "windowck", "notes", "sample"}


class Value:
    """A leaf xfconf value: ``kind`` is a GVariant type letter or 'av' for arrays."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: object) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Value({self.kind!r}, {self.value!r})"


# --- GVariant text serialisation (mirrors g_variant_print(value, TRUE)) ---------------------
def _print_string(text: str) -> str:
    quote = '"' if "'" in text else "'"
    out = [quote]
    for ch in text:
        if ch == quote or ch == "\\":
            out.append("\\" + ch)
            continue
        code = ord(ch)
        if ch.isprintable() and code >= 0x20:
            out.append(ch)
        elif ch == "\a":
            out.append("\\a")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\v":
            out.append("\\v")
        elif code < 0x10000:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    out.append(quote)
    return "".join(out)


def _print_double(num: float) -> str:
    if num != num:  # NaN
        return "nan"
    if num in (float("inf"), float("-inf")):
        return "inf" if num > 0 else "-inf"
    text = repr(float(num))
    if "." not in text and "e" not in text and "E" not in text:
        text += ".0"
    return text


def _print_scalar(kind: str, value: object, annotate: bool) -> str:
    if kind == "s":
        return _print_string(str(value))
    if kind == "b":
        return "true" if value else "false"
    if kind == "d":
        return _print_double(float(value))  # type: ignore[arg-type]
    if kind == "i":
        return str(int(value))  # type: ignore[call-overload]
    if kind == "y":
        text = "0x%02x" % (int(value) & 0xFF)  # type: ignore[call-overload]
    else:
        text = str(int(value))  # type: ignore[call-overload]
    if annotate and kind in ANNOTATED:
        return f"{ANNOTATED[kind]} {text}"
    return text


def gvariant_text(val: Value, annotate: bool = True) -> str:
    """Text form of *val* as ``str(GLib.Variant)`` (type_annotate=True) would print it."""
    if val.kind == "av":
        items: List[Value] = val.value  # type: ignore[assignment]
        if not items:
            return "@av []" if annotate else "[]"
        # elements of an 'av' array are variants → always printed annotated inside <>
        return "[" + ", ".join("<" + gvariant_text(item, True) + ">" for item in items) + "]"
    return _print_scalar(val.kind, val.value, annotate)


# --- xfconf XML parsing -----------------------------------------------------------------------
def _parse_scalar(kind: str, raw: Optional[str], where: str) -> object:
    raw = "" if raw is None else raw
    if kind == "s":
        return raw
    if kind == "b":
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", ""):
            return False
        raise ValueError(f"{where}: bad bool {raw!r}")
    if kind == "d":
        return float(raw.strip() or "0")
    try:
        num = int(raw.strip() or "0", 0)
    except ValueError as exc:
        raise ValueError(f"{where}: bad integer {raw!r}") from exc
    if kind in ("u", "t", "q", "y") and num < 0:
        raise ValueError(f"{where}: negative value {num} for unsigned type")
    return num


def _walk(elem: ET.Element, prefix: str, props: Dict[str, Value]) -> None:
    for child in elem:
        if child.tag != "property":
            continue
        name = child.get("name")
        if not name:
            raise ValueError(f"property without name under {prefix or '/'}")
        path = f"{prefix}/{name}"
        ptype = (child.get("type") or "empty").strip()
        if ptype == "array":
            items: List[Value] = []
            for v in child:
                if v.tag != "value":
                    continue
                vt = (v.get("type") or "string").strip()
                if vt not in XFCONF_TO_GVARIANT:
                    raise ValueError(f"{path}: unsupported array element type {vt!r}")
                kind = XFCONF_TO_GVARIANT[vt]
                items.append(Value(kind, _parse_scalar(kind, v.get("value"), path)))
            props[path] = Value("av", items)
        elif ptype == "empty":
            pass  # container node: xfconfd never reports a value for it
        elif ptype in XFCONF_TO_GVARIANT:
            kind = XFCONF_TO_GVARIANT[ptype]
            props[path] = Value(kind, _parse_scalar(kind, child.get("value"), path))
        else:
            raise ValueError(f"{path}: unsupported xfconf type {ptype!r}")
        _walk(child, path, props)


def load_channel_xml(xml_path: str) -> Dict[str, Value]:
    """Parse an xfconf per-channel XML file into ``{'/path/prop': Value}``."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if root.tag != "channel":
        raise ValueError(f"{xml_path}: root element is <{root.tag}>, expected <channel>")
    channel = root.get("name")
    if channel != CHANNEL:
        raise ValueError(f"{xml_path}: channel is {channel!r}, expected {CHANNEL!r}")
    props: Dict[str, Value] = {}
    _walk(root, "", props)
    if not props:
        raise ValueError(f"{xml_path}: no properties found")
    return props


def config_lines(props: Dict[str, Value]) -> List[str]:
    return [f"{path} {gvariant_text(value)}" for path, value in sorted(props.items())]


# --- companions: rc files & launcher desktop files ------------------------------------------
_PLUGIN_RE = re.compile(r"^/plugins/plugin-(\d+)$")


def plugin_table(props: Dict[str, Value]) -> Dict[int, str]:
    """``{plugin_id: plugin_name}`` from ``/plugins/plugin-N`` string properties."""
    table: Dict[int, str] = {}
    for path, value in props.items():
        m = _PLUGIN_RE.match(path)
        if m and value.kind == "s":
            table[int(m.group(1))] = str(value.value)
    return table


def panel_plugin_ids(props: Dict[str, Value]) -> List[int]:
    ids: List[int] = []
    for path, value in props.items():
        if path.endswith("/plugin-ids") and value.kind == "av":
            ids.extend(int(v.value) for v in value.value if v.kind in ("i", "u"))  # type: ignore[union-attr]
    return ids


def find_companions(panel_dir: str, props: Dict[str, Value]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ``([(arcname, abs_path), …], warnings)`` for rc and launcher files in *panel_dir*."""
    members: List[Tuple[str, str]] = []
    warnings: List[str] = []
    plugins = plugin_table(props)
    expected_rc = {f"{name}-{pid}.rc" for pid, name in plugins.items() if name != "launcher"}
    for entry in sorted(os.listdir(panel_dir)):
        full = os.path.join(panel_dir, entry)
        if entry == "xfce4-panel.xml":
            continue
        if os.path.isfile(full) and entry.endswith(".rc"):
            if entry not in expected_rc:
                warnings.append(f"{entry}: no matching plugin in xfce4-panel.xml (packed anyway)")
            members.append((entry, full))
        elif os.path.isdir(full) and re.match(r"^launcher-\d+$", entry):
            for f in sorted(os.listdir(full)):
                if f.endswith(".desktop"):
                    members.append((f"{entry}/{f}", os.path.join(full, f)))
        elif os.path.isfile(full):
            warnings.append(f"{entry}: ignored (not an rc file)")
    packed = {m[0] for m in members}
    for pid, name in sorted(plugins.items()):
        rc = f"{name}-{pid}.rc"
        if name in RC_PLUGINS and rc not in packed:
            warnings.append(f"{rc}: plugin has no rc file in {panel_dir} (plugin keeps its defaults)")
    return members, warnings


# --- verification through PyGObject (optional) ------------------------------------------------
def gi_verify(lines: Iterable[str]) -> Tuple[bool, str]:
    """Round-trip every value through GLib.Variant.parse when PyGObject is available."""
    try:
        import gi  # type: ignore  # noqa: F401
        from gi.repository import GLib  # type: ignore
    except Exception:  # pragma: no cover - depends on host
        return True, "PyGObject not available; skipped GLib.Variant round-trip"
    bad: List[str] = []
    for line in lines:
        key, _, text = line.partition(" ")
        try:
            GLib.Variant.parse(None, text, None, None)
        except Exception as exc:  # pragma: no cover - depends on host
            bad.append(f"{key}: {exc}")
    if bad:
        return False, "; ".join(bad)
    return True, "all values parse with GLib.Variant.parse"


# --- pack / check -----------------------------------------------------------------------------
def pack(panel_dir: str, out_path: str, verify: bool = False) -> Tuple[List[str], List[str]]:
    """Create *out_path* (tar.bz2) from *panel_dir*; returns ``(config_lines, warnings)``."""
    xml_path = os.path.join(panel_dir, "xfce4-panel.xml")
    if not os.path.isfile(xml_path):
        raise FileNotFoundError(xml_path)
    props = load_channel_xml(xml_path)
    lines = config_lines(props)
    members, warnings = find_companions(panel_dir, props)
    ids = panel_plugin_ids(props)
    plugins = plugin_table(props)
    for pid in ids:
        if pid not in plugins:
            warnings.append(f"plugin-ids references plugin-{pid} which has no /plugins/plugin-{pid} entry")
    if verify:
        ok, msg = gi_verify(lines)
        if not ok:
            raise ValueError("GVariant verification failed: " + msg)
        warnings.append("verify: " + msg)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with tarfile.open(tmp, mode="w:bz2") as tar:
        info = tarfile.TarInfo(CONFIG_TXT)
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
        for arcname, path in members:
            with open(path, "rb") as fh:
                payload = fh.read()
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    os.replace(tmp, out_path)
    return lines, warnings


def check(profile_path: str) -> Tuple[bool, List[str]]:
    """Validate a profile tarball: config.txt present, every line ``path value``, ids consistent."""
    problems: List[str] = []
    try:
        with tarfile.open(profile_path, mode="r:*") as tar:
            names = tar.getnames()
            if CONFIG_TXT not in names:
                return False, [f"{CONFIG_TXT} missing"]
            member = tar.extractfile(CONFIG_TXT)
            assert member is not None
            text = member.read().decode("utf-8")
    except (tarfile.TarError, OSError, UnicodeDecodeError) as exc:
        return False, [f"cannot read {profile_path}: {exc}"]
    plugin_names: Dict[int, str] = {}
    ids: List[int] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        key, sep, value = line.partition(" ")
        if not sep or not key.startswith("/") or not value.strip():
            problems.append(f"line {n}: malformed: {line!r}")
            continue
        m = _PLUGIN_RE.match(key)
        if m:
            sm = re.match(r"^'([^']*)'$", value.strip())
            if sm:
                plugin_names[int(m.group(1))] = sm.group(1)
        if key.endswith("/plugin-ids"):
            ids.extend(int(x) for x in re.findall(r"<(\d+)>", value))
    if not ids:
        problems.append("no /panels/panel-N/plugin-ids found")
    for pid in ids:
        if pid not in plugin_names:
            problems.append(f"plugin-{pid} in plugin-ids has no /plugins/plugin-{pid}")
    for pid, name in plugin_names.items():
        rc = f"{name}-{pid}.rc"
        if name in ("whiskermenu", "docklike") and rc not in names:
            problems.append(f"{rc} not in tarball (plugin will use defaults)")
    return not problems, problems


def pack_all(modes_dir: str, verify: bool = False) -> int:
    """Pack every ``<modes_dir>/<mode>/panel/`` into ``<mode>/panel.tar.bz2``; returns #failures."""
    failures = 0
    for mode in sorted(os.listdir(modes_dir)):
        panel_dir = os.path.join(modes_dir, mode, "panel")
        if not os.path.isfile(os.path.join(panel_dir, "xfce4-panel.xml")):
            continue
        out = os.path.join(modes_dir, mode, "panel.tar.bz2")
        try:
            lines, warnings = pack(panel_dir, out, verify=verify)
        except (OSError, ValueError, ET.ParseError) as exc:
            log.error("%s: %s", mode, exc)
            failures += 1
            continue
        for w in warnings:
            log.warning("%s: %s", mode, w)
        log.info("%s: %s (%d properties)", mode, out, len(lines))
    return failures


# --- CLI --------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="panel-profile-pack.py",
                                     description="Build xfce4-panel-profiles tarballs from xfconf XML")
    parser.add_argument("--quiet", "-q", action="store_true", help="only warnings and errors")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pack = sub.add_parser("pack", help="pack one panel/ dir into a .tar.bz2")
    p_pack.add_argument("panel_dir")
    p_pack.add_argument("out")
    p_pack.add_argument("--verify", action="store_true", help="round-trip values through GLib.Variant if PyGObject is present")
    p_dump = sub.add_parser("dump", help="print the config.txt that would be generated")
    p_dump.add_argument("panel_dir")
    p_check = sub.add_parser("check", help="validate an existing profile tarball")
    p_check.add_argument("profile")
    p_all = sub.add_parser("all", help="pack every <modes_dir>/<mode>/panel/")
    p_all.add_argument("modes_dir")
    p_all.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="panel-profile-pack: %(levelname)s: %(message)s")
    try:
        if args.cmd == "dump":
            props = load_channel_xml(os.path.join(args.panel_dir, "xfce4-panel.xml"))
            sys.stdout.write("\n".join(config_lines(props)) + "\n")
            return 0
        if args.cmd == "pack":
            lines, warnings = pack(args.panel_dir, args.out, verify=args.verify)
            for w in warnings:
                log.warning("%s", w)
            log.info("wrote %s (%d properties)", args.out, len(lines))
            return 0
        if args.cmd == "check":
            ok, problems = check(args.profile)
            for p in problems:
                log.error("%s", p)
            if ok:
                log.info("%s: OK", args.profile)
            return 0 if ok else 1
        if args.cmd == "all":
            return 1 if pack_all(args.modes_dir, verify=args.verify) else 0
    except (OSError, ValueError, ET.ParseError) as exc:
        log.error("%s", exc)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
