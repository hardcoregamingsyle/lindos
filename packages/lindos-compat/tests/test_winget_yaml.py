"""lindos_compat.wingetyaml: the stdlib YAML subset parser and the python3-yaml (libyaml) path
(SPEC-WINDOWS §28.10). Both paths must keep every scalar a string and refuse the same things
winget itself never emits (anchors, aliases, tags, complex keys, duplicate keys, nested flow
collections). Fixtures are lines lifted from real winget manifests, per the winget research
digest's yaml.subset.repo / yaml.subset.cdn notes."""
from __future__ import annotations

from typing import Any, Callable

import pytest

from lindos_compat import wingetyaml
from lindos_compat.wingetyaml import YamlError

# --------------------------------------------------------------------------- #
# run every test against both the stdlib parser and (when installed) the
# python3-yaml path, since both must return identical, all-string shapes.
# --------------------------------------------------------------------------- #


def _stdlib(text: str) -> Any:
    return wingetyaml.load(text, use_libyaml=False)


def _libyaml(text: str) -> Any:
    if not wingetyaml.libyaml_available():
        pytest.skip("python3-yaml is not installed")
    return wingetyaml.load(text, use_libyaml=True)


LOADERS = [pytest.param(_stdlib, id="stdlib"), pytest.param(_libyaml, id="libyaml")]


@pytest.fixture(params=LOADERS)
def load(request: pytest.FixtureRequest) -> Callable[[str], Any]:
    return request.param


# --------------------------------------------------------------------------- #
# scalars stay strings (the whole point of BaseLoader / the stdlib parser)
# --------------------------------------------------------------------------- #
def test_scalars_never_coerce_type(load: Callable[[str], Any]) -> None:
    doc = load(
        "PackageVersion: 1.10\n"
        "ReleaseDate: 2024-11-12\n"
        "Quoted: \"001\"\n"
        "Flag: true\n"
        "Negative: -1073720687\n"
        "Moniker: 26.03\n"
    )
    assert doc == {
        "PackageVersion": "1.10", "ReleaseDate": "2024-11-12", "Quoted": "001",
        "Flag": "true", "Negative": "-1073720687", "Moniker": "26.03",
    }
    for value in doc.values():
        assert isinstance(value, str)


def test_plain_scalar_and_comment(load: Callable[[str], Any]) -> None:
    doc = load("PackageIdentifier: Mozilla.Firefox  # yaml-language-server: $schema=...\n")
    assert doc == {"PackageIdentifier": "Mozilla.Firefox"}


def test_full_line_comment_is_ignored(load: Callable[[str], Any]) -> None:
    doc = load("# Created with YamlCreate.ps1\nPackageIdentifier: Mozilla.Firefox\n")
    assert doc == {"PackageIdentifier": "Mozilla.Firefox"}


# --------------------------------------------------------------------------- #
# block mappings / sequences, both sequence indent styles
# --------------------------------------------------------------------------- #
def test_block_mapping_and_sequence(load: Callable[[str], Any]) -> None:
    doc = load(
        "PackageIdentifier: Microsoft.Edge\n"
        "Installers:\n"
        "- Architecture: x64\n"
        "  InstallerUrl: https://example.test/a.exe\n"
        "- Architecture: x86\n"
        "  InstallerUrl: https://example.test/b.exe\n"
    )
    assert doc["PackageIdentifier"] == "Microsoft.Edge"
    assert doc["Installers"] == [
        {"Architecture": "x64", "InstallerUrl": "https://example.test/a.exe"},
        {"Architecture": "x86", "InstallerUrl": "https://example.test/b.exe"},
    ]


def test_sequence_indented_under_its_key(load: Callable[[str], Any]) -> None:
    doc = load("Platform:\n  - Windows.Desktop\n  - Windows.Universal\n")
    assert doc == {"Platform": ["Windows.Desktop", "Windows.Universal"]}


def test_nested_list_of_maps(load: Callable[[str], Any]) -> None:
    doc = load(
        "Installers:\n"
        "- Architecture: x64\n"
        "  AppsAndFeaturesEntries:\n"
        "  - DisplayName: PowerToys\n"
        "    ProductCode: '{GUID}'\n"
    )
    assert doc["Installers"][0]["AppsAndFeaturesEntries"] == [
        {"DisplayName": "PowerToys", "ProductCode": "{GUID}"}
    ]


# --------------------------------------------------------------------------- #
# quoting
# --------------------------------------------------------------------------- #
def test_single_quote_escape(load: Callable[[str], Any]) -> None:
    doc = load("InstallLocation: '%ProgramFiles%\\Notepad++'\n")
    assert doc == {"InstallLocation": "%ProgramFiles%\\Notepad++"}
    doc = load("ProductCode: 'it''s quoted'\n")
    assert doc == {"ProductCode": "it's quoted"}


def test_double_quote_escapes(load: Callable[[str], Any]) -> None:
    doc = load(r'Description: "line1\nline2\ttabbed"' + "\n")
    assert doc == {"Description": "line1\nline2\ttabbed"}


def test_double_quote_hex_unicode_escapes(load: Callable[[str], Any]) -> None:
    doc = load(r'Notes: "\x41é\U0001F41B"' + "\n")
    assert doc == {"Notes": "Aé\U0001F41B"}


def test_double_quoted_multiline_rp_path(load: Callable[[str], Any]) -> None:
    # from the research digest: rP is double-quoted with \xNN escapes for non-ASCII path segments
    doc = load('rP: "manifests/b/BR\\xD6TJE/ProfiTool/2026.9.90/c81b"\n')
    assert doc == {"rP": "manifests/b/BRÖTJE/ProfiTool/2026.9.90/c81b"}


# --------------------------------------------------------------------------- #
# folding (plain and quoted multi-line scalars)
# --------------------------------------------------------------------------- #
def test_plain_scalar_folds_across_lines(load: Callable[[str], Any]) -> None:
    doc = load("Description: >\n  first line\n  second line\n")
    assert doc == {"Description": "first line second line\n"}


def test_double_quoted_folds_across_lines(load: Callable[[str], Any]) -> None:
    doc = load('Description: "first line\n  second line"\n')
    assert doc == {"Description": "first line second line"}


# --------------------------------------------------------------------------- #
# block scalars: | (literal) and > (folded), with chomping
# --------------------------------------------------------------------------- #
def test_literal_block_scalar_keeps_newlines(load: Callable[[str], Any]) -> None:
    doc = load("ReleaseNotes: |\n  line one\n  line two\n")
    assert doc == {"ReleaseNotes": "line one\nline two\n"}


def test_literal_block_scalar_strip_chomp(load: Callable[[str], Any]) -> None:
    doc = load("ReleaseNotes: |-\n  line one\n  line two\n")
    assert doc == {"ReleaseNotes": "line one\nline two"}


def test_folded_block_scalar_joins_lines(load: Callable[[str], Any]) -> None:
    doc = load("Description: >-\n  first\n  second\n\n  third\n")
    assert doc == {"Description": "first second\nthird"}


def test_block_scalar_content_can_look_like_structure(load: Callable[[str], Any]) -> None:
    doc = load("ReleaseNotes: |-\n  ---\n  # not a comment\n  a: b\n")
    assert doc == {"ReleaseNotes": "---\n# not a comment\na: b"}


# --------------------------------------------------------------------------- #
# empty flow sequences / maps (seen in real repo manifests)
# --------------------------------------------------------------------------- #
def test_empty_flow_sequence(load: Callable[[str], Any]) -> None:
    doc = load("PackageDependencies: []\nWindowsFeatures: []\n")
    assert doc == {"PackageDependencies": [], "WindowsFeatures": []}


def test_flow_sequence_of_scalars(load: Callable[[str], Any]) -> None:
    doc = load("UnsupportedOSArchitectures: [arm, arm64]\n")
    assert doc == {"UnsupportedOSArchitectures": ["arm", "arm64"]}


def test_empty_flow_mapping(load: Callable[[str], Any]) -> None:
    doc = load("Extra: {}\n")
    assert doc == {"Extra": {}}


# --------------------------------------------------------------------------- #
# CRLF / BOM tolerance (real repo files use CRLF; CDN files always do)
# --------------------------------------------------------------------------- #
def test_crlf_line_endings(load: Callable[[str], Any]) -> None:
    doc = load("PackageIdentifier: Microsoft.Edge\r\nPackageVersion: 1.0\r\n")
    assert doc == {"PackageIdentifier": "Microsoft.Edge", "PackageVersion": "1.0"}


def test_utf8_bom_is_stripped(load: Callable[[str], Any]) -> None:
    doc = load("﻿PackageIdentifier: Microsoft.Edge\n")
    assert doc == {"PackageIdentifier": "Microsoft.Edge"}


def test_document_markers(load: Callable[[str], Any]) -> None:
    doc = load("---\nPackageIdentifier: Microsoft.Edge\n...\n")
    assert doc == {"PackageIdentifier": "Microsoft.Edge"}


def test_empty_document_is_none(load: Callable[[str], Any]) -> None:
    assert load("") is None
    assert load("# just a comment\n") is None


# --------------------------------------------------------------------------- #
# refusals -- both loaders must reject the same things
# --------------------------------------------------------------------------- #
def test_anchors_refused(load: Callable[[str], Any]) -> None:
    with pytest.raises(YamlError):
        load("Foo: &anchor bar\nBaz: *anchor\n")


def test_tags_refused(load: Callable[[str], Any]) -> None:
    with pytest.raises(YamlError):
        load("Foo: !!str bar\n")


def test_duplicate_keys_refused(load: Callable[[str], Any]) -> None:
    with pytest.raises(YamlError):
        load("Foo: bar\nFoo: baz\n")


def test_nested_flow_collections_refused_stdlib() -> None:
    # the stdlib parser only supports one-line flow collections of *scalars* (winget's actual
    # usage); nesting is refused outright. Real winget manifests never nest them (research digest
    # yaml.subset.repo/.cdn), so this is a parser-subset limitation, not a universal refusal.
    with pytest.raises(YamlError):
        wingetyaml.parse("Foo: [[1, 2], 3]\n")


def test_explicit_key_syntax_refused_stdlib() -> None:
    # the stdlib parser refuses '? ' explicit-key syntax outright, whether or not the key turns
    # out to be a simple scalar.
    with pytest.raises(YamlError):
        wingetyaml.parse("? complex\n: value\n")


def test_truly_complex_key_refused(load: Callable[[str], Any]) -> None:
    # a genuinely non-scalar (sequence) mapping key: refused by both loaders.
    with pytest.raises(YamlError):
        load("? [a, b]\n: value\n")


# The stdlib parser also refuses tabs-for-indentation and multi-document streams;
# libyaml's own error messages differ but both must raise YamlError.
def test_tab_indentation_refused_stdlib() -> None:
    with pytest.raises(YamlError):
        wingetyaml.parse("Foo:\n\tBar: baz\n")


def test_multi_document_refused_stdlib() -> None:
    with pytest.raises(YamlError):
        wingetyaml.parse("Foo: bar\n---\nBaz: qux\n")


def test_mapping_value_inside_plain_scalar_refused_stdlib() -> None:
    with pytest.raises(YamlError):
        wingetyaml.parse("Description: a: b\n")


def test_oversized_document_refused_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wingetyaml, "MAX_BYTES", 16)
    with pytest.raises(YamlError):
        wingetyaml.parse("PackageIdentifier: " + "x" * 100 + "\n")


def test_non_printable_character_refused_stdlib() -> None:
    with pytest.raises(YamlError):
        wingetyaml.parse("Foo: bar\x01baz\n")


# --------------------------------------------------------------------------- #
# load(): dispatch and forcing the stdlib fallback
# --------------------------------------------------------------------------- #
def test_load_uses_libyaml_when_available_and_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    if not wingetyaml.libyaml_available():
        pytest.skip("python3-yaml is not installed")
    calls = []
    real = wingetyaml._load_with_libyaml

    def spy(yaml_mod: Any, text: str) -> Any:
        calls.append(text)
        return real(yaml_mod, text)

    monkeypatch.setattr(wingetyaml, "_load_with_libyaml", spy)
    doc = wingetyaml.load("PackageIdentifier: Mozilla.Firefox\n")
    assert doc == {"PackageIdentifier": "Mozilla.Firefox"}
    assert calls


def test_load_falls_back_when_yaml_module_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wingetyaml, "_yaml_module", lambda: None)
    doc = wingetyaml.load("PackageIdentifier: Mozilla.Firefox\n")
    assert doc == {"PackageIdentifier": "Mozilla.Firefox"}


def test_load_accepts_bytes() -> None:
    doc = wingetyaml.load(b"PackageIdentifier: Mozilla.Firefox\n", use_libyaml=False)
    assert doc == {"PackageIdentifier": "Mozilla.Firefox"}


def test_load_rejects_non_utf8_bytes() -> None:
    with pytest.raises(YamlError):
        wingetyaml.load(b"\xff\xfe\x00\x01", use_libyaml=False)


# --------------------------------------------------------------------------- #
# a realistic merged-manifest-shaped fragment (root -> Installers, mixed types)
# --------------------------------------------------------------------------- #
def test_realistic_fragment(load: Callable[[str], Any]) -> None:
    text = (
        "PackageIdentifier: Microsoft.PowerToys\n"
        "PackageVersion: 0.101.2362.0\n"
        "InstallerType: burn\n"
        "InstallerSwitches:\n"
        "  Silent: /quiet\n"
        "  Custom: /norestart\n"
        "Installers:\n"
        "- Architecture: x64\n"
        "  Scope: user\n"
        "  InstallerUrl: https://example.test/PowerToysSetup-x64.exe\n"
        "  InstallerSha256: \"AABBCCDDEEFF00112233445566778899AABBCCDDEEFF00112233445566778899\"\n"
        "- Architecture: x64\n"
        "  Scope: machine\n"
        "  InstallerUrl: https://example.test/PowerToysSetup-x64-machine.exe\n"
        "  InstallerSha256: \"00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEE\"\n"
    )
    doc = load(text)
    assert doc["InstallerType"] == "burn"
    assert doc["InstallerSwitches"] == {"Silent": "/quiet", "Custom": "/norestart"}
    assert len(doc["Installers"]) == 2
    assert doc["Installers"][0]["Scope"] == "user"
    assert doc["Installers"][1]["Scope"] == "machine"
