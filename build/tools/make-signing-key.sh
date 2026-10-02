#!/bin/bash
# ============================================================================
#  build/tools/make-signing-key.sh - the offline key ceremony for the Lindos apt archive key
#
#  Usage: build/tools/make-signing-key.sh [options]
#     (no option)        print the ceremony step by step, with copy-paste commands. Runs nothing.
#     --generate         do the key steps for you (interactive: gpg asks for the passphrase)
#     --out DIR          with --generate: where the key material goes. It must be OUTSIDE this
#                        repository, and empty or new. Use an encrypted / offline medium.
#     --uid "Name <mail>"   the key's user id (default: Lindos Archive <archive@lindos.dev>;
#                        use an address you control)
#     --expire 2y        signing-subkey lifetime (default 2y; the primary key never expires)
#     -h, --help
#
#  What it makes: an ed25519 certify-only PRIMARY key that stays offline for ever, and a SIGNING
#  SUBKEY with an expiry that CI uses. Only the public keyring and the fingerprint go into the
#  repository; only the armored secret subkey goes into the GitHub secret LINDOS_APT_SIGNING_KEY.
#  The primary key, its passphrase and the revocation certificate are stored offline.
#
#  This script never writes a private key anywhere inside the repository (it refuses an --out
#  inside it), never puts a passphrase on a command line, and never sends anything anywhere.
#  Never paste a private key into a chat, an issue or a file in the repository.
#  Exit codes: 0 ok - 1 error - 2 usage
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="make-signing-key"

ROOT="$(repo_root)"
KEYRING_DEST="packages/lindos-archive-keyring/root/usr/share/keyrings/lindos-archive-keyring.gpg"
FPR_DEST="packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint"
SOURCES_DEST="packages/lindos-archive-keyring/root/etc/apt/sources.list.d/lindos.sources"

GENERATE=0
OUT=""
UID_TEXT="Lindos Archive <archive@lindos.dev>"
EXPIRE="2y"

usage() {
    sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --generate) GENERATE=1; shift ;;
        --out) [ $# -ge 2 ] || die "--out needs an argument" 2; OUT="$2"; shift 2 ;;
        --out=*) OUT="${1#*=}"; shift ;;
        --uid) [ $# -ge 2 ] || die "--uid needs an argument" 2; UID_TEXT="$2"; shift 2 ;;
        --uid=*) UID_TEXT="${1#*=}"; shift ;;
        --expire) [ $# -ge 2 ] || die "--expire needs an argument" 2; EXPIRE="$2"; shift 2 ;;
        --expire=*) EXPIRE="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" 2 ;;
    esac
done

print_steps() {
    cat <<EOF
The Lindos archive key - offline ceremony
=========================================

Do this on a machine that is offline (a live USB session is ideal) with the output on an
encrypted or removable medium that is NOT inside the Lindos repository. Pick a long passphrase and
write it down offline. Nothing below is ever pasted into a chat or committed.

1. A private key ring, so nothing touches your everyday keys:
     export GNUPGHOME="\$(mktemp -d)"; chmod 700 "\$GNUPGHOME"

2. The primary key (certify-only, never expires) and TWO signing subkeys (they expire): the first is the
   one CI uses, the second is a SPARE that stays offline. Both go into the public keyring installed systems
   trust, so if the CI secret ever leaks you rotate by switching CI to the spare - no action on installed systems:
     gpg --quick-generate-key "${UID_TEXT}" ed25519 cert never
     FPR="\$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ && !done { print \$10; done = 1 }')"
     gpg --quick-add-key "\$FPR" ed25519 sign ${EXPIRE}
     SUBFPR="\$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ { n++; if (n == 2) print \$10 }')"
     gpg --quick-add-key "\$FPR" ed25519 sign ${EXPIRE}          # the spare: its secret is never exported to CI

3. Export what is needed (mind the order: the subkey exists before the public export):
     gpg --export "\$FPR" > lindos-archive-keyring.gpg                           # public, binary
     printf '%s\\n' "\$FPR" > archive-key.fingerprint                             # the pin
     gpg --armor --export-secret-subkeys "\$SUBFPR"'!' > lindos-signing-subkey.asc # for CI only
     gpg --output revoke.asc --gen-revoke "\$FPR"                                # offline, keep safe

4. Into the repository (public material only), then commit it in ONE commit together with the
   real server address and "Enabled: yes" (a build refuses a switched-on source without them):
     cp lindos-archive-keyring.gpg   ${KEYRING_DEST}
     cp archive-key.fingerprint      ${FPR_DEST}
     \$EDITOR ${SOURCES_DEST}         # set URIs: and Enabled: yes

5. Into GitHub, from the machine that is logged in with gh (the secret never touches a file there):
     gh secret set LINDOS_APT_SIGNING_KEY --env apt-publish < lindos-signing-subkey.asc
     gh secret set LINDOS_APT_SIGNING_KEY_PASSPHRASE --env apt-publish   # type it when asked
   Then destroy lindos-signing-subkey.asc (shred -u) - the primary key stays offline.

6. Keep offline, in two places: the primary key (\$GNUPGHOME), its passphrase, revoke.asc.
   Rotate the subkey before it expires (docs/RELEASING.md, section 10).

Full walk-through with the rest of the release setup: docs/RELEASING.md
EOF
}

if [ "${GENERATE}" -eq 0 ]; then
    print_steps
    exit 0
fi

# ---------------------------------------------------------------------------
# --generate
# ---------------------------------------------------------------------------
[ -n "${OUT}" ] || die "--generate needs --out DIR (outside this repository)" 2
require_cmd gpg realpath

# resolve --out WITHOUT creating it, so a refused directory is never left behind
OUT_ABS="$(realpath -m "${OUT}")"
ROOT_ABS="$(cd "${ROOT}" && pwd -P)"
case "${OUT_ABS}/" in
    "${ROOT_ABS}/"*) die "refusing: ${OUT_ABS} is inside the repository (${ROOT_ABS}); a private key must never be stored there" 1 ;;
esac
mkdir -p "${OUT_ABS}"
if [ -n "$(find "${OUT_ABS}" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]; then
    die "refusing: ${OUT_ABS} is not empty" 1
fi

umask 077
GNUPGHOME="${OUT_ABS}/gnupg"
export GNUPGHOME
mkdir -p "${GNUPGHOME}"
chmod 0700 "${GNUPGHOME}"

log "creating the primary key (gpg asks for the passphrase)"
gpg --quick-generate-key "${UID_TEXT}" ed25519 cert never
FPR="$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ && !done { print $10; done = 1 }')"
[ -n "${FPR}" ] || die "could not read the new key's fingerprint"
log "adding the signing subkey (${EXPIRE})"
gpg --quick-add-key "${FPR}" ed25519 sign "${EXPIRE}"
SUBFPR="$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ { n++; if (n == 2) print $10 }')"
[ -n "${SUBFPR}" ] || die "could not read the signing subkey's fingerprint"
log "adding the SPARE signing subkey (${EXPIRE}); its secret stays in gnupg/ and is never exported"
gpg --quick-add-key "${FPR}" ed25519 sign "${EXPIRE}"

gpg --export "${FPR}" > "${OUT_ABS}/lindos-archive-keyring.gpg"
printf '%s\n' "${FPR}" > "${OUT_ABS}/archive-key.fingerprint"
gpg --armor --export-secret-subkeys "${SUBFPR}!" > "${OUT_ABS}/lindos-signing-subkey.asc"
gpg --output "${OUT_ABS}/revoke.asc" --gen-revoke "${FPR}"
chmod 0600 "${OUT_ABS}/lindos-signing-subkey.asc" "${OUT_ABS}/revoke.asc"

ok "key created: ${FPR}"
log "in ${OUT_ABS}: lindos-archive-keyring.gpg (public), archive-key.fingerprint (public), lindos-signing-subkey.asc (SECRET, for the GitHub secret), revoke.asc (SECRET, offline), gnupg/ (the PRIMARY key: keep it offline)"
log "next: the repository files and the GitHub secret - run this script without options for the exact commands (steps 4 and 5)"
