#!/bin/bash
# ============================================================================
#  30-lindos-debs.sh — install the Lindos packages built by build/mkdeb.sh
#
#  Runs INSIDE the squashfs chroot as root.  build-iso.sh stages out/debs/*.deb
#  to /tmp/lindos/debs/.  Order (LINDOS_DEB_ORDER in build/config.env):
#      lindos-archive-keyring lindos-core lindos-desktop lindos-tune lindos-compat
#      lindos-gaming lindos-transfer lindos-setup lindos-settings lindos-installer lindos-meta
#  (lindos-archive-keyring - the update source and its key - is installed here and nowhere earlier; it
#  needs nothing, so no order can leave apt with an unmet dependency; afterwards this hook checks the
#  source and, when it is enabled, exercises it with apt-get update)
#  (lindos-installer: the installer scripts, on the medium only; 79-installer-flow.sh wires them into Ubiquity)
#  (lindos-transfer installs before lindos-setup per SPEC-WINDOWS §33 so the
#  OOBE's optional "Bring your stuff from Windows" page can call it.)
#  All debs are handed to ONE 'apt-get install --no-install-recommends ./x.deb …'
#  transaction (apt resolves the inter-dependencies and pulls Depends from the
#  archive; Recommends are deliberately NOT followed — see below);
#  if that fails we fall back to 'dpkg -i' + 'apt-get -f install'.
#  Afterwards: optional install of the everyday mode's package list, then
#  'lindos-tune status --json || true' as a smoke test, then (ADD_CHROME_REPO=1,
#  default) pre-stage Google Chrome's apt repo + signing key via lindos-core's
#  own install-browser.sh --repo-only (SPEC §0.1: never installs the package).
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "lindos debs"

: "${LINDOS_DEB_ORDER:=lindos-archive-keyring lindos-core lindos-desktop lindos-tune lindos-compat lindos-gaming lindos-transfer lindos-setup lindos-settings lindos-installer lindos-meta}"
: "${INSTALL_MODE_PACKAGES:=1}"
: "${TUNE_MODE:=everyday}"

DEBS_DIR="${LINDOS_DEBS_DIR}"
[ -d "${DEBS_DIR}" ] || die "no staged debs directory at ${DEBS_DIR} (run build/mkdeb.sh all first)"

read -r -a ORDER <<< "${LINDOS_DEB_ORDER}"

shopt -s nullglob
ordered=()
seen=()

is_seen() {
    local f
    for f in "${seen[@]}"; do
        [ "${f}" = "$1" ] && return 0
    done
    return 1
}

for name in "${ORDER[@]}"; do
    matches=("${DEBS_DIR}/${name}_"*.deb)
    if [ "${#matches[@]}" -eq 0 ]; then
        warn "no .deb for ${name} in ${DEBS_DIR} — skipped"
        continue
    fi
    # Newest version last in glob order → take the last one.
    deb="${matches[$(( ${#matches[@]} - 1 ))]}"
    ordered+=("${deb}")
    seen+=("${deb}")
done

# Any additional debs not in the order list (future packages) go last.
for deb in "${DEBS_DIR}"/*.deb; do
    if ! is_seen "${deb}"; then
        log "extra deb (not in LINDOS_DEB_ORDER): $(basename "${deb}")"
        ordered+=("${deb}")
    fi
done
shopt -u nullglob

[ "${#ordered[@]}" -gt 0 ] || die "no .deb files found in ${DEBS_DIR}"

log "installing ${#ordered[@]} package file(s):"
for deb in "${ordered[@]}"; do
    log "  $(basename "${deb}")"
done

apt_update

# --no-install-recommends: our packages *Recommend* the big optional stacks
# (winehq-staging, steam-launcher, lutris, corectrl, openrgb, timeshift, …).
# Whether those go on the ISO is decided by INCLUDE_WINE / INCLUDE_STEAM /
# 20-base.sh, not by apt's recommends resolver.
if apt-get "${APT_ARGS[@]}" install --no-install-recommends "${ordered[@]}"; then
    log "apt-get install of local debs succeeded"
else
    warn "apt-get install ./debs failed — falling back to dpkg -i + apt-get -f install"
    dpkg -i "${ordered[@]}" || true
    apt-get "${APT_ARGS[@]}" -f install --no-install-recommends
    # Verify every package is now configured.
    failed=0
    for deb in "${ordered[@]}"; do
        pkg="$(dpkg-deb -f "${deb}" Package)"
        if ! pkg_installed "${pkg}"; then
            warn "package still not installed after fallback: ${pkg}"
            failed=1
        fi
    done
    [ "${failed}" -eq 0 ] || die "one or more Lindos packages failed to install"
fi

# ---------------------------------------------------------------------------
# The xfconf defaults of lindos-desktop that stock packages also ship as conffiles (xfce4-session.xml, xsettings.xml,
# xfce4-keyboard-shortcuts.xml, xfce4-power-manager.xml) must really have been installed.  An earlier build's dpkg run kept
# the stock conffile as "deleted" and parked ours as *.dpkg-dist, and the live session then ended in xfce4-session's "Unable to
# determine failsafe session name" (CI boot test of 7fc3aae).  lindos-desktop's postinst restores one that is missing; when it
# is still not there the build stops here, not at 82-session-sanity.sh an hour later.  LINDOS_XFCONF_DIR: test seam.
# ---------------------------------------------------------------------------
if pkg_installed lindos-desktop; then
    XFCONF_DIR="${LINDOS_XFCONF_DIR:-/etc/xdg/xfce4/xfconf/xfce-perchannel-xml}"
    for ch in xfce4-session xsettings xfce4-keyboard-shortcuts xfce4-power-manager; do
        [ -s "${XFCONF_DIR}/${ch}.xml" ] \
            || die "${XFCONF_DIR}/${ch}.xml is missing after the install (dpkg kept another package's conffile of that name as deleted?) - see docs/BUILDING.md, Session sanity"
    done
    log "lindos-desktop's xfconf defaults are in place"
fi

# ---------------------------------------------------------------------------
# The Lindos update source (SPEC-UPDATE.md, docs/UPDATES.md): lindos.sources and its key come from the
# lindos-archive-keyring package installed above; nothing is fetched from the network. It ships switched OFF
# until a real key and server exist. LINDOS_APT_REPO_REQUIRE=1 (release images) fails the build unless the
# source is enabled and its key is on the image; an enabled source is exercised by apt-get update right away.
# ---------------------------------------------------------------------------
: "${LINDOS_APT_REPO_REQUIRE:=0}"
: "${LINDOS_SOURCES_FILE:=/etc/apt/sources.list.d/lindos.sources}"
: "${LINDOS_KEYRING_FILE:=/usr/share/keyrings/lindos-archive-keyring.gpg}"
if ! pkg_installed lindos-archive-keyring; then
    if [ "${LINDOS_APT_REPO_REQUIRE}" = "1" ]; then
        die "LINDOS_APT_REPO_REQUIRE=1 but lindos-archive-keyring is not installed (no .deb staged in ${DEBS_DIR}?)"
    fi
    warn "no lindos-archive-keyring installed - the image gets no Lindos update source (lindos-update sideload still works)"
elif [ -f "${LINDOS_SOURCES_FILE}" ] && ! grep -qiE '^[[:space:]]*Enabled:[[:space:]]*(no|false|0|off)[[:space:]]*$' "${LINDOS_SOURCES_FILE}"; then
    [ -s "${LINDOS_KEYRING_FILE}" ] || die "the Lindos apt source is enabled but ${LINDOS_KEYRING_FILE} is missing"
    log "Lindos apt source enabled: $(sed -n 's/^URIs:[[:space:]]*//p' "${LINDOS_SOURCES_FILE}" | head -n 1)"
    if apt-get "${APT_ARGS[@]}" update; then
        log "apt-get update accepted the Lindos source"
    elif [ "${LINDOS_APT_REPO_REQUIRE}" = "1" ]; then
        die "apt-get update failed with the Lindos source enabled (LINDOS_APT_REPO_REQUIRE=1)"
    else
        warn "apt-get update failed with the Lindos source enabled - is the repository reachable and signed with the shipped key?"
    fi
elif [ "${LINDOS_APT_REPO_REQUIRE}" = "1" ]; then
    die "LINDOS_APT_REPO_REQUIRE=1 but the Lindos apt source is not enabled (${LINDOS_SOURCES_FILE}); see docs/RELEASING.md"
else
    log "Lindos apt source is switched off (no real signing key and server yet) - the honest default; see docs/UPDATES.md"
fi

# ---------------------------------------------------------------------------
# Optional: packages the default mode lists in its mode.json (usually empty
# for 'everyday'; the installer adds every Mode's extras: packages/lindos-installer).  Best effort per package.
# ---------------------------------------------------------------------------
MODE_JSON="/usr/share/lindos/modes/${TUNE_MODE}/mode.json"
if [ "${INSTALL_MODE_PACKAGES}" = "1" ] && [ -f "${MODE_JSON}" ] && have python3; then
    mapfile -t mode_pkgs < <(python3 - "${MODE_JSON}" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for p in data.get("packages", []) or []:
    if isinstance(p, str) and p.strip():
        print(p.strip())
PY
)
    if [ "${#mode_pkgs[@]}" -gt 0 ]; then
        log "mode '${TUNE_MODE}' package list: ${mode_pkgs[*]}"
        apt_try_install "${mode_pkgs[@]}"
    else
        log "mode '${TUNE_MODE}' lists no extra packages"
    fi
fi

# ---------------------------------------------------------------------------
# Smoke tests (never fatal)
# ---------------------------------------------------------------------------
for pkg in "${ORDER[@]}"; do
    if pkg_installed "${pkg}"; then
        log "installed: ${pkg} $(dpkg-query -W -f='${Version}' "${pkg}")"
    fi
done

if have lindos-tune; then
    log "lindos-tune status --json:"
    lindos-tune status --json 2>&1 | head -c 4000 >&2 || true
    printf '\n' >&2
else
    warn "lindos-tune not on PATH after install (lindos-tune package missing?)"
fi

if have python3; then
    python3 -c 'import lindos, sys; print("lindos module import OK from", getattr(lindos, "__file__", "?"))' >&2 \
        || warn "python3 cannot import the 'lindos' module (lindos-core missing?)"
fi

# ---------------------------------------------------------------------------
# Pre-stage Google Chrome's apt repository + signing key on the image — same
# pattern as the WineHQ/Steam repos in 00-repos.sh (SPEC §0.1, §8), but this
# has to run here (after lindos-core is installed above) rather than in
# 00-repos.sh, because it reuses lindos-core's own install-browser.sh
# (--repo-only: adds the repo/key, never runs 'apt-get install') instead of
# duplicating Chrome's key URL / repo line a second time.  google-chrome-stable
# itself is NEVER installed at build time — that would be redistribution; it is
# downloaded by the installer's target-config hook (packages/lindos-installer) - or, when
# that could not, silently by lindos-browser-firstboot.service - both call this exact script.
# ---------------------------------------------------------------------------
: "${ADD_CHROME_REPO:=1}"
INSTALL_BROWSER_SH=/usr/libexec/lindos/install-browser.sh
if [ "${ADD_CHROME_REPO}" = "1" ]; then
    if [ -x "${INSTALL_BROWSER_SH}" ]; then
        if "${INSTALL_BROWSER_SH}" chrome --repo-only; then
            log "Google Chrome apt repository staged (package not installed — SPEC §0.1)"
        else
            warn "could not stage Chrome's apt repository (offline?) — install-browser.sh adds it when the installer runs"
        fi
    else
        warn "${INSTALL_BROWSER_SH} not found (lindos-core missing?) — Chrome repo NOT staged"
    fi
else
    log "ADD_CHROME_REPO=0 — Chrome apt repository not pre-staged"
fi

hook_end
