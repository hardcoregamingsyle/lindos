"""Transfer report: per-item results, next steps, JSON + a readable HTML page (SPEC-WINDOWS §29.2, §29.5).

The JSON report lives in ``~/.local/state/lindos/transfer/<plan-id>/report.json`` (plus
``last-report.json``, read by ``lindos-transfer report``); a plain HTML copy for the user is
written to ``~/Documents/Transferred from Windows/``.  Secrets never reach a report: Wi-Fi items
carry network names only, and deny-listed paths are listed only as "never read".
"""

from __future__ import annotations

import html
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, secrets, state_dir
from .copyengine import CopyStats, human_size, open_private

__all__ = [
    "ItemResult",
    "REPORT_SCHEMA",
    "MAX_LISTED",
    "build_report",
    "next_steps",
    "save_report",
    "load_last_report",
    "report_html",
    "format_text",
]

REPORT_SCHEMA = 1
#: Longest list of skipped files kept in a report (the rest is counted).
MAX_LISTED = 5000

REASON_ONEDRIVE = "OneDrive online-only file (not on this disk)"


@dataclass
class ItemResult:
    """Outcome of one plan item."""

    id: str
    category: str
    label: str
    status: str = "done"              # done | partial | skipped | failed | dry-run
    files: int = 0
    bytes: int = 0
    identical: int = 0
    renamed: int = 0
    skipped: List[Dict[str, str]] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    dest: str = ""
    notes: List[str] = field(default_factory=list)

    @classmethod
    def from_stats(cls, item: Dict[str, Any], stats: CopyStats, *, dest: Optional[str] = None,
                   notes: Optional[List[str]] = None) -> "ItemResult":
        res = cls(id=str(item["id"]), category=str(item["category"]), label=str(item.get("label") or ""),
                  files=stats.files, bytes=stats.bytes, identical=stats.identical, renamed=stats.renamed,
                  skipped=list(stats.skipped), errors=list(stats.errors),
                  dest=dest if dest is not None else str(item.get("dest") or ""), notes=list(notes or []))
        res.status = "partial" if res.errors else "done"
        return res

    @classmethod
    def failed(cls, item: Dict[str, Any], message: str) -> "ItemResult":
        return cls(id=str(item["id"]), category=str(item["category"]), label=str(item.get("label") or ""),
                   status="failed", dest=str(item.get("dest") or ""),
                   errors=[{"path": str(item.get("src") or ""), "reason": message}])

    @classmethod
    def skipped_item(cls, item: Dict[str, Any], message: str) -> "ItemResult":
        return cls(id=str(item["id"]), category=str(item["category"]), label=str(item.get("label") or ""),
                   status="skipped", dest=str(item.get("dest") or ""), notes=[message])

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["skipped_count"] = len(self.skipped)
        out["error_count"] = len(self.errors)
        out["skipped"] = self.skipped[:MAX_LISTED]
        return out


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #
def next_steps(plan: Dict[str, Any], results: List[ItemResult]) -> List[str]:
    """Plain-language follow-ups for a Windows user (what did not move and why, what to do next)."""
    steps: List[str] = []
    cats = {r.category for r in results if r.status in ("done", "partial", "dry-run")}
    online_only = sum(1 for r in results for s in r.skipped if s.get("reason") == REASON_ONEDRIVE)
    online_only += sum(1 for s in plan.get("skipped") or [] if "online-only" in str(s.get("reason", "")))
    if online_only:
        steps.append(f"{online_only} file(s) are only stored in OneDrive (not on this disk), so they were not "
                     "copied. Open onedrive.live.com in your browser to get them, or download them in Windows "
                     "first and run the transfer again.")
    if "bookmarks" in cats or "firefox" in cats:
        steps.append("Bookmarks were saved as 'Bookmarks - <browser>.html' in 'Documents/Transferred from "
                     "Windows'. In your browser choose Bookmarks > Import bookmarks from HTML file.")
    if any(r.category == "firefox" and r.status in ("done", "partial") for r in results):
        steps.append("Your Firefox bookmarks and history are in a new Firefox profile called 'windows-import'. "
                     "Open Firefox, type about:profiles in the address bar and choose 'Launch profile in new "
                     "browser' (or 'Set as default profile').")
    steps.append("Browser passwords, cookies and payment cards do not transfer (Windows encrypts them). Turn on "
                 "sync in your browser (Google, Microsoft or Firefox account), or on Windows export your "
                 "passwords from the browser's password manager and import them on Lindos.")
    if not (plan.get("options") or {}).get("firefox_passwords") and "firefox" in cats:
        steps.append("Firefox saved passwords were not moved. Use Firefox Sync, or run the transfer again with "
                     "--firefox-passwords to move them as they are (still protected by your Primary Password).")
    if any(r.category == "steam-games" and r.status in ("done", "partial") for r in results):
        steps.append("Steam games: open Steam (close it before the transfer if it was running). For a game that "
                     "also has a Linux version, first right-click it > Properties > Compatibility > tick 'Force "
                     "the use of a specific Steam Play compatibility tool'. Then press Install/Update: Steam "
                     "discovers the existing files and only downloads what is missing.")
    if any(r.category == "wifi" and r.status in ("done", "partial") for r in results):
        steps.append("Wi-Fi networks were added. The first time you connect, Lindos asks for the password "
                     "unless it came from the transfer kit's password export.")
    if (plan.get("source") or {}).get("type") == "bundle" and any(
            "password" in n.lower() for r in results if r.category == "wifi" for n in r.notes):
        steps.append("Delete the 'wifi' folder from the transfer USB stick now: it contains your Wi-Fi passwords "
                     "in plain text (deleting from a USB stick does not securely erase it).")
    if any(r.category == "fonts" and r.files for r in results):
        steps.append("Your own fonts were installed for your account. Windows' built-in fonts are licensed for "
                     "Windows only and were not copied; Lindos includes compatible free fonts.")
    if (plan.get("source") or {}).get("hibernated"):
        steps.append("Windows was hibernated (Fast Startup), so some files may be slightly out of date. Restart "
                     "Windows (or run 'shutdown /s /t 0'), then run the transfer again to pick up changes - "
                     "files already copied are skipped.")
    if plan.get("apps"):
        steps.append("To reinstall your apps, run: lindos-transfer install-apps --plan <plan.json> (or use the "
                     "Apps page of the transfer window). Nothing is installed without your choice.")
    return steps


def build_report(plan: Dict[str, Any], results: List[ItemResult], *, started: float,
                 finished: Optional[float] = None, dry_run: bool = False,
                 app_results: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The report dictionary (schema 1)."""
    finished = time.time() if finished is None else finished
    listed: List[Dict[str, str]] = []
    total_skipped = 0
    for r in results:
        total_skipped += len(r.skipped)
        for s in r.skipped:
            if len(listed) < MAX_LISTED:
                listed.append({"item": r.id, "path": s.get("path", ""), "reason": s.get("reason", "")})
    return {
        "schema": REPORT_SCHEMA,
        "tool_version": __version__,
        "plan_id": plan.get("id", ""),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "dry_run": dry_run,
        "source": plan.get("source") or {},
        "user": plan.get("user", ""),
        "dest_home": plan.get("dest_home", ""),
        "totals": {
            "files": sum(r.files for r in results),
            "bytes": sum(r.bytes for r in results),
            "skipped": total_skipped,
            "errors": sum(len(r.errors) for r in results),
        },
        "items": [r.as_dict() for r in results],
        "skipped": listed,
        "plan_skipped": (plan.get("skipped") or [])[:MAX_LISTED],
        "never_transferred": secrets.denied_names_help(),
        "warnings": list(plan.get("warnings") or []),
        "apps": app_results or [],
        "next_steps": next_steps(plan, results),
    }


# --------------------------------------------------------------------------- #
# saving / loading
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    # The report names the source computer, the Windows user, every path that was copied and
    # whether Firefox passwords were included, so it is written 0600 (independent of the umask),
    # like the journal and the saved plan under the same state directory.
    with open_private(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def save_report(report: Dict[str, Any], *, html_dir: Optional[Path] = None,
                state: Optional[Path] = None) -> Dict[str, str]:
    """Write ``report.json`` (+ ``last-report.json``) and, with *html_dir*, the HTML page."""
    base = Path(state) if state is not None else state_dir()
    pid = str(report.get("plan_id") or "unknown")
    blob = json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8")
    paths: Dict[str, str] = {}
    run_dir = base / pid
    _atomic_write(run_dir / "report.json", blob)
    _atomic_write(base / "last-report.json", blob)
    paths["json"] = str(run_dir / "report.json")
    if html_dir is not None and not report.get("dry_run"):
        stamp = str(report.get("finished") or "")[:10] or time.strftime("%Y-%m-%d")
        page = html_dir / f"Transfer report {stamp}.html"
        from .copyengine import write_generated

        _status, final = write_generated(page, report_html(report).encode("utf-8"))
        paths["html"] = str(final)
    return paths


def load_last_report(state: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    base = Path(state) if state is not None else state_dir()
    try:
        with open(base / "last-report.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
_STATUS_TEXT = {"done": "Done", "partial": "Done, with problems", "skipped": "Not transferred",
                "failed": "Failed", "dry-run": "Would be transferred"}


def format_text(report: Dict[str, Any]) -> str:
    """Terminal summary."""
    t = report.get("totals") or {}
    src = report.get("source") or {}
    lines = [f"Transfer from {src.get('computer') or 'Windows'} ({src.get('windows') or ''}) "
             f"for {report.get('user') or ''} - finished {report.get('finished', '')}",
             f"  {t.get('files', 0)} files copied ({human_size(t.get('bytes', 0))}), "
             f"{t.get('skipped', 0)} skipped, {t.get('errors', 0)} problems", ""]
    for it in report.get("items") or []:
        lines.append(f"  [{_STATUS_TEXT.get(it.get('status'), it.get('status'))}] {it.get('label')}: "
                     f"{it.get('files', 0)} files, {human_size(it.get('bytes', 0))}"
                     + (f" -> {it['dest']}" if it.get("dest") else ""))
        for note in it.get("notes") or []:
            lines.append(f"      {note}")
        for err in (it.get("errors") or [])[:5]:
            lines.append(f"      problem: {err.get('path')}: {err.get('reason')}")
    for w in report.get("warnings") or []:
        lines.append(f"  ! {w}")
    if report.get("next_steps"):
        lines.append("")
        lines.append("Next steps:")
        for s in report["next_steps"]:
            lines.append(f"  * {s}")
    return "\n".join(lines)


def report_html(report: Dict[str, Any]) -> str:
    """A self-contained page the user can open by double-click."""
    e = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    t = report.get("totals") or {}
    src = report.get("source") or {}
    rows = []
    for it in report.get("items") or []:
        notes = "".join(f"<li>{e(n)}</li>" for n in it.get("notes") or [])
        errs = "".join(f"<li>{e(x.get('path'))}: {e(x.get('reason'))}</li>" for x in (it.get("errors") or [])[:50])
        rows.append(
            f"<tr><td>{e(it.get('label'))}</td><td>{e(_STATUS_TEXT.get(it.get('status'), it.get('status')))}</td>"
            f"<td>{e(it.get('files', 0))}</td><td>{e(human_size(it.get('bytes', 0)))}</td>"
            f"<td>{e(it.get('dest', ''))}{('<ul>' + notes + errs + '</ul>') if (notes or errs) else ''}</td></tr>")
    skipped = "".join(f"<li>{e(s.get('path'))} &mdash; {e(s.get('reason'))}</li>"
                      for s in (report.get("skipped") or [])[:1000])
    steps = "".join(f"<li>{e(s)}</li>" for s in report.get("next_steps") or [])
    never = "".join(f"<li>{e(s)}</li>" for s in report.get("never_transferred") or [])
    warns = "".join(f"<li>{e(w)}</li>" for w in report.get("warnings") or [])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Lindos transfer report</title>
<style>
body {{ font-family: "Segoe UI", "Noto Sans", sans-serif; margin: 2em; color: #1b1b1b; background: #fafafa; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; }}
th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; }}
h1 {{ font-weight: 600; }}
</style></head><body>
<h1>Your files from {e(src.get('computer') or 'Windows')}</h1>
<p>{e(src.get('windows', ''))} &middot; user {e(report.get('user', ''))} &middot; finished {e(report.get('finished', ''))}</p>
<p><strong>{e(t.get('files', 0))}</strong> files copied ({e(human_size(t.get('bytes', 0)))}),
{e(t.get('skipped', 0))} skipped, {e(t.get('errors', 0))} problems.</p>
{('<h2>Please note</h2><ul>' + warns + '</ul>') if warns else ''}
<h2>What was transferred</h2>
<table><tr><th>Item</th><th>Result</th><th>Files</th><th>Size</th><th>Where / notes</th></tr>
{''.join(rows)}
</table>
<h2>Next steps</h2><ul>{steps}</ul>
<h2>Never transferred (for your safety)</h2><ul>{never}</ul>
{('<h2>Skipped files</h2><ul>' + skipped + '</ul>') if skipped else ''}
</body></html>
"""
