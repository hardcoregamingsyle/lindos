"""Tests for the transfer report: JSON schema, next-steps, HTML page, save/load
(SPEC-WINDOWS §29.2, §29.5, §27.3)."""
from __future__ import annotations

import sys
from pathlib import Path

from lindos_transfer.report import (ItemResult, build_report, format_text, load_last_report,
                                    next_steps, report_html, save_report)


def _plan(**overrides):
    plan = {"id": "20260926-103000-ab12", "source": {"computer": "DESKTOP-1", "windows": "Windows 11 Pro",
           "hibernated": False}, "user": "alice", "dest_home": "/home/alice",
           "items": [], "apps": [], "options": {"firefox_passwords": False}, "skipped": [], "warnings": []}
    plan.update(overrides)
    return plan


def test_item_result_from_stats_marks_partial_on_errors() -> None:
    from lindos_transfer.copyengine import CopyStats

    stats = CopyStats(files=1, bytes=5, errors=[{"path": "x", "reason": "boom"}])
    item = {"id": "documents", "category": "documents", "label": "Documents", "dest": "/home/alice/Documents"}
    res = ItemResult.from_stats(item, stats)
    assert res.status == "partial"
    stats_ok = CopyStats(files=1, bytes=5)
    res_ok = ItemResult.from_stats(item, stats_ok)
    assert res_ok.status == "done"


def test_item_result_failed_and_skipped_helpers() -> None:
    item = {"id": "wifi", "category": "wifi", "label": "Wi-Fi", "dest": "/etc/NetworkManager/system-connections"}
    failed = ItemResult.failed(item, "boom")
    assert failed.status == "failed" and failed.errors[0]["reason"] == "boom"
    skipped = ItemResult.skipped_item(item, "nothing to do")
    assert skipped.status == "skipped" and skipped.notes == ["nothing to do"]


def test_item_result_as_dict_truncates_skipped_list() -> None:
    from lindos_transfer.report import MAX_LISTED

    res = ItemResult(id="x", category="documents", label="X",
                     skipped=[{"path": str(i), "reason": "r"} for i in range(MAX_LISTED + 5)])
    d = res.as_dict()
    assert d["skipped_count"] == MAX_LISTED + 5
    assert len(d["skipped"]) == MAX_LISTED


# --------------------------------------------------------------------------- #
# next_steps(): the honest follow-ups
# --------------------------------------------------------------------------- #
def test_next_steps_mentions_onedrive_online_only_files() -> None:
    plan = _plan(skipped=[{"path": "x", "reason": "OneDrive online-only file (not on this disk)"}])
    steps = next_steps(plan, [])
    assert any("OneDrive" in s for s in steps)


def test_next_steps_bookmarks_and_firefox_profile() -> None:
    plan = _plan()
    results = [ItemResult(id="bookmarks:chrome:Default", category="bookmarks", label="Chrome bookmarks",
                          status="done", files=1),
              ItemResult(id="firefox:x", category="firefox", label="Firefox", status="done", files=1)]
    steps = next_steps(plan, results)
    assert any("Bookmarks - <browser>.html" in s or "Bookmarks >" in s for s in steps)
    assert any("windows-import" in s for s in steps)


def test_next_steps_suggests_firefox_passwords_flag_when_not_used() -> None:
    plan = _plan(options={"firefox_passwords": False})
    results = [ItemResult(id="firefox:x", category="firefox", label="Firefox", status="done", files=1)]
    steps = next_steps(plan, results)
    assert any("--firefox-passwords" in s for s in steps)


def test_next_steps_steam_and_wifi_and_fonts() -> None:
    plan = _plan()
    results = [
        ItemResult(id="steam:620", category="steam-games", label="Portal 2", status="done", files=10),
        ItemResult(id="wifi", category="wifi", label="Wi-Fi", status="done", files=2),
        ItemResult(id="fonts", category="fonts", label="Fonts", status="done", files=1),
    ]
    steps = next_steps(plan, results)
    assert any("Force" in s or "Compatibility" in s for s in steps)
    assert any("Wi-Fi networks were added" in s for s in steps)
    assert any("Windows' built-in fonts" in s for s in steps)


def test_next_steps_hibernated_source_advises_restart() -> None:
    plan = _plan(source={"computer": "X", "windows": "Windows 11", "hibernated": True})
    steps = next_steps(plan, [])
    assert any("hibernated" in s for s in steps)


def test_next_steps_apps_mentions_install_apps_command() -> None:
    plan = _plan(apps=[{"windows_name": "VLC"}])
    steps = next_steps(plan, [])
    assert any("install-apps" in s for s in steps)


def test_next_steps_wifi_password_cleanup_reminder_for_bundle() -> None:
    plan = _plan(source={"computer": "X", "windows": "W", "hibernated": False, "type": "bundle"})
    plan["source"]["type"] = "bundle"
    results = [ItemResult(id="wifi", category="wifi", label="Wi-Fi", status="done", files=1,
                          notes=["Wi-Fi passwords came from your Windows export."])]
    steps = next_steps(plan, results)
    assert any("Delete the 'wifi' folder" in s for s in steps)


# --------------------------------------------------------------------------- #
# build_report() / save_report() / load_last_report()
# --------------------------------------------------------------------------- #
def test_build_report_totals_and_never_transferred_list() -> None:
    plan = _plan(items=[{"id": "documents", "category": "documents", "label": "Documents", "src": "x",
                        "dest": "y", "files": 1, "bytes": 10, "selected": True, "notes": []}])
    results = [ItemResult(id="documents", category="documents", label="Documents", files=1, bytes=10,
                          errors=[{"path": "x", "reason": "boom"}])]
    report = build_report(plan, results, started=0.0, finished=60.0)
    assert report["totals"] == {"files": 1, "bytes": 10, "skipped": 0, "errors": 1}
    assert report["schema"] == 1
    assert "SAM" in " ".join(report["never_transferred"]) or "security databases" in " ".join(
        report["never_transferred"])


def test_save_report_writes_json_and_html_and_last_report(tmp_path: Path) -> None:
    plan = _plan()
    report = build_report(plan, [], started=0.0, finished=1.0)
    paths = save_report(report, html_dir=tmp_path / "docs", state=tmp_path / "state")
    assert Path(paths["json"]).is_file()
    assert Path(paths["html"]).is_file()
    assert (tmp_path / "state" / "last-report.json").is_file()
    loaded = load_last_report(state=tmp_path / "state")
    assert loaded["plan_id"] == plan["id"]


def test_save_report_json_is_private_regardless_of_the_umask(tmp_path: Path) -> None:
    """The JSON report (under the state directory) names the source computer, the Windows user
    and every path that was copied, so it is written 0600/0700, not left to the umask (unlike the
    HTML page under ``html_dir``, which is an ordinary user-visible document -- see
    sec-transfer:state-dir-file-permissions)."""
    plan = _plan()
    report = build_report(plan, [], started=0.0, finished=1.0)
    paths = save_report(report, html_dir=tmp_path / "docs", state=tmp_path / "state")
    if not sys.platform.startswith("win"):
        json_path = Path(paths["json"])
        assert (json_path.stat().st_mode & 0o777) == 0o600
        assert (json_path.parent.stat().st_mode & 0o777) == 0o700
        last = tmp_path / "state" / "last-report.json"
        assert (last.stat().st_mode & 0o777) == 0o600


def test_save_report_dry_run_writes_no_html(tmp_path: Path) -> None:
    plan = _plan()
    report = build_report(plan, [], started=0.0, finished=1.0, dry_run=True)
    paths = save_report(report, html_dir=tmp_path / "docs", state=tmp_path / "state")
    assert "html" not in paths


def test_load_last_report_missing_returns_none(tmp_path: Path) -> None:
    assert load_last_report(state=tmp_path) is None


# --------------------------------------------------------------------------- #
# rendering: format_text / report_html escape user-controlled strings
# --------------------------------------------------------------------------- #
def test_format_text_includes_items_and_next_steps() -> None:
    plan = _plan(items=[{"id": "documents", "category": "documents", "label": "Documents", "src": "x",
                        "dest": "y", "files": 1, "bytes": 10, "selected": True, "notes": []}])
    results = [ItemResult(id="documents", category="documents", label="Documents", files=1, bytes=10)]
    report = build_report(plan, results, started=0.0, finished=1.0)
    text = format_text(report)
    assert "Documents" in text and "Next steps:" in text


def test_report_html_escapes_untrusted_file_names() -> None:
    plan = _plan()
    results = [ItemResult(id="documents", category="documents", label="Documents", files=1, bytes=10,
                          skipped=[{"path": "<script>alert(1)</script>", "reason": "x"}])]
    report = build_report(plan, results, started=0.0, finished=1.0)
    html = report_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
