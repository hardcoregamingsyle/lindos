# build/kernel/config/ — placeholder

This directory is intentionally almost empty. The **single source of truth** for the Lindos
kernel config fragment is **not** here — it lives in the `lindos-kernel` package so that the
same file is shipped to installed systems and read by the `lindos-kernel` CLI:

    packages/lindos-kernel/root/usr/share/lindos/kernel/lindos.config

`build-kernel.sh` reads that file by default (falling back to the installed
`/usr/share/lindos/kernel/lindos.config` when run outside a checkout). Do **not** copy the
fragment here — keep one source of truth.

## When to put something here

Only drop a file in this directory if you need a **full, base defconfig override** for an
unusual target (e.g. a vendor board defconfig) and you want to pass it explicitly:

    build-kernel.sh --config build/kernel/config/my-full.config

Even then, prefer extending `lindos.config` (a *fragment* merged onto `make defconfig`) over
maintaining a whole config by hand — fragments survive kernel version bumps far better.
