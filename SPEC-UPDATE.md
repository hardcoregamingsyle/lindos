# Lindos — Engineering Specification, Addendum U (Updates)

> Extends [`SPEC.md`](SPEC.md) (§0–§13). Same rules: this file is the contract. Section numbers
> continue at §35 (after Addendum W, [`SPEC-WINDOWS.md`](SPEC-WINDOWS.md) §27–§34). The user-facing
> description is [`docs/UPDATES.md`](docs/UPDATES.md); the owner's steps are [`docs/RELEASING.md`](docs/RELEASING.md).

Goal: once someone has installed Lindos, they never need to reinstall it to get new fixes. One tool
(`lindos-update`), one privileged helper and one repository serve **both** the Lindos packages (`lindos-*`) and
the base system underneath (kernel, desktop libraries, apps), kept apart honestly:

1. **A root timer only *finds out*.** `lindos-update-refresh` refreshes apt's package lists twice a day, simulates
   the upgrade and writes `/var/lib/lindos/update-state.json` (§39-§40). It never installs.
2. **Installing is always asked for.** `lindos-update apply` (Lindos components, exact list) and
   `lindos-update apply --all` (the whole plan, checked against the plan the user was shown) go through the
   privileged helper (§36.4, §42).
3. **The Lindos repository** carries the `lindos-*` updates. It is built and tested, but it does not exist until
   the owner supplies a domain and a signing key (§43); until then `lindos-update sideload` installs `lindos-*`
   packages from a local folder. This addendum builds the pipeline and does not claim a repository exists.

## 35. Honesty rules for this addendum (extend §0.1, §27)

- `lindos-update` never installs anything the user has not asked for. Reading (`status`, `check`, `plan`,
  `history`) is always safe and needs no privilege; applying is opt-in and confirmed; a kernel is never included
  without `--include-kernel` and a removal is never approved without `--allow-removals`. **Nothing is scheduled
  to install unattended.** Should an automatic install ever be added it must be an explicit opt-in and this
  section amended first.
- The refresh timer only refreshes the package lists and *simulates* (`apt-get -s`); it never downloads a package
  and never installs one.
- The repository address is written in **one** place, the conffile
  `/etc/apt/sources.list.d/lindos.sources` of the `lindos-archive-keyring` package, so it can move by a package
  update. It is a reserved `.invalid` placeholder, and the source is `Enabled: no`, until a real signing key and a
  real server exist. `lindos-update` and Settings say plainly when no repository is configured or reachable and
  never pretend one exists. No host name of a domain Lindos does not own is ever hard-wired.
- The signing key is supplied by the owner (an offline certify-only primary key and a signing subkey with an
  expiry); CI never generates a key that could ship to users. A repository without a valid signature is refused
  by apt, never downgraded to "trusted"; the publish step refuses to sign a release with any key but the pinned
  one (§43).
- The stock `apt-daily` / `apt-daily-upgrade` timers are **switched off** on Lindos (the `lindos-tune` preset,
  `packages/lindos-tune/root/usr/lib/systemd/system-preset/90-lindos.preset`). Lindos's own timer replaces the
  *refresh*; nothing replaces the unattended *install*. (`SERVICE_WHITELIST` in `lindos.helper` lists them only as
  units the helper is *allowed* to enable or disable.)
- The name of the base distribution never appears in an update label, category or notification (§40 maps every
  origin to a neutral label).
- Honesty about hosting: kernel `.deb`s (about 115 MiB each) and the ISO are not published on GitHub Pages;
  nothing here promises a mirror, a date or a release channel that does not exist.

## 36. `lindos-update` (in `lindos-core`)

### 36.1 CLI (`/usr/bin/lindos-update`, argparse, `--json` on every read)

```
lindos-update status [--json]                     # what the last refresh found: update-state.json (no root, no network)
lindos-update check [--json] [--refresh]          # what apt lists as upgradable now; --refresh: apt-get-update first
lindos-update plan [--json]                       # the full upgrade apt would do (simulation, no root) + its digest
lindos-update apply [--all] [--include-kernel] [--allow-removals] [--yes] [--dry-run] [--no-refresh] [--json]
lindos-update history [--limit N] [--json]        # apt's history.log, newest first
lindos-update cleanup [--yes] [--dry-run] [--json]                # a SAFE autoremove (§42.3)
lindos-update kernel-status [--json]
lindos-update sideload <DIR> [--yes] [--dry-run] [--json]         # install/replace lindos-* .debs from a local folder
lindos-update repo status [--json]                                # the Lindos apt source configured & reachable
```
Exit codes: 0 ok · 1 error · 2 usage · 3 nothing to do (also `status` before the first refresh).

`check` (with `--refresh`) and `apply` refresh the package lists first through the helper's `apt-get-update`
(unless `--no-refresh`); a failed refresh is reported and the command continues with the cache. `apply` without
`--all` sends the exact Lindos `name=version` list to `system-upgrade`; `apply --all` simulates the full upgrade
itself, shows it, and sends its digest to `apt-full-upgrade`. With `--json` stdout is exactly one JSON document
(helper output goes to stderr). `--dry-run` runs the helper in its dry-run mode.

### 36.2 `lindos/update.py` API (binding) - *reading*

```python
@dataclass
class PackageUpdate:
    name: str; installed: str; candidate: str; channel: str   # channel: "lindos" | "system" | "kernel"

@dataclass
class UpdateStatus:
    refreshed_at: Optional[str]       # ISO-8601, mtime of apt's lists dir; None if never refreshed
    lindos_updates: List[PackageUpdate]
    system_updates: List[PackageUpdate]   # the rich, categorised picture is update-state.json (§40)
    kernel_available: Optional[PackageUpdate]
    booted_kernel: str                 # uname -r
    booted_is_lindos_kernel: bool
    reboot_required: bool              # /var/run/reboot-required exists (written by the apt hook, §41)
    repo_configured: bool              # an ENABLED Lindos source with an address
    repo_reachable: Optional[bool]     # None when repo_configured is False

def apt_list_upgradable(*, run=subprocess.run) -> List[PackageUpdate]      # parses `apt list --upgradable` (no root)
def check(*, run=..., which=..., fetch=None) -> UpdateStatus               # never calls apt-get update, never elevates
def parse_deb822(text) -> List[Dict[str, str]]
def source_info() -> Dict[str, Any]     # {present, format: "deb822"|"list"|None, path, enabled, url, suites, signed_by,
                                        #  placeholder, configured, keyring_present}  (deb822 first, the old one-line list as fallback)
def repo_info() -> Dict[str, Any]       # the subset the state file reports
def configured_repo_url() -> Optional[str]   # the address of the ENABLED source, else None
def is_placeholder_url(url) -> bool     # a .invalid name or the old placeholder host
def repo_status(url, *, fetch=None) -> Tuple[bool, str]      # (reachable, message); HTTPS required unless localhost; placeholders refused
def refresh_payload() -> {}                                  # helper action "apt-get-update"
def apply_payload(names_versions, *, allow_kernel=False) -> {"packages": [...], "allow_kernel"?: True}
def apply_all_payload(plan_digest, *, allow_kernel=False, allow_removals=False) -> {"plan_digest": ..., ...}
def cleanup_payload() -> {}                                  # the helper decides what is safe
# sideload: scan_sideload_dir / sideload_payload / dpkg_deb_field / dpkg_installed_version / compare_versions (unchanged, §36.3)
```
`check()` never elevates and never refreshes the apt cache itself: it reads whatever Lindos's own timer (or an
explicit `apt-get-update`) already downloaded. The engine that parses apt's output and writes the state is
`lindos/updatestate.py` (§39-§42).

### 36.3 Sideload (`.deb` folder, no repo needed)

`lindos-update sideload <DIR>` inspects every `*.deb` in `DIR` (via `dpkg-deb --field`), keeps only packages
whose name matches `^lindos-`, flags (never blocks) a downgrade, and passes the exact file list to helper action
`install-local-debs`, which re-verifies each file's real `Package:` field.

### 36.4 Helper actions (`lindos.helper.ACTIONS` + `lindos-helper` handlers)

| action | payload | effect |
|---|---|---|
| `apt-get-update` | `{}` | takes its turn (§42.1), `apt-get update`; then rewrites `update-state.json` from the new lists with `refreshed_at` = now (a failure to do so never fails the action) |
| `system-upgrade` | `{"packages": ["name=version", …], "allow_kernel"?: bool}` | validates every entry (`PACKAGE_RE` + `=` + a version pattern), refuses a kernel package unless `allow_kernel`, then `apt-get install --only-upgrade -y -- name=version …` - the exact tokens, never a bare upgrade; afterwards rewrites `update-state.json` (`refreshed_at` untouched) |
| `apt-full-upgrade` | `{"plan_digest": "sha256:<64 hex>", "allow_kernel"?: bool, "allow_removals"?: bool}` | the base-system upgrade, §42.1 |
| `cleanup-old-packages` | `{}` | a safe autoremove, §42.3 |
| `install-local-debs` | `{"files": ["/abs/path/lindos-core_1.0.1_all.deb", …]}` | absolute, existing, `.deb`, `lindos-*` names; the helper re-verifies `Package:` then `dpkg -i` and `apt-get -f install -y`; afterwards rewrites `update-state.json` |

All are allowed inside `run-batch` (one `pkexec`, one prompt) and go through the same validators as a single call.

### 36.5 Repo publishing (`build/publish-apt-repo.sh`)

```
build/publish-apt-repo.sh [--in DIR]... [--previous DIR]... [--kernel-in DIR] [--out DIR=out/apt-repo] [--keep N=3]
                          [--key-id ID | --gen-key | --release] [--expect-fingerprint FPR|FILE] [--keyring FILE]
                          [--sources-file FILE] [--valid-until-days N=0]
```
Builds a **flat-format** repository (no `dists/`; works from any static host): `Packages`, `Packages.gz`, `Release`
(`apt-ftparchive`, Origin/Label `Lindos`, Suite `stable`), a clearsigned `InRelease`, a detached `Release.gpg`,
the exported public keyring (convenience only: never the trust anchor of an installed system), every `.deb` (at
most `--keep` versions of each, numerically ordered) and `lindos.sources.example` (its address read from the
keyring package's conffile). Signing key, first that applies: `LINDOS_APT_SIGNING_KEY` (armored secret subkey, +
`LINDOS_APT_SIGNING_KEY_PASSPHRASE`; imported into a private temporary `GNUPGHOME`, passphrase through a file
there, removed on exit), `--key-id`, the first secret key, `--gen-key` (a throw-away key, CI tests only, with
`README-CI-KEY.txt`). Unless the key is ephemeral it must be the **pinned** one when a fingerprint is pinned
(`packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint`); `--release` makes the pin
mandatory, refuses `--gen-key`, refuses unless the committed keyring holds exactly one primary key (the pinned one:
apt trusts every key of a keyring), and verifies the result with `gpgv` against the *committed* keyring - a
repository that would not verify for an installed system is never produced. Never invents trust: no key and no
`--gen-key` fails.

`--in` is the release being published and is authoritative: a file name that occurs twice (two `--in`, or a kernel
`.deb`) is an error, nothing is overwritten, and with `--release` every file must be a `lindos-*` package named for
what `dpkg-deb -f` says is inside, all of one plain `X.Y.Z` version. `--previous` (the older releases kept for rollback)
is **untrusted input**: each file must also be a `lindos-*` package *of the release being published*, its name must
match its content, its version must be a plain `X.Y.Z` **older** than the one being published, and it may never replace
a file already there; anything else stops the publish before anything is signed.

### 36.6 Build/ISO wiring

- `packages/lindos-archive-keyring` (§43) ships `/etc/apt/sources.list.d/lindos.sources` (conffile) and
  `/usr/share/keyrings/lindos-archive-keyring.gpg`. `lindos-meta` depends on it (`= <version>`).
  `build/mkdeb.sh` refuses to build it with a switched-on source and a placeholder key, fingerprint or address
  (`build/tools/check-archive-keyring.sh`) and never ships the placeholder key file.
- `build/chroot/30-lindos-debs.sh` installs the staged package, first in `LINDOS_DEB_ORDER`, in the **same apt
  transaction** as `lindos-core` and the rest; no earlier hook unpacks a Lindos package (a `dpkg -i --force-depends`
  in `00-repos.sh` once left the keyring with an unmet dependency, and apt then refused every install and purge of
  hooks 10 and 20). The package has no dependencies of its own, so no order can leave apt broken. Afterwards the hook
  checks the source and, when it is enabled, runs `apt-get update` to exercise it. **No key is fetched from the
  network at build time.** `LINDOS_APT_REPO_REQUIRE=1` (`build/config.env`, default 0) fails the build unless the
  package is installed and the source ends up enabled.
  The old build variables for the repository address and for switching it on no longer exist: the address is in
  the conffile, and switching it on is part of the package.
- CI (`.github/workflows/ci.yml`): `publish-repo` (manual, throw-away key, artifact only, never deployed),
  `release-repo` (tags `vX.Y.Z`; the only job that sees the signing secret; environment `apt-publish`; its token is
  `contents: write` only - no Pages permission sits next to the key),
  `deploy-pages` (after `release-repo`, only when the variable `LINDOS_PAGES_ENABLED` is `true`), `repo-e2e`
  (opt-in, §43.5).

## 37. UI contract

The Settings **Updates** page and the notifier (built by their own streams) read `lindos-update status --json`
(= `update-state.json`, §40) - no password, no apt in the user session - and act through the helper:

- **Status hero:** "up to date" / "N updates" / "restart required" from `counts.total`, `plan_ok`, `reboot.required`;
  "last checked" from `refreshed_at` (stale after three days); the refresh error from `refresh.message`.
- **Check now:** helper `apt-get-update` (one prompt), then re-read the state.
- **Install:** the whole plan through `apt-full-upgrade` with a digest computed from a *fresh* `lindos-update plan`
  (the user must be shown what the digest covers: groups, removals, kernel, `would_require_reboot`); Lindos
  components only through `system-upgrade`. Per-category installs need a helper primitive that does not exist yet.
- **Restart:** "Restart now / later" when `reboot.required`; nothing ever restarts by itself; `reboot.relogin`
  asks for a new login.
- **History:** `lindos-update history --json`.
- **No repository:** a plain note ("switched off until a real signing key and server exist") and the sideload
  picker (`lindos-update sideload`) when `repo.configured` is false.
- Labels are the neutral origin labels and category titles of §40; the UI never says the name of the base
  distribution. The read-only notifier (`lindos-update-notify`, login + 6 hours, one toast per boot per set of
  updates) is unchanged and never elevates.

*Transition:* until that page exists, the current Settings page still offers a launcher for the base system's own
update tool for the non-Lindos part. Replacing that launcher by the page above - and hiding the base system's tool from
the desktop - belongs to the Settings and desktop streams, and must happen in the same release that ships the
base-system install path (never before: hiding it first would leave nothing that shows base updates).

## 38. Docs & tests

- `docs/UPDATES.md` (how it works), `docs/RELEASING.md` (the owner's steps), `CHANGELOG.md` (per release).
- Tests are hermetic (fake `apt`, `gpg`, `dpkg-deb`, `systemctl`, clock and paths; no repo script is executed
  directly): `packages/lindos-core/tests/test_{update,updatestate,update_cli_engine,update_scripts,
  helper_update_actions,helper_apt_upgrade}.py`, `packages/lindos-archive-keyring/tests/`,
  `build/tests/test_{publish_apt_repo,mkdeb_version,make_signing_key,ci_repo_release,archive_keyring_wiring,
  update_docs}.py`. The Linux-only end-to-end job is `repo-e2e` (§43.5).

## 39. The refresh timer

`lindos-update-refresh.timer` (`OnBootSec=5min`, `OnCalendar=*-*-* 06,18:00`, `RandomizedDelaySec=2h`,
`AccuracySec=10min`, `Persistent=true`; enabled by a static `.wants` symlink from `lindos-core`'s postinst)
starts `lindos-update-refresh.service` (`Type=oneshot`, `Nice=15`, idle I/O, 45 min timeout), which runs
`/usr/libexec/lindos/update-refresh`. Conditions: not in the live session (`!boot=casper`, `!boot=live`), not before
the account wizard finished (`!/lib/systemd/system/oem-config.target`), not while `/var/lib/lindos/update-in-progress`
exists. The script waits for the network with `/usr/libexec/lindos/wait-for-network` (default 90 s,
`LINDOS_NETWORK_WAIT`), takes its turn with `apt-serialise`, then runs
`python3 -m lindos.updatestate refresh [--offline]`: `apt-get update` (unless offline), `apt-get -s dist-upgrade`,
`apt-get --print-uris -y dist-upgrade` (sizes, best effort), `apt-mark showhold`, the reboot reasons (§41), and
writes the state (§40) atomically. Offline is not a failure: the state is written from the existing lists with
`refresh.attempted: false` and `refreshed_at` unchanged. The marker is checked a last time inside `refresh` itself,
after the queue behind other apt jobs, and a run that finds it does nothing. Nothing in this path installs.

## 40. `update-state.json` (schema 1)

`/var/lib/lindos/update-state.json`, mode 0644, written atomically (temp file + `os.replace`).

| field | meaning |
|---|---|
| `schema` | `1` |
| `written_at` | when this file was written (UTC ISO-8601) |
| `refreshed_at` | the last **successful** `apt-get update` (the timer's, or the helper's `apt-get-update`, which stamps it now); a failed or offline run keeps the previous value, and an install (`system-upgrade`, `install-local-debs`) rewrites the state without touching it; `null` if never |
| `refresh` | `{attempted, ok, message, at}` for this run |
| `repo` | `{configured, enabled, url, placeholder, reachable}` (`reachable` from the update output; `null` = unknown) |
| `plan_ok`, `plan_errors` | false + apt's `E:` lines when the simulation failed (e.g. dpkg interrupted) |
| `counts` | `{total, lindos, security, drivers-kernel, apps, other}` |
| `download_bytes` | bytes still to download, or `null` |
| `groups` | per non-empty category, in the order `lindos, security, drivers-kernel, apps, other`: `{id, title, count, download_bytes, items[]}`; item = `{name, arch, from (null = new), to, origin, origin_label, category, security, download_bytes}` |
| `removals` | `[{name, version, purge}]` the plan would remove |
| `kept_back`, `held` | apt's kept-back list; `apt-mark showhold` |
| `reboot` | `{required, packages[], reasons[{kind: reboot\|relogin, package, text}], relogin[], would_require_reboot, would_require_reboot_packages[]}` |
| `booted_kernel` | `uname -r` |
| `digest` | `sha256:` of the sorted `inst <name> <version>` / `remv <name>` lines - what the helper compares (§42.1) |

Categories: a kernel/driver/firmware package (`linux-image|headers|modules-*`, `nvidia-*`, microcode, Mesa ...) is
`drivers-kernel`; else `lindos-*` or a Lindos origin is `lindos`; else a `-security` pocket is `security`; else a
third-party origin (Google, Microsoft, WineHQ, Valve, Flathub, Mozilla ...) or a known app is `apps`; else `other`.
**Neutral origin labels:** `Lindos`, `Security`, `Apps`, `Lindos base system`, `Other sources` - never the name of
the base distribution.

## 41. Restart handling

`/etc/apt/apt.conf.d/98lindos-reboot-required` runs `/usr/libexec/lindos/reboot-required-hook` after every apt run
(`DPkg::Post-Invoke`, always exits 0). It runs `python3 -m lindos.updatestate reboot-hook`, which writes
`/run/reboot-required` (`*** System restart required ***`) and merges `/run/reboot-required.pkgs` for: a package of
the fixed list (`linux-image-*`, `libc6`, `systemd`, `udev`, `dbus`, the core GLib/GTK libraries, `xserver-xorg-core`,
`nvidia-driver-*`/`libnvidia-*`, `intel-microcode`, `amd64-microcode`) that `dpkg.log` shows installed or upgraded
after boot (`/proc/stat` `btime`), or a newer installed kernel *of the running flavour* than `uname -r`
(`/boot/vmlinuz-*`; a stock kernel never nags a user on the Lindos kernel). `lindos-desktop` upgraded after boot is
a `relogin` reason, not a reboot. `/run` is cleared by the restart itself.

## 42. Safety of applying

### 42.1 `apt-full-upgrade`
Order, each step aborting with nothing changed unless noted: online → take the apt turn (the `flock` on
`/run/lindos/apt.lock` that `apt-serialise` queues on, up to 15 minutes, then stop; nested inside `apt-serialise`, or
without `fcntl`: no queueing) → repair an earlier interrupted run
(`update-in-progress`) → simulate `apt-get -s dist-upgrade` as root → refuse if the simulation has errors, **the digest
is not the one supplied**, a kernel package is in the plan without `allow_kernel`, a **protected package** would be
removed (`lindos-*`, `xfce4*`, `xfwm4`, `xfdesktop4`, `xfconf`, `thunar`, `lightdm`, `slick-greeter`, `network-manager`,
`plymouth`, `grub`, `shim`, `casper`, `ubiquity`, `systemd`, `dbus`, `libc6`, `polkit*`, `pipewire`, `wireplumber`,
`xserver-xorg-core`, `xorg`, `mint-meta*`, `sudo`, `apt`, `dpkg`, `python3`, the running kernel's image/modules; the
authoritative list is `UPGRADE_PROTECT_RE` in `lindos/updatestate.py`), or any
removal without `allow_removals` → refuse below 1 GiB free on `/` (200 MiB on `/boot` with a kernel in the plan) →
write the marker → `apt-get -d` (download first; a failure clears the marker) → **simulate again and apply every check
above to the fresh result** (apt re-resolves in each process, so the first checks do not cover what would run after an
hour-long download; a changed plan clears the marker, rewrites the state and installs nothing) →
`apt-get dist-upgrade -y -q` with `--force-confdef --force-confold`, `DPkg::Lock::Timeout=300`, `Acquire::Retries=3`
(the download and this run add `--no-remove` unless `allow_removals`, so apt itself aborts instead of removing) →
**always** `dpkg --configure -a` then `apt-get -f install` → clear the
marker only when the repair succeeded → refresh the state. Exit 0 only when the upgrade and the repair both succeeded.

### 42.2 Interrupted upgrades
The marker `/var/lib/lindos/update-in-progress` (`{action, digest, pid, started_at}`) is left in place when the process
dies or the repair fails. `lindos-update-repair.service` (condition: the marker exists; `WantedBy=multi-user.target`)
runs `/usr/libexec/lindos/update-repair`: `dpkg --configure -a`, `apt-get -f install`, marker removed only if both
succeeded, otherwise the unit fails and the next start tries again. The refresh timer does nothing while the marker exists
(the unit condition, the script and `lindos.updatestate refresh` each check it).

### 42.3 `cleanup-old-packages`
`apt-get -s autoremove --purge` → leave out everything matching the protected families (the desktop and base
system, `mint*`, `gnome-*`, `lindos-*`, `linux-generic`/`-hwe`/`-firmware` metapackages, ...) and the **running, the
newest and the previous kernel** (by kernel build, all its flavours) → more than 30 packages: refuse → prove with
`apt-get -s purge -- <the rest>` that nothing outside that list would go → `apt-get purge -y -q -- <the rest>`.
Otherwise nothing is removed.

## 43. Versions, the keyring package and releases

1. **One version.** `VERSION` (repository root) is the release version; `build/config.env` (`LINDOS_VERSION`, the ISO
   name) follows it. `build/mkdeb.sh` stamps it in the **staging copy** of every package (`--version` / `LINDOS_PKG_VERSION`
   override it): the `Version:` field, every exact `lindos-x (= old)` pin in `Depends`, `Pre-Depends`, `Recommends`,
   `Suggests`, `Breaks`, `Conflicts`, `Replaces` and `Enhances`, and `lindos.__version__`. The source tree keeps its
   development version and its tests are unchanged; a test checks that the source tree is internally in lock-step
   (one version everywhere, `lindos-meta` pinning exactly it). A release tag `vX.Y.Z` must equal `VERSION` (CI checks).
2. **`lindos-archive-keyring`** (`Architecture: all`, **no dependencies** - it must be installable alone and in any
   order): `/etc/apt/sources.list.d/lindos.sources`
   (conffile: `Enabled`, `Types: deb`, `URIs` - the one place - `Suites: ./`, `Signed-By`) and
   `/usr/share/keyrings/lindos-archive-keyring.gpg` and `/usr/share/lindos/archive-key.fingerprint`. Committed: the source
   `Enabled: no` with a `.invalid` address; the key as a text placeholder (`LINDOS-PLACEHOLDER-KEYRING`) and the fingerprint
   `PLACEHOLDER`. The guard (`build/tools/check-archive-keyring.sh`, run by `mkdeb`) refuses: an enabled source with a
   placeholder/missing/armored key, without a 40-digit fingerprint, or at a placeholder address; a real key without its
   fingerprint; a keyring that holds anything but exactly one primary key, or whose primary fingerprint is not the
   pinned one (apt trusts every key in it), and a real keyring when `gpg` is not there to check it; a source file with
   more than one stanza or address, an address that is neither https nor plain http to the local machine (or that has
   credentials in it), a `Signed-By` other than `/usr/share/keyrings/lindos-archive-keyring.gpg`, or any `Trusted`,
   `Allow-Insecure`, `Allow-Weak` or `Allow-Downgrade-To-Insecure` override. A placeholder key is dropped from the built
   package. `postinst` removes a leftover one-line `lindos.list` (+ its key) from an earlier image build.
3. **Retention.** The repository keeps the last three versions of each package (the two previous GitHub Releases are
   merged into every publish), so a bad update can be rolled back by installing an older version. Release assets are
   mutable, so an older release's `.deb`s are **verified, not trusted** (`build/tools/fetch-previous-releases.sh`): the
   release job attaches the `Release`, `Release.gpg` and `Packages` it just signed to the GitHub Release (after the pin
   and keyring guards), and a later run keeps a `.deb` only when `Release.gpg` verifies against the committed keyring,
   the `SHA256` of `Packages` is the one the signed `Release` lists, and the `.deb`'s `SHA256` is the one `Packages`
   lists. A release without that metadata (or with any mismatch) contributes nothing. `publish-apt-repo.sh --previous`
   then applies its own checks (§36.5).
4. **Signing.** Offline certify-only primary key + signing subkey (expiry); only the armored secret subkey is a CI secret
   (`LINDOS_APT_SIGNING_KEY`, optional `LINDOS_APT_SIGNING_KEY_PASSPHRASE`, environment `apt-publish`); the public keyring and
   the fingerprint are committed; the key is rotated by shipping a new keyring package while the old key is still valid.
   `build/tools/make-signing-key.sh` prints (and with `--generate` performs) the ceremony and never writes a private key
   inside the repository.
5. **End-to-end test** (`build/tools/repo-e2e.sh`, CI job `repo-e2e`, opt-in): a throw-away key, a copy of the keyring
   package that trusts it (real gpg runs the guard's positive path), builds 1.0.0/1.0.1 (lock-step stamping), publishes with
   `--release` importing the key from an environment variable as CI does, serves it on localhost, installs in an
   `ubuntu:24.04` container, publishes the next version and requires that `lindos-update check --refresh` sees it,
   `update-state.json` lists it under `lindos` with the label `Lindos`, the plan digest is the same for the state file and a
   fresh plan, the helper refuses a wrong digest, `apply` installs it, `check` then says nothing to do, `history` shows the
   upgrade and `cleanup` succeeds; with `with-full-upgrade` also `apply --all`.
6. **Not published:** kernel `.deb`s, the ISO (`docs/RELEASING.md` explains where they go instead).
