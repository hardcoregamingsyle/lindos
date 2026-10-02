# lindos-transfer — bring your stuff from Windows

> **What it is, in one paragraph.** `lindos-transfer` is Lindos' Windows-Easy-Transfer-style
> migration tool. It copies your personal files, browser bookmarks, desktop wallpaper, your own
> fonts, your Wi-Fi networks and a list of your installed apps from a Windows installation into your
> Lindos account — either straight from the Windows partition on a dual-boot PC, or from a **transfer
> folder** made by a small double-click kit on your old Windows PC. It never touches Windows itself:
> every read is read-only, and nothing on the Windows side is ever changed, moved or deleted.

This is binding-consistent with [`SPEC-WINDOWS.md`](../SPEC-WINDOWS.md) §29 (the contract for every
command, JSON shape and file this document describes) and extends the honesty rules in
[`SPEC.md`](../SPEC.md) §0.1 and [`SPEC-WINDOWS.md`](../SPEC-WINDOWS.md) §27.

## 1. Honesty — read this first

* **Windows is only ever read, never written.** A Windows partition is mounted **read-only**; the
  Windows-side kit only copies files outward. Nothing on the Windows side is changed, moved or
  deleted, and nothing is ever done to Windows without you choosing it in the wizard or on the
  command line first.
* **What never transfers, and why:**
  * **Saved browser passwords, cookies and payment cards.** Chrome, Edge, Brave, Opera and Vivaldi
    encrypt these so that they only work on the PC that saved them (Windows DPAPI, and increasingly
    Chrome's "App-Bound Encryption"); copying the encrypted files to Lindos would not decrypt them,
    it would just move unusable data. Use your browser's own sync (a Google/Microsoft/Firefox
    account), or export passwords from the browser's own password manager on Windows and import them
    on Lindos.
  * **Firefox's saved passwords** are the one exception, and only if you explicitly ask for them
    (`-IncludeFirefoxPasswords` / the "Include Firefox saved passwords" checkbox / `--firefox-
    passwords`): the files that hold them are copied **unchanged**, still protected by your Firefox
    Primary Password if you have one. Firefox Sync is the safer way to move passwords.
  * **Windows itself, and the fonts and wallpapers that come with it.** `C:\Windows\Fonts` and the
    stock/Spotlight wallpapers are licensed to that PC, not to be copied elsewhere (Microsoft's font
    FAQ is explicit about this). Only fonts **you installed yourself** and a wallpaper that is **your
    own picture** are copied.
  * **Windows Hello, account hashes, `SAM`/`SECURITY`, DPAPI master keys, hibernation/page/swap
    files.** These are never opened, on either side of the transfer.
  * **Wi-Fi passwords** move only if you explicitly ask, and Windows itself decides whether it can
    give them out in plain text (it needs an administrator prompt); otherwise only the network name
    and security type move, and Lindos asks you for the password the first time it connects — exactly
    like moving to a new phone.
  * **Files that only exist in the cloud** (a OneDrive file with the cloud icon, not yet downloaded to
    that PC) are **never downloaded** by the transfer — that would surprise you with a multi-gigabyte
    download and possibly fill your disk. They are listed in the report instead, with a note to get
    them from onedrive.live.com or make them "Always keep on this device" on Windows first.
  * **Installed programs cannot be copied.** Windows programs are not files you can just move; instead
    Lindos builds a **list** of what you had and offers, per app, the closest honest match it knows —
    already built in, a native Linux package, a Flatpak, the Linux version of the same browser, a
    Windows-app installer through Lindos' own Wine/Proton support, a `.desktop` shortcut to the
    website, or "run it from your own licensed Windows" for the handful of things that genuinely need
    Windows. **Nothing is installed without you choosing it.**
  * **Kernel-anti-cheat games are never claimed to "transfer".** Steam game *files* can be copied (see
    §5 below), but a title that needs Vanguard/EAC-kernel/BattlEye-kernel is shown with its honest
    [play-anywhere route](GAMING.md) (cloud, dual-boot, or "not supported on Lindos yet" — the
    publisher decides, no date), never as something that just works after the copy.
* **BitLocker recovery keys are typed by you, into udisks/cryptsetup/dislocker's own prompt.** Lindos
  never sees, stores or passes them on.

## 2. Two ways to bring your files over

### A. Same PC, dual-boot (the Windows partition is right there)

If Lindos and Windows are on the same machine (a dual-boot install, or Windows on another disk in the
same PC), `lindos-transfer` can read the Windows partition directly, read-only, with no USB stick
needed.

1. Open **Transfer from Windows** (Settings → Windows apps → "Transfer from Windows…", or the app
   `lindos-transfer-gui` from the Start Menu). It also appears as an optional page in Lindos Setup, the
   wizard on your first login (nothing is copied there).
2. On the **Where are your Windows files?** page, pick the detected Windows partition. If Windows was
   hibernated or used Fast Startup, you will be warned that the files might be a little out of date —
   either restart Windows and do a **full shutdown** first (`shutdown /s /t 0`, or Shift+Shut down),
   or continue anyway.
3. If the partition is BitLocker-protected, unlock it first (a file-manager prompt, or in a terminal:
   `udisksctl unlock -b /dev/<partition> --read-only`, entering the 48-digit recovery key). Lindos
   never stores that key.
4. Continue through **Whose files?**, **What to bring**, **Your apps**, **Transferring your files**
   and **Done**, exactly as described in §4 below.

### B. A different PC, or no dual-boot: the USB transfer kit

If your Windows files are on a *different* PC (the usual "I'm switching to a new Lindos computer"
case), or this PC has no second Windows partition, use the small Windows-side kit instead:

1. **On Lindos**, click "Make a transfer USB for another PC…" on the Source page (or run
   `lindos-transfer make-usb-kit /media/your-usb-stick`). This copies three small files —
   `LindosTransfer.cmd`, `LindosTransfer.ps1` and `README.txt` — onto the drive you choose. An **exFAT
   or NTFS** USB stick is recommended; a FAT32 stick works too, but cannot hold any single file larger
   than 4 GB (such files are skipped and listed, never split or corrupted).
2. **On the old Windows PC**, plug the drive in and double-click `LindosTransfer.cmd`. If Windows says
   "Windows protected your PC", choose "More info" → "Run anyway" (the kit is a plain-text script; you
   can read every line of it in Notepad first). It creates a folder named
   `LindosTransfer-<computer name>-<date>-<time>` next to itself and copies your files into it,
   showing plain progress the whole time. See
   [`root/usr/share/lindos/transfer/windows/README.txt`](../packages/lindos-transfer/root/usr/share/lindos/transfer/windows/README.txt)
   for the full list of options (`-Include`, `-IncludeWifiPasswords`, `-IncludeSteamGames`,
   `-InventoryOnly`, `-WhatIf`, …) — the same file ships next to the kit itself.
3. Bring that USB stick to your Lindos PC, plug it in, open **Transfer from Windows**, and on the
   Source page either pick the detected transfer folder or click "Use a transfer folder…" and browse
   to it. From here on it works exactly like the same-PC path.

## 3. What moves

| Category (id) | What it is | Notes |
|---|---|---|
| `desktop`, `documents`, `downloads`, `music`, `pictures`, `videos` | your personal folders | resolved from the Windows registry, not guessed by name — this also catches folders you moved to another drive |
| `saved-games` | `Documents\Saved Games` | |
| `favorites` | Internet Explorer / old-Edge favourites | kept as a plain folder of shortcuts |
| `onedrive` | OneDrive files **already downloaded to that PC** | cloud-only files are skipped and listed, never downloaded |
| `bookmarks` | Chrome, Edge, Brave, Opera, Vivaldi bookmarks | never passwords/cookies/payment data |
| `firefox` | Firefox bookmarks and history | into a new Linux Firefox profile named `windows-import`; passwords only with the explicit opt-in |
| `wallpaper` | your own desktop picture | never Windows' own wallpapers or Spotlight images |
| `fonts` | fonts you installed yourself | never `C:\Windows\Fonts` |
| `wifi` | your saved Wi-Fi networks | names and security type always; passwords only with the explicit opt-in |
| `apps` | the list of programs you had | a list only — see §4 |
| `steam-games` | your installed Steam library | opt-in (can be very large); see §5 |

Destinations follow your XDG user directories (so a renamed or localised "Documents" folder is still
used correctly), with `onedrive` landing in `~/OneDrive (from Windows)` and generated items (bookmark
HTML, the report) in `~/Documents/Transferred from Windows/`.

## 4. Apps: a list, and an honest route for each one

Windows programs cannot simply be copied — a program is not a file, it is an installation spread
across the registry, `Program Files` and more. Instead, `lindos-transfer` builds a plain list of what
you had (from the Uninstall registry keys, or from `winget export`/`apps.json` when the USB kit
collected them) and, for each one, offers the closest honest match:

* **already included** in Lindos — nothing to do;
* a **native package** or **Flatpak**, installed through Lindos itself;
* the **Linux version of the same browser** (Chrome/Edge/Firefox);
* a **game launcher** (Steam, Lutris, Heroic, …);
* Lindos' own **Windows-app support** (a Wine/Proton "recipe", or `winget install` with hash
  verification and no override — see [WINGET.md](WINGET.md));
* a `.desktop` **shortcut to the website**, when that is genuinely the modern equivalent;
* **your own licensed Windows**, over [WinApps](WINAPPS.md) or the [Windows VM](VM.md), for the
  handful of things that need it;
* or, honestly, **"not supported on Lindos yet"** — for example a kernel-anti-cheat game (whether it
  ever runs on Linux is its publisher's decision, and no date is given), which is shown with its
  [play-anywhere route](GAMING.md) instead of a fake "installed" checkmark.

**Nothing is installed without you choosing it**: by ticking items in a saved plan file before running
`lindos-transfer install-apps --plan plan.json` (Lindos Setup no longer has an Apps page; the installer
already installed Lindos' own launchers and Mode apps).

## 5. Steam games

The transfer always records which Steam games you had installed (from `libraryfolders.vdf` and each
game's `appmanifest_*.acf`). Copying the actual game **files** is opt-in (`-IncludeSteamGames` / the
Steam item's checkbox), because a library can be very large. After copying:

1. Open Steam on Lindos (install it first if you have not already — Lindos offers this on the Apps
   page).
2. For a game that also has a **native Linux build**, first right-click it → Properties →
   Compatibility → **untick** "Force the use of a specific Steam Play compatibility tool" if you want
   the Linux build, or **tick it and pick a Proton version** if you want to keep using the Windows
   build.
3. Press **Install**: Steam finds the files already on disk and only downloads what is missing or
   different, instead of downloading the whole game again.

Kernel-anti-cheat titles are still listed with their [play-anywhere route](GAMING.md), never claimed
to simply work because the files were copied.

## 6. Using the GUI wizard (`lindos-transfer-gui`)

The wizard walks through seven pages, matching the two paths in §2:

1. **Welcome** — what the tool does and does not do.
2. **Where are your Windows files?** — detected partitions and transfer folders, "Use a transfer
   folder…" to browse to one by hand, and "Make a transfer USB for another PC…" to prepare a USB
   stick with the Windows-side kit (§2B).
3. **Whose files?** — the Windows user account to copy (most PCs only have one).
4. **What to bring** — every category with its file count and size, all ticked by default except
   Steam games; the Firefox-passwords opt-in (asked again, in plain language, before it is turned on).
5. **Your apps** — every detected program with its offered route (§4); pick which ones to act on, and
   which route when more than one applies.
6. **Transferring your files** — live progress (current item, a progress bar, a plain-language log),
   then an optional prompt to install the apps you selected.
7. **Done** — a summary (files copied, skipped, problems) and plain-language next steps (e.g. "import
   your bookmarks from `Bookmarks - Chrome.html`"), plus "Open folder" for the destination.

Nothing is copied and nothing is installed before you reach the Transfer page and it starts running;
every earlier page only reads.

## 7. Using the command line (`lindos-transfer`)

```
lindos-transfer sources [--json]                       detected Windows partitions and transfer folders
lindos-transfer mount <device> [--json]                 mount a Windows partition read-only
lindos-transfer users --from <PATH> [--json]            Windows user accounts found at PATH
lindos-transfer plan  --from <PATH> [--user NAME] [--only CAT,…] [--exclude CAT,…] [--dest DIR]
                      [--firefox-passwords] [--json] [-o plan.json]
lindos-transfer run   (--plan plan.json | --from PATH [--user NAME] [--only …]) [--yes] [--dry-run]
                      [--json-progress]
lindos-transfer apps  --from <PATH> [--user NAME] [--json]
lindos-transfer install-apps --plan plan.json [--yes] [--dry-run]
lindos-transfer make-usb-kit <DIR>                       copies the Windows-side kit to DIR
lindos-transfer report [--json]                          the last transfer's report
```

`PATH` is either a mounted Windows root (contains `Windows/` and `Users/`) or a transfer folder made
by the USB kit (contains `lindos-transfer.json`). Exit codes: **0** ok, **1** error, **2** usage,
**4** nothing to transfer.

A typical scripted run:

```bash
lindos-transfer plan --from /media/alice/OS --user alice -o plan.json
# edit plan.json by hand if you want to untick anything, then:
lindos-transfer run --plan plan.json --yes --json-progress
lindos-transfer install-apps --plan plan.json --yes
```

## 8. Troubleshooting

* **"Windows is hibernated (Fast Startup)"** — restart Windows and do a full shutdown
  (`shutdown /s /t 0`, or hold Shift while clicking Shut down) before transferring, or continue anyway
  and accept that the files might be a few minutes out of date. Restarting is always safe even with
  Fast Startup on; only *shutting down* is affected.
* **A partition shows "BitLocker"** — unlock it first with your 48-digit recovery key (a file
  manager's unlock prompt, or `udisksctl unlock -b /dev/<partition> --read-only` in a terminal). If it
  says the key cannot be used for a "partially decrypted" or "clear key" device (common on a fresh
  Windows 11 24H2 local-account install), use the USB kit instead — it runs on Windows itself, where
  no key is needed.
* **"N files are only in OneDrive"** — sign in to OneDrive on Lindos and download them there, or on
  Windows make them "Always keep on this device" and run the transfer again.
* **The Windows-side kit says it "cannot run on this PC"** — a company policy or Smart App Control is
  blocking scripts. Copy your folders to the USB stick by hand in File Explorer instead, or use the
  same-PC path if this machine also has Lindos installed.
* **A FAT32 USB stick skipped some files** — any file over 4 GB cannot exist on FAT32; use an exFAT or
  NTFS drive, or copy that file separately.
* **Wi-Fi passwords were not included even though you asked** — Windows only puts plaintext passwords
  in the export when the step runs as administrator; the kit will have asked for that permission. The
  network **names** always come across either way, and Lindos asks for the password the first time you
  connect.

## 9. Where things are (for maintainers)

```
packages/lindos-transfer/
  root/usr/bin/lindos-transfer                       CLI entry point
  root/usr/bin/lindos-transfer-gui                   GUI entry point
  root/usr/lib/lindos-transfer/lindos_transfer/       the Python package (CLI logic + gui.py)
  root/usr/share/lindos/transfer/app-map.json         Windows-app -> Lindos-action map (§4)
  root/usr/share/lindos/transfer/windows/             the Windows-side kit (§2B): LindosTransfer.ps1,
                                                       LindosTransfer.cmd, README.txt
  root/usr/share/applications/lindos-transfer.desktop
  root/usr/share/icons/hicolor/scalable/apps/lindos-transfer.svg
  tests/                                              hermetic pytest (plus a real PowerShell parse
                                                       check of LindosTransfer.ps1 on Windows)
```

See [`SPEC-WINDOWS.md`](../SPEC-WINDOWS.md) §29 for the full binding contract (JSON shapes, the
transfer-folder manifest, the exact `robocopy` flags the Windows-side kit uses, and more).
