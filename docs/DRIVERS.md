# Drivers on Lindos

> **What it is, in one paragraph.** `lindos-drivers` detects your hardware and installs the right
> drivers for it — **GPU** (NVIDIA / AMD / Intel), **Broadcom Wi-Fi**, and **audio firmware** — with a
> single `install --auto`, or one class at a time. The **installer** installs firmware and the free drivers
> while it installs the system, so a fresh Lindos install ends up with working graphics, Wi-Fi and sound
> without you hunting for packages; a silent background service retries whatever the installer could not do.
> Proprietary drivers are **never installed silently**: only with your consent, and never while Secure Boot
> would need a key enrolment for them (§3).

This extends the GPU/driver CLI documented in [GAMING.md](GAMING.md) §3 and is binding-consistent with
[`SPEC-VM.md`](../SPEC-VM.md) §24. `lindos-drivers` lives in the `lindos-gaming` package.

## 1. `lindos-drivers autodetect`

```
lindos-drivers autodetect [--json]
```

Scans every relevant device class and returns the **recommended packages** — it detects, it does not
install. It covers three classes:

* **GPU** — the existing detection (`lspci -nn` / sysfs → vendor `nvidia | amd | intel | other`) and
  its recommended driver packages.
* **Broadcom Wi-Fi** — maps the Wi-Fi chip's PCI id to the correct driver, because Broadcom needs
  different drivers per chip:

  | Situation | Recommended package |
  |---|---|
  | Most modern BCM43xx (STA-supported) | `bcmwl-kernel-source` (a.k.a. `broadcom-sta-dkms`) |
  | Older b43-family chips | `firmware-b43-installer` |

  The chip → package mapping ships as data in
  `/usr/share/lindos/drivers/wifi.json` so the choice is auditable and testable against faked PCI ids.
* **Audio firmware** — modern Intel/AMD laptops need SOF firmware and UCM configs for the internal
  speakers/mics to work: `sof-firmware`, `alsa-ucm-conf`, and `firmware-sof-signed` where applicable.

`--json` emits the machine-readable recommendation set (per class: detected device + recommended
packages) that the silent retry and Lindos Settings consume.

## 2. `lindos-drivers install --auto`

```
lindos-drivers install --auto [--dry-run]
```

Installs the `autodetect` recommendations for **GPU + Wi-Fi + audio in one pass**. It routes the
privileged work through the existing helper `install-drivers` → `/usr/libexec/lindos/install-drivers`
(non-root callers get one polkit prompt; the tool never calls `sudo` itself). `--dry-run` prints the
exact package set and commands without changing anything. **Offline → exit 3**, with a clear message
(nothing is faked, nothing is half-installed).

The per-class commands (`install --nvidia-open | --nvidia-proprietary | --amd | --intel`, and the
Wi-Fi/audio installs) remain available individually — `--auto` is the convenience that does all three
at once from the autodetect result. GPU specifics (NVIDIA not on the ISO, `nvidia-drm modeset=1`, the
32-bit Vulkan/GL halves Proton needs, `LINDOS_NVIDIA_DRIVER`) are in [GAMING.md](GAMING.md) §3.

## 3. Installer, first boot and consent

**While installing.** The Lindos installer (SPEC §17, [INSTALLER.md](INSTALLER.md)) runs a `drivers` step
inside the new system, after the package lists are refreshed:

1. **firmware** the image already carries is made sure of (`linux-firmware`, `firmware-sof-signed`, the CPU
   microcode packages; a package no archive carries is left out instead of failing the step);
2. **free drivers**: `ubuntu-drivers install --free-only` — never anything proprietary; "nothing to install"
   is not a failure;
3. **a proprietary GPU driver** (`lindos-drivers install --auto`) **only with your consent** — the installer's
   *Install multimedia codecs* (third-party software) checkbox (`ubiquity/use_nonfree`) or the kernel word
   `lindos.proprietary_drivers=1`; the consent is recorded in `/var/lib/lindos/driver-proprietary-consent`. The
   checkbox's own label mentions only codecs, so the kernel word is the only fully explicit consent.

**Secure Boot.** With Secure Boot **enabled or undetectable**, a PC that may have an NVIDIA GPU never gets the
proprietary driver from the installer, consent or not: an NVIDIA module built on your PC (DKMS) is unsigned
and would need an interactive key enrolment (a blue screen) at the next start, which does not belong in a
first boot that only asks for an account. The step is recorded **`skipped`** with the reason and the driver
stays available in Lindos Settings › Hardware or `lindos-drivers install --nvidia-open|--nvidia-proprietary`
(or turn Secure Boot off). Lindos does not enrol keys for you (OEM mode also skips Ubiquity's own key-enrolment
copy). Legacy-BIOS machines have no Secure Boot. Detection uses `mokutil`, then the EFI variables; whether it
works from the live installer environment is unverified.

**The result** is recorded in `/var/lib/lindos/install-state.json`, step `drivers`: `done`, `pending`
(offline, out of time or disk space), `failed` or `skipped`. `lindos-config install-state` prints it, and
Settings › Apps › Left to finish from setup offers an **Install now** button for `pending`/`failed`.

**Silent retry on the installed system.** `/usr/lib/systemd/system/lindos-driver-firstboot.service` is a
**oneshot** that only covers what the installer could not do. It runs on the installed system only
(`ConditionKernelCommandLine=!boot=casper` and `!boot=live`, never in a container or chroot), only after the
account wizard has finished (`ConditionPathExists=!/lib/systemd/system/oem-config.target`), and only until
`/var/lib/lindos/driver-firstboot.done` exists. It runs `/usr/libexec/lindos/driver-firstboot.sh`, which:

1. reads the install state: `done` or `skipped` → writes the marker and exits at once;
2. for `pending`, `failed` or no record, waits a bounded time for NetworkManager (`wait-for-network`;
   Lindos masks `NetworkManager-wait-online`, so `network-online.target` comes before Wi-Fi is up), and, when
   online, retries **silently** — no window, no wizard, no notification — and records the outcome;
3. never goes beyond what the installer was allowed to do: `ubuntu-drivers install --free-only` by default;
   `lindos-drivers install --auto` only when the consent file exists, and never an NVIDIA DKMS driver with
   Secure Boot on (recorded `skipped`);
4. gives up after three attempts (an attempt counter), never blocks the boot and always exits 0.

The old behaviour — the service writing a `driver-install-offer.json` for the wizard to show — is gone
(nothing ever read it); a retry still records the output of `lindos-drivers autodetect --json` in
`/var/lib/lindos/driver-recommendations.json`, which it uses to tell whether an NVIDIA GPU is present.

**Enabled by default.** `lindos-driver-firstboot.service` is explicitly listed in
`packages/lindos-tune/root/usr/lib/systemd/system-preset/90-lindos.preset`'s enable list
(applied by `lindos-tune apply` at ISO build time, SPEC §8, §11) and covered by a regression test
(`packages/lindos-tune/tests/test_data.py`'s preset test). `lindos-drivers install --auto` already resolves
the NVIDIA package via `ubuntu-drivers devices` — see §2.

**Unverified.** None of this has run on a real install yet: `ubuntu-drivers install --free-only` output and exit
codes for "nothing to install" (handled defensively, not confirmed), `lindos-drivers install --auto` inside a
chroot with the custom `6.14.0-lindos` kernel (DKMS needs matching headers), and the Secure Boot detection.
See [INSTALLER.md](INSTALLER.md#known-limitations-and-what-is-unverified).

## 4. Non-free repositories and metadata

Some of these drivers are proprietary or firmware blobs that live in Ubuntu's `restricted` /
`multiverse` components (and `ubuntu-drivers` metadata for GPU recommendations). When a recommendation
needs them, Lindos **enables those components as required** and refreshes the driver metadata — this
is documented, expected behaviour, not a silent change of your sources. It **never** installs a
proprietary driver silently: enabling a repo makes a package *available*; installing it still goes
through the consent gate above (the installer's checkbox or kernel word, or your own explicit
`lindos-drivers install` / Settings action).

## 5. Controllers and other classes

Xbox controller drivers are separate opt-in installers, documented in [GAMING.md](GAMING.md) §3:

```
lindos-drivers xpadneo install|remove|status
lindos-drivers xone   install|remove|status [--accept-firmware-license] [--skip-firmware]
```

`xone` downloads Microsoft's wireless-dongle firmware **only** with `--accept-firmware-license`.

## 6. Quick reference

```
lindos-drivers detect [--json]                 GPUs (lspci -nn / sysfs)
lindos-drivers autodetect [--json]             GPU + Broadcom Wi-Fi + audio recommendations
lindos-drivers status [--json]                 driver status report  (alias: --status)
lindos-drivers install --auto [--dry-run]      install all autodetect recommendations (offline → 3)
lindos-drivers install --nvidia-open|--nvidia-proprietary|--amd|--intel [--dry-run] [--json]
lindos-drivers gui                             Driver Manager (mintdrivers)
lindos-drivers xpadneo|xone install|remove|status …
```

`LINDOS_OFFLINE=1` forces the offline path (exit 3). Non-root installs go through the helper action
`install-drivers`.

## 7. See also

* [GAMING.md](GAMING.md) — GPU drivers in depth, Vulkan/GL, controllers, Proton.
* [HARDWARE-CONTROL.md](HARDWARE-CONTROL.md) — governor, power profiles, fans, RGB, refresh rate.
* [ARCHITECTURE.md](ARCHITECTURE.md) — the helper and its `install-drivers` action.
* [INSTALLER.md](INSTALLER.md) — the installer flow, offline installs and pending items.
