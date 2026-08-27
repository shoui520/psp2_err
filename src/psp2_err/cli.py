from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence, TextIO

from . import __version__
from .database import Database, ErrorRecord, load_database, parse_numeric_query


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psp2_err",
        description="Look up PS Vita error codes without an internet connection.",
        epilog=(
            "Examples: psp2_err C2-2000-2 0x80100600; "
            "psp2_err -s 'savedata slot'"
        ),
    )
    parser.add_argument(
        "queries",
        nargs="*",
        metavar="ERROR",
        help="display code, hexadecimal/decimal value, or SCE_* name",
    )
    parser.add_argument(
        "-s",
        "--search",
        metavar="TEXT",
        help="search identifiers and community remarks",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=_nonnegative_int,
        default=25,
        help="maximum search results (0 means unlimited; default: 25)",
    )
    parser.add_argument("-j", "--json", action="store_true", help="emit JSON")
    parser.add_argument("--stats", action="store_true", help="show database statistics")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.queries and args.search is None and not args.stats:
        parser.print_help()
        return 0
    if args.search is not None and args.queries:
        parser.error("ERROR values cannot be combined with --search")
    if args.stats and (args.queries or args.search is not None):
        parser.error("--stats cannot be combined with lookups or --search")

    database = load_database()
    if args.stats:
        _print_stats(database, args.json, sys.stdout)
        return 0

    if args.search is not None:
        matches = database.search(args.search, args.limit)
        if args.json:
            json.dump(
                {
                    "search": args.search,
                    "limit": args.limit,
                    "matches": [record.to_dict() for record in matches],
                },
                sys.stdout,
                indent=2,
                ensure_ascii=False,
            )
            sys.stdout.write("\n")
        else:
            _print_search(args.search, matches, args.limit, sys.stdout)
        return 0 if matches else 1

    lookups = []
    any_missing = False
    for query in args.queries:
        numeric = parse_numeric_query(query)
        matches = database.lookup(query)
        decoded = database.decode(numeric) if numeric is not None else None
        lookups.append((query, matches, decoded))
        if not matches:
            any_missing = True

    if args.json:
        json.dump(
            [
                {
                    "query": query,
                    "matches": [record.to_dict() for record in matches],
                    "decoded": decoded,
                }
                for query, matches, decoded in lookups
            ],
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    else:
        for index, (query, matches, decoded) in enumerate(lookups):
            if index:
                sys.stdout.write("\n")
            _print_lookup(query, matches, decoded, sys.stdout)
    return 1 if any_missing else 0


def _print_lookup(
    query: str,
    matches: Sequence[ErrorRecord],
    decoded: Optional[dict[str, Any]],
    stream: TextIO,
) -> None:
    stream.write(f"# {query}\n")
    if decoded:
        stream.write(f"  Hex:      {decoded['hex']}\n")
        stream.write(f"  Signed:   {decoded['signed']}\n")
        kind = "error" if decoded["error"] else "success/non-error"
        if decoded["critical"]:
            kind = f"critical {kind}"
        facility = decoded["facility_name"] or "unknown"
        stream.write(
            f"  Fields:   {kind}; facility {decoded['facility_hex']} "
            f"({facility}); code 0x{decoded['code']:04x}\n"
        )
    if not matches:
        stream.write("  No exact match found.\n")
        if decoded and decoded["patterns"]:
            stream.write("  Matching ranges:\n")
            for pattern in decoded["patterns"]:
                label = pattern.get("name") or pattern["pattern"]
                detail = f" — {pattern['remark']}" if pattern.get("remark") else ""
                stream.write(f"    {pattern['pattern']}: {label}{detail}\n")
        return

    for record_number, record in enumerate(matches):
        if record_number:
            stream.write("  ---\n")
        if record.hex and not decoded:
            value = int(record.hex, 16)
            signed = value if value < 0x80000000 else value - 0x100000000
            stream.write(f"  Hex:      {record.hex}\n")
            stream.write(f"  Signed:   {signed}\n")
        if record.codes:
            stream.write(f"  Display:  {', '.join(record.codes)}\n")
        if record.names:
            stream.write(f"  Name:     {', '.join(record.names)}\n")
        for remark in record.remarks:
            stream.write(f"  Remark:   {remark.text} [{remark.source}]\n")
        for conflict in record.conflicts:
            stream.write(
                f"  Conflict: {conflict.source} lists {conflict.field} "
                f"{conflict.value}; it belongs to {conflict.conflicts_with}\n"
            )
        stream.write(f"  Sources:  {', '.join(record.sources)}\n")


def _print_search(
    query: str,
    matches: Sequence[ErrorRecord],
    limit: int,
    stream: TextIO,
) -> None:
    stream.write(f"# search: {query}\n")
    if not matches:
        stream.write("No matches found.\n")
        return
    for record in matches:
        fields = [record.hex or "----------"]
        fields.append(record.codes[0] if record.codes else "-")
        fields.append(record.names[0] if record.names else "-")
        line = "  ".join(fields)
        if record.remarks:
            line += f" — {record.remarks[0].text}"
        stream.write(line + "\n")
    if limit:
        stream.write(f"# showing at most {limit} matches; use -n 0 for all\n")


def _print_stats(database: Database, as_json: bool, stream: TextIO) -> None:
    stats = database.stats()
    if as_json:
        json.dump(stats, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        return
    stream.write("psp2_err database\n")
    for key in (
        "records",
        "with_hex",
        "with_display_code",
        "with_remarks",
        "patterns",
        "facilities",
        "facility_ranges",
    ):
        stream.write(f"  {key.replace('_', ' ').title()}: {stats[key]}\n")
    stream.write(f"  Generated: {stats['generated_at']}\n")
    stream.write("  Sources:\n")
    for source in stats["sources"]:
        stream.write(f"    {source['id']}: {source['url']}\n")


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
