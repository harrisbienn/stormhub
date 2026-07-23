"""Inventory or quarantine DSS files not referenced by collection manifests."""

from __future__ import annotations

import argparse
import json

from stormhub.logger import initialize_logger
from stormhub.met.storm_catalog import quarantine_catalog_orphaned_dss_files


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Find DSS files in collection dss folders that are not referenced by "
            "the current dss-manifest.csv. By default this is a dry run."
        )
    )
    parser.add_argument("catalog", help="Path to catalog.json.")
    parser.add_argument(
        "--durations",
        nargs="+",
        type=int,
        required=True,
        help="Storm durations to inspect, such as 24 48 72.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move orphaned DSS files into timestamped backup folders.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the DSS orphan inventory or quarantine workflow."""
    args = parse_args()
    initialize_logger()
    results = quarantine_catalog_orphaned_dss_files(
        catalog=args.catalog,
        storm_durations=args.durations,
        dry_run=not args.apply,
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
