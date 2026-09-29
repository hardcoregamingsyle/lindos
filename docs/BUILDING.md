# Building Lindos

Two products come out of this repository:

1. **twelve `.deb` packages** (`out/debs/lindos-*_1.0.0_all.deb`) — installable on any Linux
   Mint 22.x XFCE / Ubuntu 24.04 system (the nine packages `lindos-meta` Depends on, including
   `lindos-transfer` from Addendum W, plus `lindos-kernel`, and the opt-in `lindos-vm` /
   `lindos-winapps` from Addendum V);
2. **the ISO** (`out/lindos-1.0.0-xfce-64bit.iso`) — Linux Mint 22.2 XFCE remastered with those
   packages, the Lindos theme assets, the base tune, Wine and Steam pre-installed.

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
   `filesystem.manifest-remove`.
10. **overlay + branding** — `build/overlay/` copied verbatim (`boot/grub/grub.cfg`,
    `boot/grub/loopback.cfg`, `.disk/info`, `README.diskdefines`), then `@LINDOS_VERSION@`,
    `@LINDOS_VERSION_SHORT@`, `@LINDOS_CODENAME@`, `@MINT_VERSION@`, `@DISK_INFO@`,
    `@ISO_VOLID@` placeholders are filled from `config.env` and `@PRESEED@` from the base
    ISO's `preseed/*.seed` (Mint's `file=/cdrom/preseed/linuxmint.seed`, empty when absent; the
    OEM entry keeps `only-ubiquity`); the build dies on an unfilled placeholder or a
    kernel/initrd path in `grub.cfg` that is missing from the tree; every existing
    `isolinux/*.cfg`, `boot/grub/**/*.cfg` and `README.diskdefines` is sed-branded (Mint
    strings → Lindos) and `initrd`/`initrd.lz` names fixed; `md5sum.txt` regenerated.
11. **ISO** — method `mkisofs` (default): `xorriso -indev BASE -report_el_torito as_mkisofs`
    describes the base ISO's boot equipment (GRUB MBR, protective GPT, appended EFI partition)
    and those options are replayed verbatim with `xorriso -as mkisofs`, so BIOS + UEFI boot
    exactly like Mint's; the base ISO's `--modification-date` is kept (`KEEP_ISO_MODDATE=1`) so
    the EFI GRUB finds the medium by fs-uuid. Method `replay`: `xorriso -indev BASE -outdev OUT
    -boot_image any replay -update_r ISO_DIR /`. Each falls back to the other automatically.
    Volume id `LINDOS_1_0_0` (`ISO_VOLID`), publisher/app id from `config.env`.
12. **verify + sha256** — `out/<ISO_NAME>.sha256`; `--no-cleanup` keeps `out/work/`.

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
| `ISO_METHOD` | `mkisofs` | `mkisofs` \| `replay` (see step 11) |
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
| `LINDOS_DEB_ORDER` | `lindos-core lindos-desktop lindos-tune lindos-compat lindos-gaming lindos-transfer lindos-setup lindos-settings lindos-meta` | 30-lindos-debs.sh (`lindos-transfer` installs before `lindos-setup` so the OOBE's transfer page can call it, SPEC-WINDOWS §33) |
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
pkg_available svc_disable svc_mask svc_enable fetch online`).

| Hook | What it does |
|---|---|
| `00-repos.sh` | `dpkg --add-architecture i386`; WineHQ deb822 `.sources` + key (`/etc/apt/keyrings/winehq-archive.key`, `/etc/apt/sources.list.d/winehq-noble.sources`); Valve Steam repo (`/usr/share/keyrings/steam.gpg`, `/etc/apt/sources.list.d/lindos-steam.list` — same paths as `install-gaming.sh`); Flathub system remote; optional Kisak Mesa (`KISAK_MESA=1`, key from Launchpad API + keyserver); optional Mozilla repo (`ADD_MOZILLA_REPO=1`, pinned 1000 — off by default because Mint pins its own firefox); optional `APT_MIRROR` rewrite; `apt-get update` |
| `10-debloat.sh` | purge `DEBLOAT_PURGE` one package at a time (safety net refuses network/printing basics); `safe_autoremove` (lib.sh: simulates first, marks xfce/mint/lightdm/network-manager/linux-/grub… candidates manual, refuses when > 60 packages would go); disable `DEBLOAT_DISABLE_SERVICES` (bluetooth stays enabled — see the table above); hide `mintwelcome` / `mintreport` / `update-notifier` autostarts; mintwelcome stays installed (Mint's metapackages need it) but is never shown — lindos-tune and the Mint sweep hide it as well, the sweep also its menu entry |
| `20-base.sh` | required set in one transaction (unavailable packages skipped with a loud warning): `xfce4-docklike-plugin xfce4-panel-profiles xfce4-clipman-plugin xfce4-notifyd xfce4-whiskermenu-plugin xfce4-pulseaudio-plugin xfce4-power-manager xfce4-screenshooter xfce4-taskmanager xfce4-appfinder picom systemd-zram-generator earlyoom power-profiles-daemon lm-sensors fancontrol gamemode mangohud python3 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 gir1.2-glib-2.0 polkitd pkexec flatpak xdg-desktop-portal-gtk xdg-utils desktop-file-utils shared-mime-info fonts-noto-color-emoji winbind cabextract icoutils zenity librsvg2-bin curl wget gpg ca-certificates zstd` + `EXTRA_PACKAGES`; nice-to-have set best effort per package (`fonts-noto-core fonts-inter fonts-jetbrains-mono fonts-liberation libnotify-bin xdotool wmctrl x11-xserver-utils mesa-utils vulkan-tools libvulkan1 mesa-vulkan-drivers pciutils usbutils hdparm nvme-cli smartmontools inxi yad emote baobab gnome-disk-utility pavucontrol gufw timeshift mugshot plymouth-themes plymouth-label`); then `LAPTOP_ESSENTIALS` best effort (firmware, audio UCM configs, Bluetooth, driver metadata, fwupd, a print driver — see the config.env table above and docs/RAM-BUDGET.md); everything `--no-install-recommends` |
| `30-lindos-debs.sh` | one `apt-get install --no-install-recommends ./…deb` transaction in `LINDOS_DEB_ORDER` (fallback `dpkg -i` + `apt-get -f install`) — the heavy Recommends of lindos-compat/gaming/meta (Wine, Steam, Lutris, …) are *not* pulled onto the ISO; `INCLUDE_WINE` / `INCLUDE_STEAM` / 20-base.sh decide the optional stacks; optional mode packages; `lindos-tune status --json` smoke test |
| `40-theme.sh` | `/tmp/lindos/assets/install-into-chroot.sh` (Fluent → `Lindos-Dark`/`Lindos-Light`, icons `Lindos`, cursors, fonts); `/usr/libexec/lindos/apply-branding.sh` (os-release sed keeping `ID=linuxmint`, `/etc/issue`, `/etc/lindos-release`, plymouth + wallpaper alternatives, and the Mint sweep — see below); `/usr/libexec/lindos/build-panel-profiles.sh` (`panel.tar.bz2` per mode); `fc-cache`, icon caches, glib schemas, desktop/mime databases; `plymouth-set-default-theme lindos && update-initramfs -u -k all` |
| `50-tune.sh` | `lindos-tune apply --mode ${TUNE_MODE} --system --offline` (presets, sysctl, zram, journald, tmpfiles, earlyoom); honest minimum (preset + fstrim.timer) with a loud warning if lindos-tune is missing |
| `60-compat.sh` | `INCLUDE_WINE=1` → `/usr/libexec/lindos/install-compat.sh --minimal --from-chroot --no-update` (Ubuntu `wine` + `wine32:i386`, winetricks, cabextract, 32-bit GL/Vulkan; WineHQ *staging* and umu-launcher are installed later at OOBE to keep the ISO small); always: MIME/desktop database refresh + `lindos-compat doctor` report |
| `70-gaming.sh` | always: `mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 libvulkan1:i386 steam-devices vulkan-tools mesa-utils` (+ `mangohud:i386` best effort); `INCLUDE_STEAM=1` → `install-gaming.sh --from-chroot ${GAMING_ITEMS}`; `INCLUDE_FLATPAK_LAUNCHERS=1` → Flatpaks. NVIDIA drivers are **not** preinstalled (mintdrivers / `lindos-drivers` at first boot) |
| `77-mint-sweep.sh` | last pass over what still says "Linux Mint" — see *Mint sweep* below: re-runs `apply-branding.sh --files-only` over the final image, checks Mint Welcome cannot autostart, purges `mint-backgrounds-*` only when `apt-get -s purge` shows nothing else would go with them, prints an audit of everything left; every step guarded, idempotent, never fails the build |
| `78-installer-brand.sh` | rebrands the live installer (Ubiquity): product name, launcher, artwork, slideshow, GTK skin — see *Installer branding* below; every step guarded (a missing file is a warning), idempotent |
| `80-cleanup.sh` | `apt-get autoremove --purge`, `apt-get clean`, drop apt lists, machine-id reset, resolv.conf restore, logs truncated, root history/caches, `/tmp` `/var/tmp` emptied, crash reports/journal removed |

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
| `/usr/share/ubiquity-slideshow/slides/` (shown while files are copied; `slideshow.conf` keeps the window size) | replaced by `build/installer/slideshow/`: six static slides, CSS-only, no script, no network — Windows-style look, the five Modes, Windows apps via Wine/Proton, gaming with the honest anti-cheat caveat, privacy, what first-boot Setup does |
| `/usr/share/ubiquity/pixmaps/{ubuntu_installed,cd_in_tray}.png`, `ubuntu/logo.png` | redrawn from `/usr/share/pixmaps/lindos-logo.svg` with `rsvg-convert` at the original pixel size (a size mismatch keeps the original) |
| `/usr/share/icons/hicolor/*/apps/{ubiquity,mintubiquity}.svg` | the Lindos logo |
| `/usr/share/themes/Lindos-Setup/` | `build/installer/themes/Lindos-Setup/gtk-3.0/gtk.css`: `Lindos-Dark` plus accent colours for Ubiquity's own `.ubiquity-next`, `.ubiquity-menubar` and progress bars — colours and buttons only, no geometry; only installed when `Lindos-Dark` is present |
| `/sbin/casper-stop` ("Please remove the installation medium, then press ENTER") | carries no product name; only a defensive text rewrite |

Deliberately **not** changed: partitioning behaviour, Ubiquity's Python code, compiled `.mo`
catalogues (a non-English installer can still say "Linux Mint" in a few translated strings),
`/cdrom/.disk/info` (written by `build-iso.sh`; Ubiquity's `get_release()` and casper read it),
the live user name and host (`liveuser` / `lindos` on the GRUB command lines — changed by the Mint sweep
below) and the installed system's `/etc/lsb-release` / GRUB title (also the sweep's). The hook ends with an audit that lists, in `out/hooks/78-installer-brand.log`,
every installer file that still mentions "Linux Mint". The Ubiquity files it edits are owned by the
`ubiquity*` packages, which the installer removes from the installed system; the few files the hook
adds (slideshow, `Lindos-Setup` theme) are tiny and stay behind as orphans.

Hermetic tests: `build/tests/test_installer_brand.py` runs the real hook under bash against a fake root.
Test seams (unset in a real build): `LINDOS_INSTALLER_ROOT` (prefix for every path),
`LINDOS_INSTALLER_SRC` (where `build/installer/` is), `LINDOS_RSVG` (rsvg-convert replacement). What
only a real boot can confirm: how the slideshow renders in Ubiquity's WebKit view, that the GTK skin
loads and looks right, the launcher on the live desktop, and the strings on every page — eyeball the
installer in QEMU (`make qemu`) after a build.

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
| Menu: **Update Manager**, **Driver Manager** | kept — Lindos has no replacement for the OS-update and driver GUIs (`lindos-update` only handles `lindos-*` packages, `lindos-drivers` wraps Driver Manager); they get Lindos icons (`lindos-update`, `lindos-drivers`). Their tray icon and window icon are upstream's |
| Any other entry with "Linux Mint" in Name/GenericName/Comment/Keywords | the text says "Lindos"; an entry whose `Exec` only opens linuxmint.com is hidden. `lindos-*` and `ubiquity*` entries are never touched (the installer hook owns those) |
| `lsb_release -d`, the MOTD header, other systems' boot menus (os-prober) | `/etc/lsb-release` `DISTRIB_DESCRIPTION` = "Lindos 1.0 (Aurora)" |
| `/etc/linuxmint/info` (read by Mint's tools) | `DESCRIPTION` and `GRUB_TITLE` say Lindos; `RELEASE`, `CODENAME`, `EDITION`, the URLs stay |
| Boot menu of the installed system | `/etc/default/grub.d/49-lindos-distributor.cfg`: `GRUB_DISTRIBUTOR="Lindos"` (entries read "Lindos GNU/Linux"; update-grub sources the drop-in after `/etc/default/grub`) |
| `/etc/os-release` | as before plus `SUPPORT_URL`, `BUG_REPORT_URL`, `PRIVACY_POLICY_URL` of the project |
| Live session prompt (`mint@mint`) | `username=liveuser hostname=lindos` on the GRUB command lines and in `/etc/casper.conf` (also `FLAVOUR`) — `casper.conf` is copied into the initrd, so it must change before `40-theme.sh` rebuilds it (the postinst does) |
| Firefox: Linux Mint start page, welcome page, bookmarks, search engine | the homepage prefs, `policies.json` and `distribution.ini` in `/usr/lib/firefox*` and `/etc/firefox` are pointed at `about:home` / cleaned where they name linuxmint.com; anything else is only listed by the audit |
| Desktop background picker | `mint-backgrounds-*` are purged when `apt-get -s purge` shows nothing but them would be removed; otherwise they stay and the hook says which packages would have gone too |
| Settings → About | "Based on: Ubuntu 24.04 LTS (noble) · Linux Mint 22.2" — provenance is kept on purpose; the hero line no longer calls Lindos a Mint remaster |

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
`make qemu-uefi` after every change to the overlay or the ISO method.

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
