"""Portable acquisition and interval-contract regressions; no live provider."""

import json
from pathlib import Path

import pytest

from stormhub.met.hrrr_acquisition import acquire, select_hour, validate_request
from stormhub.met.hrrr_normalization import validate_hour_tags
from stormhub.met.precipitation_features import PrecipitationFeatureError

FIXTURES = Path(__file__).parent / "fixtures/hrrr"
REQUEST = {
    "initialization": "2025-10-25T18:00:00Z",
    "hours": 2,
    "model_version": "hrrr-v4",
    "base_url": "https://example.test",
}


def fake_provider(*, failure=None):
    """Return realistic byte-range transport responses for retained fixture bytes."""
    calls = []

    def transport(url, *, method, headers, limit):
        calls.append((url, method))
        hour = int(url.split("wrfsfcf")[1][:2])
        message = (FIXTURES / f"f{hour:02d}.grib2").read_bytes()
        inventory = f"1:0:d=2025102518:APCP:surface:{hour - 1}-{hour} hour acc fcst:\n".encode()
        meta = {"etag": '"fixture"', "content-length": str(len(message)), "last-modified": "fixture-time"}
        if method == "HEAD":
            return 200, meta, b""
        if url.endswith(".idx"):
            return 200, {}, inventory
        assert headers == {"Range": f"bytes=0-{len(message) - 1}", "If-Match": '"fixture"'}
        assert limit == len(message)
        if failure == "missing-hour" and hour == 2:
            raise PrecipitationFeatureError("missing hour")
        if failure == "changed-etag":
            meta["etag"] = '"changed"'
        meta["content-range"] = f"bytes 0-{len(message) - 1}/{len(message)}"
        return 200 if failure == "ignored-range" else 206, meta, message

    return transport, calls


def test_acquire_offline_identity_and_corruption(tmp_path):
    """Retrying a complete request uses the same receipt; corruption fails closed."""
    provider, calls = fake_provider()
    first = acquire(REQUEST, tmp_path, transport=provider)
    count = len(calls)
    assert acquire(REQUEST, tmp_path, offline=True, transport=provider) == first
    assert len(calls) == count
    assert first["availability_status"] == "archive-retrieval-only"
    assert first["original_publication_time"] is None
    path = tmp_path / first["records"][0]["message"]["href"]
    path.write_bytes(b"corruption")
    with pytest.raises(PrecipitationFeatureError, match="checksum or size"):
        acquire(REQUEST, tmp_path, offline=True)


@pytest.mark.parametrize("failure", ["missing-hour", "changed-etag", "ignored-range"])
def test_partial_or_changed_provider_never_completes(tmp_path, failure):
    """No complete acquisition is emitted for a partial cycle or changed object."""
    provider, _ = fake_provider(failure=failure)
    with pytest.raises(PrecipitationFeatureError):
        acquire(REQUEST, tmp_path, transport=provider)
    assert not list(tmp_path.glob("acquisition-*.json"))
    with pytest.raises(PrecipitationFeatureError, match="offline"):
        acquire(REQUEST, tmp_path, offline=True)


def test_incomplete_cycle_and_budgets(tmp_path):
    """An 18-hour cycle cannot supply 24 hours; small budgets stop before transfer."""
    with pytest.raises(PrecipitationFeatureError, match="18"):
        validate_request({**REQUEST, "initialization": "2025-10-25T19:00:00Z", "hours": 24})
    provider, calls = fake_provider()
    with pytest.raises(PrecipitationFeatureError, match="budget"):
        acquire(
            REQUEST, tmp_path, transport=provider, max_message_bytes=100, max_request_bytes=1000, max_cache_bytes=1000
        )
    assert len(calls) == 2


def test_hourly_selection_never_uses_cumulative():
    """Surface hourly APCP must be unique even when cumulative APCP is present."""
    cumulative = "1:0:d=2025102518:APCP:surface:0-2 hour acc fcst:\n"
    hourly = "2:100:d=2025102518:APCP:surface:1-2 hour acc fcst:\n"
    assert select_hour(cumulative + hourly, initialization=REQUEST["initialization"], hour=2, object_size=200) == (
        100,
        199,
    )
    with pytest.raises(PrecipitationFeatureError, match="exactly one"):
        select_hour(cumulative, initialization=REQUEST["initialization"], hour=2, object_size=200)
    with pytest.raises(PrecipitationFeatureError, match="exactly one"):
        select_hour(
            hourly.replace("2:100", "1:0") + hourly, initialization=REQUEST["initialization"], hour=2, object_size=200
        )


@pytest.mark.parametrize(
    "field,value", [("GRIB_REF_TIME", "0"), ("GRIB_VALID_TIME", "0"), ("GRIB_UNIT", "[mm/s]"), ("GRIB_PDS_PDTN", "0")]
)
def test_wrong_grib_semantics(field, value):
    """GRIB metadata independently authenticates initialization, interval and units."""
    tags = json.loads((FIXTURES / "f02-tags.json").read_bytes())
    assert validate_hour_tags(tags, REQUEST["initialization"], 2)["duration_seconds"] == 3600
    with pytest.raises(PrecipitationFeatureError):
        validate_hour_tags({**tags, field: value}, REQUEST["initialization"], 2)


def test_cumulative_grib_rejected_despite_hourly_label():
    """A misleading display label cannot override the interval duration."""
    tags = json.loads((FIXTURES / "f02-tags.json").read_bytes())
    values = tags["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"].split()
    values[26] = "2"
    tags["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"] = " ".join(values)
    with pytest.raises(PrecipitationFeatureError):
        validate_hour_tags(tags, REQUEST["initialization"], 2)


def test_transient_transport_retry_and_permanent_failure(monkeypatch):
    """Retry transient responses while preserving the conditional request."""
    from urllib.error import HTTPError
    from stormhub.met import hrrr_acquisition as module

    calls = []

    class Response:
        """A small context-managed HTTP response."""

        status = 206
        headers = {"ETag": '"one"'}

        def __enter__(self):
            """Enter the response context."""
            return self

        def __exit__(self, *args):
            """Leave exceptions visible."""
            return False

        def read(self, limit):
            """Return bounded fixture bytes."""
            return b"abc"

    def open_request(request, timeout):
        calls.append(request)
        if len(calls) < 3:
            raise HTTPError(request.full_url, 503, "temporary", {}, None)
        return Response()

    monkeypatch.setattr(module, "urlopen", open_request)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.http_request("https://example.test", method="GET", headers={"If-Match": '"one"'}, limit=3) == (
        206,
        {"etag": '"one"'},
        b"abc",
    )
    assert len(calls) == 3
    assert all(r.get_header("If-match") == '"one"' for r in calls)

    def changed(request, timeout):
        raise HTTPError(request.full_url, 412, "changed", {}, None)

    monkeypatch.setattr(module, "urlopen", changed)
    with pytest.raises(PrecipitationFeatureError, match="412"):
        module.http_request("https://example.test", method="GET", headers={}, limit=3)
