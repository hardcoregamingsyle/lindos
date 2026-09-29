#!/bin/bash
# install-compat.sh — privileged installer for Lindos "Windows program" support (SPEC §9, §13).
#
# Called by the lindos helper action 'install-compat' ({"items": [...]}) and by
# build/chroot/60-compat.sh.  Runs as root; never call sudo here.
#
# Usage: install-compat.sh [--minimal] [--from-chroot] [--dry-run] [--no-update] [--list]
#                          [--in-installer [--download-only | --no-download]] [item...]
#   items: all wine wine-staging umu umu-launcher winetricks bottles dxvk vkd3d fonts dependencies proton
#          (default: all = wine winetricks dependencies fonts umu)
#   wine / wine-staging  WineHQ repository for Ubuntu noble -> winehq-staging
#                        (fallback wine-staging, then Ubuntu's own 'wine')
#   winetricks           winetricks + cabextract
#   dependencies         32-bit Vulkan/GL libs, winbind, icoutils, zenity, fonts-liberation
#   fonts                fonts-liberation (+ ttf-mscorefonts-installer only when
#                        ACCEPT_MSCOREFONTS_EULA=1 — Microsoft's EULA must be accepted)
#   umu / umu-launcher   'lindos-compat install-umu --system' (pinned GitHub release)
#   bottles              Flatpak com.usebottles.bottles from Flathub (skipped in chroot)
#   dxvk / vkd3d         no-op: built into Proton (umu); for plain Wine use 'winetricks dxvk'
#   proton               'lindos-proton update' as the calling desktop user (PKEXEC_UID/SUDO_USER)
#
#   --minimal      only Ubuntu's own 'wine' (+ wine32:i386) and winetricks/cabextract; no WineHQ,
#                  no umu — used by build/chroot/60-compat.sh when INCLUDE_WINE=1
#   --from-chroot  no Flatpak, no per-user steps
#   --no-update    do not run 'apt-get update' (the caller just did)
#   --in-installer root inside 'chroot /target' for the Lindos installer (the Ubiquity target-config
#                  hook): apt items yes; Flatpak (Bottles) and per-user steps (Proton-GE) no - the
#                  caller handles Flatpak; the caller already refreshed the apt lists; apt never
#                  reads the 'deb cdrom:' source.  Without a phase flag: download, then install.
#   --download-only  (with --in-installer) everything that needs the network and is not a dpkg run:
#                  keys, repository files, the umu download, 'apt-get -d install' - kill-safe
#   --no-download    (with --in-installer) dpkg runs from the files --download-only fetched; no
#                  network at all, and the installer never kills it mid-transaction
#
# Idempotent: every step checks before acting.  Offline-aware: exits 3 with a message
# when the network is unreachable and something needs downloading.
# Exit codes: 0 done · 1 one or more items failed · 2 usage / not root · 3 offline
set -Eeuo pipefail

PROG="install-compat"
LOG_FILE="/var/log/lindos/install-compat.log"

MINIMAL=0
FROM_CHROOT=0
DRY_RUN=0
NO_UPDATE=0
APT_UPDATED=0
NEED_UPDATE=0
IN_INSTALLER=0
DOWNLOAD_ONLY=0
NO_DOWNLOAD=0
# --in-installer: never read the 'deb cdrom:' source, never clean the medium's lists, fail fast
APT_EXTRA=()

WINEHQ_KEY_URL="https://dl.winehq.org/wine-builds/winehq.key"
WINEHQ_KEYRING="/etc/apt/keyrings/winehq-archive.key"
UBUNTU_CODENAME_DEFAULT="noble"
FLATHUB_URL="https://dl.flathub.org/repo/flathub.flatpakrepo"
BOTTLES_APP="com.usebottles.bottles"

SUPPORT_PKGS=(winetricks cabextract winbind libvulkan1 libvulkan1:i386 mesa-vulkan-drivers
              mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 fonts-liberation icoutils zenity)

ALL_ITEMS=(wine winetricks dependencies fonts umu)
KNOWN_ITEMS=(all wine wine-staging umu umu-launcher winetricks bottles dxvk vkd3d fonts dependencies proton)

OK_ITEMS=()
FAILED_ITEMS=()
SKIPPED_ITEMS=()

# ----------------------------------------------------------------------------- #
log() {
    local msg
    msg="[$(date '+%F %T')] ${PROG}: $*"
    printf '%s\n' "${msg}" >&2
    if mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null; then
        printf '%s\n' "${msg}" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    local code="${2:-1}"
    log "ERROR: $1"
    exit "${code}"
}

usage() {
    sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
}

have() { command -v "$1" >/dev/null 2>&1; }

run() {
    if [ "${DRY_RUN}" = 1 ]; then
        log "(dry-run) $*"
        return 0
    fi
    log "+ $*"
    "$@"
}

is_installed() {
    local status
    status="$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)"
    [ "${status}" = "installed" ]
}

fetch() {
    # fetch URL DEST  (curl or wget, atomic)
    local url="$1" dest="$2" tmp
    tmp="$(mktemp "${dest}.XXXXXX")"
    if have curl; then
        if ! curl -fsSL --retry 3 --max-time 120 -o "${tmp}" "${url}"; then rm -f "${tmp}"; return 1; fi
    elif have wget; then
        if ! wget -q --tries=3 --timeout=120 -O "${tmp}" "${url}"; then rm -f "${tmp}"; return 1; fi
    else
        rm -f "${tmp}"
        log "neither curl nor wget is installed"
        return 1
    fi
    [ -s "${tmp}" ] || { rm -f "${tmp}"; return 1; }
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${dest}"
}

ONLINE_STATE=""   # cached result of check_online: "yes" / "no"

check_online() {
    [ "${DRY_RUN}" = 1 ] && return 0
    if [ -n "${ONLINE_STATE}" ]; then
        [ "${ONLINE_STATE}" = yes ]
        return
    fi
    local url
    ONLINE_STATE=no
    if have curl || have wget; then
        for url in "${WINEHQ_KEY_URL}" "https://archive.ubuntu.com/ubuntu/" "https://github.com/"; do
            if have curl && curl -fsSIL --max-time 10 "${url}" >/dev/null 2>&1; then ONLINE_STATE=yes; return 0; fi
            if have wget && wget -q --spider --timeout=10 "${url}" 2>/dev/null; then ONLINE_STATE=yes; return 0; fi
        done
        return 1
    fi
    if getent hosts dl.winehq.org >/dev/null 2>&1; then
        ONLINE_STATE=yes
        return 0
    fi
    return 1
}

require_online() {
    # the --no-download phase never touches the network
    [ "${NO_DOWNLOAD}" = 1 ] && return 0
    if ! check_online; then
        log "No internet connection: cannot download $1."
        log "Connect to the internet and run again (Lindos Settings > Windows apps > Install)."
        exit 3
    fi
}

apt_update() {
    [ "${NO_DOWNLOAD}" = 1 ] && return 0
    if [ "${NO_UPDATE}" = 1 ] && [ "${NEED_UPDATE}" = 0 ]; then return 0; fi
    if [ "${APT_UPDATED}" = 1 ] && [ "${NEED_UPDATE}" = 0 ]; then return 0; fi
    run apt-get update -qq "${APT_EXTRA[@]}" || log "apt-get update reported errors (continuing with cached lists)"
    APT_UPDATED=1
    NEED_UPDATE=0
}

apt_install() {
    # apt_install [--recommends] pkg...   -> 0 if all installed (or already), 1 otherwise
    local recommends="--no-install-recommends" pkgs=() p
    if [ "${1:-}" = "--recommends" ]; then recommends="--install-recommends"; shift; fi
    for p in "$@"; do
        is_installed "${p}" || pkgs+=("${p}")
    done
    if [ "${#pkgs[@]}" = 0 ]; then
        log "already installed: $*"
        return 0
    fi
    if [ "${IN_INSTALLER}" = 1 ]; then
        # two phases: a download (kill-safe) and a dpkg run from the downloaded files
        if [ "${NO_DOWNLOAD}" = 0 ]; then
            require_online "packages: ${pkgs[*]}"
            apt_update
            run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q -d "${APT_EXTRA[@]}" \
                -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
                "${recommends}" "${pkgs[@]}" || return 1
        fi
        if [ "${DOWNLOAD_ONLY}" = 0 ]; then
            run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-download "${APT_EXTRA[@]}" \
                -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
                "${recommends}" "${pkgs[@]}" || return 1
        fi
        return 0
    fi
    require_online "packages: ${pkgs[*]}"
    apt_update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o Dpkg::Options::=--force-confdef \
        -o Dpkg::Options::=--force-confold "${recommends}" "${pkgs[@]}"
}

apt_install_each() {
    # tolerant: install one by one, report the failures, return 1 if any failed
    local failed=() p
    for p in "$@"; do
        if ! apt_install "${p}"; then
            failed+=("${p}")
        fi
    done
    if [ "${#failed[@]}" -gt 0 ]; then
        log "could not install: ${failed[*]}"
        return 1
    fi
    return 0
}

ubuntu_codename() {
    local name=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        name="$( . /etc/os-release && printf '%s' "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" )"
    fi
    printf '%s' "${name:-${UBUNTU_CODENAME_DEFAULT}}"
}

# ----------------------------------------------------------------------------- #
ensure_i386() {
    if dpkg --print-foreign-architectures 2>/dev/null | grep -qx i386; then
        return 0
    fi
    log "enabling the 32-bit (i386) architecture (many Windows programs are 32-bit)"
    run dpkg --add-architecture i386
    NEED_UPDATE=1
}

add_winehq_repo() {
    local codename sources
    codename="$(ubuntu_codename)"
    sources="/etc/apt/sources.list.d/winehq-${codename}.sources"
    if [ "${NO_DOWNLOAD}" = 1 ]; then
        # no network in this phase: the key and the repository file must already be there
        [ -s "${WINEHQ_KEYRING}" ] && [ -s "${sources}" ]
        return
    fi
    run mkdir -p /etc/apt/keyrings
    if [ ! -s "${WINEHQ_KEYRING}" ]; then
        require_online "the WineHQ signing key"
        log "adding the WineHQ signing key -> ${WINEHQ_KEYRING}"
        if [ "${DRY_RUN}" = 1 ]; then
            log "(dry-run) fetch ${WINEHQ_KEY_URL} -> ${WINEHQ_KEYRING}"
        else
            fetch "${WINEHQ_KEY_URL}" "${WINEHQ_KEYRING}" || { log "cannot download ${WINEHQ_KEY_URL}"; return 1; }
        fi
        NEED_UPDATE=1
    fi
    if [ ! -s "${sources}" ]; then
        require_online "the WineHQ repository definition"
        log "adding the WineHQ repository for ${codename} -> ${sources}"
        if [ "${DRY_RUN}" = 1 ]; then
            log "(dry-run) fetch https://dl.winehq.org/wine-builds/ubuntu/dists/${codename}/winehq-${codename}.sources"
        else
            fetch "https://dl.winehq.org/wine-builds/ubuntu/dists/${codename}/winehq-${codename}.sources" "${sources}" \
                || { log "cannot download the WineHQ sources file for ${codename}"; return 1; }
        fi
        NEED_UPDATE=1
    fi
    return 0
}

install_wine() {
    ensure_i386
    if [ "${MINIMAL}" = 1 ]; then
        log "minimal mode: Ubuntu's own Wine"
        if is_installed wine || is_installed wine64; then
            log "wine already installed"
        elif ! apt_install --recommends wine wine32:i386; then
            apt_install --recommends wine || return 1
        fi
        apt_install winetricks cabextract || log "winetricks/cabextract not installed (non-fatal)"
        return 0
    fi
    if is_installed winehq-staging || is_installed wine-staging; then
        log "Wine (staging) already installed: $(wine --version 2>/dev/null || true)"
        return 0
    fi
    if ! add_winehq_repo; then
        log "WineHQ repository unavailable - falling back to Ubuntu's 'wine'"
    fi
    apt_update
    if apt_install --recommends winehq-staging; then
        return 0
    fi
    log "winehq-staging failed - trying wine-staging"
    if apt_install --recommends wine-staging; then
        return 0
    fi
    log "wine-staging failed - trying Ubuntu's 'wine'"
    if apt_install --recommends wine wine32:i386 || apt_install --recommends wine; then
        return 0
    fi
    return 1
}

install_winetricks() {
    apt_install winetricks cabextract
}

install_dependencies() {
    ensure_i386
    apt_update
    if apt_install "${SUPPORT_PKGS[@]}"; then
        return 0
    fi
    log "group install failed - installing support packages one by one"
    apt_install_each "${SUPPORT_PKGS[@]}"
}

install_fonts() {
    local rc=0
    apt_install fonts-liberation || rc=1
    if [ "${ACCEPT_MSCOREFONTS_EULA:-0}" = 1 ]; then
        if is_installed ttf-mscorefonts-installer; then
            log "ttf-mscorefonts-installer already installed"
        else
            log "installing Microsoft core fonts (EULA accepted via ACCEPT_MSCOREFONTS_EULA=1)"
            if [ "${DRY_RUN}" = 0 ] && have debconf-set-selections; then
                printf 'ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true\n' \
                    | debconf-set-selections || true
            fi
            apt_install ttf-mscorefonts-installer || rc=1
        fi
    else
        log "Microsoft core fonts skipped (needs EULA: rerun with ACCEPT_MSCOREFONTS_EULA=1, or per C:\\ drive: winetricks corefonts)"
    fi
    return "${rc}"
}

install_umu() {
    if have umu-run; then
        log "umu-run already installed: $(command -v umu-run)"
        return 0
    fi
    if [ "${NO_DOWNLOAD}" = 1 ]; then
        # 'lindos-compat install-umu' downloads a pinned release: it belongs to the download phase
        log "umu-launcher was not fetched by the download phase"
        return 1
    fi
    require_online "umu-launcher"
    if ! have lindos-compat; then
        log "lindos-compat is not on PATH - cannot install umu-launcher (is lindos-compat installed?)"
        return 1
    fi
    apt_install python3 >/dev/null 2>&1 || true
    if [ "${DRY_RUN}" = 1 ]; then
        log "(dry-run) lindos-compat install-umu --system"
        return 0
    fi
    log "+ lindos-compat install-umu --system"
    lindos-compat install-umu --system
}

install_bottles() {
    if [ "${FROM_CHROOT}" = 1 ]; then
        log "Bottles (Flatpak) is skipped inside the chroot - the caller handles Flatpak (the installer, or Settings > Apps later)"
        SKIPPED_ITEMS+=("bottles")
        return 0
    fi
    if ! have flatpak; then
        apt_install flatpak || { log "flatpak is not installed"; return 1; }
    fi
    if flatpak info --system "${BOTTLES_APP}" >/dev/null 2>&1; then
        log "Bottles already installed"
        return 0
    fi
    require_online "Bottles"
    if ! flatpak remotes --system --columns=name 2>/dev/null | grep -qx flathub; then
        run flatpak remote-add --system --if-not-exists flathub "${FLATHUB_URL}" || return 1
    fi
    run flatpak install --system -y --noninteractive flathub "${BOTTLES_APP}"
}

install_dxvk() {
    log "DXVK/VKD3D: built into Proton (umu-run) - nothing to install. For a plain Wine C:\\ drive run:"
    log "  WINEPREFIX=~/.local/share/lindos/prefixes/<name> winetricks dxvk vkd3d"
    return 0
}

calling_user() {
    local uid="${PKEXEC_UID:-}" name=""
    if [ -n "${uid}" ]; then
        name="$(getent passwd "${uid}" | cut -d: -f1 || true)"
    fi
    if [ -z "${name}" ] && [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != root ]; then
        name="${SUDO_USER}"
    fi
    printf '%s' "${name}"
}

install_proton() {
    local user home
    if [ "${FROM_CHROOT}" = 1 ]; then
        log "Proton-GE download skipped inside the chroot (per-user: lindos-proton update as your user)"
        SKIPPED_ITEMS+=("proton")
        return 0
    fi
    if ! have lindos-proton; then
        log "lindos-proton (package lindos-gaming) is not installed - install lindos-gaming for Proton-GE management"
        return 1
    fi
    user="$(calling_user)"
    if [ -z "${user}" ]; then
        log "no desktop user to install Proton-GE for (run 'lindos-proton update' as your user)"
        SKIPPED_ITEMS+=("proton")
        return 0
    fi
    require_online "Proton-GE"
    home="$(getent passwd "${user}" | cut -d: -f6)"
    run runuser -u "${user}" -- env HOME="${home}" XDG_DATA_HOME="${home}/.local/share" lindos-proton update
}

# ----------------------------------------------------------------------------- #
do_item() {
    local item="$1" rc=0
    case "${item}" in
        wine|wine-staging)   install_wine || rc=1 ;;
        winetricks)          install_winetricks || rc=1 ;;
        dependencies)        install_dependencies || rc=1 ;;
        fonts)               install_fonts || rc=1 ;;
        umu|umu-launcher)    install_umu || rc=1 ;;
        bottles)             install_bottles || rc=1 ;;
        dxvk|vkd3d)          install_dxvk || rc=1 ;;
        proton)              install_proton || rc=1 ;;
        *)                   log "unknown item '${item}' ignored"; rc=1 ;;
    esac
    if [ "${rc}" = 0 ]; then
        OK_ITEMS+=("${item}")
    else
        FAILED_ITEMS+=("${item}")
        log "item '${item}' FAILED"
    fi
}

main() {
    local items=() arg known
    while [ $# -gt 0 ]; do
        arg="$1"; shift
        case "${arg}" in
            --minimal)      MINIMAL=1 ;;
            --from-chroot)  FROM_CHROOT=1 ;;
            --in-installer) IN_INSTALLER=1 ;;
            --download-only) DOWNLOAD_ONLY=1 ;;
            --no-download)  NO_DOWNLOAD=1 ;;
            --dry-run)      DRY_RUN=1 ;;
            --no-update)    NO_UPDATE=1 ;;
            --list)         printf '%s\n' "${KNOWN_ITEMS[@]}"; exit 0 ;;
            -h|--help)      usage; exit 0 ;;
            --*)            usage >&2; die "unknown option ${arg}" 2 ;;
            *)
                known=0
                for k in "${KNOWN_ITEMS[@]}"; do [ "${k}" = "${arg}" ] && known=1; done
                [ "${known}" = 1 ] || die "unknown item '${arg}' (see --list)" 2
                items+=("${arg}")
                ;;
        esac
    done

    if { [ "${DOWNLOAD_ONLY}" = 1 ] || [ "${NO_DOWNLOAD}" = 1 ]; } && [ "${IN_INSTALLER}" = 0 ]; then
        die "--download-only and --no-download need --in-installer" 2
    fi
    if [ "${DOWNLOAD_ONLY}" = 1 ] && [ "${NO_DOWNLOAD}" = 1 ]; then
        die "--download-only and --no-download exclude each other" 2
    fi
    if [ "${IN_INSTALLER}" = 1 ]; then
        # same restrictions as the ISO build chroot (no Flatpak, no per-user steps) ...
        FROM_CHROOT=1
        # ... and the installer has already refreshed the lists (a repository added here still is)
        NO_UPDATE=1
        APT_EXTRA=(-o "Dir::Etc::SourceList=/dev/null" -o "APT::Get::List-Cleanup=0"
                   -o "Acquire::Retries=2" -o "Acquire::http::Timeout=20" -o "Acquire::https::Timeout=20")
    fi
    if [ "$(id -u)" != 0 ] && [ "${DRY_RUN}" = 0 ]; then
        die "must run as root (use: pkexec $0 ...)" 2
    fi
    have apt-get || die "apt-get not found - this script is for Debian/Ubuntu based systems" 2

    local want_all=0 it
    for it in "${items[@]:-}"; do
        [ "${it}" = all ] && want_all=1
    done
    if [ "${#items[@]}" = 0 ] || [ "${want_all}" = 1 ]; then
        items=("${ALL_ITEMS[@]}")
    fi
    if [ "${MINIMAL}" = 1 ]; then
        items=(wine)
    fi

    log "starting (items: ${items[*]}; minimal=${MINIMAL} chroot=${FROM_CHROOT} dry-run=${DRY_RUN} in-installer=${IN_INSTALLER} download-only=${DOWNLOAD_ONLY} no-download=${NO_DOWNLOAD})"
    for it in "${items[@]}"; do
        do_item "${it}"
    done

    log "done: ok=[${OK_ITEMS[*]:-}] failed=[${FAILED_ITEMS[*]:-}] skipped=[${SKIPPED_ITEMS[*]:-}]"
    if have wine; then
        log "Wine: $(wine --version 2>/dev/null || echo unknown)"
    fi
    if [ "${#FAILED_ITEMS[@]}" -gt 0 ]; then
        exit 1
    fi
    exit 0
}

trap 'log "aborted at line ${LINENO} (exit code $?)"' ERR
main "$@"
