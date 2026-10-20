"""Export immutable rainfall features without importing native tools until needed."""

import argparse
import json
from pathlib import Path

from .precipitation_adapters import extract_normalized_file, extractor_identity, read_dss
from .precipitation_features import PrecipitationFeatureError, extract_features, write_features


def main(argv=None) -> int:
    """Run an explicit, locally bound feature extraction job."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--format", choices=("normalized-json", "dss"), required=True)
    parser.add_argument(
        "--source", type=Path, required=True, help="Exact asset identity JSON, including checksum and role"
    )
    parser.add_argument("--support", type=Path, required=True, help="Grid-bound fractional-area support JSON")
    parser.add_argument("--revision", required=True, help="Full owning StormHub Git revision")
    parser.add_argument("--dirty", action="store_true", help="Record uncommitted extractor work explicitly")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selector")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args(argv)
    if args.format == "dss" and not all((args.selector, args.start, args.end)):
        parser.error("DSS requires --selector, --start and --end")
    try:
        source = json.loads(args.source.read_bytes())
        support = json.loads(args.support.read_bytes())
        extractor = extractor_identity(
            args.revision, dirty=args.dirty, dependencies=("hecdss", "numpy") if args.format == "dss" else ()
        )
        if args.format == "dss":
            normalized = read_dss(
                args.input, source=source, selector=args.selector, event_start=args.start, event_end=args.end
            )
            manifest = extract_features(normalized, source=source, support=support, extractor=extractor)
        else:
            manifest = extract_normalized_file(args.input, source=source, support=support, extractor=extractor)
        path = write_features(manifest, args.output)
    except (PrecipitationFeatureError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"rainfall extraction failed: {exc}\n")
    print(json.dumps({"feature_id": manifest["feature_id"], "path": str(path), "complete": manifest["complete"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
