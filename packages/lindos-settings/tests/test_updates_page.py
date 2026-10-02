"""Tests for the Updates page (SPEC-UPDATE §37): JSON-parsing helpers for lindos-update's
check/kernel-status/repo-status output and lindos-kernel's secureboot status (valid + malformed
+ missing-binary + non-zero-exit), the pure button-visibility/summary logic in
``lindos_settings.model``, the ``Backend`` adapter methods that call the CLIs defensively, and a
page construction/import-smoke test under the gi stub (no real display, no real CLIs).
"""
from __future__ import annotations

import os

import pytest

from lindos_settings import model
from lindos_settings.backend import Backend, CmdResult, HelperResult

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "lib", "lindos-settings", "main.py"))


# --------------------------------------------------------------------------- fixtures (hand-built
# per SPEC-UPDATE §36.2's binding UpdateStatus/PackageUpdate dataclasses; kernel-status/repo-status
# CLI shapes are not fully pinned down by the spec, so the normalisers below accept the same field
# names defensively -- see model.normalize_kernel_status / normalize_repo_status docstrings)

CHECK_VALID = {
    "refreshed_at": "2026-09-26T10:00:00",
    "lindos_updates": [
        {"name": "lindos-core", "installed": "1.0.0", "candidate": "1.0.1", "channel": "lindos"},
        {"name": "lindos-settings", "installed": "1.0.0", "candidate": "1.0.1", "channel": "lindos"},
    ],
    "system_updates": [
        {"name": "firefox", "installed": "128.0", "candidate": "129.0", "channel": "system"},
    ],
    "kernel_available": {"name": "linux-image-6.8.0-lindos", "installed": "6.8.0-1", "candidate": "6.8.0-2", "channel": "kernel"},
    "booted_kernel": "6.8.0-1-lindos",
    "booted_is_lindos_kernel": True,
    "reboot_required": False,
    "repo_configured": True,
    "repo_reachable": True,
}

KERNEL_STATUS_VALID = {
    "booted_kernel": "6.8.0-1-lindos",
    "booted_is_lindos_kernel": True,
    "kernel_available": {"name": "linux-image-6.8.0-lindos", "installed": "6.8.0-1", "candidate": "6.8.0-2", "channel": "kernel"},
    "reboot_required": False,
}

REPO_STATUS_VALID = {"configured": True, "reachable": True, "message": "reachable", "url": "https://packages.lindos.dev"}

SECUREBOOT_VALID = {
    "firmware": "uefi",
    "secure_boot": True,
    "mok": {"present": True, "enrolled": True, "priv": "/var/lib/shim-signed/mok/MOK.priv", "der": "/var/lib/shim-signed/mok/MOK.der"},
    "tools": {"sbsign": True, "sbverify": True, "mokutil": True},
    "kernels": [{"version": "6.8.0-1-lindos", "path": "/boot/vmlinuz-6.8.0-1-lindos", "signed": True}],
    "any_lindos_kernel_signed": True,
}


# --------------------------------------------------------------------------- normalize_update_status (check --json)
def test_normalize_update_status_valid():
    st = model.normalize_update_status(CHECK_VALID)
    assert st["error"] == ""
    assert [u["name"] for u in st["lindos_updates"]] == ["lindos-core", "lindos-settings"]
    assert st["lindos_updates"][0]["channel"] == "lindos"
    assert len(st["system_updates"]) == 1
    assert st["kernel_available"] == {"name": "linux-image-6.8.0-lindos", "installed": "6.8.0-1", "candidate": "6.8.0-2", "channel": "kernel"}
    assert st["booted_kernel"] == "6.8.0-1-lindos"
    assert st["booted_is_lindos_kernel"] is True
    assert st["reboot_required"] is False
    assert st["repo_configured"] is True
    assert st["repo_reachable"] is True


@pytest.mark.parametrize("bad", [None, [], "not a dict", 42, True])
def test_normalize_update_status_malformed_top_level(bad):
    st = model.normalize_update_status(bad)
    assert st["lindos_updates"] == []
    assert st["system_updates"] == []
    assert st["kernel_available"] is None
    assert st["booted_kernel"] == ""
    assert st["booted_is_lindos_kernel"] is False
    assert st["repo_configured"] is False
    assert st["repo_reachable"] is None
    assert st["error"] == ""


def test_normalize_update_status_malformed_inner_shapes_never_raise():
    st = model.normalize_update_status({
        "lindos_updates": "not a list",
        "system_updates": [1, 2, "x", {"no": "name"}, {"name": "ok-pkg"}],
        "kernel_available": "also not a dict",
        "booted_is_lindos_kernel": "yes",  # non-bool: coerced, never raises
        "repo_reachable": "maybe",  # not True/False -> stays unknown (None)
    })
    assert st["lindos_updates"] == []
    # entries without a usable "name" are dropped; a bare "ok-pkg" with no candidate still parses
    assert [u["name"] for u in st["system_updates"]] == ["ok-pkg"]
    assert st["kernel_available"] is None
    assert st["booted_is_lindos_kernel"] is True  # bool("yes") -- honest, documented coercion
    assert st["repo_reachable"] is None


def test_normalize_update_status_missing_binary():
    st = model.normalize_update_status({"_missing": True})
    assert st["error"] == "lindos-update is not installed"
    assert st["lindos_updates"] == []


def test_normalize_update_status_error_from_nonzero_exit():
    st = model.normalize_update_status({"_error": "lindos-update exited 1"})
    assert st["error"] == "lindos-update exited 1"


def test_normalize_package_update_unknown_channel_falls_back_to_lindos():
    pu = model.normalize_package_update({"name": "x", "channel": "weird"})
    assert pu["channel"] == "weird" or pu["channel"] == "lindos"
    # an empty/missing channel always falls back to "lindos" (never crashes on odd values)
    pu2 = model.normalize_package_update({"name": "x"})
    assert pu2["channel"] == "lindos"
    assert model.normalize_package_update("not a dict") == {"name": "", "installed": "", "candidate": "", "channel": "lindos"}


# --------------------------------------------------------------------------- normalize_kernel_status (kernel-status --json)
def test_normalize_kernel_status_valid():
    k = model.normalize_kernel_status(KERNEL_STATUS_VALID)
    assert k["booted_kernel"] == "6.8.0-1-lindos"
    assert k["booted_is_lindos_kernel"] is True
    assert k["kernel_available"]["candidate"] == "6.8.0-2"
    assert k["reboot_required"] is False
    assert k["error"] == ""


@pytest.mark.parametrize("bad", [None, [], "nope", 3.14])
def test_normalize_kernel_status_malformed(bad):
    k = model.normalize_kernel_status(bad)
    assert k["booted_kernel"] == ""
    assert k["kernel_available"] is None
    assert k["error"] == ""


def test_normalize_kernel_status_missing_binary_and_error():
    assert model.normalize_kernel_status({"_missing": True})["error"] == "lindos-update is not installed"
    assert model.normalize_kernel_status({"_error": "boom"})["error"] == "boom"


# --------------------------------------------------------------------------- normalize_repo_status (repo status --json)
def test_normalize_repo_status_valid():
    r = model.normalize_repo_status(REPO_STATUS_VALID)
    assert r == {"configured": True, "reachable": True, "message": "reachable", "url": "https://packages.lindos.dev"}


def test_normalize_repo_status_accepts_update_status_field_spelling():
    r = model.normalize_repo_status({"repo_configured": True, "repo_reachable": False})
    assert r["configured"] is True
    assert r["reachable"] is False


@pytest.mark.parametrize("bad", [None, [], "nope", 1])
def test_normalize_repo_status_malformed(bad):
    r = model.normalize_repo_status(bad)
    assert r == {"configured": False, "reachable": None, "message": "", "url": ""}


def test_normalize_repo_status_missing_and_error():
    assert model.normalize_repo_status({"_missing": True})["message"] == "lindos-update is not installed"
    assert model.normalize_repo_status({"_error": "network unreachable"})["message"] == "network unreachable"


# --------------------------------------------------------------------------- normalize_secureboot_status
def test_normalize_secureboot_status_valid():
    sb = model.normalize_secureboot_status(SECUREBOOT_VALID)
    assert sb["firmware"] == "uefi"
    assert sb["secure_boot"] is True
    assert sb["mok_present"] is True
    assert sb["mok_enrolled"] is True
    assert sb["kernels"] == [{"version": "6.8.0-1-lindos", "path": "/boot/vmlinuz-6.8.0-1-lindos", "signed": True}]
    assert sb["any_lindos_kernel_signed"] is True
    assert sb["error"] == ""


def test_normalize_secureboot_status_bios_and_unknown_values():
    sb = model.normalize_secureboot_status({"firmware": "bios", "secure_boot": False})
    assert sb["firmware"] == "bios"
    assert sb["secure_boot"] is False
    sb2 = model.normalize_secureboot_status({"secure_boot": "true"})  # non-bool string -> unknown
    assert sb2["secure_boot"] is None


@pytest.mark.parametrize("bad", [None, [], "nope", 7])
def test_normalize_secureboot_status_malformed(bad):
    sb = model.normalize_secureboot_status(bad)
    assert sb["firmware"] == "unknown"
    assert sb["secure_boot"] is None
    assert sb["mok_present"] is False
    assert sb["kernels"] == []
    assert sb["any_lindos_kernel_signed"] is None
    assert sb["error"] == ""


def test_normalize_secureboot_status_missing_and_error():
    assert model.normalize_secureboot_status({"_missing": True})["error"] == "lindos-kernel is not installed"
    assert model.normalize_secureboot_status({"_error": "sbverify not found"})["error"] == "sbverify not found"


# --------------------------------------------------------------------------- pure logic: payload / visibility / summaries
def test_lindos_update_payload_builds_exact_name_equals_version_and_skips_kernel():
    status = model.normalize_update_status(CHECK_VALID)
    payload = model.lindos_update_payload(status)
    assert payload == ["lindos-core=1.0.1", "lindos-settings=1.0.1"]
    assert not any("linux-image" in p for p in payload)  # kernel never rides along with "Update now"


def test_lindos_update_payload_empty_when_no_updates():
    status = model.normalize_update_status({})
    assert model.lindos_update_payload(status) == []


def test_kernel_update_payload_present_and_absent():
    status = model.normalize_update_status(CHECK_VALID)
    assert model.kernel_update_payload(status) == ["linux-image-6.8.0-lindos=6.8.0-2"]
    kernel = model.normalize_kernel_status(KERNEL_STATUS_VALID)
    assert model.kernel_update_payload(kernel) == ["linux-image-6.8.0-lindos=6.8.0-2"]
    assert model.kernel_update_payload(model.normalize_update_status({})) == []
    assert model.kernel_update_payload({"kernel_available": {"name": "x"}}) == []  # no candidate -> nothing to apply


def test_update_button_visibility_logic():
    """The page shows "Update now" iff there is an exact package=version list to install."""
    no_updates = model.normalize_update_status({})
    has_updates = model.normalize_update_status(CHECK_VALID)
    assert bool(model.lindos_update_payload(no_updates)) is False
    assert bool(model.lindos_update_payload(has_updates)) is True


def test_kernel_apply_button_visibility_logic():
    no_kernel = model.normalize_kernel_status({})
    has_kernel = model.normalize_kernel_status(KERNEL_STATUS_VALID)
    assert bool(model.kernel_update_payload(no_kernel)) is False
    assert bool(model.kernel_update_payload(has_kernel)) is True


def test_needs_sideload_note_true_when_not_configured():
    assert model.needs_sideload_note(model.normalize_update_status({})) is True
    assert model.needs_sideload_note(model.normalize_update_status(CHECK_VALID)) is False


def test_sideload_note_text_mentions_unreachable_when_configured_but_down():
    configured_down = model.normalize_update_status(dict(CHECK_VALID, repo_reachable=False))
    text = model.sideload_note_text(configured_down)
    assert "could not be reached" in text
    not_configured = model.normalize_update_status({})
    assert "No Lindos update channel is configured yet" in model.sideload_note_text(not_configured)


def test_update_status_summary_variants():
    assert "up to date" in model.update_status_summary(model.normalize_update_status({}))
    summary = model.update_status_summary(model.normalize_update_status(CHECK_VALID))
    assert "2 updates available" in summary
    assert "lindos-core" in summary
    err = model.update_status_summary(model.normalize_update_status({"_missing": True}))
    assert err == "lindos-update is not installed"


def test_kernel_status_summary_and_secureboot_summary():
    summary = model.kernel_status_summary(model.normalize_kernel_status(KERNEL_STATUS_VALID))
    assert "Booted: 6.8.0-1-lindos" in summary
    assert "6.8.0-1 → Available: 6.8.0-2" in summary
    no_kernel_summary = model.kernel_status_summary(model.normalize_kernel_status({}))
    assert "No newer Lindos kernel available" in no_kernel_summary

    assert "signed" in model.secureboot_summary(model.normalize_secureboot_status(SECUREBOOT_VALID))
    assert "off" in model.secureboot_summary(model.normalize_secureboot_status({"firmware": "uefi", "secure_boot": False})).lower()
    assert "not applicable" in model.secureboot_summary(model.normalize_secureboot_status({"firmware": "bios"}))
    assert "NOT signed" in model.secureboot_summary(model.normalize_secureboot_status({"firmware": "uefi", "secure_boot": True, "any_lindos_kernel_signed": False}))


# --------------------------------------------------------------------------- Backend adapter (subprocess plumbing)
class _Which:
    def __init__(self, present):
        self.present = set(present)

    def __call__(self, name):
        return ("/usr/bin/" + name) if name in self.present else None


def _backend_with(monkeypatch, present=(), run_result=None):
    b = Backend()
    monkeypatch.setattr(b, "which", _Which(present))
    if run_result is not None:
        monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: run_result)
    return b


def test_update_check_missing_binary(monkeypatch):
    b = _backend_with(monkeypatch, present=())
    st = b.update_check()
    assert st["error"] == "lindos-update is not installed"


def test_update_check_nonzero_exit(monkeypatch):
    b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(1, "", "boom"))
    st = b.update_check()
    assert st["error"] == "boom"


def test_update_check_bad_json(monkeypatch):
    b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(0, "{not json", ""))
    st = b.update_check()
    assert "could not read" in st["error"]


def test_update_check_valid(monkeypatch):
    import json as _json
    b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(0, _json.dumps(CHECK_VALID), ""))
    st = b.update_check()
    assert st["error"] == ""
    assert len(st["lindos_updates"]) == 2


CHECK_UP_TO_DATE = {**CHECK_VALID, "lindos_updates": [], "system_updates": [], "kernel_available": None}


def test_update_check_exit_3_nothing_to_do_is_a_normal_answer(monkeypatch):
    """lindos-update exits 3 (EXIT_NOTHING) after printing a complete document when there is nothing to
    update - the normal steady state.  It must read as 'up to date', not as the raw JSON in an error."""
    import json as _json
    b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(3, _json.dumps(CHECK_UP_TO_DATE), ""))
    st = b.update_check()
    assert st["error"] == ""
    assert st["repo_configured"] is True and st["booted_kernel"] == "6.8.0-1-lindos"
    assert "up to date" in model.update_status_summary(st)
    assert model.needs_sideload_note(st) is False


def test_update_check_a_failure_with_the_other_exit_codes_stays_an_error(monkeypatch):
    import json as _json
    for code in (1, 2):
        b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(code, _json.dumps(CHECK_UP_TO_DATE), "usage: lindos-update"))
        assert b.update_check()["error"] != ""
    b = _backend_with(monkeypatch, present=("lindos-update",), run_result=CmdResult(3, "", ""))
    assert "exited 3" in b.update_check()["error"]


def test_update_kernel_and_repo_status_accept_the_nothing_to_do_exit(monkeypatch):
    import json as _json
    b = _backend_with(monkeypatch, present=("lindos-update",))
    not_configured = {"configured": False, "url": None, "reachable": None, "message": "no Lindos apt repository is configured yet"}
    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(3, _json.dumps(not_configured), ""))
    repo = b.update_repo_status()
    assert repo["configured"] is False and repo["message"] == "no Lindos apt repository is configured yet"
    assert b.update_kernel_status()["error"] == ""
    # configured but unreachable: exit 1 with a complete document (reachable=false), not a failure to run
    unreachable = {"configured": True, "url": "https://packages.lindos.dev", "reachable": False, "message": "timed out"}
    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(1, _json.dumps(unreachable), ""))
    repo = b.update_repo_status()
    assert repo["configured"] is True and repo["reachable"] is False and repo["message"] == "timed out"
    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(1, "", "boom"))
    assert b.update_repo_status()["message"] == "boom"


def test_update_kernel_status_and_repo_status_and_secureboot_status(monkeypatch):
    import json as _json
    b = _backend_with(monkeypatch, present=("lindos-update", "lindos-kernel"))

    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(0, _json.dumps(KERNEL_STATUS_VALID), ""))
    k = b.update_kernel_status()
    assert k["kernel_available"]["candidate"] == "6.8.0-2"

    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(0, _json.dumps(REPO_STATUS_VALID), ""))
    r = b.update_repo_status()
    assert r["configured"] is True

    monkeypatch.setattr(b, "run", lambda argv, timeout=20, **kw: CmdResult(0, _json.dumps(SECUREBOOT_VALID), ""))
    sb = b.secureboot_status()
    assert sb["any_lindos_kernel_signed"] is True


def test_secureboot_status_missing_binary(monkeypatch):
    b = _backend_with(monkeypatch, present=())
    sb = b.secureboot_status()
    assert sb["error"] == "lindos-kernel is not installed"


def test_update_refresh_calls_apt_get_update_helper_action(monkeypatch):
    b = Backend()
    calls = []
    monkeypatch.setattr(b, "run_privileged", lambda action, payload=None: calls.append((action, payload)) or HelperResult(True))
    res = b.update_refresh()
    assert res.ok
    assert calls == [("apt-get-update", {})]


def test_update_apply_builds_system_upgrade_payload(monkeypatch):
    b = Backend()
    calls = []
    monkeypatch.setattr(b, "run_privileged", lambda action, payload=None: calls.append((action, payload)) or HelperResult(True))
    b.update_apply(["lindos-core=1.0.1"], allow_kernel=False)
    assert calls[-1] == ("system-upgrade", {"packages": ["lindos-core=1.0.1"]})
    b.update_apply(["linux-image-6.8.0-lindos=6.8.0-2"], allow_kernel=True)
    assert calls[-1] == ("system-upgrade", {"packages": ["linux-image-6.8.0-lindos=6.8.0-2"], "allow_kernel": True})


def test_sideload_argv_is_non_interactive():
    argv = Backend.sideload_argv("/tmp/lindos-updates")
    assert argv == ["lindos-update", "sideload", "/tmp/lindos-updates", "--yes"]


# --------------------------------------------------------------------------- page registry wiring
def test_updates_page_registered_in_pages_json_and_model():
    assert "updates" in model.PAGE_ORDER
    assert "updates" in model.NATIVE_PAGES
    by_id = model.pages_by_id(model.builtin_pages())
    page = by_id["updates"]
    assert page.kind == "native"
    assert page.title == "Updates"
    for kw in ("update", "upgrade", "apt", "package", "manager", "mintupdate", "lindos-update", "kernel"):
        assert kw in page.keywords, kw


def test_main_no_longer_aliases_updates_to_update():
    """"updates" used to be a CLI alias for the "update" (Update & Recovery) page; now that it is
    a real, distinct page id (SPEC-UPDATE §37) the alias must be gone, or `lindos-settings
    updates` would silently open the wrong page."""
    with open(MAIN_PY, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert '"updates": "update"' not in src


# --------------------------------------------------------------------------- page construction / import-smoke (gi stub)
class _FakeApp:
    def __init__(self, backend):
        self.backend = backend
        self.window = None

    def toast(self, text):  # noqa: ARG002
        pass

    def show_page(self, page_id):  # noqa: ARG002
        pass


def test_updates_page_imports_and_constructs_under_gi_stub():
    from lindos_settings import pages
    from lindos_settings.pages import updates as updates_mod

    assert pages.PAGE_CLASSES["updates"] == ("updates", "UpdatesPage")

    by_id = model.pages_by_id(model.builtin_pages())
    app = _FakeApp(Backend())
    page_obj = pages.build_page(app, by_id["updates"])
    assert isinstance(page_obj, updates_mod.UpdatesPage)
    assert page_obj.id == "updates"
    assert page_obj.PAGE_ID == "updates"
    # widgets referenced by the button-visibility logic exist
    assert page_obj.update_btn is not None
    assert page_obj.kernel_apply_btn is not None
    assert page_obj.sideload_section is not None


def test_updates_page_render_helpers_never_raise_for_any_status_shape():
    """Exercises the same render code paths the async refresh callbacks use, for a spread of
    status shapes (no updates, updates present, kernel update present, no repo configured, an
    honest error) -- the actual show/hide decision is asserted at the model layer above; this
    only guards against the render code itself throwing."""
    from lindos_settings import pages

    by_id = model.pages_by_id(model.builtin_pages())
    app = _FakeApp(Backend())
    page_obj = pages.build_page(app, by_id["updates"])

    for status_src in ({}, CHECK_VALID, {"_missing": True}, {"_error": "boom"}, dict(CHECK_VALID, repo_configured=False)):
        page_obj._status = model.normalize_update_status(status_src)
        page_obj._render_components()

    for kernel_src in ({}, KERNEL_STATUS_VALID, {"_missing": True}):
        page_obj._kernel = model.normalize_kernel_status(kernel_src)
        for sb_src in ({}, SECUREBOOT_VALID, {"_error": "no sbverify"}):
            page_obj._secureboot = model.normalize_secureboot_status(sb_src)
            page_obj._render_kernel()
