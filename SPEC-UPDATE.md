# Lindos — Engineering Specification, Addendum U (Updates)

> Extends [`SPEC.md`](SPEC.md) (§0–§13). Same rules: this file is the contract. Section numbers
> continue at §35 (after Addendum W, [`SPEC-WINDOWS.md`](SPEC-WINDOWS.md) §27–§34).

Goal: once someone has booted Lindos, they never need to reinstall it to get new fixes. Two update
channels exist and are kept separate, honestly:

1. **The base system** (kernel, XFCE, Firefox, Wine, everything from Ubuntu/Mint's own
   repositories) already updates through **Mint's own Update Manager (`mintupdate`)**, which the
   debloat script explicitly protects (`build/chroot/10-debloat.sh` `PROTECTED` list). Addendum U
   does not touch this — it already works.
2. **Lindos's own 12 packages** (`lindos-*`) have no update channel today; they only ever land on
   the machine at ISO-build time. Addendum U adds one: a real signed apt repository that
   `mintupdate`/`apt` picks up automatically once it exists, **plus** an honest fallback for anyone
   who hasn't set one up yet (`lindos-update`'s "sideload" mode: apply `.deb` files placed in a
   local folder). Hosting a real repo is an infrastructure step outside what code can solve — this
   addendum builds the pipeline and wires the placeholder; it does not claim a repo exists.

## 35. Honesty rules for this addendum (extend §0.1, §27)

- `lindos-update` never installs anything the user has not asked for. Checking is always safe and
  needs no privilege; applying kernel/base-package updates is opt-in (`--include-kernel` /
  confirmation), never silent, never scheduled to run unattended.
- The repo-URL is a **placeholder** (`https://packages.lindos.dev` — not a domain Lindos owns) until
  someone points `LINDOS_APT_REPO_URL` at real hosting. `lindos-update` and the Settings page must
  say plainly when no repo is configured or reachable, never pretend one exists.
- The signing key is generated, never hard-coded; a repo without a valid signature is refused, not
  downgraded to "trusted" automatically.
- Nothing here ever disables `apt-daily`/`apt-daily-upgrade`/`mintupdate`'s own background refresh
  (§11's `SERVICE_WHITELIST` already lists them) — Addendum U reads their result, it does not
  replace or duplicate their privileged background timer.

## 36. `lindos-update` (in `lindos-core`)

### 36.1 CLI (`/usr/bin/lindos-update`, argparse, `--json` on every read)

```
lindos-update check [--json]
lindos-update apply [--include-kernel] [--yes] [--dry-run] [--json]
lindos-update cleanup [--yes] [--dry-run] [--json]       # apt-get autoremove --purge (old kernels/orphans)
lindos-update kernel-status [--json]
lindos-update sideload <DIR> [--yes] [--dry-run] [--json]  # install/replace lindos-* .debs from a local folder
lindos-update repo status [--json]                         # is LINDOS_APT_REPO_URL configured & reachable
```
Exit codes: 0 ok · 1 error · 2 usage · 3 nothing to do.

### 36.2 `lindos/update.py` API (binding)

```python
@dataclass
class PackageUpdate:
    name: str; installed: str; candidate: str; channel: str   # channel: "lindos" | "system" | "kernel"

@dataclass
class UpdateStatus:
    refreshed_at: Optional[str]       # ISO-8601, mtime of apt's lists dir; None if never refreshed
    lindos_updates: List[PackageUpdate]
    system_updates: List[PackageUpdate]   # count only matters here; Settings just says "N available — open Update Manager"
    kernel_available: Optional[PackageUpdate]
    booted_kernel: str                 # uname -r
    booted_is_lindos_kernel: bool
    reboot_required: bool              # /var/run/reboot-required exists
    repo_configured: bool
    repo_reachable: Optional[bool]     # None when repo_configured is False

def apt_list_upgradable(*, run: Callable[..., Any] = subprocess.run) -> List[PackageUpdate]     # parses `apt list --upgradable` (no root)
def check(*, run: Callable[..., Any] = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which,
          fetch: Optional[Callable[[str], Tuple[bool, str]]] = None) -> UpdateStatus     # never calls apt-get update itself (read-only, no root)
def refresh_payload() -> Dict[str, object]                                # {} — helper action "apt-get-update" takes no fields
def apply_payload(names_versions: Mapping[str, str], *, allow_kernel: bool = False) -> Dict[str, object]
    # {"packages": ["lindos-core=1.0.1", ...]} — always an explicit, exact list; never a bare "upgrade everything"
def cleanup_payload() -> Dict[str, object]                                # {}
def repo_status(url: str, *, fetch: Optional[Callable[[str], Tuple[bool, str]]] = None) -> Tuple[bool, str]             # (reachable, message); HTTPS required unless localhost
```
`check()` never elevates and never refreshes the apt cache itself — it reads whatever
`apt-daily.timer` (or a manual `lindos-update`-triggered refresh, see below) already downloaded, so
running it costs nothing and can be called as often as the UI wants. A **separate**, explicit,
privileged step refreshes the cache: helper action `apt-get-update` (§36.4) runs plain
`apt-get update`, nothing else. `apply()`/`cleanup()` always compute an **exact package=version
list** in Python first (from `check()`'s own output) and send that list to the helper — never a
bare `apt-get upgrade`/`dist-upgrade` with no visibility into what it will touch. Kernel packages
(`linux-image-*`, `linux-headers-*`, `linux-modules-*`) are excluded from the list unless
`--include-kernel`.

### 36.3 Sideload (`.deb` folder, no repo needed)

`lindos-update sideload <DIR>` inspects every `*.deb` in `DIR` (via `dpkg-deb --field`), keeps only
packages whose name matches `^lindos-`, verifies each is not older than what's installed (unless
`--yes` twice — actually just `--yes`; a downgrade is not blocked, only warned about), and passes
the exact file list to helper action `install-local-debs`. This lets someone who built or downloaded
new `lindos-*.deb`s (e.g. from a CI artifact) update without any apt repo at all — the honest,
today-usable path while `LINDOS_APT_REPO_URL` is a placeholder.

### 36.4 New helper actions (`lindos.helper.ACTIONS` + `lindos-helper` handlers)

| action | payload | effect |
|---|---|---|
| `apt-get-update` | `{}` | `apt-get update` (refresh package indexes only) |
| `system-upgrade` | `{"packages": ["name=version", …]}` | validates every entry against `PACKAGE_RE`+`=`+a version-string pattern, checks each is actually installable via `apt-get install --only-upgrade -y --` with the **exact** `name=version` tokens (never a bare `upgrade`); refuses if the list contains a kernel package unless `{"allow_kernel": true}` is also set |
| `cleanup-old-packages` | `{}` | `apt-get autoremove --purge -y` |
| `install-local-debs` | `{"files": ["/abs/path/lindos-core_1.0.1_all.deb", …]}` | validates every path is absolute, exists, ends `.deb`, and its `dpkg-deb --field … Package` matches `^lindos-`; then `dpkg -i` the list followed by `apt-get -f install -y` |

### 36.5 Repo publishing (`build/publish-apt-repo.sh`)

```
build/publish-apt-repo.sh [--in DIR=out/debs] [--kernel-in DIR=out/kernel] [--out DIR=out/apt-repo]
                          [--key-id ID] [--gen-key]
```
Builds a **flat-format** apt repository (no `dists/` tree — works from any static file host,
including one that can't serve arbitrary directory redirects): `Packages`, `Packages.gz`, `Release`
(`apt-ftparchive`), signed `InRelease` + `Release.gpg` (`gpg --local-user <key>`), and copies every
`.deb` alongside. `--gen-key` creates `out/apt-repo/lindos-archive-keyring.gpg` (a fresh ed25519/
RSA-4096 key, never checked into git) when no signing key is available — CI uses a secret-provided
key when one exists, else generates an ephemeral one and clearly labels the artifact
"unsigned-CI-key, do not treat as a stable trust root" in a `README-CI-KEY.txt` it writes alongside.
Resulting sources.list line: `deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg]
<LINDOS_APT_REPO_URL> ./`.

### 36.6 Build/ISO wiring

- `build/config.env` gains `LINDOS_APT_REPO_URL` (default: the placeholder
  `https://packages.lindos.dev`, commented with "not a real host — see docs/UPDATES.md").
  `build/chroot/00-repos.sh` installs the keyring (fetched from `LINDOS_APT_REPO_ENABLE=1`'s
  configured key path, or skipped with a log line when unset) and writes
  `/etc/apt/sources.list.d/lindos.list` **only when `LINDOS_APT_REPO_ENABLE=1`** (default `0` —
  an ISO built today ships with no repo configured, exactly as honest as the current state, and
  the OOBE/Settings "Lindos updates" area shows "not configured yet" instead of a dead URL).
- CI job `publish-repo` (workflow_dispatch, `needs: debs`) runs `build/publish-apt-repo.sh`, uploads
  `out/apt-repo/**` as an artifact always, and additionally deploys it to GitHub Pages **only** when
  the repository variable `LINDOS_PAGES_ENABLED` is `true` (left unset by default — enabling it is a
  one-time choice for whoever owns the repo, documented in `docs/UPDATES.md`, not something CI does
  on its own).

## 37. UI

- **Settings** → new "Updates" page (`lindos-settings`, `pages.json` id `updates`, keywords `update
  upgrade apt package manager mintupdate lindos-update kernel`): "Operating system & apps" section
  = one button, "Open Update Manager" (`mintupdate`, unchanged, already works); "Lindos components"
  section lists each `lindos-*` package's installed/available version from `lindos-update check
  --json`, a "Check now" button (helper `apt-get-update` + re-check), an "Update now" button when
  updates exist (confirms, then helper `system-upgrade`), and — only when `repo_configured` is
  `False` — a plain note "No Lindos update channel is configured yet" with a "Load from a folder…"
  file picker calling `lindos-update sideload`. Kernel card: booted vs installed vs available,
  Secure-Boot-signed indicator (reuses `lindos-kernel secureboot status`, Addendum W §31.3), "apply
  now (needs a restart)" when a new one exists.
- A read-only, unprivileged autostart entry `lindos-update-notify` (`~/.config/autostart`, or system
  `/etc/xdg/autostart` per §5 convention) runs `lindos-update check --json` on login and on a 6-hour
  systemd **user** timer, and shows one `notify-send` toast when `lindos_updates` is non-empty
  (throttled to once per boot per version so it never nags). It never elevates and never installs
  anything itself.

## 38. Docs & tests

- New `docs/UPDATES.md`: how updates work (the two channels), what "no repo configured" means, how
  to self-host one (`build/publish-apt-repo.sh` + any static HTTPS host + `LINDOS_APT_REPO_URL`/
  `LINDOS_APT_REPO_ENABLE=1` in `config.env`), and the sideload path for today.
- Tests: `lindos-core` gains `lindos/update.py` coverage (parsing `apt list --upgradable` fixtures,
  payload building/validation, kernel exclusion, sideload package-name filtering) and helper-action
  tests (`apt-get-update`, `system-upgrade` payload validation incl. rejecting a bare package name
  with no `=version`, `cleanup-old-packages`, `install-local-debs` path/name validation, kernel-gate
  refusal). `build/tests/test_publish_apt_repo.py` (bash -n + argument parsing + a hermetic run
  against fixture `.deb`-shaped files, mocking `apt-ftparchive`/`gpg` via `PATH` stubs).
  `lindos-settings` gains an "Updates" page test (JSON-parsing helpers, defensive handling of a
  missing `lindos-update` binary or non-zero exit, same pattern as Addendum W's other pages).
