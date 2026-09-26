"""Read-only, bounds-checked reader for Windows registry hive files ("regf") (SPEC-WINDOWS §29.10).

Used on the *offline* hives of a Windows partition (``SOFTWARE``, ``SYSTEM``, each user's
``NTUSER.DAT``) and never on the deny-listed ``SAM``/``SECURITY`` (opening goes through
:func:`lindos_transfer.secrets.safe_open`, which refuses them).  Nothing is ever written.

Format (msuhanov "Windows registry file format specification", libyal libregf):

* base block (4096 bytes): ``regf``; primary/secondary sequence numbers @0x04/0x08; major @0x14
  (1); minor @0x18 (3..6); file type @0x1C (0 = primary hive); root cell offset @0x24; hive-bins
  data size @0x28; XOR checksum of the 127 dwords 0x000..0x1FB stored @0x1FC.
* hive bins start at file offset 0x1000; every cell offset is relative to it.  A cell is an i32
  size (negative = allocated, 8-byte aligned, includes the size field) followed by its data.
* ``nk`` key node (flags @0x02, KEY_COMP_NAME 0x20 = Latin-1 name else UTF-16LE; subkey count
  @0x14, subkey list @0x1C; value count @0x24, value list @0x28; name length @0x48; name @0x4C).
* subkey lists: ``lf``/``lh`` (offset + 4-byte hint), ``li`` (offsets), ``ri`` (offsets of leaf lists;
  never another ``ri``).
* ``vk`` value (name length @0x02, data size @0x04 -- MSB set = up to 4 bytes inline in the offset
  field --, data offset @0x08, type @0x0C, flags @0x10: 0x0001 Latin-1 name, 0x0002 tombstone
  (skipped); name @0x14).
* ``db`` big data (values > 16344 bytes when minor > 3): segment count, segment-list offset;
  segments of at most 16344 bytes are concatenated.

A hive whose checksum is wrong or whose two sequence numbers differ is *dirty*: its latest changes
may live only in the ``.LOG1``/``.LOG2`` files, which this reader deliberately does **not**
replay.  Such hives open fine but report ``dirty = stale = True`` so callers can label the results
"possibly out of date".  Out-of-range offsets, unallocated cells, bad signatures and key loops
raise :class:`RegfError`; nothing is trusted.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple, Union

from . import secrets

__all__ = [
    "RegfError",
    "REG_NONE", "REG_SZ", "REG_EXPAND_SZ", "REG_BINARY", "REG_DWORD", "REG_DWORD_BIG_ENDIAN",
    "REG_LINK", "REG_MULTI_SZ", "REG_RESOURCE_LIST", "REG_FULL_RESOURCE_DESCRIPTOR",
    "REG_RESOURCE_REQUIREMENTS_LIST", "REG_QWORD", "TYPE_NAMES",
    "BIG_DATA_SEGMENT", "MAX_HIVE_SIZE",
    "RegValue", "Key", "Hive", "decode_value", "checksum", "iter_values",
]

REG_NONE = 0
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4
REG_DWORD_BIG_ENDIAN = 5
REG_LINK = 6
REG_MULTI_SZ = 7
REG_RESOURCE_LIST = 8
REG_FULL_RESOURCE_DESCRIPTOR = 9
REG_RESOURCE_REQUIREMENTS_LIST = 10
REG_QWORD = 11

TYPE_NAMES: Dict[int, str] = {
    REG_NONE: "REG_NONE", REG_SZ: "REG_SZ", REG_EXPAND_SZ: "REG_EXPAND_SZ", REG_BINARY: "REG_BINARY",
    REG_DWORD: "REG_DWORD", REG_DWORD_BIG_ENDIAN: "REG_DWORD_BIG_ENDIAN", REG_LINK: "REG_LINK",
    REG_MULTI_SZ: "REG_MULTI_SZ", REG_RESOURCE_LIST: "REG_RESOURCE_LIST",
    REG_FULL_RESOURCE_DESCRIPTOR: "REG_FULL_RESOURCE_DESCRIPTOR",
    REG_RESOURCE_REQUIREMENTS_LIST: "REG_RESOURCE_REQUIREMENTS_LIST", REG_QWORD: "REG_QWORD",
}

BASE_BLOCK_SIZE = 4096
HBIN_START = 0x1000
BIG_DATA_SEGMENT = 16344
#: Largest hive file this reader accepts (real SOFTWARE hives are ~50-300 MB).
MAX_HIVE_SIZE = 2 << 30
#: Largest single value this reader returns.
MAX_VALUE_SIZE = 64 << 20
#: Deepest key nesting accepted (Windows itself limits paths to 512 levels).
MAX_DEPTH = 512
_MMAP_THRESHOLD = 32 << 20

KEY_COMP_NAME = 0x0020
VALUE_COMP_NAME = 0x0001
VALUE_TOMBSTONE = 0x0002
_NONE = 0xFFFFFFFF

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")

Buffer = Union[bytes, bytearray, memoryview, mmap.mmap]


class RegfError(ValueError):
    """The file is not a readable registry hive (or this part of it is damaged)."""


# --------------------------------------------------------------------------- #
# decoding
# --------------------------------------------------------------------------- #
def _utf16z(raw: bytes) -> str:
    if len(raw) % 2:
        raw = raw[:-1]
    text = raw.decode("utf-16-le", errors="replace")
    cut = text.find("\x00")
    return text if cut < 0 else text[:cut]


def decode_value(vtype: int, raw: bytes) -> Any:
    """Decode *raw* value bytes by registry type (strings are cut at the first NUL)."""
    if vtype in (REG_SZ, REG_EXPAND_SZ, REG_LINK):
        return _utf16z(raw)
    if vtype == REG_MULTI_SZ:
        if len(raw) % 2:
            raw = raw[:-1]
        out: List[str] = []
        for part in raw.decode("utf-16-le", errors="replace").split("\x00"):
            if not part:
                break
            out.append(part)
        return out
    if vtype == REG_DWORD:
        return _U32.unpack(raw[:4])[0] if len(raw) >= 4 else None
    if vtype == REG_DWORD_BIG_ENDIAN:
        return struct.unpack(">I", raw[:4])[0] if len(raw) >= 4 else None
    if vtype == REG_QWORD:
        return struct.unpack("<Q", raw[:8])[0] if len(raw) >= 8 else None
    return bytes(raw)


def checksum(block: Buffer) -> int:
    """XOR checksum of the base block's first 127 dwords (0 -> 1, 0xFFFFFFFF -> 0xFFFFFFFE)."""
    value = 0
    for i in range(0, 0x1FC, 4):
        value ^= _U32.unpack_from(block, i)[0]
    if value == 0xFFFFFFFF:
        return 0xFFFFFFFE
    if value == 0:
        return 1
    return value


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class RegValue:
    """One registry value (``name`` "" is the key's default value)."""

    name: str
    type: int
    raw: bytes

    @property
    def value(self) -> Any:
        return decode_value(self.type, self.raw)

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"type-{self.type}")

    def as_str(self) -> Optional[str]:
        """The value as text when it is a string type (or a number rendered as text)."""
        val = self.value
        if isinstance(val, str):
            return val
        if isinstance(val, int):
            return str(val)
        if isinstance(val, list):
            return "\n".join(val)
        return None

    def as_int(self) -> Optional[int]:
        """The value as an integer (DWORD/QWORD, or a decimal string)."""
        val = self.value
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val.strip(), 0)
            except ValueError:
                return None
        return None


class Key:
    """A registry key.  Children are parsed lazily; loops raise :class:`RegfError`."""

    __slots__ = ("hive", "offset", "name", "path", "_ancestors", "_nsub", "_sublist", "_nval", "_vallist")

    def __init__(self, hive: "Hive", offset: int, *, parent: Optional["Key"] = None) -> None:
        self.hive = hive
        self.offset = offset
        ancestors: FrozenSet[int] = frozenset()
        if parent is not None:
            if offset == parent.offset or offset in parent._ancestors:
                raise RegfError(f"registry key loop at cell 0x{offset:x}")
            ancestors = parent._ancestors | {parent.offset}
            if len(ancestors) > MAX_DEPTH:
                raise RegfError("registry keys nested too deeply")
        self._ancestors = ancestors
        start, length = hive._cell(offset)
        data = hive._data
        if length < 0x4C or bytes(data[start:start + 2]) != b"nk":
            raise RegfError(f"cell 0x{offset:x} is not a key (nk)")
        flags = _U16.unpack_from(data, start + 0x02)[0]
        self._nsub = _U32.unpack_from(data, start + 0x14)[0]
        self._sublist = _U32.unpack_from(data, start + 0x1C)[0]
        self._nval = _U32.unpack_from(data, start + 0x24)[0]
        self._vallist = _U32.unpack_from(data, start + 0x28)[0]
        name_len = _U16.unpack_from(data, start + 0x48)[0]
        if 0x4C + name_len > length:
            raise RegfError(f"key name at cell 0x{offset:x} runs past its cell")
        raw = bytes(data[start + 0x4C:start + 0x4C + name_len])
        self.name = raw.decode("latin-1") if flags & KEY_COMP_NAME else _utf16z(raw)
        if parent is None:
            self.path = ""                      # paths are relative to the hive root
        elif parent.path:
            self.path = f"{parent.path}\\{self.name}"
        else:
            self.path = self.name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Key {self.path or '(root)'} @0x{self.offset:x}>"

    # -- subkeys ------------------------------------------------------------
    def subkeys(self) -> List["Key"]:
        """Direct subkeys (on-disk "stable" ones; volatile keys never reach the file)."""
        if self._nsub == 0 or self._sublist == _NONE:
            return []
        offsets = self.hive._subkey_offsets(self._sublist)
        return [Key(self.hive, off, parent=self) for off in offsets]

    def subkey(self, name: str) -> Optional["Key"]:
        """Case-insensitive direct child lookup."""
        want = name.casefold()
        for child in self.subkeys():
            if child.name.casefold() == want:
                return child
        return None

    def find(self, path: str) -> Optional["Key"]:
        """Resolve a ``\\``- or ``/``-separated path below this key (case-insensitive)."""
        key: Optional[Key] = self
        for part in path.replace("/", "\\").split("\\"):
            if not part:
                continue
            key = key.subkey(part) if key is not None else None
            if key is None:
                return None
        return key

    def walk(self) -> Iterator["Key"]:
        """Depth-first iteration over this key and every descendant (loops raise RegfError)."""
        seen = {self.offset}
        stack = [self]
        while stack:
            key = stack.pop()
            yield key
            children = key.subkeys()
            for child in reversed(children):
                if child.offset in seen:
                    raise RegfError(f"registry key reached twice (cell 0x{child.offset:x})")
                seen.add(child.offset)
                stack.append(child)

    # -- values -------------------------------------------------------------
    def values(self) -> List[RegValue]:
        if self._nval == 0 or self._vallist == _NONE:
            return []
        hive = self.hive
        start, length = hive._cell(self._vallist)
        if self._nval * 4 > length:
            raise RegfError(f"value list of key {self.path!r} runs past its cell")
        out: List[RegValue] = []
        for i in range(self._nval):
            voff = _U32.unpack_from(hive._data, start + 4 * i)[0]
            val = hive._value(voff)
            if val is not None:
                out.append(val)
        return out

    def value(self, name: str) -> Optional[RegValue]:
        """Case-insensitive value lookup (``""`` = default value)."""
        want = name.casefold()
        for val in self.values():
            if val.name.casefold() == want:
                return val
        return None

    def get(self, name: str, default: Any = None) -> Any:
        """Decoded value or *default*."""
        val = self.value(name)
        return default if val is None else val.value

    def get_str(self, name: str, default: Optional[str] = None) -> Optional[str]:
        val = self.value(name)
        if val is None:
            return default
        text = val.as_str()
        return default if text is None else text

    def get_int(self, name: str, default: Optional[int] = None) -> Optional[int]:
        val = self.value(name)
        if val is None:
            return default
        num = val.as_int()
        return default if num is None else num


class Hive:
    """A loaded hive file (read-only).

    Attributes: ``minor`` (3..6), ``seq_primary``/``seq_secondary``, ``checksum_ok``,
    ``dirty`` (checksum wrong or sequence numbers differ), ``truncated`` (the file is shorter than
    its header claims), ``stale`` (dirty or truncated: results may be out of date).
    """

    def __init__(self, data: Buffer, *, name: str = "", _fh: Optional[BinaryIO] = None) -> None:
        self._data = data
        self._fh = _fh
        self.name = name
        size = len(data)
        if size < BASE_BLOCK_SIZE + 32:
            raise RegfError(f"{name or 'hive'}: file too small to be a registry hive")
        if bytes(data[0:4]) != b"regf":
            raise RegfError(f"{name or 'hive'}: not a registry hive (no 'regf' signature)")
        self.seq_primary = _U32.unpack_from(data, 0x04)[0]
        self.seq_secondary = _U32.unpack_from(data, 0x08)[0]
        self.major = _U32.unpack_from(data, 0x14)[0]
        self.minor = _U32.unpack_from(data, 0x18)[0]
        self.file_type = _U32.unpack_from(data, 0x1C)[0]
        self.root_offset = _U32.unpack_from(data, 0x24)[0]
        self.bins_size = _U32.unpack_from(data, 0x28)[0]
        if self.major != 1 or not 3 <= self.minor <= 6:
            raise RegfError(f"{name or 'hive'}: unsupported hive version {self.major}.{self.minor}")
        if self.file_type != 0:
            raise RegfError(f"{name or 'hive'}: this is a registry log file, not a hive")
        self.checksum_ok = checksum(data) == _U32.unpack_from(data, 0x1FC)[0]
        self.dirty = (not self.checksum_ok) or self.seq_primary != self.seq_secondary
        available = size - HBIN_START
        self.truncated = self.bins_size > available
        self._end = HBIN_START + min(self.bins_size, available)
        if bytes(data[HBIN_START:HBIN_START + 4]) != b"hbin":
            raise RegfError(f"{name or 'hive'}: first hive bin is missing")
        self.stale = self.dirty or self.truncated
        self._root: Optional[Key] = None

    # -- construction -------------------------------------------------------
    @classmethod
    def from_bytes(cls, data: Buffer, name: str = "") -> "Hive":
        return cls(data, name=name)

    @classmethod
    def open(cls, path: Union[str, "os.PathLike[str]"], *, max_size: int = MAX_HIVE_SIZE) -> "Hive":
        """Open a hive file read-only through the secrets gate (deny-listed hives raise)."""
        fh = secrets.safe_open(path)
        try:
            size = os.fstat(fh.fileno()).st_size
            name = os.path.basename(os.fspath(path))
            if size > max_size:
                raise RegfError(f"{name}: hive file is unreasonably large ({size} bytes)")
            if size >= _MMAP_THRESHOLD:
                data: Buffer = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                return cls(data, name=name, _fh=fh)
            buf = fh.read(size + 1)
        except BaseException:
            fh.close()
            raise
        fh.close()
        return cls(buf[:max_size], name=os.path.basename(os.fspath(path)))

    def close(self) -> None:
        if isinstance(self._data, mmap.mmap):
            try:
                self._data.close()
            except (BufferError, ValueError):  # pragma: no cover - still referenced
                pass
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Hive":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- navigation ---------------------------------------------------------
    @property
    def root(self) -> Key:
        if self._root is None:
            self._root = Key(self, self.root_offset)
        return self._root

    def find(self, path: str) -> Optional[Key]:
        """Find a key by path from the hive root (the root key's own name is not part of paths)."""
        return self.root.find(path)

    # -- cells --------------------------------------------------------------
    def _cell(self, offset: int) -> Tuple[int, int]:
        """(absolute data start, data length) of the allocated cell at *offset*."""
        if offset == _NONE or offset & 7:
            raise RegfError(f"invalid cell offset 0x{offset:x}")
        pos = HBIN_START + offset
        if pos + 8 > self._end:
            raise RegfError(f"cell offset 0x{offset:x} is outside the hive")
        size = _I32.unpack_from(self._data, pos)[0]
        if size >= 0:
            raise RegfError(f"cell 0x{offset:x} is not in use")
        size = -size
        if size < 8 or pos + size > self._end:
            raise RegfError(f"cell 0x{offset:x} has an impossible size")
        return pos + 4, size - 4

    def _subkey_offsets(self, list_offset: int, _depth: int = 0) -> List[int]:
        start, length = self._cell(list_offset)
        if length < 4:
            raise RegfError(f"subkey list 0x{list_offset:x} is too small")
        data = self._data
        sig = bytes(data[start:start + 2])
        count = _U16.unpack_from(data, start + 2)[0]
        out: List[int] = []
        if sig in (b"lf", b"lh"):
            if 4 + count * 8 > length:
                raise RegfError(f"subkey list 0x{list_offset:x} runs past its cell")
            out = [_U32.unpack_from(data, start + 4 + 8 * i)[0] for i in range(count)]
        elif sig == b"li":
            if 4 + count * 4 > length:
                raise RegfError(f"subkey list 0x{list_offset:x} runs past its cell")
            out = [_U32.unpack_from(data, start + 4 + 4 * i)[0] for i in range(count)]
        elif sig == b"ri":
            if _depth:
                raise RegfError(f"index root 0x{list_offset:x} points at another index root")
            if 4 + count * 4 > length:
                raise RegfError(f"index root 0x{list_offset:x} runs past its cell")
            for i in range(count):
                leaf = _U32.unpack_from(data, start + 4 + 4 * i)[0]
                out.extend(self._subkey_offsets(leaf, _depth + 1))
        else:
            raise RegfError(f"unknown subkey list type {sig!r} at 0x{list_offset:x}")
        seen: set = set()
        unique: List[int] = []
        for off in out:
            if off in seen:
                raise RegfError(f"subkey 0x{off:x} listed twice (damaged hive)")
            seen.add(off)
            unique.append(off)
        return unique

    def _value(self, offset: int) -> Optional[RegValue]:
        start, length = self._cell(offset)
        data = self._data
        if length < 0x14 or bytes(data[start:start + 2]) != b"vk":
            raise RegfError(f"cell 0x{offset:x} is not a value (vk)")
        name_len = _U16.unpack_from(data, start + 0x02)[0]
        size_raw = _U32.unpack_from(data, start + 0x04)[0]
        data_off = _U32.unpack_from(data, start + 0x08)[0]
        vtype = _U32.unpack_from(data, start + 0x0C)[0]
        flags = _U16.unpack_from(data, start + 0x10)[0]
        if flags & VALUE_TOMBSTONE:
            return None
        if 0x14 + name_len > length:
            raise RegfError(f"value name at 0x{offset:x} runs past its cell")
        raw_name = bytes(data[start + 0x14:start + 0x14 + name_len])
        name = raw_name.decode("latin-1") if flags & VALUE_COMP_NAME else _utf16z(raw_name)
        inline = bool(size_raw & 0x80000000)
        size = size_raw & 0x7FFFFFFF
        if inline:
            if size > 4:
                raise RegfError(f"value {name!r}: inline data larger than 4 bytes")
            raw = bytes(data[start + 0x08:start + 0x08 + size])
        elif size == 0:
            raw = b""
        else:
            if size > MAX_VALUE_SIZE:
                raise RegfError(f"value {name!r} is unreasonably large ({size} bytes)")
            dstart, dlen = self._cell(data_off)
            if size > BIG_DATA_SEGMENT and self.minor > 3 and dlen >= 8 and bytes(data[dstart:dstart + 2]) == b"db":
                raw = self._big_data(dstart, dlen, size, name)
            else:
                if size > dlen:
                    raise RegfError(f"value {name!r}: data runs past its cell")
                raw = bytes(data[dstart:dstart + size])
        return RegValue(name, vtype, raw)

    def _big_data(self, start: int, length: int, size: int, name: str) -> bytes:
        data = self._data
        count = _U16.unpack_from(data, start + 2)[0]
        list_off = _U32.unpack_from(data, start + 4)[0]
        if count == 0 or count * BIG_DATA_SEGMENT < size:
            raise RegfError(f"value {name!r}: big-data record has too few segments")
        lstart, llen = self._cell(list_off)
        if count * 4 > llen:
            raise RegfError(f"value {name!r}: big-data segment list runs past its cell")
        parts: List[bytes] = []
        remaining = size
        seen: set = set()
        for i in range(count):
            if remaining <= 0:
                break
            seg = _U32.unpack_from(data, lstart + 4 * i)[0]
            if seg in seen:
                raise RegfError(f"value {name!r}: big-data segment used twice")
            seen.add(seg)
            sstart, slen = self._cell(seg)
            take = min(remaining, BIG_DATA_SEGMENT, slen)
            parts.append(bytes(data[sstart:sstart + take]))
            remaining -= take
        if remaining > 0:
            raise RegfError(f"value {name!r}: big data is truncated")
        return b"".join(parts)

    def describe(self) -> Dict[str, Any]:
        """Header facts (for reports and debugging)."""
        return {"name": self.name, "minor": self.minor, "dirty": self.dirty, "stale": self.stale,
                "checksum_ok": self.checksum_ok, "truncated": self.truncated,
                "sequence": [self.seq_primary, self.seq_secondary]}


def iter_values(key: Key, names: Sequence[str]) -> Dict[str, Any]:
    """Convenience: decoded values for *names* that exist on *key*."""
    out: Dict[str, Any] = {}
    for n in names:
        val = key.value(n)
        if val is not None:
            out[n] = val.value
    return out
