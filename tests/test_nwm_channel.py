"""Portable NWM selection, immutability and bounded acquisition regressions."""

from copy import deepcopy

import pytest

from stormhub.met.nwm_channel import acquire, selections
from stormhub.met.precipitation_features import PrecipitationFeatureError

REQUEST = {
    "cycle": "2025-10-25T18:00:00Z",
    "product": "short_range",
    "hours": 2,
    "reach_ids": [15223060],
    "model_version": "v3.0",
    "network": "NHDPlusv2-CONUS",
    "base_url": "https://example.test",
    "retrieval_revision": "v1",
}


def test_selection_roles_and_horizons():
    """Archive analysis and forecast use different time suffixes and horizons."""
    assert selections(REQUEST)[-1]["url"].endswith("short_range.channel_rt.f002.conus.nc")
    assert selections({**REQUEST, "product": "analysis_assim", "hours": 0})[0]["url"].endswith(
        "analysis_assim.channel_rt.tm00.conus.nc"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("hours", 19),
        ("hours", True),
        ("product", "medium_range"),
        ("network", "NHDPlusHR"),
        ("model_version", "v2.1"),
        ("reach_ids", [1, 1]),
        ("cycle", "2025-10-25T18:30:00Z"),
        ("base_url", "https://user:password@example.test"),
    ],
)
def test_invalid_requests(field, value):
    """Unsupported network, temporal and selection inputs fail before acquisition."""
    with pytest.raises(PrecipitationFeatureError):
        selections({**REQUEST, field: value})


def test_acquisition_repeat_revision_and_corruption(tmp_path):
    """Completed acquisitions remain immutable and authenticate offline bytes."""
    payload = b"\x89HDF\r\n\x1a\nsynthetic-header-only"
    calls = []

    def transport(url, *, method, headers, limit):
        calls.append(method)
        if method == "GET":
            assert headers == {"If-Match": '"one"'}
        return 200, {"etag": '"one"', "content-length": str(len(payload))}, payload if method == "GET" else b""

    receipt = acquire(REQUEST, tmp_path, transport=transport)
    assert acquire(REQUEST, tmp_path, offline=True) == receipt
    assert len(calls) == 4
    revised = acquire({**REQUEST, "retrieval_revision": "v2"}, tmp_path, transport=transport)
    assert revised["acquisition_id"] != receipt["acquisition_id"]
    (tmp_path / receipt["records"][0]["asset"]["href"]).write_bytes(b"corrupt")
    with pytest.raises(PrecipitationFeatureError, match="checksum or size"):
        acquire(REQUEST, tmp_path, offline=True)


def test_changed_or_oversized_objects_never_complete(tmp_path):
    """Conditional fetch identity and byte limits precede completion receipts."""
    for size, returned_etag in [(40 * 1024**2, '"one"'), (8, '"two"')]:

        def transport(url, *, method, headers, limit):
            return (
                200,
                {"etag": '"one"' if method == "HEAD" else returned_etag, "content-length": str(size)},
                b"\x89HDF\r\n\x1a\n",
            )

        with pytest.raises(PrecipitationFeatureError):
            acquire(deepcopy(REQUEST), tmp_path, transport=transport)
    assert not list(tmp_path.glob("nwm-acquisition-*.json"))
