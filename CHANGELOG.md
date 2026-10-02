# Changelog

All notable changes to Lindos. The version in [`VERSION`](VERSION) is the release version: every `lindos-*`
package of a release carries exactly that version (lock-step; `build/mkdeb.sh` stamps it, and `lindos-meta`
pins its parts to it), and a release tag `vX.Y.Z` must match it (`docs/RELEASING.md`). The section of the
release being published is used as the text of its GitHub Release.

## [Unreleased]

### Added
- The update system, phase 1 (`SPEC-UPDATE.md`, `docs/UPDATES.md`): a root timer refreshes the package lists
  twice a day and writes `/var/lib/lindos/update-state.json`; `lindos-update` gained `status`, `plan`,
  `apply --all`, `history` and `check --refresh`; the helper gained `apt-full-upgrade` (checked against the plan
  you were shown - and again after the download - never removes what was not approved, repairs afterwards) and a
  safe `cleanup-old-packages`; an apt hook notes `/run/reboot-required`; an interrupted upgrade is repaired at
  the next start.
- `lindos-archive-keyring`: ships the Lindos apt source (switched off until a real signing key and server
  exist) and its public key; it has no dependencies. The image build installs it with the other Lindos packages
  instead of fetching a key from the network, and refuses a keyring that holds more than the one pinned key.
- Release tooling: one `VERSION` file, lock-step version stamping, a signed and pinned apt repository job
  (`release-repo`; it keeps older releases only when their own signed metadata vouches for them), an opt-in
  end-to-end job (`repo-e2e`), the offline key ceremony helper (`build/tools/make-signing-key.sh`) and
  `docs/RELEASING.md`.

### Changed
- The stock apt-daily timers stay switched off on Lindos; Lindos's own refresh timer replaces them
  (the documentation used to claim they were never disabled).
- `lindos-update check --refresh` and `apply` refresh the package lists first (the documentation used to claim
  `apply` did while the code did not); `cleanup` is a safe autoremove instead of a bare one.
- The base system is updated by `lindos-update apply --all`; `lindos-update` no longer sends you to another
  update tool.

## [1.0.0]

First version ("Aurora"): the Windows-11-style desktop remaster, the Wine/Proton layer, the installer that does
everything while it installs, the first-run wizard and Settings. Not yet released: there is no hosted update
repository until the owner creates the signing key and enables it (`docs/RELEASING.md`).
