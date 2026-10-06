# Installing Lindos

> **In one paragraph.** Boot the USB stick and choose **Install Lindos**. The installer does
> *everything heavy* while it installs: system updates, drivers and firmware, Google Chrome
> (downloaded from Google's own apt repository — its licence forbids putting it on the ISO), the
> Wine/Proton layer, the game launchers and the apps of every Mode, plus their Flatpaks. When it
> says so, remove the stick and reboot. The first boot only asks **who you are** (account name,
> password, computer name, language, time zone); then **Lindos Setup** asks for a Mode, a browser and a
> few personal choices. Neither of those two installs or updates anything.

This page is for people installing Lindos. Maintainers: the contract is [SPEC.md §17](../SPEC.md),
the hook and how to test it are in [BUILDING.md](BUILDING.md) ("Installer flow"). **Please read
[Known limitations and what is unverified](#known-limitations-and-what-is-unverified): the flow was
built and unit-tested without a Linux machine and has not yet been run end to end.**

## The three phases

| Phase | What you see | What happens |
|---|---|---|
| **1. The USB session** | The installer and nothing else (*Install Lindos*), or a live desktop (*Try Lindos*) with an *Install Lindos* icon | No first-run wizard, no package installs, no password prompts. The live session is set up so that the PC does not sleep, blank or lock (a closed laptop lid should not suspend it), so a long install is not interrupted: a logind inhibitor covers sleep and the lid in both entries, a small Ubiquity-DM hook switches the screen blanking off in the *Install Lindos* session (which has no desktop), the *Try Lindos* desktop has its own live power settings. |
| **2. Installing** | Ubiquity's pages: language, keyboard, Wi-Fi (if needed), what to install, disk, time zone, a *temporary* account page, then the slideshow with a one-line status | Files are copied, then Lindos' installer step downloads and installs everything heavy into the new system (below), then the boot loader is set up and the first-boot account wizard is armed. |
| **3. First boot** | Remove the stick and reboot: the **account setup** (name, password, computer name, …), then the desktop, then **Lindos Setup** | Only settings are saved. Nothing is installed or updated at first boot (except a silent background retry of anything the installer could not do, below). |

## Before you start

* **Connect to the internet** if you can: wired, or Wi-Fi on the installer's Wi-Fi page. The install
  works offline too (see [Offline installs](#offline-installs-and-pending-items)) but then most
  of the extras are left for later.
* The extras are large. As a **rough, unmeasured estimate**, expect a few GB of downloads (Flatpak
  runtimes are the biggest part) and something between ten minutes and most of an hour on a good link;
  after 45 minutes the installer starts no more downloads and leaves the remaining steps *pending* (a
  package install that has already begun is allowed to finish; `lindos.install_budget=SECONDS` changes the
  45 minutes). **Give the target disk room**: the full install adds roughly 15-25 GB to the base system. The
  installer never fills the disk: it keeps a share free for you and leaves a step *pending* when it does not fit
  ([Disk space](#disk-space-what-the-installer-does-when-the-disk-is-small)); on a partition below **40 GB** the extra
  apps and the Flatpaks are skipped altogether.
* Use a **compatibility mode** entry (`nomodeset`) only if the normal entry shows no picture.
* Everything the installer downloads comes from the vendors' own sources: the Ubuntu and Linux Mint
  package archives, Google's Chrome repository, the WineHQ and Valve (Steam) repositories, the pinned
  umu-launcher release on GitHub and Flathub. No Windows is ever downloaded, nothing about you is sent
  anywhere (no telemetry), and no proprietary GPU driver is installed without your consent.

## The boot menu

| Entry | What it is |
|---|---|
| **Install Lindos** (first, default after 10 s) | Boots straight into the installer and nothing else. |
| Install Lindos (compatibility mode) | The same with `nomodeset`. |
| **Try Lindos (live session)** | The live desktop. Its *Install Lindos* icon runs the *same* flow (the entry carries the same installer settings). Lindos Setup does not start in it. |
| Try Lindos (compatibility mode) | The same with `nomodeset`. |
| Check the integrity of the medium | Verifies the stick. |
| Boot from the first hard disk | Skip the USB stick. |

The BIOS (legacy) menu offers the same entries; it is generated from the UEFI menu when the ISO is
built. Booting the ISO through Ventoy (loopback) uses the same entries.
The old *OEM install (for manufacturers)* entry is gone: every entry prepares the first-boot account
wizard for you.

## During the install

**The temporary-account page.** Lindos uses Ubiquity's *OEM mode*, in which the installer creates a
**temporary** account (named `oem`) and the real account is created at the first boot. Ubiquity shows a
page for that temporary account (computer name and a password) that cannot be hidden: **leave the
password empty and press Continue.** The installer creates the account with a locked password whatever you
type, so nothing needs to be chosen. (An earlier build created it with the typed password, and an empty one
stopped the installer at "Creating user" - if a build of yours still does, type any password on that page; the
account is deleted at the first boot anyway.) It is deleted at the first boot. (If the account setup could not be prepared, the account cannot be locked away
because it is then your only way in: it gets a random password instead, see the troubleshooting table.) The window title may say "OEM mode, for manufacturers only": that is
Ubiquity's own text and it does not mean anything is wrong.

**What the installer window shows.** After the files are copied, the slideshow keeps playing and the
installer's status line is meant to name the current step ("Refreshing package lists...", "Downloading
Google Chrome...", "Installing system updates..."); that this renders as intended in the real window is
unverified. The progress bar barely moves during this stage; only the text changes. Do not turn the PC off
and do not remove the stick until the installer says so.

**What it does, in order** (each step is time-boxed; a problem in one never stops the others or the install). The
order is also the **priority** when the disk is too small for everything: the first steps get the room, the last one is
the first to be skipped ([Disk space](#disk-space-what-the-installer-does-when-the-disk-is-small)). The third column is
what the installer *estimates* the step needs (an estimate from the first real install, not a measurement).

| Step | What | Needs (est.) | If it cannot |
|---|---|---|---|
| Package lists | refreshes the new system's apt lists | - | later steps are left *pending* |
| `updates` | `apt-get upgrade` of what is installed (never a `dist-upgrade`); the kernel, the boot loader and the installer's own packages are left alone. First, because these are the security fixes | ~3 GB | *pending* (also: not enough disk space) / *failed* |
| `browser` | Google Chrome from Google's apt repository, set as the system default browser for new users | ~0.6 GB | *pending* (offline, timeout, disk) or *failed*; *skipped* if the system default is another browser |
| `drivers` | firmware and the free drivers (`ubuntu-drivers install --free-only`); a proprietary GPU driver only with your consent ([below](#drivers-proprietary-consent-and-secure-boot)) | ~0.8 GB | *pending*, *failed* or *skipped* with the reason |
| `compat` | Wine (WineHQ staging), winetricks, umu-launcher | ~2 GB | *pending* / *failed* |
| `gaming` | Steam and Lutris | ~1.5 GB | *pending* / *failed* |
| `mode_extras` | the apt apps of **every** Mode (the Mode is only chosen after the install), one Mode's apps at a time: LibreOffice, Thunderbird, GIMP, Krita, Kdenlive, GameMode, MangoHud and friends | ~6 GB | *pending* / *failed*, naming each app that was left out and why; not on a partition below 40 GB |
| `flatpaks` | Prism Launcher, Sober, Heroic and Bottles from Flathub (best effort) | ~5 GB | *pending*; not on a partition below 40 GB |

A step is recorded as **done** only after the result was checked (Chrome really is installed, and so
on). Anything else is recorded honestly: *pending* (wanted, could not: offline, timed out, no time or disk
space left), *failed*, or *skipped* (by choice or policy). OnlyOffice, which some Modes only *suggest*, is
not installed by the installer.

**A "done" is looked at again at the very end** (once when the hook finishes, once more in the finalisation
after Ubiquity's own package clean-up): a step whose result is no longer on the disk - the Steam launcher,
Wine, Chrome or an extra app that a later step or the clean-up removed - is turned into *failed*, and Chrome's
first-boot marker is taken away so the silent retry installs it again. If the list of installed packages
cannot be read, nothing is judged.

**Extras never remove what is already installed.** Every extras install is *simulated* first
(`apt-get -s install`): one that would **remove** an installed package - Valve's `steam-launcher` was removed by
`apt-get install steam-devices` in the first real install, although the Steam step had just said "done" - or
that apt cannot resolve at all is not run; that app is recorded as *not installed* with the reason ("would
remove steam-launcher", or apt's own error line) and the others are still installed. The only removals that are
accepted are on purpose (Ubuntu's `wine`, which WineHQ staging replaces) or of packages Ubiquity removes from
the new system anyway. `steam-devices` is no longer in the gaming Mode's package list at all: `steam-launcher`
ships the udev rules itself.

The hook keeps the machine awake, never touches the kernel or the boot loader, releases everything it
held when it ends and always repairs the package database before it finishes, so the rest of the install
continues normally even if a step went wrong. The kernel, the boot loader and the installer's own packages
are held for the whole hook; the packages Ubiquity removes anyway (language packs, `libreoffice-l10n-*` and so
on) are held only while the *updates* step runs - held at any other time an exact-version dependency of such a
pack on `libreoffice-common` made apt refuse every LibreOffice app. apt is given the installer's settings
(no `cdrom:` source, no list clean-up, waiting for locks) as `-c FILE` on its command line, never as the
`APT_CONFIG` environment variable: every package's maintainer scripts would inherit that, and Google Chrome's
own post-install script then ran `apt-config` with the binary as its configuration file. On the installed system
the helper that queues the first-boot apt jobs (`apt-serialise`: the Chrome retry, *Install now* in Settings, the
update repair) follows the same rule: it hands its lock-wait setting over as `LINDOS_APT_CONF` (for `-c FILE`) and exports
`APT_CONFIG` only on request (`--apt-config`), which only the driver retry does - it starts apt through
`ubuntu-drivers` / `lindos-drivers`, whose command line it cannot change.

## Disk space: what the installer does when the disk is small

The full install adds roughly **15-25 GB** (a few hundred upgrades, Chrome, Wine, LibreOffice/GIMP/Krita/Kdenlive, four
Flatpaks). A disk that fills up in the middle of a package install leaves a half-unpacked system (the first real laptop
install ended at the first boot in "Unable to load failsafe session / xfconfd isn't running", which is what a full or
half-written root filesystem looks like; the logs are still to be read, so this is a precaution against the most likely
cause, not a diagnosis). So the installer **never runs a step it cannot afford**:

* **Measured at the start and again before every step.** The size and the free space of the partition `/target` is on
  (`df`, the "available" figure) go into `/var/log/lindos/installer.log` as `disk: ...`.
* **A reserve is kept free for you**: the larger of **8 GB** and **12 %** of the partition (30 GB for a 250 GB disk,
  8 GB for anything up to 66 GB). `lindos.install_reserve=GB` on the kernel command line changes it; **less than 2 GB
  is never accepted**, whatever you write, and the installer never leaves less than about 2 GB free.
* **A step runs only if `free >= its estimate + the reserve`.** Otherwise it is left *pending* with the reason in plain
  words, e.g. `not enough disk space: needs ~13.0 GB, 11.1 GB free (about 5.0 GB for this step plus the 8.0 GB kept free
  for you)`, and the steps after it still get their turn (a cheaper one may fit). The order is the priority: the
  updates first (security fixes), then Chrome, the drivers, Wine, the game launchers, the extra apps and, last, the Flatpaks
  (the biggest, so the first to be skipped).
* **A partition below 40 GB** (the size of the whole partition, not the free part) gets **no extra apps and no Flatpaks**:
  those two steps are *pending* with the reason `partition too small`.
* **apt's own figure is used too.** Before any download the simulation's "Need to get ... After this operation, ... of
  additional disk space" is read: the archives and the unpacked files are on the disk at the same time, and both have to
  fit above the reserve, or the step (or, for the extra apps, that Mode's group of apps) is left out without downloading
  anything.
* **Looked at again in the middle.** Between the groups of extra apps and before each Flatpak the free space is measured
  again and the step stops cleanly, *pending*, naming what was left out, as soon as it is down to the reserve (or the
  next app would not fit above it). Before a package install starts (which is never interrupted half way) what is still to
  be unpacked plus the 2 GB floor has to fit once more.
* **Downloaded packages are cleaned out after every step** (`apt-get clean`), not only at the very end: they are
  gigabytes of files that would otherwise sit on the disk until the installer ends.
* **If `df` cannot say**, nothing that needs room is started (the steps are *pending*: "the free disk space could not be
  measured").

What was left out is not lost: it is listed in **Lindos Settings › Apps › Left to finish from setup**, to install
later when there is room (free some space, or add a disk, first). The numbers are **estimates**: the real sizes on real
mirrors have not been measured, and a PC with a very small disk may still end with less free space than the reserve if
the estimates are too low (the 2 GB floor is checked before every package install, not guaranteed).

## Offline installs and pending items

With no internet the install still completes: the installer says "No internet connection - downloads are
skipped" and every step is recorded as *pending* ("offline while installing"). The new system then:

* **Chrome and drivers** are retried **silently in the background** the first time the installed system is
  online (no window, no wizard, no notification; it waits a little for Wi-Fi to connect, and only after the
  account wizard has finished). If you choose Firefox in Lindos Setup while Chrome is pending, the retry
  does not add Chrome — you chose Firefox — and Chrome stays available in Settings.
* **Everything else** (Wine/Proton, the launchers, the Modes' apps, the Flatpaks) waits for you:
  **Lindos Settings › Apps › Left to finish from setup** lists every pending or failed item with an
  **Install now** button (one administrator-password prompt). System updates are handled by the normal
  update tools (Lindos Settings › Update & Recovery / Updates).
* To see the raw record: `lindos-config install-state` (or `--json`), which reads
  `/var/lib/lindos/install-state.json`.

Note that Ubiquity's OEM mode does **not** copy the live session's Wi-Fi profile into the new system, so the
first boot starts offline until you connect on the account wizard's Wi-Fi page (then the silent retries work).

## First boot

1. **Account setup** — Ubiquity's *oem-config* wizard: language, keyboard, Wi-Fi if needed, time zone and
   **your account** (full name, user name, password, computer name, whether to log in automatically). This is
   where your account is created. It runs on its own screen before the desktop exists, so the desktop's
   theme does not reach it; instead the installer gives it the **Lindos-Setup** look (dark, the Lindos accent
   colour) and calls its window "Lindos Setup" (see [The look of the installer and the account
   wizard](#the-look-of-the-installer-and-the-account-wizard) - **unverified until a real boot has shown it**).
   When it finishes it removes the temporary account and the installer's own packages, and the login screen or
   your desktop appears.
2. **Lindos Setup** — full screen on your first login: *mode → browser → personalise → privacy → bring your
   stuff from Windows → summary → all set*. It only saves choices. The browser page offers what is on the PC
   (Firefox; Chrome if the installer added it, or marked "will be added when you're online"). When you press
   **Apply** it asks for your administrator password **once**, to save the system-wide defaults for the Mode
   you chose. The "all set" page shows what the installer did and points to Settings for anything still
   waiting.

## The look of the installer and the account wizard

Both are Ubiquity's GTK program on its **own X server** (`ubiquity-dm`), started by a systemd unit: the *Install
Lindos* boot entries by `ubiquity.service`, the account wizard at the first boot by `oem-config.service`. Neither
runs inside the Lindos desktop session, so neither used the desktop's theme (they showed GTK's light default and a
light title bar, and the wizard was called "System Configuration"). Now:

* **The skin.** `Lindos-Setup` (`build/installer/themes/`, installed by the image build) is dark - surfaces `#202020`
  and `#2B2B2B`, accent `#60CDFF` - and builds on the `Lindos-Dark` theme; when the image has no `Lindos-Dark` it
  falls back to GTK's built-in dark Adwaita, so the pages are dark either way. It sets only colours and typography,
  never sizes.
* **How it is applied.** `GTK_THEME=Lindos-Setup` in a systemd **drop-in** of the unit: the image build writes
  `/etc/systemd/system/ubiquity.service.d/10-lindos.conf`, the installer's finalisation writes
  `/etc/systemd/system/oem-config.service.d/10-lindos.conf` into the new system. The environment of the unit
  reaches the GTK program (Ubiquity's start scripts and `ubiquity-dm` only *add* to it) and GTK honours
  `GTK_THEME` over the desktop's settings. The wizard removes its own drop-in when it has finished
  (`oem-config/late_command`); the installer's is removed by the finalisation. The window frame is drawn by
  metacity (the base image has it, and `ubiquity-dm` prefers it to xfwm4), which reads the `wm_*` colours of the
  same skin.
* **The title.** Ubiquity's own answer `ubiquity/custom_title_text` = "Lindos Setup" replaces "Install (OEM mode,
  for manufacturers only)" and "System Configuration".

Not done: the temporary-account page still carries Ubiquity's own strings ("OEM Configuration (temporary
user)", the OEM-id box: Python literals), the wizard's window icon is Ubiquity's fixed "preferences-system", and
translations of the pages are Ubiquity's.

## Drivers, proprietary consent and Secure Boot

Free drivers and firmware are always installed. A **proprietary** GPU driver (NVIDIA's) is installed only if
you agreed: tick Ubiquity's *Install multimedia codecs* (third-party software) checkbox on the "what to
install" page, or boot with `lindos.proprietary_drivers=1` (the second is the only wording that says drivers
out loud — the checkbox's own label mentions only codecs). And even with consent it is **never installed while Secure Boot
is on** (or cannot be detected) on a PC that may have an NVIDIA GPU: an NVIDIA driver built on your PC is
unsigned and would need an interactive key enrolment at the next start. The step is then recorded *skipped*
with that reason; install it later from Lindos Settings › Hardware or `lindos-drivers install`, or turn
Secure Boot off. The silent retry never goes beyond what the installer was allowed to do. More in
[DRIVERS.md](DRIVERS.md).

## Kernel switches, logs and troubleshooting

Add these words to the kernel command line (GRUB: press `e` on the entry; no spaces in values):

| Word | Effect |
|---|---|
| `lindos.install=off` | The installer step does nothing; every step is recorded *skipped*. |
| `lindos.install_budget=SECONDS` | How long the whole download/install step may take (default `2700`). |
| `lindos.install_reserve=GB` | How much disk space the installer keeps free for you (a whole number of GB; default the larger of 8 and 12 % of the partition; never less than 2). |
| `lindos.proprietary_drivers=1` | Consent to proprietary GPU drivers (still never with Secure Boot on). |

Logs: while installing, `/var/log/lindos/installer-hook.log` (open a terminal in a *Try Lindos* session and
`tail -f` it); afterwards `/var/log/lindos/installer.log` on the installed system, plus Ubiquity's own
`/var/log/installer/`. Silent retries log to `/var/log/lindos/browser-firstboot.log` and
`driver-firstboot.log`.

**The progress note.** If the installer crashes or the PC hangs during the install, read
`/var/lib/lindos/installer-progress` (on the installed disk; `/target/var/lib/lindos/installer-progress` while the
installer still runs). It is **one line**, rewritten before and after every step and every group of extra apps, and
flushed to the disk (`sync -f`) each time, so it still says what the installer was doing when the machine stopped:

```
step=mode_extras phase=group-start free_kb=14376960 utc=2026-10-02T14:03:17Z group=creator
```

`step` is the step id (`-` for the installer itself), `phase` is `start`, `end`, `skipped`, `repair`, `group-start`,
`group-end`, `package`, `app`, `package-lists`, `finishing`, `finished` (the installer ended normally) or `interrupted`
(it was told to stop), `free_kb` is the free space of the target in KB (`unknown` when `df` could not say) and
`utc` the time. A line whose `phase` is not `finished` means the installer did not get to the end. The file is a
diagnostic aid, nothing reads it; it is written as a whole new file and renamed, so it is never half a line. A full
disk can refuse even this line.

**The install state is true even after a crash.** Before the first step runs, every step is written to
`install-state.json` as `pending: the installer ended before this step` (each write is atomic: a temporary file, a flush,
a rename), and each step then records its real result over it. So after a hard hang or a power cut the file already tells
the truth, and Settings lists the steps as left to finish. If the installer is stopped cleanly the step that was running
says `pending: the installer was stopped during this step`.

| Symptom | What to look at |
|---|---|
| The installer looks stuck on "Configuring target system" / a status line | It is probably downloading; the log says what. It stops by itself after the 45-minute budget. |
| The installer crashed or the PC hung during the install, or the first boot cannot start the desktop ("Unable to load failsafe session", "xfconfd isn't running") | Boot the live stick again, mount the installed system and read `var/lib/lindos/installer-progress` (the last thing the installer was doing and the free space then) and `var/log/lindos/installer.log` (`disk:` lines, the reasons). A very low `free_kb` points at a full disk. Please report both files. |
| A step says *pending* with "not enough disk space" or "partition too small" | The installer did not have room for it and left it out on purpose ([Disk space](#disk-space-what-the-installer-does-when-the-disk-is-small)). Free some space, then Settings › Apps › Left to finish from setup. |
| The first boot lands on a desktop as the user `oem` instead of the account wizard | The wizard could not be armed. `/var/lib/lindos/oem-config-not-armed` holds the reason (line 1) and what was done to the account (line 2), `/var/log/lindos/installer.log` the details (`CRITICAL`). The desktop is kept, but the temporary account is **not left open**: its password was empty (the page told you to leave it so), so it gets a **random password** — printed in `LINDOS-ACCOUNT-SETUP-FAILED.txt` on that desktop (readable by that account only; change it with `passwd`, then delete the note) and kept root-only in `/var/lib/lindos/oem-temporary-password`; a password you typed yourself is kept, and if none can be set the account is locked (administrator tasks then need the wizard fixed first). If oem-config is installed, `sudo oem-config-prepare` (then reboot) arms it by hand — it also deletes saved Wi-Fi profiles; if the reason says oem-config is missing, it cannot. Please report it. |
| Updates, drivers or extra apps say *pending* although the PC was online | The package lists could not be refreshed completely (a source timed out or the connection dropped during `apt-get update`): the installer does not trust an "up to date" or "no drivers" answer from incomplete lists, records the step as pending and retries it later. `/var/log/lindos/installer.log` names the failed fetches. |
| The installer screen goes dark after about ten minutes | Input wakes it. It should not happen: the *Install Lindos* session runs `50lindos-noblank` (`xset s off s noblank -dpms`); `grep lindos-noblank /var/log/installer/dm` in a terminal shows what it did. Please report it. |
| Chrome or a driver is missing | `lindos-config install-state`; then Settings › Apps › Left to finish from setup. |
| An extra app is *failed* and the reason says "would remove …" or shows an apt error | The installer did not install it on purpose: installing it would have removed another installed package, or apt could not resolve it (see "Extras never remove what is already installed" above). `/var/log/lindos/installer.log` has the `Remv`/`E:` lines. The rest of the extras were installed. |
| A step says *failed* although it said done in the installer window | The final check found its result gone again (a later step or Ubiquity's clean-up removed it): `installer.log` says "final check: step … said done but … is not installed". |
| The first boot starts offline | Connect on the wizard's Wi-Fi page; the retries then run on their own. |

## Known limitations and what is unverified

Honesty first. **The whole flow was written against the source code of Ubiquity, casper and oem-config and
unit-tested with fake target systems on a Windows PC. It has run end to end exactly once, in QEMU on a CI
runner (`build/qa/install_test.py`, an automated Ubiquity install with real internet, then a boot of the
installed disk into the account wizard) — never on real hardware.** That run worked (updates, drivers, Chrome,
Wine, Lutris and the Flatpaks were installed, oem-config was armed, the installed disk booted into the
wizard) and found the problems this page now describes as fixed: LibreOffice refused because of held
packages, the Steam launcher was removed by an extra app, Chrome's post-install script complained about
`APT_CONFIG`, and the installer and the wizard did not have the Lindos look. **Those fixes themselves are
tested only against fake targets: the next CI install is their first proof.** Where a claim in these docs
says "verified", it means "read in upstream source, in the real Mint 22.2 ISO's file tree or in the log of
that one QEMU run". Real hardware will find more; expect several fix rounds.

### What may surprise you (user-visible)

* **The installer still shows a temporary-account page** and Ubiquity's "OEM mode, for manufacturers
  only" wording (a preseed cannot hide it; the alternative is patching Ubiquity, a maintenance and product
  decision). Leave the password empty and press Continue (if the installer stops at "Creating user", type any password).
* **The first-boot account wizard is still Ubiquity's**: it asks for language, keyboard and time zone again
  (the installer's answers are carried over as defaults) and, at its end, may show a small package clean-up
  window. It has the Lindos-Setup skin and the title "Lindos Setup" now ([above](#the-look-of-the-installer-and-the-account-wizard)),
  but nobody has seen the result yet: in the one QEMU run it was Ubiquity's light default with the title "System
  Configuration".
* **The installer window shows one status line**; the bar barely moves, and the whole download step can
  take most of an hour. It may look frozen.
* **Everything for every Mode is installed**, because the Mode is chosen after the installation. That is
  several GB you may never use, and there is no "minimal" install switch yet (only
  `lindos.install=off`, which skips *all* of it, or a shorter `lindos.install_budget`).
* **Offline installs** leave the extras *pending*; only Chrome and the drivers are retried automatically,
  and only after the account wizard and once a connection exists. Wi-Fi profiles are not copied to the new
  system. A Wi-Fi chip that needs a downloadable driver cannot get online during the install (chicken and
  egg); expect *pending* and the silent retry.
* **Firefox chosen in Lindos Setup while Chrome is still pending** means Chrome is not added
  automatically (Settings › Apps adds it).
* **Proprietary GPU drivers**: consent is the "third-party software" checkbox (whose label only says
  codecs) or `lindos.proprietary_drivers=1`; with Secure Boot on (or unknown) an NVIDIA driver is never
  installed by the installer, and Lindos does not enrol keys for you. Whether the NVIDIA DKMS build works
  with the custom `6.14.0-lindos` kernel is unverified.
* **Lindos Setup asks for the administrator password once**, when you press Apply (to save system-wide
  defaults, also for the Everyday Mode). A prompt-free Lindos Setup is not achieved.
* **Microsoft Edge is never installed by the installer**; add it in Settings › Apps.
* **Ubiquity's own later steps now go online too.** The installer refreshes the new system's package lists
  (they were missing, which silently disabled Ubiquity's language-pack and codec steps). That makes the
  install longer, and a connection that drops during those later steps is not caught by Lindos' step (it can
  abort the install). The first-boot wizard's own packages may pull in extras (Mint 21 users reported a
  desktop environment being pulled in by oem-config-gtk's Recommends).
* **Kernel, boot loader and firmware**: the installer never upgrades the kernel or boot loader (Ubiquity does
  its own initramfs and boot-loader work afterwards). A firmware or systemd upgrade inside the installer can
  rebuild the initramfs an extra time, which is slow.
* **Wine**: the installer adds WineHQ staging on top of the Ubuntu `wine` the image carries; how the two
  coexist on a real system is unverified. Wine is not Windows, and programs that need kernel drivers or
  kernel-level anti-cheat do not run ([README](../README.md), [ANTI-CHEAT.md](ANTI-CHEAT.md)).

### What only a real install can confirm (maintainers)

These are the unverified assumptions; each one has a check in the QEMU procedure of
[BUILDING.md](BUILDING.md) ("Installer flow").

* That Ubiquity (Mint 24.04.3+mint18) really runs `/usr/lib/ubiquity/target-config/50lindos-install` (name,
  exec bit, working directory, stdin/stdout through `log-output`), that a slow, hung or failing hook cannot
  abort the install, and that the `ubiquity/success_command=…` kernel word and the baked debconf seed reach
  Ubiquity and run `finalize.sh` in well under 5 s in every entry (also Ventoy loopback and the *Try*
  desktop launcher).
* That the private mount namespace (`unshare --mount --propagation private`, proc/sysfs/`/dev`/fresh `/run`
  plus `chroot`) leaves no mount behind and does not disturb Ubiquity's later steps, that name resolution
  works inside the chroot, and that `resolv.conf` is restored.
* `apt-get update` verdicts: stock apt exits 0 after transient index failures (timeouts, DNS, refused
  connections), so the hook asks for `APT::Update::Error-Mode "any"` (from apt's manual, apt 2.7 on noble; not run
  here) **and** does not trust the exit status alone: an `Err:` / `E:` / "Failed to fetch" line in the output, or no
  non-empty network `Packages` list on disk, also makes the update incomplete (retried once, then the steps that
  depend on the lists - updates, drivers, extra apps - stay *pending*). Whether real apt output matches those
  patterns on every failure mode is unverified.
* Real apt/dpkg behaviour: the apt configuration that ignores the medium's `cdrom:` source; download first
  then `--no-download` install; holds and the version pin of ~300 names; that `apt-get upgrade` keeps back
  what it must; that the repair pass really leaves dpkg clean after a killed or failed run and that
  Ubiquity's later python-apt steps (language packs, codecs, `remove_extras`) then work against refreshed
  network lists.
* Debconf: that `db_x_loadtemplatefile` + `db_subst` + `db_progress INFO` really renders a one-line status in
  the GTK window; `db_get ubiquity/use_nonfree`; the confmodule file-descriptor handling under
  `systemd-inhibit`.
* The `only-ubiquity` session has no desktop, so only two things keep it awake: `lindos-live-inhibit.service`
  (a logind inhibitor: sleep, idle action, lid and suspend keys - it cannot stop the X server's own
  screensaver/DPMS) and the ubiquity-dm hook `/usr/lib/ubiquity/dm-scripts/install/50lindos-noblank`
  (`xset s off s noblank -dpms`, from `dm-noblank.sh`). Unverified: that ubiquity-dm really runs the hook (it is
  read in its source: every dot-less executable of that directory, once, after X is up, as the live user) and
  that the display then stays on for a long idle install (`DISPLAY=:0 xset q` must say `timeout: 0` and
  `DPMS is Disabled`; the hook logs the same to `/var/log/installer/dm`).
* `systemd-inhibit` from the hook in the `only-ubiquity` boot (no logind user session), and
  `lindos-live-inhibit.service`, the live-session power settings (xfconf property names, a
  `Phase=Initialization` autostart running before xfce4-power-manager reads them) and the `Exec` of
  `lindos-live-session.desktop` under a real XFCE session.
* Chrome from the staged repository inside the chroot (dependencies, duplicate repository entries),
  `ubuntu-drivers install --free-only` output and exit codes when there is nothing to install,
  `lindos-drivers install --auto` in a chroot with the custom kernel and DKMS, Secure Boot detection through
  `mokutil`/efivars from the live system; WineHQ staging next to Ubuntu's wine, the Steam repository, the umu
  download, the size and time of the union of Mode extras (and whether `ananicy-cpp` exists for noble);
  the 45-minute default on real links.
* Flatpak inside the installer's chroot (bwrap, triggers): it fails soft to *pending* by design but is
  unproven.
* **The disk policy** ([Disk space](#disk-space-what-the-installer-does-when-the-disk-is-small)) was only run against a
  fake disk: that `df -Pk /target` in the live session reports the partition Ubiquity mounted at `/target` (and not,
  say, the live overlay), that the "available" figure is a fair measure on the real file system, that the per-step
  estimates (3 / 0.6 / 0.8 / 2 / 1.5 / 6 / 5 GB; Flatpaks ~1.25 GB each) are about right on real mirrors (they come from
  the first QEMU install and from guesses), that real `apt-get -s` prints the "Need to get ..." and "After this
  operation, ..." lines in the formats the parser reads (powers of 1000, thousands separators; nothing is judged from
  a simulation without them), that `apt-get clean` between the steps does not disturb Ubiquity's later steps, and that
  `sync -f` on the target is quick enough to be done a few dozen times. **Whether a full disk is what actually broke
  the first laptop install is not known** - the logs and `installer-progress` of the next failure will say.
* **OEM mode**: that the temporary-account page appears as described and accepts an empty password (the account is then created from the preseeded locked hash `passwd/user-password-crypted`; a real install with an EMPTY password stopped at "Creating user" before that seed existed, and CI now runs this path by default, `--typed-password` is the other);
  that `systemctl --root=/target enable` / `set-default oem-config.target` works and the first start really
  shows the account wizard; that a locked `oem` account (`passwd -l`) does not stop `ubiquity-dm` from running
  the wizard as `oem`; that no autologin is left in `lightdm.conf`; that resetting
  `user-setup/allow-password-empty` lands in the database the wizard reads; that `oem-config.target` is removed
  before `graphical.target` is isolated (the silent retries depend on that); that
  `filesystem.manifest-remove` with `lindos-installer` appended makes Ubiquity remove it in OEM mode.
  The fallback when the wizard cannot be armed (random password through `chpasswd` inside the chroot, the
  autologin desktop still signing in, the note on the desktop) has only run against fake targets.
* **The base ISO**: that its pool carries `oem-config` and `oem-config-gtk` (and `aptdaemon` with its GTK
  widgets) at exactly the squashfs's Ubiquity version, and that `.disk/cd_type` and `dists/` look as
  `build/lib/verify_oem_pool.py` expects (the build asserts this; the source of the assumption is the file
  tree of the Mint 22.2 XFCE ISO, read over HTTP). Mint bumps Ubiquity every few months: re-read
  `plugininstall.py` when `MINT_VERSION` changes.
* **The boot menus**: `boot_menu.py`'s rewrite of the real Mint 22.2 `isolinux/live.cfg` (its structure is
  known from research; the file was never processed here), the generated BIOS menu, the UEFI menu, and
  `xorriso` including `/lindos/oem-debs`.
* **The look of the installer and the wizard**: that `GTK_THEME` from the unit's drop-in reaches the GTK program (read
  in Ubiquity's source; the CI observer now reads the program's environment and fails the run when it is missing),
  that GTK honours it over the desktop's settings (its documentation says so), that the `Lindos-Setup` style sheet
  loads and every page is readable (a dark background never with dark text: checked only by reading the CSS), that
  metacity's window frame follows the skin's `wm_*` colours (it may stay light: the first-boot screenshot is
  checked for "mostly light" and only warns), and that `oem-config/late_command` removes the drop-in. The
  *first-boot.png* and *install-progress-\*.png* files of the CI artifact are the evidence.
* **GTK, seen by nobody yet**: the wizard pages (in particular the banners and the "Set up while Lindos was
  installing" box on the done page), Settings › Apps › Left to finish from setup with real pending items and
  the **Install now** buttons through the real helper actions, and the search box interplay.
* **Tests that do exist** run everything else hermetically (fake targets, fake runners, `LINDOS_TEST_CMDLINE`):
  `packages/lindos-installer/tests`, `build/tests`, `tests/test_boot_menu.py`, the lindos-core session /
  install-state / first-boot tests and the lindos-setup / lindos-settings suites. The CI `boot-test` job
  boots the kernel directly and never sees the installer. The QEMU install test (`build/qa/install_test.py`, the
  opt-in CI job `install-test`: blank disk, `automatic-ubiquity` with a CI-only seed, read-only inspection of the
  installed disk, then a boot to see the account wizard) has run once; its findings are in the list above. Two
  of its verdicts were the test's own faults and are fixed: the check for "no desktop session" matched the
  command line of `earlyoom` (which names the panel in a regular expression) instead of process names, and its
  listing of the ISO's boot files put several `xorriso -find` commands into one run. `/run` in the installed system
  is not empty after the install (`/run/mount` and `/run/adduser`): that is Ubiquity's own user setup (it runs
  `mount` and `adduser` in a bare chroot before it binds `/run`), not the hook; harmless on a tmpfs, tidied by the
  finalisation and only noted, not warned about, by the test.

## See also

* [BUILDING.md](BUILDING.md) — "Installer flow": the hook contract, files, logs, base-ISO assumptions, how to
  test in QEMU; `config.env` knobs `LIVE_ONLY_PACKAGES`, `REQUIRE_OEM_POOL`, `OEM_DEBS_DIR`.
* [SPEC.md](../SPEC.md) §17 — the binding contract.
* [DRIVERS.md](DRIVERS.md), [SETTINGS.md](SETTINGS.md), [MODES.md](MODES.md), [FAQ.md](FAQ.md).
