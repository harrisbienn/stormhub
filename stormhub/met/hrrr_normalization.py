"""Decode hourly HRRR APCP and conservatively aggregate onto explicit supports.

Native readers and geospatial calculations stay in StormHub. This research
adapter treats APCP as liquid-water-equivalent precipitation; it does not model
snow storage, melt, antecedent conditions or hydrologic initial state.
"""

from __future__ import annotations

import math
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


def source_grid(dataset) -> dict:
    """Bind the native raster coordinate system and north-to-south transform."""
    transform = dataset.transform
    if (
        dataset.count != 1
        or dataset.crs is None
        or not dataset.crs.is_projected
        or transform.b != 0
        or transform.d != 0
        or transform.a <= 0
        or transform.e >= 0
    ):
        raise PrecipitationFeatureError("expected one projected north-to-south GRIB raster")
    return {"crs": dataset.crs.to_wkt(), "shape": [dataset.height, dataset.width], "transform": list(transform)[:6]}


def conservative_weights(dataset, grid: dict, indices: list[int]) -> dict:
    """Intersect native cells with selected target cells in the target metre CRS.

    Edges are densified into four segments before projection. Conservation is
    measured over this projected, piecewise-linear common footprint. This is
    a first-order remap, not a claim of geodesic or subcell rainfall accuracy.
    """
    import numpy as np
    import shapely
    from pyproj import CRS, Transformer
    from shapely.geometry import box

    validate_grid(grid)
    target = CRS.from_user_input(grid["crs"])
    if not target.is_projected or any(a.unit_conversion_factor != 1 for a in target.axis_info):
        raise PrecipitationFeatureError("target requires a projected metre CRS")
    rows, cols = grid["shape"]
    if rows * cols > 250_000 or not indices or len(indices) > 50_000:
        raise PrecipitationFeatureError("target exceeds bounded research subset limits")
    if indices != sorted(set(indices)) or any(type(i) is not int or not 0 <= i < rows * cols for i in indices):
        raise PrecipitationFeatureError("target cell indices must be sorted, unique and in bounds")
    x0, y0 = grid["origin"]
    size = grid["cell_size_meters"]
    left = x0 + min(i % cols for i in indices) * size
    right = x0 + (max(i % cols for i in indices) + 1) * size
    bottom = y0 + min(i // cols for i in indices) * size
    top = y0 + (max(i // cols for i in indices) + 1) * size
    inverse = Transformer.from_crs(target, dataset.crs, always_xy=True)
    bounds = inverse.transform_bounds(left, bottom, right, top, densify_pts=21)
    transform = dataset.transform
    c0 = max(0, math.floor((bounds[0] - transform.c) / transform.a) - 2)
    c1 = min(dataset.width, math.ceil((bounds[2] - transform.c) / transform.a) + 2)
    r0 = max(0, math.floor((bounds[3] - transform.f) / transform.e) - 2)
    r1 = min(dataset.height, math.ceil((bounds[1] - transform.f) / transform.e) + 2)
    if c1 <= c0 or r1 <= r0 or (r1 - r0) * (c1 - c0) > 100_000:
        raise PrecipitationFeatureError("native subset is empty or exceeds 100000 cells")
    project = Transformer.from_crs(dataset.crs, target, always_xy=True)
    weights = {i: [] for i in indices}
    targets = {
        i: box(x0 + i % cols * size, y0 + i // cols * size, x0 + (i % cols + 1) * size, y0 + (i // cols + 1) * size)
        for i in indices
    }
    # Shared edge vertices ensure adjacent native cells form the same partition.
    offsets = [(i / 4, 0) for i in range(4)] + [(1, i / 4) for i in range(4)]
    offsets += [(1 - i / 4, 1) for i in range(4)] + [(0, 1 - i / 4) for i in range(4)]
    for row in range(r0, r1):
        for col in range(c0, c1):
            xy = [
                (transform.c + transform.a * (col + dx), transform.f + transform.e * (row + dy)) for dx, dy in offsets
            ]
            px, py = project.transform([p[0] for p in xy], [p[1] for p in xy])
            polygon = shapely.polygons(np.column_stack((px, py)))
            if polygon.is_empty or not polygon.is_valid:
                raise PrecipitationFeatureError("invalid projected native cell")
            a, b, c, d = polygon.bounds
            for tr in range(max(0, math.floor((b - y0) / size)), min(rows, math.ceil((d - y0) / size))):
                for tc in range(max(0, math.floor((a - x0) / size)), min(cols, math.ceil((c - x0) / size))):
                    index = tr * cols + tc
                    if index not in weights:
                        continue
                    area = polygon.intersection(targets[index]).area
                    if area > 0:
                        weights[index].append([(row - r0) * (c1 - c0) + col - c0, area])
    errors = [abs(math.fsum(a for _, a in pairs) / size**2 - 1) for pairs in weights.values()]
    if max(errors) > 1e-7:
        raise PrecipitationFeatureError("native cells do not completely partition every selected target cell")
    return {
        "method": "projected-cell-intersection-v1",
        "edge_segments": 4,
        "native_grid": source_grid(dataset),
        "target_grid_sha256": digest(grid),
        "window": [c0, r0, c1 - c0, r1 - r0],
        "target_cells": [{"index": i, "sources": pairs} for i, pairs in weights.items()],
        "max_coverage_fraction_error": max(errors),
        "coverage_tolerance": 1e-7,
    }


def remap_interval(dataset, weights: dict, grid: dict) -> tuple[list, dict]:
    """Remap one interval, rejecting missing/negative consumed source values."""
    import numpy as np
    from rasterio.windows import Window

    if source_grid(dataset) != weights["native_grid"]:
        raise PrecipitationFeatureError("native grid changed between hourly messages")
    depths = dataset.read(1, window=Window(*weights["window"]), masked=True).flatten()
    used = sorted({i for cell in weights["target_cells"] for i, _ in cell["sources"]})
    values = depths[used]
    if np.ma.getmaskarray(values).any() or not np.isfinite(values.data).all() or (values.data < 0).any():
        raise PrecipitationFeatureError("missing, nonfinite or negative precipitation in consumed native cells")
    output = [None] * (grid["shape"][0] * grid["shape"][1])
    source_areas = {}
    target_volumes = []
    for cell in weights["target_cells"]:
        volume = math.fsum(float(depths[i]) * area / 1000 for i, area in cell["sources"])
        output[cell["index"]] = volume * 1000 / grid["cell_size_meters"] ** 2
        target_volumes.append(output[cell["index"]] * grid["cell_size_meters"] ** 2 / 1000)
        for i, area in cell["sources"]:
            source_areas.setdefault(i, []).append(area)
    source_volume = math.fsum(float(depths[i]) * math.fsum(areas) / 1000 for i, areas in source_areas.items())
    target_volume = math.fsum(target_volumes)
    error = abs(target_volume - source_volume)
    tolerance = max(1e-6, source_volume * 1e-10)
    if error > tolerance:
        raise PrecipitationFeatureError("remapping fails common-footprint volume accounting")
    return output, {
        "source_common_footprint_volume_m3": source_volume,
        "target_volume_m3": target_volume,
        "absolute_error_m3": error,
        "tolerance_m3": tolerance,
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
                for name in ("hrrr_acquisition.py", "hrrr_normalization.py")
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
