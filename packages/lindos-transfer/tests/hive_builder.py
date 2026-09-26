"""A minimal, from-scratch Windows registry hive ("regf") builder, for tests only.

Produces real, spec-shaped bytes (base block, one ``hbin``, ``nk``/``vk``/``lf``/``lh``/``li``/``ri``/
``db`` cells) so :mod:`lindos_transfer.regf` and :mod:`lindos_transfer.winreg` can be exercised against
byte-accurate fixtures instead of mocks. Never vendors a real Windows hive.

Usage::

    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "values": [("Ver", REG_SZ, "1".encode("utf-16-le"))],
                           "children": [{"name": "Sub", "values": [...]}]})
    data = b.to_bytes(root_offset=root)
    hive = Hive.from_bytes(data, name="TEST")
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

BASE_BLOCK_SIZE = 4096
HBIN_START = 0x1000
BIG_DATA_SEGMENT = 16344

REG_SZ = 1
REG_BINARY = 3
REG_DWORD = 4

ChildSpec = Union[Dict[str, Any], Tuple[str, int]]


def checksum(block: bytes) -> int:
    """Same rule as :func:`lindos_transfer.regf.checksum` (XOR of the first 127 dwords), reimplemented
    independently here so a bug in the real module can never make this builder agree with it by
    construction."""
    value = 0
    for i in range(0, 0x1FC, 4):
        value ^= struct.unpack_from("<I", block, i)[0]
    if value == 0xFFFFFFFF:
        return 0xFFFFFFFE
    if value == 0:
        return 1
    return value


class HiveBuilder:
    """Bump-allocates hive-bin cells into one in-memory buffer; ``to_bytes()`` renders the file."""

    def __init__(self, *, minor: int = 5) -> None:
        self.minor = minor
        # The first 32 bytes of hive-bins data are the (only) hbin header (sig, bin offset, bin
        # size, 2 reserved dwords, a FILETIME and a spare dword); cell offsets are relative to
        # HBIN_START, so the very first cell naturally lands right after it. The bin-size field is
        # filled in by to_bytes() once the final buffer length is known.
        self.buf = bytearray(b"hbin" + struct.pack("<I", 0) + struct.pack("<I", 0) + b"\x00" * 20)
        assert len(self.buf) == 32

    # -- low-level cell allocation -------------------------------------------------------------
    def _alloc(self, body: bytes) -> int:
        """Append one allocated cell (``-size`` + *body*, 8-byte aligned); return its offset."""
        offset = len(self.buf)
        total = 4 + len(body)
        aligned = (total + 7) & ~7
        pad = aligned - total
        self.buf += struct.pack("<i", -aligned) + bytes(body) + b"\x00" * pad
        return offset

    def add_free_cell(self, size: int) -> int:
        """An *unallocated* cell (positive size): exercises the "not in use" RegfError path."""
        size = max(8, (size + 7) & ~7)
        offset = len(self.buf)
        self.buf += struct.pack("<i", size) + b"\x00" * (size - 4)
        return offset

    def patch_u32(self, offset: int, value: int) -> None:
        """Overwrite a previously written little-endian u32 at absolute buffer *offset*."""
        struct.pack_into("<I", self.buf, offset, value)

    # -- values ---------------------------------------------------------------------------------
    def _add_big_data(self, raw: bytes) -> Tuple[int, int]:
        chunks = [raw[i:i + BIG_DATA_SEGMENT] for i in range(0, len(raw), BIG_DATA_SEGMENT)] or [b""]
        seg_offsets = [self._alloc(chunk) for chunk in chunks]
        seglist_off = self._alloc(b"".join(struct.pack("<I", o) for o in seg_offsets))
        db_off = self._alloc(b"db" + struct.pack("<H", len(seg_offsets)) + struct.pack("<I", seglist_off))
        return db_off, len(raw)

    def add_value(self, name: str, vtype: int, raw: bytes, *, compressed: bool = True,
                 big_data: bool = False, tombstone: bool = False) -> int:
        """Build one ``vk`` cell (inline for <= 4 bytes, a plain data cell otherwise, or ``db`` big
        data when *big_data*); return its offset."""
        name_bytes = name.encode("latin-1") if compressed else name.encode("utf-16-le")
        flags = (0x0001 if compressed else 0) | (0x0002 if tombstone else 0)
        if tombstone:
            vtype, size_field, off_bytes = 0, 0, b"\xff\xff\xff\xff"
        elif big_data:
            off, size = self._add_big_data(raw)
            size_field, off_bytes = size, struct.pack("<I", off)
        elif len(raw) <= 4:
            size_field = 0x80000000 | len(raw)
            off_bytes = raw.ljust(4, b"\x00")[:4]
        else:
            off = self._alloc(raw)
            size_field, off_bytes = len(raw), struct.pack("<I", off)
        body = (b"vk" + struct.pack("<H", len(name_bytes)) + struct.pack("<I", size_field) + off_bytes
               + struct.pack("<I", vtype) + struct.pack("<H", flags) + struct.pack("<H", 0) + name_bytes)
        return self._alloc(body)

    # -- subkey lists -----------------------------------------------------------------------------
    @staticmethod
    def _lh_hash(name: str) -> int:
        h = 0
        for ch in name.upper():
            h = (37 * h + ord(ch)) & 0xFFFFFFFF
        return h

    def subkey_list(self, entries: Sequence[Tuple[str, int]], *, style: str = "lh") -> int:
        """A ``li``/``lf``/``lh`` cell listing *entries* (``[(name, nk_offset), ...]``)."""
        count = len(entries)
        if style == "li":
            body = b"li" + struct.pack("<H", count) + b"".join(struct.pack("<I", off) for _n, off in entries)
        elif style in ("lf", "lh"):
            pieces = [style.encode("ascii") + struct.pack("<H", count)]
            for name, off in entries:
                if style == "lf":
                    hint = name.encode("latin-1", "replace")[:4].ljust(4, b"\x00")
                else:
                    hint = struct.pack("<I", self._lh_hash(name))
                pieces.append(struct.pack("<I", off) + hint)
            body = b"".join(pieces)
        else:
            raise ValueError(f"unknown subkey list style {style!r}")
        return self._alloc(body)

    def index_root(self, list_offsets: Sequence[int]) -> int:
        """An ``ri`` cell pointing at several ``lf``/``lh``/``li`` lists."""
        body = b"ri" + struct.pack("<H", len(list_offsets)) + b"".join(struct.pack("<I", o) for o in list_offsets)
        return self._alloc(body)

    # -- keys -------------------------------------------------------------------------------------
    def _nk_body(self, name: str, *, compressed: bool, nsub: int, sublist: int, nval: int,
                vallist: int, is_root: bool) -> bytes:
        name_bytes = name.encode("latin-1") if compressed else name.encode("utf-16-le")
        flags = (0x0020 if compressed else 0) | (0x0004 if is_root else 0)
        return (b"nk" + struct.pack("<H", flags) + b"\x00" * 8
               + struct.pack("<I", 0)                       # access bits / spare
               + struct.pack("<I", 0xFFFFFFFF)               # parent offset (never read back)
               + struct.pack("<I", nsub) + struct.pack("<I", 0)
               + struct.pack("<I", sublist) + struct.pack("<I", 0xFFFFFFFF)
               + struct.pack("<I", nval) + struct.pack("<I", vallist)
               + struct.pack("<I", 0xFFFFFFFF) + struct.pack("<I", 0xFFFFFFFF)
               + struct.pack("<I", 0) * 4
               + struct.pack("<I", 0)
               + struct.pack("<H", len(name_bytes)) + struct.pack("<H", 0)
               + name_bytes)

    def add_key_tree(self, spec: Dict[str, Any], *, is_root: bool = True) -> int:
        """Build a key and (recursively) its children/values from a nested dict spec; returns its
        ``nk`` cell offset.

        ``spec``: ``name`` (str), ``values`` (list of ``(name, type, raw[, opts-dict])``),
        ``children`` (list of nested specs, or ``(name, prebuilt_nk_offset)`` tuples to splice in an
        already-built key -- used for cycle/shared-cell fixtures), ``compressed`` (bool, default
        True), ``subkey_style`` (``"lf"``/``"lh"``/``"li"``/``"ri-split"``, default ``"lh"``).
        """
        child_entries: List[Tuple[str, int]] = []
        for child in spec.get("children", []):
            if isinstance(child, tuple):
                child_entries.append(child)
            else:
                child_entries.append((child["name"], self.add_key_tree(child, is_root=False)))
        nsub = len(child_entries)
        sublist = 0xFFFFFFFF
        if nsub:
            style = spec.get("subkey_style", "lh")
            if style == "ri-split" and nsub > 1:
                half = (nsub + 1) // 2
                l1 = self.subkey_list(child_entries[:half], style="lf")
                l2 = self.subkey_list(child_entries[half:], style="lh")
                sublist = self.index_root([l1, l2])
            else:
                sublist = self.subkey_list(child_entries, style="li" if style == "ri-split" else style)
        vk_offsets = []
        for entry in spec.get("values", []):
            vname, vtype, vraw = entry[0], entry[1], entry[2]
            opts = entry[3] if len(entry) > 3 else {}
            vk_offsets.append(self.add_value(vname, vtype, vraw, **opts))
        nval = len(vk_offsets)
        vallist = 0xFFFFFFFF
        if nval:
            vallist = self._alloc(b"".join(struct.pack("<I", o) for o in vk_offsets))
        body = self._nk_body(spec["name"], compressed=spec.get("compressed", True), nsub=nsub,
                             sublist=sublist, nval=nval, vallist=vallist, is_root=is_root)
        return self._alloc(body)

    def add_self_cycle_key(self, name: str = "CycleKey") -> int:
        """A key that lists *itself* as its only subkey -- the direct ancestor-cycle fixture."""
        placeholder = self.subkey_list([(name, 0)], style="li")
        nk_off = self._alloc(self._nk_body(name, compressed=True, nsub=1, sublist=placeholder,
                                           nval=0, vallist=0xFFFFFFFF, is_root=False))
        # The li cell body is [sig(2) count(2) offset(4)]; the offset sits 4 bytes into the cell
        # data, which itself starts 4 bytes after the cell's own size-prefix.
        self.patch_u32(placeholder + 4 + 4, nk_off)
        return nk_off

    # -- rendering --------------------------------------------------------------------------------
    def to_bytes(self, *, root_offset: int, seq_primary: int = 1, seq_secondary: int = 1,
                minor: Optional[int] = None, truncate_by: int = 0, corrupt_checksum: bool = False) -> bytes:
        """Render the full hive file. ``truncate_by`` chops bytes off the end (truncated-hive
        fixture); ``corrupt_checksum`` flips a checksum bit (dirty-hive fixture)."""
        minor = self.minor if minor is None else minor
        struct.pack_into("<I", self.buf, 0x08, len(self.buf))   # hbin's own "bin size" field
        base = bytearray(BASE_BLOCK_SIZE)
        base[0:4] = b"regf"
        struct.pack_into("<I", base, 0x04, seq_primary)
        struct.pack_into("<I", base, 0x08, seq_secondary)
        struct.pack_into("<I", base, 0x14, 1)
        struct.pack_into("<I", base, 0x18, minor)
        struct.pack_into("<I", base, 0x1C, 0)
        struct.pack_into("<I", base, 0x20, 1)
        struct.pack_into("<I", base, 0x24, root_offset)
        struct.pack_into("<I", base, 0x28, len(self.buf))
        struct.pack_into("<I", base, 0x2C, 1)
        cs = checksum(bytes(base))
        if corrupt_checksum:
            cs ^= 0x1
        struct.pack_into("<I", base, 0x1FC, cs)
        out = bytes(base) + bytes(self.buf)
        if truncate_by:
            out = out[: max(0, len(out) - truncate_by)]
        return out
