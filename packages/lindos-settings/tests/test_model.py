"""Unit tests for ``lindos_settings.model`` (pure logic, no GTK, any OS)."""
from __future__ import annotations

import json
import os
import sys

import pytest

from lindos_settings import model

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.normpath(os.path.join(HERE, "..", "root"))
PAGES_JSON = os.path.join(PKG_ROOT, "usr", "share", "lindos", "settings", "pages.json")

SPEC_ORDER = ["home", "system", "personalization", "apps", "windows-apps", "gaming", "hardware", "network", "accounts", "mode", "update", "updates", "about"]


# --------------------------------------------------------------------------- registry
def test_model_has_no_gi_import():
    assert "gi" not in sys.modules or "lindos_settings.model" in sys.modules
    src = open(model.__file__, encoding="utf-8").read()
    assert "import gi" not in src and "gi.repository" not in src


def test_page_order_matches_spec():
    assert list(model.PAGE_ORDER) == SPEC_ORDER
    assert model.page_ids(model.builtin_pages()) == SPEC_ORDER


def test_shipped_pages_json_matches_builtin_registry():
    with open(PAGES_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data == model.BUILTIN_PAGES
    pages = model.pages_from_data(data)
    assert model.page_ids(pages) == SPEC_ORDER


def test_shipped_pages_json_loads_from_lindos_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    dst = root / "usr" / "share" / "lindos" / "settings"
    dst.mkdir(parents=True)
    with open(PAGES_JSON, encoding="utf-8") as src, open(dst / "pages.json", "w", encoding="utf-8") as out:
        out.write(src.read())
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    pages = model.load_pages()
    assert model.page_ids(pages) == SPEC_ORDER
    by_id = model.pages_by_id(pages)
    assert by_id["system"].kind == "delegate"
    assert [s.id for s in by_id["system"].subitems][:2] == ["display", "sound"]
    assert by_id["system"].subitems[0].exec == ("xfce4-display-settings",)


def test_load_pages_falls_back_when_missing_or_invalid(tmp_path):
    assert model.page_ids(model.load_pages(str(tmp_path / "nope.json"))) == SPEC_ORDER
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert model.page_ids(model.load_pages(str(bad))) == SPEC_ORDER


def test_native_and_delegate_kinds():
    by_id = model.pages_by_id(model.builtin_pages())
    for pid in model.NATIVE_PAGES:
        assert by_id[pid].kind == "native", pid
        assert by_id[pid].is_native
    for pid in model.DELEGATE_PAGES:
        assert by_id[pid].kind == "delegate", pid
        assert by_id[pid].subitems, pid
        for s in by_id[pid].subitems:
            assert s.exec, (pid, s.id)


def test_delegate_pages_cover_spec_tools():
    by_id = model.pages_by_id(model.builtin_pages())
    execs = {s.exec[0] for p in by_id.values() for s in p.subitems}
    for tool in ("xfce4-display-settings", "pavucontrol", "xfce4-notifyd-config", "xfce4-power-manager-settings", "baobab", "xfce4-mime-settings", "blueman-manager", "system-config-printer", "mintinstall", "xfce4-session-settings", "nm-connection-editor", "gufw", "users-admin", "mugshot", "mintupdate", "mintdrivers"):
        assert tool in execs, tool
    storage = next(s for s in by_id["system"].subitems if s.id == "storage")
    assert ("gnome-disks",) in storage.alternatives


def test_page_from_dict_validation():
    with pytest.raises(ValueError):
        model.page_from_dict({"id": "x", "title": "X", "kind": "weird"})
    with pytest.raises(ValueError):
        model.page_from_dict({"title": "no id"})
    with pytest.raises(ValueError):
        model.pages_from_data({"pages": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]})
    with pytest.raises(ValueError):
        model.subitem_from_dict({"description": "no label"})


def test_exec_is_never_split_on_whitespace():
    sub = model.subitem_from_dict({"label": "Weird", "exec": "prog with spaces"})
    assert sub.exec == ("prog with spaces",)
    sub2 = model.subitem_from_dict({"label": "VPN", "exec": ["nm-connection-editor", "--create", "--type=vpn"]})
    assert sub2.exec == ("nm-connection-editor", "--create", "--type=vpn")


# --------------------------------------------------------------------------- search
def test_search_filter_pages():
    pages = model.builtin_pages()
    assert model.page_ids(model.filter_pages(pages, "")) == SPEC_ORDER
    assert model.page_ids(model.filter_pages(pages, "   ")) == SPEC_ORDER
    ids = model.page_ids(model.filter_pages(pages, "wallpaper"))
    assert "personalization" in ids and "network" not in ids
    ids = model.page_ids(model.filter_pages(pages, "printer"))
    assert ids == ["system"]
    ids = model.page_ids(model.filter_pages(pages, "VALORANT"))
    assert ids == []
    # multi-token AND
    ids = model.page_ids(model.filter_pages(pages, "gaming proton"))
    assert "gaming" in ids


def test_search_subitems_and_hits():
    by_id = model.pages_by_id(model.builtin_pages())
    system = by_id["system"]
    assert [s.id for s in model.filter_subitems(system, "blue")] == ["bluetooth"]
    assert len(model.filter_subitems(system, "system")) == len(system.subitems)
    assert len(model.filter_subitems(system, "")) == len(system.subitems)
    hits = model.search(model.builtin_pages(), "timeshift")
    assert any(h.page_id == "update" and h.subitem_id == "timeshift" for h in hits)
    assert model.search(model.builtin_pages(), "") == []


def test_text_matches():
    assert model.text_matches("Dark Mode theme", "dark")
    assert model.text_matches("Dark Mode theme", "MODE  dark")
    assert not model.text_matches("Dark Mode theme", "light")
    assert model.text_matches("anything", None)
    assert model.normalize_query("  A   b ") == "a b"


# --------------------------------------------------------------------------- which_or_install
def test_which_or_install_with_fake_path():
    fake_bin = {"pavucontrol": "/usr/bin/pavucontrol", "xfce4-display-settings": "/usr/bin/xfce4-display-settings"}
    which = lambda name: fake_bin.get(name)  # noqa: E731
    assert model.which_or_install(["pavucontrol"], "pavucontrol", which) == ("run", ["pavucontrol"])
    assert model.which_or_install(["gufw"], "gufw", which) == ("install", "gufw")
    assert model.which_or_install(["gufw"], "", which) == ("install", "gufw")
    assert model.which_or_install(["xfce4-display-settings", "--socket-id", "1"], "xfce4-settings", which) == ("run", ["xfce4-display-settings", "--socket-id", "1"])
    assert model.which_or_install([], "pkg", which) == ("install", "pkg")


def test_resolve_subitem_alternatives():
    item = model.subitem_from_dict({"label": "Storage", "exec": ["baobab"], "alternatives": [["gnome-disks"]], "package": "baobab"})
    assert model.resolve_subitem(item, lambda n: None) == ("install", "baobab")
    assert model.resolve_subitem(item, lambda n: "/x" if n == "gnome-disks" else None) == ("run", ["gnome-disks"])
    assert model.resolve_subitem(item, lambda n: "/x") == ("run", ["baobab"])


# --------------------------------------------------------------------------- quick toggles
def test_quick_toggle_round_trip_with_fake_backend():
    be = model.InMemoryToggleBackend(dark=True, compositor=True, config={"gamemode_auto": True, "mangohud": False})
    toggles = {t.id: t for t in model.build_quick_toggles(be)}
    assert list(toggles) == list(model.QUICK_TOGGLE_IDS) == ["dark", "gamemode", "mangohud", "compositor"]
    assert toggles["dark"].get() is True
    assert toggles["dark"].set(False) is True
    assert be.dark is False and toggles["dark"].get() is False
    assert toggles["mangohud"].get() is False
    toggles["mangohud"].set(True)
    assert be.config["mangohud"] is True and toggles["mangohud"].get() is True
    toggles["gamemode"].set(False)
    assert be.config["gamemode_auto"] is False
    toggles["compositor"].set(False)
    assert be.compositor is False and toggles["compositor"].get() is False
    assert "picom" in toggles["compositor"].search_text()


def test_quick_toggle_backend_failure_does_not_raise():
    class Broken:
        def is_dark(self):
            raise OSError("no xfconf")

        def set_dark(self, v):
            raise OSError("no xfconf")

        def config_get(self, k, d=None):
            return d

        def config_set(self, k, v):
            return None

        def compositor_running(self):
            return False

        def set_compositor(self, v):
            return None

    t = {q.id: q for q in model.build_quick_toggles(Broken())}
    assert t["dark"].get() is False
    assert t["dark"].set(True) is False


# --------------------------------------------------------------------------- format helpers
def test_format_helpers():
    assert model.format_mb(512) == "512 MB"
    assert model.format_mb(2048) == "2.0 GB"
    assert model.format_mb("x") == "—"
    assert model.format_bytes(1023) == "1023 B"
    assert model.format_bytes(1536) == "1.5 KB"
    assert model.format_uptime(30) == "< 1 min"
    assert model.format_uptime(3600 * 25 + 60 * 5) == "1 day 1 h 5 min"
    assert model.format_uptime(2 * 86400) == "2 days"
    assert model.format_uptime(90) == "1 min"
    assert model.format_uptime(None) == "—"
    assert model.ram_summary(812, 15872) == "812 MB used of 15.5 GB — target 350–500 MB idle"
    assert model.ram_verdict(300) == "below target"
    assert model.ram_verdict(450) == "within target"
    assert model.ram_verdict(900) == "above target"
    assert model.ram_fraction(500, 1000) == 0.5
    assert model.ram_fraction(5, 0) == 0.0
    assert model.initials("Jagat Goel") == "JG"
    assert model.initials("nitish") == "N"
    assert model.initials("") == "?"
    assert model.display_name("nitish", "Nitish Kumar,,,") == "Nitish Kumar"
    assert model.display_name("nitish", "") == "nitish"
    assert model.mode_display_name("gaming") == "Gaming"
    assert model.mode_display_name(None) == "Everyday"
    assert model.mode_display_name("gaming", {"gaming": {"name": "Gaming!"}}) == "Gaming!"


# --------------------------------------------------------------------------- static data
def test_accents_parsing():
    assert len(model.DEFAULT_ACCENTS) == 8
    setup_style = {"schema": 1, "accents": [{"id": "a", "name": "Aurora Blue", "hex": "#60CDFF"}, {"name": "Bad", "hex": "nope"}]}
    assert model.parse_accents(setup_style) == [("Aurora Blue", "#60CDFF")]
    assert model.parse_accents(["#0067c0"]) == [("#0067C0", "#0067C0")]
    assert model.parse_accents({"Teal": "#00B7C3"}) == [("Teal", "#00B7C3")]
    assert model.parse_accents(None) == list(model.DEFAULT_ACCENTS)
    assert model.parse_accents([f"#{i:06X}" for i in range(12)]) and len(model.parse_accents([f"#{i:06X}" for i in range(12)])) == 8
    assert model.normalize_hex("60cdff") == "#60CDFF"
    assert model.normalize_hex("#GGGGGG") is None


def test_launchers_and_install_items():
    ids = [launcher.id for launcher in model.LAUNCHERS]
    assert ids == ["steam", "lutris", "heroic", "prism", "sober", "vinegar", "mcpelauncher", "bottles"]
    assert set(model.LAUNCHER_INSTALL_ITEMS) <= {"steam", "lutris", "heroic", "prism", "sober", "vinegar", "mcpelauncher", "bottles", "all"}
    steam = model.LAUNCHERS[0]
    assert model.launcher_installed(steam, lambda n: "/usr/bin/steam" if n == "steam" else None)
    assert not model.launcher_installed(steam, lambda n: None)
    assert model.launcher_installed(steam, lambda n: None, flatpaks={"com.valvesoftware.Steam"})
    assert model.launcher_run_argv(steam, lambda n: "/x" if n in ("flatpak",) else None, {"com.valvesoftware.Steam"}) == ["flatpak", "run", "com.valvesoftware.Steam"]
    sober = next(x for x in model.LAUNCHERS if x.id == "sober")
    assert "Sober" in sober.name and "Windows client" in sober.description


def test_power_menu_items_are_win_x_clone():
    items = model.power_menu_items()
    labels = [i.label for i in items]
    assert labels == ["Sleep", "Restart", "Shut down", "Sign out", "Lock", "Settings", "File Explorer", "Terminal", "Task Manager"]
    argv = {i.id: i.argv for i in items}
    assert argv["sleep"] == ("xfce4-session-logout", "--suspend")
    assert argv["restart"] == ("xfce4-session-logout", "--reboot")
    assert argv["shutdown"] == ("xfce4-session-logout", "--halt")
    assert argv["signout"] == ("xfce4-session-logout", "--logout")
    assert argv["lock"] == ("xflock4",)
    assert argv["settings"] == ("lindos-settings",)
    assert argv["files"] == ("thunar",)
    assert argv["terminal"] == ("xfce4-terminal",)
    assert argv["taskmanager"] == ("xfce4-taskmanager",)


def test_honesty_texts():
    t = model.ANTICHEAT_TEXT
    assert "Valorant" in t and "Fortnite" in t and "NOT run" in t
    assert "Sober" in t and "not Windows" in t
    assert any("areweanticheatyet.com" in url for _, url in model.ANTICHEAT_LINKS)
    assert any("protondb.com" in url for _, url in model.ANTICHEAT_LINKS)
    assert "not Windows" in model.WINE_HONESTY_TEXT
    assert model.RAM_TARGET_MIN_MB == 350 and model.RAM_TARGET_MAX_MB == 500


# --------------------------------------------------------------------------- normalisers
def test_normalize_cpu_gpu_ram():
    cpu = model.normalize_cpu({"model": "AMD Ryzen 5 5600X", "cores": 6, "threads": 12, "arch": "x86_64"})
    assert cpu["cores"] == 6 and cpu["threads"] == 12 and cpu["model"].startswith("AMD")
    assert model.normalize_cpu(None)["model"] == "Unknown CPU"
    gpus = model.normalize_gpus([{"vendor": "nvidia", "name": "GeForce RTX 3060", "driver": "nvidia"}, "Intel UHD Graphics"])
    assert gpus[0]["vendor"] == "nvidia" and gpus[0]["model"] == "GeForce RTX 3060" and gpus[0]["driver"] == "nvidia"
    assert gpus[1]["vendor"] == "intel"
    assert model.guess_gpu_vendor("Advanced Micro Devices Radeon RX 6600") == "amd"
    ram = model.normalize_ram({"total": 16000, "available": 12000})
    assert ram["used"] == 4000
    snap = model.normalize_snapshot({"total": 16000, "used": 900, "available": 15100, "top": [("Xorg", 80.5), {"name": "xfwm4", "rss_mb": 30}]})
    assert snap["top"] == [("Xorg", 80.5), ("xfwm4", 30.0)]


def test_normalize_fans_core_shapes():
    core = [{"chip": "k10temp-pci-00c3", "name": "Tctl", "celsius": 45.5}, {"chip": "nct6798-isa-0290", "name": "fan1", "rpm": 812}]
    out = model.normalize_fans(core)
    assert out[0]["kind"] == "temp" and out[0]["unit"] == "°C" and out[0]["value"] == 45.5 and "k10temp" in out[0]["name"]
    assert out[1]["kind"] == "fan" and out[1]["unit"] == "RPM" and out[1]["value"] == 812
    sensors_j = {"coretemp-isa-0000": {"Adapter": "ISA adapter", "Package id 0": {"temp1_input": 52.0, "temp1_max": 100.0}}, "thinkpad-isa-0000": {"fan1": {"fan1_input": 3100.0}}}
    out2 = model.normalize_fans(sensors_j)
    kinds = {o["kind"] for o in out2}
    assert kinds == {"temp", "fan"}


def test_refresh_rates_core_shape_and_xrandr():
    core = {
        "eDP-1": {"connected": True, "current_mode": "1920x1080", "current_rate": 144.0, "modes": {"1920x1080": [144.0, 60.0], "1280x720": [60.0]}, "primary": True},
        "HDMI-1": {"connected": False, "current_mode": None, "current_rate": None, "modes": {}},
    }
    out = model.normalize_refresh_rates(core)
    assert len(out) == 1
    assert out[0]["output"] == "eDP-1" and out[0]["current"] == "144.00" and out[0]["rates"] == ["144.00", "60.00"] and out[0]["resolution"] == "1920x1080"
    text = "Screen 0: minimum 320 x 200\nDP-1 connected primary 2560x1440+0+0\n   2560x1440     59.95*+ 143.97   120.00\n   1920x1080     60.00\nHDMI-1 disconnected\n"
    parsed = model.parse_xrandr(text)
    assert parsed[0]["output"] == "DP-1" and parsed[0]["current"] == "59.95" and parsed[0]["resolution"] == "2560x1440"
    assert parsed[0]["rates"] == ["143.97", "120.00", "59.95"]


def test_compat_matrix_and_recipes():
    data = {"games": [{"name": "Valorant", "status": "not possible", "reason": "Vanguard", "link": "https://areweanticheatyet.com"}, {"name": "Roblox", "status": "works", "how": "Sober"}, {"name": "CS2", "status": "native"}]}
    m = model.parse_compat_matrix(data)
    assert [e["name"] for e in m] == ["CS2", "Roblox", "Valorant"]
    assert m[2]["status"] == "not-possible" and m[1]["how"] == "Sober" and m[0]["status"] == "native"
    assert model.normalize_compat_status("Broken") == "broken"
    assert model.normalize_compat_status("gold") == "works"
    rec = model.parse_recipe({"id": "photoshop-cc-2021", "name": "Photoshop CC 2021", "status": "partial", "runner": "wine"}, "x")
    assert rec["status"] == "partial" and rec["runner"] == "wine"
    assert model.parse_recipe({"name": "no id"}, "fallback-id")["id"] == "fallback-id"
    assert model.parse_recipe("junk") is None


def test_apps_db_and_prefix_path():
    db = {"notepad-plus-plus": {"name": "Notepad++", "exe": "C:/Program Files/npp.exe", "prefix": "notepad-plus-plus", "runner": "wine", "installed_at": "2026-01-01T10:00:00", "kind": "app"}, "zz": {"name": "AAA", "exe": "a.exe"}}
    entries = model.normalize_apps_db(db)
    assert [e["name"] for e in entries] == ["AAA", "Notepad++"]
    assert entries[1]["slug"] == "notepad-plus-plus" and entries[1]["runner"] == "wine"
    assert entries[0]["prefix"] == "zz" and entries[0]["runner"] == "wine"
    assert model.prefix_path("foo", "/home/u/.local/share/lindos/prefixes") == os.path.join("/home/u/.local/share/lindos/prefixes", "foo")
    assert model.prefix_path("/abs/prefix", "/x") == "/abs/prefix"


def test_mode_diff_and_helpers():
    cur = {"governor": "schedutil", "compositor": "picom", "pins": ["a", "b"], "zram_percent": 50}
    tgt = {"governor": "performance", "compositor": "picom", "pins": ["a", "b", "c", "d"], "zram_percent": 75, "packages": ["steam", "lutris"], "flatpaks": ["x"], "services_enable": ["ananicy-cpp"], "services_disable": [], "sysctl": {"vm.max_map_count": "2147483642"}}
    rows = model.mode_diff(cur, tgt)
    d = {r[0]: (r[1], r[2]) for r in rows}
    assert d["CPU governor"] == ("schedutil", "performance")
    assert d["zram size"] == ("50 %", "75 %")
    assert d["Packages"][1].startswith("2 apt + 1 flatpak")
    assert d["Kernel sysctl"][1] == "1 setting"
    assert model.parse_governor_list("schedutil performance powersave") == ["schedutil", "performance", "powersave"]
    assert model.parse_lines("# c\n\nquiet\n performance \n") == ["quiet", "performance"]
    assert model.compositor_state_from_output(0, "picom running") is True
    assert model.compositor_state_from_output(0, "compositor: stopped") is False
    assert model.compositor_state_from_output(1, "running") is False


def test_driver_payload_and_status_summary():
    assert model.driver_install_payload("nvidia", "open") == {"driver": "nvidia-open"}
    assert model.driver_install_payload("nvidia", "proprietary") == {"driver": "nvidia-proprietary"}
    assert model.driver_install_payload("amd") == {"driver": "amd"}
    assert model.driver_install_payload("intel") == {"driver": "intel"}
    assert model.driver_install_payload("auto") == {"args": []}
    st = {"gpus": [{"vendor": "nvidia", "model": "GeForce RTX 3060", "driver": "nouveau"}], "vendors": ["nvidia"], "nvidia_packages": [], "mesa_vulkan": {"amd64": True, "i386": False}, "secure_boot": "enabled", "reboot_required": False, "ubuntu_drivers": [{"package": "nvidia-driver-550", "recommended": True}]}
    s = model.summarize_driver_status(st)
    assert "GeForce RTX 3060 [nouveau]" in s and "not installed" in s and "32-bit: missing" in s and "Secure Boot" in s and "nvidia-driver-550" in s
    assert model.summarize_driver_status("nope") == ""


# --------------------------------------------------------------------------- Windows apps: formats/binfmt/winget
def test_windows_apps_and_gaming_keywords_cover_spec_terms():
    """SPEC-WINDOWS §32 pages.json keywords."""
    by_id = model.pages_by_id(model.builtin_pages())
    wa_kw = " ".join(by_id["windows-apps"].keywords)
    for term in ("winget", "msix", "appx", "msi", "reg", "powershell", "dos", "transfer", "migrate", "easy transfer"):
        assert term in wa_kw, term
    g_kw = " ".join(by_id["gaming"].keywords)
    for term in ("dual boot", "restart into windows", "cloud gaming", "geforce now", "xbox cloud"):
        assert term in g_kw, term


def test_parse_formats_table():
    data = [
        {"id": "msi", "label": "Windows Installer", "suffixes": [".msi"], "mime": "application/x-msi",
         "handler": "msiexec-install", "status": "works", "note": ""},
        {"id": "arm-exe", "label": "ARM program", "suffixes": [".exe"], "handler": "explain",
         "status": "unsupported", "note": "built for ARM Windows"},
        {"id": "msp", "label": ".msp patch", "suffixes": ".msp", "handler": "msiexec-patch", "status": "partial", "note": "fixes the /i bug"},
        "junk",
    ]
    rows = model.parse_formats_table(data)
    assert [r["id"] for r in rows] == ["msi", "msp", "arm-exe"]  # works, partial, unsupported
    assert rows[1]["suffixes"] == [".msp"]  # a bare string suffix is wrapped in a list
    assert rows[2]["note"] == "built for ARM Windows"
    # wrapped shape and odd status both degrade honestly
    assert model.parse_formats_table({"formats": data[:1]})[0]["id"] == "msi"
    assert model.parse_formats_table(None) == []
    assert model.normalize_format_row({"id": "x", "status": "weird"})["status"] == "unsupported"
    assert model.normalize_format_row({})["status"] == "unknown"


def test_normalize_binfmt_status_and_summary():
    st = model.normalize_binfmt_status({
        "registered": True, "enabled": True, "masked": False,
        "conflicts": [{"name": "qemu-x86_64", "interpreter": "/usr/bin/qemu-x86_64-static"}, "junk"],
        "interpreter": "/usr/libexec/lindos/lindos-binfmt", "note": "",
    })
    assert st["conflicts"] == [{"name": "qemu-x86_64", "interpreter": "/usr/bin/qemu-x86_64-static"}]
    summary = model.binfmt_summary(st)
    assert "On" in summary and "qemu-x86_64" in summary and "does not change it" in summary
    off = model.normalize_binfmt_status({"registered": False, "masked": True})
    assert "Off" in model.binfmt_summary(off)
    assert model.normalize_binfmt_status(None) == {
        "registered": False, "enabled": False, "masked": False, "conflicts": [], "interpreter": "", "note": ""}


def test_parse_winget_results():
    data = [
        {"id": "7zip.7zip", "name": "7-Zip", "version": "24.08", "moniker": "7zip", "match": "PackageIdentifier"},
        {"id": "", "name": "no id"},  # dropped: no id
        "junk",
    ]
    rows = model.parse_winget_results(data)
    assert len(rows) == 1 and rows[0]["id"] == "7zip.7zip" and rows[0]["name"] == "7-Zip"
    assert model.parse_winget_results({"not": "a list"}) == []


# --------------------------------------------------------------------------- Gaming: routes / dual boot
def test_normalize_game_route():
    data = {
        "title": "Valorant", "id": "valorant", "status": "not_possible", "anticheat": "Riot Vanguard",
        "region": "US",
        "routes": [
            {"type": "cloud", "provider": "geforce-now", "label": "GeForce NOW", "available": False,
             "why": "not in your library", "requires": [], "action": {"open_url": "https://x"}},
            {"type": "windows", "label": "Restart into Windows", "available": True, "why": "",
             "requires": ["secure-boot", "tpm2"], "action": {}},
            "junk",
        ],
        "recommended": 1, "notes": ["Vanguard blocks VMs"],
    }
    route = model.normalize_game_route(data)
    assert route["title"] == "Valorant" and route["anticheat"] == "Riot Vanguard"
    assert len(route["routes"]) == 2 and route["recommended"] == 1
    assert route["routes"][0]["provider"] == "geforce-now" and route["routes"][0]["available"] is False
    assert route["routes"][1]["requires"] == ["secure-boot", "tpm2"]
    assert route["notes"] == ["Vanguard blocks VMs"]
    # odd recommended index / missing routes degrade honestly instead of raising
    assert model.normalize_game_route({"recommended": 99, "routes": []})["recommended"] == 0
    assert model.normalize_game_route(None) == {
        "title": "", "id": "", "status": "", "anticheat": "", "region": "unknown",
        "routes": [], "recommended": 0, "notes": [], "disclaimer": None}
    dis = {"badge": "Not supported yet", "short": "s", "cause": "c"}
    assert model.normalize_game_route({"disclaimer": dis})["disclaimer"] == dis
    assert model.normalize_game_route({"disclaimer": "junk"})["disclaimer"] is None


def test_normalize_dualboot_status_and_summary():
    st = model.normalize_dualboot_status({
        "firmware": "uefi", "secure_boot": "enabled", "tpm": 2,
        "windows_entries": [{"num": "0001", "label": "Windows Boot Manager", "partuuid": "abcd", "disk": "/dev/nvme0n1"}, "junk"],
        "can_reboot_to_windows": True, "method": "bootnext", "why": "", "lindos_kernel_signed": True,
        "bitlocker_hint": False,
    })
    assert st["windows_entries"] == [{"num": "0001", "label": "Windows Boot Manager", "partuuid": "abcd", "disk": "/dev/nvme0n1"}]
    summary = model.dualboot_summary(st)
    assert "Ready" in summary and "Secure Boot enabled" in summary and "TPM 2" in summary
    blocked = model.normalize_dualboot_status({"can_reboot_to_windows": False, "why": "grubenv is read-only"})
    assert "Not available" in model.dualboot_summary(blocked) and "grubenv" in model.dualboot_summary(blocked)
    assert model.normalize_dualboot_status(None)["can_reboot_to_windows"] is False


def test_region_choices_and_mapping():
    ids = [i for i, _name in model.REGION_CHOICES]
    assert ids[0] == "auto" and "US" in ids and "IN" in ids
    assert model.region_combo_id("") == "auto"
    assert model.region_combo_id("IN") == "IN"
    assert model.region_config_value("auto") == ""
    assert model.region_config_value("") == ""
    assert model.region_config_value("IN") == "IN"


# --------------------------------------------------------------------------- backend (no core, no GTK)
def test_backend_imports_and_degrades_gracefully(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    # never touches lindos-core when asked for static data
    assert b.effective_mode() in model.MODE_IDS
    assert b.config_set("mangohud", True) is True
    assert b.config_get("mangohud") is True
    b2 = be.Backend()
    assert b2.config_get("mangohud") is True  # persisted to LINDOS_HOME/.config/lindos/config.json
    r = b.run(["definitely-not-a-program-xyz"])
    assert r.code == 127 and not r.ok
    assert isinstance(b.accents(), list) and len(b.accents()) == 8
    modes = b.load_modes()
    assert set(model.MODE_IDS) <= set(modes)
    assert b.lindos_release()
    assert isinstance(b.compat_matrix(), list)
    assert isinstance(b.recipes(), list)
    assert isinstance(b.apps_db_load(), list)
    assert b.which("definitely-not-a-program-xyz") is None


# --------------------------------------------------------------------------- backend: Windows apps CLI wrappers
def test_backend_formats_binfmt_winget_missing_cli(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    monkeypatch.setattr(b, "which", lambda cmd: None)
    assert b.formats() == []
    assert b.binfmt_status()["note"] == "lindos-compat is not installed"
    res = b.set_binfmt(True)
    assert res.ok is False and "not installed" in res.err
    assert b.winget_search("firefox") == []
    assert b.winget_install_argv("Mozilla.Firefox") == [
        "lindos-compat", "winget", "install", "Mozilla.Firefox", "--accept-package-agreements"]
    assert b.transfer_gui_available() is False
    assert b.launch_transfer_gui() is False


def test_backend_formats_binfmt_winget_parses_json(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    monkeypatch.setattr(b, "which", lambda cmd: f"/usr/bin/{cmd}")
    calls = []

    def fake_run(argv, timeout=20, env=None, cwd=None):
        calls.append(list(argv))
        rest = argv[1:]
        if rest[:2] == ["formats", "--json"]:
            return be.CmdResult(0, json.dumps([
                {"id": "msi", "label": "Windows Installer", "suffixes": [".msi"], "status": "works", "note": ""}]))
        if rest[:3] == ["binfmt", "status", "--json"]:
            return be.CmdResult(0, json.dumps({"registered": True, "enabled": True, "masked": False,
                                               "conflicts": [], "interpreter": "/usr/libexec/lindos/lindos-binfmt", "note": ""}))
        if rest[:2] == ["binfmt", "enable"]:
            return be.CmdResult(0, json.dumps({"ok": True, "action": "enable", "message": "turned on"}))
        if rest[:2] == ["winget", "search"]:
            return be.CmdResult(0, json.dumps([
                {"id": "Mozilla.Firefox", "name": "Mozilla Firefox", "version": "130.0", "moniker": "firefox", "match": "Name"}]))
        return be.CmdResult(1, "", "unexpected argv " + " ".join(argv))

    monkeypatch.setattr(b, "run", fake_run)
    rows = b.formats()
    assert rows and rows[0]["id"] == "msi"
    st = b.binfmt_status()
    assert st["registered"] and st["enabled"]
    res = b.set_binfmt(True)
    assert res.ok and res.out == "turned on"
    assert calls[-1] == ["lindos-compat", "binfmt", "enable", "--json"]
    results = b.winget_search("firefox", limit=5)
    assert results and results[0]["id"] == "Mozilla.Firefox"
    assert ["lindos-compat", "winget", "search", "firefox", "--limit", "5", "--json"] in calls

    # bad JSON degrades honestly instead of raising
    monkeypatch.setattr(b, "run", lambda argv, **kw: be.CmdResult(0, "{not json"))
    assert b.formats() == []
    assert b.binfmt_status()["note"].startswith("lindos-compat returned")
    assert b.winget_search("x") == []


def test_backend_not_possible_games_filters_status(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    monkeypatch.setattr(b, "compat_matrix", lambda: [
        {"name": "Roblox", "status": "works", "reason": "", "link": "", "how": "Sober"},
        {"name": "Valorant", "status": "not-possible", "reason": "Vanguard", "link": "", "how": ""},
        {"name": "Fortnite", "status": "not-possible", "reason": "EAC disabled by Epic", "link": "", "how": ""},
    ])
    games = b.not_possible_games()
    assert [g["name"] for g in games] == ["Valorant", "Fortnite"]


def test_backend_game_route_region_and_dualboot_missing_cli(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    assert b.region() == ""
    assert b.set_region("IN") is True
    assert b.region() == "IN"

    monkeypatch.setattr(b, "which", lambda cmd: None)
    route = b.game_route("Valorant")
    assert route["title"] == "Valorant" and "not installed" in route["notes"][0]
    assert b.game_route("   ") == model.normalize_game_route({})
    res = b.install_geforce_now()
    assert res.ok is False and "not installed" in res.err
    dstatus = b.dualboot_status()
    assert dstatus["can_reboot_to_windows"] is False and "not installed" in dstatus["why"]
    res2 = b.reboot_to_windows()
    assert res2.ok is False


def test_backend_game_route_and_dualboot_parse_json(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    monkeypatch.setattr(b, "which", lambda cmd: f"/usr/bin/{cmd}")
    calls = []

    def fake_run(argv, timeout=20, env=None, cwd=None):
        calls.append(list(argv))
        if argv[:2] == ["lindos-game", "route"]:
            return be.CmdResult(0, json.dumps({
                "title": "Valorant", "id": "valorant", "status": "not_possible", "anticheat": "Riot Vanguard",
                "region": "US", "routes": [{"type": "windows", "label": "Restart into Windows", "available": True,
                                           "why": "", "requires": ["secure-boot", "tpm2"], "action": {}}],
                "recommended": 0, "notes": []}))
        if argv[:3] == ["lindos-game", "cloud", "install"]:
            return be.CmdResult(0, "installed geforce-now")
        if argv[:2] == ["lindos-dualboot", "status"]:
            return be.CmdResult(0, json.dumps({
                "firmware": "uefi", "secure_boot": "enabled", "tpm": 2, "windows_entries": [],
                "can_reboot_to_windows": True, "method": "bootnext", "why": "", "lindos_kernel_signed": True,
                "bitlocker_hint": False}))
        if argv[:2] == ["lindos-dualboot", "reboot-to-windows"]:
            return be.CmdResult(0, "rebooting")
        return be.CmdResult(1, "", "unexpected argv " + " ".join(argv))

    monkeypatch.setattr(b, "run", fake_run)
    route = b.game_route("Valorant")
    assert route["routes"][0]["type"] == "windows" and route["routes"][0]["available"] is True
    assert route["routes"][0]["requires"] == ["secure-boot", "tpm2"]
    res = b.install_geforce_now()
    assert res.ok and "installed" in res.out
    dstatus = b.dualboot_status()
    assert dstatus["can_reboot_to_windows"] is True and dstatus["secure_boot"] == "enabled" and dstatus["tpm"] == 2
    res2 = b.reboot_to_windows("0001")
    assert res2.ok and ["lindos-dualboot", "reboot-to-windows", "--yes", "--entry", "0001"] in calls


def test_backend_transfer_gui(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    monkeypatch.setattr(b, "which", lambda cmd: "/usr/bin/lindos-transfer-gui" if cmd == "lindos-transfer-gui" else None)
    assert b.transfer_gui_available() is True
    calls = []
    monkeypatch.setattr(be.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert b.launch_transfer_gui() is True
    assert calls == [["lindos-transfer-gui"]]


def test_widgets_and_pages_import_without_real_gtk():
    """Every UI module must be importable (SPEC §4/§12: gi stub or real gi)."""
    import importlib

    for name in ("lindos_settings", "lindos_settings.widgets", "lindos_settings.sidebar", "lindos_settings.pages", "lindos_settings.pages.home", "lindos_settings.pages.about", "lindos_settings.pages.mode", "lindos_settings.pages.gaming", "lindos_settings.pages.hardware", "lindos_settings.pages.windows_apps", "lindos_settings.pages.personalization", "lindos_settings.pages.system", "lindos_settings.pages.apps", "lindos_settings.pages.network", "lindos_settings.pages.accounts", "lindos_settings.pages.update", "lindos_settings.power_menu", "lindos_settings.app"):
        importlib.import_module(name)
    from lindos_settings.pages import PAGE_CLASSES

    assert list(PAGE_CLASSES) == SPEC_ORDER


def test_backend_browsers_and_apps_page(tmp_path, monkeypatch):
    """Settings > Apps browser cards: backend.browsers()/install_browser()/set_default_browser()
    (helper install-browser through lindos.browsers, guarded) and the page builds under the stub."""
    import types

    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("LINDOS_FORCE_OFFLINE", "1")
    b = be.Backend()
    rows = b.browsers()
    assert [r["id"] for r in rows] == ["edge", "chrome", "firefox"]
    for r in rows:
        assert set(("name", "installed", "default")) <= set(r)
        assert isinstance(r["installed"], bool) and isinstance(r["default"], bool)

    # a fake lindos.browsers: install() succeeds, set_default() records the id
    calls: list[tuple[str, str]] = []
    fake = types.SimpleNamespace(
        list_browsers=lambda: [{"id": "edge", "name": "Microsoft Edge", "installed": False, "note": "vendor"},
                               {"id": "firefox", "name": "Mozilla Firefox", "installed": True, "note": "iso"}],
        default_browser=lambda: "firefox",
        install=lambda bid, log=print: (log(f"installing {bid}"), calls.append(("install", bid)))[0] is None,
        set_default=lambda bid: calls.append(("default", bid)) is None,
    )
    b._mods["browsers"] = fake
    rows = b.browsers()
    assert rows[1]["default"] is True and rows[0]["default"] is False
    res = b.install_browser("edge", set_default=True)
    assert res.ok and ("install", "edge") in calls and ("default", "edge") in calls
    assert b.config_get("browser") == "edge"
    assert b.set_default_browser("firefox") is True and b.config_get("browser") == "firefox"

    # page smoke build (gi stub or real GTK)
    from lindos_settings.pages.apps import AppsPage

    page = model.pages_by_id(model.load_pages())["apps"]
    app = types.SimpleNamespace(backend=b, window=None, show_page=lambda pid: None)
    p = AppsPage(app, page)
    assert set(p._browser_cards) == {"edge", "chrome", "firefox"}
    p.on_show()
    p._refresh_browsers()


def test_windows_apps_page_builds_new_sections(tmp_path, monkeypatch):
    """SPEC-WINDOWS §32: formats/binfmt/winget/transfer sections build under the gi stub and
    never touch the network (lindos-compat is simply absent on the test machine)."""
    import types

    from lindos_settings import backend as be
    from lindos_settings.pages.windows_apps import WindowsAppsPage

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    page = model.pages_by_id(model.load_pages())["windows-apps"]
    app = types.SimpleNamespace(backend=b, window=None, toast=lambda *_a, **_k: None)
    p = WindowsAppsPage(app, page)
    assert p.formats_card is not None
    assert p.binfmt_card is not None
    assert p.winget_entry is not None and p.winget_results is not None
    assert p.transfer_card is not None
    p.on_show()
    p._winget_search()          # empty query -> placeholder, no crash, no subprocess


def test_gaming_page_builds_needs_windows_section(tmp_path, monkeypatch):
    """SPEC-WINDOWS §32: the 'Games that need Windows' card + region selector build under the
    gi stub without lindos-game/lindos-dualboot installed."""
    import types

    from lindos_settings import backend as be
    from lindos_settings.pages.gaming import GamingPage

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    page = model.pages_by_id(model.load_pages())["gaming"]
    app = types.SimpleNamespace(backend=b, window=None, toast=lambda *_a, **_k: None)
    p = GamingPage(app, page)
    assert p.region_card is not None
    assert p.needs_windows_section is not None
    p.on_show()


def test_compat_disclaimer_parsing_and_kind_passthrough():
    data = {"disclaimer": {"badge": "Not supported yet", "short": "s", "long": "l", "via": "v", "kinds": {"a": "b"}},
            "entries": [{"game": "Valorant", "status": "not_possible", "unsupported_kind": "no-linux-version"},
                        {"game": "Roblox", "status": "works"}]}
    assert model.parse_compat_disclaimer(data) == {"badge": "Not supported yet", "short": "s", "long": "l",
                                                   "via": "v", "kinds": {"a": "b"}}
    assert model.parse_compat_disclaimer({"entries": []}) == {}
    assert model.parse_compat_disclaimer(None) == {}
    rows = {e["name"]: e for e in model.parse_compat_matrix(data)}
    assert rows["Valorant"]["kind"] == "no-linux-version" and rows["Valorant"]["status"] == "not-possible"
    assert rows["Roblox"]["kind"] == ""
    assert model.BADGE_NOT_SUPPORTED_YET == "not-supported-yet"


def test_backend_compat_disclaimer_reads_the_shipped_matrix(tmp_path, monkeypatch):
    from lindos_settings import backend as be

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    b = be.Backend()
    assert b.compat_disclaimer() == {}                       # no matrix installed: honest empty, no crash
    dest = tmp_path / "root" / "usr" / "share" / "lindos" / "compat-matrix.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"disclaimer": {"badge": "Not supported yet", "short": "s"}, "entries": []}),
                    encoding="utf-8")
    assert b.compat_disclaimer()["badge"] == "Not supported yet"


# --------------------------------------------------------------------------- "Not supported yet" (SPEC 0.1)
_SHIPPED_MATRIX = os.path.normpath(os.path.join(
    HERE, "..", "..", "lindos-gaming", "root", "usr", "share", "lindos", "compat-matrix.json"))


def test_shipped_matrix_disclaimer_reaches_settings_intact():
    if not os.path.isfile(_SHIPPED_MATRIX):
        pytest.skip("lindos-gaming not present in this checkout")
    with open(_SHIPPED_MATRIX, encoding="utf-8") as fh:
        data = json.load(fh)
    dis = model.parse_compat_disclaimer(data)
    assert dis["badge"] == "Not supported yet" and dis["long"] == data["disclaimer"]["long"]
    assert dis["short"] == data["disclaimer"]["short"] and set(dis["kinds"]) == {"no-linux-version", "publisher-disabled"}
    rows = model.parse_compat_matrix(data)
    tagged = [r["name"] for r in rows if r["kind"]]
    assert len(tagged) == 14 and all(r["status"] == "not-possible" for r in rows if r["kind"])
    # the Xbox app is blocked by Store licensing, not an anti-cheat: it keeps a plain "Not possible" badge
    assert next(r for r in rows if r["name"] == "Xbox app / PC Game Pass")["kind"] == ""


def test_anticheat_text_pairs_not_supported_yet_with_the_publisher():
    low = model.ANTICHEAT_TEXT.lower()
    assert "not supported on lindos yet" in low and "publishers" in low and "cannot promise when" in low
    for phrase in ("coming soon", "will be supported", "will be coming"):
        assert phrase not in low


def test_not_supported_yet_badge_has_a_css_rule_and_the_page_is_searchable():
    css = open(os.path.join(PKG_ROOT, "usr", "lib", "lindos-settings", "ui", "settings.css"), encoding="utf-8").read()
    assert ".badge-" + model.BADGE_NOT_SUPPORTED_YET in css
    gaming = model.pages_by_id(model.builtin_pages())["gaming"]
    assert "not supported yet" in gaming.keywords
    assert "gaming" in model.page_ids(model.filter_pages(model.builtin_pages(), "not supported yet"))


def test_badge_takes_a_text_override_and_a_tooltip(monkeypatch):
    import types

    from lindos_settings import widgets

    class FakeLabel:
        def __init__(self, label=""):
            self.label, self.tip, self.classes = label, None, ()

        def set_valign(self, _v):
            pass

        def set_tooltip_text(self, tip):
            self.tip = tip

    monkeypatch.setattr(widgets, "Gtk", types.SimpleNamespace(Label=FakeLabel, Align=types.SimpleNamespace(CENTER=0)))
    monkeypatch.setattr(widgets, "add_class", lambda w, *c: setattr(w, "classes", c))
    lbl = widgets.badge("not-supported-yet", "Not supported yet", "publisher decides")
    assert (lbl.label, lbl.tip) == ("Not supported yet", "publisher decides")
    assert "badge-not-supported-yet" in lbl.classes
    assert widgets.badge("not-possible").label == "Not possible"          # other users of badge() are unchanged
    assert widgets.badge("works").tip is None


class _Rec:
    """Records the widget constructors the Gaming page calls (no GTK needed)."""

    def __init__(self, *args, **kwargs):
        self.args, self.controls = args, []

    def set_control(self, ctl):
        self.controls.append(ctl)

    add_control = set_control


class _Section:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items = []

    def add(self, item):
        self.items.append(item)

    def show_all(self):
        pass


def test_gaming_page_marks_only_disclaimed_games_and_never_shows_the_badge_bare(tmp_path, monkeypatch):
    from lindos_settings import backend as be
    from lindos_settings.pages import gaming

    monkeypatch.setenv("LINDOS_HOME", str(tmp_path))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    matrix = {"disclaimer": {"badge": "Not supported yet", "short": "publisher decides, no date", "long": "LONG TEXT"},
              "entries": [{"game": "Valorant", "status": "not_possible", "unsupported_kind": "no-linux-version"},
                          {"game": "Xbox app / PC Game Pass", "status": "not_possible"},
                          {"game": "Elden Ring", "status": "works"}]}
    dest = tmp_path / "root" / "usr" / "share" / "lindos" / "compat-matrix.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps(matrix), encoding="utf-8")

    monkeypatch.setattr(gaming, "run_async", lambda fn, done=None, name="": done(fn(), None))
    monkeypatch.setattr(gaming, "Card", _Rec)
    monkeypatch.setattr(gaming, "InfoCard", _Rec)
    monkeypatch.setattr(gaming, "button", lambda *a, **k: ("button", a))
    monkeypatch.setattr(gaming, "badge", lambda *a: ("badge",) + a)
    page = gaming.GamingPage.__new__(gaming.GamingPage)
    page.backend = be.Backend()
    page.needs_windows_section = _Section()
    page._refresh_needs_windows()

    first, valorant, xbox = page.needs_windows_section.items
    assert first.args[:2] == ("Not supported yet", "LONG TEXT")          # the disclaimer paragraph leads the section
    assert valorant.args[0] == "Valorant" and xbox.args[0] == "Xbox app / PC Game Pass"
    # the badge carries the publisher-decides sentence as its tooltip; the Xbox app gets none
    assert ("badge", "not-supported-yet", "Not supported yet", "publisher decides, no date") in valorant.controls
    assert not any(isinstance(c, tuple) and c[:1] == ("badge",) for c in xbox.controls)
