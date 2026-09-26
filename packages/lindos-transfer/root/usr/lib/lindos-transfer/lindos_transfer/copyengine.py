"""The read-only, resumable, placeholder-aware copier (SPEC-WINDOWS §29.7).

Rules (binding):

* Walk with ``lstat``; **never follow** symlinks, junctions or reparse points.  Windows' legacy
  compatibility junctions ("My Documents", "Application Data", ...) are skipped quietly; other
  links are skipped and reported.
* Skipped quietly: ``desktop.ini``, ``Thumbs.db``, ``~$*`` (Office lock files), ``NTUSER*``,
  ``$RECYCLE.BIN``, ``System Volume Information``, ``AppData``.  Skipped and reported: everything in
  :data:`lindos_transfer.secrets.SECRETS_DENYLIST` (it is never opened).
* **Cloud-only placeholders are never copied** (copying one would give a zero-filled file on ntfs3):
  on ntfs-3g they are symlinks whose target starts ``unsupported reparse tag``; on ntfs3 the
  ``system.ntfs_attrib`` xattr has REPARSE_POINT (0x400) with RECALL_ON_DATA_ACCESS (0x400000) or
  OFFLINE (0x1000) -- never 0x40000 alone --, and without the xattr a non-sparse file with fewer
  allocated blocks than its size is treated as online-only.  EFS/deduplicated files (EOPNOTSUPP)
  are skipped and reported.
* Conflicts: identical (size + modification time) -> skip; otherwise keep both, the Windows copy as
  ``name (from Windows).ext``.  Modification times are preserved; sizes are verified after every
  file; extended attributes and ACLs are never copied.
* The free space is checked first; a JSON-lines journal
  (``~/.local/state/lindos/transfer/<plan-id>/journal.jsonl``) makes a stopped transfer resumable.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import TransferError, secrets

__all__ = [
    "SKIP_FILE_NAMES",
    "SKIP_DIR_NAMES",
    "LEGACY_JUNCTIONS",
    "PLACEHOLDER_LINK_PREFIX",
    "ATTR_REPARSE_POINT",
    "ATTR_OFFLINE",
    "ATTR_ENCRYPTED",
    "ATTR_RECALL_ON_DATA_ACCESS",
    "REASON_PLACEHOLDER",
    "REASON_ENCRYPTED",
    "REASON_UNREADABLE",
    "REASON_LINK",
    "Journal",
    "ScanResult",
    "CopyStats",
    "CopyEngine",
    "placeholder_reason",
    "conflict_name",
    "human_size",
    "check_free_space",
    "copy_to_scratch",
    "write_generated",
    "open_private",
    "DiskFullError",
]

log = logging.getLogger("lindos-transfer.copy")

SKIP_FILE_NAMES = frozenset({"desktop.ini", "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db"})
SKIP_DIR_NAMES = frozenset({"$recycle.bin", "system volume information", "appdata", "$windows.~bt",
                            "$windows.~ws", "$sysreset", "$getcurrent"})
#: Windows' compatibility junctions inside profiles (English names; others are reported as links).
LEGACY_JUNCTIONS = frozenset({"my documents", "my music", "my pictures", "my videos", "application data",
                              "local settings", "cookies", "nethood", "printhood", "recent", "sendto",
                              "start menu", "templates", "documents and settings", "default user",
                              "all users"})
PLACEHOLDER_LINK_PREFIX = "unsupported reparse tag"

ATTR_SPARSE_FILE = 0x200
ATTR_REPARSE_POINT = 0x400
ATTR_COMPRESSED = 0x800
ATTR_OFFLINE = 0x1000
ATTR_ENCRYPTED = 0x4000
ATTR_RECALL_ON_DATA_ACCESS = 0x400000

REASON_PLACEHOLDER = "OneDrive online-only file (not on this disk)"
REASON_OFFLINE = "file is stored offline (not on this disk)"
REASON_ENCRYPTED = "encrypted by Windows (EFS) - copy it on Windows with the Lindos transfer kit"
REASON_UNREADABLE = ("Linux cannot read this file (Windows file encryption or deduplication) - copy it "
                     "on Windows with the Lindos transfer kit")
REASON_LINK = "link to another folder (not followed)"
REASON_SPECIAL = "not a regular file"
REASON_PERMISSION = "no permission to read it"

CHUNK = 1 << 20
_PROGRESS_EVERY_S = 0.5
_FREE_MARGIN = 64 << 20

Emit = Callable[[Dict[str, Any]], None]
GetXattr = Callable[[str, str], bytes]


def _default_getxattr() -> Optional[GetXattr]:
    fn = getattr(os, "getxattr", None)
    if fn is None:
        return None

    def get(path: str, name: str) -> bytes:
        return fn(path, name, follow_symlinks=False)

    return get


def human_size(num: float) -> str:
    """``1536`` -> ``"1.5 KB"`` (decimal units, like the Windows/Linux file managers)."""
    num = float(num)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(num) < 1000 or unit == "TB":
            return f"{int(num)} bytes" if unit == "bytes" else f"{num:.1f} {unit}"
        num /= 1000.0
    return f"{num:.1f} TB"  # pragma: no cover


def conflict_name(dest: Path) -> Path:
    """``name (from Windows).ext`` (then ``(from Windows 2)`` ...) that does not exist yet."""
    stem, suffix = dest.stem, dest.suffix
    if dest.name.startswith(".") and dest.suffix == dest.name:
        stem, suffix = dest.name, ""
    for n in range(1, 10000):
        tag = "from Windows" if n == 1 else f"from Windows {n}"
        cand = dest.with_name(f"{stem} ({tag}){suffix}")
        if not os.path.lexists(cand):
            return cand
    raise TransferError(f"too many copies of {dest.name} already exist")


def check_free_space(dest: Path, needed: int) -> None:
    """Raise :class:`TransferError` when the disk holding *dest* has less than *needed* (+ margin) free."""
    probe = Path(dest)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if needed + _FREE_MARGIN > free:
        raise TransferError(f"Not enough free space: this transfer needs {human_size(needed)} but only "
                            f"{human_size(free)} is free on the Lindos disk. Untick some items or free "
                            f"up space, then try again.")


def placeholder_reason(path: str, st: os.stat_result, *, driver: str = "unknown",
                       getxattr: Optional[GetXattr] = None) -> Optional[str]:
    """Why a *regular file* must not be copied because its data is not really on the disk (or ``None``)."""
    attrs: Optional[int] = None
    if getxattr is not None:
        try:
            raw = getxattr(path, "system.ntfs_attrib")
            if raw and len(raw) >= 4:
                attrs = int.from_bytes(bytes(raw[:4]), sys.byteorder)
        except OSError:
            attrs = None
    if attrs is not None:
        if attrs & ATTR_REPARSE_POINT and attrs & (ATTR_RECALL_ON_DATA_ACCESS | ATTR_OFFLINE):
            return REASON_PLACEHOLDER
        if attrs & ATTR_OFFLINE:
            return REASON_OFFLINE
        if attrs & ATTR_ENCRYPTED:
            return REASON_ENCRYPTED
        if attrs & (ATTR_SPARSE_FILE | ATTR_COMPRESSED):
            return None
    if driver in ("ntfs-3g", "bundle") or st.st_size <= 0:
        return None
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return None
    if blocks * 512 + 512 <= st.st_size:
        return REASON_PLACEHOLDER
    if blocks == 0 and st.st_size < 512:
        # tiny files: an online-only placeholder reads back as zeros; a real one almost never does
        try:
            with secrets.safe_open(path) as fh:
                data = fh.read(512)
        except OSError:
            return None
        if data and not data.strip(b"\x00"):
            return REASON_PLACEHOLDER
    return None


# --------------------------------------------------------------------------- #
# private (state-directory) files: the journal, the saved plan, the transfer report
# --------------------------------------------------------------------------- #
def open_private(path: Path, mode: str = "w", **kwargs: Any) -> Any:
    """Open *path* mode 0600, independent of the process umask, creating its parent 0700 first.

    For files under ``~/.local/state/lindos/transfer/<plan-id>/`` (the journal, the saved plan,
    the JSON report): these can name the source computer, the Windows user, every path that was
    copied and whether Firefox passwords were included, so -- like the Wi-Fi keyfiles the root
    helper already writes 0600 -- they must not depend on the ambient umask (Ubuntu's default
    interactive umask, 022, already makes a plain ``open(path, "w")`` world-readable).  *kwargs*
    are forwarded to :func:`os.fdopen` (e.g. ``newline="\\n"`` for a text mode).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    if "b" not in mode:
        kwargs.setdefault("encoding", "utf-8")
    return os.fdopen(fd, mode, **kwargs)


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
class Journal:
    """Append-only JSON-lines record of copied files (resume support)."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._done: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._fh = None
        if path is None:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("status") in ("copied", "renamed", "identical"):
                        self._done[(str(rec.get("item")), str(rec.get("rel")))] = rec
        except OSError:
            pass

    def done(self, item: str, rel: str) -> Optional[Dict[str, Any]]:
        return self._done.get((item, rel))

    def record(self, **entry: Any) -> None:
        key = (str(entry.get("item")), str(entry.get("rel")))
        if entry.get("status") in ("copied", "renamed", "identical"):
            self._done[key] = entry
        if self.path is None:
            return
        if self._fh is None:
            self._fh = open_private(self.path, "a")
        self._fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class ScanResult:
    files: int = 0
    bytes: int = 0
    skipped: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class CopyStats:
    files: int = 0              # copied (incl. renamed)
    bytes: int = 0
    identical: int = 0          # already present, left alone
    renamed: int = 0            # kept both: "(from Windows)"
    skipped: List[Dict[str, str]] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    last_dest: Optional[str] = None     # where the last copied/identical file is (single-file copies)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def merge(self, other: "CopyStats") -> None:
        self.files += other.files
        self.bytes += other.bytes
        self.identical += other.identical
        self.renamed += other.renamed
        self.skipped.extend(other.skipped)
        self.errors.extend(other.errors)
        if other.last_dest:
            self.last_dest = other.last_dest


class DiskFullError(TransferError):
    """The Lindos disk ran out of space while copying."""


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class CopyEngine:
    """Scan and copy trees from a (read-only) Windows source into the Lindos home."""

    def __init__(self, *, driver: str = "unknown", journal: Optional[Journal] = None,
                 emit: Optional[Emit] = None, dry_run: bool = False,
                 getxattr: Optional[GetXattr] = None, use_default_xattr: bool = True,
                 allow_firefox_passwords: bool = False, total_bytes: int = 0) -> None:
        self.driver = driver
        self.journal = journal or Journal(None)
        self.emit_cb = emit
        self.dry_run = dry_run
        self.getxattr = getxattr if getxattr is not None else (_default_getxattr() if use_default_xattr else None)
        self.allow_firefox_passwords = allow_firefox_passwords
        self.total_bytes = total_bytes
        self.done_bytes = 0
        self._last_progress = 0.0

    # -- events -------------------------------------------------------------
    def emit(self, event: str, *, item: str = "", path: str = "", message: str = "") -> None:
        if self.emit_cb is None:
            return
        self.emit_cb({"event": event, "item": item, "done_bytes": self.done_bytes,
                      "total_bytes": self.total_bytes, "path": path, "message": message})

    def _progress(self, item: str, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress >= _PROGRESS_EVERY_S:
            self._last_progress = now
            self.emit("progress", item=item)

    # -- walking ------------------------------------------------------------
    def walk(self, src: Path, *, exclude: Iterable[Path] = (),
             skip_appdata: bool = True) -> Iterator[Tuple[str, Path, str, Any]]:
        """Yield ``("file", path, rel, stat)`` and ``("skip", path, rel, reason)``; never follows links."""
        excl = {os.path.normcase(os.path.abspath(str(e))) for e in exclude}
        reason = secrets.is_denied(src, allow_firefox_passwords=self.allow_firefox_passwords)
        if reason:
            yield ("skip", src, "", reason)
            return
        stack: List[Tuple[Path, str]] = [(Path(src), "")]
        while stack:
            directory, rel = stack.pop()
            try:
                with os.scandir(directory) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except PermissionError:
                yield ("skip", directory, rel, REASON_PERMISSION)
                continue
            except OSError as exc:
                yield ("skip", directory, rel, f"folder cannot be read ({exc.strerror or exc})")
                continue
            subdirs: List[Tuple[Path, str]] = []
            for e in entries:
                path = Path(e.path)
                erel = f"{rel}/{e.name}" if rel else e.name
                lname = e.name.lower()
                denied = secrets.is_denied(path, allow_firefox_passwords=self.allow_firefox_passwords)
                if denied:
                    yield ("skip", path, erel, denied)
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError as exc:
                    yield ("skip", path, erel, f"cannot be read ({exc.strerror or exc})")
                    continue
                if stat.S_ISLNK(st.st_mode):
                    try:
                        target = os.readlink(path)
                    except OSError:
                        target = ""
                    if str(target).lower().startswith(PLACEHOLDER_LINK_PREFIX):
                        yield ("skip", path, erel, REASON_PLACEHOLDER)
                    elif lname in LEGACY_JUNCTIONS:
                        log.debug("skipping Windows compatibility junction %s", path)
                    else:
                        yield ("skip", path, erel, REASON_LINK)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if lname in SKIP_DIR_NAMES and (lname != "appdata" or skip_appdata):
                        log.debug("skipping %s", path)
                        continue
                    if os.path.normcase(os.path.abspath(str(path))) in excl:
                        continue
                    subdirs.append((path, erel))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    yield ("skip", path, erel, REASON_SPECIAL)
                    continue
                if lname in SKIP_FILE_NAMES or lname.startswith("~$") or lname.startswith("ntuser"):
                    continue
                why = placeholder_reason(str(path), st, driver=self.driver, getxattr=self.getxattr)
                if why:
                    yield ("skip", path, erel, why)
                    continue
                yield ("file", path, erel, st)
            for sub in reversed(subdirs):
                stack.append(sub)

    def scan(self, src: Path, *, exclude: Iterable[Path] = (), skip_appdata: bool = True) -> ScanResult:
        """Count what :meth:`copy_tree` would copy (nothing is opened except tiny placeholder checks)."""
        res = ScanResult()
        for kind, path, _rel, info in self.walk(src, exclude=exclude, skip_appdata=skip_appdata):
            if kind == "file":
                res.files += 1
                res.bytes += info.st_size
            else:
                res.skipped.append({"path": str(path), "reason": info})
        return res

    # -- copying ------------------------------------------------------------
    def copy_tree(self, src: Path, dest: Path, *, item: str, exclude: Iterable[Path] = (),
                  skip_appdata: bool = True,
                  accept: Optional[Callable[[Path], bool]] = None) -> CopyStats:
        """Copy the tree *src* into *dest* (created).  Returns per-item statistics."""
        stats = CopyStats()
        dest = Path(dest)
        for kind, path, rel, info in self.walk(src, exclude=exclude, skip_appdata=skip_appdata):
            if kind == "skip":
                stats.skipped.append({"path": str(path), "reason": info})
                self.emit("skip", item=item, path=str(path), message=info)
                continue
            if accept is not None and not accept(path):
                continue
            target = dest.joinpath(*rel.split("/"))
            self._copy_counted(path, info, target, item=item, rel=rel, stats=stats)
        self._progress(item, force=True)
        return stats

    def copy_file(self, src: Path, dest: Path, *, item: str, rel: Optional[str] = None) -> CopyStats:
        """Copy one file (same checks as the tree walk) to the exact path *dest*."""
        stats = CopyStats()
        src = Path(src)
        denied = secrets.is_denied(src, allow_firefox_passwords=self.allow_firefox_passwords)
        if denied:
            stats.skipped.append({"path": str(src), "reason": denied})
            self.emit("skip", item=item, path=str(src), message=denied)
            return stats
        try:
            st = os.lstat(src)
        except OSError as exc:
            stats.errors.append({"path": str(src), "reason": f"cannot be read ({exc.strerror or exc})"})
            self.emit("error", item=item, path=str(src), message=stats.errors[-1]["reason"])
            return stats
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            reason = REASON_LINK if stat.S_ISLNK(st.st_mode) else REASON_SPECIAL
            stats.skipped.append({"path": str(src), "reason": reason})
            self.emit("skip", item=item, path=str(src), message=reason)
            return stats
        why = placeholder_reason(str(src), st, driver=self.driver, getxattr=self.getxattr)
        if why:
            stats.skipped.append({"path": str(src), "reason": why})
            self.emit("skip", item=item, path=str(src), message=why)
            return stats
        self._copy_counted(src, st, Path(dest), item=item, rel=rel or src.name, stats=stats)
        return stats

    def _copy_counted(self, src: Path, st: os.stat_result, target: Path, *, item: str, rel: str,
                      stats: CopyStats) -> None:
        status, final, message = self._copy_one(src, st, target, item=item, rel=rel)
        if final is not None and status in ("copied", "renamed", "identical"):
            stats.last_dest = str(final)
        if status in ("copied", "renamed"):
            stats.files += 1
            stats.bytes += st.st_size
            if status == "renamed":
                stats.renamed += 1
            self.emit("file", item=item, path=str(final), message=message)
        elif status == "identical":
            stats.identical += 1
        elif status == "skip":
            stats.skipped.append({"path": str(src), "reason": message})
            self.emit("skip", item=item, path=str(src), message=message)
        else:
            stats.errors.append({"path": str(src), "reason": message})
            self.emit("error", item=item, path=str(src), message=message)
        self.done_bytes += st.st_size
        self._progress(item)

    @staticmethod
    def _same(dest: Path, st: os.stat_result) -> bool:
        try:
            dst = os.lstat(dest)
        except OSError:
            return False
        return (stat.S_ISREG(dst.st_mode) and dst.st_size == st.st_size
                and abs(dst.st_mtime - st.st_mtime) < 2.0)

    def _copy_one(self, src: Path, st: os.stat_result, target: Path, *, item: str,
                  rel: str) -> Tuple[str, Optional[Path], str]:
        prev = self.journal.done(item, rel)
        if prev is not None:
            prev_dest = Path(str(prev.get("dest") or ""))
            if prev.get("dest") and self._same(prev_dest, st):
                return "identical", prev_dest, "already copied"
        final = target
        status = "copied"
        if os.path.lexists(final):
            if self._same(final, st):
                self.journal.record(item=item, rel=rel, dest=str(final), size=st.st_size, status="identical")
                return "identical", final, "already there (same size and date)"
            final = conflict_name(target)
            status = "renamed"
        if self.dry_run:
            return status, final, "dry run"
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DiskFullError("The Lindos disk is full.") from exc
            return "error", None, f"cannot create folder {final.parent} ({exc.strerror or exc})"
        part = final.with_name(f".{final.name[:200]}.lindos-part")
        try:
            if os.path.lexists(part):
                os.unlink(part)
            written = 0
            with secrets.safe_open(src, allow_firefox_passwords=self.allow_firefox_passwords) as fin:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(part, flags, 0o644)
                with os.fdopen(fd, "wb") as fout:
                    while True:
                        chunk = fin.read(CHUNK)
                        if not chunk:
                            break
                        fout.write(chunk)
                        written += len(chunk)
            size_now = os.stat(part).st_size
            if written != st.st_size or size_now != st.st_size:
                os.unlink(part)
                return "error", None, (f"size check failed (expected {st.st_size} bytes, got {written}); "
                                       "the file may have changed or the disk has errors")
            os.utime(part, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(part, final)
        except secrets.SecretPathError as exc:
            return "skip", None, exc.reason
        except secrets.UnsafeFileError:
            return "skip", None, REASON_LINK
        except OSError as exc:
            try:
                if os.path.lexists(part):
                    os.unlink(part)
            except OSError:
                pass
            if exc.errno == errno.ENOSPC:
                raise DiskFullError("The Lindos disk is full. Free up space and run the transfer again "
                                    "- it continues where it stopped.") from exc
            if exc.errno in (errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)):
                return "skip", None, REASON_UNREADABLE
            if exc.errno in (errno.EACCES, errno.EPERM):
                return "skip", None, REASON_PERMISSION
            return "error", None, f"copy failed ({exc.strerror or exc})"
        self.journal.record(item=item, rel=rel, dest=str(final), size=st.st_size, status=status)
        return status, final, "kept both: the Windows copy is named '(from Windows)'" if status == "renamed" else ""


def copy_to_scratch(src: Path, dest: Path, *, allow_firefox_passwords: bool = False) -> int:
    """Stream one source file through the secrets gate to a scratch/new location (no conflict rules).

    Used for temporary copies (e.g. ``places.sqlite`` before reading it) and for files written into
    brand-new folders.  Returns the number of bytes copied.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with secrets.safe_open(src, allow_firefox_passwords=allow_firefox_passwords) as fin, open(dest, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
    return written


def write_generated(dest: Path, data: bytes, *, dry_run: bool = False) -> Tuple[str, Path]:
    """Write a file Lindos generated (bookmark HTML, report ...) without overwriting anything.

    Returns ``("identical"|"written"|"renamed", path)``: identical content is left alone, a
    different existing file is kept and the new one gets the ``(from Windows)`` name.
    """
    dest = Path(dest)
    status = "written"
    if os.path.lexists(dest):
        try:
            if stat.S_ISREG(os.lstat(dest).st_mode) and dest.read_bytes() == data:
                return "identical", dest
        except OSError:
            pass
        dest = conflict_name(dest)
        status = "renamed"
    if dry_run:
        return status, dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name[:200]}.lindos-part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    return status, dest
