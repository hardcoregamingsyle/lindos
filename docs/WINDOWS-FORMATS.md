# Every Windows file type: "If Windows can open it, Lindos can open it — or tells you why not"

> This extends [WINDOWS-APPS.md](WINDOWS-APPS.md) (`.exe`/`.msi`/`.bat`/`.lnk`, recipes, runners,
> the doctor) to **every other kind of file Windows double-clicks**: Store-style app packages
> (`.msix`/`.appx`), Windows-Installer patches (`.msp`), registry files (`.reg`), scripts
> (`.ps1`/`.vbs`/`.wsf`), Internet shortcuts (`.url`), screen savers and Control Panel items,
> driver/setup information files (`.inf`), cabinet archives (`.cab`), disk images
> (`.iso`/`.img`), ClickOnce apps, DOS programs and 16-bit Windows programs. One rule holds for
> all of them: **`lindos-run` never lies about what it did.** Every file type has an honest
> status — `works`, `partial` (runs with limits) or `unsupported` — and unsupported files are
> *explained*, in plain language, never "tried anyway" in a way that looks like it worked.

## 1. How it decides what a file is

`lindos-run <file>` looks at the file's **content**, not just its name (a downloaded installer
is often called `setup.exe` or `download.bin` — the extension lies far more often than the
bytes do):

* an `MZ` header → parsed as a Windows executable (32/64-bit `PE`, 16-bit `NE`, an old
  DOS-extender `LE`/`LX`, or plain DOS if none of those match);
* an OLE compound-document header → a Windows Installer file (`.msi`/`.msp`/`.mst`, told apart
  by name);
* a ZIP that contains `AppxManifest.xml` or `AppxBundleManifest.xml` → an MSIX/APPX app package
  or bundle (never guessed from the `.msix`/`.appx` extension alone — Store uploads and other ZIPs
  use the same extensions);
* `EXPH`/`EXSH`/`EXBH` at the very start → a Microsoft-Store-encrypted package;
* `MSCF` → a cabinet (`.cab`); `CD001`/`NSR0x` → an ISO9660/UDF disk image;
* anything else falls back to the file name — but only as a last resort.

`lindos-run --info <file>` prints exactly what it decided, without running anything:

```
$ lindos-run --info Update.msp
{
  ...
  "format": {"id": "msp", "label": "Windows Installer patch", "status": "partial",
             "handler": "msiexec-patch", "note": "…", "reason": "OLE2 compound document, suffix .msp"}
}
```

`status` is always one of:

| Status | Meaning |
|---|---|
| **works** | Runs the same as on Windows (or close enough that Lindos is confident) |
| **partial** | Runs, but with a real, named limitation — read the `note` |
| **unsupported** | Cannot run on Wine/Linux at all; the `note`/on-screen message says why and what to do instead |

Exit codes: `0` ok · `1` error (or the user said no) · `2` usage error · **`3` unsupported,
explained** (the reason was already printed/shown — nothing was silently skipped).

## 2. The format table

| You double-click… | What Lindos does | Status |
|---|---|---|
| `.exe`, `.msi`, `.bat`/`.cmd`, `.lnk` | The normal flow — see [WINDOWS-APPS.md](WINDOWS-APPS.md) | works / partial |
| `.exe` built with .NET (has a CLR header, or a native "apphost" next to a `.runtimeconfig.json`) | Runs through Wine/Proton; `wine-mono` covers a lot of .NET Framework apps. .NET (Core) 3+ apps need Microsoft's own .NET runtime for Windows inside the C:\ drive — Lindos never installs it for you (see §7) | partial |
| `.exe` built for ARM Windows | Explained: get the x64/x86 build of the program instead — Wine cannot translate ARM instructions | unsupported |
| `.dll`, `.ocx`, a driver `.sys`, `.efi` | Explained: "a library/driver, not a program" — there is nothing to run | unsupported |
| `.msp` (Windows Installer **patch**) | `msiexec /p <patch> REINSTALL=ALL REINSTALLMODE=omus`, run inside the **C:\ drive of the program being patched** (you pick it, or Lindos shows a list) — never `/i`, which Windows Installer refuses on a patch file | partial |
| `.mst` (Installer **transform**) | Explained: it only makes sense together with the `.msi` it modifies — `lindos-run app.msi TRANSFORMS=x.mst` | partial |
| An `.exe` "installer" that downloads an app package (`.msix`) and asks Windows to install it | The package is caught when the installer exits and offered through the row below, or explained once — §3.1 | partial |
| `.msix`, `.appx`, `.msixbundle`, `.appxbundle`, `.msixupload`/`.appxupload` | Unpacked into their own C:\ drive and added to the Start Menu — §3 | partial |
| `.emsix`/`.eappx`/`.emsixbundle`/`.eappxbundle` (Store-encrypted) | Explained: locked to the Microsoft Store, no program outside Windows can open it | unsupported |
| `.msixvc` (Xbox/PC Game Pass game package) | Explained: an encrypted Xbox/GDK package, not a Wine-runnable program | unsupported |
| `.appinstaller` | Shows the publisher and download server, asks first, then behaves like an MSIX file — §4 | partial |
| `.ps1` | **PowerShell 7** (`pwsh -NoProfile -File`), only after you confirm — §5 | partial |
| `.vbs`/`.vbe`/`.wsf` | `wine wscript` (a console `.vbs` uses `cscript //nologo` instead) | partial |
| `.reg` | A Windows-style confirmation that **always lists every key and value it would delete**, then `wine regedit /S` into the C:\ drive you pick | works |
| `.url` (Internet Shortcut) | Opens `http(s)://`, `mailto:` and `ftp://` links with your default browser/mail app; anything else (an app-launching URI, a local path) is explained, never opened blindly | works |
| `.scr` (screen saver) | `wine <file> /s` — plays it like Windows would | works |
| `.cpl` (Control Panel item) | `wine control <file>` | partial |
| `.inf` — **software** setup information | `wine rundll32 setupapi.dll,InstallHinfSection DefaultInstall 132 <file>` | partial |
| `.inf` — **driver** information | Explained: Windows drivers do not work on Linux; run `lindos-drivers detect` for what Lindos can install for your hardware instead | unsupported |
| `.cab` | `cabextract`s it into a new folder next to the file (or in Downloads), then opens that folder | works |
| `.msu` (Windows Update package) | Explained: not applicable to Lindos (offers to extract it with `.cab` handling instead) | unsupported |
| `.iso`, `.img` (disk image) | Mounted **read-only**, then behaves like Windows AutoPlay — §6 | works |
| `.application`, `.appref-ms` (ClickOnce) | Runs only if the C:\ drive already has Microsoft .NET Framework 4.x — §7 | partial |
| DOS programs (`.com`, `.pif`, old `.exe`) | **DOSBox** — §8, no Wine, no C:\ drive at all | works |
| 16-bit Windows programs (`.exe` with an `NE` header) | Wine, in the C:\ drive layout your Wine version needs — §8 | partial |

## 3. MSIX / APPX app packages

MSIX is the Windows 10/11 app-packaging format (also called APPX). Lindos treats a package's
content, not its extension, as the truth — a `.msix` bundle is really a ZIP file, and a bundle
is told apart from a single package by which manifest it contains.

**What can run:** only **win32** parts of a package — desktop programs packaged for
distribution, marked `Windows.FullTrustApplication` (or an equivalent packaged-classic-app entry
point) in the package's manifest. A **UWP/WinUI** app (the kind you can only get from the
Microsoft Store, built against the Windows Runtime) has no equivalent under Wine and is
explained, not attempted: *"Wine has no UWP app model — try the web version, a Linux
alternative, or the Windows virtual machine."* A package that mixes both kinds installs only its
win32 parts and says so. A package that is only a "framework" or "resource" component (not an
app by itself) is explained the same honest way.

**Installing one:**

```
$ lindos-run ContosoApp.msix
Install Contoso App?

Publisher: Contoso Ltd.
Version: 1.2.0 (x64)
Signature: Signed (not verified by Lindos)

Install?  [Install]  [Cancel]
```

Lindos shows the signature status (Lindos never verifies a package's cryptographic signature
itself — that would need a Store-trusted certificate chain it does not have) and any Store
signals it recognises (packages that check their licence with the Microsoft Store during
installation are shown a "Try anyway" instead of a plain "Install", because they may refuse to
start under Wine). A confirmed install:

1. gets its **own C:\ drive**, named after the package's *package family name* (so re-installing
   the same app, or a later version of it, reuses the same drive);
2. is unpacked with a zip-slip-safe extractor (paths that try to escape the install folder, or
   that only differ by case on a case-sensitive filesystem, are refused) into
   `drive_c\Program Files\WindowsApps\<package full name>\`, with the package's `VFS\...` folders
   materialised into the right Windows folder (`Program Files`, `System32`, `ProgramData`, …) —
   **inside that one C:\ drive only**, never into a shared prefix;
3. gets a Start-Menu entry (and a Lindos Settings → Windows apps record) for each win32 app it
   contains, with its own icon;
4. the package's own registry settings are imported too, when `python3-hivex` is installed (the
   doctor tells you if it is not — nothing else about the install depends on it).

Nothing is installed without your say-so, and packages with **no way to run under Wine** never
create a C:\ drive at all.

**Big packages.** Desktop apps such as Electron programs come as packages of hundreds of MB.
Unpacking streams the files one megabyte at a time (nothing is loaded into memory), the zip-slip
and "zip bomb" guards above stay on, and the question tells you the size up front:
*"Disk space: about 612.3 MB once unpacked"*. If the disk cannot take it, Lindos says so **before**
asking — *"Not enough free disk space: the app needs about … but only … is free on that drive"* —
instead of failing after a long unpack. The progress window moves once per percent, and half-unpacked
folders left by an install that was killed (power cut, `kill -9`) are deleted a day later.

### 3.1 Installers that download an app package ("MSIX bootstrappers")

Many current Windows "installers" are not the installer at all. The Claude desktop app's setup
program, as first seen on Lindos, is one example: a small program that downloads a large `.msix`
into the Windows temp folder and then asks Windows to install it. Wine has no Windows app-deployment service, so this
used to end in a Windows-looking dialog — roughly *"There is no Windows program configured to
open this type of file"* — and a file manager showing a temp folder with a huge package and a log file.
Lindos now handles the hand-off itself, in two layers:

1. **Before the installer runs** (once per C:\ drive, about a second): the C:\ drive gets a file
   association for `.msix`, `.appx`, `.msixbundle`, `.appxbundle`, `.msixupload`/`.appxupload`,
   the Store-encrypted variants, `.appinstaller` and the `ms-appinstaller:` link type. It is not a
   program that installs anything — it only *writes down* which package the installer asked for
   (`C:\ProgramData\Lindos\handoff.log`) and returns, so the installer's "open this package" call
   succeeds instead of failing. Nothing else runs at that moment.
2. **After the installer exits:** Lindos looks at what was written down and at what is new in the
   C:\ drive's Temp, Downloads and Desktop folders (packages that were already there are never
   offered again), and decides by the package's *content*:

   | What the installer left behind | What you see |
   |---|---|
   | A **desktop app** package | The normal §3 question, with a first line saying which installer downloaded it — *"claude.exe downloaded this app and asked Windows to install it… Install Claude?"* — including publisher, version, signature status and disk space. Say yes and it is unpacked into its own C:\ drive and added to the Start Menu. Nothing is installed without your yes (`--yes` for scripts). |
   | A **UWP/WinUI** app, a **Store-encrypted** package, or a **damaged/half-downloaded** file | **One** plain message naming the app and the file, why it cannot run here, and what to do instead: a Linux or web version, `lindos-compat winget search "<name>"` (many apps also offer a normal `.exe`/`.msi`), or the Windows virtual machine (`lindos-vm`) / your own Windows over RDP (`lindos-winapps`). The exit code is `3` (explained). |
   | Only frameworks or resource packages (Visual C++ runtimes, language packs) | Nothing extra — they are components, not the app. |
   | An `ms-appinstaller:` link | Explained, never followed: Microsoft turned these one-click installs off in 2023 because criminals abused them. Download the package from the publisher's site and open it instead. |

   In every case **Lindos never opens a file manager on the installer's temp folder**, and the
   installer's own "exit code 1" dialog is not shown on top of a hand-off it already dealt with.

`lindos-run --info setup.exe` lists the strings that make an installer look like such a
bootstrapper (`"msix_handoff": {"hints": [...]}`), and `lindos-compat recipes show claude-desktop`
has the honest status of that app (**partial**: the hand-off and unpacking are covered by tests,
but nobody has yet confirmed on real hardware that the unpacked Electron app runs well).

**What this cannot do.** If the installer *waits for Windows to report the app as installed* (a
check Wine cannot answer) it will still time out and complain — Lindos then offers the package
afterwards, as above. If it deletes its download before it exits there is nothing left to offer.
The hand-off is registered only for Wine C:\ drives (not Proton/`umu` game drives or Bottles); the
after-exit check works for Proton drives too. Windows itself would apply the package's URL and
file-type registrations; Lindos does not, so features such as "sign in through the browser and
return to the app" may not work.

## 4. `.appinstaller` files

An `.appinstaller` file is not the app — it is a small XML pointer that says *"download this
MSIX package from this web address."* Because criminals abused that (Microsoft turned off
one-click installs from these files in 2023), Lindos:

* only downloads over **HTTPS**, from a plain host name (never a bare IP with embedded
  credentials, never `http://`, a network share or a local path) — anything else is explained
  and nothing is downloaded;
* always shows the publisher and the server name and asks first — there is no silent/automatic
  mode;
* downloads into an isolated, `0700` **quarantine folder** (`~/.cache/lindos/quarantine/…`),
  deleted again once the install finishes (successfully or not);
* checks that what was actually downloaded is the same app the `.appinstaller` file promised
  (name, publisher, and version when given) — a mismatch deletes the download and installs
  nothing;
* then follows the exact same honest MSIX install flow as §3.

Lindos never registers the `ms-appinstaller:` link type with your desktop or browser. Inside a Wine
C:\ drive it is registered only as the *recorder* described in §3.1 (so an installer's call does
not fail); it records only that a link was handed over (never the link text, which could hold commands) and the link is **never downloaded**.

## 5. PowerShell scripts (`.ps1`)

Windows Explorer's own default for a `.ps1` file is to **open it in an editor**, not run it —
that is what Lindos does too (`.ps1`'s default file-manager action is the text editor,
Mousepad). "Run with PowerShell" is a right-click action and `lindos-run script.ps1` runs it
directly, but **always asks first**: Linux's PowerShell 7 has no execution-policy gate the way
Windows does, so Lindos' own confirmation is the only thing standing between you and a script
you did not mean to run. The dialog also says plainly that Windows-only commands (services, the
registry, WMI/CIM, Windows features) will not work under Linux's PowerShell — many scripts
written for Windows will still show errors for those parts.

## 6. Disk images (`.iso`, `.img`)

Opened the way Windows' **AutoPlay** used to work, and just as read-only:

1. mounted through `udisksctl` (never written to — Lindos never edits, and never "fixes", the
   image);
2. its `autorun.inf` is read (if present) for a friendly label and a program to offer;
3. if a setup program exists (from `autorun.inf`, or a top-level `setup.exe`/`install.exe`),
   Lindos asks — *"Run setup.exe from Game Name?"* — **it is never started automatically**,
   whichever way you opened the disc;
4. either way the folder opens in the file manager, and the terminal prints the exact commands
   to eject it again.

## 7. ClickOnce (`.application`, `.appref-ms`)

ClickOnce apps need Microsoft's **.NET Framework 4.x** installed inside the C:\ drive that runs
them. Its licence is tied to a licensed copy of Windows, so **Lindos never installs it for
you** — if the C:\ drive does not already have it, the file is explained instead of failing with
a confusing Wine error, and the message tells you how to add it yourself
(`winetricks dotnet48`) if you own a Windows licence, or to look for the program's normal
installer on its own website (most ClickOnce apps still ship one).

## 8. DOS programs and 16-bit Windows programs

**DOS** programs (`.com`, `.pif`, and old `.exe` files with a plain DOS or `LE`/`LX` header)
run in **DOSBox** (DOSBox-X preferred, then DOSBox Staging, then classic DOSBox — whichever is
installed) — never Wine, and **never get a C:\ drive at all**; DOSBox brings its own virtual PC.

**16-bit Windows** programs (an `NE` header) still need Wine, but *which* C:\ drive they get
depends on your Wine build, because Wine's own 16-bit support changed underneath WoW64:

* an older ("classic") Wine gets a dedicated 32-bit C:\ drive just for 16-bit programs;
* Wine 10.16/11.0 and newer run 16-bit programs in a normal 64-bit C:\ drive;
* a Wine *between* those (new-style 64-bit-only Wine, before 16-bit support was restored) simply
  cannot run 16-bit programs — Lindos says so plainly and points at installing a newer Wine,
  rather than failing with an obscure error.

## 9. `./setup.exe` from a terminal

Typing `./setup.exe` (or any Windows program's path) directly into a terminal works too, the
same way Linux's own binfmt_misc lets you run a script by name: the kernel hands the file to
`lindos-run` automatically. This uses a `binfmt_misc` rule (`lindos-pe`, registered from
`/usr/lib/binfmt.d/lindos-pe.conf`) that only matches files starting with `MZ` — since that also
catches DLLs, drivers, `.efi` files and Control Panel items (any executable-flagged `MZ` file,
which NTFS/USB mounts mark generously), the wrapper re-checks the file first and refuses
anything that is not a program, with the same explanation `lindos-run` would give.

```
lindos-compat binfmt status [--json]     # registered / enabled / masked, plus any conflicts
lindos-compat binfmt enable              # turn it on (persists across reboots)
lindos-compat binfmt disable             # turn it off (persists — masks the rule)
```

If another package also registers an `MZ` handler (Wine's own `wine-binfmt`, `binfmt-support`,
qemu's user-mode emulation), Lindos reports honestly which one currently wins (binfmt_misc always
prefers the entry registered most recently) — it never disables or overwrites someone else's
rule. Turning `lindos-pe` off never touches `systemd-binfmt` itself (which would flush every
other program's binfmt rule too, not just this one).

## 10. Case-insensitive C:\ drives

Windows file names are case-insensitive; Linux's normally are not. Every *new* C:\ drive Lindos
creates gets an empty `drive_c` folder marked case-insensitive (`chattr +F`, the same ext4
**casefold** feature some Android/Linux phones use) *before* Wine populates it — when the
filesystem supports it, this measurably speeds up Wine's own file lookups on big Program Files
trees, and it happens invisibly, the moment a new C:\ drive is created.

Most desktop installs do not have casefold-enabled ext4 by default: it has to be turned on when
the filesystem is made (`mkfs.ext4 -O casefold`), or with `tune2fs -O casefold` while the
filesystem is **not mounted** (from a live USB, for instance). Lindos never runs `tune2fs`
itself — the doctor only tells you whether it would help and how to do it, if you want to.
`lindos-compat doctor` reports one of:

| Doctor says | Meaning |
|---|---|
| `casefold: active` | New C:\ drives on this filesystem are already case-insensitive |
| `not enabled on this filesystem` | The kernel supports it, but this particular filesystem was not made with it — optional, only a speed-up |
| `kernel lacks CONFIG_UNICODE` | This kernel cannot do it at all (the Lindos kernel has it) |

## 11. Desktop integration

* **File types** — `lindos-compat formats [--json]` lists every type in §2 with its status and a
  short explanation; Lindos Settings → *Windows apps* shows the same table.
* **Double-click defaults** — every type in §2 (except `.ps1`, which defaults to the text
  editor like Windows, and disk images) opens with **Windows App Runner (Lindos)**; `.iso`/`.img`
  open with the **Disk Image AutoPlay (Lindos)** opener instead.
* **Thunar right-click actions** — *Run with Lindos (Windows app)*, *Run with Proton (game)*,
  *Open C:\ drive*, *Run with PowerShell*, *Install into a C:\ drive (MSIX)*, *Mount disk image*,
  *Merge into a C:\ drive (.reg)*.
* Lindos reuses the system's own MIME types wherever `shared-mime-info` already has them
  (`.exe`/`.msi`/`.lnk`/`.bat`/`.reg`/`.url`/`.ps1`/`.vbs`/`.cab`/`.iso`/`.img`/MSIX and friends);
  it only adds its own `application/x-lindos-*` types for the handful `shared-mime-info` does
  not know yet (`.msp`, `.mst`, `.cpl`, driver/setup `.inf`, encrypted/upload/GDK MSIX variants,
  ClickOnce, WSH `.wsf`, DOS programs).

## 12. Doctor additions

`lindos-compat doctor` also checks, alongside the Wine/Proton checks in
[WINDOWS-APPS.md](WINDOWS-APPS.md):

| Check | What it means when it fails |
|---|---|
| `dosbox` | Install with `sudo apt install dosbox-x` for DOS programs |
| `powershell` | Optional; installs PowerShell 7 from Microsoft's own repository (never a snap — Mint's version string is not one Microsoft's repository recognises, so the doctor gives the Ubuntu-24.04 repository directly) |
| `udisks2` | Needed to open `.iso`/`.img` disk images |
| `binfmt` | Whether `./setup.exe` works from a terminal, and any other program claiming the same file type |
| `casefold` | §10 |
| `wine-wow64` | Whether this Wine build can run 16-bit Windows programs, and how (§8) |
| `hivex` | Optional; `python3-hivex` lets MSIX installs bring their own registry settings along |

## 13. Games that need a Windows-only anti-cheat

If a game's `.exe` matches Lindos' per-title profile of a Windows-only kernel anti-cheat (Riot
Vanguard, Denuvo Anti-Cheat that blocks Linux, etc.), `lindos-run` refuses honestly instead of
trying and failing halfway through, and points at `lindos-game route <title>` for the real
options (official cloud streaming where one exists, or restarting into Windows) — see
[ANTI-CHEAT.md](ANTI-CHEAT.md) and [GAMING.md](GAMING.md).
