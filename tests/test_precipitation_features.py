"""Known-value contracts for rainfall descriptors, independent of HEC software."""

import copy
import json
from pathlib import Path

import pytest

from stormhub.met.precipitation_features import (
    NORMALIZED_CONTRACT,
    SUPPORT_CONTRACT,
    PrecipitationFeatureError,
    digest,
    extract_features,
    file_sha256,
    write_features,
    canonical_bytes,
)
from stormhub.met.precipitation_adapters import extract_normalized_file


def test_versioned_fixture_reproduces_export():
    """Real JSON contracts reproduce the reviewed package export byte-for-value."""
    folder = Path(__file__).parent / "fixtures/precipitation"
    expected = json.loads((folder / "features.json").read_bytes())
    actual = extract_normalized_file(
        folder / "normalized.json",
        source=json.loads((folder / "source.json").read_bytes()),
        support=json.loads((folder / "support.json").read_bytes()),
        extractor=expected["extractor"],
    )
    assert actual == expected


def test_canonical_identity_preserves_unicode():
    """Cross-package identities use UTF-8 rather than ASCII escape sequences."""
    assert canonical_bytes({"region": "Région"}) == '{"region":"Région"}'.encode("utf-8")


@pytest.fixture
def rainfall():
    """Provide independently calculated fractional-area rainfall inputs."""
    grid = {
        "crs": "EPSG:5070",
        "shape": [1, 2],
        "origin": [0, 0],
        "cell_size_meters": 1000,
        "row_order": "south_to_north",
    }
    normalized = {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": grid,
        "event_start": "2020-01-01T00:00:00Z",
        "event_end": "2020-01-01T03:00:00Z",
        "interval_minutes": 60,
        "units": "mm",
        "measurement": "interval-depth",
        "values": [[2, 8], [4, 2], [0, 6]],
    }
    regions = []
    for name, kind, weights in [
        ("basin", "basin", [(0, 1000000), (1, 500000)]),
        ("west", "subbasin", [(0, 1000000)]),
        ("east", "subbasin", [(1, 500000)]),
    ]:
        regions.append(
            {
                "id": name,
                "kind": kind,
                "geometry_sha256": digest(name),
                "area_m2": sum(w for _, w in weights),
                "cells": [{"index": i, "area_m2": w} for i, w in weights],
            }
        )
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "regions": regions,
        "partition": {"parent": "basin", "children": ["west", "east"]},
    }
    source = {
        "collection_id": "targets",
        "item_id": "rain",
        "asset_key": "dss",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "role": "target-precipitation",
    }
    extractor = {
        "name": "stormhub.precipitation-features",
        "version": "0.1.0",
        "revision": "b" * 40,
        "dirty": False,
        "code_sha256": "c" * 64,
        "dependencies": {},
    }
    return normalized, dict(source=source, support=support, extractor=extractor)


def test_known_totals_volumes_windows_and_distribution(rainfall):
    """Fractional cells conserve volume without averaging subbasin means."""
    normalized, arguments = rainfall
    result = extract_features(normalized, **arguments)
    basin = result["features"]["basin"]
    assert basin["interval_depth_mm"] == pytest.approx([4, 10 / 3, 2])
    assert basin["total_depth_mm"] == pytest.approx(28 / 3)
    assert basin["total_volume_m3"] == pytest.approx(14000)
    assert basin["temporal_fraction"] == pytest.approx([3 / 7, 5 / 14, 3 / 14])
    assert basin["centroid_minute"] == pytest.approx(540 / 7)
    assert basin["maxima"]["60"] == {"depth_mm": 4, "end_minute": 60, "status": "complete"}
    assert basin["maxima"]["180"]["depth_mm"] == pytest.approx(28 / 3)
    assert basin["maxima"]["360"]["status"] == "unsupported_window"
    assert result["spatial_allocation"] == pytest.approx({"west": 3 / 7, "east": 4 / 7})


def test_missing_cells_and_outside_grid_are_not_zero(rainfall):
    """Incomplete support exposes coverage and observed volume, never a full total."""
    normalized, arguments = rainfall
    normalized["values"][0][1] = None
    result = extract_features(normalized, **arguments)
    basin = result["features"]["basin"]
    assert not result["complete"]
    assert basin["coverage_fraction"][0] == pytest.approx(2 / 3)
    assert basin["observed_volume_m3"][0] == 2000
    assert basin["total_depth_mm"] is None
    assert basin["maxima"]["60"]["status"] == "incomplete_region"
    assert result["features"]["west"]["total_depth_mm"] == 6
    arguments["support"]["partition"] = None
    arguments["support"]["regions"][1]["area_m2"] = 2000000
    assert extract_features(normalized, **arguments)["features"]["west"]["total_depth_mm"] is None


def test_dry_event_and_units(rainfall):
    """Dry observations stay zero; fractions are undefined rather than fabricated."""
    normalized, arguments = rainfall
    millimeters = extract_features(normalized, **arguments)
    normalized["units"] = "in"
    normalized["values"] = [[v / 25.4 for v in row] for row in normalized["values"]]
    inches = extract_features(normalized, **arguments)
    assert inches["features"]["basin"]["total_volume_m3"] == pytest.approx(14000)
    assert inches["features"]["west"]["total_depth_mm"] == pytest.approx(
        millimeters["features"]["west"]["total_depth_mm"]
    )
    normalized["values"] = [[0, 0]] * 3
    dry = extract_features(normalized, **arguments)
    assert dry["complete"]
    assert dry["features"]["basin"]["total_depth_mm"] == 0
    assert dry["features"]["basin"]["temporal_fraction"] is None
    assert dry["spatial_allocation"] == {"east": None, "west": None}


def test_windows_do_not_interpolate_coarser_intervals(rainfall):
    """A short requested window cannot be invented from coarser interval depths."""
    normalized, arguments = rainfall
    normalized.update(interval_minutes=90, event_end="2020-01-01T04:30:00Z")
    result = extract_features(normalized, **arguments)
    assert result["features"]["basin"]["maxima"]["60"]["status"] == "unsupported_window"
    assert result["features"]["basin"]["maxima"]["180"]["depth_mm"] == pytest.approx(22 / 3)


def test_dss_midnight_parser_is_locale_independent():
    """DSS previous-day 2400 and ordinary midnight denote the same instant."""
    from stormhub.met.precipitation_adapters import _dss_time

    assert _dss_time("31DEC2019:2400") == _dss_time("01JAN2020:0000")
    with pytest.raises(PrecipitationFeatureError):
        _dss_time("31DEC2019:2401")


@pytest.mark.parametrize("mutation", ["negative", "nan", "rate", "gap", "local-clock", "excess", "partition"])
def test_invalid_contracts_fail_closed(rainfall, mutation):
    """Ambiguous scientific inputs must fail before entering a feature cache."""
    normalized, arguments = rainfall
    if mutation == "negative":
        normalized["values"][0][0] = -1
    elif mutation == "nan":
        normalized["values"][0][0] = float("nan")
    elif mutation == "rate":
        normalized["measurement"] = "rate"
    elif mutation == "gap":
        normalized["values"].pop()
    elif mutation == "local-clock":
        normalized["event_start"] = "2020-01-01T00:00:00"
    elif mutation == "excess":
        arguments["source"]["role"] = "excess-rainfall"
    else:
        arguments["support"]["regions"][2]["cells"][0]["index"] = 0
    with pytest.raises(PrecipitationFeatureError):
        extract_features(normalized, **arguments)


@pytest.mark.parametrize("identity", ["source", "geometry", "extractor", "definition", "values"])
def test_changed_identity_invalidates_cache(rainfall, identity):
    """All result-defining inputs change the immutable feature identity."""
    normalized, arguments = rainfall
    original = extract_features(normalized, **arguments)
    if identity == "source":
        arguments["source"]["sha256"] = "d" * 64
    elif identity == "geometry":
        arguments["support"]["regions"][0]["geometry_sha256"] = "d" * 64
    elif identity == "extractor":
        arguments["extractor"]["revision"] = "d" * 40
    elif identity == "definition":
        arguments["definition"] = copy.deepcopy(original["definition"])
        arguments["definition"]["window_minutes"] = [60, 120]
    else:
        normalized["values"][0][0] += 1
    assert extract_features(normalized, **arguments)["feature_id"] != original["feature_id"]


def test_static_and_incoming_share_descriptors_and_immutable_export(rainfall, tmp_path):
    """Equivalent rainfall takes the same calculation path regardless of its role."""
    normalized, arguments = rainfall
    source_file = tmp_path / "rainfall.json"
    source_file.write_text(json.dumps(normalized), encoding="utf-8")
    arguments["source"].update(sha256=file_sha256(source_file), size_bytes=source_file.stat().st_size)
    static = extract_normalized_file(source_file, **arguments)
    arguments["source"]["role"] = "incoming-precipitation"
    incoming = extract_normalized_file(source_file, **arguments)
    assert static["features"] == incoming["features"]
    assert static["normalized_values_sha256"] == incoming["normalized_values_sha256"]
    assert static["feature_id"] != incoming["feature_id"]
    output = write_features(static, tmp_path)
    assert write_features(static, tmp_path) == output
    output.write_bytes(b"different")
    with pytest.raises(PrecipitationFeatureError, match="overwrite"):
        write_features(static, tmp_path)
    source_file.write_bytes(b"changed")
    with pytest.raises(PrecipitationFeatureError, match="checksum"):
        extract_normalized_file(source_file, **arguments)
