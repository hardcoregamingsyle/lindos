#!/bin/bash
# ============================================================================
#  build/publish-apt-repo.sh - build a flat-format, signed apt repository for
#  Lindos's own lindos-*.deb packages (SPEC-UPDATE.md §36.5, docs/RELEASING.md).
#
#  Usage: build/publish-apt-repo.sh [options]
#     --in DIR          directory of the .deb files of the release being published (default:
#                       out/debs). May be given more than once. These files are authoritative:
#                       nothing else may ever replace one of them
#     --previous DIR    directory of an OLDER release's .deb files, kept so a bad update can be
#                       rolled back (repeatable). Treated as untrusted input: every file must be a
#                       lindos-* package, its name must say what is inside (dpkg-deb -f), its version
#                       must be a plain X.Y.Z older than the one being published, and a file name that
#                       exists already is refused, never overwritten. A package the release being
#                       published no longer ships (retired or renamed) is warned about and left out of
#                       the repository - it is never signed and it does not stop the release
#     --kernel-in DIR   directory of the tuned kernel's linux-image/-headers/-modules
#                       .deb files to include too (default: out/kernel; skipped when it
#                       does not exist - a kernel build is optional, everything else is not)
#     --out DIR         output repository directory (default: out/apt-repo)
#     --keep N          keep at most N versions of each package (default 3; 0 = all)
#     --key-id ID       gpg key id/fingerprint to sign with (default: the first usable
#                       secret key already available)
#     --gen-key         TEST BUILDS ONLY: generate a fresh ephemeral signing key when none
#                       is available (README-CI-KEY.txt says so; the key changes on every
#                       run, so such a repository is never deployed)
#     --release         the repository is for real users: refuse unless it is signed with
#                       the pinned Lindos archive key, the committed public keyring holds exactly
#                       that one primary key, every file of the release is a lindos-* package of
#                       one version, and the result verifies against the committed keyring;
#                       never with an ephemeral key
#     --expect-fingerprint FPR|FILE   the pinned primary fingerprint (default: the file in
#                       packages/lindos-archive-keyring; PLACEHOLDER there = nothing pinned)
#     --keyring FILE    the committed public keyring the release is verified against
#                       (default: the one in packages/lindos-archive-keyring)
#     --sources-file FILE   the lindos.sources conffile the example is made from
#     --valid-until-days N  put Valid-Until (N days ahead) into Release; 0 = off (default).
#                       Only turn it on together with a scheduled re-sign: an expired
#                       Release makes apt refuse the repository
#     -h, --help
#
#  Signing key, first that applies:
#     1. LINDOS_APT_SIGNING_KEY in the environment (a GitHub secret): the armored secret
#        SUBKEY exported from the offline primary key (gpg --export-secret-subkeys), with
#        LINDOS_APT_SIGNING_KEY_PASSPHRASE if it has one. It is imported into a private,
#        temporary GNUPGHOME that is removed on exit; nothing is ever written to disk in clear
#        outside it.
#     2. --key-id ID          3. the first secret key in the gpg keyring
#     4. --gen-key            (never with --release)
#  Unless the key is ephemeral it must be the pinned one whenever a fingerprint is pinned;
#  --release makes the pin mandatory.
#
#  Produces a **flat-format** repository (no dists/ tree - works from any static HTTPS host):
#  Packages, Packages.gz, Release (apt-ftparchive), a clearsigned InRelease and a detached
#  Release.gpg, the exported public keyring (lindos-archive-keyring.gpg - for convenience only,
#  never the trust anchor of an installed system: that is the keyring package), every .deb, and
#  an example source (lindos.sources.example). Never invents trust: no signing key and no
#  --gen-key fails outright rather than shipping something unsigned.
#
#  Exit codes: 0 ok - 1 build/usage error
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="publish-apt-repo"

ROOT="$(repo_root)"
lindos_load_config

PKG_ROOT="${LINDOS_KEYRING_PKG_ROOT:-${ROOT}/packages/lindos-archive-keyring/root}"
IN_DIRS=()
PREVIOUS_DIRS=()
KERNEL_IN_DIR="out/kernel"
REPO_DIR="${OUT_DIR:-out}/apt-repo"
KEEP_VERSIONS=3
KEY_ID=""
GEN_KEY=0
RELEASE=0
EXPECT_FPR="${PKG_ROOT}/usr/share/lindos/archive-key.fingerprint"
KEYRING_FILE="${PKG_ROOT}/usr/share/keyrings/lindos-archive-keyring.gpg"
SOURCES_FILE="${PKG_ROOT}/etc/apt/sources.list.d/lindos.sources"
VALID_UNTIL_DAYS=0

usage() {
    sed -n '3,/^#  Exit codes/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --in) [ $# -ge 2 ] || die "--in needs an argument" 1; IN_DIRS+=("$2"); shift 2 ;;
        --in=*) IN_DIRS+=("${1#*=}"); shift ;;
        --previous) [ $# -ge 2 ] || die "--previous needs an argument" 1; PREVIOUS_DIRS+=("$2"); shift 2 ;;
        --previous=*) PREVIOUS_DIRS+=("${1#*=}"); shift ;;
        --kernel-in) [ $# -ge 2 ] || die "--kernel-in needs an argument" 1; KERNEL_IN_DIR="$2"; shift 2 ;;
        --kernel-in=*) KERNEL_IN_DIR="${1#*=}"; shift ;;
        --out) [ $# -ge 2 ] || die "--out needs an argument" 1; REPO_DIR="$2"; shift 2 ;;
        --out=*) REPO_DIR="${1#*=}"; shift ;;
        --keep) [ $# -ge 2 ] || die "--keep needs an argument" 1; KEEP_VERSIONS="$2"; shift 2 ;;
        --keep=*) KEEP_VERSIONS="${1#*=}"; shift ;;
        --key-id) [ $# -ge 2 ] || die "--key-id needs an argument" 1; KEY_ID="$2"; shift 2 ;;
        --key-id=*) KEY_ID="${1#*=}"; shift ;;
        --gen-key) GEN_KEY=1; shift ;;
        --release) RELEASE=1; shift ;;
        --expect-fingerprint) [ $# -ge 2 ] || die "--expect-fingerprint needs an argument" 1; EXPECT_FPR="$2"; shift 2 ;;
        --expect-fingerprint=*) EXPECT_FPR="${1#*=}"; shift ;;
        --keyring) [ $# -ge 2 ] || die "--keyring needs an argument" 1; KEYRING_FILE="$2"; shift 2 ;;
        --keyring=*) KEYRING_FILE="${1#*=}"; shift ;;
        --sources-file) [ $# -ge 2 ] || die "--sources-file needs an argument" 1; SOURCES_FILE="$2"; shift 2 ;;
        --sources-file=*) SOURCES_FILE="${1#*=}"; shift ;;
        --valid-until-days) [ $# -ge 2 ] || die "--valid-until-days needs an argument" 1; VALID_UNTIL_DAYS="$2"; shift 2 ;;
        --valid-until-days=*) VALID_UNTIL_DAYS="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" 1 ;;
        *) die "unexpected argument: $1 (see --help)" 1 ;;
    esac
done

[ "${#IN_DIRS[@]}" -gt 0 ] || IN_DIRS=("${DEBS_DIR:-out/debs}")
case "${KEEP_VERSIONS}" in ''|*[!0-9]*) die "--keep needs a number" 1 ;; esac
case "${VALID_UNTIL_DAYS}" in ''|*[!0-9]*) die "--valid-until-days needs a number" 1 ;; esac
if [ "${RELEASE}" -eq 1 ] && [ "${GEN_KEY}" -eq 1 ]; then
    die "--release and --gen-key cannot be combined: a release is never signed with an ephemeral key" 1
fi

require_cmd apt-ftparchive gpg gzip find

abs() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s/%s\n' "${ROOT}" "$1" ;; esac; }
IN_ABS=()
for d in "${IN_DIRS[@]}"; do
    IN_ABS+=("$(abs "${d}")")
done
PREVIOUS_ABS=()
for d in ${PREVIOUS_DIRS[@]+"${PREVIOUS_DIRS[@]}"}; do
    PREVIOUS_ABS+=("$(abs "${d}")")
done
KERNEL_IN_DIR="$(abs "${KERNEL_IN_DIR}")"
REPO_DIR="$(abs "${REPO_DIR}")"

for d in "${IN_ABS[@]}"; do
    [ -d "${d}" ] || die "--in directory not found: ${d}"
done
for d in ${PREVIOUS_ABS[@]+"${PREVIOUS_ABS[@]}"}; do
    [ -d "${d}" ] || die "--previous directory not found: ${d}"
done

# ---------------------------------------------------------------------------
# The pinned fingerprint (40 hex digits) - or none yet (PLACEHOLDER / missing)
# ---------------------------------------------------------------------------
PIN=""
if [ -n "${EXPECT_FPR}" ]; then
    if [ -f "${EXPECT_FPR}" ]; then
        pin_raw="$(tr -d ' \r\n' < "${EXPECT_FPR}")"
    else
        pin_raw="$(printf '%s' "${EXPECT_FPR}" | tr -d ' ')"
    fi
    pin_raw="$(printf '%s' "${pin_raw}" | tr '[:lower:]' '[:upper:]')"
    if printf '%s' "${pin_raw}" | grep -Eq '^[0-9A-F]{40}$'; then
        PIN="${pin_raw}"
    elif [ -n "${pin_raw}" ] && [ "${pin_raw}" != "PLACEHOLDER" ]; then
        die "the pinned fingerprint is not 40 hex digits: '${pin_raw}'" 1
    fi
fi

# ---------------------------------------------------------------------------
# Clean-up of every private directory we create (and the gpg-agent that ran in it)
# ---------------------------------------------------------------------------
CLEAN_DIRS=()
cleanup() {
    local d
    for d in ${CLEAN_DIRS[@]+"${CLEAN_DIRS[@]}"}; do
        if command -v gpgconf >/dev/null 2>&1; then
            gpgconf --homedir "${d}" --kill gpg-agent >/dev/null 2>&1 || true
        fi
        rm -rf "${d}"
    done
}
trap cleanup EXIT

timer_start "publish-apt-repo"
rm -rf "${REPO_DIR}"
ensure_dir "${REPO_DIR}"

# ---------------------------------------------------------------------------
# 1. Copy every .deb in flat: the release being published (authoritative), the tuned kernel's when
#    present, then the kept OLDER releases (untrusted: each one is checked), and keep at most
#    --keep versions of each package. A file name that is already there is never replaced.
# ---------------------------------------------------------------------------
N_DEBS=0
copy_debs() {
    local dir="$1" label="$2" f b
    [ -d "${dir}" ] || return 0
    while IFS= read -r -d '' f; do
        b="$(basename "${f}")"
        if [ -e "${REPO_DIR}/${b}" ]; then
            die "${b} (from ${label}, ${dir}) is already in the repository from an earlier directory: the same file name twice - the release being published is authoritative and nothing else may replace it" 1
        fi
        cp "${f}" "${REPO_DIR}/${b}"
    done < <(find "${dir}" -maxdepth 1 -type f -name '*.deb' -print0)
}

# What a copied .deb says it is (its control data, never its file name): sets D_PKG D_VER D_ARCH and refuses a file
# whose name is not <Package>_<Version>_<Architecture>.deb, or with a Package/Version that is not plain Debian syntax
# (they become array keys and file names below)
deb_identity() {
    local f="$1" fields line b
    fields="$(dpkg-deb -f "${f}" Package Version Architecture 2>/dev/null)" || fields=""
    D_PKG="" D_VER="" D_ARCH=""
    while IFS= read -r line; do
        line="${line%$'\r'}"
        case "${line}" in
            "Package: "*) D_PKG="${line#Package: }" ;;
            "Version: "*) D_VER="${line#Version: }" ;;
            "Architecture: "*) D_ARCH="${line#Architecture: }" ;;
        esac
    done <<< "${fields}"
    b="$(basename "${f}")"
    { [ -n "${D_PKG}" ] && [ -n "${D_VER}" ] && [ -n "${D_ARCH}" ]; } || die "${b}: dpkg-deb -f cannot read its control data - not a usable .deb" 1
    [[ "${D_PKG}" =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || die "${b}: its Package field is not a Debian package name" 1
    [[ "${D_VER}" =~ ^[A-Za-z0-9.+~:-]+$ ]] || die "${b}: its Version field is not Debian version syntax" 1
    [[ "${D_ARCH}" =~ ^[a-z0-9-]+$ ]] || die "${b}: its Architecture field is not an architecture name" 1
    [ "${b}" = "${D_PKG}_${D_VER#*:}_${D_ARCH}.deb" ] \
        || die "${b}: what is inside says ${D_PKG}_${D_VER#*:}_${D_ARCH}.deb - a file whose name does not match its content is refused" 1
}
# A is older than B: plain X.Y.Z in pure bash (no forks), anything else by sort -V
version_lt() {
    [ "$1" != "$2" ] || return 1
    local plain='^([0-9]+)\.([0-9]+)\.([0-9]+)$' a1 a2 a3 b1 b2 b3
    if [[ "$1" =~ ${plain} ]]; then
        a1="${BASH_REMATCH[1]}" a2="${BASH_REMATCH[2]}" a3="${BASH_REMATCH[3]}"
        if [[ "$2" =~ ${plain} ]]; then
            b1="${BASH_REMATCH[1]}" b2="${BASH_REMATCH[2]}" b3="${BASH_REMATCH[3]}"
            if [ "$((10#${a1}))" -ne "$((10#${b1}))" ]; then [ "$((10#${a1}))" -lt "$((10#${b1}))" ]; return; fi
            if [ "$((10#${a2}))" -ne "$((10#${b2}))" ]; then [ "$((10#${a2}))" -lt "$((10#${b2}))" ]; return; fi
            [ "$((10#${a3}))" -lt "$((10#${b3}))" ]
            return
        fi
    fi
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}
PLAIN_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+$'
PKG_NAME_RE='^lindos-[a-z0-9][a-z0-9+.-]*$'

for d in "${IN_ABS[@]}"; do
    copy_debs "${d}" "the release"
done
declare -A CUR_VER=()       # package -> version of the release being published (a plain X.Y.Z)
RELEASE_VERSIONS=""
if [ "${RELEASE}" -eq 1 ] || [ "${#PREVIOUS_ABS[@]}" -gt 0 ]; then
    require_cmd dpkg-deb
    for f in "${REPO_DIR}"/*.deb; do
        [ -e "${f}" ] || continue
        deb_identity "${f}"
        if [ "${RELEASE}" -eq 1 ]; then
            [[ "${D_PKG}" =~ ${PKG_NAME_RE} ]] || die "--release: $(basename "${f}") is not a lindos-* package" 1
            [[ "${D_VER}" =~ ${PLAIN_VERSION_RE} ]] || die "--release: $(basename "${f}") has the version ${D_VER}, not a plain X.Y.Z" 1
        fi
        if [ -z "${CUR_VER[${D_PKG}]:-}" ] || version_lt "${CUR_VER[${D_PKG}]}" "${D_VER}"; then
            CUR_VER["${D_PKG}"]="${D_VER}"
        fi
        RELEASE_VERSIONS+="${D_VER}"$'\n'
    done
    if [ "${RELEASE}" -eq 1 ] && [ "$(printf '%s' "${RELEASE_VERSIONS}" | sort -u | wc -l | tr -d ' ')" -gt 1 ]; then
        die "--release: the packages of the release are not one version ($(printf '%s' "${RELEASE_VERSIONS}" | sort -u | tr '\n' ' ')) - they always move together" 1
    fi
fi
if [ -d "${KERNEL_IN_DIR}" ]; then
    copy_debs "${KERNEL_IN_DIR}" "the kernel build"
else
    log "--kernel-in directory not found (${KERNEL_IN_DIR}) — publishing without kernel .debs"
fi
# the older releases: copied first (a name that is already there stops the run), then every copy is checked
for d in ${PREVIOUS_ABS[@]+"${PREVIOUS_ABS[@]}"}; do
    copy_debs "${d}" "a previous release"
    while IFS= read -r -d '' f; do
        b="$(basename "${f}")"
        deb_identity "${REPO_DIR}/${b}"
        [[ "${D_PKG}" =~ ${PKG_NAME_RE} ]] || die "previous release, ${b}: ${D_PKG} is not a lindos-* package" 1
        if [ -z "${CUR_VER[${D_PKG}]:-}" ]; then
            # A verified older lindos-* package that the release being published no longer ships (a retired or renamed
            # package): it can only be for rollback of something that no longer exists, so it is left out - warned about,
            # not signed - and the release goes on. Dropping keeps the safety property: what is in the repository is
            # exactly what was checked above, and nothing else is ever signed. (Dying here made one retired package
            # block every release until the older releases were deleted by hand.)
            warn "previous release, ${b}: ${D_PKG} is not a package of the release being published any more - left out of the repository (it is not signed)"
            rm -f "${REPO_DIR}/${b}"
            continue
        fi
        [[ "${D_VER}" =~ ${PLAIN_VERSION_RE} ]] || die "previous release, ${b}: the version ${D_VER} is not a plain X.Y.Z" 1
        version_lt "${D_VER}" "${CUR_VER[${D_PKG}]}" \
            || die "previous release, ${b}: version ${D_VER} is not older than ${CUR_VER[${D_PKG}]}, the release being published" 1
    done < <(find "${d}" -maxdepth 1 -type f -name '*.deb' -print0)
done

prune_versions() {
    [ "${KEEP_VERSIONS}" -gt 0 ] || return 0
    local f b n a key
    declare -A groups=()
    while IFS= read -r -d '' f; do
        b="$(basename "${f}" .deb)"
        n="${b%%_*}"
        a="${b##*_}"
        key="${n}|${a}"
        groups["${key}"]+="${f}"$'\n'
    done < <(find "${REPO_DIR}" -maxdepth 1 -type f -name '*.deb' -print0)
    for key in "${!groups[@]}"; do
        local sorted=() count drop i
        mapfile -t sorted < <(printf '%s' "${groups[${key}]}" | sed '/^$/d' | sort -V)
        count="${#sorted[@]}"
        if [ "${count}" -gt "${KEEP_VERSIONS}" ]; then
            drop=$(( count - KEEP_VERSIONS ))
            for (( i = 0; i < drop; i++ )); do
                log "dropping old version $(basename "${sorted[${i}]}") (keeping the newest ${KEEP_VERSIONS})"
                rm -f "${sorted[${i}]}"
            done
        fi
    done
}
prune_versions

N_DEBS="$(find "${REPO_DIR}" -maxdepth 1 -type f -name '*.deb' | wc -l | tr -d ' ')"
[ "${N_DEBS}" -gt 0 ] || die "no .deb files found under ${IN_ABS[*]} (or ${KERNEL_IN_DIR})"
log "${N_DEBS} .deb file(s) in ${REPO_DIR}"

# ---------------------------------------------------------------------------
# 2. Packages / Packages.gz / Release - flat-format: apt-ftparchive is run
#    from inside the repo dir so recorded paths are bare filenames, not a
#    dists/<suite>/<component>/ tree.
# ---------------------------------------------------------------------------
RELEASE_OPTS=()
if [ "${VALID_UNTIL_DAYS}" -gt 0 ]; then
    valid_until="$(LC_ALL=C date -u -d "+${VALID_UNTIL_DAYS} days" '+%a, %d %b %Y %H:%M:%S UTC')"
    RELEASE_OPTS+=(-o "APT::FTPArchive::Release::Valid-Until=${valid_until}")
    log "Release will carry Valid-Until: ${valid_until}"
fi
(
    cd "${REPO_DIR}"
    apt-ftparchive packages . > Packages
    gzip -9 -c Packages > Packages.gz
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin="Lindos" \
        -o APT::FTPArchive::Release::Label="Lindos" \
        -o APT::FTPArchive::Release::Suite="stable" \
        -o APT::FTPArchive::Release::Codename="${LINDOS_CODENAME:-Aurora}" \
        -o APT::FTPArchive::Release::Architectures="amd64 all" \
        -o APT::FTPArchive::Release::Description="Lindos ${LINDOS_VERSION:-1.0.0} package repository" \
        ${RELEASE_OPTS[@]+"${RELEASE_OPTS[@]}"} \
        release . > Release
)
log "wrote Packages, Packages.gz, Release (${REPO_DIR})"

# ---------------------------------------------------------------------------
# 3. Signing key. Decide the mode first, so --release can refuse before anything is generated.
# ---------------------------------------------------------------------------
KEY_MODE=""            # env | existing | ephemeral
GPG_KEY_ARGS=()        # extra gpg arguments needed to use the key (passphrase handling)
GEN_GNUPGHOME="${REPO_DIR}/.gnupg-publish"

first_secret_key_id() {
    gpg --list-secret-keys --with-colons 2>/dev/null | awk -F: '/^sec:/ && !done { print $5; done = 1 }'
}
primary_fingerprint() {
    gpg --batch --list-secret-keys --with-colons "$1" 2>/dev/null | awk -F: '/^fpr:/ && !done { print toupper($10); done = 1 }'
}

if [ -n "${LINDOS_APT_SIGNING_KEY:-}" ]; then
    KEY_MODE="env"
    PRIVATE_HOME="$(mktemp -d)"
    chmod 0700 "${PRIVATE_HOME}"
    CLEAN_DIRS+=("${PRIVATE_HOME}")
    export GNUPGHOME="${PRIVATE_HOME}"
    if [ -n "${LINDOS_APT_SIGNING_KEY_PASSPHRASE:-}" ]; then
        ( umask 077; printf '%s' "${LINDOS_APT_SIGNING_KEY_PASSPHRASE}" > "${PRIVATE_HOME}/passphrase" )
        GPG_KEY_ARGS=(--pinentry-mode loopback --passphrase-file "${PRIVATE_HOME}/passphrase")
    fi
    log "importing the signing key from LINDOS_APT_SIGNING_KEY into a private, temporary keyring"
    printf '%s\n' "${LINDOS_APT_SIGNING_KEY}" | gpg --batch --yes --quiet ${GPG_KEY_ARGS[@]+"${GPG_KEY_ARGS[@]}"} --import \
        || die "could not import LINDOS_APT_SIGNING_KEY (is it an armored secret key, and the passphrase right?)"
    KEY_ID="$(first_secret_key_id)"
    [ -n "${KEY_ID}" ] || die "LINDOS_APT_SIGNING_KEY did not contain a secret key"
    KEY_ID="$(primary_fingerprint "${KEY_ID}")"
    [ -n "${KEY_ID}" ] || die "could not read the fingerprint of the imported signing key"
elif [ -n "${KEY_ID}" ]; then
    KEY_MODE="existing"
elif [ "${RELEASE}" -eq 1 ]; then
    die "--release needs the signing key: set LINDOS_APT_SIGNING_KEY (or pass --key-id)" 1
elif KEY_ID="$(first_secret_key_id)" && [ -n "${KEY_ID}" ]; then
    KEY_MODE="existing"
elif [ "${GEN_KEY}" -eq 1 ]; then
    KEY_MODE="ephemeral"
else
    die "no gpg signing key available (pass --key-id, or --gen-key for an ephemeral CI-only key)"
fi

if [ "${KEY_MODE}" = "ephemeral" ]; then
    log "no signing key available — generating an ephemeral one (--gen-key)"
    ensure_dir "${GEN_GNUPGHOME}"
    chmod 0700 "${GEN_GNUPGHOME}"
    CLEAN_DIRS+=("${GEN_GNUPGHOME}")
    export GNUPGHOME="${GEN_GNUPGHOME}"
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "Lindos CI (ephemeral, do not trust) <ci@lindos.dev>" ed25519 sign 0
    KEY_ID="$(first_secret_key_id)"
    [ -n "${KEY_ID}" ] || die "gpg --quick-generate-key did not produce a usable key"
else
    log "signing with ${KEY_MODE} key: ${KEY_ID}"
fi

# The key must be the pinned Lindos archive key whenever one is pinned; a release needs the pin.
if [ "${KEY_MODE}" != "ephemeral" ]; then
    SIGN_FPR="$(primary_fingerprint "${KEY_ID}")"
    if [ -n "${PIN}" ]; then
        [ -n "${SIGN_FPR}" ] || die "cannot read the fingerprint of the signing key ${KEY_ID}" 1
        if [ "${SIGN_FPR}" != "${PIN}" ]; then
            die "REFUSING to publish: the signing key (${SIGN_FPR}) is not the pinned Lindos archive key (${PIN}) that installed systems trust" 1
        fi
        log "the signing key is the pinned Lindos archive key (${PIN})"
    elif [ "${RELEASE}" -eq 1 ]; then
        die "--release needs a pinned fingerprint: commit the archive key's fingerprint to ${EXPECT_FPR} (docs/RELEASING.md)" 1
    else
        warn "signing with a key that is NOT pinned (no fingerprint committed yet): fine for a private test repository, never for real users"
    fi
fi

gpg --batch --yes --output "${REPO_DIR}/lindos-archive-keyring.gpg" --export "${KEY_ID}"

if [ "${KEY_MODE}" = "ephemeral" ]; then
    cat > "${REPO_DIR}/README-CI-KEY.txt" <<EOF
This repository was signed with an EPHEMERAL key generated on the fly by
build/publish-apt-repo.sh --gen-key, because no real signing key was
available. It is NOT a stable trust root and it is NEVER deployed: a fresh
key is generated every time this script runs without a real one, so any
machine that trusted a previous lindos-archive-keyring.gpg would reject the
next publish's signature. It exists so CI can test the repository tooling.
Real releases are signed with the Lindos archive key (docs/RELEASING.md).
EOF
    warn "signed with an EPHEMERAL CI-only key — see ${REPO_DIR}/README-CI-KEY.txt"
fi

# ---------------------------------------------------------------------------
# 4. InRelease (clearsigned) + Release.gpg (detached, armored) — both, so any
#    apt client (old: Release.gpg; modern: InRelease) can verify the repo.
# ---------------------------------------------------------------------------
GPG_SIGN_ARGS=(--batch --yes --digest-algo SHA256 --local-user "${KEY_ID}")
if [ "${KEY_MODE}" = "ephemeral" ]; then
    GPG_SIGN_ARGS+=(--pinentry-mode loopback --passphrase '')
else
    GPG_SIGN_ARGS+=(${GPG_KEY_ARGS[@]+"${GPG_KEY_ARGS[@]}"})
fi
(
    cd "${REPO_DIR}"
    gpg "${GPG_SIGN_ARGS[@]}" --clearsign --output InRelease Release
    gpg "${GPG_SIGN_ARGS[@]}" --detach-sign --armor --output Release.gpg Release
)
log "signed: InRelease, Release.gpg (key ${KEY_ID})"

# The signature must verify the way an installed system will check it: against the committed
# public keyring (release), or against the keyring just exported (test builds).
verify_with() {
    local keyring="$1"
    if ! command -v gpgv >/dev/null 2>&1; then
        [ "${RELEASE}" -eq 0 ] || die "gpgv is not installed: cannot verify the signature of a release" 1
        warn "gpgv is not installed - the signature was not verified"
        return 0
    fi
    gpgv --keyring "${keyring}" "${REPO_DIR}/InRelease" >/dev/null 2>&1 \
        || die "the signed InRelease does not verify against ${keyring}" 1
    gpgv --keyring "${keyring}" "${REPO_DIR}/Release.gpg" "${REPO_DIR}/Release" >/dev/null 2>&1 \
        || die "the detached Release.gpg does not verify against ${keyring}" 1
    log "the signature verifies against ${keyring}"
}
if [ "${RELEASE}" -eq 1 ]; then
    [ -f "${KEYRING_FILE}" ] || die "--release: the committed keyring is missing: ${KEYRING_FILE}" 1
    if [ "$(head -c 18 "${KEYRING_FILE}" | tr -d '\000\r')" = "LINDOS-PLACEHOLDER" ]; then
        die "--release: the committed keyring is still the placeholder (${KEYRING_FILE})" 1
    fi
    # apt trusts every key in a Signed-By keyring and gpgv accepts a signature from any of them, so the
    # committed keyring must hold exactly one primary key, the pinned one
    keyring_listing="$(gpg --batch --no-tty --show-keys --with-colons "${KEYRING_FILE}" 2>/dev/null)" \
        || die "--release: gpg cannot read the committed keyring ${KEYRING_FILE}" 1
    keyring_primaries="$(printf '%s\n' "${keyring_listing}" | grep -c '^pub:' || true)"
    keyring_fpr="$(printf '%s\n' "${keyring_listing}" | awk -F: '$1 == "fpr" && !done { print toupper($10); done = 1 }')"
    [ "${keyring_primaries}" -eq 1 ] \
        || die "--release: the committed keyring holds ${keyring_primaries} primary keys; exactly one, the pinned one, is allowed" 1
    [ "${keyring_fpr}" = "${PIN}" ] \
        || die "--release: the committed keyring's primary key (${keyring_fpr:-none}) is not the pinned one (${PIN})" 1
    verify_with "${KEYRING_FILE}"
else
    verify_with "${REPO_DIR}/lindos-archive-keyring.gpg"
fi

# ---------------------------------------------------------------------------
# 5. Example source (the real one is the lindos-archive-keyring package's conffile) + summary.
# ---------------------------------------------------------------------------
EXAMPLE_URL="https://example.invalid/stable/"
if [ -f "${SOURCES_FILE}" ]; then
    found="$(awk '/^[ \t]*#/ { next } tolower($1) == "uris:" { print $2; exit }' "${SOURCES_FILE}" | tr -d '\r')"
    [ -z "${found}" ] || EXAMPLE_URL="${found}"
fi
cat > "${REPO_DIR}/lindos.sources.example" <<EOF
# Example only. The real source ships in the lindos-archive-keyring package
# (/etc/apt/sources.list.d/lindos.sources) - the one place the address lives.
Types: deb
URIs: ${EXAMPLE_URL}
Suites: ./
Signed-By: /usr/share/keyrings/lindos-archive-keyring.gpg
EOF

ok "repository ready: ${REPO_DIR} (${N_DEBS} package(s), ${KEY_MODE} key)"
log "example source: ${REPO_DIR}/lindos.sources.example (${EXAMPLE_URL})"
timer_end "publish-apt-repo"
