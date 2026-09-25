"""Command-line entry point for catalog population and DSS export."""

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Sequence


def _date(value: str) -> datetime:
    """Accept UTC ISO dates consistently on supported Python versions."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO event start, e.g. 2016-03-08T18:00:00Z") from exc


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit workflow, returning nonzero for errors or partial exports."""
    parser = argparse.ArgumentParser(prog="stormhub", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("populate", "Search and rank storms into a fresh duration collection"),
        ("resume", "Resume a recorded interrupted full search before Item creation"),
        ("export-dss", "Export DSS for selected Items in an existing collection"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("catalog", type=Path, help="Catalog directory or local catalog.json")
        command.add_argument("--duration", type=int, required=True, help="Storm duration in hours, e.g. 24, 48, 72")
        command.add_argument(
            "--dry-run", action="store_true", help="Validate and print the plan without writes or AORC access"
        )
        command.add_argument("--traceback", action="store_true", help="Include tracebacks when diagnosing failures")
        if name != "export-dss":
            command.add_argument("--workers", type=int, help="Override the frozen worker count")
        if name == "populate":
            command.add_argument(
                "--specific-date",
                action="append",
                type=_date,
                help="Exact UTC event start for a smoke search; repeat for multiple dates (not a range)",
            )
        if name == "export-dss":
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument("--item-ids", nargs="+", help="Only export these Item IDs")
            selection.add_argument("--all-items", action="store_true", help="Explicitly export the entire collection")
            command.add_argument(
                "--output-modes", nargs="+", choices=("source", "target"), help="Override the frozen DSS output modes"
            )
            command.add_argument(
                "--overwrite", action="store_true", help="Replace existing selected DSS products; not atomic"
            )
    args = parser.parse_args(argv)
    from stormhub.logger import initialize_logger

    initialize_logger()
    try:
        from stormhub.met.catalog_population import export_catalog_dss, populate_catalog, resume_catalog

        common = {"duration_hours": args.duration, "dry_run": args.dry_run}
        if args.command == "export-dss":
            result = export_catalog_dss(
                args.catalog,
                item_ids=args.item_ids,
                all_items=args.all_items,
                output_modes=args.output_modes,
                overwrite=args.overwrite,
                **common,
            )
        else:
            operation = populate_catalog if args.command == "populate" else resume_catalog
            options = {"specific_dates": args.specific_date} if args.command == "populate" else {}
            result = operation(args.catalog, num_workers=args.workers, with_tb=args.traceback, **common, **options)
        if args.dry_run or args.command == "export-dss":
            print(json.dumps(result, indent=2))
        else:
            logging.info("%s completed: %s", args.command, result.id)
        return 1 if isinstance(result, dict) and result.get("failed_count", 0) else 0
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        logging.error("%s", exc, exc_info=args.traceback)
        return 1
    except KeyboardInterrupt:
        logging.error("Interrupted; retain partial outputs and inspect before resuming")
        return 130
