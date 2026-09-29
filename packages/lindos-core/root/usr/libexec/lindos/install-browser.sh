#!/bin/bash
# install-browser.sh — install Microsoft Edge / Google Chrome / Mozilla Firefox (SPEC §4.4, §13).
#
# Called as root by the lindos helper action 'install-browser' (pkexec), by build hooks, and by
# lindos-browser-firstboot.service (chrome, on the installed system's first boot).
# Edge and Chrome are NOT on the ISO (their licences forbid redistribution): this script adds
# the vendor's official apt repository (keyring in /etc/apt/keyrings) and installs the package
# from there.  Firefox is Mint's .deb (no snap) — 'apt-get install firefox'.
#
# Usage: install-browser.sh <edge|chrome> [--repo-only] [--dry-run] [--no-update]
#                           [--in-installer [--download-only | --no-download]]
#        install-browser.sh firefox [--dry-run] [--no-update]
#   --repo-only   only add the vendor's apt repository + signing key (no 'apt-get install').
#                 Used by build/chroot/00-repos.sh to pre-stage Chrome's repo/key on the ISO
#                 (same pattern as the WineHQ/Steam repos there) WITHOUT ever installing the
#                 google-chrome-stable package at build time — that would be redistribution.
#                 Reusing this script (not duplicating the repo/key logic in 00-repos.sh) keeps
#                 there being exactly one place that knows Chrome's key URL / repo line.
#   --in-installer  root inside 'chroot /target' for the Lindos installer (the Ubiquity target-config
#                 hook, packages/lindos-installer): the caller has already refreshed the apt lists,
#                 so the vendor list is only refreshed when this run had to write it; apt never
#                 consults the 'deb cdrom:' source and never cleans lists.  With no phase flag it
#                 downloads, then installs from the downloaded files.
#   --download-only (with --in-installer) stage key + repo and download the package; install nothing
#                 - safe to kill.  The installer gives this phase a short timeout.
#   --no-download   (with --in-installer) install from the files --download-only fetched; never
#                 touches the network.  The installer never kills this phase mid-transaction.
# Exit codes: 0 installed (or already installed / repo staged) · 1 failure · 2 usage / not root
#             · 3 offline
#
# apt discipline (the first boot runs the Chrome retry and the driver retry at the same moment, and
# the user can start an install from Settings meanwhile - apt does not queue by itself):
#   * every apt-get here passes -o DPkg::Lock::Timeout=N: a dpkg lock somebody else holds is waited
#     for instead of failing at once (exit 100);
#   * the whole run takes its turn behind every other Lindos apt job (libexec/apt-serialise, a flock
#     on /run/lindos/apt.lock; it also exports the same lock timeout to anything apt starts);
#   * 'apt-get update' - whose lists lock DPkg::Lock::Timeout does not cover - is retried a few times
#     before the machine is called offline.
# The installer (--in-installer) is one sequential hook: it keeps its own apt.conf and time boxes.
set -Eeuo pipefail

PROG="install-browser"
LOG_FILE="${LINDOS_ROOT:-}/var/log/lindos/install-browser.log"
DRY_RUN=0
NO_UPDATE=0
REPO_ONLY=0
IN_INSTALLER=0
DOWNLOAD_ONLY=0
NO_DOWNLOAD=0
LIST_CHANGED=0
# --in-installer: never read the 'deb cdrom:' source, never clean the lists of the medium, fail fast
APT_EXTRA=()
BROWSER=""
ORIG_ARGS=("$@")
SELF="${BASH_SOURCE[0]}"
APT_SERIALISE="$(dirname "${SELF}")/apt-serialise"
# seconds to wait for a held dpkg lock (same value the installer's apt.conf uses when --in-installer)
APT_LOCK_TIMEOUT="${LINDOS_APT_LOCK_TIMEOUT:-300}"
APT_LOCK_OPT=()
# 'apt-get update' attempts and the pause between them (the lists lock is not covered by the dpkg lock wait)
APT_UPDATE_TRIES="${LINDOS_APT_UPDATE_TRIES:-3}"
APT_UPDATE_DELAY="${LINDOS_APT_UPDATE_RETRY_DELAY:-15}"

# --- vendor metadata (must match lindos/browsers.py BROWSERS) ---------------------------------
EDGE_PACKAGE="microsoft-edge-stable"
EDGE_KEY_URL="https://packages.microsoft.com/keys/microsoft.asc"
EDGE_KEYRING="/etc/apt/keyrings/microsoft.gpg"
EDGE_LIST="/etc/apt/sources.list.d/microsoft-edge.list"
EDGE_REPO="deb [arch=amd64 signed-by=${EDGE_KEYRING}] https://packages.microsoft.com/repos/edge stable main"

CHROME_PACKAGE="google-chrome-stable"
CHROME_KEY_URL="https://dl.google.com/linux/linux_signing_key.pub"
CHROME_KEYRING="/etc/apt/keyrings/google-chrome.gpg"
CHROME_LIST="/etc/apt/sources.list.d/google-chrome.list"
CHROME_REPO="deb [arch=amd64 signed-by=${CHROME_KEYRING}] https://dl.google.com/linux/chrome/deb/ stable main"

FIREFOX_PACKAGE="firefox"

log() {
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] ${PROG}: $*"
    printf '%s\n' "${msg}"
    if [[ -d "$(dirname "${LOG_FILE}")" ]] || mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null; then
        printf '%s\n' "${msg}" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    local code="${2:-1}"
    log "ERROR: $1"
    printf '%s: %s\n' "${PROG}" "$1" >&2
    exit "${code}"
}

usage() {
    printf 'Usage: %s <edge|chrome|firefox> [--repo-only] [--dry-run] [--no-update] [--in-installer [--download-only|--no-download]]\n' "${PROG}" >&2
    exit 2
}

run() {
    # run <argv…> — execute (or print in dry-run); never through a shell
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[dry-run] would run: $*"
        return 0
    fi
    log "run: $*"
    "$@"
}

is_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}

online() {
    # cheap probe: any of the vendor hosts reachable on 443
    local host
    if [[ "${LINDOS_FORCE_OFFLINE:-0}" == "1" ]]; then
        return 1
    fi
    for host in packages.microsoft.com dl.google.com deb.debian.org; do
        if command -v curl >/dev/null 2>&1; then
            curl -fsSI --max-time 6 "https://${host}/" >/dev/null 2>&1 && return 0
        elif command -v wget >/dev/null 2>&1; then
            wget -q --spider --timeout=6 "https://${host}/" >/dev/null 2>&1 && return 0
        fi
    done
    return 1
}

fetch_key() {
    # fetch_key <url> <dest.gpg> — download an ASCII/binary key and de-armor it into <dest.gpg>
    local url="$1" dest="$2" tmp
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[dry-run] would fetch ${url} → ${dest}"
        return 0
    fi
    mkdir -p "$(dirname "${dest}")"
    chmod 0755 "$(dirname "${dest}")"
    tmp="$(mktemp)"
    # explicit cleanup, no RETURN trap: a RETURN trap that names a local outlives the function in some bash
    # versions and then fires (with 'set -u': fails) when the caller returns
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --max-time 60 -o "${tmp}" "${url}" || { rm -f "${tmp}"; die "cannot download signing key ${url} (offline?)" 3; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=60 -O "${tmp}" "${url}" || { rm -f "${tmp}"; die "cannot download signing key ${url} (offline?)" 3; }
    else
        rm -f "${tmp}"
        die "neither curl nor wget is installed" 1
    fi
    if grep -q 'BEGIN PGP PUBLIC KEY BLOCK' "${tmp}"; then
        gpg --dearmor --yes --output "${dest}" "${tmp}" || { rm -f "${tmp}"; die "gpg --dearmor failed for ${url}" 1; }
    else
        install -m 0644 "${tmp}" "${dest}"
    fi
    rm -f "${tmp}"
    chmod 0644 "${dest}"
    log "keyring installed: ${dest}"
}

write_list() {
    # write_list <list-file> <repo-line>
    local list="$1" line="$2"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[dry-run] would write ${list}: ${line}"
        return 0
    fi
    mkdir -p "$(dirname "${list}")"
    if [[ -f "${list}" ]] && grep -qxF "${line}" "${list}"; then
        log "apt source already present: ${list}"
        return 0
    fi
    printf '%s\n' "${line}" >"${list}.tmp"
    chmod 0644 "${list}.tmp"
    mv -f "${list}.tmp" "${list}"
    LIST_CHANGED=1
    log "apt source written: ${list}"
}

apt_update_list() {
    # apt_update_list <list-file> — refresh only this source (fast, no full 'apt-get update').
    # Retried a few times: another apt job (the driver retry, apt-daily, the Update Manager) may hold
    # the lists lock, which DPkg::Lock::Timeout does not cover, and a lock is not "offline".
    local list="$1" attempt=1
    if [[ "${NO_UPDATE}" -eq 1 ]]; then
        return 0
    fi
    while ! run apt-get update "${APT_LOCK_OPT[@]}" -qq \
        -o "Dir::Etc::sourcelist=${list}" \
        -o "Dir::Etc::sourceparts=-" \
        -o "APT::Get::List-Cleanup=0"; do
        if [[ "${attempt}" -ge "${APT_UPDATE_TRIES}" ]]; then
            die "apt-get update for ${list} failed (offline or repository unreachable)" 3
        fi
        log "apt-get update failed (attempt ${attempt} of ${APT_UPDATE_TRIES}; another apt job may hold the lists lock) - trying again in ${APT_UPDATE_DELAY}s"
        sleep "${APT_UPDATE_DELAY}" || true
        attempt=$((attempt + 1))
    done
}

apt_install() {
    if [[ "${IN_INSTALLER}" -eq 0 ]]; then
        run apt-get install "${APT_LOCK_OPT[@]}" -y -q \
            -o "Dpkg::Options::=--force-confdef" -o "Dpkg::Options::=--force-confold" "$@" \
            || die "apt-get install $* failed" 1
        return 0
    fi
    # The installer runs this in two phases (see --download-only / --no-download): a download that
    # may be killed on a timeout, then a dpkg run from the downloaded files that never is.
    if [[ "${NO_DOWNLOAD}" -eq 0 ]]; then
        run apt-get install "${APT_LOCK_OPT[@]}" -y -q -d "${APT_EXTRA[@]}" \
            -o "Dpkg::Options::=--force-confdef" -o "Dpkg::Options::=--force-confold" "$@" \
            || die "apt-get download of $* failed" 1
    fi
    if [[ "${DOWNLOAD_ONLY}" -eq 0 ]]; then
        run apt-get install "${APT_LOCK_OPT[@]}" -y -q --no-download "${APT_EXTRA[@]}" \
            -o "Dpkg::Options::=--force-confdef" -o "Dpkg::Options::=--force-confold" "$@" \
            || die "apt-get install $* failed" 1
    fi
}

install_vendor() {
    # install_vendor <package> <key_url> <keyring> <list> <repo-line> <name>
    local package="$1" key_url="$2" keyring="$3" list="$4" repo="$5" name="$6"
    if [[ "${REPO_ONLY}" -eq 1 ]]; then
        # Pre-stage the repo + signing key only — never 'apt-get install' the package.  Used at
        # ISO build time (build/chroot/00-repos.sh) so Chrome's repo/key are on the image exactly
        # like the WineHQ/Steam repos, without ever installing google-chrome-stable there.
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            online || die "offline: cannot stage ${name}'s apt repository/key without internet" 3
        fi
        fetch_key "${key_url}" "${LINDOS_ROOT:-}${keyring}"
        write_list "${LINDOS_ROOT:-}${list}" "${repo}"
        apt_update_list "${list}"
        log "${name} apt repository staged (--repo-only: ${package} not installed)"
        return 0
    fi
    # In --dry-run, always print the full plan (keyring + .list + apt) so it is
    # visible regardless of what the *host* already has installed; only a real
    # run short-circuits when the package is present.
    if [[ "${DRY_RUN}" -eq 0 ]] && is_installed "${package}"; then
        log "${name} (${package}) is already installed"
        return 0
    fi
    if [[ "${IN_INSTALLER}" -eq 1 ]]; then
        install_vendor_in_installer "${package}" "${key_url}" "${keyring}" "${list}" "${repo}" "${name}"
        return 0
    fi
    if [[ "${DRY_RUN}" -eq 0 ]]; then
        online || die "offline: ${name} is downloaded from the vendor's apt repository — connect to the internet and run 'lindos-browser install ${BROWSER}' later (Firefox is available meanwhile)" 3
    fi
    fetch_key "${key_url}" "${LINDOS_ROOT:-}${keyring}"
    write_list "${LINDOS_ROOT:-}${list}" "${repo}"
    apt_update_list "${list}"
    apt_install "${package}"
    log "${name} installed"
}

install_vendor_in_installer() {
    # install_vendor_in_installer <package> <key_url> <keyring> <list> <repo-line> <name>
    # The ISO already carries the vendor's repo + key (build-time pre-staging) and the installer's
    # own 'apt-get update' has refreshed its lists, so only what is missing is fetched here.
    local package="$1" key_url="$2" keyring="$3" list="$4" repo="$5" name="$6"
    local key_path="${LINDOS_ROOT:-}${keyring}" list_path="${LINDOS_ROOT:-}${list}"
    if [[ "${NO_DOWNLOAD}" -eq 1 ]]; then
        # no network in this phase: everything must already be staged by the download phase
        if [[ "${DRY_RUN}" -eq 0 && ( ! -s "${key_path}" || ! -s "${list_path}" ) ]]; then
            die "${name}: repository not staged (run the --download-only phase first)" 1
        fi
    else
        if [[ "${DRY_RUN}" -eq 0 ]]; then
            online || die "offline: ${name} is downloaded from the vendor's apt repository" 3
        fi
        if [[ "${DRY_RUN}" -eq 1 || ! -s "${key_path}" ]]; then
            fetch_key "${key_url}" "${key_path}"
        fi
        write_list "${list_path}" "${repo}"
        if [[ "${LIST_CHANGED}" -eq 1 ]]; then
            apt_update_list "${list}"
        fi
    fi
    apt_install "${package}"
    if [[ "${DOWNLOAD_ONLY}" -eq 1 ]]; then
        log "${name} downloaded (--download-only: nothing installed)"
    else
        log "${name} installed"
    fi
}

install_firefox() {
    if [[ "${DRY_RUN}" -eq 0 ]] && is_installed "${FIREFOX_PACKAGE}"; then
        log "Mozilla Firefox is already installed"
        return 0
    fi
    # never the snap: Mint ships firefox as a .deb from its own repository; if a Ubuntu transitional
    # 'firefox' snap-wrapper is pinned away this still resolves to the Mint package.
    if [[ "${NO_UPDATE}" -eq 0 ]]; then
        run apt-get update "${APT_LOCK_OPT[@]}" -qq || log "apt-get update failed; trying with current lists"
    fi
    apt_install "${FIREFOX_PACKAGE}"
    log "Mozilla Firefox installed"
}

main() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
            edge|chrome|firefox) BROWSER="${arg}" ;;
            --repo-only) REPO_ONLY=1 ;;
            --dry-run) DRY_RUN=1 ;;
            --no-update) NO_UPDATE=1 ;;
            --in-installer) IN_INSTALLER=1 ;;
            --download-only) DOWNLOAD_ONLY=1 ;;
            --no-download) NO_DOWNLOAD=1 ;;
            -h|--help) usage ;;
            *) printf '%s: unknown argument %s\n' "${PROG}" "${arg}" >&2; usage ;;
        esac
    done
    [[ -n "${BROWSER}" ]] || usage
    if [[ "${REPO_ONLY}" -eq 1 && "${BROWSER}" == "firefox" ]]; then
        printf '%s: --repo-only is not meaningful for firefox (no separate vendor repo)\n' "${PROG}" >&2
        usage
    fi
    if [[ ( "${DOWNLOAD_ONLY}" -eq 1 || "${NO_DOWNLOAD}" -eq 1 ) && "${IN_INSTALLER}" -eq 0 ]]; then
        printf '%s: --download-only and --no-download need --in-installer\n' "${PROG}" >&2
        usage
    fi
    if [[ "${DOWNLOAD_ONLY}" -eq 1 && "${NO_DOWNLOAD}" -eq 1 ]]; then
        printf '%s: --download-only and --no-download exclude each other\n' "${PROG}" >&2
        usage
    fi
    if [[ "${IN_INSTALLER}" -eq 1 ]]; then
        APT_EXTRA=(-o "Dir::Etc::SourceList=/dev/null" -o "APT::Get::List-Cleanup=0"
                   -o "Acquire::Retries=2" -o "Acquire::http::Timeout=20" -o "Acquire::https::Timeout=20")
        if [[ "${NO_DOWNLOAD}" -eq 1 ]]; then
            NO_UPDATE=1
        fi
    fi
    if [[ "${DRY_RUN}" -eq 0 && "$(id -u)" -ne 0 ]]; then
        die "must run as root (the lindos helper calls this through pkexec)" 2
    fi
    if [[ "${IN_INSTALLER}" -eq 1 ]]; then
        # one sequential hook with its own apt.conf (the same lock wait) and time boxes: no retries here
        APT_LOCK_TIMEOUT="${LINDOS_APT_LOCK_TIMEOUT:-120}"
        APT_UPDATE_TRIES=1
    fi
    APT_LOCK_OPT=(-o "DPkg::Lock::Timeout=${APT_LOCK_TIMEOUT}")
    # Take our turn behind every other Lindos apt job (the driver retry, another browser install): re-run
    # this very command once under apt-serialise.  Not for a dry run (nothing to protect), the installer
    # hook (sequential) or the ISO build's --repo-only (a chroot with no other apt job).
    if [[ "${LINDOS_APT_SERIALISED:-}" != "1" && "${DRY_RUN}" -eq 0 && "${IN_INSTALLER}" -eq 0 \
          && "${REPO_ONLY}" -eq 0 && -f "${APT_SERIALISE}" ]]; then
        exec bash "${APT_SERIALISE}" -- bash "${SELF}" "${ORIG_ARGS[@]}"
    fi
    export DEBIAN_FRONTEND=noninteractive
    log "install ${BROWSER}${DRY_RUN:+ (dry-run=${DRY_RUN})}${REPO_ONLY:+ (repo-only=${REPO_ONLY})}"
    case "${BROWSER}" in
        edge)    install_vendor "${EDGE_PACKAGE}" "${EDGE_KEY_URL}" "${EDGE_KEYRING}" "${EDGE_LIST}" "${EDGE_REPO}" "Microsoft Edge" ;;
        chrome)  install_vendor "${CHROME_PACKAGE}" "${CHROME_KEY_URL}" "${CHROME_KEYRING}" "${CHROME_LIST}" "${CHROME_REPO}" "Google Chrome" ;;
        firefox) install_firefox ;;
    esac
    # refresh desktop database so the new .desktop file is picked up by the panel / xdg-settings
    if [[ "${DRY_RUN}" -eq 0 ]] && command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications 2>/dev/null || true
    fi
    log "done: ${BROWSER}"
}

main "$@"
