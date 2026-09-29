#!/bin/bash
# install-gaming.sh — privileged installer for the Lindos gaming launchers.
#
# Called by the lindos helper action 'install-gaming' ({"items": [...]}) and by
# build/chroot/70-gaming.sh (--from-chroot).  Runs as root; never call sudo here.
#
# Usage: install-gaming.sh [--from-chroot] [--no-flatpak] [--dry-run] [--list]
#                          [--in-installer [--download-only | --no-download]] <item>...
#   items: steam lutris heroic prism sober vinegar mcpelauncher bottles all
#
#   steam        Valve apt repository (repo.steampowered.com) -> steam-launcher
#                fallback: Ubuntu multiverse 'steam-installer'
#   lutris       apt 'lutris' (noble universe)     fallback: Flatpak net.lutris.Lutris
#   heroic       pinned GitHub .deb (HEROIC_VERSION) fallback: Flatpak com.heroicgameslauncher.hgl
#   prism        Flatpak org.prismlauncher.PrismLauncher (bundles Java)
#                fallback: apt 'prismlauncher' if a source exists (PRISM_PPA opt-in) + openjdk-21-jre
#   sober        Flatpak org.vinegarhq.Sober        (Roblox — community client, not the Windows one)
#   vinegar      Flatpak org.vinegarhq.Vinegar      (Roblox Studio through Wine)
#   mcpelauncher Flatpak io.mrarm.mcpelauncher      (Minecraft Bedrock, unofficial, needs Google Play licence)
#   bottles      Flatpak com.usebottles.bottles
#
# --from-chroot : only apt items (no Flatpak, no service reloads); Flatpak-only
#                 items are reported as skipped so OOBE installs them at first boot.
# --in-installer: root inside 'chroot /target' for the Lindos installer (the Ubiquity target-config
#                 hook): the same restrictions as --from-chroot (apt/deb items only; Flatpak items
#                 are skipped because the caller handles Flatpak), the caller already refreshed the
#                 apt lists (a repository added here still is refreshed), and apt never reads the
#                 'deb cdrom:' source.  Without a phase flag it downloads, then installs.
# --download-only (with --in-installer) keys, repository files, .deb downloads and 'apt-get -d
#                 install' - nothing is installed; safe to kill.
# --no-download   (with --in-installer) dpkg runs from the files --download-only fetched; no
#                 network at all; the installer never kills it mid-transaction.
# Exit codes: 0 all requested items done · 1 one or more items failed
#             2 usage / not root · 3 offline
set -Eeuo pipefail

PROG="install-gaming"
LOG_FILE="/var/log/lindos/install-gaming.log"

FROM_CHROOT=0
NO_FLATPAK=0
DRY_RUN=0
APT_UPDATED=0
IN_INSTALLER=0
DOWNLOAD_ONLY=0
NO_DOWNLOAD=0
# --in-installer: never read the 'deb cdrom:' source, never clean the medium's lists, fail fast
APT_EXTRA=()
# --in-installer: where the download phase leaves the Heroic .deb for the install phase
HEROIC_CACHE_DIR="${HEROIC_CACHE_DIR:-/var/cache/lindos-installer}"

STEAM_KEY_URL="https://repo.steampowered.com/steam/archive/stable/steam.gpg"
STEAM_KEYRING="/usr/share/keyrings/steam.gpg"
STEAM_LIST="/etc/apt/sources.list.d/lindos-steam.list"
STEAM_REPO_LINE="deb [arch=amd64,i386 signed-by=${STEAM_KEYRING}] https://repo.steampowered.com/steam/ stable steam"
STEAM_I386_LIBS=(libgl1-mesa-dri:i386 libgl1:i386 mesa-vulkan-drivers:i386 libvulkan1:i386)

HEROIC_REPO="Heroic-Games-Launcher/HeroicGamesLauncher"
HEROIC_VERSION="${HEROIC_VERSION:-2.22.1}"     # pinned (asset Heroic-<ver>-linux-amd64.deb); 'latest' = ask the GitHub API
HEROIC_SHA256="${HEROIC_SHA256:-}"             # optional sha256 of the pinned .deb (empty = not verified)

# Prism Launcher has no official apt repository for Ubuntu; a PPA is opt-in:
#   PRISM_PPA=ppa:<owner>/<name> install-gaming.sh prism
PRISM_PPA="${PRISM_PPA:-}"

FLATHUB_URL="https://dl.flathub.org/repo/flathub.flatpakrepo"

ALL_ITEMS=(steam lutris heroic prism sober vinegar mcpelauncher bottles)
APT_ITEMS=(steam lutris heroic prism)

OK_ITEMS=()
FAILED_ITEMS=()
SKIPPED_ITEMS=()

# ----------------------------------------------------------------------------- #
log() {
    local msg
    msg="[$(date '+%F %T')] ${PROG}: $*"
    printf '%s\n' "${msg}" >&2
    if [ -w "$(dirname "${LOG_FILE}")" ] 2>/dev/null || mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null; then
        printf '%s\n' "${msg}" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    local code="${2:-1}"
    log "ERROR: $1"
    exit "${code}"
}

usage() {
    # print the header comment block (everything before `set -Eeuo pipefail`)
    sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

have() { command -v "$1" >/dev/null 2>&1; }

run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "DRY-RUN: $*"
        return 0
    fi
    "$@"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "must run as root (the lindos helper / pkexec does this for you)" 2
    fi
}

online() {
    [ -n "${LINDOS_OFFLINE:-}" ] && return 1
    local u
    for u in "https://repo.steampowered.com/steam/" "https://dl.flathub.org/repo/" \
             "https://api.github.com/" "http://archive.ubuntu.com/"; do
        if have curl; then
            curl -s --max-time 6 -o /dev/null "${u}" && return 0
        elif have wget; then
            wget -q --spider --timeout=6 "${u}" && return 0
        fi
    done
    return 1
}

fetch() {
    # fetch <url> <dest>
    local url="$1" dest="$2"
    if have curl; then
        curl -fL --retry 3 --connect-timeout 15 -sS -o "${dest}" "${url}"
    elif have wget; then
        wget -q --tries=3 --timeout=30 -O "${dest}" "${url}"
    else
        log "neither curl nor wget available"
        return 1
    fi
}

fetch_ok() {
    # fetch_ok <url> : HEAD request, follows redirects
    local url="$1"
    if have curl; then
        curl -fsIL --max-time 20 -o /dev/null "${url}"
    elif have wget; then
        wget -q --spider --timeout=20 "${url}"
    else
        return 1
    fi
}

github_deb_url() {
    # github_deb_url <owner/repo> <tag|latest> -> prints browser_download_url of the amd64 .deb
    local repo="$1" tag="$2" api tmp
    if [ "${tag}" = "latest" ]; then
        api="https://api.github.com/repos/${repo}/releases/latest"
    else
        api="https://api.github.com/repos/${repo}/releases/tags/${tag}"
    fi
    tmp="$(mktemp)"
    if have curl; then
        if [ -n "${GITHUB_TOKEN:-}" ]; then
            curl -fsSL --max-time 30 -H "Authorization: Bearer ${GITHUB_TOKEN}" \
                -H "Accept: application/vnd.github+json" -o "${tmp}" "${api}" || { rm -f "${tmp}"; return 1; }
        else
            curl -fsSL --max-time 30 -H "Accept: application/vnd.github+json" -o "${tmp}" "${api}" || { rm -f "${tmp}"; return 1; }
        fi
    else
        wget -q --timeout=30 --header="Accept: application/vnd.github+json" -O "${tmp}" "${api}" || { rm -f "${tmp}"; return 1; }
    fi
    python3 - "${tmp}" <<'PY' || { rm -f "${tmp}"; return 1; }
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for asset in data.get("assets", []):
    name = str(asset.get("name", ""))
    if re.search(r"(amd64|x86_64)\.deb$", name):
        print(asset["browser_download_url"])
        break
else:
    sys.exit(1)
PY
    rm -f "${tmp}"
}

apt_env() {
    export DEBIAN_FRONTEND=noninteractive
    export LC_ALL=C.UTF-8
}

apt_update() {
    # apt_update [force]
    [ "${NO_DOWNLOAD}" -eq 1 ] && return 0
    if [ "${APT_UPDATED}" -eq 1 ] && [ "${1:-}" != "force" ]; then
        return 0
    fi
    # the installer refreshed the lists itself: only a repository added by this run is refreshed
    if [ "${IN_INSTALLER}" -eq 1 ] && [ "${1:-}" != "force" ]; then
        return 0
    fi
    log "apt-get update"
    if run apt-get update -qq "${APT_EXTRA[@]}"; then
        APT_UPDATED=1
    else
        log "warning: apt-get update reported errors (continuing)"
        APT_UPDATED=1
    fi
}

apt_install() {
    # apt_install <pkg>...
    if [ "${IN_INSTALLER}" -eq 0 ]; then
        log "apt-get install $*"
        run apt-get install -y -qq \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
        return
    fi
    # two phases: a download (kill-safe), then a dpkg run from the downloaded files
    if [ "${NO_DOWNLOAD}" -eq 0 ]; then
        log "apt-get download $*"
        run apt-get install -y -qq -d "${APT_EXTRA[@]}" \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@" || return 1
    fi
    if [ "${DOWNLOAD_ONLY}" -eq 0 ]; then
        log "apt-get install $* (from the downloaded files)"
        run apt-get install -y -qq --no-download "${APT_EXTRA[@]}" \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
    fi
}

pkg_installed() {
    [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)" = "installed" ]
}

apt_candidate() {
    # apt_candidate <pkg> : true if apt knows a candidate version
    local cand
    cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    [ -n "${cand}" ] && [ "${cand}" != "(none)" ]
}

ensure_i386() {
    if dpkg --print-foreign-architectures 2>/dev/null | grep -qx i386; then
        return 0
    fi
    log "enabling i386 (32-bit) architecture for Steam/Wine libraries"
    run dpkg --add-architecture i386
    apt_update force
}

flatpak_ready() {
    [ "${FROM_CHROOT}" -eq 0 ] && [ "${NO_FLATPAK}" -eq 0 ] && have flatpak
}

ensure_flathub() {
    if flatpak remotes --system --columns=name 2>/dev/null | grep -qx flathub; then
        return 0
    fi
    log "adding Flathub remote"
    run flatpak remote-add --if-not-exists --system flathub "${FLATHUB_URL}"
}

flatpak_installed() {
    flatpak info --system "$1" >/dev/null 2>&1 || flatpak info "$1" >/dev/null 2>&1
}

flatpak_install() {
    # flatpak_install <app-id>
    local id="$1"
    if flatpak_installed "${id}"; then
        log "${id}: already installed (flatpak)"
        return 0
    fi
    ensure_flathub || return 1
    log "flatpak install flathub ${id}"
    run flatpak install -y --noninteractive --system flathub "${id}"
}

preseed_steam() {
    have debconf-set-selections || return 0
    printf '%s\n' \
        "steam-launcher steam/question select I AGREE" \
        "steam-launcher steam/license note " \
        "steam-installer steam/question select I AGREE" \
        "steam-installer steam/license note " \
        "steam steam/question select I AGREE" \
        "steam steam/license note " \
        | run debconf-set-selections 2>/dev/null || true
}

# ----------------------------------------------------------------------------- #
# per-item installers: return 0 ok, 1 failed, 2 skipped
# ----------------------------------------------------------------------------- #
STEAM_LIST_CHANGED=1

steam_stage_repo() {
    # 0 = Valve's key + repository line are in place (STEAM_LIST_CHANGED says whether this run wrote
    # them) · 1 = the key could not be fetched (fall back to Ubuntu's steam-installer) · 2 = fatal
    local tmpkey
    STEAM_LIST_CHANGED=1
    # --in-installer: the ISO already carries Valve's key and repository line (build-time pre-staging)
    if [ "${IN_INSTALLER}" -eq 1 ] && [ -s "${STEAM_KEYRING}" ] && [ -s "${STEAM_LIST}" ] \
            && grep -qF -- "${STEAM_REPO_LINE}" "${STEAM_LIST}"; then
        STEAM_LIST_CHANGED=0
        log "steam: Valve's repository is already staged"
        return 0
    fi
    tmpkey="$(mktemp)"
    if ! fetch "${STEAM_KEY_URL}" "${tmpkey}"; then
        rm -f "${tmpkey}"
        log "steam: could not download Valve's signing key; trying Ubuntu multiverse steam-installer"
        return 1
    fi
    if grep -q "BEGIN PGP" "${tmpkey}" 2>/dev/null; then
        if have gpg; then
            run gpg --dearmor --yes --output "${STEAM_KEYRING}" "${tmpkey}"
        else
            log "steam: key is ASCII-armoured and gpg is missing"
            rm -f "${tmpkey}"
            return 2
        fi
    else
        run install -m 0644 "${tmpkey}" "${STEAM_KEYRING}"
    fi
    rm -f "${tmpkey}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "DRY-RUN: write ${STEAM_LIST}: ${STEAM_REPO_LINE}"
    else
        printf '# Valve Steam repository — added by lindos install-gaming\n%s\n' "${STEAM_REPO_LINE}" >"${STEAM_LIST}"
    fi
    return 0
}

steam_drop_duplicate_list() {
    # steam-launcher ships its own /etc/apt/sources.list.d entry; drop ours to avoid duplicates
    if [ "${DRY_RUN}" -eq 0 ] && dpkg -L steam-launcher 2>/dev/null | grep -q '^/etc/apt/sources.list.d/'; then
        rm -f "${STEAM_LIST}"
    fi
}

install_steam_offline() {
    # the --no-download phase: the download phase staged the repository and fetched the packages
    if apt_install steam-launcher; then
        steam_drop_duplicate_list
        apt_install "${STEAM_I386_LIBS[@]}" || log "steam: warning — some 32-bit Mesa libraries could not be installed"
        return 0
    fi
    log "steam: steam-launcher was not downloaded; trying Ubuntu multiverse steam-installer"
    if apt_install steam-installer; then
        apt_install "${STEAM_I386_LIBS[@]}" || log "steam: warning — some 32-bit Mesa libraries could not be installed"
        return 0
    fi
    return 1
}

install_steam() {
    if pkg_installed steam-launcher || pkg_installed steam-installer; then
        log "steam: already installed"
        return 0
    fi
    ensure_i386
    preseed_steam
    if [ "${NO_DOWNLOAD}" -eq 1 ]; then
        install_steam_offline
        return
    fi
    local rc=0
    steam_stage_repo || rc=$?
    if [ "${rc}" -eq 2 ]; then
        return 1
    fi
    if [ "${rc}" -eq 0 ]; then
        if [ "${IN_INSTALLER}" -eq 0 ] || [ "${STEAM_LIST_CHANGED}" -eq 1 ]; then
            apt_update force
        fi
        if apt_install steam-launcher; then
            if [ "${DOWNLOAD_ONLY}" -eq 0 ]; then
                steam_drop_duplicate_list
            fi
            apt_install "${STEAM_I386_LIBS[@]}" || log "steam: warning — some 32-bit Mesa libraries could not be installed"
            return 0
        fi
        log "steam: Valve repository install failed; trying Ubuntu multiverse steam-installer"
        [ "${DRY_RUN}" -eq 0 ] && rm -f "${STEAM_LIST}"
        apt_update force
    fi
    apt_update
    if apt_install steam-installer; then
        apt_install "${STEAM_I386_LIBS[@]}" || log "steam: warning — some 32-bit Mesa libraries could not be installed"
        return 0
    fi
    return 1
}

install_lutris() {
    if pkg_installed lutris; then
        log "lutris: already installed"
        return 0
    fi
    apt_update
    if apt_candidate lutris && apt_install lutris; then
        return 0
    fi
    if flatpak_ready; then
        log "lutris: apt package unavailable; using Flatpak net.lutris.Lutris"
        flatpak_install net.lutris.Lutris && return 0
        return 1
    fi
    log "lutris: no apt candidate and Flatpak not usable here"
    return 1
}

heroic_cleanup() {
    # heroic_cleanup <dir> <deb> — a private temp dir goes away; the installer's shared cache dir stays
    if [ "${IN_INSTALLER}" -eq 1 ]; then
        rm -f "$2"
    else
        rm -rf "$1"
    fi
}

install_heroic() {
    if pkg_installed heroic; then
        log "heroic: already installed"
        return 0
    fi
    local url="" tmp deb sum
    if [ "${NO_DOWNLOAD}" -eq 1 ]; then
        # the --no-download phase: the download phase left the .deb in the cache directory
        deb="${HEROIC_CACHE_DIR}/heroic.deb"
        if [ -s "${deb}" ] && apt_install "${deb}"; then
            rm -f "${deb}"
            return 0
        fi
        log "heroic: the .deb was not downloaded by the download phase (or its installation failed)"
        return 1
    fi
    if [ "${HEROIC_VERSION}" = "latest" ]; then
        url="$(github_deb_url "${HEROIC_REPO}" latest || true)"
    else
        url="https://github.com/${HEROIC_REPO}/releases/download/v${HEROIC_VERSION}/Heroic-${HEROIC_VERSION}-linux-amd64.deb"
        if ! fetch_ok "${url}"; then
            log "heroic: pinned asset name not found, asking the GitHub API for v${HEROIC_VERSION}"
            url="$(github_deb_url "${HEROIC_REPO}" "v${HEROIC_VERSION}" || true)"
        fi
    fi
    if [ -n "${url}" ]; then
        if [ "${IN_INSTALLER}" -eq 1 ]; then
            tmp="${HEROIC_CACHE_DIR}"
            run mkdir -p "${tmp}"
        else
            tmp="$(mktemp -d)"
        fi
        deb="${tmp}/heroic.deb"
        log "heroic: downloading ${url}"
        if fetch "${url}" "${deb}"; then
            if [ -n "${HEROIC_SHA256}" ]; then
                sum="$(sha256sum "${deb}" | awk '{print $1}')"
                if [ "${sum}" != "${HEROIC_SHA256}" ]; then
                    log "heroic: sha256 mismatch (expected ${HEROIC_SHA256}, got ${sum}) — refusing to install"
                    heroic_cleanup "${tmp}" "${deb}"
                    return 1
                fi
                log "heroic: sha256 verified"
            else
                log "heroic: no pinned sha256 (HEROIC_SHA256 empty) — trusting HTTPS/GitHub"
            fi
            apt_update
            if [ "${DOWNLOAD_ONLY}" -eq 1 ]; then
                log "heroic: downloaded to ${deb} (--download-only: nothing installed)"
                return 0
            fi
            if apt_install "${deb}"; then
                heroic_cleanup "${tmp}" "${deb}"
                return 0
            fi
            log "heroic: .deb installation failed"
        else
            log "heroic: download failed"
        fi
        heroic_cleanup "${tmp}" "${deb}"
    else
        log "heroic: could not resolve a .deb download URL"
    fi
    if flatpak_ready; then
        log "heroic: falling back to Flatpak com.heroicgameslauncher.hgl"
        flatpak_install com.heroicgameslauncher.hgl && return 0
    fi
    return 1
}

install_prism() {
    if pkg_installed prismlauncher; then
        log "prism: already installed"
        return 0
    fi
    if flatpak_ready; then
        # the Flatpak bundles its own Java runtimes (openjdk extensions)
        flatpak_install org.prismlauncher.PrismLauncher && return 0
        log "prism: Flatpak install failed; trying apt"
    fi
    apt_update
    if apt_candidate prismlauncher; then
        apt_install prismlauncher openjdk-21-jre && return 0
    fi
    if [ -n "${PRISM_PPA}" ] && have add-apt-repository; then
        log "prism: adding ${PRISM_PPA}"
        if run add-apt-repository -y "${PRISM_PPA}"; then
            apt_update force
            if apt_candidate prismlauncher && apt_install prismlauncher openjdk-21-jre; then
                return 0
            fi
            log "prism: PPA did not provide prismlauncher; removing it again"
            run add-apt-repository -r -y "${PRISM_PPA}" || true
        else
            log "prism: could not add ${PRISM_PPA}"
        fi
    fi
    if [ "${FROM_CHROOT}" -eq 1 ] || [ "${NO_FLATPAK}" -eq 1 ]; then
        log "prism: no apt source available here; it is a Flatpak (Flathub) - the caller installs it, or: lindos-game install prism"
        return 2
    fi
    return 1
}

install_flatpak_item() {
    # install_flatpak_item <item> <app-id> <label>
    local item="$1" id="$2" label="$3"
    if ! flatpak_ready; then
        if [ "${FROM_CHROOT}" -eq 1 ] || [ "${NO_FLATPAK}" -eq 1 ]; then
            log "${item}: ${label} is a Flatpak (${id}); skipped in chroot/no-flatpak mode - the caller handles Flatpak"
            return 2
        fi
        log "${item}: flatpak is not available; cannot install ${label} (${id})"
        return 1
    fi
    flatpak_install "${id}"
}

dispatch_item() {
    local item="$1"
    case "${item}" in
        steam)        install_steam ;;
        lutris)       install_lutris ;;
        heroic)       install_heroic ;;
        prism)        install_prism ;;
        sober)        install_flatpak_item sober org.vinegarhq.Sober "Sober (Roblox)" ;;
        vinegar)      install_flatpak_item vinegar org.vinegarhq.Vinegar "Vinegar (Roblox Studio)" ;;
        mcpelauncher) install_flatpak_item mcpelauncher io.mrarm.mcpelauncher "mcpelauncher (Minecraft Bedrock, unofficial)" ;;
        bottles)      install_flatpak_item bottles com.usebottles.bottles "Bottles" ;;
        *)            log "unknown item: ${item}"; return 1 ;;
    esac
}

is_known_item() {
    local it
    for it in "${ALL_ITEMS[@]}"; do
        [ "${it}" = "$1" ] && return 0
    done
    return 1
}

# ----------------------------------------------------------------------------- #
main() {
    local items=() arg it rc
    while [ $# -gt 0 ]; do
        arg="$1"
        case "${arg}" in
            --from-chroot) FROM_CHROOT=1 ;;
            --in-installer) IN_INSTALLER=1; FROM_CHROOT=1 ;;
            --download-only) DOWNLOAD_ONLY=1 ;;
            --no-download) NO_DOWNLOAD=1 ;;
            --no-flatpak)  NO_FLATPAK=1 ;;
            --dry-run)     DRY_RUN=1 ;;
            --list)        printf '%s\n' "${ALL_ITEMS[@]}"; exit 0 ;;
            -h|--help)     usage; exit 0 ;;
            all)
                if [ "${FROM_CHROOT}" -eq 1 ] || [ "${NO_FLATPAK}" -eq 1 ]; then
                    items+=("${APT_ITEMS[@]}")
                else
                    items+=("${ALL_ITEMS[@]}")
                fi
                ;;
            -*)            usage >&2; die "unknown option: ${arg}" 2 ;;
            *)
                if is_known_item "${arg}"; then
                    items+=("${arg}")
                else
                    usage >&2
                    die "unknown item: ${arg} (known: ${ALL_ITEMS[*]} all)" 2
                fi
                ;;
        esac
        shift
    done
    if [ "${#items[@]}" -eq 0 ]; then
        usage >&2
        die "no items given" 2
    fi
    if { [ "${DOWNLOAD_ONLY}" -eq 1 ] || [ "${NO_DOWNLOAD}" -eq 1 ]; } && [ "${IN_INSTALLER}" -eq 0 ]; then
        usage >&2
        die "--download-only and --no-download need --in-installer" 2
    fi
    if [ "${DOWNLOAD_ONLY}" -eq 1 ] && [ "${NO_DOWNLOAD}" -eq 1 ]; then
        usage >&2
        die "--download-only and --no-download exclude each other" 2
    fi
    if [ "${IN_INSTALLER}" -eq 1 ]; then
        APT_EXTRA=(-o "Dir::Etc::SourceList=/dev/null" -o "APT::Get::List-Cleanup=0"
                   -o "Acquire::Retries=2" -o "Acquire::http::Timeout=20" -o "Acquire::https::Timeout=20")
    fi
    if [ "${DRY_RUN}" -eq 0 ]; then
        require_root
    fi
    apt_env
    # the --no-download phase never touches the network
    if [ "${NO_DOWNLOAD}" -eq 0 ] && ! online; then
        die "offline — installing launchers needs internet access (Valve/Ubuntu/Flathub/GitHub). Try again later or run: lindos-game install ${items[*]}" 3
    fi
    [ "${FROM_CHROOT}" -eq 1 ] && log "chroot mode: apt items only (Flatpak items are left to the caller)"

    # de-duplicate while keeping order
    local uniq=() seen=" "
    for it in "${items[@]}"; do
        case "${seen}" in *" ${it} "*) continue ;; esac
        uniq+=("${it}")
        seen="${seen}${it} "
    done

    for it in "${uniq[@]}"; do
        log "=== ${it} ==="
        set +e
        dispatch_item "${it}"
        rc=$?
        set -e
        case "${rc}" in
            0) OK_ITEMS+=("${it}");      log "${it}: done" ;;
            2) SKIPPED_ITEMS+=("${it}"); log "${it}: skipped" ;;
            *) FAILED_ITEMS+=("${it}");  log "${it}: FAILED" ;;
        esac
    done

    if [ "${FROM_CHROOT}" -eq 0 ] && [ "${DRY_RUN}" -eq 0 ]; then
        have update-desktop-database && update-desktop-database -q /usr/share/applications 2>/dev/null || true
    fi

    log "summary: installed=[${OK_ITEMS[*]:-}] skipped=[${SKIPPED_ITEMS[*]:-}] failed=[${FAILED_ITEMS[*]:-}]"
    if [ "${#FAILED_ITEMS[@]}" -gt 0 ]; then
        exit 1
    fi
    exit 0
}

main "$@"
