#!/bin/bash
# ============================================================================
#  build/tools/fetch-previous-releases.sh - the older releases kept for rollback, verified
#
#  Usage: fetch-previous-releases.sh --tag vX.Y.Z --keyring FILE [--out DIR] [--count N] [--limit N]
#     --tag TAG        the release being published; it is skipped, and so is every release that is not
#                      OLDER than it
#     --keyring FILE   the committed public keyring (the trust anchor of installed systems)
#     --out DIR        where the verified .debs go (default out/previous)
#     --count N        how many older releases to keep (default 2; with the one being published: 3)
#     --limit N        how many releases to look at, newest first (default 10)
#
#  Why it exists: GitHub Release assets are mutable and anyone with write access can add or replace one, so a
#  .deb found there proves nothing - and everything that is published is signed with the archive key. Each
#  release therefore also carries the signed repository metadata that was published with it (Release,
#  Release.gpg, Packages; release-repo uploads them after signing). This script trusts a .deb only when
#     1. Release.gpg verifies against the committed keyring (gpgv),
#     2. the SHA256 of Packages in that verified Release matches the downloaded Packages, and
#     3. the SHA256 of the .deb matches its entry in that Packages.
#  Anything else - no signed metadata (a release from before this existed), a bad signature, a file that is not
#  listed or whose hash differs - is left out with a warning; nothing unverified reaches out/previous. It is an
#  ordinary outcome, not an error: the first release has no older ones. The publish script then applies its own
#  checks (build/publish-apt-repo.sh --previous).
#
#  Needs: gh (GH_TOKEN), gpgv, sha256sum, awk.  Exit: 0 (also when nothing could be kept) - 1 usage or tool error
#  Test seams: LINDOS_GH=<program> is run instead of gh, LINDOS_GPGV=<program> instead of gpgv.
# ============================================================================
set -Eeuo pipefail

GH="${LINDOS_GH:-gh}"
GPGV="${LINDOS_GPGV:-gpgv}"
TAG=""
KEYRING=""
OUT="out/previous"
COUNT=2
LIMIT=10

say() { printf 'fetch-previous-releases: %s\n' "$*"; }
warn() { printf 'fetch-previous-releases: warning: %s\n' "$*" >&2; }
fail() { printf 'fetch-previous-releases: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '3,/^#  Test seams/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit "${1:-1}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) [ $# -ge 2 ] || usage; TAG="$2"; shift 2 ;;
        --keyring) [ $# -ge 2 ] || usage; KEYRING="$2"; shift 2 ;;
        --out) [ $# -ge 2 ] || usage; OUT="$2"; shift 2 ;;
        --count) [ $# -ge 2 ] || usage; COUNT="$2"; shift 2 ;;
        --limit) [ $# -ge 2 ] || usage; LIMIT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) usage ;;
    esac
done

TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'
[[ "${TAG}" =~ ${TAG_RE} ]] || fail "--tag needs the tag of the release being published (vX.Y.Z), got '${TAG}'"
[ -n "${KEYRING}" ] && [ -f "${KEYRING}" ] || fail "--keyring needs the committed public keyring (not found: '${KEYRING}')"
case "${COUNT}${LIMIT}" in ''|*[!0-9]*) fail "--count and --limit need numbers" ;; esac
for tool in "${GH}" "${GPGV}" sha256sum awk sort; do
    command -v "${tool}" >/dev/null 2>&1 || fail "${tool} is not installed"
done

mkdir -p "${OUT}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

version_lt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}

# "<sha256>  <file name>" for every stanza of a Packages file that has both
packages_hashes() {
    awk 'BEGIN { RS = ""; FS = "\n" }
        {
            file = ""; sum = ""
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^Filename:/) { file = $i; sub(/^Filename:[ \t]*(\.\/)?/, "", file); sub(/[ \t\r]+$/, "", file) }
                if ($i ~ /^SHA256:/)   { sum = $i;  sub(/^SHA256:[ \t]*/, "", sum);            sub(/[ \t\r]+$/, "", sum) }
            }
            if (file != "" && sum != "") print sum "  " file
        }' "$1"
}

# the SHA256 the (verified) Release file lists for a file of the repository
release_hash_of() {
    awk -v want="$2" '
        /^SHA256:/ { in_sha = 1; next }
        /^[^ \t]/ { in_sha = 0 }
        in_sha && $3 == want { print $1; exit }' "$1"
}

tags=""
if ! tags="$("${GH}" release list --limit "${LIMIT}" --json tagName,isDraft,isPrerelease \
        --jq '.[] | select(.isDraft | not) | select(.isPrerelease | not) | .tagName' 2>/dev/null)"; then
    warn "could not list the releases (gh) - nothing is kept for rollback this time"
    tags=""
fi
tags="${tags//$'\r'/}"
[ -n "${tags}" ] || say "no earlier release found - nothing to keep for rollback (normal for the first release)"
TAG_LIST=()
[ -z "${tags}" ] || mapfile -t TAG_LIST <<< "${tags}"

kept=0
for t in ${TAG_LIST[@]+"${TAG_LIST[@]}"}; do
    [ "${kept}" -lt "${COUNT}" ] || break
    [ "${t}" != "${TAG}" ] || continue
    [[ "${t}" =~ ${TAG_RE} ]] || { warn "${t}: not a release tag (vX.Y.Z) - skipped"; continue; }
    version_lt "${t#v}" "${TAG#v}" || { warn "${t}: not older than ${TAG} - skipped"; continue; }

    dir="${WORK}/${t}"
    mkdir -p "${dir}"
    if ! "${GH}" release download "${t}" --pattern 'Release' --pattern 'Release.gpg' --pattern 'Packages' --pattern '*.deb' \
            --dir "${dir}" --clobber >/dev/null 2>&1; then
        warn "${t}: could not download its assets - skipped"
        continue
    fi
    if [ ! -s "${dir}/Release" ] || [ ! -s "${dir}/Release.gpg" ] || [ ! -s "${dir}/Packages" ]; then
        warn "${t}: it has no signed repository metadata (Release, Release.gpg, Packages) - none of its packages can be trusted, skipped"
        continue
    fi
    if ! "${GPGV}" --keyring "${KEYRING}" "${dir}/Release.gpg" "${dir}/Release" >/dev/null 2>&1; then
        warn "${t}: the signature of its Release does not verify against the committed keyring - skipped"
        continue
    fi
    want="$(release_hash_of "${dir}/Release" Packages)"
    have="$(sha256sum "${dir}/Packages" | awk '{print $1}')"
    if [ -z "${want}" ] || [ "${want}" != "${have}" ]; then
        warn "${t}: its Packages is not the one the signed Release lists - skipped"
        continue
    fi
    packages_hashes "${dir}/Packages" > "${dir}/hashes"

    verified=0
    for f in "${dir}"/*.deb; do
        [ -e "${f}" ] || continue
        b="$(basename "${f}")"
        expect="$(awk -v n="${b}" '$2 == n { print $1; exit }' "${dir}/hashes")"
        actual="$(sha256sum "${f}" | awk '{print $1}')"
        if [ -z "${expect}" ]; then
            warn "${t}: ${b} is not listed in its signed Packages - left out"
        elif [ "${expect}" != "${actual}" ]; then
            warn "${t}: ${b} does not match the hash in its signed Packages - left out"
        elif [ -e "${OUT}/${b}" ]; then
            warn "${t}: ${b} is already there - left out"
        else
            cp "${f}" "${OUT}/${b}"
            verified=$((verified + 1))
        fi
    done
    if [ "${verified}" -gt 0 ]; then
        say "${t}: ${verified} package(s) verified against its signed metadata"
        kept=$((kept + 1))
    else
        warn "${t}: no package could be verified - skipped"
    fi
done

say "kept ${kept} earlier release(s) in ${OUT}"
