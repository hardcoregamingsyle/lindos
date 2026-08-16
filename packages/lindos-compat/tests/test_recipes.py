"""Tests for the shipped recipes (/usr/share/lindos/recipes/*.json) and recipe application."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lindos_compat import recipes

REQUIRED_IDS = {
    "photoshop-cc-2021", "photoshop-cs6", "illustrator-cc-2021", "premiere", "office-2016", "office-365",
    "notepad-plus-plus", "7zip", "winrar", "paint-net", "foobar2000", "autocad", "epic-games-launcher",
    "roblox-player", "riot-client",
}
# SPEC §9 statuses that must not drift (honesty rules §0.1)
EXPECTED_STATUS = {
    "photoshop-cs6": "works", "office-2016": "works", "notepad-plus-plus": "works", "7zip": "works",
    "winrar": "works", "foobar2000": "works",
    "photoshop-cc-2021": "partial", "illustrator-cc-2021": "partial", "paint-net": "partial",
    "premiere": "broken", "office-365": "broken", "autocad": "broken", "roblox-player": "broken",
    "riot-client": "broken", "epic-games-launcher": "broken",
}
# real winetricks verbs we allow ourselves to reference (keeps typos out of the recipes)
KNOWN_VERBS = {
    "vcrun2005", "vcrun2008", "vcrun2010", "vcrun2012", "vcrun2013", "vcrun2015", "vcrun2017", "vcrun2019",
    "vcrun2022", "dotnet40", "dotnet45", "dotnet46", "dotnet462", "dotnet472", "dotnet48", "gdiplus", "atmlib",
    "msxml3", "msxml4", "msxml6", "riched20", "riched30", "corefonts", "tahoma", "arial", "fontsmooth=rgb",
    "fontsmooth=gray", "win7", "win8", "win81", "win10", "win11", "winxp", "dxvk", "vkd3d", "d3dcompiler_47",
    "d3dx9", "d3dx11_43", "xact", "xinput", "mfc42", "mfc140", "ie8", "wmp11", "quartz", "devenum",
    "physx", "faudio", "allfonts", "cjkfonts", "liberation",
}


def _all_recipe_files(repo_recipes_dir: Path):
    files = sorted(repo_recipes_dir.glob("*.json"))
    assert files, f"no recipes in {repo_recipes_dir}"
    return files


def test_every_recipe_is_valid_json_with_required_keys(repo_recipes_dir: Path):
    for f in _all_recipe_files(repo_recipes_dir):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        problems = recipes.validate_recipe(data, filename=str(f))
        assert not problems, f"{f.name}: {problems}"
        for key in recipes.REQUIRED_KEYS:
            assert key in data, f"{f.name} misses {key}"
        assert data["id"] == f.stem
        assert data["status"] in recipes.STATUSES
        assert data["runner"] in recipes.RUNNERS
        assert data["arch"] in recipes.ARCHES
        assert isinstance(data["notes"], str) and len(data["notes"]) > 40
        assert isinstance(data.get("summary", ""), str) and data.get("summary")
        for verb in data["winetricks"]:
            assert verb in KNOWN_VERBS, f"{f.name}: unknown winetricks verb {verb!r}"
        for k, v in data["dll_overrides"].items():
            assert isinstance(k, str) and isinstance(v, str) and v
        for entry in data["registry"]:
            assert entry["type"] in recipes.REG_TYPES
            assert entry["key"].startswith(("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE", "HKCU", "HKLM"))


def test_required_ids_and_statuses(repo_recipes_dir: Path):
    loaded = recipes.load_recipes(repo_recipes_dir, strict=True)
    assert set(loaded) >= REQUIRED_IDS
    for rid, status in EXPECTED_STATUS.items():
        assert loaded[rid].status == status, f"{rid}: {loaded[rid].status} != {status}"


def test_broken_recipes_have_alternatives_and_honest_notes(repo_recipes_dir: Path):
    loaded = recipes.load_recipes(repo_recipes_dir, strict=True)
    for r in loaded.values():
        if r.status == "broken":
            assert r.alternatives, f"{r.id} is broken but lists no alternatives"
            for alt in r.alternatives:
                assert alt.get("name") and alt.get("how")
            assert not r.winetricks and not r.post_cmds, f"{r.id}: broken recipes must not pretend to set things up"
    alts = lambda rid: " ".join(f"{a['name']} {a['how']}" for a in loaded[rid].alternatives)  # noqa: E731
    assert "Kdenlive" in alts("premiere") and "DaVinci" in alts("premiere")
    assert "LibreOffice" in alts("office-365") and "OnlyOffice" in alts("office-365") and "office.com" in alts("office-365")
    assert "Sober" in alts("roblox-player") and "lindos-game install sober" in alts("roblox-player")
    assert "Vanguard" in alts("riot-client") and "Not possible" in alts("riot-client")
    assert "FreeCAD" in alts("autocad") and "web" in alts("autocad").lower()
    assert "Heroic" in alts("epic-games-launcher")
    # honesty rules (SPEC §0.1)
    assert "Vanguard" in loaded["riot-client"].notes and "Windows-only" in loaded["riot-client"].summary
    assert "Fortnite" in loaded["epic-games-launcher"].notes
    assert "Sober" in loaded["roblox-player"].notes
    for rid in ("photoshop-cc-2021", "illustrator-cc-2021"):
        assert "2021" in loaded[rid].name and "partial" == loaded[rid].status
    assert "GPU" in loaded["photoshop-cc-2021"].notes or "Graphics Processor" in loaded["photoshop-cc-2021"].notes


def test_spec_examples_of_verbs(repo_recipes_dir: Path):
    loaded = recipes.load_recipes(repo_recipes_dir, strict=True)
    ps = loaded["photoshop-cc-2021"].winetricks
    for verb in ("vcrun2019", "fontsmooth=rgb", "gdiplus", "atmlib", "msxml6", "msxml3", "corefonts"):
        assert verb in ps
    off = loaded["office-2016"].winetricks
    for verb in ("msxml6", "riched20", "corefonts"):
        assert verb in off
    assert loaded["notepad-plus-plus"].winetricks == []


def test_get_and_format(repo_recipes_dir: Path):
    r = recipes.get_recipe("7zip", repo_recipes_dir)
    assert r is not None and r.name == "7-Zip"
    text = recipes.format_recipe(r)
    assert "7-Zip" in text and "Status:" in text and "Alternatives:" in text
    assert recipes.get_recipe("does-not-exist", repo_recipes_dir) is None
    d = r.as_dict()
    assert "path" not in d and d["id"] == "7zip"


def test_validate_rejects_bad_recipes(tmp_path: Path):
    bad = {"id": "Bad Id", "name": "x"}
    problems = recipes.validate_recipe(bad, filename=str(tmp_path / "bad-id.json"))
    assert any("missing key" in p for p in problems)
    assert any("'id'" in p for p in problems)
    broken_no_alt = {k: ([] if k in ("winetricks", "post_cmds", "registry") else ({} if k in ("dll_overrides", "env") else "x"))
                     for k in recipes.REQUIRED_KEYS}
    broken_no_alt.update({"id": "b", "status": "broken", "runner": "wine", "arch": "win64", "notes": "does not work"})
    problems = recipes.validate_recipe(broken_no_alt, filename=str(tmp_path / "b.json"))
    assert problems == ["broken recipes must list 'alternatives'"]
    (tmp_path / "x.json").write_text('{"id": "y"}', encoding="utf-8")
    with pytest.raises(ValueError):
        recipes.load_recipe_file(tmp_path / "x.json")
    assert recipes.load_recipes(tmp_path) == {}
    with pytest.raises(ValueError):
        recipes.load_recipes(tmp_path, strict=True)


def test_recipes_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LINDOS_RECIPES_DIR", str(tmp_path))
    assert recipes.recipes_dir() == tmp_path


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def test_apply_refuses_broken(repo_recipes_dir: Path, fake_core, home: Path, fake_which, fake_run):
    r = recipes.get_recipe("riot-client", repo_recipes_dir)
    res = recipes.apply_recipe(r, which=fake_which("wine", "wineboot", "winetricks"), run=fake_run)
    assert not res.ok
    assert "Vanguard" in res.message and "does NOT work" in res.message
    assert fake_run.calls == []
    assert not (home / ".local/share/lindos/prefixes/riot-client").exists()


def test_apply_dry_run(repo_recipes_dir: Path, fake_core, home: Path, fake_which, fake_run):
    r = recipes.get_recipe("office-2016", repo_recipes_dir)
    res = recipes.apply_recipe(r, dry_run=True, which=fake_which(), run=fake_run)
    assert res.ok and res.message == "dry run" and res.prefix_slug == "office-2016"
    assert fake_run.calls == []


def test_apply_runs_winetricks_overrides_registry_and_records_db(repo_recipes_dir: Path, fake_core, home: Path,
                                                                 fake_which, fake_run, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    r = recipes.get_recipe("photoshop-cc-2021", repo_recipes_dir)
    w = fake_which("wine", "wineboot", "wineserver", "winetricks")
    progress = []
    res = recipes.apply_recipe(r, which=w, run=fake_run, on_progress=progress.append,
                               log_file=home / "recipe.log")
    assert res.ok, res.steps
    pfx = home / ".local/share/lindos/prefixes/photoshop-cc-2021"
    assert res.prefix_path == str(pfx) and (pfx / "system.reg").exists()
    # winetricks -q <verbs> with WINEPREFIX set
    wt_calls = [c for c in fake_run.calls if c[0].endswith("winetricks")]
    assert len(wt_calls) == 1
    assert wt_calls[0][1] == "-q" and wt_calls[0][2:] == r.winetricks
    env = fake_run.envs[fake_run.calls.index(wt_calls[0])]
    assert env["WINEPREFIX"] == str(pfx) and env["WINEARCH"] == "win64" and env["WINEESYNC"] == "1"
    # dll overrides + registry through `wine reg add`
    reg_calls = [c for c in fake_run.calls if c[1:3] == ["reg", "add"]]
    assert any("HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides" in c and "gdiplus" in c and "native,builtin" in c
               for c in reg_calls)
    assert any("HKEY_CURRENT_USER\\Software\\Wine\\Direct3D" in c and "renderer" in c and "gl" in c for c in reg_calls)
    for c in reg_calls:
        assert c[-1] == "/f"
    # apps DB entry with env + overrides so lindos-run --prefix picks them up
    db = fake_core.apps_db_load()
    entry = db["recipe-photoshop-cc-2021"]
    assert entry["prefix"] == "photoshop-cc-2021" and entry["kind"] == "recipe" and entry["status"] == "partial"
    assert entry["env"] == {"WINEESYNC": "1"} and entry["dll_overrides"]["gdiplus"] == "native,builtin"
    assert progress and any("winetricks" in p for p in progress)
    marker = json.loads((pfx / ".lindos.json").read_text(encoding="utf-8"))
    assert marker["recipe"] == "photoshop-cc-2021"
    from lindos_compat.runner import load_prefix_settings

    env2, overrides2 = load_prefix_settings("photoshop-cc-2021")
    assert env2 == {"WINEESYNC": "1"} and overrides2["atmlib"] == "native,builtin"


def test_apply_reports_failures(repo_recipes_dir: Path, fake_core, home: Path, fake_which, fake_run):
    r = recipes.get_recipe("office-2016", repo_recipes_dir)
    fake_run.rc_for = lambda argv: 1 if any(a.endswith("winetricks") for a in argv[:1]) else 0
    res = recipes.apply_recipe(r, which=fake_which("wine", "wineboot", "winetricks"), run=fake_run,
                               log_file=home / "recipe.log")
    assert not res.ok
    failed = [s for s in res.steps if not s[1]]
    assert failed and failed[0][0].startswith("winetricks")
    # missing wine entirely
    fake_run2 = type(fake_run)()
    res = recipes.apply_recipe(r, which=fake_which(), run=fake_run2, log_file=home / "recipe.log", prefix_slug="office-alt")
    assert not res.ok and "prefix creation failed" in res.message
