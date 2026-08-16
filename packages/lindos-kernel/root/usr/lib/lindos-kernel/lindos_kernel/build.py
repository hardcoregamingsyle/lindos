"""Thin wrapper around ``build/kernel/build-kernel.sh`` (SPEC-KERNEL §15.4/§15.5).

The heavy lifting (fetch → merge config → patches → ``make bindeb-pkg``) lives in the shell
recipe, which only runs on a Linux host and is shipped in the ``build/`` subtree, not inside the
installed package.  This module locates that script from a source checkout, builds the argv and
either execs it (Linux) or prints the plan (Windows / no script) without ever compiling.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from . import config_path, is_linux, manifest as _manifest

SCRIPT_ENV = "LINDOS_BUILD_KERNEL"
#: When truthy, ``run`` prints the plan and the exec command but never launches a build.
#: Used by the test-suite (so CI never triggers a real kernel compile) and as a user preview.
DRYRUN_ENV = "LINDOS_KERNEL_BUILD_DRYRUN"
SCRIPT_RELPATH = os.path.join("build", "kernel", "build-kernel.sh")


def _dry_run() -> bool:
    return os.environ.get(DRYRUN_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BuildPlan:
    """Everything ``build`` would do, without doing it."""

    version: Optional[str]
    config: str
    localversion: str
    jobs: Optional[int]
    out: str
    kdir: Optional[str]
    menuconfig: bool
    script: Optional[str]

    def argv(self) -> List[str]:
        """The argument vector passed to ``build-kernel.sh`` (script path excluded)."""
        args: List[str] = []
        if self.version:
            args += ["--version", self.version]
        args += ["--config", self.config, "--localversion", self.localversion, "--out", self.out]
        if self.jobs is not None:
            args += ["--jobs", str(self.jobs)]
        if self.kdir:
            args += ["--kdir", self.kdir, "--skip-fetch"]
        if self.menuconfig:
            args += ["--menuconfig"]
        return args

    def describe(self) -> List[str]:
        """Human-readable plan lines."""
        lines = [
            f"kernel version : {self.version or '(manifest recommended series, resolved by the script)'}",
            f"config fragment: {self.config}",
            f"localversion   : {self.localversion}",
            f"jobs           : {self.jobs if self.jobs is not None else 'nproc'}",
            f"output dir     : {self.out}",
        ]
        if self.kdir:
            lines.append(f"kernel dir     : {self.kdir} (--skip-fetch)")
        if self.menuconfig:
            lines.append("menuconfig     : yes")
        lines.append(f"build script   : {self.script or '(not found — see below)'}")
        return lines


def find_script() -> Optional[str]:
    """Locate ``build-kernel.sh``: ``$LINDOS_BUILD_KERNEL``, then upward from CWD/this file."""
    override = os.environ.get(SCRIPT_ENV)
    if override and os.path.isfile(override):
        return override
    starts = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]
    for start in starts:
        cur = start
        for _ in range(8):
            candidate = os.path.join(cur, SCRIPT_RELPATH)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return None


def default_version() -> Optional[str]:
    """The manifest's recommended series (used when ``--version`` is omitted)."""
    try:
        return _manifest.load().recommended.series
    except _manifest.ManifestError:
        return None


def make_plan(version: Optional[str] = None, config: Optional[str] = None,
              localversion: str = "-lindos", jobs: Optional[int] = None,
              out: str = "out/kernel", kdir: Optional[str] = None,
              menuconfig: bool = False) -> BuildPlan:
    return BuildPlan(
        version=version or default_version(),
        config=config or config_path(),
        localversion=localversion,
        jobs=jobs,
        out=out,
        kdir=kdir,
        menuconfig=menuconfig,
        script=find_script(),
    )


def obtain_hint() -> List[str]:
    """What to tell the user when the build script is not on disk (installed package)."""
    return [
        "build-kernel.sh is not installed with the package (kernels are built on a Linux host,",
        "never on the user's machine).  Get it from a Lindos source checkout:",
        "    git clone https://lindos.dev/lindos && cd lindos",
        "    build/kernel/build-kernel.sh --help",
    ]


def run(plan: BuildPlan, log=print) -> int:
    """Execute the plan.

    * Non-Linux host: print the plan and return 0 (nothing is compiled — SPEC-KERNEL §15.4/§15.5).
    * Linux but no script: print how to obtain it and return 2.
    * Linux + script: exec ``bash build-kernel.sh <argv>`` and return its exit code.
    """
    for line in plan.describe():
        log(line)
    if not is_linux():
        log("")
        log("not a Linux host: printing the plan only, no kernel is built.")
        return 0
    if not plan.script:
        log("")
        for line in obtain_hint():
            log(line)
        return 2
    bash = shutil.which("bash") or "/bin/bash"
    cmd = [bash, plan.script, *plan.argv()]
    log("")
    log("exec: " + " ".join(cmd))
    if _dry_run():
        log(f"{DRYRUN_ENV} set: plan only, not launching the build.")
        return 0
    try:
        completed = subprocess.run(cmd, check=False)
    except OSError as exc:
        log(f"failed to start build script: {exc}")
        return 2
    return completed.returncode


__all__ = [
    "SCRIPT_ENV", "SCRIPT_RELPATH", "BuildPlan", "find_script", "default_version",
    "make_plan", "obtain_hint", "run",
]
