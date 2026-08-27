from psp2_err.database import load_database, parse_numeric_query


def test_database_contains_all_three_sources():
    database = load_database()
    assert database.schema_version == 2
    source_ids = {source["id"] for source in database.sources}
    assert source_ids == {"sony-sdk", "henkaku", "psdevwiki"}
    assert len(database.records) >= 2500
    assert len(database.facilities) >= 90
    assert len(database.facility_ranges) == 4
    assert len(database.patterns) >= 70


def test_lookup_by_all_common_identifier_forms():
    database = load_database()
    expected_hex = "0x80100600"
    for query in (
        "C2-2000-2",
        "c2-2000-2",
        "0x80100600",
        "80100600",
        "-2146433536",
        "2148533760",
        "SCE_APPUTIL_ERROR_PARAMETER",
        "sce_apputil_error_parameter",
    ):
        matches = database.lookup(query)
        assert matches
        assert matches[0].hex == expected_hex


def test_community_only_display_code_is_embedded():
    match = load_database().lookup("C2-14391-8")[0]
    assert match.hex is None
    assert any("NPXS10072" in remark.text for remark in match.remarks)
    assert "psdevwiki" in match.sources


def test_henkaku_exact_code_and_range_are_embedded():
    database = load_database()
    exact = database.lookup("0x800f0516")[0]
    assert "SCE_SBL_ERROR_AM_EINVAL" in exact.names
    assert "henkaku" in exact.sources

    decoded = database.decode(0x80107E42)
    assert decoded["facility_name"] == "SCE_ERROR_FACILITY_VSH"
    assert any(pattern["pattern"] == "0x80107EXX" for pattern in decoded["patterns"])

    reserved = database.decode(0x80190001)
    assert reserved["facility_name"] == "reserved for updater"


def test_multiline_psdevwiki_rows_and_current_remarks_are_embedded():
    database = load_database()
    registry = database.lookup("C1-3473-7")[0]
    assert registry.hex == "0x800d000e"
    assert registry.names == ("SCE_ERROR_FACILITY_REGISTRY_KERNEL",)

    np_facility = database.lookup("NP-9974-9")[0]
    assert np_facility.hex == "0x80558350"
    assert np_facility.names == ("SCE_ERROR_FACILITY_NP",)

    near = database.lookup("C2-13700-1")[0]
    assert "Mr.Gas" in near.remarks[0].text


def test_conflicting_wiki_alias_is_retained_without_overwriting_sdk_mapping():
    matches = load_database().lookup("NP-6162-5")
    assert matches[0].hex == "0x80551605"
    conflict_record = next(match for match in matches if match.hex == "0x80551604")
    assert conflict_record.conflicts[0].source == "psdevwiki"
    assert conflict_record.conflicts[0].value == "NP-6162-5"


def test_search_uses_names_and_remarks():
    matches = load_database().search("CMA data verify", limit=0)
    assert {code for match in matches for code in match.codes} >= {
        "C2-12858-4",
        "C2-112745",
    }


def test_numeric_parser():
    assert parse_numeric_query("0x80010002") == 0x80010002
    assert parse_numeric_query("80010002") == 0x80010002
    assert parse_numeric_query("2147549186") == 0x80010002
    assert parse_numeric_query("-2147418110") == 0x80010002
    assert parse_numeric_query("not-a-code") is None
