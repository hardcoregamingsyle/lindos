# lindos-winapps — seamless Windows apps over RDP

> **What it is, in one paragraph.** `lindos-winapps` makes individual Windows programs — Photoshop,
> Illustrator, Premiere, the Office apps — appear as **normal windows on your Lindos desktop**, each
> in its own window with its own menu entry, while the program actually runs on a **Windows backend**
> and is streamed to you over **RDP** (FreeRDP's RemoteApp). It is the honest way to run the Windows
> software that Wine/Proton cannot: it is real Windows, doing the work, shown seamlessly. You supply a
> **licensed Windows** and licensed apps; Lindos never enters your credentials and never bypasses
> licensing.

This follows the well-known [winapps-org/winapps](https://github.com/winapps-org/winapps) pattern and
is binding-consistent with [`SPEC-VM.md`](../SPEC-VM.md) §20 / §22 and [`SPEC.md`](../SPEC.md) §0.1.

## 1. Honesty — read this first

* **You supply Windows and the apps.** `lindos-winapps` needs a running Windows backend with the
  program **already installed and licensed** inside it. Lindos does not download Windows, does not buy
  or install Adobe/Office for you, and does not bypass any activation or subscription.
* **Lindos never enters your credentials.** `setup` writes a config file but **never prompts for or
  stores your RDP/Windows password**. FreeRDP handles the credential itself — you type it into
  FreeRDP's own prompt, or point it at a credential file/`RDP_PASS` you create. This is the base
  safety rule ([SPEC.md](../SPEC.md) §0.1): Lindos does not type your passwords for you.
* **It is RDP, not magic.** The app runs on Windows and is streamed; it needs the backend up and
  reachable. It is excellent for creative and office apps; it is **not** a way around anti-cheat (that
  lives in [ANTI-CHEAT.md](ANTI-CHEAT.md)), and latency/colour fidelity depend on your link to the
  backend.

## 2. The backend — where Windows actually runs

`lindos-winapps` talks to a Windows instance over RDP. Two backends are supported:

* **`libvirt` (default)** — the [`lindos-vm`](VM.md) Windows VM, by convention named **`RDPWindows`**.
  This is the recommended path: one honest local Windows VM serves both full-desktop use (`lindos-vm`)
  and seamless apps (`lindos-winapps`). Enable Remote Desktop inside that Windows once.
* **`podman` (advanced)** — a `dockur/windows` container for users who already run that. Suggested,
  not required.

The package depends on `python3`, `lindos-core` and `freerdp3-x11 | freerdp2-x11`; it **Recommends**
`lindos-vm | libvirt-daemon-system` and **Suggests** `podman`.

## 3. The CLI

Exit codes: **0** ok · **1** error · **2** usage · **3** backend-unreachable.

```
lindos-winapps setup            write ~/.config/lindos/winapps/winapps.conf (backend, host, user,
                                domain, flags) — never the password
lindos-winapps check            backend reachable? FreeRDP present? RDP port open?
lindos-winapps list             installed Windows apps (catalog + probing the backend when reachable)
lindos-winapps install <id>     create a menu entry (.desktop) for the app
lindos-winapps run <id> [-- args]   launch the app via FreeRDP RemoteApp
lindos-winapps remove <id>      remove the menu entry
lindos-winapps --version
```

### setup

`lindos-winapps setup` writes `~/.config/lindos/winapps/winapps.conf` with the backend type, the
backend host/IP, the RDP **username** and domain, and FreeRDP flags. It deliberately **does not**
capture a password: it points you at `~/.config/lindos/winapps/` and FreeRDP's own credential
handling (an interactive FreeRDP prompt, a documented `RDP_PASS` environment variable, or a file you
create yourself). You keep control of the secret; Lindos never sees it.

### check

`lindos-winapps check` verifies FreeRDP is installed, the backend is up (VM running / container up),
and the RDP port answers. Anything not ready is reported plainly; a down backend exits **3**.

### list / install / run / remove

`list` shows apps from the shipped catalog and, when the backend is reachable, what is actually
installed on it. `install <id>` writes `~/.local/share/applications/lindos-winapp-<id>.desktop`
(`Exec=lindos-winapps run <id>`, with the real app icon when available) so the program shows up in the
Whisker menu like any native app. `run <id>` launches it through FreeRDP RemoteApp
(`xfreerdp /app:… /app-cmd:…`), so a **single window** appears — not a whole Windows desktop.
`remove <id>` deletes the menu entry.

## 4. The app catalog — `apps.json`

`/usr/share/lindos/winapps/apps.json` is the known-app catalog. Each entry has an `id`, display
`name`, the Windows `rdp_path` to the executable, `categories` and an `icon`, plus an honest note that
it **requires a licensed Windows with that app installed** in the backend. Shipped entries:

| id | App | Note |
|---|---|---|
| `photoshop` | Adobe Photoshop | needs licensed Windows + Photoshop installed in the backend |
| `illustrator` | Adobe Illustrator | needs licensed Windows + Illustrator |
| `premiere` | Adobe Premiere Pro | needs licensed Windows + Premiere |
| `office-word` | Microsoft Word | needs licensed Windows + Office |
| `office-excel` | Microsoft Excel | needs licensed Windows + Office |
| `office-powerpoint` | Microsoft PowerPoint | needs licensed Windows + Office |
| `office-outlook` | Microsoft Outlook | needs licensed Windows + Office |
| `explorer` | Windows File Explorer | generic; always present in Windows |

There is **no license bypass** anywhere in the catalog or the tool — every note simply tells you what
you must already own and have installed.

## 5. Typical first run

1. Build the honest Windows VM and install/activate your licensed Windows in it — see [VM.md](VM.md)
   — naming it `RDPWindows`, and **enable Remote Desktop** inside that Windows.
2. Install your Windows apps (Photoshop, Office, …) in that VM and sign in **there** with your own
   Adobe/Microsoft account. Lindos is not involved in that sign-in.
3. `lindos-winapps setup` — record the backend, host and your RDP username (no password).
4. `lindos-winapps check` — confirm FreeRDP, the backend and the RDP port are all green.
5. `lindos-winapps install photoshop` (and any others) — they appear in your menu.
6. Launch Photoshop from the menu; it opens as a single window on your Lindos desktop, running on
   Windows over RDP.

## 6. When to use this vs. Wine/Proton

* **Try Wine/Proton first** for `.exe`/`.msi` — it is a translation layer with no VM overhead. See
  [WINDOWS-APPS.md](WINDOWS-APPS.md). Photoshop CS6 and some CC-2021-era apps work there (marked
  *partial*); most utilities work well.
* **Use `lindos-winapps`** when the app is unreliable or impossible under Wine (current Adobe CC,
  Microsoft 365 Click-to-Run) but you have a licensed Windows to run it on. It is real Windows, so
  fidelity is exact — at the cost of running a Windows backend.

## 7. See also

* [VM.md](VM.md) — the honest Windows VM that is the default backend.
* [WINDOWS-APPS.md](WINDOWS-APPS.md) — the no-VM Wine/Proton path and the Adobe/Office recipe status.
* [ANTI-CHEAT.md](ANTI-CHEAT.md) — why neither a VM nor RDP defeats VM-blocking anti-cheat.
