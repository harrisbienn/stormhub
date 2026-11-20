"""Manufactured reference for StormHub's four-cell transposition search."""

import numpy as np
import pytest
import rioxarray  # noqa: F401 - registers the xarray raster accessor
import xarray as xr
from affine import Affine
from shapely.geometry import box

from stormhub.met.transpose import NoValidPlacementError, Transpose
from stormhub.met.aorc.aorc import AORCItem
from stormhub.met.tests.transpose_test import (
    create_test_data_array,
    create_test_transposition_domain_polygon,
    create_test_watershed_polygon,
)


@pytest.mark.parametrize("missing_corner", [False, True])
def test_four_cell_search_on_north_up_grid(missing_corner: bool) -> None:
    """Check placements, coverage, translation and statistics against known values."""
    # Each cell contains 13 * row + column. A translated 4x4 block therefore
    # has a known mean and maximum, independent of StormHub's implementation.
    values = np.arange(169, dtype=float).reshape(13, 13)
    if missing_corner:
        values[11, 11] = np.nan
    centers = np.arange(13) + 0.5
    grid = xr.DataArray(values, dims=("y", "x"), coords={"y": centers[::-1], "x": centers})
    grid = grid.rio.write_crs("EPSG:5070")
    search = Transpose(grid, box(0.1, 9.1, 3.9, 12.9), "x", "y")

    expected_shifts = {(x, y) for x in (0, 4, 8) for y in (0, 4, 8)}
    footprint = box(0, 1, 12, 13)
    if missing_corner:
        expected_shifts.remove((8, 8))
        footprint = footprint.difference(box(8, 1, 12, 5))
    assert set(search.valid_shifts) == expected_shifts
    assert search.selected_stride_cells == 4
    assert len(search.valid_shifts) == len(expected_shifts)
    assert search.valid_spaces_polygon.equals(footprint)

    polygon, transform, statistics = search.max_transpose(lambda cells: (cells.mean(), cells.max()))
    if missing_corner:
        assert polygon.equals(box(4, 1, 8, 5))
        assert transform == Affine.translation(4, -8)
        assert statistics == (129, 150)
    else:
        assert polygon.equals(box(8, 1, 12, 5))
        assert transform == Affine.translation(8, -8)
        assert statistics == (133, 154)


def historical_search():
    """Reuse the original clipped five-by-five rainfall fixture."""
    grid = create_test_data_array(create_test_transposition_domain_polygon(), 1)
    return Transpose(grid, create_test_watershed_polygon(), "longitude", "latitude")


def test_fallback_records_actual_spacing_on_item():
    """The original fixture selects six fine placements and records the winning method."""
    search = historical_search()
    item = object.__new__(AORCItem)
    item._transpose = search
    item._transposed_watershed = item._transposition_transform = item._stats = None
    item.properties = {}
    _, transform, _, _ = item.max_transpose()
    assert transform == Affine.translation(2, 0)
    assert len(search.valid_shifts) == 6
    assert item.properties["stormhub:transposition_search"] == {
        "method": "aorc-maximum-mean-transposition-v2", "stride_cells": 1
    }


@pytest.mark.parametrize("empty_geometry", [False, True])
def test_no_valid_placement(empty_geometry):
    """Reject all-missing rainfall and an empty rasterized footprint in both consumers."""
    search = historical_search()
    if empty_geometry:
        search.watershed = box(40, 40, 41, 41)
    else:
        search.data_array.values[:] = np.nan
    with pytest.raises(NoValidPlacementError, match="no_valid_placement"):
        search.max_transpose()
    with pytest.raises(NoValidPlacementError):
        _ = search.valid_spaces_polygon


def test_search_error_does_not_retry_finer_grid(monkeypatch):
    """Only an empty candidate set can start a second pass."""
    search = historical_search()
    calls = []

    def fail(stride):
        calls.append(stride)
        raise OSError("raster unavailable")

    monkeypatch.setattr(search, "_find_valid_shifts", fail)
    with pytest.raises(OSError, match="raster unavailable"):
        search.max_transpose()
    assert calls == [4]
