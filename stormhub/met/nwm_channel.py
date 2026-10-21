"""Bounded NWM v3 CONUS channel analysis and short-range research exports.

Archive cycle, model initialization and valid time are distinct. Native reads
stay here; consumers receive portable selected-reach JSON, never NetCDF arrays.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from .hrrr_acquisition import descriptor, http_request, verify_asset, write_immutable
from .precipitation_features import PrecipitationFeatureError, canonical_bytes, digest, file_sha256, utc

NETWORK = "NHDPlusv2-CONUS"
CONTRACT = "stormhub/nwm-channel-series/0.1.0"
ACQUISITION = "stormhub/nwm-acquisition/0.1.0"
MAX_OBJECT = 32 * 1024**2
MAX_CACHE = 768 * 1024**2


def selections(request: dict) -> list[dict]:
    """Validate an explicit product and build at most eighteen hourly selections."""
    if set(request) != {
        "cycle",
        "product",
        "hours",
        "reach_ids",
        "model_version",
        "network",
        "base_url",
        "retrieval_revision",
    }:
        raise PrecipitationFeatureError("NWM request fields differ from the channel contract")
    cycle = utc(request["cycle"])
    if cycle.minute or cycle.second or cycle.microsecond:
        raise PrecipitationFeatureError("NWM archive cycle must be hourly")
    if request["model_version"] != "v3.0" or request["network"] != NETWORK:
        raise PrecipitationFeatureError("unsupported NWM model/network")
    reaches = request["reach_ids"]
    if (
        not isinstance(reaches, list)
        or not 1 <= len(reaches) <= 100
        or any(type(r) is not int or r <= 0 for r in reaches)
    ):
        raise PrecipitationFeatureError("select one to 100 positive integer reach IDs")
    if sorted(set(reaches)) != reaches:
        raise PrecipitationFeatureError("reach IDs must be unique and sorted")
    url = urlsplit(request["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise PrecipitationFeatureError("NWM base requires HTTPS without credentials, query or fragment")
    if not isinstance(request["retrieval_revision"], str) or not request["retrieval_revision"].strip():
        raise PrecipitationFeatureError("explicit retrieval revision required")
    product, hours = request["product"], request["hours"]
    if type(hours) is not int or not (
        (product == "analysis_assim" and hours == 0) or (product == "short_range" and 1 <= hours <= 18)
    ):
        raise PrecipitationFeatureError("select tm00 analysis or one to eighteen short-range hours")
    result = []
    for hour in [0] if product == "analysis_assim" else range(1, hours + 1):
        suffix = "tm00" if product == "analysis_assim" else f"f{hour:03d}"
        key = f"nwm.{cycle:%Y%m%d}/{product}/nwm.t{cycle:%H}z.{product}.channel_rt.{suffix}.conus.nc"
        result.append(
            {
                "hour": hour,
                "valid_time": (cycle + timedelta(hours=hour)).isoformat(),
                "url": request["base_url"].rstrip("/") + "/" + key,
            }
        )
    return result


def validate_acquisition(receipt: dict, root: Path) -> None:
    """Authenticate every retained object and its product/clock selection."""
    if receipt.get("contract_version") != ACQUISITION or receipt.get("acquisition_id") != digest(
        {k: v for k, v in receipt.items() if k != "acquisition_id"}
    ):
        raise PrecipitationFeatureError("NWM acquisition identity differs")
    expected = selections(receipt["request"])
    if len(expected) != len(receipt["records"]):
        raise PrecipitationFeatureError("incomplete NWM acquisition")
    for selected, record in zip(expected, receipt["records"]):
        if any(record[k] != v for k, v in selected.items()):
            raise PrecipitationFeatureError("NWM object selection differs")
        utc(record["observed_at"])
        verify_asset(root, record["asset"])


def acquire(request: dict, root: Path, *, offline: bool = False, transport=http_request) -> dict:
    """Retain bounded conditional GETs; complete receipts replay without network."""
    selected = selections(request)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"nwm-acquisition-{digest(request)}.json"
    if target.exists():
        receipt = json.loads(target.read_bytes())
        if receipt["request"] != request:
            raise PrecipitationFeatureError("cached NWM request differs")
        validate_acquisition(receipt, root)
        return receipt
    if offline:
        raise PrecipitationFeatureError("complete NWM acquisition is unavailable offline")
    records = []
    for selection in selected:
        status, headers, _ = transport(selection["url"], method="HEAD", headers={}, limit=0)
        size, etag = int(headers.get("content-length", 0)), headers.get("etag", "")
        if status != 200 or not 0 < size <= MAX_OBJECT or not re.fullmatch(r'"[^"\r\n]+"', etag):
            raise PrecipitationFeatureError("NWM object needs bounded size and strong ETag")
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        if used + size > MAX_CACHE:
            raise PrecipitationFeatureError("NWM retained cache exceeds 768 MiB budget")
        status, response, content = transport(
            selection["url"], method="GET", headers={"If-Match": etag}, limit=MAX_OBJECT
        )
        if (
            status != 200
            or response.get("etag") != etag
            or len(content) != size
            or not content.startswith(b"\x89HDF\r\n\x1a\n")
        ):
            raise PrecipitationFeatureError("NWM object changed, truncated or is not NetCDF4/HDF5")
        import hashlib

        path = root / (hashlib.sha256(content).hexdigest() + ".nc")
        write_immutable(path, content)
        records.append(
            {
                **selection,
                "asset": descriptor(path, root),
                "etag": etag,
                "last_modified": headers.get("last-modified"),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    receipt = {
        "contract_version": ACQUISITION,
        "request": request,
        "records": records,
        "availability_status": "archive-retrieval-only",
        "original_publication_time": None,
    }
    receipt["acquisition_id"] = digest(receipt)
    write_immutable(target, canonical_bytes(receipt))
    return receipt


def decode(path: Path, request: dict, selection: dict) -> dict:
    """Validate native identity/clocks/packing and extract only selected flows."""
    import h5netcdf
    import numpy as np

    selections(request)
    with h5netcdf.File(path, "r") as dataset:
        attrs = dataset.attrs
        product = request["product"]
        configuration = "analysis_and_assimilation" if product == "analysis_assim" else product
        if (
            attrs.get("NWM_version_number") != request["model_version"]
            or attrs.get("model_output_type") != "channel_rt"
            or attrs.get("model_configuration") != configuration
        ):
            raise PrecipitationFeatureError("native NWM model/product differs")
        cycle = utc(request["cycle"])
        initialization = cycle - timedelta(hours=3) if product == "analysis_assim" else cycle
        valid = utc(selection["valid_time"])
        for name, expected in (("model_initialization_time", initialization), ("model_output_valid_time", valid)):
            if attrs.get(name) != expected.strftime("%Y-%m-%d_%H:%M:%S"):
                raise PrecipitationFeatureError("native NWM initialization/valid time differs")
        for name, expected in (("reference_time", initialization), ("time", valid)):
            var = dataset.variables[name]
            if (
                var.shape != (1,)
                or var.attrs.get("units") != "minutes since 1970-01-01 00:00:00 UTC"
                or float(var[0]) != expected.timestamp() / 60
            ):
                raise PrecipitationFeatureError("native NWM coordinate clock differs")
        ids = dataset.variables["feature_id"]
        if (
            ids.dimensions != ("feature_id",)
            or ids.shape[0] > 3_000_000
            or ids.attrs.get("comment") != "NHDPlusv2 ComIDs within CONUS, arbitrary Reach IDs outside of CONUS"
        ):
            raise PrecipitationFeatureError("native reach network/dimension differs")
        values = ids[:]
        flow = dataset.variables["streamflow"]
        if (
            flow.dimensions != ("feature_id",)
            or flow.shape != ids.shape
            or flow.attrs.get("units") != "m3 s-1"
            or flow.attrs.get("long_name") != "River Flow"
        ):
            raise PrecipitationFeatureError("expected channel streamflow in m3 s-1")
        scale, offset = float(flow.attrs.get("scale_factor", 1)), float(flow.attrs.get("add_offset", 0))
        if not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
            raise PrecipitationFeatureError("invalid flow packing")
        selected = {}
        for reach in request["reach_ids"]:
            indices = np.flatnonzero(values == reach)
            if len(indices) != 1:
                raise PrecipitationFeatureError(f"reach {reach} missing or duplicated")
            raw = float(flow[int(indices[0])])
            limits = flow.attrs.get("valid_range")
            value = raw * scale + offset
            if (
                raw in (flow.attrs.get("_FillValue"), flow.attrs.get("missing_value"))
                or not math.isfinite(value)
                or value < 0
                or (limits is not None and not limits[0] <= raw <= limits[1])
            ):
                raise PrecipitationFeatureError(f"missing or invalid flow at reach {reach}")
            selected[str(reach)] = value
        return {
            "valid_time": valid.isoformat(),
            "model_initialization": initialization.isoformat(),
            "flow_m3_s": selected,
            "native_code_version": str(attrs.get("code_version", "unknown")),
        }


def extract(acquisition: dict, raw_root: Path, output: Path, *, revision: str, dirty: bool = False) -> dict:
    """Export immutable selected channel samples with exact source provenance."""
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise PrecipitationFeatureError("producer requires an exact Git revision")
    validate_acquisition(acquisition, raw_root)
    request = acquisition["request"]
    samples = [decode(verify_asset(raw_root, r["asset"]), request, r) for r in acquisition["records"]]
    series = {
        "request": request,
        "quantity": "streamflow",
        "units": "m3 s-1",
        "temporal_support": "instantaneous",
        "interval_seconds": 3600,
        "samples": samples,
    }
    write_immutable(output / "series.json", canonical_bytes(series))
    write_immutable(output / "acquisition.json", canonical_bytes(acquisition))
    receipt = {
        "contract_version": CONTRACT,
        "request": request,
        "series": descriptor(output / "series.json", output),
        "acquisition": descriptor(output / "acquisition.json", output),
        "producer": {
            "name": "stormhub.nwm-channel",
            "revision": revision,
            "dirty": dirty,
            "code_sha256": file_sha256(Path(__file__)),
            "dependencies": {n: version(n) for n in ("h5netcdf", "h5py", "numpy")},
        },
        "forecast_eligible": False,
        "network_artifact_sha256": None,
        "limitations": [
            "network release inferred from explicit native NHDPlusv2 metadata; RouteLink artifact not supplied",
            "archive retrieval does not prove original issue-time availability",
            "NWM shares HRRR/MRMS forcing; not independent validation",
            "channel flow does not determine soil moisture or HMS loss state",
        ],
    }
    receipt["dataset_id"] = "nwm-channel-series-" + digest(receipt)[:24]
    write_immutable(output / "receipt.json", canonical_bytes(receipt))
    return receipt
