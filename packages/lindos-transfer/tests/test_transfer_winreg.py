"""Tests for the offline-registry facts lindos-transfer needs (SPEC-WINDOWS §29.4, §29.10)."""
from __future__ import annotations

from hive_builder import HiveBuilder, REG_DWORD, REG_SZ

from lindos_transfer.regf import Hive
from lindos_transfer import winreg


def utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def _hive(spec, **kw):
    b = HiveBuilder(minor=5)
    root = b.add_key_tree(spec)
    return Hive.from_bytes(b.to_bytes(root_offset=root, **kw), name="TEST")


# --------------------------------------------------------------------------- #
# uninstall_entries
# --------------------------------------------------------------------------- #
def _uninstall_entry(name, **values):
    vals = [("DisplayName", REG_SZ, utf16z(values.pop("DisplayName")))] if "DisplayName" in values else []
    for k, v in values.items():
        if isinstance(v, int):
            vals.append((k, REG_DWORD, v.to_bytes(4, "little")))
        else:
            vals.append((k, REG_SZ, utf16z(v)))
    return {"name": name, "values": vals}


def test_uninstall_entries_filters_like_programs_and_features() -> None:
    entries = [
        _uninstall_entry("{1}", DisplayName="VLC media player", Publisher="VideoLAN", DisplayVersion="3.0.20"),
        _uninstall_entry("{2}", DisplayName="Update for Windows (KB5000001)"),
        _uninstall_entry("{3}"),  # no DisplayName at all -> dropped
        _uninstall_entry("{4}", DisplayName="Hidden", SystemComponent=1),
        _uninstall_entry("{5}", DisplayName="Office patch", ParentKeyName="Office16"),
        _uninstall_entry("{6}", DisplayName="Some fix", ReleaseType="Security Update"),
        _uninstall_entry("{7}", DisplayName="Notepad++", DisplayVersion="8.6", EstimatedSize=12345),
    ]
    software = _hive({"name": "ROOT", "children": [
        {"name": "Microsoft", "children": [{"name": "Windows", "children": [
            {"name": "CurrentVersion", "children": [{"name": "Uninstall", "children": entries}]}]}]}]})
    kept = winreg.uninstall_entries(software)
    assert {e["name"] for e in kept} == {"VLC media player", "Notepad++"}
    vlc = next(e for e in kept if e["name"] == "VLC media player")
    assert vlc["publisher"] == "VideoLAN" and vlc["version"] == "3.0.20" and vlc["scope"] == "machine"
    npp = next(e for e in kept if e["name"] == "Notepad++")
    assert npp["size_kb"] == 12345


def test_uninstall_entries_unfiltered_keeps_everything_with_a_name() -> None:
    entries = [_uninstall_entry("{1}", DisplayName="A", SystemComponent=1), _uninstall_entry("{2}")]
    software = _hive({"name": "ROOT", "children": [
        {"name": "Microsoft", "children": [{"name": "Windows", "children": [
            {"name": "CurrentVersion", "children": [{"name": "Uninstall", "children": entries}]}]}]}]})
    all_entries = winreg.uninstall_entries(software, filtered=False)
    assert len(all_entries) == 1  # {2} still has no DisplayName, so it can never become an entry


def test_uninstall_entries_reads_wow6432node_from_software_hive() -> None:
    entries32 = [_uninstall_entry("{x86}", DisplayName="32-bit App")]
    software = _hive({"name": "ROOT", "children": [
        {"name": "Microsoft", "children": [
            {"name": "Windows", "children": [{"name": "CurrentVersion", "children": [
                {"name": "Uninstall", "children": []}]}]}]},
        # WOW6432Node is a sibling of Microsoft directly under the SOFTWARE root, not nested in it.
        {"name": "WOW6432Node", "children": [
            {"name": "Microsoft", "children": [{"name": "Windows", "children": [
                {"name": "CurrentVersion", "children": [
                    {"name": "Uninstall", "children": entries32}]}]}]}]},
    ]})
    kept = winreg.uninstall_entries(software)
    assert kept[0]["name"] == "32-bit App" and kept[0]["arch"] == "x86"


def test_uninstall_entries_from_ntuser_hive_uses_user_scope() -> None:
    entries = [_uninstall_entry("{u1}", DisplayName="Discord")]
    ntuser = _hive({"name": "ROOT", "children": [
        {"name": "Software", "children": [
            {"name": "Microsoft", "children": [{"name": "Windows", "children": [
                {"name": "CurrentVersion", "children": [{"name": "Uninstall", "children": entries}]}]}]}]}]})
    kept = winreg.uninstall_entries(ntuser)
    assert kept[0]["name"] == "Discord" and kept[0]["scope"] == "user"


# --------------------------------------------------------------------------- #
# user shell folders / environment / OneDrive
# --------------------------------------------------------------------------- #
def _ntuser_with_folders(folders, onedrive_accounts=None, env=None):
    accounts = []
    for i, folder in enumerate(onedrive_accounts or []):
        accounts.append({"name": f"Account{i}", "values": [("UserFolder", REG_SZ, utf16z(folder))]})
    return _hive({"name": "ROOT", "children": [
        {"name": "Software", "children": [
            {"name": "Microsoft", "children": [
                {"name": "Windows", "children": [{"name": "CurrentVersion", "children": [
                    {"name": "Explorer", "children": [
                        {"name": "User Shell Folders",
                         "values": [(k, REG_SZ, utf16z(v)) for k, v in folders.items()]}]}]}]},
                {"name": "OneDrive", "children": [{"name": "Accounts", "children": accounts}]},
            ]},
        ]},
        {"name": "Environment", "values": [(k, REG_SZ, utf16z(v)) for k, v in (env or {}).items()]},
    ]})


def test_user_shell_folders_returns_raw_values() -> None:
    ntuser = _ntuser_with_folders({"Personal": "%USERPROFILE%\\Documents", "Desktop": "%USERPROFILE%\\Desktop"})
    usf = winreg.user_shell_folders(ntuser)
    assert usf["Personal"] == "%USERPROFILE%\\Documents"
    assert usf["Desktop"] == "%USERPROFILE%\\Desktop"


def test_user_environment_reads_hkcu_environment() -> None:
    ntuser = _ntuser_with_folders({}, env={"TEMP": "%USERPROFILE%\\AppData\\Local\\Temp"})
    env = winreg.user_environment(ntuser)
    assert env["TEMP"] == "%USERPROFILE%\\AppData\\Local\\Temp"


def test_onedrive_folders_prefers_personal_account_first() -> None:
    ntuser = _ntuser_with_folders({}, onedrive_accounts=[])
    # rebuild manually with named accounts so "Personal" sorts first regardless of insertion order
    b = HiveBuilder(minor=5)
    spec = {"name": "ROOT", "children": [{"name": "Software", "children": [{"name": "Microsoft", "children": [
        {"name": "OneDrive", "children": [{"name": "Accounts", "children": [
            {"name": "Business1", "values": [("UserFolder", REG_SZ, utf16z("C:\\Users\\a\\OneDrive - Org"))]},
            {"name": "Personal", "values": [("UserFolder", REG_SZ, utf16z("C:\\Users\\a\\OneDrive"))]},
        ]}]}]}]}]}
    root = b.add_key_tree(spec)
    ntuser2 = Hive.from_bytes(b.to_bytes(root_offset=root), name="OD")
    roots = winreg.onedrive_folders(ntuser2)
    assert roots[0] == "C:\\Users\\a\\OneDrive"
    assert "C:\\Users\\a\\OneDrive - Org" in roots


# --------------------------------------------------------------------------- #
# ProfileList / skip lists
# --------------------------------------------------------------------------- #
def test_profile_list_flags_backup_and_service_sids() -> None:
    software = _hive({"name": "ROOT", "children": [{"name": "Microsoft", "children": [
        {"name": "Windows NT", "children": [{"name": "CurrentVersion", "children": [
            {"name": "ProfileList", "children": [
                {"name": "S-1-5-21-1-2-3-1001",
                 "values": [("ProfileImagePath", REG_SZ, utf16z("C:\\Users\\alice"))]},
                {"name": "S-1-5-21-1-2-3-1001.bak",
                 "values": [("ProfileImagePath", REG_SZ, utf16z("C:\\Users\\alice"))]},
                {"name": "S-1-5-18",
                 "values": [("ProfileImagePath", REG_SZ, utf16z("C:\\Windows\\system32\\config\\systemprofile"))]},
            ]}]}]}]}]})
    # profile_list() is a raw listing (deduplicating real-vs-.bak profiles is profiles.py's job),
    # so both the real SID and its ".bak" copy come back, each correctly flagged.
    entries = winreg.profile_list(software)
    real = [e for e in entries if e["sid"] == "S-1-5-21-1-2-3-1001" and not e["backup"]]
    backup = [e for e in entries if e["sid"] == "S-1-5-21-1-2-3-1001" and e["backup"]]
    system = [e for e in entries if e["sid"] == "S-1-5-18"]
    assert len(real) == 1 and len(backup) == 1
    assert real[0]["service"] is False
    assert system and system[0]["service"] is True


# --------------------------------------------------------------------------- #
# ControlSet / MountedDevices / HiberbootEnabled / ComputerName
# --------------------------------------------------------------------------- #
def _system_hive(*, current=1, computer="DESKTOP-1", hiberboot=None, mounted=None):
    power_values = []
    if hiberboot is not None:
        power_values.append(("HiberbootEnabled", REG_DWORD, (1 if hiberboot else 0).to_bytes(4, "little")))
    mounted_values = [(f"\\DosDevices\\{letter}:", 3, raw) for letter, raw in (mounted or {}).items()]
    return _hive({"name": "ROOT", "children": [
        {"name": "Select", "values": [("Current", REG_DWORD, current.to_bytes(4, "little"))]},
        {"name": f"ControlSet{current:03d}", "children": [
            {"name": "Control", "children": [
                {"name": "ComputerName", "children": [
                    {"name": "ComputerName", "values": [("ComputerName", REG_SZ, utf16z(computer))]}]},
                {"name": "Session Manager", "children": [{"name": "Power", "values": power_values}]},
            ]},
        ]},
        {"name": "MountedDevices", "values": mounted_values},
    ]})


def test_current_control_set_resolves_select_current() -> None:
    system = _system_hive(current=2, computer="PC2")
    assert winreg.current_control_set(system) == "ControlSet002"
    assert winreg.computer_name(system) == "PC2"


def test_current_control_set_falls_back_to_001() -> None:
    # "Select\\Current" points nowhere sane; only ControlSet001 exists
    b = HiveBuilder(minor=5)
    spec = {"name": "ROOT", "children": [
        {"name": "Select", "values": [("Current", REG_DWORD, (99).to_bytes(4, "little"))]},
        {"name": "ControlSet001", "children": []},
    ]}
    root = b.add_key_tree(spec)
    system = Hive.from_bytes(b.to_bytes(root_offset=root), name="FALLBACK")
    assert winreg.current_control_set(system) == "ControlSet001"


def test_hiberboot_enabled_true_false_and_absent() -> None:
    assert winreg.hiberboot_enabled(_system_hive(hiberboot=True)) is True
    assert winreg.hiberboot_enabled(_system_hive(hiberboot=False)) is False
    assert winreg.hiberboot_enabled(_system_hive(hiberboot=None)) is None


def test_mounted_devices_decodes_mbr_and_gpt_and_ignores_junk() -> None:
    import uuid

    mbr = (0x12345678).to_bytes(4, "little") + (65536).to_bytes(8, "little")
    part_uuid = uuid.uuid4()
    gpt = b"DMIO:ID:" + part_uuid.bytes_le
    junk = b"\\??\\Volume{...}"
    system = _system_hive(mounted={"C": mbr, "D": gpt, "E": junk})
    devices = winreg.mounted_devices(system)
    assert devices["C:"] == mbr and devices["D:"] == gpt and devices["E:"] == junk
    from lindos_transfer.profiles import decode_mounted_device

    assert decode_mounted_device(mbr) == ("mbr", "12345678", "65536")
    assert decode_mounted_device(gpt) == ("gpt", str(part_uuid))
    assert decode_mounted_device(junk) is None


# --------------------------------------------------------------------------- #
# windows_version / wallpaper_settings / steam_paths
# --------------------------------------------------------------------------- #
def _current_version_hive(**values):
    vals = [(k, REG_SZ, utf16z(v)) if isinstance(v, str) else (k, REG_DWORD, v.to_bytes(4, "little"))
           for k, v in values.items()]
    return _hive({"name": "ROOT", "children": [{"name": "Microsoft", "children": [
        {"name": "Windows NT", "children": [{"name": "CurrentVersion", "values": vals}]}]}]})


def test_windows_version_relabels_build_22000_plus_as_windows_11() -> None:
    software = _current_version_hive(ProductName="Windows 10 Pro", CurrentBuildNumber="26100",
                                     CurrentMajorVersionNumber=10, CurrentMinorVersionNumber=0,
                                     DisplayVersion="24H2")
    info = winreg.windows_version(software)
    assert info["caption"] == "Windows 11 Pro"
    assert info["version"] == "10.0.26100"
    assert info["display_version"] == "24H2"
    assert info["text"] == "Windows 11 Pro 10.0.26100"


def test_windows_version_keeps_windows_10_below_build_22000() -> None:
    software = _current_version_hive(ProductName="Windows 10 Home", CurrentBuildNumber="19045",
                                     CurrentMajorVersionNumber=10, CurrentMinorVersionNumber=0)
    info = winreg.windows_version(software)
    assert info["caption"] == "Windows 10 Home"


def test_windows_version_missing_key_gives_generic_fallback() -> None:
    software = _hive({"name": "ROOT"})
    info = winreg.windows_version(software)
    assert info == {"caption": "Windows", "version": "", "display_version": "", "text": "Windows"}


def test_wallpaper_settings_reads_control_panel_and_policy() -> None:
    ntuser = _hive({"name": "ROOT", "children": [
        {"name": "Control Panel", "children": [{"name": "Desktop", "values": [
            ("WallPaper", REG_SZ, utf16z("C:\\wall.jpg")), ("WallpaperStyle", REG_SZ, utf16z("10")),
            ("TileWallpaper", REG_SZ, utf16z("0"))]}]},
        {"name": "Software", "children": [{"name": "Microsoft", "children": [{"name": "Windows", "children": [
            {"name": "CurrentVersion", "children": [{"name": "Policies", "children": [
                {"name": "System", "values": [("Wallpaper", REG_SZ, utf16z("C:\\policy.jpg")),
                                              ("WallpaperStyle", REG_SZ, utf16z("4"))]}]}]}]}]}]},
    ]})
    settings = winreg.wallpaper_settings(ntuser)
    assert settings["wallpaper"] == "C:\\wall.jpg" and settings["style"] == "10"
    assert settings["policy_wallpaper"] == "C:\\policy.jpg" and settings["policy_style"] == "4"


def test_steam_paths_prefers_ntuser_then_software_then_wow6432() -> None:
    ntuser = _hive({"name": "ROOT", "children": [{"name": "Software", "children": [
        {"name": "Valve", "children": [{"name": "Steam", "values": [("SteamPath", REG_SZ, utf16z("D:/Steam"))]}]}]}]})
    software = _hive({"name": "ROOT", "children": [{"name": "WOW6432Node", "children": [
        {"name": "Valve", "children": [{"name": "Steam", "values": [
            ("InstallPath", REG_SZ, utf16z("C:\\Program Files (x86)\\Steam"))]}]}]}]})
    paths = winreg.steam_paths(software, ntuser)
    assert paths == ["D:\\Steam", "C:\\Program Files (x86)\\Steam"]
