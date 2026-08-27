from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable, Mapping, Optional, Tuple


_HEX_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{1,8})$")
_DISPLAY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,3}-[A-Za-z0-9?-]+$")


@dataclass(frozen=True)
class Remark:
    text: str
    source: str


@dataclass(frozen=True)
class Conflict:
    field: str
    value: str
    source: str
    conflicts_with: str


@dataclass(frozen=True)
class ErrorRecord:
    hex: Optional[str]
    codes: Tuple[str, ...]
    names: Tuple[str, ...]
    remarks: Tuple[Remark, ...]
    conflicts: Tuple[Conflict, ...]
    sources: Tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ErrorRecord":
        return cls(
            hex=value.get("hex"),
            codes=tuple(value.get("codes", ())),
            names=tuple(value.get("names", ())),
            remarks=tuple(Remark(**item) for item in value.get("remarks", ())),
            conflicts=tuple(Conflict(**item) for item in value.get("conflicts", ())),
            sources=tuple(value.get("sources", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hex": self.hex,
            "codes": list(self.codes),
            "names": list(self.names),
            "remarks": [remark.__dict__ for remark in self.remarks],
            "conflicts": [conflict.__dict__ for conflict in self.conflicts],
            "sources": list(self.sources),
        }

    def searchable_text(self) -> str:
        pieces: Iterable[str] = (
            *((self.hex,) if self.hex else ()),
            *self.codes,
            *self.names,
            *(remark.text for remark in self.remarks),
            *(conflict.value for conflict in self.conflicts),
            *(conflict.conflicts_with for conflict in self.conflicts),
        )
        return "\n".join(pieces).casefold()


class Database:
    def __init__(self, payload: Mapping[str, Any]):
        self.schema_version = int(payload["schema_version"])
        self.generated_at = str(payload["generated_at"])
        self.sources = tuple(payload["sources"])
        self.facilities = dict(payload.get("facilities", {}))
        self.facility_ranges = tuple(payload.get("facility_ranges", ()))
        self.patterns = tuple(payload.get("patterns", ()))
        self.records = tuple(ErrorRecord.from_dict(item) for item in payload["records"])
        self._exact: dict[str, list[ErrorRecord]] = {}
        for record in self.records:
            keys = [
                *record.codes,
                *record.names,
                *(conflict.value for conflict in record.conflicts),
            ]
            if record.hex:
                keys.append(record.hex)
            for key in keys:
                normalized = _normalize_text_key(key)
                bucket = self._exact.setdefault(normalized, [])
                if record not in bucket:
                    bucket.append(record)

    def lookup(self, query: str) -> Tuple[ErrorRecord, ...]:
        """Return records that exactly match a display code, value, or symbol."""
        text_key = _normalize_text_key(query)
        matches = list(self._exact.get(text_key, ()))
        numeric = parse_numeric_query(query)
        if numeric is not None:
            hex_key = f"0x{numeric:08x}"
            for record in self._exact.get(hex_key, ()):
                if record not in matches:
                    matches.append(record)
        matches.sort(key=lambda record: _exact_match_priority(record, text_key))
        return tuple(matches)

    def search(self, text: str, limit: int = 25) -> Tuple[ErrorRecord, ...]:
        """Case-insensitive substring search over all identifiers and remarks."""
        terms = [term.casefold() for term in text.split() if term]
        if not terms:
            return ()
        found = [
            record
            for record in self.records
            if all(term in record.searchable_text() for term in terms)
        ]
        found.sort(key=_record_sort_key)
        return tuple(found if limit == 0 else found[:limit])

    def decode(self, value: int) -> dict[str, Any]:
        value &= 0xFFFFFFFF
        facility_number = (value >> 16) & 0xFFF
        facility_key = f"0x{facility_number:03x}"
        facility_name = self.facilities.get(facility_key)
        if facility_name is None:
            for facility_range in self.facility_ranges:
                if int(facility_range["start"], 16) <= facility_number <= int(
                    facility_range["end"], 16
                ):
                    facility_name = facility_range["name"]
                    break
        return {
            "hex": f"0x{value:08x}",
            "signed": value if value < 0x80000000 else value - 0x100000000,
            "error": bool(value & 0x80000000),
            "critical": bool(value & 0x40000000),
            "facility": facility_number,
            "facility_hex": facility_key,
            "facility_name": facility_name,
            "code": value & 0xFFFF,
            "patterns": list(self.matching_patterns(value)),
        }

    def matching_patterns(self, value: int) -> Tuple[Mapping[str, Any], ...]:
        value &= 0xFFFFFFFF
        return tuple(
            pattern
            for pattern in self.patterns
            if value & int(pattern["mask"], 16) == int(pattern["value"], 16)
        )

    def stats(self) -> dict[str, Any]:
        return {
            "records": len(self.records),
            "with_hex": sum(record.hex is not None for record in self.records),
            "with_display_code": sum(bool(record.codes) for record in self.records),
            "with_remarks": sum(bool(record.remarks) for record in self.records),
            "patterns": len(self.patterns),
            "facilities": len(self.facilities),
            "facility_ranges": len(self.facility_ranges),
            "generated_at": self.generated_at,
            "sources": list(self.sources),
        }


def parse_numeric_query(query: str) -> Optional[int]:
    """Parse the numeric forms commonly copied from debuggers and logs."""
    value = query.strip().replace("_", "")
    if not value:
        return None

    if value.startswith(("-0x", "-0X")):
        try:
            return (-int(value[3:], 16)) & 0xFFFFFFFF
        except ValueError:
            return None
    if value.startswith("-") and value[1:].isdigit():
        try:
            return int(value, 10) & 0xFFFFFFFF
        except ValueError:
            return None
    if value.startswith(("0x", "0X")):
        match = _HEX_RE.fullmatch(value)
        return int(match.group(1), 16) if match else None
    if any(character in "abcdefABCDEF" for character in value):
        match = _HEX_RE.fullmatch(value)
        return int(match.group(1), 16) if match else None
    if not value.isdigit():
        return None

    # Vita values are commonly pasted as eight bare hexadecimal digits.
    # Longer values are unambiguously unsigned decimal; short values are decimal.
    base = 16 if len(value) == 8 and value[0] in "89" else 10
    number = int(value, base)
    return number if 0 <= number <= 0xFFFFFFFF else None


def _normalize_text_key(value: str) -> str:
    text = value.strip()
    if _DISPLAY_RE.fullmatch(text):
        return text.upper()
    return text.casefold()


def _record_sort_key(record: ErrorRecord) -> tuple[Any, ...]:
    return (
        record.hex is None,
        int(record.hex, 16) if record.hex else 0,
        record.codes[0] if record.codes else "",
        record.names[0] if record.names else "",
    )


def _exact_match_priority(record: ErrorRecord, query_key: str) -> int:
    identifiers = [*record.codes, *record.names]
    if record.hex:
        identifiers.append(record.hex)
    return 0 if any(_normalize_text_key(value) == query_key for value in identifiers) else 1


@lru_cache(maxsize=1)
def load_database() -> Database:
    data_path = resources.files("psp2_err.data").joinpath("errors.json")
    with data_path.open("r", encoding="utf-8") as stream:
        return Database(json.load(stream))
