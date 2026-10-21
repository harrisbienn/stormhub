"""Bounded acquisition of one CONUS HRRR cycle's hourly surface APCP messages.

The archive index selects byte ranges; the decoder must independently validate
GRIB metadata. Last-Modified is retained as object metadata, never interpreted
as the original forecast publication time.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .precipitation_features import PrecipitationFeatureError, canonical_bytes, digest, file_sha256, utc

ACQUISITION_CONTRACT = "stormhub/hrrr-acquisition/0.1.0"
DEFAULT_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"


def validate_request(request: dict) -> None:
    """Limit this adapter to explicit v4 CONUS hourly precipitation requests."""
    if set(request) != {"initialization", "hours", "base_url", "model_version"}:
        raise PrecipitationFeatureError("HRRR request requires initialization, hours, base_url and model_version")
    cycle = utc(request["initialization"])
    if cycle.minute or cycle.second or cycle.microsecond or request["model_version"] != "hrrr-v4":
        raise PrecipitationFeatureError("expected an exact hourly HRRR v4 initialization")
    if cycle < datetime(2020, 12, 3, tzinfo=timezone.utc):
        raise PrecipitationFeatureError("cycle predates this adapter's HRRR v4 scope")
    hours = request["hours"]
    horizon = 48 if cycle.hour in (0, 6, 12, 18) else 18
    if type(hours) is not int or not 1 <= hours <= horizon:
        raise PrecipitationFeatureError(f"cycle supports at most {horizon} complete hourly intervals")
    url = urlsplit(request["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise PrecipitationFeatureError("provider base must be an HTTPS URL without credentials, query or fragment")


def select_hour(index: str, *, initialization: str, hour: int, object_size: int) -> tuple[int, int]:
    """Select exactly one hourly APCP record, rejecting duplicate or malformed offsets."""
    rows = []
    for line in index.splitlines():
        fields = line.split(":")
        if len(fields) < 7 or not fields[0].isdigit() or not fields[1].isdigit():
            raise PrecipitationFeatureError("malformed HRRR inventory")
        rows.append((int(fields[1]), fields))
    offsets = [row[0] for row in rows]
    if not offsets or offsets[0] != 0 or any(b <= a for a, b in zip(offsets, offsets[1:])):
        raise PrecipitationFeatureError("inventory offsets must increase from zero")
    if offsets[-1] >= object_size:
        raise PrecipitationFeatureError("inventory offset exceeds object size")
    clock = utc(initialization).strftime("d=%Y%m%d%H")
    matches = [
        (offset, offsets[i + 1] - 1 if i + 1 < len(rows) else object_size - 1)
        for i, (offset, fields) in enumerate(rows)
        if fields[2:6] == [clock, "APCP", "surface", f"{hour - 1}-{hour} hour acc fcst"]
    ]
    if len(matches) != 1:
        raise PrecipitationFeatureError(f"expected exactly one hourly APCP interval ending at f{hour:02d}")
    return matches[0]


def http_request(url: str, *, method: str, headers: dict, limit: int) -> tuple[int, dict, bytes]:
    """Use verified TLS and bounded reads, retrying only transient transport failures."""
    for attempt in range(3):
        try:
            with urlopen(Request(url, method=method, headers=headers), timeout=45) as response:
                content = b"" if method == "HEAD" else response.read(limit + 1)
                if len(content) > limit:
                    raise PrecipitationFeatureError("HTTP response exceeds configured byte limit")
                return response.status, {k.lower(): v for k, v in response.headers.items()}, content
        except (HTTPError, URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise PrecipitationFeatureError(f"provider HTTP {exc.code}: {url}") from exc
            if attempt == 2:
                raise PrecipitationFeatureError(f"provider unavailable after three attempts: {url}") from exc
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable retry state")


def write_immutable(path: Path, content: bytes) -> None:
    """Preserve existing evidence and refuse changes to an established artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise PrecipitationFeatureError(f"immutable artifact differs: {path.name}")
        return
    with path.open("xb") as stream:
        stream.write(content)


def descriptor(path: Path, root: Path) -> dict:
    """Describe portable bytes within a retained evidence root."""
    return {"href": path.relative_to(root).as_posix(), "sha256": file_sha256(path), "size_bytes": path.stat().st_size}


def verify_asset(root: Path, asset: dict) -> Path:
    """Authenticate an artifact before native decoding or offline replay."""
    href = asset.get("href", "")
    if not isinstance(href, str) or "\\" in href or not href or Path(href).is_absolute():
        raise PrecipitationFeatureError("asset must use a portable relative href")
    path = (root / href).resolve()
    if not path.is_relative_to(root.resolve()) or ".." in Path(href).parts:
        raise PrecipitationFeatureError("asset escapes evidence root")
    if path.stat().st_size != asset["size_bytes"] or file_sha256(path) != asset["sha256"]:
        raise PrecipitationFeatureError(f"asset checksum or size mismatch: {href}")
    return path


def validate_acquisition(receipt: dict, root: Path) -> None:
    """Check receipt identity, contiguous intervals and all retained source bytes."""
    body = {k: v for k, v in receipt.items() if k != "acquisition_id"}
    if receipt.get("contract_version") != ACQUISITION_CONTRACT or receipt.get("acquisition_id") != digest(body):
        raise PrecipitationFeatureError("acquisition identity mismatch")
    validate_request(receipt["request"])
    records = receipt["records"]
    if [r["hour"] for r in records] != list(range(1, receipt["request"]["hours"] + 1)):
        raise PrecipitationFeatureError("acquisition has missing, duplicate or reordered intervals")
    for record in records:
        verify_asset(root, record["index"])
        verify_asset(root, record["message"])
        start, end = record["byte_range"]
        if record["message"]["size_bytes"] != end - start + 1:
            raise PrecipitationFeatureError("message size does not match recorded range")


def acquire(
    request: dict,
    root: Path,
    *,
    offline: bool = False,
    transport=http_request,
    max_message_bytes: int = 8 * 1024**2,
    max_request_bytes: int = 64 * 1024**2,
    max_cache_bytes: int = 256 * 1024**2,
) -> dict:
    """Retain exact hourly GRIB messages and an immutable acquisition receipt.

    Completed requests are verified and reused without network access. A failed
    request has no completion receipt and cannot be normalized as a full cycle.
    Byte budgets cover retained indices/messages; the caller owns cache eviction.
    """
    validate_request(request)
    if not 0 < max_message_bytes <= max_request_bytes <= max_cache_bytes <= 1024**3:
        raise PrecipitationFeatureError("byte budgets must increase and cache must not exceed 1 GiB")
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / f"acquisition-{digest(request)}.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        if receipt["request"] != request:
            raise PrecipitationFeatureError("cached request differs")
        validate_acquisition(receipt, root)
        return receipt
    if offline:
        raise PrecipitationFeatureError("complete acquisition not retained for offline replay")
    retained = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    consumed = 0
    records = []
    cycle = utc(request["initialization"])

    def retain(content: bytes, suffix: str) -> dict:
        nonlocal retained, consumed
        consumed += len(content)
        path = root / (hashlib.sha256(content).hexdigest() + suffix)
        extra = 0 if path.exists() else len(content)
        if consumed > max_request_bytes or retained + extra > max_cache_bytes:
            raise PrecipitationFeatureError("acquisition exceeds request/cache byte budget")
        write_immutable(path, content)
        retained += extra
        return descriptor(path, root)

    for hour in range(1, request["hours"] + 1):
        url = f"{request['base_url'].rstrip('/')}/hrrr.{cycle:%Y%m%d}/conus/hrrr.t{cycle:%H}z.wrfsfcf{hour:02d}.grib2"
        status, head, _ = transport(url, method="HEAD", headers={}, limit=0)
        if status != 200 or not head.get("etag") or head["etag"].startswith("W/"):
            raise PrecipitationFeatureError("provider must expose a strong object ETag")
        try:
            size = int(head["content-length"])
        except (KeyError, ValueError) as exc:
            raise PrecipitationFeatureError("provider must expose object length") from exc
        status, _, content = transport(url + ".idx", method="GET", headers={}, limit=256 * 1024)
        if status != 200 or len(content) > 256 * 1024:
            raise PrecipitationFeatureError("invalid inventory response")
        index = retain(content, ".idx")
        try:
            start, end = select_hour(
                content.decode("ascii"), initialization=request["initialization"], hour=hour, object_size=size
            )
        except UnicodeDecodeError as exc:
            raise PrecipitationFeatureError("inventory is not ASCII") from exc
        if end - start + 1 > max_message_bytes or consumed + end - start + 1 > max_request_bytes:
            raise PrecipitationFeatureError("selected message exceeds acquisition byte budget")
        status, headers, content = transport(
            url,
            method="GET",
            headers={"Range": f"bytes={start}-{end}", "If-Match": head["etag"]},
            limit=end - start + 1,
        )
        if (
            status != 206
            or headers.get("content-range") != f"bytes {start}-{end}/{size}"
            or headers.get("etag") != head["etag"]
            or len(content) != end - start + 1
        ):
            raise PrecipitationFeatureError("range response or object identity changed")
        if len(content) < 20 or content[:4] != b"GRIB" or content[7] != 2 or content[-4:] != b"7777":
            raise PrecipitationFeatureError("range is not a complete GRIB2 message")
        if int.from_bytes(content[8:16], "big") != len(content):
            raise PrecipitationFeatureError("range contains truncated or multiple GRIB messages")
        records.append(
            {
                "hour": hour,
                "url": url,
                "etag": head["etag"],
                "object_size_bytes": size,
                "object_last_modified": head.get("last-modified"),
                "byte_range": [start, end],
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "index": index,
                "message": retain(content, ".grib2"),
            }
        )
    receipt = {
        "contract_version": ACQUISITION_CONTRACT,
        "request": request,
        "records": records,
        "original_publication_time": None,
        "availability_status": "archive-retrieval-only",
    }
    receipt["acquisition_id"] = digest(receipt)
    encoded = canonical_bytes(receipt)
    if retained + len(encoded) > max_cache_bytes:
        raise PrecipitationFeatureError("acquisition receipt exceeds cache byte budget")
    write_immutable(receipt_path, encoded)
    return receipt
