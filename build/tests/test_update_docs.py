"""docs/UPDATES.md, SPEC-UPDATE.md and docs/RELEASING.md against the code (SPEC-UPDATE.md §38).

The update documentation used to contradict the code (the stock apt-daily timers were "never disabled" although a
preset disables them; `apply` "refreshed the cache" although it did not; a placeholder host named as the address
of the repository). These tests keep that from coming back: every claim that can be checked against a file, a unit
value, a CLI subcommand, a helper action or the state schema is checked.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CORE = REPO / "packages" / "lindos-core"
LIB = CORE / "root" / "usr" / "lib" / "python3" / "dist-packages"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

UPDATES = (REPO / "docs" / "UPDATES.md").read_text(encoding="utf-8")
SPEC = (REPO / "SPEC-UPDATE.md").read_text(encoding="utf-8")
RELEASING = (REPO / "docs" / "RELEASING.md").read_text(encoding="utf-8")
DOCS: Dict[str, str] = {"docs/UPDATES.md": UPDATES, "SPEC-UPDATE.md": SPEC, "docs/RELEASING.md": RELEASING}
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_docs_are_lf() -> None:
    for name in DOCS:
        assert b"\r" not in (REPO / name).read_bytes(), name


# --- claims that used to be wrong ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(DOCS))
def test_no_stale_claims(name: str) -> None:
    text = DOCS[name]
    for stale in ("never disabled", "LINDOS_APT_REPO_URL", "LINDOS_APT_REPO_ENABLE", "packages.lindos.dev",
                  "mintupdate", "Update Manager", "Linux Mint", "protected, never disabled"):
        assert stale not in text, f"{name} still says {stale!r}"


def test_the_apt_daily_timers_really_are_switched_off_as_the_docs_say() -> None:
    preset = (REPO / "packages/lindos-tune/root/usr/lib/systemd/system-preset/90-lindos.preset").read_text(encoding="utf-8")
    assert re.search(r"^disable apt-daily\.timer\s*$", preset, re.M)
    assert re.search(r"^disable apt-daily-upgrade\.timer\s*$", preset, re.M)
    for text in (UPDATES, SPEC):
        assert "apt-daily" in text and re.search(r"switched \*?\*?off", text)
    assert "nothing replaces the unattended *install*" in UPDATES or "nothing replaces the unattended" in SPEC


def test_apply_refreshes_the_lists_as_the_docs_say() -> None:
    cli = (CORE / "root/usr/bin/lindos-update").read_text(encoding="utf-8")
    assert "_refresh_lists" in cli and "--no-refresh" in cli
    assert "refresh the package lists first" in UPDATES and "--no-refresh" in SPEC


def test_the_repository_address_is_not_named_as_a_real_host() -> None:
    for text in DOCS.values():
        assert not re.search(r"https://packages\.", text)
    assert re.search(r"one\s+(place|file)", UPDATES.lower())
    assert "lindos.sources" in UPDATES and "Enabled: no" in UPDATES and "Enabled: no" in SPEC


# --- the docs match the code ---------------------------------------------------------------------------------------
def _cli_module():
    path = CORE / "root" / "usr" / "bin" / "lindos-update"
    loader = importlib.machinery.SourceFileLoader("lindos_update_docs_test", str(path))
    spec = importlib.util.spec_from_loader("lindos_update_docs_test", loader)
    module = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


def test_every_lindos_update_subcommand_is_documented() -> None:
    parser = _cli_module().build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    commands = sorted(action.choices)
    assert {"status", "check", "plan", "apply", "history", "cleanup", "kernel-status", "sideload", "repo"} <= set(commands)
    for command in commands:
        assert f"lindos-update {command}" in UPDATES, command
        assert f"lindos-update {command}" in SPEC, command


def test_every_apply_flag_is_documented() -> None:
    parser = _cli_module().build_parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    apply_flags = [o for a in action.choices["apply"]._actions for o in a.option_strings if o.startswith("--")]
    for flag in apply_flags:
        if flag == "--help":
            continue
        assert flag in UPDATES and flag in SPEC, flag


def test_every_update_helper_action_is_in_the_spec_table() -> None:
    from lindos import helper
    for action in ("apt-get-update", "system-upgrade", "apt-full-upgrade", "cleanup-old-packages", "install-local-debs"):
        assert action in helper.ACTIONS
        assert f"`{action}`" in SPEC, action
        assert action in UPDATES, action


def test_the_state_schema_in_the_spec_lists_every_top_level_field() -> None:
    from lindos import updatestate as us
    plan = us.parse_simulation("Inst lindos-core [1] (2 Lindos:stable [all])\n")
    import datetime as dt
    state = us.build_state(plan, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), refreshed_at=None,
                           refresh={}, repo={}, reboot_pending={}, reboot_reasons=[], booted_kernel="", held=[])
    section = SPEC.split("## 40.")[1].split("## 41.")[0]
    for key in state:
        assert f"`{key}`" in section, f"SPEC-UPDATE.md §40 does not document {key!r}"
    for key in state["reboot"]:
        assert key in section, key
    for cat in us.CATEGORY_ORDER:
        assert cat in section and cat in UPDATES
    for label in us.ORIGIN_LABELS:
        assert label in section, label
    assert '"schema": 1' in UPDATES


def test_the_documented_timer_values_are_the_shipped_ones() -> None:
    timer = (CORE / "root/usr/lib/systemd/system/lindos-update-refresh.timer").read_text(encoding="utf-8")
    for setting in ("OnBootSec=5min", "OnCalendar=*-*-* 06,18:00:00", "RandomizedDelaySec=2h", "AccuracySec=10min", "Persistent=true"):
        assert setting in timer
    assert "OnBootSec=5min" in SPEC and "06,18:00" in SPEC and "RandomizedDelaySec=2h" in SPEC and "Persistent=true" in SPEC
    assert "06,18:00" in UPDATES and "RandomizedDelaySec=2h" in UPDATES


def test_the_documented_limits_are_the_coded_ones() -> None:
    src = (CORE / "root/usr/libexec/lindos/lindos-helper").read_text(encoding="utf-8")
    assert "MIN_FREE_ROOT_BYTES = 1024 * 1024 * 1024" in src and "MIN_FREE_BOOT_BYTES = 200 * 1024 * 1024" in src
    assert "1 GiB" in UPDATES and "200 MiB" in UPDATES and "1 GiB" in SPEC and "200 MiB" in SPEC
    from lindos import updatestate as us
    assert us.CLEANUP_MAX_PACKAGES == 30 and "30 packages" in UPDATES and "30 packages" in SPEC
    assert us.STALE_AFTER_SECONDS == 3 * 24 * 3600 and "three days" in UPDATES


def test_the_files_named_in_the_docs_exist() -> None:
    for text in (UPDATES, SPEC, RELEASING):
        for path in re.findall(r"`((?:build|packages|docs|\.github)/[A-Za-z0-9_./*{},-]+)`", text):
            if any(ch in path for ch in "*{},"):
                continue
            if path.endswith("/"):
                assert (REPO / path).is_dir(), path
            else:
                assert (REPO / path).exists(), path


def test_relative_links_resolve() -> None:
    for name, text in DOCS.items():
        base = (REPO / name).parent
        for target in re.findall(r"\]\((?!https?://|#)([^)#]+)", text):
            assert (base / target).exists(), f"{name}: broken link {target}"


# --- RELEASING.md matches the repository ----------------------------------------------------------------------------
def test_releasing_names_the_secrets_variables_and_environment_ci_uses() -> None:
    for token in ("LINDOS_APT_SIGNING_KEY", "LINDOS_APT_SIGNING_KEY_PASSPHRASE", "LINDOS_PAGES_ENABLED", "apt-publish",
                  "LINDOS_APT_REPO_REQUIRE=1 make iso", "gh secret set LINDOS_APT_SIGNING_KEY --env apt-publish",
                  "gh variable set LINDOS_PAGES_ENABLED", "git tag -a v", "make-signing-key.sh", "check-archive-keyring.sh"):
        assert token in RELEASING, token
    for token in ("secrets.LINDOS_APT_SIGNING_KEY", "secrets.LINDOS_APT_SIGNING_KEY_PASSPHRASE", "vars.LINDOS_PAGES_ENABLED",
                  "environment: apt-publish"):
        assert token in CI, token


def test_releasing_dispatches_the_e2e_job_with_a_real_option() -> None:
    options = re.search(r"repo_e2e:.*?options:\n((?:\s+- .*\n)+)", CI, re.S).group(1)
    values = [line.strip()[2:].strip('"') for line in options.splitlines()]
    for option in re.findall(r"-f repo_e2e=([a-z-]+)", RELEASING):
        assert option in values, option


def test_releasing_commits_the_files_the_package_really_has() -> None:
    for path in ("packages/lindos-archive-keyring/root/usr/share/keyrings/lindos-archive-keyring.gpg",
                 "packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint",
                 "packages/lindos-archive-keyring/root/etc/apt/sources.list.d/lindos.sources"):
        assert path in RELEASING and (REPO / path).is_file(), path


def test_releasing_scripts_exist_and_every_bash_command_in_it_is_a_real_script() -> None:
    for script in re.findall(r"bash (build/[A-Za-z0-9_./-]+\.sh)", RELEASING):
        assert (REPO / script).is_file(), script


def test_releasing_has_the_ordered_owner_steps() -> None:
    headings = re.findall(r"^## (\d+)\. ", RELEASING, re.M)
    assert headings[:9] == [str(i) for i in range(1, 10)]
    for word in ("domain", "signing key", "Pages", "tagged release", "clean machine", "Release images"):
        assert word.lower() in RELEASING.lower()
    assert "never" in RELEASING.lower() and "offline" in RELEASING.lower()


def test_releasing_states_what_ci_never_does() -> None:
    section = RELEASING.split("## What CI will never do")[1].split("## When something fails")[0]
    for claim in ("throw-away", "release-repo", "fingerprint", "Pages", "VERSION"):
        assert claim in section, claim
