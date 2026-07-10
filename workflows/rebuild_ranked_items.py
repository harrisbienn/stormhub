"""Rebuild rank-addressed storm item folders from existing storm-stats CSV files."""

from __future__ import annotations

import argparse

from stormhub.met.storm_catalog import rebuild_ranked_collection_items


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Repair storm event collections whose numeric item folders were "
            "created from stale ranked-storms.csv output."
        )
    )
    parser.add_argument("catalog", help="Path to catalog.json.")
    parser.add_argument(
        "--durations",
        nargs="+",
        type=int,
        required=True,
        help="Storm durations to repair, such as 24 48 72.",
    )
    parser.add_argument(
        "--min-precip-threshold",
        type=float,
        required=True,
        help="Mean precipitation threshold used for ranking.",
    )
    parser.add_argument(
        "--top-n-events",
        type=int,
        required=True,
        help="Number of top-ranked events to recreate as STAC items.",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=None,
        help="Number of worker processes used to recreate item JSON.",
    )
    parser.add_argument(
        "--use-threads",
        action="store_true",
        help="Use threads instead of processes. Keep unset on native Windows.",
    )
    parser.add_argument(
        "--with-traceback",
        action="store_true",
        help="Log full tracebacks for item creation failures.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Fail if numeric item folders already exist instead of quarantining them.",
    )
    return parser.parse_args()


def main() -> None:
    """Run ranked item rebuilds for the requested durations."""
    args = parse_args()
    for duration in args.durations:
        rebuild_ranked_collection_items(
            catalog=args.catalog,
            storm_duration=duration,
            min_precip_threshold=args.min_precip_threshold,
            top_n_events=args.top_n_events,
            num_workers=args.num_workers,
            use_threads=args.use_threads,
            with_tb=args.with_traceback,
            backup_existing_items=not args.no_backup,
        )


if __name__ == "__main__":
    main()
