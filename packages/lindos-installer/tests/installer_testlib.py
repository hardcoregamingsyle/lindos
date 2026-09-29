"""Test harness for the Lindos installer scripts: a fake target system and fake system commands.

Nothing here needs Linux, root, apt, dpkg, chroot or a network.  The hook and the finalizer are run for
real with bash against a scratch directory that plays /target; every system command is a small shim
first on PATH that records its call in ``calls.log`` and behaves according to ``FAKE_*`` variables.

What this can NOT show (real Linux only): unshare/mount/chroot behaviour, the real apt and dpkg, systemd,
debconf's real confmodule and Ubiquity's filter, GTK.  See the follow-ups of the installer stream.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parents[3]
PKG_ROOT = REPO / "packages" / "lindos-installer" / "root"
LIBEXEC = PKG_ROOT / "usr" / "libexec" / "lindos" / "installer"
SHARE = PKG_ROOT / "usr" / "share" / "lindos" / "installer"
PYLIB = REPO / "packages" / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"
CORE_LIBEXEC = REPO / "packages" / "lindos-core" / "root" / "usr" / "libexec" / "lindos"
COMPAT_SH = REPO / "packages" / "lindos-compat" / "root" / "usr" / "libexec" / "lindos" / "install-compat.sh"
GAMING_SH = REPO / "packages" / "lindos-gaming" / "root" / "usr" / "libexec" / "lindos" / "install-gaming.sh"
BROWSER_SH = CORE_LIBEXEC / "install-browser.sh"

BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

#: what "is installed" on the fake image at the start
BASE_PACKAGES = [
    "ubiquity", "ubiquity-frontend-gtk", "ubiquity-casper", "casper", "oem-config-x", "linux-image-6.14.0-lindos",
    "linux-headers-6.14.0-lindos", "linux-firmware", "grub-efi-amd64-signed", "grub-common", "shim-signed", "initramfs-tools",
    "plymouth", "libplymouth5", "firefox-locale-de", "language-pack-de", "libfoo", "libbar", "libreoffice-writer", "firefox",
    "flatpak", "apt", "dpkg",
]

#: oem's line in the fake /etc/shadow when the user left the temporary account's password empty
EMPTY_PASSWORD_SHADOW = "oem::19000:0:99999:7:::"

#: the shim behind every faked command (installed under many names, told apart by $0)
FAKE_COMMAND = r'''#!/bin/bash
name="$(basename "$0")"
name="${name%.sh}"
# Symlinks are unavailable on some hosts (Windows): a "link" is a regular file that holds its target.
case "${name}" in
    ln)
        args=()
        for a in "$@"; do case "${a}" in -s|-f|-sf|-fs) ;; *) args+=("${a}") ;; esac; done
        rm -f "${args[1]}"
        echo "LINK:${args[0]}" >"${args[1]}"
        exit 0 ;;
    readlink)
        if [ "$1" != "-f" ] && [ -f "$1" ] && [ "$(head -c 5 "$1")" = "LINK:" ]; then
            sed -n '1s/^LINK://p' "$1"
            exit 0
        fi
        exec /usr/bin/readlink "$@" ;;
esac
printf '%s %s\n' "${name}" "$*" >>"${FAKE_CALLS}"
st="${FAKE_STATE}"
installed="${st}/installed"
held="${st}/held"
touch "${installed}" "${held}"
[ -f "${FAKE_TARGET}/var/lib/lindos/installer-apt.conf" ] && cp -f "${FAKE_TARGET}/var/lib/lindos/installer-apt.conf" "${st}/apt.conf.seen"

if [ "${FAKE_ALL_FAIL:-0}" = 1 ]; then
    case "${name}" in
        wget|curl|getent|systemd-inhibit|python3|id|mount|chroot|unshare|sync) ;;
        dpkg) [ "$1" = "--print-architecture" ] || exit 1 ;;
        *) exit 1 ;;
    esac
fi

pkgs_from_args() {
    local skip=0 a
    for a in "$@"; do
        if [ "${skip}" = 1 ]; then skip=0; continue; fi
        case "${a}" in
            -o) skip=1 ;;
            -*) ;;
            install|update|upgrade|clean|remove|purge|hold|unhold|policy) ;;
            *) printf '%s\n' "${a}" ;;
        esac
    done
}

case "${name}" in
    wget|curl)
        [ -e "${st}/net-down" ] && exit 4
        exit "${FAKE_NET_RC:-0}" ;;
    getent)
        if [ "${FAKE_DNS_BROKEN:-0}" = 1 ] && ! grep -q nameserver "${FAKE_TARGET}/etc/resolv.conf" 2>/dev/null; then
            exit 2
        fi
        echo "91.189.91.83 archive.ubuntu.com"
        exit 0 ;;
    id)
        if [ "$1" = "-u" ]; then echo 0; else echo root; fi
        exit 0 ;;
    sync|mount)
        exit 0 ;;
    lspci)
        printf '%s\n' "${FAKE_LSPCI-}"
        exit 0 ;;
    mokutil)
        [ -n "${FAKE_SB:-}" ] || exit 1
        echo "${FAKE_SB}"
        exit 0 ;;
    ubuntu-drivers)
        [ -z "${FAKE_UBUNTU_DRIVERS_OUT:-}" ] || echo "${FAKE_UBUNTU_DRIVERS_OUT}"
        exit "${FAKE_UBUNTU_DRIVERS_RC:-0}" ;;
    lindos-drivers)
        exit "${FAKE_LINDOS_DRIVERS_RC:-0}" ;;
    passwd)
        [ "${FAKE_PASSWD_RC:-0}" = 0 ] || exit "${FAKE_PASSWD_RC}"
        # 'passwd -l USER' prefixes the password field with '!' (the fake target's /etc/shadow)
        if [ "$1" = "-l" ] && [ -f "${FAKE_TARGET}/etc/shadow" ]; then
            sed -i "s/^$2:/&!/" "${FAKE_TARGET}/etc/shadow"
        fi
        exit 0 ;;
    chpasswd)
        # 'USER:PASSWORD' arrives on stdin (the real one takes it there too); the fake keeps every line for the tests
        cat >>"${st}/chpasswd-stdin"
        [ "${FAKE_CHPASSWD_RC:-0}" = 0 ] || exit "${FAKE_CHPASSWD_RC}"
        if [ -f "${FAKE_TARGET}/etc/shadow" ]; then
            user="$(tail -n 1 "${st}/chpasswd-stdin" | cut -d: -f1)"
            hash='$6$fakesalt$fakehash'
            sed -i "s|^${user}:[^:]*:|${user}:${hash}:|" "${FAKE_TARGET}/etc/shadow"
        fi
        exit 0 ;;
    debconf-set-selections)
        env | grep '^DEBCONF_' >"${st}/debconf-env" || : >"${st}/debconf-env"
        cat >>"${st}/debconf-selections"
        exit "${FAKE_DEBCONF_RC:-0}" ;;
    chroot)
        shift
        exec "$@" ;;
    unshare)
        [ "${FAKE_UNSHARE_FAIL:-0}" = 1 ] && exit 97
        while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
        shift
        exec "$@" ;;
    systemd-inhibit)
        [ "${FAKE_INHIBIT_FAIL:-0}" = 1 ] && exit 1
        while [ $# -gt 0 ]; do
            case "$1" in --*) shift ;; *) break ;; esac
        done
        exec "$@" ;;
    systemctl)
        root=""; verb=""; rest=()
        for a in "$@"; do
            case "${a}" in
                --root=*) root="${a#--root=}" ;;
                enable|set-default|disable) verb="${a}" ;;
                *) rest+=("${a}") ;;
            esac
        done
        if [ "${verb}" = set-default ] && [ "${FAKE_SYSTEMCTL_NOOP:-0}" != 1 ]; then
            mkdir -p "${root}/etc/systemd/system"
            ln -sf "/lib/systemd/system/${rest[0]}" "${root}/etc/systemd/system/default.target"
        fi
        exit "${FAKE_SYSTEMCTL_RC:-0}" ;;
    flatpak)
        fp="${st}/flatpaks"; touch "${fp}"
        case "$1" in
            remote-add) exit "${FAKE_FLATPAK_REMOTE_RC:-0}" ;;
            install)
                id="${!#}"
                case " ${FAKE_FLATPAK_FAIL:-} " in *" ${id} "*) exit 1 ;; esac
                echo "${id}" >>"${fp}"
                exit 0 ;;
            info)
                id="${!#}"
                grep -qxF "${id}" "${fp}" && exit 0
                exit 1 ;;
        esac
        exit 0 ;;
    apt-mark)
        [ "${FAKE_APT_MARK_FAIL:-0}" = 1 ] && exit 1
        verb="$1"; shift
        case "${verb}" in
            hold)
                for p in "$@"; do grep -qxF "${p}" "${held}" || echo "${p}" >>"${held}"; done ;;
            unhold)
                for p in "$@"; do grep -vxF "${p}" "${held}" >"${held}.tmp"; mv -f "${held}.tmp" "${held}"; done ;;
        esac
        exit 0 ;;
    apt-cache)
        for p in $(pkgs_from_args "$@"); do
            printf '%s:\n  Installed: (none)\n' "${p}"
            case " ${FAKE_NO_CANDIDATE:-} " in
                *" ${p} "*) printf '  Candidate: (none)\n' ;;
                *) printf '  Candidate: 1.0\n' ;;
            esac
        done
        exit 0 ;;
    dpkg-query)
        fmt=""; pk=""
        for a in "$@"; do
            case "${a}" in -f=*) fmt="${a#-f=}" ;; -*) ;; *) pk="${a}" ;; esac
        done
        case "${fmt}" in
            *'${Version}'*)
                printf '%s' "${FAKE_UBIQUITY_VERSION-24.04.3+mint18}"
                exit 0 ;;
            *'${binary:Package} ${db:Status-Want}'*)
                while read -r p; do [ -n "${p}" ] && echo "${p} install installed"; done <"${installed}"
                exit 0 ;;
            *'${Package}'*)
                cat "${installed}"
                exit 0 ;;
            *'${db:Status-Status}'*)
                if grep -qxF "${pk}" "${installed}"; then printf 'installed'; exit 0; fi
                printf 'not-installed'
                exit 1 ;;
        esac
        exit 0 ;;
    dpkg)
        case "$1" in
            --print-architecture) echo amd64; exit 0 ;;
            --print-foreign-architectures) echo i386; exit 0 ;;
            --audit) [ -s "${st}/audit" ] && cat "${st}/audit"; exit 0 ;;
            --configure) [ "${FAKE_CONFIGURE_FAILS:-0}" = 1 ] && exit 1; : >"${st}/audit"; exit 0 ;;
            --remove|--purge) : >"${st}/audit"; exit 0 ;;
            -i)
                if [ "${FAKE_DPKG_INSTALLS_OEM:-0}" = 1 ]; then
                    mkdir -p "${FAKE_TARGET}/usr/lib/oem-config" "${FAKE_TARGET}/usr/sbin"
                    printf '[Service]\nExecStart=/usr/sbin/oem-config-firstboot\n' >"${FAKE_TARGET}/usr/lib/oem-config/oem-config.service"
                    printf '[Unit]\nDescription=oem-config\n' >"${FAKE_TARGET}/usr/lib/oem-config/oem-config.target"
                    printf '#!/bin/sh\n' >"${FAKE_TARGET}/usr/sbin/oem-config-firstboot"
                fi
                exit 0 ;;
        esac
        exit 0 ;;
    apt-get)
        verb=""; dl=0; sim=0; fix=0
        for a in "$@"; do
            case "${a}" in
                update|upgrade|install|clean) [ -n "${verb}" ] || verb="${a}" ;;
                -d) dl=1 ;;
                -s) sim=1 ;;
                -f) fix=1 ;;
            esac
        done
        case "${verb}" in
            update)
                # every call is counted; FAKE_UPDATE_OUT_FILE is what apt printed (only on the first call with
                # FAKE_UPDATE_OUT_ONCE=1), FAKE_UPDATE_RC_FIRST the exit status of the first call only
                n=0
                [ -f "${st}/update-calls" ] && n="$(cat "${st}/update-calls")"
                n=$((n + 1))
                echo "${n}" >"${st}/update-calls"
                if [ -n "${FAKE_UPDATE_OUT_FILE:-}" ] && { [ "${FAKE_UPDATE_OUT_ONCE:-0}" != 1 ] || [ "${n}" = 1 ]; }; then
                    cat "${FAKE_UPDATE_OUT_FILE}"
                fi
                if [ "${n}" = 1 ] && [ -n "${FAKE_UPDATE_RC_FIRST:-}" ]; then exit "${FAKE_UPDATE_RC_FIRST}"; fi
                exit "${FAKE_UPDATE_RC:-0}" ;;
            clean) exit 0 ;;
            upgrade)
                if [ "${sim}" = 1 ]; then
                    for p in ${FAKE_UPGRADES:-}; do
                        # apt keeps a held package back; FAKE_IGNORE_HOLDS=1 plays holds that did not take effect
                        if [ "${FAKE_IGNORE_HOLDS:-0}" != 1 ] && grep -qxF "${p}" "${held}"; then continue; fi
                        echo "Inst ${p} [1.0] (2.0 Ubuntu:24.04/noble-updates [amd64])"
                    done
                    exit "${FAKE_SIM_RC:-0}"
                fi
                if [ "${dl}" = 1 ]; then
                    if [ "${FAKE_KILL_HOOK:-0}" = 1 ]; then
                        kill -TERM "${LINDOS_INSTALLER_PID}"
                        sleep 5
                    fi
                    [ "${FAKE_SLEEP_UPGRADE:-0}" = 1 ] && sleep 12
                    exit "${FAKE_DL_RC:-0}"
                fi
                rc="${FAKE_INST_RC:-0}"
                if [ "${rc}" = 0 ]; then
                    for p in ${FAKE_UPGRADES:-}; do
                        grep -qxF "${p}" "${held}" || echo "${p}" >>"${st}/upgraded"
                    done
                fi
                exit "${rc}" ;;
            install)
                if [ "${fix}" = 1 ]; then exit "${FAKE_FIX_RC:-0}"; fi
                list="$(pkgs_from_args "$@")"
                for p in ${list}; do
                    case " ${FAKE_FAIL_PKGS:-} " in *" ${p} "*) exit 100 ;; esac
                done
                if [ "${dl}" = 1 ]; then
                    if [ "${FAKE_KILL_HOOK:-0}" = 1 ]; then
                        kill -TERM "${LINDOS_INSTALLER_PID}"
                        sleep 5
                    fi
                    for p in ${list}; do
                        case " ${FAKE_SLEEP_PKG:-} " in *" ${p} "*) sleep 12 ;; esac
                    done
                    # FAKE_DL_DROPS_NET=1: a failed download also takes the connection down (a lost network)
                    if [ "${FAKE_DL_RC:-0}" != 0 ] && [ "${FAKE_DL_DROPS_NET:-0}" = 1 ]; then touch "${st}/net-down"; fi
                    exit "${FAKE_DL_RC:-0}"
                fi
                rc="${FAKE_INST_RC:-0}"
                if [ "${rc}" = 0 ]; then
                    for p in ${list}; do grep -qxF "${p}" "${installed}" || echo "${p}" >>"${installed}"; done
                fi
                exit "${rc}" ;;
        esac
        exit 0 ;;
esac
exit 0
'''

#: names the fake command is installed under
FAKE_NAMES = ["ln", "readlink", "wget", "curl", "getent", "id", "sync", "mount", "lspci", "mokutil", "ubuntu-drivers", "lindos-drivers",
              "passwd", "chpasswd", "debconf-set-selections", "chroot", "unshare", "systemd-inhibit", "systemctl", "flatpak", "apt-mark", "apt-cache",
              "dpkg-query", "dpkg", "apt-get"]

#: LINDOS_TARGET_RUNNER: plays 'enter the target': the "chroot" is the host, absolute paths map into the fake target
RUNNER = r'''#!/bin/bash
t="$1"
shift
[ "${FAKE_ENTER_FAIL:-0}" = 1 ] && exit 97
cmd="$1"
shift
case "${cmd}" in
    /*) [ -e "${t}${cmd}" ] && cmd="${t}${cmd}" ;;
esac
exec "${cmd}" "$@"
'''

#: what lives in the fake target under usr/libexec/lindos (stand-ins for the real install scripts)
FAKE_INSTALL_SCRIPT = r'''#!/bin/bash
name="$(basename "$0")"
printf '%s %s\n' "${name}" "$*" >>"${FAKE_CALLS}"
[ "${FAKE_ALL_FAIL:-0}" = 1 ] && exit 1
key="$(printf '%s' "${name%.sh}" | tr 'a-z-' 'A-Z_')"
phase=inst
for a in "$@"; do
    case "${a}" in --download-only) phase=dl ;; esac
done
rc_var="FAKE_${key}_${phase^^}_RC"
rc="${!rc_var:-0}"
if [ "${phase}" = inst ] && [ "${rc}" = 0 ] && [ "${name}" = install-browser.sh ]; then
    echo google-chrome-stable >>"${FAKE_STATE}/installed"
fi
exit "${rc}"
'''

FAKE_BROWSER_FIRSTBOOT = r'''#!/bin/bash
printf '%s %s\n' "browser-firstboot.sh" "$*" >>"${FAKE_CALLS}"
[ "${FAKE_FIRSTBOOT_WRITES:-1}" = 1 ] && : >"${FAKE_TARGET}/var/lib/lindos/browser-firstboot.done"
exit 0
'''

FAKE_CONFMODULE = r'''# stand-in for /usr/share/debconf/confmodule: protocol on fd 3, stdout -> stderr, like the real one
exec 3>&1 1>&2
db_x_loadtemplatefile() { echo "X_LOADTEMPLATEFILE $*" >&3; return 0; }
db_subst() { echo "SUBST $*" >&3; return 0; }
db_progress() { echo "PROGRESS $*" >&3; return 0; }
db_get() {
    echo "GET $1" >&3
    case "$1" in
        ubiquity/use_nonfree) RET="${FAKE_USE_NONFREE:-false}" ;;
        mirror/http/proxy) RET="${FAKE_PROXY:-}" ;;
        *) RET="" ;;
    esac
    return 0
}
'''


def write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    path.chmod(0o755)
    return path


class Sandbox:
    """A scratch directory that plays the live system and /target."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.target = root / "target"
        self.bin = root / "bin"
        self.state = root / "state"
        self.live_log = root / "live" / "installer-hook.log"
        self.calls = root / "calls.log"
        self.cmdline = root / "cmdline"
        self.manifest_remove = root / "manifest-remove"
        self.resolv_source = root / "resolv-source.conf"
        self.confmodule = root / "confmodule"
        self.runner = self.bin / "lindos-runner"
        self.build()

    # -- construction ------------------------------------------------------------------------
    def build(self) -> None:
        for d in (self.bin, self.state, self.live_log.parent):
            d.mkdir(parents=True, exist_ok=True)
        for name in FAKE_NAMES:
            write_exec(self.bin / name, FAKE_COMMAND)
        write_exec(self.runner, RUNNER)
        self.calls.write_text("", encoding="utf-8")
        (self.state / "installed").write_text("\n".join(BASE_PACKAGES) + "\n", encoding="utf-8", newline="\n")
        self.cmdline.write_text("BOOT_IMAGE=/casper/vmlinuz boot=casper only-ubiquity quiet splash --\n", encoding="utf-8")
        self.manifest_remove.write_text("firefox-locale-de\nubiquity\nlanguage-pack-de:amd64\n", encoding="utf-8", newline="\n")
        self.resolv_source.write_text("nameserver 192.0.2.53\n", encoding="utf-8", newline="\n")
        self.confmodule.write_text(FAKE_CONFMODULE, encoding="utf-8", newline="\n")
        t = self.target
        for d in ("usr/bin", "usr/sbin", "usr/libexec/lindos", "usr/lib/ubiquity/target-config", "etc/lindos",
                  "etc/apt/preferences.d", "etc/lightdm", "var/lib/lindos", "var/lib/apt/lists", "var/log/lindos",
                  "usr/lib/oem-config", "home/oem"):
            (t / d).mkdir(parents=True, exist_ok=True)
        for f in ("usr/bin/dpkg", "usr/bin/apt-get", "usr/bin/ubuntu-drivers", "usr/bin/lindos-drivers", "usr/bin/flatpak"):
            write_exec(t / f, "#!/bin/sh\n")
        (t / "etc" / "lindos" / "system.json").write_text(
            json.dumps({"mode": "everyday", "browser": "chrome", "oem": False}), encoding="utf-8", newline="\n")
        (t / "etc" / "resolv.conf").write_text("# original resolv.conf\n", encoding="utf-8", newline="\n")
        (t / "var" / "lib" / "apt" / "lists" / "fake_Packages").write_text("Package: x\n", encoding="utf-8", newline="\n")
        for name in ("install-browser.sh", "install-compat.sh", "install-gaming.sh"):
            write_exec(t / "usr" / "libexec" / "lindos" / name, FAKE_INSTALL_SCRIPT)
        write_exec(t / "usr" / "libexec" / "lindos" / "browser-firstboot.sh", FAKE_BROWSER_FIRSTBOOT)

    def make_oem_target(self, *, with_oem_config: bool = True, with_user: bool = True, autologin: bool = True,
                        shadow: Optional[str] = EMPTY_PASSWORD_SHADOW) -> None:
        """What Ubiquity's OEM mode leaves in /target before the success command runs.

        ``shadow`` is oem's line in /etc/shadow: by default an EMPTY password (what the installer's page tells the
        user to leave), ``None`` = no shadow file at all.
        """
        t = self.target
        (t / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\n" + ("oem:x:29999:29999:OEM Configuration:/home/oem:/bin/bash\n" if with_user else ""),
            encoding="utf-8", newline="\n")
        if shadow is not None:
            (t / "etc" / "shadow").write_text("root:*:19000:0:99999:7:::\n" + shadow + "\n", encoding="utf-8", newline="\n")
        if with_oem_config:
            (t / "usr" / "lib" / "oem-config").mkdir(parents=True, exist_ok=True)
            (t / "usr" / "lib" / "oem-config" / "oem-config.service").write_text(
                "[Unit]\nDescription=oem-config\n[Service]\nExecStart=/usr/sbin/oem-config-firstboot --x\n[Install]\nWantedBy=oem-config.target\n",
                encoding="utf-8", newline="\n")
            (t / "usr" / "lib" / "oem-config" / "oem-config.target").write_text(
                "[Unit]\nDescription=oem-config target\nConflicts=multi-user.target\n", encoding="utf-8", newline="\n")
            write_exec(t / "usr" / "sbin" / "oem-config-firstboot", "#!/bin/sh\n")
        lines = ["[Seat:*]\n"]
        if autologin:
            lines += ["autologin-guest=false\n", "autologin-user=oem\n", "autologin-user-timeout=0\n"]
        lines += ["greeter-session=slick-greeter\n"]
        (t / "etc" / "lightdm" / "lightdm.conf").write_text("".join(lines), encoding="utf-8", newline="\n")
        write_exec(t / "usr" / "lib" / "ubiquity" / "target-config" / "50lindos-install", "#!/bin/sh\n")
        write_exec(t / "usr" / "lib" / "ubiquity" / "dm-scripts" / "install" / "50lindos-noblank", "#!/bin/sh\n")

    # -- environment -------------------------------------------------------------------------
    def env(self, **over: str) -> Dict[str, str]:
        env = dict(os.environ)
        for key in [k for k in env if k.startswith(("FAKE_", "LINDOS_", "DEBIAN_", "DEBCONF_"))]:
            del env[key]
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.update({
            "LINDOS_TARGET": self.target.as_posix(),
            "LINDOS_INSTALLER_LOG": self.live_log.as_posix(),
            "LINDOS_PYTHON": sys.executable,
            "PYTHONPATH": str(PYLIB) + os.pathsep + env.get("PYTHONPATH", ""),
            "LINDOS_EXTRAS_JSON": (SHARE / "extras.json").as_posix(),
            "LINDOS_INSTALLER_TEMPLATES": (SHARE / "lindos-installer.templates").as_posix(),
            "LINDOS_INSTALLER_LIB": (LIBEXEC / "lib.sh").as_posix(),
            "LINDOS_TARGET_RUNNER": self.runner.as_posix(),
            "LINDOS_TEST_CMDLINE": self.cmdline.as_posix(),
            "LINDOS_MANIFEST_REMOVE": self.manifest_remove.as_posix(),
            "LINDOS_RESOLV_SOURCES": self.resolv_source.as_posix(),
            "LINDOS_SYS_EFI": (self.root / "no-efi").as_posix(),
            "LINDOS_OEM_DEBS_DIR": (self.root / "oem-debs").as_posix(),
            "LINDOS_TIMEOUT_PCT": "100",
            "LINDOS_INSTALL_BUDGET": "900",
            "LINDOS_FREE_KB": "60000000",
            "FAKE_CALLS": self.calls.as_posix(),
            "FAKE_STATE": self.state.as_posix(),
            "FAKE_TARGET": self.target.as_posix(),
        })
        env.update(over)
        return env

    def run(self, script: Path, *args: str, timeout: int = 240, **over: str) -> "subprocess.CompletedProcess[str]":
        assert BASH is not None
        return subprocess.run([BASH, str(script), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=self.env(**over), stdin=subprocess.DEVNULL)

    def run_hook(self, **over: str) -> "subprocess.CompletedProcess[str]":
        return self.run(LIBEXEC / "target-config.sh", **over)

    def run_finalize(self, **over: str) -> "subprocess.CompletedProcess[str]":
        return self.run(LIBEXEC / "finalize.sh", **over)

    # -- observations --------------------------------------------------------------------------
    def call_log(self) -> List[str]:
        return [ln.rstrip("\r") for ln in self.calls.read_text(encoding="utf-8", errors="replace").splitlines()]

    def calls_of(self, name: str) -> List[str]:
        return [ln for ln in self.call_log() if ln == name or ln.startswith(name + " ")]

    def first_index(self, prefix: str) -> int:
        for i, ln in enumerate(self.call_log()):
            if ln.startswith(prefix):
                return i
        return -1

    def last_index(self, prefix: str) -> int:
        idx = -1
        for i, ln in enumerate(self.call_log()):
            if ln.startswith(prefix):
                idx = i
        return idx

    def held(self) -> List[str]:
        return [ln.strip() for ln in (self.state / "held").read_text(encoding="utf-8").splitlines() if ln.strip()]

    def upgraded(self) -> List[str]:
        """The packages the fake 'apt-get upgrade' actually upgraded (held ones are kept back)."""
        f = self.state / "upgraded"
        return f.read_text(encoding="utf-8").split() if f.is_file() else []

    def install_state(self) -> dict:
        path = self.target / "var" / "lib" / "lindos" / "install-state.json"
        if not path.is_file():
            return {"steps": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def step(self, name: str) -> dict:
        return self.install_state().get("steps", {}).get(name, {})

    def statuses(self) -> Dict[str, str]:
        return {k: v.get("status", "") for k, v in self.install_state().get("steps", {}).items()}

    def log_text(self) -> str:
        return self.live_log.read_text(encoding="utf-8", errors="replace") if self.live_log.is_file() else ""

    def set_cmdline(self, text: str) -> None:
        self.cmdline.write_text(text + "\n", encoding="utf-8")


ALL_STEPS = ["browser", "drivers", "updates", "compat", "gaming", "mode_extras", "flatpaks"]
