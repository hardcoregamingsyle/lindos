# Restarting into Windows for games that need it

> **What this is.** A handful of games use a **kernel-mode Windows anti-cheat** (Riot Vanguard,
> Activision Ricochet, EA Javelin) that checks your PC's actual hardware — via **TPM 2.0** and
> **Secure Boot** — before it lets you play. That check can only ever pass on a **real Windows
> boot**, on the **real hardware**, with those firmware settings genuinely turned on. It cannot be
> faked from Linux, from a virtual machine, or by Lindos in any way — see
> [ANTI-CHEAT.md](ANTI-CHEAT.md) for why. `lindos-dualboot` does not try. It restarts your PC once
> into the Windows installation already sitting on your disk, using the exact same one-shot boot
> switch Windows itself uses for "Restart now" after an update. The next time you restart, the PC
> boots back into Lindos on its own — nothing is changed permanently, and Windows, its boot
> configuration and your firmware settings are never edited.

## 1. What it actually does (and does not do)

`lindos-dualboot` reads two things and acts on the first one that is available:

1. **UEFI one-shot boot** (`efibootmgr --bootnext`) — if your firmware already lists a
   **Windows Boot Manager** entry (it does, on almost every PC that dual-boots Windows and
   Linux), Lindos tells the firmware "use that entry for the *next* boot only." The firmware
   clears this setting itself the moment it is used, so the boot after Windows returns to Lindos
   automatically — you never have to remember to switch back.
2. **GRUB one-shot boot** (`grub-reboot`) — on older BIOS/MBR PCs, or if `efibootmgr` is not
   installed, Lindos instead tells **GRUB** (Lindos' own boot menu) to pick the Windows entry
   *once*. This only happens when GRUB can actually save that choice (see [§4](#4-when-it-refuses)
   below) — when it can't, restarting into Windows by hand from the GRUB menu is exactly as fast
   and does not carry the risk of getting stuck on Windows.

What it never does, by design: it never installs, downloads or activates Windows; never edits the
Windows Boot Configuration Data (BCD); never turns Secure Boot, TPM or any other firmware setting
on or off by itself (see [§3](#3-turning-on-secure-boot--tpm-firmware-setup)); never touches
BitLocker or asks for your Windows password; and it is not a virtual machine — the anti-cheat is
running on your actual hardware, on an actual Windows boot, which is exactly what it is designed
to check for.

Every actual privilege — reading the firmware boot list as root, and the reboot itself — happens
inside Lindos' polkit-guarded helper, which **re-checks everything itself** before doing anything:
it re-reads the firmware's boot entries (or GRUB's menu) fresh, and refuses unless the chosen entry
really is the Windows Boot Manager (identified by its loader path, not by a name that could be
faked) and really is still active. Nothing you pass to `lindos-dualboot` on the command line is
trusted blindly.

## 2. Commands

```
lindos-dualboot status [--json]
lindos-dualboot reboot-to-windows [--entry ID] [--yes] [--no-reboot]
lindos-dualboot firmware-setup [--yes]
lindos-dualboot shortcut
```

* **`status`** — shows the firmware type (UEFI/BIOS), the current Secure Boot and TPM state, every
  Windows entry Lindos found, and whether (and how) it can restart into Windows right now. Lindos
  Settings → *Gaming → Games that need Windows* renders this same information.
* **`reboot-to-windows`** — restarts the PC once into Windows. Asks for confirmation first (unless
  `--yes`); `--entry ID` picks a specific boot entry or GRUB menu id when there is more than one
  (for example, two Windows installs on different disks); `--no-reboot` sets the one-shot choice
  without actually restarting yet, in case you want to close other programs first.
* **`firmware-setup`** — restarts straight into the UEFI setup screen, so you can turn on Secure
  Boot or TPM for a game that needs it (see [§3](#3-turning-on-secure-boot--tpm-firmware-setup)).
  Prints the safety checklist below and asks for confirmation first.
* **`shortcut`** — writes a "Restart into Windows" launcher onto the Lindos application menu, so
  you don't need a terminal each time. `lindos-game shortcut <title> --route windows` (see
  [GAMING.md](GAMING.md)) writes a similar per-game shortcut that calls this underneath.

## 3. Turning on Secure Boot / TPM (`firmware-setup`)

Several of the games below only pass their hardware check once **Secure Boot** and/or **TPM 2.0**
are turned on in the PC's firmware — settings Windows itself cannot change from inside its own
Settings app either; they live one level below any operating system. Before you ask
`lindos-dualboot firmware-setup` to open that screen, three things matter, and Lindos prints all
three every time:

1. **Make sure Lindos can still boot afterwards.** Once Secure Boot is on, the firmware only runs
   *signed* boot loaders and kernels. Lindos' GRUB is signed already (via `shim`); the Lindos
   kernel itself needs to be **MOK-signed** first, or you should pick a stock signed kernel from
   the GRUB menu instead — `lindos-kernel secureboot status` tells you which applies. Skipping this
   step is the single most common way a dual-boot PC "won't boot Linux anymore" after turning
   Secure Boot on.
2. **Suspend BitLocker in Windows first, if it's on.** BitLocker ties itself to a measurement of
   your boot settings (Secure Boot and TPM are both part of that measurement). Flipping either one
   *while BitLocker is active* looks exactly like tampering to Windows, and it will demand your
   **48-digit recovery key** the next time Windows starts — the key you saved (or should save) to
   your Microsoft account, a USB drive or on paper. Suspending BitLocker first (in Windows:
   *Settings → Privacy & security → Device encryption*, or `manage-bde -protectors -disable C:` in
   an administrator prompt) avoids this entirely and re-enables itself automatically.
3. **The disk must already be UEFI + GPT, for both operating systems.** Some of these games'
   support pages tell Windows users to run `mbr2gpt` to convert their disk. **Never do this on a
   disk that also holds Lindos** — `mbr2gpt` only knows about Windows partitions, and it will
   corrupt or delete the Lindos ones. If your disk is still MBR, this needs a fresh, planned
   installation of both systems, not a firmware toggle.

## 4. When it refuses

`lindos-dualboot status` explains itself in the `why` field whenever it cannot offer a one-shot
restart, most commonly one of:

* **No Windows Boot Manager entry was found** — Windows was never installed alongside Lindos on
  this PC (or its boot entry was removed some other way). There is nothing to restart into; a
  Windows install is needed first.
* **`efibootmgr` is not installed** — Lindos Recommends it, but a minimal install might not have
  it; `sudo apt install efibootmgr` (or let Lindos Settings offer it) fixes this on any UEFI PC.
* **GRUB cannot save the one-shot choice** — `grub-reboot`'s selection lives in a small file
  (`grubenv`) that GRUB has to be able to write to. On **btrfs, zfs, LVM or software RAID
  (mdraid) `/boot`**, GRUB itself does not support writing it, so the "next boot only" promise
  could not be kept — every subsequent boot would go to Windows until you fixed it by hand. Lindos
  refuses outright rather than risk that; restarting and picking Windows from the GRUB menu
  yourself, once, is just as fast and has no such risk.

## 5. Specific games (checked 2026-09-26 — always confirm with the publisher; requirements change)

| Game | Needs on Windows | Notes |
|---|---|---|
| **Valorant** | Secure Boot **+** TPM 2.0 (Windows 11) | Riot Vanguard refuses to start without both; this is Riot's own requirement, not negotiable from Lindos' side. |
| **League of Legends** | TPM 2.0 (Windows 11); Secure Boot recommended | Vanguard also protects LoL on Windows 11; an optional "Vanguard Pre-Check" mode additionally wants IOMMU/VBS/HVCI if you opt in. |
| **Call of Duty** (Warzone, Black Ops 6/7, MW III) | Secure Boot **+** TPM 2.0 | Ricochet enrols the TPM (`enrollaik.exe`); without both you are placed in a separate matchmaking pool or blocked from Ranked, depending on the title. |
| **Battlefield 6** | Secure Boot **+** TPM 2.0 (hard requirement) | EA states plainly that Secure Boot must be on to play at all — this is not a soft warning like the others. |
| **Rainbow Six Siege** | Nothing for casual play; Secure Boot + TPM 2.0 + VBS for **Ranked "Legend Division"** | Ubisoft's newest anti-cheat tier gates the top ranked bracket specifically. |
| **Delta Force** | Secure Boot + TPM 2.0 (being rolled out) | Some players are prompted for both; the studio's own guide includes the same `mbr2gpt` steps to avoid on a dual-boot disk (see [§3](#3-turning-on-secure-boot--tpm-firmware-setup)). |
| **Fortnite** | Nothing for casual play; Secure Boot + TPM 2.0 (+ IOMMU) for **ranked tournaments** | Works fine casually on Lindos-hosted cloud too (see below) — dual-boot is only needed to enter tournaments. |
| **Apex Legends, GTA Online, Destiny 2, PUBG, Rust, Escape from Tarkov, Battlefield 2042** | Nothing beyond a normal Windows boot | These need Windows only because their publisher has not enabled Proton/Linux support (or, for GTA/Rust/Tarkov/2042, blocks virtual machines) — no Secure Boot/TPM prompt has been found for them. |

Every one of these is also in the compatibility matrix with an official **cloud-gaming**
alternative where one exists (`lindos-game route <title>` — see [GAMING.md](GAMING.md) and
[COMPATIBILITY.md](COMPATIBILITY.md)): GeForce NOW's native Linux app plays several of them
(Valorant is the notable exception — it is on no cloud service) without touching Windows at all.
Dual-boot is for the titles, or the modes (Ranked, tournaments), that cloud streaming does not
cover. On Lindos itself these games are marked **Not supported yet**: whether that ever changes is
up to their publishers, and Lindos gives no date — restarting into Windows is how you play them today.

## 6. See also

* [ANTI-CHEAT.md](ANTI-CHEAT.md) — why kernel-mode anti-cheat cannot run on Linux or in a VM, and
  why Lindos ships no spoofer/attester of any kind.
* [GAMING.md](GAMING.md) — `lindos-game route/play/shortcut`, cloud-provider details, and the full
  per-title routing logic that calls `lindos-dualboot` underneath.
* [KERNEL.md](KERNEL.md) — `lindos-kernel secureboot status`, MOK signing, and keeping Lindos
  bootable once Secure Boot is on.
* [COMPATIBILITY.md](COMPATIBILITY.md) — the generated per-title matrix this table is drawn from.
