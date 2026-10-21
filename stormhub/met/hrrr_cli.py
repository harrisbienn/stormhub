"""Acquire and normalize one bounded, research-only HRRR precipitation cycle."""

import argparse
import json
from pathlib import Path

from .hrrr_acquisition import acquire
from .hrrr_normalization import normalize
from .precipitation_features import PrecipitationFeatureError


def main(argv=None) -> int:
    """Run explicit acquisition or verified offline replay in the owning environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("request", "grid", "support", "cache", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = acquire(json.loads(args.request.read_bytes()), args.cache, offline=args.offline)
        result = normalize(
            receipt,
            args.cache,
            grid=json.loads(args.grid.read_bytes()),
            support=json.loads(args.support.read_bytes()),
            output=args.output,
            revision=args.revision,
            dirty=args.dirty,
        )
    except (PrecipitationFeatureError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"HRRR forcing failed: {exc}\n")
    print(
        json.dumps({"dataset_id": result["dataset_id"], "feature_id": result["feature_id"], "forecast_eligible": False})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
