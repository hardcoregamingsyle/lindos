"""Tests for the ``lindos-transfer`` CLI (SPEC-WINDOWS §29.2): commands, JSON mode, exit codes,
and that nothing runs without ``--yes`` or a confirmation on a non-interactive stream."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from lindos_transfer import EXIT_ERROR, EXIT_NOTHING, EXIT_OK, EXIT_USAGE
from lindos_transfer.cli import main


def _run(argv, monkeypatch: pytest.MonkeyPatch = None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _prepared_root(tmp_path: Path, winbuild):
    ntuser = winbuild.ntuser(shell_folders={"Personal": "%USERPROFILE%\\Documents"})
    root = winbuild.root(tmp_path, ntuser=ntuser, user="alice")
    docs = root / "Users" / "alice" / "Documents"
    docs.mkdir(parents=True)
    (docs / "a.txt").write_text("hello")
    return root


def test_no_command_prints_help_and_usage_exit() -> None:
    rc, out, err = _run([])
    assert rc == EXIT_USAGE
    assert "lindos-transfer" in out


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# sources / mount
# --------------------------------------------------------------------------- #
def test_sources_json_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lindos_transfer.cli.detect_sources", lambda **kw: {"partitions": [], "bundles": []})
    rc, out, err = _run(["sources", "--json"])
    assert rc == EXIT_OK
    assert json.loads(out) == {"partitions": [], "bundles": []}


def test_mount_bitlocker_prints_guidance_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lindos_transfer.cli._device_fstype", lambda device, run=None: "BitLocker")
    rc, out, err = _run(["mount", "/dev/sda3", "--json"])
    assert rc == EXIT_ERROR
    data = json.loads(out)
    assert data["ok"] is False and "bitlocker" in data


def test_mount_success_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lindos_transfer.cli._device_fstype", lambda device, run=None: "ntfs")
    monkeypatch.setattr("lindos_transfer.cli.mount_device", lambda device, **kw: {
        "device": device, "mountpoint": "/media/x", "driver": "ntfs-3g", "read_only": True,
        "already_mounted": False, "windows": True, "hibernated": False, "hibernation": "clean",
        "notes": [], "command": []})
    rc, out, err = _run(["mount", "/dev/sda3", "--json"])
    assert rc == EXIT_OK
    data = json.loads(out)
    assert data["ok"] is True and data["mountpoint"] == "/media/x"


# --------------------------------------------------------------------------- #
# users / plan / apps / report against a synthetic partition
# --------------------------------------------------------------------------- #
def test_users_lists_the_found_profile(tmp_path: Path, winbuild) -> None:
    root = _prepared_root(tmp_path, winbuild)
    rc, out, err = _run(["users", "--from", str(root), "--json"])
    assert rc == EXIT_OK
    users = json.loads(out)
    assert users[0]["name"] == "alice"


def test_plan_json_and_save_to_file(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    out_file = tmp_path / "plan.json"
    rc, out, err = _run(["plan", "--from", str(root), "--only", "documents", "--dest", str(home),
                        "--json", "-o", str(out_file)])
    assert rc == EXIT_OK
    plan = json.loads(out)
    assert plan["items"][0]["category"] == "documents"
    assert out_file.is_file()
    saved = json.loads(out_file.read_text())
    assert saved["id"] == plan["id"]


def test_plan_text_summary(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    rc, out, err = _run(["plan", "--from", str(root), "--only", "documents", "--dest", str(home)])
    assert rc == EXIT_OK
    assert "Plan" in out and "Documents" in out


def test_apps_command_lists_installed_programs(tmp_path: Path, winbuild) -> None:
    root = winbuild.root(tmp_path, user="alice")
    rc, out, err = _run(["apps", "--from", str(root), "--json"])
    assert rc == EXIT_OK
    apps = json.loads(out)
    assert apps and apps[0]["windows_name"] == "VLC media player"


def test_report_no_prior_transfer(tmp_path: Path, home: Path) -> None:
    rc, out, err = _run(["report", "--json"])
    assert rc == EXIT_NOTHING
    assert json.loads(out) is None


# --------------------------------------------------------------------------- #
# run: confirmation gate, --yes, --dry-run, --json-progress
# --------------------------------------------------------------------------- #
def test_run_refuses_without_yes_when_not_a_tty(tmp_path: Path, winbuild, home: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    root = _prepared_root(tmp_path, winbuild)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out, err = _run(["run", "--from", str(root), "--only", "documents", "--dest", str(home)])
    assert rc == EXIT_USAGE
    assert not (home / "Documents" / "a.txt").exists()


def test_run_with_yes_copies_files(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    rc, out, err = _run(["run", "--from", str(root), "--only", "documents", "--dest", str(home), "--yes"])
    assert rc == EXIT_OK
    assert (home / "Documents" / "a.txt").read_text() == "hello"


def test_run_dry_run_does_not_ask_and_copies_nothing(tmp_path: Path, winbuild, home: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    root = _prepared_root(tmp_path, winbuild)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out, err = _run(["run", "--from", str(root), "--only", "documents", "--dest", str(home), "--dry-run"])
    assert rc == EXIT_OK
    assert not (home / "Documents" / "a.txt").exists()


def test_run_json_progress_emits_one_json_object_per_line(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    rc, out, err = _run(["run", "--from", str(root), "--only", "documents", "--dest", str(home), "--yes",
                        "--json-progress"])
    assert rc == EXIT_OK
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines
    for line in lines:
        obj = json.loads(line)
        assert "event" in obj


def test_run_nothing_selected_exit_code_4(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    rc, out, err = _run(["run", "--from", str(root), "--only", "wifi", "--exclude", "wifi",
                        "--dest", str(home), "--yes"])
    assert rc == EXIT_NOTHING


def test_run_from_a_saved_plan(tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    plan_path = tmp_path / "plan.json"
    rc, _out, _err = _run(["plan", "--from", str(root), "--only", "documents", "--dest", str(home),
                          "--json", "-o", str(plan_path)])
    assert rc == EXIT_OK
    rc2, out2, _err2 = _run(["run", "--plan", str(plan_path), "--yes"])
    assert rc2 == EXIT_OK
    assert (home / "Documents" / "a.txt").is_file()


# --------------------------------------------------------------------------- #
# run --plan: a tampered/foreign dest_home is never trusted at face value
# (sec-transfer:run-plan-dest-home-unconfined)
# --------------------------------------------------------------------------- #
def test_run_plan_refuses_a_dest_home_that_does_not_match_the_real_home(
        tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    plan_path = tmp_path / "plan.json"
    rc, _out, _err = _run(["plan", "--from", str(root), "--only", "documents", "--dest", str(home),
                          "--json", "-o", str(plan_path)])
    assert rc == EXIT_OK
    plan = json.loads(plan_path.read_text())
    evil_home = tmp_path / "not-your-home"
    plan["dest_home"] = str(evil_home)
    plan_path.write_text(json.dumps(plan))
    rc2, _out2, _err2 = _run(["run", "--plan", str(plan_path), "--yes"])
    assert rc2 == EXIT_ERROR
    assert not evil_home.exists()


def test_run_plan_with_explicit_dest_overrides_a_tampered_dest_home(
        tmp_path: Path, winbuild, home: Path) -> None:
    root = _prepared_root(tmp_path, winbuild)
    plan_path = tmp_path / "plan.json"
    rc, _out, _err = _run(["plan", "--from", str(root), "--only", "documents", "--dest", str(home),
                          "--json", "-o", str(plan_path)])
    assert rc == EXIT_OK
    plan = json.loads(plan_path.read_text())
    plan["dest_home"] = str(tmp_path / "not-your-home")
    plan_path.write_text(json.dumps(plan))
    rc2, _out2, _err2 = _run(["run", "--plan", str(plan_path), "--yes", "--dest", str(home)])
    assert rc2 == EXIT_OK
    assert (home / "Documents" / "a.txt").is_file()


# --------------------------------------------------------------------------- #
# install-apps: nothing without a selection, --yes gate, dry-run
# --------------------------------------------------------------------------- #
def _plan_with_one_app(tmp_path: Path, home: Path, *, selected: bool = True) -> Path:
    plan = {
        "schema": 1, "id": "20260926-103000-ab12", "created": "2026-09-26T10:30:00Z",
        "source": {"type": "partition", "root": str(tmp_path), "computer": "X", "windows": "Windows 11",
                  "hibernated": False, "driver": "ntfs-3g"},
        "user": "alice", "dest_home": str(home), "items": [],
        "apps": [{"windows_name": "VLC media player", "publisher": "VideoLAN", "version": "3.0.20",
                 "winget_id": "VideoLAN.VLC",
                 "actions": [{"type": "apt", "id": "vlc", "label": "VLC media player", "packages": ["vlc"]}],
                 "chosen": 0, "selected": selected}],
        "options": {"firefox_passwords": False}, "skipped": [], "warnings": [],
    }
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


def test_install_apps_nothing_selected(tmp_path: Path, home: Path) -> None:
    plan_path = _plan_with_one_app(tmp_path, home, selected=False)
    rc, out, err = _run(["install-apps", "--plan", str(plan_path)])
    assert rc == EXIT_NOTHING


def test_install_apps_refuses_without_yes_on_non_tty(tmp_path: Path, home: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan_with_one_app(tmp_path, home)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out, err = _run(["install-apps", "--plan", str(plan_path)])
    assert rc == EXIT_USAGE


def _real_plan_with_apps(tmp_path: Path, home: Path, winbuild) -> Path:
    """A plan built for real from a synthetic Windows partition (unlike ``_plan_with_one_app``,
    this one's ``source.root`` can actually be reopened -- needed by anything that reaches
    ``verify_apps_against_source``, i.e. anything past the --yes/--dry-run gate)."""
    root = winbuild.root(tmp_path, user="alice")
    rc, out, _err = _run(["plan", "--from", str(root), "--only", "apps", "--dest", str(home), "--json"])
    assert rc == EXIT_OK
    plan = json.loads(out)
    assert plan["apps"] and plan["apps"][0]["windows_name"] == "VLC media player"
    assert plan["apps"][0]["actions"] == [{"type": "apt", "id": "vlc", "label": "VLC media player",
                                           "packages": ["vlc"]}]
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    return plan_path


def test_install_apps_dry_run_json(tmp_path: Path, home: Path, winbuild) -> None:
    plan_path = _real_plan_with_apps(tmp_path, home, winbuild)
    rc, out, err = _run(["install-apps", "--plan", str(plan_path), "--dry-run", "--json"])
    assert rc == EXIT_OK
    events = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert any(e.get("event") == "item" for e in events)


def test_install_apps_with_yes_and_fake_helper(tmp_path: Path, home: Path, winbuild,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _real_plan_with_apps(tmp_path, home, winbuild)
    calls = []

    class FakeHelper:
        def install_packages(self, packages):
            calls.append(list(packages))
            import types

            return types.SimpleNamespace(ok=True, message="ok", __bool__=lambda self: True)

    monkeypatch.setattr("lindos_transfer.apps._default_helper", lambda: FakeHelper())
    rc, out, err = _run(["install-apps", "--plan", str(plan_path), "--yes"])
    assert rc == EXIT_OK
    assert calls == [["vlc"]]
    assert "OK" in out


# --------------------------------------------------------------------------- #
# install-apps: a tampered/foreign plan file must never run an unverified action
# (sec-transfer:install-apps-untrusted-plan)
# --------------------------------------------------------------------------- #
def test_install_apps_drops_a_tampered_action(tmp_path: Path, home: Path, winbuild,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _real_plan_with_apps(tmp_path, home, winbuild)
    plan = json.loads(plan_path.read_text())
    # Tamper with the chosen action after the plan was made, exactly as an edited/foreign plan
    # file would: a different package list than app-map.json actually matched for this app.
    plan["apps"][0]["actions"][0]["packages"] = ["totally-not-vlc"]
    plan_path.write_text(json.dumps(plan))
    calls = []

    class FakeHelper:
        def install_packages(self, packages):
            calls.append(list(packages))
            import types

            return types.SimpleNamespace(ok=True, message="ok", __bool__=lambda self: True)

    monkeypatch.setattr("lindos_transfer.apps._default_helper", lambda: FakeHelper())
    rc, out, err = _run(["install-apps", "--plan", str(plan_path), "--yes"])
    assert rc == EXIT_NOTHING
    assert calls == [], "a tampered action must never reach the privileged helper"


def test_install_apps_refuses_when_source_cannot_be_reopened(tmp_path: Path, home: Path, winbuild,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _real_plan_with_apps(tmp_path, home, winbuild)
    calls = []

    class FakeHelper:
        def install_packages(self, packages):
            calls.append(list(packages))
            import types

            return types.SimpleNamespace(ok=True, message="ok", __bool__=lambda self: True)

    monkeypatch.setattr("lindos_transfer.apps._default_helper", lambda: FakeHelper())
    # The USB stick/mount is gone by the time install-apps runs.
    shutil.rmtree(tmp_path / "winroot")
    rc, out, err = _run(["install-apps", "--plan", str(plan_path), "--yes"])
    assert rc == EXIT_ERROR
    assert calls == []


def test_install_apps_web_action_uses_the_real_home_not_the_plans_dest_home(
        tmp_path: Path, home: Path, winbuild, monkeypatch: pytest.MonkeyPatch) -> None:
    """dest_home from a loaded plan is never trusted for where a web-app shortcut is written."""
    monkeypatch.setattr("lindos_transfer.apps.load_app_map", lambda path=None: {
        "schema": 1, "apps": [{"id": "vlc-web", "match": ["VLC media player"],
                               "actions": [{"type": "web", "id": "vlc-site", "label": "VLC website",
                                          "url": "https://www.videolan.org/vlc/"}]}]})
    root = winbuild.root(tmp_path, user="alice")
    rc, out, _err = _run(["plan", "--from", str(root), "--only", "apps", "--dest", str(home), "--json"])
    assert rc == EXIT_OK
    plan = json.loads(out)
    assert plan["apps"][0]["actions"][0]["type"] == "web"
    evil_home = tmp_path / "not-your-home"
    plan["dest_home"] = str(evil_home)   # a tampered/foreign dest_home
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    rc2, out2, err2 = _run(["install-apps", "--plan", str(plan_path), "--yes"])
    assert rc2 == EXIT_OK
    assert not evil_home.exists()
    shortcuts = list((home / ".local" / "share" / "applications").glob("lindos-webapp-*.desktop"))
    assert shortcuts, "the shortcut must be written under the real home, not the plan's dest_home"


# --------------------------------------------------------------------------- #
# make-usb-kit
# --------------------------------------------------------------------------- #
def test_make_usb_kit_handles_a_missing_kit_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lindos_transfer.cli.share_dir", lambda: tmp_path / "no-such-share")
    rc, out, err = _run(["make-usb-kit", str(tmp_path / "usb"), "--json"])
    assert rc == EXIT_ERROR
    data = json.loads(out)
    assert data["ok"] is False


def test_make_usb_kit_copies_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    share = tmp_path / "share" / "windows"
    share.mkdir(parents=True)
    (share / "LindosTransfer.ps1").write_text("# ps1")
    (share / "LindosTransfer.cmd").write_text("@echo off")
    monkeypatch.setattr("lindos_transfer.cli.share_dir", lambda: tmp_path / "share")
    dest = tmp_path / "usb"
    rc, out, err = _run(["make-usb-kit", str(dest), "--json"])
    assert rc == EXIT_OK
    data = json.loads(out)
    assert set(data["files"]) == {"LindosTransfer.ps1", "LindosTransfer.cmd"}
    assert (dest / "LindosTransfer.ps1").is_file()
