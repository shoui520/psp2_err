from tools.build_database import parse_wikitext_rows


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
