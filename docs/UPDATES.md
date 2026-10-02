# Updates: how Lindos stays current

> **What this is.** Once you have installed Lindos you should never need to reinstall it to get new fixes.
> Lindos keeps itself current with three parts that are kept honestly apart: a **refresh timer** that finds
> out which updates exist (and installs nothing), the **`lindos-update`** tool that installs them when you ask
> (Lindos's own `lindos-*` packages and the base system underneath), and a **package repository** that the
> updates come from. The repository is built and tested but **does not exist yet**: it needs a domain, a signing
> key and a hosting choice that only the owner can make ([`RELEASING.md`](RELEASING.md)). Until then the
> refresh timer works against the base system's own repositories, and `lindos-update sideload` installs
> `lindos-*` packages from a folder. Nothing here pretends otherwise.
>
> Contract and file formats: [`SPEC-UPDATE.md`](../SPEC-UPDATE.md) (§35-§43). Releasing: [`RELEASING.md`](RELEASING.md).

## 1. The pieces

| Piece | Where | What it does | Needs |
|---|---|---|---|
| Refresh timer | `lindos-update-refresh.timer` / `.service` (`lindos-core`) | Twice a day (and 5 minutes after start-up): `apt-get update`, then `apt-get -s dist-upgrade` (a simulation), then writes `/var/lib/lindos/update-state.json`. **Never installs anything.** | root (a system unit) |
| State file | `/var/lib/lindos/update-state.json` | Which updates exist, by category, sizes, security flags, removals, restart reasons, when it was last refreshed. World-readable, written atomically. | nothing to read |
| `lindos-update` | `/usr/bin/lindos-update` | `status`, `check`, `plan`, `apply`, `history`, `cleanup`, `kernel-status`, `sideload`, `repo status` | reading: nothing; applying: one password prompt (polkit) |
| Helper actions | `lindos-helper` (`pkexec`) | `apt-get-update`, `system-upgrade`, `apt-full-upgrade`, `cleanup-old-packages`, `install-local-debs` | root, validated, whitelisted |
| Restart hook | `/etc/apt/apt.conf.d/98lindos-reboot-required` | After every apt run: notes `/run/reboot-required` when an update needs a restart | root (runs inside apt) |
| Repair unit | `lindos-update-repair.service` | Finishes an upgrade that was interrupted (power loss) at the next start | root, only when needed |
| Repository source | `lindos-archive-keyring` package | The Lindos apt source and its public key; **switched off** until a real key and server exist | - |
| Repository | `build/publish-apt-repo.sh`, CI `release-repo` | A signed, flat apt repository built from the last three releases | the owner's key and domain |

## 2. Refreshing: what the timer does, and what it does not

`lindos-update-refresh.timer` runs a few minutes after start-up and twice a day at a randomised time
(`OnCalendar=*-*-* 06,18:00`, `RandomizedDelaySec=2h`, `Persistent=true`, so a missed run happens at the next
start). The service waits for the network (`/usr/libexec/lindos/wait-for-network`, because the stock wait-online
unit is masked on Lindos and `network-online.target` arrives before Wi-Fi does), takes its turn behind every
other Lindos apt job (`apt-serialise`), runs `apt-get update` and simulates `apt-get dist-upgrade`. It does not
run in the live USB session, before the first-boot account wizard has finished, or while an update is in
progress or waiting for repair. Offline is not a failure: the state is still written from the lists apt already
has, marked "not refreshed".

**About the stock timers.** The base system refreshes apt through `apt-daily.timer` and installs through
`apt-daily-upgrade.timer`. Lindos switches both **off** (the `lindos-tune` preset,
`90-lindos.preset`: `disable apt-daily.timer` and `disable apt-daily-upgrade.timer`, applied when the image is
built), because nothing on the base system would notice a refresh anyway and an unattended upgrade is exactly
what Lindos will not do. Lindos's own timer replaces the *refresh*; nothing replaces the unattended *install*,
on purpose.

Nothing installs by itself, and no update package is ever downloaded in the background: the timer fetches
the package lists (a few megabytes); the packages are fetched only when you apply.

## 3. The state file (`/var/lib/lindos/update-state.json`)

Schema 1 (the fields are the contract, [`SPEC-UPDATE.md`](../SPEC-UPDATE.md) §40):

```json
{
  "schema": 1,
  "written_at": "2026-09-30T08:00:03+00:00",
  "refreshed_at": "2026-09-30T08:00:03+00:00",
  "refresh": {"attempted": true, "ok": true, "message": "", "at": "2026-09-30T08:00:02+00:00"},
  "repo": {"configured": false, "enabled": false, "url": null, "placeholder": true, "reachable": null},
  "plan_ok": true, "plan_errors": [],
  "counts": {"total": 8, "lindos": 2, "security": 1, "drivers-kernel": 2, "apps": 1, "other": 2},
  "download_bytes": 182300000,
  "groups": [{"id": "lindos", "title": "Lindos", "count": 2, "download_bytes": 34567,
              "items": [{"name": "lindos-core", "arch": "all", "from": "1.0.0", "to": "1.0.1",
                         "origin": "Lindos:stable", "origin_label": "Lindos", "category": "lindos",
                         "security": false, "download_bytes": 34567}]}],
  "removals": [{"name": "libold1", "version": "1.0-1", "purge": false}],
  "kept_back": ["gimp"], "held": [],
  "reboot": {"required": false, "packages": [], "reasons": [], "relogin": [],
             "would_require_reboot": true, "would_require_reboot_packages": ["libc6"]},
  "booted_kernel": "6.8.0-45-generic",
  "digest": "sha256:..."
}
```

* **Categories:** `lindos`, `security`, `drivers-kernel`, `apps`, `other` (shown as "Lindos", "Security",
  "Drivers & kernel", "Apps", "Other"). A kernel/driver package is `drivers-kernel` even when it is a security
  update or comes from Lindos; each item also has its own `security` flag.
* **Neutral origin labels:** `Lindos`, `Security`, `Apps`, `Lindos base system`, `Other sources`. The name of
  the base distribution is never shown, on purpose.
* **`digest`** is a fingerprint of exactly what would change (the `Inst`/`Remv` set); it is what the helper
  compares before it installs (section 5).
* **Sizes** come from `apt-get --print-uris` and are the bytes still to download; they are `null` when apt did
  not say.
* **`refreshed_at`** is the last *successful* `apt-get update` - the timer's, or the one `check --refresh` and
  `apply` run through the helper (`apt-get-update`); a failed or offline run keeps the previous value, so "last
  checked" is never a lie. Installing something (`apply`, `sideload`) rewrites the state too - the list of
  available updates then no longer shows what was just installed - but leaves `refreshed_at` alone: no update
  ran. `lindos-update status` calls a state older than three days out of date.

## 4. `lindos-update`

```
lindos-update status [--json]              what the last refresh found (no root, no network)
lindos-update check [--json] [--refresh]   what apt lists as upgradable now (--refresh: refresh first)
lindos-update plan [--json]                the full upgrade apt would do (a simulation, no root), with its digest
lindos-update apply [--all] [--include-kernel] [--allow-removals] [--yes] [--dry-run] [--no-refresh] [--json]
lindos-update history [--limit N] [--json] what was installed, upgraded and removed
lindos-update cleanup [--yes] [--dry-run] [--json]
lindos-update kernel-status [--json]
lindos-update sideload <DIR> [--yes] [--dry-run] [--json]
lindos-update repo status [--json]
```

Exit codes: `0` ok, `1` error, `2` usage, `3` nothing to do (also: `status` with no state file yet).

* `status` is what the Settings page and the notifier use: it needs no password and does not touch apt.
* `check` and `apply` refresh the package lists first (one privileged `apt-get-update`, which also rewrites the
  state file) unless you pass `--no-refresh`; a refresh that fails (offline) is reported and the command
  carries on with what is cached.
* `apply` (no `--all`) installs the **Lindos components** `check` found, as an exact `name=version` list
  through the helper's `system-upgrade`. A kernel package is never included without `--include-kernel`.
* `apply --all` installs the **whole plan**, base system included, through `apt-full-upgrade` (section 5).
  A plan that contains a kernel needs `--include-kernel`; a plan that removes packages needs
  `--allow-removals`. Without them it stops before asking for a password and says which flag it wants.
* Every privileged step is one `pkexec` and one confirmation, `--yes` skips the question (never the checks),
  `--dry-run` prints what would run and changes nothing.

## 5. What `apply --all` does, and why it is safe

The CLI simulates the upgrade itself as you (`apt-get -s dist-upgrade`), shows you the plan and remembers its
**digest**. The privileged helper (`apt-full-upgrade`) then:

1. **takes its turn** behind every other Lindos apt job (the lock `/run/lindos/apt.lock` that `apt-serialise`
   uses), so a list refresh cannot change the lists in the middle; if another job still holds it after 15
   minutes it stops and changes nothing;
2. **simulates again as root** and refuses unless the result has exactly the digest you were shown (the
   package lists may have changed in between: "check for updates again and review the list");
3. refuses if a **protected package** would be removed (the Lindos packages, the desktop, the network, the
   boot loader, `systemd`, `dbus`, `libc6`, the running kernel, ...), refuses any **removal** you did not
   approve and any **kernel** you did not include;
4. checks **free disk space** (1 GiB on `/`, 200 MiB on `/boot` when a kernel is part of the plan);
5. writes the **in-progress marker** `/var/lib/lindos/update-in-progress` (before the download, which can take
   an hour: a list refresh that starts meanwhile sees it and stays out);
6. **downloads first** (`apt-get -d`): a failed download stops before dpkg is touched and clears the marker;
7. **simulates once more** and applies the same checks to the result: the lists can change during the download,
   and apt works out its plan again in every process, so what was checked in step 2 does not cover what would
   run now. If the plan is no longer the one you were shown, nothing is installed and the state file is
   rewritten with the new plan;
8. runs `apt-get dist-upgrade` keeping your edited configuration files (`--force-confold`), and - unless you
   approved removals - with `--no-remove`, so apt itself aborts rather than remove anything (the download uses
   it too);
9. **always repairs afterwards** - `dpkg --configure -a` and `apt-get -f install` - and clears the marker only
   when the repair succeeded;
10. rewrites the state file.

If the machine loses power in step 8, the marker is still there at the next start and
`lindos-update-repair.service` finishes the job (`dpkg --configure -a`, `apt-get -f install`); the next
`apt-full-upgrade` also repairs an earlier interrupted run before it starts.

The Lindos-only path (`system-upgrade`) is kept for installs that only want the Lindos components. Nothing
here ever runs through a shell, and the helper validates every payload (`lindos.helper.validate_payload`).

## 6. Restart handling

The base system writes `/run/reboot-required` through a package that is not on Lindos, so Lindos does it: an
apt `DPkg::Post-Invoke` hook (`reboot-required-hook`) looks, after every apt run, at what dpkg installed since
boot (`/var/log/dpkg.log`) and at whether a newer kernel of the running flavour is installed
(`/boot/vmlinuz-*`), and writes `/run/reboot-required` and `/run/reboot-required.pkgs` (`/run` is cleared by
the restart itself). A fixed list counts: kernels, `libc6`, `systemd`/`udev`, `dbus`, the core GLib/GTK
libraries, the X server, NVIDIA driver packages, CPU microcode. `lindos-desktop` needs a new login, not a
restart, and is reported separately (`reboot.relogin`). Nothing ever restarts by itself: the desktop shows
"restart now / later". The state file also says beforehand whether the plan **would** need a restart.

## 7. Cleanup

`lindos-update cleanup` is a **safe** autoremove (ported from the image build's `safe_autoremove`): the helper
simulates `apt-get autoremove --purge`, leaves out everything protected (the desktop and base-system families,
`lindos-*`) and the **running, the newest and the previous kernel**, and then proves with a second simulation
that purging just the rest removes nothing else - or it removes nothing at all. More than 30 packages is
refused. (Before, the helper ran a bare `apt-get autoremove --purge -y`, which can dismantle the desktop when a
metapackage disappears.)

## 8. History

`lindos-update history` reads apt's `history.log` (and its rotated copies): each transaction with its command
line and the packages installed, upgraded, removed and purged, and whether it failed.

## 9. The repository

* **The source and key ship in a package.** `lindos-archive-keyring` installs
  `/etc/apt/sources.list.d/lindos.sources` (deb822, a **conffile**) and
  `/usr/share/keyrings/lindos-archive-keyring.gpg`. The repository address and suites are written in that one
  file only, so the repository can move to another server by a package update while installed systems keep
  working. It is the trust anchor: the keyring copy that is also published next to the repository is a
  convenience and is never trusted by an installed system.
* **Switched off until it is real.** The shipped source says `Enabled: no` and points at a reserved,
  never-resolving `.invalid` name; the key file is a text placeholder that **no build ever ships**
  (`build/mkdeb.sh` drops it and `build/tools/check-archive-keyring.sh` refuses to build a switched-on source
  without a real, pinned key or with a placeholder address; a test fails if that is ever committed). The owner
  flips it on in one commit together with the real key, its fingerprint and the real address
  ([`RELEASING.md`](RELEASING.md)).
* **The image does not fetch a key.** `build/chroot/30-lindos-debs.sh` installs the staged package in the same
  apt transaction as the other Lindos packages - the trust-on-first-use download of a key at build time is
  gone. The package needs nothing, so no install order can leave it half-installed: an earlier hook that
  unpacked it with `dpkg -i --force-depends` made apt refuse every install and purge of the hooks in between.
  `30-lindos-debs.sh` then checks the source, exercises an enabled one with `apt-get update`, and
  `LINDOS_APT_REPO_REQUIRE=1` makes a release image fail unless the source ends up enabled.
* **Exactly one key.** apt trusts every key of a `Signed-By` keyring, so the guard
  (`build/tools/check-archive-keyring.sh`) refuses a keyring that holds anything but one primary key - the pinned
  one (subkeys are fine) - and a source file that has more than one stanza, an address that is not https (or plain
  http to the local machine, for the e2e job), a `Signed-By` other than the shipped keyring, or a `Trusted` /
  `Allow-Insecure` override. The release job checks the committed keyring the same way.
* **One version for everything.** Every `lindos-*` package of a release has the same version (the `VERSION`
  file; `build/mkdeb.sh` stamps it in the staging copy) and `lindos-meta` pins its parts to exactly that
  version, so they always update together.
* **What is published.** `build/publish-apt-repo.sh` builds a flat repository (`Packages`, `Release`,
  `InRelease`, `Release.gpg`) from the current release and the previous ones - the last **three** versions of
  each package stay, so a bad update can be rolled back - and refuses to sign with anything but the pinned
  key when it is a release. The kernel `.deb`s (about 115 MiB each) are deliberately **not** in the repository:
  they do not belong on GitHub Pages.
* **Older releases are verified, not trusted.** Everything published is signed with the archive key, and
  GitHub Release assets can be added or replaced by anyone with write access, so an older release's `.deb`
  is only kept when the signed metadata published with that release (`Release`, `Release.gpg`, `Packages`,
  attached by the release job after it signed) vouches for its exact bytes
  (`build/tools/fetch-previous-releases.sh`); a release without that metadata contributes nothing.
  `publish-apt-repo.sh` then treats them as untrusted again (`--previous`): only `lindos-*` packages of the
  current release, named for what is inside, with a plain older `X.Y.Z` version - and a file of the release
  being published is never replaced by anything.
* **Valid-Until is off.** The script can put an expiry into `Release`, but apt refuses an expired repository,
  so it stays off until a scheduled re-sign job exists.
* **Hosting** is GitHub Pages fed by the `release-repo` and `deploy-pages` CI jobs, and only when the owner
  has enabled Pages and set `LINDOS_PAGES_ENABLED=true`. CI never turns Pages on by itself, never generates a
  key that could ship, and a manual test build (`publish-repo`) uses a throw-away key and can never deploy.

## 10. Sideload: install from a local folder, no repository needed

```
lindos-update sideload <DIR> [--yes] [--dry-run] [--json]
```

The honest, works-today path while there is no repository. Point it at a folder with `lindos-*.deb` files
(built with `make debs`, or from a CI artifact); it inspects them (`dpkg-deb --field`), keeps only packages
named `lindos-*`, flags a downgrade, and hands the exact absolute file list to the helper (`install-local-debs`),
which **re-verifies** every file's real `Package:` field before `dpkg -i` and `apt-get -f install`.

## 11. Hosting your own repository

```
build/publish-apt-repo.sh --in out/debs --out out/apt-repo --key-id <your-gpg-key>
```

works for any static HTTPS host and produces a repository plus `lindos.sources.example`. For a repository of
your own (a fork, a private mirror) create your own key, replace the keyring, fingerprint and address in
`packages/lindos-archive-keyring`, and rebuild the package. `--gen-key` exists only for CI tests: it makes a
fresh throw-away key every run, so a machine that trusted a previous one rejects the next; it can never be
combined with `--release`.

## 12. What "not configured yet" means

Until the owner has created the key and the server and switched the source on, an installed Lindos has the
source file, but it is `Enabled: no`: `lindos-update check` and `repo status` say "not configured yet" (and why),
`repo_configured` is `false`, `repo_reachable` is `null` - never a fabricated "yes". The refresh timer, `status`,
`plan`, `apply --all` and `history` all work regardless, against the base system's repositories.

## 13. What is unverified

The parsers and every safety check are unit-tested on any OS with fake `apt`, but nothing here has run against a
real apt on a real Lindos yet. The opt-in `repo-e2e` CI job (real apt, dpkg and gpg in an Ubuntu 24.04
container, against a localhost repository signed with a throw-away key) settles the repository items, the
parsers on the container's apt and, with `with-full-upgrade`, `apt-full-upgrade`; the rest needs a VM or real
hardware. Specifically unverified:

* the exact `apt-get -s dist-upgrade` / `--print-uris` output formats on the shipped apt (the parsers ignore what
  they do not know, but a format change would show as an empty plan);
* `apt-get --print-uris` for sizes as an unprivileged user (best effort: sizes are `null` otherwise);
* the repository end to end: signing from the secret, `apt-get update` trusting the keyring, install and upgrade;
* `apt-full-upgrade` on a real system (the `repo-e2e` job runs it only with `with-full-upgrade`), including what
  `apt-get dist-upgrade --no-remove` prints when it aborts, and the shared lock against a real `apt-serialise`;
* `build/tools/fetch-previous-releases.sh` against the real `gh` and `gpgv` (tested with fakes; the hashes are
  real), and the `Release`/`Packages` layout `apt-ftparchive` writes;
* the reboot hook against a real `dpkg.log` and a real kernel upgrade;
* `lindos-update-repair.service` after a real interruption (pull the power in a VM mid-upgrade);
* multi-arch (i386 for Wine/Steam) and phased updates in the plan;
* Ubiquity keeping `lindos.sources` on an installed system;
* a base-system update not re-introducing the base distribution's name into the identity files (the
  `99lindos-branding` hook covers it at build time only).

## 14. Where things are

| Path | What |
|---|---|
| `packages/lindos-core/root/usr/lib/python3/dist-packages/lindos/update.py` | reading: status, the source, sideload, payload builders |
| `.../lindos/updatestate.py` | the engine: parsers, digest, state writer, reboot logic, history, safe-cleanup planner |
| `packages/lindos-core/root/usr/bin/lindos-update` | the CLI |
| `packages/lindos-core/root/usr/libexec/lindos/lindos-helper` | `apt-full-upgrade`, `cleanup-old-packages`, ... |
| `.../libexec/lindos/{update-refresh,update-repair,reboot-required-hook}` | the scripts behind the units and the apt hook |
| `.../usr/lib/systemd/system/lindos-update-{refresh.timer,refresh.service,repair.service}` | the units |
| `packages/lindos-archive-keyring/` | the source and key |
| `build/publish-apt-repo.sh`, `build/tools/{make-signing-key,check-archive-keyring,fetch-previous-releases,repo-e2e}.sh` | repository tooling |
| `VERSION`, `CHANGELOG.md` | the release version and its notes |
