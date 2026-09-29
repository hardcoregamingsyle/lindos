# Lindos 1.0 "Aurora"

**A Linux Mint XFCE remaster with a Windows-11-style desktop, an integrated Wine/Proton layer for
Windows programs, pre-wired gaming launchers and drivers, and an idle-RAM target of 350–500 MB.**

Lindos is *not* Windows and does not pretend to be. It is Linux Mint 22.x XFCE (Ubuntu 24.04
"noble") with a familiar look, an installer that does the heavy lifting once (updates, drivers,
Chrome, Wine and the game launchers), a first-boot setup that asks for your account and then the
two questions Windows users actually care about (which **Mode** and which **browser**), and a set
of small, honest tools that make Wine, Proton, Steam, Roblox (Sober), Minecraft and drivers work
out of the box.

> The engineering contract for everything in this repository is [`SPEC.md`](SPEC.md). If a
> document and the code disagree, the code is wrong or this document is wrong — never the SPEC.

## What you get

| Area | What Lindos ships |
|---|---|
| Desktop | XFCE 4.18 with a bottom 48 px translucent taskbar (`xfce4-panel` + Whisker menu + `xfce4-docklike-plugin`), `Lindos-Dark` / `Lindos-Light` GTK + xfwm4 themes (built from Fluent), `Lindos` icon theme, Fluent cursors, Selawik UI font, picom compositor with 8 px rounded corners, Windows-style keyboard shortcuts (`Super`, `Super+E`, `Super+I`, `Super+X`, `Super+L`, …). See [docs/KEYBOARD-SHORTCUTS.md](docs/KEYBOARD-SHORTCUTS.md). |
| Installing | Boot the USB and choose **Install Lindos**: the live session is the installer and nothing else (no wizard, no prompts, set up not to sleep). The installer does everything heavy while it installs — system updates, drivers and firmware, Google Chrome (downloaded from Google's own apt repository), the Wine/Proton layer and game launchers, the apps of every Mode and their Flatpaks — so it needs an internet connection for the best result and says plainly what it could not do when offline. See [docs/INSTALLER.md](docs/INSTALLER.md). |
| First boot | Remove the USB and reboot. The first boot only asks for who you are: Ubiquity's **account setup** (language, keyboard, time zone, your name, password and computer name). Then `lindos-setup`, a full-screen Windows 11-style out-of-box experience, appears once you are on the desktop: mode → browser → personalise → privacy → bring your stuff from Windows → summary → "Just a moment…" → all set. It only saves choices — it never installs or updates anything. |
| Modes | **Everyday**, **Gaming**, **Work**, **Creator**, **Lite** — switchable any time with `lindos-mode set <id>` or in Lindos Settings. Each mode = taskbar pins, packages, services, sysctl, CPU governor, zram size, compositor. See [docs/MODES.md](docs/MODES.md). |
| Settings | `lindos-settings`, a Windows-11-style settings centre (12 pages, Win+X power menu). Native pages for Home, Personalization, Windows apps, Gaming, Hardware, Mode and About; the rest delegate to the existing Mint/XFCE tools to keep RAM low. See [docs/SETTINGS.md](docs/SETTINGS.md). |
| Windows programs | Double-click any `.exe`/`.msi`/`.bat`/`.lnk` → `lindos-run` picks Wine or Proton-GE (umu-launcher), creates a per-program `C:\` drive, installs, scans for new programs and puts them in the Start menu. Recipes for well-known programs (`lindos-compat recipes`), `lindos-compat doctor`. See [docs/WINDOWS-APPS.md](docs/WINDOWS-APPS.md). |
| Every other Windows file type | `lindos-run` also opens MSIX/APPX packages and bundles (`.msix`/`.appx`/`.msixbundle`/`.appxbundle`/`.msixupload`, `.appinstaller`), `.msp` patches, `.reg` (with a preview of what it deletes), `.ps1`/`.vbs`, `.url`, `.scr`, `.cpl`, software `.inf`, `.cab`, disk images (`.iso`/`.img`, AutoPlay-style), ClickOnce, DOS programs (DOSBox-X) and 16-bit Windows programs — or explains plainly why a file can't run (ARM binaries, drivers, Store-encrypted/UWP packages). `./setup.exe` also works straight from a terminal via `binfmt_misc`. `lindos-compat winget install <id>` installs by package id, hash-verified against the publisher's own manifest over HTTPS, no override. See [docs/WINDOWS-FORMATS.md](docs/WINDOWS-FORMATS.md) and [docs/WINGET.md](docs/WINGET.md). |
| Bring your stuff from Windows | `lindos-transfer`: a Windows-Easy-Transfer-style migration from the Windows partition on the same PC (strictly read-only, never written to) or from a "transfer folder" made by a double-click kit run on the old PC — Desktop/Documents/Pictures/etc., browser bookmarks, wallpaper, fonts, Wi-Fi networks (via your own `netsh` export, opt-in, sent to the root helper on stdin only), Steam library, and a list of your Windows programs with an honest way to get each back (apt/Flatpak, a Wine recipe, `winget`, a web-app shortcut, or "does not run on Linux" with the game route). Never touches account/security databases, DPAPI, credential/cookie stores or hibernation/page/swap files. See [docs/TRANSFER.md](docs/TRANSFER.md). |
| Gaming | Steam (Valve repo) and Lutris on the ISO; Heroic, Prism Launcher (Minecraft), Sober (Roblox), Bottles installable in one click; GE-Proton manager (`lindos-proton`), driver installer (`lindos-drivers`), Feral GameMode + MangoHud pre-configured, gamescope wrapper, per-title profiles, controller udev rules, optional xpadneo/xone. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md). |
| Games that need Windows | For kernel-level anti-cheat that cannot run on any Linux (Valorant, League of Legends, Call of Duty, and more), `lindos-game route <title>` shows the honest way to actually play: an official cloud-streaming service where the publisher offers one (GeForce NOW, Xbox Cloud Gaming, Boosteroid, Amazon Luna), or a one-command, one-shot restart into the PC's own Windows installation (`lindos-dualboot` — a genuine UEFI/GRUB boot-once, never a VM trick and never a Windows/BCD/firmware edit). The Lindos VM is never offered for these titles. See [docs/DUALBOOT.md](docs/DUALBOOT.md) and [docs/ANTI-CHEAT.md](docs/ANTI-CHEAT.md). |
| Kernel (optional) | `lindos-kernel`: a tuned kernel recipe with **ntsync** (Wine/Proton sync), **sched_ext** low-latency schedulers, 1000 Hz + full preempt, MGLRU and BBR, plus a runtime CLI to build, select and set its cmdline. Built on a Linux host (not Windows); the stock kernel stays as a fallback. See [docs/KERNEL.md](docs/KERNEL.md). |
| Windows VM (optional) | `lindos-vm`: an **honest** KVM/QEMU + libvirt Windows VM (q35, OVMF/UEFI, virtio) with **optional GPU passthrough** — for the Windows software Wine/Proton cannot run. It ships **no** spoofing/hypervisor-hiding and does **not** defeat VM-blocking anti-cheat; you supply a licensed Windows. See [docs/VM.md](docs/VM.md). |
| Windows apps over RDP (optional) | `lindos-winapps`: run individual Windows programs (Photoshop, Office) as **seamless windows** on the Lindos desktop, streamed over RDP from a Windows backend you supply. Lindos never enters your credentials or bypasses licensing. See [docs/WINAPPS.md](docs/WINAPPS.md). |
| RAM / performance | `lindos-tune`: zram (zstd), earlyoom, systemd presets that disable what a desktop does not need, journald cap, `/tmp` on tmpfs, sysctl base tune, per-mode governor / zram / earlyoom overrides, fan profiles (nbfc-linux / thinkpad_acpi), power profiles. `lindos-tune status` measures idle RAM against the target. See [docs/RAM-BUDGET.md](docs/RAM-BUDGET.md) and [docs/HARDWARE-CONTROL.md](docs/HARDWARE-CONTROL.md). |
| Browser | Firefox is on the ISO. Google Chrome is the default and is **downloaded by the installer** from Google's official apt repository (its licence forbids shipping it on the ISO); Microsoft Edge is available from Lindos Settings › Apps. If the install was offline, Chrome is added silently in the background the first time the PC is online, or with **Install now** in Lindos Settings › Apps. |

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
* **Edge / Chrome** are never on the ISO; Chrome is the system default and is downloaded by the
  **installer** from Google's official apt repository while it installs (never in the live/ISO
  session, never at build time). An offline install leaves Chrome *pending*: Firefox is your browser
  until `lindos-browser-firstboot.service` adds Chrome silently the first time the installed system
  is online (no window, no wizard), or you press **Install now** in Lindos Settings › Apps ›
  Web browsers (`lindos-browser install chrome --set-default`). Edge is never installed
  automatically: `lindos-browser install edge --set-default`.
* **The installer downloads things, and says so.** Updates, drivers, Chrome, Wine/Proton, the game
  launchers and the Flatpak apps come from the internet while you install (a few GB the first
  time; the installer's status line is meant to name the current step). Offline, the install still
  completes; whatever could not be done is recorded as *pending* and offered in Settings › Apps.
  Proprietary GPU drivers are installed only with your consent and never when Secure Boot would
  need a key enrolment. Details: [docs/INSTALLER.md](docs/INSTALLER.md).
* **The new install flow is unproven on real hardware.** It was built and unit-tested without a
  Linux machine; it has not yet been run end to end (QEMU install or a real PC). The installer
  still shows a page for a *temporary* account (just press Continue: your real account is
  created on first boot), and the first-boot account screen looks like Ubiquity, not like Lindos
  Setup. The full list is in [docs/INSTALLER.md](docs/INSTALLER.md#known-limitations-and-what-is-unverified).
* No telemetry, no ads, no crash uploads (apport/whoopsie/kerneloops are disabled).
* **No kernel-level anti-cheat circumvention, ever.** Lindos ships nothing that emulates, forges
  or hides from Vanguard, Ricochet, EAC/BattlEye's kernel parts or any other kernel-mode
  anti-cheat: no fake driver, no attestation/TPM/Secure-Boot/HVCI forger, no HWID/SMBIOS/CPUID
  spoofing, no VM-hiding, no user-agent spoofing. None of that can produce the hardware-rooted
  signature the anti-cheat server checks — it only gets the account **hardware-banned**. The
  honest alternative is official cloud streaming or a one-shot restart into the PC's real Windows
  installation (`lindos-game route`, `lindos-dualboot`) — see
  [docs/ANTI-CHEAT.md](docs/ANTI-CHEAT.md) and [docs/DUALBOOT.md](docs/DUALBOOT.md).
* **Transfer is read-only and never touches secrets.** `lindos-transfer` mounts a Windows volume
  `ro` only, never writes to it, and refuses to open account/security hives, DPAPI/Credential
  Manager/Vault data, browser password/cookie stores, or hibernation/page/swap files beyond a
  4 KiB safety check. Wi-Fi passwords move only through your own Windows export, opt-in, over the
  root helper's stdin — never a command line, a log, or a network call.

## Quickstart

### Use it

1. Build or download `lindos-1.0.0-xfce-64bit.iso`, write it to a USB stick (Rufus / Etcher /
   `dd`) and boot it (the first menu entry is **Install Lindos**; **Try Lindos** gives a live
   desktop whose *Install Lindos* icon does the same thing). Connect to the internet first if you
   can: **Install Lindos does everything, including the downloads** — updates, drivers, Google
   Chrome, Wine/Proton, the game launchers and the apps of every Mode. Answer its questions
   (language, keyboard, disk; ignore the temporary-account page: just press Continue).
2. When it says so, remove the USB stick and reboot. The first boot shows only the **account
   setup** (your name, password, computer name, time zone).
3. On the desktop, **Lindos Setup** asks for a Mode, a browser and a few personal choices. It
   only saves settings; nothing is installed or updated there.
4. Everything Lindos Setup did — and anything the installer could not do offline — can be
   changed or finished later in **Lindos Settings** (`Super+I`, Apps shows "Left to finish from
   setup") or from the terminal:

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
lindos-run ~/Downloads/App.msix     # MSIX/APPX, .reg, .ps1, disk images and more (docs/WINDOWS-FORMATS.md)
lindos-compat winget install 7zip.7zip   # install by winget package id, hash-verified
lindos-transfer sources             # find a Windows partition or transfer-folder kit to migrate from
lindos-game route valorant          # honest routes for a title that needs Windows' anti-cheat
lindos-dualboot status              # can this PC one-shot restart into its existing Windows?
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
                     theme, compat, ram, dualboot, update) + polkit helper + CLIs
                     lindos-mode/-browser/-config/-ram/-dualboot/-update
  lindos-desktop     panel, xfconf defaults, shortcuts, picom, wallpapers, plymouth, greeter,
                     branding, lindos-compositor, per-mode panel profiles
  lindos-setup       first-boot OOBE (lindos-setup): mode, browser, personalise, privacy — config only
  lindos-settings    settings centre (lindos-settings, --power-menu)
  lindos-compat      lindos-run, lindos-compat, recipes, MIME/Thunar integration, the Windows
                     format engine (MSIX/APPX, winget, DOSBox, disk images, binfmt_misc, …)
  lindos-gaming      lindos-proton, lindos-drivers, lindos-game (incl. `route`/`play`/`cloud`),
                     lindos-mangohud, gamemode/MangoHud configs, controller udev rules,
                     compat-matrix.json (incl. cloud-provider/route data)
  lindos-tune        lindos-tune (status/apply/services/zram/governor/fan/power/sched/report), presets,
                     sysctl, earlyoom, journald, ananicy rules, sched_ext, ram-budget.json
  lindos-kernel      tuned kernel recipe + kconfig fragment + grub cmdline drop-in + lindos-kernel CLI
                     (compiled .deb is a build artifact, not committed); Recommended by lindos-meta
  lindos-transfer    lindos-transfer / lindos-transfer-gui: Windows Easy Transfer-style migration
                     (read-only), Windows-side kit under root/usr/share/lindos/transfer/windows/
  lindos-meta        depends on all of the above, incl. lindos-transfer (Recommends lindos-kernel)
  lindos-vm          honest KVM/VFIO Windows VM (no spoofing) + optional GPU passthrough; lindos-vm CLI
  lindos-winapps     seamless Windows apps (Adobe/Office) over RDP; lindos-winapps CLI
  lindos-installer   the install medium's installer flow: the Ubiquity target-config hook that does the
                     downloads/installs, and the success command that arms the first-boot account
                     setup. On the ISO only (not in lindos-meta, removed from the installed system)
build/kernel/        build-kernel.sh (Linux host only) + config/ + patches/ — builds out/kernel/*.deb
docs/                BUILDING · INSTALLER · ARCHITECTURE · MODES · SETTINGS · UPDATES · WINDOWS-APPS ·
                     WINDOWS-FORMATS · WINGET · TRANSFER · DUALBOOT · GAMING ·
                     COMPATIBILITY (generated) · KERNEL · VM · WINAPPS · DRIVERS · ANTI-CHEAT ·
                     RAM-BUDGET · HARDWARE-CONTROL · KEYBOARD-SHORTCUTS · FAQ
tests/               run.sh (lint everything), pytest config + gi stub, gen-compat-doc.py
out/                 build products (git-ignored)
```

## Documentation

| Document | Contents |
|---|---|
| [docs/BUILDING.md](docs/BUILDING.md) | host requirements, `make` targets, `build-iso.sh` flags, `config.env`, chroot hooks, the installer flow (hook contract, files, logs, testing it in QEMU), Docker, QEMU |
| [docs/INSTALLER.md](docs/INSTALLER.md) | how installing Lindos works for users: what the installer downloads, offline installs and pending items, the boot entries and kernel switches, logs, and the honest known-limitations / unverified list |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | packages, the `lindos` Python module, the polkit helper and its actions, paths, config, cross-component call map |
| [docs/MODES.md](docs/MODES.md) | the five modes: what each one changes, `lindos-mode`, mode.json format |
| [docs/SETTINGS.md](docs/SETTINGS.md) | Lindos Settings pages, what is native and what delegates, the power menu |
| [docs/UPDATES.md](docs/UPDATES.md) | `lindos-update`: the two update channels, checking vs. applying, sideloading `lindos-*.deb` files with no apt repo, self-hosting a real one |
| [docs/WINDOWS-APPS.md](docs/WINDOWS-APPS.md) | `lindos-run`, prefixes ("C:\ drives"), runners, recipes table, `lindos-compat` |
| [docs/WINDOWS-FORMATS.md](docs/WINDOWS-FORMATS.md) | every other Windows file type `lindos-run` opens: MSIX/APPX + bundles/uploads, `.appinstaller`, `.msp`/`.reg`/`.ps1`/`.vbs`/`.url`/`.scr`/`.cpl`/`.inf`/`.cab`, disk images, ClickOnce, DOS/16-bit programs, `./setup.exe` via `binfmt_misc`, casefold, doctor checks |
| [docs/WINGET.md](docs/WINGET.md) | `lindos-compat winget search\|show\|install`: the index/manifest hash chain, installer selection, the honest refusal list |
| [docs/TRANSFER.md](docs/TRANSFER.md) | `lindos-transfer`: sources (partition / transfer-folder kit), what moves and what never does, the Windows-side kit, `lindos-transfer-gui` |
| [docs/DUALBOOT.md](docs/DUALBOOT.md) | `lindos-dualboot`: one-shot restart into Windows (UEFI `BootNext` / GRUB `grub-reboot`), Secure-Boot/TPM/BitLocker checklist, per-title requirements |
| [docs/GAMING.md](docs/GAMING.md) | launchers, `lindos-game` (incl. `route`/`play`/`cloud install`), `lindos-proton`, `lindos-drivers`, GameMode, MangoHud, gamescope, per-title profiles, controllers |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | **generated** game compatibility matrix (do not edit by hand) |
| [docs/ANTI-CHEAT.md](docs/ANTI-CHEAT.md) | why kernel-level anti-cheat cannot run on Linux, why Lindos ships no spoofer, and what does work |
| [docs/KERNEL.md](docs/KERNEL.md) | the tuned `lindos-kernel` (ntsync, sched_ext, 1000 Hz, MGLRU, BBR): build, install, cmdline, schedulers |
| [docs/VM.md](docs/VM.md) | `lindos-vm`: the honest KVM/VFIO Windows VM, host requirements, create/passthrough flow, why it ships no spoofing and does not defeat VM-blocking anti-cheat |
| [docs/WINAPPS.md](docs/WINAPPS.md) | `lindos-winapps`: seamless Adobe/Office over RDP, backends, the app catalog, and how Lindos never enters your credentials |
| [docs/DRIVERS.md](docs/DRIVERS.md) | `lindos-drivers autodetect` / `install --auto` for GPU + Broadcom Wi-Fi + audio, what the installer installs and when it asks for consent, the silent retry service, Secure Boot, non-free repos |
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
