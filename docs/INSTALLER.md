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
  45 minutes). Give the target disk several GB more than the base
  system needs; a step is skipped, and recorded as *pending*, when the disk is too small for it.
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
password empty and press Continue.** The temporary account is locked when the installation ends and is
deleted at the first boot. (If the account setup could not be prepared, the account cannot be locked away
because it is then your only way in: it gets a random password instead, see the troubleshooting table.) The window title may say "OEM mode, for manufacturers only": that is
Ubiquity's own text and it does not mean anything is wrong.

**What the installer window shows.** After the files are copied, the slideshow keeps playing and the
installer's status line is meant to name the current step ("Refreshing package lists...", "Downloading
Google Chrome...", "Installing system updates..."); that this renders as intended in the real window is
unverified. The progress bar barely moves during this stage; only the text changes. Do not turn the PC off
and do not remove the stick until the installer says so.

**What it does, in order** (each step is time-boxed; a problem in one never stops the others or the install):

| Step | What | If it cannot |
|---|---|---|
| Package lists | refreshes the new system's apt lists | later steps are left *pending* |
| `browser` | Google Chrome from Google's apt repository, set as the system default browser for new users | *pending* (offline, timeout) or *failed*; *skipped* if the system default is another browser |
| `drivers` | firmware and the free drivers (`ubuntu-drivers install --free-only`); a proprietary GPU driver only with your consent ([below](#drivers-proprietary-consent-and-secure-boot)) | *pending*, *failed* or *skipped* with the reason |
| `updates` | `apt-get upgrade` of what is installed (never a `dist-upgrade`); the kernel, the boot loader and the installer's own packages are left alone | *pending* / *failed* |
| `compat` | Wine (WineHQ staging), winetricks, umu-launcher | *pending* / *failed* |
| `gaming` | Steam and Lutris | *pending* / *failed* |
| `mode_extras` | the apt apps of **every** Mode at once (the Mode is only chosen after the install): LibreOffice, Thunderbird, GIMP, Krita, Kdenlive, GameMode, MangoHud and friends | *pending* / *failed* |
| `flatpaks` | Prism Launcher, Sober, Heroic and Bottles from Flathub (best effort) | *pending* |

A step is recorded as **done** only after the result was checked (Chrome really is installed, and so
on). Anything else is recorded honestly: *pending* (wanted, could not: offline, timed out, no time or disk
space left), *failed*, or *skipped* (by choice or policy). OnlyOffice, which some Modes only *suggest*, is
not installed by the installer.

The hook keeps the machine awake, never touches the kernel or the boot loader, releases everything it
held when it ends and always repairs the package database before it finishes, so the rest of the install
continues normally even if a step went wrong.

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
   where your account is created. It looks like Ubiquity, not like Lindos Setup: it runs on its own screen
   before the desktop exists, and Lindos' own styling only reaches the windows inside the desktop session.
   When it finishes it removes the temporary account and the installer's own packages, and the login screen or
   your desktop appears.
2. **Lindos Setup** — full screen on your first login: *mode → browser → personalise → privacy → bring your
   stuff from Windows → summary → all set*. It only saves choices. The browser page offers what is on the PC
   (Firefox; Chrome if the installer added it, or marked "will be added when you're online"). When you press
   **Apply** it asks for your administrator password **once**, to save the system-wide defaults for the Mode
   you chose. The "all set" page shows what the installer did and points to Settings for anything still
   waiting.

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
| `lindos.proprietary_drivers=1` | Consent to proprietary GPU drivers (still never with Secure Boot on). |

Logs: while installing, `/var/log/lindos/installer-hook.log` (open a terminal in a *Try Lindos* session and
`tail -f` it); afterwards `/var/log/lindos/installer.log` on the installed system, plus Ubiquity's own
`/var/log/installer/`. Silent retries log to `/var/log/lindos/browser-firstboot.log` and
`driver-firstboot.log`.

| Symptom | What to look at |
|---|---|
| The installer looks stuck on "Configuring target system" / a status line | It is probably downloading; the log says what. It stops by itself after the 45-minute budget. |
| The first boot lands on a desktop as the user `oem` instead of the account wizard | The wizard could not be armed. `/var/lib/lindos/oem-config-not-armed` holds the reason (line 1) and what was done to the account (line 2), `/var/log/lindos/installer.log` the details (`CRITICAL`). The desktop is kept, but the temporary account is **not left open**: its password was empty (the page told you to leave it so), so it gets a **random password** — printed in `LINDOS-ACCOUNT-SETUP-FAILED.txt` on that desktop (readable by that account only; change it with `passwd`, then delete the note) and kept root-only in `/var/lib/lindos/oem-temporary-password`; a password you typed yourself is kept, and if none can be set the account is locked (administrator tasks then need the wizard fixed first). If oem-config is installed, `sudo oem-config-prepare` (then reboot) arms it by hand — it also deletes saved Wi-Fi profiles; if the reason says oem-config is missing, it cannot. Please report it. |
| Updates, drivers or extra apps say *pending* although the PC was online | The package lists could not be refreshed completely (a source timed out or the connection dropped during `apt-get update`): the installer does not trust an "up to date" or "no drivers" answer from incomplete lists, records the step as pending and retries it later. `/var/log/lindos/installer.log` names the failed fetches. |
| The installer screen goes dark after about ten minutes | Input wakes it. It should not happen: the *Install Lindos* session runs `50lindos-noblank` (`xset s off s noblank -dpms`); `grep lindos-noblank /var/log/installer/dm` in a terminal shows what it did. Please report it. |
| Chrome or a driver is missing | `lindos-config install-state`; then Settings › Apps › Left to finish from setup. |
| The first boot starts offline | Connect on the wizard's Wi-Fi page; the retries then run on their own. |

## Known limitations and what is unverified

Honesty first. **The whole flow was written against the source code of Ubiquity, casper and oem-config and
unit-tested with fake target systems on a Windows PC. No install has run end to end — not in QEMU, not on
real hardware.** Where a claim in these docs says "verified", it means "read in upstream source or in the
real Mint 22.2 ISO's file tree"; nothing below has been *run*. The first QEMU install may find problems, and
the first real-hardware install will; expect several fix rounds.

### What may surprise you (user-visible)

* **The installer still shows a temporary-account page** and Ubiquity's "OEM mode, for manufacturers
  only" wording (a preseed cannot hide it; the alternative is patching Ubiquity, a maintenance and product
  decision). Leave the password empty and press Continue.
* **The first-boot account wizard looks like Ubiquity**, not like Lindos Setup, and asks for language,
  keyboard and time zone again (the installer's answers are carried over as defaults). At its end it
  may show a small package clean-up window. Lindos-branded styling for it is not done.
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
* **OEM mode**: that the temporary-account page appears as described and accepts an empty password;
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
* **GTK, seen by nobody yet**: the wizard pages (in particular the banners and the "Set up while Lindos was
  installing" box on the done page), Settings › Apps › Left to finish from setup with real pending items and
  the **Install now** buttons through the real helper actions, and the search box interplay.
* **Tests that do exist** run everything else hermetically (fake targets, fake runners, `LINDOS_TEST_CMDLINE`):
  `packages/lindos-installer/tests`, `build/tests`, `tests/test_boot_menu.py`, the lindos-core session /
  install-state / first-boot tests and the lindos-setup / lindos-settings suites. The CI `boot-test` job
  boots the kernel directly and never sees the installer. A QEMU install test (`build/qa/install_test.py`:
  blank disk, `automatic-ubiquity` with a CI-only seed, read-only inspection of the installed disk, then a boot
  to see the account wizard) is being added to CI and has not yet run on GitHub Actions.

## See also

* [BUILDING.md](BUILDING.md) — "Installer flow": the hook contract, files, logs, base-ISO assumptions, how to
  test in QEMU; `config.env` knobs `LIVE_ONLY_PACKAGES`, `REQUIRE_OEM_POOL`, `OEM_DEBS_DIR`.
* [SPEC.md](../SPEC.md) §17 — the binding contract.
* [DRIVERS.md](DRIVERS.md), [SETTINGS.md](SETTINGS.md), [MODES.md](MODES.md), [FAQ.md](FAQ.md).
