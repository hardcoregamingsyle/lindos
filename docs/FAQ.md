# FAQ

Short, honest answers to the questions Windows users ask first.

### Is Lindos Windows?
No. Lindos is **Linux Mint 22.x XFCE** (Ubuntu 24.04 base) with a Windows-11-style desktop, a
first-boot wizard, and integrated Wine/Proton so many Windows programs run. `/etc/os-release`
even keeps `ID=linuxmint` so Mint's tools and apt keep working; `PRETTY_NAME` says
`Lindos 1.0 (Aurora)`.

### Does it "run Windows apps natively"?
It runs `.exe`/`.msi` programs **through Wine and Proton** — translation layers, without a
virtual machine, at near-native speed. That is what "natively" means on this project, and we
always say so. Programs that need Windows kernel drivers (anti-cheat, some DRM), Windows-only
services (Microsoft 365 Click-to-Run, Creative Cloud) or DirectX features Wine lacks do not work.
[WINDOWS-APPS.md](WINDOWS-APPS.md) lists what we tested; `lindos-compat recipes list` on your PC
shows the same table.

### Can I play Valorant / Fortnite / League of Legends / Apex / Rainbow Six / Destiny 2 / PUBG?
**No — on no Linux distribution, Lindos included.** Valorant and League use Riot Vanguard (a
Windows-only kernel driver); Epic disabled Easy Anti-Cheat's Linux support for Fortnite; Apex
Legends disabled it in November 2024; Ubisoft, Bungie and Krafton never enabled BattlEye's Linux
support; Rust's developer refused; Call of Duty's Ricochet is a kernel driver. Nothing on the
Lindos side can change a publisher's decision. See [COMPATIBILITY.md](COMPATIBILITY.md) (56
titles) and [areweanticheatyet.com](https://areweanticheatyet.com/).

### Which games do work?
Native Linux builds (Counter-Strike 2, Dota 2, Minecraft Java, Stardew Valley, Terraria, War
Thunder), most single-player Steam titles through Proton (Elden Ring, Cyberpunk 2077, Baldur's
Gate 3, Hogwarts Legacy, Forza Horizon 5, GTA V story mode …) and multiplayer titles whose
developers enabled anti-cheat for Proton (Halo Infinite, Helldivers 2, Marvel Rivals, The
Finals, Dead by Daylight, Warframe, Sea of Thieves …). Check [ProtonDB](https://www.protondb.com/)
before buying; statuses change with publisher updates.

### Roblox?
Yes, through **Sober** — a community runtime for the official Android build — with your normal
account (`lindos-game install sober`, or tick it in the wizard). The Windows Roblox client
blocks Wine and does not run. Roblox Studio runs through **Vinegar** (partial). Sober and
Vinegar are not affiliated with Roblox Corporation.

### Minecraft?
Java Edition is native (Prism Launcher, `lindos-game install prism`; you sign in with the
Microsoft account that owns it). Bedrock has no Linux build; the unofficial `mcpelauncher`
runs the Android version you bought on Google Play (Marketplace/Realms may not work) — *partial*.

### Photoshop / Illustrator / Premiere / Office / AutoCAD?
Photoshop CS6 **works**; Photoshop and Illustrator CC 2021 are **partial** (editing works, no
Creative Cloud sign-in, GPU features or 2022+ releases); Premiere Pro, AutoCAD and Microsoft 365
Click-to-Run are **broken** — the recipes refuse to pretend and point to Kdenlive / DaVinci
Resolve, FreeCAD / LibreCAD, LibreOffice / OnlyOffice. Office 2016 (32-bit offline installer,
product key) works. Notepad++, 7-Zip, WinRAR, foobar2000 work; Paint.NET 4.2.16 partial.

### Where is Edge / Chrome? Why only Firefox on the disc?
Microsoft's and Google's licences forbid redistributing Edge/Chrome inside an ISO. Firefox
(Mint's build) is on the disc; the wizard installs Edge or Chrome from the vendors' official apt
repositories when you are online. Offline it falls back to Firefox and tells you how to add
them later: Lindos Settings › Apps › Web browsers, or `lindos-browser install edge --set-default`
(or `chrome`).

### How much RAM does it use?
Target **350–500 MB** idle (Lite ≈ 300–380 MB), measured as `free -m` "used" after login with
nothing open. Run `lindos-tune status` — it prints the verdict for your machine. Until the
reference measurements are published the per-measure savings in [RAM-BUDGET.md](RAM-BUDGET.md)
are estimates, and they say so.

### What are Modes and can I change my mind?
Everyday, Gaming, Work, Creator, Lite — bundles of taskbar pins, packages, services, governor,
zram and compositor. Switch any time in Lindos Settings → Lindos Mode or `lindos-mode set <id>`;
nothing is uninstalled when you leave a mode. [MODES.md](MODES.md).

### I have 4 GB of RAM / an old PC.
Pick **Lite**: no compositor, no animations, minimal tray, zram at 100 %, earlyoom at 8 %.
The wizard suggests it automatically on machines with ≤ 4 GB.

### Where is the Control Panel? Task Manager? Win+X?
Lindos Settings (`Super+I`), Task Manager `Ctrl+Shift+Esc` (`xfce4-taskmanager`), `Super+X`
power menu, `Super+E` File Explorer, `Super+L` lock, `Super+V` clipboard, `Super+Shift+S`
screenshot region. Full list: [KEYBOARD-SHORTCUTS.md](KEYBOARD-SHORTCUTS.md).

### Does Lindos phone home?
No telemetry, no ads, no crash uploads: `apport`, `whoopsie`, `kerneloops`, `ubuntu-report`
are disabled, and the config key `telemetry` (written by the wizard's *crash reports* toggle,
default off) has no consumer — nothing in Lindos sends anything anywhere. The only network
traffic Lindos itself starts is what you ask for (updates, installs, GE-Proton downloads).

### Which NVIDIA driver do I get?
None on the disc (size, proprietary). After install: Lindos Settings → Hardware → Install
drivers, or `lindos-drivers install --nvidia-open` (Turing and newer) / `--nvidia-proprietary`,
which follows `ubuntu-drivers`' recommendation and sets `nvidia-drm.modeset=1`. Mint's Driver
Manager is also there.

### Is Wine on the disc?
Ubuntu's `wine` + `wine32`, winetricks and cabextract are on the ISO (`INCLUDE_WINE=1`); WineHQ
staging and umu-launcher (Proton-GE for games) are downloaded during setup or later
(`pkexec /usr/libexec/lindos/install-compat.sh`, `lindos-compat doctor` tells you what is
missing). Steam and Lutris are on the ISO too (`INCLUDE_STEAM=1`); Heroic, Prism, Sober,
Bottles are installed on demand.

### Something broke — how do I report it?
`lindos-tune report -o report.md` produces a Markdown bug report (RAM, zram, top processes,
services, governor, kernel, mode, GPU, config). Attach it together with the relevant log from
`~/.local/state/lindos/` (`setup.log`, `settings.log`, `run-<program>.log`, `gamemode.log`) or
`/var/log/lindos/` (`helper.log`, `install-compat.log`, `install-gaming.log`, `tune.log`).

### Can I get the Mint look back?
Lindos changes theme, panel, shortcuts and defaults but keeps Mint's packages. Choose another GTK
theme in Settings → Personalization → Fonts (XFCE Appearance) / Window Manager, load a
different panel layout with `xfce4-panel-profiles`, or reset shortcuts in Keyboard settings.
The RAM tune is reversible piece by piece — see [RAM-BUDGET.md](RAM-BUDGET.md) §7.

### How is the ISO built?
`make iso` (Ubuntu/Debian host with root) or `make iso-docker`: Mint 22.2 XFCE ISO → chroot
hooks → repacked hybrid BIOS+UEFI ISO. [BUILDING.md](BUILDING.md).
