#!/bin/bash
# ============================================================================
#  30-lindos-debs.sh — install the Lindos packages built by build/mkdeb.sh
#
#  Runs INSIDE the squashfs chroot as root.  build-iso.sh stages out/debs/*.deb
#  to /tmp/lindos/debs/.  Order (LINDOS_DEB_ORDER in build/config.env):
#      lindos-core lindos-desktop lindos-tune lindos-compat lindos-gaming
#      lindos-transfer lindos-setup lindos-settings lindos-installer lindos-meta
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

: "${LINDOS_DEB_ORDER:=lindos-core lindos-desktop lindos-tune lindos-compat lindos-gaming lindos-transfer lindos-setup lindos-settings lindos-installer lindos-meta}"
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
