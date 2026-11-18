"""Versioned forecast-rainfall exports, independent of consumer cycle selection.

Transport and native decoding remain in their product adapters. This boundary
authenticates retained evidence and exports one deterministic source; it neither
selects an event nor blends models. New products require an explicit owned adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .hrrr_acquisition import descriptor, validate_acquisition, validate_request, verify_asset, write_immutable
from .hrrr_normalization import FORCING_CONTRACT, validate_hour_tags
from .precipitation_features import (
    NORMALIZED_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    exact,
    extract_features,
    number,
    sha256,
    utc,
    validate_grid,
    validate_manifest,
    validate_support,
)

CONTRACT = "stormhub/forecast-precipitation/1.0.0"
_PRODUCT_FIELDS = ("provider", "model", "model_version", "member", "product", "adapter_version")
_HRRR_PRODUCT = ("noaa", "hrrr", "hrrr-v4", "deterministic", "conus-hourly-surface-apcp", "0.1.0")
_NORMALIZATION = {
    "source_units": "kg m-2",
    "source_measurement": "interval-depth",
    "target_measurement": "interval-depth",
    "method": "projected-cell-intersection-v1",
    "accumulation_resets": "not-applicable",
    "missingness": "null-is-missing",
    "negative_values": "refuse",
    "outside_support": "null-not-consumed",
}


def _asset(asset: Any) -> None:
    exact(asset, ("href", "sha256", "size_bytes"), "asset")
    href = asset["href"]
    if (
        not isinstance(href, str)
        or not href
        or "\\" in href
        or ":" in href
        or PurePosixPath(href).is_absolute()
        or any(part in ("", ".", "..") for part in href.split("/"))
        or any(ord(character) < 32 for character in href)
    ):
        raise PrecipitationFeatureError("asset requires a contained portable relative href")
    sha256(asset["sha256"], "asset")
    if type(asset["size_bytes"]) is not int or asset["size_bytes"] <= 0:
        raise PrecipitationFeatureError("asset size must be a positive integer")


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrecipitationFeatureError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_asset(root: Path, asset: Mapping[str, Any]) -> dict[str, Any]:
    _asset(asset)
    path = verify_asset(root, asset)
    content = path.read_bytes()
    if len(content) != asset["size_bytes"] or hashlib.sha256(content).hexdigest() != asset["sha256"]:
        raise PrecipitationFeatureError("asset changed while reading")
    try:
        value = json.loads(content, object_pairs_hook=_unique_fields)
        canonical_bytes(value)  # Non-finite JSON is not an accepted wire value.
    except (ValueError, UnicodeDecodeError) as error:
        raise PrecipitationFeatureError("asset is not finite, unambiguous JSON") from error
    if not isinstance(value, dict):
        raise PrecipitationFeatureError("asset must contain a JSON object")
    return value


def _native_grid(grid: Any) -> None:
    exact(grid, ("crs", "shape", "transform"), "native source grid")
    if not isinstance(grid["crs"], str) or not grid["crs"].strip():
        raise PrecipitationFeatureError("native source grid requires a CRS")
    if (
        not isinstance(grid["shape"], list)
        or len(grid["shape"]) != 2
        or any(type(value) is not int or value <= 0 for value in grid["shape"])
        or not isinstance(grid["transform"], list)
        or len(grid["transform"]) != 6
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in grid["transform"])
        or grid["transform"][0] <= 0
        or grid["transform"][4] >= 0
        or grid["transform"][1] != 0
        or grid["transform"][3] != 0
    ):
        raise PrecipitationFeatureError("native source grid must be a regular north-to-south raster")


def _product(value: Mapping[str, Any]) -> tuple[str, ...]:
    key = tuple(value[field] for field in _PRODUCT_FIELDS)
    if any(not isinstance(part, str) or not part for part in key) or key not in _PRODUCT_POLICIES:
        raise PrecipitationFeatureError("unsupported provider/model/version/member/product/adapter combination")
    return key


def validate_export(value: Mapping[str, Any]) -> None:
    """Validate the reusable envelope and its explicitly registered product policy.

    A valid receipt is not proof of retained bytes. Use ``verify_export`` at an
    input boundary. Product-specific raw evidence, normalization and source grids
    belong to the selected policy; registration is an owning-package code change.
    """
    exact(
        value,
        (
            "contract_version",
            "export_id",
            *_PRODUCT_FIELDS,
            "initialization",
            "valid_start",
            "valid_end",
            "interval_seconds",
            "units",
            "source_grid_sha256",
            "source_grid",
            "target_grid",
            "normalized",
            "weights",
            "support",
            "features",
            "feature_id",
            "normalization",
            "availability",
            "available_at",
            "source_revision",
            "intervals",
            "provenance",
            "forecast_eligible",
        ),
        "forecast precipitation export",
    )
    if value["contract_version"] != CONTRACT:
        raise PrecipitationFeatureError("unsupported forecast precipitation contract")
    policy = _PRODUCT_POLICIES[_product(value)][0]
    if value["forecast_eligible"] is not False:
        raise PrecipitationFeatureError("forecast precipitation remains research-only")
    seconds = value["interval_seconds"]
    if type(seconds) is not int or seconds <= 0:
        raise PrecipitationFeatureError("forecast interval_seconds must be a positive integer")
    start, end = utc(value["valid_start"]), utc(value["valid_end"])
    initialization = utc(value["initialization"])
    duration = (end - start).total_seconds()
    if start < initialization or duration <= 0 or duration % seconds:
        raise PrecipitationFeatureError("forecast precipitation valid window is inconsistent")
    if utc(value["available_at"]) < initialization:
        raise PrecipitationFeatureError("source availability predates initialization")
    if value["units"] != "mm":
        raise PrecipitationFeatureError("forecast exports require normalized millimetre depth")
    for key in ("source_grid", "normalization", "availability", "source_revision", "provenance"):
        if not isinstance(value[key], Mapping) or not value[key]:
            raise PrecipitationFeatureError(f"{key} requires explicit owner evidence")
    if value["source_grid_sha256"] != digest(value["source_grid"]):
        raise PrecipitationFeatureError("source-grid identity differs")
    validate_grid(value["target_grid"])
    for key in ("normalized", "weights", "support", "features"):
        _asset(value[key])
    if not isinstance(value["feature_id"], str) or not re.fullmatch(
        r"precipitation-features-[0-9a-f]{24}", value["feature_id"]
    ):
        raise PrecipitationFeatureError("forecast export requires a shared precipitation feature identity")
    intervals = value["intervals"]
    if not isinstance(intervals, list) or len(intervals) != int(duration / seconds):
        raise PrecipitationFeatureError("missing, duplicated or extra forecast intervals")
    cell_count = math.prod(value["target_grid"]["shape"])
    for index, interval in enumerate(intervals):
        exact(interval, ("start", "end", "row", "values_sha256", "missing_cells", "accumulation", "raw"), "interval")
        interval_start = start + timedelta(seconds=index * seconds)
        interval_end = interval_start + timedelta(seconds=seconds)
        if (
            type(interval["row"]) is not int
            or interval["row"] != index
            or utc(interval["start"]) != interval_start
            or utc(interval["end"]) != interval_end
        ):
            raise PrecipitationFeatureError("intervals must be contiguous, ordered and bound to normalized rows")
        sha256(interval["values_sha256"], "normalized interval values")
        if type(interval["missing_cells"]) is not int or not 0 <= interval["missing_cells"] <= cell_count:
            raise PrecipitationFeatureError("invalid missing-cell count")
        accumulation = exact(interval["accumulation"], ("start", "end", "reset_at"), "accumulation")
        origin = utc(accumulation["start"])
        if origin < initialization or origin > interval_start or utc(accumulation["end"]) != interval_end:
            raise PrecipitationFeatureError("source accumulation interval is inconsistent")
        if accumulation["reset_at"] is not None and utc(accumulation["reset_at"]) != origin:
            raise PrecipitationFeatureError("explicit reset must identify the source accumulation origin")
        if not isinstance(interval["raw"], Mapping) or not interval["raw"]:
            raise PrecipitationFeatureError("interval requires raw owner evidence")
    policy(value)
    body = {key: item for key, item in value.items() if key != "export_id"}
    if value["export_id"] != "forecast-precipitation-" + digest(body)[:24]:
        raise PrecipitationFeatureError("forecast precipitation export identity differs")


def _validate_hrrr_export(value: Mapping[str, Any]) -> None:
    if value["normalization"] != _NORMALIZATION:
        raise PrecipitationFeatureError("unsupported product normalization; no implicit accumulation conversion")
    start, end = utc(value["valid_start"]), utc(value["valid_end"])
    if value["interval_seconds"] != 3600 or start != utc(value["initialization"]):
        raise PrecipitationFeatureError("selected HRRR product requires its complete hourly forecast")
    validate_request(
        {
            "initialization": value["initialization"],
            "hours": int((end - start).total_seconds() / 3600),
            "model_version": value["model_version"],
            "base_url": "https://contract.invalid",
        }
    )
    _native_grid(value["source_grid"])
    revision = exact(value["source_revision"], ("retrieval_id", "provider_revision"), "source revision")
    sha256(revision["retrieval_id"], "retrieval identity")
    if revision["provider_revision"] is not None:
        raise PrecipitationFeatureError("legacy HRRR provider revision is unknown; do not infer it")
    availability = exact(
        value["availability"],
        ("status", "original_publication_time", "first_observed_at", "complete_observed_at"),
        "availability",
    )
    if availability["status"] != "archive-retrieval-only" or availability["original_publication_time"] is not None:
        raise PrecipitationFeatureError(
            "legacy retrieval evidence cannot establish original publication or as-of skill"
        )
    first, complete = utc(availability["first_observed_at"]), utc(availability["complete_observed_at"])
    if first < start or complete < first or utc(value["available_at"]) != complete:
        raise PrecipitationFeatureError("availability must retain actual raw acquisition observations")
    provenance = exact(
        value["provenance"], ("forcing", "dataset_id", "contract_version", "acquisition"), "HRRR provenance"
    )
    _asset(provenance["forcing"])
    _asset(provenance["acquisition"])
    if provenance["contract_version"] != FORCING_CONTRACT:
        raise PrecipitationFeatureError("unsupported retained forcing contract")
    observations = []
    for interval in value["intervals"]:
        accumulation = interval["accumulation"]
        if utc(accumulation["start"]) != utc(interval["start"]) or accumulation["reset_at"] is not None:
            raise PrecipitationFeatureError(
                "hourly HRRR cannot accept cumulative, reset or substituted accumulation fields"
            )
        raw = exact(interval["raw"], ("message", "index", "observed_at", "etag", "byte_range"), "HRRR raw interval")
        _asset(raw["message"])
        _asset(raw["index"])
        bounds = raw["byte_range"]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(type(bound) is not int or bound < 0 for bound in bounds)
            or bounds[1] - bounds[0] + 1 != raw["message"]["size_bytes"]
            or not isinstance(raw["etag"], str)
            or not raw["etag"]
            or raw["etag"].startswith("W/")
        ):
            raise PrecipitationFeatureError("raw interval requires its exact byte range and strong object identity")
        observations.append(utc(raw["observed_at"]))
    if min(observations) != first or max(observations) != complete:
        raise PrecipitationFeatureError("availability differs from raw-byte observation evidence")


def from_hrrr(forcing_file: str | Path, *, raw_root: str | Path) -> dict[str, Any]:
    """Project one authenticated retained HRRR bundle without changing its bytes.

    The forcing file's parent is the bundle root; raw acquisition descriptors
    resolve under the separately supplied raw root. No transport, native reader,
    cumulative differencing or event selection runs here. Model version comes
    from the existing validated request, never from a filename or display label.
    """
    try:
        return _from_hrrr(Path(forcing_file), Path(raw_root))
    except (KeyError, TypeError, IndexError) as error:
        # This is a wire-parser boundary. Legacy nested values can otherwise
        # surface incidental indexing errors instead of an actionable refusal.
        raise PrecipitationFeatureError("malformed retained HRRR evidence") from error


def _from_hrrr(forcing_file: Path, raw_root: Path) -> dict[str, Any]:
    forcing_path = Path(forcing_file).absolute()
    root, raw = forcing_path.parent, Path(raw_root)
    forcing_asset = descriptor(forcing_path, root)
    receipt = _read_asset(root, forcing_asset)
    exact(
        receipt,
        (
            "contract_version",
            "forcing_id",
            "request",
            "acquisition",
            "producer",
            "normalized",
            "weights",
            "support",
            "features",
            "feature_id",
            "intervals",
            "selected_target_cells",
            "outside_support",
            "availability",
            "applicability",
            "dataset_id",
        ),
        "retained HRRR forcing",
    )
    if receipt.get("contract_version") != FORCING_CONTRACT:
        raise PrecipitationFeatureError("expected the retained HRRR forcing contract")
    body = {key: item for key, item in receipt.items() if key != "dataset_id"}
    if receipt.get("dataset_id") != "forcing-dataset-" + digest(body)[:24]:
        raise PrecipitationFeatureError("retained forcing dataset identity differs")
    request = receipt["request"]
    validate_request(request)
    producer = receipt["producer"]
    exact(
        producer,
        ("name", "version", "revision", "dirty", "code_sha256", "dependencies", "gdal"),
        "retained HRRR producer",
    )
    if producer.get("name") != "stormhub.hrrr-forcing" or producer.get("version") != "0.1.0":
        raise PrecipitationFeatureError("unsupported retained HRRR normalizer identity")
    if (
        not isinstance(producer["revision"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", producer["revision"])
        or type(producer["dirty"]) is not bool
        or not isinstance(producer["gdal"], str)
        or not producer["gdal"].strip()
    ):
        raise PrecipitationFeatureError("retained HRRR producer requires revision, dirty status and GDAL identity")
    sha256(producer["code_sha256"], "retained HRRR producer code")
    dependencies = producer["dependencies"]
    if (
        not isinstance(dependencies, Mapping)
        or set(dependencies) != {"rasterio", "numpy", "pyproj", "shapely"}
        or any(not isinstance(version, str) or not version.strip() for version in dependencies.values())
    ):
        raise PrecipitationFeatureError("retained HRRR producer requires exact scientific dependency identities")
    acquisition = _read_asset(root, receipt["acquisition"])
    validate_acquisition(acquisition, raw)
    if acquisition["request"] != request:
        raise PrecipitationFeatureError("forcing and acquisition requests differ")
    normalized = _read_asset(root, receipt["normalized"])
    support = _read_asset(root, receipt["support"])
    weights = _read_asset(root, receipt["weights"])
    features = _read_asset(root, receipt["features"])
    exact(
        normalized,
        ("contract_version", "grid", "event_start", "event_end", "interval_minutes", "units", "measurement", "values"),
        "normalized rainfall",
    )
    if (
        normalized["contract_version"] != NORMALIZED_CONTRACT
        or normalized["units"] != "mm"
        or normalized["measurement"] != "interval-depth"
        or type(normalized["interval_minutes"]) is not int
        or normalized["interval_minutes"] != 60
        or utc(normalized["event_start"]) != utc(request["initialization"])
        or utc(normalized["event_end"]) != utc(request["initialization"]) + timedelta(hours=request["hours"])
        or not isinstance(normalized["values"], list)
        or len(normalized["values"]) != request["hours"]
    ):
        raise PrecipitationFeatureError("retained HRRR normalized clocks, units or measurement differ")
    validate_grid(normalized["grid"])
    validate_support(support, normalized["grid"])
    validate_manifest(features)
    if "native_grid" not in weights:
        raise PrecipitationFeatureError("retained HRRR weights lack native grid provenance")
    _native_grid(weights["native_grid"])
    if weights.get("method") != _NORMALIZATION["method"] or weights.get("target_grid_sha256") != digest(
        normalized["grid"]
    ):
        raise PrecipitationFeatureError("retained normalization grid or method differs")
    forcing_id = digest(
        {
            "acquisition": acquisition["acquisition_id"],
            "normalized": receipt["normalized"],
            "support": digest(support),
            "producer": producer,
        }
    )
    if receipt["forcing_id"] != forcing_id or receipt["feature_id"] != features["feature_id"]:
        raise PrecipitationFeatureError("retained forcing or feature identity differs")
    if (
        features["grid"] != normalized["grid"]
        or features["support_sha256"] != digest(support)
        or features["source"]
        != {
            "collection_id": "hrrr-conus-hourly-apcp",
            "item_id": forcing_id,
            "asset_key": "normalized-precipitation",
            "role": "incoming-precipitation",
            "sha256": receipt["normalized"]["sha256"],
            "size_bytes": receipt["normalized"]["size_bytes"],
        }
        or utc(features["event"]["start"]) != utc(normalized["event_start"])
        or utc(features["event"]["end"]) != utc(normalized["event_end"])
        or features["event"]["interval_minutes"] != 60
    ):
        raise PrecipitationFeatureError("retained features do not bind the normalized rainfall")
    intervals = []
    value_hash = hashlib.sha256()
    if not isinstance(receipt["intervals"], list) or len(receipt["intervals"]) != request["hours"]:
        raise PrecipitationFeatureError("retained forcing interval count differs")
    for index, (values, evidence, record) in enumerate(
        zip(normalized["values"], receipt["intervals"], acquisition["records"])
    ):
        if not isinstance(values, list) or len(values) != math.prod(normalized["grid"]["shape"]):
            raise PrecipitationFeatureError("normalized rainfall row shape differs")
        depths = [None if value is None else number(value, "precipitation") for value in values]
        value_hash.update(canonical_bytes(depths) + b"\n")
        exact(evidence, ("hour", "metadata", "grib_tags", "volume_check"), "retained HRRR interval")
        metadata = validate_hour_tags(evidence["grib_tags"], request["initialization"], index + 1)
        if evidence["hour"] != index + 1 or evidence["metadata"] != metadata:
            raise PrecipitationFeatureError("retained decoded accumulation metadata differs")
        intervals.append(
            {
                "start": metadata["start"],
                "end": metadata["end"],
                "row": index,
                "values_sha256": digest(depths),
                "missing_cells": sum(value is None for value in depths),
                "accumulation": {"start": metadata["start"], "end": metadata["end"], "reset_at": None},
                "raw": {key: record[key] for key in ("message", "index", "observed_at", "etag", "byte_range")},
            }
        )
    if value_hash.hexdigest() != features["normalized_values_sha256"]:
        raise PrecipitationFeatureError("retained feature values hash differs")
    if (
        extract_features(
            normalized,
            source=features["source"],
            support=support,
            extractor=features["extractor"],
            definition=features["definition"],
        )
        != features
    ):
        raise PrecipitationFeatureError("retained feature summaries differ from normalized rainfall")
    availability = receipt["availability"]
    if (
        acquisition.get("availability_status") != availability["status"]
        or acquisition.get("original_publication_time") != availability["original_publication_time"]
    ):
        raise PrecipitationFeatureError("forcing and acquisition availability differ")
    if (
        receipt["outside_support"] != _NORMALIZATION["outside_support"]
        or receipt["applicability"].get("forecast_eligible") is not False
    ):
        raise PrecipitationFeatureError("retained applicability or missingness differs")
    exported = {
        "contract_version": CONTRACT,
        **dict(zip(_PRODUCT_FIELDS, _HRRR_PRODUCT)),
        "initialization": request["initialization"],
        "valid_start": normalized["event_start"],
        "valid_end": normalized["event_end"],
        "interval_seconds": 3600,
        "units": "mm",
        "source_grid": weights["native_grid"],
        "source_grid_sha256": digest(weights["native_grid"]),
        "target_grid": normalized["grid"],
        **{key: receipt[key] for key in ("normalized", "weights", "support", "features", "feature_id")},
        "normalization": dict(_NORMALIZATION),
        "availability": availability,
        "available_at": availability["complete_observed_at"],
        "source_revision": {"retrieval_id": acquisition["acquisition_id"], "provider_revision": None},
        "intervals": intervals,
        "provenance": {
            "forcing": forcing_asset,
            "dataset_id": receipt["dataset_id"],
            "contract_version": FORCING_CONTRACT,
            "acquisition": receipt["acquisition"],
        },
        "forecast_eligible": False,
    }
    exported["export_id"] = "forecast-precipitation-" + digest(exported)[:24]
    validate_export(exported)
    return exported


def _verify_hrrr_export(value: Mapping[str, Any], bundle_root: Path, raw_root: Path) -> None:
    forcing = verify_asset(bundle_root, value["provenance"]["forcing"])
    # Bundle descriptors are relative to the forcing receipt's parent.
    if forcing.parent != bundle_root.resolve():
        raise PrecipitationFeatureError("forcing receipt must be at the bound bundle root")
    if from_hrrr(forcing, raw_root=raw_root) != value:
        raise PrecipitationFeatureError("forecast precipitation export differs from retained owner evidence")


def verify_export(value: Mapping[str, Any], *, bundle_root: str | Path, raw_root: str | Path) -> None:
    """Authenticate retained assets with the selected product's owner verifier."""
    validate_export(value)
    _PRODUCT_POLICIES[_product(value)][1](value, Path(bundle_root), Path(raw_root))


def write_export(
    value: Mapping[str, Any], directory: str | Path, *, bundle_root: str | Path, raw_root: str | Path
) -> Path:
    """Verify before creating output, then publish an immutable content-named receipt.

    Asset hrefs keep their explicit bundle/raw roots; ``directory`` stores only
    this receipt. Identical retries reuse complete bytes. Conflicts are retained
    and refused by the package's existing atomic publication boundary.
    """
    verify_export(value, bundle_root=bundle_root, raw_root=raw_root)
    path = Path(directory) / (value["export_id"] + ".json")
    write_immutable(path, canonical_bytes(value))
    return path


# A new product must supply both semantic admission and retained-byte proof.
# This fixed table is not populated from configuration or dynamic imports.
_PRODUCT_POLICIES = {_HRRR_PRODUCT: (_validate_hrrr_export, _verify_hrrr_export)}
