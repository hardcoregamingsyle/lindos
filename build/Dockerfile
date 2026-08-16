# Lindos ISO builder image (SPEC §1, §8)
#
#   docker build -t lindos-builder -f build/Dockerfile build/
#   docker run --rm -it --privileged -v "$PWD":/lindos lindos-builder [build-iso.sh options]
#
# --privileged is required: build-iso.sh chroots into the unpacked squashfs
# and bind-mounts /dev, /proc, /sys, /run there.  build/docker-build.sh wraps
# both commands.  Nothing from the repository is copied into the image; the
# repository is bind-mounted at /lindos at run time so out/ lands on the host.
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ENV LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TERM=xterm \
    NO_COLOR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        xorriso squashfs-tools rsync wget curl dosfstools mtools p7zip-full \
        dpkg-dev git ca-certificates gnupg gpg \
        python3 python3-pytest unzip \
        bash coreutils util-linux mount procps file zstd xz-utils bzip2 \
        shellcheck lintian isolinux syslinux-common grub-pc-bin grub-efi-amd64-bin \
        genisoimage \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /lindos
VOLUME ["/lindos"]

# The whole repository is expected at /lindos (bind mount).  build-iso.sh
# needs root inside the container (default for ubuntu images).  Invoked via
# bash so a checkout without the executable bit (Windows hosts) still works.
ENTRYPOINT ["/bin/bash", "/lindos/build/build-iso.sh"]
CMD []
