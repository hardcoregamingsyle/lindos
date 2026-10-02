#!/bin/bash
# ============================================================================
#  build/tools/check-archive-keyring.sh - a build must never ship a fake trusted key
#
#  Usage: check-archive-keyring.sh PACKAGE_ROOT
#     PACKAGE_ROOT   the root/ tree of packages/lindos-archive-keyring (or a staging copy of it)
#
#  Looks at three files inside PACKAGE_ROOT:
#     etc/apt/sources.list.d/lindos.sources     the source (Enabled:, URIs:, Signed-By:, ...)
#     usr/share/keyrings/lindos-archive-keyring.gpg   the public keyring, or the text placeholder
#     usr/share/lindos/archive-key.fingerprint  the pinned primary fingerprint, or PLACEHOLDER
#
#  Prints "placeholder" or "real" on stdout (what the keyring file is; the caller drops a
#  placeholder from the package). Reasons for a refusal go to stderr.
#  Exit: 0 fine - 1 refused - 2 usage
#  Test seam: LINDOS_GPG=<program> is run instead of gpg.
#
#  Refused, because each of these would ship a source that trusts nothing or the wrong thing:
#     * the source is enabled but the keyring is a placeholder / missing / not a binary keyring
#     * the source is enabled but the pinned fingerprint is not a 40-digit hex fingerprint
#     * the source is enabled but its address is a reserved placeholder name
#     * a real keyring that holds anything but exactly ONE primary key, or whose primary fingerprint
#       differs from the pinned one (apt trusts every key of a Signed-By keyring, so a second key
#       appended behind the pinned one would be trusted too) - and a real keyring is never accepted
#       without gpg to check it
#     * a real keyring without a pinned fingerprint (the release job could not check what it signs with)
#     * a source file with more than one stanza, more than one address, an address that is neither https
#       nor plain http to the local machine (the e2e job), an address with credentials in it, a
#       Signed-By other than the shipped keyring, or any Trusted / Allow-Insecure / Allow-Weak /
#       Allow-Downgrade-To-Insecure override
# ============================================================================
set -Eeuo pipefail

ROOT="${1:-}"
if [ -z "${ROOT}" ] || [ "$#" -ne 1 ]; then
    printf 'usage: check-archive-keyring.sh PACKAGE_ROOT\n' >&2
    exit 2
fi
[ -d "${ROOT}" ] || { printf 'check-archive-keyring: not a directory: %s\n' "${ROOT}" >&2; exit 2; }

SOURCES="${ROOT}/etc/apt/sources.list.d/lindos.sources"
KEYRING="${ROOT}/usr/share/keyrings/lindos-archive-keyring.gpg"
FPR_FILE="${ROOT}/usr/share/lindos/archive-key.fingerprint"
GPG="${LINDOS_GPG:-gpg}"
SHIPPED_KEYRING="/usr/share/keyrings/lindos-archive-keyring.gpg"

problems=()
say() { printf 'check-archive-keyring: %s\n' "$*" >&2; }

if [ ! -f "${SOURCES}" ]; then
    say "missing ${SOURCES}"
    exit 1
fi

# deb822 stanzas (blank-line separated; comment lines skipped; continuation lines joined): "@stanzas|N", then
# one "N|lowercased-field|value" line per field
PARSED="$(awk '
    /^[ \t\r]*#/ { next }
    /^[ \t\r]*$/ { open = 0; next }
    {
        if (!open) { n++; open = 1 }
        if ($0 ~ /^[ \t]/ && cnt > 0 && stz[cnt] == n) {
            line = $0
            gsub(/^[ \t]+|[ \t\r]+$/, "", line)
            vals[cnt] = vals[cnt] " " line
            next
        }
        cnt++
        name = $0
        sub(/:.*/, "", name)
        gsub(/[ \t\r]/, "", name)
        value = $0
        sub(/^[^:]*:[ \t]*/, "", value)
        sub(/[ \t\r]+$/, "", value)
        stz[cnt] = n
        names[cnt] = tolower(name)
        vals[cnt] = value
    }
    END {
        printf "@stanzas|%d\n", n + 0
        for (i = 1; i <= cnt; i++) printf "%d|%s|%s\n", stz[i], names[i], vals[i]
    }' "${SOURCES}")"

stanzas="$(printf '%s\n' "${PARSED}" | sed -n 's/^@stanzas|//p')"
# the first stanza's field (case-insensitive name); empty when it is absent
field() {
    printf '%s\n' "${PARSED}" | awk -F'|' -v want="$1" '
        $1 == "1" && $2 == want && !done { sub(/^[^|]*\|[^|]*\|/, ""); print; done = 1 }'
}
has_field() {
    printf '%s\n' "${PARSED}" | awk -F'|' -v want="$1" '$1 == "1" && $2 == want { found = 1 } END { exit found ? 0 : 1 }'
}

if [ "${stanzas}" != "1" ]; then
    problems+=("the source file has ${stanzas} stanzas (exactly one is allowed: a second one could point apt anywhere)")
fi

enabled_raw="$(field enabled | tr '[:upper:]' '[:lower:]')"
case "${enabled_raw}" in
    no|false|0|off) enabled=0 ;;
    *) enabled=1 ;;   # apt's default is enabled
esac

uris="$(field uris)"
uri_count="$(printf '%s\n' "${uris}" | wc -w | tr -d ' ')"
uri="$(printf '%s\n' "${uris}" | awk '{print $1}')"
# scheme://authority/... -> authority (credentials and port kept, so they can be judged)
authority="$(printf '%s' "${uri}" | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#[/?].*$##')"
hostport="${authority##*@}"
case "${hostport}" in
    \[*\]*) host="${hostport%%]*}]" ;;          # [::1]:8099
    *) host="${hostport%%:*}" ;;                # name:port
esac
host="$(printf '%s' "${host}" | tr '[:upper:]' '[:lower:]')"
uri_placeholder=0
case "${host}" in
    ''|*.invalid|*.example|example.com|example.net|example.org|packages.lindos.dev) uri_placeholder=1 ;;
esac

if [ "${uri_count}" -gt 1 ]; then
    problems+=("the source lists ${uri_count} addresses (exactly one is allowed)")
elif [ -n "${uri}" ]; then
    case "${uri}" in
        https://*) ;;
        http://*)
            case "${host}" in
                localhost|127.0.0.1|'[::1]') ;;
                *) problems+=("the address ${uri} is plain http; only https (or http to the local machine, for the e2e job) is allowed") ;;
            esac ;;
        *) problems+=("the address ${uri} is neither https nor local http") ;;
    esac
    case "${authority}" in
        *@*) problems+=("the address ${uri} carries credentials or a decoy before an @") ;;
    esac
fi

signed_by="$(field signed-by)"
if [ "${signed_by}" != "${SHIPPED_KEYRING}" ]; then
    problems+=("Signed-By is '${signed_by:-missing}', it has to be exactly ${SHIPPED_KEYRING} (the shipped keyring)")
fi
for override in trusted allow-insecure allow-weak allow-downgrade-to-insecure; do
    if has_field "${override}"; then
        case "$(field "${override}" | tr '[:upper:]' '[:lower:]')" in
            no|false|0|off) ;;
            *) problems+=("the source sets ${override} (that switches signature checking off or weakens it)") ;;
        esac
    fi
done

key_state="missing"
if [ -f "${KEYRING}" ]; then
    if [ "$(head -c 18 "${KEYRING}" | tr -d '\000\r')" = "LINDOS-PLACEHOLDER" ]; then
        key_state="placeholder"
    elif LC_ALL=C grep -aq -- '-----BEGIN PGP' "${KEYRING}"; then
        key_state="armored"
    else
        # an OpenPGP public-key packet starts with tag byte 0x98-0x9b (old format) or 0xc6 (new format)
        first="$(od -An -tx1 -N1 "${KEYRING}" | tr -d ' \n')"
        case "${first}" in
            98|99|9a|9b|c6) key_state="real" ;;
            *) key_state="invalid" ;;
        esac
    fi
fi

fpr_state="missing"
fpr=""
if [ -f "${FPR_FILE}" ]; then
    fpr="$(tr -d ' \r\n' < "${FPR_FILE}" | tr '[:lower:]' '[:upper:]')"
    if [ "${fpr}" = "PLACEHOLDER" ]; then
        fpr_state="placeholder"
    elif printf '%s' "${fpr}" | grep -Eq '^[0-9A-F]{40}$'; then
        fpr_state="valid"
    else
        fpr_state="invalid"
    fi
fi

if [ "${enabled}" -eq 1 ]; then
    [ "${key_state}" = "real" ] || problems+=("the source is enabled but the keyring is ${key_state} (a real binary keyring is required: gpg --export, no --armor)")
    [ "${fpr_state}" = "valid" ] || problems+=("the source is enabled but the pinned fingerprint is ${fpr_state} (40 hex digits required)")
    [ "${uri_placeholder}" -eq 0 ] || problems+=("the source is enabled but its address (${uri:-none}) is a reserved placeholder name")
else
    case "${key_state}" in
        real) [ "${fpr_state}" = "valid" ] || problems+=("a real keyring is present but the pinned fingerprint is ${fpr_state}") ;;
        placeholder) ;;
        *) problems+=("the keyring is ${key_state}: commit the real keyring or leave the placeholder text file") ;;
    esac
fi

# the keyring is exactly the pinned key: one primary key, and it is the pinned one. A real keyring is never let
# through unchecked - without gpg the build stops, it does not skip the comparison.
if [ "${key_state}" = "real" ] && [ "${fpr_state}" = "valid" ]; then
    if command -v "${GPG}" >/dev/null 2>&1; then
        listing="$("${GPG}" --batch --no-tty --show-keys --with-colons "${KEYRING}" 2>/dev/null)" || listing=""
        primaries="$(printf '%s\n' "${listing}" | grep -c '^pub:' || true)"
        actual="$(printf '%s\n' "${listing}" | awk -F: '$1 == "fpr" && !done { print toupper($10); done = 1 }')"
        if [ -z "${actual}" ] || [ "${primaries}" -eq 0 ]; then
            problems+=("gpg could not read the keyring")
        elif [ "${primaries}" -ne 1 ]; then
            problems+=("the keyring holds ${primaries} primary keys; exactly one, the pinned one, is allowed (apt trusts every key in it)")
        elif [ "${actual}" != "${fpr}" ]; then
            problems+=("the keyring's primary fingerprint (${actual}) is not the pinned one (${fpr})")
        fi
    else
        problems+=("gpg is not installed, so the keyring cannot be compared with the pinned fingerprint (install gnupg; a real key is never shipped unchecked)")
    fi
fi

if [ "${#problems[@]}" -gt 0 ]; then
    for p in "${problems[@]}"; do
        say "REFUSED: ${p}"
    done
    exit 1
fi

if [ "${key_state}" = "real" ]; then
    printf 'real\n'
else
    printf 'placeholder\n'
fi
