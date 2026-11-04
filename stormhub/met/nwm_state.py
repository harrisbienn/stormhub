"""Bounded NWM v3.1 analysis land and reservoir exports for state research."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .hrrr_acquisition import descriptor, http_request, verify_asset, write_immutable
from .precipitation_adapters import build_spatial_support
from .precipitation_features import PrecipitationFeatureError, canonical_bytes, digest, utc, validate_grid

CONTRACT = "stormhub/nwm-state/0.1.0"
ACQUISITION = "stormhub/nwm-state-acquisition/0.1.0"
MAX_OBJECT = 128 * 1024**2
MAX_CACHE = 512 * 1024**2


def selection(request: dict) -> dict:
    """Select one explicit tm00 analysis; no silent vintage substitution."""
    if set(request) != {"cycle", "product", "model_version", "lake_ids", "base_url", "retrieval_revision"}:
        raise PrecipitationFeatureError("NWM state request fields differ")
    cycle = utc(request["cycle"])
    if cycle.minute or cycle.second or cycle.microsecond or request["model_version"] != "v3.1":
        raise PrecipitationFeatureError("state adapter requires hourly v3.1 analysis")
    product, lakes = request["product"], request["lake_ids"]
    if product not in {"land", "reservoir"} or not isinstance(lakes, list):
        raise PrecipitationFeatureError("unsupported NWM state product")
    if any(type(n) is not int or n <= 0 for n in lakes) or lakes != sorted(set(lakes)):
        raise PrecipitationFeatureError("lake IDs must be sorted unique positive integers")
    if (product == "land" and lakes) or (product == "reservoir" and not 1 <= len(lakes) <= 100):
        raise PrecipitationFeatureError("select one to 100 lakes for reservoir only")
    url = urlsplit(request["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise PrecipitationFeatureError("state base requires HTTPS without credentials/query/fragment")
    if not isinstance(request["retrieval_revision"], str) or not request["retrieval_revision"].strip():
        raise PrecipitationFeatureError("retrieval revision is required")
    key = f"nwm.{cycle:%Y%m%d}/analysis_assim/nwm.t{cycle:%H}z.analysis_assim.{product}.tm00.conus.nc"
    return {"url": request["base_url"].rstrip("/") + "/" + key, "valid_time": cycle.isoformat()}


def validate_acquisition(record: dict, root: Path) -> Path:
    """Authenticate retained bytes and exact product/time selection."""
    if record.get("contract_version") != ACQUISITION or record.get("acquisition_id") != digest(
        {k: v for k, v in record.items() if k != "acquisition_id"}
    ):
        raise PrecipitationFeatureError("state acquisition identity differs")
    if any(record[k] != value for k, value in selection(record["request"]).items()):
        raise PrecipitationFeatureError("state selection differs")
    if not 0 < record["asset"]["size_bytes"] <= MAX_OBJECT:
        raise PrecipitationFeatureError("state object exceeds byte limit")
    if utc(record["observed_at"]) < utc(record["valid_time"]):
        raise PrecipitationFeatureError("state acquisition precedes analysis time")
    return verify_asset(root, record["asset"])


def acquire(request: dict, root: Path, *, offline: bool = False, transport=http_request) -> dict:
    """Retain one conditional, size-bounded object and replay without network."""
    import json

    selected = selection(request)
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("state-acquisition-" + digest(request) + ".json")
    if target.exists():
        record = json.loads(target.read_bytes())
        if record["request"] != request:
            raise PrecipitationFeatureError("cached state request differs")
        validate_acquisition(record, root)
        return record
    if offline:
        raise PrecipitationFeatureError("state acquisition unavailable offline")
    status, headers, _ = transport(selected["url"], method="HEAD", headers={}, limit=0)
    size, etag = int(headers.get("content-length", 0)), headers.get("etag", "")
    if status != 200 or not 0 < size <= MAX_OBJECT or not re.fullmatch(r'"[^"\r\n]+"', etag):
        raise PrecipitationFeatureError("state object requires bounded size and strong ETag")
    if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) + size > MAX_CACHE:
        raise PrecipitationFeatureError("state cache exceeds 512 MiB")
    status, response, content = transport(selected["url"], method="GET", headers={"If-Match": etag}, limit=size)
    if (
        status != 200
        or response.get("etag") != etag
        or len(content) != size
        or not content.startswith(b"\x89HDF\r\n\x1a\n")
    ):
        raise PrecipitationFeatureError("state object changed, truncated or is not NetCDF4")
    import hashlib

    path = root / (hashlib.sha256(content).hexdigest() + ".nc")
    write_immutable(path, content)
    record = {
        "contract_version": ACQUISITION,
        "request": request,
        **selected,
        "asset": descriptor(path, root),
        "etag": etag,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "original_publication_time": None,
    }
    record["acquisition_id"] = digest(record)
    write_immutable(target, canonical_bytes(record))
    return record


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _clock(dataset, request: dict) -> None:
    valid = utc(request["cycle"])
    initial = valid - timedelta(hours=3)
    attrs = dataset.attrs
    if (
        attrs.get("NWM_version_number") != request["model_version"]
        or attrs.get("model_output_type") != request["product"]
        or attrs.get("model_configuration") != "analysis_and_assimilation"
    ):
        raise PrecipitationFeatureError("native state model/product differs")
    for attr, name, expected in [
        ("model_initialization_time", "reference_time", initial),
        ("model_output_valid_time", "time", valid),
    ]:
        var = dataset.variables[name]
        if (
            attrs.get(attr) != expected.strftime("%Y-%m-%d_%H:%M:%S")
            or var.shape != (1,)
            or _text(var.attrs.get("units")) != "minutes since 1970-01-01 00:00:00 UTC"
            or float(var[0]) != expected.timestamp() / 60
        ):
            raise PrecipitationFeatureError("native state clock differs")


def land_grid(dataset) -> dict:
    """Use native coordinate centers, not a conflicting auxiliary GeoTransform."""
    import numpy as np

    axes = []
    for name in ["x", "y"]:
        var = dataset.variables[name]
        values = var[:]
        if (
            var.dimensions != (name,)
            or not 2 <= len(values) <= 10000
            or _text(var.attrs.get("units")) != "m"
            or not np.isfinite(values).all()
            # Native coordinate arrays retain sub-metre quantization even when
            # stored as float64. Bound absolute lattice error, not accumulated
            # step error; 0.25 m is the observed continental-axis precision.
            or not np.allclose(values, values[0] + np.arange(len(values)) * 1000, rtol=0, atol=0.25)
        ):
            raise PrecipitationFeatureError("unsupported land grid axes")
        axes.append(values)
    grid = {
        "crs": _text(dataset.attrs["proj4"]),
        "shape": [len(axes[1]), len(axes[0])],
        "origin": [float(axis[0]) - 500 for axis in axes],
        "cell_size_meters": 1000,
        "row_order": "south_to_north",
    }
    validate_grid(grid)
    return grid


def _value(var, raw: float) -> float | None:
    if raw in (var.attrs.get("_FillValue"), var.attrs.get("missing_value")):
        return None
    scale, offset = float(var.attrs.get("scale_factor", 1)), float(var.attrs.get("add_offset", 0))
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
        raise PrecipitationFeatureError("invalid state packing")
    value = raw * scale + offset
    limits = var.attrs.get("valid_range")
    if not math.isfinite(value) or (limits is not None and not limits[0] <= raw <= limits[1]):
        raise PrecipitationFeatureError("invalid native state value")
    return value


def decode(path: Path, request: dict, *, geometry: dict | None = None) -> dict:
    """Extract area-weighted wetness or selected lake values, preserving unknowns."""
    import h5netcdf
    import numpy as np

    selection(request)
    with h5netcdf.File(path, "r") as dataset:
        _clock(dataset, request)
        if request["product"] == "land":
            if geometry is None or set(geometry) != {"regions", "crs", "partition"}:
                raise PrecipitationFeatureError("land export needs explicit geometry")
            grid = land_grid(dataset)
            support = build_spatial_support(
                grid, geometry["regions"], region_crs=geometry["crs"], partition=geometry["partition"]
            )
            var = dataset.variables["SOILSAT_TOP"]
            if (
                var.dimensions != ("time", "y", "x")
                or var.shape != (1, *grid["shape"])
                or _text(var.attrs.get("units")) != "1"
                or var.attrs.get("grid_mapping") != "crs"
            ):
                raise PrecipitationFeatureError("expected dimensionless SOILSAT_TOP on native land grid")
            columns = grid["shape"][1]
            indices = {c["index"] for r in support["regions"] for c in r["cells"]}
            values = {}
            # Read each selected row segment once, without loading the continental array.
            rows = {}
            for index in indices:
                row, col = divmod(index, columns)
                rows.setdefault(row, []).append(col)
            for row, cols in rows.items():
                left, right = min(cols), max(cols)
                raw = var[0, row, left : right + 1]
                for col in cols:
                    value = _value(var, float(raw[col - left]))
                    if value is not None and not -1e-6 <= value <= 1 + 1e-6:
                        raise PrecipitationFeatureError("soil saturation outside unit fraction")
                    values[row * columns + col] = value
            regions = []
            for region in support["regions"]:
                valid = [c for c in region["cells"] if values[c["index"]] is not None]
                area = math.fsum(c["area_m2"] for c in valid)
                regions.append(
                    {
                        "id": region["id"],
                        "kind": region["kind"],
                        "area_m2": region["area_m2"],
                        "value": math.fsum(values[c["index"]] * c["area_m2"] for c in valid) / area if area else None,
                        "valid_area_m2": area,
                        "missing_fraction": max(0.0, 1 - area / region["area_m2"]),
                    }
                )
            return {
                "grid": grid,
                "coordinate_sha256": digest({name: dataset.variables[name][:].tolist() for name in ("x", "y")}),
                "grid_normalization": "1000m-center-lattice-maximum-axis-error-0.25m",
                "geometry_sha256": digest(geometry),
                "support": support,
                "quantity": "SOILSAT_TOP",
                "units": "1",
                "depth_m": 0.4,
                "regions": regions,
                "temporal_support": "instantaneous",
                "percentile": None,
            }
        if geometry is not None:
            raise PrecipitationFeatureError("reservoir export does not consume land geometry")
        ids = dataset.variables["feature_id"]
        if (
            ids.dimensions != ("feature_id",)
            or not 1 <= ids.shape[0] <= 100000
            or ids.attrs.get("comment") != "ComID from NHDPlusV2 waterbody layer"
        ):
            raise PrecipitationFeatureError("reservoir waterbody network differs")
        identifiers, lakes = ids[:], []
        for lake in request["lake_ids"]:
            positions = np.flatnonzero(identifiers == lake)
            if len(positions) != 1:
                raise PrecipitationFeatureError("selected lake absent or duplicated")
            quantities = {}
            for name, units in [("inflow", "m3 s-1"), ("outflow", "m3 s-1"), ("water_sfc_elev", "m")]:
                var = dataset.variables[name]
                if var.dimensions != ids.dimensions or var.shape != ids.shape or _text(var.attrs.get("units")) != units:
                    raise PrecipitationFeatureError("reservoir variable units/dimensions differ")
                quantities[name] = {"value": _value(var, float(var[int(positions[0])])), "units": units}
            lakes.append(
                {
                    "lake_id": lake,
                    "quantities": quantities,
                    "vertical_datum": "unknown",
                    "storage": None,
                    "mapping_status": "unreviewed",
                }
            )
        return {"network": "NHDPlusv2-CONUS-waterbodies", "lakes": lakes, "temporal_support": "instantaneous"}


def extract(
    acquisition: dict, raw_root: Path, output: Path, *, revision: str, geometry: dict | None = None, dirty: bool = False
) -> dict:
    """Export immutable state and provenance; scientific mapping remains consumer-owned."""
    if not re.fullmatch("[0-9a-f]{40}", revision) or type(dirty) is not bool:
        raise PrecipitationFeatureError("state producer requires exact revision and dirty flag")
    source = validate_acquisition(acquisition, raw_root)
    state = decode(source, acquisition["request"], geometry=geometry)
    write_immutable(output / "state.json", canonical_bytes(state))
    write_immutable(output / "acquisition.json", canonical_bytes(acquisition))
    receipt = {
        "contract_version": CONTRACT,
        "request": acquisition["request"],
        "valid_time": acquisition["valid_time"],
        "state": descriptor(output / "state.json", output),
        "acquisition": descriptor(output / "acquisition.json", output),
        "producer": {"name": "stormhub.nwm-state", "revision": revision, "dirty": dirty},
        "forecast_eligible": False,
    }
    receipt["dataset_id"] = digest(receipt)
    write_immutable(output / "receipt.json", canonical_bytes(receipt))
    return receipt
