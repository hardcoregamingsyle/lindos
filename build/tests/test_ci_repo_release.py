"""The update jobs of .github/workflows/ci.yml (SPEC-UPDATE.md, docs/RELEASING.md): the TEST build with a
throw-away key, the real signed release, the Pages deploy and the opt-in repo e2e.

The properties that matter are structural and security-relevant, so they are asserted here:
the signing secret is visible to exactly one job; a throw-away key can never reach Pages; nothing is
deployed unless the fingerprint of the signing key is the committed one and the owner switched Pages on; a release
tag must be the version in VERSION. Text checks run everywhere; the YAML-level checks need PyYAML (skipped without it).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
E2E = REPO / "build" / "tools" / "repo-e2e.sh"

TEXT = CI_YML.read_text(encoding="utf-8")


def _job_text(name: str) -> str:
    """The raw text of one top-level job (from '  name:' to the next top-level job)."""
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", TEXT, re.M | re.S)
    assert m, f"job {name} not found"
    return m.group(0)


@pytest.fixture(scope="module")
def doc() -> Dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(TEXT)


def _steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(job.get("steps") or [])


def _runs(job: Dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(job))


# --- text level (no PyYAML needed) ------------------------------------------------------------------------------
def test_the_file_is_lf_and_has_the_update_jobs() -> None:
    assert "\r" not in TEXT
    for job in ("publish-repo", "release-repo", "deploy-pages", "repo-e2e"):
        assert re.search(rf"^  {job}:\n", TEXT, re.M), job


def test_the_signing_secret_is_visible_to_exactly_one_job() -> None:
    holders = [name for name in ("lint-test", "pytest-windows", "debs", "publish-repo", "release-repo", "deploy-pages",
                                 "repo-e2e", "kernel", "iso", "boot-test", "install-test", "install-test-offline", "menu-test")
               if "secrets.LINDOS_APT_SIGNING_KEY" in _job_text(name)]
    assert holders == ["release-repo"]
    assert "secrets.LINDOS_APT_SIGNING_KEY_PASSPHRASE" in _job_text("release-repo")


def test_the_test_build_uses_a_throw_away_key_and_cannot_deploy() -> None:
    job = _job_text("publish-repo")
    assert "--gen-key" in job and "--release" not in job
    assert "secrets." not in job and "environment:" not in job
    assert "pages:" not in job and "id-token" not in job and "deploy-pages" not in job and "upload-pages-artifact" not in job
    assert "TEST" in job and "never deployed" in job


def test_the_release_job_signs_with_the_archive_key_and_checks_the_fingerprint() -> None:
    job = _job_text("release-repo")
    assert "--release" in job and "--gen-key" not in job
    assert "environment: apt-publish" in job
    assert "startsWith(github.ref, 'refs/tags/v')" in job
    assert "--keep 3" in job and "--previous out/previous" in job         # the last three releases stay in the repository
    assert "--in out/previous" not in job                                 # ... as untrusted previous releases, never as the release itself
    assert "archive-key.fingerprint" in job and "README-CI-KEY.txt" in job    # the belt-and-braces guard
    assert "gh release" in job and "contents: write" in job
    assert "needs: debs" in job


def test_the_release_job_refuses_to_run_without_the_key() -> None:
    job = _job_text("release-repo")
    assert re.search(r'-z "\$\{LINDOS_APT_SIGNING_KEY\}"', job) and "exit 1" in job


def test_pages_is_only_ever_deployed_by_the_deploy_job_after_a_successful_release() -> None:
    deploy = _job_text("deploy-pages")
    assert "needs: release-repo" in deploy
    assert "vars.LINDOS_PAGES_ENABLED == 'true'" in deploy
    assert "name: github-pages" in deploy and "pages: write" in deploy and "id-token: write" in deploy
    assert TEXT.count("pages: write") == 1                                # no other job may deploy
    assert TEXT.count("actions/deploy-pages") == 1
    release = _job_text("release-repo")
    assert re.search(r"- name: Upload Pages artifact\n\s+if: \$\{\{ vars\.LINDOS_PAGES_ENABLED == 'true' \}\}", release)


def test_the_signing_job_holds_no_pages_permission_and_does_not_call_the_pages_api() -> None:
    """actions/configure-pages calls the Pages API, which the job's contents: write token may not: the step failed the
    job after the release was created and deploy-pages was skipped. Its outputs are unused, and adding pages: write to
    the job that holds the signing key is not an option, so the step is gone (deploying is deploy-pages' job)."""
    release = _job_text("release-repo")
    assert not re.search(r"uses:\s*actions/configure-pages", TEXT)
    assert "pages:" not in release and "id-token" not in release
    assert "actions/upload-pages-artifact" in release                    # that one needs no Pages permission


def test_a_release_tag_must_be_the_version_in_the_version_file() -> None:
    job = _job_text("debs")
    assert "startsWith(github.ref, 'refs/tags/v')" in job and "VERSION" in job
    assert "GITHUB_REF_NAME" in job and "exit 1" in job


def test_repo_e2e_is_opt_in_runs_in_a_container_and_has_no_secrets() -> None:
    job = _job_text("repo-e2e")
    assert "workflow_dispatch" in job and "inputs.repo_e2e != 'off'" in job
    assert "image: ubuntu:24.04" in job
    assert "build/tools/repo-e2e.sh" in job
    assert "secrets." not in job and "environment:" not in job
    assert "out/e2e/logs" in job                                          # only logs are uploaded, never the throw-away key


def test_the_e2e_script_keeps_its_key_outside_the_uploaded_directory_and_is_linux_only() -> None:
    text = E2E.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text and "\r" not in text
    assert 'KEYHOME="$(mktemp -d)"' in text and "out/e2e" in text
    assert '[ "$(id -u)" -eq 0 ]' in text                                 # refuses to install packages on a developer machine
    for step in ("--release", "LINDOS_APT_SIGNING_KEY", "lindos-update check --refresh", "lindos-update apply",
                 "apt-full-upgrade", "lindos-update history", "python3 -m http.server"):
        assert step in text, step


def test_the_kernel_debs_are_not_published_to_the_repository() -> None:
    for name in ("publish-repo", "release-repo"):
        assert "lindos-kernel-debs" not in _job_text(name)
        assert "--kernel-in" not in _job_text(name)


# --- YAML level ----------------------------------------------------------------------------------------------------
def test_yaml_jobs_and_needs(doc) -> None:
    jobs = doc["jobs"]
    assert jobs["publish-repo"]["needs"] == "debs" and jobs["release-repo"]["needs"] == "debs"
    assert jobs["deploy-pages"]["needs"] == "release-repo" and jobs["repo-e2e"]["needs"] == "lint-test"
    assert jobs["release-repo"]["environment"] == "apt-publish"
    assert jobs["deploy-pages"]["environment"]["name"] == "github-pages"
    for name in ("lint-test", "pytest-windows", "debs", "kernel", "iso"):     # the older jobs are still there
        assert name in jobs


def test_yaml_event_gating(doc) -> None:
    jobs = doc["jobs"]
    assert "workflow_dispatch" in jobs["publish-repo"]["if"] and "refs/tags" not in jobs["publish-repo"]["if"]
    assert jobs["release-repo"]["if"].strip("${} ") == "startsWith(github.ref, 'refs/tags/v')"
    assert "workflow_dispatch" in jobs["repo-e2e"]["if"]
    assert jobs["deploy-pages"]["if"].strip("${} ") == "vars.LINDOS_PAGES_ENABLED == 'true'"


def test_yaml_permissions_are_minimal(doc) -> None:
    jobs = doc["jobs"]
    assert doc["permissions"] == {"contents": "read"}
    assert jobs["publish-repo"]["permissions"] == {"contents": "read"}
    assert jobs["release-repo"]["permissions"] == {"contents": "write"}
    assert jobs["deploy-pages"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert "permissions" not in jobs["repo-e2e"] or jobs["repo-e2e"]["permissions"] in ({}, {"contents": "read"})


def test_yaml_release_environment_carries_the_secrets_only_as_environment_variables(doc) -> None:
    env = doc["jobs"]["release-repo"]["env"]
    assert env["LINDOS_APT_SIGNING_KEY"] == "${{ secrets.LINDOS_APT_SIGNING_KEY }}"
    assert env["LINDOS_APT_SIGNING_KEY_PASSPHRASE"] == "${{ secrets.LINDOS_APT_SIGNING_KEY_PASSPHRASE }}"
    for step in _steps(doc["jobs"]["release-repo"]):
        assert "secrets." not in str(step.get("run", ""))                # never interpolated into a command line
        assert "secrets." not in str(step.get("with", ""))


def test_yaml_the_publish_script_call_in_the_release_job(doc) -> None:
    runs = _runs(doc["jobs"]["release-repo"])
    call = next(line for line in runs.splitlines() if "publish-apt-repo.sh" in line)
    assert "--release" in call and "--in out/debs" in call and "--previous out/previous" in call and "--out out/apt-repo" in call
    assert "--in out/previous" not in call


def test_yaml_dispatch_inputs(doc) -> None:
    inputs = doc[True]["workflow_dispatch"]["inputs"]                    # 'on' parses as the boolean True in YAML 1.1
    assert inputs["repo_e2e"]["type"] == "choice" and inputs["repo_e2e"]["default"] == "off"
    assert inputs["repo_e2e"]["options"] == ["off", "basic", "with-full-upgrade"]
    for old in ("build_iso", "build_kernel", "install_test", "menu_test"):
        assert old in inputs


def test_yaml_pages_steps_are_conditional(doc) -> None:
    seen = 0
    for step in _steps(doc["jobs"]["release-repo"]):
        uses = str(step.get("uses", ""))
        assert "configure-pages" not in uses
        if "upload-pages-artifact" in uses:
            seen += 1
            assert step["if"].strip("${} ") == "vars.LINDOS_PAGES_ENABLED == 'true'"
    assert seen == 1


def _step_index(doc, needle: str) -> int:
    names = [str(s.get("name", "")) for s in _steps(doc["jobs"]["release-repo"])]
    hits = [i for i, n in enumerate(names) if needle in n]
    assert len(hits) == 1, (needle, names)
    return hits[0]


def test_yaml_previous_releases_are_verified_before_anything_is_signed(doc) -> None:
    """The regression: every '*.deb' of earlier releases was downloaded as is and signed with the archive key."""
    steps = _steps(doc["jobs"]["release-repo"])
    fetch = steps[_step_index(doc, "Fetch the previous releases")]
    assert "fetch-previous-releases.sh" in fetch["run"]
    assert '--tag "${GITHUB_REF_NAME}"' in fetch["run"]
    assert "packages/lindos-archive-keyring/root/usr/share/keyrings/lindos-archive-keyring.gpg" in fetch["run"]
    runs = _runs(doc["jobs"]["release-repo"])
    assert "--pattern '*.deb'" not in runs and "gh release download" not in runs      # no raw download of mutable assets
    # the tools it needs (gpgv) are installed first; it runs before the repository is built and signed
    assert (_step_index(doc, "Install apt-ftparchive") < _step_index(doc, "Fetch the previous releases")
            < _step_index(doc, "build/publish-apt-repo.sh --release"))


def test_yaml_the_signed_metadata_is_attached_only_after_the_guard(doc) -> None:
    steps = _steps(doc["jobs"]["release-repo"])
    attach = steps[_step_index(doc, "Attach the signed repository metadata")]
    assert "gh release upload" in attach["run"] and "--clobber" in attach["run"]
    for name in ("out/apt-repo/Release ", "out/apt-repo/Release.gpg", "out/apt-repo/Packages"):
        assert name in attach["run"]
    assert ".deb" not in attach["run"]                                          # the debs go up early (rollback copies), the metadata late
    assert _step_index(doc, "Guard - only the committed archive key") < _step_index(doc, "Attach the signed repository metadata")


def test_yaml_the_guard_also_checks_the_committed_keyring_and_source(doc) -> None:
    guard = _steps(doc["jobs"]["release-repo"])[_step_index(doc, "Guard - only the committed archive key")]["run"]
    assert "check-archive-keyring.sh packages/lindos-archive-keyring/root" in guard
    assert "grep -c '^pub:'" in guard and "exactly one is allowed" in guard      # one primary key in the committed keyring


def test_yaml_release_uploads_the_debs_only_on_tags(doc) -> None:
    step = next(s for s in _steps(doc["jobs"]["release-repo"]) if "Attach the .debs" in s.get("name", ""))
    assert "refs/tags/v" in step["if"] and "gh release upload" in step["run"] and "--clobber" in step["run"]


def test_yaml_still_parses_the_older_wiring(doc) -> None:
    jobs = doc["jobs"]
    assert jobs["iso"]["needs"] == ["debs", "kernel"]
    assert jobs["kernel"]["needs"] == "lint-test"
