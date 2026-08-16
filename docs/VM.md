# lindos-vm — an honest Windows virtual machine

> **What it is, in one paragraph.** `lindos-vm` builds a **real, standard** Windows virtual machine
> with KVM/QEMU and libvirt: q35 machine type, OVMF/UEFI firmware, virtio disk and network, SPICE
> display by default, and — for people with the hardware — **optional GPU passthrough** (VFIO) for
> near-bare-metal graphics. It is the honest way to run Windows-only software that Wine/Proton
> cannot: a genuine Windows kernel, running in a window (or on a passed-through GPU) next to your
> Lindos desktop. You bring a **licensed Windows** ISO; Lindos never downloads Windows, never
> stores your Windows credentials, and never touches activation.

This document is binding-consistent with [`SPEC-VM.md`](../SPEC-VM.md) §20–§21 and the honesty rules
in [`SPEC.md`](../SPEC.md) §0.1 / [`SPEC-KERNEL.md`](../SPEC-KERNEL.md) §14.

## 1. Honesty — read this first

`lindos-vm` is a **truthful** VM. It ships, generates and documents **no** anti-cheat or
VM-detection evasion of any kind:

* no hypervisor hiding (`kvm=off`, `hv-vendor-id` masquerading, hiding the hypervisor CPUID bit);
* no SMBIOS / motherboard-UUID / serial spoofing, no fake ACPI tables;
* no CPUID/vendor forging, no TPM-EK / attestation forging, no HWID evasion.

The generated libvirt XML and every template and config use standard, truthful values. A unit test
(`tests/test_no_spoof.py`) greps the generated XML, the templates and the configs and **fails the
build** if any of those tokens ever appear.

**Therefore a VM does not defeat VM-blocking anti-cheat.** Kernel-mode anti-cheat that refuses to
run inside a virtual machine — Riot **Vanguard** (Valorant, League of Legends), and titles whose
publisher disabled EAC/BattlEye's Linux runtime — will **not** run in `lindos-vm`, and Lindos does
nothing to change that. A VM presents itself honestly as a VM; these anti-cheats block VMs on
purpose. `lindos-vm` is for **Windows software and VM-tolerant games** (Adobe, Office, most
single-player titles), not for getting around anti-cheat. Attempting to spoof your way past it does
not grant access — it gets your real hardware banned. See [ANTI-CHEAT.md](ANTI-CHEAT.md).

**You supply Windows.** `lindos-vm create --windows-iso …` takes a Windows ISO **you** provide from
a licensed source. Lindos does not fetch it, does not enter your product key, and does not bypass
activation. Install and activate Windows inside the VM exactly as you would on real hardware.

## 2. The package

`lindos-vm` (`Architecture: all`) depends on `python3` and `lindos-core`, and **Recommends** the
real virtualization stack so a normal install pulls it in:

* `qemu-system-x86 | qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`, `ovmf` (UEFI firmware),
  `virt-manager` (a full GUI, if you prefer clicking), `virtiofsd` (shared folders);
* **Suggests** `looking-glass-client` (low-latency framebuffer view for single-GPU passthrough).

`postinst` adds the invoking user to the `libvirt` and `kvm` groups **only if those groups exist**
(`getent`) and never fails the install. Log out and back in for the group change to take effect.

## 3. Host requirements — `lindos-vm check`

`lindos-vm check [--json]` probes the host and prints a plain **"ready / not ready + why"** summary.
It reads everything through `LINDOS_ROOT`, so it is testable and never guesses. It reports:

| Probe | Source | Why it matters |
|---|---|---|
| CPU vendor + virtualization extension | `vmx` (Intel) / `svm` (AMD) in `/proc/cpuinfo` | KVM needs hardware virtualization |
| `/dev/kvm` present | device node | the kernel KVM module is loaded and usable |
| IOMMU enabled | kernel cmdline `intel_iommu=on` / `amd_iommu=on` **and** a populated `/sys/kernel/iommu_groups/` | required for **GPU passthrough** only |
| GPUs + their IOMMU groups | `lspci` / sysfs | which GPU can be isolated for passthrough |
| Total / free RAM | `/proc/meminfo` | enough to give the guest without starving the host |
| OVMF / libvirt / qemu installed | package/file probe | the tools the create/start flow needs |

A plain VM (SPICE display, no passthrough) needs only CPU virtualization + `/dev/kvm` + qemu/OVMF.
IOMMU and isolated GPU groups matter **only** if you want GPU passthrough.

## 4. The CLI

`lindos-vm` uses argparse; **reads** support `--json`. It **never calls `sudo`**: any step that
needs root is printed as an exact, copy-pasteable `pkexec …` / `virsh …` command when you are not
already root. Exit codes: **0** ok · **1** error · **2** usage · **3** host-not-capable.

```
lindos-vm check [--json]
lindos-vm setup [--single-gpu | --dual-gpu] [--dry-run]
lindos-vm create --name N --windows-iso PATH [--disk-gb 64] [--ram-gb 8] [--cpus N]
                 [--gpu auto|PCI] [--virtio-iso PATH]
lindos-vm start | stop | status <name>
lindos-vm list
lindos-vm passthrough list | bind <pci> | unbind <pci>
lindos-vm --version
```

## 5. Setup flow (passthrough only) — `lindos-vm setup`

For a plain windowed VM you can **skip setup entirely** and go straight to `create`. `setup` exists
for **GPU passthrough**, where the host must isolate a GPU from its own drivers and hand it to VFIO.
`setup` **prints a plan; it never auto-applies** anything privileged:

* `--dual-gpu` — you have a second GPU (or an iGPU) to keep the Lindos desktop on. The plan is the
  IOMMU grub edit, the `vfio-pci ids=…` line for the passthrough GPU, and the initramfs update.
* `--single-gpu` — you have one GPU. The plan additionally installs the libvirt hook (§7) that hands
  the GPU to the guest on start and gives it back on shutdown.
* `--dry-run` — print only, change nothing.

Where the base OS keeps the kernel command line, `setup` **delegates the IOMMU edit to
`lindos-kernel cmdline set`** when that package is present (it writes the marker-fenced
`/etc/default/grub.d/50-lindos.cfg`), otherwise it prints the exact grub edit. Every privileged step
is shown as a `pkexec …` you run yourself.

## 6. Create and run

```
lindos-vm create --name win11 --windows-iso ~/ISOs/Win11_24H2.iso --disk-gb 100 --ram-gb 12 --cpus 6
lindos-vm start  win11
lindos-vm status win11
lindos-vm stop   win11
```

`create` renders `/usr/share/lindos/vm/win.xml.template` into
`~/.local/share/lindos/vm/<name>.xml` and `virsh define`s it. The domain is a **standard** VM
description: q35, OVMF UEFI, a virtio system disk (a qcow2 of `--disk-gb`), virtio network, and by
default a **SPICE / qxl** display you view with `virt-viewer` or `virt-manager`. There is no
hypervisor hiding and no SMBIOS/UUID/ACPI/CPUID spoofing anywhere in it.

* `--virtio-iso PATH` attaches the **virtio-win** driver ISO as a second CD so the Windows installer
  can load the virtio storage/network drivers (Windows has none built in). You supply this ISO too;
  it is Red Hat's freely redistributable driver disc.
* `--gpu auto|PCI` swaps the SPICE display for a VFIO **host device** (your passthrough GPU) — use it
  only after `setup` and a reboot, when `check` shows the GPU isolated in its own IOMMU group.

Inside the running VM you install Windows from your ISO and activate it normally. For seamless
individual Windows apps on your Lindos desktop (Photoshop, Office) over RDP, point
[`lindos-winapps`](WINAPPS.md) at this VM (its default backend VM name is `RDPWindows`).

## 7. Single-GPU passthrough and the libvirt hook

Single-GPU passthrough means the machine has exactly one GPU, so starting the VM must **take the
display away from Lindos** and give it back on shutdown. `lindos-vm setup --single-gpu` installs
`/etc/libvirt/hooks/qemu` (a conffile) implementing the well-known pattern:

* on `prepare/begin`: stop the display manager, unbind the GPU/console from the host DRM/framebuffer,
  bind `vfio-pci`;
* on `release/end`: reverse it — rebind the host driver and restart the display manager.

The hook is guarded and idempotent, **no-ops for any domain it does not manage**, logs to
`/var/log/lindos/vm-hook.log`, and contains **no** detection-evasion (no `kvm=off`, no SMBIOS/ACPI
spoof). During a single-GPU session your Lindos desktop is gone until the VM shuts down; a
`looking-glass-client` view or a second machine is the usual way to keep working. Dual-GPU
passthrough avoids all of this — the host keeps its own GPU — and is much easier to live with.

## 8. `passthrough` — bind/unbind by hand

`lindos-vm passthrough list` shows PCI devices and their IOMMU groups; `bind <pci>` / `unbind <pci>`
attach or detach a device to/from `vfio-pci`. Binding is a privileged action, so when you are not
root the tool prints the exact `pkexec …` command rather than running it. Remember that VFIO isolates
a whole IOMMU group — everything in the group moves together.

## 9. Performance and limits (honest estimates)

* **CPU/RAM inside KVM is near-native** for compute; whole-VM overhead is small. Exact numbers
  depend on your hardware — these are estimates, not benchmarks Lindos measured for you.
* **Graphics without passthrough** (SPICE/qxl) is fine for desktop apps and 2D, **not** for demanding
  3D — there is no real GPU in the guest.
* **Graphics with passthrough** is near-bare-metal because the guest drives a real GPU, at the cost of
  the setup above and (single-GPU) losing the host display during the session.
* A VM uses real RAM and disk for the guest. Give it enough without starving the host (`check` shows
  free RAM); a Windows 11 guest wants roughly 8 GB+ and 64 GB+ of disk as a comfortable estimate.
* **Anti-cheat that blocks VMs stays blocked** (§1). No setting in `lindos-vm` changes that, by design.

## 10. See also

* [WINAPPS.md](WINAPPS.md) — run individual Windows apps (Adobe/Office) seamlessly over RDP against
  this VM.
* [ANTI-CHEAT.md](ANTI-CHEAT.md) — why a VM does not defeat VM-blocking anti-cheat, and why Lindos
  ships no spoofer.
* [WINDOWS-APPS.md](WINDOWS-APPS.md) — the **no-VM** path (Wine/Proton) for `.exe`/`.msi`, which is
  what most software should use first.
* `virt-manager` — the full libvirt GUI, installed alongside `lindos-vm`, for anything the CLI does
  not cover.
