"""Remove exact duplicate rows from storm-stats CSV files."""

from __future__ import annotations

import argparse
import json

from stormhub.logger import initialize_logger
from stormhub.met.storm_catalog import clean_catalog_storm_stats


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Clean exact duplicate rows from storm-stats.csv files while "
            "preserving duplicate storm_date rows that have different values."
        )
    )
    parser.add_argument("catalog", help="Path to catalog.json.")
    parser.add_argument(
        "--durations",
        nargs="+",
        type=int,
        required=True,
        help="Storm durations to clean, such as 24 48 72.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not write timestamped .bak copies before cleaning.",
    )
    return parser.parse_args()


def main() -> None:
    """Clean duplicate storm stats rows and print a JSON summary."""
    args = parse_args()
    initialize_logger()
    results = clean_catalog_storm_stats(
        catalog=args.catalog,
        storm_durations=args.durations,
        backup=not args.no_backup,
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
