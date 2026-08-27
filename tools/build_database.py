#!/usr/bin/env python3
"""Build the embedded psp2_err database.

The installed CLI never uses the network. This maintainer tool reads Sony's SDK
CSV and obtains wiki source through the MediaWiki API. PSDevWiki currently puts
its API behind a browser challenge, so a documented Common Crawl snapshot plus
the small current-page supplement is used when that API returns HTTP 403.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SDK_CSV = Path(
    "/mnt/c/PATH/sony-psp2sdk/sdk/SDK Software/PSVITA/sdk/host_tools/"
    "debugging/error_code/error_table.csv"
)
DEFAULT_OUTPUT = ROOT / "src/psp2_err/data/errors.json"
SUPPLEMENT = ROOT / "tools/psdevwiki_supplement.csv"

HENKAKU_API = "https://wiki.henkaku.xyz/api.php"
HENKAKU_PAGE = "Error_codes"
PSDEVWIKI_API = "https://www.psdevwiki.com/vita/api.php"
PSDEVWIKI_PAGE = "Error_Codes"

# PSDevWiki revision 7551, last modified 2023-09-06. This is only used when
# Cloudflare blocks its MediaWiki API. The current-page deltas we rely on are
# checked into psdevwiki_supplement.csv.
CC_RANGE = (1067262900, 1067294726)
CC_URL = (
    "https://data.commoncrawl.org/crawl-data/CC-MAIN-2023-50/segments/"
    "1700679100232.63/warc/"
    "CC-MAIN-20231130193829-20231130223829-00377.warc.gz"
)

_EXACT_HEX_RE = re.compile(r"^0x([0-9a-fA-F]{8})$")
_PATTERN_HEX_RE = re.compile(r"^0x([0-9a-fA-FxX]{8})$")
_DISPLAY_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{0,3}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")
_FACILITY_RE = re.compile(r"^\s*-\s+0x([0-9A-Fa-f]{3})\s*=\s*([A-Za-z0-9_]+)", re.MULTILINE)
_FACILITY_RANGE_RE = re.compile(
    r"^\s*-\s+0x([0-9A-Fa-f]{3})-0x([0-9A-Fa-f]{3})\s*=\s*([^\r\n]+)",
    re.MULTILINE,
)
_LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_REF_RE = re.compile(r"<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


@dataclass(frozen=True)
class RawRow:
    source: str
    name: str = ""
    hex: str = ""
    code: str = ""
    remark: str = ""


@dataclass
class MutableRecord:
    hex: Optional[str] = None
    codes: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    remarks: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def fetch_mediawiki_source(api: str, page: str) -> tuple[str, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "revisions",
            "titles": page,
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "format": "json",
            "formatversion": "2",
        }
    )
    request = urllib.request.Request(
        f"{api}?{query}",
        headers={"User-Agent": "psp2_err/0.1 database builder"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    page_value = payload["query"]["pages"][0]
    revision = page_value["revisions"][0]
    source = revision["slots"]["main"]["content"]
    return source, {"revision": revision["revid"], "timestamp": revision["timestamp"]}


def parse_wikitext_rows(source: str, source_id: str) -> tuple[list[RawRow], list[dict[str, str]]]:
    rows: list[RawRow] = []
    patterns: list[dict[str, str]] = []
    for cells in _iter_wikitable_rows(source):
        if len(cells) < 4 or cells[0].casefold() == "name":
            continue
        name, hex_value, code, remark = cells[:4]
        pattern = _make_pattern(hex_value, name, remark, source_id)
        if pattern:
            patterns.append(pattern)
            continue
        rows.append(RawRow(source_id, name, hex_value, code, remark))
    return rows, patterns


def _iter_wikitable_rows(source: str) -> Iterator[list[str]]:
    in_table = False
    cells: list[str] = []

    def finish() -> Optional[list[str]]:
        nonlocal cells
        result = [_clean_wikitext(cell) for cell in cells]
        cells = []
        return result if result else None

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if line.startswith("{|"):
            in_table = True
            cells = []
        elif in_table and line.startswith("|-"):
            row = finish()
            if row:
                yield row
        elif in_table and line.startswith("|}"):
            row = finish()
            if row:
                yield row
            in_table = False
        elif in_table and line.startswith("!"):
            cells.extend(line[1:].split("!!"))
        elif in_table and line.startswith("|"):
            cells.extend(line[1:].split("||"))
        elif in_table and cells and line:
            cells[-1] += " " + line


def parse_sdk_csv(path: Path) -> list[RawRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream, skipinitialspace=True))
    malformed = [row for row in rows if len(row) != 3]
    if malformed:
        raise ValueError(f"{path}: found {len(malformed)} rows that do not have 3 columns")
    return [RawRow("sony-sdk", name=row[2], hex=row[1], code=row[0]) for row in rows]


class WikiTableParser(HTMLParser):
    def __init__(self, source_id: str):
        super().__init__(convert_charrefs=True)
        self.source_id = source_id
        self.table_depth = 0
        self.row: Optional[list[str]] = None
        self.cell: Optional[list[str]] = None
        self.rows: list[RawRow] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "table":
            self.table_depth += 1
        elif self.table_depth and tag == "tr":
            self.row = []
        elif self.row is not None and tag in {"td", "th"}:
            self.cell = []
        elif self.cell is not None and tag == "br":
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.cell is not None and tag in {"td", "th"}:
            assert self.row is not None
            self.row.append(_clean_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.row) >= 4 and [part.casefold() for part in self.row[:4]] != [
                "name",
                "hex",
                "error code",
                "remarks",
            ]:
                self.rows.append(RawRow(self.source_id, *self.row[:4]))
            self.row = None
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1


def parse_html_rows(source: str, source_id: str) -> list[RawRow]:
    parser = WikiTableParser(source_id)
    parser.feed(source)
    return parser.rows


def fetch_psdevwiki_archive() -> str:
    start, end = CC_RANGE
    request = urllib.request.Request(
        CC_URL,
        headers={"Range": f"bytes={start}-{end}", "User-Agent": "psp2_err/0.1 database builder"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        compressed = response.read()
    warc = gzip.decompress(compressed)
    pieces = warc.split(b"\r\n\r\n", 2)
    if len(pieces) != 3 or b"<!DOCTYPE html" not in pieces[2][:100]:
        raise RuntimeError("unexpected Common Crawl WARC response")
    return pieces[2].rstrip(b"\r\n").decode("utf-8")


def load_supplement(path: Path) -> list[RawRow]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return [
            RawRow(
                "psdevwiki",
                name=row["name"],
                hex=row["hex"],
                code=row["code"],
                remark=row["remark"],
            )
            for row in reader
        ]


class Merger:
    def __init__(self) -> None:
        self.records: list[MutableRecord] = []
        self.hex_index: dict[str, MutableRecord] = {}
        self.code_index: dict[str, MutableRecord] = {}
        self.name_index: dict[str, MutableRecord] = {}

    def add(self, row: RawRow) -> None:
        hex_value = _normalize_exact_hex(row.hex)
        codes = _extract_display_codes(row.code)
        name = _normalize_name(row.name)
        hex_candidate = self.hex_index.get(hex_value) if hex_value else None
        code_candidates = _unique_records(
            self.code_index[code.casefold()]
            for code in codes
            if code.casefold() in self.code_index
        )

        if hex_candidate:
            target = hex_candidate
        elif hex_value:
            compatible = [candidate for candidate in code_candidates if candidate.hex is None]
            target = compatible[0] if compatible else MutableRecord()
            if not compatible:
                self.records.append(target)
        elif code_candidates:
            target = code_candidates[0]
        elif name and name.casefold() in self.name_index:
            target = self.name_index[name.casefold()]
        else:
            target = MutableRecord()
            self.records.append(target)

        # A no-value community row and a later exact row can safely be joined.
        # Conflicting non-null values are kept separate; wiki tables contain a
        # few known display-code typos and must not collapse two SDK records.
        for candidate in code_candidates:
            if candidate is not target and (candidate.hex is None or target.hex is None):
                self._combine(target, candidate)

        if hex_value and target.hex is None:
            target.hex = hex_value
        for code in codes:
            owner = self.code_index.get(code.casefold())
            if owner is None or owner is target:
                _append_unique(target.codes, code)
            else:
                conflict = {
                    "field": "display code",
                    "value": code,
                    "source": row.source,
                    "conflicts_with": owner.hex or ", ".join(owner.names) or "another record",
                }
                if conflict not in target.conflicts:
                    target.conflicts.append(conflict)
        if name:
            _append_unique(target.names, name)
        remark = _clean_text(row.remark)
        if remark and remark not in {"-", "?", "Unknown."}:
            value = {"text": remark, "source": row.source}
            if value not in target.remarks:
                target.remarks.append(value)
        _append_unique(target.sources, row.source)
        self._reindex(target)

    def _combine(self, target: MutableRecord, duplicate: MutableRecord) -> None:
        if target.hex is None:
            target.hex = duplicate.hex
        for value in duplicate.codes:
            _append_unique(target.codes, value)
        for value in duplicate.names:
            _append_unique(target.names, value)
        for value in duplicate.remarks:
            if value not in target.remarks:
                target.remarks.append(value)
        for value in duplicate.conflicts:
            if value not in target.conflicts:
                target.conflicts.append(value)
        for value in duplicate.sources:
            _append_unique(target.sources, value)
        self.records = [record for record in self.records if record is not duplicate]
        self._rebuild_indexes()

    def _reindex(self, record: MutableRecord) -> None:
        if record.hex:
            self.hex_index[record.hex] = record
        for code in record.codes:
            self.code_index[code.casefold()] = record
        for name in record.names:
            self.name_index.setdefault(name.casefold(), record)

    def _rebuild_indexes(self) -> None:
        self.hex_index.clear()
        self.code_index.clear()
        self.name_index.clear()
        for record in self.records:
            self._reindex(record)

    def serialize(self) -> list[dict[str, Any]]:
        useful = [record for record in self.records if record.hex or record.codes or record.names]
        useful.sort(
            key=lambda record: (
                record.hex is None,
                int(record.hex, 16) if record.hex else 0,
                record.codes[0] if record.codes else "",
                record.names[0] if record.names else "",
            )
        )
        return [
            {
                "hex": record.hex,
                "codes": sorted(record.codes, key=str.casefold),
                "names": sorted(record.names, key=str.casefold),
                "remarks": record.remarks,
                "conflicts": record.conflicts,
                "sources": record.sources,
            }
            for record in useful
        ]


def build(args: argparse.Namespace) -> dict[str, Any]:
    sdk_rows = parse_sdk_csv(args.sdk_csv)
    henkaku_source, henkaku_info = fetch_mediawiki_source(HENKAKU_API, HENKAKU_PAGE)
    henkaku_rows, patterns = parse_wikitext_rows(henkaku_source, "henkaku")
    facilities = {
        f"0x{match.group(1).lower()}": match.group(2)
        for match in _FACILITY_RE.finditer(henkaku_source)
    }
    facility_ranges = [
        {
            "start": f"0x{match.group(1).lower()}",
            "end": f"0x{match.group(2).lower()}",
            "name": _clean_text(match.group(3)),
            "source": "henkaku",
        }
        for match in _FACILITY_RANGE_RE.finditer(henkaku_source)
    ]

    psdev_method = "mediawiki-api"
    psdev_info: dict[str, Any]
    supplements: list[RawRow] = []
    try:
        psdev_source, psdev_info = fetch_mediawiki_source(PSDEVWIKI_API, PSDEVWIKI_PAGE)
        psdev_rows, psdev_patterns = parse_wikitext_rows(psdev_source, "psdevwiki")
        patterns.extend(psdev_patterns)
    except Exception as error:
        if args.no_archive_fallback:
            raise
        print(f"warning: PSDevWiki API unavailable ({error}); using archived snapshot", file=sys.stderr)
        psdev_rows = parse_html_rows(fetch_psdevwiki_archive(), "psdevwiki")
        psdev_method = "common-crawl-revision-7551-plus-supplement"
        psdev_info = {
            "revision": 7551,
            "timestamp": "2023-09-06T00:23:04Z",
            "supplement_revision": 8855,
            "supplement_timestamp": "2026-06-28T18:29:00Z",
            "supplement_retrieval": "user-provided-current-wikitext-delta",
        }
        supplements = load_supplement(args.supplement)
        replacement_codes = {
            code.casefold()
            for row in supplements
            for code in _extract_display_codes(row.code)
        }
        psdev_rows = [
            row
            for row in psdev_rows
            if not replacement_codes.intersection(
                code.casefold() for code in _extract_display_codes(row.code)
            )
        ]
        psdev_rows.extend(supplements)

    merger = Merger()
    for row in (*sdk_rows, *psdev_rows, *henkaku_rows):
        merger.add(row)

    records = merger.serialize()
    _validate_sdk_rows(records, sdk_rows)
    _validate_merged_rows(records, supplements, "PSDevWiki supplement")
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sources": [
            {
                "id": "sony-sdk",
                "url": str(args.sdk_csv),
                "rows": len(sdk_rows),
            },
            {
                "id": "henkaku",
                "url": f"{HENKAKU_API}?action=query&prop=revisions&titles={HENKAKU_PAGE}",
                "rows": len(henkaku_rows),
                "patterns": len(patterns),
                **henkaku_info,
                "retrieval": "mediawiki-api",
            },
            {
                "id": "psdevwiki",
                "url": f"{PSDEVWIKI_API}?action=query&prop=revisions&titles={PSDEVWIKI_PAGE}",
                "rows": len(psdev_rows),
                **psdev_info,
                "retrieval": psdev_method,
            },
        ],
        "facilities": dict(sorted(facilities.items())),
        "facility_ranges": facility_ranges,
        "patterns": _deduplicate_dicts(patterns),
        "records": records,
    }


def _clean_wikitext(value: str) -> str:
    value = _REF_RE.sub("", value)
    value = _BR_RE.sub(" ", value)
    previous = None
    while previous != value:
        previous = value
        value = _LINK_RE.sub(r"\1", value)
    value = re.sub(r"'{2,5}", "", value)
    return _clean_text(html.unescape(value))


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _normalize_exact_hex(value: str) -> Optional[str]:
    match = _EXACT_HEX_RE.fullmatch(value.strip())
    return f"0x{match.group(1).lower()}" if match else None


def _normalize_name(value: str) -> Optional[str]:
    name = _clean_text(value)
    return None if not name or name.casefold() in {"-", "?", "unknown"} else name


def _extract_display_codes(value: str) -> list[str]:
    if not value or value.strip() in {"-", "?", "unknown"}:
        return []
    return [match.group(0).upper() for match in _DISPLAY_RE.finditer(value)]


def _make_pattern(hex_value: str, name: str, remark: str, source: str) -> Optional[dict[str, str]]:
    match = _PATTERN_HEX_RE.fullmatch(hex_value.strip())
    if not match or "x" not in match.group(1).casefold():
        return None
    digits = match.group(1).lower()
    mask_digits = "".join("0" if digit == "x" else "f" for digit in digits)
    value_digits = "".join("0" if digit == "x" else digit for digit in digits)
    normalized_name = _normalize_name(name) or ""
    normalized_remark = _clean_text(remark)
    return {
        "pattern": "0x" + digits.upper(),
        "mask": "0x" + mask_digits,
        "value": "0x" + value_digits,
        "name": normalized_name,
        "remark": "" if normalized_remark == "-" else normalized_remark,
        "source": source,
    }


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _unique_records(values: Iterable[MutableRecord]) -> list[MutableRecord]:
    result: list[MutableRecord] = []
    for value in values:
        if all(value is not existing for existing in result):
            result.append(value)
    return result


def _deduplicate_dicts(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _validate_sdk_rows(records: list[dict[str, Any]], sdk_rows: list[RawRow]) -> None:
    by_code = {
        code.casefold(): record
        for record in records
        for code in record["codes"]
    }
    failures = []
    for row in sdk_rows:
        record = by_code.get(row.code.casefold())
        if (
            record is None
            or record["hex"] != row.hex.casefold()
            or row.name not in record["names"]
        ):
            failures.append(row)
    if failures:
        sample = ", ".join(row.code for row in failures[:5])
        raise RuntimeError(f"merge lost or changed {len(failures)} SDK rows: {sample}")


def _validate_merged_rows(
    records: list[dict[str, Any]], rows: list[RawRow], label: str
) -> None:
    failures: list[RawRow] = []
    for row in rows:
        expected_hex = _normalize_exact_hex(row.hex)
        expected_name = _normalize_name(row.name)
        expected_remark = _clean_text(row.remark)
        for code in _extract_display_codes(row.code):
            candidates = [
                record
                for record in records
                if code in record["codes"]
                or any(conflict["value"] == code for conflict in record["conflicts"])
            ]
            if not any(
                (expected_hex is None or record["hex"] == expected_hex)
                and (expected_name is None or expected_name in record["names"])
                and (
                    not expected_remark
                    or {"text": expected_remark, "source": row.source} in record["remarks"]
                )
                for record in candidates
            ):
                failures.append(row)
                break
    if failures:
        sample = ", ".join(row.code for row in failures[:5])
        raise RuntimeError(f"merge lost or changed {len(failures)} {label} rows: {sample}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-csv", type=Path, default=DEFAULT_SDK_CSV)
    parser.add_argument("--supplement", type=Path, default=SUPPLEMENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-archive-fallback", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    payload = build(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
    print(
        f"wrote {len(payload['records'])} records, {len(payload['patterns'])} patterns "
        f"to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
