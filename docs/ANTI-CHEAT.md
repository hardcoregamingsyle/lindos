# Anti-cheat on Lindos — the honest version

> **The one-paragraph answer.** Lindos runs Windows games through **Wine/Proton**, a translation
> layer, not Windows. Whether a *multiplayer* game runs is decided entirely by its **anti-cheat
> vendor and the publisher**, never by Lindos. Games whose publisher **enabled** Easy Anti-Cheat
> or BattlEye for Proton (Elden Ring, Halo Infinite, Marvel Rivals, Dead by Daylight, …) work well.
> Games with a **kernel-mode Windows anti-cheat** (Valorant's Vanguard, Call of Duty's Ricochet,
> Battlefield's Javelin) or whose publisher **chose not to enable** their anti-cheat's Linux
> support (Fortnite, Apex Legends, GTA Online, Rainbow Six Siege, Destiny 2, PUBG, Rust) **do not
> run on any Linux distribution today, including Lindos — so Lindos does not support them yet.**
> Nothing on the Lindos side can change that; only the publisher can (see
> [§0](#0-what-not-supported-yet-means-and-does-not-mean)).

This document is the canonical, binding explanation for the honesty rules in
[`SPEC.md`](../SPEC.md) §0.1 and [`SPEC-KERNEL.md`](../SPEC-KERNEL.md) §14. It must stay consistent
with the compatibility matrix (`packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json`,
rendered as [COMPATIBILITY.md](COMPATIBILITY.md)); the games named as *not possible* below are the
same `not_possible` entries in that matrix.

## 0. What "Not supported yet" means and does not mean

Lindos Settings, `lindos-game`, the game list and the docs mark these titles **Not supported yet**.
The wording is written once, in the `disclaimer` block of `compat-matrix.json`, and every surface
reads it from there:

> Some online games use an anti-cheat that does not run on Linux today: Valorant, League of
> Legends, Fortnite, Call of Duty, Apex Legends, Rainbow Six Siege, Destiny 2, PUBG, Rust, GTA
> Online and a few more. Lindos does not support these games yet. Whether they ever will be is
> decided by their publishers, not by Lindos: some use a Windows kernel driver that has no Linux
> version, and others could run on Linux but their publisher has not turned that on. When a
> publisher enables Linux, Lindos will list the game as supported once it has been tested. We
> cannot promise when, and some publishers have said they will not. Until then you can play these
> games through the official cloud-streaming service where the publisher offers one, or by
> restarting into your PC's own Windows (run `lindos-game route` with the game's name to see what
> applies to your PC).

* **It is a statement about today, not a schedule.** "Yet" is not a timetable: Lindos gives no date,
  because the date is the publisher's. Are We Anti-Cheat Yet? currently lists most of these titles as
  *Denied* and only Escape from Tarkov as *Planned*.
* **What Lindos does when a publisher enables Linux:** the entry in `compat-matrix.json` is changed
  from `not_possible` to `works` (or `partial`) after a maintainer has checked it against the
  publisher's announcement and Are We Anti-Cheat Yet? and tested it, and the change ships as a
  `lindos-gaming` package update, delivered like any other Lindos update (see
  [UPDATES.md](UPDATES.md): until a Lindos update repository is hosted, that means a new ISO or a
  sideloaded package). Until that entry changes, the title stays **Not supported yet**.
* **What Lindos will not do:** ship a workaround, an "anti-cheat helper" or a spoofer to bring the
  date forward (§3), or offer the Lindos VM for it (§3.1).
* **The Xbox app / PC Game Pass** is listed as *Not possible* but is **not** marked "Not supported
  yet": it is blocked by Microsoft Store licensing, not by an anti-cheat, and nothing suggests that
  will change.

## 1. Why kernel-level anti-cheat cannot work on Linux

A "kernel-level" anti-cheat is a **Windows kernel-mode driver** (a `.sys` module) that the game
loads into the Windows kernel — Ring 0, the most privileged level — to watch memory, processes and
other drivers from below the game. Riot **Vanguard** (Valorant, League of Legends), Activision
**Ricochet** (Call of Duty), EA **Javelin** (Battlefield 6) and Tencent/ACE (Delta Force) are of
this kind.

Linux does not run Windows kernel drivers. There is no Windows kernel on a Lindos machine for such
a driver to load into — the kernel is Linux. Wine and Proton translate **user-space** Windows API
calls into Linux system calls; they are, by design, not a Windows kernel and cannot host a Windows
`.sys` driver. So a Vanguard-class anti-cheat has nothing to attach to and refuses to start, which
means the game refuses to start. This is a structural fact about the two operating systems, not a
missing feature Lindos could add, and not something a newer or "tuned" kernel changes. The
[Lindos kernel](KERNEL.md) is faster (ntsync, sched_ext, 1000 Hz); it is still a Linux kernel and
runs no Windows driver. For the games that rely on such a driver, that means they are not supported
on Lindos yet; whether that ever changes is up to their publishers, and no date is given.

The same games are equally blocked on **Steam Deck** (which is Linux) — a useful sanity check: if
Valve's own hardware cannot run it, neither can Lindos.

## 2. Why "publisher enabled it" is the whole game for EAC and BattlEye

**Easy Anti-Cheat (EAC)** and **BattlEye** are different: Epic (which owns EAC) and BattlEye both
ship a **Proton/Linux runtime**, and both added a switch a developer can flip to make their
anti-cheat accept Proton clients. When the publisher flips it, the game works on Linux with normal
performance. When the publisher does **not** flip it — even though the runtime exists — the game
does not run on Linux. It is a one-line business decision on the publisher's side, and it can be
turned on or off with a single update.

That is why the matrix has EAC titles in **both** columns:

* **Enabled → works:** Elden Ring, Halo Infinite, Marvel Rivals, The Finals, Dead by Daylight,
  Sea of Thieves, Hunt: Showdown 1896, Squad, Lost Ark, New World: Aeternum. Helldivers 2
  (nProtect GameGuard) and Warframe (Digital Extremes' in-house anti-cheat) are enabled for Proton
  the same way.
* **Not enabled → not possible on Linux today, so not supported on Lindos yet:** Fortnite (Epic
  disabled it for its own game), Apex Legends (EA/Respawn disabled Linux in November 2024), Rust
  (Facepunch refused, October 2022), Escape from Tarkov (Battlestate has not enabled BattlEye's Linux
  runtime), GTA Online, Rainbow Six Siege, Destiny 2 and PUBG (publisher never enabled the Linux
  runtime).

None of these are Lindos limitations. The fix, in every case, is for the publisher to enable Linux
support — which is why the honest advice is to **check before you buy**, not to look for a
workaround.

## 3. Lindos ships no attester, spoofer or evader — and why one cannot exist

Lindos contains **no** "Windows attester", attestation/TPM/EK-certificate forger, Secure-Boot or
HVCI/PatchGuard faker, HWID spoofer or VM-detection hider — and never will. This is a hard rule
([SPEC-KERNEL.md](../SPEC-KERNEL.md) §14), not a roadmap item.

Such a tool cannot work, because of what it is being asked to fake:

* **Hardware-rooted attestation** (TPM quotes, EK certificates, Secure Boot measurements) is a
  cryptographic signature produced by a key that is **fused into your physical TPM/CPU at
  manufacture** and never leaves it. Software cannot compute that signature; it does not hold the
  key. A "forger" can only send a **made-up** value, and the anti-cheat server checks it against
  the manufacturer's certificate chain — so the fake is detected on the first packet.
* Modern anti-cheats (EA Javelin, and Vanguard on Windows 11) require Secure Boot and TPM 2.0
  precisely so that the measurement is **rooted in hardware you cannot forge in software**. Trying
  to spoof it does not grant access; it flags the account as tampering with the anti-cheat.

The result of running a spoofer is not "the game works." It is a **hardware ban**: the anti-cheat
records your machine's real hardware identifiers and bans the *hardware*, so a new account on the
same PC is banned too. You would break the game you were trying to play, permanently, on that
computer. That is why shipping or documenting such a tool would actively harm Lindos users, and why
Lindos refuses to. If a request ever asks for one, the honest and only correct answer is: it does
not work, do not attempt it.

### 3.1 A virtual machine is still a VM — the `lindos-vm` route does not defeat VM-blocking anti-cheat

Lindos ships an honest Windows virtual machine, [`lindos-vm`](VM.md), for the Windows software
Wine/Proton cannot run. It is worth being just as plain about what it is **not**: running a game
inside `lindos-vm` does **not** get you past anti-cheat that blocks virtual machines.

* Kernel-mode anti-cheat that refuses to run in a VM — Riot **Vanguard** (Valorant, League of
  Legends), and titles whose publisher disabled EAC/BattlEye's Linux runtime — detects that it is in
  a VM and refuses to start. That is the anti-cheat working as designed, not a bug Lindos can patch.
* `lindos-vm` is a **truthful, standard** Windows VM. It ships **no hypervisor hiding** (`kvm=off`,
  `hv-vendor-id` masquerading, hiding the hypervisor CPUID bit), **no SMBIOS / motherboard-UUID /
  serial spoofing**, **no fake ACPI tables**, and no CPUID/vendor/TPM/HWID forging. The generated
  libvirt XML and every template use standard, truthful values — a unit test greps them and fails the
  build if any evasion token ever appears.
* "Hiding" the VM from anti-cheat is the same losing move as §3's spoofer, for the same reason. It
  cannot produce hardware-rooted signatures, it only escalates a soft refusal into a **hardware ban**,
  and it is a cheat/evasion tool. Lindos will not implement it.

So `lindos-vm` is for Windows **software** and **VM-tolerant** games (Adobe, Office, most
single-player titles) — not for defeating anti-cheat. For those blocked multiplayer titles the honest
options are unchanged: a native/single-player alternative, cloud streaming, or a Windows dual-boot.

## 4. What actually works — and where to run it fast

Everything that is *allowed* to run, Lindos makes run well. The [Lindos kernel](KERNEL.md) (ntsync
for Wine/Proton sync, sched_ext low-latency schedulers, 1000 Hz preempt) and the Proton stack
(GE-Proton via `lindos-proton`, DXVK/VKD3D-Proton, gamescope, MangoHud, per-title profiles) help
every one of these:

| Category | Examples | How |
|---|---|---|
| **Native Linux builds** | Counter-Strike 2, Dota 2, Stardew Valley, Terraria, War Thunder | Steam / native client — no anti-cheat problem at all |
| **Single-player through Proton** | Cyberpunk 2077, Elden Ring, Baldur's Gate 3, Hogwarts Legacy, GTA V Story Mode | Steam Play or Heroic; no anti-cheat, or the single-player half has none |
| **Multiplayer, publisher enabled EAC/BattlEye** | Halo Infinite, Marvel Rivals, The Finals, Dead by Daylight, Apex-*style* shooters that opted in | Steam/Proton or Heroic — works because the publisher flipped the switch |
| **Server-side anti-cheat (tolerant of Wine)** | Overwatch 2, Diablo IV, World of Warcraft (Battle.net) | Lutris / Proton; Blizzard's Warden runs server-side and tolerates Wine |
| **Community runtimes** | Roblox (Sober), Genshin Impact (AAGL) | not the Windows client; see the caveats in the matrix |

## 5. How to check any game yourself

Statuses change with a single publisher update, so **always verify before buying**:

1. **[Are We Anti-Cheat Yet?](https://areweanticheatyet.com/)** — the definitive per-game
   anti-cheat status (Supported / Running / Broken / Denied / Planned). This is the first place to
   look for any multiplayer title.
2. **[ProtonDB](https://www.protondb.com/)** — community reports and per-game tweaks for Steam
   titles (search by name or Steam AppID).
3. **Lindos' own snapshot** — `lindos-compat doctor` checks your Wine/Proton/Vulkan stack, and
   Lindos Settings → Gaming → Compatibility renders [COMPATIBILITY.md](COMPATIBILITY.md) (the same
   `compat-matrix.json` data). Treat it as a snapshot, cross-checked against the two sites above.

If a game you want is listed as `not_possible` (shown as **Not supported yet**), that is the
publisher's decision and there is no Lindos-side workaround — the honest options are a native or
single-player alternative, official cloud streaming (`lindos-game route <title>` — GeForce NOW's
native Linux app, Xbox Cloud Gaming in Chrome/Edge, Boosteroid or Amazon Luna, whichever the title
actually offers in your region), or a Windows dual-boot for that one game (`lindos-game play <title> --route windows`). Lindos will not
pretend otherwise. See [GAMING.md §7](GAMING.md#7-play-anywhere--honest-routes-for-blocked-titles)
for the full `lindos-game route/play/shortcut/cloud` command set.

## 6. See also

* [KERNEL.md](KERNEL.md) — the Lindos kernel: what it enables and, explicitly, what it does not.
* [GAMING.md](GAMING.md) — launchers, GE-Proton, gamescope, MangoHud, controllers.
* [COMPATIBILITY.md](COMPATIBILITY.md) — the generated per-title matrix (do not edit by hand).
* [WINDOWS-APPS.md](WINDOWS-APPS.md) — `lindos-run`, prefixes and the `riot-client` /
  `roblox-player` recipes that document why they are `broken`.
* [VM.md](VM.md) — the honest `lindos-vm` Windows VM: what it is for, and why it does not defeat
  VM-blocking anti-cheat (no hypervisor hiding, no spoofing).
