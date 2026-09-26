# Updates: how Lindos stays current

> **What this is.** Once you have booted Lindos, you should never need to reinstall it to get new
> fixes. There are two update channels, and Lindos is honest about which is which: the **base
> system** (kernel, XFCE, Firefox, Wine, everything from Ubuntu/Mint's own repositories) already
> updates through **Mint's own Update Manager** — Lindos does not touch it, because it already
> works. **Lindos's own twelve `lindos-*` packages** are a separate thing: they only ever land on
> your machine at ISO-build time today, and `lindos-update` is what changes that — a real apt
> repository once someone hosts one, and an honest "load from a folder" fallback for right now.

## 1. The two channels, and why they are kept separate

| | Base system (kernel, XFCE, Firefox, Wine, …) | Lindos's own packages (`lindos-*`) |
|---|---|---|
| Update tool | **Mint's own Update Manager** (`mintupdate`) | `lindos-update` |
| Already works today? | Yes — untouched by this addendum | Only via sideload (§4) until a repo is configured |
| Background refresh | `apt-daily.timer` / `apt-daily-upgrade.timer` (protected, never disabled — see `docs/RAM-BUDGET.md`'s `SERVICE_WHITELIST`) | none — `lindos-update` never runs unattended |

`lindos-update` reads `apt-daily.timer`'s results (or its own explicit, privileged refresh — §3) —
it never replaces or duplicates Mint's own updater, and it never touches `mintupdate`,
`unattended-upgrades` or the timers that drive them.

## 2. Checking is always safe

```
lindos-update check [--json]
```

This is entirely read-only and needs no password: it parses `apt list --upgradable` (whatever is
already in apt's cache — it never calls `apt-get update` itself), reads `uname -r` and
`/var/run/reboot-required`, and looks at `/etc/apt/sources.list.d/lindos.list` to see whether a
Lindos apt repository is configured at all. Run it as often as you like; it changes nothing.

Sample output:

```
apt lists last refreshed: 2026-09-20T08:00:03+00:00
Lindos component updates (2):
  lindos-core: 1.0.0 -> 1.0.1
  lindos-tune: 1.0.0 -> 1.0.1
other system updates: 14 (see Update Manager / mintupdate)
kernel update available: none
booted kernel: 6.8.0-45-generic
Lindos apt repo: not configured yet — see docs/UPDATES.md ('lindos-update sideload' works today with no repo)
```

"other system updates" is a **count only** — the point is to tell you Update Manager has work to
do, not to duplicate it; open Update Manager (or run `lindos-tune report`) for the details.

## 3. Applying an update is opt-in, confirmed, and exact

```
lindos-update apply [--include-kernel] [--yes] [--dry-run] [--json]
```

`apply` always builds an **explicit list** of `name=version` pairs from what `check` just saw —
never a bare "upgrade everything" — asks you to confirm (unless `--yes`), and sends that exact
list to the privileged helper (`system-upgrade`), which runs
`apt-get install --only-upgrade -y -- name=version …` with those exact tokens. A kernel package
(`linux-image-*`/`linux-headers-*`/`linux-modules-*`) is **never** included unless you pass
`--include-kernel` — both `lindos-update` and the root helper enforce this independently, so a
kernel update can never sneak in silently.

Need the apt cache refreshed first? That is a separate, explicit, privileged step
(`apt-get-update`, a plain `apt-get update` and nothing else) — `lindos-update apply` triggers it
automatically before computing the package list; nothing here is silent or scheduled.

```
lindos-update cleanup [--yes] [--dry-run] [--json]
```

Runs `apt-get autoremove --purge` (old kernels, orphaned dependencies) through the same
confirmed, privileged path.

```
lindos-update kernel-status [--json]
```

Shows the booted kernel, whether it is the Lindos-tuned build (`-lindos` in `uname -r`), any
kernel update `check` saw, and whether a reboot is already pending from a previous update.

## 4. Sideload: install from a local folder, no apt repo needed

```
lindos-update sideload <DIR> [--yes] [--dry-run] [--json]
```

This is the **honest, works-today** path while `LINDOS_APT_REPO_URL` is still a placeholder. Point
it at a folder containing `lindos-*.deb` files — built locally (`make debs`) or downloaded from a
CI artifact — and it will:

1. Inspect every `*.deb` in the folder (`dpkg-deb --field`, no root needed for this part).
2. Keep only packages whose name matches `^lindos-`; anything else is skipped and reported, never
   silently installed.
3. Compare each kept package's version against what is currently installed. A downgrade is not
   blocked — only flagged, and confirmed before proceeding (`--yes` accepts it too).
4. Send the exact, absolute file list to the privileged helper (`install-local-debs`), which
   **re-verifies** every file's real `Package:` control field via `dpkg-deb --field` (never trusts
   the filename alone) before running `dpkg -i` and then `apt-get -f install -y` to resolve any
   dependencies.

## 5. Self-hosting a real Lindos apt repository

`build/publish-apt-repo.sh` builds a **flat-format** apt repository (`Packages`, `Packages.gz`,
`Release`, a clearsigned `InRelease`, a detached `Release.gpg`, and the exported public keyring)
from a folder of `.deb` files — no `dists/` tree, so it works from any static HTTPS host:

```
build/publish-apt-repo.sh --in out/debs --kernel-in out/kernel --out out/apt-repo \
    --key-id <your-gpg-key-id>       # or --gen-key for a one-off/CI key
```

Upload the contents of `out/apt-repo/` to any static HTTPS host you control, then point Lindos at
it:

```ini
# build/config.env (or build/config.local.env)
LINDOS_APT_REPO_URL=https://your-host.example/lindos-apt
LINDOS_APT_REPO_ENABLE=1
```

The next ISO build's `build/chroot/00-repos.sh` will fetch the keyring from
`<LINDOS_APT_REPO_URL>/lindos-archive-keyring.gpg` and write
`/etc/apt/sources.list.d/lindos.list`:

```
deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] <LINDOS_APT_REPO_URL> ./
```

A repository with no valid signature is refused outright by `apt` — never silently downgraded to
"trusted anyway". Signing with `--gen-key` (no `--key-id` given, and no signing key already
available) generates a **fresh, ephemeral** key and labels the result plainly in
`README-CI-KEY.txt`: it is not a stable trust root, and a machine that trusted a previous ephemeral
key will reject the next one — fine for a one-off smoke test, not for anything you plan to keep
trusting.

### CI

The `publish-repo` GitHub Actions job (`workflow_dispatch` only — it never runs on a normal push or
PR) builds the repository from the `debs` job's artifact and always uploads `out/apt-repo/**` as a
downloadable artifact. It additionally deploys that repository to **GitHub Pages** — but only when
the repository variable `LINDOS_PAGES_ENABLED` is set to `true`. That variable is **unset by
default**: turning it on is a one-time choice for whoever owns this repository (Settings → Secrets
and variables → Actions → Variables), never something CI decides on its own. Until you do that (or
self-host the artifact elsewhere), `LINDOS_APT_REPO_URL` stays a placeholder and every honest
status message here keeps saying so.

## 6. What "not configured yet" means

`https://packages.lindos.dev` in `build/config.env` is a **placeholder** — Lindos does not own that
domain, and no code here pretends otherwise. Until `LINDOS_APT_REPO_URL` is pointed at a real,
reachable HTTPS host and `LINDOS_APT_REPO_ENABLE=1` is set for a build:

* An ISO built today ships with **no** `/etc/apt/sources.list.d/lindos.list` at all.
* `lindos-update check` reports `repo_configured: false` and `repo_reachable: null` — never a
  fabricated "yes".
* The Settings "Updates" page shows a plain "No Lindos update channel is configured yet" note with
  a "Load from a folder…" picker (`lindos-update sideload`) instead of a dead URL.

`lindos-update repo status` reports the same thing on the command line, and additionally probes
reachability (HTTPS required, unless the host is `localhost` — a repo hosted otherwise never gets
treated as trusted). Exit codes throughout `lindos-update`: `0` ok, `1` error, `2` usage, `3`
nothing to do (e.g. "check" found no Lindos updates, or "repo status" found no repo configured).

## 7. Settings integration

Lindos Settings' "Updates" page (`lindos-settings updates`) is the GUI face of everything above:
an "Open Update Manager" button for the base system, a list of installed/available versions for
each `lindos-*` package with "Check now" / "Update now" buttons that call exactly the same
`lindos-update check` / `apply` underneath, and the kernel card (booted vs. installed vs.
available, Secure-Boot-signed indicator, "apply now (needs a restart)"). A read-only, unprivileged
autostart entry checks once per login and on a 6-hour timer, showing at most one notification per
boot — it never elevates and never installs anything by itself.
