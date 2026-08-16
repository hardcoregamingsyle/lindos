#!/bin/bash
# install-browser.sh — install Microsoft Edge / Google Chrome / Mozilla Firefox (SPEC §4.4, §13).
#
# Called as root by the lindos helper action 'install-browser' (pkexec) and by build hooks.
# Edge and Chrome are NOT on the ISO (their licences forbid redistribution): this script adds
# the vendor's official apt repository (keyring in /etc/apt/keyrings) and installs the package
# from there.  Firefox is Mint's .deb (no snap) — 'apt-get install firefox'.
#
# Usage: install-browser.sh <edge|chrome|firefox> [--dry-run] [--no-update]
# Exit codes: 0 installed (or already installed) · 1 failure · 2 usage / not root · 3 offline
set -Eeuo pipefail

PROG="install-browser"
LOG_FILE="${LINDOS_ROOT:-}/var/log/lindos/install-browser.log"
DRY_RUN=0
NO_UPDATE=0
BROWSER=""

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
    printf 'Usage: %s <edge|chrome|firefox> [--dry-run] [--no-update]\n' "${PROG}" >&2
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
    trap 'rm -f "${tmp}"' RETURN
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --max-time 60 -o "${tmp}" "${url}" || die "cannot download signing key ${url} (offline?)" 3
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=60 -O "${tmp}" "${url}" || die "cannot download signing key ${url} (offline?)" 3
    else
        die "neither curl nor wget is installed" 1
    fi
    if grep -q 'BEGIN PGP PUBLIC KEY BLOCK' "${tmp}"; then
        gpg --dearmor --yes --output "${dest}" "${tmp}" || die "gpg --dearmor failed for ${url}" 1
    else
        install -m 0644 "${tmp}" "${dest}"
    fi
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
    log "apt source written: ${list}"
}

apt_update_list() {
    # apt_update_list <list-file> — refresh only this source (fast, no full 'apt-get update')
    local list="$1"
    if [[ "${NO_UPDATE}" -eq 1 ]]; then
        return 0
    fi
    run apt-get update -qq \
        -o "Dir::Etc::sourcelist=${list}" \
        -o "Dir::Etc::sourceparts=-" \
        -o "APT::Get::List-Cleanup=0" \
        || die "apt-get update for ${list} failed (offline or repository unreachable)" 3
}

apt_install() {
    run apt-get install -y -q \
        -o "Dpkg::Options::=--force-confdef" -o "Dpkg::Options::=--force-confold" "$@" \
        || die "apt-get install $* failed" 1
}

install_vendor() {
    # install_vendor <package> <key_url> <keyring> <list> <repo-line> <name>
    local package="$1" key_url="$2" keyring="$3" list="$4" repo="$5" name="$6"
    # In --dry-run, always print the full plan (keyring + .list + apt) so it is
    # visible regardless of what the *host* already has installed; only a real
    # run short-circuits when the package is present.
    if [[ "${DRY_RUN}" -eq 0 ]] && is_installed "${package}"; then
        log "${name} (${package}) is already installed"
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

install_firefox() {
    if [[ "${DRY_RUN}" -eq 0 ]] && is_installed "${FIREFOX_PACKAGE}"; then
        log "Mozilla Firefox is already installed (Mint .deb)"
        return 0
    fi
    # never the snap: Mint ships firefox as a .deb from its own repository; if a Ubuntu transitional
    # 'firefox' snap-wrapper is pinned away this still resolves to the Mint package.
    if [[ "${NO_UPDATE}" -eq 0 ]]; then
        run apt-get update -qq || log "apt-get update failed; trying with current lists"
    fi
    apt_install "${FIREFOX_PACKAGE}"
    log "Mozilla Firefox installed"
}

main() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
            edge|chrome|firefox) BROWSER="${arg}" ;;
            --dry-run) DRY_RUN=1 ;;
            --no-update) NO_UPDATE=1 ;;
            -h|--help) usage ;;
            *) printf '%s: unknown argument %s\n' "${PROG}" "${arg}" >&2; usage ;;
        esac
    done
    [[ -n "${BROWSER}" ]] || usage
    if [[ "${DRY_RUN}" -eq 0 && "$(id -u)" -ne 0 ]]; then
        die "must run as root (the lindos helper calls this through pkexec)" 2
    fi
    export DEBIAN_FRONTEND=noninteractive
    log "install ${BROWSER}${DRY_RUN:+ (dry-run=${DRY_RUN})}"
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
