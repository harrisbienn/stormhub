"""Manufactured reference for StormHub's four-cell transposition search."""

import numpy as np
import pytest
import rioxarray  # noqa: F401 - registers the xarray raster accessor
import xarray as xr
from affine import Affine
from shapely.geometry import box

from stormhub.met.transpose import Transpose


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
