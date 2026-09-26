# Getting Windows programs with winget

> **What this is.** `lindos-compat winget` reads Microsoft's public **winget** catalogue (the same
> list of programs Windows' own "App Installer" uses) and installs the program you pick with
> `lindos-run`, the same way double-clicking its `.exe`/`.msi` would. It is **not** the Microsoft
> Store — Lindos never runs Microsoft's winget client, never signs in, and never downloads
> anything Microsoft-licensed (fonts, Store apps, .NET Framework). It downloads only the file the
> catalogue names, straight from the publisher's own server, over a secure connection, and
> **refuses to run it if the file's fingerprint (SHA-256) does not match exactly what Microsoft's
> catalogue says it should be** — there is no "install anyway" button for that.

## 1. Command line

```
lindos-compat winget search <name> [--json] [--limit N]
lindos-compat winget show <PackageIdentifier> [--version V] [--json]
lindos-compat winget install <PackageIdentifier> [--version V] [--arch x64|x86] [--prefix NAME]
                             [--interactive] [--accept-package-agreements] [--dry-run] [--json]
lindos-compat winget list [--json]
lindos-compat winget update-index [--json]
```

* **search** — look a program up by name, id, a command it installs (e.g. `msedge`) or a
  catalogue tag (e.g. `browser`). Prints the best matches first.
* **show** — a package's licence, publisher, homepage, every installer it offers, and (marked
  with `->`) the one Lindos would actually use.
* **install** — downloads the chosen installer (hash-checked) and runs it through `lindos-run`
  into a C:\ drive, exactly like double-clicking a downloaded `.exe`/`.msi` would. Prints the
  licence and any agreements first and asks you to confirm, unless you pass
  `--accept-package-agreements` (needed for scripts).
  * `--prefix NAME` picks the C:\ drive; the default is a new one named after the program.
  * `--arch x64` / `--arch x86` forces an architecture (the default is the best one for your PC).
  * `--interactive` shows the installer's own window instead of installing silently.
  * `--dry-run` prints exactly what would be downloaded and run — nothing is downloaded or run.
* **list** — programs you installed this way, and whether a newer version is in the catalogue.
* **update-index** — refresh the local copy of the catalogue right now (it otherwise refreshes on
  its own, at most every 15 minutes).

Every command also has `--json` for scripts and for Lindos Settings → *Windows apps → Get apps
with winget*, which calls this exact CLI.

### Examples

```
$ lindos-compat winget search notepad
Name         Id                       Version    Match
Notepad++    Notepad++.Notepad++      8.9.8.1

Details: lindos-compat winget show <Id>   |   Install: lindos-compat winget install <Id>

$ lindos-compat winget show Notepad++.Notepad++
Notepad++ 8.9.8.1  (Notepad++.Notepad++)
  Publisher: Notepad++
  Homepage:  https://notepad-plus-plus.org/
  Licence:   GPL-2.0-or-later
  Installers:
   -> x64     wix              -        en-US  https://.../npp.8.9.8.1.Installer.x64.exe
      x64     zip/portable     -               https://.../npp.8.9.8.1.portable.x64.zip
   (-> is the one Lindos would use)

Install it:  lindos-compat winget install Notepad++.Notepad++

$ lindos-compat winget install Notepad++.Notepad++
Package:    Notepad++ 8.9.8.1  (Notepad++.Notepad++)  by Notepad++
Installer:  wix, x64, from downloads.notepad-plus-plus.org
C:\ drive:  notepad
Licence:    GPL-2.0-or-later
Note:       Integrity: the file is checked against the SHA-256 in the hash-checked winget
            catalogue before it runs. The publisher's code signature is not verified.
Install Notepad++ 8.9.8.1 into the C:\ drive 'notepad'? You agree to the licence terms shown
above. [y/N] y
Downloading from downloads.notepad-plus-plus.org ...
Download verified (SHA-256 matches the winget manifest).
Running the installer through lindos-run ...
Notepad++ 8.9.8.1: installed.
Added to the Start Menu / Lindos Settings > Windows apps: notepad-plus-plus
```

## 2. Where the files come from, and how they are checked

Nothing here is Lindos-invented: it is the same public data Microsoft's own winget client reads,
fetched over a plain HTTPS connection.

1. **The catalogue** (`source2.msix`, about 3.7 MB, from `cdn.winget.microsoft.com`) is a small
   signed package holding a SQLite index of every package name and id — this is what `search`
   looks through. It is cached and only re-checked at most every 15 minutes.
2. Picking a package looks up its **version list**, which carries a SHA-256 fingerprint for every
   version's install instructions ("manifest"). Lindos downloads that list and **refuses it** if
   its own fingerprint does not match the one named in the catalogue.
3. The **manifest** for the chosen version is downloaded the same way, and checked against *its*
   fingerprint from the version list.
4. The manifest names the **installer's own download link and SHA-256**. Lindos downloads it,
   over HTTPS only (a plain `http://` link, or one that redirects away from HTTPS, is refused
   outright), computes its SHA-256, and **only if the two match exactly** does it rename the file
   from a harmless temporary name to something runnable and hand it to `lindos-run`. On a mismatch
   the file is deleted, and the error message shows both fingerprints so you can tell the
   publisher's website is offering something different. **There is no way to skip this check.**

If Microsoft's catalogue server cannot be reached the way Lindos expects, it falls back to reading
the same manifests straight from the `microsoft/winget-pkgs` project on GitHub (the catalogue's
public source repository) — a little slower, and GitHub limits how often an anonymous computer may
ask, but the same fingerprint checks apply.

## 3. Which installer Lindos picks (and why it can differ from Windows' own choice)

A package can offer several installers — different processor architectures, an "everyone" vs.
"just me" copy, an installer vs. a portable zip. Windows' own winget prefers the "everyone" copy
and, since a September 2026 update, prefers an MSIX package when one is offered. **Lindos chooses
differently, on purpose:**

1. **Architecture**: 64-bit, then 32-bit, then "any" — never an ARM-only build (Lindos PCs are
   Intel/AMD).
2. **"Just me" over "everyone"**: inside a C:\ drive, "everyone" still only means *your* Linux
   account, so the distinction is meaningless here, but "just me" installers ask fewer questions.
3. **Your language**, then U.S. English.
4. **The kind of installer**: a plain `.msi`/WiX installer first, then Inno Setup, then NSIS
   ("Nullsoft"), then a "Burn" bundle, then a plain `.exe`, then a portable zip. **An MSIX/APPX
   package is used only when a package offers nothing else** — Wine (which runs every other kind
   here) cannot install an MSIX package itself; Lindos unpacks the desktop program inside it
   instead, and says plainly when that program turns out to be a Store-only app that cannot run
   outside Windows at all.

`lindos-compat winget show <id>` always marks the installer it would use, so you can see this
before installing.

## 4. What Lindos refuses, and says so instead of pretending

* **Microsoft Store apps** (ids that look like `9NBLGGH4NNS1`) — the Store is not available on
  Linux, and Store packages are licensed to it; Lindos points you at the app's page on
  `apps.microsoft.com` instead, in case it also has a web version.
  Store client (`msstore`) installers are refused the same way.
* **Web apps ("PWA")** and **fonts** — winget lists these, but they are not Windows programs;
  Lindos names the homepage instead of pretending to "install" them, and never ships Microsoft's
  own fonts.
* **Plain `http://` downloads** — about one in 200 installers on the catalogue still uses an
  unencrypted link. Lindos refuses those (a file fetched that way could be swapped on the way to
  your PC) and suggests getting it from the publisher's site yourself.
* **ARM-only builds** — Lindos PCs run the x64/x86 build.
* Anything else winget itself would call "unsupported" is explained the same way, never attempted.

## 5. Things worth knowing before you click "yes"

* **Licence first.** The publisher's licence (and any extra agreement text) is always shown before
  installing; typing `yes` (or passing `--accept-package-agreements`) means you agree to it, the
  same as Windows' own winget.
* **Dependencies.** When a package needs another one first (for example a Visual C++ runtime),
  Lindos offers to install that one into the same C:\ drive first — it is never installed silently
  behind your back.
* **Administrator rights.** Under Wine, an installer that asks for administrator rights gets them
  without a prompt, inside that C:\ drive only — nothing outside it. Only install software you
  trust, same as you would on real Windows.
* **The publisher's code signature is not checked**, only the SHA-256 in the winget catalogue.
  That is exactly the same guarantee Microsoft's own winget gives you.
* **"Restart to finish"** — some installers ask Windows to restart. Nothing restarts on Lindos:
  just close and reopen the program (or the C:\ drive's programs) instead.
* **Portable programs** are not "installed" anywhere special: Lindos unpacks them next to where
  winget itself would (`AppData\Local\Microsoft\WinGet\Packages` inside the C:\ drive) and adds
  them to the Start Menu / Lindos Settings → Windows apps like anything else.

## 6. Where things are

| Path | Contents |
|---|---|
| `~/.cache/lindos/winget/index/` | the cached catalogue index (`index.db` + its ETag/checked time) |
| `~/.cache/lindos/winget/downloads/<Id>/<Version>/` | downloaded installers (verified before use) |
| `~/.local/share/lindos/winget-installed.json` | what `winget install` has installed, and into which C:\ drive |
| `~/.local/share/lindos/apps.json` | the shared Windows-apps database (Start Menu + Lindos Settings list); winget installs are tagged `source: "winget"` |

`LINDOS_HOME` / `LINDOS_ROOT` (see the main compatibility docs) are honoured everywhere above, so
tests and alternate profiles never touch your real home directory.

## 7. When something goes wrong

* *"Cannot reach cdn.winget.microsoft.com"* — check the internet connection; a saved copy of the
  catalogue is used if one exists, with a warning that it may be out of date.
  `lindos-compat winget update-index` forces a fresh check.
* *"does not match the fingerprint"* — the file at the publisher's link has changed since
  Microsoft's catalogue was last updated (this happens for a small number of "always latest"
  download links). Lindos will not run a file it cannot verify; try again in a few days, or get
  the program from the publisher's own website.
* *"none of its installers can be used here"* — every installer this package offers is one of the
  refusals in §4, or none fit your requested `--arch`; `winget show <id>` lists the reason for
  each one.
* Settings → Windows apps → Doctor also checks the pieces winget installs need (Wine, PowerShell
  for `.ps1`, `cabextract`) — see [WINDOWS-FORMATS.md](WINDOWS-FORMATS.md).
