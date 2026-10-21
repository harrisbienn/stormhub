"""Small real GRIB decodes and conservative remapping, without network access."""

import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from stormhub.met.hrrr_acquisition import acquire
from stormhub.met.hrrr_normalization import conservative_weights, normalize, remap_interval
from stormhub.met.precipitation_features import SUPPORT_CONTRACT, PrecipitationFeatureError, digest
from test_hrrr_acquisition import REQUEST, fake_provider

FIXTURES = Path(__file__).parent / "fixtures/hrrr"


def target(dataset):
    """Split each fixture cell into four target cells with exact CRS identity."""
    x, y = dataset.transform.c, dataset.transform.f + dataset.height * dataset.transform.e
    return {
        "crs": dataset.crs.to_wkt(),
        "shape": [4, 4],
        "origin": [x, y],
        "cell_size_meters": 1500.0,
        "row_order": "south_to_north",
    }


def test_decode_known_totals_and_offline_replay(tmp_path):
    """Two decoded hours retain 7.5 mm mean, volume and south-to-north ordering."""
    provider, _ = fake_provider()
    raw = tmp_path / "raw"
    receipt = acquire(REQUEST, raw, transport=provider)
    with rasterio.open(FIXTURES / "f01.grib2") as dataset:
        grid = target(dataset)
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "partition": None,
        "regions": [
            {
                "id": "basin",
                "kind": "basin",
                "geometry_sha256": "a" * 64,
                "area_m2": 36_000_000,
                "cells": [{"index": i, "area_m2": 2_250_000} for i in range(16)],
            }
        ],
    }
    result = normalize(receipt, raw, grid=grid, support=support, output=tmp_path / "first", revision="a" * 40)
    normalized = json.loads((tmp_path / "first/normalized.json").read_bytes())
    assert normalized["values"][0] == pytest.approx([3, 3, 4, 4, 3, 3, 4, 4, 1, 1, 2, 2, 1, 1, 2, 2])
    features = json.loads((tmp_path / "first" / result["features"]["href"]).read_bytes())
    assert features["features"]["basin"]["total_depth_mm"] == pytest.approx(7.5)
    assert features["features"]["basin"]["total_volume_m3"] == pytest.approx(270_000)
    assert not result["applicability"]["forecast_eligible"]
    offline = acquire(REQUEST, raw, offline=True)
    replay = normalize(offline, raw, grid=grid, support=support, output=tmp_path / "second", revision="a" * 40)
    assert replay == result
    for path in (tmp_path / "first").iterdir():
        assert path.read_bytes() == (tmp_path / "second" / path.name).read_bytes()


def test_subset_missing_negative_and_shifted_grid(tmp_path):
    """Missing values are not dry; grid changes cannot reuse remapping weights."""
    from rasterio.io import MemoryFile

    with rasterio.open(FIXTURES / "f01.grib2") as dataset:
        grid = target(dataset)
        weights = conservative_weights(dataset, grid, [0])
        values, audit = remap_interval(dataset, weights, grid)
        assert values[0] == pytest.approx(3)
        assert values[1:] == [None] * 15
        assert audit["target_volume_m3"] == pytest.approx(6750)
        crs, transform = dataset.crs, dataset.transform
    for bad in (-1, float("nan"), -9999):
        with MemoryFile() as mem:
            with mem.open(
                driver="GTiff", height=2, width=2, count=1, dtype="float32", crs=crs, transform=transform, nodata=-9999
            ) as dataset:
                dataset.write(np.full((2, 2), bad, dtype="float32"), 1)
                with pytest.raises(PrecipitationFeatureError, match="precipitation"):
                    remap_interval(dataset, weights, grid)
    with rasterio.open(FIXTURES / "f01.grib2") as dataset:
        with pytest.raises(PrecipitationFeatureError, match="partition"):
            conservative_weights(dataset, {**grid, "origin": [grid["origin"][0] - 1000, grid["origin"][1]]}, [0])


def test_cross_crs_constant_depth_conserves_volume():
    """A different target CRS preserves a constant field on a covered footprint."""
    from pyproj import Transformer
    from rasterio.io import MemoryFile

    with rasterio.open(FIXTURES / "f01.grib2") as source:
        crs, transform = source.crs, source.transform
    x, y = Transformer.from_crs(crs, "EPSG:5070", always_xy=True).transform(3000, 3000)
    grid = {
        "crs": "EPSG:5070",
        "shape": [2, 2],
        "origin": [x - 500, y - 500],
        "cell_size_meters": 500,
        "row_order": "south_to_north",
    }
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", height=2, width=2, count=1, dtype="float32", crs=crs, transform=transform
        ) as dataset:
            dataset.write(np.full((2, 2), 3.0, dtype="float32"), 1)
            weights = conservative_weights(dataset, grid, list(range(4)))
            values, audit = remap_interval(dataset, weights, grid)
            assert values == pytest.approx([3.0] * 4)
            assert audit["target_volume_m3"] == pytest.approx(3000)
            assert audit["source_common_footprint_volume_m3"] == pytest.approx(3000)
            weights["native_grid"]["shape"] = [3, 3]
            with pytest.raises(PrecipitationFeatureError, match="grid changed"):
                remap_interval(dataset, weights, grid)
