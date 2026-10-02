# Releasing Lindos: what only the owner can do

> **Who this is for:** the person who owns the GitHub repository and the domain. Everything the build,
> the tests and CI can do without you is done; this page is the exact list of what is left, in order, with
> copy-paste commands. Nothing here is automatic on purpose: a domain, a signing key and a hosting switch are
> decisions with consequences. How updates work once this is done: [`UPDATES.md`](UPDATES.md); the contract:
> [`SPEC-UPDATE.md`](../SPEC-UPDATE.md) §43.
>
> Commands assume `export REPO=hardcoregamingsyle/lindos` and the GitHub CLI (`gh auth login`) as the owner. The
> `gh api` commands are from memory of the GitHub REST API and have not been run by the tooling: if one is
> refused, do the same thing in the web UI (the path is given each time).

## The checklist

- [ ] 1. Keep the repository **public** (or decide otherwise, section 1)
- [ ] 2. Register the domain and point `apt.<your domain>` at GitHub Pages (section 2)
- [ ] 3. Make the signing key **offline** (section 3)
- [ ] 4. Commit the *public* key, its fingerprint and the real address in **one** commit (section 4)
- [ ] 5. GitHub: environment `apt-publish`, two secrets, Pages, one variable (section 5)
- [ ] 6. Test before you release: run the `repo-e2e` job (section 6)
- [ ] 7. First tagged release (section 7)
- [ ] 8. Check it from a clean machine (section 8)
- [ ] 9. Only then build a release image with `LINDOS_APT_REPO_REQUIRE=1` (section 9)

Until step 4 the Lindos update source is switched off and no image trusts anything: that is the honest state
of a repository nobody has hosted yet.

## 1. Repository visibility

GitHub Pages is free for **public** repositories and needs a paid plan for private ones; on the free plan a
private repository also gets only 2,000 Actions minutes a month and 500 MB of artifact storage (the CI's ISO
artifact alone is about 3.75 GB). So: keep it public.

```
gh repo view "$REPO" --json visibility,isPrivate      # expect PUBLIC
```

If it must be private, the update repository has to be published from a *separate public* repository that only
holds the published files; that workflow is not built - ask for it before you change the visibility.

## 2. The domain

The project already names `lindos.dev` (the maintainer address `team@lindos.dev`, the homepage, the vendor URL
of every package). When this was last checked (2026-09-29) it was **not registered** - anyone could take it:

```
curl -s -o /dev/null -w '%{http_code}\n' https://pubapi.registry.google/rdap/domain/lindos.dev   # 404 = free (check again)
```

1. Register it (or another domain you control), and turn on the registrar lock, two-factor login and auto-renew.
   Do this **before** building an image you give to anyone: the address is written into installed systems (it
   can be moved later by a package update, but only while the old address still answers).
2. DNS: a `CNAME` record `apt` -> `hardcoregamingsyle.github.io.` (your GitHub user or organisation, with the
   trailing dot). Optionally mail routing for `team@` / `archive@`.
3. The hostname is only what you put into the source file in step 4; the recommended one is
   `https://apt.lindos.dev/stable/`. The repository is served under `/stable/`, so a testing channel can be added
   later without touching installed systems.

## 3. The signing key (offline)

Apt trusts one thing: the public keyring shipped in the `lindos-archive-keyring` package. The key is created
**once, offline**: an ed25519 *certify-only* primary key that never leaves your offline medium, plus a *signing
subkey* with an expiry that CI uses. Only the armored secret subkey goes to GitHub.

```
bash build/tools/make-signing-key.sh                 # prints every step with copy-paste commands, runs nothing
bash build/tools/make-signing-key.sh --generate --out /media/keys/lindos-archive    # or do the gpg steps for you
```

Use a machine that is offline (a live USB session is ideal) and an encrypted or removable output medium **outside**
this repository (the script refuses a directory inside it). Choose a long passphrase and write it down offline.
It creates, in the output directory:

| file | what | goes |
|---|---|---|
| `lindos-archive-keyring.gpg` | the public keyring (primary key + both signing subkeys) | into the repository (step 4) |
| `archive-key.fingerprint` | the primary key's 40-digit fingerprint (the pin) | into the repository (step 4) |
| `lindos-signing-subkey.asc` | the armored *secret* signing subkey | into the GitHub secret, then destroyed (step 5) |
| `revoke.asc` | the revocation certificate | offline, in two places |
| `gnupg/` | the **primary key** | offline, in two places, never online |

**Recommended: a spare subkey.** The steps create two signing subkeys; only the first goes to CI, the second stays
offline. Both are in the public keyring installed systems trust, so if the CI secret ever leaks you rotate by
switching CI to the spare (section 10) - a rotation that needs no action on installed systems. A key that is not in
the keyring already cannot be introduced later without a keyring update signed by a key the system already trusts.

Never paste a private key into a chat, an issue, an email or a file in this repository. Never store the primary key
on a networked machine.

## 4. Commit the public parts - in one commit

The Lindos update source is switched off and its key is a placeholder text file. A build refuses to ship a
switched-on source without a real key, its fingerprint and a real address, so all of it goes in together:

```
cp /media/keys/lindos-archive/lindos-archive-keyring.gpg packages/lindos-archive-keyring/root/usr/share/keyrings/lindos-archive-keyring.gpg
cp /media/keys/lindos-archive/archive-key.fingerprint    packages/lindos-archive-keyring/root/usr/share/lindos/archive-key.fingerprint
$EDITOR packages/lindos-archive-keyring/root/etc/apt/sources.list.d/lindos.sources
#   URIs: https://apt.lindos.dev/stable/        <- your address, with the trailing slash
#   Enabled: yes                                <- was: no

bash build/tools/check-archive-keyring.sh packages/lindos-archive-keyring/root      # must print: real
python -m pytest -q -c tests/pytest.ini --rootdir . -p lindos_testsupport packages/lindos-archive-keyring/tests build/tests/test_archive_keyring_wiring.py
git add packages/lindos-archive-keyring && git commit -m "feat: the Lindos archive key and update source (enabled)" && git push
```

The guard refuses (with the reason) an enabled source with a placeholder or armored key, a missing fingerprint,
a `.invalid`/`example` address, a keyring whose primary fingerprint is not the pinned one or that holds **more than
one primary key** (apt trusts every key in a keyring; subkeys are fine), a real keyring when `gpg` is not installed
to check it, and a source file with a second stanza or address, a non-https address (plain http only to the local
machine, for the e2e job), a `Signed-By` other than the shipped keyring, or any `Trusted` / `Allow-Insecure`
override. The release job runs the same guard on the committed files again. The address lives in
this one file only; to move the repository later, edit the URIs line and ship a new `lindos-archive-keyring`
release while the old address still answers.

## 5. GitHub settings

**5.1 The `apt-publish` environment** (Settings -> Environments -> New environment). Add yourself as a *required
reviewer* (you approve each release run) and restrict "Deployment branches and tags" to the tag pattern `v*`.

```
ME="$(gh api user --jq .id)"
gh api -X PUT "repos/$REPO/environments/apt-publish" --input - <<EOF
{"reviewers":[{"type":"User","id":$ME}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
EOF
gh api -X POST "repos/$REPO/environments/apt-publish/deployment-branch-policies" -f name='v*' -f type=tag
```

**5.2 The secrets** (into the *environment*, so only the gated release job can read them):

```
gh secret set LINDOS_APT_SIGNING_KEY --env apt-publish --repo "$REPO" < /media/keys/lindos-archive/lindos-signing-subkey.asc
gh secret set LINDOS_APT_SIGNING_KEY_PASSPHRASE --env apt-publish --repo "$REPO"     # type the passphrase when asked
shred -u /media/keys/lindos-archive/lindos-signing-subkey.asc
```

Only the `release-repo` job can see them; the test job (`publish-repo`) never does.

**5.3 GitHub Pages** (Settings -> Pages -> Source: **GitHub Actions**; Custom domain: `apt.lindos.dev`; tick
*Enforce HTTPS* once DNS resolves):

```
gh api -X POST "repos/$REPO/pages" -f build_type=workflow
gh api -X PUT  "repos/$REPO/pages" -f cname=apt.lindos.dev                       # after the DNS record exists
gh api -X PUT  "repos/$REPO/pages" -F https_enforced=true                        # after the certificate is issued
```

**5.4 The switch** that lets CI deploy to Pages (CI never turns Pages on by itself):

```
gh variable set LINDOS_PAGES_ENABLED --body true --repo "$REPO"
```

Without it, a release still builds, signs, verifies and attaches the packages to the GitHub Release - it just does
not publish the site.

**5.5 Do not turn on "immutable releases" (yet).** The release job creates the release, attaches the `.deb`s, and only
after signing attaches the metadata that lets the next release trust them again; an immutable release refuses the
second upload. Using that setting needs a draft-then-publish flow that is not built.

## 6. Test before you release

The opt-in `repo-e2e` job builds a repository with a throw-away key inside CI, serves it on localhost, installs
Lindos from it in a bare Ubuntu 24.04 container, publishes a newer version and checks that `lindos-update` sees
it and installs it (and, with the second option, runs a full `apply --all`). It uses no secret and no key of yours.

```
gh workflow run CI -f repo_e2e=basic --repo "$REPO"                  # or: -f repo_e2e=with-full-upgrade
gh run watch --repo "$REPO" --exit-status
```

It has never run yet: expect to fix small things the first time (it is the first contact with real apt). Do this
before step 7.

## 7. The first tagged release

Versions move in lock-step: every `lindos-*` package of a release has the same version, the one in `VERSION`, and
`lindos-meta` pins its parts to exactly it. A release tag must equal `VERSION` (CI checks it).

```
cat VERSION                                   # e.g. 1.0.0
$EDITOR CHANGELOG.md                          # a "## [1.0.0]" section: it becomes the text of the GitHub Release
git commit -am "release: 1.0.0" && git push
git tag -a v1.0.0 -m "Lindos 1.0.0" && git push origin v1.0.0
gh run watch --repo "$REPO"                   # approve the apt-publish environment when asked
```

What CI does for the tag: lint and tests -> build the `.deb`s (with the version from `VERSION`) -> **wait for your
approval** -> create the GitHub Release and attach the `.deb`s -> fetch the two previous releases (kept so a bad
update can be rolled back; none on the first release) and keep only the `.deb`s the signed metadata published with
that release vouches for byte for byte (`build/tools/fetch-previous-releases.sh`: release assets can be changed by
anyone with write access, so they are never trusted on their own) -> build the repository, sign it with the secret
subkey, **refuse unless the signing key's fingerprint is the committed one** and the committed keyring holds only that
key, and verify the result against the committed keyring -> attach the `Release`, `Release.gpg` and `Packages` it
signed to the GitHub Release (what lets the *next* release keep these packages) -> upload the site as an artifact ->
(only if `LINDOS_PAGES_ENABLED` is true) deploy to Pages.
The tag also starts the long kernel build; it does not delay or block the release.

The kernel `.deb`s (about 115 MiB each) and the ISO (about 3.75 GB - larger than GitHub's 2 GiB release-asset
limit) are **not** part of the repository. Where they are hosted (Cloudflare R2, a mirror, a torrent) is a decision
that is yours and is not built yet.

## 8. Check it from a clean machine

On a fresh Lindos VM (or Ubuntu 24.04) with the keyring package installed:

```
curl -fsSL https://apt.lindos.dev/stable/InRelease | head -n 12                # signed, Origin: Lindos
gpg --show-keys /usr/share/keyrings/lindos-archive-keyring.gpg                 # the fingerprint you pinned
sudo apt-get update && apt-cache policy lindos-core                            # a candidate from the Lindos repository
lindos-update repo status                                                      # configured, reachable
lindos-update check --refresh
```

## 9. Release images

An image built with the source enabled trusts your key from the first boot. Only after steps 1-8 (a stable key,
a tagged release, `repo-e2e` green, and an install-and-update test in a VM), build the image so that it cannot
be made without a working update channel:

```
LINDOS_APT_REPO_REQUIRE=1 make iso
```

Never build an image you give to anyone from a repository that was signed with the throw-away CI key.

## 10. Later releases, rollback, rotating and revoking

**A new release:** bump `VERSION` and `CHANGELOG.md`, commit, tag `vX.Y.Z`, push the tag, approve the run. Never
release a single package: they always move together.

**Rollback** (the last three versions stay in the repository):

```
apt-cache policy lindos-core                                                   # the versions on offer
V=1.0.0; sudo apt-get install --allow-downgrades $(dpkg-query -W -f='${Package}\n' 'lindos-*' | sed "s/\$/=$V/")
```

**Rotating the signing subkey (before it expires; at least six months ahead).** Because both signing subkeys are
already in the keyring installed systems have, the switch needs no update on their side:

1. Offline, export the spare subkey: `gpg --armor --export-secret-subkeys "$SPAREFPR"'!' > spare.asc`.
2. `gh secret set LINDOS_APT_SIGNING_KEY --env apt-publish --repo "$REPO" < spare.asc`, destroy `spare.asc`.
3. Add a new spare to the primary key (`gpg --quick-add-key "$FPR" ed25519 sign 2y`), re-export the public
   keyring, and ship it in the next release of `lindos-archive-keyring` (installed systems pick it up as a normal
   update) - so the *next* rotation has a spare too.

**Extending an expiry:** `gpg --quick-set-expire "$FPR" 2y "$SUBFPR"`, re-export the keyring, ship it in a
keyring release **before** the old expiry (an expired subkey no longer verifies anything).

**If the CI secret leaks:** whoever holds the signing subkey can publish updates to every installed system until
that subkey is revoked in a keyring those systems have. Switch CI to the spare subkey immediately (above), revoke the
leaked one offline (`gpg --edit-key "$FPR"`, `key N`, `revkey`), re-export the keyring and ship it in the next release
signed by the spare. Keep `revoke.asc` for the day the *primary* key is compromised.

**If the primary key is lost:** nothing can be rotated remotely - installed systems only trust the keyring file they
have. Recovery then needs a manual keyring install (or a reinstall). That is why the primary key and its
passphrase are kept in two offline places.

## What CI will never do

- Generate a signing key that ships: the only generated key is the throw-away one of the test jobs, which sign
  artifacts that are never deployed (`publish-repo`, `repo-e2e`).
- Show the signing secret to any job but `release-repo`, or write it to a log, an argument list or an artifact.
- Publish a repository that was not signed with the committed key (the script checks the fingerprint, verifies
  the signature against the committed keyring, and a second CI step compares the fingerprints again).
- Sign a `.deb` it did not build in the same run unless the signed metadata published with the release it came
  from vouches for its exact bytes; a same-named older file never replaces one of the release being published, and
  only `lindos-*` packages of that release, of an older version, are accepted.
- Give the job that holds the signing key any permission but `contents: write` (the Pages deploy is a separate job).
- Enable GitHub Pages, change the domain, or create a release for a tag that is not the version in `VERSION`.
- Install anything on an installed system: updates are only ever installed by a person running `lindos-update`
  (or the Settings page) on their own machine.

## When something fails

| message | meaning |
|---|---|
| `the secret LINDOS_APT_SIGNING_KEY is not set` | step 5.2 is missing, or the secret was put in the repository instead of the `apt-publish` environment |
| `REFUSING to publish: the signing key (...) is not the pinned Lindos archive key (...)` | the secret is not the subkey of the key whose fingerprint you committed (step 3/4 do not match) |
| `--release needs a pinned fingerprint` | `archive-key.fingerprint` is still `PLACEHOLDER` (step 4) |
| `refusing to build ... REFUSED: the source is enabled but ...` | the guard: enabled source without a real key / fingerprint / address (step 4) |
| `the tag v1.2.3 is not v1.0.0, the version in VERSION` | bump `VERSION` first, or tag the right version |
| `fetch-previous-releases: warning: v1.0.0: it has no signed repository metadata` | that release was made before its metadata was attached (or someone deleted it): its packages are left out of the repository, on purpose |
| `... does not match the hash in its signed Packages - left out` / `... is not listed ...` | a `.deb` of an older release was replaced or added after publication; it is not published. Look at who changed the release assets |
| `previous release, ...: version X is not older than Y` / `... is not a package of the release being published` | `publish-apt-repo.sh --previous` refused a file: only older `lindos-*` packages of the current release may stay |
| `--release: the committed keyring holds N primary keys` / `REFUSED: the keyring holds N primary keys` | the committed `lindos-archive-keyring.gpg` has more than the one pinned key (step 3 exports exactly one) |
| `apt` says the repository "is not signed" / `NO_PUBKEY` | the installed keyring is not the one that signed: an image built before step 4, or a mismatched key |
| `apt` says `Release file ... expired` | `--valid-until-days` was turned on without a scheduled re-sign; leave it off |
| the site returns 404 | Pages is not enabled (5.3), the variable is not set (5.4), or the custom domain / DNS is not ready |

## Decisions only you can make

- The domain and the repository hostname; who holds the offline key and its backups.
- Whether to add a separate public repository for the update files (only needed if the code repository goes private).
- Where kernel `.deb`s and the ISO are hosted (they do not fit on Pages / release assets).
- Whether Lindos ever installs updates automatically (today: never; it would have to be an explicit opt-in and
  `SPEC-UPDATE.md` §35 amended first).
- How many releases the repository keeps (three today) and whether users may downgrade one component or only the release.
