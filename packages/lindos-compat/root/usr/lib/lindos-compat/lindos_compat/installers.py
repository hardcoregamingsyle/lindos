"""Installers for optional pieces of the compatibility layer.

* :func:`install_umu`      -- umu-launcher (``umu-run``) from the pinned GitHub release:
  the Ubuntu-noble ``.deb`` pair when running as root with apt (dependencies resolved by
  apt), otherwise the self-contained *zipapp* copied to ``/usr/local/bin/umu-run`` (root)
  or ``~/.local/bin/umu-run`` (user).  SHA-256 is verified against the pinned digest
  and/or the digest published by the GitHub release API.
* :func:`install_bottles`  -- Bottles from Flathub (``flatpak install -y flathub
  com.usebottles.bottles``), system-wide when possible, ``--user`` as fallback.

Nothing here runs at import time; every network/subprocess call is injectable so the
decision logic is unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import get_logger, path_const, user_home
from .runner import BOTTLES_APP_ID

__all__ = [
    "UMU_REPO",
    "UMU_VERSION",
    "UMU_ASSETS",
    "UMU_PINNED_SHA256",
    "InstallResult",
    "is_root",
    "umu_asset_names",
    "umu_download_url",
    "umu_target_path",
    "fetch_release_assets",
    "sha256_file",
    "install_umu",
    "install_bottles",
    "FLATHUB_URL",
    "COMPONENT_KEYS",
    "components_path",
    "load_components",
    "github_asset_url",
    "install_into_prefix",
    "install_dxvk",
    "install_vkd3d",
]

log = get_logger("lindos-compat.installers")

UMU_REPO = "Open-Wine-Components/umu-launcher"
UMU_VERSION = "1.4.4"
USER_AGENT = "lindos-compat/1.0 (+https://lindos.dev)"

#: asset name patterns of a umu-launcher GitHub release (``{v}`` = version tag)
UMU_ASSETS: Dict[str, str] = {
    "zipapp": "umu-launcher-{v}-zipapp.tar",
    "deb-python": "python3-umu-launcher_{v}-1_amd64_ubuntu-noble.deb",
    "deb-meta": "umu-launcher_{v}-1_all_ubuntu-noble.deb",
}

#: pinned SHA-256 digests (release 1.4.4, taken from the GitHub release API ``digest`` field)
UMU_PINNED_SHA256: Dict[str, str] = {
    "umu-launcher-1.4.4-zipapp.tar": "eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851",
    "python3-umu-launcher_1.4.4-1_amd64_ubuntu-noble.deb": "86b7a234f77fbcd13699654656192a12ed3852ec2bcc721506ae4f91436b3793",
    "umu-launcher_1.4.4-1_all_ubuntu-noble.deb": "8d11aa5bf0edaa988a4cbb04a1414a3d2249128913e6130e6a2a8528c9ef31b1",
}

FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"

CHUNK = 1024 * 1024


@dataclass
class InstallResult:
    ok: bool
    what: str
    message: str = ""
    path: str = ""
    method: str = ""  # deb | zipapp | flatpak-system | flatpak-user | already | delegated | dry-run
    steps: List[Tuple[str, bool, str]] = field(default_factory=list)
    offline: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "what": self.what, "message": self.message, "path": self.path,
                "method": self.method, "offline": self.offline, "steps": [list(s) for s in self.steps]}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    try:
        return bool(callable(geteuid) and geteuid() == 0)
    except OSError:
        return False


def umu_asset_names(version: str = UMU_VERSION) -> Dict[str, str]:
    return {k: v.format(v=version) for k, v in UMU_ASSETS.items()}


def umu_download_url(asset: str, version: str = UMU_VERSION) -> str:
    return f"https://github.com/{UMU_REPO}/releases/download/{version}/{asset}"


def umu_target_path(system: bool, home: Optional[Path] = None) -> Path:
    if system:
        return Path("/usr/local/bin/umu-run")
    return (home or user_home()) / ".local/bin/umu-run"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _urlopen(url: str, timeout: int = 30):  # type: ignore[no-untyped-def]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json, */*"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - https URLs only


def fetch_release_assets(version: str = UMU_VERSION, *, opener: Callable[..., object] = _urlopen) -> Dict[str, Dict[str, str]]:
    """``{asset_name: {"url": ..., "sha256": ...}}`` from the GitHub release API (``{}`` on failure)."""
    tag = "latest" if version == "latest" else f"tags/{version}"
    url = f"https://api.github.com/repos/{UMU_REPO}/releases/{tag}"
    try:
        with opener(url, 20) as resp:  # type: ignore[attr-defined]
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log.debug("GitHub API unavailable (%s): %s", url, exc)
        return {}
    out: Dict[str, Dict[str, str]] = {}
    tag_name = str(data.get("tag_name") or version)
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not name:
            continue
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        out[name] = {"url": str(asset.get("browser_download_url") or ""), "sha256": sha, "tag": tag_name}
    return out


def _download(url: str, dest: Path, *, opener: Callable[..., object] = _urlopen,
              on_progress: Optional[Callable[[str], None]] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(f"Downloading {dest.name}…")
    with opener(url, 60) as resp, open(dest, "wb") as fh:  # type: ignore[attr-defined]
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)


def _verify(dest: Path, expected: str, result: InstallResult, *, verify: bool) -> bool:
    if not expected:
        result.steps.append(("verify " + dest.name, True, "no known SHA-256 (not verified)"))
        return True
    actual = sha256_file(dest)
    if actual.lower() == expected.lower():
        result.steps.append(("verify " + dest.name, True, "sha256 ok"))
        return True
    if verify:
        result.steps.append(("verify " + dest.name, False, f"sha256 mismatch: expected {expected}, got {actual}"))
        return False
    result.steps.append(("verify " + dest.name, True, f"sha256 mismatch IGNORED (--no-verify): {actual}"))
    return True


def _extract_zipapp(tar_path: Path) -> bytes:
    """Return the bytes of the ``umu-run`` zipapp inside the release tarball."""
    with tarfile.open(tar_path, "r:*") as tf:
        member = None
        for m in tf.getmembers():
            if m.isfile() and os.path.basename(m.name) == "umu-run":
                member = m
                break
        if member is None:  # fall back to the first regular file that is a zip
            for m in tf.getmembers():
                if m.isfile() and m.size > 1024:
                    member = m
                    break
        if member is None:
            raise ValueError("no umu-run zipapp inside the tarball")
        fh = tf.extractfile(member)
        if fh is None:
            raise ValueError("cannot read umu-run from the tarball")
        return fh.read()


# ---------------------------------------------------------------------------
# umu-launcher
# ---------------------------------------------------------------------------


def install_umu(
    *,
    system: bool = False,
    version: str = UMU_VERSION,
    verify: bool = True,
    force: bool = False,
    prefer_deb: Optional[bool] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    opener: Callable[..., object] = _urlopen,
    on_progress: Optional[Callable[[str], None]] = None,
    home: Optional[Path] = None,
    dry_run: bool = False,
) -> InstallResult:
    """Install ``umu-run`` (see module docstring)."""
    result = InstallResult(ok=False, what="umu-launcher")
    target = umu_target_path(system, home)

    existing = which("umu-run")
    if existing and not force:
        result.ok = True
        result.method = "already"
        result.path = existing
        result.message = f"umu-run is already installed at {existing} (use --force to reinstall)"
        return result

    if system and not is_root():
        result.method = "delegated"
        return _delegate_umu(result)

    names = umu_asset_names(version if version != "latest" else UMU_VERSION)
    assets = fetch_release_assets(version, opener=opener)
    if version == "latest" and assets:
        tag = next(iter(assets.values())).get("tag", "")
        if tag:
            version = tag
            names = umu_asset_names(version)
    if not assets and version == "latest":
        version = UMU_VERSION
        names = umu_asset_names(version)

    def sha_for(name: str) -> str:
        api = assets.get(name, {}).get("sha256", "")
        pinned = UMU_PINNED_SHA256.get(name, "")
        if api and pinned and api.lower() != pinned.lower():
            log.warning("pinned SHA-256 for %s differs from the GitHub release digest; using the published one", name)
        return api or pinned

    def url_for(name: str) -> str:
        return assets.get(name, {}).get("url") or umu_download_url(name, version)

    if prefer_deb is None:
        prefer_deb = system and bool(which("apt-get")) and bool(which("dpkg"))

    if dry_run:
        result.ok = True
        result.method = "dry-run"
        result.path = str(target)
        plan = ("deb: " + ", ".join(url_for(names[k]) for k in ("deb-python", "deb-meta"))) if prefer_deb \
            else ("zipapp: " + url_for(names["zipapp"]))
        result.message = f"would install umu-launcher {version} -> {target} ({plan})"
        result.steps.append(("plan", True, plan))
        return result

    with tempfile.TemporaryDirectory(prefix="lindos-umu-") as td:
        tmp = Path(td)
        # --- 1. .deb (root + apt) ------------------------------------------------
        if prefer_deb:
            deb_ok = True
            deb_files: List[Path] = []
            for key in ("deb-python", "deb-meta"):
                name = names[key]
                dest = tmp / name
                try:
                    _download(url_for(name), dest, opener=opener, on_progress=on_progress)
                    result.steps.append(("download " + name, True, url_for(name)))
                except (OSError, urllib.error.URLError) as exc:
                    result.steps.append(("download " + name, False, str(exc)))
                    result.offline = _looks_offline(exc)
                    deb_ok = False
                    break
                if not _verify(dest, sha_for(name), result, verify=verify):
                    deb_ok = False
                    break
                deb_files.append(dest)
            if deb_ok and deb_files:
                env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
                argv = [str(which("apt-get")), "install", "-y", "--no-install-recommends"] + [str(p) for p in deb_files]
                if on_progress:
                    on_progress("Installing umu-launcher packages…")
                try:
                    proc = run(argv, env=env, capture_output=True, text=True, timeout=1800, check=False)
                    ok = proc.returncode == 0
                    result.steps.append(("apt-get install", ok, "ok" if ok else (proc.stderr or proc.stdout or "").strip()[-400:]))
                except (OSError, subprocess.SubprocessError) as exc:
                    ok = False
                    result.steps.append(("apt-get install", False, str(exc)))
                if ok:
                    found = which("umu-run") or "/usr/bin/umu-run"
                    result.ok = True
                    result.method = "deb"
                    result.path = found
                    result.message = f"umu-launcher {version} installed system-wide ({found})"
                    return result
            if result.offline:
                result.message = "Cannot download umu-launcher: no internet connection."
                return result
            log.warning("umu-launcher .deb install failed; falling back to the zipapp")

        # --- 2. zipapp -----------------------------------------------------------
        name = names["zipapp"]
        dest = tmp / name
        try:
            _download(url_for(name), dest, opener=opener, on_progress=on_progress)
            result.steps.append(("download " + name, True, url_for(name)))
        except (OSError, urllib.error.URLError) as exc:
            result.steps.append(("download " + name, False, str(exc)))
            result.offline = _looks_offline(exc)
            result.message = ("Cannot download umu-launcher: no internet connection." if result.offline
                              else f"Download failed: {exc}")
            return result
        if not _verify(dest, sha_for(name), result, verify=verify):
            result.message = "SHA-256 check failed - the download is corrupt or the pinned digest is stale " \
                             "(retry with --version latest, or --no-verify if you trust the source)"
            return result
        try:
            payload = _extract_zipapp(dest)
        except (OSError, ValueError, tarfile.TarError) as exc:
            result.steps.append(("extract", False, str(exc)))
            result.message = f"Could not unpack the umu-launcher zipapp: {exc}"
            return result
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_name(target.name + ".tmp")
            with open(tmp_target, "wb") as fh:
                fh.write(payload)
            os.chmod(tmp_target, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            os.replace(tmp_target, target)
        except OSError as exc:
            result.steps.append(("install", False, str(exc)))
            result.message = f"Cannot write {target}: {exc}"
            return result
        sane = payload[:2] == b"#!" and zipfile.is_zipfile(target)
        result.steps.append(("install", True, f"{target} ({len(payload)} bytes{'' if sane else ', unexpected format'})"))
        result.ok = True
        result.method = "zipapp"
        result.path = str(target)
        note = ""
        if not system:
            path_dirs = os.environ.get("PATH", "").split(os.pathsep)
            if str(target.parent) not in path_dirs:
                note = f"  Note: {target.parent} is not on your PATH yet - log out and back in (or add it to PATH)."
        result.message = f"umu-launcher {version} installed at {target} (needs python3 >= 3.10){note}"
        return result


def _looks_offline(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("name or service not known", "temporary failure in name resolution",
                                   "network is unreachable", "no route to host", "connection refused",
                                   "timed out", "getaddrinfo", "nodename nor servname"))


def _delegate_umu(result: InstallResult) -> InstallResult:
    """Non-root ``--system``: ask the polkit helper (action ``install-compat``, items ["umu"])."""
    try:
        from lindos import helper as core_helper  # type: ignore[import-not-found]
    except ImportError:
        result.message = ("System-wide install needs root. Run:  pkexec /usr/libexec/lindos/install-compat.sh umu\n"
                          "(lindos-core's helper module is not available for delegation)")
        return result
    try:
        hres = core_helper.run_privileged("install-compat", {"items": ["umu"]})
    except Exception as exc:  # noqa: BLE001
        result.message = f"Privileged install failed: {exc}\nRun manually:  pkexec /usr/libexec/lindos/install-compat.sh umu"
        return result
    ok = bool(getattr(hres, "ok", False))
    result.ok = ok
    result.message = str(getattr(hres, "message", "") or ("installed" if ok else "helper failed"))
    result.steps.append(("helper install-compat umu", ok, result.message))
    if ok:
        result.path = shutil.which("umu-run") or "/usr/local/bin/umu-run"
    return result


# ---------------------------------------------------------------------------
# Bottles
# ---------------------------------------------------------------------------


def _flatpak_remotes(flatpak: str, run: Callable[..., "subprocess.CompletedProcess[str]"], scope: str) -> List[str]:
    try:
        proc = run([flatpak, "remotes", scope, "--columns=name"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip() and not ln.startswith("Name")]


def install_bottles(
    *,
    system: Optional[bool] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    on_progress: Optional[Callable[[str], None]] = None,
    dry_run: bool = False,
) -> InstallResult:
    """``flatpak install -y flathub com.usebottles.bottles`` (system first, then ``--user``)."""
    result = InstallResult(ok=False, what="bottles")
    flatpak = which("flatpak")
    if not flatpak:
        result.message = "flatpak is not installed (apt install flatpak), so Bottles cannot be installed."
        return result

    scopes: List[str]
    if system is True:
        scopes = ["--system"]
    elif system is False:
        scopes = ["--user"]
    else:  # system-wide (polkit prompt for non-root users), then per-user as fallback
        scopes = ["--system", "--user"]

    if dry_run:
        result.ok = True
        result.method = "dry-run"
        result.message = f"would run: flatpak install -y flathub {BOTTLES_APP_ID} ({'/'.join(scopes)})"
        return result

    last_err = ""
    for scope in scopes:
        remotes = _flatpak_remotes(flatpak, run, scope)
        if "flathub" not in remotes:
            argv = [flatpak, "remote-add", scope, "--if-not-exists", "flathub", FLATHUB_URL]
            try:
                proc = run(argv, capture_output=True, text=True, timeout=300, check=False)
                result.steps.append((f"remote-add flathub {scope}", proc.returncode == 0,
                                     (proc.stderr or "").strip()[-200:] or "ok"))
                if proc.returncode != 0:
                    last_err = (proc.stderr or proc.stdout or "").strip()
                    continue
            except (OSError, subprocess.SubprocessError) as exc:
                result.steps.append((f"remote-add flathub {scope}", False, str(exc)))
                last_err = str(exc)
                continue
        if on_progress:
            on_progress(f"Installing Bottles from Flathub ({scope.lstrip('-')})…")
        argv = [flatpak, "install", scope, "-y", "--noninteractive", "flathub", BOTTLES_APP_ID]
        try:
            proc = run(argv, capture_output=True, text=True, timeout=3600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            result.steps.append((f"flatpak install {scope}", False, str(exc)))
            last_err = str(exc)
            continue
        ok = proc.returncode == 0
        result.steps.append((f"flatpak install {scope}", ok, "ok" if ok else (proc.stderr or proc.stdout or "").strip()[-300:]))
        if ok:
            result.ok = True
            result.method = "flatpak-" + scope.lstrip("-")
            result.path = BOTTLES_APP_ID
            result.message = f"Bottles installed ({scope.lstrip('-')}). Start it from the Start Menu or: flatpak run {BOTTLES_APP_ID}"
            return result
        last_err = (proc.stderr or proc.stdout or "").strip()
        if _looks_offline(RuntimeError(last_err)):
            result.offline = True
            break
    result.message = ("Cannot install Bottles: no internet connection." if result.offline
                      else f"Bottles install failed: {last_err[-300:] or 'unknown error'}")
    return result


# ---------------------------------------------------------------------------
# DXVK / VKD3D-Proton into a plain-Wine prefix (SPEC-KERNEL §17.2)
# ---------------------------------------------------------------------------

#: map a friendly component name -> the key used in components.json
COMPONENT_KEYS = {"dxvk": "dxvk", "vkd3d": "vkd3d_proton", "vkd3d_proton": "vkd3d_proton"}


def components_path() -> Path:
    """``/usr/share/lindos/compat/components.json`` (LINDOS_ROOT-aware; env override)."""
    override = os.environ.get("LINDOS_COMPONENTS")
    if override:
        return Path(override)
    return Path(path_const("SHARE_DIR")) / "compat" / "components.json"


def load_components() -> Dict[str, Dict[str, str]]:
    """Load the pinned component versions ({} when the file is missing/broken)."""
    try:
        with open(components_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log.debug("components.json unavailable: %s", exc)
        return {}


def github_asset_url(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def _find_libexec_script(name: str) -> Optional[Path]:
    """Locate a libexec helper (installed path, LINDOS_ROOT, or the repo checkout)."""
    candidates = []
    root = os.environ.get("LINDOS_ROOT")
    if root:
        candidates.append(Path(root.rstrip("/\\")) / "usr/libexec/lindos" / name)
    here = Path(__file__).resolve()
    # .../root/usr/lib/lindos-compat/lindos_compat/installers.py -> .../root/usr/libexec/lindos
    for parent in here.parents:
        if parent.name == "lindos-compat" and (parent / "root").is_dir():
            candidates.append(parent / "root/usr/libexec/lindos" / name)
            break
    candidates.append(Path("/usr/libexec/lindos") / name)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def install_into_prefix(
    component: str,
    prefix: Path,
    *,
    components: Optional[Dict[str, Dict[str, str]]] = None,
    tag: Optional[str] = None,
    url: Optional[str] = None,
    sha256: Optional[str] = None,
    arch: str = "win64",
    uninstall: bool = False,
    verify: bool = True,
    dry_run: bool = False,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    script: Optional[Path] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> InstallResult:
    """Install/refresh DXVK or VKD3D-Proton into a Wine ``prefix`` via install-into-prefix.sh."""
    friendly = component.lower()
    key = COMPONENT_KEYS.get(friendly)
    result = InstallResult(ok=False, what=friendly if key else component)
    if not key:
        result.message = f"unknown component '{component}' (dxvk | vkd3d)"
        return result
    comp_arg = "dxvk" if key == "dxvk" else "vkd3d"

    comps = load_components() if components is None else components
    pin = comps.get(key, {}) if isinstance(comps.get(key), dict) else {}
    if tag is None:
        tag = str(pin.get("tag") or "")
    if sha256 is None:
        sha256 = str(pin.get("sha256") or "")
    if url is None and not uninstall:
        repo = str(pin.get("repo") or "")
        asset = str(pin.get("asset") or "")
        if not (repo and asset and tag):
            result.message = (f"no pinned {comp_arg} release found in {components_path()} "
                              "(pass --tag/--url, or check the package installation)")
            return result
        url = github_asset_url(repo, tag, asset)

    sh = script or _find_libexec_script("install-into-prefix.sh")
    if sh is None:
        result.message = "install-into-prefix.sh not found (reinstall lindos-compat)"
        return result

    argv: List[str] = [str(which("bash") or "bash"), str(sh), "--component", comp_arg,
                       "--prefix", str(prefix), "--arch", arch]
    if uninstall:
        argv.append("--uninstall")
    else:
        argv += ["--url", str(url)]
        if tag:
            argv += ["--tag", tag]
        if sha256 and verify:
            argv += ["--sha256", sha256]
    if dry_run:
        argv.append("--dry-run")

    if on_progress:
        on_progress(f"{'Removing' if uninstall else 'Installing'} {comp_arg} in {prefix.name}…")
    try:
        proc = run(argv, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        result.steps.append(("install-into-prefix.sh", False, str(exc)))
        result.message = f"could not run install-into-prefix.sh: {exc}"
        return result
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0
    result.steps.append((f"{comp_arg} {'uninstall' if uninstall else 'install'}", ok, out[-400:] or "ok"))
    result.ok = ok
    result.method = "dry-run" if dry_run else ("uninstall" if uninstall else "prefix")
    result.path = str(prefix)
    if proc.returncode == 3:
        result.offline = True
        result.message = f"Cannot download {comp_arg}: no internet connection."
    elif ok:
        verb = "removed from" if uninstall else f"{tag or 'installed'} into"
        result.message = f"{comp_arg} {verb} {prefix}"
    else:
        result.message = f"{comp_arg} install failed (exit {proc.returncode}): {out[-300:] or 'see log'}"
    return result


def install_dxvk(prefix: Path, **kwargs: object) -> InstallResult:
    """Install/refresh DXVK into a Wine prefix (SPEC-KERNEL §17.2)."""
    return install_into_prefix("dxvk", prefix, **kwargs)  # type: ignore[arg-type]


def install_vkd3d(prefix: Path, **kwargs: object) -> InstallResult:
    """Install/refresh VKD3D-Proton into a Wine prefix (SPEC-KERNEL §17.2)."""
    return install_into_prefix("vkd3d", prefix, **kwargs)  # type: ignore[arg-type]
