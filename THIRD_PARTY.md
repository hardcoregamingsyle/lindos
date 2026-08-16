# Third-party components and their licences

Lindos' own code, packaging, artwork (wallpapers, logo, icons, plymouth theme) and documentation
are **GPL-3.0-or-later**. Everything below is somebody else's work and keeps its own licence.

## 1. Assets fetched by `build/fetch-assets.sh` (staged in `out/assets/`, installed into the ISO)

`build/fetch-assets.sh` is the only place the build downloads look-and-feel assets. It records
the exact commits / URLs / SHA-256 of what it fetched in `out/assets/assets.lock` and
`out/assets/SHA256SUMS`, and writes the same table below to `out/assets/THIRD_PARTY-assets.md`.
Licence files found in the upstream archives are copied next to the installed files.

| Asset | Upstream | Pinned ref (default) | Licence | Used as |
|---|---|---|---|---|
| Fluent-gtk-theme | https://github.com/vinceliuice/Fluent-gtk-theme | `master` (commit recorded in `assets.lock`; `FLUENT_GTK_REF` overrides) | GPL-3.0 | GTK 2/3/4 + xfwm4 themes, installed as `Lindos-Dark` / `Lindos-Light` (`install.sh -n Lindos -t default -c dark light --tweaks round`) |
| Fluent-icon-theme (incl. `cursors/`) | https://github.com/vinceliuice/Fluent-icon-theme | `master` (commit recorded; `FLUENT_ICON_REF` overrides) | GPL-3.0 | icon theme `Lindos`, `Lindos-dark`, `Lindos-light`; cursor themes `Fluent-cursors`, `Fluent-dark-cursors` |
| Selawik | https://github.com/microsoft/Selawik | release `1.01` (`Selawik_Release.zip`; `SELAWIK_URL` / `SELAWIK_SHA256`) | SIL Open Font License 1.1 | default UI font (`Selawik 10`), fontconfig alias target for "Segoe UI" (`/etc/fonts/conf.d/60-lindos-ui.conf`) → `/usr/share/fonts/truetype/selawik` |
| Inter | https://github.com/rsms/inter | release `v4.1` (`Inter-4.1.zip`, fallback `v4.0`; `INTER_URL` / `INTER_SHA256`) | SIL Open Font License 1.1 | fallback UI font → `/usr/share/fonts/truetype/inter` |

Notes
* `SELAWIK_SHA256` / `INTER_SHA256` are empty by default; the observed hashes are recorded and a
  warning is printed. Fill them in `build/config.env` (or the environment) for reproducible
  release builds.
* `--offline` re-uses a previous `out/assets/` checkout; `--verify-only` only checks the hashes.
* Nothing in `packages/lindos-desktop` downloads anything: the .deb only ships the fontconfig
  alias, xfconf defaults and Lindos' own artwork.

## 2. Packages taken from the Linux Mint / Ubuntu archives (on the ISO)

The base image is **Linux Mint 22.x XFCE** (Ubuntu 24.04 "noble"), used under the Linux Mint /
Ubuntu / Debian licences of the respective packages. `build/chroot/20-base.sh` adds, among
others: `xfce4-docklike-plugin`, `xfce4-panel-profiles`, `xfce4-clipman-plugin`,
`xfce4-whiskermenu-plugin`, `picom`, `systemd-zram-generator`, `earlyoom`,
`power-profiles-daemon`, `lm-sensors`, `fancontrol`, `gamemode`, `mangohud`, `python3-gi`,
`flatpak`, `winbind`, `cabextract`, `icoutils`, `zenity`, `librsvg2-bin` and, best effort,
`fonts-inter` (OFL-1.1), `fonts-jetbrains-mono` (OFL-1.1, monospace fallback), `fonts-noto-*`
(OFL-1.1), `fonts-liberation`. Each package's licence is in `/usr/share/doc/<pkg>/copyright`
on the installed system.

## 3. Third-party repositories and downloads configured or performed at build / run time

These are **not redistributed** inside Lindos packages; the ISO only carries the apt sources /
keys, or Lindos downloads them on the user's machine on request.

| Component | Where it comes from | When | Licence (upstream) |
|---|---|---|---|
| Wine (Ubuntu `wine`, WineHQ `winehq-staging`) | Ubuntu noble archive (on the ISO when `INCLUDE_WINE=1`); WineHQ apt repo `dl.winehq.org` (key + `.sources` added by `build/chroot/00-repos.sh`, package installed by `install-compat.sh` at OOBE / on demand) | ISO / OOBE | LGPL-2.1+ |
| winetricks, DXVK, VKD3D-Proton | Ubuntu archive / winetricks | on demand | LGPL-2.1+ / zlib / LGPL-2.1 |
| umu-launcher | GitHub release `Open-Wine-Components/umu-launcher` (pinned 1.4.4, SHA-256 verified) | `lindos-compat install-umu` / `install-compat.sh umu` | GPL-3.0 |
| GE-Proton | GitHub releases `GloriousEggroll/proton-ge-custom` (SHA-512 verified) | `lindos-proton update` | BSD-3-Clause + component licences |
| Steam | Valve apt repo `repo.steampowered.com` (key + list added by `00-repos.sh` / `install-gaming.sh`) | ISO when `INCLUDE_STEAM=1`, else on demand | proprietary (Steam Subscriber Agreement) |
| Lutris | Ubuntu archive (`lutris`) or Flathub | ISO when `INCLUDE_STEAM=1`, else on demand | GPL-3.0 |
| Heroic Games Launcher | GitHub release `Heroic-Games-Launcher/HeroicGamesLauncher` (.deb, pinned `HEROIC_VERSION`) or Flathub | on demand | GPL-3.0 |
| Prism Launcher, Sober, Vinegar, mcpelauncher, Bottles, OnlyOffice | Flathub (`org.prismlauncher.PrismLauncher`, `org.vinegarhq.Sober`, `org.vinegarhq.Vinegar`, `io.mrarm.mcpelauncher`, `com.usebottles.bottles`, `org.onlyoffice.desktopeditors`) | on demand (or ISO with `INCLUDE_FLATPAK_LAUNCHERS=1`) | Prism GPL-3.0 · Sober proprietary freeware (closed source, not affiliated with Roblox) · Vinegar GPL-3.0 · mcpelauncher GPL-3.0 · Bottles GPL-3.0 · OnlyOffice AGPL-3.0 |
| Microsoft Edge | `packages.microsoft.com/repos/edge` (key `microsoft.asc`) | OOBE / `lindos-browser install edge` | proprietary — **never on the ISO** |
| Google Chrome | `dl.google.com/linux/chrome/deb` (key `linux_signing_key.pub`) | OOBE / `lindos-browser install chrome` | proprietary — **never on the ISO** |
| Mozilla Firefox | Linux Mint's `firefox` .deb (Mozilla apt repo only with `ADD_MOZILLA_REPO=1`) | on the ISO | MPL-2.0 |
| NVIDIA driver | Ubuntu archive via `ubuntu-drivers` (`lindos-drivers install --nvidia-open` / `--nvidia-proprietary`) | on demand, never preinstalled | NVIDIA licence / MIT+GPL (open modules) |
| Kisak fresh Mesa PPA | Launchpad `ppa:kisak/kisak-mesa` | only with `KISAK_MESA=1` | MIT (Mesa) |
| nbfc-linux | GitHub release `nbfc-linux/nbfc-linux` (pinned 0.5.3 .deb) | `/usr/libexec/lindos/install-nbfc.sh` on explicit request | GPL-3.0 |
| xpadneo | GitHub `atar-axis/xpadneo` (dkms, pinned v0.9.6) | `lindos-drivers xpadneo install` | GPL-3.0 |
| xone (+ Microsoft wireless-adapter firmware) | GitHub `dlundqvist/xone` (dkms); firmware downloaded from Microsoft only with `--accept-firmware-license` | `lindos-drivers xone install` | GPL-2.0 (driver); firmware: Microsoft licence, user must accept |
| Microsoft core fonts (`ttf-mscorefonts-installer`) | Ubuntu multiverse installer | only with `ACCEPT_MSCOREFONTS_EULA=1` | Microsoft EULA (user must accept) |
| Feral GameMode, MangoHud, OpenRGB, CoreCtrl, Piper, GOverlay, AntiMicroX | Ubuntu archive | ISO (`gamemode`, `mangohud`) / on demand | GameMode BSD-3-Clause · MangoHud MIT · OpenRGB GPL-2.0 · CoreCtrl GPL-3.0 · Piper GPL-2.0 · AntiMicroX GPL-3.0 · GOverlay see upstream |

## 4. Trademarks

Windows, Microsoft Edge, Segoe, Google Chrome, Steam, Roblox, Minecraft, Adobe product names,
Linux Mint and Ubuntu are trademarks of their respective owners. Lindos is an independent
community project and is not affiliated with, endorsed by or sponsored by any of them. The
Lindos name, logo and wallpapers are original work.
