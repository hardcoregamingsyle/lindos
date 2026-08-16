#!/bin/bash
# ============================================================================
#  build/docker-build.sh — build the Lindos ISO inside Docker (any host with
#  Docker: Ubuntu, Fedora, macOS with a Linux VM, WSL2 …).
#
#  Usage: build/docker-build.sh [--no-build] [--image NAME] [--shell] [-- build-iso.sh options…]
#     --no-build     do not (re)build the builder image
#     --image NAME   image tag (default ${DOCKER_IMAGE:-lindos-builder:latest})
#     --shell        open a root shell in the container instead of building
#     everything after '--' (or any unknown option) is passed to build-iso.sh,
#     e.g.  build/docker-build.sh -- --skip-download --no-cleanup
#
#  The repository is bind-mounted at /lindos, so out/ (cache, debs, ISO) lands
#  in the host checkout.  --privileged is needed for the chroot bind mounts.
#  Every build/config.env variable present in the caller's environment is
#  forwarded into the container (INCLUDE_STEAM=0 build/docker-build.sh …).
#  Docker itself may need sudo on your host; this script never calls sudo.
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="docker-build"

ROOT="$(repo_root)"
lindos_load_config

IMAGE="${DOCKER_IMAGE:-lindos-builder:latest}"
DO_BUILD=1
SHELL_MODE=0
PASS=()

usage() { sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) DO_BUILD=0; shift ;;
        --image) [ $# -ge 2 ] || die "--image needs a name" 2; IMAGE="$2"; shift 2 ;;
        --image=*) IMAGE="${1#*=}"; shift ;;
        --shell) SHELL_MODE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; PASS+=("$@"); break ;;
        *) PASS+=("$1"); shift ;;
    esac
done

require_cmd docker
if ! docker info >/dev/null 2>&1; then
    die "cannot talk to the Docker daemon (is it running? are you in the 'docker' group?)"
fi

if [ "${DO_BUILD}" -eq 1 ]; then
    log "docker build -t ${IMAGE} -f build/Dockerfile build/"
    docker build -t "${IMAGE}" -f "${BUILD_DIR}/Dockerfile" "${BUILD_DIR}"
fi

# Forward the effective configuration (config.env defaults + the caller's
# environment overrides, which lindos_load_config already merged) into the
# container.  Host-specific paths stay host-side: inside the container the
# repository is always /lindos and out/ is relative to it.
ENV_ARGS=()
mapfile -t names < <(grep -oE '^: "\$\{[A-Za-z_][A-Za-z0-9_]*:=' "${BUILD_DIR}/config.env" | sed -E 's/^: "\$\{//; s/:=$//')
names+=(http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY HEROIC_VERSION HEROIC_SHA256 PRISM_PPA ACCEPT_MSCOREFONTS_EULA)
for v in "${names[@]}"; do
    case "${v}" in
        OUT_DIR|WORK_DIR|CACHE_DIR|DEBS_DIR|ASSETS_DIR|BUILD_LOG|OVMF_CODE|OVMF_VARS|DOCKER_IMAGE|BASE_ISO_FILE) continue ;;
    esac
    if [ -n "${!v+x}" ]; then
        ENV_ARGS+=(-e "${v}=${!v}")
    fi
done

TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then TTY_ARGS=(-it); fi

log "docker run --privileged -v ${ROOT}:/lindos ${IMAGE} ${PASS[*]:-}"
if [ "${SHELL_MODE}" -eq 1 ]; then
    exec docker run --rm "${TTY_ARGS[@]}" --privileged \
        -v "${ROOT}:/lindos" -w /lindos "${ENV_ARGS[@]}" \
        --entrypoint /bin/bash "${IMAGE}"
fi
exec docker run --rm "${TTY_ARGS[@]}" --privileged \
    -v "${ROOT}:/lindos" -w /lindos "${ENV_ARGS[@]}" \
    "${IMAGE}" "${PASS[@]}"
