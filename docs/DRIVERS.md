# Drivers on Lindos

> **What it is, in one paragraph.** `lindos-drivers` detects your hardware and installs the right
> drivers for it — **GPU** (NVIDIA / AMD / Intel), **Broadcom Wi-Fi**, and **audio firmware** — with a
> single `install --auto`, or one class at a time. A first-boot service runs the detection once and
> offers the install when you are online, so a fresh Lindos install ends up with working graphics,
> Wi-Fi and sound without you hunting for packages. Proprietary drivers are **never installed silently**
> — the first-boot/OOBE consent gate always applies.

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
packages) that the first-boot service and Lindos Settings consume.

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

## 3. First-boot service

Lindos ships `/usr/lib/systemd/system/lindos-driver-firstboot.service`: a **oneshot** unit gated on a
first-run marker (systemd `ConditionPathExists` / `ConditionFirstBoot`) that:

1. runs `lindos-drivers autodetect`;
2. when the machine is **online and not an OEM image**, offers the install — either interactively or
   by writing a notification for the OOBE / Lindos Settings to surface;
3. does nothing on later boots (idempotent) and **never blocks boot** — if it cannot run (offline, OEM,
   already done) it exits cleanly and gets out of the way.

Because it only *offers* the install, proprietary drivers are never pulled in behind your back; you
confirm through the first-boot prompt or the OOBE. On an OEM image the offer is deferred to the end
user's own first boot.

## 4. Non-free repositories and metadata

Some of these drivers are proprietary or firmware blobs that live in Ubuntu's `restricted` /
`multiverse` components (and `ubuntu-drivers` metadata for GPU recommendations). When a recommendation
needs them, Lindos **enables those components as required** and refreshes the driver metadata — this
is documented, expected behaviour, not a silent change of your sources. It **never** installs a
proprietary driver silently: enabling a repo makes a package *available*; installing it still goes
through the first-boot / OOBE consent gate above.

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
