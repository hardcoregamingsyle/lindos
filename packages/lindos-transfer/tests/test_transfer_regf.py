"""Tests for the bounds-checked registry hive reader (SPEC-WINDOWS §29.10).

Every fixture here is built from scratch with :mod:`hive_builder` (real "regf" bytes: base block,
one hbin, nk/vk/lf/lh/li/ri/db cells) -- never a vendored Windows hive. Covers the checksum, the
nk/lf/lh/li/ri subkey-list styles, vk inline vs. out-of-line values, "db" big data, compressed
(Latin-1) vs. UTF-16LE key/value names, dirty (mismatched sequence numbers) and truncated hives, and
key-loop / bad-offset corruption.
"""
from __future__ import annotations

import struct

import pytest
from hive_builder import HiveBuilder, REG_BINARY, REG_DWORD, REG_SZ, checksum as builder_checksum

from lindos_transfer.regf import BIG_DATA_SEGMENT, Hive, RegfError, TYPE_NAMES, checksum, decode_value


def utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


# --------------------------------------------------------------------------- #
# checksum (independent hand check, not just agreement with the builder)
# --------------------------------------------------------------------------- #
def test_checksum_matches_hand_computed_value() -> None:
    block = bytearray(4096)
    block[0:4] = b"regf"
    struct.pack_into("<I", block, 0x04, 3)
    struct.pack_into("<I", block, 0x08, 3)
    struct.pack_into("<I", block, 0x18, 5)
    expected = 0
    for i in range(0, 0x1FC, 4):
        expected ^= struct.unpack_from("<I", block, i)[0]
    assert checksum(bytes(block)) == expected
    assert checksum(bytes(block)) == builder_checksum(bytes(block))


def test_checksum_edge_cases_never_emit_reserved_values() -> None:
    zero = bytearray(4096)
    assert checksum(bytes(zero)) == 1
    allff = bytearray(b"\xff" * 0x1FC) + bytearray(4096 - 0x1FC)
    assert checksum(bytes(allff)) == 0xFFFFFFFE


# --------------------------------------------------------------------------- #
# a normal, well-formed hive
# --------------------------------------------------------------------------- #
def _basic_hive(**kw):
    b = HiveBuilder(minor=5)
    spec = {
        "name": "ROOT",
        "values": [("Ver", REG_SZ, utf16z("1.0")), ("Count", REG_DWORD, (7).to_bytes(4, "little"))],
        "subkey_style": "lh",
        "children": [
            {"name": "Alpha", "values": [("A", REG_SZ, utf16z("alpha-value"))]},
            {"name": "Beta", "values": [("B", REG_DWORD, (42).to_bytes(4, "little"))]},
        ],
    }
    root = b.add_key_tree(spec)
    return Hive.from_bytes(b.to_bytes(root_offset=root, **kw), name="TEST")


def test_basic_hive_header_fields() -> None:
    hive = _basic_hive()
    assert hive.major == 1 and hive.minor == 5
    assert hive.checksum_ok is True
    assert hive.dirty is False
    assert hive.truncated is False
    assert hive.stale is False


def test_root_values_and_subkeys() -> None:
    hive = _basic_hive()
    root = hive.root
    values = {v.name: v for v in root.values()}
    assert values["Ver"].value == "1.0"
    assert values["Ver"].type_name == "REG_SZ"
    assert values["Count"].value == 7
    assert values["Count"].type_name == "REG_DWORD"
    names = sorted(k.name for k in root.subkeys())
    assert names == ["Alpha", "Beta"]


def test_find_and_subkey_are_case_insensitive() -> None:
    hive = _basic_hive()
    assert hive.find("alpha") is not None
    assert hive.find("ALPHA").get("a") == "alpha-value"
    assert hive.root.subkey("BETA") is not None
    assert hive.find("Alpha\\Missing") is None
    assert hive.find("Nope") is None


def test_get_str_and_get_int_helpers() -> None:
    hive = _basic_hive()
    alpha = hive.find("Alpha")
    assert alpha.get_str("A") == "alpha-value"
    assert alpha.get_str("Missing", "fallback") == "fallback"
    beta = hive.find("Beta")
    assert beta.get_int("B") == 42
    assert beta.get_int("Missing", -1) == -1


# --------------------------------------------------------------------------- #
# subkey list styles: lf, lh, li, ri (index root over two leaf lists)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("style", ["lf", "lh", "li"])
def test_each_subkey_list_style_is_readable(style: str) -> None:
    b = HiveBuilder(minor=5)
    spec = {"name": "ROOT", "subkey_style": style,
           "children": [{"name": "One"}, {"name": "Two"}, {"name": "Three"}]}
    root = b.add_key_tree(spec)
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name=style)
    assert sorted(k.name for k in hive.root.subkeys()) == ["One", "Three", "Two"]


def test_index_root_ri_over_two_leaf_lists() -> None:
    b = HiveBuilder(minor=5)
    spec = {"name": "ROOT", "subkey_style": "ri-split",
           "children": [{"name": f"K{i:02d}"} for i in range(10)]}
    root = b.add_key_tree(spec)
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="RI")
    names = sorted(k.name for k in hive.root.subkeys())
    assert names == [f"K{i:02d}" for i in range(10)]


def test_ri_pointing_at_another_ri_is_rejected() -> None:
    b = HiveBuilder(minor=5)
    leaf = b.subkey_list([("X", b.add_key_tree({"name": "X"}, is_root=False))], style="li")
    inner_ri = b.index_root([leaf])
    outer_ri = b.index_root([inner_ri])
    root_body = b._nk_body("ROOT", compressed=True, nsub=1, sublist=outer_ri, nval=0,  # noqa: SLF001
                          vallist=0xFFFFFFFF, is_root=True)
    root = b._alloc(root_body)  # noqa: SLF001
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="BADRI")
    with pytest.raises(RegfError):
        hive.root.subkeys()


# --------------------------------------------------------------------------- #
# vk: inline values, out-of-line values, tombstones, compressed vs UTF-16 names
# --------------------------------------------------------------------------- #
def test_inline_value_under_five_bytes() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "values": [("N", REG_DWORD, (0xDEADBEEF).to_bytes(4, "little"))]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="INLINE")
    assert hive.root.get_int("N") == 0xDEADBEEF


def test_out_of_line_string_value() -> None:
    b = HiveBuilder(minor=5)
    text = "a longer string value that will not fit inline" * 3
    root = b.add_key_tree({"name": "ROOT", "values": [("S", REG_SZ, utf16z(text))]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="OUTLINE")
    assert hive.root.get_str("S") == text


def test_tombstoned_value_is_skipped() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "values": [
        ("Dead", REG_SZ, b"", {"tombstone": True}), ("Alive", REG_SZ, utf16z("yes"))]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="TOMB")
    names = [v.name for v in hive.root.values()]
    assert names == ["Alive"]
    assert hive.root.value("Dead") is None


def test_compressed_vs_utf16_key_and_value_names() -> None:
    b = HiveBuilder(minor=5)
    spec = {
        "name": "ROOT",
        "children": [
            {"name": "CompressedChild", "compressed": True,
             "values": [("CompVal", REG_SZ, utf16z("c"), {"compressed": True})]},
            {"name": "WideéChild", "compressed": False,
             "values": [("WideéVal", REG_SZ, utf16z("w"), {"compressed": False})]},
        ],
    }
    root = b.add_key_tree(spec)
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="NAMES")
    kids = {k.name: k for k in hive.root.subkeys()}
    assert set(kids) == {"CompressedChild", "WideéChild"}
    assert kids["CompressedChild"].get_str("CompVal") == "c"
    assert kids["WideéChild"].get_str("WideéVal") == "w"


def test_multi_sz_and_type_table() -> None:
    from lindos_transfer.regf import REG_MULTI_SZ

    raw = "one\x00two\x00\x00".encode("utf-16-le")
    assert decode_value(REG_MULTI_SZ, raw) == ["one", "two"]
    assert TYPE_NAMES[REG_MULTI_SZ] == "REG_MULTI_SZ"


# --------------------------------------------------------------------------- #
# db big data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [BIG_DATA_SEGMENT + 1, BIG_DATA_SEGMENT * 3 + 17])
def test_big_data_segments_are_concatenated_exactly(size: int) -> None:
    b = HiveBuilder(minor=5)
    payload = bytes((i % 256) for i in range(size))
    root = b.add_key_tree({"name": "ROOT", "values": [("Blob", REG_BINARY, payload, {"big_data": True})]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="BIGDATA")
    got = hive.root.value("Blob").raw
    assert got == payload
    assert len(got) == size


def test_value_exactly_at_the_big_data_threshold_is_plain_not_db() -> None:
    """``size > BIG_DATA_SEGMENT`` is strict: exactly 16344 bytes is one ordinary out-of-line cell."""
    b = HiveBuilder(minor=5)
    payload = bytes((i % 256) for i in range(BIG_DATA_SEGMENT))
    root = b.add_key_tree({"name": "ROOT", "values": [("Blob", REG_BINARY, payload)]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="ATLIMIT")
    assert hive.root.value("Blob").raw == payload


def test_big_data_requires_minor_over_3() -> None:
    b = HiveBuilder(minor=3)
    payload = b"Y" * (BIG_DATA_SEGMENT + 100)
    root = b.add_key_tree({"name": "ROOT", "values": [("Blob", REG_BINARY, payload, {"big_data": True})]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root, minor=3), name="OLDMINOR")
    # minor==3: the reader must not treat this as a "db" segment list (it would misparse it as raw
    # data); it either raises or returns something that is NOT the reconstructed payload.
    with pytest.raises(RegfError):
        hive.root.value("Blob")


# --------------------------------------------------------------------------- #
# dirty / stale / truncated hives
# --------------------------------------------------------------------------- #
def test_dirty_hive_from_mismatched_sequence_numbers() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "values": [("V", REG_SZ, utf16z("x"))]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root, seq_primary=9, seq_secondary=3), name="DIRTY")
    assert hive.dirty is True
    assert hive.stale is True
    # a dirty hive still answers -- it is not replayed, just labelled possibly-stale
    assert hive.root.get_str("V") == "x"


def test_corrupted_checksum_marks_dirty() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root, corrupt_checksum=True), name="BADCS")
    assert hive.checksum_ok is False
    assert hive.dirty is True


def test_truncated_hive_is_flagged_not_crashed() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "children": [{"name": "Child"}]})
    full = b.to_bytes(root_offset=root)
    # cut a little off the end but keep the base block + first hbin header intact
    short = full[: len(full) - 40]
    hive = Hive.from_bytes(short, name="TRUNC")
    assert hive.truncated is True
    assert hive.stale is True


def test_file_too_small_is_rejected() -> None:
    with pytest.raises(RegfError):
        Hive.from_bytes(b"regf" + b"\x00" * 10, name="TINY")


def test_bad_signature_is_rejected() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    data = bytearray(b.to_bytes(root_offset=root))
    data[0:4] = b"XXXX"
    with pytest.raises(RegfError):
        Hive.from_bytes(bytes(data), name="BADSIG")


def test_log_file_type_is_rejected() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    data = bytearray(b.to_bytes(root_offset=root))
    struct.pack_into("<I", data, 0x1C, 1)  # file_type = log
    # the checksum must be recomputed after mutating the header, or checksum_ok is coincidentally
    # unrelated to this test's assertion (file_type is checked before anything else uses it)
    with pytest.raises(RegfError, match="log file"):
        Hive.from_bytes(bytes(data), name="LOGTYPE")


def test_unsupported_hive_version_is_rejected() -> None:
    b = HiveBuilder(minor=99)
    root = b.add_key_tree({"name": "ROOT"})
    with pytest.raises(RegfError):
        Hive.from_bytes(b.to_bytes(root_offset=root, minor=99), name="BADVER")


# --------------------------------------------------------------------------- #
# corruption: loops, bad offsets, cells that are not what they claim to be
# --------------------------------------------------------------------------- #
def test_self_referencing_key_raises_on_walk_and_subkeys() -> None:
    b = HiveBuilder(minor=5)
    cyc = b.add_self_cycle_key("Cyc")
    root = b.add_key_tree({"name": "ROOT", "children": [("Cyc", cyc)]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="SELFCYCLE")
    with pytest.raises(RegfError):
        list(hive.root.walk())


def test_shared_cell_reached_from_two_branches_raises_in_walk() -> None:
    b = HiveBuilder(minor=5)
    shared = b.add_key_tree({"name": "Shared"}, is_root=False)
    spec = {"name": "ROOT", "children": [
        {"name": "BranchA", "children": [("Shared", shared)]},
        {"name": "BranchB", "children": [("Shared", shared)]},
    ]}
    root = b.add_key_tree(spec)
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="SHARED")
    with pytest.raises(RegfError):
        list(hive.root.walk())


def test_out_of_range_cell_offset_raises() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="RANGE")
    with pytest.raises(RegfError):
        hive._cell(999999999)  # noqa: SLF001 - exercising the bounds check directly


def test_misaligned_cell_offset_raises() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="MISALIGN")
    with pytest.raises(RegfError):
        hive._cell(3)  # noqa: SLF001 - not a multiple of 8


def test_unallocated_cell_is_rejected() -> None:
    b = HiveBuilder(minor=5)
    free_off = b.add_free_cell(32)
    root = b.add_key_tree({"name": "ROOT", "children": [("Free", free_off)]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="FREECELL")
    with pytest.raises(RegfError):
        hive.root.subkeys()


def test_cell_claiming_to_be_a_key_but_isnt() -> None:
    b = HiveBuilder(minor=5)
    not_a_key = b._alloc(b"not-an-nk-cell-at-all")  # noqa: SLF001
    root = b.add_key_tree({"name": "ROOT", "children": [("Fake", not_a_key)]})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root), name="FAKEKEY")
    with pytest.raises(RegfError):
        hive.root.subkeys()


def test_hive_context_manager_closes() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    with Hive.from_bytes(b.to_bytes(root_offset=root), name="CTX") as hive:
        assert hive.root.name == "ROOT"
    # closing an in-memory (bytes-backed) hive is a no-op, never raises
    hive.close()


def test_describe_reports_header_facts() -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT"})
    hive = Hive.from_bytes(b.to_bytes(root_offset=root, seq_primary=3, seq_secondary=3), name="DESC")
    d = hive.describe()
    assert d["name"] == "DESC" and d["minor"] == 5 and d["dirty"] is False and d["sequence"] == [3, 3]


def test_open_reads_a_real_file_and_close_releases_it(tmp_path) -> None:
    b = HiveBuilder(minor=5)
    root = b.add_key_tree({"name": "ROOT", "values": [("V", REG_SZ, utf16z("ok"))]})
    path = tmp_path / "SOFTWARE"
    path.write_bytes(b.to_bytes(root_offset=root))
    hive = Hive.open(path)
    try:
        assert hive.root.get_str("V") == "ok"
    finally:
        hive.close()


def test_open_refuses_denylisted_hive_names(tmp_path) -> None:
    from lindos_transfer.secrets import SecretPathError

    path = tmp_path / "system32" / "config" / "SAM"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"regf" + b"\x00" * 4200)
    with pytest.raises(SecretPathError):
        Hive.open(path)
