"""Tests for the Windows-side transfer kit (SPEC-WINDOWS §29.11, §27.3).

``LindosTransfer.ps1`` + ``LindosTransfer.cmd`` + ``README.txt`` run on the *old* Windows PC, so
most checks here are static and run on every OS: ASCII/LF, required parameters, the exact
robocopy contract, the §29.11 manifest keys, and the honesty rules (no secret stores read, no
elevation except the opt-in Wi-Fi step, never ``C:\\Windows\\Fonts``, never deleting user data).

Where PowerShell exists the script is also parsed with
``[System.Management.Automation.Language.Parser]::ParseFile`` (must report zero errors).  On
Windows a single harness dot-sources the script (its main body is skipped when dot-sourced) and
exercises the pure functions plus real robocopy / XML / JSON behaviour on synthetic fixtures made
here; finally the whole script runs once with ``-WhatIf -InventoryOnly -Include apps`` into a
temp dir, which only reads the registry and must write nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PKG = Path(__file__).resolve().parents[1]
KIT = PKG / "root" / "usr" / "share" / "lindos" / "transfer" / "windows"
PS1 = KIT / "LindosTransfer.ps1"
CMD = KIT / "LindosTransfer.cmd"
README = KIT / "README.txt"
KIT_FILES = (PS1, CMD, README)

# SPEC-WINDOWS §29.5 category ids (binding, in this order).
CATEGORIES = ["desktop", "documents", "downloads", "music", "pictures", "videos", "saved-games",
              "favorites", "onedrive", "bookmarks", "firefox", "wallpaper", "fonts", "wifi", "apps",
              "steam-games"]
# SPEC-WINDOWS §29.11 manifest (binding keys, in this order).
MANIFEST_KEYS = ["schema", "tool", "tool_version", "created", "computer", "windows", "user", "folders",
                 "browsers", "wallpaper", "wallpaper_style", "fonts", "wifi", "apps", "winget_export",
                 "steam", "skipped"]
APP_KEYS = ["name", "version", "publisher", "install_location", "scope", "arch"]
# SPEC-WINDOWS §29.11 robocopy contract.
ROBOCOPY_FLAGS = ["/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1", "/XJ", "/XA:O", "/MT:8", "/NP", "/NDL",
                  "/XF"]
ROBOCOPY_EXCLUDES = ["desktop.ini", "Thumbs.db", "~$*"]
# Honesty tokens that must never appear in anything Lindos ships (SPEC-WINDOWS §27.1, SPEC-VM §26).
FORBIDDEN_TOKENS = ("spoof", "hwid", "attestation", "smbios", "vm-detect", "vmdetect", "kvm=off",
                    "hv-vendor-id", "acpitable")


def _text(path: Path) -> str:
    return path.read_bytes().decode("ascii")


def _ps1() -> str:
    return _text(PS1)


def _code_lines() -> List[str]:
    """Script lines without comment-only lines and without the comment-based help block."""
    out: List[str] = []
    in_help = False
    for line in _ps1().split("\n"):
        stripped = line.strip()
        if stripped.startswith("<#"):
            in_help = True
        if in_help:
            if stripped.endswith("#>"):
                in_help = False
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def _block(name: str) -> str:
    text = _ps1()
    start = text.index("# BEGIN OPT-IN: %s" % name)
    end = text.index("# END OPT-IN: %s" % name)
    assert start < end
    return text[start:end]


def _outside_blocks(*names: str) -> str:
    text = _ps1()
    for name in names:
        block = _block(name)
        text = text.replace(block, "")
    return text


def _function_body(name: str) -> str:
    text = _ps1()
    m = re.search(r"^function %s \{\n(.*?)^\}\n" % re.escape(name), text, re.S | re.M)
    assert m, "function %s not found" % name
    return m.group(1)


# --------------------------------------------------------------------------------------------
# files, encodings, line endings
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", KIT_FILES, ids=lambda p: p.name)
def test_kit_file_is_ascii_lf_without_bom(path: Path) -> None:
    data = path.read_bytes()
    assert data, "%s is empty" % path
    assert not data.startswith(b"\xef\xbb\xbf"), "%s must not have a BOM (ASCII-only)" % path.name
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    assert not bad, "%s has non-ASCII bytes at %s (PowerShell 5.1 reads BOM-less files as ANSI)" % (path.name, bad[:5])
    assert b"\r" not in data, "%s must use LF line endings" % path.name
    assert b"\t" not in data or path is README, "%s: use spaces, not tabs" % path.name
    assert data.endswith(b"\n")


@pytest.mark.parametrize("path", KIT_FILES, ids=lambda p: p.name)
def test_kit_file_has_no_evasion_tokens(path: Path) -> None:
    low = path.read_bytes().decode("ascii").lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in low, "HONESTY VIOLATION: %r in %s" % (tok, path.name)


# --------------------------------------------------------------------------------------------
# LindosTransfer.ps1: structure and parameters
# --------------------------------------------------------------------------------------------
def test_ps1_header_is_strict_and_51_compatible() -> None:
    text = _ps1()
    assert text.startswith("#Requires -Version 5.1\n")
    assert "Set-StrictMode -Version Latest" in text
    assert "$ErrorActionPreference = 'Stop'" in text
    assert "[CmdletBinding(SupportsShouldProcess = $true)]" in text  # gives -WhatIf
    # PowerShell 7-only syntax must not be used (the kit targets Windows PowerShell 5.1).
    code = "\n".join(_code_lines())
    for pattern in (r"\?\?", r"\?\.", r"&&", r"\|\|", r"\bclean\s*\{", r"-Parallel\b",
                    r"ConvertFrom-Json\s+-AsHashtable", r"-Encoding\s+utf8NoBOM", r"\$IsWindows"):
        assert not re.search(pattern, code), "PowerShell 7-only construct %r used" % pattern


def _param_block() -> str:
    text = _ps1()
    m = re.search(r"\[CmdletBinding\(SupportsShouldProcess = \$true\)\]\nparam\((.*?)\n\)\n", text, re.S)
    assert m, "param() block not found right after [CmdletBinding()]"
    return m.group(1)


@pytest.mark.parametrize("name,typ", [
    ("Destination", "string"), ("User", "string"), ("Include", "string[]"),
    ("IncludeWifiPasswords", "switch"), ("IncludeSteamGames", "switch"), ("InventoryOnly", "switch"),
    ("IncludeFirefoxPasswords", "switch"),
])
def test_ps1_has_parameter(name: str, typ: str) -> None:
    assert re.search(r"\[%s\]\$%s\b" % (re.escape(typ), name), _param_block()), "missing -%s [%s]" % (name, typ)
    assert ".PARAMETER %s" % name in _ps1(), "-%s is not documented in the help" % name


def test_ps1_whatif_comes_from_shouldprocess_not_a_custom_switch() -> None:
    assert "$WhatIf" not in _param_block()
    assert "$script:DryRun = [bool]$WhatIfPreference" in _ps1()


def test_ps1_categories_match_spec() -> None:
    m = re.search(r"\$script:Categories = @\((.*?)\)\n", _ps1(), re.S)
    assert m
    assert re.findall(r"'([a-z-]+)'", m.group(1)) == CATEGORIES
    help_text = _ps1().split(".PARAMETER Include", 1)[1].split(".PARAMETER", 1)[0]
    for cat in CATEGORIES:
        assert cat in help_text


def test_ps1_chromium_sources_have_distinct_ids_per_browser() -> None:
    """Regression test: Opera and Opera GX must not share an ``Id`` (they used to, so the manifest
    labelled every Opera GX bookmark item as plain 'Opera' -- see ``lindos_transfer.browsers``'s
    ``CHROMIUM_BROWSERS``, which already keeps ``opera``/``opera-gx`` distinct on the reading side)."""
    body = _function_body("Get-ChromiumSources")
    ids = re.findall(r"Id = '([a-z-]+)'", body)
    assert ids, "no Chromium sources were found in Get-ChromiumSources"
    assert len(ids) == len(set(ids)), f"Get-ChromiumSources has duplicate browser ids: {ids}"
    assert "opera" in ids and "opera-gx" in ids


def test_ps1_start_up_checks() -> None:
    text = _ps1()
    assert "$ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage'" in text
    assert "Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe" in text
    assert "[Environment]::Is64BitProcess" in text
    # Dot-sourcing loads functions only (used by the harness below).
    assert "if ($MyInvocation.InvocationName -ne '.') {" in text
    # The language-mode check runs before anything that needs FullLanguage.
    main = _function_body("Invoke-LindosTransfer")
    assert main.index("Test-FullLanguage") < main.index("Invoke-64BitRelaunch") < main.index("Invoke-TransferMain")


def test_ps1_output_folder_name_and_json_writing() -> None:
    text = _ps1()
    assert "'LindosTransfer-' + $computer + '-' + $stamp" in text
    assert "ToString('yyyyMMdd-HHmm', [System.Globalization.CultureInfo]::InvariantCulture)" in text
    body = _function_body("Write-JsonFile")
    assert "ConvertTo-Json -InputObject $Object -Depth 10" in body
    assert "Out-File -LiteralPath $Path -Encoding utf8" in body
    # ISO-8601 UTC strings, invariant culture (':' is culture-dependent in .NET formats).
    assert "ToUniversalTime().ToString(\"yyyy-MM-dd'T'HH:mm:ss'Z'\", [System.Globalization.CultureInfo]::InvariantCulture)" in text


def test_ps1_manifest_keys_match_spec() -> None:
    text = _ps1()
    block = text[text.index("# BEGIN MANIFEST"):text.index("# END MANIFEST")]
    keys = re.findall(r"^        (\w+) = ", block, re.M)
    assert keys == MANIFEST_KEYS
    assert "user = [ordered]@{ name = $UserName; profile = $UserProfile }" in block
    assert "schema = 1" in block
    # Nested shapes (§29.11): wifi {dir, with_keys}, steam {dir, games_copied}, browser entries.
    assert "[ordered]@{ dir = 'wifi'; with_keys = [bool]$withKeys }" in text
    assert "[ordered]@{ dir = 'steam'; games_copied = [bool]$gamesCopied }" in text
    assert "[ordered]@{ browser = $source.Id; profile = $prof.Profile; name = $name; bookmarks = $bookmarks }" in text
    assert "[ordered]@{ browser = 'firefox'; profile = $prof.Id; path = $rel }" in text
    assert "[ordered]@{ path = $Path; reason = $Reason }" in text
    assert "Add-Skipped $f 'online-only'" in text


def test_ps1_apps_json_record_keys() -> None:
    body = _function_body("ConvertTo-AppRecord")
    assert re.findall(r"^        (\w+) = ", body, re.M) == APP_KEYS
    apps = _function_body("Get-InstalledApps")
    assert "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall" in apps
    assert "HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall" in apps
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall" in apps
    keep = _function_body("Test-KeepApp")
    for needle in ("DisplayName", "SystemComponent", "ParentKeyName", "ReleaseType", "KB\\d{5,}", "RuntimeNamePattern"):
        assert needle in keep


def test_ps1_robocopy_contract() -> None:
    body = _function_body("Get-RobocopyArguments")
    for flag in ROBOCOPY_FLAGS:
        assert "'%s'" % flag in body, "robocopy flag %s missing" % flag
    assert "'/UNILOG+:' + $LogPath" in body
    for pattern in ROBOCOPY_EXCLUDES:
        assert "'%s'" % pattern in body
    # FAT32: exclude files > 4 GiB - 1 byte (and use FAT time granularity); never split files.
    assert "$script:Fat32MaxFileBytes = 4294967295" in _ps1()
    assert "'/MAX:' + $MaxFileBytes" in body and "'/FFT'" in body
    # Success is exit code < 8 (robocopy's bitmask), never "== 0".
    copy = _function_body("Copy-PlannedFolder")
    assert "if ($code -lt 8)" in copy
    assert "robocopy.exe" in _function_body("Invoke-RobocopyWithProgress")


def test_ps1_never_uses_destructive_or_hydrating_robocopy_options() -> None:
    code = "\n".join(_code_lines())
    # Mirror/purge/move would delete or move source files; /B and /ZB (backup mode) and /EFSRAW
    # misbehave on cloud files; /SEC and /COPYALL copy ACLs; /IPG and /LFSM clash with /MT.
    bad = re.findall(r"'/(MIR|PURGE|MOVE?|B|ZB|EFSRAW|COPYALL|SEC|SECFIX|IPG|LFSM|CREATE)(?::[^']*)?'", code, re.I)
    assert not bad, "forbidden robocopy options: %s" % bad
    assert "/COPY:DATSOU" not in code and "/COPY:DATS" not in code


# --------------------------------------------------------------------------------------------
# LindosTransfer.ps1: honesty rules (SPEC-WINDOWS §27.3, §27.4)
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "Login Data", "Local State", "Web Data", "Cookies", "Network\\Cookies", "cookies.sqlite", "Wlansvc",
    "\\Protect", "\\Credentials", "\\Vault", "hiberfil", "pagefile", "swapfile", "MEMORY.DMP", "systemprofile\\",
    "ServiceProfiles", "C:\\Windows\\Fonts", "SystemRoot%\\Fonts", "EncryptedBookmarks2", "os_crypt", "CryptUnprotectData",
    "ProtectedData", "Unprotect", "cert9.db", "Get-FileHash",
])
def test_ps1_never_touches_secret_stores(name: str) -> None:
    assert name not in _ps1(), "the kit must never read %r (SPEC-WINDOWS §27.3)" % name


@pytest.mark.parametrize("word", ["SAM", "SECURITY"])
def test_ps1_never_names_the_sam_or_security_hive(word: str) -> None:
    assert not re.search(r"\b%s\b" % word, _ps1())


def test_ps1_firefox_password_files_only_inside_the_opt_in() -> None:
    outside = _outside_blocks("firefox-passwords")
    for name in ("key4.db", "logins.json"):
        assert name in _block("firefox-passwords")
        assert name not in outside, "%s referenced outside the opt-in block" % name
    calls = [line.strip() for line in _ps1().split("\n")
             if "Copy-FirefoxPasswordFiles" in line and not line.strip().startswith("function")]
    assert calls == ["if ($script:FirefoxPasswords) { Copy-FirefoxPasswordFiles -ProfileDir $prof.Path -DestDir $destDir }"]
    # The flag is only ever set from the interactive confirmation.
    sets = [line.strip() for line in _ps1().split("\n") if re.match(r"\s*\$script:FirefoxPasswords = ", line)]
    assert sets[-1].startswith("$script:FirefoxPasswords = Confirm-OptIn ")
    assert all(s in ("$script:FirefoxPasswords = $false",) or "Confirm-OptIn" in s for s in sets)


def test_ps1_elevation_only_in_the_wifi_opt_in() -> None:
    block = _block("wifi-passwords")
    outside = _outside_blocks("wifi-passwords")
    assert "key=clear" in block and "key=clear" not in outside
    assert block.count("-Verb RunAs") == 1
    assert not re.search(r"RunAs|runas|-Verb\b", outside), "elevation outside the Wi-Fi opt-in"
    for forbidden in ("Set-ExecutionPolicy", "Start-Process -Verb", "gsudo", "sudo "):
        assert forbidden not in outside
    # Wi-Fi password export only happens after the confirmation, and <protected> is checked.
    sets = [line.strip() for line in _ps1().split("\n") if re.match(r"\s*\$script:WifiPasswords = ", line)]
    assert sets[-1].startswith("$script:WifiPasswords = Confirm-OptIn ")
    assert "if ($WithKeys) { $exported = Invoke-WifiPasswordExport" in _ps1()
    update = _function_body("Update-WifiExportFiles")
    assert "local-name()='protected'" in update and "-ieq 'false'" in update
    assert "$withKeys = ($WithKeys -and $counts.Clear -gt 0)" in _ps1()


def test_ps1_wifi_prompt_text_matches_spec() -> None:
    text = _ps1()
    assert "This writes your Wi-Fi passwords in plain text into the transfer folder. Keep it private and" in text
    assert "delete it after importing - deleting from a USB stick does not securely erase it." in text


def test_ps1_never_copies_windows_fonts() -> None:
    text = _ps1()
    assert text.count("Join-Path $script:WindowsDir 'Fonts'") == 1
    assert "Join-Path $script:WindowsDir 'Fonts'" in _function_body("Test-IsWindowsFontsPath")
    fonts = _function_body("Copy-UserFonts")
    assert "'Microsoft\\Windows\\Fonts'" in fonts and "Test-IsWindowsFontsPath $dir" in fonts
    assert not re.search(r"\$env:(WINDIR|SystemRoot)[^\n]*Fonts", text, re.I)


def test_ps1_deletes_only_its_own_files() -> None:
    lines = [i for i, line in enumerate(_code_lines()) if "Remove-Item" in line]
    body = _function_body("Remove-OwnFile")
    assert body.count("Remove-Item") == 2 and len(lines) == 2, "Remove-Item used outside Remove-OwnFile"
    assert "Refusing to delete" in body
    code = "\n".join(_code_lines())
    for verb in ("Move-Item", "Rename-Item", "Clear-Content", "Set-Content", "Set-ItemProperty",
                 "New-ItemProperty", "Remove-ItemProperty", "reg.exe", "reg load", "reg add", "reg delete",
                 "Invoke-Expression", "iex ", "Invoke-WebRequest", "Invoke-RestMethod", "Net.WebClient",
                 "Start-BitsTransfer", "DownloadFile", "Get-Content", "vssadmin", "cipher"):
        assert verb not in code, "%s must not be used by the kit" % verb


def test_ps1_skips_online_only_files_without_opening_them() -> None:
    text = _ps1()
    state = _function_body("Get-CloudState")
    for bit in ("0x00400000", "0x00040000", "0x00001000"):
        assert bit in state
    measure = _function_body("Measure-TransferFolder")
    assert "GetFileSystemInfos()" in measure and "Get-LinkKind" in measure
    for opener in ("OpenRead", "ReadAll", "Open(", "Get-Content", "Copy-Item"):
        assert opener not in measure
    assert "'/XA:O'" in text


# --------------------------------------------------------------------------------------------
# LindosTransfer.cmd and README.txt
# --------------------------------------------------------------------------------------------
def test_cmd_launcher_is_minimal() -> None:
    text = _text(CMD)
    lines = [line for line in text.split("\n") if line]
    assert lines[0] == "@echo off"
    launch = [line for line in lines if "powershell.exe" in line]
    assert launch == ['"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile '
                      '-ExecutionPolicy Bypass -File "%~dp0LindosTransfer.ps1" %*']
    # LF-only batch files break labels/goto (cmd.exe's 512-byte block parser): keep it tiny and
    # label-free.
    assert len(CMD.read_bytes()) < 512
    for line in lines:
        assert not line.lstrip().startswith(":"), "no labels in the LF-only launcher"
        assert not re.search(r"\b(goto|call)\b", line, re.I), "no goto/call in the LF-only launcher"
    assert "pause" in lines and lines[-1] == "exit /b %LINDOS_TRANSFER_RC%"


def test_readme_is_plain_language_and_complete() -> None:
    text = _text(README)
    for needle in ("LindosTransfer.cmd", "exFAT", "FAT32", "4 GB", "OneDrive", "Transfer from Windows",
                   "Use a transfer folder", "PLAIN TEXT", "does not\n      securely erase", "-WhatIf",
                   "-IncludeWifiPasswords", "-IncludeSteamGames", "-InventoryOnly", "never copied",
                   "Extract All", "Windows 10 or Windows 11"):
        assert needle.lower() in text.lower(), needle
    for cat in CATEGORIES:
        assert cat in text
    assert max(len(line) for line in text.split("\n")) <= 90      # readable in Notepad without wrapping


# --------------------------------------------------------------------------------------------
# PowerShell available: parse check (Windows PowerShell 5.1 or pwsh)
# --------------------------------------------------------------------------------------------
def _powershell() -> Optional[str]:
    for exe in ("powershell.exe", "powershell", "pwsh"):
        path = shutil.which(exe)
        if path:
            return path
    return None


def _windows_powershell() -> Optional[str]:
    if os.name != "nt":
        return None
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _run_ps(exe: str, command: str, env: Optional[Dict[str, str]] = None, timeout: int = 180) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, env=full_env, timeout=timeout, stdin=subprocess.DEVNULL)


def test_ps1_parses_with_zero_errors() -> None:
    exe = _powershell()
    if exe is None:
        pytest.skip("PowerShell is not installed (static checks above still ran)")
    command = (
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($env:LT_KIT, [ref]$tokens, [ref]$errors); "
        "'ERRORS=' + $errors.Count; foreach ($e in $errors) { 'L' + $e.Extent.StartLineNumber + ': ' + $e.Message }"
    )
    proc = _run_ps(exe, command, env={"LT_KIT": str(PS1)})
    assert proc.returncode == 0, proc.stderr
    assert "ERRORS=0" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------------------------
# Windows: behaviour of the real functions (dot-sourced) on synthetic fixtures
# --------------------------------------------------------------------------------------------
HARNESS = r"""
$ErrorActionPreference = 'Stop'
. $env:LT_KIT
$script:WindowsDir = 'C:\Windows'
$script:Skipped = New-Object System.Collections.ArrayList
$script:SkippedOverflow = @{}
$script:Failures = 0
$root = $env:LT_ROOT
$r = [ordered]@{}

# -- quoting ---------------------------------------------------------------------------------
$r['quote'] = @((ConvertTo-CommandLineArg 'plain'), (ConvertTo-CommandLineArg 'with space'),
    (ConvertTo-CommandLineArg 'E:\My Stuff\'), (ConvertTo-CommandLineArg 'a"b c'), (ConvertTo-CommandLineArg ''))
$fwd = @{ Destination = 'E:\x y\'; Include = @('documents', 'pictures'); WhatIf = [System.Management.Automation.SwitchParameter]$true; InventoryOnly = [System.Management.Automation.SwitchParameter]$false }
$r['forward'] = (Get-ForwardedArguments -Bound $fwd)

# -- categories ------------------------------------------------------------------------------
$r['cats_default'] = (Get-SelectedCategories -Include @() -WifiPasswords $false -FirefoxPasswords $false -SteamGames $false)
$r['cats_csv'] = (Get-SelectedCategories -Include @('documents,Pictures') -WifiPasswords $false -FirefoxPasswords $false -SteamGames $false)
$r['cats_one'] = (Get-SelectedCategories -Include @('apps') -WifiPasswords $false -FirefoxPasswords $false -SteamGames $false)
$r['cats_switches'] = (Get-SelectedCategories -Include @('documents') -WifiPasswords $true -FirefoxPasswords $true -SteamGames $true)
$r['cats_alias'] = (Get-SelectedCategories -Include @('steam', 'wi-fi') -WifiPasswords $false -FirefoxPasswords $false -SteamGames $false)
$r['cats_bad'] = (Get-SelectedCategories -Include @('documents', 'passwords') -WifiPasswords $false -FirefoxPasswords $false -SteamGames $false)
$r['cats_bad_error'] = $script:UsageError

# -- attributes, styles, magic ---------------------------------------------------------------
$r['cloud'] = @((Get-CloudState 0x00401600), (Get-CloudState 0x00400400), (Get-CloudState 0x00040020),
    (Get-CloudState 0x20), (Get-CloudState 0x00080400), (Get-CloudState 0x400))
$r['styles'] = @((Get-WallpaperStyleName '10' '0' $false), (Get-WallpaperStyleName '0' '1' $false),
    (Get-WallpaperStyleName '0' '0' $false), (Get-WallpaperStyleName '2' '' $false), (Get-WallpaperStyleName '6' '' $false),
    (Get-WallpaperStyleName '22' '' $false), (Get-WallpaperStyleName '99' '' $false), (Get-WallpaperStyleName '3' '' $true),
    (Get-WallpaperStyleName '4' '' $true), (Get-WallpaperStyleName '5' '' $true), (Get-WallpaperStyleName '1' '' $true))
$r['magic'] = @((Get-ImageExtension ([byte[]](0xFF, 0xD8, 0xFF, 0xE0))), (Get-ImageExtension ([byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D))),
    (Get-ImageExtension ([byte[]](0x42, 0x4D, 0x00))), (Get-ImageExtension ([byte[]](0x00, 0x00, 0x00))), (Get-ImageExtension ([byte[]](0xFF))))
$r['stock'] = @((Test-StockWallpaper 'C:\Windows\Web\Wallpaper\Windows\img0.jpg'), (Test-StockWallpaper 'C:\Users\a\Pictures\me.jpg'),
    (Test-StockWallpaper 'C:\Users\a\AppData\Local\Packages\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\LocalCache\Microsoft\IrisService\x.jpg'),
    (Test-StockWallpaper ''))
$r['winfonts'] = @((Test-IsWindowsFontsPath 'C:\Windows\Fonts'), (Test-IsWindowsFontsPath 'c:\windows\fonts\arial.ttf'),
    (Test-IsWindowsFontsPath 'C:\Users\a\AppData\Local\Microsoft\Windows\Fonts'), (Test-IsWindowsFontsPath 'C:\Windows\FontsX'))
$r['safe_names'] = @((Test-SafeFolderName 'Portal 2'), (Test-SafeFolderName '..'), (Test-SafeFolderName 'a\b'),
    (Test-SafeFolderName 'C:x'), (Test-SafeFolderName ''))
$r['inside'] = @((Test-PathInside 'C:\A\B' 'C:\a'), (Test-PathInside 'C:\AB' 'C:\A'), (Test-PathInside 'C:\A' 'C:\A\'))
$temp = [System.IO.Path]::GetTempPath()
$r['zip_preview'] = @((Test-LooksLikeZipPreview (Join-Path $temp 'Temp1_LindosTransfer.zip\LindosTransfer')), (Test-LooksLikeZipPreview 'E:\LindosTransfer'))

# -- apps ------------------------------------------------------------------------------------
$apps = @(
    [pscustomobject]@{ DisplayName = 'Mozilla Firefox (x64 en-US)'; DisplayVersion = '131.0'; Publisher = 'Mozilla'; InstallLocation = '"C:\Program Files\Mozilla Firefox"' },
    [pscustomobject]@{ DisplayVersion = '1.0' },
    [pscustomobject]@{ DisplayName = '   ' },
    [pscustomobject]@{ DisplayName = 'Hidden thing'; SystemComponent = 1 },
    [pscustomobject]@{ DisplayName = 'Office patch'; ParentKeyName = 'Office16' },
    [pscustomobject]@{ DisplayName = 'Some fix'; ReleaseType = 'Security Update' },
    [pscustomobject]@{ DisplayName = 'Update for Windows (KB5000001)' },
    [pscustomobject]@{ DisplayName = 'Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40' },
    [pscustomobject]@{ DisplayName = 'Microsoft .NET Runtime - 8.0.8 (x64)' },
    @{ DisplayName = 'VLC media player'; SystemComponent = '0' }
)
$r['keep'] = @($apps | ForEach-Object { Test-KeepApp $_ })
$r['record_user'] = (ConvertTo-AppRecord -Entry ([pscustomobject]@{ DisplayName = ' Discord '; DisplayVersion = '1.0.9'; Publisher = 'Discord Inc.'; InstallLocation = 'C:\Users\a\AppData\Local\Discord' }) -Scope 'user' -Arch '' -Is64 $true)
$r['record_x86'] = (ConvertTo-AppRecord -Entry ([pscustomobject]@{ DisplayName = 'Old'; DisplayIcon = 'C:\Program Files (x86)\Old\old.exe' }) -Scope 'user' -Arch '' -Is64 $true)
$real = (Get-InstalledApps -IncludeCurrentUser $true)
$r['real_apps_is_array'] = ($real -is [array])
$r['real_apps_keys'] = @()
if (@($real).Count -gt 0) { $r['real_apps_keys'] = @(@($real)[0].Keys) }

# -- Steam / ini -----------------------------------------------------------------------------
$vdf = "`"libraryfolders`"`n{`n  `"0`"`n  {`n    `"path`"    `"C:\\Program Files (x86)\\Steam`"`n    `"apps`" { `"620`" `"1`" }`n  }`n  `"1`"`n  {`n    `"path`"    `"D:\\SteamLibrary`"`n  }`n}`n"
$r['steam_libs'] = (Get-SteamLibraryPaths -VdfText $vdf -SteamRoot 'C:\Program Files (x86)\Steam')
$r['steam_libs_old'] = (Get-SteamLibraryPaths -VdfText "`"LibraryFolders`"`n{`n `"TimeNextStatsReport`" `"123`"`n `"1`" `"E:\\Games\\Steam`"`n}" -SteamRoot '')
$acf = "`"AppState`"`n{`n `"appid`" `"620`"`n `"name`" `"Portal \`"2\`"`"`n `"installdir`" `"Portal 2`"`n `"UserConfig`" { `"language`" `"english`" }`n}"
$r['acf'] = @((Get-VdfValue $acf 'appid'), (Get-VdfValue $acf 'NAME'), (Get-VdfValue $acf 'installdir'), (Get-VdfValue $acf 'missing'))
$ini = ConvertFrom-IniText "[General]`r`nStartWithLastProfile=1`r`n`r`n[Profile0]`r`nName=default-release`r`nIsRelative=1`r`nPath=Profiles/abcd.default-release`r`n; comment`r`n[Install4F96]`r`nDefault=Profiles/abcd.default-release"
$r['ini'] = @(@($ini.Keys), $ini['Profile0']['Path'], $ini['Profile0']['IsRelative'])

# -- skipped list overflow -------------------------------------------------------------------
$script:MaxSkippedListed = 3
foreach ($i in 1..5) { Add-Skipped ('C:\x\' + $i) 'online-only' 'C:\x' }
$r['skipped'] = (Get-SkippedList)
$script:MaxSkippedListed = 5000
$script:Skipped.Clear(); $script:SkippedOverflow = @{}

# -- manifest --------------------------------------------------------------------------------
$m0 = New-TransferManifest -Created (Get-UtcTimestamp) -Computer 'DESKTOP-1' -Windows ([ordered]@{ caption = 'Microsoft Windows 11 Pro'; version = '10.0.26100' }) -UserName 'alice' -UserProfile 'C:\Users\alice' -Folders $null -Browsers @() -Wallpaper $null -WallpaperStyle $null -Fonts $null -Wifi $null -Apps $null -WingetExport $null -Steam $null -Skipped @()
Write-JsonFile -Path (Join-Path $root 'manifest-empty.json') -Object $m0
$one = New-Object System.Collections.ArrayList
[void]$one.Add([ordered]@{ browser = 'chrome'; profile = 'Default'; name = 'Person 1'; bookmarks = 'browsers/chrome/Default/Bookmarks' })
$folders = [ordered]@{ desktop = 'files/desktop'; documents = 'files/documents' }
$m1 = New-TransferManifest -Created (Get-UtcTimestamp) -Computer 'DESKTOP-1' -Windows (Get-WindowsInfo) -UserName 'alice' -UserProfile 'C:\Users\alice' -Folders $folders -Browsers $one.ToArray() -Wallpaper 'wallpaper.jpg' -WallpaperStyle 'fill' -Fonts 'fonts' -Wifi ([ordered]@{ dir = 'wifi'; with_keys = $false }) -Apps 'apps.json' -WingetExport 'apps-winget.json' -Steam ([ordered]@{ dir = 'steam'; games_copied = $false }) -Skipped @([ordered]@{ path = 'C:\Users\alice\OneDrive\x.mkv'; reason = 'online-only' })
Write-JsonFile -Path (Join-Path $root 'manifest-full.json') -Object $m1
$appsOne = New-Object System.Collections.ArrayList
[void]$appsOne.Add((ConvertTo-AppRecord -Entry ([pscustomobject]@{ DisplayName = 'Only app' }) -Scope 'machine' -Arch 'x64' -Is64 $true))
Write-JsonFile -Path (Join-Path $root 'apps-one.json') -Object (ConvertTo-JsonArray $appsOne.ToArray())
Write-JsonFile -Path (Join-Path $root 'apps-none.json') -Object (ConvertTo-JsonArray @())

# -- measuring + robocopy on a synthetic tree ------------------------------------------------
$src = Join-Path $root 'profile\Documents'
$script:OutDir = Join-Path $root 'out'
New-Item -ItemType Directory -Path $script:OutDir -Force | Out-Null
$scan = Measure-TransferFolder -Root $src -ExcludeDirs @((Join-Path $src 'Excluded')) -MaxFileBytes 4000
$r['scan'] = [ordered]@{ files = $scan.Files; bytes = $scan.Bytes; too_big = @($scan.TooBig | ForEach-Object { Split-Path -Leaf $_ });
    link_dirs = @($scan.LinkDirs | ForEach-Object { Split-Path -Leaf $_ }); other = $scan.OtherReparse; unreadable = $scan.Unreadable.Count }
$argsNormal = (Get-RobocopyArguments -Source $src -Target 'T' -LogPath 'L x.txt' -Scan $scan -ExcludeDirs @('C:\ex') -MaxFileBytes 0)
$cloudScan = [pscustomobject]@{ OtherReparse = $true; CloudRecall = [System.Collections.ArrayList]@('C:\od\r1.txt'); LinkFiles = [System.Collections.ArrayList]@('C:\od\l.lnkfile'); LinkDirs = [System.Collections.ArrayList]@('C:\od\junction') }
$argsCloud = (Get-RobocopyArguments -Source 'C:\od' -Target 'T' -LogPath 'L' -Scan $cloudScan -ExcludeDirs @() -MaxFileBytes 4294967295)
$r['args_normal'] = $argsNormal
$r['args_cloud'] = $argsCloud
$entry = [pscustomobject]@{ Category = 'documents'; Label = 'Documents'; Source = $src; Target = 'files\documents'; Exclude = @((Join-Path $src 'Excluded')); Scan = $scan }
$r['copy_ok'] = (Copy-PlannedFolder -Entry $entry -MaxFileBytes 4000)
Add-ScanSkipped -Entry $entry -MaxFileBytes 4000
$r['copy_skipped'] = (Get-SkippedList)
$script:Skipped.Clear()

# -- Remove-OwnFile guard --------------------------------------------------------------------
$outside = Join-Path $root 'outside.txt'
'keep me' | Out-File -LiteralPath $outside -Encoding ascii
try { Remove-OwnFile $outside; $r['remove_outside'] = 'deleted' } catch { $r['remove_outside'] = 'refused' }
$r['remove_outside_exists'] = (Test-Path -LiteralPath $outside)
$inside = Join-Path $script:OutDir 'scratch.tmp'
'x' | Out-File -LiteralPath $inside -Encoding ascii
Remove-OwnFile $inside
$r['remove_inside_exists'] = (Test-Path -LiteralPath $inside)
try { Remove-OwnFile $script:OutDir; $r['remove_outdir'] = 'deleted' } catch { $r['remove_outdir'] = 'refused' }

# -- Wi-Fi exports ---------------------------------------------------------------------------
$r['wifi'] = (Update-WifiExportFiles -Folder (Join-Path $root 'wifi'))

# -- browsers and fonts ----------------------------------------------------------------------
$paths = [pscustomobject]@{ Local = (Join-Path $root 'profile\AppData\Local'); Roaming = (Join-Path $root 'profile\AppData\Roaming') }
$r['chromium_name'] = (Get-ChromiumProfileName (Join-Path $paths.Local 'Google\Chrome\User Data\Default') 'Default')
$r['chromium'] = (Copy-ChromiumBookmarks $paths)
$r['chromium_skipped'] = (Get-SkippedList)
$script:Skipped.Clear()
$script:FirefoxPasswords = $false
$r['firefox'] = (Copy-FirefoxData $paths)
$script:OutDir = Join-Path $root 'out2'
New-Item -ItemType Directory -Path $script:OutDir -Force | Out-Null
$script:FirefoxPasswords = $true
$r['firefox_optin'] = (Copy-FirefoxData $paths)
$script:FirefoxPasswords = $false
$r['fonts'] = (Copy-UserFonts $paths)

ConvertTo-Json -InputObject $r -Depth 10 | Out-File -LiteralPath $env:LT_OUT -Encoding utf8
"""


def _write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="")
    return path


WIFI_PROTECTED = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>Home</name>
  <SSIDConfig><SSID><name>Home</name></SSID></SSIDConfig>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>true</protected><keyMaterial>01000000D08C9DDF0115D1118C7A00C04FC297EB0100</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>
"""
WIFI_CLEAR = WIFI_PROTECTED.replace("<name>Home</name>", "<name>Cafe</name>").replace(
    "<protected>true</protected><keyMaterial>01000000D08C9DDF0115D1118C7A00C04FC297EB0100</keyMaterial>",
    "<protected>false</protected><keyMaterial>correct horse battery</keyMaterial>")
WIFI_OPEN = """<?xml version="1.0"?>
<WLANProfile xmlns="https://www.microsoft.com/networking/WLAN/profile/v1">
  <name>Airport</name>
  <SSIDConfig><SSID><name>Airport</name></SSID></SSIDConfig>
  <MSM><security><authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption></security></MSM>
</WLANProfile>
"""


def _make_fixtures(root: Path) -> None:
    docs = root / "profile" / "Documents"
    _write(docs / "letter.txt", b"hello" * 10)                 # 50 bytes
    _write(docs / "sub dir" / "photo.jpg", b"\xff\xd8\xff" + b"x" * 97)  # 100 bytes
    _write(docs / "desktop.ini", b"[.ShellClassInfo]\n")
    _write(docs / "Thumbs.db", b"cache")
    _write(docs / "~$draft.docx", b"lock")
    _write(docs / "big.iso", b"z" * 5000)                      # > MaxFileBytes 4000 in the harness
    _write(docs / "Excluded" / "inner.txt", b"transfer folder inside Documents")
    _write(root / "junction-target" / "loop.txt", b"must not be copied via the junction")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(docs / "My Music"), str(root / "junction-target")],
                   check=True, capture_output=True)
    for name, text in (("Wi-Fi-Home.xml", WIFI_PROTECTED), ("Wi-Fi-Cafe.xml", WIFI_CLEAR), ("Wi-Fi-Airport.xml", WIFI_OPEN)):
        _write(root / "wifi" / name, text)
    local = root / "profile" / "AppData" / "Local"
    roaming = root / "profile" / "AppData" / "Roaming"
    chrome = local / "Google" / "Chrome" / "User Data" / "Default"
    _write(chrome / "Bookmarks", json.dumps({"version": 1, "roots": {"bookmark_bar": {"children": []}}}))
    # Keys differing only in case break PowerShell 5.1's ConvertFrom-Json; the kit must cope.
    _write(chrome / "Preferences", '{"profile": {"name": "Person \\u00e9 1", "avatar_index": 26}, '
                                   '"x": {"Key": 1, "key": 2}}')
    _write(chrome / "Login Data", b"SQLite format 3\x00secret")   # must never be copied
    _write(chrome / "Network" / "Cookies", b"SQLite format 3\x00secret")
    _write(local / "Google" / "Chrome" / "User Data" / "Local State", b'{"os_crypt": {"encrypted_key": "x"}}')
    _write(local / "Google" / "Chrome" / "User Data" / "Guest Profile" / "Bookmarks", b"{}")
    edge = local / "Microsoft" / "Edge" / "User Data" / "Profile 1"
    _write(edge / "EncryptedBookmarks2", b"\x00encrypted")
    ff_root = roaming / "Mozilla" / "Firefox"
    _write(ff_root / "profiles.ini", "[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default-release\n"
                                     "IsRelative=1\nPath=Profiles/abcd.default-release\nDefault=1\n")
    prof = ff_root / "Profiles" / "abcd.default-release"
    for name in ("places.sqlite", "places.sqlite-wal", "favicons.sqlite", "key4.db", "logins.json",
                 "cookies.sqlite", "cert9.db", "extensions.json", "compatibility.ini"):
        _write(prof / name, b"data:" + name.encode())
    fonts = local / "Microsoft" / "Windows" / "Fonts"
    _write(fonts / "MyHand.ttf", b"\x00\x01\x00\x00font")
    _write(fonts / "Other.OTF", b"OTTOfont")
    _write(fonts / "notes.txt", b"not a font")


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    exe = _windows_powershell()
    if exe is None or not shutil.which("robocopy"):
        pytest.skip("needs Windows PowerShell 5.1 and robocopy (Windows host)")
    root = tmp_path_factory.mktemp("kit harness")      # a space in the path on purpose
    _make_fixtures(root)
    fake_temp = root / "faketemp"
    fake_temp.mkdir()
    out = root / "result.json"
    script = _write(root / "harness.ps1", HARNESS.lstrip())
    env = {"LT_KIT": str(PS1), "LT_ROOT": str(root), "LT_OUT": str(out), "TEMP": str(fake_temp), "TMP": str(fake_temp)}
    full_env = dict(os.environ)
    full_env.update(env)
    proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                          capture_output=True, text=True, env=full_env, timeout=300, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and out.exists(), "harness failed:\nSTDOUT:\n%s\nSTDERR:\n%s" % (proc.stdout, proc.stderr)
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    data["_root"] = str(root)
    data["_stdout"] = proc.stdout
    return data


def test_harness_quoting(harness: Dict[str, Any]) -> None:
    assert harness["quote"] == ["plain", '"with space"', '"E:\\My Stuff\\\\"', '"a\\"b c"', '""']
    assert harness["forward"] == ["-Destination", "E:\\x y\\", "-Include", "documents,pictures", "-WhatIf"]


def test_harness_categories(harness: Dict[str, Any]) -> None:
    assert harness["cats_default"] == CATEGORIES
    assert harness["cats_csv"] == ["documents", "pictures"]
    assert harness["cats_one"] == ["apps"]
    assert harness["cats_switches"] == ["documents", "firefox", "wifi", "steam-games"]
    assert harness["cats_alias"] == ["wifi", "steam-games"]
    assert harness["cats_bad"] == []
    assert "passwords" in harness["cats_bad_error"] and "steam-games" in harness["cats_bad_error"]


def test_harness_attributes_styles_magic(harness: Dict[str, Any]) -> None:
    assert harness["cloud"] == ["offline", "recall", "recall", "", "", ""]
    assert harness["styles"] == ["fill", "tile", "center", "stretch", "fit", "span", "fill", "fit", "fill", "span", "tile"]
    assert harness["magic"] == ["jpg", "png", "bmp", "", ""]
    assert harness["stock"] == [True, False, True, False]
    assert harness["winfonts"] == [True, True, False, False]
    assert harness["safe_names"] == [True, False, False, False, False]
    assert harness["inside"] == [True, False, True]
    assert harness["zip_preview"] == [True, False]


def test_harness_app_filtering(harness: Dict[str, Any]) -> None:
    assert harness["keep"] == [True, False, False, False, False, False, False, False, False, True]
    user = harness["record_user"]
    assert list(user) == APP_KEYS
    assert user == {"name": "Discord", "version": "1.0.9", "publisher": "Discord Inc.",
                    "install_location": "C:\\Users\\a\\AppData\\Local\\Discord", "scope": "user", "arch": "x64"}
    assert harness["record_x86"]["arch"] == "x86"
    assert harness["real_apps_is_array"] is True
    if harness["real_apps_keys"]:
        assert harness["real_apps_keys"] == APP_KEYS


def test_harness_steam_and_ini(harness: Dict[str, Any]) -> None:
    assert harness["steam_libs"] == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]
    assert harness["steam_libs_old"] == ["E:\\Games\\Steam"]
    assert harness["acf"] == ["620", 'Portal "2"', "Portal 2", ""]
    keys, path, rel = harness["ini"]
    assert keys == ["General", "Profile0", "Install4F96"] and path == "Profiles/abcd.default-release" and rel == "1"


def test_harness_skipped_overflow(harness: Dict[str, Any]) -> None:
    skipped = harness["skipped"]
    assert [s["path"] for s in skipped[:3]] == ["C:\\x\\1", "C:\\x\\2", "C:\\x\\3"]
    assert skipped[3]["path"] == "C:\\x" and "2 more" in skipped[3]["reason"]


def _load(root: str, name: str) -> Any:
    raw = (Path(root) / name).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "%s: Out-File -Encoding utf8 writes a BOM (readers use utf-8-sig)" % name
    return json.loads(raw.decode("utf-8-sig"))


def test_harness_manifest_json(harness: Dict[str, Any]) -> None:
    empty = _load(harness["_root"], "manifest-empty.json")
    assert list(empty) == MANIFEST_KEYS
    assert empty["schema"] == 1 and empty["tool"] == "LindosTransfer.ps1" and empty["tool_version"] == "1.0.0"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", empty["created"])
    assert empty["folders"] == {} and empty["browsers"] == [] and empty["skipped"] == []
    assert empty["user"] == {"name": "alice", "profile": "C:\\Users\\alice"}
    for key in ("wallpaper", "wallpaper_style", "fonts", "wifi", "apps", "winget_export", "steam"):
        assert empty[key] is None
    full = _load(harness["_root"], "manifest-full.json")
    assert list(full) == MANIFEST_KEYS
    assert full["browsers"] == [{"browser": "chrome", "profile": "Default", "name": "Person 1",
                                 "bookmarks": "browsers/chrome/Default/Bookmarks"}]
    assert full["folders"] == {"desktop": "files/desktop", "documents": "files/documents"}
    assert full["wifi"] == {"dir": "wifi", "with_keys": False}
    assert full["steam"] == {"dir": "steam", "games_copied": False}
    assert full["skipped"] == [{"path": "C:\\Users\\alice\\OneDrive\\x.mkv", "reason": "online-only"}]
    assert set(full["windows"]) == {"caption", "version"} and full["windows"]["version"].startswith("10.")
    assert _load(harness["_root"], "apps-one.json") == [{"name": "Only app", "version": "", "publisher": "",
                                                         "install_location": "", "scope": "machine", "arch": "x64"}]
    assert _load(harness["_root"], "apps-none.json") == []


def test_harness_measure_never_follows_links(harness: Dict[str, Any]) -> None:
    scan = harness["scan"]
    assert scan["files"] == 2 and scan["bytes"] == 150      # letter.txt + photo.jpg only
    assert scan["too_big"] == ["big.iso"]
    assert scan["link_dirs"] == ["My Music"]
    assert scan["other"] is False and scan["unreadable"] == 0


def test_harness_robocopy_arguments(harness: Dict[str, Any]) -> None:
    normal = harness["args_normal"]
    assert normal[:2] == [str(Path(harness["_root"]) / "profile" / "Documents"), "T"]
    assert normal[2:13] == ["/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1", "/XJ", "/XA:O", "/MT:8", "/NP", "/NDL",
                            "/UNILOG+:L x.txt"]
    assert normal[13:17] == ["/XF", "desktop.ini", "Thumbs.db", "~$*"]
    assert normal[17:] == ["/XD", "C:\\ex"]
    cloud = harness["args_cloud"]
    # OneDrive folders are reparse points: no /XJ, real links excluded one by one instead.
    assert "/XJ" not in cloud and "/XA:O" in cloud
    assert cloud[cloud.index("/UNILOG+:L") + 1:cloud.index("/UNILOG+:L") + 3] == ["/MAX:4294967295", "/FFT"]
    xf = cloud.index("/XF")
    assert cloud[xf + 1:xf + 6] == ["desktop.ini", "Thumbs.db", "~$*", "C:\\od\\r1.txt", "C:\\od\\l.lnkfile"]
    assert cloud[cloud.index("/XD") + 1:] == ["C:\\od\\junction"]


def test_harness_real_robocopy_copy(harness: Dict[str, Any]) -> None:
    assert harness["copy_ok"] is True
    dest = Path(harness["_root"]) / "out" / "files" / "documents"
    copied = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert copied == ["letter.txt", "sub dir/photo.jpg"]      # no junction loop, no desktop.ini/~$, no big file
    assert (dest / "letter.txt").read_bytes() == b"hello" * 10
    assert (Path(harness["_root"]) / "out" / "robocopy-log.txt").exists()
    reasons = {Path(s["path"]).name: s["reason"] for s in harness["copy_skipped"]}
    assert reasons == {"big.iso": "too large for the file system of the target drive (use an exFAT or NTFS drive)",
                       # a user-made junction (not a hidden+system Windows compatibility junction) is reported
                       "My Music": "a link to another folder (not followed; copy the real folder instead)"}
    # The source is untouched.
    assert (Path(harness["_root"]) / "profile" / "Documents" / "desktop.ini").exists()


def test_harness_remove_guard(harness: Dict[str, Any]) -> None:
    assert harness["remove_outside"] == "refused" and harness["remove_outside_exists"] is True
    assert harness["remove_inside_exists"] is False
    assert harness["remove_outdir"] == "refused"


def test_harness_wifi_protected_keys_are_stripped(harness: Dict[str, Any]) -> None:
    assert harness["wifi"] == {"Total": 3, "Clear": 1, "Protected": 1}
    folder = Path(harness["_root"]) / "wifi"
    home = (folder / "Wi-Fi-Home.xml").read_bytes().decode("utf-8-sig")
    assert "keyMaterial" not in home and "<protected>true</protected>" in home and "<name>Home</name>" in home
    cafe = (folder / "Wi-Fi-Cafe.xml").read_text(encoding="utf-8")
    assert "<keyMaterial>correct horse battery</keyMaterial>" in cafe      # untouched (user opted in)
    assert (folder / "Wi-Fi-Airport.xml").read_text(encoding="utf-8") == WIFI_OPEN


def test_harness_browsers_copy_only_bookmark_files(harness: Dict[str, Any]) -> None:
    root = Path(harness["_root"])
    assert harness["chromium_name"] == "Person \u00e9 1"
    assert harness["chromium"] == [{"browser": "chrome", "profile": "Default", "name": "Person \u00e9 1",
                                    "bookmarks": "browsers/chrome/Default/Bookmarks"}]
    chrome = root / "out" / "browsers" / "chrome"
    assert sorted(p.relative_to(chrome).as_posix() for p in chrome.rglob("*") if p.is_file()) == ["Default/Bookmarks"]
    assert not (root / "out" / "browsers" / "edge").exists()
    skipped = harness["chromium_skipped"]
    assert len(skipped) == 1 and skipped[0]["path"].endswith("Profile 1") and "Export bookmarks" in skipped[0]["reason"]
    assert harness["firefox"] == [{"browser": "firefox", "profile": "abcd.default-release",
                                   "path": "browsers/firefox/abcd.default-release"}]
    ff = root / "out" / "browsers" / "firefox" / "abcd.default-release"
    assert sorted(p.name for p in ff.iterdir()) == ["favicons.sqlite", "places.sqlite", "places.sqlite-wal"]
    ff2 = root / "out2" / "browsers" / "firefox" / "abcd.default-release"
    assert sorted(p.name for p in ff2.iterdir()) == ["favicons.sqlite", "key4.db", "logins.json", "places.sqlite",
                                                     "places.sqlite-wal"]
    for secret in ("Login Data", "Cookies", "Local State", "cookies.sqlite", "cert9.db"):
        assert not list((root / "out").rglob(secret)) and not list((root / "out2").rglob(secret))


def test_harness_user_fonts_only(harness: Dict[str, Any]) -> None:
    assert harness["fonts"] == 2
    fonts = Path(harness["_root"]) / "out2" / "fonts"
    assert sorted(p.name for p in fonts.iterdir()) == ["MyHand.ttf", "Other.OTF"]


# --------------------------------------------------------------------------------------------
# Windows: the whole script, test mode (reads the registry only, writes nothing)
# --------------------------------------------------------------------------------------------
def _run_kit(args: List[str], tmp_path: Path, timeout: int = 240) -> subprocess.CompletedProcess:
    exe = _windows_powershell()
    if exe is None:
        pytest.skip("needs Windows PowerShell 5.1 (Windows host)")
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1)] + args,
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=str(tmp_path))


def test_kit_whatif_inventory_writes_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "usb stick"
    dest.mkdir()
    proc = _run_kit(["-WhatIf", "-InventoryOnly", "-Include", "apps", "-Destination", str(dest)], tmp_path)
    assert proc.returncode in (0, 4), proc.stdout + proc.stderr     # 4 only on a PC without any app
    assert "TEST RUN" in proc.stdout and "nothing was written" in proc.stdout
    assert "What if: Performing" not in proc.stdout, "cmdlet WhatIf noise leaked into the output"
    assert list(dest.iterdir()) == [], "a -WhatIf run must not write anything"


def test_kit_rejects_unknown_category(tmp_path: Path) -> None:
    proc = _run_kit(["-WhatIf", "-Include", "passwords", "-Destination", str(tmp_path)], tmp_path)
    assert proc.returncode == 2
    assert "Unknown category 'passwords'" in proc.stdout
    assert list(tmp_path.iterdir()) == []


def test_kit_rejects_missing_destination_and_unknown_user(tmp_path: Path) -> None:
    proc = _run_kit(["-WhatIf", "-Include", "apps", "-Destination", str(tmp_path / "missing")], tmp_path)
    assert proc.returncode == 2 and "does not exist" in proc.stdout
    proc = _run_kit(["-WhatIf", "-Include", "apps", "-User", "Default", "-Destination", str(tmp_path)], tmp_path)
    assert proc.returncode == 2 and "no user" in proc.stdout
