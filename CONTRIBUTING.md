# Contributing to Lindos

Thanks for helping. Lindos is small on purpose: a handful of Debian packages, a build pipeline
and honest documentation. The rules below keep it that way.

## 1. The contract comes first

[`SPEC.md`](SPEC.md) defines every name, path, file format, CLI and Python API. **If you need
something that is not in the SPEC, add it to the SPEC first, then implement it.** Pull requests
that silently deviate from the SPEC are sent back; pull requests that change the SPEC and the
code together are welcome.

Honesty rules (SPEC §0.1) apply to *every* string a user can see: UI labels, `.desktop` files,
recipe notes, the compat matrix, docs and this file. In particular:

* Wine/Proton is a translation layer, not Windows — never write "runs Windows natively"
  without that qualification.
* Valorant, Fortnite, League of Legends, Apex Legends, Rainbow Six Siege, Destiny 2, PUBG (and
  every other kernel-anti-cheat title) are **not possible** on any Linux; say so and say why.
* Roblox is "via Sober", never "the Windows client works".
* Adobe CC 2019–2021 recipes are `partial` at best (CS6 `works`); newer releases, Premiere and
  AutoCAD are `broken` and must name native alternatives.
* RAM: "target 350–500 MB, measured as `free -m` used at idle" — no marketing numbers.
* Chrome is downloaded by the installer from Google's own apt repository, and Edge on demand from Microsoft's
  (`install-browser.sh`); never put either on the ISO or install them at build time (`79-installer-flow.sh` dies
  if one is in the image).

## 2. Repository map

```
build/        ISO + .deb pipeline (bash, runs on Ubuntu/Debian or in Docker)
packages/     one directory per .deb: DEBIAN/ (control, maintainer scripts) + root/ (filesystem tree) + tests/
docs/         user/developer docs; docs/COMPATIBILITY.md is GENERATED (see §5)
tests/        run.sh (lint everything), pytest.ini, lindos_testsupport.py (gi stub + sys.path), gen-compat-doc.py
```

Cross-package interfaces (who calls what) are listed in SPEC §13 and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). If you change a CLI flag, a helper payload, a JSON
schema or a path, grep the other packages and the docs for it.

## 3. Style

* **Line endings: LF only**, everywhere (`.gitattributes` and `.editorconfig` enforce it;
  `tests/run.sh` fails on any CR byte). No BOMs. Files are authored on Windows too, so never rely
  on the executable bit — `build/mkdeb.sh` sets permissions in the .deb and every script call in
  the build goes through `bash …` / `sh …`.
* **Shell:** `#!/bin/bash` + `set -Eeuo pipefail` for build scripts and libexec helpers;
  `#!/bin/sh` + `set -e` for `DEBIAN/{preinst,postinst,prerm,postrm}` and `lindos-compositor`.
  Functions, `log()` / `warn()` / `die()`, quote everything, ShellCheck-clean, **no `sudo`
  inside scripts** (callers use pkexec/sudo).
* **Python:** 3.10+ (target 3.12), type hints, stdlib only (PyGObject only in the two GTK
  apps, always guarded), `argparse` CLIs with `--json` where sensible, `logging`, never
  `shell=True`, never run anything at import time. Every module must import on Windows/macOS
  (the test suite runs there): wrap Linux-only calls.
* **Exit codes:** 0 ok, 1 error, 2 usage; 3 = offline where a network is required
  (`install-*.sh`, `lindos-proton`, `lindos-drivers`).
* **Packages:** `Version: 1.0.0`, `Architecture: all`, `Maintainer: Lindos Team <team@lindos.dev>`;
  maintainer scripts idempotent; conffiles listed in `DEBIAN/conffiles`; never ship a file another
  Mint/Ubuntu package owns (dpkg-divert in `preinst` if unavoidable — see lindos-desktop).
* **Docs:** plain Markdown, LF, one topic per file, tables generated from the shipped data files
  where a data file exists (modes, recipes, matrix, ram-budget, shortcuts).

## 4. Running the checks

```sh
make test                      # = bash tests/run.sh
bash tests/run.sh --quick      # static checks only (no pytest, no compat-doc check)
bash tests/run.sh --skip-pytest
python -m pytest -q -c tests/pytest.ini --rootdir . -p lindos_testsupport tests packages/*/tests build/tests
python -m pytest -q packages/lindos-tune/tests            # one package (its conftest works standalone)
make lint                      # bash -n / sh -n / py_compile / shellcheck (if installed) / JSON
```

`tests/run.sh` performs: `bash -n` on every bash script, `sh -n` on every `#!/bin/sh` file,
`python3 -m py_compile` on every `.py` and python bin, `shellcheck` if installed, JSON validity of
every `*.json`, XML well-formedness of every `*.xml`/`*.policy`/`*.svg`/`*.ui`, `.desktop`
sanity (`[Desktop Entry]`, `Name=`, `Exec=`, `desktop-file-validate` if present), CRLF detection,
an exec-bit reminder on Linux, `pytest`, and `tests/gen-compat-doc.py --check`.

The pytest suites are designed to pass **without** GTK, Wine, systemd or root: `tests/
lindos_testsupport.py` installs a permissive `gi` stub only when the real PyGObject is missing,
and every package's tests inject fake runners / `LINDOS_ROOT` / `LINDOS_HOME` sandboxes. Keep it
that way — a test that needs a Linux host must be skipped, not failing, elsewhere.

CI (`.github/workflows/ci.yml`) runs exactly `bash tests/run.sh --verbose` on Ubuntu 24.04
(shellcheck + desktop-file-validate installed) and the pytest suite on Windows, then builds the
`.deb`s with `build/mkdeb.sh --lintian all`; the ISO job only runs on `workflow_dispatch`. On a
Windows host without shellcheck on PATH, `pip install shellcheck-py` is enough — `run.sh` finds
the bundled binary in the interpreter's scripts directory.

## 5. Data files that generate documentation

| Change this | Then |
|---|---|
| `packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json` | `python3 tests/gen-compat-doc.py` — commits `docs/COMPATIBILITY.md`; CI/`tests/run.sh` fail when it is stale. Entry keys: `game, status (native\|works\|partial\|not_possible\|unknown), how, anticheat, reason, link`; `not_possible` entries must not advertise a launcher (`how` = `—`). |
| `packages/lindos-core/root/usr/share/lindos/modes/<id>/mode.json` (+ `packages/lindos-tune/root/etc/lindos/tune.d/<id>.conf`, `packages/lindos-desktop/root/usr/share/lindos/modes/<id>/panel/`) | update the table in `docs/MODES.md` |
| `packages/lindos-compat/root/usr/share/lindos/recipes/<id>.json` | update the recipe table in `docs/WINDOWS-APPS.md`; `id` must equal the file name; `broken` recipes need `alternatives` |
| `packages/lindos-settings/root/usr/share/lindos/settings/pages.json` | update `docs/SETTINGS.md`; `model.BUILTIN_PAGES` must stay identical (a test checks) |
| `packages/lindos-tune/root/usr/share/lindos/tune/ram-budget.json` | update `docs/RAM-BUDGET.md` |
| `packages/lindos-desktop/root/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml` | regenerate the tables in `docs/KEYBOARD-SHORTCUTS.md` (keep the `default` and `custom` branches identical) |
| `build/config.env`, `build/build-iso.sh` flags, `Makefile` targets | update `docs/BUILDING.md` and the README quickstart |
| `build/fetch-assets.sh` (assets, pins) | update `THIRD_PARTY.md` |

## 6. Adding things

* **A mode:** exactly five modes exist by contract; changing them is a SPEC change.
* **A recipe:** copy an existing JSON, keep the schema, set the honest `status`, run
  `packages/lindos-compat/tests` (`recipes.load_recipes(strict=True)` validates it), add it to
  `docs/WINDOWS-APPS.md`.
* **A launcher:** add it to `packages/lindos-gaming/root/usr/share/lindos/gaming/launchers.json`,
  to the `case` in `/usr/libexec/lindos/install-gaming.sh`, to `GAMING_ITEMS` in
  `lindos-core/.../lindos/helper.py` and to `lindos-game`'s help — the tests assert the four agree.
* **A helper action:** SPEC §4.6 lists the fifteen actions; a new one needs a SPEC entry, a
  validator in `lindos/helper.py`, a handler in `/usr/libexec/lindos/lindos-helper`, and a test.
* **A settings page:** register it in `pages.json` *and* `model.BUILTIN_PAGES`, keep the SPEC §7
  order.

## 7. Commit / PR checklist

- [ ] `make test` (or `bash tests/run.sh`) passes locally
- [ ] no CR bytes, no `__pycache__`, no `out/` artefacts committed
- [ ] SPEC updated if a name/path/format/API changed
- [ ] docs updated (see §5), `docs/COMPATIBILITY.md` regenerated if the matrix changed
- [ ] every user-visible string obeys the honesty rules
- [ ] third-party downloads pinned (version + hash where the upstream publishes one) and listed
      in `THIRD_PARTY.md`

Licence: by contributing you agree that your contribution is licensed under GPL-3.0-or-later.
