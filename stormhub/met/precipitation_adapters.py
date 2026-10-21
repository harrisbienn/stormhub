"""Authenticated input adapters for the shared rainfall feature calculator."""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

from .precipitation_features import (
    NORMALIZED_CONTRACT,
    SUPPORT_CONTRACT,
    PrecipitationFeatureError,
    digest,
    extract_features,
    file_sha256,
    utc,
    validate_grid,
    validate_support,
)


def extractor_identity(revision: str, *, dirty: bool, dependencies=()) -> dict:
    """Bind the calculator and adapters to code bytes and explicit dependencies."""
    folder = Path(__file__).parent
    code = {
        name: (folder / name).read_text(encoding="utf-8")
        for name in ("precipitation_features.py", "precipitation_adapters.py")
    }
    return {
        "name": "stormhub.precipitation-features",
        "version": "0.1.0",
        "revision": revision,
        "dirty": dirty,
        "code_sha256": digest(code),
        "dependencies": {name: version(name) for name in sorted(dependencies)},
    }


def authenticate_source(path: Path, source: dict) -> None:
    """Verify the original asset bytes before reading any format."""
    if path.stat().st_size != source["size_bytes"] or file_sha256(path) != source["sha256"]:
        raise PrecipitationFeatureError("source asset size or checksum mismatch")


def extract_normalized_file(path: Path, *, source: dict, support: dict, extractor: dict, definition=None) -> dict:
    """Read an authenticated normalized JSON asset through the common calculator."""
    authenticate_source(path, source)
    content = path.read_bytes()
    import hashlib

    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise PrecipitationFeatureError("source changed while reading")
    return extract_features(
        json.loads(content), source=source, support=support, extractor=extractor, definition=definition
    )


def build_spatial_support(grid: dict, regions: list[dict], *, region_crs: str, partition=None) -> dict:
    """Intersect declared region polygons with cells in a projected metre CRS.

    Region geometries are GeoJSON, with explicit id and kind. Full polygon area
    remains the denominator even when the forcing grid does not cover it.
    """
    import numpy as np
    import shapely
    from pyproj import CRS, Transformer
    from shapely.geometry import box, mapping
    from shapely.prepared import prep

    validate_grid(grid)
    target = CRS.from_user_input(grid["crs"])
    if not target.is_projected or any(a.unit_conversion_factor != 1 for a in target.axis_info):
        raise PrecipitationFeatureError("spatial support requires a projected metre CRS")
    project = Transformer.from_crs(region_crs, target, always_xy=True).transform
    polygons = {}
    result = []
    rows, columns = grid["shape"]
    x0, y0 = grid["origin"]
    size = grid["cell_size_meters"]
    for region in sorted(regions, key=lambda r: r["id"]):
        # GEOS decoding and the coordinate-array API avoid MultiPolygon
        # collection coercion failures with Shapely 2.0 and newer NumPy.
        polygon = shapely.transform(
            shapely.from_geojson(json.dumps(region["geometry"])),
            lambda xy: np.column_stack(project(xy[:, 0], xy[:, 1])),
        )
        if polygon.geom_type not in {"Polygon", "MultiPolygon"} or polygon.is_empty or not polygon.is_valid:
            raise PrecipitationFeatureError("regions must be valid nonempty polygons")
        if region["id"] in polygons:
            raise PrecipitationFeatureError("duplicate region identity")
        polygons[region["id"]] = polygon
        prepared = prep(polygon)
        left, bottom, right, top = polygon.bounds
        weights = []
        for row in range(max(0, math.floor((bottom - y0) / size)), min(rows, math.ceil((top - y0) / size))):
            for column in range(max(0, math.floor((left - x0) / size)), min(columns, math.ceil((right - x0) / size))):
                x, y = x0 + column * size, y0 + row * size
                cell = box(x, y, x + size, y + size)
                if not prepared.intersects(cell):
                    continue
                area = size * size if prepared.contains(cell) else polygon.intersection(cell).area
                if area > 0:
                    weights.append({"index": row * columns + column, "area_m2": area})
        result.append(
            {
                "id": region["id"],
                "kind": region["kind"],
                "area_m2": polygon.area,
                "geometry_sha256": digest({"crs": grid["crs"], "geometry": mapping(polygon)}),
                "cells": weights,
            }
        )
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "regions": result,
        "partition": partition,
    }
    validate_support(support, grid)
    if partition:
        parent = polygons[partition["parent"]]
        children = [polygons[name] for name in partition["children"]]
        # Scalar operations also support older Shapely with newer NumPy builds.
        union = children[0]
        for child in children[1:]:
            union = union.union(child)
        tolerance = max(1e-6, parent.area * 1e-9)
        if (
            sum(p.area for p in children) - union.area > tolerance
            or parent.symmetric_difference(union).area > tolerance
        ):
            raise PrecipitationFeatureError("subbasin polygons overlap or do not partition their parent")
    return support


def _dss_time(value: str) -> datetime:
    match = re.fullmatch(r"(\d{2})([A-Za-z]{3})(\d{4}):(\d{2})(\d{2})", value)
    if not match:
        raise PrecipitationFeatureError("DSS interval pathname must carry date and time")
    day, month, year, hour, minute = match.groups()
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    try:
        base = datetime(int(year), months.index(month.upper()) + 1, int(day), tzinfo=timezone.utc)
        if int(hour) > 24 or int(minute) > 59 or (int(hour) == 24 and int(minute)):
            raise ValueError("invalid clock")
        return base + timedelta(hours=int(hour), minutes=int(minute))
    except ValueError as exc:
        raise PrecipitationFeatureError("invalid DSS pathname clock") from exc


def read_dss(path: Path, *, source: dict, selector: str, event_start: str, event_end: str) -> dict:
    """Read total interval precipitation from a temporary DSS copy, never the source.

    The caller explicitly declares UTC event bounds. Record clocks must be UTC
    compatible, contiguous, and fully contained in those bounds. No transposition,
    padding, accumulation differencing, or inferred missing intervals occurs.
    """
    from fnmatch import fnmatchcase
    from hecdss import HecDss

    authenticate_source(path, source)
    start, end = utc(event_start), utc(event_end)
    if end <= start:
        raise PrecipitationFeatureError("event end must follow start")
    records = []
    with tempfile.TemporaryDirectory(prefix="stormhub-rainfall-") as temporary:
        copy = Path(temporary) / "input.dss"
        shutil.copyfile(path, copy)
        authenticate_source(copy, source)
        with HecDss(str(copy)) as dss:
            for pathname in dss.get_catalog():
                pathname = str(pathname)
                if not fnmatchcase(pathname, selector):
                    continue
                parts = pathname.strip("/").split("/")
                if len(parts) != 6 or parts[2].upper() != "PRECIPITATION":
                    raise PrecipitationFeatureError("selector must resolve total PRECIPITATION grids")
                left, right = _dss_time(parts[3]), _dss_time(parts[4])
                if right <= start or left >= end:
                    continue
                if left < start or right > end or right <= left:
                    raise PrecipitationFeatureError("DSS interval crosses event bounds or is reversed")
                grid = dss.get(pathname)
                # PER-CUM and explicit D/E bounds define interval depth. The
                # historical StormHub writer leaves the auxiliary isInterval=0.
                if grid is None or grid.data_type != 1:
                    raise PrecipitationFeatureError("DSS requires PER-CUM interval precipitation")
                if grid.timeZoneRawOffset != 0 or str(grid.timeZoneID).upper() not in {"", "UTC", "ETC/GMT", "GMT"}:
                    raise PrecipitationFeatureError("DSS clock is not explicitly UTC compatible")
                metadata = {
                    "crs": grid.srsDefinition,
                    "shape": [grid.numberOfCellsY, grid.numberOfCellsX],
                    "origin": [
                        grid.xCoordOfGridCellZero + grid.lowerLeftCellX * grid.cellSize,
                        grid.yCoordOfGridCellZero + grid.lowerLeftCellY * grid.cellSize,
                    ],
                    "cell_size_meters": grid.cellSize,
                    "row_order": "south_to_north",
                }
                units = str(grid.dataUnits).strip().lower()
                if units not in {"mm", "in"}:
                    raise PrecipitationFeatureError("unsupported DSS precipitation units")
                # hecdss stores missing floats using the HEC sentinel. StormHub's
                # historical writer sets nullValue=0 even though dry zeros are valid.
                values = [
                    None if not math.isfinite(float(v)) or float(v) < -3e38 else float(v) for v in grid.data.ravel()
                ]
                records.append((left, right, tuple(parts[i] for i in (0, 1, 2, 5)), metadata, units, values))
    authenticate_source(path, source)
    records.sort(key=lambda r: r[0])
    if not records:
        raise PrecipitationFeatureError("no DSS precipitation intervals resolved")
    interval = (records[0][1] - records[0][0]).total_seconds() / 60
    if not interval.is_integer() or interval <= 0:
        raise PrecipitationFeatureError("DSS requires whole-minute intervals")
    cursor = start
    for left, right, identity, metadata, units, _ in records:
        if left != cursor or (right - left).total_seconds() != interval * 60:
            raise PrecipitationFeatureError("DSS intervals have gaps, duplicates, or changing resolution")
        if identity != records[0][2] or metadata != records[0][3] or units != records[0][4]:
            raise PrecipitationFeatureError("DSS selector resolves ambiguous series or changing grids/units")
        cursor = right
    if cursor != end:
        raise PrecipitationFeatureError("DSS does not cover the complete event")
    return {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": records[0][3],
        "event_start": event_start,
        "event_end": event_end,
        "interval_minutes": int(interval),
        "units": records[0][4],
        "measurement": "interval-depth",
        "values": [r[5] for r in records],
    }
