"""build/chroot/76-mint-purge.sh (and the lib.sh helpers it uses): Mint's own apps and artwork leave the image.

The hook is run for real (bash) with fake apt-get / apt-mark / dpkg-query / dpkg first in PATH (lib.sh test seam
LINDOS_HOOK_PATH_PREFIX) and a fake root (LINDOS_MINT_ROOT) for the few files it edits.  The fakes keep the set of
installed packages in a JSON file, answer `apt-get -s purge` with the packages that would go (plus the reverse
dependencies the test declares) and log every call, so the tests can read off what the hook asked apt to do.

What is proven: the keep-set and everything mint-meta-* depends on is marked manual before the first purge; the
metapackages, the artwork stack and the Mint apps are purged in guarded groups; a group whose simulation names
anything outside itself, a group whose replacement is missing, a failed purge and a silent simulation are all
skipped/warned about (MINT-PURGE-SKIPPED / MINT-PURGE-FAILED) without failing the build; the interim tools and the
chain Mint's Firefox pre-depends on are never touched; a second run does nothing.  Needs bash (skipped otherwise).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK_DIR = REPO_ROOT / "build" / "chroot"
HOOK = HOOK_DIR / "76-mint-purge.sh"
LIB = HOOK_DIR / "lib.sh"
DEBLOAT = HOOK_DIR / "10-debloat.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

FAKE_TOOLS = r'''
# sourced by every fake tool after it set TOOL; state directory: $LINDOS_FAKE_STATE
#   installed.txt  name#depends#recommends        rdeps.txt  name#reverse dependencies (space separated)
#   manual.txt     packages apt-mark manual saw   log.txt    "tool args" of every call
#   sim_empty (file)  apt-get -s purge gives no answer     fail_purge.txt  packages whose real purge fails
#   audit.txt      what 'dpkg --audit' prints
#   unneeded.txt   packages 'apt-get -s autoremove' lists (unless apt-mark manual saw them)
#   orphans.txt    name#packages that a purge of NAME leaves unneeded: 'apt-get -s --auto-remove purge' lists them
#   nomanual.txt   packages whose 'apt-mark manual' is silently ignored (the mark never sticks)
#   flaky.txt      packages whose FIRST 'apt-mark manual' is silently ignored
S="${LINDOS_FAKE_STATE}"
printf '%s %s\n' "${TOOL}" "$*" >> "${S}/log.txt"

is_installed() { local n; while IFS='#' read -r n _; do [ "${n}" = "$1" ] && return 0; done < "${S}/installed.txt"; return 1; }
in_list() { local x="$1" y; shift; for y in "$@"; do [ "${x}" = "${y}" ] && return 0; done; return 1; }

dq() {
    local fmt="" a pats=() rc=0 name dep rec p hit out
    for a in "$@"; do
        case "${a}" in -f=*) fmt="${a#-f=}" ;; -*) ;; *) pats+=("${a}") ;; esac
    done
    emit() {
        out="${fmt//\$\{db:Status-Status\}/installed}"
        out="${out//\$\{Package\}/$1}"
        out="${out//\$\{Depends\}/$2}"
        out="${out//\$\{Recommends\}/$3}"
        printf '%b' "${out}"
    }
    if [ "${#pats[@]}" -eq 0 ]; then
        while IFS='#' read -r name dep rec; do emit "${name}" "${dep}" "${rec}"; done < "${S}/installed.txt"
        return 0
    fi
    for p in "${pats[@]}"; do
        hit=0
        while IFS='#' read -r name dep rec; do
            # shellcheck disable=SC2053
            if [[ "${name}" == ${p} ]]; then hit=1; emit "${name}" "${dep}" "${rec}"; fi
        done < "${S}/installed.txt"
        if [ "${hit}" -eq 0 ]; then printf 'dpkg-query: no packages found matching %s\n' "${p}" >&2; rc=1; fi
    done
    return "${rc}"
}

is_manual() { grep -qx -- "$1" "${S}/manual.txt" 2>/dev/null; }

ag() {
    local sim=0 auto=0 pos=() skip=0 a names=() gone=() n r k v line name o
    for a in "$@"; do
        if [ "${skip}" = 1 ]; then skip=0; continue; fi
        case "${a}" in -o) skip=1 ;; -s|--simulate) sim=1 ;; --auto-remove|--autoremove) auto=1 ;; -*) ;; *) pos+=("${a}") ;; esac
    done
    if [ "${pos[0]:-}" = "autoremove" ]; then
        if [ "${sim}" = 1 ] && [ -f "${S}/unneeded.txt" ]; then
            while IFS= read -r n; do
                if [ -n "${n}" ] && is_installed "${n}" && ! is_manual "${n}"; then printf 'Remv %s [1.0]\n' "${n}"; fi
            done < "${S}/unneeded.txt"
        fi
        return 0
    fi
    [ "${pos[0]:-}" = "purge" ] || return 0
    names=("${pos[@]:1}")
    if [ "${sim}" = 1 ] && [ -e "${S}/sim_empty" ]; then return 100; fi
    for n in "${names[@]}"; do
        if is_installed "${n}"; then gone+=("${n}"); fi
    done
    for n in "${names[@]}"; do
        [ -f "${S}/rdeps.txt" ] || continue
        while IFS='#' read -r k v; do
            [ "${k}" = "${n}" ] || continue
            for r in ${v}; do
                if is_installed "${r}" && ! in_list "${r}" "${gone[@]}"; then gone+=("${r}"); fi
            done
        done < "${S}/rdeps.txt"
    done
    if [ "${sim}" = 1 ]; then
        for n in "${gone[@]}"; do printf 'Purg %s [1.0] \n' "${n}"; done
        if [ "${auto}" = 1 ] && [ -f "${S}/orphans.txt" ]; then
            for n in "${names[@]}"; do
                while IFS='#' read -r k v; do
                    [ "${k}" = "${n}" ] || continue
                    for o in ${v}; do
                        if is_installed "${o}" && ! is_manual "${o}" && ! in_list "${o}" "${gone[@]}"; then printf 'Remv %s [1.0]\n' "${o}"; fi
                    done
                done < "${S}/orphans.txt"
            done
        fi
        return 0
    fi
    if [ -f "${S}/fail_purge.txt" ]; then
        for n in "${names[@]}"; do
            if grep -qx "${n}" "${S}/fail_purge.txt"; then
                echo "E: Sub-process /usr/bin/dpkg returned an error code (1)" >&2
                return 100
            fi
        done
    fi
    : > "${S}/installed.new"
    while IFS= read -r line; do
        name="${line%%#*}"
        in_list "${name}" "${gone[@]}" || printf '%s\n' "${line}" >> "${S}/installed.new"
    done < "${S}/installed.txt"
    mv -f "${S}/installed.new" "${S}/installed.txt"
    return 0
}

am() {
    [ "${1:-}" = manual ] || return 0
    shift
    local n
    for n in "$@"; do
        if ! is_installed "${n}"; then echo "E: Unable to locate package ${n}" >&2; return 100; fi
    done
    for n in "$@"; do
        if grep -qx -- "${n}" "${S}/nomanual.txt" 2>/dev/null; then continue; fi
        if grep -qx -- "${n}" "${S}/flaky.txt" 2>/dev/null && ! grep -qx -- "${n}" "${S}/flaky.seen" 2>/dev/null; then
            printf '%s\n' "${n}" >> "${S}/flaky.seen"; continue      # the first mark of a flaky package does not stick
        fi
        printf '%s\n' "${n}" >> "${S}/manual.txt"
    done
    return 0
}

dp() {
    if [ "${1:-}" = "--audit" ] && [ -f "${S}/audit.txt" ]; then cat "${S}/audit.txt"; fi
    return 0
}

case "${TOOL}" in
    dpkg-query) dq "$@" ;;
    apt-get) ag "$@" ;;
    apt-mark) am "$@" ;;
    dpkg) dp "$@" ;;
esac
exit $?
'''


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _image() -> Dict[str, Dict[str, str]]:
    """A cut-down Mint 22.2 XFCE image: what the research found installed, with the metapackages' real shape."""
    img: Dict[str, Dict[str, str]] = {}

    def add(names: str, depends: str = "", recommends: str = "") -> None:
        for n in names.split():
            img[n] = {"depends": depends, "recommends": recommends}

    add("mint-meta-core", "linuxmint-keyring, mint-info-xfce, mint-artwork, mintsystem, ubuntu-system-adjustments, "
        "plymouth-theme-ubuntu-text, mintbackup, mintinstall (>= 8.0), mintupdate, mintwelcome, mintsources, mintdrivers, "
        "mintreport, mintstick", "thingy")
    add("mint-meta-xfce", "mint-meta-core, thunar, xfce4-terminal, mintdesktop, mugshot, xfce4-xapp-status-plugin | xfce4-foo, "
        "xfce4-eyes-plugin, xfce4-systemload-plugin:amd64, file-roller (>= 1.0), gnome-calculator")
    add("mint-meta-codecs", "libavcodec-extra")
    # kept: apt trust and the mintsystem chain Mint's Firefox pre-depends, the interim tools, the desktop parts
    add("linuxmint-keyring mint-info-xfce mintsystem ubuntu-system-adjustments plymouth-theme-ubuntu-text mint-common "
        "mint-translations aptkit mintinstall mintupdate mintsources mintdrivers mintreport mintlocale libavcodec-extra "
        "thunar xfce4-terminal mugshot xfce4-xapp-status-plugin file-roller gnome-calculator firefox ubiquity casper libc6 "
        "mousepad ristretto evince vlc")
    # the artwork stack
    add("mint-artwork mint-themes mint-x-icons mint-y-icons mint-cursor-themes mint-backgrounds-xia mint-backgrounds-wilma")
    # Mint's apps that simply go
    add("mintbackup mintwelcome captain mintstick mintdesktop lightdm-settings fingwit libpam-fingwit mint-upgrade-info "
        "mintchat webapp-manager warpinator sticky neofetch xfce4-eyes-plugin xfce4-systemload-plugin")
    # apps with a replacement (mousepad, ristretto, evince, vlc above)
    add("xed xed-common xed-dbg xviewer xviewer-plugins gir1.2-xviewer-3.0 pix pix-data thingy xreader xreader-common "
        "libxreaderdocument3 celluloid")
    return img


GONE = ("mint-meta-core mint-meta-xfce mint-artwork mint-themes mint-x-icons mint-y-icons mint-cursor-themes "
        "mint-backgrounds-xia mint-backgrounds-wilma mintbackup mintwelcome captain mintstick mintdesktop lightdm-settings "
        "fingwit libpam-fingwit mint-upgrade-info mintchat webapp-manager warpinator sticky neofetch xfce4-eyes-plugin "
        "xfce4-systemload-plugin xed xed-common xed-dbg xviewer xviewer-plugins gir1.2-xviewer-3.0 pix pix-data thingy "
        "xreader xreader-common libxreaderdocument3 celluloid").split()
KEPT = ("mint-meta-codecs mintupdate mintinstall mintdrivers mintsources mintreport mintlocale mintsystem mint-common "
        "mint-info-xfce mint-translations ubuntu-system-adjustments linuxmint-keyring plymouth-theme-ubuntu-text aptkit "
        "thunar xfce4-terminal mugshot xfce4-xapp-status-plugin file-roller gnome-calculator firefox ubiquity casper libc6 "
        "libavcodec-extra mousepad ristretto evince vlc").split()


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, state: Dict[str, Any], root: Path) -> None:
        self.proc, self.state, self.root = proc, state, root
        self.stderr = proc.stderr

    @property
    def installed(self) -> List[str]:
        return sorted(self.state["installed"])

    def calls(self, tool: str) -> List[List[str]]:
        return [c["args"] for c in self.state["log"] if c["tool"] == tool]

    def purges(self, simulated: bool = False) -> List[List[str]]:
        """Package lists of the (simulated) 'apt-get ... purge PKG...' calls, in order."""
        out = []
        for args in self.calls("apt-get"):
            if "purge" not in args:
                continue
            if ("-s" in args) != simulated:
                continue
            out.append(args[args.index("purge") + 1:])
        return out


def _write_state(sd: Path, installed: Dict[str, Dict[str, str]], extra: Dict[str, Any]) -> None:
    sd.mkdir(parents=True, exist_ok=True)
    rows = ["%s#%s#%s" % (n, i.get("depends", ""), i.get("recommends", "")) for n, i in installed.items()]
    (sd / "installed.txt").write_bytes(("\n".join(rows) + "\n").encode("utf-8"))
    for f in ("log.txt", "manual.txt"):
        (sd / f).write_bytes(b"")
    if extra.get("rdeps"):
        (sd / "rdeps.txt").write_bytes(("\n".join("%s#%s" % (k, " ".join(v)) for k, v in extra["rdeps"].items()) + "\n").encode("utf-8"))
    if extra.get("sim_empty"):
        (sd / "sim_empty").write_bytes(b"")
    if extra.get("fail_purge"):
        (sd / "fail_purge.txt").write_bytes(("\n".join(extra["fail_purge"]) + "\n").encode("utf-8"))
    if extra.get("audit"):
        (sd / "audit.txt").write_bytes(extra["audit"].encode("utf-8"))
    if extra.get("unneeded"):
        (sd / "unneeded.txt").write_bytes(("\n".join(extra["unneeded"]) + "\n").encode("utf-8"))
    if extra.get("orphans"):
        (sd / "orphans.txt").write_bytes(("\n".join("%s#%s" % (k, " ".join(v)) for k, v in extra["orphans"].items()) + "\n").encode("utf-8"))
    if extra.get("nomanual"):
        (sd / "nomanual.txt").write_bytes(("\n".join(extra["nomanual"]) + "\n").encode("utf-8"))
    if extra.get("flaky"):
        (sd / "flaky.txt").write_bytes(("\n".join(extra["flaky"]) + "\n").encode("utf-8"))


def _read_state(sd: Path) -> Dict[str, Any]:
    installed: Dict[str, Dict[str, str]] = {}
    for row in (sd / "installed.txt").read_text(encoding="utf-8").splitlines():
        if row:
            name, dep, rec = (row.split("#") + ["", ""])[:3]
            installed[name] = {"depends": dep, "recommends": rec}
    log = []
    for row in (sd / "log.txt").read_text(encoding="utf-8").splitlines():
        parts = row.split()
        if parts:
            log.append({"tool": parts[0], "args": parts[1:]})
    manual = [ln for ln in (sd / "manual.txt").read_text(encoding="utf-8").splitlines() if ln]
    return {"installed": installed, "log": log, "manual": manual}


def _make_fakes(tmp: Path, installed: Dict[str, Dict[str, str]], extra: Dict[str, Any]) -> Dict[str, str]:
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    tools = tmp / "fake_tools.sh"
    tools.write_bytes(FAKE_TOOLS.encode("utf-8"))
    sd = tmp / "fakestate"
    _write_state(sd, installed, extra)
    for tool in ("dpkg-query", "apt-get", "apt-mark", "dpkg"):
        w = fake / tool
        w.write_bytes(('#!/bin/bash\nTOOL=%s\n. "%s"\n' % (tool, _posix(tools))).encode("utf-8"))
        os.chmod(w, 0o755)
    return {"LINDOS_HOOK_PATH_PREFIX": _posix(fake), "LINDOS_FAKE_STATE": _posix(sd)}


def run_hook(tmp: Path, *, installed: Optional[Dict[str, Dict[str, str]]] = None, extra_state: Optional[Dict[str, Any]] = None,
             env_extra: Optional[Dict[str, str]] = None, root: Optional[Path] = None, hook: Path = HOOK) -> Run:
    assert BASH is not None
    root = root if root is not None else tmp / "root"
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(_make_fakes(tmp, installed if installed is not None else _image(), extra_state or {}))
    config = tmp / "config.env"
    config.write_bytes(b"# empty on purpose: lib.sh falls back to its own apt defaults\n")
    env.update({"LINDOS_MINT_ROOT": _posix(root), "LINDOS_STAGE_DIR": _posix(tmp / "no-such-stage"),
                "LINDOS_CONFIG_ENV": _posix(config)})
    env.update(env_extra or {})
    proc = subprocess.run([BASH, "-Eeuo", "pipefail", _posix(hook)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, check=False, env=env)
    return Run(proc, _read_state(tmp / "fakestate"), root)


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """One run over the whole fake image, shared by the tests that only read its outcome (a run is slow on Windows)."""
    if BASH is None:
        pytest.skip("bash not available on this host")
    return run_hook(tmp_path_factory.mktemp("full"))


def _small_image() -> Dict[str, Dict[str, str]]:
    """Just enough for the scenario tests: the metapackages, the artwork stack, two apps and a keeper."""
    img = _image()
    keep = ("mint-meta-core mint-meta-xfce mint-artwork mint-themes mintbackup mintstick mintupdate thunar mousepad "
            "linuxmint-keyring mintsystem ubuntu-system-adjustments mint-info-xfce plymouth-theme-ubuntu-text mintinstall "
            "mintsources mintdrivers mintreport mintwelcome captain xfce4-terminal mugshot xfce4-xapp-status-plugin "
            "file-roller gnome-calculator").split()
    return {n: v for n, v in img.items() if n in keep}


# --------------------------------------------------------------------------- structure
def test_hook_house_style() -> None:
    raw = HOOK.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw
    t = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in t and "hook_begin" in t and "hook_end" in t
    assert re.search(r'^\. "\$\(dirname "\$\(readlink -f "\$0"\)"\)/lib\.sh"$', t, flags=re.M)
    assert re.search(r"^\s*(sudo|pkexec)\s", t, flags=re.M) is None
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t and "die " in t, "must refuse to run on a build host"
    body = "\n".join(ln for ln in t.splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in ("dpkg-divert", "apt-get remove", "sources.list", "partman", "grub-installer"):
        assert forbidden not in body, forbidden
    # it never runs an autoremove (it asks lib.sh's unneeded_pkgs, a simulation, and 'apt-get -s --auto-remove purge')
    assert not [ln for ln in body.splitlines() if "apt-get" in ln and "autoremove" in ln]
    assert "apt-get -s --auto-remove purge" in body and "unneeded_pkgs" in body


def test_hook_runs_after_every_package_hook_and_before_the_sweep() -> None:
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    i = names.index(HOOK.name)
    before, after = names[:i], names[i + 1:]
    for h in ("00-repos.sh", "10-debloat.sh", "20-base.sh", "30-lindos-debs.sh", "40-theme.sh", "50-tune.sh",
              "60-compat.sh", "70-gaming.sh", "75-vm.sh"):
        assert h in before, f"{HOOK.name} must run after {h}"
    for h in ("77-mint-sweep.sh", "78-installer-brand.sh", "79-installer-flow.sh", "80-cleanup.sh"):
        assert h in after, f"{HOOK.name} must run before {h}"


def _find_shellcheck() -> Optional[str]:
    found = shutil.which("shellcheck")
    if found:
        return found
    import sysconfig
    for scheme in (None, "nt_user", "posix_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        for name in ("shellcheck", "shellcheck.exe"):
            cand = os.path.join(d or "", name)
            if d and os.path.isfile(cand):
                return cand
    return None


@needs_bash
@pytest.mark.parametrize("script", [HOOK, LIB, DEBLOAT, HOOK_DIR / "20-base.sh", HOOK_DIR / "40-theme.sh",
                                    HOOK_DIR / "81-unrecognisable-gate.sh", HOOK_DIR / "82-session-sanity.sh",
                                    REPO_ROOT / "build" / "fetch-assets.sh"],
                         ids=lambda p: p.name)
def test_syntax_and_shellcheck(script: Path) -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode == 0, res.stderr
    sc = _find_shellcheck()
    if sc is None:
        pytest.skip("shellcheck not installed (tests/run.sh lints the hooks when it is)")
    res = subprocess.run([sc, "-S", "warning", "-e", "SC1090,SC1091", "-x", str(script)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120, cwd=str(REPO_ROOT))
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- behaviour
@needs_bash
def test_full_run_purges_mint_apps_and_artwork_and_keeps_everything_else(full_run: Run) -> None:
    run = full_run
    assert run.proc.returncode == 0, run.stderr
    for n in GONE:
        assert n not in run.installed, f"{n} should be purged"
    for n in KEPT:
        assert n in run.installed, f"{n} must survive the purge"
    assert "MINT-PURGE-SKIPPED" not in run.stderr and "MINT-PURGE-FAILED" not in run.stderr, run.stderr
    assert "MINT-PURGE-RESULT purged=%d skipped=0 failed=0" % len(GONE) in run.stderr, run.stderr
    # nothing but purges (and the dpkg audit's repair calls) touched apt
    for args in run.calls("apt-get"):
        assert "purge" in args or "-f" in args or ("-s" in args and "autoremove" in args), args   # (the last one: the session guard)
    purged_names = {n for group in run.purges() for n in group}
    assert purged_names == set(GONE)
    assert not purged_names & set(KEPT)


@needs_bash
def test_purge_order_metapackages_then_artwork_then_apps_then_replaced_apps(full_run: Run) -> None:
    run = full_run
    groups = run.purges()
    flat = [n for g in groups for n in g]
    assert groups[0] == ["mint-meta-core", "mint-meta-xfce"], groups[0]
    assert "mint-meta-codecs" not in flat, "purging it would orphan the codecs it pulled in"
    art = flat.index("mint-artwork")
    assert flat.index("mint-meta-xfce") < art < flat.index("mintbackup") < flat.index("xed") < flat.index("celluloid")
    assert flat.index("thingy") < flat.index("xreader") or {"thingy", "xreader"} <= set(next(g for g in groups if "thingy" in g))


@needs_bash
def test_keep_set_and_metapackage_dependencies_are_marked_manual_before_the_first_purge(full_run: Run) -> None:
    run = full_run
    log = run.state["log"]
    first_purge = next(i for i, c in enumerate(log) if c["tool"] == "apt-get" and "purge" in c["args"] and "-s" not in c["args"])
    marks = [i for i, c in enumerate(log) if c["tool"] == "apt-mark"]
    assert marks and max(marks) < first_purge, "every apt-mark manual call comes before the first real purge"
    manual = set(run.state["manual"])
    # explicit keep-set
    for n in ("mintupdate", "mintinstall", "mintdrivers", "mintsources", "mintreport", "mintsystem", "ubuntu-system-adjustments",
              "linuxmint-keyring", "mint-info-xfce", "mintlocale", "casper", "ubiquity", "mousepad", "vlc"):
        assert n in manual, n
    # what the metapackages depend on: version constraints, alternatives and arch qualifiers parsed away
    for n in ("thunar", "xfce4-terminal", "mugshot", "xfce4-xapp-status-plugin", "file-roller", "gnome-calculator",
              "xfce4-systemload-plugin", "libavcodec-extra"):
        assert n in manual, n
    assert "xfce4-foo" not in manual, "apt-mark on a package that is not installed would fail the whole call"
    assert "apt-mark manual failed" not in run.stderr


@needs_bash
def test_a_group_that_would_take_something_else_along_is_skipped_loudly_and_the_rest_goes_on(tmp_path: Path) -> None:
    img = _small_image()
    img["mint-mystery"] = {}
    run = run_hook(tmp_path, installed=img, extra_state={"rdeps": {"mintbackup": ["mint-mystery"]}})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-PURGE-SKIPPED group 'backup'" in run.stderr and "would also remove: mint-mystery" in run.stderr
    assert "mintbackup" in run.installed and "mint-mystery" in run.installed
    assert ["mintbackup"] not in run.purges()
    for n in ("mint-artwork", "mintstick", "mintwelcome"):
        assert n not in run.installed, "the other groups are not held up by one skipped group"
    assert re.search(r"MINT-PURGE-RESULT purged=\d+ skipped=\d+ failed=0", run.stderr)
    assert "backup" in re.search(r"MINT-PURGE-SKIPPED groups: (.*)", run.stderr).group(1).split()


@needs_bash
def test_a_missing_replacement_keeps_the_mint_app(tmp_path: Path) -> None:
    img = _image()
    del img["mousepad"], img["evince"]
    run = run_hook(tmp_path, installed=img)
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-PURGE-SKIPPED group 'text-editor'" in run.stderr and "replacement 'mousepad' is not installed" in run.stderr
    assert "MINT-PURGE-SKIPPED group 'pdf-viewer'" in run.stderr
    for n in ("xed", "xreader", "thingy"):
        assert n in run.installed
    for n in ("xviewer", "pix", "celluloid", "mint-artwork"):
        assert n not in run.installed
    assert "MINT-DEFAULT-APP-MISSING: mousepad" in run.stderr and "MINT-DEFAULT-APP-MISSING: evince" in run.stderr


@needs_bash
def test_no_answer_from_the_simulation_skips_every_group(tmp_path: Path) -> None:
    run = run_hook(tmp_path, installed=_small_image(), extra_state={"sim_empty": True})
    assert run.proc.returncode == 0, run.stderr
    assert not run.purges(), "nothing is purged without a simulation that says what would go"
    assert "gave no answer" in run.stderr and "MINT-PURGE-SKIPPED" in run.stderr
    assert "mint-artwork" in run.installed


@needs_bash
def test_a_failed_purge_is_a_warning_and_the_next_groups_still_run(tmp_path: Path) -> None:
    run = run_hook(tmp_path, installed=_small_image(), extra_state={"fail_purge": ["mintstick"]})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-PURGE-FAILED group 'usb-writer'" in run.stderr
    assert "mintstick" in run.installed and "mintbackup" not in run.installed and "mint-artwork" not in run.installed
    assert "MINT-PURGE-RESULT" in run.stderr and "failed=1" in run.stderr


@needs_bash
def test_a_broken_dpkg_after_the_purges_is_repaired(tmp_path: Path) -> None:
    run = run_hook(tmp_path, installed=_small_image(),
                   extra_state={"audit": "The following packages are only half configured:\n xed\n"})
    assert run.proc.returncode == 0
    assert "MINT-PURGE-DPKG-AUDIT" in run.stderr
    assert ["--configure", "-a"] in run.calls("dpkg")
    assert any("-f" in a and "install" in a for a in run.calls("apt-get"))


@needs_bash
def test_a_second_run_finds_nothing_to_do(tmp_path: Path, full_run: Run) -> None:
    second = run_hook(tmp_path, installed=full_run.state["installed"])
    assert second.proc.returncode == 0, second.stderr
    assert not second.purges() and not second.purges(simulated=True)
    assert "MINT-PURGE-RESULT purged=0 skipped=0 failed=0" in second.stderr
    assert sorted(second.installed) == sorted(full_run.installed)


@needs_bash
def test_switched_off_the_hook_never_asks_apt_anything(tmp_path: Path) -> None:
    run = run_hook(tmp_path, installed=_small_image(), env_extra={"LINDOS_MINT_PURGE": "0"})
    assert run.proc.returncode == 0
    assert "the Mint purge is switched off" in run.stderr
    assert not run.calls("apt-get") and not run.calls("apt-mark")
    assert "mint-artwork" in run.installed


@needs_bash
def test_the_matrix_web_app_leaves_the_skeleton(tmp_path: Path) -> None:
    root = tmp_path / "root"
    apps = root / "etc" / "skel" / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "webapp-OnlineChat4519.desktop").write_bytes(
        b"[Desktop Entry]\nName=Matrix\nExec=mintchat\nIcon=mintchat\nX-WebApp-URL=https://www.linuxmint.com/matrix.php\n")
    (apps / "webapp-Mine.desktop").write_bytes(b"[Desktop Entry]\nName=Mine\nExec=firefox https://example.org\n")
    run = run_hook(tmp_path, installed={"libc6": {}}, root=root)
    assert run.proc.returncode == 0, run.stderr
    assert not (apps / "webapp-OnlineChat4519.desktop").exists()
    assert (apps / "webapp-Mine.desktop").exists(), "only the Mint entry goes"


@needs_bash
def test_a_leftover_xdg_symlink_into_mint_artwork_is_moved_aside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "usr" / "share" / "mint-artwork" / "xfce"
    target.mkdir(parents=True)
    (root / "etc" / "xdg").mkdir(parents=True)
    try:
        os.symlink(str(target), str(root / "etc" / "xdg" / "xdg-xfce"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this host")
    run = run_hook(tmp_path, installed={"libc6": {}}, root=root)
    assert run.proc.returncode == 0, run.stderr
    link = root / "etc" / "xdg" / "xdg-xfce"
    assert "MINT-XDG-FIXED" in run.stderr
    assert not link.is_symlink() and link.is_dir(), "an empty directory replaces the symlink"
    assert (root / "etc" / "xdg" / "xdg-xfce.lindos-orig").is_symlink()


# --------------------------------------------------------------------------- lib.sh
def _protect_re() -> "re.Pattern[str]":
    m = re.search(r"^AUTOREMOVE_PROTECT_RE='(.+)'$", _text(LIB), flags=re.M)
    assert m
    return re.compile(m.group(1))


@pytest.mark.parametrize("name", ["linuxmint-keyring", "mintupdate", "mintinstall", "mintdrivers", "mintsources", "mintreport",
                                  "mintsystem", "mint-common", "mint-info-xfce", "mint-translations", "ubuntu-system-adjustments",
                                  "gnome-keyring", "gnome-calculator", "gnome-system-tools", "mate-polkit", "thunar-volman",
                                  "xfce4-panel", "firefox", "casper", "ubiquity-frontend-gtk", "lindos-core", "file-roller",
                                  "mousepad", "vlc", "libreoffice-core", "timeshift"])
def test_autoremove_still_protects_what_must_stay(name: str) -> None:
    assert _protect_re().match(name), name


@pytest.mark.parametrize("name", ["mint-artwork", "mint-themes", "mint-y-icons", "mint-cursor-themes", "mint-backgrounds-xia",
                                  "mintbackup", "mintwelcome", "mintstick", "mint-upgrade-info", "mint-meta-xfce",
                                  "gnome-mines", "gnome-games", "mate-panel", "mate-desktop-common"])
def test_autoremove_no_longer_shields_the_mint_apps_and_blanket_gnome_mate_prefixes(name: str) -> None:
    assert not _protect_re().match(name), name


def test_lib_exposes_the_path_seam_and_the_metapackage_helpers() -> None:
    t = _text(LIB)
    assert 'LINDOS_HOOK_PATH_PREFIX:+${LINDOS_HOOK_PATH_PREFIX}:}/usr/local/sbin' in t
    for fn in ("pkgs_installed_matching()", "mark_manual_installed()", "meta_deps_installed()", "mark_meta_deps_manual()"):
        assert fn in t, fn


def test_debloat_marks_the_metapackage_dependencies_before_it_purges_anything() -> None:
    t = _text(DEBLOAT)
    assert t.index("mark_meta_deps_manual") < t.index('for pkg in "${PURGE_LIST[@]}"')
    protected = t.split("PROTECTED=")[1].split('"\n')[0]
    for interim in ("mintinstall", "mintupdate", "mintdrivers", "mintsources", "mintreport", "mintsystem",
                    "ubuntu-system-adjustments", "linuxmint-keyring", "thunderbird"):
        assert interim in protected.split(), interim
    for gone in ("mintwelcome", "warpinator", "celluloid"):
        assert gone not in protected.split(), f"{gone} is removed by 76-mint-purge.sh, not protected here"
    assert re.search(r'^hide_autostart "mintwelcome"$', t, flags=re.M)


def test_base_hook_installs_the_default_apps() -> None:
    t = _text(HOOK_DIR / "20-base.sh")
    nice = t.split("NICE=(")[1].split("\n)\n")[0]
    for pkg in ("mousepad", "ristretto", "evince", "vlc"):
        assert re.search(r"^\s*%s\s*$" % pkg, nice, flags=re.M), pkg


# --------------------------------------------------------------------------- the session packages (CI boot test of 7fc3aae)
SESSION = ("xfce4-session xfwm4 xfce4-panel xfdesktop4 xfconf xfce4-settings dbus-x11 dbus-user-session libpam-systemd lightdm "
           "slick-greeter xorg xserver-xorg-core network-manager plymouth").split()


def _with_session(img: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    out = dict(img)
    for n in SESSION:
        out.setdefault(n, {})
    return out


def _session_pkgs_from_lib() -> List[str]:
    m = re.search(r"^SESSION_PKGS=\((.*?)\)$", _text(LIB), flags=re.M | re.S)
    assert m
    return m.group(1).split()


def test_lib_names_the_session_packages_and_autoremove_protects_every_one() -> None:
    assert sorted(_session_pkgs_from_lib()) == sorted(SESSION)
    for n in SESSION:
        assert _protect_re().match(n), f"AUTOREMOVE_PROTECT_RE must protect {n}"
    for fn in ("session_pkg_re()", "mark_session_manual()", "unneeded_pkgs()"):
        assert fn in _text(LIB), fn


@needs_bash
def test_the_session_packages_are_marked_manual_before_the_first_purge(tmp_path: Path) -> None:
    run = run_hook(tmp_path, installed=_with_session(_small_image()))
    assert run.proc.returncode == 0, run.stderr
    log = run.state["log"]
    first_purge = next(i for i, c in enumerate(log) if c["tool"] == "apt-get" and "purge" in c["args"] and "-s" not in c["args"])
    marks = [i for i, c in enumerate(log) if c["tool"] == "apt-mark"]
    assert marks and max(marks) < first_purge
    for n in SESSION:
        assert n in run.state["manual"], f"{n} must be explicitly manual"
        assert n in run.installed
    assert "MINT-SESSION-OK" in run.stderr and "MINT-PURGE-SESSION-KEPT" not in run.stderr
    assert "mint-meta-xfce" not in run.installed


@needs_bash
def test_a_purge_that_would_leave_a_session_package_unneeded_is_skipped_when_apt_will_not_keep_it(tmp_path: Path) -> None:
    """mint-meta-xfce's removal would make dbus-x11 an autoremove candidate and apt-mark does not stick: the group stays."""
    img = _with_session(_small_image())
    run = run_hook(tmp_path, installed=img, extra_state={"orphans": {"mint-meta-xfce": ["dbus-x11"]}, "nomanual": ["dbus-x11"]})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-PURGE-SKIPPED group 'metapackages'" in run.stderr
    assert "autoremove candidates: dbus-x11" in run.stderr
    assert "mint-meta-xfce" in run.installed and "mint-meta-core" in run.installed
    assert ["mint-meta-core", "mint-meta-xfce"] not in run.purges()
    assert "mint-artwork" not in run.installed, "the other groups go on"


@needs_bash
def test_a_session_package_that_apt_would_still_autoremove_is_kept_manual_and_the_purge_goes_on(tmp_path: Path) -> None:
    """The first apt-mark of dbus-x11 did not stick; the guard sees it in the simulation, marks it again and purges."""
    img = _with_session(_small_image())
    run = run_hook(tmp_path, installed=img, extra_state={"orphans": {"mint-meta-xfce": ["dbus-x11"]}, "flaky": ["dbus-x11"]})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-PURGE-SESSION-KEPT group 'metapackages'" in run.stderr and "(dbus-x11)" in run.stderr
    assert "MINT-PURGE-SKIPPED group 'metapackages'" not in run.stderr
    assert "mint-meta-xfce" not in run.installed and "dbus-x11" in run.installed
    assert run.state["manual"].count("dbus-x11") == 1


@needs_bash
def test_the_final_check_keeps_a_session_package_apt_still_lists_and_never_fails_the_build(tmp_path: Path) -> None:
    img = _with_session(_small_image())
    run = run_hook(tmp_path, installed=img, extra_state={"unneeded": ["lightdm", "libfoo1"], "nomanual": ["lightdm"]})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-SESSION-KEPT: apt would autoremove session package(s) (lightdm)" in run.stderr
    assert "MINT-SESSION-AT-RISK" in run.stderr and "lightdm" in run.installed


@needs_bash
def test_the_final_check_is_quiet_when_the_session_packages_are_manual(tmp_path: Path) -> None:
    img = _with_session(_small_image())
    run = run_hook(tmp_path, installed=img, extra_state={"unneeded": ["xfce4-session", "xfwm4", "libfoo1"]})
    assert run.proc.returncode == 0, run.stderr
    assert "MINT-SESSION-OK" in run.stderr and "MINT-SESSION-KEPT" not in run.stderr
