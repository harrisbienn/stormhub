"""Optional native and geometric checks; no installed HMS or RAS is needed."""

import pytest

from stormhub.met.precipitation_adapters import build_spatial_support, read_dss
from stormhub.met.precipitation_features import (
    PrecipitationFeatureError,
    file_sha256,
    extract_features,
    digest,
    SUPPORT_CONTRACT,
)


def test_fractional_geometry_and_partition():
    """Actual polygon intersections preserve fractions and full region area."""
    pytest.importorskip("pyproj")
    geometry = pytest.importorskip("shapely.geometry")
    grid = {
        "crs": "EPSG:5070",
        "shape": [1, 2],
        "origin": [0, 0],
        "cell_size_meters": 1000,
        "row_order": "south_to_north",
    }
    regions = [
        {"id": name, "kind": kind, "geometry": geometry.mapping(geometry.box(*bounds))}
        for name, kind, bounds in [
            ("basin", "basin", (0, 0, 1500, 1000)),
            ("west", "subbasin", (0, 0, 1000, 1000)),
            ("east", "subbasin", (1000, 0, 1500, 1000)),
        ]
    ]
    support = build_spatial_support(
        grid, regions, region_crs="EPSG:5070", partition={"parent": "basin", "children": ["east", "west"]}
    )
    basin = next(r for r in support["regions"] if r["id"] == "basin")
    assert basin["cells"] == [{"index": 0, "area_m2": 1000000}, {"index": 1, "area_m2": 500000}]
    regions[2]["geometry"] = regions[1]["geometry"]
    with pytest.raises(PrecipitationFeatureError):
        build_spatial_support(
            grid, regions, region_crs="EPSG:5070", partition={"parent": "basin", "children": ["east", "west"]}
        )


def test_real_dss_preserves_zero_missing_row_order_and_original_bytes(tmp_path):
    """Round-trip a small actual DSS asset separately from portable unit tests."""
    hecdss = pytest.importorskip("hecdss")
    np = pytest.importorskip("numpy")
    from hecdss.gridded_data import GriddedData

    path = tmp_path / "rain.dss"
    with hecdss.HecDss(str(path)) as dss:
        for hour, values in [(0, [[0, 2], [4, 6]]), (1, [[1, 3], [5, np.nan]])]:
            pathname = f"/SHG/TEST/PRECIPITATION/01JAN2020:0{hour}00/01JAN2020:0{hour + 1}00/TARGET/"
            grid = GriddedData.create(
                path=pathname,
                data=np.array(values, dtype=float),
                dataType=1,
                isInterval=0,
                timeZoneID="UTC",
                cellSize=1000,
                lowerLeftCellX=10,
                lowerLeftCellY=20,
            )
            dss.put(grid)
    source = {"sha256": file_sha256(path), "size_bytes": path.stat().st_size}
    normalized = read_dss(
        path,
        source=source,
        selector="/SHG/TEST/PRECIPITATION/*/*/TARGET/",
        event_start="2020-01-01T00:00:00Z",
        event_end="2020-01-01T02:00:00Z",
    )
    assert normalized["values"] == [[0, 2, 4, 6], [1, 3, 5, None]]
    assert normalized["grid"]["origin"] == [10000, 20000]
    assert normalized["grid"]["shape"] == [2, 2]
    assert file_sha256(path) == source["sha256"]
    source.update(collection_id="synthetic", item_id="known-rain", asset_key="target", role="target-precipitation")
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(normalized["grid"]),
        "partition": None,
        "regions": [
            {
                "id": "south",
                "kind": "domain",
                "geometry_sha256": "a" * 64,
                "area_m2": 2000000,
                "cells": [{"index": 0, "area_m2": 1000000}, {"index": 1, "area_m2": 1000000}],
            }
        ],
    }
    extractor = {
        "name": "stormhub.precipitation-features",
        "version": "0.1.0",
        "revision": "b" * 40,
        "dirty": False,
        "code_sha256": "c" * 64,
        "dependencies": {},
    }
    static = extract_features(normalized, source=source, support=support, extractor=extractor)
    incoming = {**normalized, "values": [[0, 2, 4, 6], [1, 3, 5, None]]}
    forecast = extract_features(
        incoming, source={**source, "role": "incoming-precipitation"}, support=support, extractor=extractor
    )
    assert static["features"] == forecast["features"]
    assert static["features"]["south"]["total_depth_mm"] == 3
    with pytest.raises(PrecipitationFeatureError, match="complete event"):
        read_dss(
            path,
            source=source,
            selector="/SHG/TEST/PRECIPITATION/*/*/TARGET/",
            event_start="2020-01-01T00:00:00Z",
            event_end="2020-01-01T03:00:00Z",
        )
