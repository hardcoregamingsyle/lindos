"""Play-anywhere: honest routes for games Linux cannot run (SPEC-WINDOWS §30, W-F).

Covers: compat-matrix.json "cloud_providers" + per-entry "routes" data invariants
(§30.1), route computation in lindos-game (region / dual-boot / client detection,
§30.2) with everything network/host-dependent mocked, .desktop shortcut writing
under LINDOS_HOME, the GeForce NOW Flatpak install path (NVIDIA's own remote, not
Flathub) and the no-evasion-token guarantee (SPEC-WINDOWS §27.1).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import SHARE, load_bin, parse_desktop, read_json  # noqa: E402

MATRIX_PATH = SHARE / "compat-matrix.json"

# Official domains the research (games-routing.md) verified for each provider; a
# route URL pointing anywhere else would not be an "official provider URL" (§30.1).
PROVIDER_DOMAINS = {
    "geforce-now": ("nvidia.com",),
    "xbox-cloud": ("xbox.com",),
    "boosteroid": ("boosteroid.com",),
    "amazon-luna": ("amazon.com",),
}

FORBIDDEN_TOKENS = ("attest", "vanguard", "battleye", " eac", "eac ", "tpm-spoof", "secure-boot-spoof",
                    "hwid", "patchguard", "spoof", "bypass", "fake-tpm", "faketpm", "hv-vendor-id",
                    "kvm=off", "user-agent", "useragent")


def _domain_ok(url: str, provider_id: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in PROVIDER_DOMAINS[provider_id])


@pytest.fixture(scope="module")
def matrix():
    return read_json(MATRIX_PATH)


@pytest.fixture(scope="module")
def game():
    return load_bin("lindos-game")


def _find(entries, fragment: str):
    frag = fragment.casefold()
    exact = [e for e in entries if e["game"].casefold() == frag]
    if exact:
        return exact[0]
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(frag) + r"(?![a-z0-9])")
    hits = [e for e in entries if pat.search(e["game"].casefold())]
    return hits[0] if hits else None


# --------------------------------------------------------------------------- data invariants
def test_cloud_providers_present_and_shaped(matrix):
    providers = matrix["cloud_providers"]
    assert set(providers) == {"geforce-now", "xbox-cloud", "boosteroid", "amazon-luna"}
    for pid, p in providers.items():
        assert p["name"] and p["url"].startswith("https://")
        assert _domain_ok(p["url"], pid), f"{pid}: url not on its official domain: {p['url']}"
        assert p["linux"] in ("official-app", "browser-unofficial")
        browsers = p.get("browsers_official", [])
        assert "firefox" not in browsers, f"{pid}: Firefox is not an official browser for any provider here"
        assert isinstance(p.get("note", ""), str) and p["note"]


def test_geforce_now_client_is_flatpak_not_flathub(matrix):
    gfn = matrix["cloud_providers"]["geforce-now"]
    assert gfn["client"] == {"type": "flatpak", "id": "com.nvidia.geforcenow", "remote": "GeForceNOW"}
    assert gfn["client"]["remote"] != "flathub"


def test_boosteroid_region_group_and_no_browser_claim(matrix):
    b = matrix["cloud_providers"]["boosteroid"]
    assert set(b["regions"]) == {"EU", "NA", "BR"}
    assert not b.get("browsers_official"), "Boosteroid is an installed client here, not a browser route"


def test_every_not_possible_entry_has_honest_routes(matrix):
    entries = matrix["entries"]
    providers = matrix["cloud_providers"]
    not_possible = [e for e in entries if e["status"] == "not_possible"]
    assert len(not_possible) == 15
    for e in not_possible:
        assert "routes" in e, f"{e['game']}: not_possible entry needs a 'routes' block"
        r = e["routes"]
        assert r["windows"] is True, f"{e['game']}: windows route must be true (SPEC §30.1)"
        assert r["vm"] is False, f"{e['game']}: vm route must be false for every not_possible title"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["verified"])
        assert isinstance(r.get("windows_requires"), list)
        for req in r["windows_requires"]:
            assert req in ("secure-boot", "tpm2", "iommu", "vbs")
        for c in r.get("cloud", []):
            assert c["provider"] in providers, f"{e['game']}: unknown cloud provider {c['provider']!r}"
            assert c["url"].startswith("https://"), f"{e['game']}: cloud route url must be https"
            assert _domain_ok(c["url"], c["provider"]), \
                f"{e['game']}: {c['provider']} url not on its official domain: {c['url']}"
            assert c["tier"] in ("free", "premium")


def test_no_league_of_legends_on_geforce_now(matrix):
    """Errata fix (games-routing.md): NVIDIA pulled LoL from GeForce NOW on 2024-05-01."""
    lol = _find(matrix["entries"], "league of legends")
    assert lol is not None and lol["status"] == "not_possible"
    cloud = lol.get("routes", {}).get("cloud", [])
    assert all(c["provider"] != "geforce-now" for c in cloud)
    assert not cloud, "League of Legends has no cloud route at all"
    assert "vanguard" in lol["anticheat"].lower()


def test_no_valorant_cloud_route(matrix):
    val = _find(matrix["entries"], "valorant")
    assert val["routes"]["cloud"] == []
    assert val["routes"]["windows_requires"] == ["secure-boot", "tpm2"]


def test_no_firefox_for_xbox_cloud_or_luna(matrix):
    for pid in ("xbox-cloud", "amazon-luna"):
        browsers = matrix["cloud_providers"][pid]["browsers_official"]
        assert "firefox" not in browsers
        assert set(browsers) <= {"chrome", "edge"}


def test_fortnite_errata_fixed(matrix):
    fn = _find(matrix["entries"], "fortnite")
    assert "battleye" not in fn["anticheat"].lower() or "removed" in fn["anticheat"].lower()
    providers = {c["provider"] for c in fn["routes"]["cloud"]}
    assert providers == {"geforce-now", "xbox-cloud", "boosteroid", "amazon-luna"}
    assert fn["routes"]["windows_requires"] == []


def test_apex_legends_javelin_errata(matrix):
    apex = _find(matrix["entries"], "apex legends")
    assert "javelin" in apex["anticheat"].lower()
    assert apex["routes"]["windows_requires"] == []


def test_escape_from_tarkov_steam_errata(matrix):
    tarkov = _find(matrix["entries"], "escape from tarkov")
    assert "steam" in tarkov["reason"].lower()


def test_xbox_app_pc_game_pass_errata(matrix):
    xb = _find(matrix["entries"], "xbox app / pc game pass")
    reason = xb["reason"].lower()
    assert "edge/chrome/firefox" not in reason, "the old (wrong) browser list must be gone"
    assert "never firefox" in reason or "not firefox" in reason
    assert "chrome" in reason and "edge" in reason
    assert "pc game pass" in reason and "not include cloud" in reason


def test_call_of_duty_requires_secure_boot_tpm(matrix):
    cod = _find(matrix["entries"], "call of duty")
    assert set(cod["routes"]["windows_requires"]) == {"secure-boot", "tpm2"}


def test_no_evasion_tokens_in_matrix_text(matrix):
    blob = json.dumps(matrix).lower()
    for tok in ("kvm=off", "hv-vendor-id", "faketpm", "fake-tpm", "user-agent spoof"):
        assert tok not in blob


# --------------------------------------------------------------------------- region detection
def test_region_priority_explicit_wins(game):
    assert game.detect_region("in", config={"region": "us"}, env={"LANG": "de_DE.UTF-8"}) == "IN"


def test_region_from_config(game):
    assert game.detect_region(None, config={"region": "gb"}, env={}) == "GB"


def test_region_from_locale(game):
    assert game.detect_region(None, config={}, env={"LANG": "en_IN.UTF-8"}) == "IN"
    assert game.detect_region(None, config={}, env={"LC_ALL": "de_DE.UTF-8"}) == "DE"
    assert game.detect_region(None, config={}, env={"LANG": "C"}) is None


def test_region_from_timezone(game, tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "timezone").write_text("Asia/Kolkata\n", encoding="utf-8")
    assert game.detect_region(None, config={}, env={}, root=root) == "IN"


def test_region_unknown_when_nothing_resolves(game, tmp_path):
    root = tmp_path / "empty-root"
    root.mkdir()
    assert game.detect_region(None, config={}, env={}, root=root) is None


# --------------------------------------------------------------------------- provider availability
def test_provider_region_available_excluded_list(game, matrix):
    luna = matrix["cloud_providers"]["amazon-luna"]
    assert game.provider_region_available(luna, "IN") is False
    assert game.provider_region_available(luna, "US") is True
    assert game.provider_region_available(luna, None) is None


def test_provider_region_available_allowlist_groups(game, matrix):
    boosteroid = matrix["cloud_providers"]["boosteroid"]
    assert game.provider_region_available(boosteroid, "DE") is True   # EU
    assert game.provider_region_available(boosteroid, "US") is True   # NA
    assert game.provider_region_available(boosteroid, "BR") is True
    assert game.provider_region_available(boosteroid, "IN") is False


def test_provider_client_ready_flatpak(game, matrix, monkeypatch):
    gfn = matrix["cloud_providers"]["geforce-now"]
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"com.nvidia.geforcenow"})
    ok, via = game.provider_client_ready(gfn)
    assert ok is True and via == "flatpak:com.nvidia.geforcenow"
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    ok, via = game.provider_client_ready(gfn)
    assert ok is False


def test_provider_client_ready_browser_never_firefox(game, matrix):
    xcloud = matrix["cloud_providers"]["xbox-cloud"]

    def fake_which(name):
        return "/usr/bin/" + name if "firefox" not in name else None

    ok, path = game.provider_client_ready(xcloud, which=fake_which)
    assert ok is True and "firefox" not in path

    def none_which(name):
        return None

    ok, path = game.provider_client_ready(xcloud, which=none_which)
    assert ok is False


def test_pick_browser_order_follows_provider_list(game):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/usr/bin/microsoft-edge-stable" if name == "microsoft-edge-stable" else None

    path, bid = game.pick_browser(["edge", "chrome"], which=fake_which)
    assert bid == "edge" and path == "/usr/bin/microsoft-edge-stable"


# --------------------------------------------------------------------------- dual-boot status
def test_dualboot_status_missing_binary(game, monkeypatch):
    monkeypatch.setattr(game, "_which", lambda name: None)
    st = game.dualboot_status()
    assert st["can_reboot_to_windows"] is False
    assert "lindos-dualboot" in st["why"]


def test_dualboot_status_parses_json(game, monkeypatch):
    monkeypatch.setattr(game, "_which", lambda name: "/usr/bin/lindos-dualboot")
    payload = {"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 2, "firmware": "uefi"}

    def fake_run(cmd, timeout=30.0):
        assert cmd[1:] == ["status", "--json"]
        return 0, json.dumps(payload)

    monkeypatch.setattr(game, "run", fake_run)
    st = game.dualboot_status()
    assert st["can_reboot_to_windows"] is True and st["secure_boot"] == "enabled"


# --------------------------------------------------------------------------- build_routes
@pytest.fixture()
def valorant_entry(matrix):
    return _find(matrix["entries"], "valorant")


def test_build_routes_vm_never_available(game, matrix, valorant_entry):
    routes = game.build_routes(valorant_entry, matrix, "IN",
                               dualboot={"can_reboot_to_windows": False, "why": "no entry"})
    vm = [r for r in routes if r["type"] == "vm"][0]
    assert vm["available"] is False
    assert "vanguard" in vm["why"].lower()
    assert valorant_entry["routes"]["cloud"] == []
    assert not any(r["type"] == "cloud" for r in routes)


def test_build_routes_windows_needs_secure_boot_and_tpm(game, matrix, valorant_entry):
    routes = game.build_routes(valorant_entry, matrix, "IN",
                               dualboot={"can_reboot_to_windows": True, "secure_boot": "disabled", "tpm": None})
    win = [r for r in routes if r["type"] == "windows"][0]
    assert win["available"] is True
    assert "secure boot" in win["why"].lower() and "tpm" in win["why"].lower()


def test_build_routes_windows_tpm1_not_treated_as_tpm2(game, matrix, valorant_entry):
    # A TPM 1.2 chip reports major version 1, which is truthy but not TPM 2.0.
    # build_routes() must still flag the "tpm2" requirement as missing.
    routes = game.build_routes(valorant_entry, matrix, "IN",
                               dualboot={"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 1})
    win = [r for r in routes if r["type"] == "windows"][0]
    assert win["available"] is True
    assert "no tpm 2.0 detected" in win["why"].lower()


def test_build_routes_windows_unavailable_when_no_boot_entry(game, matrix, valorant_entry):
    routes = game.build_routes(valorant_entry, matrix, "IN",
                               dualboot={"can_reboot_to_windows": False, "why": "no Windows entry found"})
    win = [r for r in routes if r["type"] == "windows"][0]
    assert win["available"] is False
    assert win["action"] is None


def test_build_routes_fortnite_cloud_region_and_client(game, matrix, monkeypatch):
    fn = _find(matrix["entries"], "fortnite")
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game, "_which", lambda name: None)
    routes = game.build_routes(fn, matrix, "IN", dualboot={"can_reboot_to_windows": True})
    by_provider = {r["provider"]: r for r in routes if r["type"] == "cloud"}
    assert by_provider["geforce-now"]["available"] is False   # flatpak not installed
    assert by_provider["amazon-luna"]["available"] is False   # India excluded
    assert by_provider["boosteroid"]["available"] is False    # India not in EU/NA/BR
    assert by_provider["xbox-cloud"]["available"] is False    # no browser found


def test_build_routes_fortnite_geforce_now_available_when_installed(game, matrix, monkeypatch):
    fn = _find(matrix["entries"], "fortnite")
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"com.nvidia.geforcenow"})
    routes = game.build_routes(fn, matrix, "IN", dualboot={"can_reboot_to_windows": True})
    gfn = [r for r in routes if r["provider"] == "geforce-now"][0]
    assert gfn["available"] is True
    assert gfn["action"] == {"cmd": ["flatpak", "run", "com.nvidia.geforcenow"]}


def test_build_routes_status_not_blocked_returns_single_route(game, matrix):
    elden = _find(matrix["entries"], "elden ring")
    routes = game.build_routes(elden, matrix, "IN")
    assert len(routes) == 1 and routes[0]["type"] == "proton" and routes[0]["available"] is True


def test_recommend_prefers_available_cloud_then_windows(game):
    routes = [
        {"type": "cloud", "available": False},
        {"type": "cloud", "available": True},
        {"type": "windows", "available": True},
    ]
    assert game._recommend(routes) == 1
    routes2 = [{"type": "cloud", "available": False}, {"type": "windows", "available": True}]
    assert game._recommend(routes2) == 1
    routes3 = [{"type": "cloud", "available": False}, {"type": "windows", "available": False}]
    assert game._recommend(routes3) == 0


# --------------------------------------------------------------------------- resolve_title
def test_resolve_title_by_exact_title(game, matrix):
    entry = game.resolve_title(matrix, [], "Valorant")
    assert entry is not None and entry["game"] == "Valorant"


def test_resolve_title_by_exe_via_profile(game, matrix):
    profiles = game.load_profiles()
    entry = game.resolve_title(matrix, profiles, "RiotClientServices.exe")
    assert entry is not None and entry["game"] == "Valorant"
    entry2 = game.resolve_title(matrix, profiles, "SomeUnrelatedGame.exe")
    assert entry2 is None


def test_resolve_title_unknown_returns_none(game, matrix):
    assert game.resolve_title(matrix, [], "Some Game Nobody Ships") is None


# --------------------------------------------------------------------------- cmd_route / cmd_play
def test_cmd_route_json(game, matrix, capsys, monkeypatch):
    monkeypatch.setattr(game, "dualboot_status",
                        lambda: {"can_reboot_to_windows": False, "why": "no entry"})
    args = game.build_parser().parse_args(["route", "Valorant", "--json", "--region", "IN"])
    rc = game.cmd_route(args, matrix, [])
    assert rc == game.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["title"] == "Valorant" and data["region"] == "IN"
    assert data["status"] == "not_possible"
    assert any(r["type"] == "windows" for r in data["routes"])
    assert any(r["type"] == "vm" and r["available"] is False for r in data["routes"])


def test_cmd_route_unknown_game(game, matrix, capsys):
    args = game.build_parser().parse_args(["route", "Not A Real Game At All", "--json"])
    rc = game.cmd_route(args, matrix, [])
    assert rc == game.EXIT_USAGE


def test_cmd_play_refuses_vm_route(game, matrix, capsys):
    args = game.build_parser().parse_args(["play", "Valorant", "--route", "vm"])
    rc = game.cmd_play(args, matrix, [])
    assert rc == game.EXIT_USAGE


def test_cmd_play_windows_route_asks_and_runs_dualboot(game, matrix, monkeypatch, capsys):
    monkeypatch.setattr(game, "have", lambda b: False)  # no zenity -> falls back to input()
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr(game, "_which", lambda name: "/usr/bin/lindos-dualboot" if name == "lindos-dualboot" else None)
    calls = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[1:] == ["status", "--json"]:
            return 0, json.dumps({"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 2})
        if cmd[1:3] == ["reboot-to-windows", "--yes"]:
            return 0, "rebooting\n"
        return 127, ""

    monkeypatch.setattr(game, "run", fake_run)
    args = game.build_parser().parse_args(["play", "Valorant", "--route", "windows"])
    rc = game.cmd_play(args, matrix, [])
    assert rc == game.EXIT_OK
    assert any(c[1:3] == ["reboot-to-windows", "--yes"] for c in calls)


def test_cmd_play_windows_route_declined_does_not_reboot(game, matrix, monkeypatch):
    monkeypatch.setattr(game, "have", lambda b: False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    monkeypatch.setattr(game, "_which", lambda name: "/usr/bin/lindos-dualboot")
    monkeypatch.setattr(game, "run", lambda cmd, timeout=30.0: (
        0, json.dumps({"can_reboot_to_windows": True, "secure_boot": "enabled", "tpm": 2})))
    args = game.build_parser().parse_args(["play", "Valorant", "--route", "windows"])
    rc = game.cmd_play(args, matrix, [])
    assert rc == game.EXIT_OK  # cancelling is not an error


def test_cmd_play_cloud_route_launches_geforce_now(game, matrix, monkeypatch):
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"com.nvidia.geforcenow"})
    launched = {}

    def fake_execvp(prog, argv):
        launched["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(game.os, "execvp", fake_execvp)
    monkeypatch.setattr(game.os, "name", "posix")
    args = game.build_parser().parse_args(["play", "Fortnite", "--route", "cloud",
                                          "--provider", "geforce-now", "--region", "IN"])
    with pytest.raises(SystemExit):
        game.cmd_play(args, matrix, [])
    assert launched["argv"] == ["flatpak", "run", "com.nvidia.geforcenow"]


def test_cmd_play_cloud_route_not_available_errors(game, matrix, monkeypatch):
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game, "_which", lambda name: None)
    args = game.build_parser().parse_args(["play", "Fortnite", "--route", "cloud",
                                          "--provider", "geforce-now", "--region", "IN"])
    rc = game.cmd_play(args, matrix, [])
    assert rc == game.EXIT_ERROR


# --------------------------------------------------------------------------- shortcut writing
def test_cmd_shortcut_windows_route(game, matrix, fake_home, monkeypatch):
    monkeypatch.setattr(game, "dualboot_status",
                        lambda: {"can_reboot_to_windows": True, "secure_boot": "disabled", "tpm": None})
    args = game.build_parser().parse_args(["shortcut", "Valorant", "--route", "windows"])
    rc = game.cmd_shortcut(args, matrix, [])
    assert rc == game.EXIT_OK
    dest = fake_home / ".local" / "share" / "applications" / "lindos-play-valorant-windows.desktop"
    assert dest.is_file()
    groups = parse_desktop(dest)
    entry = groups["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert "restarts into Windows" in entry["Name"]
    assert "lindos-game" in entry["Exec"] and "play" in entry["Exec"] and "--yes" in entry["Exec"]
    assert entry["Terminal"] == "false"
    blob = dest.read_text(encoding="utf-8").lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in blob, f"shortcut must not contain an evasion token: {tok!r}"


def test_cmd_shortcut_cloud_route(game, matrix, fake_home, monkeypatch):
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"com.nvidia.geforcenow"})
    args = game.build_parser().parse_args(["shortcut", "Fortnite", "--route", "cloud",
                                          "--provider", "geforce-now", "--region", "IN"])
    rc = game.cmd_shortcut(args, matrix, [])
    assert rc == game.EXIT_OK
    dest = fake_home / ".local" / "share" / "applications" / "lindos-play-fortnite-cloud.desktop"
    assert dest.is_file()
    entry = parse_desktop(dest)["Desktop Entry"]
    assert "GeForce NOW" in entry["Name"] or "geforce-now" in entry.get("Exec", "")
    assert "--provider" in entry["Exec"] and "geforce-now" in entry["Exec"]


def test_cmd_shortcut_unknown_route_for_query(game, matrix, fake_home):
    args = game.build_parser().parse_args(["shortcut", "Elden Ring", "--route", "cloud"])
    rc = game.cmd_shortcut(args, matrix, [])
    assert rc == game.EXIT_USAGE


# --------------------------------------------------------------------------- cloud install (GeForce NOW)
def test_geforce_now_flatpak_commands_exact():
    game_mod = load_bin("lindos-game")
    cmds = game_mod.geforce_now_flatpak_commands("user")
    assert cmds[0] == ["flatpak", "remote-add", "--user", "--if-not-exists", "GeForceNOW",
                       "https://international.download.nvidia.com/GFNLinux/flatpak/geforcenow.flatpakrepo"]
    assert cmds[1] == ["flatpak", "install", "--user", "-y", "--noninteractive", "GeForceNOW",
                       "com.nvidia.geforcenow"]
    blob = " ".join(" ".join(c) for c in cmds).lower()
    assert "flathub" not in blob


def test_cmd_cloud_install_prints_when_no_flatpak(game, monkeypatch, capsys):
    monkeypatch.setattr(game, "have", lambda b: False)
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_ERROR
    out = capsys.readouterr().out
    assert "flatpak remote-add" in out and "GeForceNOW" in out


def test_cmd_cloud_install_runs_commands(game, monkeypatch):
    monkeypatch.setattr(game, "have", lambda b: True)
    calls = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(game, "run", fake_run)
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_OK
    assert len(calls) == 2
    assert calls[0][:3] == ["flatpak", "remote-add", "--user"]


class _FakeHelperResult:
    def __init__(self, ok, message=""):
        self.ok = ok
        self.message = message


def test_cmd_cloud_install_system_uses_helper_when_available(game, monkeypatch):
    """SPEC-WINDOWS §33.1: cloud install geforce-now --system -> helper install-flatpaks (NVIDIA remote)."""
    seen = {}

    def fake_helper(ids, remote):
        seen["ids"] = ids
        seen["remote"] = remote
        return _FakeHelperResult(True)

    monkeypatch.setattr(game, "_try_helper_install_flatpaks", fake_helper)

    def fail_run(cmd, timeout=30.0):
        raise AssertionError("must not shell out to flatpak directly when the helper succeeds")

    monkeypatch.setattr(game, "run", fail_run)
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now", "--system"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_OK
    assert seen["ids"] == ["com.nvidia.geforcenow"]
    assert seen["remote"] == {"name": "GeForceNOW",
                              "url": "https://international.download.nvidia.com/GFNLinux/flatpak/geforcenow.flatpakrepo"}


def test_cmd_cloud_install_system_helper_failure_prints_manual_commands(game, monkeypatch, capsys):
    monkeypatch.setattr(game, "_try_helper_install_flatpaks",
                        lambda ids, remote: _FakeHelperResult(False, "flatpak install failed: boom"))
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now", "--system"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_ERROR
    out = capsys.readouterr().out
    assert "--system" in " ".join(out.split())
    assert "flatpak remote-add" in out


def test_cmd_cloud_install_system_falls_back_without_helper(game, monkeypatch):
    """No lindos.helper here (dev box) -> falls back to a --user install, no root needed."""
    monkeypatch.setattr(game, "_try_helper_install_flatpaks", lambda ids, remote: None)
    monkeypatch.setattr(game, "have", lambda b: True)
    calls = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(game, "run", fake_run)
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now", "--system"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_OK
    assert calls and calls[0][:3] == ["flatpak", "remote-add", "--user"]


def test_cmd_cloud_install_json_never_runs(game, monkeypatch, capsys):
    monkeypatch.setattr(game, "have", lambda b: True)

    def fail_run(cmd, timeout=30.0):
        raise AssertionError("must not execute in --json mode")

    monkeypatch.setattr(game, "run", fail_run)
    args = game.build_parser().parse_args(["cloud", "install", "geforce-now", "--json"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["app_id"] == "com.nvidia.geforcenow"
    assert data["remote"]["name"] == "GeForceNOW"


def test_cmd_cloud_install_unknown_provider(game, capsys):
    args = game.build_parser().parse_args(["cloud", "install", "boosteroid"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_OK
    out = capsys.readouterr().out
    assert "boosteroid.com" in out


def test_cmd_cloud_install_bad_provider(game):
    args = game.build_parser().parse_args(["cloud", "install", "totally-unknown"])
    rc = game.cmd_cloud_install(args)
    assert rc == game.EXIT_USAGE


# --------------------------------------------------------------------------- slug / quoting
def test_slug():
    game_mod = load_bin("lindos-game")
    assert game_mod._slug("Valorant") == "valorant"
    assert game_mod._slug("Call of Duty (Warzone / Black Ops 6 / Modern Warfare III)") == \
        "call-of-duty-warzone-black-ops-6-modern-warfare-iii"


def test_desktop_quote_handles_spaces_and_parens():
    game_mod = load_bin("lindos-game")
    assert game_mod._desktop_quote("simple") == "simple"
    quoted = game_mod._desktop_quote("Call of Duty (Warzone)")
    assert quoted.startswith('"') and quoted.endswith('"')
