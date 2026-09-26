"""``lindos-compat formats|binfmt|winget`` (SPEC-WINDOWS §28.10, plus 'formats'/'binfmt'). These
subcommands are thin wiring over ``formats.py``/``binfmt.py`` (owned by W-A1, written concurrently)
and ``winget.py``/``lindos.helper`` (this owner's / W-G1's files), so every test monkeypatches the
sibling module's public API rather than depending on its exact behaviour."""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pytest

from lindos_compat import cli_compat, winget
from lindos_compat import formats as formats_mod
from lindos_compat import binfmt as binfmt_mod


def _run(monkeypatch: pytest.MonkeyPatch, argv: List[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["lindos-compat"] + argv)
    return cli_compat.main(argv)


# --------------------------------------------------------------------------- #
# formats
# --------------------------------------------------------------------------- #
_FAKE_ROWS = [
    {"id": "exe", "label": "Windows program (.exe)", "suffixes": [".exe"], "status": "works",
     "note": "runs normally", "handler": "run", "mime": "application/x-msdownload"},
    {"id": "msu", "label": "Windows Update package", "suffixes": [".msu"], "status": "unsupported",
     "note": "not applicable", "handler": "explain", "mime": "application/x-ms-update"},
]


def test_cmd_formats_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(formats_mod, "formats_table", lambda: list(_FAKE_ROWS))
    rc = _run(monkeypatch, ["formats", "--json"])
    assert rc == cli_compat.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out == _FAKE_ROWS


def test_cmd_formats_text_orders_works_before_unsupported(monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(formats_mod, "formats_table", lambda: list(_FAKE_ROWS))
    rc = _run(monkeypatch, ["formats"])
    assert rc == cli_compat.EXIT_OK
    text = capsys.readouterr().out
    assert text.index(".exe") < text.index(".msu")


def test_cmd_formats_missing_module_is_a_clean_error(monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture) -> None:
    def boom(_name: str) -> Any:
        raise ImportError("no formats module")

    monkeypatch.setattr(cli_compat, "_sibling", boom)
    rc = _run(monkeypatch, ["formats", "--json"])
    assert rc == cli_compat.EXIT_ERROR


# --------------------------------------------------------------------------- #
# binfmt
# --------------------------------------------------------------------------- #
_FAKE_STATUS_ON = {"registered": True, "enabled": True, "masked": False, "conflicts": [],
                   "interpreter": "/usr/libexec/lindos/lindos-binfmt", "note": "on"}
_FAKE_STATUS_OFF = {"registered": False, "enabled": False, "masked": True, "conflicts": [],
                    "interpreter": "", "note": "off"}


def test_cmd_binfmt_status_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_ON))
    rc = _run(monkeypatch, ["binfmt", "status", "--json"])
    assert rc == cli_compat.EXIT_OK
    assert json.loads(capsys.readouterr().out) == _FAKE_STATUS_ON


def test_cmd_binfmt_status_text_mentions_conflicts(monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture) -> None:
    st = dict(_FAKE_STATUS_ON)
    st["conflicts"] = [{"name": "qemu-x86_64", "interpreter": "/usr/bin/qemu-x86_64"}]
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: st)
    rc = _run(monkeypatch, ["binfmt", "status"])
    assert rc == cli_compat.EXIT_OK
    text = capsys.readouterr().out
    assert "qemu-x86_64" in text


def test_cmd_binfmt_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_name: str) -> Any:
        raise ImportError("no binfmt module")

    monkeypatch.setattr(cli_compat, "_sibling", boom)
    rc = _run(monkeypatch, ["binfmt", "status", "--json"])
    assert rc == cli_compat.EXIT_ERROR


def _install_fake_helper(monkeypatch: pytest.MonkeyPatch, *, actions: List[str],
                         run_privileged: Callable[[str, Dict[str, Any]], Any]) -> None:
    mod = types.ModuleType("lindos.helper")
    mod.ACTIONS = actions  # type: ignore[attr-defined]
    mod.run_privileged = run_privileged  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lindos.helper", mod)
    if "lindos" in sys.modules:
        monkeypatch.setattr(sys.modules["lindos"], "helper", mod, raising=False)


@dataclass
class _FakeHelperResult:
    ok: bool
    code: int = 0
    message: str = ""


def test_cmd_binfmt_enable_when_helper_action_missing(monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture) -> None:
    _install_fake_helper(monkeypatch, actions=[], run_privileged=lambda a, p: _FakeHelperResult(False))
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_OFF))
    rc = _run(monkeypatch, ["binfmt", "enable", "--json"])
    assert rc == cli_compat.EXIT_UNSUPPORTED
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["code"] == 127
    assert "pkexec" in out["command"] and "set-binfmt" in out["command"]
    payload_text = out["command"].rsplit(" ", 1)[-1].strip("'")
    assert json.loads(payload_text) == {"enabled": True}


def test_cmd_binfmt_enable_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    calls: List[Any] = []

    def run_privileged(action: str, payload: Dict[str, Any]) -> _FakeHelperResult:
        calls.append((action, payload))
        return _FakeHelperResult(True, 0)

    _install_fake_helper(monkeypatch, actions=["set-binfmt"], run_privileged=run_privileged)
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_ON))
    rc = _run(monkeypatch, ["binfmt", "enable", "--json"])
    assert rc == cli_compat.EXIT_OK
    assert calls == [("set-binfmt", {"enabled": True})]
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["action"] == "enable"


def test_cmd_binfmt_disable_success_sends_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    _install_fake_helper(monkeypatch, actions=["set-binfmt"],
                         run_privileged=lambda a, p: calls.append((a, p)) or _FakeHelperResult(True, 0))
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_OFF))
    rc = _run(monkeypatch, ["binfmt", "disable", "--json"])
    assert rc == cli_compat.EXIT_OK
    assert calls == [("set-binfmt", {"enabled": False})]


def test_cmd_binfmt_enable_cancelled_password(monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture) -> None:
    _install_fake_helper(monkeypatch, actions=["set-binfmt"],
                         run_privileged=lambda a, p: _FakeHelperResult(False, 126, "cancelled"))
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_OFF))
    rc = _run(monkeypatch, ["binfmt", "enable", "--json"])
    assert rc == cli_compat.EXIT_ERROR
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == 126


def test_cmd_binfmt_enable_helper_raises(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def boom(a: str, p: Dict[str, Any]) -> Any:
        raise RuntimeError("helper crashed")

    _install_fake_helper(monkeypatch, actions=["set-binfmt"], run_privileged=boom)
    monkeypatch.setattr(binfmt_mod, "status", lambda **kw: dict(_FAKE_STATUS_OFF))
    rc = _run(monkeypatch, ["binfmt", "enable", "--json"])
    # a crashed helper falls back to the same honest "run it yourself" offer as an unavailable
    # action (code 127 -> EXIT_UNSUPPORTED), with the crash folded into the message.
    assert rc == cli_compat.EXIT_UNSUPPORTED
    out = json.loads(capsys.readouterr().out)
    assert "helper crashed" in out["message"]


def test_helper_command_uses_pkexec_and_sorted_json() -> None:
    text = cli_compat.helper_command("set-binfmt", {"enabled": True})
    assert text == 'pkexec /usr/libexec/lindos/lindos-helper set-binfmt \'{"enabled":true}\''


def test_cmd_binfmt_bad_subcommand_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = cli_compat.build_parser()
    ns = parser.parse_args(["binfmt"])
    with pytest.raises(SystemExit):
        cli_compat.cmd_binfmt(ns, parser)


# --------------------------------------------------------------------------- #
# winget: CLI parsing
# --------------------------------------------------------------------------- #
def test_build_parser_has_winget_subcommands() -> None:
    parser = cli_compat.build_parser()
    ns = parser.parse_args(["winget", "search", "firefox", "--limit", "5", "--json"])
    assert ns.cmd == "winget" and ns.sub == "search" and ns.query == ["firefox"] and ns.limit == 5
    ns = parser.parse_args(["winget", "install", "Mozilla.Firefox", "--arch", "x64", "--dry-run"])
    assert ns.sub == "install" and ns.id == "Mozilla.Firefox" and ns.arch == "x64" and ns.dry_run
    ns = parser.parse_args(["winget", "show", "Mozilla.Firefox", "--version", "1.0"])
    assert ns.sub == "show" and ns.pkg_version == "1.0"
    ns = parser.parse_args(["winget", "list"])
    assert ns.sub == "list"
    ns = parser.parse_args(["winget", "update-index"])
    assert ns.sub == "update-index"


def test_winget_install_rejects_bad_arch() -> None:
    parser = cli_compat.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["winget", "install", "Mozilla.Firefox", "--arch", "arm64"])


# --------------------------------------------------------------------------- #
# winget: search
# --------------------------------------------------------------------------- #
def test_winget_search_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    results = [{"id": "Mozilla.Firefox", "name": "Mozilla Firefox", "version": "156.0.1", "moniker": "firefox",
               "match": ""}]
    calls: List[Any] = []
    monkeypatch.setattr(winget, "search", lambda q, **kw: calls.append((q, kw)) or results)
    rc = _run(monkeypatch, ["winget", "search", "fire", "fox", "--json"])
    assert rc == cli_compat.EXIT_OK
    assert json.loads(capsys.readouterr().out) == results
    assert calls[0][0] == "fire fox"


def test_winget_search_text_no_results(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(winget, "search", lambda q, **kw: [])
    rc = _run(monkeypatch, ["winget", "search", "nope"])
    assert rc == cli_compat.EXIT_OK
    assert "No package matches" in capsys.readouterr().out


def test_winget_search_bad_limit_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    rc = _run(monkeypatch, ["winget", "search", "x", "--limit", "0"])
    assert rc == cli_compat.EXIT_USAGE


# --------------------------------------------------------------------------- #
# winget: show (winget_show_data is exercised against the real winget module,
# since it composes effective_installers/select_installer/installer_notes)
# --------------------------------------------------------------------------- #
def _sample_manifest() -> Dict[str, Any]:
    return {
        "PackageIdentifier": "Mozilla.Firefox", "PackageVersion": "156.0.1", "PackageName": "Mozilla Firefox",
        "Publisher": "Mozilla", "License": "MPL-2.0",
        "Installers": [
            {"Architecture": "x64", "InstallerType": "exe", "Scope": "user",
             "InstallerUrl": "https://download.mozilla.test/firefox.exe", "InstallerSha256": "a" * 64,
             "InstallerSwitches": {"Silent": "-ms"}},
            {"Architecture": "x86", "InstallerType": "exe", "Scope": "user",
             "InstallerUrl": "https://download.mozilla.test/firefox32.exe", "InstallerSha256": "b" * 64,
             "InstallerSwitches": {"Silent": "-ms"}},
        ],
    }


def test_winget_show_data_picks_the_installer_lindos_would_use() -> None:
    data = cli_compat.winget_show_data(_sample_manifest())
    assert data["id"] == "Mozilla.Firefox"
    assert data["selected"] is not None
    assert data["installers"][data["selected"]]["Architecture"].lower() in ("x64", "neutral")
    assert data["selection_error"] == ""


def test_winget_show_data_reports_selection_error_when_nothing_usable() -> None:
    manifest = {"PackageIdentifier": "Some.App", "PackageVersion": "1.0",
               "Installers": [{"Architecture": "arm64", "InstallerType": "exe",
                               "InstallerUrl": "https://x.test/a.exe", "InstallerSha256": "a" * 64}]}
    data = cli_compat.winget_show_data(manifest)
    assert data["selected"] is None and data["selection_error"]


def test_cmd_winget_show_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(winget, "load_manifest", lambda pid, version=None, **kw: _sample_manifest())
    rc = _run(monkeypatch, ["winget", "show", "Mozilla.Firefox", "--json"])
    assert rc == cli_compat.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "Mozilla.Firefox" and out["selected"] is not None


def test_cmd_winget_show_text_marks_the_chosen_installer(monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(winget, "load_manifest", lambda pid, version=None, **kw: _sample_manifest())
    rc = _run(monkeypatch, ["winget", "show", "Mozilla.Firefox"])
    assert rc == cli_compat.EXIT_OK
    text = capsys.readouterr().out
    assert "->" in text and "Install it:" in text


# --------------------------------------------------------------------------- #
# winget: install
# --------------------------------------------------------------------------- #
def test_cmd_winget_install_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    result = {"ok": True, "status": "installed", "message": "Firefox 156.0.1: installed.", "apps": ["firefox"],
             "dependency_results": []}
    captured_kwargs: Dict[str, Any] = {}

    def fake_install(pid: str, **kw: Any) -> Dict[str, Any]:
        captured_kwargs.update(kw)
        return result

    monkeypatch.setattr(winget, "install", fake_install)
    rc = _run(monkeypatch, ["winget", "install", "Mozilla.Firefox", "--accept-package-agreements", "--json"])
    assert rc == cli_compat.EXIT_OK
    assert json.loads(capsys.readouterr().out) == result
    assert captured_kwargs["accept_package_agreements"] is True


def test_cmd_winget_install_unsupported_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winget, "install", lambda pid, **kw: {"ok": False, "status": "unsupported",
                                                               "message": "not possible"})
    rc = _run(monkeypatch, ["winget", "install", "Some.App", "--json"])
    assert rc == cli_compat.EXIT_UNSUPPORTED


def test_cmd_winget_install_failed_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winget, "install", lambda pid, **kw: {"ok": False, "status": "failed", "message": "boom"})
    rc = _run(monkeypatch, ["winget", "install", "Some.App", "--json"])
    assert rc == cli_compat.EXIT_ERROR


def test_cmd_winget_install_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    plan = {"status": "dry-run", "ok": True, "message": "Nothing was downloaded or run (--dry-run).",
           "name": "Firefox", "version": "156.0.1", "id": "Mozilla.Firefox", "publisher": "Mozilla",
           "installer": {"effective_type": "exe", "type": "exe", "architecture": "x64", "scope": "user",
                        "host": "download.mozilla.test"},
           "prefix": "firefox", "license": "MPL-2.0", "license_url": "", "agreements": [], "notes": [],
           "dependencies": {"packages": []}, "argv": ["lindos-run", "--prefix", "firefox"]}
    monkeypatch.setattr(winget, "install", lambda pid, **kw: plan)
    rc = _run(monkeypatch, ["winget", "install", "Mozilla.Firefox", "--dry-run"])
    assert rc == cli_compat.EXIT_OK
    text = capsys.readouterr().out
    assert "Firefox" in text and "Would run:" in text


# --------------------------------------------------------------------------- #
# winget: list / update-index / error propagation
# --------------------------------------------------------------------------- #
def test_cmd_winget_list_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    items = [{"id": "Mozilla.Firefox", "name": "Mozilla Firefox", "version": "156.0.1", "prefix": "firefox",
             "update_available": True, "latest": "157.0"}]
    monkeypatch.setattr(winget, "list_installed", lambda **kw: items)
    rc = _run(monkeypatch, ["winget", "list"])
    assert rc == cli_compat.EXIT_OK
    text = capsys.readouterr().out
    assert "157.0" in text


def test_cmd_winget_list_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(winget, "list_installed", lambda **kw: [])
    rc = _run(monkeypatch, ["winget", "list"])
    assert rc == cli_compat.EXIT_OK
    assert "Nothing installed" in capsys.readouterr().out


def test_cmd_winget_update_index(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    class FakeConn:
        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(winget, "load_index", lambda **kw: FakeConn())
    monkeypatch.setattr(winget, "index_info", lambda con, *a, **kw: {"packages": 15141, "last_modified": "", "etag": ""})
    rc = _run(monkeypatch, ["winget", "update-index", "--json"])
    assert rc == cli_compat.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["packages"] == 15141


def test_cmd_winget_wraps_wingeterror_as_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def boom(q: str, **kw: Any) -> Any:
        raise winget.WingetError("catalogue is unreachable")

    monkeypatch.setattr(winget, "search", boom)
    rc = _run(monkeypatch, ["winget", "search", "x", "--json"])
    assert rc == cli_compat.EXIT_ERROR
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "unreachable" in out["error"]


def test_cmd_winget_wraps_wingetunsupported_with_exit_3(monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture) -> None:
    def boom(pid: str, version: Optional[str] = None, **kw: Any) -> Any:
        raise winget.WingetUnsupported("this is a Microsoft Store package")

    monkeypatch.setattr(winget, "load_manifest", boom)
    rc = _run(monkeypatch, ["winget", "show", "9NBLGGH4NNS1", "--json"])
    assert rc == cli_compat.EXIT_UNSUPPORTED
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "unsupported"


def test_cmd_winget_no_subcommand_prints_help(monkeypatch: pytest.MonkeyPatch) -> None:
    # consistent with cmd_prefixes/cmd_recipes/cmd_binfmt: an unrecognised sub-action prints the
    # subcommand's own --help (argparse exits 0), rather than returning EXIT_USAGE.
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, ["winget"])
    assert ei.value.code == 0
