"""Comparable rainfall descriptors from normalized interval-depth grids.

This module uses only the standard library. Native formats and geometry
intersection belong to adapters; matching weights and eligibility belong to
FloodForecast.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT = "stormhub/precipitation-features/0.1.0"
NORMALIZED_CONTRACT = "stormhub/normalized-precipitation/0.1.0"
SUPPORT_CONTRACT = "stormhub/precipitation-support/0.1.0"
DEFAULT_DEFINITION = {
    "id": "area-weighted-interval-rainfall",
    "version": "0.1.0",
    "window_minutes": [60, 180, 360],
    "aggregation": "fractional-cell-area",
    "missingness": "require-complete-region",
    "time_alignment": "elapsed-from-event-start",
}


class PrecipitationFeatureError(ValueError):
    """Invalid or ambiguous rainfall, support, or feature evidence."""


def canonical_bytes(value: Any) -> bytes:
    """Encode finite JSON consistently for content identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    """Hash a canonical JSON value."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash a source without importing native readers."""
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def exact(value: Any, fields: Sequence[str], label: str) -> Mapping[str, Any]:
    """Reject missing and unknown fields at owned wire boundaries."""
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise PrecipitationFeatureError(f"{label}: expected exactly {sorted(fields)}")
    return value


def sha256(value: Any, label: str) -> str:
    """Validate a lowercase SHA256 identity."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PrecipitationFeatureError(f"{label}: expected SHA256")
    return value


def number(value: Any, label: str, *, positive: bool = False) -> float:
    """Require finite, nonnegative numeric quantities without coercing strings."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise PrecipitationFeatureError(f"{label}: expected finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def utc(value: str) -> datetime:
    """Read an explicit UTC timestamp, rejecting local or ambiguous clocks."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PrecipitationFeatureError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise PrecipitationFeatureError("timestamps must be explicit UTC")
    return parsed


def validate_grid(grid: Mapping[str, Any]) -> None:
    """Validate an axis-aligned square grid with south-to-north row order."""
    exact(grid, ("crs", "shape", "origin", "cell_size_meters", "row_order"), "grid")
    if not isinstance(grid["crs"], str) or not grid["crs"].strip():
        raise PrecipitationFeatureError("grid requires a CRS")
    if (
        not isinstance(grid["shape"], (list, tuple))
        or len(grid["shape"]) != 2
        or any(type(n) is not int or n <= 0 for n in grid["shape"])
    ):
        raise PrecipitationFeatureError("grid shape must contain positive row and column counts")
    if (
        not isinstance(grid["origin"], (list, tuple))
        or len(grid["origin"]) != 2
        or any(type(n) not in (int, float) or not math.isfinite(n) for n in grid["origin"])
    ):
        raise PrecipitationFeatureError("grid origin must contain finite coordinates")
    number(grid["cell_size_meters"], "cell size", positive=True)
    if grid["row_order"] != "south_to_north":
        raise PrecipitationFeatureError("normalize row order before extraction")


def validate_support(support: Mapping[str, Any], grid: Mapping[str, Any]) -> None:
    """Validate sparse intersection areas and an optional exact subbasin partition."""
    exact(support, ("contract_version", "grid_sha256", "regions", "partition"), "support")
    if support["contract_version"] != SUPPORT_CONTRACT or support["grid_sha256"] != digest(grid):
        raise PrecipitationFeatureError("support contract or grid identity mismatch")
    cells = grid["shape"][0] * grid["shape"][1]
    cell_area = grid["cell_size_meters"] ** 2
    regions = {}
    if not isinstance(support["regions"], list) or not support["regions"]:
        raise PrecipitationFeatureError("support requires explicit regions")
    for region in support["regions"]:
        exact(region, ("id", "kind", "geometry_sha256", "area_m2", "cells"), "region")
        name = region["id"]
        if not isinstance(name, str) or not name or name in regions:
            raise PrecipitationFeatureError("region identities must be nonempty and unique")
        if region["kind"] not in {"basin", "subbasin", "domain", "application-area"}:
            raise PrecipitationFeatureError("unknown spatial support kind")
        sha256(region["geometry_sha256"], "region geometry")
        area = number(region["area_m2"], "region area", positive=True)
        weights = {}
        for cell in region["cells"]:
            exact(cell, ("index", "area_m2"), "cell intersection")
            index = cell["index"]
            weight = number(cell["area_m2"], "intersection area", positive=True)
            if type(index) is not int or index < 0 or index >= cells or index in weights:
                raise PrecipitationFeatureError("invalid or duplicate cell index")
            if weight > cell_area and not math.isclose(weight, cell_area, rel_tol=1e-9):
                raise PrecipitationFeatureError("intersection exceeds grid-cell area")
            weights[index] = weight
        covered = math.fsum(weights.values())
        if covered > area and not math.isclose(covered, area, rel_tol=1e-9, abs_tol=1e-6):
            raise PrecipitationFeatureError("intersection areas exceed region area")
        regions[name] = (region, weights)
    partition = support["partition"]
    if partition is not None:
        exact(partition, ("parent", "children"), "partition")
        names = partition["children"]
        if (
            partition["parent"] not in regions
            or not names
            or len(set(names)) != len(names)
            or partition["parent"] in names
            or any(n not in regions for n in names)
        ):
            raise PrecipitationFeatureError("invalid partition identities")
        parent, parent_weights = regions[partition["parent"]]
        children = [regions[n] for n in names]
        if not math.isclose(
            math.fsum(r["area_m2"] for r, _ in children), parent["area_m2"], rel_tol=1e-9, abs_tol=1e-6
        ):
            raise PrecipitationFeatureError("partition areas do not cover parent")
        for index in set(parent_weights).union(*(set(w) for _, w in children)):
            if not math.isclose(
                math.fsum(w.get(index, 0) for _, w in children),
                parent_weights.get(index, 0),
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise PrecipitationFeatureError("partition overlaps or fails to cover parent cells")


def _definition(value: Mapping[str, Any]) -> None:
    exact(value, tuple(DEFAULT_DEFINITION), "feature definition")
    for key in DEFAULT_DEFINITION:
        if key != "window_minutes" and value[key] != DEFAULT_DEFINITION[key]:
            raise PrecipitationFeatureError(f"unsupported feature definition {key}")
    windows = value["window_minutes"]
    if not isinstance(windows, list) or not windows or any(type(w) is not int or w <= 0 for w in windows):
        raise PrecipitationFeatureError("windows must be positive whole minutes")
    if windows != sorted(set(windows)):
        raise PrecipitationFeatureError("windows must be a sorted unique list")


def _source(value: Mapping[str, Any]) -> None:
    exact(value, ("collection_id", "item_id", "asset_key", "sha256", "size_bytes", "role"), "source")
    for key in ("collection_id", "item_id", "asset_key"):
        if not isinstance(value[key], str) or not value[key]:
            raise PrecipitationFeatureError(f"source {key} is required")
    sha256(value["sha256"], "source")
    if type(value["size_bytes"]) is not int or value["size_bytes"] < 0:
        raise PrecipitationFeatureError("invalid source size")
    if value["role"] not in {"target-precipitation", "incoming-precipitation"}:
        raise PrecipitationFeatureError("features require total target or incoming precipitation, not source/excess")


def _extractor(value: Mapping[str, Any]) -> None:
    exact(value, ("name", "version", "revision", "dirty", "code_sha256", "dependencies"), "extractor")
    if value["name"] != "stormhub.precipitation-features" or value["version"] != "0.1.0":
        raise PrecipitationFeatureError("unsupported extractor")
    if not isinstance(value["revision"], str) or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None:
        raise PrecipitationFeatureError("extractor revision must be a full Git SHA")
    sha256(value["code_sha256"], "extractor code")
    if type(value["dirty"]) is not bool or not isinstance(value["dependencies"], dict):
        raise PrecipitationFeatureError("invalid extractor provenance")


def _region_features(
    series: list[float | None],
    coverage: list[float],
    volumes: list[float],
    area: float,
    interval: int,
    windows: list[int],
) -> dict[str, Any]:
    complete = all(v is not None for v in series)
    total = math.fsum(series) if complete else None
    maxima = {}
    for window in windows:
        if window % interval or window > interval * len(series):
            maxima[str(window)] = {"depth_mm": None, "end_minute": None, "status": "unsupported_window"}
        elif not complete:
            maxima[str(window)] = {"depth_mm": None, "end_minute": None, "status": "incomplete_region"}
        else:
            count = window // interval
            sums = [math.fsum(series[i : i + count]) for i in range(len(series) - count + 1)]
            maximum = max(sums)
            maxima[str(window)] = {
                "depth_mm": maximum,
                "end_minute": (sums.index(maximum) + count) * interval,
                "status": "complete",
            }
    positive = total is not None and total > 0
    return {
        "complete": complete,
        "area_m2": area,
        "total_depth_mm": total,
        "total_volume_m3": total * area / 1000 if complete else None,
        "interval_depth_mm": series,
        "coverage_fraction": coverage,
        "observed_volume_m3": volumes,
        "temporal_fraction": [v / total for v in series] if positive else None,
        "centroid_minute": math.fsum((i + 0.5) * interval * v for i, v in enumerate(series)) / total
        if positive
        else None,
        "maxima": maxima,
    }


def extract_features(
    normalized: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    support: Mapping[str, Any],
    extractor: Mapping[str, Any],
    definition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate all descriptors using one shared, format-independent algorithm.

    Values are interval depths on a flattened south-to-north grid. ``None`` is
    missing; zeros are observed dry cells. A partial region never yields an
    extrapolated total. Adapters authenticate original input bytes separately.
    """
    exact(
        normalized,
        ("contract_version", "grid", "event_start", "event_end", "interval_minutes", "units", "measurement", "values"),
        "normalized precipitation",
    )
    if normalized["contract_version"] != NORMALIZED_CONTRACT or normalized["measurement"] != "interval-depth":
        raise PrecipitationFeatureError("normalize cumulative/rate products into interval depths first")
    _source(source)
    _extractor(extractor)
    definition = dict(DEFAULT_DEFINITION if definition is None else definition)
    _definition(definition)
    grid = normalized["grid"]
    validate_grid(grid)
    validate_support(support, grid)
    start, end = utc(normalized["event_start"]), utc(normalized["event_end"])
    interval = normalized["interval_minutes"]
    if type(interval) is not int or interval <= 0:
        raise PrecipitationFeatureError("interval_minutes must be a positive integer")
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds % (interval * 60):
        raise PrecipitationFeatureError("event must contain whole positive intervals")
    values = normalized["values"]
    if not isinstance(values, (list, tuple)) or len(values) != seconds // (interval * 60):
        raise PrecipitationFeatureError("missing or extra event intervals")
    factors = {"mm": 1.0, "in": 25.4}
    if normalized["units"] not in factors:
        raise PrecipitationFeatureError("supported depth units are mm and in")
    factor = factors[normalized["units"]]
    regions = sorted(support["regions"], key=lambda r: r["id"])
    series = {r["id"]: [] for r in regions}
    coverage = {r["id"]: [] for r in regions}
    volumes = {r["id"]: [] for r in regions}
    normalized_hash = hashlib.sha256()
    for row in values:
        if len(row) != grid["shape"][0] * grid["shape"][1]:
            raise PrecipitationFeatureError("interval grid shape mismatch")
        depths = [None if v is None else number(v, "precipitation") * factor for v in row]
        normalized_hash.update(canonical_bytes(depths) + b"\n")
        for region in regions:
            valid = [(depths[c["index"]], c["area_m2"]) for c in region["cells"] if depths[c["index"]] is not None]
            area = region["area_m2"]
            valid_area = math.fsum(w for _, w in valid)
            volume = math.fsum(v * w for v, w in valid) / 1000
            complete = math.isclose(valid_area, area, rel_tol=1e-9)
            name = region["id"]
            series[name].append(volume * 1000 / area if complete else None)
            coverage[name].append(min(1.0, valid_area / area))
            volumes[name].append(volume)
    features = {
        r["id"]: _region_features(
            series[r["id"]], coverage[r["id"]], volumes[r["id"]], r["area_m2"], interval, definition["window_minutes"]
        )
        for r in regions
    }
    allocation = None
    if support["partition"]:
        parent = features[support["partition"]["parent"]]["total_volume_m3"]
        names = sorted(support["partition"]["children"])
        available = parent is not None and parent > 0 and all(features[n]["complete"] for n in names)
        allocation = {name: features[name]["total_volume_m3"] / parent if available else None for name in names}
    payload = {
        "contract_version": CONTRACT,
        "source": dict(source),
        "grid": dict(grid),
        "definition": definition,
        "definition_sha256": digest(definition),
        "support_sha256": digest(support),
        "regions": [{k: v for k, v in r.items() if k != "cells"} for r in regions],
        "partition": support["partition"],
        "extractor": dict(extractor),
        "extractor_sha256": digest(extractor),
        "event": {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "interval_minutes": interval,
            "interval_end_minutes": [(i + 1) * interval for i in range(len(values))],
        },
        "normalized_values_sha256": normalized_hash.hexdigest(),
        "features": features,
        "spatial_allocation": allocation,
        "complete": all(f["complete"] for f in features.values()),
    }
    payload["feature_id"] = "precipitation-features-" + digest(payload)[:24]
    validate_manifest(payload)
    return payload


def validate_manifest(value: Mapping[str, Any]) -> None:
    """Authenticate an exported feature manifest and its owned identities."""
    exact(
        value,
        (
            "contract_version",
            "feature_id",
            "source",
            "grid",
            "definition",
            "definition_sha256",
            "support_sha256",
            "regions",
            "partition",
            "extractor",
            "extractor_sha256",
            "event",
            "normalized_values_sha256",
            "features",
            "spatial_allocation",
            "complete",
        ),
        "feature manifest",
    )
    if value["contract_version"] != CONTRACT:
        raise PrecipitationFeatureError("unsupported precipitation feature contract")
    body = {k: v for k, v in value.items() if k != "feature_id"}
    if value["feature_id"] != "precipitation-features-" + digest(body)[:24]:
        raise PrecipitationFeatureError("feature identity does not authenticate its content")
    _source(value["source"])
    _extractor(value["extractor"])
    _definition(value["definition"])
    validate_grid(value["grid"])
    for name in ("definition", "extractor"):
        if digest(value[name]) != value[name + "_sha256"]:
            raise PrecipitationFeatureError(f"{name} identity mismatch")
    sha256(value["support_sha256"], "support")
    sha256(value["normalized_values_sha256"], "normalized values")
    if type(value["complete"]) is not bool:
        raise PrecipitationFeatureError("complete must be boolean")
    event = exact(value["event"], ("start", "end", "interval_minutes", "interval_end_minutes"), "event")
    interval = event["interval_minutes"]
    if type(interval) is not int or interval <= 0:
        raise PrecipitationFeatureError("invalid feature interval")
    duration = (utc(event["end"]) - utc(event["start"])).total_seconds() / 60
    if duration <= 0 or duration % interval:
        raise PrecipitationFeatureError("invalid feature event duration")
    count = int(duration / interval)
    ends = event["interval_end_minutes"]
    if not isinstance(ends, list) or len(ends) != count or any(end != (i + 1) * interval for i, end in enumerate(ends)):
        raise PrecipitationFeatureError("feature elapsed time axis mismatch")
    if not isinstance(value["regions"], list) or not value["regions"]:
        raise PrecipitationFeatureError("feature regions must be a nonempty array")
    names = set()
    for region in value["regions"]:
        exact(region, ("id", "kind", "geometry_sha256", "area_m2"), "feature region")
        name = region["id"]
        if not isinstance(name, str) or not name or name in names:
            raise PrecipitationFeatureError("feature regions require unique identities")
        names.add(name)
        sha256(region["geometry_sha256"], "geometry")
        area = number(region["area_m2"], "region area", positive=True)
        feature = value["features"].get(name)
        if not isinstance(feature, Mapping):
            raise PrecipitationFeatureError("missing region features")
        series, coverage, volumes = (
            feature.get(k) for k in ("interval_depth_mm", "coverage_fraction", "observed_volume_m3")
        )
        if any(not isinstance(s, list) or len(s) != count for s in (series, coverage, volumes)):
            raise PrecipitationFeatureError("region feature time axis mismatch")
        for depth, fraction, volume in zip(series, coverage, volumes):
            number(fraction, "coverage")
            number(volume, "observed volume")
            if fraction > 1 or (depth is not None) != math.isclose(fraction, 1, rel_tol=1e-9):
                raise PrecipitationFeatureError("inconsistent feature coverage")
            if depth is not None:
                number(depth, "depth")
                if not math.isclose(depth * area / 1000, volume, rel_tol=1e-9, abs_tol=1e-9):
                    raise PrecipitationFeatureError("inconsistent feature volume")
        expected = _region_features(series, coverage, volumes, area, interval, value["definition"]["window_minutes"])
        if feature != expected:
            raise PrecipitationFeatureError("inconsistent region descriptors")
    if set(value["features"]) != names or value["complete"] != all(f["complete"] for f in value["features"].values()):
        raise PrecipitationFeatureError("inconsistent feature completeness or membership")
    allocation = None
    if value["partition"] is not None:
        partition = exact(value["partition"], ("parent", "children"), "feature partition")
        children = partition["children"]
        if (
            partition["parent"] not in names
            or not isinstance(children, list)
            or not children
            or any(not isinstance(n, str) or n not in names or n == partition["parent"] for n in children)
            or len(set(children)) != len(children)
        ):
            raise PrecipitationFeatureError("invalid feature partition")
        parent = value["features"][partition["parent"]]["total_volume_m3"]
        available = parent is not None and parent > 0 and all(value["features"][n]["complete"] for n in children)
        allocation = {n: value["features"][n]["total_volume_m3"] / parent if available else None for n in children}
    if value["spatial_allocation"] != allocation:
        raise PrecipitationFeatureError("inconsistent spatial allocation")


def write_features(manifest: Mapping[str, Any], directory: Path) -> Path:
    """Write an immutable content-named manifest; identical retries succeed."""
    validate_manifest(manifest)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (manifest["feature_id"] + ".json")
    content = canonical_bytes(manifest) + b"\n"
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise PrecipitationFeatureError("refusing to overwrite a different feature manifest") from None
    return path
