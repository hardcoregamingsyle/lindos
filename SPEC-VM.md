# Lindos — Engineering Specification, Addendum V (Virtualization, WinApps, Proton-system, Drivers, CI)

Extends `SPEC.md` and `SPEC-KERNEL.md`. Sections continue at §20–§25. All base rules apply
(package format, LF-only, stdlib-only Python importable on any OS with `LINDOS_ROOT`/`LINDOS_HOME`
guards, ShellCheck-clean `set -Eeuo pipefail` shell, argparse `--json`, unit-tested, green under
`bash tests/run.sh` on Windows).

## 20. Honesty rules for this addendum (hard, binding — extends §0.1 and §14)

- **`lindos-vm` is a real, honestly-labelled Windows virtual machine.** It MUST NOT ship, generate,
  or document ANY anti-cheat/VM-detection evasion: no hypervisor-hiding (`kvm=off`,
  `hv-vendor-id` masquerading to defeat detection, hiding the hypervisor CPUID bit), no SMBIOS /
  motherboard-UUID / serial spoofing, no fake ACPI tables, no CPUID/vendor forging, no
  TPM-EK/attestation forging, no HWID evasion. The generated libvirt XML and all configs use
  standard, truthful values. A unit test greps the generated XML/templates/configs and asserts NONE
  of these tokens are present. If a task asks for any of it, implement nothing and record it under
  `deviations` with the reason: it cannot produce hardware-rooted signatures, only gets the user
  hardware-banned, and is a cheat/evasion tool.
- Kernel-mode anti-cheat that blocks VMs (Riot Vanguard, and titles whose publisher disabled
  EAC/BattlEye on Linux) will NOT run in `lindos-vm`, and Lindos does nothing to change that. Docs
  MUST say so plainly. `lindos-vm` is for Windows software and VM-tolerant games (Adobe, Office,
  most single-player titles), not for defeating anti-cheat.
- `lindos-vm` / `lindos-winapps` require a **user-supplied, licensed Windows** and licensed apps.
  Lindos never downloads Windows, never enters or stores the user's Windows/app credentials, and
  never bypasses activation. WinApps runs over RDP against the user's own Windows.

## 21. `lindos-vm` — KVM/VFIO Windows VM with (optional) GPU passthrough

New package `packages/lindos-vm/`:
```
DEBIAN/control    # Depends: python3, lindos-core ;
                  # Recommends: qemu-system-x86 | qemu-kvm, libvirt-daemon-system, libvirt-clients,
                  #             ovmf, virt-manager, virtiofsd ; Suggests: looking-glass-client
DEBIAN/postinst   # add invoking user to libvirt/kvm groups IF present (getent); never fail install
DEBIAN/postrm
root/usr/bin/lindos-vm
root/usr/lib/lindos-vm/lindos_vm/__init__.py
root/usr/lib/lindos-vm/lindos_vm/caps.py         # host capability probe (LINDOS_ROOT-aware)
root/usr/lib/lindos-vm/lindos_vm/plan.py         # setup plan (IOMMU, vfio bind) — prints, never auto-applies
root/usr/lib/lindos-vm/lindos_vm/domain.py       # libvirt domain XML generation (honest, no spoofing)
root/usr/lib/lindos-vm/lindos_vm/passthrough.py  # vfio-pci bind/unbind helpers (root via pkexec)
root/usr/share/lindos/vm/win.xml.template        # q35 + OVMF/UEFI + virtio template (no hidden/spoof knobs)
root/usr/share/lindos/vm/vfio.conf.template      # modprobe vfio-pci ids= template
root/etc/libvirt/hooks/qemu                       # single-GPU passthrough hook (see §21.3), conffile
tests/conftest.py tests/test_caps.py tests/test_domain.py tests/test_plan.py tests/test_no_spoof.py
```

### 21.1 CLI `/usr/bin/lindos-vm` (argparse, `--json` on reads; mutations print the exact pkexec/virsh command when not root — never call sudo)
- `check [--json]` — probe: CPU vendor + virtualization ext (`vmx`/`svm` from `/proc/cpuinfo`),
  `/dev/kvm` present, IOMMU enabled (kernel cmdline `intel_iommu=on|amd_iommu=on` + populated
  `/sys/kernel/iommu_groups/`), GPUs and their IOMMU groups (`lspci`/sysfs), total/free RAM, whether
  OVMF/libvirt/qemu are installed. All reads are `LINDOS_ROOT`-aware so the probe is testable on
  Windows against a faked tree. Reports a clear "ready / not ready + why" summary.
- `setup [--single-gpu|--dual-gpu] [--dry-run]` — produce the passthrough plan via `plan.py`:
  the exact IOMMU grub edit (delegates to `lindos-kernel cmdline set` when present), the vfio-pci
  `ids=` line for the chosen passthrough GPU, initramfs update, and (single-GPU) the libvirt hook.
  Prints every privileged step as a copy-pasteable `pkexec …`; `--dry-run` prints only.
- `create --name N --windows-iso PATH [--disk-gb 64] [--ram-gb 8] [--cpus N] [--gpu auto|PCI] [--virtio-iso PATH]`
  — render `win.xml.template` (q35, OVMF UEFI, virtio disk/net, SPICE/qxl by default or vfio host
  device when `--gpu`), write `~/.local/share/lindos/vm/<name>.xml`, and `virsh define` it. The XML
  is a **standard** VM description — no hypervisor hiding, no SMBIOS/UUID/ACPI/CPUID spoofing.
- `start|stop|status <name>`, `list`.
- `passthrough list|bind <pci>|unbind <pci>` — bind/unbind a device to `vfio-pci` (root via pkexec).
- `--version`. Exit codes: 0 ok · 1 error · 2 usage · 3 host-not-capable.

### 21.3 `/etc/libvirt/hooks/qemu` (single-GPU passthrough)
A real, well-known pattern: on `prepare/begin`, stop the display manager + unbind the GPU/console
from host DRM/framebuffer and bind `vfio-pci`; on `release/end`, reverse it. It contains NO
detection-evasion (no `kvm=off`, no SMBIOS/ACPI spoof). Guarded, idempotent, logs to
`/var/log/lindos/vm-hook.log`, and no-ops for domains it does not manage.

## 22. `lindos-winapps` — seamless Windows apps over RDP (Adobe / Office)

New package `packages/lindos-winapps/`:
```
DEBIAN/control   # Depends: python3, lindos-core, freerdp3-x11 | freerdp2-x11 ;
                 # Recommends: lindos-vm | libvirt-daemon-system ; Suggests: podman
root/usr/bin/lindos-winapps
root/usr/lib/lindos-winapps/lindos_winapps/{__init__,backend,apps,rdp}.py
root/usr/share/lindos/winapps/apps.json          # known app catalog (id,name,rdp_path,categories,icon)
root/usr/share/applications/lindos-winapps.desktop
tests/conftest.py tests/test_apps.py tests/test_backend.py
```
Implements the winapps-org/winapps pattern: a backend Windows instance (default the `lindos-vm`
libvirt VM `RDPWindows`; optional podman `dockur/windows` for advanced users) reachable over RDP;
FreeRDP RemoteApp launches an individual Windows program in its own window on the Lindos desktop.
CLI `lindos-winapps`:
- `setup` — write `~/.config/lindos/winapps/winapps.conf` (backend, host, RDP user/domain, flags).
  It NEVER prompts for or stores the password (points the user to `~/.config/lindos/winapps/`
  and FreeRDP's own credential handling / a documented `RDP_PASS` env or a file the user creates);
  Lindos does not enter Windows/app credentials for the user (base safety rule).
- `check` — backend reachable? FreeRDP present? RDP port open?
- `list` — detected installed Windows apps (from the catalog + probing the backend when reachable).
- `install <id>` — create `~/.local/share/applications/lindos-winapp-<id>.desktop`
  (Exec=`lindos-winapps run <id>`, real icon when available) so Photoshop/Office appear in the menu.
- `run <id> [-- args]` — launch via FreeRDP RemoteApp (`xfreerdp /app:… /app-cmd:…`).
- `remove <id>`, `--version`. Exit: 0/1/2/3(backend-unreachable).
`apps.json` ships at least: `photoshop`, `illustrator`, `premiere`, `office-word`, `office-excel`,
`office-powerpoint`, `office-outlook`, plus a generic `explorer`. Each entry documents that it needs
a licensed Windows + that app installed in the backend. Honest notes; no license bypass.

## 23. Proton-GE system-wide + gamescope session (extend `lindos-gaming` / `lindos-compat`)

- `lindos-proton` gains `install --system [<tag>]` / `list --system` / `path`: install a GE-Proton
  build to `/usr/share/lindos/proton/<tag>/` (system-wide) in addition to the per-user
  `compatibilitytools.d`. `lindos-run`/umu prefer `UMU_PROTONPATH`/profile, then the newest
  system GE, then user GE — documented resolution order. Mutating the system dir is root (pkexec).
- gamescope session: `lindos-gaming` ships `/usr/bin/lindos-gamescope-session` and
  `/usr/share/wayland-sessions/lindos-gaming.desktop` (a "Lindos Gaming" session at the login
  screen: a gamescope Wayland micro-compositor hosting Steam Big Picture / a chosen launcher),
  degrading with a clear message when gamescope/Steam are absent. This does not change the default
  XFCE session; it is an optional extra session.
- Honesty: Proton is a translation layer, not Windows; near-native, not universally "identical".
  No claim that Wayland/gamescope makes anti-cheat titles work.

## 24. Driver automation (extend `lindos-drivers` + first-boot)

- `lindos-drivers autodetect [--json]` — scan all relevant classes and return recommended packages:
  GPU (existing logic), **Broadcom Wi-Fi** (PCI ids → `bcmwl-kernel-source` / `broadcom-sta-dkms` /
  `firmware-b43-installer`, pick per chip, record mapping in `/usr/share/lindos/drivers/wifi.json`),
  and **audio** firmware (`sof-firmware`, `alsa-ucm-conf`, `firmware-sof-signed` where applicable).
- `lindos-drivers install --auto [--dry-run]` — install the `autodetect` recommendations for GPU +
  Wi-Fi + audio in one pass (root via the existing helper `install-drivers`; offline → exit 3).
- Installer and first boot (amended by SPEC §17): the **installer** installs the drivers while it installs
  the system (`target-config.sh`, step `drivers` of `/var/lib/lindos/install-state.json`): firmware and
  `ubuntu-drivers install --free-only` always; a **proprietary** GPU driver only with the user's consent (the
  installer's third-party-software checkbox `ubiquity/use_nonfree`, or `lindos.proprietary_drivers=1` on the
  kernel command line, recorded in `/var/lib/lindos/driver-proprietary-consent`) and **never** for a
  possibly-NVIDIA GPU when Secure Boot is enabled or unknown (a DKMS module would need a key enrolment,
  an interactive screen at the next start) — that case is recorded `skipped` and left to Settings.
  `/usr/lib/systemd/system/lindos-driver-firstboot.service` (oneshot; `ConditionKernelCommandLine=!boot=casper`
  and `!boot=live`, `ConditionPathExists=!/lib/systemd/system/oem-config.target`, first-run marker) remains
  only as a **silent** retry on the installed system: it reads install-state, does nothing when the step is
  `done`/`skipped`, and otherwise retries while online (after a bounded wait for NetworkManager) without any
  window, wizard or notification, never beyond the installer's consent. It no longer writes an install offer
  (nothing read it). Idempotent; never blocks boot; always exits 0.
- Non-free repo automation: enable `multiverse`/`restricted` and `ubuntu-drivers` metadata as needed
  (document); never auto-install proprietary drivers silently — the consent above (installer checkbox or
  kernel word, or an explicit `lindos-drivers install` / Settings action) always applies.

## 25. CI — real Linux builds on GitHub Actions (extend `.github/workflows/`)

Extend `.github/workflows/ci.yml` (or add `kernel.yml`) so the Ubuntu 24.04 runner does the real
Linux work this Windows host cannot:
- New `kernel` job (on `workflow_dispatch` input `build_kernel`, and on tags): install
  `bc bison flex libssl-dev libelf-dev dpkg-dev`, run `build/kernel/build-kernel.sh --version <from
  manifest> --jobs $(nproc)`, upload `out/kernel/linux-image-*.deb linux-headers-*.deb` as an
  artifact. Cache the kernel source tree. Keep it optional/slow.
- The `iso` job gains an optional step to download the `kernel` artifact into `out/kernel/` before
  `build-iso.sh` so `35-kernel.sh` installs the Lindos kernel; the ISO still builds if absent.
- Ensure the new `lindos-vm` and `lindos-winapps` packages are built by `build/mkdeb.sh all` and
  covered by lintian; new `packages/*/tests` run in both the Ubuntu and Windows pytest jobs.
- Document (in `docs/BUILDING.md`) that Lindos is built/verified on GitHub Actions (a bare-metal-ish
  Ubuntu runner): push to GitHub → Actions builds debs+kernel+ISO. This addendum does NOT push or
  create a remote (that is the user's action); it only provides the workflows.

## 26. Tests & acceptance
`bash tests/run.sh` stays green on Windows. New tests must cover: `lindos-vm` caps probe against a
faked `LINDOS_ROOT` (`/proc/cpuinfo`, `/dev/kvm`, `/sys/kernel/iommu_groups`), domain-XML generation
+ the **no-spoof** assertion (grep the XML/templates for `kvm=off`/`hv-vendor-id`/`smbios`/`acpitable`
/UUID-spoof tokens → none), `lindos-winapps` catalog + backend logic (guarded), driver `autodetect`
mapping (Broadcom/audio) against faked PCI data, and Proton system/user resolution order. Docs
`docs/VM.md`, `docs/WINAPPS.md`, `docs/DRIVERS.md` added and consistent with the CLIs; `ANTI-CHEAT.md`
updated to state the VM does not defeat VM-blocking anti-cheat and ships no spoofing.
