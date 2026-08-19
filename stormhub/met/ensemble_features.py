"""Export authenticated storm features for deterministic ensemble selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable
from urllib.parse import urlsplit

import geopandas as gpd
import numpy as np
import pandas as pd
import pystac
import xarray as xr
from pyproj import CRS, Geod
from rasterio.features import geometry_mask, shapes
from shapely.geometry import mapping, shape

from stormhub.met.storm_catalog import source_watershed_geometry
from stormhub.met.zarr_to_dss import (
    NOAADataVariable,
    calculate_shg_translation,
    clip_shg_data_to_geometry,
    get_noaa_data_for_dss,
    prepare_noaa_variable_for_dss,
    reproject_to_shg,
    translate_shg_data,
)
from stormhub.utils import sha256_file, sha256_multihash

FEATURE_TABLE_SCHEMA = "floodforecast/ensemble-feature-table/1.0"
FEATURE_CHECKPOINT_SCHEMA = "stormhub/ensemble-feature-checkpoint/1.0"
REQUIRED_MANIFEST_COLUMNS = {
    "item_id",
    "rank",
    "spatial_role",
    "start_datetime",
    "end_datetime",
    "duration_hours",
    "asset_key",
    "dss_href",
    "checksum",
    "target_watershed_id",
    "x_offset_m",
    "y_offset_m",
    "validation_status",
}

BASE_FEATURE_DEFINITIONS = (
    {
        "name": "accumulation_mean_mm",
        "category": "accumulation",
        "units": "mm",
        "description": "Mean event-total precipitation over finite target-grid cells.",
        "owner": "stormhub",
    },
    {
        "name": "accumulation_max_mm",
        "category": "accumulation",
        "units": "mm",
        "description": "Maximum event-total precipitation among finite target-grid cells.",
        "owner": "stormhub",
    },
    {
        "name": "maximum_hourly_mean_mm",
        "category": "intensity",
        "units": "mm",
        "description": "Maximum target-grid mean precipitation in one hourly interval.",
        "owner": "stormhub",
    },
    {
        "name": "precipitation_centroid_x_m",
        "category": "spatial_centroid",
        "units": "m",
        "description": "SHG x coordinate of the event-total precipitation-weighted centroid.",
        "owner": "stormhub",
    },
    {
        "name": "precipitation_centroid_y_m",
        "category": "spatial_centroid",
        "units": "m",
        "description": "SHG y coordinate of the event-total precipitation-weighted centroid.",
        "owner": "stormhub",
    },
    {
        "name": "maximum_precipitation_x_m",
        "category": "spatial_centroid",
        "units": "m",
        "description": "SHG x coordinate of the maximum event-total precipitation cell.",
        "owner": "stormhub",
    },
    {
        "name": "maximum_precipitation_y_m",
        "category": "spatial_centroid",
        "units": "m",
        "description": "SHG y coordinate of the maximum event-total precipitation cell.",
        "owner": "stormhub",
    },
    {
        "name": "spatial_accumulation_cv",
        "category": "spatial_distribution",
        "units": "ratio",
        "description": (
            "Population coefficient of variation of event-total precipitation over the configured spatial basis; "
            "target-zone values are weighted by their forcing-covered geodesic area."
        ),
        "owner": "stormhub",
    },
    {
        "name": "peak_time_fraction",
        "category": "temporal_shape",
        "units": "fraction",
        "description": "Fraction of the event elapsed at the end of the maximum target-mean hourly interval.",
        "owner": "stormhub",
    },
    {
        "name": "cumulative_fraction_at_25pct",
        "category": "temporal_shape",
        "units": "fraction",
        "description": "Fraction of target-mean event precipitation accumulated in the first quarter of the event.",
        "owner": "stormhub",
    },
    {
        "name": "cumulative_fraction_at_50pct",
        "category": "temporal_shape",
        "units": "fraction",
        "description": "Fraction of target-mean event precipitation accumulated in the first half of the event.",
        "owner": "stormhub",
    },
    {
        "name": "cumulative_fraction_at_75pct",
        "category": "temporal_shape",
        "units": "fraction",
        "description": "Fraction of target-mean event precipitation accumulated in the first three quarters of the event.",
        "owner": "stormhub",
    },
    {
        "name": "x_offset_m",
        "category": "transposition_offset",
        "units": "m",
        "description": "Signed source-to-target SHG x offset recorded on the target DSS asset.",
        "owner": "stormhub",
    },
    {
        "name": "y_offset_m",
        "category": "transposition_offset",
        "units": "m",
        "description": "Signed source-to-target SHG y offset recorded on the target DSS asset.",
        "owner": "stormhub",
    },
    {
        "name": "offset_distance_m",
        "category": "transposition_offset",
        "units": "m",
        "description": "Euclidean distance of the recorded source-to-target SHG offset.",
        "owner": "stormhub",
    },
)


def _finite_number(value: object, *, label: str) -> float:
    """Return a finite float or raise an actionable validation error."""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric, not {value!r}.") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be finite, not {value!r}.")
    return numeric


def _positive_int(value: object, *, label: str) -> int:
    """Return a strictly positive integer."""
    numeric = _finite_number(value, label=label)
    integer = int(numeric)
    if numeric != integer or integer <= 0:
        raise ValueError(f"{label} must be a positive integer, not {value!r}.")
    return integer


def _portable_relative_href(path: Path, base_dir: Path) -> str:
    """Return a POSIX relative href and reject nonportable results."""
    href = Path(os.path.relpath(path.resolve(), start=base_dir.resolve())).as_posix()
    parsed = urlsplit(href)
    if (
        parsed.scheme
        or parsed.netloc
        or PurePosixPath(href).is_absolute()
        or PureWindowsPath(href).is_absolute()
        or "\\" in href
    ):
        raise ValueError(f"Could not create a portable relative href for {path}.")
    return href


def _canonical_sha256(payload: dict, *, hash_key: str | None = None) -> str:
    """Hash canonical compact JSON, optionally omitting one top-level key."""
    content = dict(payload)
    if hash_key is not None:
        content.pop(hash_key, None)
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rounded(value: float) -> float:
    """Normalize computed feature precision for cross-run JSON stability."""
    return round(_finite_number(value, label="computed feature"), 8)


def _coefficient_of_variation(values: np.ndarray, *, label: str) -> float:
    """Calculate population coefficient of variation for finite values."""
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ValueError(f"{label} does not contain finite precipitation values.")
    mean = float(np.mean(finite))
    if mean <= 0:
        raise ValueError(f"{label} mean precipitation must be positive.")
    return float(np.std(finite, ddof=0) / mean)


def _weighted_coefficient_of_variation(
    values: np.ndarray,
    weights: np.ndarray,
    *,
    label: str,
) -> float:
    """Calculate a weighted population coefficient of variation."""
    finite_values = np.asarray(values, dtype=float)
    finite_weights = np.asarray(weights, dtype=float)
    if finite_values.ndim != 1 or finite_weights.ndim != 1:
        raise ValueError(f"{label} values and weights must be one-dimensional.")
    if finite_values.size == 0 or finite_values.size != finite_weights.size:
        raise ValueError(f"{label} values and weights must have the same non-zero length.")
    if not np.isfinite(finite_values).all():
        raise ValueError(f"{label} contains a non-finite precipitation value.")
    if not np.isfinite(finite_weights).all() or np.any(finite_weights <= 0):
        raise ValueError(f"{label} weights must be finite and positive.")
    mean = float(np.average(finite_values, weights=finite_weights))
    if mean <= 0:
        raise ValueError(f"{label} weighted mean precipitation must be positive.")
    variance = float(np.average((finite_values - mean) ** 2, weights=finite_weights))
    return float(math.sqrt(variance) / mean)


def _geodesic_zone_areas(zones: gpd.GeoDataFrame) -> np.ndarray:
    """Return positive WGS84 geodesic polygon areas in square metres."""
    for index, geometry in enumerate(zones.geometry):
        if geometry is None or geometry.is_empty:
            raise ValueError(f"Feature zone {index} has empty geometry.")
        source_area = float(geometry.area)
        if not geometry.is_valid or not math.isfinite(source_area) or source_area <= 0:
            raise ValueError(f"Feature zone {index} must have valid positive source geometry area.")
    geographic = zones.to_crs("EPSG:4326")
    geod = Geod(ellps="WGS84")
    areas = []
    for index, geometry in enumerate(geographic.geometry):
        if geometry is None or geometry.is_empty:
            raise ValueError(f"Feature zone {index} has empty geometry.")
        area, _ = geod.geometry_area_perimeter(geometry)
        absolute_area = abs(float(area))
        if not math.isfinite(absolute_area) or absolute_area <= 0:
            raise ValueError(f"Feature zone {index} must have finite positive geodesic area.")
        areas.append(absolute_area)
    return np.asarray(areas, dtype=float)


def _zone_identifiers(zones: gpd.GeoDataFrame) -> tuple[str, tuple[str, ...]]:
    """Return stable, unique zone identifiers for authenticated diagnostics."""
    for field in ("zone_id", "subbasin_id", "name", "id"):
        if field not in zones.columns:
            continue
        identifiers = tuple(str(value).strip() for value in zones[field])
        if all(identifiers) and len(set(identifiers)) == len(identifiers):
            return field, identifiers
    return "$feature_index", tuple(str(index) for index in range(len(zones)))


def _zonal_accumulations(
    event_total: xr.DataArray,
    zones: gpd.GeoDataFrame,
) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    """Return means and areas for zones intersecting finite forcing coverage."""
    if zones.empty:
        raise ValueError("Feature-zone geometry contains no records.")
    if zones.crs is None:
        raise ValueError("Feature-zone geometry must declare a CRS.")
    target_crs = event_total.rio.crs
    if target_crs is None:
        raise ValueError("Target precipitation grid must declare a CRS.")
    # Validate every supplied source geometry even when it is outside forcing
    # coverage. This keeps an excluded zone from hiding malformed evidence.
    _geodesic_zone_areas(zones)
    projected = zones.to_crs(target_crs)
    values = np.asarray(event_total.values, dtype=float)
    transform = event_total.rio.transform(recalc=True)
    finite_mask = np.isfinite(values)
    coverage_parts = [
        shape(geometry)
        for geometry, value in shapes(
            finite_mask.astype(np.uint8),
            mask=finite_mask,
            transform=transform,
        )
        if value == 1
    ]
    if not coverage_parts:
        raise ValueError("Target precipitation does not contain a finite spatial footprint.")
    coverage = coverage_parts[0]
    for part in coverage_parts[1:]:
        coverage = coverage.union(part)
    zone_means = []
    covered_geometries = []
    covered_indices = []
    for index, geometry in enumerate(projected.geometry):
        if geometry is None or geometry.is_empty:
            raise ValueError(f"Feature zone {index} has empty geometry.")
        covered_geometry = geometry.intersection(coverage)
        if covered_geometry.is_empty or covered_geometry.area <= 0:
            continue
        mask = geometry_mask(
            [mapping(covered_geometry)],
            out_shape=values.shape,
            transform=transform,
            all_touched=True,
            invert=True,
        )
        selected = values[mask & np.isfinite(values)]
        if selected.size == 0:
            continue
        zone_means.append(float(np.mean(selected)))
        covered_geometries.append(covered_geometry)
        covered_indices.append(index)
    if len(zone_means) < 2:
        raise ValueError("At least two feature zones must intersect finite target precipitation.")
    covered = gpd.GeoDataFrame(
        geometry=covered_geometries,
        crs=target_crs,
    )
    zone_areas = _geodesic_zone_areas(covered)
    return np.asarray(zone_means, dtype=float), zone_areas, tuple(covered_indices)


def _compute_precipitation_features(
    precipitation: xr.DataArray,
    *,
    zones: gpd.GeoDataFrame | None = None,
) -> tuple[dict[str, float], tuple[int, ...] | None]:
    """Derive deterministic accumulation, intensity, spatial, and temporal metrics.

    ``precipitation`` must be an hourly target-location grid with one time and
    two rioxarray spatial dimensions. When ``zones`` is supplied, the spatial
    coefficient of variation is calculated across zone-average event totals
    using geodesic polygon-area weights; otherwise it is calculated across
    finite target-grid cells.
    """
    if "time" not in precipitation.dims:
        raise ValueError("Target precipitation must contain a time dimension.")
    try:
        x_dim = precipitation.rio.x_dim
        y_dim = precipitation.rio.y_dim
    except Exception as exc:
        raise ValueError("Target precipitation must declare rioxarray spatial dimensions.") from exc
    if precipitation.sizes["time"] <= 0:
        raise ValueError("Target precipitation must contain at least one time step.")

    ordered = precipitation.transpose("time", y_dim, x_dim).astype(float).compute()
    raw = np.asarray(ordered.values, dtype=float)
    finite = raw[np.isfinite(raw)]
    if finite.size == 0:
        raise ValueError("Target precipitation does not contain finite values.")
    if np.any(finite < 0):
        raise ValueError("Target precipitation contains negative values.")
    if any(not np.isfinite(raw[index]).any() for index in range(raw.shape[0])):
        raise ValueError("Every target precipitation time step must contain finite values.")

    finite_mask = np.isfinite(raw)
    cells_with_any_data = finite_mask.any(axis=0)
    valid_cell = finite_mask.all(axis=0)
    if np.any(cells_with_any_data != valid_cell):
        raise ValueError("Target precipitation contains a spatial cell with incomplete time coverage.")
    event_total_values = np.nansum(raw, axis=0)
    event_total_values[~valid_cell] = np.nan
    event_total = xr.DataArray(
        event_total_values,
        dims=(y_dim, x_dim),
        coords={y_dim: ordered[y_dim], x_dim: ordered[x_dim]},
    )
    event_total.rio.set_spatial_dims(x_dim=x_dim, y_dim=y_dim, inplace=True)
    event_total.rio.write_crs(ordered.rio.crs, inplace=True)
    event_total.rio.write_transform(ordered.rio.transform(recalc=True), inplace=True)

    cell_totals = event_total_values[np.isfinite(event_total_values)]
    total_weight = float(np.sum(cell_totals))
    if total_weight <= 0:
        raise ValueError("Target precipitation event total must be positive.")

    x_values = np.asarray(ordered[x_dim].values, dtype=float)
    y_values = np.asarray(ordered[y_dim].values, dtype=float)
    x_grid, y_grid = np.meshgrid(x_values, y_values)
    weights = np.where(np.isfinite(event_total_values), event_total_values, 0.0)
    centroid_x = float(np.sum(x_grid * weights) / total_weight)
    centroid_y = float(np.sum(y_grid * weights) / total_weight)
    maximum_index = np.unravel_index(np.nanargmax(event_total_values), event_total_values.shape)

    hourly_means = np.mean(raw[:, valid_cell], axis=1)
    event_mean_total = float(np.sum(hourly_means))
    if event_mean_total <= 0:
        raise ValueError("Target-mean event precipitation must be positive.")
    cumulative = np.cumsum(hourly_means) / event_mean_total
    time_steps = len(hourly_means)

    def cumulative_at(fraction: float) -> float:
        index = max(0, math.ceil(time_steps * fraction) - 1)
        return float(cumulative[index])

    if zones is None:
        spatial_cv = _coefficient_of_variation(cell_totals, label="spatial accumulation")
        covered_zone_indices = None
    else:
        spatial_values, spatial_weights, covered_zone_indices = _zonal_accumulations(event_total, zones)
        spatial_cv = _weighted_coefficient_of_variation(
            spatial_values,
            spatial_weights,
            label="spatial accumulation",
        )
    return {
        "accumulation_mean_mm": _rounded(float(np.mean(cell_totals))),
        "accumulation_max_mm": _rounded(float(np.max(cell_totals))),
        "maximum_hourly_mean_mm": _rounded(float(np.max(hourly_means))),
        "precipitation_centroid_x_m": _rounded(centroid_x),
        "precipitation_centroid_y_m": _rounded(centroid_y),
        "maximum_precipitation_x_m": _rounded(float(x_grid[maximum_index])),
        "maximum_precipitation_y_m": _rounded(float(y_grid[maximum_index])),
        "spatial_accumulation_cv": _rounded(spatial_cv),
        "peak_time_fraction": _rounded((int(np.argmax(hourly_means)) + 1) / time_steps),
        "cumulative_fraction_at_25pct": _rounded(cumulative_at(0.25)),
        "cumulative_fraction_at_50pct": _rounded(cumulative_at(0.50)),
        "cumulative_fraction_at_75pct": _rounded(cumulative_at(0.75)),
    }, covered_zone_indices


def compute_precipitation_features(
    precipitation: xr.DataArray,
    *,
    zones: gpd.GeoDataFrame | None = None,
) -> dict[str, float]:
    """Derive deterministic precipitation features for a target forcing cube."""
    features, _ = _compute_precipitation_features(precipitation, zones=zones)
    return features


def _catalog_item(catalog: pystac.Catalog, item_id: str) -> pystac.Item:
    """Resolve one exact STAC Item identity from a catalog."""
    matches = [item for item in catalog.get_all_items() if item.id == item_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one catalog Item '{item_id}', found {len(matches)}.")
    return matches[0]


def _events_collection(catalog: pystac.Catalog, collection_id: str) -> pystac.Collection:
    """Resolve one exact events collection without heuristic selection."""
    matches = [collection for collection in catalog.get_all_collections() if collection.id == collection_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one collection '{collection_id}', found {len(matches)}.")
    if "-events" not in matches[0].id:
        raise ValueError(f"Collection '{collection_id}' is not an events collection.")
    return matches[0]


def _derive_target_precipitation(
    catalog: pystac.Catalog,
    item: pystac.Item,
    target_asset: pystac.Asset,
) -> tuple[xr.DataArray, dict]:
    """Reconstruct an item's target AORC cube from its authenticated STAC metadata."""
    metadata = target_asset.extra_fields
    source_domain_id = str(metadata.get("stormhub:source_domain_id", "")).strip()
    target_watershed_id = str(metadata.get("stormhub:target_watershed_id", "")).strip()
    if not source_domain_id or not target_watershed_id:
        raise ValueError(f"Storm item '{item.id}' target DSS asset lacks source or target domain identity.")
    source_domain = _catalog_item(catalog, source_domain_id)
    watershed = _catalog_item(catalog, target_watershed_id)
    source_href = source_domain.get_self_href()
    if source_href is None:
        raise ValueError(f"Source domain Item '{source_domain_id}' has no self href.")

    duration_hours = _positive_int(metadata.get("stormhub:duration_hours"), label="target DSS duration")
    resolution_m = _positive_int(metadata.get("stormhub:output_resolution_m"), label="target DSS resolution")
    if resolution_m % 1000:
        raise ValueError("Target DSS resolution must be a whole number of kilometers for SHG derivation.")
    buffer_km = _finite_number(metadata.get("stormhub:target_buffer_km", 0), label="target DSS buffer")
    if buffer_km < 0:
        raise ValueError("Target DSS buffer cannot be negative.")

    start_text = item.properties.get("start_datetime")
    try:
        start = pd.Timestamp(start_text).to_pydatetime().replace(tzinfo=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Storm item '{item.id}' has invalid start_datetime {start_text!r}.") from exc
    target_geometry = shape(watershed.geometry)
    source_geometry = source_watershed_geometry(item, target_geometry)
    translation = calculate_shg_translation(source_geometry, target_geometry, resolution_m // 1000)
    for key in ("x_offset_m", "y_offset_m"):
        recorded = _finite_number(metadata.get(f"stormhub:{key}"), label=f"target DSS {key}")
        if recorded != translation[key]:
            raise ValueError(
                f"Storm item '{item.id}' recorded {key} {recorded:g} does not match derived {translation[key]:g}."
            )

    variable_map = {NOAADataVariable.APCP: duration_hours}
    source_dataset = get_noaa_data_for_dss(source_href, start, variable_map)
    source_precipitation = prepare_noaa_variable_for_dss(
        source_dataset,
        NOAADataVariable.APCP,
        start,
        duration_hours,
    )
    shg_source = reproject_to_shg(source_precipitation, resolution_m // 1000)
    translated = translate_shg_data(shg_source, translation["x_offset_m"], translation["y_offset_m"])
    target = clip_shg_data_to_geometry(translated, target_geometry, buffer_km=buffer_km)
    return target, translation


def _load_manifest(manifest_path: Path) -> pd.DataFrame:
    """Load and validate the authoritative passed target-DSS manifest rows."""
    try:
        manifest = pd.read_csv(manifest_path, dtype=str, keep_default_na=False)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"DSS manifest does not exist: {manifest_path}") from exc
    missing = REQUIRED_MANIFEST_COLUMNS - set(manifest.columns)
    if missing:
        raise ValueError(f"DSS manifest is missing columns: {', '.join(sorted(missing))}.")
    targets = manifest.loc[manifest["spatial_role"] == "target"].copy()
    if targets.empty:
        raise ValueError("DSS manifest contains no target rows.")
    if not (targets["validation_status"] == "passed").all():
        failed = sorted(targets.loc[targets["validation_status"] != "passed", "item_id"].tolist())
        raise ValueError(f"Every target DSS manifest row must have passed validation; failed: {failed}.")
    if targets["item_id"].duplicated().any():
        duplicates = sorted(targets.loc[targets["item_id"].duplicated(keep=False), "item_id"].unique())
        raise ValueError(f"DSS manifest contains duplicate target rows for Items: {duplicates}.")
    targets["_rank"] = targets["rank"].map(lambda value: _positive_int(value, label="manifest rank"))
    if targets["_rank"].duplicated().any():
        raise ValueError("DSS manifest target ranks must be unique.")
    if targets["dss_href"].duplicated().any():
        raise ValueError("DSS manifest target hrefs must be unique.")
    return targets.sort_values(["_rank", "item_id"])


def _asset_path(asset: pystac.Asset, *, label: str) -> Path:
    """Resolve a local STAC Asset path."""
    href = asset.get_absolute_href()
    if href is None:
        raise ValueError(f"{label} must resolve to a local file.")
    parsed = urlsplit(href)
    is_windows_drive = len(parsed.scheme) == 1 and len(href) >= 3 and href[1] == ":"
    if parsed.scheme not in {"", "file"} and not is_windows_drive:
        raise ValueError(f"{label} must resolve to a local file.")
    return Path(parsed.path if parsed.scheme == "file" else href).resolve()


def _verify_target_asset(
    item: pystac.Item,
    row: pd.Series,
    *,
    collection_dir: Path,
) -> tuple[pystac.Asset, Path]:
    """Bind one manifest row to its STAC target asset and local file content."""
    asset_key = row["asset_key"]
    if asset_key not in item.assets:
        raise ValueError(f"Storm item '{item.id}' does not contain manifest asset '{asset_key}'.")
    asset = item.assets[asset_key]
    if asset.extra_fields.get("stormhub:spatial_role") != "target":
        raise ValueError(f"Storm item '{item.id}' manifest asset is not a target DSS product.")
    if asset.extra_fields.get("stormhub:validation_status") != "passed":
        raise ValueError(f"Storm item '{item.id}' target DSS asset has not passed validation.")
    asset_path = _asset_path(asset, label=f"Storm item '{item.id}' target DSS asset")
    manifest_href = row["dss_href"]
    if (
        PurePosixPath(manifest_href).is_absolute()
        or PureWindowsPath(manifest_href).is_absolute()
        or "\\" in manifest_href
    ):
        raise ValueError(f"Storm item '{item.id}' manifest dss_href must be portable and relative.")
    manifest_asset_path = (collection_dir / Path(*PurePosixPath(manifest_href).parts)).resolve()
    if manifest_asset_path != asset_path:
        raise ValueError(f"Storm item '{item.id}' manifest and STAC target DSS hrefs do not match.")
    if not asset_path.is_file():
        raise FileNotFoundError(f"Storm item '{item.id}' target DSS file does not exist: {asset_path}")
    checksum = str(asset.extra_fields.get("file:checksum", "")).lower()
    if row["checksum"].lower() != checksum:
        raise ValueError(f"Storm item '{item.id}' manifest and STAC target DSS checksums do not match.")
    actual_checksum = sha256_multihash(sha256_file(asset_path))
    if checksum != actual_checksum:
        raise ValueError(f"Storm item '{item.id}' target DSS checksum does not match file content.")
    recorded_size = _positive_int(asset.extra_fields.get("file:size"), label="target DSS file:size")
    if recorded_size != asset_path.stat().st_size:
        raise ValueError(f"Storm item '{item.id}' target DSS byte size does not match file content.")
    for key in ("x_offset_m", "y_offset_m"):
        manifest_value = _finite_number(row[key], label=f"manifest {key}")
        asset_value = _finite_number(asset.extra_fields.get(f"stormhub:{key}"), label=f"target DSS {key}")
        if manifest_value != asset_value:
            raise ValueError(f"Storm item '{item.id}' manifest and STAC target DSS {key} values do not match.")
    return asset, asset_path


def _forcing_record(item: pystac.Item, asset: pystac.Asset, duration_hours: int) -> dict:
    """Build the versioned forcing identity for one candidate."""
    start = str(item.properties.get("start_datetime", ""))
    end = str(item.properties.get("end_datetime", ""))
    start_timestamp = pd.Timestamp(start)
    end_timestamp = pd.Timestamp(end)
    if start_timestamp.tzinfo is None or end_timestamp.tzinfo is None:
        raise ValueError(f"Storm item '{item.id}' forcing window must be timezone-aware.")
    actual_hours = (end_timestamp - start_timestamp).total_seconds() / 3600
    if actual_hours != duration_hours:
        raise ValueError(f"Storm item '{item.id}' forcing window is {actual_hours:g} hours, expected {duration_hours}.")
    interval = str(asset.extra_fields.get("stormhub:time_step", "")).strip()
    time_zone = str(asset.extra_fields.get("stormhub:time_zone", "")).strip()
    if not interval or not time_zone:
        raise ValueError(f"Storm item '{item.id}' target DSS interval and time zone must be non-empty.")
    if interval != "PT1H" or time_zone != "UTC":
        raise ValueError(f"Storm item '{item.id}' target DSS forcing must use hourly UTC intervals.")
    return {
        "start": start,
        "end": end,
        "interval": interval,
        "time_zone": time_zone,
        "duration_hours": duration_hours,
        "units": "mm",
    }


def _grid_record(item: pystac.Item, asset: pystac.Asset, watershed_id: str) -> dict:
    """Build and validate target-grid identity from one DSS asset."""
    metadata = asset.extra_fields
    if str(metadata.get("stormhub:target_watershed_id")) != watershed_id:
        raise ValueError(f"Storm item '{item.id}' target watershed does not match table watershed.")
    shape_value = metadata.get("proj:shape")
    bbox_value = metadata.get("proj:bbox")
    if not isinstance(shape_value, list) or len(shape_value) != 2:
        raise ValueError(f"Storm item '{item.id}' target DSS proj:shape must contain two values.")
    if not isinstance(bbox_value, list) or len(bbox_value) != 4:
        raise ValueError(f"Storm item '{item.id}' target DSS proj:bbox must contain four values.")
    grid_shape = [_positive_int(value, label="target grid shape") for value in shape_value]
    grid_bbox = [_finite_number(value, label="target grid bbox") for value in bbox_value]
    if grid_bbox[0] >= grid_bbox[2] or grid_bbox[1] >= grid_bbox[3]:
        raise ValueError(f"Storm item '{item.id}' target DSS proj:bbox is not ordered.")
    crs = str(metadata.get("proj:code", "")).strip()
    if not crs:
        raise ValueError(f"Storm item '{item.id}' target DSS CRS must be non-empty.")
    return {
        "crs": crs,
        "shape": grid_shape,
        "bbox": grid_bbox,
        "resolution_m": _positive_int(
            metadata.get("stormhub:output_resolution_m"),
            label="target grid resolution",
        ),
        "target_watershed_id": watershed_id,
    }


def _verify_precipitation_cube(
    item: pystac.Item,
    precipitation: xr.DataArray,
    grid: dict,
    duration_hours: int,
) -> None:
    """Verify derived precipitation matches authenticated time and grid metadata."""
    try:
        x_dim = precipitation.rio.x_dim
        y_dim = precipitation.rio.y_dim
        crs = precipitation.rio.crs
        bounds = [float(value) for value in precipitation.rio.bounds()]
        resolution = [abs(float(value)) for value in precipitation.rio.resolution()]
    except Exception as exc:
        raise ValueError(f"Storm item '{item.id}' derived precipitation has incomplete spatial metadata.") from exc
    actual_shape = [int(precipitation.sizes[y_dim]), int(precipitation.sizes[x_dim])]
    if actual_shape != grid["shape"]:
        raise ValueError(f"Storm item '{item.id}' derived grid shape does not match target DSS metadata.")
    if crs is None or not CRS.from_user_input(crs).equals(CRS.from_user_input(grid["crs"])):
        raise ValueError(f"Storm item '{item.id}' derived grid CRS does not match target DSS metadata.")
    # GDAL/PROJ patch revisions can shift a repeated geographic-to-SHG
    # projection by a sub-metre amount without changing cell membership.
    # Bound that repeatability allowance to 0.1% of a cell and at most 0.5 m.
    bounds_tolerance_m = min(float(grid["resolution_m"]) * 1e-3, 0.5)
    if not np.allclose(bounds, grid["bbox"], rtol=0, atol=bounds_tolerance_m):
        raise ValueError(f"Storm item '{item.id}' derived grid bounds do not match target DSS metadata.")
    if not np.allclose(resolution, [grid["resolution_m"], grid["resolution_m"]], rtol=0, atol=1e-6):
        raise ValueError(f"Storm item '{item.id}' derived grid resolution does not match target DSS metadata.")
    if precipitation.sizes.get("time") != duration_hours:
        raise ValueError(f"Storm item '{item.id}' derived precipitation does not contain {duration_hours} hours.")
    actual_time = pd.DatetimeIndex(pd.to_datetime(precipitation["time"].values))
    if actual_time.tz is not None:
        actual_time = actual_time.tz_convert("UTC").tz_localize(None)
    start = pd.Timestamp(item.properties["start_datetime"])
    end = pd.Timestamp(item.properties["end_datetime"])
    expected_time = pd.date_range(start=start + pd.Timedelta(hours=1), end=end, freq="h")
    if expected_time.tz is not None:
        expected_time = expected_time.tz_convert("UTC").tz_localize(None)
    if not actual_time.equals(expected_time):
        raise ValueError(f"Storm item '{item.id}' derived precipitation time axis does not match its forcing window.")


def _write_table(output_path: Path, payload: dict) -> None:
    """Write an authenticated table idempotently and refuse changed replacement."""
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output_path.exists():
        current = output_path.read_text(encoding="utf-8")
        if current == rendered:
            return
        raise FileExistsError(f"Refusing to replace different ensemble feature table: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    _replace_with_retry(temporary, output_path)


def _replace_with_retry(source: Path, target: Path) -> None:
    """Replace a file despite brief Windows reader/antivirus locks."""
    for attempt in range(5):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (2**attempt))


def _checkpoint_identity(
    *,
    catalog_file: Path,
    collection_file: Path,
    collection_id: str,
    manifest_file: Path,
    output_file: Path,
    study_id: str,
    zones_file: Path | None,
) -> dict:
    """Authenticate result-defining inputs for a local resume checkpoint."""
    return {
        "feature_table_schema": FEATURE_TABLE_SCHEMA,
        "feature_algorithm": "covered-zone-area-v1",
        "collection_id": collection_id,
        "catalog_sha256": sha256_file(catalog_file),
        "collection_sha256": sha256_file(collection_file),
        "manifest_sha256": sha256_file(manifest_file),
        "zones_sha256": sha256_file(zones_file) if zones_file is not None else None,
        "study_id": study_id,
        "output_path": str(output_file),
    }


def _load_checkpoint(checkpoint_path: Path, *, identity: dict) -> tuple[list[dict], tuple[int, ...] | None]:
    """Load an authenticated checkpoint whose inputs exactly match this run."""
    if not checkpoint_path.exists():
        return [], None
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ensemble feature checkpoint JSON: {checkpoint_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != FEATURE_CHECKPOINT_SCHEMA:
        raise ValueError(f"Unsupported ensemble feature checkpoint: {checkpoint_path}")
    expected_hash = str(payload.get("checkpoint_sha256", ""))
    if expected_hash != _canonical_sha256(payload, hash_key="checkpoint_sha256"):
        raise ValueError(f"Ensemble feature checkpoint hash mismatch: {checkpoint_path}")
    if payload.get("identity") != identity:
        raise ValueError(f"Ensemble feature checkpoint inputs do not match this run: {checkpoint_path}")
    records = payload.get("records")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError(f"Ensemble feature checkpoint records are invalid: {checkpoint_path}")
    scenario_ids = [record.get("source_scenario_id") for record in records]
    if any(not isinstance(value, str) or not value for value in scenario_ids) or len(scenario_ids) != len(
        set(scenario_ids)
    ):
        raise ValueError(f"Ensemble feature checkpoint scenario identities are invalid: {checkpoint_path}")
    raw_coverage = payload.get("covered_zone_indices")
    if raw_coverage is None:
        coverage = None
    elif not isinstance(raw_coverage, list) or any(
        not isinstance(index, int) or index < 0 for index in raw_coverage
    ):
        raise ValueError(f"Ensemble feature checkpoint coverage is invalid: {checkpoint_path}")
    else:
        coverage = tuple(raw_coverage)
    return records, coverage


def _write_checkpoint(
    checkpoint_path: Path,
    *,
    identity: dict,
    records: list[dict],
    covered_zone_indices: tuple[int, ...] | None,
) -> None:
    """Atomically replace a local checkpoint after one completed candidate."""
    payload = {
        "schema": FEATURE_CHECKPOINT_SCHEMA,
        "identity": identity,
        "covered_zone_indices": list(covered_zone_indices) if covered_zone_indices is not None else None,
        "records": records,
    }
    payload["checkpoint_sha256"] = _canonical_sha256(payload)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_name(f".{checkpoint_path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _replace_with_retry(temporary, checkpoint_path)


def export_ensemble_feature_table(
    catalog_path: str | Path,
    *,
    collection_id: str,
    manifest_path: str | Path,
    study_id: str,
    output_path: str | Path,
    zones_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    precipitation_provider: Callable[[pystac.Catalog, pystac.Item, pystac.Asset], tuple[xr.DataArray, dict]]
    | None = None,
) -> dict:
    """Derive and write a checksum-pinned FloodForecast ensemble feature table.

    The explicit DSS manifest is authoritative. Every collection Item must map
    to exactly one passed target row whose STAC asset checksum matches the local
    DSS content. ``precipitation_provider`` exists for deterministic tests; the
    production default reconstructs the translated target AORC grid.
    """
    catalog_file = Path(catalog_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    output_file = Path(output_path).resolve()
    checkpoint_file = Path(checkpoint_path).resolve() if checkpoint_path is not None else None
    if not study_id.strip():
        raise ValueError("study_id must be non-empty.")
    if not catalog_file.is_file():
        raise FileNotFoundError(f"STAC catalog does not exist: {catalog_file}")

    catalog = pystac.read_file(str(catalog_file))
    if not isinstance(catalog, pystac.Catalog):
        raise ValueError(f"Expected a STAC Catalog at {catalog_file}.")
    collection = _events_collection(catalog, collection_id)
    collection_href = collection.get_self_href()
    if collection_href is None:
        raise ValueError(f"Collection '{collection_id}' must have a self href.")
    collection_file = Path(collection_href).resolve()
    collection_dir = collection_file.parent
    manifest_asset = collection.assets.get("dss_manifest")
    if manifest_asset is None:
        raise ValueError(f"Collection '{collection_id}' does not contain a dss_manifest asset.")
    if _asset_path(manifest_asset, label="Collection DSS manifest") != manifest_file:
        raise ValueError("Explicit manifest path does not match the collection dss_manifest asset.")

    targets = _load_manifest(manifest_file)
    items = {item.id: item for item in collection.get_items()}
    if len(items) != len(list(collection.get_items())):
        raise ValueError(f"Collection '{collection_id}' contains duplicate Item IDs.")
    manifest_ids = set(targets["item_id"])
    if manifest_ids != set(items):
        missing = sorted(set(items) - manifest_ids)
        unexpected = sorted(manifest_ids - set(items))
        raise ValueError(
            "Target DSS manifest must cover the collection exactly; "
            f"missing Items: {missing}; unexpected Items: {unexpected}."
        )

    zones = None
    zones_file = None
    if zones_path is not None:
        zones_file = Path(zones_path).resolve()
        if not zones_file.is_file():
            raise FileNotFoundError(f"Feature-zone geometry does not exist: {zones_file}")
        if zones_file.suffix.lower() not in {".geojson", ".json", ".gpkg"}:
            raise ValueError("Feature zones must use a checksum-complete GeoJSON or GeoPackage file.")
        zones = gpd.read_file(zones_file)
        if zones.empty:
            raise ValueError("Feature-zone geometry contains no records.")

    provider = precipitation_provider or _derive_target_precipitation
    output_dir = output_file.parent
    checkpoint_identity = _checkpoint_identity(
        catalog_file=catalog_file,
        collection_file=collection_file,
        collection_id=collection_id,
        manifest_file=manifest_file,
        output_file=output_file,
        study_id=study_id.strip(),
        zones_file=zones_file,
    )
    if checkpoint_file is None:
        checkpoint_records = []
        legacy_covered_zone_indices = None
    else:
        checkpoint_records, legacy_covered_zone_indices = _load_checkpoint(
            checkpoint_file,
            identity=checkpoint_identity,
        )
    zone_id_field = None
    zone_ids = None
    if zones is not None:
        zone_id_field, zone_ids = _zone_identifiers(zones)
        if checkpoint_records and any(
            "spatial_distribution_coverage" not in record for record in checkpoint_records
        ):
            if legacy_covered_zone_indices is None:
                raise ValueError("Ensemble feature checkpoint lacks target-zone coverage metadata.")
            legacy_excluded = sorted(set(range(len(zones))) - set(legacy_covered_zone_indices))
            for record in checkpoint_records:
                record.setdefault(
                    "spatial_distribution_coverage",
                    {
                        "covered_zone_count": len(legacy_covered_zone_indices),
                        "excluded_zero_coverage_zone_ids": [zone_ids[index] for index in legacy_excluded],
                    },
                )
    checkpoint_by_id = {record["source_scenario_id"]: record for record in checkpoint_records}
    if set(checkpoint_by_id) - set(items):
        raise ValueError("Ensemble feature checkpoint contains a scenario outside the collection.")
    records = []
    table_duration = None
    table_watershed = None
    for position, (_, row) in enumerate(targets.iterrows(), start=1):
        item = items[row["item_id"]]
        item_href = item.get_self_href()
        if item_href is None:
            raise ValueError(f"Storm item '{item.id}' must have a self href.")
        item_file = Path(item_href).resolve()
        if not item_file.is_file():
            raise FileNotFoundError(f"Storm item file does not exist: {item_file}")
        target_asset, target_path = _verify_target_asset(item, row, collection_dir=collection_dir)

        if row["start_datetime"] != str(item.properties.get("start_datetime", "")):
            raise ValueError(f"Storm item '{item.id}' manifest and STAC start datetimes do not match.")
        if row["end_datetime"] != str(item.properties.get("end_datetime", "")):
            raise ValueError(f"Storm item '{item.id}' manifest and STAC end datetimes do not match.")

        duration_hours = _positive_int(row["duration_hours"], label="manifest duration_hours")
        asset_duration = _positive_int(
            target_asset.extra_fields.get("stormhub:duration_hours"),
            label="target DSS duration_hours",
        )
        if duration_hours != asset_duration:
            raise ValueError(f"Storm item '{item.id}' manifest and asset durations do not match.")
        watershed_id = str(row["target_watershed_id"]).strip()
        if not watershed_id:
            raise ValueError(f"Storm item '{item.id}' target watershed identity is empty.")
        if table_duration is None:
            table_duration = duration_hours
            table_watershed = watershed_id
        if duration_hours != table_duration or watershed_id != table_watershed:
            raise ValueError("All feature-table candidates must share one duration and target watershed.")

        checkpoint_record = checkpoint_by_id.get(item.id)
        if checkpoint_record is not None:
            expected_rank = int(row["_rank"])
            if (
                checkpoint_record.get("rank") != expected_rank
                or checkpoint_record.get("stac_item_id") != item.id
                or checkpoint_record.get("stac_item_sha256") != sha256_file(item_file)
                or checkpoint_record.get("target_dss", {}).get("checksum")
                != target_asset.extra_fields["file:checksum"]
                or checkpoint_record.get("target_dss", {}).get("bytes") != target_path.stat().st_size
            ):
                raise ValueError(f"Ensemble feature checkpoint record for '{item.id}' is stale.")
            records.append(checkpoint_record)
            logging.info("Resumed ensemble feature %s/%s for item %s", position, len(targets), item.id)
            continue

        precipitation, translation = provider(catalog, item, target_asset)
        grid = _grid_record(item, target_asset, watershed_id)
        _verify_precipitation_cube(item, precipitation, grid, duration_hours)
        features, covered_zone_indices = _compute_precipitation_features(precipitation, zones=zones)
        x_offset = _finite_number(row["x_offset_m"], label="manifest x_offset_m")
        y_offset = _finite_number(row["y_offset_m"], label="manifest y_offset_m")
        for key, value in (("x_offset_m", x_offset), ("y_offset_m", y_offset)):
            derived = _finite_number(translation[key], label=f"derived {key}")
            if derived != value:
                raise ValueError(f"Storm item '{item.id}' manifest and derived {key} do not match.")
        features.update(
            {
                "x_offset_m": _rounded(x_offset),
                "y_offset_m": _rounded(y_offset),
                "offset_distance_m": _rounded(math.hypot(x_offset, y_offset)),
            }
        )
        record = {
            "source_scenario_id": item.id,
            "rank": int(row["_rank"]),
            "stac_item_id": item.id,
            "stac_item_href": _portable_relative_href(item_file, output_dir),
            "stac_item_sha256": sha256_file(item_file),
            "target_dss": {
                "href": _portable_relative_href(target_path, output_dir),
                "checksum": target_asset.extra_fields["file:checksum"],
                "bytes": target_path.stat().st_size,
                "validation_status": "passed",
            },
            "forcing": _forcing_record(item, target_asset, duration_hours),
            "grid": grid,
            "features": features,
        }
        if zones is not None:
            excluded_zone_indices = sorted(set(range(len(zones))) - set(covered_zone_indices))
            record["spatial_distribution_coverage"] = {
                "covered_zone_count": len(covered_zone_indices),
                "excluded_zero_coverage_zone_ids": [zone_ids[index] for index in excluded_zone_indices],
            }
        records.append(record)
        if checkpoint_file is not None:
            _write_checkpoint(
                checkpoint_file,
                identity=checkpoint_identity,
                records=records,
                covered_zone_indices=None,
            )
        logging.info("Completed ensemble feature %s/%s for item %s", position, len(targets), item.id)

    source = {
        "owner_repository": "stormhub",
        "catalog_href": _portable_relative_href(collection_file, output_dir),
        "catalog_sha256": sha256_file(collection_file),
        "manifest_href": _portable_relative_href(manifest_file, output_dir),
        "manifest_sha256": sha256_file(manifest_file),
        "root_catalog_href": _portable_relative_href(catalog_file, output_dir),
        "root_catalog_sha256": sha256_file(catalog_file),
    }
    spatial_basis = {
        "type": "target_grid_cells",
        "statistic": "population-cv-of-cell-event-accumulation",
        "weighting": "equal-cell",
    }
    if zones_file is not None:
        coverage_records = [record["spatial_distribution_coverage"] for record in records]
        covered_counts = [record["covered_zone_count"] for record in coverage_records]
        excluded_sets = [set(record["excluded_zero_coverage_zone_ids"]) for record in coverage_records]
        always_excluded = set.intersection(*excluded_sets)
        ever_excluded = set.union(*excluded_sets)
        source["zones_href"] = _portable_relative_href(zones_file, output_dir)
        source["zones_sha256"] = sha256_file(zones_file)
        spatial_basis = {
            "type": "target_zones",
            "zone_count": len(zones),
            "covered_zone_count_min": min(covered_counts),
            "covered_zone_count_max": max(covered_counts),
            "always_excluded_zero_coverage_zone_ids": sorted(always_excluded),
            "variably_excluded_zero_coverage_zone_ids": sorted(ever_excluded - always_excluded),
            "zone_id_field": zone_id_field,
            "statistic": "area-weighted-population-cv-of-covered-zone-mean-event-accumulation",
            "weighting": "forcing-covered-polygon-area",
            "area_method": "WGS84-geodesic",
            "coverage_method": "per-event-finite-target-grid-cell-footprint",
            "zone_mean_method": "equal-finite-grid-cell",
            "metric_definition_status": "provisional",
        }

    payload = {
        "schema": FEATURE_TABLE_SCHEMA,
        "study_id": study_id.strip(),
        "duration_hours": table_duration,
        "watershed_id": table_watershed,
        "candidate_count": len(records),
        "source": source,
        "spatial_distribution_basis": spatial_basis,
        "feature_definitions": list(BASE_FEATURE_DEFINITIONS),
        "records": records,
    }
    payload["table_sha256"] = _canonical_sha256(payload)
    _write_table(output_file, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    """Build the ensemble-feature export command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Derive an authenticated StormHub precipitation-feature table from "
            "an explicit STAC events collection and target-DSS manifest."
        )
    )
    parser.add_argument("catalog", help="Path to the root STAC catalog.json.")
    parser.add_argument("--collection-id", required=True, help="Exact events collection ID to export.")
    parser.add_argument("--manifest", required=True, help="Path to the collection dss-manifest.csv.")
    parser.add_argument("--study-id", required=True, help="Consumer study identity recorded in the table.")
    parser.add_argument("--output", required=True, help="New JSON feature-table path.")
    parser.add_argument(
        "--checkpoint",
        help="Optional local JSON checkpoint. Matching reruns resume completed candidates.",
    )
    parser.add_argument(
        "--zones",
        help=(
            "Optional target-zone vector dataset. When provided, spatial_accumulation_cv "
            "is calculated across covered-zone-average event totals using WGS84 geodesic "
            "forcing-covered polygon-area weights instead of grid cells."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the installed ensemble-feature export command."""
    args = build_parser().parse_args(argv)
    payload = export_ensemble_feature_table(
        args.catalog,
        collection_id=args.collection_id,
        manifest_path=args.manifest,
        study_id=args.study_id,
        output_path=args.output,
        zones_path=args.zones,
        checkpoint_path=args.checkpoint,
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(Path(args.output).resolve()),
                "candidate_count": payload["candidate_count"],
                "table_sha256": payload["table_sha256"],
            },
            indent=2,
        )
    )
    return 0
