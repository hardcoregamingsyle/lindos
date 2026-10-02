#!/bin/bash
# ============================================================================
#  00-repos.sh — apt repositories for the Lindos live system (SPEC §8)
#
#  Runs INSIDE the squashfs chroot as root (staged by build/build-iso.sh).
#    * i386 multiarch (Wine / Steam / 32-bit Vulkan)
#    * WineHQ  — deb822 .sources for Ubuntu ${BASE_UBUNTU_CODENAME} + key
#                (used by install-compat.sh, run by the installer, for winehq-staging)
#    * Steam   — Valve's apt repository (same key/list paths as
#                /usr/libexec/lindos/install-gaming.sh so both stay idempotent)
#    * Mozilla — NOT added by default: Linux Mint ships its own firefox .deb
#                from packages.linuxmint.com and pins it (priority 700 in
#                /etc/apt/preferences.d/official-package-repositories.pref); a
#                second Firefox source would only fight that pin.  Opt-in with
#                ADD_MOZILLA_REPO=1 (then pinned to 1000 so Mozilla wins).
#    * Flathub — system-wide flatpak remote (flatpak is preinstalled on Mint)
#    * Kisak   — fresh Mesa PPA, opt-in with KISAK_MESA=1 (key fetched from
#                Launchpad's API + keyserver, no hard-coded fingerprint)
#    * Lindos  — NOT here: the lindos-archive-keyring package (Lindos's update source and public
#                key) is installed by 30-lindos-debs.sh, in the same apt transaction as lindos-core.
#                Nothing is fetched from the network for it.
#    * optional APT_MIRROR rewrite, then apt-get update.
#  Idempotent: re-running rewrites the same files with the same content.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "apt repositories"

: "${BASE_UBUNTU_CODENAME:=noble}"
: "${ENABLE_I386:=1}"
: "${ADD_WINEHQ_REPO:=1}"
: "${ADD_STEAM_REPO:=1}"
: "${ADD_FLATHUB:=1}"
: "${ADD_MOZILLA_REPO:=0}"
: "${KISAK_MESA:=0}"
: "${APT_MIRROR:=}"

KEYRINGS_DIR=/etc/apt/keyrings
SOURCES_DIR=/etc/apt/sources.list.d
mkdir -p "${KEYRINGS_DIR}" "${SOURCES_DIR}" /etc/apt/preferences.d

# ---------------------------------------------------------------------------
# 1. i386 multiarch
# ---------------------------------------------------------------------------
if [ "${ENABLE_I386}" = "1" ]; then
    if dpkg --print-foreign-architectures | grep -qx i386; then
        log "i386 architecture already enabled"
    else
        log "dpkg --add-architecture i386"
        dpkg --add-architecture i386
    fi
else
    log "ENABLE_I386=0 — leaving foreign architectures untouched"
fi

# ---------------------------------------------------------------------------
# 1b. Neutralise 'deb cdrom:' sources — 'apt-get update' aborts with exit 100
#     ("Please use apt-cdrom …") when one is active and no medium is mounted.
# ---------------------------------------------------------------------------
for f in /etc/apt/sources.list "${SOURCES_DIR}"/*.list; do
    [ -f "${f}" ] || continue
    if grep -qE '^[[:space:]]*deb(-src)?[[:space:]]+(\[[^]]*\][[:space:]]+)?cdrom:' "${f}"; then
        log "commenting out cdrom: source(s) in ${f}"
        sed -i -E 's/^([[:space:]]*deb(-src)?[[:space:]]+(\[[^]]*\][[:space:]]+)?cdrom:)/# \1/' "${f}"
    fi
done

# ---------------------------------------------------------------------------
# 2. Optional mirror rewrite (build farms behind a local mirror)
# ---------------------------------------------------------------------------
if [ -n "${APT_MIRROR}" ]; then
    log "rewriting archive.ubuntu.com → ${APT_MIRROR} in apt sources"
    for f in /etc/apt/sources.list "${SOURCES_DIR}"/*.list; do
        [ -f "${f}" ] || continue
        sed -i "s#https\\?://archive\\.ubuntu\\.com/ubuntu#${APT_MIRROR}#g; s#https\\?://[a-z]*\\.archive\\.ubuntu\\.com/ubuntu#${APT_MIRROR}#g" "${f}"
    done
fi

# ---------------------------------------------------------------------------
# 3. WineHQ (deb822 .sources published by WineHQ for every Ubuntu release)
# ---------------------------------------------------------------------------
if [ "${ADD_WINEHQ_REPO}" = "1" ]; then
    WINEHQ_KEY="${KEYRINGS_DIR}/winehq-archive.key"
    WINEHQ_SRC="${SOURCES_DIR}/winehq-${BASE_UBUNTU_CODENAME}.sources"
    if fetch "https://dl.winehq.org/wine-builds/winehq.key" "${WINEHQ_KEY}"; then
        chmod 0644 "${WINEHQ_KEY}"
        if fetch "https://dl.winehq.org/wine-builds/ubuntu/dists/${BASE_UBUNTU_CODENAME}/winehq-${BASE_UBUNTU_CODENAME}.sources" "${WINEHQ_SRC}"; then
            chmod 0644 "${WINEHQ_SRC}"
            # WineHQ's file references the keyring path we just wrote; make sure.
            if ! grep -q "^Signed-By:" "${WINEHQ_SRC}"; then
                printf 'Signed-By: %s\n' "${WINEHQ_KEY}" >> "${WINEHQ_SRC}"
            fi
            log "WineHQ repository configured: ${WINEHQ_SRC}"
        else
            warn "could not download WineHQ .sources — writing a local equivalent"
            cat > "${WINEHQ_SRC}" <<EOF
Types: deb
URIs: https://dl.winehq.org/wine-builds/ubuntu
Suites: ${BASE_UBUNTU_CODENAME}
Components: main
Architectures: amd64 i386
Signed-By: ${WINEHQ_KEY}
EOF
        fi
    else
        warn "could not download the WineHQ key — WineHQ repo NOT added (install-compat.sh adds it when online)"
        rm -f "${WINEHQ_SRC}"
    fi
else
    log "ADD_WINEHQ_REPO=0 — WineHQ repo skipped"
fi

# ---------------------------------------------------------------------------
# 4. Steam (Valve) — identical paths to install-gaming.sh
# ---------------------------------------------------------------------------
if [ "${ADD_STEAM_REPO}" = "1" ]; then
    STEAM_KEYRING=/usr/share/keyrings/steam.gpg
    STEAM_LIST="${SOURCES_DIR}/lindos-steam.list"
    STEAM_LINE="deb [arch=amd64,i386 signed-by=${STEAM_KEYRING}] https://repo.steampowered.com/steam/ stable steam"
    if fetch "https://repo.steampowered.com/steam/archive/stable/steam.gpg" "${STEAM_KEYRING}"; then
        chmod 0644 "${STEAM_KEYRING}"
        # Valve publishes a binary keyring; if we ever get an armored one, dearmor it.
        if head -c 5 "${STEAM_KEYRING}" | grep -q -- '-----'; then
            gpg --dearmor --yes -o "${STEAM_KEYRING}.tmp" "${STEAM_KEYRING}" && mv -f "${STEAM_KEYRING}.tmp" "${STEAM_KEYRING}"
        fi
        printf '%s\n' "${STEAM_LINE}" > "${STEAM_LIST}"
        chmod 0644 "${STEAM_LIST}"
        log "Steam repository configured: ${STEAM_LIST}"
    else
        warn "could not download Valve's key — Steam repo NOT added (install-gaming.sh adds it later)"
        rm -f "${STEAM_LIST}"
    fi
else
    log "ADD_STEAM_REPO=0 — Steam repo skipped"
fi

# ---------------------------------------------------------------------------
# 5. Mozilla apt repo — opt-in only (see header for why Mint's firefox is kept)
# ---------------------------------------------------------------------------
if [ "${ADD_MOZILLA_REPO}" = "1" ]; then
    MOZ_KEY="${KEYRINGS_DIR}/packages.mozilla.org.asc"
    MOZ_LIST="${SOURCES_DIR}/mozilla.list"
    if fetch "https://packages.mozilla.org/apt/repo-signing-key.gpg" "${MOZ_KEY}"; then
        chmod 0644 "${MOZ_KEY}"
        printf 'deb [signed-by=%s] https://packages.mozilla.org/apt mozilla main\n' "${MOZ_KEY}" > "${MOZ_LIST}"
        cat > /etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
EOF
        log "Mozilla apt repository configured (pinned 1000)"
    else
        warn "could not download Mozilla's key — Mozilla repo NOT added"
        rm -f "${MOZ_LIST}" /etc/apt/preferences.d/mozilla
    fi
else
    log "ADD_MOZILLA_REPO=0 — keeping Linux Mint's own firefox package (pinned by Mint)"
fi

# ---------------------------------------------------------------------------
# 6. Flathub remote (system-wide)
# ---------------------------------------------------------------------------
if [ "${ADD_FLATHUB}" = "1" ]; then
    if have flatpak; then
        if flatpak remotes --system 2>/dev/null | awk '{print $1}' | grep -qx flathub; then
            log "flathub remote already present"
        else
            log "flatpak remote-add flathub"
            if ! flatpak remote-add --system --if-not-exists flathub \
                    https://dl.flathub.org/repo/flathub.flatpakrepo; then
                warn "flatpak remote-add flathub failed (offline?) — the installer adds it when online"
            fi
        fi
    else
        log "flatpak not installed yet (20-base.sh installs it); flathub remote is added there"
    fi
else
    log "ADD_FLATHUB=0 — flathub remote skipped"
fi

# ---------------------------------------------------------------------------
# 7. Kisak fresh Mesa PPA (opt-in)
# ---------------------------------------------------------------------------
if [ "${KISAK_MESA}" = "1" ]; then
    KISAK_KEY="${KEYRINGS_DIR}/kisak-mesa.gpg"
    KISAK_SRC="${SOURCES_DIR}/kisak-mesa.sources"
    fp=""
    tmpjson="$(mktemp)"
    if fetch "https://api.launchpad.net/1.0/~kisak/+archive/ubuntu/kisak-mesa" "${tmpjson}"; then
        fp="$(python3 - "${tmpjson}" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.load(fh).get("signing_key_fingerprint", ""))
PY
)"
    fi
    rm -f "${tmpjson}"
    if [ -n "${fp}" ]; then
        tmpkey="$(mktemp)"
        if fetch "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x${fp}" "${tmpkey}"; then
            gpg --dearmor --yes -o "${KISAK_KEY}" "${tmpkey}"
            chmod 0644 "${KISAK_KEY}"
            cat > "${KISAK_SRC}" <<EOF
Types: deb
URIs: https://ppa.launchpadcontent.net/kisak/kisak-mesa/ubuntu
Suites: ${BASE_UBUNTU_CODENAME}
Components: main
Architectures: amd64 i386
Signed-By: ${KISAK_KEY}
EOF
            log "Kisak Mesa PPA configured (fingerprint ${fp})"
        else
            warn "could not fetch the Kisak PPA key from keyserver.ubuntu.com — PPA NOT added"
        fi
        rm -f "${tmpkey}"
    else
        warn "could not resolve the Kisak PPA signing key via Launchpad — PPA NOT added"
    fi
else
    log "KISAK_MESA=0 — stock Ubuntu Mesa kept"
fi

# ---------------------------------------------------------------------------
# 7b. Lindos's own apt repository (SPEC-UPDATE.md, docs/UPDATES.md): nothing to do here, on purpose.
#     The source (/etc/apt/sources.list.d/lindos.sources) and the public key come from the
#     lindos-archive-keyring package, and 30-lindos-debs.sh installs it with the other Lindos debs, where
#     it also checks the source and exercises it. It must not be unpacked from this hook: hooks 10 and 20 run
#     apt after this one, and apt refuses every install/purge/autoremove while a package is half-installed
#     (dpkg -i --force-depends left exactly that behind). Nothing is fetched from the network for the key.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. Never let apt inside the image install snaps or Ubuntu's firefox stub:
#    Mint already ships /etc/apt/preferences.d/nosnap.pref — just make sure.
# ---------------------------------------------------------------------------
if [ ! -f /etc/apt/preferences.d/nosnap.pref ]; then
    cat > /etc/apt/preferences.d/nosnap.pref <<'EOF'
# Lindos: keep snapd out (Mint policy); Firefox comes from packages.linuxmint.com
Package: snapd
Pin: release a=*
Pin-Priority: -10
EOF
    log "wrote /etc/apt/preferences.d/nosnap.pref"
fi

# ---------------------------------------------------------------------------
# 9. apt-get update
# ---------------------------------------------------------------------------
apt_update --force
if [ "${__APT_UPDATED:-0}" -ne 1 ]; then
    die "apt-get update failed — the ISO build needs network access (or APT_MIRROR)"
fi

hook_end
