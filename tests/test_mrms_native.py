"""Native MRMS geographic remapping and explicit event composition tests."""

import json
from pathlib import Path

import pytest

pytest.importorskip("rasterio")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from stormhub.met.hrrr_acquisition import descriptor, write_immutable
from stormhub.met.mrms_precipitation import acquire, normalize
from stormhub.met.precipitation_event import assemble
from stormhub.met.precipitation_features import SUPPORT_CONTRACT, PrecipitationFeatureError, canonical_bytes, digest
from test_mrms_precipitation import REQUEST, provider


def test_geographic_normalization_and_known_composition(tmp_path):
    """A target entirely within the 3-mm source cell has 3 mm after normalization."""
    from pyproj import Transformer
    import rasterio

    fixture = Path(__file__).parent / "fixtures/mrms/pass2.grib2"
    with rasterio.open(fixture) as dataset:
        x, y = Transformer.from_crs(dataset.crs, "EPSG:5070", always_xy=True).transform(-92.995, 32.985)
    grid = {
        "crs": "EPSG:5070",
        "shape": [1, 1],
        "origin": [x - 50, y - 50],
        "cell_size_meters": 100,
        "row_order": "south_to_north",
    }
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "partition": None,
        "regions": [
            {
                "id": "basin",
                "kind": "basin",
                "geometry_sha256": "a" * 64,
                "area_m2": 10000,
                "cells": [{"index": 0, "area_m2": 10000}],
            }
        ],
    }
    transport, _ = provider()
    acquisition = acquire(REQUEST, tmp_path / "raw", transport=transport)
    receipt = normalize(
        acquisition, tmp_path / "raw", grid=grid, support=support, output=tmp_path / "bundle", revision="a" * 40
    )
    values = json.loads((tmp_path / "bundle/normalized.json").read_bytes())["values"]
    assert values[0] == pytest.approx([3])
    assert (
        normalize(
            acquisition, tmp_path / "raw", grid=grid, support=support, output=tmp_path / "bundle", revision="a" * 40
        )
        == receipt
    )
    plan = {
        "contract_version": "floodforecast/forecast-cycle/0.1.0",
        "event_origin": REQUEST["start"],
        "event_end": REQUEST["end"],
        "sources": [{"dataset_id": receipt["dataset_id"], "receipt_sha256": digest(receipt)}],
        "intervals": [
            {"dataset_id": receipt["dataset_id"], "row": 0, "start": REQUEST["start"], "end": REQUEST["end"]}
        ],
    }
    plan["cycle_id"] = "forecast-cycle-" + digest(plan)[:24]
    event = assemble(
        plan,
        {receipt["dataset_id"]: (receipt, tmp_path / "bundle")},
        support=support,
        output=tmp_path / "event",
        revision="a" * 40,
    )
    features = json.loads((tmp_path / "event" / event["features"]["href"]).read_bytes())
    assert features["features"]["basin"]["total_depth_mm"] == pytest.approx(3)
    assert features["features"]["basin"]["total_volume_m3"] == pytest.approx(30)
    # A second explicit normalized input exercises QPE/QPF composition independently
    # of native decoding: the selected 3-mm observation plus 4-mm forecast is 7 mm.
    second = json.loads((tmp_path / "bundle/normalized.json").read_bytes())
    second.update(event_start=REQUEST["end"], event_end="2025-10-25T19:00:00Z", values=[[4.0]])
    second_root = tmp_path / "forecast"
    write_immutable(second_root / "normalized.json", canonical_bytes(second))
    write_immutable(second_root / "support.json", canonical_bytes(support))
    second_receipt = {
        "dataset_id": "synthetic-forecast",
        "normalized": descriptor(second_root / "normalized.json", second_root),
        "support": descriptor(second_root / "support.json", second_root),
    }
    combined = {
        **plan,
        "event_end": "2025-10-25T19:00:00Z",
        "sources": plan["sources"] + [{"dataset_id": "synthetic-forecast", "receipt_sha256": digest(second_receipt)}],
        "intervals": plan["intervals"]
        + [{"dataset_id": "synthetic-forecast", "row": 0, "start": REQUEST["end"], "end": "2025-10-25T19:00:00Z"}],
    }
    combined["cycle_id"] = "forecast-cycle-" + digest({k: v for k, v in combined.items() if k != "cycle_id"})[:24]
    composite = assemble(
        combined,
        {receipt["dataset_id"]: (receipt, tmp_path / "bundle"), "synthetic-forecast": (second_receipt, second_root)},
        support=support,
        output=tmp_path / "composite",
        revision="a" * 40,
    )
    combined_features = json.loads((tmp_path / "composite" / composite["features"]["href"]).read_bytes())
    assert combined_features["features"]["basin"]["total_depth_mm"] == pytest.approx(7)
    plan["intervals"][0]["row"] = 1
    plan["cycle_id"] = "forecast-cycle-" + digest({k: v for k, v in plan.items() if k != "cycle_id"})[:24]
    with pytest.raises(PrecipitationFeatureError, match="out of range"):
        assemble(
            plan,
            {receipt["dataset_id"]: (receipt, tmp_path / "bundle")},
            support=support,
            output=tmp_path / "bad",
            revision="a" * 40,
        )
