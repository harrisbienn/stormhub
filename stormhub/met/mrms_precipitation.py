"""Bounded MRMS hourly multi-sensor QPE acquisition and normalization.

Product codes, rather than an outdated GDAL display description, define the
one-hour accumulation ending at observation time. Nominal latency never
substitutes for evidence of historical publication availability.
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from .hrrr_acquisition import descriptor, http_request, verify_asset, write_immutable
from .precipitation_features import (
    NORMALIZED_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    utc,
    validate_support,
)
from .precipitation_remapping import conservative_weights, remap_interval

CONTRACT = "stormhub/observed-precipitation/0.1.0"
PRODUCTS = {"MultiSensor_QPE_01H_Pass1": 30, "MultiSensor_QPE_01H_Pass2": 37}


def interval_ends(request: dict) -> list[datetime]:
    """Require a bounded, explicit hourly interval window and named retrieval vintage."""
    if set(request) != {"start", "end", "product", "base_url", "retrieval_revision"}:
        raise PrecipitationFeatureError("MRMS request requires start/end/product/base_url/retrieval_revision")
    start, end = utc(request["start"]), utc(request["end"])
    if any(t.minute or t.second or t.microsecond for t in (start, end)) or not timedelta(0) < end - start <= timedelta(
        hours=24
    ):
        raise PrecipitationFeatureError("MRMS requires 1–24 complete UTC hours")
    if (
        request["product"] not in PRODUCTS
        or not isinstance(request["retrieval_revision"], str)
        or not request["retrieval_revision"].strip()
    ):
        raise PrecipitationFeatureError("unsupported MRMS product or empty retrieval revision")
    url = urlsplit(request["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise PrecipitationFeatureError("MRMS provider must be HTTPS without credentials/query/fragment")
    return [start + timedelta(hours=i) for i in range(1, int((end - start).total_seconds() / 3600) + 1)]


def validate_tags(tags: dict, end: str, product: str) -> dict:
    """Validate NOAA local discipline/category/product and observation-end clock."""
    try:
        pds = [int(x) for x in tags["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"].split()]
        valid, reference = int(tags["GRIB_VALID_TIME"]), int(tags["GRIB_REF_TIME"])
    except (KeyError, ValueError) as exc:
        raise PrecipitationFeatureError("MRMS lacks decoded product/time metadata") from exc
    clock = utc(end)
    if (
        product not in PRODUCTS
        or tags.get("GRIB_DISCIPLINE") != "209"
        or tags.get("GRIB_PDS_PDTN") != "0"
        or len(pds) != 15
        or pds[:3] != [6, PRODUCTS[product], 8]
        or pds[4] != 97
        or pds[8] != 0
        or pds[9] != 102
        or pds[12] != 255
        or tags.get("GRIB_UNIT") != "[mm]"
        or tags.get("GRIB_FORECAST_SECONDS") != "0"
        or "CENTER=161(" not in tags.get("GRIB_IDS", "")
        or "SIGNF_REF_TIME=3(" not in tags.get("GRIB_IDS", "")
        or valid != int(clock.timestamp())
        or reference != valid
    ):
        raise PrecipitationFeatureError("not the requested MRMS hourly observation-end product")
    return {
        "start": (clock - timedelta(hours=1)).isoformat(),
        "end": clock.isoformat(),
        "product": product,
        "duration_seconds": 3600,
        "units": "mm",
        "measurement": "interval-depth",
        "interval_basis": "NOAA-MRMS-local-product-table",
    }


def validate_acquisition(receipt: dict, root: Path) -> None:
    """Authenticate retained compressed and decoded bytes and contiguous intervals."""
    body = {k: v for k, v in receipt.items() if k != "acquisition_id"}
    if receipt.get("contract_version") != "stormhub/mrms-acquisition/0.1.0" or receipt.get("acquisition_id") != digest(
        body
    ):
        raise PrecipitationFeatureError("MRMS acquisition identity mismatch")
    ends = interval_ends(receipt["request"])
    if [utc(r["end"]) for r in receipt["records"]] != ends:
        raise PrecipitationFeatureError("MRMS acquisition has missing or duplicate intervals")
    for record in receipt["records"]:
        verify_asset(root, record["compressed"])
        verify_asset(root, record["grib"])


def acquire(request: dict, root: Path, *, offline: bool = False, transport=http_request) -> dict:
    """Retain at most 24 hourly products under fixed compressed/expanded/cache limits.

    Completed requests never refetch. Use an explicit new retrieval_revision to
    examine a provider revision; earlier receipts and bytes remain immutable.
    """
    ends = interval_ends(request)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"mrms-acquisition-{digest(request)}.json"
    if path.exists():
        receipt = json.loads(path.read_bytes())
        if receipt["request"] != request:
            raise PrecipitationFeatureError("cached MRMS request differs")
        validate_acquisition(receipt, root)
        return receipt
    if offline:
        raise PrecipitationFeatureError("complete MRMS acquisition not retained")
    retained = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    def retain(content: bytes, suffix: str) -> dict:
        nonlocal retained
        import hashlib

        target = root / (hashlib.sha256(content).hexdigest() + suffix)
        extra = 0 if target.exists() else len(content)
        if retained + extra > 512 * 1024**2:
            raise PrecipitationFeatureError("MRMS cache exceeds 512 MiB")
        write_immutable(target, content)
        retained += extra
        return descriptor(target, root)

    records = []
    for end in ends:
        product = request["product"]
        url = (
            f"{request['base_url'].rstrip('/')}/CONUS/{product}_00.00/{end:%Y%m%d}/"
            f"MRMS_{product}_00.00_{end:%Y%m%d-%H%M%S}.grib2.gz"
        )
        status, head, _ = transport(url, method="HEAD", headers={}, limit=0)
        if (
            status != 200
            or not head.get("etag")
            or head["etag"].startswith("W/")
            or not 0 < int(head.get("content-length", 0)) <= 8 * 1024**2
        ):
            raise PrecipitationFeatureError("MRMS object lacks bounded length/strong ETag")
        status, headers, content = transport(url, method="GET", headers={"If-Match": head["etag"]}, limit=8 * 1024**2)
        if status != 200 or headers.get("etag") != head["etag"] or len(content) != int(head["content-length"]):
            raise PrecipitationFeatureError("MRMS object changed or was truncated")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                decoded = stream.read(32 * 1024**2 + 1)
        except (OSError, EOFError) as exc:
            raise PrecipitationFeatureError("invalid compressed MRMS object") from exc
        if (
            len(decoded) > 32 * 1024**2
            or len(decoded) < 20
            or decoded[:4] != b"GRIB"
            or decoded[7] != 2
            or decoded[-4:] != b"7777"
            or int.from_bytes(decoded[8:16], "big") != len(decoded)
        ):
            raise PrecipitationFeatureError("MRMS must contain one bounded GRIB2 message")
        records.append(
            {
                "end": end.isoformat(),
                "url": url,
                "etag": head["etag"],
                "object_last_modified": head.get("last-modified"),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "compressed": retain(content, ".grib2.gz"),
                "grib": retain(decoded, ".grib2"),
            }
        )
    receipt = {
        "contract_version": "stormhub/mrms-acquisition/0.1.0",
        "request": request,
        "records": records,
        "availability_status": "archive-retrieval-only",
        "original_publication_time": None,
    }
    receipt["acquisition_id"] = digest(receipt)
    if retained + len(canonical_bytes(receipt)) > 512 * 1024**2:
        raise PrecipitationFeatureError("MRMS receipt exceeds cache budget")
    write_immutable(path, canonical_bytes(receipt))
    return receipt


def normalize(
    acquisition: dict, raw_root: Path, *, grid: dict, support: dict, output: Path, revision: str, dirty: bool = False
) -> dict:
    """Remap only approved support cells; reject MRMS missing/no-coverage sentinels."""
    import rasterio

    validate_acquisition(acquisition, raw_root)
    validate_support(support, grid)
    indices = sorted({c["index"] for region in support["regions"] for c in region["cells"]})
    request = acquisition["request"]
    normalized = {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": grid,
        "event_start": request["start"],
        "event_end": request["end"],
        "interval_minutes": 60,
        "units": "mm",
        "measurement": "interval-depth",
        "values": [],
    }
    weights = None
    intervals = []
    for record in acquisition["records"]:
        with rasterio.open(verify_asset(raw_root, record["grib"])) as dataset:
            metadata = validate_tags(dataset.tags(1), record["end"], request["product"])
            if dataset.crs is None or not dataset.crs.is_geographic:
                raise PrecipitationFeatureError("MRMS requires its geographic native grid")
            if weights is None:
                weights = conservative_weights(dataset, grid, indices)
            values, audit = remap_interval(dataset, weights, grid)
            normalized["values"].append(values)
            intervals.append({"metadata": metadata, "grib_tags": dataset.tags(1), "volume_check": audit})
    assets = {}
    for name, value in (
        ("normalized", normalized),
        ("support", support),
        ("weights", weights),
        ("acquisition", acquisition),
    ):
        path = output / (name + ".json")
        write_immutable(path, canonical_bytes(value))
        assets[name] = descriptor(path, output)
    producer = {
        "name": "stormhub.mrms-precipitation",
        "revision": revision,
        "dirty": dirty,
        "code_sha256": digest(
            {
                n: (Path(__file__).parent / n).read_text(encoding="utf-8")
                for n in ("mrms_precipitation.py", "precipitation_remapping.py", "hrrr_acquisition.py")
            }
        ),
        "dependencies": {n: version(n) for n in ("numpy", "rasterio", "shapely", "pyproj")},
    }
    receipt = {
        "contract_version": CONTRACT,
        "request": request,
        "producer": producer,
        **assets,
        "intervals": intervals,
        "availability_status": "archive-retrieval-only",
        "forecast_eligible": False,
        "applicability": "multi-sensor-estimate-not-independent-ground-truth; no-snow-storage-or-melt",
    }
    receipt["dataset_id"] = "observed-precipitation-" + digest(receipt)[:24]
    write_immutable(output / (receipt["dataset_id"] + ".json"), canonical_bytes(receipt))
    return receipt
