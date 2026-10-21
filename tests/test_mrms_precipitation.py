"""Portable MRMS source/interval contracts with no network access."""

import gzip
import json
from pathlib import Path

import pytest

from stormhub.met.mrms_precipitation import acquire, interval_ends, validate_tags
from stormhub.met.precipitation_features import PrecipitationFeatureError

FIXTURE = Path(__file__).parent / "fixtures/mrms"
REQUEST = {
    "start": "2025-10-25T17:00:00Z",
    "end": "2025-10-25T18:00:00Z",
    "product": "MultiSensor_QPE_01H_Pass2",
    "base_url": "https://example.test",
    "retrieval_revision": "v1",
}


def provider(*, changed=False, content=None):
    """Return a bounded fixture provider with conditional-object semantics."""
    content = content if content is not None else gzip.compress((FIXTURE / "pass2.grib2").read_bytes(), mtime=0)
    calls = []

    def transport(url, *, method, headers, limit):
        calls.append((url, method))
        if method == "HEAD":
            return 200, {"etag": '"one"', "content-length": str(len(content))}, b""
        assert headers == {"If-Match": '"one"'}
        return 200, {"etag": '"two"' if changed else '"one"'}, content

    return transport, calls


def test_acquisition_replay_revision_and_corruption(tmp_path):
    """Complete requests replay identically; revisions create separate evidence."""
    transport, calls = provider()
    original = acquire(REQUEST, tmp_path, transport=transport)
    assert acquire(REQUEST, tmp_path, offline=True) == original
    assert len(calls) == 2
    revised = acquire({**REQUEST, "retrieval_revision": "v2"}, tmp_path, transport=transport)
    assert revised["acquisition_id"] != original["acquisition_id"]
    assert len(list(tmp_path.glob("mrms-acquisition-*.json"))) == 2
    (tmp_path / original["records"][0]["grib"]["href"]).write_bytes(b"bad")
    with pytest.raises(PrecipitationFeatureError, match="checksum or size"):
        acquire(REQUEST, tmp_path, offline=True)


@pytest.mark.parametrize("changed,content", [(True, None), (False, b"not gzip")])
def test_failed_acquisition_has_no_completion(tmp_path, changed, content):
    """Changed or malformed provider responses cannot complete an acquisition."""
    transport, _ = provider(changed=changed, content=content)
    with pytest.raises(PrecipitationFeatureError):
        acquire(REQUEST, tmp_path, transport=transport)
    assert not list(tmp_path.glob("mrms-acquisition-*.json"))


def test_observation_end_and_product_code():
    """One-hour meaning comes from the product table, not the GRIB display label."""
    tags = json.loads((FIXTURE / "tags.json").read_bytes())
    metadata = validate_tags(tags, REQUEST["end"], REQUEST["product"])
    assert metadata["start"] == "2025-10-25T17:00:00+00:00"
    for product, end in [("MultiSensor_QPE_01H_Pass1", REQUEST["end"]), (REQUEST["product"], "2025-10-25T19:00:00Z")]:
        with pytest.raises(PrecipitationFeatureError):
            validate_tags(tags, end, product)
    with pytest.raises(PrecipitationFeatureError):
        interval_ends({**REQUEST, "start": "2025-10-25T17:30:00Z"})
    with pytest.raises(PrecipitationFeatureError):
        interval_ends({**REQUEST, "product": "MultiSensor_QPE_24H_Pass2"})
