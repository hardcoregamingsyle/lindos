#!/bin/bash
# ============================================================================
#  build/build-iso.sh — Lindos ISO remaster pipeline (SPEC §1, §8, §13)
#
#  fetch base ISO → extract (xorriso) → unsquashfs → chroot hooks →
#  mksquashfs → casper metadata → overlay + branding → md5sum.txt →
#  hybrid BIOS+UEFI ISO (xorriso) → sha256sum
#
#  Usage: sudo build/build-iso.sh [options]
#     --iso PATH          use this local base ISO instead of downloading
#     --skip-download     use the cached ISO in out/cache/ (error if absent)
#     --skip-debs         do not rebuild packages (use out/debs as is)
#     --only-debs         build the .debs and stop (no root needed)
#     --skip-assets       do not run build/fetch-assets.sh
#     --skip-hooks NN,NN  skip chroot hooks by number (e.g. 60,70) or name
#     --hooks-only        reuse out/work/{iso,squashfs-root} from a previous
#                         --no-cleanup run: skip download/extract/unsquash,
#                         run the hooks again and repack
#     --no-cleanup        keep out/work/ after a successful build
#     --method M          ISO repack method: mkisofs (default) | replay
#     -h, --help
#
#  Every knob lives in build/config.env (env overrides win, e.g.
#  INCLUDE_STEAM=0 KISAK_MESA=1 sudo -E build/build-iso.sh).
#  Everything is logged to out/build.log; per-hook logs go to out/hooks/.
#  Needs root (chroot + bind mounts) — or use build/docker-build.sh.
#
#  Why two ISO methods
#  -------------------
#  Mint 22 / Ubuntu 24.04 ISOs are hybrid: ISO 9660 + GRUB MBR + protective
#  GPT + an *appended* EFI System Partition that is not a file in the tree.
#  Method "mkisofs" asks xorriso to describe the base ISO's boot equipment
#  ('xorriso -indev BASE -report_el_torito as_mkisofs') and reuses those
#  options verbatim for 'xorriso -as mkisofs' — the MBR template and the EFI
#  partition are read straight out of the base ISO by byte range
#  (--interval:local_fs:…:'BASE'), so BIOS + UEFI boot exactly like Mint's.
#  Method "replay" ('xorriso -indev BASE -outdev OUT -boot_image any replay
#  -update_r ISO_DIR /') lets xorriso copy the loaded image's boot records
#  into the new one; it is the documented fallback and is tried automatically
#  when the first method fails.
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# No ANSI colours: everything is tee'd into out/build.log.
export NO_COLOR=1
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="build-iso"

# run_xorriso CMD… — run an xorriso command, hide its UPDATE progress lines,
# return the command's own exit status (not grep's).
run_xorriso() {
    local rc=0
    if "$@" 2>&1 | grep -v -E '^xorriso : UPDATE'; then
        rc=0
    else
        rc="${PIPESTATUS[0]}"
    fi
    return "${rc}"
}

ROOT="$(repo_root)"
cd "${ROOT}"
lindos_load_config

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
BASE_ISO=""
SKIP_DOWNLOAD=0
SKIP_DEBS=0
ONLY_DEBS=0
SKIP_ASSETS=0
SKIP_HOOKS=""
HOOKS_ONLY=0
NO_CLEANUP=0
METHOD="${ISO_METHOD:-mkisofs}"

usage() {
    sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iso) [ $# -ge 2 ] || die "--iso needs a path" 2; BASE_ISO="$2"; shift 2 ;;
        --iso=*) BASE_ISO="${1#*=}"; shift ;;
        --skip-download) SKIP_DOWNLOAD=1; shift ;;
        --skip-debs) SKIP_DEBS=1; shift ;;
        --only-debs) ONLY_DEBS=1; shift ;;
        --skip-assets) SKIP_ASSETS=1; shift ;;
        --skip-hooks) [ $# -ge 2 ] || die "--skip-hooks needs a list" 2; SKIP_HOOKS="$2"; shift 2 ;;
        --skip-hooks=*) SKIP_HOOKS="${1#*=}"; shift ;;
        --hooks-only) HOOKS_ONLY=1; shift ;;
        --no-cleanup) NO_CLEANUP=1; shift ;;
        --method) [ $# -ge 2 ] || die "--method needs mkisofs|replay" 2; METHOD="$2"; shift 2 ;;
        --method=*) METHOD="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" 2 ;;
    esac
done
case "${METHOD}" in mkisofs|replay) ;; *) die "--method must be mkisofs or replay (got '${METHOD}')" 2 ;; esac

# ---------------------------------------------------------------------------
# Paths (absolute)
# ---------------------------------------------------------------------------
OUT_DIR="$(abs_path "${OUT_DIR}")"
WORK_DIR="$(abs_path "${WORK_DIR}")"
CACHE_DIR="$(abs_path "${CACHE_DIR}")"
DEBS_DIR="$(abs_path "${DEBS_DIR}")"
ASSETS_DIR="$(abs_path "${ASSETS_DIR}")"
BUILD_LOG="$(abs_path "${BUILD_LOG}")"
ISO_DIR="${WORK_DIR}/iso"
SQ="${WORK_DIR}/squashfs-root"
HOOK_LOG_DIR="${OUT_DIR}/hooks"
OUT_ISO="${OUT_DIR}/${ISO_NAME}"
STAGE="/tmp/lindos"                       # inside the chroot
STAGE_HOST="${SQ}${STAGE}"                # same dir seen from the host
LIVE_DIR="casper"
: "${SYNC_CASPER_KERNEL:=1}"
: "${LIVE_ONLY_PACKAGES:=lindos-installer}"   # appended to casper/filesystem.manifest-remove
: "${REQUIRE_OEM_POOL:=1}"                     # verify_oem_offline: fail the build when oem-config cannot come from the medium
: "${OEM_DEBS_DIR:=}"                          # fallback: oem-config debs of the squashfs's ubiquity version (+ closure)
: "${KEEP_ISO_MODDATE:=1}"
: "${LINDOS_PASSTHRU_VARS:=HEROIC_VERSION HEROIC_SHA256 PRISM_PPA ACCEPT_MSCOREFONTS_EULA}"

ensure_dir "${OUT_DIR}" "${WORK_DIR}" "${CACHE_DIR}" "${DEBS_DIR}" "${HOOK_LOG_DIR}" "$(dirname "${BUILD_LOG}")"

# ---------------------------------------------------------------------------
# Logging: everything to the terminal AND out/build.log
# ---------------------------------------------------------------------------
TEE_PID=""
{
    printf '\n===== Lindos build-iso.sh started %s (pid %s) =====\n' "$(date '+%F %T')" "$$"
    printf 'args: %s\n' "$*"
} >> "${BUILD_LOG}"
exec > >(tee -a "${BUILD_LOG}") 2>&1
TEE_PID=$!

# ---------------------------------------------------------------------------
# State for traps
# ---------------------------------------------------------------------------
CHROOT_ACTIVE=0
MOUNTED=()
RESOLV_BACKUP=""          # path of the backup we made (host view) or ""
POLICY_RC="${SQ}/usr/sbin/policy-rc.d"
BUILD_T0="$(date +%s)"

on_err() {
    local line="$1"
    warn "command failed at line ${line}: ${BASH_COMMAND}"
}
trap 'on_err ${LINENO}' ERR

# Ctrl-C / SIGTERM: leave through the EXIT trap so the chroot is torn down.
trap 'exit 130' INT
trap 'exit 143' TERM

on_exit() {
    local rc=$?
    trap - ERR
    set +e
    if [ "${CHROOT_ACTIVE}" -eq 1 ]; then
        warn "build interrupted while the chroot was active — cleaning up mounts"
        chroot_teardown
    fi
    local total=$(( $(date +%s) - BUILD_T0 ))
    timer_summary
    if [ "${rc}" -eq 0 ]; then
        ok "build-iso.sh finished in $(elapsed "${total}")"
    else
        printf '%s[%s] %s: ERROR:%s build FAILED (exit %s) after %s — see %s\n' \
            "${_C_ERR}" "$(date '+%F %T')" "${LOG_PREFIX}" "${_C_RST}" "${rc}" "$(elapsed "${total}")" "${BUILD_LOG}" >&2
    fi
    if [ -n "${TEE_PID}" ]; then
        exec >&- 2>&-
        wait "${TEE_PID}" 2>/dev/null
    fi
    exit "${rc}"
}
trap on_exit EXIT

# ---------------------------------------------------------------------------
# Chroot helpers
# ---------------------------------------------------------------------------
mount_one() {
    # mount_one TYPE SOURCE TARGET [OPTIONS…]
    local type="$1" src="$2" target="$3"
    shift 3
    ensure_dir "${target}"
    if [ "${type}" = "bind" ]; then
        mount --bind "${src}" "${target}" "$@"
        # On a systemd host every mount is 'shared': a bind of /dev joins
        # /dev's peer group and anything mounted *below* it (dev/pts, dev/shm)
        # would propagate back onto the HOST's /dev.  Make our copy private.
        mount --make-private "${target}" 2>/dev/null || true
    else
        mount -t "${type}" "${src}" "${target}" "$@"
    fi
    MOUNTED+=("${target}")
}

chroot_setup() {
    timer_start "chroot setup"
    [ -d "${SQ}" ] || die "squashfs-root missing: ${SQ}"
    CHROOT_ACTIVE=1
    mount_one bind /dev "${SQ}/dev"
    mount_one bind /dev/pts "${SQ}/dev/pts"
    mount_one tmpfs tmpfs "${SQ}/dev/shm" -o mode=1777,nosuid,nodev
    mount_one proc proc "${SQ}/proc"
    mount_one sysfs sysfs "${SQ}/sys"
    # /run: a private tmpfs, NOT a bind of the host's /run.  A bind would let
    # postinst scripts write into the host's /run, expose the host's D-Bus
    # sockets and make /run/systemd/system exist inside the chroot (tools
    # then believe systemd is running).  We do not need anything from the
    # host there: resolv.conf is copied as a plain file below.
    mount_one tmpfs tmpfs "${SQ}/run" -o mode=0755,nosuid,nodev
    ensure_dir "${SQ}/run/lock"
    # tmpfs on /tmp of the chroot?  No: /tmp/lindos must persist across hooks
    # and hold the (large) staged assets; it lives on the host disk and is
    # removed after the hooks.

    # resolv.conf: keep the image's own file/symlink, use the host's resolver.
    if [ -e "${SQ}/etc/resolv.conf" ] || [ -L "${SQ}/etc/resolv.conf" ]; then
        if [ ! -e "${SQ}/etc/resolv.conf.lindos-orig" ] && [ ! -L "${SQ}/etc/resolv.conf.lindos-orig" ]; then
            mv "${SQ}/etc/resolv.conf" "${SQ}/etc/resolv.conf.lindos-orig"
        else
            rm -f "${SQ}/etc/resolv.conf"
        fi
        RESOLV_BACKUP="${SQ}/etc/resolv.conf.lindos-orig"
    fi
    if [ -r /etc/resolv.conf ]; then
        cat /etc/resolv.conf > "${SQ}/etc/resolv.conf"
    else
        warn "host has no readable /etc/resolv.conf — using public resolvers inside the chroot"
        printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "${SQ}/etc/resolv.conf"
    fi
    chmod 0644 "${SQ}/etc/resolv.conf"

    # Block service starts during package installation.
    if [ -e "${POLICY_RC}" ] && [ ! -e "${POLICY_RC}.lindos-orig" ]; then
        mv "${POLICY_RC}" "${POLICY_RC}.lindos-orig"
    fi
    printf '#!/bin/sh\n# Lindos build: never start services inside the chroot\nexit 101\n' > "${POLICY_RC}"
    chmod 0755 "${POLICY_RC}"

    # /etc/hostname & /etc/hosts of the image are left untouched.
    log "chroot ready at ${SQ}"
    timer_end "chroot setup"
}

kill_chroot_processes() {
    # Anything still running with its root inside the chroot (a gpg-agent or
    # dirmngr spawned by gpg, a stray dbus-daemon, …) would keep /dev busy.
    local p root pid sig
    for sig in TERM KILL; do
        local found=0
        for p in /proc/[0-9]*; do
            pid="${p#/proc/}"
            [ "${pid}" != "$$" ] || continue
            root="$(readlink "${p}/root" 2>/dev/null || true)"
            if [ -n "${root}" ] && [ "${root}" = "${SQ}" ]; then
                found=1
                warn "process ${pid} ($(tr '\0' ' ' < "${p}/cmdline" 2>/dev/null | cut -c1-60)) still runs inside the chroot — SIG${sig}"
                kill "-${sig}" "${pid}" 2>/dev/null || true
            fi
        done
        [ "${found}" -eq 1 ] || break
        sleep 1
    done
}

chroot_teardown() {
    # Reverse-order unmount with lazy fallback; idempotent.
    local i target
    if [ -d "${SQ}" ]; then
        kill_chroot_processes
    fi
    if [ "${#MOUNTED[@]}" -gt 0 ]; then
        for (( i=${#MOUNTED[@]}-1; i>=0; i-- )); do
            target="${MOUNTED[$i]}"
            if mountpoint -q "${target}" 2>/dev/null; then
                if ! umount "${target}" 2>/dev/null; then
                    warn "busy: ${target} — lazy unmount"
                    umount -l "${target}" 2>/dev/null || warn "could not unmount ${target}"
                fi
            fi
        done
    fi
    MOUNTED=()
    # Anything else that ended up mounted below the chroot (paranoia).
    if [ -d "${SQ}" ]; then
        while IFS= read -r target; do
            [ -n "${target}" ] || continue
            warn "stray mount below chroot: ${target} — lazy unmount"
            umount -l "${target}" 2>/dev/null || true
        done < <(awk -v p="${SQ}/" 'index($2, p) == 1 {print $2}' /proc/mounts 2>/dev/null | sort -r)
    fi
    # policy-rc.d
    if [ -e "${POLICY_RC}" ] && grep -q 'Lindos build' "${POLICY_RC}" 2>/dev/null; then
        rm -f "${POLICY_RC}"
    fi
    if [ -e "${POLICY_RC}.lindos-orig" ]; then
        mv -f "${POLICY_RC}.lindos-orig" "${POLICY_RC}"
    fi
    # resolv.conf (80-cleanup.sh may already have restored it)
    if [ -n "${RESOLV_BACKUP}" ] && { [ -e "${RESOLV_BACKUP}" ] || [ -L "${RESOLV_BACKUP}" ]; }; then
        rm -f "${SQ}/etc/resolv.conf"
        mv -f "${RESOLV_BACKUP}" "${SQ}/etc/resolv.conf"
        log "restored original /etc/resolv.conf in the image"
    fi
    RESOLV_BACKUP=""
    # staged inputs
    if [ -d "${STAGE_HOST}" ]; then
        rm -rf "${STAGE_HOST}"
    fi
    CHROOT_ACTIVE=0
}

assert_no_mounts_below() {
    local dir="$1"
    local n
    n="$(awk -v p="${dir}/" 'index($2, p) == 1' /proc/mounts 2>/dev/null | wc -l)"
    if [ "${n}" -ne 0 ]; then
        awk -v p="${dir}/" 'index($2, p) == 1 {print "  still mounted: " $2}' /proc/mounts >&2
        die "refusing to continue: ${n} mount(s) still active below ${dir}"
    fi
}

# write_stage_config — the effective configuration (env overrides included)
# for the hooks, which run under 'env -i' inside the chroot.
write_stage_config() {
    local dst="$1" v names=()
    mapfile -t names < <(grep -oE '^: "\$\{[A-Za-z_][A-Za-z0-9_]*:=' "${BUILD_DIR}/config.env" | sed -E 's/^: "\$\{//; s/:=$//')
    # shellcheck disable=SC2086  # LINDOS_PASSTHRU_VARS is a space separated list
    for v in ${LINDOS_PASSTHRU_VARS}; do
        names+=("${v}")
    done
    {
        printf '# shellcheck shell=bash\n'
        printf '# Generated by build/build-iso.sh on %s — effective build configuration.\n' "$(date '+%F %T')"
        printf '# (values already include environment overrides; hooks run under env -i)\n'
        for v in "${names[@]}"; do
            if [ -n "${!v+x}" ]; then
                printf 'export %s=%q\n' "${v}" "${!v}"
            fi
        done
    } > "${dst}"
    chmod 0644 "${dst}"
}

hook_skipped() {
    # hook_skipped NAME → true when NAME (e.g. 60-compat.sh) is in SKIP_HOOKS
    local name="$1" item
    local items=()
    [ -n "${SKIP_HOOKS}" ] || return 1
    IFS=',' read -r -a items <<< "${SKIP_HOOKS}"
    for item in "${items[@]}"; do
        item="${item// /}"
        [ -n "${item}" ] || continue
        case "${name}" in
            "${item}"|"${item}.sh"|"${item}-"*) return 0 ;;
        esac
    done
    return 1
}

run_hooks() {
    local hook name hooklog rc
    local chroot_env=(
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        "HOME=/root"
        "TERM=${TERM:-xterm}"
        "DEBIAN_FRONTEND=noninteractive"
        "DEBCONF_NONINTERACTIVE_SEEN=true"
        "LC_ALL=C.UTF-8"
        "LANG=C.UTF-8"
        "LANGUAGE=C.UTF-8"
        "LINDOS_BUILD=1"
        "LINDOS_CHROOT=1"
        "LINDOS_STAGE_DIR=${STAGE}"
    )
    local v
    for v in http_proxy https_proxy ftp_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
        if [ -n "${!v:-}" ]; then
            chroot_env+=("${v}=${!v}")
        fi
    done

    local hooks=()
    while IFS= read -r hook; do
        hooks+=("${hook}")
    done < <(find "${STAGE_HOST}/hooks" -maxdepth 1 -type f -name '[0-9][0-9]-*.sh' | sort)
    [ "${#hooks[@]}" -gt 0 ] || die "no hooks found in ${STAGE_HOST}/hooks"

    for hook in "${hooks[@]}"; do
        name="$(basename "${hook}")"
        if hook_skipped "${name}"; then
            warn "hook ${name} SKIPPED (--skip-hooks)"
            continue
        fi
        hooklog="${HOOK_LOG_DIR}/${name%.sh}.log"
        timer_start "hook ${name}"
        log "log: ${hooklog}"
        rc=0
        chroot "${SQ}" /usr/bin/env -i "${chroot_env[@]}" \
            /bin/bash -Eeuo pipefail "${STAGE}/hooks/${name}" </dev/null 2>&1 | tee "${hooklog}" || rc="${PIPESTATUS[0]}"
        if [ "${rc}" -ne 0 ]; then
            die "hook ${name} failed with exit code ${rc} (see ${hooklog})"
        fi
        timer_end "hook ${name}"
    done
}

# ---------------------------------------------------------------------------
# Step 0: preflight
# ---------------------------------------------------------------------------
preflight() {
    timer_start "preflight"
    log "Lindos ${LINDOS_VERSION} \"${LINDOS_CODENAME}\" — base: Mint ${MINT_VERSION} (${BASE_UBUNTU_CODENAME})"
    log "repo: ${ROOT}"
    log "out:  ${OUT_DIR}  work: ${WORK_DIR}  cache: ${CACHE_DIR}"
    log "flags: INCLUDE_WINE=${INCLUDE_WINE} INCLUDE_STEAM=${INCLUDE_STEAM} INCLUDE_FLATPAK_LAUNCHERS=${INCLUDE_FLATPAK_LAUNCHERS} KISAK_MESA=${KISAK_MESA} method=${METHOD}"
    need_root
    # Hard requirements: what this script and its helpers actually execute.
    APT_HINT_PACKAGES="xorriso squashfs-tools rsync wget curl dosfstools mtools p7zip-full dpkg-dev git ca-certificates gnupg python3 file" \
        require_cmd xorriso unsquashfs mksquashfs rsync dpkg-deb python3 \
                    chroot mount umount mountpoint du md5sum sha256sum awk sed find tee cmp stat
    if ! have wget && ! have curl; then
        die "need wget or curl to download the base ISO (apt-get install -y wget curl)"
    fi
    if [ "${SKIP_ASSETS}" -eq 0 ] && ! have git; then
        die "git is required by build/fetch-assets.sh (apt-get install -y git) — or pass --skip-assets"
    fi
    # Nice to have (SPEC §8 lists them; nothing here calls them directly).
    local c
    for c in mkfs.vfat mcopy 7z gpg file unzip; do
        have "${c}" || warn "optional tool missing: ${c} (apt-get install -y dosfstools mtools p7zip-full gnupg file unzip)"
    done
    local free
    free="$(free_gb "${OUT_DIR}")"
    if [ "${free:-0}" -lt "${MIN_FREE_GB}" ]; then
        die "only ${free} GiB free on the filesystem holding ${OUT_DIR}; need at least ${MIN_FREE_GB} GiB (set MIN_FREE_GB to override)"
    fi
    log "free space: ${free} GiB (minimum ${MIN_FREE_GB})"
    if [ "${HOOKS_ONLY}" -eq 1 ]; then
        [ -d "${SQ}" ] && [ -d "${ISO_DIR}/${LIVE_DIR}" ] \
            || die "--hooks-only needs ${SQ} and ${ISO_DIR} from a previous --no-cleanup run"
    fi
    timer_end "preflight"
}

# ---------------------------------------------------------------------------
# Step 1: debs
# ---------------------------------------------------------------------------
build_debs() {
    if [ "${SKIP_DEBS}" -eq 1 ]; then
        log "--skip-debs: using existing ${DEBS_DIR}"
    else
        timer_start "build debs"
        bash "${BUILD_DIR}/mkdeb.sh" --out "${DEBS_DIR}" all
        timer_end "build debs"
    fi
    local n
    n="$(find "${DEBS_DIR}" -maxdepth 1 -name '*.deb' | wc -l)"
    [ "${n}" -gt 0 ] || die "no .deb files in ${DEBS_DIR}"
    log "${n} .deb file(s) ready in ${DEBS_DIR}"
}

# ---------------------------------------------------------------------------
# Step 2: assets (theme/icons/cursors/fonts) — build/fetch-assets.sh
# ---------------------------------------------------------------------------
fetch_assets() {
    if [ "${SKIP_ASSETS}" -eq 1 ]; then
        log "--skip-assets: not running fetch-assets.sh"
        return 0
    fi
    if [ ! -x "${BUILD_DIR}/fetch-assets.sh" ] && [ ! -f "${BUILD_DIR}/fetch-assets.sh" ]; then
        warn "build/fetch-assets.sh not found — Lindos GTK/icon themes and fonts will be missing from the ISO"
        return 0
    fi
    timer_start "fetch assets"
    # SPEC §8: 'build/fetch-assets.sh --out out/assets' (--out is an alias of
    # --assets-dir; the script also honours ASSETS_DIR from the environment).
    # Its output must contain install-into-chroot.sh, which stage_inputs()
    # copies to /tmp/lindos/assets/ and 40-theme.sh runs inside the chroot.
    log "fetch-assets.sh --out ${ASSETS_DIR}"
    export ASSETS_DIR
    if ! bash "${BUILD_DIR}/fetch-assets.sh" --out "${ASSETS_DIR}"; then
        warn "fetch-assets.sh failed — continuing without (theme falls back to Mint's)"
    fi
    if [ -f "${ASSETS_DIR}/install-into-chroot.sh" ]; then
        log "assets ready: ${ASSETS_DIR}/install-into-chroot.sh"
    else
        warn "fetch-assets.sh did not produce ${ASSETS_DIR}/install-into-chroot.sh — 40-theme.sh will skip the Fluent/Selawik install"
    fi
    timer_end "fetch assets"
}

# ---------------------------------------------------------------------------
# Step 3: base ISO
# ---------------------------------------------------------------------------
sha256_of() { sha256sum "$1" | awk '{print $1}'; }

download_base_iso() {
    timer_start "base iso"
    if [ -n "${BASE_ISO}" ]; then
        BASE_ISO="$(abs_path "${BASE_ISO}")"
        [ -f "${BASE_ISO}" ] || die "--iso: file not found: ${BASE_ISO}"
        log "using local base ISO: ${BASE_ISO}"
    else
        BASE_ISO="${CACHE_DIR}/${BASE_ISO_FILE}"
        if [ "${SKIP_DOWNLOAD}" -eq 1 ]; then
            [ -f "${BASE_ISO}" ] || die "--skip-download but ${BASE_ISO} does not exist"
            log "--skip-download: using cached ${BASE_ISO}"
        else
            case "${BASE_ISO_URL}" in
                file://*)
                    local src="${BASE_ISO_URL#file://}"
                    [ -f "${src}" ] || die "BASE_ISO_URL points to a missing file: ${src}"
                    if [ ! -f "${BASE_ISO}" ] || ! cmp -s "${src}" "${BASE_ISO}"; then
                        log "copying ${src} → ${BASE_ISO}"
                        cp -f "${src}" "${BASE_ISO}"
                    fi ;;
                *)
                    log "downloading (resumable): ${BASE_ISO_URL}"
                    if ! wget -c --progress=dot:giga --tries=5 --timeout=60 -O "${BASE_ISO}" "${BASE_ISO_URL}"; then
                        warn "wget failed — trying curl"
                        curl -fL --retry 5 -C - -o "${BASE_ISO}" "${BASE_ISO_URL}" || die "download failed: ${BASE_ISO_URL}"
                    fi ;;
            esac
        fi
    fi
    local size
    size="$(stat -c %s "${BASE_ISO}")"
    log "base ISO: ${BASE_ISO} ($(human_size "${size}"))"
    [ "${size}" -gt $(( 500 * 1024 * 1024 )) ] || die "base ISO is suspiciously small (${size} bytes) — incomplete download?"

    if [ -n "${BASE_ISO_SHA256}" ]; then
        log "verifying sha256…"
        local got
        got="$(sha256_of "${BASE_ISO}")"
        if [ "${got}" != "${BASE_ISO_SHA256}" ]; then
            die "sha256 mismatch for ${BASE_ISO}: expected ${BASE_ISO_SHA256} got ${got} (delete the file to re-download)"
        fi
        ok "sha256 verified"
    else
        warn "BASE_ISO_SHA256 is empty — base ISO NOT verified (fill it in build/config.env for release builds)"
    fi
    timer_end "base iso"
}

# ---------------------------------------------------------------------------
# Step 4: extract ISO tree + unsquash
# ---------------------------------------------------------------------------
extract_iso() {
    timer_start "extract iso"
    rm -rf "${ISO_DIR}"
    ensure_dir "${ISO_DIR}"
    log "xorriso -osirrox on -indev ${BASE_ISO} -extract / ${ISO_DIR}"
    run_xorriso xorriso -osirrox on -indev "${BASE_ISO}" -extract / "${ISO_DIR}" \
        || die "xorriso could not extract ${BASE_ISO}"
    chmod -R u+w "${ISO_DIR}"
    [ -d "${ISO_DIR}/${LIVE_DIR}" ] || die "no ${LIVE_DIR}/ directory in the base ISO — not a casper live ISO?"
    if [ ! -f "${ISO_DIR}/${LIVE_DIR}/filesystem.squashfs" ]; then
        find "${ISO_DIR}/${LIVE_DIR}" -name '*.squashfs' -printf '  %f\n' >&2 || true
        die "${LIVE_DIR}/filesystem.squashfs not found (layered squashfs ISOs are not supported by this remaster)"
    fi
    # Keep a copy of the original boot configs for reference/debugging.
    rm -rf "${WORK_DIR}/orig"
    ensure_dir "${WORK_DIR}/orig"
    if [ -d "${ISO_DIR}/boot/grub" ]; then cp -a "${ISO_DIR}/boot/grub" "${WORK_DIR}/orig/grub"; fi
    if [ -d "${ISO_DIR}/isolinux" ]; then cp -a "${ISO_DIR}/isolinux" "${WORK_DIR}/orig/isolinux"; fi
    if [ -f "${ISO_DIR}/.disk/info" ]; then cp -a "${ISO_DIR}/.disk/info" "${WORK_DIR}/orig/disk-info"; fi
    log "base .disk/info: $(cat "${ISO_DIR}/.disk/info" 2>/dev/null || echo '?')"
    ls -la "${ISO_DIR}/${LIVE_DIR}" | sed 's/^/  /' >&2
    timer_end "extract iso"
}

unsquash() {
    timer_start "unsquashfs"
    rm -rf "${SQ}"
    local extra=()
    if unsquashfs -help 2>&1 | grep -q -- '-no-progress'; then extra+=(-no-progress); fi
    log "unsquashfs -d ${SQ} ${ISO_DIR}/${LIVE_DIR}/filesystem.squashfs"
    unsquashfs "${extra[@]}" -d "${SQ}" "${ISO_DIR}/${LIVE_DIR}/filesystem.squashfs"
    [ -x "${SQ}/bin/bash" ] || [ -x "${SQ}/usr/bin/bash" ] || die "unsquashed tree has no /bin/bash — corrupt squashfs?"
    log "squashfs-root: $(du -sh "${SQ}" 2>/dev/null | cut -f1)"
    timer_end "unsquashfs"
}

# ---------------------------------------------------------------------------
# Step 5: stage inputs + hooks
# ---------------------------------------------------------------------------
stage_inputs() {
    timer_start "stage inputs"
    rm -rf "${STAGE_HOST}"
    ensure_dir "${STAGE_HOST}/debs" "${STAGE_HOST}/hooks" "${STAGE_HOST}/assets"
    cp -f "${DEBS_DIR}"/*.deb "${STAGE_HOST}/debs/"
    cp -f "${BUILD_DIR}/chroot/"*.sh "${STAGE_HOST}/hooks/"
    chmod 0755 "${STAGE_HOST}/hooks/"*.sh
    strip_crlf "${STAGE_HOST}/hooks/"*.sh
    if [ -d "${ASSETS_DIR}" ] && [ "${SKIP_ASSETS}" -eq 0 ]; then
        # (.git of the theme checkouts and the raw zip downloads are not needed
        # inside the chroot — install-into-chroot.sh works from the trees)
        rsync -a --delete --exclude '.git' --exclude '/downloads' "${ASSETS_DIR}/" "${STAGE_HOST}/assets/"
    fi
    write_stage_config "${STAGE_HOST}/config.env"
    # Installer branding inputs (slideshow, GTK skin) for build/chroot/78-installer-brand.sh.
    if [ -d "${BUILD_DIR}/installer" ]; then
        ensure_dir "${STAGE_HOST}/installer"
        rsync -a --delete "${BUILD_DIR}/installer/" "${STAGE_HOST}/installer/"
        find "${STAGE_HOST}/installer" -type f \( -name '*.html' -o -name '*.css' \) -exec sed -i 's/\r$//' {} +
    else
        warn "no build/installer directory: the installer keeps the base ISO's product artwork"
    fi
    log "staged: $(find "${STAGE_HOST}/debs" -name '*.deb' | wc -l) debs, $(find "${STAGE_HOST}/hooks" -name '[0-9][0-9]-*.sh' | wc -l) hooks, $(find "${STAGE_HOST}/installer" -type f 2>/dev/null | wc -l) installer files, assets: $(du -sh "${STAGE_HOST}/assets" 2>/dev/null | cut -f1)"
    timer_end "stage inputs"
}

# ---------------------------------------------------------------------------
# Step 6: casper metadata + squashfs
# ---------------------------------------------------------------------------
sync_casper_kernel() {
    # Copy the (possibly regenerated) kernel + initrd from the chroot into
    # casper/ so the live boot uses the same modules and the Lindos plymouth
    # theme.  Same approach as Cubic; disable with SYNC_CASPER_KERNEL=0.
    [ "${SYNC_CASPER_KERNEL}" = "1" ] || { log "SYNC_CASPER_KERNEL=${SYNC_CASPER_KERNEL}: keeping the base ISO's casper kernel/initrd"; return 0; }
    local kver vmlinuz initrd target_initrd
    kver="$(find "${SQ}/boot" -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' 2>/dev/null | sed 's/^vmlinuz-//' | sort -V | tail -n1)"
    if [ -z "${kver}" ]; then
        warn "no /boot/vmlinuz-* in squashfs-root — casper kernel/initrd left unchanged"
        return 0
    fi
    vmlinuz="${SQ}/boot/vmlinuz-${kver}"
    initrd="${SQ}/boot/initrd.img-${kver}"
    if [ ! -f "${initrd}" ]; then
        warn "no ${initrd} — casper kernel/initrd left unchanged"
        return 0
    fi
    if [ -f "${ISO_DIR}/${LIVE_DIR}/initrd.lz" ]; then target_initrd=initrd.lz
    elif [ -f "${ISO_DIR}/${LIVE_DIR}/initrd" ]; then target_initrd=initrd
    else target_initrd=initrd.lz; fi
    if cmp -s "${vmlinuz}" "${ISO_DIR}/${LIVE_DIR}/vmlinuz"; then
        log "kernel ${kver} unchanged (casper/vmlinuz identical)"
    else
        log "updating ${LIVE_DIR}/vmlinuz from /boot/vmlinuz-${kver}"
        cp -f "${vmlinuz}" "${ISO_DIR}/${LIVE_DIR}/vmlinuz"
    fi
    if cmp -s "${initrd}" "${ISO_DIR}/${LIVE_DIR}/${target_initrd}"; then
        log "initrd unchanged (casper/${target_initrd} identical)"
    else
        log "updating ${LIVE_DIR}/${target_initrd} from /boot/initrd.img-${kver} ($(human_size "$(stat -c %s "${initrd}")"))"
        cp -f "${initrd}" "${ISO_DIR}/${LIVE_DIR}/${target_initrd}"
    fi
    chmod 0644 "${ISO_DIR}/${LIVE_DIR}/vmlinuz" "${ISO_DIR}/${LIVE_DIR}/${target_initrd}"
}

casper_metadata() {
    timer_start "casper metadata"
    local cdir="${ISO_DIR}/${LIVE_DIR}"
    log "filesystem.manifest"
    # Same format as Ubuntu/Mint (binary:Package keeps the :i386 qualifier of
    # multi-arch packages, tab separated) — ubiquity diffs it against
    # filesystem.manifest-remove to know what to drop after installation.
    chroot "${SQ}" /usr/bin/dpkg-query -W --showformat='${binary:Package}\t${Version}\n' > "${cdir}/filesystem.manifest"
    log "  $(wc -l < "${cdir}/filesystem.manifest") packages"
    if [ -f "${cdir}/filesystem.manifest-remove" ]; then
        log "filesystem.manifest-remove kept from base ($(wc -l < "${cdir}/filesystem.manifest-remove") entries)"
        # Packages that exist only on the installation medium and must not be in the installed system:
        # the Lindos installer scripts (lindos-installer), like the ubiquity family the base lists.
        local live_only
        for live_only in ${LIVE_ONLY_PACKAGES}; do
            if ! grep -qxF "${live_only}" "${cdir}/filesystem.manifest-remove"; then
                if [ -n "$(tail -c 1 "${cdir}/filesystem.manifest-remove")" ]; then
                    printf '\n' >> "${cdir}/filesystem.manifest-remove"
                fi
                printf '%s\n' "${live_only}" >> "${cdir}/filesystem.manifest-remove"
                log "  + ${live_only} added to filesystem.manifest-remove (removed from the installed system)"
            fi
        done
    else
        warn "base ISO has no filesystem.manifest-remove (installer will keep live-only packages)"
    fi
    if [ -f "${cdir}/filesystem.manifest-desktop" ]; then
        # Older Ubuntu layout: manifest-desktop = manifest minus manifest-remove.
        if [ -f "${cdir}/filesystem.manifest-remove" ]; then
            awk 'NR==FNR { rm[$1]=1; next } !($1 in rm)' \
                "${cdir}/filesystem.manifest-remove" "${cdir}/filesystem.manifest" \
                > "${cdir}/filesystem.manifest-desktop"
        else
            cp -f "${cdir}/filesystem.manifest" "${cdir}/filesystem.manifest-desktop"
        fi
        log "filesystem.manifest-desktop regenerated"
    fi
    log "filesystem.size"
    du -sx --block-size=1 "${SQ}" | cut -f1 > "${cdir}/filesystem.size"
    log "  $(human_size "$(cat "${cdir}/filesystem.size")") uncompressed"
    sync_casper_kernel
    timer_end "casper metadata"
}

make_squashfs() {
    timer_start "mksquashfs"
    assert_no_mounts_below "${SQ}"
    local out="${ISO_DIR}/${LIVE_DIR}/filesystem.squashfs"
    rm -f "${out}"
    local sq_args=()
    read -r -a sq_args <<< "${SQUASHFS_ARGS}"
    local extra=()
    if mksquashfs -help 2>&1 | grep -q -- '-no-progress'; then extra+=(-no-progress); fi
    log "mksquashfs ${SQ} ${out} -comp ${SQUASHFS_COMP} ${SQUASHFS_ARGS} -noappend -wildcards -e proc/* sys/* dev/* run/* tmp/*"
    mksquashfs "${SQ}" "${out}" -comp "${SQUASHFS_COMP}" "${sq_args[@]}" -noappend "${extra[@]}" \
        -wildcards -e 'proc/*' 'sys/*' 'dev/*' 'run/*' 'tmp/*' 'var/tmp/*' 'root/.cache/*' 'var/crash/*'
    chmod 0644 "${out}"
    log "filesystem.squashfs: $(human_size "$(stat -c %s "${out}")")"
    timer_end "mksquashfs"
}

# ---------------------------------------------------------------------------
# Step 7: overlay + branding
# ---------------------------------------------------------------------------
brand_file() {
    # brand_file FILE — replace Mint product strings with Lindos ones (in place)
    local f="$1"
    [ -f "${f}" ] || return 0
    is_text_file "${f}" || return 0
    local before after
    before="$(md5sum < "${f}")"
    sed -i -E \
        -e "s/Start Linux Mint [0-9][0-9.]* [A-Za-z]+ 64-bit/Start Lindos ${LINDOS_VERSION}/g" \
        -e "s/Linux Mint [0-9][0-9.]* [A-Za-z]+ 64-bit/Lindos ${LINDOS_VERSION} (${LINDOS_CODENAME})/g" \
        -e "s/Linux Mint [0-9][0-9.]* \"[A-Za-z]+\"/Lindos ${LINDOS_VERSION} \"${LINDOS_CODENAME}\"/g" \
        -e "s/Linux Mint [0-9][0-9.]*/Lindos ${LINDOS_VERSION}/g" \
        -e "s/Linux Mint/Lindos/g" \
        -e "s/LinuxMint/Lindos/g" \
        "${f}"
    after="$(md5sum < "${f}")"
    if [ "${before}" != "${after}" ]; then
        log "branded: ${f#"${ISO_DIR}"/}"
    fi
}

fix_boot_paths() {
    # fix_boot_paths FILE — make sure kernel/initrd names in a boot config
    # match what casper/ actually contains (initrd.lz vs initrd).
    local f="$1"
    [ -f "${f}" ] || return 0
    local cdir="${ISO_DIR}/${LIVE_DIR}"
    if grep -q "/${LIVE_DIR}/initrd\.lz" "${f}" && [ ! -f "${cdir}/initrd.lz" ] && [ -f "${cdir}/initrd" ]; then
        sed -i "s#/${LIVE_DIR}/initrd\.lz#/${LIVE_DIR}/initrd#g" "${f}"
        log "boot config ${f#"${ISO_DIR}"/}: initrd.lz → initrd"
    elif grep -qE "/${LIVE_DIR}/initrd([^.a-z]|$)" "${f}" && [ ! -f "${cdir}/initrd" ] && [ -f "${cdir}/initrd.lz" ]; then
        sed -i -E "s#/${LIVE_DIR}/initrd([^.a-z]|$)#/${LIVE_DIR}/initrd.lz\1#g" "${f}"
        log "boot config ${f#"${ISO_DIR}"/}: initrd → initrd.lz"
    fi
    if grep -q "/${LIVE_DIR}/vmlinuz" "${f}" && [ ! -f "${cdir}/vmlinuz" ]; then
        warn "${f#"${ISO_DIR}"/} references /${LIVE_DIR}/vmlinuz which does not exist!"
    fi
}

apply_overlay() {
    timer_start "overlay + branding"
    local overlay="${BUILD_DIR}/overlay"
    local f rel
    if [ -d "${overlay}" ]; then
        log "rsync overlay → ISO tree"
        rsync -a --no-owner --no-group --exclude '.gitkeep' --exclude '*.md' "${overlay}/" "${ISO_DIR}/"
        # Fill @PLACEHOLDERS@ in the text files that came from the overlay.
        local vshort="${LINDOS_VERSION%.*}"
        # Mint's boot entries pass 'file=/cdrom/preseed/linuxmint.seed' to
        # casper/ubiquity (installer defaults: no language-pack downloads,
        # no auto-updates prompt, …).  Keep it when the base ISO ships a seed.
        local preseed="" seedfile
        for seedfile in "${ISO_DIR}/preseed/linuxmint.seed" "${ISO_DIR}"/preseed/*.seed; do
            if [ -f "${seedfile}" ]; then
                preseed="file=/cdrom/preseed/$(basename "${seedfile}") "
                break
            fi
        done
        if [ -n "${preseed}" ]; then
            log "boot entries keep the base preseed: ${preseed% }"
        else
            log "base ISO has no preseed/*.seed — boot entries carry no file= argument"
        fi
        while IFS= read -r f; do
            rel="${f#"${overlay}"/}"
            [ -f "${ISO_DIR}/${rel}" ] || continue
            is_text_file "${ISO_DIR}/${rel}" || continue
            if grep -q '@[A-Z_]*@' "${ISO_DIR}/${rel}"; then
                sed -i \
                    -e "s|@LINDOS_VERSION_SHORT@|${vshort}|g" \
                    -e "s|@LINDOS_VERSION@|${LINDOS_VERSION}|g" \
                    -e "s|@LINDOS_CODENAME@|${LINDOS_CODENAME}|g" \
                    -e "s|@MINT_VERSION@|${MINT_VERSION}|g" \
                    -e "s|@DISK_INFO@|${DISK_INFO}|g" \
                    -e "s|@ISO_VOLID@|${ISO_VOLID}|g" \
                    -e "s|@PRESEED@|${preseed}|g" \
                    "${ISO_DIR}/${rel}"
                log "overlay: ${rel} (placeholders filled)"
            else
                log "overlay: ${rel}"
            fi
        done < <(find "${overlay}" -type f ! -name '.gitkeep' ! -name '*.md')
    else
        warn "no build/overlay directory"
    fi
    ensure_dir "${ISO_DIR}/.disk"
    printf '%s\n' "${DISK_INFO}" > "${ISO_DIR}/.disk/info"
    log ".disk/info: ${DISK_INFO}"
    if [ -f "${ISO_DIR}/.disk/release_notes_url" ]; then
        printf '%s/releases/%s\n' "${LINDOS_HOME_URL}" "${LINDOS_VERSION}" > "${ISO_DIR}/.disk/release_notes_url"
    fi

    # BIOS boots through ISOLINUX, whose base entries say username=mint and know nothing of the Lindos
    # flow: regenerate its casper entries from the GRUB entries just filled in (single source of truth).
    if [ -f "${ISO_DIR}/isolinux/live.cfg" ] && [ -f "${ISO_DIR}/boot/grub/grub.cfg" ]; then
        python3 "${BUILD_DIR}/lib/boot_menu.py" isolinux --grub "${ISO_DIR}/boot/grub/grub.cfg" \
            --live "${ISO_DIR}/isolinux/live.cfg" --write \
            || die "could not rewrite isolinux/live.cfg from the GRUB entries (BIOS boot would keep the base entries)"
    else
        log "no isolinux/live.cfg in the ISO tree - nothing to rewrite for BIOS boot"
    fi

    # Brand every boot config / text that still carries Mint's product name.
    local f
    while IFS= read -r f; do
        brand_file "${f}"
    done < <(find "${ISO_DIR}/boot" "${ISO_DIR}/isolinux" "${ISO_DIR}/EFI" -type f \( -name '*.cfg' -o -name '*.txt' -o -name '*.conf' \) 2>/dev/null)
    brand_file "${ISO_DIR}/README.diskdefines"
    for f in "${ISO_DIR}/boot/grub/grub.cfg" "${ISO_DIR}/boot/grub/loopback.cfg" \
             "${ISO_DIR}/boot/grub/x86_64-efi/grub.cfg" "${ISO_DIR}/boot/grub/i386-efi/grub.cfg" \
             "${ISO_DIR}"/isolinux/*.cfg; do
        fix_boot_paths "${f}"
    done
    if [ -f "${ISO_DIR}/boot/grub/grub.cfg" ]; then
        log "grub.cfg menu entries:"
        grep -E '^\s*menuentry' "${ISO_DIR}/boot/grub/grub.cfg" | sed 's/^/    /' >&2 || true
        grep -q 'boot=casper' "${ISO_DIR}/boot/grub/grub.cfg" || die "boot/grub/grub.cfg lost 'boot=casper' — refusing to build an unbootable ISO"
        if grep -v '^[[:space:]]*#' "${ISO_DIR}/boot/grub/grub.cfg" | grep -q '@[A-Z_][A-Z_]*@'; then
            die "boot/grub/grub.cfg still contains an unfilled @PLACEHOLDER@: $(grep -v '^[[:space:]]*#' "${ISO_DIR}/boot/grub/grub.cfg" | grep -o '@[A-Z_][A-Z_]*@' | sort -u | tr '\n' ' ')"
        fi
        # The installer flow lives in these entries: the first one is the installer-only OEM entry.
        local first_linux
        first_linux="$(grep -E '^[[:space:]]*linux[[:space:]]' "${ISO_DIR}/boot/grub/grub.cfg" | head -n 1)"
        case "${first_linux}" in
            *only-ubiquity*oem-config/enable=true*|*oem-config/enable=true*only-ubiquity*) ;;
            *) die "the first boot entry lost 'only-ubiquity oem-config/enable=true': ${first_linux}" ;;
        esac
        if grep -rEq --include='*.cfg' 'username=mint|hostname=mint' "${ISO_DIR}/boot/grub" "${ISO_DIR}/isolinux" 2>/dev/null; then
            die "a boot entry still says username=mint/hostname=mint (the live user is liveuser@lindos): $(grep -rEl --include='*.cfg' 'username=mint|hostname=mint' "${ISO_DIR}/boot/grub" "${ISO_DIR}/isolinux" | tr '\n' ' ')"
        fi
        # The kernel/initrd the menu points at must exist in the tree.
        local kimg iimg
        kimg="$(grep -oE '^\s*linux\s+\S+' "${ISO_DIR}/boot/grub/grub.cfg" | awk '{print $2; exit}')"
        iimg="$(grep -oE '^\s*initrd\s+\S+' "${ISO_DIR}/boot/grub/grub.cfg" | awk '{print $2; exit}')"
        [ -n "${kimg}" ] && [ -f "${ISO_DIR}${kimg}" ] || die "grub.cfg kernel '${kimg:-?}' not found in the ISO tree"
        [ -n "${iimg}" ] && [ -f "${ISO_DIR}${iimg}" ] || die "grub.cfg initrd '${iimg:-?}' not found in the ISO tree"
        log "grub.cfg boots ${kimg} + ${iimg}"
    else
        die "boot/grub/grub.cfg missing in ISO tree"
    fi
    if [ -d "${ISO_DIR}/isolinux" ]; then
        log "isolinux/ present (BIOS via ISOLINUX or legacy leftovers): configs branded via sed"
    fi
    timer_end "overlay + branding"
}

# ---------------------------------------------------------------------------
# Step 7b: can the installed system get oem-config from the medium?
# ---------------------------------------------------------------------------
# Ubiquity's OEM mode installs oem-config-gtk from the medium's pool and silently skips it when the
# pool has no matching version - the install would "succeed" without the first-boot account wizard.
# (No Lindos hook upgrades ubiquity, so the pool of the base ISO matches the squashfs; this proves it.)
verify_oem_offline() {
    timer_start "oem-config pool check"
    local fallback=() result rc=0
    if [ -n "${OEM_DEBS_DIR}" ]; then
        fallback=(--fallback-dir "$(abs_path "${OEM_DEBS_DIR}")" --copy-fallback-to "${ISO_DIR}/lindos/oem-debs")
    fi
    result="$(python3 "${BUILD_DIR}/lib/verify_oem_pool.py" --iso-dir "${ISO_DIR}" --squashfs-root "${SQ}" "${fallback[@]}")" || rc=$?
    printf '%s\n' "${result}" >&2
    if [ "${rc}" -ne 0 ] || printf '%s' "${result}" | grep -q 'NOT ok'; then
        if [ "${REQUIRE_OEM_POOL}" = "1" ]; then
            die "oem-config cannot be installed from this medium (see above); refusing to build an ISO whose first boot would have no account wizard (REQUIRE_OEM_POOL=0 overrides, OEM_DEBS_DIR names a fallback directory)"
        fi
        warn "REQUIRE_OEM_POOL=${REQUIRE_OEM_POOL}: continuing without a usable oem-config source"
    fi
    timer_end "oem-config pool check"
}

# ---------------------------------------------------------------------------
# Step 8: md5sum.txt
# ---------------------------------------------------------------------------
regen_md5() {
    timer_start "md5sum.txt"
    ( cd "${ISO_DIR}" && find . -type f \
        ! -name md5sum.txt ! -name 'sha256sum.txt' \
        ! -path './isolinux/*boot.cat' ! -name 'boot.catalog' \
        ! -path './boot/grub/i386-pc/eltorito.img' ! -name 'isolinux.bin' \
        -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum \
        | sed 's|^\([0-9a-f]*\) [ *]\./|\1  ./|' ) > "${ISO_DIR}/md5sum.txt"
    log "md5sum.txt: $(wc -l < "${ISO_DIR}/md5sum.txt") entries"
    timer_end "md5sum.txt"
}

# ---------------------------------------------------------------------------
# Step 9: ISO
# ---------------------------------------------------------------------------
ELTORITO_REPORT="${WORK_DIR}/eltorito-report.txt"
ISO_MODDATE=""

capture_eltorito_report() {
    log "xorriso -indev ${BASE_ISO} -report_el_torito as_mkisofs"
    if ! xorriso -indev "${BASE_ISO}" -report_el_torito as_mkisofs > "${ELTORITO_REPORT}" 2> "${ELTORITO_REPORT}.err"; then
        warn "xorriso could not report the base ISO's boot equipment (see ${ELTORITO_REPORT}.err)"
        return 1
    fi
    sed 's/^/    /' "${ELTORITO_REPORT}" >&2
    ISO_MODDATE="$(grep -oE "^--modification-date='?[0-9]+'?" "${ELTORITO_REPORT}" | grep -oE '[0-9]+' | head -n1 || true)"
    [ -n "${ISO_MODDATE}" ] && log "base ISO modification date (GRUB fs uuid): ${ISO_MODDATE}"
    return 0
}

build_iso_mkisofs() {
    # Method 1: reuse the exact mkisofs-style boot options of the base ISO.
    local opts=() python_args=() boot_paths=() p
    local opts_file="${WORK_DIR}/mkisofs-opts.txt"
    python_args=(--report "${ELTORITO_REPORT}" --iso "${BASE_ISO}" --check --format lines)
    # Keeping the modification date keeps the ISO 9660 "fs uuid" GRUB derives
    # from it — the EFI GRUB in the appended partition may search by it.
    [ "${KEEP_ISO_MODDATE}" = "1" ] && python_args+=(--keep-moddate)
    if ! python3 "${BUILD_DIR}/lib/eltorito_opts.py" "${python_args[@]}" > "${opts_file}"; then
        warn "eltorito_opts.py could not turn the report into mkisofs options (see ${ELTORITO_REPORT})"
        return 1
    fi
    mapfile -t opts < "${opts_file}"
    [ "${#opts[@]}" -gt 0 ] || { warn "empty option list from eltorito_opts.py"; return 1; }
    mapfile -t boot_paths < <(python3 "${BUILD_DIR}/lib/eltorito_opts.py" --report "${ELTORITO_REPORT}" --print-boot-paths || true)
    for p in "${boot_paths[@]}"; do
        p="/${p#/}"
        if [ ! -f "${ISO_DIR}${p}" ]; then
            warn "boot image ${p} referenced by the base ISO is missing from the tree"
            return 1
        fi
        log "boot image present: ${p}"
    done
    log "mkisofs options reused from the base ISO (${#opts[@]} tokens): $(tr '\n' ' ' < "${opts_file}")"
    rm -f "${OUT_ISO}"
    log "xorriso -as mkisofs -r -J -joliet-long -l -iso-level 3 -V ${ISO_VOLID} … -o ${OUT_ISO} ${ISO_DIR}"
    run_xorriso xorriso -as mkisofs \
        -r -J -joliet-long -l -iso-level 3 \
        -V "${ISO_VOLID}" \
        -A "${ISO_APPID}" \
        -publisher "${ISO_PUBLISHER}" \
        -p "Lindos build-iso.sh" \
        -o "${OUT_ISO}" \
        "${opts[@]}" \
        "${ISO_DIR}" || return 1
    [ -s "${OUT_ISO}" ] || return 1
    return 0
}

build_iso_replay() {
    # Method 2: let xorriso replay the loaded image's boot equipment.
    local extra=()
    if [ -n "${ISO_MODDATE}" ] && [ "${KEEP_ISO_MODDATE}" = "1" ]; then
        extra+=(-volume_date uuid "${ISO_MODDATE}")
    fi
    rm -f "${OUT_ISO}"
    # -update_r (not -map): files that vanished from ISO_DIR are removed from
    # the new image too, changed files are replaced, everything else is kept
    # from the loaded base image together with its boot equipment (replay).
    log "xorriso -indev ${BASE_ISO} -outdev ${OUT_ISO} -boot_image any replay -update_r ${ISO_DIR} / -commit"
    run_xorriso xorriso -indev "${BASE_ISO}" -outdev "${OUT_ISO}" \
        -boot_image any replay \
        -joliet on -compliance joliet_long_names \
        -volid "${ISO_VOLID}" -publisher "${ISO_PUBLISHER}" -application_id "${ISO_APPID}" \
        "${extra[@]}" \
        -update_r "${ISO_DIR}" / \
        -commit -end || return 1
    [ -s "${OUT_ISO}" ] || return 1
    return 0
}

build_iso() {
    timer_start "xorriso iso"
    # The El Torito catalog is (re)generated by xorriso at the -c path; the
    # copy extracted from the base ISO must not sit in the tree as a plain file.
    rm -f "${ISO_DIR}/boot.catalog" "${ISO_DIR}/isolinux/boot.cat"
    capture_eltorito_report || true
    local ok_method=""
    if [ "${METHOD}" = "mkisofs" ]; then
        if [ -s "${ELTORITO_REPORT}" ] && build_iso_mkisofs; then
            ok_method="mkisofs (report_el_torito as_mkisofs)"
        else
            warn "method mkisofs failed — falling back to 'boot_image any replay'"
            if build_iso_replay; then ok_method="replay (fallback)"; fi
        fi
    else
        if build_iso_replay; then
            ok_method="replay"
        else
            warn "method replay failed — falling back to 'as mkisofs' with the reported options"
            if [ -s "${ELTORITO_REPORT}" ] && build_iso_mkisofs; then ok_method="mkisofs (fallback)"; fi
        fi
    fi
    [ -n "${ok_method}" ] || die "could not build the ISO with either method (see ${BUILD_LOG})"
    ok "ISO written with method: ${ok_method}"
    timer_end "xorriso iso"
}

verify_iso() {
    timer_start "verify iso"
    local size
    size="$(stat -c %s "${OUT_ISO}")"
    log "ISO: ${OUT_ISO} ($(human_size "${size}"))"
    log "xorriso -indev OUT -toc / boot report:"
    xorriso -indev "${OUT_ISO}" -toc -report_el_torito plain -report_system_area plain 2>&1 \
        | grep -v -E '^xorriso : (UPDATE|NOTE)' | sed 's/^/    /' >&2 || warn "xorriso could not read back the ISO"
    if have isoinfo; then
        isoinfo -d -i "${OUT_ISO}" 2>/dev/null | grep -E 'Volume id|Application id|Publisher|El Torito|Joliet|Rock Ridge' | sed 's/^/    /' >&2 || true
    fi
    local listing f iso_bad=0
    listing="$(xorriso -indev "${OUT_ISO}" -ls "/${LIVE_DIR}/" 2>/dev/null || true)"
    for f in filesystem.squashfs filesystem.manifest filesystem.size vmlinuz; do
        if printf '%s\n' "${listing}" | grep -q "${f}"; then
            log "  ok: /${LIVE_DIR}/${f}"
        else
            warn "  MISSING in ISO: /${LIVE_DIR}/${f}"
            iso_bad=1
        fi
    done
    if printf '%s\n' "${listing}" | grep -qE 'initrd(\.lz)?'; then
        log "  ok: /${LIVE_DIR}/initrd*"
    else
        warn "  MISSING in ISO: /${LIVE_DIR}/initrd"; iso_bad=1
    fi
    for f in /boot/grub/grub.cfg /.disk/info /md5sum.txt; do
        if xorriso -indev "${OUT_ISO}" -ls "${f}" >/dev/null 2>&1; then
            log "  ok: ${f}"
        else
            warn "  MISSING in ISO: ${f}"; iso_bad=1
        fi
    done
    [ "${iso_bad}" -eq 0 ] || die "ISO content check failed"
    ( cd "${OUT_DIR}" && sha256sum "${ISO_NAME}" > "${ISO_NAME}.sha256" )
    log "sha256: $(cat "${OUT_DIR}/${ISO_NAME}.sha256")"
    timer_end "verify iso"
}

cleanup_work() {
    if [ "${NO_CLEANUP}" -eq 1 ]; then
        log "--no-cleanup: keeping ${WORK_DIR}"
        return 0
    fi
    timer_start "cleanup work dir"
    assert_no_mounts_below "${SQ}"
    rm -rf "${SQ}" "${ISO_DIR}"
    timer_end "cleanup work dir"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    if [ "${ONLY_DEBS}" -eq 1 ]; then
        build_debs
        ok "--only-debs: done"
        return 0
    fi
    preflight
    build_debs
    fetch_assets
    download_base_iso
    if [ "${HOOKS_ONLY}" -eq 1 ]; then
        log "--hooks-only: reusing ${ISO_DIR} and ${SQ}"
    else
        extract_iso
        unsquash
    fi
    stage_inputs
    chroot_setup
    run_hooks
    timer_start "chroot teardown"
    chroot_teardown
    timer_end "chroot teardown"
    casper_metadata
    make_squashfs
    apply_overlay
    verify_oem_offline
    regen_md5
    build_iso
    verify_iso
    cleanup_work
    log "=================================================================="
    log " Lindos ISO ready: ${OUT_ISO}"
    log " test it:  sudo build/test-qemu.sh            (BIOS)"
    log "           sudo build/test-qemu.sh --uefi     (UEFI/OVMF)"
    log "=================================================================="
}

main "$@"
