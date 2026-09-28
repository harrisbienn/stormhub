"""Tests for staged AORC-to-DSS processing."""

from datetime import datetime
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import Point, box
from shapely.ops import transform
import xarray as xr

from stormhub.met.zarr_to_dss import (
    NOAADataVariable,
    calculate_shg_translation,
    get_noaa_data_for_dss,
    noaa_zarr_to_dss,
    noaa_zarr_to_dss_products,
    prepare_noaa_variable_for_dss,
    reproject_to_shg,
    required_source_footprint,
    remove_existing_dss_files,
    translate_shg_data,
    validate_dss_record_counts,
    validate_translated_shg_data,
)


def test_prepare_noaa_variable_selects_requested_event_window() -> None:
    """Select hourly values after the exclusive event start through its duration."""
    storm_start = datetime(2020, 1, 1)
    times = pd.date_range("2020-01-01T01:00:00", periods=4, freq="h")
    dataset = xr.Dataset(
        {
            NOAADataVariable.APCP.value: (
                ("time", "latitude", "longitude"),
                np.ones((4, 2, 2)),
            )
        },
        coords={"time": times, "latitude": [30.0, 29.0], "longitude": [-91.0, -90.0]},
    )

    selected = prepare_noaa_variable_for_dss(dataset, NOAADataVariable.APCP, storm_start, 2)

    assert selected.sizes["time"] == 2
    assert selected.time.values[0] == np.datetime64("2020-01-01T01:00:00")
    assert selected.time.values[-1] == np.datetime64("2020-01-01T02:00:00")


def test_reproject_to_shg_returns_requested_grid_resolution() -> None:
    """Project a small WGS84 time series onto a one-kilometer SHG grid."""
    data = xr.DataArray(
        np.ones((2, 3, 3)),
        dims=("time", "latitude", "longitude"),
        coords={
            "time": pd.date_range("2020-01-01T01:00:00", periods=2, freq="h"),
            "latitude": [31.0, 30.5, 30.0],
            "longitude": [-91.0, -90.5, -90.0],
        },
    )
    data.rio.set_spatial_dims(x_dim="longitude", y_dim="latitude", inplace=True)
    data.rio.write_crs("EPSG:4326", inplace=True)

    projected = reproject_to_shg(data, output_resolution_km=1)

    assert CRS(projected.rio.crs).equals(CRS.from_epsg(5070))
    assert tuple(abs(value) for value in projected.rio.resolution()) == (1000.0, 1000.0)
    assert projected.sizes["time"] == 2


def test_noaa_zarr_to_dss_runs_explicit_processing_stages(monkeypatch) -> None:
    """Run retrieval, preparation, reprojection, and writing as distinct stages."""
    calls = []
    sentinel_dataset = object()
    sentinel_source = object()
    sentinel_projected = object()

    def fake_get(*args):
        calls.append(("get", args))
        return sentinel_dataset

    def fake_prepare(*args):
        calls.append(("prepare", args))
        return sentinel_source

    def fake_reproject(*args):
        calls.append(("reproject", args))
        return sentinel_projected

    def fake_write(**kwargs):
        calls.append(("write", kwargs))

    monkeypatch.setattr("stormhub.met.zarr_to_dss.get_noaa_data_for_dss", fake_get)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.prepare_noaa_variable_for_dss", fake_prepare)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.reproject_to_shg", fake_reproject)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.write_shg_to_dss", fake_write)

    noaa_zarr_to_dss(
        output_dss_path="event.dss",
        aoi_geometry_gpkg_path="domain.json",
        aoi_name="TEST",
        storm_start=datetime(2020, 1, 1),
        variable_duration_map={NOAADataVariable.APCP: 24},
        output_resolution_km=1,
    )

    assert [name for name, _ in calls] == ["get", "prepare", "reproject", "write"]
    assert calls[-1][1]["data"] is sentinel_projected


def test_remove_existing_dss_files_makes_reruns_fresh(tmp_path) -> None:
    """Remove an old DSS product while tolerating outputs that do not exist yet."""
    existing = tmp_path / "event.dss"
    existing.write_bytes(b"old-dss")

    remove_existing_dss_files([str(existing), str(existing), str(tmp_path / "missing.dss")])

    assert not existing.exists()


def test_calculate_shg_translation_snaps_to_output_cells() -> None:
    """Calculate source-to-target offsets as whole SHG cells."""
    translation = calculate_shg_translation(
        source_geometry=Point(-92.8, 32.5),
        target_geometry=Point(-92.4, 32.7),
        output_resolution_km=1,
    )

    assert translation["direction"] == "source-to-target"
    assert translation["x_offset_m"] == translation["x_offset_cells"] * 1000
    assert translation["y_offset_m"] == translation["y_offset_cells"] * 1000
    assert abs(translation["x_snap_residual_m"]) <= 500
    assert abs(translation["y_snap_residual_m"]) <= 500


def test_translate_shg_data_moves_coordinates_without_changing_values() -> None:
    """Translate SHG coordinates while preserving every precipitation value."""
    data = xr.DataArray(
        np.arange(18, dtype=float).reshape((2, 3, 3)),
        dims=("time", "y", "x"),
        coords={
            "time": pd.date_range("2020-01-01T01:00:00", periods=2, freq="h"),
            "y": [2500.0, 1500.0, 500.0],
            "x": [500.0, 1500.0, 2500.0],
        },
    )
    data.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
    data.rio.write_crs("EPSG:5070", inplace=True)

    translated = translate_shg_data(data, x_offset_m=2000, y_offset_m=-1000)

    np.testing.assert_array_equal(translated.values, data.values)
    np.testing.assert_array_equal(translated.x.values, data.x.values + 2000)
    np.testing.assert_array_equal(translated.y.values, data.y.values - 1000)
    assert translated.rio.resolution() == data.rio.resolution()


def test_validate_translated_shg_data_accepts_clipped_subset() -> None:
    """Validate a target grid that is a spatial subset of translated source data."""
    source = xr.DataArray(
        np.arange(18, dtype=float).reshape((2, 3, 3)),
        dims=("time", "y", "x"),
        coords={
            "time": pd.date_range("2020-01-01T01:00:00", periods=2, freq="h"),
            "y": [2500.0, 1500.0, 500.0],
            "x": [500.0, 1500.0, 2500.0],
        },
    )
    source.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
    source.rio.write_crs("EPSG:5070", inplace=True)
    target = translate_shg_data(source, 2000, -1000).isel(x=slice(1, 3), y=slice(0, 2))

    result = validate_translated_shg_data(source, target, 2000, -1000)

    assert result["status"] == "passed"
    assert result["values_preserved"] is True
    assert result["target_geometry_covered"] is True
    assert result["target_rows"] == 2
    assert result["target_columns"] == 2


def test_validate_dss_record_counts_reopens_written_file(monkeypatch) -> None:
    """Count requested meteorological records from a reopened DSS catalog."""
    class FakeDss:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_catalog(self):
            return [
                "/SHG1K/TEST/PRECIPITATION/01JAN2020:0000/01JAN2020:0100/AORC/",
                "/SHG1K/TEST/PRECIPITATION/01JAN2020:0100/01JAN2020:0200/AORC/",
            ]

    monkeypatch.setattr("stormhub.met.zarr_to_dss.HecDss", lambda _path: FakeDss())

    result = validate_dss_record_counts("event.dss", {NOAADataVariable.APCP: 2})

    assert result["status"] == "passed"
    assert result["actual_records"] == {"PRECIPITATION": 2}


def test_dss_products_writes_source_and_target_from_one_retrieval(monkeypatch) -> None:
    """Create source and target products while retrieving AORC only once."""
    calls = []
    sentinel_dataset = object()
    sentinel_source = object()
    sentinel_shg = object()
    sentinel_target = object()
    sentinel_clipped = object()
    translation = {"x_offset_m": 1000, "y_offset_m": -2000}

    def fake_get(*args, **kwargs):
        assert kwargs["required_geometry"].covers(Point(-92.4, 32.7))
        calls.append("get")
        return sentinel_dataset

    def fake_prepare(*args):
        calls.append("prepare")
        return sentinel_source

    def fake_reproject(*args):
        calls.append("reproject")
        return sentinel_shg

    def fake_calculate(*args):
        calls.append("calculate")
        return translation

    def fake_translate(*args):
        calls.append("translate")
        return sentinel_target

    def fake_clip(*args, **kwargs):
        calls.append("clip")
        return sentinel_clipped

    def fake_write(**kwargs):
        calls.append(("write", kwargs["output_dss_path"], kwargs["data"]))

    monkeypatch.setattr("stormhub.met.zarr_to_dss.get_noaa_data_for_dss", fake_get)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.prepare_noaa_variable_for_dss", fake_prepare)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.reproject_to_shg", fake_reproject)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.calculate_shg_translation", fake_calculate)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.translate_shg_data", fake_translate)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.clip_shg_data_to_geometry", fake_clip)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.write_shg_to_dss", fake_write)
    monkeypatch.setattr(
        "stormhub.met.zarr_to_dss.validate_translated_shg_data",
        lambda *_args, **_kwargs: {"status": "passed", "values_preserved": True},
    )
    monkeypatch.setattr(
        "stormhub.met.zarr_to_dss.validate_dss_record_counts",
        lambda *_args, **_kwargs: {"status": "passed"},
    )

    result = noaa_zarr_to_dss_products(
        output_dss_paths={"source": "source.dss", "target": "target.dss"},
        aoi_geometry_path="domain.json",
        aoi_name="TEST",
        storm_start=datetime(2020, 1, 1),
        variable_duration_map={NOAADataVariable.APCP: 24},
        output_resolution_km=1,
        source_geometry=Point(-92.8, 32.5),
        target_geometry=Point(-92.4, 32.7),
    )

    writes = [call for call in calls if isinstance(call, tuple) and call[0] == "write"]
    assert calls.count("get") == 1
    assert writes == [
        ("write", "source.dss", sentinel_shg),
        ("write", "target.dss", sentinel_clipped),
    ]
    assert result["x_offset_m"] == translation["x_offset_m"]
    assert result["spatial_validation"]["PRECIPITATION"]["status"] == "passed"
    assert result["dss_validation"]["target"]["status"] == "passed"


def test_retrieval_includes_inverse_target_footprint(monkeypatch) -> None:
    """Fetch real precipitation beyond the storm-placement boundary when required."""
    to_wgs84 = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True).transform
    target = transform(to_wgs84, box(150000, 960000, 440000, 1316000))
    required = required_source_footprint(target, {"x_offset_m": 181000, "y_offset_m": -217000}, 5, 1)
    original = transform(to_wgs84, box(-24000, 730000, 630000, 1546000))
    assert not original.covers(required)
    monkeypatch.setattr("stormhub.met.zarr_to_dss.gpd.read_file", lambda _: gpd.GeoDataFrame(geometry=[original], crs=4326))
    captured = []

    def retrieve(paths, geometry, start, end, variables):
        captured.append(geometry.geometry.iloc[0])
        return "dataset"

    monkeypatch.setattr("stormhub.met.zarr_to_dss.get_s3_zarr_data", retrieve)
    result = get_noaa_data_for_dss("domain.json", datetime(2020, 1, 1), {NOAADataVariable.APCP: 72}, required_geometry=required)
    assert result == "dataset"
    assert original.difference(captured[0]).area < 1e-12
    assert required.difference(captured[0]).area < 1e-12
    projected = transform(Transformer.from_crs(4326, 5070, always_xy=True).transform, required)
    assert projected.bounds[0] < -31000 - 5000
    assert projected.bounds[2] > 259000 + 5000


def test_validation_rejects_nodata_hole_inside_watershed() -> None:
    """A grid envelope and some preserved values cannot hide missing watershed cells."""
    source = xr.DataArray(
        np.ones((2, 3, 3)), dims=("time", "y", "x"),
        coords={"time": pd.date_range("2020-01-01", periods=2, freq="h"), "y": [2500., 1500., 500.], "x": [500., 1500., 2500.]},
    ).rio.write_crs("EPSG:5070")
    source.values[0, 1, 1] = np.nan
    target = translate_shg_data(source, 0, 0)
    to_wgs84 = Transformer.from_crs(5070, 4326, always_xy=True).transform
    watershed = transform(to_wgs84, box(100, 100, 2900, 2900))
    result = validate_translated_shg_data(source, target, 0, 0, target_geometry=watershed)
    assert result["target_geometry_covered"] is True
    assert result["values_preserved"] is True
    assert result["target_valid_data_covered"] is False
    assert result["missing_watershed_cell_values"] == 1
    assert result["status"] == "failed"


@pytest.mark.parametrize("missing", [False, True])
def test_dss_readback_checks_watershed_values(monkeypatch, missing) -> None:
    """Accept zero precipitation but reject native DSS nodata within the watershed."""
    from hecdss.gridded_data import NULL_INT

    data = np.zeros((3, 3))
    if missing:
        data[1, 1] = NULL_INT
    grid = SimpleNamespace(data=data, cellSize=1000, lowerLeftCellX=0, lowerLeftCellY=0,
                           xCoordOfGridCellZero=0, yCoordOfGridCellZero=0, numberOfCellsX=3, numberOfCellsY=3)

    class FakeDss:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_catalog(self):
            return ["/SHG1K/TEST/PRECIPITATION/01JAN2020:0000/01JAN2020:0100/AORC/"]

        def get(self, _path):
            return grid

    monkeypatch.setattr("stormhub.met.zarr_to_dss.HecDss", lambda _: FakeDss())
    watershed = transform(Transformer.from_crs(5070, 4326, always_xy=True).transform, box(100, 100, 2900, 2900))
    result = validate_dss_record_counts("test.dss", {NOAADataVariable.APCP: 1}, target_geometry=watershed)
    assert result["actual_records"] == {"PRECIPITATION": 1}
    assert result["target_coverage_failed_records"] == int(missing)
    assert result["status"] == ("failed" if missing else "passed")
