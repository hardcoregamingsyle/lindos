"""launchers.json ↔ install-gaming.sh ↔ lindos-game ↔ lindos-core helper whitelist consistency."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import APPS, LIBEXEC, SHARE, load_bin, parse_desktop, read_json  # noqa: E402

CATALOGUE = SHARE / "gaming" / "launchers.json"
SCRIPT = LIBEXEC / "install-gaming.sh"

# SPEC §10: lindos-game install <steam|lutris|heroic|prism|sober|vinegar|mcpelauncher|bottles|all>
SPEC_IDS = ["steam", "lutris", "heroic", "prism", "sober", "vinegar", "mcpelauncher", "bottles"]
INSTALL_KINDS = {"apt", "deb", "flatpak"}
ID_RE = re.compile(r"^[a-z0-9-]+$")
FLATPAK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _case_labels(text: str) -> list[str]:
    """Item labels of the `case "${item}" in … esac` block inside dispatch_item()."""
    m = re.search(r"dispatch_item\(\)\s*\{(.*?)\n\}", text, re.S)
    assert m, "dispatch_item() not found in install-gaming.sh"
    body = m.group(1)
    labels: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        lm = re.match(r"^([a-z0-9-]+)\)\s", s)
        if lm:
            labels.append(lm.group(1))
    return labels


def _bash_array(text: str, name: str) -> list[str]:
    m = re.search(rf"^{re.escape(name)}=\(([^)]*)\)", text, re.M)
    assert m, f"{name}=(…) not found in install-gaming.sh"
    return m.group(1).split()


# --------------------------------------------------------------------------- catalogue
def test_catalogue_is_valid_json_lf():
    raw = CATALOGUE.read_bytes()
    assert b"\r\n" not in raw, "LF line endings only"
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data, dict) and isinstance(data["launchers"], list)
    assert data.get("schema") == 1
    assert re.fullmatch(r"\d{4}-\d{2}", data["updated"])


def test_catalogue_ids_match_spec_exactly(launchers_catalogue):
    ids = [item["id"] for item in launchers_catalogue["launchers"]]
    assert ids == SPEC_IDS, f"launcher ids/order must be exactly SPEC §10: {ids}"
    assert len(set(ids)) == len(ids), "duplicate launcher ids"


@pytest.mark.parametrize("field", ["id", "name", "description", "install", "check", "desktop", "icon"])
def test_every_launcher_has_required_field(launchers_catalogue, field):
    for item in launchers_catalogue["launchers"]:
        assert field in item and item[field] not in ("", None), f"{item.get('id')}: missing {field}"


def test_launcher_field_shapes(launchers_catalogue):
    for item in launchers_catalogue["launchers"]:
        lid = item["id"]
        assert ID_RE.match(lid), lid
        assert item["install"] in INSTALL_KINDS, f"{lid}: install kind {item['install']!r}"
        check = item["check"]
        assert set(check) == {"dpkg", "flatpak", "which"}, f"{lid}: check keys {set(check)}"
        for key in ("dpkg", "flatpak", "which"):
            assert isinstance(check[key], list), f"{lid}: check.{key} must be a list"
        assert check["dpkg"] or check["flatpak"] or check["which"], f"{lid}: nothing to check"
        for fid in check["flatpak"]:
            assert FLATPAK_ID_RE.match(fid) and fid.count(".") >= 2, f"{lid}: bad flatpak id {fid!r}"
        assert item["desktop"].endswith(".desktop"), f"{lid}: desktop id must end with .desktop"
        assert isinstance(item.get("run"), list) and item["run"], f"{lid}: run command missing"
        assert item.get("homepage", "").startswith("https://"), f"{lid}: homepage must be https"
        # install kind must be backed by a matching source
        src = item.get("sources", {})
        if item["install"] == "apt":
            assert src.get("apt"), f"{lid}: install=apt needs sources.apt"
        elif item["install"] == "flatpak":
            assert src.get("flatpak") in check["flatpak"], f"{lid}: sources.flatpak must be checked"
        elif item["install"] == "deb":
            assert src.get("github"), f"{lid}: install=deb needs sources.github"


def test_catalogue_honesty_notes(launchers_catalogue):
    by_id = {item["id"]: item for item in launchers_catalogue["launchers"]}
    sober = json.dumps(by_id["sober"]).lower()
    assert "sober" in sober and ("windows client" in sober or "windows player" in sober)
    assert "org.vinegarhq.Sober" in by_id["sober"]["check"]["flatpak"]
    heroic = json.dumps(by_id["heroic"]).lower()
    assert "fortnite" in heroic and "not" in heroic
    prism = json.dumps(by_id["prism"]).lower()
    assert "java" in prism and "org.prismlauncher.PrismLauncher" in by_id["prism"]["check"]["flatpak"]
    mcpe = json.dumps(by_id["mcpelauncher"]).lower()
    assert "unofficial" in mcpe and "io.mrarm.mcpelauncher" in by_id["mcpelauncher"]["check"]["flatpak"]
    steam = json.dumps(by_id["steam"]).lower()
    assert "anti-cheat" in steam or "anticheat" in steam
    assert by_id["steam"]["sources"]["apt_repo"] == \
        "deb [arch=amd64,i386 signed-by=/usr/share/keyrings/steam.gpg] https://repo.steampowered.com/steam/ stable steam"


def test_lindos_desktop_ids_exist_in_package(launchers_catalogue):
    """Launchers pointing at a lindos-*.desktop must ship that file (with matching Exec/icon)."""
    for item in launchers_catalogue["launchers"]:
        did = item["desktop"]
        if not did.startswith("lindos-"):
            continue
        path = APPS / did
        assert path.is_file(), f"{item['id']}: {did} not shipped by lindos-gaming"
        entry = parse_desktop(path)["Desktop Entry"]
        assert entry["Icon"] == item["icon"], f"{did}: Icon {entry['Icon']} != catalogue icon {item['icon']}"
        assert entry.get("X-Lindos-Launcher") == item["id"], f"{did}: X-Lindos-Launcher must be {item['id']}"


# --------------------------------------------------------------------------- install script
def test_install_script_case_labels_match_catalogue(launchers_catalogue, install_script_text):
    labels = _case_labels(install_script_text)
    ids = [item["id"] for item in launchers_catalogue["launchers"]]
    assert set(labels) == set(ids), f"case labels {labels} != catalogue ids {ids}"
    assert labels == ids, "keep the case block in catalogue order for readability"
    assert _bash_array(install_script_text, "ALL_ITEMS") == ids
    apt_items = _bash_array(install_script_text, "APT_ITEMS")
    assert set(apt_items) <= set(ids)
    # apt/deb launchers are the ones the chroot hook (--from-chroot) may install; flatpak-only
    # ones are deferred to first boot.  Anything else in APT_ITEMS must have an apt fallback source.
    by_id = {item["id"]: item for item in launchers_catalogue["launchers"]}
    expected_apt = [lid for lid, item in by_id.items() if item["install"] in ("apt", "deb")]
    assert set(expected_apt) <= set(apt_items), f"APT_ITEMS {apt_items} misses apt/deb launchers {expected_apt}"
    for extra in set(apt_items) - set(expected_apt):
        assert by_id[extra].get("sources", {}).get("apt"), f"{extra} is in APT_ITEMS but has no sources.apt fallback"
    for lid, item in by_id.items():
        if item["install"] == "flatpak" and not item.get("sources", {}).get("apt"):
            assert lid not in apt_items, f"{lid} is flatpak-only and must not be in APT_ITEMS"


def test_install_script_flatpak_ids_match_catalogue(launchers_catalogue, install_script_text):
    for item in launchers_catalogue["launchers"]:
        for fid in item["check"]["flatpak"]:
            if item["install"] == "flatpak" or item["id"] in ("lutris", "heroic"):
                assert fid in install_script_text, f"{item['id']}: {fid} not referenced by install-gaming.sh"


def test_install_script_contract(install_script_text):
    t = install_script_text
    assert t.startswith("#!/bin/bash\n")
    assert "set -Eeuo pipefail" in t
    assert re.search(r"^log\(\)", t, re.M) and re.search(r"^die\(\)", t, re.M)
    assert "sudo " not in re.sub(r"#.*", "", t), "no sudo inside scripts (SPEC §12)"
    assert "--from-chroot" in t and "--dry-run" in t
    # Valve repository wiring (SPEC §8 / assignment)
    assert "https://repo.steampowered.com/steam/archive/stable/steam.gpg" in t
    assert "/usr/share/keyrings/steam.gpg" in t
    assert "https://repo.steampowered.com/steam/ stable steam" in t
    assert "steam-launcher" in t and "steam-installer" in t
    assert "dpkg --add-architecture i386" in t
    for lib in ("libgl1-mesa-dri:i386", "libgl1:i386", "mesa-vulkan-drivers:i386"):
        assert lib in t, lib
    assert "flatpak remote-add --if-not-exists" in t and "https://dl.flathub.org/repo/flathub.flatpakrepo" in t
    assert "Heroic-Games-Launcher/HeroicGamesLauncher" in t
    assert re.search(r'^HEROIC_VERSION="\$\{HEROIC_VERSION:-\d+\.\d+\.\d+\}"', t, re.M), "HEROIC_VERSION must be pinned"
    assert "HEROIC_SHA256" in t
    # offline → exit 3 with a message
    assert re.search(r'die "offline[^"]*" 3', t), "offline must exit 3 with a message"


def test_install_script_uses_lf_and_is_executable_looking():
    raw = SCRIPT.read_bytes()
    assert b"\r" not in raw, "LF line endings only"


# --------------------------------------------------------------------------- lindos-game CLI
def test_lindos_game_expand_items(launchers_catalogue):
    game = load_bin("lindos-game")
    launchers = launchers_catalogue["launchers"]
    assert game.expand_items(["all"], launchers) == SPEC_IDS
    assert game.expand_items(["steam", "steam", "sober"], launchers) == ["steam", "sober"]
    assert game.expand_items(["Prism"], launchers) == ["prism"]
    with pytest.raises(ValueError):
        game.expand_items(["fortnite"], launchers)


def test_lindos_game_catalogue_path_resolves_to_package():
    game = load_bin("lindos-game")
    assert Path(game.catalogue_path()).resolve() == CATALOGUE.resolve()
    assert Path(game.installer_path()).resolve() == SCRIPT.resolve()


def test_lindos_game_status_rows_shape(launchers_catalogue, monkeypatch):
    game = load_bin("lindos-game")
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: pkg == "lutris")
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"org.vinegarhq.Sober"})
    monkeypatch.setattr(game.shutil, "which", lambda b: "/usr/bin/heroic" if b == "heroic" else None)
    rows = game.status_rows(launchers_catalogue["launchers"])
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == set(SPEC_IDS)
    assert by_id["lutris"]["installed"] and by_id["lutris"]["via"] == "apt:lutris"
    assert by_id["sober"]["installed"] and by_id["sober"]["via"] == "flatpak:org.vinegarhq.Sober"
    assert by_id["heroic"]["installed"] and by_id["heroic"]["via"] == "path:/usr/bin/heroic"
    assert not by_id["steam"]["installed"] and by_id["steam"]["via"] is None
    for r in rows:
        assert set(r) >= {"id", "name", "installed", "via", "install", "desktop", "icon", "description"}


def test_lindos_game_status_json_cli(capsys, monkeypatch):
    game = load_bin("lindos-game")
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: False)
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game.shutil, "which", lambda b: None)
    rc = game.main(["status", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in out["launchers"]] == SPEC_IDS
    assert all(r["installed"] is False for r in out["launchers"])


def test_lindos_game_install_dry_run(capsys, monkeypatch):
    game = load_bin("lindos-game")
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: pkg == "lutris")
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game.shutil, "which", lambda b: None)
    rc = game.main(["install", "lutris", "sober", "--dry-run", "--json"])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["action"] == "install-gaming"
    assert plan["payload"] == {"items": ["sober"]}
    assert plan["already_installed"] == ["lutris"]


def test_lindos_game_install_uses_helper_payload(monkeypatch):
    """Non-root path must call lindos.helper.run_privileged('install-gaming', {'items': [...]})."""
    game = load_bin("lindos-game")
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: False)
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game.shutil, "which", lambda b: None)
    monkeypatch.setattr(game, "is_root", lambda: False)
    calls: list = []

    class Res:
        ok, out, err, code = True, "steam: done\n", "", 0

    def fake_run_privileged(action, payload, log=None):
        calls.append((action, payload))
        return Res()

    import types
    fake_helper = types.ModuleType("lindos.helper")
    fake_helper.run_privileged = fake_run_privileged  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("lindos")
    fake_pkg.helper = fake_helper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lindos", fake_pkg)
    monkeypatch.setitem(sys.modules, "lindos.helper", fake_helper)
    rc = game.main(["install", "steam", "prism"])
    assert rc == 0
    assert calls == [("install-gaming", {"items": ["steam", "prism"]})]


def test_lindos_game_launch_command_resolution(launchers_catalogue, monkeypatch):
    game = load_bin("lindos-game")
    by_id = {item["id"]: item for item in launchers_catalogue["launchers"]}
    # flatpak wins
    monkeypatch.setattr(game, "flatpak_apps", lambda: {"org.prismlauncher.PrismLauncher"})
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: False)
    monkeypatch.setattr(game.shutil, "which", lambda b: None)
    assert game.launch_command(by_id["prism"]) == ["flatpak", "run", "org.prismlauncher.PrismLauncher"]
    # binary on PATH
    monkeypatch.setattr(game, "flatpak_apps", lambda: set())
    monkeypatch.setattr(game.shutil, "which", lambda b: "/usr/bin/lutris" if b == "lutris" else None)
    assert game.launch_command(by_id["lutris"]) == ["lutris"]
    # dpkg-installed prism (PPA build) must NOT resolve to gtk-launch lindos-minecraft.desktop (recursion)
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: pkg == "prismlauncher")
    monkeypatch.setattr(game.shutil, "which", lambda b: "/usr/bin/gtk-launch" if b == "gtk-launch" else None)
    cmd = game.launch_command(by_id["prism"])
    assert cmd and cmd[0] != "gtk-launch", cmd
    # not installed at all
    monkeypatch.setattr(game, "dpkg_installed", lambda pkg: False)
    monkeypatch.setattr(game.shutil, "which", lambda b: None)
    assert game.launch_command(by_id["sober"]) is None


def test_helper_whitelist_agrees_with_catalogue(launchers_catalogue):
    """lindos-core's helper must accept every catalogue id (+ 'all') for action install-gaming."""
    try:
        from lindos import helper  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - lindos-core absent in this checkout
        pytest.skip("lindos-core not importable")
    ids = {item["id"] for item in launchers_catalogue["launchers"]} | {"all"}
    assert ids <= set(helper.GAMING_ITEMS), f"helper whitelist missing {ids - set(helper.GAMING_ITEMS)}"
    payload = helper.validate_payload("install-gaming", {"items": sorted(ids - {"all"})})
    assert set(payload["items"]) == ids - {"all"}
