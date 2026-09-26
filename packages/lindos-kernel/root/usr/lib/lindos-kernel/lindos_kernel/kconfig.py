"""Parse and validate the ``lindos.config`` kconfig fragment (SPEC-KERNEL §15.2).

A fragment contains only ``CONFIG_*=y|m|n`` (or ``=<literal>``) assignments and ``# comment``
lines.  The rules this module enforces, and which the unit test asserts:

* every :data:`REQUIRED_KEYS` symbol is present (with any required literal value), and at least
  one symbol from each :data:`REQUIRED_ANY` alternative group is present;
* no duplicate keys, and no key set to two *different* values.

The build recipe merges this fragment onto a base ``defconfig`` — it is intentionally a
*fragment*, so unknown symbols keep their defconfig value and are not an error here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import config_path

#: ``CONFIG_FOO=y`` / ``CONFIG_FOO=1000`` / ``CONFIG_FOO="str"``.
ASSIGN_RE = re.compile(r'^(CONFIG_[A-Z0-9_]+)=("?[^#]*?"?)\s*$')
#: ``# CONFIG_FOO is not set`` — the canonical kconfig way of writing ``=n``.
NOTSET_RE = re.compile(r"^#\s*(CONFIG_[A-Z0-9_]+)\s+is not set\s*$")

#: Symbols that MUST be present.  (SPEC-KERNEL §15.2.)
REQUIRED_KEYS: Tuple[str, ...] = (
    "CONFIG_NTSYNC",
    "CONFIG_SCHED_CLASS_EXT",
    "CONFIG_HZ_1000",
    "CONFIG_HZ",
    "CONFIG_PREEMPT",
    "CONFIG_LRU_GEN",
    "CONFIG_LRU_GEN_ENABLED",
    "CONFIG_ZRAM",
    "CONFIG_ZSWAP",
    "CONFIG_TRANSPARENT_HUGEPAGE",
    "CONFIG_TRANSPARENT_HUGEPAGE_MADVISE",
    "CONFIG_TCP_CONG_BBR",
    "CONFIG_NET_SCH_FQ",
    "CONFIG_X86_AMD_PSTATE",
    "CONFIG_FUTEX",
    "CONFIG_USER_NS",
    "CONFIG_CHECKPOINT_RESTORE",
) + (
    # --- Addendum W (SPEC-WINDOWS §31.1): Windows-format + transfer/play-anywhere plumbing ---
    "CONFIG_NTFS3_FS",
    "CONFIG_NTFS3_LZX_XPRESS",
    "CONFIG_NTFS3_FS_POSIX_ACL",
    "CONFIG_EXFAT_FS",
    "CONFIG_UNICODE",
    "CONFIG_BINFMT_MISC",
    "CONFIG_EFIVAR_FS",
    "CONFIG_DM_CRYPT",
    "CONFIG_CRYPTO_USER_API_SKCIPHER",
    "CONFIG_BLK_DEV_LOOP",
    "CONFIG_ISO9660_FS",
    "CONFIG_JOLIET",
    "CONFIG_UDF_FS",
    "CONFIG_FUSE_FS",
    "CONFIG_LDM_PARTITION",
)

#: The subset of :data:`REQUIRED_KEYS` added by Addendum W (SPEC-WINDOWS §31.1), kept as a
#: separate tuple purely so tests and docs can refer to "the Windows-format keys" by name.
REQUIRED_KEYS_ADDENDUM_W: Tuple[str, ...] = (
    "CONFIG_NTFS3_FS",
    "CONFIG_NTFS3_LZX_XPRESS",
    "CONFIG_NTFS3_FS_POSIX_ACL",
    "CONFIG_EXFAT_FS",
    "CONFIG_UNICODE",
    "CONFIG_BINFMT_MISC",
    "CONFIG_EFIVAR_FS",
    "CONFIG_DM_CRYPT",
    "CONFIG_CRYPTO_USER_API_SKCIPHER",
    "CONFIG_BLK_DEV_LOOP",
    "CONFIG_ISO9660_FS",
    "CONFIG_JOLIET",
    "CONFIG_UDF_FS",
    "CONFIG_FUSE_FS",
    "CONFIG_LDM_PARTITION",
)

#: Alternative groups: at least one symbol from each group must be present.
REQUIRED_ANY: Tuple[Tuple[str, ...], ...] = (
    ("CONFIG_IOSCHED_BFQ", "CONFIG_MQ_IOSCHED_KYBER"),
)

#: Symbols that must be set to a specific literal value when present.
REQUIRED_VALUES: Dict[str, str] = {
    "CONFIG_HZ": "1000",
    "CONFIG_HZ_1000": "y",
    "CONFIG_LRU_GEN": "y",
    "CONFIG_LRU_GEN_ENABLED": "y",
    "CONFIG_TRANSPARENT_HUGEPAGE_MADVISE": "y",
}


class KConfigError(ValueError):
    """Raised on a malformed fragment or a failed :func:`validate` in strict mode."""


@dataclass
class KConfig:
    """A parsed kconfig fragment."""

    #: ``(key, value, lineno)`` in file order (duplicates preserved for diagnostics).
    pairs: List[Tuple[str, str, int]] = field(default_factory=list)

    @property
    def mapping(self) -> Dict[str, str]:
        """``key -> value`` (last assignment wins; use :meth:`duplicates` to detect clashes)."""
        out: Dict[str, str] = {}
        for key, value, _lineno in self.pairs:
            out[key] = value
        return out

    def keys(self) -> List[str]:
        seen: List[str] = []
        for key, _value, _lineno in self.pairs:
            if key not in seen:
                seen.append(key)
        return seen

    def get(self, key: str) -> Optional[str]:
        return self.mapping.get(key)

    def duplicates(self) -> Dict[str, List[str]]:
        """``key -> [values...]`` for every key that appears more than once."""
        by_key: Dict[str, List[str]] = {}
        for key, value, _lineno in self.pairs:
            by_key.setdefault(key, []).append(value)
        return {key: vals for key, vals in by_key.items() if len(vals) > 1}

    def conflicts(self) -> Dict[str, List[str]]:
        """Duplicates whose values *disagree* (the only kind that is an error)."""
        out: Dict[str, List[str]] = {}
        for key, vals in self.duplicates().items():
            distinct = sorted(set(vals))
            if len(distinct) > 1:
                out[key] = distinct
        return out

    def missing_required(self) -> List[str]:
        """Required keys / alternative groups that are absent."""
        present = set(self.keys())
        missing: List[str] = [key for key in REQUIRED_KEYS if key not in present]
        for group in REQUIRED_ANY:
            if not present.intersection(group):
                missing.append("|".join(group))
        return missing

    def wrong_values(self) -> Dict[str, Tuple[str, str]]:
        """Required-value keys present but set wrong: ``key -> (expected, actual)``."""
        mapping = self.mapping
        bad: Dict[str, Tuple[str, str]] = {}
        for key, expected in REQUIRED_VALUES.items():
            if key in mapping and mapping[key] != expected:
                bad[key] = (expected, mapping[key])
        return bad

    def problems(self) -> List[str]:
        """Every rule violation as a human-readable string (empty ⇒ valid)."""
        issues: List[str] = []
        for key in self.missing_required():
            issues.append(f"missing required key: {key}")
        for key, distinct in self.conflicts().items():
            issues.append(f"key {key} set to conflicting values: {', '.join(distinct)}")
        for key, (expected, actual) in self.wrong_values().items():
            issues.append(f"key {key} must be {expected!r}, got {actual!r}")
        return issues

    def is_valid(self) -> bool:
        return not self.problems()


def _clean_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse(text: str) -> KConfig:
    """Parse fragment *text*.  Raises :class:`KConfigError` on a syntactically bad line."""
    pairs: List[Tuple[str, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        notset = NOTSET_RE.match(line)
        if notset:
            pairs.append((notset.group(1), "n", lineno))
            continue
        if line.startswith("#"):
            continue
        match = ASSIGN_RE.match(line)
        if not match:
            raise KConfigError(f"line {lineno}: not a valid kconfig assignment: {raw!r}")
        key, value = match.group(1), _clean_value(match.group(2))
        if not value:
            raise KConfigError(f"line {lineno}: empty value for {key}")
        pairs.append((key, value, lineno))
    return KConfig(pairs=pairs)


def parse_file(path: Optional[str] = None) -> KConfig:
    """Parse the fragment at *path* (default: the shipped ``lindos.config``)."""
    target = path or config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError as exc:
        raise KConfigError(f"kconfig fragment not found: {target}") from exc
    except OSError as exc:
        raise KConfigError(f"cannot read kconfig fragment {target}: {exc}") from exc
    return parse(text)


def validate(source: str, *, is_path: bool = False, strict: bool = True) -> KConfig:
    """Parse *source* (text, or a path when *is_path*) and check every rule.

    Returns the :class:`KConfig`.  When *strict* (default), raises :class:`KConfigError`
    listing every problem; otherwise inspect ``.problems()`` on the result.
    """
    cfg = parse_file(source) if is_path else parse(source)
    if strict:
        issues = cfg.problems()
        if issues:
            raise KConfigError("; ".join(issues))
    return cfg


def required_symbols() -> List[str]:
    """Flat list of all required symbols (alternative groups joined with ``|``)."""
    out: List[str] = list(REQUIRED_KEYS)
    for group in REQUIRED_ANY:
        out.append("|".join(group))
    return out


__all__ = [
    "REQUIRED_KEYS", "REQUIRED_KEYS_ADDENDUM_W", "REQUIRED_ANY", "REQUIRED_VALUES", "KConfigError",
    "KConfig", "parse", "parse_file", "validate", "required_symbols",
]
