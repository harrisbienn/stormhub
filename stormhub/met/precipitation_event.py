"""Compose explicitly selected normalized intervals through the shared calculator.

FloodForecast selects the event window and QPE/QPF seam. StormHub verifies the
selected rainfall bytes and performs rainfall composition and feature extraction.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from .hrrr_acquisition import descriptor, verify_asset, write_immutable
from .precipitation_adapters import extractor_identity
from .precipitation_features import (
    NORMALIZED_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    extract_features,
    utc,
    validate_support,
    write_features,
)


def assemble(plan: dict, sources: dict, *, support: dict, output: Path, revision: str, dirty: bool = False) -> dict:
    """Honor a content-bound cycle selection without discovering or replacing intervals.

    sources maps dataset IDs to (receipt, bundle directory). Explicit row indices
    and clocks are checked against each normalized source. The full event must
    be contiguous and share grid, units, interval length and spatial support.
    """
    body = {k: v for k, v in plan.items() if k != "cycle_id"}
    if (
        plan.get("contract_version") != "floodforecast/forecast-cycle/0.1.0"
        or plan.get("cycle_id") != "forecast-cycle-" + digest(body)[:24]
    ):
        raise PrecipitationFeatureError("invalid forecast-cycle selection identity")
    loaded = {}
    for binding in plan["sources"]:
        identity = binding["dataset_id"]
        receipt, root = sources[identity]
        if receipt["dataset_id"] != identity or digest(receipt) != binding["receipt_sha256"]:
            raise PrecipitationFeatureError("event source receipt differs from selected cycle")
        normalized = json.loads(verify_asset(root, receipt["normalized"]).read_bytes())
        owner_support = json.loads(verify_asset(root, receipt["support"]).read_bytes())
        if digest(owner_support) != digest(support):
            raise PrecipitationFeatureError("event sources must use identical spatial support")
        if (
            normalized["contract_version"] != NORMALIZED_CONTRACT
            or normalized["interval_minutes"] != 60
            or normalized["units"] != "mm"
            or normalized["measurement"] != "interval-depth"
        ):
            raise PrecipitationFeatureError("event source is not normalized hourly mm depth")
        validate_support(support, normalized["grid"])
        loaded[identity] = normalized
    current = utc(plan["event_origin"])
    values = []
    grid = None
    for selection in plan["intervals"]:
        normalized = loaded[selection["dataset_id"]]
        row = selection["row"]
        if type(row) is not int or not 0 <= row < len(normalized["values"]):
            raise PrecipitationFeatureError("event selected row is out of range")
        start = utc(normalized["event_start"]) + timedelta(hours=row)
        end = start + timedelta(hours=1)
        if (
            start != current
            or start != utc(selection["start"])
            or end != utc(selection["end"])
            or end > utc(normalized["event_end"])
        ):
            raise PrecipitationFeatureError("event interval has gap, overlap or mismatched source clock")
        if grid is not None and grid != normalized["grid"]:
            raise PrecipitationFeatureError("event grids differ")
        grid = normalized["grid"]
        values.append(normalized["values"][row])
        current = end
    if current != utc(plan["event_end"]) or not values:
        raise PrecipitationFeatureError("event selection does not cover its complete window")
    normalized = {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": grid,
        "event_start": plan["event_origin"],
        "event_end": plan["event_end"],
        "interval_minutes": 60,
        "units": "mm",
        "measurement": "interval-depth",
        "values": values,
    }
    asset_path = output / "normalized.json"
    write_immutable(asset_path, canonical_bytes(normalized))
    asset = descriptor(asset_path, output)
    producer = {"revision": revision, "dirty": dirty, "code_sha256": digest(Path(__file__).read_text(encoding="utf-8"))}
    event_id = (
        "precipitation-event-" + digest({"cycle_id": plan["cycle_id"], "normalized": asset, "producer": producer})[:24]
    )
    features = extract_features(
        normalized,
        source={
            "collection_id": "observed-forecast-events",
            "item_id": event_id,
            "asset_key": "normalized-precipitation",
            "role": "incoming-precipitation",
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
        },
        support=support,
        extractor=extractor_identity(revision, dirty=dirty),
    )
    feature_path = write_features(features, output)
    receipt = {
        "contract_version": "stormhub/precipitation-event/0.1.0",
        "event_id": event_id,
        "cycle_id": plan["cycle_id"],
        "normalized": asset,
        "producer": producer,
        "features": descriptor(feature_path, output),
        "feature_id": features["feature_id"],
        "forecast_eligible": False,
    }
    write_immutable(output / (event_id + ".json"), canonical_bytes(receipt))
    return receipt
