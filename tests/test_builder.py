import pytest

from tools.build_database import (
    Merger, RawRow, add_firmware_mappings, merge_firmware_into_payload, parse_wikitext_rows,
)


def test_firmware_merge_enriches_display_only_and_keeps_existing_metadata(tmp_path):
    merger = Merger()
    merger.add(RawRow("wiki", code="C2-12828-1", remark="Application error"))
    merger.add(RawRow("sdk", name="EXISTING_NAME", hex="0x80010001", code="C1-2737-9"))
    payload = {"records": merger.serialize(), "sources": [], "schema_version": 2}
    mapping = tmp_path / "mappings.csv"
    mapping.write_text("hex,code\n0x80103909,C2-12828-1\n0x80412190,NW-2035-0\n0x80010001,C1-2737-9\n")
    result = merge_firmware_into_payload(payload, mapping)
    records = {r["hex"]: r for r in result["records"]}
    assert len(records) == 3
    assert records["0x80103909"]["remarks"] == [{"source": "wiki", "text": "Application error"}]
    assert records["0x80010001"]["names"] == ["EXISTING_NAME"]
    assert records["0x80412190"]["codes"] == ["NW-2035-0"]
    assert payload["records"][1]["hex"] is None
    assert merge_firmware_into_payload(result, mapping) == result


def test_firmware_merge_rejects_conflicting_existing_mapping():
    merger = Merger()
    merger.add(RawRow("sdk", hex="0x80010001", code="C1-2737-9"))
    with pytest.raises(ValueError, match="conflicts"):
        add_firmware_mappings(merger, [RawRow("firmware-mappings", hex="0x80010002", code="C1-2737-9")])


def test_wikitext_parser_accepts_inline_and_multiline_rows():
    source = """
{| class="wikitable"
|-
! Name !! Hex !! Error code !! Remarks
|-
| INLINE_NAME || 0x80000001 || C1-1-1 || Inline remark
|-
|MULTILINE_NAME
|0x80000002
|C1-2-2
|Line one<br>line two
|-
| - || 0x801001XX || - || -
|}
"""
    rows, patterns = parse_wikitext_rows(source, "test")
    assert [(row.name, row.hex, row.code, row.remark) for row in rows] == [
        ("INLINE_NAME", "0x80000001", "C1-1-1", "Inline remark"),
        ("MULTILINE_NAME", "0x80000002", "C1-2-2", "Line one line two"),
    ]
    assert patterns[0]["pattern"] == "0x801001XX"
    assert patterns[0]["remark"] == ""
