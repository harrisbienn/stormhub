"""Decode hourly HRRR APCP and conservatively aggregate onto explicit supports.

Native readers and geospatial calculations stay in StormHub. This research
adapter treats APCP as liquid-water-equivalent precipitation; it does not model
snow storage, melt, antecedent conditions or hydrologic initial state.
"""

from __future__ import annotations

from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

from .hrrr_acquisition import descriptor, validate_acquisition, verify_asset, write_immutable
from .precipitation_adapters import extractor_identity
from .precipitation_features import (
    NORMALIZED_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    extract_features,
    utc,
    validate_grid,
    validate_support,
    write_features,
)

from .precipitation_remapping import conservative_weights, remap_interval, source_grid

FORCING_CONTRACT = "stormhub/forcing-dataset/0.1.0"


def validate_hour_tags(tags: dict, initialization: str, hour: int) -> dict:
    """Require GRIB2 template 4.8 surface APCP with one exact hourly accumulation.

    GDAL's assembled template values retain the interval-start lead and duration,
    unlike the display label. Cumulative fields are never differenced here.
    """
    cycle = utc(initialization)
    end = cycle + timedelta(hours=hour)
    try:
        pds = [int(v) for v in tags["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"].split()]
        reference = int(tags["GRIB_REF_TIME"])
        valid = int(tags["GRIB_VALID_TIME"])
        lead = int(tags["GRIB_FORECAST_SECONDS"])
    except (KeyError, ValueError) as exc:
        raise PrecipitationFeatureError("GRIB metadata lacks decoded interval fields") from exc
    expected = {
        0: 1,
        1: 8,
        2: 2,
        4: 83,
        7: 1,
        8: hour - 1,
        9: 1,
        10: 0,
        11: 0,
        12: 255,
        21: 1,
        22: 0,
        23: 1,
        24: 2,
        25: 1,
        26: 1,
    }
    if (
        tags.get("GRIB_PDS_PDTN") != "8"
        or len(pds) != 29
        or any(pds[i] != value for i, value in expected.items())
        or tags.get("GRIB_UNIT") != "[kg/(m^2)]"
        or not tags.get("GRIB_DISCIPLINE", "").startswith("0(")
        or "CENTER=7(" not in tags.get("GRIB_IDS", "")
        or "TYPE=1(" not in tags.get("GRIB_IDS", "")
        or reference != int(cycle.timestamp())
        or valid != int(end.timestamp())
        or lead != (hour - 1) * 3600
        or pds[15:21] != [end.year, end.month, end.day, end.hour, 0, 0]
    ):
        raise PrecipitationFeatureError("GRIB is not the requested HRRR hourly surface APCP interval")
    return {
        "start": (end - timedelta(hours=1)).isoformat(),
        "end": end.isoformat(),
        "units": "kg m-2",
        "measurement": "interval-depth",
        "template": 8,
        "generating_process": 83,
        "statistical_process": "accumulation",
        "duration_seconds": 3600,
    }


def normalize(
    acquisition: dict, raw_root: Path, *, grid: dict, support: dict, output: Path, revision: str, dirty: bool = False
) -> dict:
    """Write immutable normalized rain, weights, shared features and forcing receipt."""
    import rasterio

    validate_acquisition(acquisition, raw_root)
    validate_grid(grid)
    validate_support(support, grid)
    indices = sorted({c["index"] for region in support["regions"] for c in region["cells"]})
    request = acquisition["request"]
    normalized = {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": grid,
        "event_start": request["initialization"],
        "event_end": (utc(request["initialization"]) + timedelta(hours=request["hours"])).isoformat(),
        "interval_minutes": 60,
        "units": "mm",
        "measurement": "interval-depth",
        "values": [],
    }
    intervals = []
    weights = None
    for record in acquisition["records"]:
        path = verify_asset(raw_root, record["message"])
        with rasterio.open(path) as dataset:
            if dataset.crs is None or not dataset.crs.is_projected:
                raise PrecipitationFeatureError("HRRR requires its projected native grid")
            metadata = validate_hour_tags(dataset.tags(1), request["initialization"], record["hour"])
            if weights is None:
                weights = conservative_weights(dataset, grid, indices)
            values, audit = remap_interval(dataset, weights, grid)
            normalized["values"].append(values)
            intervals.append(
                {"hour": record["hour"], "metadata": metadata, "grib_tags": dataset.tags(1), "volume_check": audit}
            )
    output.mkdir(parents=True, exist_ok=True)
    producer = {
        "name": "stormhub.hrrr-forcing",
        "version": "0.1.0",
        "revision": revision,
        "dirty": dirty,
        "code_sha256": digest(
            {
                name: (Path(__file__).parent / name).read_text(encoding="utf-8")
                for name in ("hrrr_acquisition.py", "hrrr_normalization.py", "precipitation_remapping.py")
            }
        ),
        "dependencies": {name: version(name) for name in ("rasterio", "numpy", "pyproj", "shapely")},
        "gdal": rasterio.__gdal_version__,
    }
    paths = {}
    for name, value in (
        ("normalized.json", normalized),
        ("weights.json", weights),
        ("support.json", support),
        ("acquisition.json", acquisition),
    ):
        path = output / name
        write_immutable(path, canonical_bytes(value))
        paths[name] = descriptor(path, output)
    forcing_identity = digest(
        {
            "acquisition": acquisition["acquisition_id"],
            "normalized": paths["normalized.json"],
            "support": digest(support),
            "producer": producer,
        }
    )
    source = {
        "collection_id": "hrrr-conus-hourly-apcp",
        "item_id": forcing_identity,
        "asset_key": "normalized-precipitation",
        "role": "incoming-precipitation",
        "sha256": paths["normalized.json"]["sha256"],
        "size_bytes": paths["normalized.json"]["size_bytes"],
    }
    features = extract_features(
        normalized, source=source, support=support, extractor=extractor_identity(revision, dirty=dirty)
    )
    feature_path = write_features(features, output)
    receipt = {
        "contract_version": FORCING_CONTRACT,
        "forcing_id": forcing_identity,
        "request": request,
        "acquisition": paths["acquisition.json"],
        "producer": producer,
        "normalized": paths["normalized.json"],
        "weights": paths["weights.json"],
        "support": paths["support.json"],
        "features": descriptor(feature_path, output),
        "feature_id": features["feature_id"],
        "intervals": intervals,
        "selected_target_cells": len(indices),
        "outside_support": "null-not-consumed",
        "availability": {
            "status": "archive-retrieval-only",
            "original_publication_time": None,
            "first_observed_at": min(r["observed_at"] for r in acquisition["records"]),
            "complete_observed_at": max(r["observed_at"] for r in acquisition["records"]),
        },
        "applicability": {
            "scope": "research-future-rainfall-only",
            "forecast_eligible": False,
            "frozen_precipitation": "liquid-equivalent-no-snow-storage-or-melt",
            "ongoing_event_history": "not-included",
            "nwm_state": "not-included",
            "operational_issue_time": None,
            "hindcast_as_of_verified": False,
        },
    }
    receipt["dataset_id"] = "forcing-dataset-" + digest(receipt)[:24]
    write_immutable(output / f"{receipt['dataset_id']}.json", canonical_bytes(receipt))
    return receipt
