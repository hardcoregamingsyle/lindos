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
**Not supported on Lindos yet — and not possible on any Linux distribution today.** Valorant and
League use Riot Vanguard (a Windows-only kernel driver); Epic disabled Easy Anti-Cheat's Linux
support for Fortnite; Apex Legends disabled it in November 2024; Ubisoft, Bungie and Krafton never
enabled BattlEye's Linux support; Rust's developer refused; Call of Duty's Ricochet is a kernel
driver. Nothing on the Lindos side can change a publisher's decision. Lindos will list a game as
supported once its publisher enables Linux and it has been tested, but it cannot promise when — and
some publishers have said they will not. See
[ANTI-CHEAT.md](ANTI-CHEAT.md#0-what-not-supported-yet-means-and-does-not-mean),
[COMPATIBILITY.md](COMPATIBILITY.md) (56 titles) and
[areweanticheatyet.com](https://areweanticheatyet.com/).

Lindos does not pretend otherwise — it ships **no** attestation/TPM/Secure-Boot forger, no HWID or
CPUID spoofer, no VM-hiding trick, because none of that can produce the hardware-rooted signature
the anti-cheat server checks; it would only get the account **hardware-banned**. Instead, run
`lindos-game route <title>` for the honest way to actually play: an official cloud-streaming
service where the publisher offers one (GeForce NOW, Xbox Cloud Gaming, Boosteroid, Amazon Luna —
`lindos-game cloud install geforce-now` installs the official Flatpak), or a one-command,
one-shot restart into the PC's *own* Windows installation with `lindos-dualboot reboot-to-windows`
(a genuine UEFI `BootNext`/GRUB one-shot boot; it never edits Windows, BCD or firmware settings,
and clears itself after one boot). See [docs/DUALBOOT.md](DUALBOOT.md).

### Can I open MSIX/APPX apps, `.reg` files, PowerShell scripts, disk images, DOS programs?
Yes — `lindos-run` opens essentially every Windows file type, not just `.exe`/`.msi`: MSIX/APPX
packages and bundles, `.appinstaller` (download only on your consent, HTTPS-only, hash-verified),
`.msp` patches, `.reg` (Lindos previews what it will delete before merging — Wine's own regedit
never asks), `.ps1`/`.vbs` scripts, `.url` shortcuts, `.scr` screensavers, `.cpl` Control Panel
items, software `.inf`, `.cab` archives, `.iso`/`.img` disk images (AutoPlay-style "Run setup?"),
ClickOnce apps, and DOS/16-bit Windows programs (DOSBox-X). A file Lindos genuinely cannot run
(ARM-only binaries, drivers, Windows Update packages, Store-encrypted/true-UWP apps) is explained,
never silently "tried anyway". `./setup.exe` also works straight from a terminal. Details:
[docs/WINDOWS-FORMATS.md](WINDOWS-FORMATS.md).

### Can I install software by package name, like `winget`?
Yes: `lindos-compat winget search <name>` / `winget install <id>` downloads straight from the
publisher's own URL named in Microsoft's official winget manifest, verifies the SHA-256 before
running it (no override — a failed hash refuses to install), and runs it through the same
`lindos-run` flow as a double-clicked installer. See [docs/WINGET.md](WINGET.md).

### How do I bring my files, bookmarks and settings over from Windows?
`lindos-transfer` (or **Lindos Settings → Windows apps → Transfer from Windows…**) works two ways:
straight from the Windows partition on the same PC (mounted strictly **read-only** — nothing is
ever written to it) if you dual-booted or dual-installed, or from a "transfer folder" you make by
double-clicking the bundled `LindosTransfer.cmd`/`.ps1` kit on the old PC first (handy when
Windows is BitLocker-locked, on a different disk, or already gone). It brings over your files
(Desktop/Documents/Pictures/Music/Videos/Saved games), Chromium/Firefox bookmarks, wallpaper,
fonts, Wi-Fi networks (only from your own `netsh wlan export … key=clear`, opt-in), your Steam
library, and a list of your Windows programs with an honest way to get each one back. It never
opens Windows account/security databases, DPAPI/Credential-Manager/Vault data, browser
password/cookie stores, or hibernation/page/swap files. See [docs/TRANSFER.md](TRANSFER.md).

### Which games do work?
Native Linux builds (Counter-Strike 2, Dota 2, Minecraft Java, Stardew Valley, Terraria, War
Thunder), most single-player Steam titles through Proton (Elden Ring, Cyberpunk 2077, Baldur's
Gate 3, Hogwarts Legacy, Forza Horizon 5, GTA V story mode …) and multiplayer titles whose
developers enabled anti-cheat for Proton (Halo Infinite, Helldivers 2, Marvel Rivals, The
Finals, Dead by Daylight, Warframe, Sea of Thieves …). Check [ProtonDB](https://www.protondb.com/)
before buying; statuses change with publisher updates.

### Roblox?
Yes, through **Sober** — a community runtime for the official Android build — with your normal
account (the installer adds Sober when it is online; otherwise `lindos-game install sober` or Lindos
Settings › Gaming). The Windows Roblox client
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

### How does installing Lindos work?
Boot the USB stick and choose **Install Lindos**. The installer does everything heavy while it installs
(system updates, drivers, Google Chrome, Wine/Proton, the game launchers, the apps of every Mode); when it
says so, remove the stick and reboot; the first boot only asks for your account; then **Lindos Setup** asks
for a Mode, a browser and a few personal choices. The whole story: [INSTALLER.md](INSTALLER.md). Please note
that this flow is new and has not yet been run end to end on real hardware (same document, "Known
limitations").

### Where is my account created?
At the **first boot** of the installed system, not in the installer and not in Lindos Setup. Ubiquity's
*oem-config* account wizard asks for language, keyboard, time zone, your name, user name, password and the
computer name, then removes the temporary account the installer used. Lindos Setup starts afterwards, on
your first login, and only asks about Modes, browser and looks.

### Why does the installer ask for a temporary account (and a password)?
Lindos uses Ubiquity's *OEM mode* so that the installer can do its downloads first and the real account is
created at the first boot. Ubiquity always shows a page for the temporary account (named `oem`) and cannot be
told to skip it: leave the password empty and press Continue. The account is locked when the installation
ends and deleted at the first boot. Its wording ("OEM mode, for manufacturers only") is Ubiquity's own.

### Why did the installer download things? Does it need internet?
Some software cannot be on the disc: Google's Chrome licence forbids it, and Wine's newest builds, Steam,
the Flatpak apps (Prism, Sober, Heroic, Bottles) and system updates are far too large or change too often.
So the installer downloads them into your new system while it installs, once, from the vendors' own sources,
and says on its status line what it is doing. It needs an internet connection for that, but not to
*install*: see the next question. Nothing is sent about you (no telemetry).

### I installed offline. What is "pending"?
The install still completes; the installer records every download step it could not do as *pending* (or
*failed*, or *skipped* when it left something out on purpose, such as a proprietary driver without your
consent). See what is left with `lindos-config install-state`, or in **Lindos Settings › Apps › Left to
finish from setup**, where every item has an **Install now** button. Chrome and the drivers are also retried
**silently in the background** the first time the installed system is online (after the account wizard; no
window, no notification); everything else waits for you to press the button. Ubiquity's OEM mode does not copy
the live session's Wi-Fi profile, so connect on the account wizard's Wi-Fi page.

### Why does setup look different from the installer?
There are three programs, not one. The installer and the first-boot account screen are both Ubiquity (the same
code Linux Mint installs with; the account screen runs on its own before any desktop exists, where Lindos'
styling does not reach). **Lindos Setup** — the Mode, browser and looks wizard — is Lindos' own full-screen
program that runs inside your desktop after you have logged in for the first time. So the account screen looks
like the installer and not like Lindos Setup. Making the account screen look like Lindos is not done yet.

### Why does Lindos Setup still ask for my password?
When you press Apply it saves system-wide defaults for the Mode you chose, which needs administrator
rights; it asks **once** (one prompt for the whole batch). It installs and downloads nothing.

### Where is Edge / Chrome? Why only Firefox on the disc?
Microsoft's and Google's licences forbid redistributing Edge/Chrome inside an ISO. Firefox
(Mint's build) is on the disc; the **installer** downloads Google Chrome from Google's official apt
repository while installing (Chrome is the default). Edge is never installed for you: add it in Lindos Settings ›
Apps › Web browsers, or with `lindos-browser install edge --set-default`. If the install was offline, Firefox
is your browser until Chrome is added (silently, the first time you are online, or with **Install now** in
Settings › Apps); `lindos-browser install chrome --set-default` does it from a terminal.

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
traffic Lindos itself starts is downloading what it installs (the installer's downloads while
installing, and what you later ask for: updates, installs, GE-Proton downloads).

### Which NVIDIA driver do I get?
None on the disc (size, proprietary). The installer installs the free drivers and firmware always. It installs
an NVIDIA driver only if you consented (the installer's *Install multimedia codecs* checkbox, or the kernel
word `lindos.proprietary_drivers=1`) — and **not** while Secure Boot is on (or cannot be detected): a driver
built on your PC would need a key enrolment at the next start; the step is then shown as *skipped*. Otherwise,
or later: Lindos Settings → Hardware → Install drivers, or `lindos-drivers install --nvidia-open` (Turing and
newer) / `--nvidia-proprietary`, which follows `ubuntu-drivers`' recommendation and sets
`nvidia-drm.modeset=1`. Mint's Driver Manager is also there. Details: [DRIVERS.md](DRIVERS.md).

### Is Wine on the disc?
Ubuntu's `wine` + `wine32`, winetricks and cabextract are on the ISO (`INCLUDE_WINE=1`); WineHQ
staging and umu-launcher (Proton-GE for games) are downloaded by the installer while it installs, or
later if that was offline (Lindos Settings › Apps › Left to finish from setup, `pkexec
/usr/libexec/lindos/install-compat.sh`; `lindos-compat doctor` tells you what is missing). Steam
and Lutris are on the ISO too (`INCLUDE_STEAM=1`); Heroic, Prism, Sober and Bottles are Flatpaks the
installer adds (best effort) or you install on demand.

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

### Where does Linux Mint still show up?
Lindos is built on Linux Mint 22.x and Ubuntu 24.04, and does not hide that. What you may still see:
*Settings → About → Based on*; the **Software Sources** tool and `apt` output (`packages.linuxmint.com` is the
repository the base system updates from); the **Update Manager** and **Driver Manager** windows (Lindos has no
replacement for them; only their menu icons are Lindos's); the window title "Software Manager" inside
**Lindos Store**; the boot entry your firmware lists after an install (`linuxmint`, named after the
distribution ID Mint's tools rely on); and the Mint wallpapers if they could not be removed safely. Everything
else (the Welcome Screen, the boot menu, `lsb_release`, the login screen, menu text) is changed to say Lindos;
something else that still says Mint is a bug worth reporting. The full list and the reasons are in
[BUILDING.md](BUILDING.md) ("Mint sweep").

### How is the ISO built?
`make iso` (Ubuntu/Debian host with root) or `make iso-docker`: Mint 22.2 XFCE ISO → chroot
hooks → repacked hybrid BIOS+UEFI ISO. [BUILDING.md](BUILDING.md).
