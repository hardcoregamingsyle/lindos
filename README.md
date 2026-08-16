# Lindos 1.0 "Aurora"

**A Linux Mint XFCE remaster with a Windows-11-style desktop, an integrated Wine/Proton layer for
Windows programs, pre-wired gaming launchers and drivers, and an idle-RAM target of 350–500 MB.**

Lindos is *not* Windows and does not pretend to be. It is Linux Mint 22.x XFCE (Ubuntu 24.04
"noble") with a familiar look, a first-boot wizard that asks the two questions Windows users
actually care about (which **Mode** and which **browser**), and a set of small, honest tools that
make Wine, Proton, Steam, Roblox (Sober), Minecraft and drivers work out of the box.

> The engineering contract for everything in this repository is [`SPEC.md`](SPEC.md). If a
> document and the code disagree, the code is wrong or this document is wrong — never the SPEC.

## What you get

| Area | What Lindos ships |
|---|---|
| Desktop | XFCE 4.18 with a bottom 48 px translucent taskbar (`xfce4-panel` + Whisker menu + `xfce4-docklike-plugin`), `Lindos-Dark` / `Lindos-Light` GTK + xfwm4 themes (built from Fluent), `Lindos` icon theme, Fluent cursors, Selawik UI font, picom compositor with 8 px rounded corners, Windows-style keyboard shortcuts (`Super`, `Super+E`, `Super+I`, `Super+X`, `Super+L`, …). See [docs/KEYBOARD-SHORTCUTS.md](docs/KEYBOARD-SHORTCUTS.md). |
| First boot | `lindos-setup`, a Windows-style OOBE: Mode → browser → personalisation → apps → privacy → summary → apply. Works offline (Firefox fallback, installs deferred). |
| Modes | **Everyday**, **Gaming**, **Work**, **Creator**, **Lite** — switchable any time with `lindos-mode set <id>` or in Lindos Settings. Each mode = taskbar pins, packages, services, sysctl, CPU governor, zram size, compositor. See [docs/MODES.md](docs/MODES.md). |
| Settings | `lindos-settings`, a Windows-11-style settings centre (12 pages, Win+X power menu). Native pages for Home, Personalization, Windows apps, Gaming, Hardware, Mode and About; the rest delegate to the existing Mint/XFCE tools to keep RAM low. See [docs/SETTINGS.md](docs/SETTINGS.md). |
| Windows programs | Double-click any `.exe`/`.msi`/`.bat`/`.lnk` → `lindos-run` picks Wine or Proton-GE (umu-launcher), creates a per-program `C:\` drive, installs, scans for new programs and puts them in the Start menu. Recipes for well-known programs (`lindos-compat recipes`), `lindos-compat doctor`. See [docs/WINDOWS-APPS.md](docs/WINDOWS-APPS.md). |
| Gaming | Steam (Valve repo) and Lutris on the ISO; Heroic, Prism Launcher (Minecraft), Sober (Roblox), Bottles installable in one click; GE-Proton manager (`lindos-proton`), driver installer (`lindos-drivers`), Feral GameMode + MangoHud pre-configured, gamescope wrapper, per-title profiles, controller udev rules, optional xpadneo/xone. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md). |
| Kernel (optional) | `lindos-kernel`: a tuned kernel recipe with **ntsync** (Wine/Proton sync), **sched_ext** low-latency schedulers, 1000 Hz + full preempt, MGLRU and BBR, plus a runtime CLI to build, select and set its cmdline. Built on a Linux host (not Windows); the stock kernel stays as a fallback. See [docs/KERNEL.md](docs/KERNEL.md). |
| Windows VM (optional) | `lindos-vm`: an **honest** KVM/QEMU + libvirt Windows VM (q35, OVMF/UEFI, virtio) with **optional GPU passthrough** — for the Windows software Wine/Proton cannot run. It ships **no** spoofing/hypervisor-hiding and does **not** defeat VM-blocking anti-cheat; you supply a licensed Windows. See [docs/VM.md](docs/VM.md). |
| Windows apps over RDP (optional) | `lindos-winapps`: run individual Windows programs (Photoshop, Office) as **seamless windows** on the Lindos desktop, streamed over RDP from a Windows backend you supply. Lindos never enters your credentials or bypasses licensing. See [docs/WINAPPS.md](docs/WINAPPS.md). |
| RAM / performance | `lindos-tune`: zram (zstd), earlyoom, systemd presets that disable what a desktop does not need, journald cap, `/tmp` on tmpfs, sysctl base tune, per-mode governor / zram / earlyoom overrides, fan profiles (nbfc-linux / thinkpad_acpi), power profiles. `lindos-tune status` measures idle RAM against the target. See [docs/RAM-BUDGET.md](docs/RAM-BUDGET.md) and [docs/HARDWARE-CONTROL.md](docs/HARDWARE-CONTROL.md). |
| Browser | Firefox is on the ISO. Microsoft Edge and Google Chrome are **downloaded during setup** from Microsoft's / Google's official apt repositories (their licences forbid shipping them on the ISO). |

## Reality check — please read before you flash the ISO (SPEC §0.1)

* **"Runs Windows programs" means through Wine / Proton** — a translation layer with near-native
  speed, without a virtual machine. It is *not* Windows. Most desktop software and most
  single-player games work; software that installs Windows kernel drivers does not.
* **Anti-cheat decides multiplayer.** Valorant (Riot Vanguard), Fortnite (Epic disabled EAC's
  Linux support), League of Legends (Vanguard), Apex Legends (EAC Linux disabled Nov 2024),
  Rainbow Six Siege, Destiny 2, PUBG, Rust, Call of Duty and GTA Online **do not run on any Linux
  distribution, including Lindos.** Nothing on the Lindos side can change that; only the
  publisher can. Games work when the publisher *enabled* EAC/BattlEye for Proton (Halo Infinite,
  Marvel Rivals, Elden Ring, …). Steam titles depend on that developer choice —
  check [ProtonDB](https://www.protondb.com/) and
  [Are We Anti-Cheat Yet?](https://areweanticheatyet.com/). The shipped list is
  [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) (56 titles, generated from the same data the
  Settings app shows); the full honest explanation — and why **Lindos ships no anti-cheat
  spoofer** (it cannot work and only gets you hardware-banned) — is
  [docs/ANTI-CHEAT.md](docs/ANTI-CHEAT.md).
* **Roblox works through Sober** (a community runtime for the official Android build), not the
  Windows client, which blocks Wine. Roblox Studio runs through Vinegar. Minecraft Java is native;
  Bedrock only through the unofficial `mcpelauncher` (marked *partial*).
* **Adobe:** Photoshop CS6 works; Photoshop / Illustrator CC 2021 are *partial* (no Creative
  Cloud sign-in, no GPU features); newer releases, Premiere and AutoCAD are *broken* — the
  recipes say so and name native alternatives (Kdenlive, DaVinci Resolve, FreeCAD, GIMP, Krita).
  Microsoft 365 Click-to-Run does not install; Office 2016 (32-bit offline installer) works. When
  Wine is not enough, `lindos-vm` runs a **real** Windows VM and `lindos-winapps` shows individual
  Windows apps seamlessly over RDP — you supply a **licensed Windows**; Lindos never downloads
  Windows, enters your credentials or bypasses activation.
* **A VM is still a VM.** `lindos-vm` is a truthful, standard Windows VM with **no** hypervisor
  hiding or SMBIOS/UUID/ACPI/CPUID spoofing, so it does **not** defeat VM-blocking anti-cheat
  (Vanguard, etc.) — that is by design, and [docs/VM.md](docs/VM.md) says so plainly.
* **RAM:** the target is **350–500 MB** idle (Lite ≈ 300–380 MB), measured as `free -m` "used"
  after login into XFCE with no apps open. `lindos-tune status` prints exactly that verdict.
  Until the reference measurements are published, the per-measure savings in
  [docs/RAM-BUDGET.md](docs/RAM-BUDGET.md) are estimates and are marked as such.
* **Edge / Chrome** are never on the ISO; the OOBE installs them from the vendors' repositories
  when online, otherwise falls back to Firefox and tells you how to install them later
  (Settings › Apps › Web browsers, or `lindos-browser install edge --set-default`).
* No telemetry, no ads, no crash uploads (apport/whoopsie/kerneloops are disabled).

## Quickstart

### Use it

1. Build or download `lindos-1.0.0-xfce-64bit.iso`, write it to a USB stick (Rufus / Etcher /
   `dd`), boot, install with the Mint installer as usual.
2. On first login the **Lindos Setup** wizard appears. Pick a Mode and a browser, tick the apps
   you want (Windows app support is on by default), finish.
3. Everything the wizard did can be changed later in **Lindos Settings** (`Super+I`) or from
   the terminal:

```sh
lindos-mode list                    # the five modes
lindos-mode set gaming              # switch mode (asks for admin rights once)
lindos-browser install chrome --set-default
lindos-tune status                  # idle RAM vs. the 350–500 MB target
lindos-run ~/Downloads/setup.exe    # run / install a Windows program
lindos-compat doctor                # is Wine/Proton/Vulkan/32-bit ready?
lindos-game install steam sober prism
lindos-proton update                # latest GE-Proton for Steam + umu
lindos-drivers status               # GPU driver status; 'install --nvidia-open' etc.
```

### Build it

Building runs on Ubuntu/Debian (root needed for the chroot) or anywhere with Docker.
Full details, every flag and every `config.env` knob: [docs/BUILDING.md](docs/BUILDING.md).

```sh
make debs          # build every packages/<name> into out/debs/            (no root)
make assets        # fetch pinned Fluent theme/icons + Selawik/Inter fonts (no root)
make iso           # download Mint 22.2 XFCE ISO, remaster, write out/lindos-1.0.0-xfce-64bit.iso (sudo)
make iso-docker    # same inside a privileged ubuntu:24.04 container
make qemu          # boot the newest out/lindos-*.iso (BIOS)      make qemu-uefi   (OVMF)
make test          # tests/run.sh: bash -n / sh -n / py_compile / JSON / XML / .desktop / CRLF / pytest / compat-doc
make lint          # syntax checks + shellcheck (if installed)
make clean         # remove build products (keeps out/cache and out/assets); make distclean removes out/
```

Useful overrides: `INCLUDE_WINE=1 INCLUDE_STEAM=1 INCLUDE_FLATPAK_LAUNCHERS=0 KISAK_MESA=0`,
`BASE_ISO=path/to/linuxmint-22.2-xfce-64bit.iso`, `ISO_ARGS="--skip-download --no-cleanup"`.

## Repository layout

```
SPEC.md              the contract (names, paths, formats, APIs)
README.md            this file          THIRD_PARTY.md  licences of fetched assets
LICENSE              GPL-3.0-or-later   CONTRIBUTING.md how to work on the repo
.github/workflows/   ci.yml: lint + pytest (Ubuntu 24.04 + Windows) + .deb build; kernel + ISO on workflow_dispatch
Makefile             debs · assets · iso · iso-docker · qemu · qemu-uefi · test · lint · clean · distclean
build/               ISO remaster + .deb pipeline: build-iso.sh, mkdeb.sh, fetch-assets.sh,
                     test-qemu.sh, docker-build.sh, Dockerfile, config.env, chroot/NN-*.sh hooks, overlay/
packages/            one directory per .deb (DEBIAN/ + root/ + tests/):
  lindos-core        python3 module `lindos` (paths, config, modes, browsers, hardware, helper,
                     theme, compat, ram) + polkit helper + CLIs lindos-mode/-browser/-config/-ram
  lindos-desktop     panel, xfconf defaults, shortcuts, picom, wallpapers, plymouth, greeter,
                     branding, lindos-compositor, per-mode panel profiles
  lindos-setup       first-boot OOBE (lindos-setup)
  lindos-settings    settings centre (lindos-settings, --power-menu)
  lindos-compat      lindos-run, lindos-compat, recipes, MIME/Thunar integration
  lindos-gaming      lindos-proton, lindos-drivers, lindos-game, lindos-mangohud, gamemode/MangoHud
                     configs, controller udev rules, compat-matrix.json
  lindos-tune        lindos-tune (status/apply/services/zram/governor/fan/power/sched/report), presets,
                     sysctl, earlyoom, journald, ananicy rules, sched_ext, ram-budget.json
  lindos-kernel      tuned kernel recipe + kconfig fragment + grub cmdline drop-in + lindos-kernel CLI
                     (compiled .deb is a build artifact, not committed); Recommended by lindos-meta
  lindos-meta        depends on all of the above (Recommends lindos-kernel)
  lindos-vm          honest KVM/VFIO Windows VM (no spoofing) + optional GPU passthrough; lindos-vm CLI
  lindos-winapps     seamless Windows apps (Adobe/Office) over RDP; lindos-winapps CLI
build/kernel/        build-kernel.sh (Linux host only) + config/ + patches/ — builds out/kernel/*.deb
docs/                BUILDING · ARCHITECTURE · MODES · SETTINGS · WINDOWS-APPS · GAMING · COMPATIBILITY
                     (generated) · KERNEL · VM · WINAPPS · DRIVERS · ANTI-CHEAT · RAM-BUDGET ·
                     HARDWARE-CONTROL · KEYBOARD-SHORTCUTS · FAQ
tests/               run.sh (lint everything), pytest config + gi stub, gen-compat-doc.py
out/                 build products (git-ignored)
```

## Documentation

| Document | Contents |
|---|---|
| [docs/BUILDING.md](docs/BUILDING.md) | host requirements, `make` targets, `build-iso.sh` flags, `config.env`, chroot hooks, Docker, QEMU |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | packages, the `lindos` Python module, the polkit helper and its actions, paths, config, cross-component call map |
| [docs/MODES.md](docs/MODES.md) | the five modes: what each one changes, `lindos-mode`, mode.json format |
| [docs/SETTINGS.md](docs/SETTINGS.md) | Lindos Settings pages, what is native and what delegates, the power menu |
| [docs/WINDOWS-APPS.md](docs/WINDOWS-APPS.md) | `lindos-run`, prefixes ("C:\ drives"), runners, recipes table, `lindos-compat` |
| [docs/GAMING.md](docs/GAMING.md) | launchers, `lindos-game`, `lindos-proton`, `lindos-drivers`, GameMode, MangoHud, gamescope, per-title profiles, controllers |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | **generated** game compatibility matrix (do not edit by hand) |
| [docs/ANTI-CHEAT.md](docs/ANTI-CHEAT.md) | why kernel-level anti-cheat cannot run on Linux, why Lindos ships no spoofer, and what does work |
| [docs/KERNEL.md](docs/KERNEL.md) | the tuned `lindos-kernel` (ntsync, sched_ext, 1000 Hz, MGLRU, BBR): build, install, cmdline, schedulers |
| [docs/VM.md](docs/VM.md) | `lindos-vm`: the honest KVM/VFIO Windows VM, host requirements, create/passthrough flow, why it ships no spoofing and does not defeat VM-blocking anti-cheat |
| [docs/WINAPPS.md](docs/WINAPPS.md) | `lindos-winapps`: seamless Adobe/Office over RDP, backends, the app catalog, and how Lindos never enters your credentials |
| [docs/DRIVERS.md](docs/DRIVERS.md) | `lindos-drivers autodetect` / `install --auto` for GPU + Broadcom Wi-Fi + audio, the first-boot service, non-free repos |
| [docs/RAM-BUDGET.md](docs/RAM-BUDGET.md) | debloat list, every tuning measure and its estimated saving, how to measure |
| [docs/HARDWARE-CONTROL.md](docs/HARDWARE-CONTROL.md) | CPU governor / EPP, power profiles, fans (nbfc), GPU drivers, zram, earlyoom, TRIM, refresh rate, RGB |
| [docs/KEYBOARD-SHORTCUTS.md](docs/KEYBOARD-SHORTCUTS.md) | every shortcut, generated from the shipped xfconf XML |
| [docs/FAQ.md](docs/FAQ.md) | the questions Windows users ask first, answered honestly |

## Licence

Lindos code, packaging, artwork (wallpapers, logo, icons) and documentation are
**GPL-3.0-or-later**. Third-party assets fetched at build time (Fluent theme and icons, Selawik
and Inter fonts) keep their own licences — see [THIRD_PARTY.md](THIRD_PARTY.md). Lindos is not
affiliated with Microsoft, Google, Valve, Roblox Corporation, Mojang, Adobe, Linux Mint or
Canonical; product names belong to their owners.
