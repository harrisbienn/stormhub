"""Portable bounded acquisition tests for NWM state exports."""

from copy import deepcopy

import pytest

from stormhub.met.nwm_state import MAX_OBJECT, acquire, selection
from stormhub.met.precipitation_features import PrecipitationFeatureError

REQUEST = {
    "cycle": "2026-11-04T10:00:00Z",
    "model_version": "v3.1",
    "product": "land",
    "lake_ids": [],
    "base_url": "https://example.test",
    "retrieval_revision": "test",
}


def test_conditional_acquisition_and_offline_integrity(tmp_path):
    """Offline reuse authenticates bytes and never calls the provider."""
    content = b"\x89HDF\r\n\x1a\nfixture"

    def transport(url, *, method, headers, limit):
        if method == "GET":
            assert headers == {"If-Match": '"version"'}
        return 200, {"etag": '"version"', "content-length": str(len(content))}, content if method == "GET" else b""

    record = acquire(REQUEST, tmp_path, transport=transport)
    assert acquire(REQUEST, tmp_path, offline=True) == record
    (tmp_path / record["asset"]["href"]).write_bytes(b"changed")
    with pytest.raises(PrecipitationFeatureError, match="checksum"):
        acquire(REQUEST, tmp_path, offline=True)


@pytest.mark.parametrize(
    "key,value",
    [
        ("model_version", "v3.0"),
        ("product", "channel"),
        ("cycle", "2026-11-04T10:01:00Z"),
        ("lake_ids", [1]),
        ("base_url", "http://example.test"),
    ],
)
def test_request_rejections(key, value):
    """Explicit product and vintage boundaries reject ambiguous requests."""
    request = deepcopy(REQUEST)
    request[key] = value
    with pytest.raises(PrecipitationFeatureError):
        selection(request)


def test_oversized_object_is_not_downloaded(tmp_path):
    """The HEAD limit is enforced before GET."""

    def transport(url, *, method, **kwargs):
        assert method == "HEAD"
        return 200, {"etag": '"version"', "content-length": str(MAX_OBJECT + 1)}, b""

    with pytest.raises(PrecipitationFeatureError, match="bounded"):
        acquire(REQUEST, tmp_path, transport=transport)
