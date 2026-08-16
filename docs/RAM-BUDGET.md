# RAM budget

**Target: 350–500 MB idle** (Lite ≈ 300–380 MB), measured as `free -m` "used" (total −
available) after login into XFCE with no application open, about 60 s after the panel appears.
`lindos-tune status` prints exactly that figure and the verdict line
`Idle RAM X MB — target 350–500 MB (Lite: 300–380)`; `lindos-ram` is a shortcut for it.

> **Honesty.** Every saving below is an **estimate** derived from component RSS figures and
> upstream numbers, until it has been measured on the Lindos reference machines and logged in §6.
> Savings overlap and do not simply add up. The source of this table is
> `packages/lindos-tune/root/usr/share/lindos/tune/ram-budget.json` (schema 1); Lindos Settings
> and `lindos-tune` read the same file.

## 1. Baselines

| System | Idle used (MB) | Status | Notes |
|---|---|---|---|
| Stock Linux Mint 22 XFCE (idle after login) | 600–750 | estimate | Mint 22.x XFCE, default session, mintupdate/mintreport/blueman trays, cups+bluetooth+ModemManager on, no zram. |
| Lindos 1.0 Everyday | 420–520 | estimate | picom on, zram 50 %, debloat presets, tray delayed. |
| Lindos 1.0 Lite | 300–380 | estimate | no compositor, zram 100 %, minimal tray, bluetooth/ModemManager/cups-browsed off, earlyoom -m 8. |

Targets: `everyday gaming work creator` → 350–500 MB; `lite` → 300–380 MB.

## 2. How to measure

```sh
lindos-tune status            # RAM used/available, zram, top 10 RSS, units, governor, compositor, verdict
lindos-tune status --json     # the same as JSON (score, targets, verdict.rating)
lindos-ram [--top N] [--aggregate] [--builtin]     # alias; --builtin uses lindos.ram without lindos-tune
free -m                       # the raw figure the target refers to ("used" column)
lindos-tune report [-o FILE]  # Markdown report for bug reports / measurement logs
```

Measure after a reboot, log in, wait ~60 s, open nothing. Note the mode (`lindos-mode get`),
whether picom runs (`lindos-compositor status`), and the machine (RAM size, GPU driver, integrated
vs. discrete GPU — the driver alone moves the figure by 30–80 MB).

## 3. What the build removes or disables (`build/chroot/10-debloat.sh`)

* **Purged** (`DEBLOAT_PURGE`, tiny on purpose; absent packages are skipped):
  `hexchat rhythmbox hypnotix onboard gnome-calendar`. Thunderbird, Warpinator, simple-scan,
  Celluloid, Drawing are **kept**; mintwelcome is kept as a package (only its autostart is
  hidden — the OOBE replaces it); network and printing basics are never purged (a safety net in
  the hook refuses).
* **Disabled, never purged** (`DEBLOAT_DISABLE_SERVICES`): `bluetooth.service
  ModemManager.service apport.service whoopsie.service kerneloops.service brltty.service
  speech-dispatcher.service NetworkManager-wait-online.service`.
* **Autostarts hidden** (`NotShowIn=XFCE;` added, marker `X-Lindos-Hidden=true`, reverted
  exactly by lindos-tune): `mintwelcome`, `mintreport`, `apport`, `whoopsie`,
  `blueman-applet` (only while `bluetooth.service` is disabled), `orca-autostart`,
  `onboard-autostart` (`/usr/share/lindos/tune/autostart-hide.list`); `10-debloat.sh` also hides
  `update-notifier`.
* **systemd preset** `/usr/lib/systemd/system-preset/90-lindos.preset` (applied by
  `lindos-tune apply` at image build and on the first apply): enable `earlyoom.service
  fstrim.timer zramswap.service cups.socket cups.path avahi-daemon.service
  lindos-sensors-detect.service`; disable `bluetooth.service ModemManager.service cups.service
  cups-browsed.service NetworkManager-wait-online.service apport.service whoopsie.service
  kerneloops.service brltty.service speech-dispatcher.service ubuntu-report.service
  motd-news.timer apt-daily.timer apt-daily-upgrade.timer`. cups stays socket/path-activated
  (starts on the first print job); avahi-daemon stays on for printer discovery.

  *Note:* the shipped preset (and the ISO build) disables `bluetooth.service` for **every**
  mode (SPEC §8: disable, not purge). If you use Bluetooth headsets/mice, re-enable it once:
  `lindos-tune services enable bluetooth.service` (then Lindos Settings → System → Bluetooth &
  devices opens Blueman); the blueman tray reappears at the next login.

## 4. Base tune (`lindos-tune apply`, all modes)

| File | Content |
|---|---|
| `/etc/sysctl.d/70-lindos-base.conf` | `vm.swappiness=180` (with a zram back-end, else 60), `vm.page-cluster=0`, `vm.vfs_cache_pressure=50`, `vm.dirty_ratio=10`, `vm.dirty_background_ratio=5`, `vm.watermark_boost_factor=0`, `vm.watermark_scale_factor=125`, `kernel.nmi_watchdog=0`, `fs.inotify.max_user_watches=524288`, `net.core.default_qdisc=fq`, `net.ipv4.tcp_congestion_control=bbr` (+ `/etc/modules-load.d/lindos.conf` → `tcp_bbr`) |
| `/etc/systemd/zram-generator.conf` **or** `/etc/default/zramswap` (auto-detected) | zstd, priority 100, size = mode's `zram_percent` of RAM (50 % / gaming 75 % / lite 100 %) |
| `/etc/default/earlyoom` | `EARLYOOM_ARGS="-m 4 -s 100 --avoid '(^\|/)(Xorg\|xfwm4\|xfce4-panel\|lightdm)$'"` (Lite: `-m 8`) |
| `/etc/systemd/journald.conf.d/lindos.conf` | `Storage=persistent SystemMaxUse=64M SystemMaxFileSize=8M RuntimeMaxUse=32M Compress=yes` |
| `tmp.mount` | `/tmp` on tmpfs (copied from `/usr/share/systemd/tmp.mount` and enabled; fstab fallback) |
| `/etc/tmpfiles.d/lindos.conf` | `/var/log/lindos`, `/var/lib/lindos-tune` |
| `/etc/sysctl.d/90-lindos-mode.conf` | the mode's sysctl block (see [MODES.md](MODES.md)) |
| `/etc/tmpfiles.d/lindos-governor.conf` | governor/EPP persistence (removed when power-profiles-daemon owns it) |
| `/etc/lindos/tune.d/<mode>.conf` | per-mode overrides `ZRAM_PERCENT GOVERNOR EARLYOOM_MIN COMPOSITOR ANANICY` |

## 5. Measures and estimated savings

| id | Measure | Kind | Modes | Est. saving (MB) | How | Explanation | Reversible |
|---|---|---|---|---|---|---|---|
| `xfce-not-cinnamon` | XFCE session instead of Cinnamon | ram | all | 150–250 | Base is Linux Mint XFCE (xfwm4 + xfce4-panel), not Cinnamon/muffin + nemo-desktop. | Cinnamon idles around 800-950 MB, XFCE around 600-750 MB on the same Mint release. Already part of the base image; listed so the comparison to 'Mint' is fair. | n/a (base choice) |
| `no-picom-lite` | No compositor in Lite (no picom, xfwm compositing off) | ram | lite | 25–35 | lindos-compositor honours COMPOSITOR=none from /etc/lindos/tune.d/lite.conf; xfwm4 use_compositing=false. | picom (glx backend) holds ~20-30 MB plus GPU buffers per window; xfwm4 compositing another few MB. Also removes vsync latency on very old GPUs. | lindos-mode set everyday (or Lindos Settings > Home > Compositor) |
| `bluetooth-off` | bluetooth.service disabled + blueman-applet hidden | ram | all | 15–25 | systemd preset 90-lindos.preset (disable bluetooth.service, SPEC §8 'disable, not purge') + autostart-hide.list (blueman-applet if-disabled=bluetooth.service). | bluetoothd ~5-8 MB, blueman-applet (python3 + GTK) ~12-18 MB. Disabled in every mode by the preset (the ISO build does the same); the package stays installed, so headsets/mice work again after a single enable command (see 'reversible'). | lindos-tune services enable bluetooth.service |
| `cups-socket-activated` | CUPS socket/path activated instead of always-on; cups-browsed disabled | ram | all | 8–14 | preset: disable cups.service, enable cups.socket + cups.path; disable cups-browsed.service. | cupsd (~8-10 MB) starts on the first print job through cups.socket, cups-browsed (~4 MB, network printer discovery via avahi) is off; avahi-daemon itself stays on so printers are still found when you open the printer dialog. | lindos-tune services enable cups.service cups-browsed.service |
| `modemmanager-off` | ModemManager disabled | ram | all | 6–10 | preset: disable ModemManager.service. | Only needed for built-in WWAN/3G/4G modems. Users with a mobile modem re-enable it (Lindos Settings > Network shows the hint). | lindos-tune services enable ModemManager.service |
| `avahi-kept` | avahi-daemon KEPT enabled (documented, not a saving) | note | all | 0–4 | preset: enable avahi-daemon.service (SPEC §8: keep for printer discovery). | avahi uses ~3-4 MB; disabling it would save that but break network printer and .local discovery, so Lindos keeps it. Listed for transparency. | lindos-tune services disable avahi-daemon.service (not recommended) |
| `crash-telemetry-off` | apport, whoopsie, kerneloops, ubuntu-report disabled | ram | all | 8–12 | preset disable list + autostart-hide.list (apport, whoopsie autostart entries). | Crash uploaders and telemetry that Lindos never sends anywhere: whoopsie ~5 MB resident, kerneloops ~2 MB, apport hooks. Nothing is lost for the user. | lindos-tune services enable whoopsie.service (etc.) |
| `nm-wait-online-off` | NetworkManager-wait-online.service disabled | boot-time | all | — | preset: disable NetworkManager-wait-online.service. | No RAM saving; removes up to 30-60 s of boot delay when Wi-Fi is slow to associate. Listed because it is in the debloat list. | lindos-tune services enable NetworkManager-wait-online.service |
| `brltty-speech-off` | brltty and speech-dispatcher disabled by default | ram | all | 4–8 | preset: disable brltty.service, speech-dispatcher.service; autostart-hide.list hides orca-autostart. | Braille display and speech daemons only help users who need them; brltty also grabs some USB serial adapters (Arduino/ESP boards). Accessibility users re-enable them in Settings. | lindos-tune services enable brltty.service speech-dispatcher.service |
| `mint-trays` | mintwelcome/mintreport tray hidden; mintupdate tray kept | ram | all | 15–25 | autostart-hide.list: mintwelcome (replaced by lindos-setup OOBE), mintreport (System Reports tray). mintupdate stays (its own autostart delays 90 s). | mintwelcome ~15-20 MB (python3 + WebKit view) at every login, mintreport-tray ~10 MB polling for crash reports. mintupdate's tray is kept: it is how users learn about updates; it starts late so the early idle measurement is ~20 MB lower. | remove NotShowIn=XFCE; from /etc/xdg/autostart/mintreport.desktop (or copy the entry to ~/.config/autostart) |
| `zram` | zram compressed swap (50 % / 75 % gaming / 100 % lite of RAM, zstd) | headroom | all | — | /etc/systemd/zram-generator.conf or /etc/default/zramswap (detected) + vm.swappiness=180, vm.page-cluster=0. | Not an idle-RAM saving (zram itself costs a few hundred KB until used) but it turns 4 GB into 'feels like 6-7 GB' by compressing idle anonymous pages ~3:1 in RAM instead of hitting the disk. Effective headroom on 4 GB: +1.5-2.5 GB. | lindos-tune zram 0 |
| `journald-cap` | journald capped at 64 MB persistent (8 MB files, 32 MB runtime) | ram | all | 5–20 | /etc/systemd/journald.conf.d/lindos.conf. | journald mmaps its active journal files; huge journals mean more page cache and slower boots. 64 MB keeps about a week of desktop logs. Storage stays persistent for bug reports. | delete /etc/systemd/journald.conf.d/lindos.conf |
| `tmp-tmpfs` | /tmp on tmpfs (tmp.mount) | disk | all | — | systemctl enable tmp.mount (fallback: fstab tmpfs line, size=50%). | Uses RAM only for what is actually in /tmp (nothing at idle); saves SSD writes and speeds up installers/Wine temp files. Big /tmp users are covered by zram swap. | systemctl disable tmp.mount |
| `earlyoom` | earlyoom (-m 4, Lite -m 8) protects the desktop | stability | all | — | /etc/default/earlyoom EARLYOOM_ARGS with --avoid for Xorg/xfwm4/xfce4-panel/lightdm. | Costs ~1 MB. Kills the biggest offender (usually a browser tab process) before the kernel OOM freezes the whole desktop for minutes on 4 GB machines. | systemctl disable --now earlyoom |
| `no-indexers` | No file indexers or preloaders | ram | all | 30–80 | Base image ships no tracker/baloo/recoll/preload; ananicy-cpp rules push updatedb/man-db/timeshift to idle priority. | Desktop search indexers keep 30-80 MB resident and thrash the disk after login. Lindos uses plain Thunar search + catfish on demand. | install what you like (apt install plocate) |
| `no-snapd` | snapd not present | ram | all | — | Linux Mint already ships without snapd (pinned); Flatpak is used instead and adds no daemon. | Listed because Ubuntu-based comparisons often include snapd (~25 MB + loop mounts). Not a Lindos saving relative to Mint. | n/a |
| `sysctl-cache` | vm.vfs_cache_pressure=50, dirty ratios 10/5, page-cluster 0 | responsiveness | all | — | /etc/sysctl.d/70-lindos-base.conf. | Keeps directory/inode cache warmer and writes dirty pages sooner so a big copy does not stall the UI. Neutral for idle RAM. | edit /etc/lindos/tune.d/<mode>.conf or delete 70-lindos-base.conf |
| `greeter` | LightDM + slick-greeter kept (already light) | note | all | — | No change; slick-greeter exits after login, lightdm ~4 MB. | GDM/SDDM would cost 20-60 MB more. Nothing to gain by switching. | n/a |
| `lite-minimal-tray` | Lite: minimal tray (no clipman, no notes/weather plugins, no power-manager tray) | ram | lite | 10–20 | modes/lite panel profile (lindos-desktop) — outside lindos-tune, listed for the budget total. | Every panel plugin is a GTK object tree; the Lite profile keeps whisker, docklike, systray, audio, clock only. | lindos-mode set everyday |

Kinds: `ram` = idle-RAM saving · `headroom` / `stability` / `responsiveness` / `boot-time` /
`disk` = no idle-RAM saving, listed for transparency · `note` = documented non-change.

## 6. Measurement log

No reference measurements have been published yet for 1.0.0 — every figure above is an
estimate. When you measure, add a row (machine, RAM, GPU/driver, mode, `free -m` used, `lindos-tune
status` verdict, date) and attach `lindos-tune report` output to the issue.

| Date | Machine | RAM | GPU / driver | Mode | Idle used (MB) | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | (none yet) |

## 7. Undo any of it

`lindos-tune services list` shows the whitelisted units and their state; `lindos-tune services
enable|disable <unit>` toggles them (through the polkit helper when not root); `lindos-tune zram
0` turns zram off; `systemctl disable --now earlyoom` disables earlyoom; deleting the drop-ins
listed in §4 restores the distribution defaults; `lindos-mode set everyday` restores the default
mode. Nothing here is hidden or irreversible.
