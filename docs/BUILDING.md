# Building Lindos

Two products come out of this repository:

1. **thirteen `.deb` packages** (`out/debs/lindos-*_1.0.0_all.deb`) — installable on any Linux
   Mint 22.x XFCE / Ubuntu 24.04 system (the eight packages `lindos-meta` Depends on, including
   `lindos-transfer` from Addendum W, plus `lindos-meta` itself and `lindos-kernel`, the opt-in
   `lindos-vm` / `lindos-winapps` from Addendum V, and `lindos-installer`, the installer flow that
   exists on the installation medium only — see "Installer flow" in §5);
2. **the ISO** (`out/lindos-1.0.0-xfce-64bit.iso`) — Linux Mint 22.2 XFCE remastered with those
   packages, the Lindos theme assets, the base tune, Wine and Steam pre-installed, and an installer
   that downloads and installs the rest (updates, drivers, Chrome, Wine/Proton, launchers, Mode
   apps) while it installs.

Everything is driven by `Makefile` → `build/*.sh` → `build/config.env`. Every knob is an
environment variable with a default (`: "${VAR:=default}"`), so `INCLUDE_STEAM=0 make iso` works
without editing any file.

## 1. Host requirements

| What | Needed for | Packages (Ubuntu/Debian) |
|---|---|---|
| any Linux, macOS or Windows (Git Bash) with Python 3.10+ | `make test`, `make lint`, `--only-debs` staging checks | `python3`, `python3-pytest` (`shellcheck` optional) |
| Debian/Ubuntu host with `dpkg-deb` | `make debs` | `dpkg-dev` |
| **root** on an Ubuntu/Debian host (chroot + bind mounts) | `make iso` | `xorriso squashfs-tools rsync wget curl dosfstools mtools p7zip-full dpkg-dev git ca-certificates gnupg python3 file` (the script checks `xorriso unsquashfs mksquashfs rsync wget curl mkfs.vfat mcopy 7z dpkg-deb git gpg python3` and prints the `apt install` line for what is missing) |
| Docker (any host) | `make iso-docker` | `docker`; the image is `ubuntu:24.04` + the list above + `shellcheck lintian isolinux syslinux-common grub-pc-bin grub-efi-amd64-bin genisoimage`; needs `--privileged` for the chroot |
| QEMU | `make qemu` / `make qemu-uefi` | `qemu-system-x86`, `ovmf` for UEFI, `/dev/kvm` access (group `kvm`) |
| disk | ISO build | ≥ 15 GiB free in `out/` (`MIN_FREE_GB`), plus the 3 GB base ISO in `out/cache/` |
| network | ISO build | base ISO download, `apt-get` inside the chroot, `fetch-assets.sh` (GitHub) |

## 2. `make` targets

| Target | Runs | Root? |
|---|---|---|
| `make` / `make help` | list of targets | no |
| `make debs` | `bash build/mkdeb.sh all` → `out/debs/*.deb` | no |
| `make assets` | `ASSETS_DIR=out/assets bash build/fetch-assets.sh --out out/assets` → `out/assets/` | no |
| `make iso` | `sudo -E bash build/build-iso.sh $(ISO_ARGS) [--iso $(BASE_ISO)]` (no sudo when already root); build-iso.sh itself runs mkdeb.sh, then fetch-assets.sh, then the chroot + repack | yes (sudo) |
| `make iso-docker` (alias `make docker-iso`) | `bash build/docker-build.sh -- $(ISO_ARGS) [--iso …]` | Docker |
| `make qemu` | `bash build/test-qemu.sh $(QEMU_ARGS)` — newest `out/lindos-*.iso`, SeaBIOS | no |
| `make qemu-uefi` | `bash build/test-qemu.sh --uefi $(QEMU_ARGS)` — OVMF | no |
| `make test` | `bash tests/run.sh` (falls back to `pytest` directly if run.sh is missing) | no |
| `make lint` | `bash -n` / `sh -n` / `py_compile` on everything, `shellcheck` if installed, `bash -n build/config.env`, JSON validity | no |
| `make clean` | removes `out/work out/debs out/hooks out/qemu out/*.iso out/*.sha256 out/build.log` and all `__pycache__` / `.pytest_cache`; keeps `out/cache` and `out/assets` | no |
| `make distclean` | `make clean` + `rm -rf out/` | no |

Make variables: `BASE_ISO=path/to/linuxmint-22.2-xfce-64bit.iso` (local base ISO → `--iso`),
`ISO_ARGS="--skip-download --no-cleanup"` (extra `build-iso.sh` flags), `QEMU_ARGS`, `PYTHON`
(default `python3`), `SUDO` (default `sudo`, empty when you already are root), `OUT_DIR` (default `out`). Any `config.env`
variable can be put in front: `INCLUDE_WINE=1 INCLUDE_STEAM=1 INCLUDE_FLATPAK_LAUNCHERS=0
KISAK_MESA=0 make iso`.

## 3. `build/build-iso.sh`

```
sudo build/build-iso.sh [options]
   --iso PATH          use this local base ISO instead of downloading
   --skip-download     use the cached ISO in out/cache/ (error if absent)
   --skip-debs         do not rebuild packages (use out/debs as is)
   --only-debs         build the .debs and stop (no root needed)
   --skip-assets       do not run build/fetch-assets.sh
   --skip-hooks NN,NN  skip chroot hooks by number (e.g. 60,70) or name
   --hooks-only        reuse out/work/{iso,squashfs-root} from a previous --no-cleanup run:
                       skip download/extract/unsquash, run the hooks again and repack
   --no-cleanup        keep out/work/ after a successful build
   --method M          ISO repack method: mkisofs (default) | replay
   -h, --help
```

Steps, in order (each logged to `out/build.log`, hooks additionally to `out/hooks/NN-name.log`):

1. **preflight** — root check, tools check, free space (`MIN_FREE_GB`), `out/` layout.
2. **debs** — `build/mkdeb.sh all` unless `--skip-debs`.
3. **assets** — `build/fetch-assets.sh --out out/assets` unless `--skip-assets`
   (failure is a warning: the ISO then keeps Mint's theme).
4. **base ISO** — download `BASE_ISO_URL` into `out/cache/` (or `--iso PATH` / `--skip-download`),
   verify `BASE_ISO_SHA256` when set (empty = warning).
5. **extract** — `xorriso -osirrox on` → `out/work/iso/`; `unsquashfs casper/filesystem.squashfs`
   → `out/work/squashfs-root/`. Layered-squashfs layouts (Ubuntu 23.04+) are not supported: the
   script stops with a clear message if `casper/filesystem.squashfs` is missing.
6. **chroot** — bind `/dev /dev/pts /proc /sys` (every bind is made `--make-private` so
   nothing propagates back onto the host), `/run` is a *private tmpfs* (host D-Bus/systemd
   sockets are never exposed; `LINDOS_CHROOT=1` is exported so lindos-tune knows it is in a
   chroot), copy `resolv.conf`, `policy-rc.d` blocks service starts, generated
   `/tmp/lindos/config.env` (effective config incl. env overrides, `export VAR=…`),
   `/tmp/lindos/debs/`, `/tmp/lindos/hooks/`, `/tmp/lindos/assets/` (theme trees only, no
   `.git`/downloads). Hooks run with `stdin </dev/null`; stray chroot processes are killed and
   the mounts torn down by the EXIT/INT/TERM trap.
7. **hooks** — `build/chroot/NN-*.sh` in order via
   `chroot SQ env -i … /bin/bash -Eeuo pipefail /tmp/lindos/hooks/NN-x.sh` (see §5).
   Environment inside: `LINDOS_BUILD=1 LINDOS_STAGE_DIR=/tmp/lindos DEBIAN_FRONTEND=noninteractive
   LC_ALL=LANG=C.UTF-8 HOME=/root` + proxy vars + `LINDOS_PASSTHRU_VARS`.
8. **casper kernel sync** (`SYNC_CASPER_KERNEL=1`, default) — newest `/boot/vmlinuz-*` +
   `initrd.img-*` from the chroot are copied to `casper/` so the live initrd carries the Lindos
   plymouth theme (Cubic-style). `0` keeps Mint's kernel/initrd.
9. **squashfs** — `mksquashfs -comp zstd -Xcompression-level 19 -b 1M` (`SQUASHFS_COMP`,
   `SQUASHFS_ARGS`); `casper/filesystem.size`, `filesystem.manifest` (`dpkg-query -W`),
   `filesystem.manifest-remove` (the base ISO's list is kept and `LIVE_ONLY_PACKAGES` —
   `lindos-installer` — is appended, so the installer removes it from the new system; without a base
   list the build only warns).
10. **overlay + branding** — `build/overlay/` copied verbatim (`boot/grub/grub.cfg`,
    `boot/grub/loopback.cfg`, `.disk/info`, `README.diskdefines`), then `@LINDOS_VERSION@`,
    `@LINDOS_VERSION_SHORT@`, `@LINDOS_CODENAME@`, `@MINT_VERSION@`, `@DISK_INFO@`,
    `@ISO_VOLID@` placeholders are filled from `config.env` and `@PRESEED@` from the base
    ISO's `preseed/*.seed` — **empty on the Mint 22.2 ISO, which has no `preseed/` directory**; the
    boot entries are the installer-flow entries of SPEC §17.1 (`Install Lindos` first, with
    `only-ubiquity oem-config/enable=true`; the old "OEM install (for manufacturers)" entry is gone);
    the BIOS menu `isolinux/live.cfg` is regenerated from them by `build/lib/boot_menu.py`; the build dies
    on an unfilled placeholder, on a first entry that lost `only-ubiquity oem-config/enable=true`, on any
    `username=mint`/`hostname=mint`, or on a kernel/initrd path in `grub.cfg` that is missing from the
    tree; every existing `isolinux/*.cfg`, `boot/grub/**/*.cfg` and `README.diskdefines` is sed-branded
    (Mint strings → Lindos) and `initrd`/`initrd.lz` names fixed.
11. **oem-config pool check** (`verify_oem_offline`) — `build/lib/verify_oem_pool.py` proves that the
    medium can supply oem-config to the new system (see "Installer flow"); the build fails when it cannot,
    unless `OEM_DEBS_DIR` names a matching fallback set or `REQUIRE_OEM_POOL=0`. Then `md5sum.txt` is
    regenerated.
12. **ISO** — method `mkisofs` (default): `xorriso -indev BASE -report_el_torito as_mkisofs`
    describes the base ISO's boot equipment (GRUB MBR, protective GPT, appended EFI partition)
    and those options are replayed verbatim with `xorriso -as mkisofs`, so BIOS + UEFI boot
    exactly like Mint's; the base ISO's `--modification-date` is kept (`KEEP_ISO_MODDATE=1`) so
    the EFI GRUB finds the medium by fs-uuid. Method `replay`: `xorriso -indev BASE -outdev OUT
    -boot_image any replay -update_r ISO_DIR /`. Each falls back to the other automatically.
    Volume id `LINDOS_1_0_0` (`ISO_VOLID`), publisher/app id from `config.env`.
13. **verify + sha256** — `out/<ISO_NAME>.sha256`; `--no-cleanup` keeps `out/work/`.

Traps unmount the chroot binds on any failure. Work files: `out/work/{iso,squashfs-root,orig,
eltorito-report.txt,mkisofs-opts.txt}`.

## 4. `build/config.env` — every knob

| Variable | Default | Meaning |
|---|---|---|
| `LINDOS_VERSION` / `LINDOS_CODENAME` | `1.0.0` / `Aurora` | product version, codename |
| `LINDOS_MAINTAINER` / `LINDOS_HOME_URL` | `Lindos Team <team@lindos.dev>` / `https://lindos.dev` | |
| `MINT_VERSION` / `MINT_CODENAME` / `BASE_UBUNTU_CODENAME` | `22.2` / `Zara` / `noble` | base release |
| `BASE_ISO_URL` | `https://mirrors.edge.kernel.org/linuxmint/stable/22.2/linuxmint-22.2-xfce-64bit.iso` | |
| `BASE_ISO_SHA256` | *(empty = skip verify, warn)* | fill for release builds |
| `BASE_ISO_FILE` | basename of the URL | cache file name in `out/cache/` |
| `OUT_DIR` / `WORK_DIR` / `CACHE_DIR` / `DEBS_DIR` / `ASSETS_DIR` | `out` / `out/work` / `out/cache` / `out/debs` / `out/assets` | |
| `ISO_NAME` | `lindos-${LINDOS_VERSION}-xfce-64bit.iso` | |
| `BUILD_LOG` | `out/build.log` | |
| `SQUASHFS_COMP` / `SQUASHFS_ARGS` | `zstd` / `-Xcompression-level 19 -b 1M` | |
| `ISO_VOLID` / `ISO_PUBLISHER` / `ISO_APPID` / `DISK_INFO` | `LINDOS_1_0_0` / `Lindos Team` / `Lindos 1.0.0 Aurora` / `Lindos 1.0.0 "Aurora" - Release amd64` | |
| `MIN_FREE_GB` | `15` | free space required in `OUT_DIR` |
| `ISO_METHOD` | `mkisofs` | `mkisofs` \| `replay` (see step 12) |
| `KEEP_ISO_MODDATE` | `1` | keep base ISO modification date (GRUB fs uuid) |
| `SYNC_CASPER_KERNEL` | `1` | copy chroot kernel/initrd into `casper/` |
| `LINDOS_PASSTHRU_VARS` | `HEROIC_VERSION HEROIC_SHA256 PRISM_PPA ACCEPT_MSCOREFONTS_EULA` | extra env forwarded into the hooks |
| `INCLUDE_WINE` | `1` | 60-compat.sh: Ubuntu `wine` + winetricks on the ISO |
| `INCLUDE_STEAM` | `1` | 70-gaming.sh: `install-gaming.sh --from-chroot ${GAMING_ITEMS}` |
| `INCLUDE_FLATPAK_LAUNCHERS` | `0` | 70-gaming.sh: preinstall `FLATPAK_LAUNCHERS` (big) |
| `KISAK_MESA` | `0` | 00-repos.sh: Kisak fresh Mesa PPA |
| `ADD_WINEHQ_REPO` / `ADD_STEAM_REPO` / `ADD_FLATHUB` / `ADD_MOZILLA_REPO` / `ENABLE_I386` | `1` / `1` / `1` / `0` / `1` | 00-repos.sh |
| `DEBLOAT_PURGE` | `hexchat rhythmbox hypnotix onboard gnome-calendar` | 10-debloat.sh purge list (absent packages skipped) |
| `DEBLOAT_DISABLE_SERVICES` | `ModemManager.service apport.service whoopsie.service kerneloops.service brltty.service speech-dispatcher.service NetworkManager-wait-online.service` | 10-debloat.sh: disabled, never purged. `bluetooth.service` is deliberately **not** here — it stays enabled by default (laptops need Bluetooth headphones/mice, and it is cheap when idle); only Lite mode's own `tune.d`/`mode.json` turns it off (see docs/RAM-BUDGET.md) |
| `EXTRA_PACKAGES` | *(empty)* | appended to 20-base.sh's required list |
| `LAPTOP_ESSENTIALS` | `linux-firmware firmware-sof-signed alsa-ucm-conf pipewire-audio wireplumber pipewire-pulse bluez blueman intel-microcode amd64-microcode ubuntu-drivers-common fwupd printer-driver-gutenprint ipp-usb` | 20-base.sh: laptop hardware-enablement packages, best effort (see docs/RAM-BUDGET.md for the size estimate and why `printer-driver-all`/`hplip` are not here) |
| `LINDOS_DEB_ORDER` | `lindos-core lindos-desktop lindos-tune lindos-compat lindos-gaming lindos-transfer lindos-setup lindos-settings lindos-installer lindos-meta` | 30-lindos-debs.sh (`lindos-transfer` installs before `lindos-setup` so the OOBE's transfer page can call it, SPEC-WINDOWS §33; `lindos-installer` is the medium-only installer flow, SPEC §17) |
| `LIVE_ONLY_PACKAGES` | `lindos-installer` | build-iso.sh: appended (space separated) to `casper/filesystem.manifest-remove`, so the installer removes them from the installed system |
| `REQUIRE_OEM_POOL` | `1` | build-iso.sh `verify_oem_offline`: `1` = fail the build when the medium cannot supply `oem-config` at the squashfs's ubiquity version (Ubiquity would silently skip it and the first boot would have no account wizard); `0` = warn only |
| `OEM_DEBS_DIR` | *(empty)* | build-iso.sh: a directory of `oem-config` + `oem-config-gtk` (+ closure) `.deb`s of the squashfs's ubiquity version; copied to `/lindos/oem-debs` on the medium and installed by `finalize.sh` with `dpkg -i` when Ubiquity did not install oem-config itself |
| `INSTALL_MODE_PACKAGES` | `1` | 30-lindos-debs.sh: also apt-install `modes/${TUNE_MODE}/mode.json` packages (best effort) |
| `PLYMOUTH_THEME` | `lindos` | 40-theme.sh |
| `TUNE_MODE` | `everyday` | 50-tune.sh: `lindos-tune apply --mode ${TUNE_MODE} --system --offline` |
| `GAMING_ITEMS` | `steam lutris` | 70-gaming.sh items |
| `FLATPAK_LAUNCHERS` | `org.prismlauncher.PrismLauncher com.heroicgameslauncher.hgl org.vinegarhq.Sober` | 70-gaming.sh when `INCLUDE_FLATPAK_LAUNCHERS=1` |
| `APT_OPTS` / `APT_MIRROR` / `DEBIAN_FRONTEND` | `-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3` / *(empty)* / `noninteractive` | apt inside the chroot |
| `QEMU_RAM_MB` / `QEMU_CPUS` / `QEMU_DISK_GB` / `OVMF_CODE` / `OVMF_VARS` | `4096` / `4` / `30` / `/usr/share/OVMF/OVMF_CODE_4M.fd` / `/usr/share/OVMF/OVMF_VARS_4M.fd` | test-qemu.sh |
| `DOCKER_IMAGE` | `lindos-builder:latest` | docker-build.sh |

## 5. Chroot hooks (`build/chroot/`)

All hooks are `#!/bin/bash`, `set -Eeuo pipefail`, idempotent, and source
`/tmp/lindos/hooks/lib.sh` (`log warn die apt_install apt_try_install apt_purge pkg_installed
pkg_available pkgs_installed_matching mark_manual_installed mark_meta_deps_manual svc_disable svc_mask svc_enable fetch online`; test seam `LINDOS_HOOK_PATH_PREFIX` puts fake apt-get/dpkg-query/apt-mark first in `PATH`).

| Hook | What it does |
|---|---|
| `00-repos.sh` | `dpkg --add-architecture i386`; WineHQ deb822 `.sources` + key (`/etc/apt/keyrings/winehq-archive.key`, `/etc/apt/sources.list.d/winehq-noble.sources`); Valve Steam repo (`/usr/share/keyrings/steam.gpg`, `/etc/apt/sources.list.d/lindos-steam.list` — same paths as `install-gaming.sh`); Flathub system remote; optional Kisak Mesa (`KISAK_MESA=1`, key from Launchpad API + keyserver); optional Mozilla repo (`ADD_MOZILLA_REPO=1`, pinned 1000 — off by default because Mint pins its own firefox); optional `APT_MIRROR` rewrite; `apt-get update` |
| `10-debloat.sh` | purge `DEBLOAT_PURGE` one package at a time (safety net refuses network/printing basics); first `mark_meta_deps_manual` (everything `mint-meta-*` depends on becomes manual, purging `hexchat` drags the metapackage out), then `safe_autoremove` (lib.sh: simulates first, marks xfce/lightdm/network-manager/linux-/grub… and the named Mint keepers manual, refuses when > 60 packages would go); disable `DEBLOAT_DISABLE_SERVICES` (bluetooth stays enabled — see the table above); hide `mintwelcome` / `mintreport` / `update-notifier` autostarts (76 purges mintwelcome; lindos-tune and the Mint sweep hide it as well should that be skipped) |
| `20-base.sh` | required set in one transaction (unavailable packages skipped with a loud warning): `xfce4-docklike-plugin xfce4-panel-profiles xfce4-clipman-plugin xfce4-notifyd xfce4-whiskermenu-plugin xfce4-pulseaudio-plugin xfce4-power-manager xfce4-screenshooter xfce4-taskmanager xfce4-appfinder picom systemd-zram-generator earlyoom power-profiles-daemon lm-sensors fancontrol gamemode mangohud python3 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 gir1.2-glib-2.0 polkitd pkexec flatpak xdg-desktop-portal-gtk xdg-utils desktop-file-utils shared-mime-info fonts-noto-color-emoji winbind cabextract icoutils zenity librsvg2-bin curl wget gpg ca-certificates zstd` + `EXTRA_PACKAGES`; nice-to-have set best effort per package (`fonts-noto-core fonts-inter fonts-jetbrains-mono fonts-liberation libnotify-bin xdotool wmctrl x11-xserver-utils mesa-utils vulkan-tools libvulkan1 mesa-vulkan-drivers pciutils usbutils hdparm nvme-cli smartmontools inxi yad emote baobab gnome-disk-utility pavucontrol gufw timeshift mugshot plymouth-themes plymouth-label mousepad ristretto evince vlc`); then `LAPTOP_ESSENTIALS` best effort (firmware, audio UCM configs, Bluetooth, driver metadata, fwupd, a print driver — see the config.env table above and docs/RAM-BUDGET.md); everything `--no-install-recommends` |
| `30-lindos-debs.sh` | one `apt-get install --no-install-recommends ./…deb` transaction in `LINDOS_DEB_ORDER` (fallback `dpkg -i` + `apt-get -f install`) — the heavy Recommends of lindos-compat/gaming/meta (Wine, Steam, Lutris, …) are *not* pulled onto the ISO; `INCLUDE_WINE` / `INCLUDE_STEAM` / 20-base.sh decide the optional stacks; optional mode packages; `lindos-tune status --json` smoke test |
| `40-theme.sh` | `/tmp/lindos/assets/install-into-chroot.sh` (Fluent → `Lindos-Dark`/`Lindos-Light`, icons `Lindos`, cursors, fonts); `/usr/libexec/lindos/apply-branding.sh` (os-release sed keeping `ID=linuxmint`, `/etc/issue`, `/etc/lindos-release`, plymouth + wallpaper alternatives, and the Mint sweep — see below); `/usr/libexec/lindos/build-panel-profiles.sh` (`panel.tar.bz2` per mode); `fc-cache`, icon caches, glib schemas, desktop/mime databases; `plymouth-set-default-theme lindos && update-initramfs -u -k all` |
| `50-tune.sh` | `lindos-tune apply --mode ${TUNE_MODE} --system --offline` (presets, sysctl, zram, journald, tmpfiles, earlyoom); honest minimum (preset + fstrim.timer) with a loud warning if lindos-tune is missing |
| `60-compat.sh` | `INCLUDE_WINE=1` → `/usr/libexec/lindos/install-compat.sh --minimal --from-chroot --no-update` (Ubuntu `wine` + `wine32:i386`, winetricks, cabextract, 32-bit GL/Vulkan; WineHQ *staging* and umu-launcher are installed by the installer, step `compat`, to keep the ISO small); always: MIME/desktop database refresh + `lindos-compat doctor` report |
| `70-gaming.sh` | always: `mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 libvulkan1:i386 steam-devices vulkan-tools mesa-utils` (+ `mangohud:i386` best effort); `INCLUDE_STEAM=1` → `install-gaming.sh --from-chroot ${GAMING_ITEMS}`; `INCLUDE_FLATPAK_LAUNCHERS=1` → Flatpaks. NVIDIA drivers are **not** preinstalled (the installer's `drivers` step installs proprietary GPU drivers only with consent and never when Secure Boot would need a key enrolment; otherwise mintdrivers / `lindos-drivers` on demand) |
| `76-mint-purge.sh` | takes Mint's own apps and artwork out of the image behind `apt-get -s purge` allow-lists (a group that would remove anything outside itself is skipped with `MINT-PURGE-SKIPPED`, never a build failure) — see *Unrecognisable* below |
| `77-mint-sweep.sh` | last pass over what still says "Linux Mint" — see *Mint sweep* below: re-runs `apply-branding.sh --files-only` over the final image, checks Mint Welcome cannot autostart, purges `mint-backgrounds-*` only when `apt-get -s purge` shows nothing else would go with them, prints an audit of everything left; every step guarded, idempotent, never fails the build |
| `78-installer-brand.sh` | rebrands the live installer (Ubiquity): product name, launcher, artwork, slideshow, GTK skin — see *Installer branding* below; every step guarded (a missing file is a warning), idempotent |
| `79-installer-flow.sh` | wires the installer flow into Ubiquity — see *Installer flow* below: refuses a host outside the chroot; **dies** if Chrome/Edge is installed in the image (SPEC §0.1), if the `lindos-installer` files or `/usr/lib/ubiquity` are missing, or if the deployed hook would be skipped by Ubiquity (a `.` in the name, not executable, a symlink, a syntax error, `set -e`); `install -m 0755` of `target-config.sh` as `/usr/lib/ubiquity/target-config/50lindos-install`; `install -m 0755` of `dm-noblank.sh` as `/usr/lib/ubiquity/dm-scripts/install/50lindos-noblank` (the ubiquity-dm hook that runs `xset s off s noblank -dpms` in the `only-ubiquity` session; the build **dies** if `xset` is not in the image, if the file would be skipped by ubiquity-dm — a `.` in the name, not executable, a symlink, CRs, not `#!/bin/sh`, a syntax error, `set -e` — or if it does not really set the three things); `debconf-set-selections` of `lindos.seed` and a read-back of `ubiquity/success_command`; logs the target-config directory and the ubiquity version; idempotent. Test seams `LINDOS_INSTALLER_ROOT`, `LINDOS_DEBCONF_SET`, `LINDOS_DEBCONF_COMMUNICATE`, `LINDOS_DPKG_QUERY` |
| `80-cleanup.sh` | `apt-get autoremove --purge`, `apt-get clean`, drop apt lists (**on purpose, still**: the base ISO's lists are stale by install time and cost ~100 MB in the squashfs; the installer hook refreshes the new system's lists as its first step), machine-id reset, resolv.conf restore, logs truncated, root history/caches, `/tmp` `/var/tmp` emptied, crash reports/journal removed |
| `81-unrecognisable-gate.sh` | read-only report of what still makes the image recognisable as Linux Mint (`out/hooks/81-unrecognisable-gate.log`); report-only unless `LINDOS_STRICT_UNRECOGNISABLE=1` — see *Unrecognisable* below |

### Installer branding (Ubiquity)

The installer on the ISO is Ubiquity (Linux Mint's fork, GTK frontend). `78-installer-brand.sh` runs
after every package-installing hook and before `80-cleanup.sh`, so nothing reinstalls Ubiquity over
its edits. It reads `build/installer/` (staged into the chroot by `build-iso.sh` as
`/tmp/lindos/installer`) and changes, in the squashfs only:

| Where Ubiquity gets it | What the hook does |
|---|---|
| `/var/cache/debconf/templates.dat` — every `ubiquity/text/*` string, all languages | inside `ubiquity/*` stanzas only: `${RELEASE}`, `${DISTRO}` and the hard-coded "Linux Mint" become "Lindos"; the English window title "Install" becomes "Lindos Setup" (a line-count check discards the rewrite if the file's structure changed) |
| `/usr/share/ubiquity/gtk/*.ui` | "Linux Mint" fall-back labels (e.g. the disk-resize bar) become "Lindos" |
| `/usr/share/applications/ubiquity.desktop` (the live-desktop launcher; casper substitutes `RELEASE` from `/cdrom/.disk/info` at boot and copies it to the live user's Desktop) | `Name=Install Lindos` in every language, `Icon=lindos-logo`, and `GTK_THEME=Lindos-Setup` inside the existing `sh -c '…'` (nothing else in `Exec=` changes) |
| `/usr/share/ubiquity-slideshow/slides/` (shown while files are copied; `slideshow.conf` keeps the window size) | replaced by `build/installer/slideshow/`: six static slides, CSS-only, no script, no network — Windows-style look, the five Modes, Windows apps via Wine/Proton, gaming with the honest anti-cheat caveat, privacy, and what the installer is downloading now versus what the first boot asks (account, then Lindos Setup) |
| `/usr/share/ubiquity/pixmaps/{ubuntu_installed,cd_in_tray}.png`, `ubuntu/logo.png` | redrawn from `/usr/share/pixmaps/lindos-logo.svg` with `rsvg-convert` at the original pixel size (a size mismatch keeps the original) |
| `/usr/share/icons/hicolor/*/apps/{ubiquity,mintubiquity}.svg` | the Lindos logo |
| `/usr/share/themes/Lindos-Setup/` | `build/installer/themes/Lindos-Setup/gtk-3.0/gtk.css`: `Lindos-Dark` plus accent colours for Ubiquity's own `.ubiquity-next`, `.ubiquity-menubar` and progress bars — colours and buttons only, no geometry; only installed when `Lindos-Dark` is present |
| `/sbin/casper-stop` ("Please remove the installation medium, then press ENTER") | carries no product name; only a defensive text rewrite |

Deliberately **not** changed: partitioning behaviour, Ubiquity's Python code (so the OEM mode's
window title "OEM mode, for manufacturers only", the OEM-id box on the language page and the literal
"OEM Configuration (temporary user)" stay Ubiquity's own text), compiled `.mo`
catalogues (a non-English installer can still say "Linux Mint" in a few translated strings),
`/cdrom/.disk/info` (written by `build-iso.sh`; Ubiquity's `get_release()` and casper read it),
the live user name and host (`liveuser` / `lindos` on the GRUB command lines — changed by the Mint sweep
below) and the installed system's `/etc/lsb-release` / GRUB title (also the sweep's). The hook ends with an audit that lists, in `out/hooks/78-installer-brand.log`,
every installer file that still mentions "Linux Mint". The Ubiquity files it edits are owned by the
`ubiquity*` packages, which the installer removes from the installed system; the few files the hook
adds (slideshow, `Lindos-Setup` theme) are tiny and stay behind as orphans.

The `Lindos-Setup` skin is applied only through the launcher's `Exec` line, so the `Install Lindos` boot
entries (`only-ubiquity`, which start `ubiquity-dm` on its own X server) and the oem-config first-boot
wizard show Ubiquity with the `Lindos-Dark` defaults instead. Systemd drop-ins that set `GTK_THEME` for
`ubiquity.service`/`oem-config.service` could change that; they are not done (untested idea).

Hermetic tests: `build/tests/test_installer_brand.py` runs the real hook under bash against a fake root.
Test seams (unset in a real build): `LINDOS_INSTALLER_ROOT` (prefix for every path),
`LINDOS_INSTALLER_SRC` (where `build/installer/` is), `LINDOS_RSVG` (rsvg-convert replacement). What
only a real boot can confirm: how the slideshow renders in Ubiquity's WebKit view, that the GTK skin
loads and looks right, the launcher on the live desktop, and the strings on every page — eyeball the
installer in QEMU (`make qemu`) after a build.

### Installer flow (Ubiquity OEM mode)

The flow (SPEC §17; the user-facing version is [INSTALLER.md](INSTALLER.md)): the live session shows
nothing but the installer; the installer does everything heavy while it installs (updates, drivers,
Chrome, Wine/Proton and launchers, the Modes' apps and Flatpaks); the first boot of the new system
only asks for the account (Ubiquity's oem-config wizard) and then runs the install-free Lindos Setup.
The mechanism is **Ubiquity in OEM mode** plus two scripts of the medium-only package
`lindos-installer`.

**Where everything is**

| In the repository | On the medium / in the installed system | Role |
|---|---|---|
| `packages/lindos-installer/.../installer/target-config.sh` | copied by `79-installer-flow.sh` to `/usr/lib/ubiquity/target-config/50lindos-install` (mode 0755, no `.` in the name) | the **hook**: downloads and installs into `/target` |
| `.../installer/lib.sh` | `/usr/libexec/lindos/installer/lib.sh` | helpers (logging, time boxes, entering the target, install-state, holds, repair); also the runner `bash lib.sh --enter TARGET CMD…` |
| `.../installer/finalize.sh` | `/usr/libexec/lindos/installer/finalize.sh` | `ubiquity/success_command`: arms oem-config, <5 s |
| `.../installer/dm-noblank.sh` | copied by `79-installer-flow.sh` to `/usr/lib/ubiquity/dm-scripts/install/50lindos-noblank` (mode 0755, no `.` in the name) | the **ubiquity-dm hook**: `xset s off s noblank -dpms` in the `only-ubiquity` session (no desktop, so nothing else stops X's screensaver/DPMS; a logind inhibitor cannot). `finalize.sh` removes the copy from the new system |
| `.../share/lindos/installer/lindos.seed` | baked into the image's debconf database by `79` | `oem-config/enable`, `ubiquity/success_command`, `download_updates=false`, `apt-setup/multiarch=i386`, `user-setup/allow-password-empty=true` |
| `.../share/lindos/installer/lindos-installer.templates` | same path | the one-line status template (`db_progress INFO`) |
| `.../share/lindos/installer/extras.json` | same path | union of every Mode's extras (generated by `build/lib/installer_extras.py --write`; `--check` fails when stale) |
| `build/chroot/79-installer-flow.sh` | runs in the chroot after 78, before 80 | deploys the hook, bakes the seed, audits |
| `build/lib/boot_menu.py` | run by `build-iso.sh` | generates `isolinux/live.cfg` from `grub.cfg` |
| `build/lib/verify_oem_pool.py` | run by `build-iso.sh` (`verify_oem_offline`) | proves the medium can supply oem-config |
| `build/overlay/boot/grub/{grub,loopback}.cfg` | ISO root | the boot entries (SPEC §17.1) |
| `packages/lindos-core`: `lindos/{session,installstate}.py`, `is-live-session`, `oem-config-pending`, `wait-for-network`, `browser-firstboot.sh`, `lindos-live-inhibit.service`, `install-browser.sh` | installed system | the shared helpers, the silent retry and the `--in-installer` mode |
| `packages/lindos-gaming`: `driver-firstboot.sh` + service; `install-gaming.sh`; `packages/lindos-compat`: `install-compat.sh` | installed system | silent driver retry; `--in-installer` mode of both scripts |
| `packages/lindos-desktop`: `lindos-live-session.desktop`, `live-session-power.sh` | installed system (acts only in a live session) | never-sleep for the live desktop |

`lindos-installer` is installed into the squashfs by `30-lindos-debs.sh` (`LINDOS_DEB_ORDER`) and
listed in `casper/filesystem.manifest-remove` (`LIVE_ONLY_PACKAGES`), so Ubiquity removes it from the
new system; `lindos-meta` does not depend on it.

**The hook contract** (full text: SPEC §17.3). Ubiquity runs every executable file without a `.` in its
name in `/usr/lib/ubiquity/target-config` once per installation, in the *live* environment as root, in
`os.listdir()` order (not sorted), as `log-output -t ubiquity --pass-stdout HOOK`; the exit status is
ignored and there is **no timeout**, so a hook that hangs hangs every install and a hook that leaves
dpkg broken makes Ubiquity's later steps (language packs, codecs, removals) silently skip or abort.
Hence: always exit 0, no `set -e`, never write to stdout (it is the debconf pipe), every step
time-boxed and clamped to one 45-minute budget, downloads (killable) split from dpkg runs (never killed),
a repair pass after every step, kernel/boot-loader/Ubiquity families held and released on every exit
path, no mount left behind (private mount namespace), markers only after verified success. A
deployed name that contains a `.` or lacks the exec bit makes Ubiquity skip the hook **silently** —
`79` refuses both (and a `set -e`, a syntax error, a symlink) and logs `ls -l` of the directory.

**The ubiquity-dm hook** (`dm-noblank.sh`). The `Install Lindos` boot entries (`only-ubiquity`) start no desktop
session: `ubiquity.service` runs `ubiquity-dm`, which starts a bare `X -br -ac -noreset -nolisten tcp` and the
installer on it. Nothing configures that server's screensaver or DPMS, whose defaults blank the display after ten
minutes without input, and `lindos-live-inhibit.service` (a logind inhibitor) cannot stop them. `ubiquity-dm`
runs every executable file without a `.` in `/usr/lib/ubiquity/dm-scripts/install` once, after X is up and before
the installer window exists, as the live user, with `subprocess.call` (no shell: the shebang and the exec bit
matter; its output goes to `/var/log/installer/dm`); it waits for it and ignores the status, so the hook is
time-boxed (`timeout`), always exits 0 and makes one `xset` call per setting. `-noreset` keeps the settings after
`xset` exits. Only the install pass runs this directory (`oem-config`'s first boot uses `dm-scripts/oem`); the
*Try Lindos* desktop has `live-session-power.sh` instead.

**Ubiquity's OEM mode, in short.** With `oem-config/enable=true` the installer's account page becomes a
*temporary* account (`oem`, computer name, password, empty allowed here through
`user-setup/allow-password-empty=true`) — a preseed cannot hide that page. OEM mode does **not** arm the
first-boot wizard by itself (upstream a human runs `oem-config-prepare`), so `finalize.sh` does its
essentials: units to `/lib/systemd/system`, enable, `set-default oem-config.target`, but *without* its
deletion of the NetworkManager profiles; it also strips the stale `autologin-user=oem` from
`lightdm.conf`, locks `oem` and resets `allow-password-empty` (SPEC §17.7; the reset runs first, on every path,
because the seed that lets the temporary account keep an empty password is only for the installer's page).
**When oem-config is missing or arming fails** the machine still boots into `oem`'s autologin desktop (a machine
nobody can log in to is worse), but that account has an empty password when the user followed the page, and is
in `sudo`: `finalize.sh` therefore gives it a random password through `chpasswd` (stdin only, never in a log;
kept root-only in `/var/lib/lindos/oem-temporary-password` and printed on the account's own desktop in
`LINDOS-ACCOUNT-SETUP-FAILED.txt`), or locks it (`passwd -l`, then a direct `/etc/shadow` edit) when that fails; a
password the user chose is left alone. It logs `CRITICAL` and writes the reason plus what it did to
`/var/lib/lindos/oem-config-not-armed`.

**Steps, states and switches.** Steps run `browser drivers updates compat gaming mode_extras flatpaks`
and end in `/var/lib/lindos/install-state.json` as `done | pending | skipped | failed`
(`lindos-config install-state [--json]`; `python3 -m lindos.installstate [--root DIR] show`). Kernel
words: `lindos.install=off`, `lindos.install_budget=SECONDS` (default 2700),
`lindos.proprietary_drivers=1` (consent, SPEC §17.5). The hook refreshes the new system's apt lists
first, holds the families it must not touch, and does `apt-get upgrade` (never `dist-upgrade`). Stock
`apt-get update` exits 0 after *transient* index failures, so the exit status alone is not the verdict
(`li_apt_update` in `lib.sh`): the installer's apt.conf sets `APT::Update::Error-Mode "any"`, the output is
scanned for `Err:` / `E:` / "Failed to fetch", and at least one non-empty network `Packages` list must exist; any
of these failing retries the update once, and if it still fails the steps that learn from the lists (updates,
drivers, extra apps) end *pending*, never *done*.

**Logs.** `/var/log/lindos/installer-hook.log` (live environment, written while installing — open a
terminal in the *Try* session to follow it), `/target/var/log/lindos/installer.log` = `/var/log/lindos/
installer.log` after the reboot (the same lines plus `finalize.sh`'s), Ubiquity's own
`/var/log/installer/syslog`, the install scripts' `/var/log/lindos/install-*.log`. On a failure to
arm oem-config: `/var/lib/lindos/oem-config-not-armed` holds the reason (and, on line 2, what was done to the
temporary account); `/var/log/installer/dm` has the output of the ubiquity-dm hook.

**Build-time guards** (`build-iso.sh`): the first `grub.cfg` entry must keep `only-ubiquity
oem-config/enable=true`; no `username=mint`/`hostname=mint` anywhere in `boot/grub` or `isolinux`;
`isolinux/live.cfg` is rewritten from the GRUB entries (`boot_menu.py`); `verify_oem_offline` fails the
build unless the pool carries matching `oem-config` + `oem-config-gtk` or `OEM_DEBS_DIR` names a fallback
set (copied to `/lindos/oem-debs` and installed by `finalize.sh`) — `REQUIRE_OEM_POOL=0` downgrades
that to a warning; `79` dies when the hook, the scripts or `/usr/lib/ubiquity` are missing, or when
Chrome/Edge are installed in the image (SPEC §0.1).

**Base-ISO assumptions** (re-check them whenever `MINT_VERSION` / `BASE_ISO_URL` changes; the first four
were read from the real Mint 22.2 XFCE ISO's file tree and from Ubiquity's upstream source, none of it
run):
1. Ubiquity is Mint's fork 24.04.3+mintNN; `run_target_config_hooks` behaves as described above
   (re-read `scripts/plugininstall.py` of the new version; it differs between releases).
2. The medium's `pool/main/u/ubiquity/` holds `oem-config` and `oem-config-gtk` of the same version as
   the squashfs's `ubiquity`; `.disk/info`, `.disk/cd_type` and `dists/` exist (apt-setup's `cdrom:`
   generator needs them); `aptdaemon` and `python3-aptdaemon.gtk3widgets` are installed or in the pool.
3. There is **no `preseed/` directory**: `@PRESEED@` in the overlay is empty (the old text about Mint's
   `linuxmint.seed` was wrong for 22.2). A Lindos preseed would need its own file plus a `file=` word in
   every boot entry (GRUB, loopback and isolinux); the flow uses the baked debconf database and
   individual `owner/key=value` kernel words instead (no spaces allowed in values).
4. The base target's apt sources are `/etc/apt/sources.list.d/official-package-repositories.list` (Mint
   `zara` + Ubuntu `noble*`, http) plus the `deb cdrom:` line apt-setup adds; the hook never reads the
   latter (`Dir::Etc::SourceList=/dev/null`) so nothing asks for the medium.
5. `casper/filesystem.manifest-remove` exists; without it the build only warns and `lindos-installer`
   would stay on the installed system.

**What the base's `80-cleanup.sh` does to this.** It deletes `/var/lib/apt/lists` (stale lists cost ~100
MB in the squashfs) — so on a stock-Mint comparison Ubiquity's own language-pack and codec steps were
silent no-ops without lists. The hook's first act in the target is `apt-get update`, which repairs that as
a side effect; **consequence:** after the hook the target has network lists and Ubiquity's later
`install_language_packs` / `install_extras` / `install_restricted_extras` go online too (longer, and a
dropped connection there is not caught by the hook).

**Testing it without real hardware.** Hermetic tests run on any host (fake `/target`, a fake
`LINDOS_TARGET_RUNNER`, `LINDOS_INSTALL_BUDGET`, `LINDOS_TIMEOUT_PCT`, `LINDOS_TEST_CMDLINE`,
`LINDOS_DRY_RUN=1`): `packages/lindos-installer/tests` (hook, finalize, `--in-installer` modes, package
layout), `build/tests/test_installer_flow_hook.py` (79 against a fake root, `LINDOS_INSTALLER_ROOT`),
`build/tests/test_verify_oem_pool.py`, `tests/test_boot_menu.py`, `packages/lindos-core/tests`
(`test_session`, `test_installstate`, `test_wait_for_network`, the first-boot scripts) and
`packages/lindos-settings/tests/test_setup_pending.py`. They prove the logic; **only a real install proves
the mechanism** — this is the QEMU procedure, expect several rounds:

1. *Build check.* `out/hooks/79-installer-flow.log` shows `deployed …/50lindos-install (mode 0755 …)`, the
   directory listing next to the base's own hooks, and `read back ubiquity/success_command`; `out/build.log`
   shows the `oem-config pool check` result; `xorriso -indev out/lindos-*.iso -find /pool -name
   'oem-config*'` lists both packages (and `-find /lindos/oem-debs` when a fallback set was used).
2. *Boot both menus.* `make qemu-uefi` and `make qemu` (BIOS): entry 1 must go straight to the installer
   (no desktop), entry 3 must give the live desktop with an *Install Lindos* icon and **no** Lindos Setup
   window; the desktop must never blank or suspend (`lindos-live-inhibit.service`, `live-session-power.sh`). In
   entry 1 wait more than ten minutes at the first page: the screen must stay on, `grep lindos-noblank
   /var/log/installer/dm` shows the hook ran (from a second terminal, `DISPLAY=:0 xset q` says `timeout: 0` and
   `DPMS is Disabled`); `ls -l /usr/lib/ubiquity/dm-scripts/install` shows `50lindos-noblank`, mode 0755.
3. *Install online.* `build/test-qemu.sh --uefi --disk 30` (or `--disk` with BIOS), Try entry so you have a
   terminal: start *Install Lindos*, follow `tail -f /var/log/lindos/installer-hook.log` and watch the
   installer's status line change per step. Accept the temporary-account page (empty password).
4. *Inspect before rebooting* (from the live terminal, `/target` is still mounted): `mount | grep /target`
   shows only the partition mounts (none of `/proc /sys /dev /run`); `chroot /target dpkg --audit` is empty;
   `chroot /target dpkg -s google-chrome-stable`; `cat /target/var/lib/lindos/install-state.json`;
   `chroot /target apt-mark showhold` is empty and `/target/etc/apt/preferences.d/00lindos-installer.pref`
   is gone; `readlink /target/etc/systemd/system/default.target` is `oem-config.target`;
   `/target/lib/systemd/system/oem-config.{service,target}` exist; no `autologin-user=oem` in
   `/target/etc/lightdm/lightdm.conf`; `chroot /target passwd -S oem` says locked; `browser-firstboot.done`
   and `driver-firstboot.done` exist; `/target/var/log/lindos/installer.log` reads sensibly and ends with
   `finalize: oem-config is armed`.
5. *First boot.* Boot the disk: the oem-config account wizard must appear (not the installer, not a
   desktop), the wizard must create the account and delete `oem`, LightDM must start, and
   `lindos-setup --first-run` must start for the new user without any download or update
   (`journalctl -b` shows the retry units did not run before the wizard). Check what the wizard looks like.
6. *Failure drills.* Offline (`-nic none` in the command line `test-qemu.sh --dry-run` prints): the install
   completes, `online` is `false`, every step is `pending`; boot online later and the silent retries add
   Chrome/drivers, Settings › Apps lists the rest. A black-holed network (packets dropped): the hook times
   out inside its budget and `dpkg --audit` is still empty. `kill -TERM` the hook mid-download: holds are
   gone, dpkg is clean. `lindos.install=off`: every step `skipped`. Secure Boot with an NVIDIA GPU (real
   hardware only): the proprietary driver step is `skipped` with the reason.
7. *Automate it.* The CI work adds a QEMU install test: `build/qa/install_test.py` installs the ISO onto a
   blank virtual disk with Ubiquity in OEM mode driven by a CI-only preseed (`automatic-ubiquity`), powers the
   guest off and mounts the disk read-only; `build/qa/install_checks.py` asserts the properties of item 4 on
   the mounted tree (unit-tested with fake trees in `build/tests`), `build/qa/ci-observer.sh` logs the run to
   the serial console, and the installed disk is then booted to see that oem-config, not a desktop, starts.
   It is a manual `workflow_dispatch` job and, when this was written, had **not run on GitHub Actions**
   (check `build/qa/` and `.github/workflows/ci.yml` for its state; `CI-LOGS.md` has the history). Never put
   `automatic-ubiquity` on a consumer boot entry: when its X server fails it falls back to an unattended
   `ubiquity noninteractive` install.

What only a real install can confirm is listed in [INSTALLER.md](INSTALLER.md#known-limitations-and-what-is-unverified).

### Mint sweep (what still says "Linux Mint")

Lindos is a Linux Mint remaster; the base packages ship menu entries, autostarts, release files and
browser defaults that say so. `packages/lindos-desktop` carries the sweep (stdlib Python, offline,
idempotent): `/usr/libexec/lindos/rebrand-base.py` driven by `/usr/share/lindos/branding/base-sweep.json`,
started by `apply-branding.sh` (lindos-desktop postinst, `40-theme.sh`, and `77-mint-sweep.sh` once more over
the final image). What a user sees, and what is done about it:

| Where it shows | What Lindos does |
|---|---|
| Menu: **Welcome Screen** ("Welcome to Linux Mint") | hidden (`NoDisplay=true`); Lindos Setup is the welcome experience. The package stays (Mint's metapackages depend on it) |
| Login: the Mint Welcome window next to Lindos Setup | hidden three ways: `Hidden=true` in the system autostart entry (`10-debloat.sh` and the sweep — found by `Exec`, not by file name), `NotShowIn=XFCE` by lindos-tune, and a per-user off switch `/etc/skel/.config/autostart/mintwelcome.desktop` (the same override XFCE's *Session and Startup* dialog writes) |
| Menu: **Software Manager** | hidden; `lindos-store.desktop` (**Lindos Store**, Lindos icon, "Find, install and remove apps (system packages and Flatpak)") starts the same `mintinstall`. It is not, and is never described as, the Microsoft Store. The window title inside is still upstream's "Software Manager" |
| Menu: **Update Manager**, **Driver Manager**, Software Sources, System Reports, Languages | kept for now — Lindos has no replacement for the OS-update and driver GUIs yet; they are renamed (*Lindos Updates*, *Lindos Drivers*, ...) and get Lindos icons (`lindos-update`, `lindos-drivers`, `lindos-settings`), see *Unrecognisable*. Their tray icon and window contents are upstream's |
| Any other entry with "Linux Mint" in Name/GenericName/Comment/Keywords | the text says "Lindos"; an entry whose `Exec` only opens linuxmint.com is hidden. `lindos-*` and `ubiquity*` entries are never touched (the installer hook owns those) |
| `lsb_release -d`, the MOTD header, other systems' boot menus (os-prober) | `/etc/lsb-release` `DISTRIB_DESCRIPTION` = "Lindos 1.0 (Aurora)" |
| `/etc/linuxmint/info` (read by Mint's tools) | `DESCRIPTION` and `GRUB_TITLE` say Lindos; `RELEASE`, `CODENAME`, `EDITION`, the URLs stay |
| Boot menu of the installed system | `/etc/default/grub.d/60-lindos-distributor.cfg`: `GRUB_DISTRIBUTOR="Lindos"` (entries read "Lindos GNU/Linux"; update-grub sources the drop-ins in name order after `/etc/default/grub`, so it has to sort after Mint's `50_linuxmint.cfg`) |
| `/etc/os-release` | as before plus `SUPPORT_URL`, `BUG_REPORT_URL`, `PRIVACY_POLICY_URL` of the project |
| Live session prompt (`mint@mint`) | `username=liveuser hostname=lindos` on the GRUB command lines and in `/etc/casper.conf` (also `FLAVOUR`) — `casper.conf` is copied into the initrd, so it must change before `40-theme.sh` rebuilds it (the postinst does) |
| Firefox: Linux Mint start page, welcome page, bookmarks, search engine | the homepage prefs, `policies.json` and `distribution.ini` in `/usr/lib/firefox*` and `/etc/firefox` are pointed at `about:home` / cleaned where they name linuxmint.com; anything else is only listed by the audit |
| Desktop background picker | the Mint wallpapers go with the artwork stack in `76-mint-purge.sh`; `77-mint-sweep.sh` still purges `mint-backgrounds-*` when that group was skipped and apt says nothing else would go with them |
| Settings → About | "Based on: Ubuntu 24.04 LTS"; the Debian/Mint provenance is in the *Legal and open-source notices* screen (`/usr/share/lindos/legal/open-source-notices.txt`); the hero line no longer calls Lindos a Mint remaster |

**Kept on purpose (identity — decided, not forgotten).** `ID=linuxmint`, `ID_LIKE`, `VERSION_CODENAME`,
`UBUNTU_CODENAME` in `/etc/os-release`; `DISTRIB_ID=LinuxMint`, `DISTRIB_RELEASE`, `DISTRIB_CODENAME` in
`/etc/lsb-release`; `RELEASE`, `CODENAME`, `EDITION` in `/etc/linuxmint/info`; apt sources; package and
executable names. Mint's own tools (mintupdate, mintsources, mintupgrade), apt templating and Ubiquity's
"replace / reuse an existing installation" detection read these, so changing them would break updates or the
installer for no visible gain. Known side effect: the installer names the UEFI boot entry and the ESP folder
after `DISTRIB_ID`, so firmware boot menus still say `linuxmint`; `fastfetch`-style tools pick Mint's ASCII logo
from `ID`.

**How it stays applied.** `/etc/apt/apt.conf.d/99lindos-branding` (`DPkg::Post-Invoke`) runs
`apply-branding.sh --quiet --files-only` after every apt run, so an upgrade of `base-files` or a Mint package
cannot bring the old text back. `--files-only` skips the Plymouth and wallpaper alternatives: a user's own choice
there must survive apt runs. Entries the sweep changed carry `X-Lindos-Rebranded=true`; the untouched original
is saved once in `/var/lib/lindos/rebrand/orig/` and `rebrand-base.py --revert` (run by lindos-desktop's prerm on removal, while the files still exist)
puts it back.

**Why edit the base's files in place** (instead of `dpkg-divert` or an override earlier in `XDG_DATA_DIRS`):
the edits keep `Exec`, `TryExec` and every translation exactly as the base ships them, so nothing depends on
guessing a Mint tool's command line; there is no diversion book-keeping for packages Lindos does not depend
on; a package may not ship files under `/usr/local/share` (Debian policy) and a prepended `XDG_DATA_DIRS`
only works where the session environment is inherited; and the Post-Invoke re-run makes an upgrade
harmless. The price: `dpkg --verify` reports those files as modified, and between an upgrade and the end of the
apt run (seconds) the old entry exists.

**Checking a build.** `out/hooks/77-mint-sweep.log` ends with an `audit:` list — every place that still says
Linux Mint (also `python3 /usr/libexec/lindos/rebrand-base.py --audit` on a running system; `--dry-run` shows what
a sweep would change). Hermetic tests: `packages/lindos-desktop/tests/test_rebrand.py` (the sweep against fake
roots and through `apply-branding.sh`), `build/tests/test_mint_sweep_hook.py` (the hook against a fake root),
`tests/test_no_mint_leftovers.py` (fails on any unexplained "Mint" in shipped data). What only a real boot can
confirm: which of these entries the real Mint 22.2 image actually has (the file and `Exec` names come from
what is known of Mint's packages, not from a real image — the audit shows what really is there), that Mint
Welcome no longer appears, the Lindos Store / Update / Driver icons,
the GRUB titles after `update-grub`, `casper.conf` reaching the initrd, and Firefox's first-run page.

### Unrecognisable (Mint only as the base system)

Owner direction: Linux Mint provides the kernel and the base system (apt, casper, Ubiquity, the Mint-built
Firefox/Thunar/... packages); everything a user sees must be Lindos's own or thoroughly re-skinned, and an
installed system must stay updatable without a reinstall. This first layer is build-side and configuration only:
the Lindos Update, Store and Settings replacements are a later round, so Mint's update, driver, store and report
tools stay **installed** and are re-skinned (Lindos names and icons, see below) rather than removed — hiding them now
would leave the system without update information. Nothing here changes `ID` / `ID_LIKE` / `DISTRIB_ID` / codenames
(see *Kept on purpose* above).

**1. The precedence bug (P0).** On the Mint base `/etc/xdg/xdg-xfce`, `/etc/xdg/xdg-default` and
`/etc/xdg/xdg-default.desktop` are symlinks to `/usr/share/mint-artwork/xfce`, and Debian's
`60x11-common_xdg_path` puts `/etc/xdg/xdg-$DESKTOP_SESSION` in front of `XDG_CONFIG_DIRS`. xfconfd lets earlier
directories win per property, so Mint's defaults (theme, font, panel, Start menu favourites, desktop icons, notifyd
theme, shortcuts, Thunar actions) outranked every Lindos default in `/etc/xdg/xfce4`. (Inferred from the stock ISO's
file tree and xfconf's source; nothing was booted — check `echo $XDG_CONFIG_DIRS` and
`xfconf-query -c xsettings -p /Net/ThemeName` in a live session.) Fixed three ways:

* structurally, `76-mint-purge.sh` purges the artwork stack (`mint-artwork*` and its themes/icons/cursors/wallpapers);
* `lindos-desktop`'s preinst diverts the three symlinks (`dpkg-divert`, suffix `.lindos-orig`, undone by the postrm)
  and ships a real `/etc/xdg/xdg-xfce/README` (no `xfce4/` tree) in their place, so the defaults win even when a Mint
  package comes back;
* `/etc/X11/Xsession.d/61lindos-xdg-config-dirs` drops any `/etc/xdg/xdg-*` entry that still resolves into
  `mint-artwork` from `XDG_CONFIG_DIRS`, and 76 moves a leftover symlink aside itself (`MINT-XDG-FIXED`).

Expect a visible change on the first boot after this: the Lindos panel, Start menu, desktop icons, notification theme and
shortcuts that were written but shadowed now really apply — re-test them in QEMU. `xfce4-docklike-plugin` is still not
packaged for noble, so the taskbar's window list is xfce4-panel's own task list there (item 9 below).

**2. mint-adjust.** `mintsystem` runs `mint-adjust` at every boot and copies
`/usr/share/ubuntu-system-adjustments/firefox/distribution.ini` over Firefox's whenever they differ, which reverted the
Lindos edit. Two guards: `/usr/share/linuxmint/adjustments/99-lindos.preserve` (one destination per line:
`distribution.ini`, the system `mimeapps.list`) and the sweep also brands the *source* copy in
`/usr/share/ubuntu-system-adjustments` (`firefox.dirs` in `base-sweep.json`), so source and destination agree even if the
`.preserve` format guess is wrong. (`mint-adjust` also deletes the `oem` account and `/oem` when `/oem/done.flag` exists:
unchanged, `mintsystem` stays.)

**3. GRUB.** `/etc/default/grub.d/50_linuxmint.cfg` sets `GRUB_DISTRIBUTOR=Ubuntu` and sorts after the old
`49-lindos-distributor.cfg`, so the Lindos file never won. It is now `60-lindos-distributor.cfg` (the postinst removes
the old one; it takes effect at the next `update-grub`). The titles seen at boot are additionally rewritten by
`ubuntu-system-adjustments` from `GRUB_TITLE` in `/etc/linuxmint/info`, which the sweep sets — so neither
`ubuntu-system-adjustments` nor `mint-info-xfce` is purged. **Unverified:** upstream `grub-install` derives its default
bootloader-id (the ESP folder / UEFI entry name) from `GRUB_DISTRIBUTOR` when none is passed; the installer and the
signed-grub postinst pass their own, but a later grub-efi upgrade may create an `EFI/lindos` folder — check on real
UEFI + Secure Boot hardware before shipping.

**4. `76-mint-purge.sh`** (after `75-vm.sh`, before the sweep; never fails the build, only the host guard dies).

* *Keep-set*: `apt-mark manual` for the apt-trust and mintsystem chain (`linuxmint-keyring mint-info-xfce mintsystem
  mint-common mint-translations mint-mirrors aptkit ubuntu-system-adjustments aptitude ...`), the interim tools
  (`mintupdate mintinstall mintdrivers mintsources mintreport timeshift`), the XApp plumbing, Thunar and the panel
  parts, the installer (`casper ubiquity*`), the default apps, and everything `mint-meta-*` depends on (`lib.sh
  mark_meta_deps_manual`, also called first thing by `10-debloat.sh`: purging `hexchat` takes the metapackage along).
* *Groups*, each behind `apt-get -s purge`: the metapackages (`mint-meta-core`, `mint-meta-xfce`; not
  `mint-meta-codecs`, its codecs would be orphaned), the artwork stack, `mintwelcome captain`, `mintbackup`,
  `mintdesktop`, `mintstick`, `lightdm-settings`, `fingwit libpam-fingwit`, `mint-upgrade-info`, `mintchat
  webapp-manager`, `warpinator`, `sticky`, `hypnotix`, `neofetch`, the seven Xfce toy plugins Mint adds; and, only when
  the replacement is installed, `xed`→`mousepad`, `xviewer*`/`pix`→`ristretto`, `thingy`+`xreader*`→`evince`,
  `celluloid`→`vlc`.
* *Skip rule*: a group is purged only when the simulation names nothing outside the group. Otherwise it is skipped and
  the log says `MINT-PURGE-SKIPPED group '<name>': <why>` (would also remove …, replacement missing, no answer from apt);
  a failed purge says `MINT-PURGE-FAILED`, `dpkg --audit` problems `MINT-PURGE-DPKG-AUDIT`, and the last line is
  `MINT-PURGE-RESULT purged=N skipped=M failed=K`. The sweep hides everything that was skipped.
* *Deliberately kept* (and why): `mintupdate mintinstall mintdrivers mintsources mintreport` — no Lindos replacement yet;
  `mintlocale` — Ubiquity's language-pack step may call it (unverified); `mintsystem mint-common mint-info-xfce
  mint-translations ubuntu-system-adjustments linuxmint-keyring` — Mint's `firefox` pre-depends
  `ubuntu-system-adjustments`, which depends on `mintsystem`; the GRUB title comes from them; apt trusts the key;
  `libxapp1 gir1.2-xapp python3-xapp xdg-desktop-portal-xapp`, `libadwaita`, `packagekit`, `network-manager-gnome`,
  `blueman` (depends on `papirus-icon-theme`), `thunar`, `xfce4-session`, `gnome-calculator`, `file-roller`, Firefox and
  Thunderbird. Moving Firefox to Mozilla's repo (`ADD_MOZILLA_REPO=1`) is what would free the whole mintsystem chain.
* `lib.sh`'s `AUTOREMOVE_PROTECT_RE` no longer shields every `mint*`, `gnome-*` and `mate-*` package: it names the
  keepers above (and `gnome-keyring`, `gnome-calculator`, `mate-polkit`, ...), so the artwork and apps 76 removes are
  not protected by the autoremove sweep.

**5. Default apps (owner decision).** `20-base.sh` installs `mousepad` (text), `ristretto` (images), `evince` (PDF) and
`vlc` (audio/video) — best effort, so the build stays green if one is missing (the Mint counterpart then stays).
`lindos-desktop` ships `/usr/share/lindos/mimeapps-desktop.list` and its postinst merges it into
`/etc/xdg/mimeapps.list` (`/usr/libexec/lindos/merge-mimeapps.py`: adds keys, never replaces a default somebody else
set, unions *Added Associations*). That file outranks the base's `/usr/share/applications/mimeapps.list`. The Everyday
Start favourites and taskbar pins never contained a Mint app, so they are unchanged. Cost: `vlc` pulls a Qt stack —
expect the ISO to grow (docs/RAM-BUDGET.md has not been re-estimated).

**6. Interim hiding and re-skinning** (`base-sweep.json`, applied by `rebrand-base.py` after every apt run, revertible):

* menu entries are hidden by name or glob: Mint's extras (`mintbackup`, `mintstick`, `mintdesktop`, `warpinator`, Notes,
  Hypnotix, Library, Web Apps, ...), the apps Lindos replaces (`xed`, `xviewer`, `xreader`, `pix`, Celluloid), the Xfce/Thunar
  duplicates (`thunar*`, `xfce4-terminal*`, the *Settings Manager* and every `xfce*-settings` entry, `xfwm4-*`, Application
  Finder, ...) and anything whose command is `mintchat`, `webapp-manager` or `mint-meta-codecs`. `NoDisplay` only: the
  commands still work from Lindos Settings, shortcuts and file associations. Task Manager and Screenshot stay.
* the update, driver, sources, report and language tools are renamed (*Lindos Updates*, *Lindos Drivers*, *Lindos Update
  Sources*, *Lindos System Reports*, *Language and Region*) and get Lindos icons; any other `Icon=mint...` becomes
  `lindos-settings`. The window a running tool opens uses its own icon name: the asset installer
  (`fetch-assets.sh`) makes the Lindos icon theme answer to `mintupdate`, `mintdrivers`, `mintinstall`, ... with Lindos artwork.
  Their tray icons and window contents are still upstream's until the Lindos replacements ship.
* the file-sharing and notes daemons (`warpinator`, `sticky`) and Mint Welcome do not autostart; the Update Manager tray
  stays (it tells users about updates).
* theme packs: `index.theme` of `Mint-*`, `Yaru*`, `Papirus*`, `Humanity*`, `ubuntu-mono-*`, `Bibata*`, `GoogleDot*`,
  `XCursor-Pro*`, `DMZ-*` gets `Hidden=true` (kept as fallbacks; `blueman` depends on Papirus, `adwaita-icon-theme` on
  `ubuntu-mono`). That Xfce's Appearance and Mouse dialogs honour it is remembered, not verified; GTK theme directories cannot
  be hidden.
* the cursor themes are `Lindos-Cursors` and `Lindos-Cursors-Dark`; `Fluent-cursors` / `Fluent-dark-cursors` remain as hidden
  themes that only inherit them (user configs written by older releases still name those; `lindos.theme` and Lindos Settings
  write and show the Lindos names and map a stored Fluent name to the theme it aliases), the `index.theme` names of the Lindos
  GTK and icon themes are the Lindos names, and the xfwm4 fallback to Mint's theme is gone.

**7. Smaller pieces.** `/usr/local/bin/apt` (Mint's wrapper, "This is the Linux Mint apt command"), `search`,
`highlight-mint`, `/usr/bin/rtfm` and the `apt-linux-mint` completion are diverted by the preinst when present; the Matrix
web app in `/etc/skel` goes with `mintchat` (and 76 deletes the entry if the purge was skipped); Settings › About shows
*Based on: Ubuntu 24.04 LTS* and has a **Legal and open-source notices** button whose text is
`/usr/share/lindos/legal/open-source-notices.txt` (what Lindos is built on, licences, trademarks; `/usr/share/doc/*/copyright`
stays intact); the accent `Mint Green` is `Meadow Green`.

**8. `81-unrecognisable-gate.sh`** (last hook, read-only) reports what still makes the image recognisable in
`out/hooks/81-unrecognisable-gate.log`: `UNRECOGNISABLE-FINDING [category] ...` lines (packages that should be gone, visible
menu/autostart entries that show Mint or start a denied app, theme packs not hidden, `/etc/xdg/xdg-*` into `mint-artwork`,
Mint's wrappers, "Linux Mint" in `/etc`, `/usr/local`, the menu and `/usr/share/lindos` outside an allow-list, the GRUB
drop-in order, a missing `.preserve`, the skel web app), `UNRECOGNISABLE-NOTE` lines for what is kept on purpose or hidden, and
`UNRECOGNISABLE-GATE findings=N notes=M mode=...`. It is report-only; `LINDOS_STRICT_UNRECOGNISABLE=1` makes any finding fail the
build. The hooks run under `env -i`, so the variable reaches the gate only when it is listed in `LINDOS_PASSTHRU_VARS`
(`build/config.env`). The deny lists live at the top of the hook and are kept in step with 76 by a test.

**9. The taskbar without Docklike, and the BIOS boot path.**

* *Taskbar.* Every panel layout puts Docklike (open windows + pinned apps) in slot `plugin-2`. Ubuntu 24.04 does not package
  it (25.10+ and Debian trixie+ do; upstream's current release also needs a newer `libxfce4windowing` than noble has; the first
  CI install's dpkg list shows `xfce4-panel` 4.18.4 and `xfce4-panel-profiles` 1.0.14 but no Docklike), and once
  the precedence bug above was fixed the Lindos panel — which has no other window list — became the effective one, so a
  fresh session would have had no window buttons and no pinned apps. `/usr/libexec/lindos/taskbar-fallback.py` (pure stdlib)
  rewrites a layout for that case: the Docklike slot becomes xfce4-panel's own `tasklist` (icon-only, flat buttons), the
  pinned apps of the mode's `docklike-2.rc` that are installed become one `launcher` plugin each (ids 11 and up, desktop-ids,
  placed before the task list), and the expanding separators go (the task list expands by itself, so this taskbar is
  left-aligned and Settings says so). `first-login-panel.sh` seeds that layout for a user who has no panel layout yet —
  everyday mode too — and records `taskbar=tasklist` in `~/.config/lindos/panel-init.done`; a stamp written before this
  existed is redone once, but a user's existing `xfce4-panel.xml` is never touched. `build-panel-profiles.sh` packs the
  per-mode `panel.tar.bz2` from the same variant (kind recorded in `<mode>/.panel-taskbar`, rebuilt when it changes), so
  `lindos-mode set` loads a layout with a window list. When the plugin does get packaged (`docklike.desktop` under
  `/usr/share/xfce4/panel/plugins`) nothing needs changing: `taskbar-fallback.py status` says `docklike` and the shipped
  layouts are used as they are. `LINDOS_TASKBAR=docklike|tasklist` overrides the detection (a launcher for an app that is
  not installed would show as a blank button, hence the filter). Known gaps, both for `lindos.modes` (lindos-core) to close by
  running `taskbar-fallback.py convert` when a mode is applied: a packed profile lists only the pins whose apps are installed
  when it is built (ISO build), so Steam and the other apps the installer adds later are missing from a Gaming layout loaded by
  `lindos-mode set` until `build-panel-profiles.sh --force` runs; and `lindos.modes`' own XML fallback (no
  `xfce4-panel-profiles`) still copies the Docklike layout.
* *BIOS boot menu.* `build-iso.sh` (`lindos_bios_boot_art`) removes `/.disk/mint_iso` ("unique to Mint ISO images") and the
  unused `boot/grub/{theme.cfg,live-theme}`, draws `isolinux/splash.png` with `build/lib/boot_splash.py` from the Lindos logo
  SVG (640x480, dark, the mark where the base's ring logo was; pure stdlib, deterministic, no third-party artwork; a picture
  the owner puts at `build/overlay/isolinux/splash.png` is used instead), titles
  the menu `Lindos <version> (<codename>)` and gives the selection bar the Lindos accent (black on `#60CDFF`). The build dies
  if the base's splash or the marker is still there. Not verified: how ISOLINUX/vesamenu draws it (needs a BIOS boot).
* *Installed system.* From the first CI install (`installed-logs/`): `/etc/os-release` said `NAME="Lindos"`, `PRETTY_NAME`
  Lindos, but `VERSION="22.2 (Zara)"` — now `VERSION="1.0 (Aurora)"` (`VERSION_ID`, `ID`, `VERSION_CODENAME` stay, see above);
  the GRUB entries were titled `Lindos 1.0` with `--class linuxmint` (the base's boot-time rewrite of `Ubuntu`; the class is
  invisible in the text menu); `lightdm.conf` was the empty `[Seat:*]` oem-config leaves, with the Lindos greeter settings in
  `lightdm.conf.d`. With the `60-` drop-in winning, the next `update-grub` should title the entries `Lindos GNU/Linux` with
  `--class lindos` — read `/boot/grub/grub.cfg` after an install to confirm.

**Checking a build.** `out/hooks/76-mint-purge.log` (grep `MINT-`), `out/hooks/81-unrecognisable-gate.log`, then a boot:
`echo $XDG_CONFIG_DIRS`, `xfconf-query -c xsettings -p /Net/ThemeName` (Lindos-Dark), `/Gtk/FontName` (Selawik),
`/Gtk/CursorThemeName`, `dpkg -l | grep -i mint`, `apt` (plain apt), no Update Manager wording apart from *Lindos Updates*,
`cat /usr/lib/firefox/distribution/distribution.ini` after a reboot, the Appearance/Mouse pickers, `lsb_release -a`.
Hermetic tests: `build/tests/test_mint_purge_hook.py` (fake apt/dpkg), `build/tests/test_unrecognisable_gate_hook.py`,
`build/tests/test_fetch_assets_install.py`, `packages/lindos-desktop/tests/test_unrecognisable.py` and
`tests/test_no_mint_leftovers.py`. **Not verified (needs a real image):** every effect above on a booted system, the
`.preserve` file format, that Ubiquity does not need any purged package (only `mintlocale` was flagged and is kept), the
real `apt-get -s purge` output on the Mint image (the parser expects `Purg name [version]` lines), the desktop-file names
of Mint's apps (the rules use globs; the gate lists what is left), Xcursor following `Inherits`, and the size change from `vlc`.
Also unverified (item 9): that xfce4-panel 4.18 shows the generated task list and launchers as intended (property names and
desktop-id items were read from its source, not run), the resulting GRUB titles, and the BIOS splash on screen.
`build/tests/test_bios_boot_art.py` and `packages/lindos-desktop/tests/test_taskbar_fallback.py` cover the rest hermetically.

## 6. `build/mkdeb.sh`

```
build/mkdeb.sh [--out DIR] [--lintian] [--keep] [name…|all]
```

Per SPEC §1.1: stages `root/` + `DEBIAN/`, fixes permissions (dirs 755, files 644; 755 for
`usr/bin/*`, `usr/sbin/*`, `usr/games/*`, `usr/libexec/**`, `*.sh`, `DEBIAN/{preinst,postinst,
prerm,postrm,config}`), strips CRLF defensively from text files, validates `DEBIAN/control`
(Package, Version, Architecture, Maintainer, Description), `sh -n` / `bash -n` on maintainer
scripts, generates `DEBIAN/md5sums`, `dpkg-deb --build --root-owner-group`, prints `dpkg-deb -I`.
Output: `out/debs/<Package>_<Version>_<Arch>.deb`. Directories without `DEBIAN/control` are
skipped with a warning. Exit 0 ok · 1 build error · 2 usage.

## 7. `build/fetch-assets.sh`

```
build/fetch-assets.sh [--offline] [--clean] [--out DIR] [--no-fonts] [--no-themes] [--verify-only]
                      (--out DIR = --assets-dir DIR; default out/assets; ASSETS_DIR env honoured)
```

Clones `vinceliuice/Fluent-gtk-theme` and `vinceliuice/Fluent-icon-theme` at pinned refs
(`FLUENT_GTK_REF` / `FLUENT_ICON_REF`, default `master`; the resolved commit is recorded in
`out/assets/assets.lock` and can be replayed with `LINDOS_ASSETS_LOCK=<file>`), downloads Selawik
1.01 and Inter 4.1 (`SELAWIK_URL/SHA256`, `INTER_URL/SHA256`), writes `SHA256SUMS`,
`MANIFEST.txt`, `THIRD_PARTY-assets.md` and the generated `install-into-chroot.sh` that
`40-theme.sh` runs inside the chroot. Requires `git`, `curl` or `wget`, `unzip`, `sha256sum`,
`tar`. Licences: [THIRD_PARTY.md](../THIRD_PARTY.md).

## 8. Docker

```
build/docker-build.sh [--no-build] [--image NAME] [--shell] [-- build-iso.sh options…]
```

Builds `lindos-builder:latest` from `build/Dockerfile` (unless `--no-build`), bind-mounts the
repository at `/lindos` and runs `build/build-iso.sh` privileged; `out/` lands in your checkout.
Every `config.env` variable present in your environment is forwarded
(`INCLUDE_STEAM=0 build/docker-build.sh -- --skip-download`). `--shell` opens a root shell in the
container. The script never calls sudo (Docker itself may need it on your host).

## 9. Testing the ISO in QEMU

```
build/test-qemu.sh [ISO] [--uefi] [--headless] [--disk [GB]] [--boot-disk] [--ram MB] [--cpus N]
                   [--display gtk|sdl|spice-app|none] [--no-kvm] [--snapshot] [--dry-run]
```

Default ISO = newest `out/lindos-*.iso`; `--disk` attaches `out/qemu/lindos-test.qcow2` (created
on demand, `QEMU_DISK_GB`) for installation tests, `--boot-disk` boots from it afterwards;
`--uefi` copies `OVMF_VARS` to `out/qemu/OVMF_VARS.fd`. Check both `make qemu` (BIOS) and
`make qemu-uefi` after every change to the overlay or the ISO method. The full install → account wizard →
Lindos Setup check (online, offline, black-holed network, killed hook) is the procedure at the end of
"Installer flow" in §5; use `--dry-run` to see the QEMU command line, for example to add `-nic none`.

## 10. Building and verifying on GitHub Actions

The end-to-end Linux work this Windows development host cannot do — building `.deb`s in a real dpkg
environment, compiling the Lindos kernel, and remastering the ISO in a chroot — is done for you on a
**GitHub Actions Ubuntu 24.04 runner** (a clean, bare-metal-ish `noble` machine that matches the
Lindos base). The workflows live in `.github/workflows/`; **you** push the repository to GitHub to
trigger them. This repository provides the workflows only — it does **not** create the remote or push
for you (that is your action, with your account and your choice of remote).

```sh
git remote add origin <your GitHub repo URL>   # one time; Lindos does not do this for you
git push -u origin main                         # triggers lint + pytest + .deb build
```

`.github/workflows/ci.yml` jobs:

| Job | When | What it does |
|---|---|---|
| **lint-test** | every push / PR | `tests/run.sh` on Ubuntu 24.04: `bash -n` / `sh -n` / `py_compile`, ShellCheck, JSON/XML/`.desktop`, CRLF, `pytest`, and the generated-doc freshness check |
| **pytest-windows** | every push / PR | `pytest` on `windows-latest` with the `gi` stub — enforces SPEC §4's "importable on any OS" rule |
| **debs** | every push / PR | `build/mkdeb.sh --lintian all` → every `packages/<name>` including **`lindos-vm`** and **`lindos-winapps`** built and lintian-checked; `out/debs/*.deb` uploaded as the `lindos-debs` artifact |
| **kernel** | `workflow_dispatch` input `build_kernel`, and on tags | installs `bc bison flex libssl-dev libelf-dev dpkg-dev`, runs `build/kernel/build-kernel.sh --version <manifest> --jobs $(nproc)` (kernel source tree cached), uploads `out/kernel/linux-image-*.deb linux-headers-*.deb`. Optional and slow by design |
| **iso** | `workflow_dispatch` input `build_iso` | frees disk, then `build/docker-build.sh` (which runs `mkdeb.sh` + `fetch-assets.sh` + the chroot) in the privileged builder; optionally downloads the **kernel** artifact into `out/kernel/` first so `35-kernel.sh` installs the Lindos kernel — the ISO still builds if the kernel artifact is absent. Uploads `out/*.iso`, `*.sha256`, `build.log` |

The Windows and Ubuntu pytest jobs both run the new `packages/lindos-vm/tests` and
`packages/lindos-winapps/tests` alongside the rest, so the VM/WinApps logic (caps probe, domain-XML
**no-spoof** assertion, catalog/backend guards) is verified on every push. The ISO job is
`workflow_dispatch` because it needs roughly 15 GB and downloads the Mint base ISO; day-to-day pushes
only run lint + pytest + `.deb`.

To build and verify locally instead, use the `make` targets in §2 (or Docker in §8); the Actions
runner just does the same steps on a machine with a real Linux kernel, `/dev/kvm` and root.

## 11. Known limits

* The pipeline was authored on Windows and dry-run there; the end-to-end ISO build must be run
  on a Linux host or in Docker. If a checkout lost the executable bits, nothing breaks (every
  invocation goes through `bash`), but you may want `chmod +x build/*.sh build/chroot/*.sh`.
* `BASE_ISO_SHA256`, `SELAWIK_SHA256`, `INTER_SHA256`, `HEROIC_SHA256`, `NBFC_SHA256` are empty
  by default (observed hashes are printed/recorded) — fill them in for a release build.
* Only the classic single-squashfs Mint/Ubuntu layout is supported.
* **The installer flow has never run end to end.** Everything under "Installer flow" was written against
  the upstream source of Ubiquity/casper/oem-config and unit-tested with fake targets. The CI `boot-test`
  job boots `casper/vmlinuz` + `initrd` directly (no GRUB, no isolinux, `boot=casper ... lindos.ci_boot_test`),
  so it exercises the live session, never the `Install Lindos` entries, the hook or the first-boot wizard.
  The QEMU install test (`build/qa/install_test.py`) is the job that will; until it has run green on GitHub
  Actions (see `CI-LOGS.md`), the manual procedure in "Installer flow" is the only real test. The honest list: [INSTALLER.md](INSTALLER.md#known-limitations-and-what-is-unverified).
