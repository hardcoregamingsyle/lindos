"""lindos.update + the lindos-update CLI (SPEC-UPDATE.md §35-§36).

Hermetic: no network, no root, no real apt/dpkg/gpg -- every external command is reached through
an injectable ``run``/``which``/``fetch`` (module functions), and every filesystem path goes
through a synthetic ``LINDOS_ROOT`` (never the real ``/etc/apt``/``/var/lib/apt``), exactly like
:mod:`lindos.dualboot`'s own test suite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from lindos import paths
from lindos import update as lupdate


class FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _run_returning(stdout: str, returncode: int = 0):
    def _run(*_args: Any, **_kwargs: Any) -> FakeProc:
        return FakeProc(stdout=stdout, returncode=returncode)
    return _run


def _which_map(mapping: Dict[str, Optional[str]]):
    def _which(name: str) -> Optional[str]:
        return mapping.get(name)
    return _which


# =================================================================================================
# apt list --upgradable parsing + channel classification
# =================================================================================================
UPGRADABLE_SAMPLE = (
    "Listing... Done\n"
    "lindos-core/now 1.0.1 all [upgradable from: 1.0.0]\n"
    "lindos-tune/now 1.0.1 all [upgradable from: 1.0.0]\n"
    "firefox/noble-security 129.0+build2-0ubuntu0.24.04.1 amd64 "
    "[upgradable from: 128.0+build1-0ubuntu0.24.04.1]\n"
    "linux-image-generic/noble-updates 6.8.0.41.41 amd64 [upgradable from: 6.8.0.40.40]\n"
    "linux-headers-generic/noble-updates 6.8.0.41.41 amd64 [upgradable from: 6.8.0.40.40]\n"
)


def test_apt_list_upgradable_parses_and_classifies_channels() -> None:
    updates = lupdate.apt_list_upgradable(run=_run_returning(UPGRADABLE_SAMPLE))
    by_name = {u.name: u for u in updates}
    assert set(by_name) == {"lindos-core", "lindos-tune", "firefox", "linux-image-generic",
                            "linux-headers-generic"}
    assert by_name["lindos-core"].channel == "lindos"
    assert by_name["lindos-core"].installed == "1.0.0" and by_name["lindos-core"].candidate == "1.0.1"
    assert by_name["firefox"].channel == "system"
    assert by_name["firefox"].candidate == "129.0+build2-0ubuntu0.24.04.1"
    assert by_name["linux-image-generic"].channel == "kernel"
    assert by_name["linux-headers-generic"].channel == "kernel"


def test_apt_list_upgradable_skips_header_and_garbage_lines() -> None:
    text = UPGRADABLE_SAMPLE + "some garbage line that is not a package entry\n\n"
    updates = lupdate.apt_list_upgradable(run=_run_returning(text))
    assert len(updates) == 5


def test_apt_list_upgradable_empty_on_nonzero_exit() -> None:
    assert lupdate.apt_list_upgradable(run=_run_returning("junk", returncode=1)) == []


def test_apt_list_upgradable_empty_on_missing_apt() -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("apt not found")
    assert lupdate.apt_list_upgradable(run=_raise) == []


def test_apt_list_upgradable_empty_when_nothing_upgradable() -> None:
    assert lupdate.apt_list_upgradable(run=_run_returning("Listing... Done\n")) == []


# =================================================================================================
# configured_repo_url()
# =================================================================================================
def test_configured_repo_url_none_when_file_missing(core_env) -> None:
    assert lupdate.configured_repo_url() is None


def test_configured_repo_url_parses_flat_format_line(core_env) -> None:
    path = paths.resolve(lupdate.LINDOS_SOURCES_LIST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# a comment\n\n"
                 "deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] "
                 "https://example.test/lindos-apt ./\n")
    assert lupdate.configured_repo_url() == "https://example.test/lindos-apt"


def test_configured_repo_url_none_when_no_recognisable_line(core_env) -> None:
    path = paths.resolve(lupdate.LINDOS_SOURCES_LIST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# nothing useful here\n")
    assert lupdate.configured_repo_url() is None


# =================================================================================================
# repo_status()
# =================================================================================================
def test_repo_status_empty_url() -> None:
    ok, msg = lupdate.repo_status("")
    assert ok is False and "no repo url" in msg.lower()


def test_repo_status_placeholder_refused() -> None:
    ok, msg = lupdate.repo_status(lupdate.PLACEHOLDER_APT_REPO_URL)
    assert ok is False and "placeholder" in msg.lower()


def test_repo_status_non_https_non_local_refused() -> None:
    ok, msg = lupdate.repo_status("http://example.test/repo")
    assert ok is False and "https" in msg.lower()


def test_repo_status_localhost_http_allowed_and_uses_fetch() -> None:
    calls: List[str] = []

    def fetch(url: str):
        calls.append(url)
        return True, "HTTP 200"

    ok, msg = lupdate.repo_status("http://localhost:8080/lindos-apt", fetch=fetch)
    assert ok is True and "reachable" in msg.lower()
    assert calls == ["http://localhost:8080/lindos-apt/InRelease"]


def test_repo_status_https_fetch_success() -> None:
    ok, msg = lupdate.repo_status("https://example.test/lindos-apt", fetch=lambda _u: (True, "HTTP 200"))
    assert ok is True


def test_repo_status_fetch_failure() -> None:
    ok, msg = lupdate.repo_status("https://example.test/lindos-apt", fetch=lambda _u: (False, "timed out"))
    assert ok is False and "timed out" in msg


def test_repo_status_fetch_raises_is_never_propagated() -> None:
    def fetch(_url: str):
        raise OSError("network unreachable")
    ok, msg = lupdate.repo_status("https://example.test/lindos-apt", fetch=fetch)
    assert ok is False and "network unreachable" in msg


def test_repo_status_not_a_url() -> None:
    ok, msg = lupdate.repo_status("not a url at all")
    assert ok is False


# =================================================================================================
# check()
# =================================================================================================
def test_check_defaults_when_nothing_configured(core_env) -> None:
    st = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}))
    assert st.lindos_updates == [] and st.system_updates == [] and st.kernel_available is None
    assert st.booted_kernel == "" and st.booted_is_lindos_kernel is False
    assert st.reboot_required is False
    assert st.repo_configured is False and st.repo_reachable is None
    assert st.refreshed_at is None


def test_check_booted_kernel_and_lindos_marker(core_env) -> None:
    def run(cmd: List[str], **_k: Any) -> FakeProc:
        if os.path.basename(cmd[0]) == "uname":
            return FakeProc(stdout="6.14.0-lindos\n", returncode=0)
        return FakeProc(stdout="Listing... Done\n", returncode=0)
    st = lupdate.check(run=run, which=_which_map({"uname": "/usr/bin/uname"}))
    assert st.booted_kernel == "6.14.0-lindos"
    assert st.booted_is_lindos_kernel is True


def test_check_reboot_required(core_env) -> None:
    marker = paths.resolve(lupdate.REBOOT_REQUIRED_PATH)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    Path(marker).write_text("", encoding="utf-8")
    st = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}))
    assert st.reboot_required is True


def test_check_refreshed_at_none_for_empty_or_missing_lists_dir(core_env) -> None:
    st = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}))
    assert st.refreshed_at is None
    lists_dir = paths.resolve(lupdate.APT_LISTS_DIR)
    os.makedirs(lists_dir, exist_ok=True)
    Path(os.path.join(lists_dir, "lock")).write_text("", encoding="utf-8")
    st2 = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}))
    assert st2.refreshed_at is None       # only 'lock' present -- never actually refreshed


def test_check_refreshed_at_set_once_lists_have_real_content(core_env) -> None:
    lists_dir = paths.resolve(lupdate.APT_LISTS_DIR)
    os.makedirs(lists_dir, exist_ok=True)
    Path(os.path.join(lists_dir, "archive.ubuntu.com_dists_noble_Release")).write_text(
        "fake", encoding="utf-8")
    st = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}))
    assert st.refreshed_at is not None
    assert st.refreshed_at.endswith("+00:00") or "T" in st.refreshed_at


def test_check_kernel_available_prefers_linux_image_row(core_env) -> None:
    text = ("Listing... Done\n"
            "linux-headers-6.14.0-lindos/now 1.0.1 amd64 [upgradable from: 1.0.0]\n"
            "linux-image-6.14.0-lindos/now 1.0.1 amd64 [upgradable from: 1.0.0]\n"
            "linux-modules-6.14.0-lindos/now 1.0.1 amd64 [upgradable from: 1.0.0]\n")
    st = lupdate.check(run=_run_returning(text), which=_which_map({}))
    assert st.kernel_available is not None
    assert st.kernel_available.name == "linux-image-6.14.0-lindos"


def test_check_repo_configured_true_drives_reachability(core_env) -> None:
    path = paths.resolve(lupdate.LINDOS_SOURCES_LIST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] "
                 "https://example.test/lindos-apt ./\n")
    st_ok = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}),
                          fetch=lambda _u: (True, "HTTP 200"))
    assert st_ok.repo_configured is True and st_ok.repo_reachable is True
    st_bad = lupdate.check(run=_run_returning("Listing... Done\n"), which=_which_map({}),
                           fetch=lambda _u: (False, "nope"))
    assert st_bad.repo_configured is True and st_bad.repo_reachable is False


def test_update_status_and_package_update_to_dict_roundtrip() -> None:
    pu = lupdate.PackageUpdate(name="lindos-core", installed="1.0.0", candidate="1.0.1", channel="lindos")
    assert pu.to_dict() == {"name": "lindos-core", "installed": "1.0.0", "candidate": "1.0.1", "channel": "lindos"}
    st = lupdate.UpdateStatus(refreshed_at=None, lindos_updates=[pu], system_updates=[], kernel_available=None,
                              booted_kernel="6.8.0-generic", booted_is_lindos_kernel=False, reboot_required=False,
                              repo_configured=False, repo_reachable=None)
    data = st.to_dict()
    assert data["lindos_updates"] == [pu.to_dict()]
    assert data["kernel_available"] is None
    assert json.dumps(data)   # must be JSON-serialisable


# =================================================================================================
# refresh_payload / apply_payload / cleanup_payload
# =================================================================================================
def test_refresh_and_cleanup_payload_are_empty() -> None:
    assert lupdate.refresh_payload() == {}
    assert lupdate.cleanup_payload() == {}


def test_apply_payload_builds_sorted_exact_list() -> None:
    payload = lupdate.apply_payload({"lindos-tune": "1.0.1", "lindos-core": "1.0.1"})
    assert payload == {"packages": ["lindos-core=1.0.1", "lindos-tune=1.0.1"]}


def test_apply_payload_excludes_kernel_without_allow_kernel() -> None:
    payload = lupdate.apply_payload({"lindos-core": "1.0.1", "linux-image-6.14.0-lindos": "1.0.1"})
    assert payload["packages"] == ["lindos-core=1.0.1"]
    assert "allow_kernel" not in payload


def test_apply_payload_includes_kernel_with_allow_kernel() -> None:
    payload = lupdate.apply_payload({"linux-image-6.14.0-lindos": "1.0.1"}, allow_kernel=True)
    assert payload == {"packages": ["linux-image-6.14.0-lindos=1.0.1"], "allow_kernel": True}


def test_apply_payload_empty_mapping() -> None:
    assert lupdate.apply_payload({}) == {"packages": []}


# =================================================================================================
# sideload: dpkg-deb / dpkg-query / version compare / directory scan
# =================================================================================================
def test_dpkg_deb_field_reads_and_handles_missing_tool() -> None:
    assert lupdate.dpkg_deb_field("/x.deb", "Package", run=_run_returning("lindos-core\n"),
                                  which=_which_map({"dpkg-deb": "/usr/bin/dpkg-deb"})) == "lindos-core"
    assert lupdate.dpkg_deb_field("/x.deb", "Package", which=_which_map({})) is None
    assert lupdate.dpkg_deb_field("/x.deb", "Package", run=_run_returning("", returncode=2),
                                  which=_which_map({"dpkg-deb": "/usr/bin/dpkg-deb"})) is None


def test_dpkg_installed_version_reads_and_handles_not_installed() -> None:
    assert lupdate.dpkg_installed_version("lindos-core", run=_run_returning("1.0.0\n"),
                                          which=_which_map({"dpkg-query": "/usr/bin/dpkg-query"})) == "1.0.0"
    assert lupdate.dpkg_installed_version("lindos-core", run=_run_returning("", returncode=1),
                                          which=_which_map({"dpkg-query": "/usr/bin/dpkg-query"})) is None
    assert lupdate.dpkg_installed_version("lindos-core", which=_which_map({})) is None


def test_compare_versions_naive_fallback_when_dpkg_missing() -> None:
    which = _which_map({})
    assert lupdate.compare_versions("1.0.0", "1.0.1", which=which) < 0
    assert lupdate.compare_versions("2.0", "1.9", which=which) > 0
    assert lupdate.compare_versions("1.0.0", "1.0.0", which=which) == 0


def test_compare_versions_uses_dpkg_when_available() -> None:
    def run(cmd: List[str], **_k: Any) -> FakeProc:
        # dpkg --compare-versions A OP B -- exit 0 means "true"
        _dpkg, _flag, a, op, b = cmd
        real = {"lt": a < b, "gt": a > b}.get(op, False)
        return FakeProc(returncode=0 if real else 1)
    result = lupdate.compare_versions("1.0.0", "1.0.1", run=run, which=_which_map({"dpkg": "/usr/bin/dpkg"}))
    assert result == -1


#: path -> {"Package": ..., "Version": ...} (empty dict = dpkg-deb "fails" for that file)
_FAKE_DEB_FIELDS: Dict[str, Dict[str, str]] = {}
#: package name -> installed version (absent = not installed)
_FAKE_INSTALLED: Dict[str, str] = {}


def _sideload_run(cmd: List[str], **_k: Any) -> FakeProc:
    if cmd[1] == "--field":
        path, field = cmd[2], cmd[3]
        fields = _FAKE_DEB_FIELDS.get(os.path.basename(path), {})
        value = fields.get(field)
        return FakeProc(stdout=(value + "\n") if value else "", returncode=0 if value else 1)
    if cmd[1] == "-W":
        name = cmd[3]
        version = _FAKE_INSTALLED.get(name)
        return FakeProc(stdout=(version + "\n") if version else "", returncode=0 if version else 1)
    raise AssertionError(f"unexpected command: {cmd}")


def test_scan_sideload_dir_full_matrix(tmp_path: Path) -> None:
    _FAKE_DEB_FIELDS.clear()
    _FAKE_INSTALLED.clear()
    _FAKE_DEB_FIELDS["lindos-core_1.0.1_all.deb"] = {"Package": "lindos-core", "Version": "1.0.1"}
    _FAKE_INSTALLED["lindos-core"] = "1.0.0"                                  # normal upgrade
    _FAKE_DEB_FIELDS["lindos-old_0.9_all.deb"] = {"Package": "lindos-old", "Version": "0.9"}
    _FAKE_INSTALLED["lindos-old"] = "1.0.0"                                   # downgrade!
    _FAKE_DEB_FIELDS["evil-package_1.0_all.deb"] = {"Package": "evil-package", "Version": "1.0"}
    _FAKE_DEB_FIELDS["broken.deb"] = {}                                      # dpkg-deb can't read it
    _FAKE_DEB_FIELDS["no-version_1.0_all.deb"] = {"Package": "lindos-noversion"}  # Version missing

    for name in _FAKE_DEB_FIELDS:
        (tmp_path / name).write_text("fake bytes\n", encoding="utf-8")
    (tmp_path / "not-a-deb.txt").write_text("ignored\n", encoding="utf-8")

    which = _which_map({"dpkg-deb": "/usr/bin/dpkg-deb", "dpkg-query": "/usr/bin/dpkg-query"})
    candidates = lupdate.scan_sideload_dir(str(tmp_path), run=_sideload_run, which=which)
    by_file = {os.path.basename(c.path): c for c in candidates}

    assert set(by_file) == set(_FAKE_DEB_FIELDS)   # the stray .txt is never considered

    ok = by_file["lindos-core_1.0.1_all.deb"]
    assert ok.accepted is True and ok.package == "lindos-core" and ok.reason == ""

    down = by_file["lindos-old_0.9_all.deb"]
    assert down.accepted is True and "downgrade" in down.reason
    assert "1.0.0" in down.reason and "0.9" in down.reason

    evil = by_file["evil-package_1.0_all.deb"]
    assert evil.accepted is False and "not a lindos-* package" in evil.reason

    broken = by_file["broken.deb"]
    assert broken.accepted is False and broken.package is None

    no_ver = by_file["no-version_1.0_all.deb"]
    assert no_ver.accepted is False and "Version" in no_ver.reason


def test_scan_sideload_dir_missing_directory_returns_empty() -> None:
    assert lupdate.scan_sideload_dir("/no/such/directory/at/all") == []


def test_scan_sideload_dir_empty_directory(tmp_path: Path) -> None:
    assert lupdate.scan_sideload_dir(str(tmp_path)) == []


def test_sideload_payload_builds_absolute_paths(tmp_path: Path) -> None:
    f = tmp_path / "lindos-core_1.0.1_all.deb"
    f.write_text("x", encoding="utf-8")
    payload = lupdate.sideload_payload([str(f)])
    assert payload == {"files": [os.path.abspath(str(f))]}


# =================================================================================================
# lindos-update CLI (subprocess, LINDOS_HELPER_DRYRUN=1 from core_env; this host has no real
# apt/uname/dpkg-deb behaving as Lindos would need, so every scenario here is one where "nothing
# is configured/available" is the honest, deterministic outcome)
# =================================================================================================
def test_cli_check_json_shape(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "check", "--json")
    assert proc.returncode in (0, 3), (proc.stdout, proc.stderr)
    data = json.loads(proc.stdout)
    for key in ("refreshed_at", "lindos_updates", "system_updates", "kernel_available",
               "booted_kernel", "booted_is_lindos_kernel", "reboot_required", "repo_configured",
               "repo_reachable"):
        assert key in data
    assert data["repo_configured"] is False and data["repo_reachable"] is None


def test_cli_check_text(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "check")
    assert proc.returncode in (0, 3), (proc.stdout, proc.stderr)
    assert "apt lists last refreshed" in proc.stdout
    assert "not configured yet" in proc.stdout


def test_cli_kernel_status_json(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "kernel-status", "--json")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    data = json.loads(proc.stdout)
    assert "booted_kernel" in data and "kernel_available" in data


def test_cli_repo_status_not_configured(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "repo", "status", "--json")
    assert proc.returncode == 3
    data = json.loads(proc.stdout)
    assert data["configured"] is False and data["reachable"] is None


def test_cli_repo_no_subcommand_is_usage_error(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "repo")
    assert proc.returncode == 2


def test_cli_cleanup_dry_run(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "cleanup", "--yes", "--dry-run")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "autoremove" in proc.stdout


def test_cli_cleanup_declined_without_yes(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "cleanup", input="n\n")
    assert proc.returncode == 0
    assert "cancelled" in proc.stdout


def test_cli_apply_nothing_to_update(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "apply", "--yes", "--json")
    assert proc.returncode == 3
    data = json.loads(proc.stdout)
    assert data["status"] == "nothing-to-do"


def test_cli_sideload_missing_directory(core_env, run_cli, tmp_path: Path) -> None:
    proc = run_cli("lindos-update", "sideload", str(tmp_path / "nope"))
    assert proc.returncode == 1


def test_cli_sideload_empty_directory(core_env, run_cli, tmp_path: Path) -> None:
    proc = run_cli("lindos-update", "sideload", str(tmp_path), "--json")
    assert proc.returncode == 3
    data = json.loads(proc.stdout)
    assert data["status"] == "nothing-to-do" and data["candidates"] == []


def test_cli_help_with_no_command(core_env, run_cli) -> None:
    proc = run_cli("lindos-update")
    assert proc.returncode == 2


def test_cli_version_flag(core_env, run_cli) -> None:
    proc = run_cli("lindos-update", "--version")
    assert proc.returncode == 0
    assert "lindos-update" in (proc.stdout + proc.stderr)
