"""Tests for staged AORC-to-DSS processing."""

from datetime import datetime

import numpy as np
import pandas as pd
from pyproj import CRS
import xarray as xr

from stormhub.met.zarr_to_dss import (
    NOAADataVariable,
    noaa_zarr_to_dss,
    prepare_noaa_variable_for_dss,
    reproject_to_shg,
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
