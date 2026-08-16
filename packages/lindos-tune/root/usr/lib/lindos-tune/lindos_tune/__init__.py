"""lindos_tune — RAM / performance / hardware-control logic behind ``lindos-tune`` (SPEC §11).

Modules
-------
``common``      shared plumbing: LINDOS_ROOT-aware paths, subprocess runner, chroot detection,
                ``Step``/``Report`` result types and the ``Context`` object passed to every step
``status``      RAM snapshot, zram devices, top RSS, unit states, governor, compositor, verdict
``apply``       ``lindos-tune apply --mode <id>``: base tune + mode overrides, idempotent steps
``zram``        zram backend detection (systemd-zram-generator / zram-tools) and configuration
``services``    whitelist-guarded ``systemctl enable/disable`` and unit-state queries
``governor``    cpufreq governor + intel_pstate/amd_pstate EPP, tmpfiles persistence, PPD
``fan``         ``sensors -j`` parsing, nbfc-linux / fancontrol / thinkpad_acpi fan profiles
``power``       power profiles: powerprofilesctl → tlp → governor fallback
``sched``       sched_ext / SCX schedulers (list/status/set) + the §16 memory knobs (THP, MGLRU)
``report``      Markdown bug report
``privileged``  op payloads ``{"op": apply|zram|governor|services|fan|power, …}`` mapped onto
                lindos-core helper actions (``lindos.helper.run_privileged``) — the helper is
                the single privileged entry; lindos-tune never calls sudo itself
``cli``         argparse front-end used by ``/usr/bin/lindos-tune``

Every module is importable on any OS; nothing touches the filesystem or runs a process at
import time.  All system paths go through :func:`lindos_tune.common.path` so the test-suite
can redirect them with ``LINDOS_ROOT``.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
