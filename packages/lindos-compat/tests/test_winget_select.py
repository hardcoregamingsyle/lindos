"""lindos_compat.winget: root->installer inheritance, installer selection/ranking, switch/argv
assembly (CommandLineToArgvW splitting) and the winget version comparator (SPEC-WINDOWS §28.10).

These exercise pure functions against manifest fragments modelled on real packages named in the
winget research digest (Edge, Notepad++, PowerToys, FFmpeg, 7-Zip, VCRedist, Steam) without any
network access."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from lindos_compat import winget


def _manifest(**root: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "PackageIdentifier": "Test.Package", "PackageVersion": "1.0.0", "ManifestType": "merged",
    }
    base.update(root)
    return base


# --------------------------------------------------------------------------- #
# effective_installers: winget's ManifestYamlPopulator rules
# --------------------------------------------------------------------------- #
def test_root_scalars_copied_then_overridden() -> None:
    manifest = _manifest(
        InstallerType="msi", Scope="machine",
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.msi",
                     "InstallerSha256": "a" * 64},
                    {"Architecture": "x86", "Scope": "user", "InstallerUrl": "https://a.test/x86.msi",
                     "InstallerSha256": "b" * 64}],
    )
    installers = winget.effective_installers(manifest)
    assert installers[0]["InstallerType"] == "msi" and installers[0]["Scope"] == "machine"
    assert installers[1]["Scope"] == "user"  # installer-level override wins
    assert installers[0]["BaseInstallerType"] == "msi" and installers[0]["EffectiveInstallerType"] == "msi"


def test_installer_switches_merge_per_key() -> None:
    manifest = _manifest(
        InstallerType="msi", InstallerSwitches={"Silent": "/quiet /norestart"},
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.msi",
                     "InstallerSha256": "a" * 64, "InstallerSwitches": {"Custom": "/mycustom"}}],
    )
    switches = winget.effective_installers(manifest)[0]["InstallerSwitches"]
    assert switches["Silent"] == "/quiet /norestart"  # from root
    assert switches["Custom"] == "/mycustom"          # from the installer


def test_lists_and_dependencies_replace_wholesale() -> None:
    manifest = _manifest(
        InstallerType="exe", Commands=["root-cmd"],
        Dependencies={"PackageDependencies": [{"PackageIdentifier": "Root.Dep"}]},
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.exe",
                     "InstallerSha256": "a" * 64, "Commands": ["installer-cmd"],
                     "Dependencies": {"PackageDependencies": [{"PackageIdentifier": "Installer.Dep"}]}},
                    {"Architecture": "x86", "InstallerUrl": "https://a.test/y.exe", "InstallerSha256": "b" * 64}],
    )
    installers = winget.effective_installers(manifest)
    assert installers[0]["Commands"] == ["installer-cmd"]
    assert installers[0]["Dependencies"]["PackageDependencies"][0]["PackageIdentifier"] == "Installer.Dep"
    # the second installer has no override: it inherits the root's lists wholesale
    assert installers[1]["Commands"] == ["root-cmd"]
    assert installers[1]["Dependencies"]["PackageDependencies"][0]["PackageIdentifier"] == "Root.Dep"


def test_nested_installer_type_inherited_only_for_zip() -> None:
    manifest = _manifest(
        InstallerType="zip", NestedInstallerType="portable",
        NestedInstallerFiles=[{"RelativeFilePath": "app.exe"}],
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.zip", "InstallerSha256": "a" * 64},
                    {"Architecture": "x86", "InstallerType": "exe", "InstallerUrl": "https://a.test/x.exe",
                     "InstallerSha256": "b" * 64}],
    )
    installers = winget.effective_installers(manifest)
    assert installers[0]["EffectiveInstallerType"] == "portable"
    assert installers[0]["NestedInstallerFiles"] == [{"RelativeFilePath": "app.exe"}]
    # the second installer overrides the base type away from zip: it must NOT inherit Nested*
    assert "NestedInstallerType" not in installers[1]
    assert "NestedInstallerFiles" not in installers[1]


def test_product_code_inherited_only_for_arp_types() -> None:
    manifest = _manifest(
        InstallerType="msi", ProductCode="{ROOT-GUID}",
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.msi", "InstallerSha256": "a" * 64},
                    {"Architecture": "x64", "InstallerType": "msix", "InstallerUrl": "https://a.test/x.msix",
                     "InstallerSha256": "b" * 64, "PackageFamilyName": "Test_8wekyb3d8bbwe"}],
    )
    installers = winget.effective_installers(manifest)
    assert installers[0]["ProductCode"] == "{ROOT-GUID}"
    assert "ProductCode" not in installers[1]  # msix does not use ProductCode


def test_default_switches_filled_by_effective_type_msi() -> None:
    manifest = _manifest(
        InstallerType="msi",
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.msi", "InstallerSha256": "a" * 64}],
    )
    switches = winget.effective_installers(manifest)[0]["InstallerSwitches"]
    assert switches["Silent"] == "/quiet /norestart"
    assert switches["SilentWithProgress"] == "/passive /norestart"


def test_default_switches_filled_for_nested_zip_type() -> None:
    manifest = _manifest(
        InstallerType="zip", NestedInstallerType="nullsoft", NestedInstallerFiles=[{"RelativeFilePath": "s.exe"}],
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.zip", "InstallerSha256": "a" * 64}],
    )
    switches = winget.effective_installers(manifest)[0]["InstallerSwitches"]
    assert switches["Silent"] == "/S"


def test_explicit_switch_not_overwritten_by_default() -> None:
    manifest = _manifest(
        InstallerType="inno", InstallerSwitches={"Silent": "/CUSTOMSILENT"},
        Installers=[{"Architecture": "x64", "InstallerUrl": "https://a.test/x.exe", "InstallerSha256": "a" * 64}],
    )
    switches = winget.effective_installers(manifest)[0]["InstallerSwitches"]
    assert switches["Silent"] == "/CUSTOMSILENT"


# --------------------------------------------------------------------------- #
# select_installer / ranking (Lindos' documented deviation from winget)
# --------------------------------------------------------------------------- #
def _installer(**kw: Any) -> Dict[str, Any]:
    base = {"Architecture": "x64", "InstallerUrl": "https://pub.test/f.exe", "InstallerSha256": "a" * 64}
    base.update(kw)
    return base


def test_select_prefers_msi_over_exe() -> None:
    manifest = _manifest(Installers=[
        _installer(InstallerType="exe", InstallerUrl="https://pub.test/a.exe"),
        _installer(InstallerType="msi", InstallerUrl="https://pub.test/a.msi"),
    ])
    chosen = winget.select_installer(manifest)
    assert chosen["EffectiveInstallerType"] == "msi"


def test_select_prefers_user_scope_over_machine() -> None:
    manifest = _manifest(Installers=[
        _installer(InstallerType="exe", Scope="machine", InstallerUrl="https://pub.test/m.exe"),
        _installer(InstallerType="exe", Scope="user", InstallerUrl="https://pub.test/u.exe"),
    ])
    chosen = winget.select_installer(manifest)
    assert chosen["InstallerUrl"] == "https://pub.test/u.exe"


def test_select_prefers_requested_arch() -> None:
    manifest = _manifest(Installers=[
        _installer(InstallerType="exe", Architecture="x64", InstallerUrl="https://pub.test/64.exe"),
        _installer(InstallerType="exe", Architecture="x86", InstallerUrl="https://pub.test/86.exe"),
    ])
    chosen = winget.select_installer(manifest, arch="x86")
    assert chosen["InstallerUrl"] == "https://pub.test/86.exe"


def test_select_uses_msix_only_when_nothing_else_exists() -> None:
    manifest = _manifest(Installers=[_installer(InstallerType="msix", PackageFamilyName="Test_8wekyb3d8bbwe")])
    chosen = winget.select_installer(manifest)
    assert chosen["EffectiveInstallerType"] == "msix"
    manifest["Installers"].append(_installer(InstallerType="exe", InstallerUrl="https://pub.test/b.exe"))
    chosen = winget.select_installer(manifest)
    assert chosen["EffectiveInstallerType"] == "exe"


def test_select_refuses_arm_only(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(Installers=[_installer(InstallerType="exe", Architecture="arm64")])
    with pytest.raises(winget.WingetUnsupported, match="ARM"):
        winget.select_installer(manifest)


def test_select_refuses_plain_http() -> None:
    manifest = _manifest(Installers=[_installer(InstallerType="exe", InstallerUrl="http://pub.test/a.exe")])
    with pytest.raises(winget.WingetUnsupported, match="http"):
        winget.select_installer(manifest)


def test_select_refuses_pwa_and_msstore_and_font() -> None:
    for itype in ("pwa", "msstore", "font"):
        manifest = _manifest(Installers=[_installer(InstallerType=itype)])
        with pytest.raises(winget.WingetUnsupported):
            winget.select_installer(manifest)


def test_select_refuses_missing_hash() -> None:
    manifest = _manifest(Installers=[{"Architecture": "x64", "InstallerType": "exe",
                                       "InstallerUrl": "https://pub.test/a.exe"}])
    with pytest.raises(winget.WingetUnsupported, match="SHA-256"):
        winget.select_installer(manifest)


def test_select_honours_unsupported_os_architectures() -> None:
    manifest = _manifest(Installers=[
        _installer(InstallerType="exe", Architecture="x86", UnsupportedOSArchitectures=["x64"]),
    ])
    with pytest.raises(winget.WingetUnsupported):
        winget.select_installer(manifest, arch="x64")


def test_select_zip_portable_ranks_after_normal_installers() -> None:
    manifest = _manifest(Installers=[
        _installer(InstallerType="zip", NestedInstallerType="portable",
                   NestedInstallerFiles=[{"RelativeFilePath": "a.exe"}], InstallerUrl="https://pub.test/p.zip"),
        _installer(InstallerType="nullsoft", InstallerUrl="https://pub.test/n.exe"),
    ])
    chosen = winget.select_installer(manifest)
    assert chosen["EffectiveInstallerType"] == "nullsoft"


# --------------------------------------------------------------------------- #
# split_command_line (CommandLineToArgvW rules)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, expected", [
    ('/quiet /norestart', ['/quiet', '/norestart']),
    ('"/DIR=C:\\a b" /S', ['/DIR=C:\\a b', '/S']),
    (r'a\\b', ['a\\\\b']),
    (r'a\"b', ['a"b']),
    ('"a b\\"c"', ['a b"c']),
    ('', []),
    ('   ', []),
])
def test_split_command_line_table(text: str, expected: list) -> None:
    assert winget.split_command_line(text) == expected


def test_split_command_line_quoted_group() -> None:
    assert winget.split_command_line('INSTALLFOLDER="C:\\Program Files\\App"') == \
        ['INSTALLFOLDER=C:\\Program Files\\App']


def test_split_command_line_literal_quote_inside_quotes() -> None:
    assert winget.split_command_line('"" ""') == ['', '']


def test_split_command_line_backslash_before_non_quote_is_literal() -> None:
    # backslashes not followed by a quote pass through unchanged; unquoted whitespace still splits.
    assert winget.split_command_line(r'C:\ProgramFiles\App.exe /S') == [r'C:\ProgramFiles\App.exe', '/S']


# --------------------------------------------------------------------------- #
# installer_switches / installer_argv
# --------------------------------------------------------------------------- #
def test_installer_switches_silent_and_custom_and_tokens() -> None:
    inst = {"InstallerSwitches": {"Silent": "/quiet /norestart", "Custom": "/mycustom"},
            "EffectiveInstallerType": "msi"}
    tokens = winget.installer_switches(inst)
    assert tokens == ["/quiet", "/norestart", "/mycustom"]


def test_installer_switches_interactive() -> None:
    inst = {"InstallerSwitches": {"Silent": "/S", "Interactive": "/interactive-flag"},
            "EffectiveInstallerType": "nullsoft"}
    assert winget.installer_switches(inst, interactive=True) == ["/interactive-flag"]
    assert winget.installer_switches(inst, interactive=False) == ["/S"]


def test_installer_switches_install_location_only_when_required() -> None:
    inst = {"InstallerSwitches": {"Silent": "/S", "InstallLocation": "/D=<INSTALLPATH>"},
            "EffectiveInstallerType": "nullsoft", "InstallLocationRequired": "true"}
    tokens = winget.installer_switches(inst, install_path="C:\\Programs\\App")
    assert tokens[-1] == "/D=C:\\Programs\\App"
    inst2 = dict(inst)
    inst2.pop("InstallLocationRequired")
    assert "/D=C:\\Programs\\App" not in winget.installer_switches(inst2, install_path="C:\\Programs\\App")


def test_nullsoft_d_switch_goes_last_and_unquoted() -> None:
    inst = {"InstallerSwitches": {"Silent": "/S", "InstallLocation": '/D="<INSTALLPATH>"',
                                  "Custom": "/NCRC"},
            "EffectiveInstallerType": "nullsoft", "InstallLocationRequired": "true"}
    tokens = winget.installer_switches(inst, install_path="C:\\a b")
    assert tokens[-1] == "/D=C:\\a b"       # last, and the quotes stripped
    assert tokens[:-1] == ["/S", "/NCRC"]


def test_logpath_and_installpath_tokens_replaced() -> None:
    inst = {"InstallerSwitches": {"Silent": '/quiet /log "<LOGPATH>"'}, "EffectiveInstallerType": "msi"}
    tokens = winget.installer_switches(inst, log_path="C:\\windows\\temp\\x.log")
    assert tokens == ["/quiet", "/log", "C:\\windows\\temp\\x.log"]


def test_installer_argv_msi_prepends_msiexec() -> None:
    inst = {"InstallerSwitches": {"Silent": "/quiet"}, "EffectiveInstallerType": "msi"}
    argv = winget.installer_argv(inst, windows_file="Z:\\tmp\\app.msi")
    assert argv == ["msiexec", "/i", "Z:\\tmp\\app.msi", "/quiet"]


def test_installer_argv_exe_runs_directly() -> None:
    inst = {"InstallerSwitches": {"Silent": "/S"}, "EffectiveInstallerType": "nullsoft"}
    argv = winget.installer_argv(inst, windows_file="Z:\\tmp\\app.exe")
    assert argv == ["Z:\\tmp\\app.exe", "/S"]


def test_installer_argv_msix_and_portable_have_no_switches() -> None:
    for etype in ("msix", "portable"):
        inst = {"InstallerSwitches": {"Silent": "/S"}, "EffectiveInstallerType": etype}
        assert winget.installer_argv(inst, windows_file="Z:\\tmp\\app.exe") == ["Z:\\tmp\\app.exe"]


# --------------------------------------------------------------------------- #
# version comparator (winget Version semantics, with the verifier's corrections)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a, b, expected", [
    ("1.0", "1.0.0", 0),
    ("1.0", "1.0-beta", 1),
    ("1.0-beta", "1.0", -1),
    ("1.2", "1.10", -1),
    ("v1.2", "1.2", 0),
    ("1.0-Beta", "1.0-beta", 0),
    ("Latest", "999.0", 1),
    ("Unknown", "0.0.1", -1),
    ("1.0.0", "1.0.0", 0),
    ("2.0", "1.9.9", 1),
])
def test_compare_versions_table(a: str, b: str, expected: int) -> None:
    assert winget.compare_versions(a, b) == expected


def test_compare_versions_is_antisymmetric() -> None:
    assert winget.compare_versions("1.2.3", "1.2.4") == -winget.compare_versions("1.2.4", "1.2.3")


# --------------------------------------------------------------------------- #
# exit code outcomes (default tables per effective type + manifest overrides)
# --------------------------------------------------------------------------- #
def test_exit_code_zero_is_installed() -> None:
    inst = {"EffectiveInstallerType": "msi"}
    assert winget.exit_code_outcome(inst, 0) == ("installed", "installed")


def test_exit_code_manifest_success_codes() -> None:
    inst = {"EffectiveInstallerType": "burn", "InstallerSuccessCodes": [3010]}
    outcome, _ = winget.exit_code_outcome(inst, 3010)
    assert outcome == "installed"


def test_exit_code_msi_default_table() -> None:
    inst = {"EffectiveInstallerType": "msi"}
    outcome, _ = winget.exit_code_outcome(inst, 1602)
    assert outcome == "cancelled"
    outcome, _ = winget.exit_code_outcome(inst, 1638)
    assert outcome == "already-installed"
    outcome, _ = winget.exit_code_outcome(inst, 3010)
    assert outcome == "installed-restart"


def test_exit_code_inno_default_table() -> None:
    inst = {"EffectiveInstallerType": "inno"}
    assert winget.exit_code_outcome(inst, 2)[0] == "cancelled"
    assert winget.exit_code_outcome(inst, 5)[0] == "cancelled"
    assert winget.exit_code_outcome(inst, 8)[0] == "failed"


def test_exit_code_manifest_expected_return_codes_override() -> None:
    inst = {"EffectiveInstallerType": "exe",
            "ExpectedReturnCodes": [{"InstallerReturnCode": 42, "ReturnResponse": "cancelledByUser"}]}
    outcome, _ = winget.exit_code_outcome(inst, 42)
    assert outcome == "cancelled"


def test_exit_code_unknown_is_failed() -> None:
    inst = {"EffectiveInstallerType": "exe"}
    outcome, message = winget.exit_code_outcome(inst, 17)
    assert outcome == "failed" and "17" in message
