# Lindos — Engineering Specification (v1.0 "Aurora")

> **This file is the contract.** Every package, script and document in this repository must
> follow the names, paths, formats and APIs defined here. If you need something that is not
> here, add it here first, then implement it.

## 0. What Lindos is (one paragraph)

Lindos is a remaster of **Linux Mint XFCE** (Ubuntu LTS base, currently Mint 22.x / Ubuntu 24.04
"noble") that gives Windows 11 users a familiar, beautiful, dark-by-default desktop; runs Windows
programs (`.exe`/`.msi`) through an integrated Wine/Proton compatibility layer; ships gaming
launchers and drivers pre-wired (Steam/Proton, Lutris, Heroic, Prism/Minecraft, Sober/Roblox);
and is tuned to idle at **~350–500 MB RAM** with no bloatware. Installing is one step: the
USB session is only the installer, and the installer does everything heavy while it installs —
system updates, drivers, Google Chrome, the Wine/Proton layer, the game launchers and the apps
of every Mode (§17). The first boot of the installed system only asks for the account (Ubiquity's
oem-config wizard); at the first login a Windows-style OOBE ("Out-Of-Box Experience") lets the
user pick a **Mode** (Everyday / Gaming / Work / Creator / Lite) and a **browser** (Google Chrome /
Mozilla Firefox, plus Microsoft Edge when it is installed) and personalise the desktop — it saves
configuration only and never installs or updates anything (§6). Both choices can be changed later
in **Lindos Settings**.

### 0.1 Honesty rules (must be reflected everywhere, incl. UI and docs)
- "Runs Windows apps natively" means *without a VM*, through Wine / Proton (translation layer,
  near-native speed). Do not claim it is Windows.
- Anti-cheat reality: **Valorant (Vanguard) and Fortnite (EAC-Linux disabled by Epic) do NOT run
  on any Linux, including Lindos.** Say so plainly, and present it as **"Not supported yet"** —
  never "coming soon", never a date: whether a title ever runs on Linux is its publisher's
  decision, and Lindos lists it as supported only once its publisher enables Linux and it has been
  tested. The wording is the `disclaimer` object of `compat-matrix.json` (SPEC-WINDOWS §30.1);
  `docs/ANTI-CHEAT.md` §0 quotes it verbatim (`tests/test_anticheat_disclaimer.py`). Roblox runs
  through **Sober** (not the Windows client). Minecraft Java runs natively; Bedrock via `mcpelauncher` (unofficial). Steam games
  depend on the developer enabling anti-cheat for Proton — link to areweanticheatyet.com and
  protondb.com. Adobe: CC 2019–2021 era Photoshop/Illustrator work through Wine recipes; newer
  releases are unreliable — mark **partial**.
- Edge and Chrome licences forbid redistribution inside the ISO — the ISO/live image must never
  contain the browser binary itself, and building the ISO on CI counts as redistribution too, so
  neither is ever installed at **build** time. Only their official apt repository + signing
  keyring (freely redistributable pointers, same as the WineHQ/Steam repos) may be pre-staged on
  the image. Firefox is on the ISO. Chrome is the system default: the **installer** downloads it
  from Google's official apt repository into the new system while it installs (§17; never in the
  live session, never at build time). If that was not possible (offline, a timeout, a failure) the
  step is recorded as *pending* in `/var/lib/lindos/install-state.json`, and
  `lindos-browser-firstboot.service` retries silently on the **installed** system once it is
  online (no window, no wizard, never in the live/ISO session, never blocks boot, retries later if
  still offline); Lindos Settings › Apps shows the same item with an **Install now** button. Edge
  is never installed automatically; it and Firefox remain selectable in Settings any time. Both
  Edge and Chrome have a clean offline fallback to Firefox + "install later" notice.
- The installer is as honest as the rest: it says on its status line what it is downloading; it
  records a step as `done` only after verifying the result (never on hope), and everything it could
  not do stays visible as *pending*/*failed* (Settings › Apps, `lindos-config install-state`). It
  never installs a proprietary GPU driver without consent, and never one that would need a
  Secure-Boot key enrolment at the next start (§17.5).
- RAM: idle target is measured as `free -m` "used" after login into XFCE with no apps open. State
  the target as **350–500 MB** and ship the measurement tool (`lindos-tune status`).

## 1. Repository layout

```
Lindos/
├── SPEC.md                     ← this contract
├── README.md                   ← product README (vision, features, honesty section, quickstart)
├── LICENSE                     ← GPL-3.0-or-later for Lindos code; THIRD_PARTY.md for others
├── THIRD_PARTY.md              ← licences of fetched assets (Fluent theme, icons, fonts, …)
├── Makefile                    ← `make debs`, `make iso`, `make test`, `make qemu`, `make clean`
├── build/                      ← ISO remaster + .deb builder (runs on Ubuntu/Debian host or Docker)
│   ├── config.env              ← BASE_ISO_URL, BASE_ISO_SHA256, LINDOS_VERSION, CODENAME, …
│   ├── build-iso.sh            ← main entry: fetch → unpack → chroot hooks → repack (EFI+BIOS)
│   ├── mkdeb.sh                ← builds packages/<name> into out/debs/<name>_<ver>_all.deb
│   ├── chroot/NN-*.sh          ← ordered hooks executed INSIDE the chroot (see §8)
│   ├── overlay/                ← files copied verbatim onto ISO root (grub.cfg, loopback.cfg, …; the BIOS
│   │                              isolinux/live.cfg is generated from grub.cfg by lib/boot_menu.py, §17.1)
│   ├── lib/                    ← build helpers: boot_menu.py, installer_extras.py, verify_oem_pool.py, …
│   ├── installer/              ← slideshow + GTK skin staged for chroot/78-installer-brand.sh
│   ├── Dockerfile              ← ubuntu:24.04 + xorriso squashfs-tools … ; `make docker-iso`
│   ├── test-qemu.sh            ← boot out/lindos-*.iso in QEMU (OVMF UEFI + legacy)
│   └── fetch-assets.sh         ← pinned git checkouts of Fluent theme/icons/cursors + fonts
├── packages/                   ← one dir per .deb; each has DEBIAN/ + root/ (filesystem tree)
│   ├── lindos-core/            ← python3 module `lindos` + polkit helper + CLIs (§4)
│   ├── lindos-desktop/         ← theme, panel, shortcuts, wallpapers, branding, greeter (§5)
│   ├── lindos-setup/           ← first-boot OOBE wizard (§6)
│   ├── lindos-settings/        ← Windows-11-style settings centre (§7)
│   ├── lindos-compat/          ← .exe/.msi runner, Wine/Proton/umu, Adobe recipes (§9)
│   ├── lindos-gaming/          ← launchers, drivers, gamemode, MangoHud, controllers (§10)
│   ├── lindos-tune/            ← RAM/perf tuning, zram, services, hardware control (§11)
│   ├── lindos-transfer/        ← Windows Easy Transfer-style migration + GUI (Addendum W §29)
│   ├── lindos-installer/       ← the installer flow: Ubiquity target-config hook + success command (§17);
│   │                              on the installation medium only, removed from the installed system
│   └── lindos-meta/            ← depends on the runtime packages above (incl. lindos-transfer; not lindos-installer)
├── docs/                       ← BUILDING, INSTALLER, ARCHITECTURE, COMPATIBILITY, MODES, RAM-BUDGET,
│                                  KEYBOARD-SHORTCUTS, FAQ, HARDWARE-CONTROL, WINDOWS-APPS,
│                                  WINDOWS-FORMATS, WINGET, TRANSFER, DUALBOOT (Addendum W)
├── tests/                      ← run.sh (lint everything), pytest suites, conftest.py (gi stub)
├── .github/workflows/ci.yml    ← lint + pytest + build debs; ISO build = workflow_dispatch
└── out/                        ← build products (git-ignored)
```

Addendum K (kernel + compat-performance, §14), Addendum V (VM/WinApps/Proton-system/drivers/CI,
§15) and Addendum W (every Windows format/Transfer/Play-anywhere, §16) each add their own packages
(`lindos-kernel`; `lindos-vm`, `lindos-winapps`; `lindos-transfer`) and docs on top of this base
layout — see their own specs for the full file lists. The installer flow (§17) adds the
thirteenth package, `lindos-installer`.

### 1.1 Package format
Each `packages/<name>/` contains:
- `DEBIAN/control` (Package, Version `1.0.0`, Section, Priority optional, Architecture `all`,
  Maintainer `Lindos Team <team@lindos.dev>`, Depends, Recommends, Description).
- optional `DEBIAN/postinst`, `DEBIAN/prerm`, `DEBIAN/postrm`, `DEBIAN/conffiles` (all `#!/bin/sh`,
  `set -e`, idempotent).
- `root/` — the filesystem tree copied as-is (`root/usr/bin/foo` → `/usr/bin/foo`).
- optional `tests/` (pytest).
`build/mkdeb.sh <name>` copies `root/` into a staging dir, fixes perms (dirs 755, files 644, anything
under `bin/`, `libexec/`, `sbin/`, `*.sh`, `DEBIAN/post*|pre*` → 755), then
`dpkg-deb --build --root-owner-group`.

## 2. Naming, branding, versions

| Item | Value |
|---|---|
| Product | **Lindos** |
| Version / codename | `1.0.0` / **Aurora** |
| Base | Linux Mint 22.x XFCE (default `22.2` "Zara", Ubuntu 24.04 `noble`) — set in `build/config.env` |
| `/etc/os-release` | keep `ID=linuxmint`, `ID_LIKE="ubuntu debian"`, `VERSION_CODENAME`/`UBUNTU_CODENAME` unchanged; set `NAME="Lindos"`, `PRETTY_NAME="Lindos 1.0 (Aurora)"`, `HOME_URL="https://lindos.dev"`, add `LINDOS_VERSION=1.0.0`, `LINDOS_CODENAME=Aurora`. Rationale: apt sources & Mint tooling keep working. |
| Other release files | `/etc/lsb-release` `DISTRIB_DESCRIPTION`, `/etc/linuxmint/info` `DESCRIPTION` + `GRUB_TITLE` and `/etc/casper.conf` `FLAVOUR` are display fields and say Lindos; `DISTRIB_ID=LinuxMint`, the release number/codename, `EDITION`, apt sources and package names stay (the installer's replace/reuse detection and Mint's tools read them). `/etc/os-release` also gets `SUPPORT_URL`/`BUG_REPORT_URL`/`PRIVACY_POLICY_URL` of the project. GRUB: `/etc/default/grub.d/49-lindos-distributor.cfg` sets `GRUB_DISTRIBUTOR="Lindos"`. Live session user/host: `liveuser`/`lindos`. Mechanism and rationale: `docs/BUILDING.md` ("Mint sweep"). |
| `/etc/lindos-release` | `Lindos 1.0.0 (Aurora)` |
| Logo | `/usr/share/pixmaps/lindos-logo.svg`; start-button icon name `lindos-start` (hicolor svg) |
| Accent colours | Dark accent `#60CDFF`, Light accent `#0067C0`, dark bg `#202020`, card `#2B2B2B`, light bg `#F3F3F3` |
| Default UI font | `Selawik 10` (Microsoft's OFL Segoe-metric font); fallbacks `Inter`, `Noto Sans`; mono `JetBrains Mono` if present else `DejaVu Sans Mono` |
| GTK theme names | `Lindos-Dark` (default), `Lindos-Light` (built from Fluent-gtk-theme with `--name Lindos --tweaks round`) |
| Icon theme | `Lindos` (Fluent-icon-theme installed with `-n Lindos`, dark panel variant `Lindos-dark`) |
| Cursor theme | `Fluent-dark-cursors` (dark) / `Fluent-cursors` (light) |
| xfwm4 theme | `Lindos-Dark` / `Lindos-Light` (Fluent xfwm themes renamed) |
| Wallpapers | `/usr/share/backgrounds/lindos/aurora-dark.svg` (default), `aurora-light.svg`, `bloom-blue.svg`, `mist-purple.svg`, `nightfall.svg` — original SVG art, ≥ 3840×2160 viewBox |
| Plymouth theme | `lindos` (`/usr/share/plymouth/themes/lindos/`) |
| Greeter | LightDM + slick-greeter, `/etc/lightdm/slick-greeter.conf` uses aurora-dark + Lindos theme |
| Maintainer | `Lindos Team <team@lindos.dev>` |

## 3. Modes

Five modes. Exactly these ids and display names:

| id | Name | Purpose | Key differences |
|---|---|---|---|
| `everyday` | Everyday | default; balanced | governor `schedutil`, picom on, all launchers available not pinned |
| `gaming` | Gaming | max FPS | governor `performance`, gamemode auto, MangoHud toggle, Steam/Lutris/Heroic/Prism/Sober pinned, zram 75 %, `vm.max_map_count=2147483642`, ananicy-cpp on |
| `work` | Work | productivity | LibreOffice/Thunderbird/OnlyOffice(optional) pinned, `power-profiles-daemon` balanced, night light on |
| `creator` | Creator | Adobe/creative via Wine | Bottles + creator recipes ready, GIMP/Krita/Kdenlive suggested, colour-managed profile hint |
| `lite` | Lite | ≤ 4 GB RAM / old PCs | no compositor, no picom, no animations, zram 100 %, minimal tray, `earlyoom` aggressive |

Storage:
- System default: `/etc/lindos/system.json` → `{"mode": "everyday", "browser": "chrome", "oem": false}`
  (Chrome is downloaded from Google's apt repo — SPEC §0.1, §4.4, §6; Firefox is the always-on-ISO
  fallback)
- User: `~/.config/lindos/config.json` (see §4.2). User mode overrides system mode.
- Mode definitions: `/usr/share/lindos/modes/<id>/`
  - `mode.json` — `{ "id", "name", "description", "icon", "packages": [..apt..], "flatpaks": [..],
    "services_disable": [..], "services_enable": [..], "sysctl": {"key": "value"},
    "governor": "schedutil|performance|powersave", "pins": [".desktop ids in order"],
    "compositor": "picom|xfwm|none", "zram_percent": 50 }`
  - `panel.tar.bz2` — an **xfce4-panel-profiles** profile (created by `xfce4-panel-profiles save`)
    OR `panel/` dir with `xfce4-panel.xml` + `panel-*/…rc` that `lindos-mode` applies via
    `xfce4-panel-profiles load` (preferred format: the tarball; the desktop package must generate it
    at build time from `panel/` using `build/chroot/40-theme.sh` if xfce4-panel-profiles is present,
    else copy the xml into `~/.config/xfce4/xfconf/xfce-perchannel-xml/`).
  - optional `apply-user.sh` (runs as user after mode switch), `apply-system.sh` (runs as root).

CLI: `lindos-mode list | get | set <id> [--system] [--no-install]` (in lindos-core). `set` = write config →
`lindos.modes.apply_mode()` → user part (panel profile, xfconf, compositor, autostart of
mangohud/gamemode toggles) + privileged part via helper action `apply-mode` (packages install if
missing & online, services, sysctl drop-in `/etc/sysctl.d/90-lindos-mode.conf`, governor,
zram config, ananicy). Must be idempotent and safe offline (skip installs, log warning).
The Mode's `packages`/`flatpaks` are installed by the **installer** for every Mode at once (§17.6),
so a Mode switch normally installs nothing; `apply-mode` still installs what is missing when the
plan says `install: true` (the default for `lindos-mode set` and Settings). `--no-install` /
`install: false` (what the OOBE always sends) applies configuration only and never runs apt or
Flatpak.

## 4. `lindos-core` (Python 3, no GTK dependency)

Installed at `/usr/lib/python3/dist-packages/lindos/`. Pure-Python, stdlib only (+ optional
`PyYAML` guarded). Every module must be importable on any OS (tests run on Windows/macOS): wrap
Linux-only calls, never execute at import time.

### 4.1 Paths (`lindos/paths.py`)
```python
SYSTEM_CONF_DIR = "/etc/lindos"
SYSTEM_CONF     = "/etc/lindos/system.json"
SHARE_DIR       = "/usr/share/lindos"
MODES_DIR       = "/usr/share/lindos/modes"
RECIPES_DIR     = "/usr/share/lindos/recipes"          # compat recipes (yaml/json)
LIBEXEC_DIR     = "/usr/libexec/lindos"
HELPER          = "/usr/libexec/lindos/lindos-helper"
USER_CONF_DIR   = "~/.config/lindos"                    # expanduser at call time
USER_CONF       = "~/.config/lindos/config.json"
SETUP_DONE      = "~/.config/lindos/setup-done"
STATE_DIR       = "~/.local/share/lindos"
PREFIXES_DIR    = "~/.local/share/lindos/prefixes"
APPS_DB         = "~/.local/share/lindos/apps.json"
LOG_DIR         = "~/.local/state/lindos"
INSTALL_STATE   = "/var/lib/lindos/install-state.json"   # what the installer did / could not do (§4.11)
```
All are overridable via env `LINDOS_ROOT` (prefix for system paths, used by tests) and
`LINDOS_HOME` (replaces `~`).

### 4.2 Config (`lindos/config.py`)
```python
DEFAULTS = {"mode":"everyday","browser":"chrome","theme":"dark","accent":"#60CDFF",
            "wallpaper":"/usr/share/backgrounds/lindos/aurora-dark.svg","setup_done":False,
            "gamemode_auto":True,"mangohud":False,"telemetry":False,"schema":1}
class Config:            # user config; dict-like .get/.set/.save()/.load(); atomic write
    def load(cls) -> "Config"
    def save(self) -> None
    def __getitem__/__setitem__/get/set/as_dict
def load_system() -> dict      # /etc/lindos/system.json merged with defaults
def effective_mode() -> str    # user override else system else "everyday"
def effective_browser() -> str
```

### 4.3 Modes (`lindos/modes.py`)
```python
MODE_IDS = ["everyday","gaming","work","creator","lite"]
@dataclass class Mode: id,name,description,icon,packages,flatpaks,services_disable,
                       services_enable,sysctl,governor,pins,compositor,zram_percent,path
def load_modes(modes_dir=None) -> dict[str, Mode]
def get_mode(mode_id) -> Mode
def current_mode() -> str
def apply_mode(mode_id, *, system=False, dry_run=False, log=print) -> ApplyResult
    # ApplyResult(ok: bool, steps: list[tuple[str,bool,str]])
def build_system_plan(mode: Mode, *, set_system_default=False, offline=False,
                      install=True) -> dict   # JSON-able plan sent to helper 'apply-mode'
def system_plan(mode_id, *, system=False, offline=False, install=True) -> dict   # same, by id;
                                            # install=False = configuration only (no apt/Flatpak)
```

### 4.4 Browsers (`lindos/browsers.py`)
```python
BROWSERS = {
 "edge":    {"name":"Microsoft Edge","package":"microsoft-edge-stable","desktop":"microsoft-edge.desktop",
             "repo":"deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main",
             "key_url":"https://packages.microsoft.com/keys/microsoft.asc","list":"/etc/apt/sources.list.d/microsoft-edge.list"},
 "chrome":  {"name":"Google Chrome","package":"google-chrome-stable","desktop":"google-chrome.desktop",
             "repo":"deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main",
             "key_url":"https://dl.google.com/linux/linux_signing_key.pub","list":"/etc/apt/sources.list.d/google-chrome.list"},
 "firefox": {"name":"Mozilla Firefox","package":"firefox","desktop":"firefox.desktop","repo":None,"key_url":None,"list":None},
}
def is_installed(bid) -> bool          # shutil.which / dpkg-query, guarded
def install(bid, log=print) -> bool     # via helper 'install-browser'
def set_default(bid) -> bool           # xdg-settings set default-web-browser + xfconf helper
def online() -> bool
```
`install()`/the helper's `install-browser` action both shell out to the single script
`/usr/libexec/lindos/install-browser.sh <edge|chrome|firefox> [--repo-only] [--dry-run]
[--no-update] [--in-installer [--download-only | --no-download]]` (SPEC §13) — it owns the
repo/key/apt logic for every caller (the Lindos installer hook, Lindos Settings, `lindos-browser
install`, `build/chroot/30-lindos-debs.sh` at ISO build time via `--repo-only`, and
`lindos-browser-firstboot.service`'s silent retry), so Chrome's key URL and repo line exist in
exactly one place. `--repo-only` adds the vendor's apt repository + signing key without ever
running `apt-get install` — used to pre-stage Chrome's repo on the ISO (§8) without installing the
package there (§0.1: that would be redistribution). `--in-installer` is the installer's mode
(root inside `chroot /target`, lists already refreshed by the caller, apt never reads the medium's
`cdrom:` source); `--download-only` and `--no-download` split it into a kill-safe download phase
and a dpkg phase (§17.3).

**Silent retry (installed system only):** the *installer* installs Chrome (§17.4) and records the
`browser` step of `/var/lib/lindos/install-state.json` (§4.11). `/usr/lib/systemd/system/
lindos-browser-firstboot.service` (oneshot, `ConditionKernelCommandLine=!boot=casper` and
`!boot=live` — refuses to run in the live/ISO session — `ConditionPathExists=!/lib/systemd/system/
oem-config.target` — not before the account wizard has finished — `ConditionPathExists=!/var/lib/
lindos/browser-firstboot.done`, `After=network-online.target`) runs
`/usr/libexec/lindos/browser-firstboot.sh` on the *installed* system and only covers what the
installer could not do. It reads the state first: `done` / `skipped` → write the marker and exit
at once (for `done` with `browser: chrome` it also makes sure Chrome is the system-wide default
for new users, an idempotent edit of `/etc/xdg/mimeapps.list`'s `[Default Applications]`, the
standard xdg fallback — never touches an existing user's own `~/.config/mimeapps.list` choice);
`pending` / `failed` / no record → if `/etc/lindos/system.json`'s `browser` is `chrome` it waits a
bounded time for NetworkManager (`/usr/libexec/lindos/wait-for-network`: Lindos masks
`NetworkManager-wait-online`, so `network-online.target` comes early), calls
`install-browser.sh chrome` and records the outcome; if `browser` is anything else (the user chose
Firefox in Lindos Setup) it records `skipped` and adds nothing. It never shows a window, wizard or
notification. Offline or a failed install leaves the marker unwritten so a later boot retries
automatically; every exit path is 0 (never blocks or fails the boot). Enabled by default via
`90-lindos.preset` (lindos-tune, §11).

### 4.5 Hardware (`lindos/hardware.py`)
`cpu_info()`, `gpu_info()` (parse `lspci -nn` / sysfs; vendor in {"nvidia","amd","intel","other"}),
`ram_info()` (`/proc/meminfo` → total/used/available MB), `battery_present()`,
`available_governors()`, `current_governor()`, `set_governor(g)` (helper `set-governor`),
`fan_sensors()` (parse `sensors -j` if present), `refresh_rates()` (parse `xrandr`).

### 4.6 Helper (`lindos/helper.py` + `/usr/libexec/lindos/lindos-helper`)
- `run_privileged(action: str, payload: dict, log=None) -> HelperResult(ok, out, err, code)` →
  `pkexec /usr/libexec/lindos/lindos-helper <action> <json>` (falls back to `sudo -n` if no
  pkexec; if already root, direct).
- Polkit policy id `org.lindos.helper` in `/usr/share/polkit-1/actions/org.lindos.helper.policy`
  with `auth_admin_keep`. Every `run_privileged` call is its own `pkexec` and may ask for the
  password; a grant kept by polkit is a convenience, never something a flow relies on. A flow with
  several privileged steps (the first-boot setup) uses `run_privileged_batch` instead: one
  `pkexec`, one prompt. The prompt needs a graphical polkit agent in the session; lindos-desktop
  autostarts one (`/usr/libexec/lindos/polkit-agent-start.sh`) and no rule ever skips the password.
- Actions (helper validates every payload; never `shell=True`; whitelists only):
  `apply-mode`, `install-browser`, `install-packages`, `install-flatpaks`, `set-governor`,
  `set-services`, `apply-sysctl`, `apply-tune`, `set-zram`, `install-compat`, `install-gaming`,
  `install-drivers`, `set-fan-profile`, `write-system-config`, `enable-earlyoom`, `run-batch`.
  `apply-mode`'s payload has an optional boolean `install` (default `true`): `false` = apply the
  configuration only (services, sysctl, zram, governor, …) and never apt/Flatpak-install the
  Mode's packages — also inside a `run-batch`. The first-boot wizard sends `false`.
- `run_privileged_batch(steps, log=None, on_step=None, timeout=None) -> BatchResult` sends
  `run-batch` `{"steps": [{"id"?, "action", "payload"?}, …]}` (≤ 32 steps, ≤ 256 KiB, unique ids)
  over stdin. The helper runs the steps in order in that one root process, each through the same
  validator and handler as a single call (no nesting; `reboot-to-windows`, `firmware-setup` and
  `import-wifi` are refused inside a batch), carries on after a failed step, prints a flushed
  `@@lindos-batch {json}` line when each step starts/finishes, and exits 0 only if every step
  succeeded (1 otherwise, 2 for an invalid payload). `BatchResult.results` has one
  `BatchStepResult(id, action, ok, code, message, seconds, out)` per submitted step and `on_step`
  fires once per step as it finishes.
- Logs to `/var/log/lindos/helper.log`.

### 4.7 Theme (`lindos/theme.py`)
`set_dark(bool)` (xfconf: `/Net/ThemeName`, `/Net/IconThemeName`, xfwm `/general/theme`,
cursor; also writes `~/.config/gtk-4.0/settings.ini` `gtk-application-prefer-dark-theme` and
`gsettings set org.gnome.desktop.interface color-scheme` if available), `is_dark()`,
`set_accent(hex)` (writes `~/.config/gtk-3.0/lindos-accent.css` and `~/.config/gtk-3.0/gtk.css`
`@import`), `list_wallpapers()`, `set_wallpaper(path)` (xfconf all monitors/workspaces),
`set_font(name)`, `set_taskbar_alignment("center"|"left")`, `set_taskbar_position("bottom"|"top")`.

### 4.8 Compat helpers (`lindos/compat.py`)
```python
@dataclass class ExeInfo: path,name,kind ("installer"|"app"|"game"|"msi"|"unknown"),
                          arch ("x86"|"x64"|"unknown"), installer_type (nsis|inno|installshield|msi|wix|squirrel|None),
                          product, company, sha256_prefix
def analyze_exe(path) -> ExeInfo      # minimal PE parse: MZ→e_lfanew→PE→Machine; scan first 4 MB for markers
def slugify(name) -> str
def apps_db_load()/apps_db_save(dict)
def choose_runner(info, config) -> str  # "umu" | "wine" | "bottles"  (rules in §9)
```

### 4.9 RAM (`lindos/ram.py`)
`snapshot()` → dict(total, used, available, top=[(name,rss_mb),…]) ; `report(fmt="text|json")`.

### 4.10 CLIs shipped by lindos-core (all `/usr/bin/`, python3, argparse, `--json` where sensible)
`lindos-mode`, `lindos-browser`, `lindos-config` (`get/set/show`, `install-state [--json]`),
`lindos-ram` (alias for `lindos-tune status` output). Exit codes: 0 ok, 1 error, 2 usage.

### 4.11 Session, install state and live-session helpers (`lindos/session.py`, `lindos/installstate.py`)
**Which session is this?** `lindos.session` (stdlib only, importable on any OS) is the single source
of truth for every component that must behave differently while running from the install medium:
- `is_live_session() -> bool` — true iff the kernel command line has the word `boot=casper` or
  `boot=live` (Mint's own `xapp.os.is_live_session` test). The file read is `/proc/cmdline`; tests
  point the env var `LINDOS_TEST_CMDLINE` at a fake file. Username/hostname are never used
  (BIOS menu and GRUB have differed in the past).
- `is_installer_chroot() -> bool` — true iff env `LINDOS_INSTALLER=1`: the caller is a script the
  installer runs inside `chroot /target`. `/proc/cmdline` still says `boot=casper` in there (proc is
  the live kernel's), so this explicit flag is the only reliable signal and wins over the command line.
- `is_oem_temp_user(user=None) -> bool` — the login name is `oem`, the temporary account of
  Ubiquity's OEM mode (`pwd`/`getpass` imported lazily).
- Shell twin: `/usr/libexec/lindos/is-live-session` (exit 0 = live). Units use
  `ConditionKernelCommandLine=!boot=casper` plus `!boot=live`. Companion helper
  `/usr/libexec/lindos/oem-config-pending` (exit 0 while Ubiquity's first-boot wizard is armed: an
  `oem-config.target` exists in `/lib/systemd/system`, `/usr/lib/systemd/system` or
  `/etc/systemd/system`, or the default target points at it) and `/usr/libexec/lindos/wait-for-network
  [SECONDS]` (bounded `nm-online` wait, default 90 s, for the silent retries).

**What did the installer do?** `lindos.installstate` owns `/var/lib/lindos/install-state.json`
(`paths.INSTALL_STATE`, honours `LINDOS_ROOT`; written atomically under a best-effort file lock so two
writers never lose each other's update; kept out of `config.SYSTEM_DEFAULTS`; it must be readable by the
logged-in user, since Lindos Setup and Settings read it):
```json
{"schema": 1, "updated": "<ISO-8601 UTC>", "online": true,
 "steps": {"<step>": {"status": "done|pending|skipped|failed", "detail": "<short text>", "time": "<ISO-8601 UTC>"}}}
```
`online` is `true`, `false` or `null` (unknown). Step ids: `updates drivers browser compat gaming
mode_extras flatpaks`. `done` and `skipped` are **terminal**; `pending` (wanted, could not: offline,
timeout, no time left) and `failed` are what the silent retries and Settings › Apps pick up.
`done` is recorded only after a verified success. API: `load(root=None) -> dict` (tolerant: a missing
or corrupt file is an empty state), `mark(step, status, detail="", root=None)` (validates ids and
status), `status(step, root=None) -> str` (`""` if unknown), `pending(root=None) -> list[str]`
(`pending` or `failed`), `is_terminal(step, root=None) -> bool`. CLI: `python3 -m lindos.installstate
[--root DIR] show | mark STEP STATUS [DETAIL] | pending | status STEP | online true|false|unknown`
(the installer passes `--root /target`) and `lindos-config install-state [--json]`. Exit codes: 0 ok,
1 the file cannot be written, 2 unknown step/status or bad usage.

## 5. `lindos-desktop` (theme, panel, shortcuts, branding)

Ships:
- `/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/` defaults: `xfce4-panel.xml`, `xfwm4.xml`,
  `xsettings.xml`, `xfce4-keyboard-shortcuts.xml`, `xfce4-desktop.xml`, `thunar.xml`,
  `xfce4-notifyd.xml`, `xfce4-power-manager.xml`, `xfce4-session.xml`, `keyboards.xml`.
- `/etc/xdg/xfce4/panel/` default plugin rcs (`whiskermenu-1.rc`, `docklike-2.rc`, `clock-*.rc`…).
- Panel spec: **bottom**, 48 px, spans monitor, dark translucent (`background-style=2`, rgba
  `#202020` α 0.85), plugin order:
  `separator(expand,transparent) · whiskermenu(icon lindos-start, no title) · docklike(pinned per mode)
   · separator(expand,transparent) · systray · pulseaudio · power-manager · xfce4-notifyd panel · clock
   (two-line `%H:%M` / `%d/%m/%Y`, Selawik 9) · showdesktop`.
  Whisker: `view-mode=2` (icon grid), `favorites` = pins, `show-button-title=false`,
  `menu-width=680 menu-height=720`, category position left, search at top, `command-lockscreen`,
  `command-switchuser`, `command-logout` (xfce4-session-logout), settings → `lindos-settings`,
  `command-profile` → `lindos-settings accounts`.
- Keyboard shortcuts (`xfce4-keyboard-shortcuts.xml`, `commands/custom` + `xfwm4/custom`):
  `Super` (`Super_L`) → `xfce4-popup-whiskermenu`; `Super+E` → `thunar`; `Super+I` →
  `lindos-settings`; `Super+D` → show desktop; `Super+L` → `xflock4`; `Super+A` →
  `xfce4-notifyd` toggle (`xfce4-panel --plugin-event=notification-plugin-*:toggle` fallback to
  `xfce4-notifyd-config`); `Super+X` → `lindos-settings --power-menu`; `Super+V` →
  `xfce4-popup-clipman`; `Super+R` → `xfce4-appfinder --collapsed`; `Super+Shift+S` →
  `xfce4-screenshooter -r`; `Print` → `xfce4-screenshooter -f`; `Ctrl+Shift+Escape` →
  `xfce4-taskmanager`; `Super+Tab` → cycle windows; `Super+Left/Right/Up/Down` → tile
  left/right/maximize/restore; `Super+Ctrl+Left/Right` → prev/next workspace; `Super+1..9` →
  activate docklike item N (`xfce4-panel --plugin-event=docklike-*:…` if supported else omit);
  `Super+.` → emoji picker (`emote` if installed); `Ctrl+Alt+T` → terminal; `Super+G` →
  `lindos-settings gaming`.
- xfwm4: theme `Lindos-Dark`, `button_layout=O|HMC`, `title_font=Selawik Bold 9`,
  `placement_mode=center`, `use_compositing=true` (except lite), `frame_opacity=100`,
  `show_frame_shadow=true`, `snap_to_windows`, `tile_on_move`, `wrap_workspaces=false`,
  `focus_delay=100`, `easy_click=Super`.
- picom: `/etc/xdg/picom-lindos.conf` (`backend=glx`, `vsync=true`, `corner-radius=8`, shadows,
  no blur by default, fade 0.03), autostart `/etc/xdg/autostart/lindos-picom.desktop`
  (`OnlyShowIn=XFCE`, Exec `lindos-compositor start` which honours mode: skips in lite).
- GTK CSS overlays `/usr/share/lindos/gtk-3.0/lindos.css` (round 8 px, Fluent-ish spacing,
  accent variable `@define-color lindos_accent #60CDFF`) imported by user `gtk.css` via
  `lindos.theme.set_accent`.
- Wallpapers (§2), Plymouth theme (script-based, logo + progress bar on `#202020`), slick-greeter
  conf, lightdm conf (`greeter-hide-users=false`, `user-background=false`), `/etc/xdg/autostart/`
  entries: `lindos-setup.desktop` (first-run gate: exits in the live session and for the temporary `oem`
  user, §6), `lindos-picom.desktop`, `lindos-mode-apply-user`, `lindos-live-session.desktop` (live USB
  session only: `live-session-power.sh` keeps the PC awake during a long install, §17.2).
- Branded `.desktop` shims in `/usr/share/applications/`: `lindos-files.desktop` (Name=File
  Explorer, Exec=thunar), `lindos-settings.desktop` (Name=Settings), `lindos-store.desktop`
  (Name=Lindos Store, Icon=lindos-store, Exec=mintinstall — the base's Software Manager, described as
  apps/packages/Flatpak, never as the Microsoft Store), `lindos-terminal.desktop` (Name=Terminal,
  Exec=xfce4-terminal). The base's own Software Manager and Welcome Screen menu entries are hidden
  (they duplicate the Store shim and Lindos Setup); Update Manager and Driver Manager stay, with
  Lindos icons — `/usr/libexec/lindos/rebrand-base.py` + `/usr/share/lindos/branding/base-sweep.json`,
  re-applied after every apt run by `/etc/apt/apt.conf.d/99lindos-branding` (`docs/BUILDING.md`, "Mint sweep").
- Fonts installed to `/usr/share/fonts/truetype/{selawik,inter}` by `build/fetch-assets.sh`
  (pinned URLs + SHA256) — the package ships `fonts.conf` snippet
  `/etc/fonts/conf.d/60-lindos-ui.conf` aliasing `Segoe UI` → Selawik.
- Branding files: `os-release` fragment applied by `build/chroot/40-theme.sh` (sed, not
  overwrite), `/etc/lindos-release`, `/usr/share/pixmaps/lindos-logo.svg`, `lindos-start` icon
  in `/usr/share/icons/hicolor/scalable/apps/`, `/etc/issue`, `/etc/motd`-less.
- `lindos-compositor` CLI (`start|stop|status|toggle`) in `/usr/bin` (sh).

## 6. `lindos-setup` (OOBE)

The OOBE is the *personalisation* step of the flow in §17. By the time it runs, the installer has
already installed the updates, drivers, Chrome, Wine/Proton, launchers and Mode apps, and Ubiquity's
oem-config wizard has already created the account (name, password, computer name, language,
keyboard, time zone). The OOBE is therefore **install-free**: it saves choices and applies
configuration; it never downloads, installs or updates anything.

- Binary `/usr/bin/lindos-setup` → `python3 /usr/lib/lindos-setup/main.py "$@"`.
- Flags: `--first-run` (exit 0 silently if `SETUP_DONE` exists, if not in XFCE, **in the live
  session** (`lindos.session.is_live_session()`) **or as the temporary `oem` user**
  (`is_oem_temp_user()`) — the gate is code, the autostart `.desktop` files are unchanged), `--reconfigure`
  (a hand-started run gets the same refusal in those sessions), `--dry-run` (no helper calls, print
  plan), `--page <id>`.
- GTK 3, one `Gtk.Window` fullscreen (maximised if the WM refuses), undecorated, dark `#202020`
  fluent backdrop, Windows-11-OOBE styling via `Gtk.CssProvider` (`ui/oobe.css`, GTK 3 CSS
  subset only). Fonts follow xsettings. Layout: a slim step indicator on top ("Step n of 6"; not
  shown on `welcome`/`apply`/`done`), each page a centred column of at most 760 px (large
  semibold heading, one short subtitle, one focused question; `welcome`/`apply`/`done` are
  centred "hero" pages), and a bottom bar with a quiet **Back**, a rounded accent **Next**
  (or Accept / Apply / Start using Lindos) and — in `--reconfigure` only — **Cancel**. Escape
  behaves as before (quits only in `--reconfigure`).
- Pages (ids): `welcome` → `mode` → `browser` → `personalize` → `privacy` → `transfer` →
  `summary` → `apply` → `done`. (The former `apps` page is gone: what it offered is installed by
  the installer, §17.6, and anything left is in Settings › Apps.) Wording follows the Windows OOBE:
  "Let's get you set up", "How will you use this PC?", "Choose your web browser", "Make it
  yours", "Choose your privacy settings" (button: Accept), "Bring your stuff from Windows", "Ready
  to set up your PC?", "Just a moment…", "All set" (button: Start using Lindos).
  - `mode`: 5 cards (icon, name, one-line description, RAM hint), default Everyday. When a Mode's
    extras are still pending in install-state, a short note says so and points to Settings › Apps.
  - `browser`: cards only for browsers that are actually on this PC (or about to be): Firefox
    always; Chrome when the installer's `browser` step is `done`, or as "Will be added when you're
    online" while it is `pending`/`failed`; Edge only if it was installed by hand. **Chrome is
    pre-selected when it is installed**; while it is pending, Firefox is pre-selected and choosing
    Chrome stores it as the preferred browser (it becomes the default when the silent retry adds
    it, §4.4). No downloads happen on this page and there is no "check connection" button.
  - `personalize`: Dark/Light cards (default Dark), accent swatches (8), wallpaper thumbnails,
    taskbar alignment Center/Left cards.
  - `privacy`: informational (no telemetry, no ads), toggles: location services off, crash
    reports off (both default off; nothing to send); the only vendor note is that Chrome and Edge
    have their own privacy policies.
  - `transfer`: optional "Bring your stuff from Windows" (Addendum W); nothing is copied here.
  - `summary`: review + Back; says plainly that Apply only saves choices and nothing is downloaded.
  - `apply`: large spinner + slim progress bar with short rotating lines ("Hi", "We're getting
    things ready for you", "Saving your choices", "Setting up your desktop"); the log is hidden
    behind a "Show details" toggle; builds `Plan` (JSON) → user-side steps directly + ONE batched
    helper run (one `pkexec`, one administrator-password prompt) for the two configuration steps
    `write-system-config` and `apply-mode` with `install: false`.
  - `done`: "All set" + "Start using Lindos" (writes `SETUP_DONE`, config `setup_done=true`); a
    read-only recap of what the installer set up (from install-state), a banner if items are still
    waiting (Settings › Apps), and the always-visible §0.1 reality check (Wine and Proton are a
    translation layer, not Windows; Valorant/Fortnite do not run on any Linux today, so they are
    "not supported on Lindos yet", up to their publishers, no date) with a "Learn more about
    Windows apps" disclosure.
- Pure logic in `lindos_setup/plan.py` (`Plan`, `build_plan(selections) -> Plan`, `Plan.to_json()`,
  `Plan.user_steps()`, `Plan.system_payloads()`), unit-tested without GTK. `build_plan` is the user
  steps (save config, theme, accent, wallpaper, taskbar alignment, default browser) plus
  `write-system-config` and `apply-mode` (`install: false`); an old saved selection that still
  carries `apps` loads, and legacy install steps in an old plan are never run.
- Autostart: `/etc/xdg/autostart/lindos-setup.desktop` (`Exec=lindos-setup --first-run`,
  `OnlyShowIn=XFCE`, `X-GNOME-Autostart-Delay=2`).

## 7. `lindos-settings`

- Binary `/usr/bin/lindos-settings [page] [--power-menu]`; `python3 /usr/lib/lindos-settings/main.py`.
- Window 1100×720 min 800×560, header search box, **left sidebar** (Win11 order):
  `home` Home · `system` System · `personalization` Personalization · `apps` Apps ·
  `windows-apps` Windows apps · `gaming` Gaming · `hardware` Hardware · `network` Network ·
  `accounts` Accounts · `mode` Lindos Mode · `update` Update & Recovery · `about` About.
- Native pages (implemented in GTK): `home` (mode card, RAM meter, quick toggles: Dark mode,
  Game Mode, MangoHud, Compositor), `personalization` (theme, accent, wallpaper, taskbar
  alignment/position, fonts, cursor), `mode` (5 cards + Apply, shows what changes),
  `windows-apps` (list from APPS_DB: run/uninstall/open prefix/`winecfg`, "Install a Windows
  program…" file chooser → `lindos-run`), `gaming` (Game Mode auto, MangoHud, Proton-GE
  update/list via `lindos-proton`, launchers install buttons, controller status, FPS/refresh),
  `hardware` (CPU governor + EPP, GPU vendor + driver status + buttons for `nvidia-settings` /
  `corectrl`, fans (`sensors` readout, `nbfc` profile), power profile, RGB → OpenRGB, refresh
  rate per monitor), `about` (Lindos version, kernel, CPU/GPU/RAM, "Windows-like specs" layout).
- Delegating pages (buttons that launch existing tools — keep RAM low): `system` → Display
  (`xfce4-display-settings`), Sound (`pavucontrol`), Notifications (`xfce4-notifyd-config`),
  Power (`xfce4-power-manager-settings`), Storage (`baobab` if present else `gnome-disks`),
  Default apps (`xfce4-mime-settings`), Bluetooth (`blueman-manager`), Printers
  (`system-config-printer`); `apps` → **Left to finish from setup** (shown only when
  `/var/lib/lindos/install-state.json` has `pending`/`failed` steps: Chrome, drivers, Wine/Proton,
  game launchers, the Modes' extra apps and Flatpaks, each with an **Install now** button that
  uses the ordinary helper actions and asks for the administrator password once; System updates
  are not listed here, the Updates page says what the installer did), Store (`mintinstall`),
  Installed (`mintinstall`/`synaptic`), Startup (`xfce4-session-settings`), Default apps, Web
  browsers (Install / Make default); `network` → `nm-connection-editor`, VPN,
  Firewall (`gufw`); `accounts` → `users-admin`/`mintusers` (`cinnamon-settings users` absent →
  `mugshot` for avatar), `update` → `mintupdate`, Drivers (`mintdrivers`), Timeshift, Recovery
  (boot repair link), Kernels (`mintupdate` kernels).
- `--power-menu`: small popup at bottom-left with Sleep / Restart / Shut down / Sign out / Lock /
  Settings / File Explorer / Terminal / Task Manager (Win+X clone).
- Pure logic module `lindos_settings/model.py` (page registry, quick-toggle actions) tested.

## 8. Build pipeline (`build/`)

`build/config.env` (sourced by scripts):
```
LINDOS_VERSION=1.0.0
LINDOS_CODENAME=Aurora
BASE_ISO_URL="https://mirrors.edge.kernel.org/linuxmint/stable/22.2/linuxmint-22.2-xfce-64bit.iso"
BASE_ISO_SHA256=""     # empty = skip verify (warn); fill for releases
BASE_UBUNTU_CODENAME=noble
OUT_DIR=out
WORK_DIR=out/work
ISO_NAME="lindos-${LINDOS_VERSION}-xfce-64bit.iso"
SQUASHFS_COMP=zstd
SQUASHFS_ARGS="-Xcompression-level 19 -b 1M"
INCLUDE_WINE=1 INCLUDE_STEAM=1 INCLUDE_FLATPAK_LAUNCHERS=0
```
`build-iso.sh` steps: check root+deps (`xorriso squashfs-tools rsync wget dosfstools mtools
7zip|p7zip-full dpkg-dev`) → download/verify ISO → extract with `xorriso -osirrox on` → `unsquashfs`
→ prepare chroot (bind `/dev /dev/pts /proc /sys /run`, copy `resolv.conf`, `DEBIAN_FRONTEND=
noninteractive`, `LC_ALL=C.UTF-8`, policy-rc.d to block service starts, `dpkg-divert` for
`initctl`/`ischroot` not needed on systemd but keep policy-rc.d) → copy `out/debs/*.deb` and
`build/chroot/` into `/tmp/lindos/` inside chroot → run hooks in order → cleanup → `mksquashfs`
(zstd) → write `casper/filesystem.size`, `filesystem.manifest` (`dpkg-query -W`), remove-list (the
base ISO's `filesystem.manifest-remove` is kept and `LIVE_ONLY_PACKAGES` — `lindos-installer` — is
appended, so the installer removes it from the installed system) →
copy overlay (`boot/grub/grub.cfg` + `loopback.cfg` with the §17.1 boot entries; the BIOS
`isolinux/live.cfg` is regenerated from them by `build/lib/boot_menu.py`; `.disk/info`
"Lindos 1.0 Aurora") → `verify_oem_offline` (fail the build when the medium cannot supply
oem-config, §17.7) → `md5sum.txt` regen → build ISO with
`xorriso -indev "$BASE_ISO" -outdev "$OUT" -boot_image any replay -map "$ISO_DIR" / -volid LINDOS`
(the *replay* trick keeps the original hybrid BIOS+UEFI boot records; document why) →
`sha256sum`. Every step logged to `out/build.log`; `set -Eeuo pipefail`; traps to unmount chroot
binds even on failure. Support `--skip-download`, `--no-cleanup`, `--only-debs`, `--iso PATH`.

Chroot hooks (`build/chroot/`), each `#!/bin/bash`, `set -Eeuo pipefail`, idempotent:
- `00-repos.sh` — add WineHQ (noble), Steam (Valve repo, i386 enabled), Mozilla apt repo (pin
  firefox .deb from Mozilla or keep Mint's), Flathub remote (flatpak already on Mint), Kisak Mesa
  optional (flag), `dpkg --add-architecture i386`, `apt-get update`. Google Chrome's repo/key are
  **not** staged here (lindos-core, which owns that logic, is not installed yet at this point in
  the build) — see `30-lindos-debs.sh` below.
- `10-debloat.sh` — purge list (keep tiny): `hexchat thunderbird? (keep) rhythmbox hypnotix
  warpinator? (keep, useful) mintwelcome? (replace by lindos-setup; keep package but hide autostart)
  onboard drawing simple-scan? (keep) gnome-calendar?`. Actual list documented in
  `docs/RAM-BUDGET.md`; never purge network/printing basics; disable (not purge)
  `cups` (socket-activated), `ModemManager`, `avahi-daemon` (keep for printers? → keep enabled but
  document), `mintreport`, `apport`, `whoopsie`, `kerneloops`, `ubuntu-report`, `brltty`,
  `speech-dispatcher`, `NetworkManager-wait-online`. `bluetooth` is deliberately **not** disabled
  here — it stays enabled by default in every mode (laptops need Bluetooth headphones/mice, and
  it is cheap when idle); only Lite mode's own tune.d/mode.json turns it off (`docs/RAM-BUDGET.md`).
- `20-base.sh` — install: `xfce4-docklike-plugin xfce4-panel-profiles xfce4-clipman-plugin
  xfce4-notifyd picom zram-tools|systemd-zram-generator earlyoom gamemode mangohud
  power-profiles-daemon lm-sensors fancontrol python3-gi gir1.2-gtk-3.0 polkitd pkexec
  flatpak fonts-noto-color-emoji xdg-desktop-portal-gtk winbind cabextract`, plus mode packages,
  plus `LAPTOP_ESSENTIALS` (`build/config.env`): firmware (`linux-firmware`,
  `firmware-sof-signed`, `intel-microcode`, `amd64-microcode`), audio (`alsa-ucm-conf`,
  `pipewire-audio`/`wireplumber`/`pipewire-pulse` as a no-op safety net — Mint 22/Ubuntu 24.04
  already default to PipeWire), Bluetooth (`bluez`, `blueman`), driver metadata
  (`ubuntu-drivers-common`), firmware updates (`fwupd`), and printing
  (`printer-driver-gutenprint`, `ipp-usb` — not `printer-driver-all`/`hplip`, too large; see
  `docs/RAM-BUDGET.md` §8 for the estimated added size and the trade-off). 32-bit
  `mesa-vulkan-drivers:i386` is already installed unconditionally by `70-gaming.sh` when i386 is
  enabled, so `20-base.sh` does not repeat it.
  `xfce4-docklike-plugin` is not packaged for Ubuntu 24.04 "noble" as of this writing (only
  25.10+ / Debian trixie+ carry it): `20-base.sh` already skips any package `pkg_available`
  reports as absent with a loud warning rather than failing the build, and
  `packages/lindos-desktop/DEBIAN/control` lists it as a Recommends (not a Depends) for exactly
  this reason — see `docs/BUILDING.md` and `CI-LOGS.md`.
- `30-lindos-debs.sh` — `apt-get install ./tmp/lindos/debs/*.deb`; afterwards, pre-stages Google
  Chrome's apt repository + signing key (`install-browser.sh chrome --repo-only`, `ADD_CHROME_REPO`
  default on) — same pattern as WineHQ/Steam in `00-repos.sh`, but here because it reuses
  lindos-core's own script instead of duplicating Chrome's repo/key. The `google-chrome-stable`
  package itself is **never** installed at build time (§0.1) — the installer downloads it into the
  new system (§17.4). `LINDOS_DEB_ORDER` also installs `lindos-installer` (§17), just before
  `lindos-meta`.
- `40-theme.sh` — run `fetch-assets` outputs already staged in `/tmp/lindos/assets` (theme,
  icons, cursors, fonts) → install (`install.sh -n Lindos …`), os-release sed, plymouth default,
  `update-alternatives` for default wallpaper, `glib-compile-schemas`, `fc-cache`,
  `update-icon-caches`, generate `panel.tar.bz2` per mode, `update-initramfs -u` (plymouth).
- `50-tune.sh` — apply base tune (systemd presets, sysctl, zram, journald, tmpfiles) via
  `lindos-tune apply --system --mode everyday --offline`.
- `60-compat.sh` — with `INCLUDE_WINE=1` Ubuntu's `wine` + `winetricks` on the ISO (WineHQ
  *staging* and `umu-launcher`, a pinned GitHub release, are installed by the installer, §17.6);
  dxvk/vkd3d optional; MIME defaults; `lindos-compat doctor`.
- `70-gaming.sh` — steam-launcher (Valve deb), lutris (Ubuntu repo or GitHub deb pinned),
  heroic (GitHub deb pinned), prism launcher (PPA/Flatpak flag), `steam-devices`, `xpadneo`
  (dkms optional), `mesa-vulkan-drivers:i386`, `libgl1-mesa-dri:i386`, `nvidia` NOT preinstalled
  (the installer's drivers step / `lindos-drivers` install them on demand, with consent — §17.5; the
  ISO carries no `nvidia-driver-5xx`, keep it small), OpenRGB (repo/deb), `antimicrox`, `goverlay`,
  `piper`, `corectrl`.
- `77-mint-sweep.sh` — last pass over what still says "Linux Mint": re-runs lindos-desktop's
  `apply-branding.sh --files-only` (menu/autostart entries, lsb-release/linuxmint-info display fields,
  Firefox start page), verifies Mint Welcome cannot autostart, purges `mint-backgrounds-*` only when
  `apt-get -s purge` shows nothing else would go, and logs an audit of everything left. Guarded and
  idempotent; runs before `78-installer-brand.sh`.
- `78-installer-brand.sh` — rebrands the Ubiquity live installer (debconf templates, live-desktop
  launcher, welcome artwork, slideshow, `Lindos-Setup` GTK skin); reads `build/installer/`; every step
  guarded and idempotent (`docs/BUILDING.md`, "Installer branding").
- `79-installer-flow.sh` — wires the installer flow into Ubiquity (§17): asserts Chrome/Edge are not on
  the image, deploys the hook (`install -m 0755` of `target-config.sh` as
  `/usr/lib/ubiquity/target-config/50lindos-install`, no `.` in the name), bakes `lindos.seed` into the
  image's debconf database, logs an audit of what Ubiquity will run. Unlike the branding hook it
  **dies** when a required piece is missing (an ISO without the hook would silently install the old
  way). Idempotent; test seam `LINDOS_INSTALLER_ROOT` (`docs/BUILDING.md`, "Installer flow").
- `80-cleanup.sh` — `apt-get autoremove --purge`, `apt-get clean`, rm `/tmp/lindos`, machine-id
  reset, `/var/lib/dbus/machine-id`, resolv.conf restore, logs truncated, `/root/.bash_history`. It
  **keeps deleting `/var/lib/apt/lists`** on purpose: the base ISO's lists are stale by install time
  and cost ~100 MB in the squashfs; the installer hook refreshes the new system's lists first (§17.3).

## 9. `lindos-compat` (Windows apps)

- Depends: `lindos-core`, `wine-staging | wine`, `winetricks`, `cabextract`, `winbind`,
  `umu-launcher` (Recommends), `bottles` via Flatpak (Suggests, installed on demand).
- `/usr/bin/lindos-run <file.exe|.msi|.bat|.lnk> [--runner umu|wine|bottles] [--prefix NAME]
  [--new-prefix] [--gamemode] [--mangohud] [--info]`:
  1. `analyze_exe` → decide runner (`choose_runner`): games & unknown → **umu** (Proton-GE via
     umu-launcher, DXVK/VKD3D built-in) if available else wine; installers/apps (office, adobe,
     utilities) → **wine** (staging); creator recipes → **bottles** if installed.
  2. Prefix: default per-app `~/.local/share/lindos/prefixes/<slug>` (`WINEPREFIX`), shared
     `default` for small utilities (flag `--shared`); 64-bit default; `WINEDLLOVERRIDES=winemenubuilder.exe=d`
     to prevent .desktop spam; `WINEDEBUG=-all`; `DXVK_ASYNC`, `PROTON_*` env for umu; `gamemoderun`
     wrapper if `--gamemode` or config `gamemode_auto` & kind==game; `mangohud` if flag.
  3. Installer flow: run, then scan prefix `drive_c/Program Files*/`, `users/*/Start Menu` and
     `~/.local/share/applications/wine/` for new `.lnk`/`.exe` → create `.desktop`
     `~/.local/share/applications/lindos-<slug>.desktop` (Exec=`lindos-run --prefix <slug>
     "<exe>"`, Icon extracted via `wrestool`+`icotool` if `icoutils` present else `lindos-exe`
     generic icon) → register in `APPS_DB` `{slug:{name,exe,prefix,runner,installed_at,kind}}`.
  4. GUI feedback via `zenity`/`yad` when launched from file manager (no terminal): "Preparing
     Windows compatibility…", errors with copyable log path `~/.local/state/lindos/run-<slug>.log`.
- MIME: `/usr/share/applications/lindos-run.desktop` (`MimeType=application/x-ms-dos-executable;
  application/x-msdownload;application/x-msi;application/x-ms-shortcut;application/x-bat;`),
  `/usr/share/lindos/mimeapps-lindos.list` merged into `/etc/xdg/mimeapps.list` by postinst
  (`[Default Applications]`), `update-desktop-database`.
- Thunar custom action `/etc/xdg/Thunar/uca.xml` fragment merged in postinst: "Run with Lindos
  (Windows app)", "Run with Proton (game)", "Open C:\ drive".
- `lindos-compat` CLI: `doctor` (checks wine/umu/vulkan/32-bit libs/fonts, prints fix commands),
  `prefixes list|remove|open|winecfg <slug>`, `recipes list|apply <id> [--prefix]`, `install-umu`,
  `install-bottles`, `proton list|update` (delegates to `lindos-proton`).
- Recipes `/usr/share/lindos/recipes/*.json`: `{id,name,vendor,category,status
  ("works"|"partial"|"broken"),notes,runner,arch,winetricks:[...],dll_overrides:{},env:{},
  post_cmds:[],registry:[{key,name,type,value}]}` — ship at least:
  `photoshop-cc-2021` (partial/works w/ notes), `photoshop-cs6` (works), `illustrator-cc-2021`
  (partial), `premiere` (broken — say so, suggest Kdenlive/DaVinci Resolve native),
  `office-2016` (works), `office-365` (broken → LibreOffice/OnlyOffice/web), `notepad-plus-plus`
  (works), `7zip` (works), `winrar` (works), `paint-net` (partial), `foobar2000` (works),
  `autocad` (broken → suggest FreeCAD/web), `epic-games-launcher` (works via Heroic instead),
  `roblox-player` (broken → Sober), `riot-client` (broken — Vanguard).
- Windows-friendly touches: `~/.local/share/lindos/prefixes/<slug>/drive_c` symlinked as
  `~/Windows Apps/<Name>/C:` (created lazily); `lindos-run --info` prints PE details.

## 10. `lindos-gaming`

- Depends: `lindos-core`, `gamemode`, `mangohud`, `steam-devices`; Recommends `steam-launcher |
  steam-installer`, `lutris`, `heroic`, `prismlauncher`, `antimicrox`, `goverlay`, `piper`,
  `corectrl`, `openrgb`, `mesa-vulkan-drivers`, `libvulkan1`, `vulkan-tools`.
- CLIs: `lindos-proton list|update|remove <tag>` (Proton-GE from GitHub releases → `~/.steam/root/
  compatibilitytools.d/` and `~/.local/share/umu/compatibilitytools/` shared via symlink; also
  used by umu), `lindos-drivers detect|install [--nvidia-open|--nvidia-proprietary|--amd|--intel]
  --status` (wraps `ubuntu-drivers`, `mintdrivers`, sets `nvidia-drm.modeset=1`),
  `lindos-game install <steam|lutris|heroic|prism|sober|vinegar|mcpelauncher|bottles|all>`,
  `lindos-game status`.
- Roblox: `lindos-game install sober` → `flatpak install -y flathub org.vinegarhq.Sober`; desktop
  file `lindos-roblox.desktop` (Name=Roblox, Exec=`flatpak run org.vinegarhq.Sober`); doc note.
- Minecraft: Prism Launcher (Flatpak `org.prismlauncher.PrismLauncher` or PPA) + `openjdk-17/21`;
  Bedrock via `io.mrarm.mcpelauncher` Flatpak (unofficial; user must own Android version).
- Config: `/etc/gamemode.ini` (`renice=10`, `ioprio=0`, `inhibit_screensaver=1`, `softrealtime=auto`,
  `desiredgov=performance`, scripts to toggle picom off/on: `lindos-compositor stop|start`),
  `/etc/xdg/MangoHud/MangoHud.conf` (fps, frametime, gpu/cpu temp, ram, `position=top-left`,
  `toggle_hud=Shift_R+F12`, `font_size=20`, `no_display=1` unless mode gaming & config mangohud),
  udev: `60-lindos-controllers.rules` (Xbox/PS/8BitDo/Switch hidraw perms), `xpadneo`/`xone`
  optional installers, `/etc/sysctl.d/80-lindos-gaming.conf` (`vm.max_map_count=2147483642`).
- Compat matrix data: `/usr/share/lindos/compat-matrix.json` (games list with status + reason +
  link) — same data renders `docs/COMPATIBILITY.md` (docs must match). Minimum entries:
  Roblox (works via Sober), Minecraft Java (works), Minecraft Bedrock (partial), Valorant
  (**not possible**, Vanguard), Fortnite (**not possible**, Epic disabled EAC-Linux), Apex Legends
  (not possible since Nov 2024), CS2 (native), Dota 2 (native), GTA V (works; Online: BattlEye
  Linux enabled — verify), Elden Ring (works), Cyberpunk 2077 (works), Rocket League (works via
  Heroic/Steam), Genshin Impact (works via Anime Game Launcher — third-party), League of Legends
  (**not possible** since Vanguard 2024), Rainbow Six Siege (not possible, BattlEye disabled),
  Destiny 2 (not possible), PUBG (not possible), Overwatch 2 (works via Battle.net in Lutris),
  Sims 4 (works via EA app in Lutris/Heroic — partial), Forza Horizon 5 (works), Hogwarts Legacy
  (works), Halo Infinite (works, EAC enabled), Palworld (works), Helldivers 2 (works, nProtect
  enabled), Marvel Rivals (works), Warframe (works). The top-level `disclaimer` object and the
  per-entry `unsupported_kind` of the anti-cheat-blocked titles are specified in SPEC-WINDOWS §30.1.

## 11. `lindos-tune` (RAM/perf/hardware)

- CLI `lindos-tune status [--json]` (RAM used/available, zram, top 10 RSS, services on, governor,
  compositor, score vs target 350–500 MB), `lindos-tune apply --mode <id> [--system] [--offline]`,
  `lindos-tune services list|disable|enable <unit>` (whitelist), `lindos-tune zram <percent>`,
  `lindos-tune governor <g>`, `lindos-tune fan list|set <profile>` (nbfc/fancontrol),
  `lindos-tune power <performance|balanced|power-saver>` (powerprofilesctl), `lindos-tune report`
  (markdown for bug reports).
- Ships: `/etc/sysctl.d/70-lindos-base.conf` (`vm.swappiness=180` when zram, `vm.page-cluster=0`,
  `vm.vfs_cache_pressure=50`, `vm.dirty_ratio`, `kernel.nmi_watchdog=0`, `net.core.default_qdisc=fq`
  `net.ipv4.tcp_congestion_control=bbr`), zram (`/etc/default/zramswap` or
  `/etc/systemd/zram-generator.conf` — support both, detect), earlyoom
  `/etc/default/earlyoom` (`-m 4 -s 100 --avoid '(^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$'`),
  journald drop-in (`SystemMaxUse=64M`, `Storage=persistent`), tmpfiles (`/tmp` tmpfs via
  `tmp.mount` enable), systemd preset `/usr/lib/systemd/system-preset/90-lindos.preset`
  (disable list §8), NetworkManager `wifi.powersave` untouched, `ananicy-cpp` rules
  `/etc/ananicy.d/lindos/` (games → high prio, browsers → normal, compilers → idle) opt-in via
  gaming mode, `/etc/lindos/tune.d/*.conf` per-mode overrides, cpupower/`intel_pstate` EPP
  helper, `nbfc-linux` optional installer, `lm-sensors` autodetect at postinst (`sensors-detect
  --auto` guarded), `hdparm`/`fstrim.timer` on.
- RAM budget doc must list each measure and its estimated saving; measured baseline of stock
  Mint 22 XFCE ≈ 600–750 MB; Lindos target 350–500 MB; Lite ≈ 300–380 MB (no compositor).

## 12. Docs & tests
- `docs/COMPATIBILITY.md` is **generated** from `packages/lindos-gaming/root/usr/share/lindos/
  compat-matrix.json` by `tests/gen-compat-doc.py` (also verifies in CI that they match).
- `tests/run.sh`: `bash -n` all `*.sh`/bin scripts with bash shebang, `sh -n` for `#!/bin/sh`,
  `python3 -m py_compile` all `.py` + python bins, `shellcheck` if available, `pytest -q tests
  packages/*/tests`, JSON validity of every `*.json`, XML well-formedness of every `*.xml`,
  `.desktop` sanity (`[Desktop Entry]`, `Name=`, `Exec=`), `desktop-file-validate` if present.
- `tests/conftest.py`: adds `packages/lindos-core/root/usr/lib/python3/dist-packages` and
  `packages/lindos-setup/root/usr/lib/lindos-setup`, `packages/lindos-settings/root/usr/lib/
  lindos-settings`, `packages/lindos-compat/root/usr/lib/lindos-compat` to `sys.path`; installs
  a stub `gi` module (`gi.require_version`, `gi.repository.Gtk/Gdk/GLib/Gio/Pango/GdkPixbuf`
  as permissive dummies) **only if real gi is missing**.
- Shell style: `#!/bin/bash`, `set -Eeuo pipefail`, functions, `log()`/`die()`, no `sudo` inside
  scripts (callers use pkexec/sudo), quote everything, ShellCheck-clean.
- Python style: 3.10+ (base has 3.12), type hints, no third-party deps except `gi` in UI,
  `argparse` CLIs, `logging`, never `shell=True`.

## 13. Cross-component call map (must agree)
| Caller | Calls |
|---|---|
| lindos-setup apply page | `lindos.config`, `lindos.modes.apply_mode` (`install=False`), `lindos.browsers.set_default`, `lindos.theme.*`, helper `write-system-config` + `apply-mode` (`install: false`) in one `run-batch`; **never** an `install-*` action (§6) |
| lindos-setup browser/mode/done pages | `lindos.installstate.load()` (read-only hints), `lindos.browsers.is_installed` |
| lindos-settings › Apps "Left to finish from setup" | `lindos.installstate.load()`; helper `install-browser` / `install-compat` / `install-gaming` / `install-packages` / `install-flatpaks` / `install-drivers` (the Mode lists come from the `mode.json` files, never from the installer's `extras.json`, which exists on the medium only) |
| installer hook (`target-config.sh`, §17) | inside `chroot /target`: `install-browser.sh chrome --in-installer`, `install-compat.sh --in-installer`, `install-gaming.sh --in-installer`, `browser-firstboot.sh`, `ubuntu-drivers`, `lindos-drivers install --auto`, `apt-get`, `flatpak`; `python3 -m lindos.installstate --root /target mark …` |
| installer success command (`finalize.sh`, §17) | `systemctl --root=/target enable/set-default`, `chroot /target passwd -l oem`, `debconf-set-selections` |
| `lindos-browser-firstboot` / `lindos-driver-firstboot` | `lindos.installstate` (`status`/`mark`), `is-live-session`, `oem-config-pending`, `wait-for-network`, then `install-browser.sh chrome` / `ubuntu-drivers install --free-only` / `lindos-drivers install --auto` |
| lindos-settings | everything in lindos-core; `lindos-run`, `lindos-proton`, `lindos-drivers`, `lindos-game`, `lindos-tune`, `lindos-compat` via subprocess |
| lindos-mode set | `lindos.modes.apply_mode` → helper `apply-mode` (payload from `build_system_plan`) + `xfce4-panel-profiles load` + `lindos-compositor` |
| helper `apply-mode` | `lindos-tune apply --mode <id> --system` |
| helper `install-compat` | `/usr/libexec/lindos/install-compat.sh` (from lindos-compat) |
| helper `install-gaming` | `/usr/libexec/lindos/install-gaming.sh <items…>` (from lindos-gaming) |
| helper `install-browser` | `/usr/libexec/lindos/install-browser.sh <edge|chrome|firefox>` (from lindos-core) |
| helper `install-drivers` | `lindos-drivers install …` (from lindos-gaming) |
| build 50-tune.sh | `lindos-tune apply --mode everyday --system --offline` |
| build 40-theme.sh | `/usr/libexec/lindos/build-panel-profiles.sh` (from lindos-desktop) |

## 14. Addendum K — Kernel & Compat-Performance layer

The Lindos-tuned kernel (`lindos-kernel`: ntsync, sched_ext, 1000 Hz/preempt, MGLRU, tuned
config + build recipe), the sched_ext/THP additions to `lindos-tune`, and the Proton/DXVK/
VKD3D/gamescope/per-title-profile overhaul of `lindos-compat`/`lindos-gaming` are specified in
[`SPEC-KERNEL.md`](SPEC-KERNEL.md) (§14–§19). Its §14 honesty rules (no anti-cheat circumvention
of any kind — no "Windows attester", attestation/TPM forger, or HWID/VM-detection evader; such a
tool cannot produce hardware-rooted signatures and only gets the user hardware-banned) are binding
everywhere, extending §0.1.

## 15. Addendum V — Virtualization, WinApps, Proton-system, Drivers, CI

The honest KVM/VFIO Windows VM (`lindos-vm`, real Windows VM with optional GPU passthrough — **no**
hypervisor-hiding/SMBIOS/UUID/ACPI/CPUID spoofing, does not defeat VM-blocking anti-cheat), seamless
Windows apps over RDP (`lindos-winapps`, for Adobe/Office — user supplies licensed Windows),
system-wide Proton-GE + gamescope session, expanded driver automation (Broadcom/audio + first-boot),
and real GitHub-Actions Linux builds (kernel/deb/ISO) are specified in [`SPEC-VM.md`](SPEC-VM.md)
(§20–§26). Its §20 honesty rules (a VM is a truthful VM; no spoofing; no license bypass; user
supplies Windows) are binding, extending §0.1 and §14.

## 16. Addendum W — Every Windows format, Transfer, Play-anywhere

Opening every common Windows file type Wine/Proton alone does not cover (MSIX/APPX packages and
bundles, `.appinstaller`, `.msp`, `.reg`, `.ps1`, `.vbs`, `.url`, `.scr`, `.cpl`, `.inf`, `.cab`,
disk images, ClickOnce, DOS and 16-bit Windows programs, `./setup.exe` from a terminal via
`binfmt_misc`, and `winget install <id>`), a new `lindos-transfer` package (a read-only,
Windows-Easy-Transfer-style migration tool with a Windows-side kit and GUI), and honest
"play-anywhere" routes for games whose kernel-level anti-cheat cannot run on any Linux (official
cloud streaming where a provider offers it, or a one-shot restart into the PC's own Windows
installation via `lindos-dualboot` — never a VM route, an emulator or a spoofer) are specified in
[`SPEC-WINDOWS.md`](SPEC-WINDOWS.md) (§27–§34). Its §27 honesty rules (no anti-cheat/attestation
circumvention of any kind; every format handler states `works`/`partial`/`unsupported` and never
tries a file anyway; transfer is read-only and never touches secrets; no licence bypass; routes are
official and region-accurate) are binding, extending §0.1, §14 and §15.

## 17. Addendum I — The installer flow (install once, ask for the account at first boot)

Binding contract for how Lindos is installed and first started. It extends §0.1 (honesty), §4.4
(browsers), §4.11 (session, install state), §6 (OOBE) and §8 (build); the user-facing explanation is
[`docs/INSTALLER.md`](docs/INSTALLER.md), the maintainer detail is in `docs/BUILDING.md` ("Installer flow").

1. **Live session** (USB): almost nothing but the installer — no first-run wizard, no package
   installs, no password prompts, never sleeps or blanks.
2. **Installer**: everything heavy happens while installing — system updates, drivers and firmware,
   the default browser (Chrome is *downloaded* from Google's official apt repository; its licence
   forbids shipping it on the ISO), the Wine/Proton layer and game launchers, the Modes' apps and
   Flatpaks.
3. **After install**: remove the USB stick, reboot. The first boot shows only account setup (name,
   password, computer name, language, keyboard, time zone) and then the personalisation wizard (§6) —
   it installs and updates nothing.

Mechanism: **Ubiquity in OEM mode** (Linux Mint's fork of Ubiquity, GTK). The installer's own account
page creates a temporary account `oem`; Ubiquity's `oem-config` wizard creates the real one at the
first boot of the installed system.

### 17.1 Boot entries
`build/overlay/boot/grub/grub.cfg` (UEFI and BIOS-GRUB) and `loopback.cfg` (loop-booted ISO, e.g.
Ventoy) carry the same entries; the BIOS menu `isolinux/live.cfg` is **generated** from the finished
`grub.cfg` by `build/lib/boot_menu.py` (labels `install`, `install-compat`, `try`, `try-compat`, `check`;
the base's memtest and local-drive blocks are kept; a `default`/`ontimeout` that named a replaced label
is retargeted), so the two boot paths cannot drift apart. `default=0`, `timeout=10`.

| # | Title | Kernel words (after `/casper/vmlinuz @PRESEED@`) |
|---|---|---|
| 1 | Install Lindos *ver* | `boot=casper only-ubiquity oem-config/enable=true ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh username=liveuser hostname=lindos iso-scan/filename=${iso_path} quiet splash --` |
| 2 | Install Lindos *ver* (compatibility mode) | as 1 plus `nomodeset` |
| 3 | Try Lindos *ver* (live session) | as 1 **without** `only-ubiquity` (so the desktop *Install Lindos* icon follows the same OEM flow) |
| 4 | Try Lindos *ver* (compatibility mode) | as 3 plus `nomodeset` |
| 5 | Check the integrity of the medium | as 3 plus `integrity-check` |
| 6 | Boot from the first hard disk | (plus the base's memtest entries) |

`@PRESEED@` is **empty** on the Mint 22.2 ISO (it has no `preseed/*.seed`); values on the kernel line
cannot contain spaces. No entry says `username=mint`/`hostname=mint` (`build-iso.sh` dies if one does,
or if the first entry lost `only-ubiquity oem-config/enable=true`). The former "OEM install (for
manufacturers)" entry is gone: every entry auto-arms the first-boot wizard.
`only-ubiquity` starts the installer on its own X server (`ubiquity-dm`, no LightDM, no XFCE session),
so nothing else runs in it; the *Try* entries boot the LightDM live desktop, where the live-session
guards of §17.2 apply.

### 17.2 What runs where
- **Live** (`lindos.session.is_live_session()`): `lindos-setup --first-run` exits 0; `lindos-update-notify`
  (script, user service and timer) does nothing; `lindos-sensors-detect.service` is not started; the
  first-boot retry units are not started (`ConditionKernelCommandLine=!boot=casper`/`!boot=live`);
  `/etc/xdg/autostart/lindos-live-session.desktop` (only when `is-live-session` says so) runs
  `live-session-power.sh`, which sets xfce4-power-manager, the screensaver and `xset` to never sleep,
  blank, lock or suspend on lid close; `lindos-live-inhibit.service` holds a logind inhibitor
  (`sleep:idle:handle-lid-switch:handle-suspend-key:handle-hibernate-key`) for the whole live session
  and is what covers the `only-ubiquity` boot, where no desktop session exists. Mint's own update
  tray already gates itself off in live.
- **Installation**: Ubiquity's pages Language → Keyboard → Wi-Fi (only when needed) → Prepare →
  Partition → Time zone → a **temporary account** page. Then `plugininstall`: copy, `configure_*`,
  **the target-config hook** (§17.3), language packs, initramfs, boot loader, extras, `remove_extras`
  (removes the packages of `filesystem.manifest-remove`, including `lindos-installer`; in OEM mode the
  ubiquity family itself stays until oem-config's own clean-up at the end of the first-boot wizard),
  then **`ubiquity/success_command`** (§17.7).
- **First boot**: `default.target` is `oem-config.target`, so only the `oem-config` wizard starts (its
  own X server; language, keyboard, Wi-Fi if needed, time zone, real account and computer name;
  Lindos-worded but Ubiquity-styled). It removes the temporary account and the installer packages
  (the ubiquity family, oem-config) and isolates `graphical.target`: LightDM → the new user's XFCE session → `lindos-setup --first-run` (§6).
  The silent retries (§17.9) start only after the wizard
  (`ConditionPathExists=!/lib/systemd/system/oem-config.target`).

### 17.3 The target-config hook (`lindos-installer`: `target-config.sh` + `lib.sh`)
Deployed by `79-installer-flow.sh` as **`/usr/lib/ubiquity/target-config/50lindos-install`** — mode
0755, **no `.` in the name** (Ubiquity skips dotted and non-executable entries; git on Windows loses
exec bits, so the file is `install -m 0755`-ed with CRLF stripped). Ubiquity runs it once per
installation in the live environment as root (`log-output -t ubiquity --pass-stdout`), after the
account and locale exist and before the initramfs and the boot loader. Contract (a hook that hangs or
breaks dpkg breaks every install):
- **always exit 0**; **no `set -e`**; `cd /`; every step independently guarded and time-boxed
  (`timeout -k`); one wall-clock budget for everything (default 2700 s);
- **stdout is Ubiquity's debconf pipe**: nothing is ever printed to it (debconf's confmodule moves
  stdout to stderr; without a frontend it is redirected); children get stdin from `/dev/null`; output
  goes to `/var/log/lindos/installer-hook.log` (live) and `/target/var/log/lindos/installer.log`;
- progress is **one text line** in the installer window (`db_progress INFO lindos-installer/msg`,
  template in `/usr/share/lindos/installer/lindos-installer.templates`), no progress bars of its own;
- it re-runs itself under `systemd-inhibit --what=sleep:idle:handle-lid-switch`;
- **downloads first** (`apt-get -d`, safe to kill, clamped to the remaining budget), then dpkg runs from
  the downloaded files (`--no-download`; never killed mid-transaction, only a generous hang guard), then
  **always a repair pass** (`dpkg --configure -a`, `apt-get -f install`, `dpkg --audit`): Ubiquity's later
  python-apt steps skip everything, or abort the install, when dpkg is broken;
- every command enters the new system through `unshare --mount --propagation private` + `chroot /target`
  with `env -i`, `LINDOS_INSTALLER=1` and `DEBIAN_FRONTEND=noninteractive` (the mounts of `/proc /sys /dev
  /run` vanish with the process; `policy-rc.d` and `resolv.conf` are restored by the exit trap); apt uses
  an `APT_CONFIG` that never reads the medium's `cdrom:` source, never cleans lists and waits for the
  dpkg lock;
- the **kernel, the boot loader and the Ubiquity/oem-config/casper families** (and everything in
  `filesystem.manifest-remove`) are `apt-mark hold`-ed, and the Ubiquity family is pinned to the
  installed version, for the whole hook; both are released again on **every** exit path (trap;
  `finalize.sh` retries from a list file). Updates are a plain `apt-get upgrade` — **never a
  `dist-upgrade`** — and a simulated upgrade that would touch a held family upgrades nothing;
- the target's package lists are refreshed first (`80-cleanup.sh` deleted them on purpose); only lists
  of network sources count — a failed refresh makes `updates`/`mode_extras` *pending*, never a false
  *done*;
- nothing is created when there is no `/target` (the answer that names `finalize.sh` is also read by
  Ubiquity's OEM first-boot pass on the installed system, where there is nothing to do);
- switches on the kernel command line: `lindos.install=off` (skip everything: every step `skipped`),
  `lindos.install_budget=SECONDS` (default 2700), `lindos.proprietary_drivers=1` (§17.5).

### 17.4 Steps and their outcomes
Steps run in this order; each ends recorded in install-state (§4.11) as `done | pending | skipped |
failed`. A step whose result was never recorded is `failed`; one the budget did not reach is `pending`.

| Step id | What | Notes |
|---|---|---|
| `browser` | Google Chrome from Google's apt repository (`install-browser.sh chrome --in-installer`), then `browser-firstboot.sh` for the system default and the marker | `skipped` when `system.json` names another browser; `done` only when `dpkg-query` shows `google-chrome-stable` installed |
| `drivers` | firmware (`linux-firmware`, `firmware-sof-signed`, microcodes) + `ubuntu-drivers install --free-only`; a proprietary GPU driver only with consent (§17.5) | writes `/var/lib/lindos/driver-firstboot.done` on a terminal outcome |
| `updates` | `apt-get upgrade` with the held families excluded (§17.3) | |
| `compat` | `install-compat.sh --in-installer`: Wine (WineHQ staging), winetricks, umu | |
| `gaming` | `install-gaming.sh --in-installer`: Steam and Lutris | |
| `mode_extras` | the union of every Mode's apt packages (§17.6), as a group and, if that fails, one by one | a package no archive carries is noted, not fatal |
| `flatpaks` | the Flathub remote and the Mode Flatpaks (§17.6) | best effort: Flatpak inside the installer's chroot is unproven; failures stay `pending` |

`online` in the state file records whether the archives answered. Offline, the hook says so on the
status line, exits early and marks every step `pending` ("offline while installing").

### 17.5 Drivers, consent and Secure Boot
Free drivers and firmware are always ensured. Proprietary GPU drivers (`lindos-drivers install --auto`)
are installed **only with the user's consent** — the installer's "third-party software / multimedia
codecs" checkbox (`ubiquity/use_nonfree`) or `lindos.proprietary_drivers=1` on the kernel command line —
and **never for a possibly-NVIDIA GPU when Secure Boot is enabled or unknown** (an unsigned DKMS module
would need an interactive key enrolment at the next start): the step is then recorded `skipped` with
the reason and the driver stays available in Settings. `/var/lib/lindos/driver-proprietary-consent`
records the consent. The silent retry (§17.9) never goes beyond what the installer was allowed to do
(SPEC-VM §24).

### 17.6 The installed set
The Mode is chosen only after the installation, so the installer installs the **union** of every
Mode's extras once: `packages/lindos-installer/root/usr/share/lindos/installer/extras.json`, generated
by `build/lib/installer_extras.py` from every `mode.json` (`packages`, `flatpaks`, `compat_items`) and
the OOBE `apps.json` defaults (a test keeps it in sync). It holds the apt extras (for example
`gamemode mangohud lutris libreoffice-* thunderbird gimp krita kdenlive winetricks colord`), the
Flatpaks `org.prismlauncher.PrismLauncher org.vinegarhq.Sober com.heroicgameslauncher.hgl
com.usebottles.bottles`, the compat items `wine winetricks umu`, the gaming items `steam lutris` and the
firmware list. What the Modes call "suggested" (OnlyOffice) is **not** installed. `extras.json` exists on
the medium only; Settings and the retries read the same lists from the `mode.json` files.

### 17.7 The success command and the base-ISO assumptions
`ubiquity/success_command` = `/usr/libexec/lindos/installer/finalize.sh` (`lindos.seed`, baked into the
image's debconf database, **and** on every boot entry's kernel line). Ubiquity runs it synchronously in
its GTK main loop — the window cannot repaint — so it is a **short (<5 s), always-exit-0**
finalisation:
1. verify that oem-config is really in `/target` (else `dpkg -i` the bundled copy from `/lindos/oem-debs`);
2. arm it: copy `oem-config.service`/`.target` to `/lib/systemd/system`, `systemctl --root=/target
   enable`, `set-default oem-config.target` — the essentials of `oem-config-prepare` **without** its
   deletion of the NetworkManager profiles;
3. strip the stale `autologin-user=oem` lines from `/etc/lightdm/lightdm.conf`;
4. lock the temporary account (`passwd -l oem`) and write its `setup-done`;
5. set `user-setup/allow-password-empty` back to `false` in the new system's debconf database (the image
   bakes it `true` so the temporary account needs no password);
6. remove the hook copy, the version pin, holds and cache.

If oem-config is **not** there it logs `CRITICAL`, writes `/var/lib/lindos/oem-config-not-armed` and
leaves the `oem` account open: a machine nobody can log in to is worse.

Assumptions about the base ISO (`build/build-iso.sh` checks what it can; re-check them whenever
`MINT_VERSION`/`BASE_ISO_URL` change): Ubiquity is Mint's fork (24.04.3+mintNN) whose
`run_target_config_hooks` behaves as in §17.3; `casper/filesystem.manifest-remove` exists (else
`lindos-installer` stays on the installed system — the build only warns); the medium's `pool/` carries
`oem-config` and `oem-config-gtk` of **exactly** the squashfs's ubiquity version, with `.disk/info`,
`.disk/cd_type` and `dists/` (`build/lib/verify_oem_pool.py`; the build **fails** unless `OEM_DEBS_DIR`
supplies a matching fallback set or `REQUIRE_OEM_POOL=0`), and `aptdaemon` +
`python3-aptdaemon.gtk3widgets` are available; the ISO has no `preseed/` directory.

### 17.8 Logs and files
`/var/log/lindos/installer-hook.log` (live), `/var/log/lindos/installer.log` on the installed system (a
copy that also carries `finalize.sh`'s lines), `/var/lib/lindos/install-state.json`,
`/var/lib/lindos/{browser,driver}-firstboot.done`, `/var/lib/lindos/driver-proprietary-consent`,
`/var/lib/lindos/oem-config-not-armed` (only when arming failed) and Ubiquity's own
`/var/log/installer/`.

### 17.9 Silent retries and offline installs
Whatever ends `pending`/`failed` is retried without any UI: `lindos-browser-firstboot.service` (§4.4)
and `lindos-driver-firstboot.service` run only on the installed system, only after the account wizard,
after a bounded wait for NetworkManager, and read install-state first. Only `browser` and `drivers`
have such a retry; every other pending item (`compat`, `gaming`, `mode_extras`, `flatpaks`) is offered
by **Settings › Apps › Left to finish from setup** with an **Install now** button, and `updates` by the
normal update tools. Ubiquity's OEM mode does not copy the live session's Wi-Fi/Bluetooth profiles, so
the first boot starts offline until the account wizard's Wi-Fi page.

### 17.10 Honesty about this flow
The flow was written against the upstream source of Ubiquity, casper and oem-config and unit-tested
with fake targets; **no full install has run yet** (QEMU or real hardware).
`docs/INSTALLER.md` ("Known limitations and what is unverified") is the authoritative list, and the first
thing a maintainer does with a new ISO is the QEMU install test in `docs/BUILDING.md`.
