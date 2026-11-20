"""Class to handle transposition functionality."""

from typing import Any, Callable

import numpy as np
import xarray as xr
from affine import Affine
from rasterio.features import shapes
from rasterio.mask import geometry_mask
from rasterio.windows import Window, get_data_window
from shapely import Polygon
from shapely.affinity import translate
from shapely.geometry import shape

from stormhub.met.consts import TRANSLATION_STRIDES


class NoValidPlacementError(ValueError):
    """Neither search spacing fits a nonempty watershed entirely on finite cells."""

    code = "no_valid_placement"


class Transpose:
    """
    A class to handle the transposition of a watershed mask over a data array.

    Attributes
    ----------
        data_array (xr.DataArray): The data array to be transposed.
        watershed (Polygon): The watershed polygon.
        x_var (str): The x variable name in the data array.
        y_var (str): The y variable name in the data array.
    """

    def __init__(self, data_array: xr.DataArray, watershed: Polygon, x_var: str, y_var: str) -> None:
        """
        Initialize the Transpose class.

        Args:
            data_array (xr.DataArray): The data array to be transposed.
            watershed (Polygon): The watershed polygon.
            x_var (str): The x variable name in the data array.
            y_var (str): The y variable name in the data array.
        """
        self.data_array = data_array
        self.watershed = watershed
        self.x_var = x_var
        self.y_var = y_var
        self.x_cellsize, self.y_cellsize = self.data_array.rio.resolution()
        self.transform = self.data_array.rio.transform()
        self.width = self.data_array.rio.width
        self.height = self.data_array.rio.height
        self._np_data_array = None
        self._watershed_window = None
        self._watershed_mask = None
        self._watershed_mask_clipped = None
        self._valid_shifts = None
        self._selected_stride = None
        self._valid_spaces = None
        self._valid_spaces_polygon = None
        self._data_array_x_coords = None
        self._data_array_y_coords = None

    def _calculate_watershed_mask_and_window(self) -> None:
        """
        Calculate the watershed mask and window.

        This method creates a mask array and determines the window of the watershed
        within the data array.
        """
        mask_array = geometry_mask(
            [self.watershed],
            out_shape=(int(self.data_array.rio.height), int(self.data_array.rio.width)),
            transform=self.data_array.rio.transform(recalc=True),
            all_touched=True,
            invert=True,
        )
        self._watershed_mask = mask_array
        window = get_data_window(np.ma.masked_array(mask_array, ~mask_array))
        self._watershed_window = window
        (row_start, row_stop), (col_start, col_stop) = window.toranges()
        self._watershed_mask_clipped = mask_array[row_start:row_stop, col_start:col_stop]

    @property
    def data_array_x_coords(self) -> np.ndarray:
        """
        Get the x coordinates of the data array.

        Returns
        -------
            np.ndarray: The x coordinates of the data array.
        """
        if not isinstance(self._data_array_x_coords, np.ndarray):
            self._data_array_x_coords = self.data_array[self.x_var].to_numpy()
        return self._data_array_x_coords

    @property
    def data_array_y_coords(self) -> np.ndarray:
        """
        Get the y coordinates of the data array.

        Returns
        -------
            np.ndarray: The y coordinates of the data array.
        """
        if not isinstance(self._data_array_y_coords, np.ndarray):
            self._data_array_y_coords = self.data_array[self.y_var].to_numpy()
        return self._data_array_y_coords

    @property
    def np_data_array(self) -> np.ndarray:
        """
        Get the data array as a numpy array.

        Returns
        -------
            np.ndarray: The data array as a numpy array.
        """
        if not isinstance(self._np_data_array, np.ndarray):
            self._np_data_array = self.data_array.to_numpy()
        return self._np_data_array

    @property
    def watershed_window(self) -> Window:
        """
        Get the window of the watershed in the data array.

        Returns
        -------
            Window: The window of the watershed in the data array.
        """
        if self._watershed_window is None:
            self._calculate_watershed_mask_and_window()
        return self._watershed_window

    @property
    def watershed_mask(self) -> np.ndarray:
        """
        Get the watershed mask as a 2D boolean numpy array.

        Returns
        -------
            np.ndarray: The watershed mask as a 2D boolean numpy array.
        """
        if not isinstance(self._watershed_mask, np.ndarray):
            self._calculate_watershed_mask_and_window()
        return self._watershed_mask

    @property
    def watershed_mask_clipped(self) -> np.ndarray:
        """
        Get the clipped watershed mask.

        Returns
        -------
            np.ndarray: The clipped watershed mask.
        """
        if not isinstance(self._watershed_mask_clipped, np.ndarray):
            self._calculate_watershed_mask_and_window()
        return self._watershed_mask_clipped

    def _find_valid_shifts(self, stride: int) -> list[tuple[int, int]]:
        mask = self.watershed_mask_clipped
        if not mask.any():
            return []
        rows, cols = self.watershed_window.toslices()
        height, width = mask.shape
        values = self.np_data_array
        shifts = []
        # Start at the raster origin and retain column-first order for tied means.
        for col in range(0, self.width - width + 1, stride):
            for row in range(0, self.height - height + 1, stride):
                window = values[row : row + height, col : col + width]
                if np.isfinite(window[mask]).all():
                    shifts.append((col - cols.start, row - rows.start))
        return shifts

    @property
    def valid_shifts(self) -> list[tuple[int, int]]:
        """Use the first spacing with valid placements, including for coverage geometry."""
        if self._valid_shifts is None:
            for stride in TRANSLATION_STRIDES:
                shifts = self._find_valid_shifts(stride)
                if shifts:
                    self._selected_stride = stride
                    break
            self._valid_shifts = shifts
        if not self._valid_shifts:
            raise NoValidPlacementError("no_valid_placement: no valid watershed placement at strides 4 and 1")
        return self._valid_shifts

    @property
    def selected_stride_cells(self) -> int:
        """Return the spacing that supplied the candidate set for this event."""
        if self._selected_stride is None:
            _ = self.valid_shifts
        return self._selected_stride

    @property
    def valid_spaces(self) -> np.ndarray:
        """
        Calculate and return the valid spaces mask.

        This method initializes the valid mask as the watershed mask value, iterates over
        the list of shift values, applies the shifts to the watershed mask, and performs
        a logical OR with the valid mask and the rolled watershed mask.

        Returns
        -------
            np.ndarray: The valid spaces mask as a boolean numpy array.
        """
        if not isinstance(self._valid_spaces, np.ndarray):
            valid_spaces = np.full(self.watershed_mask.shape, False, dtype=bool)
            for shift in self.valid_shifts:
                rolled = np.roll(self.watershed_mask, shift, axis=(1, 0))
                valid_spaces = np.logical_or(valid_spaces, rolled)
            self._valid_spaces = valid_spaces
        return self._valid_spaces

    def _array_to_polygon(self, arr: np.ndarray) -> Polygon:
        """
        Convert a boolean array to a geometry using the coordinates of the dataset.

        Args:
            arr (np.ndarray): The boolean array to convert.

        Returns
        -------
            Polygon: The resulting polygon.
        """
        shapely_shapes = [
            shape(converted_shape) for converted_shape, _ in shapes(arr.astype(np.ubyte), arr, transform=self.transform)
        ]
        if len(shapely_shapes) != 1:
            raise ValueError(f"Expected single geometry feature, got {len(shapely_shapes)}")
        valid_spaces_geom = shapely_shapes[0]
        if valid_spaces_geom.geom_type != "Polygon":
            raise TypeError(f"Expected geometry type 'Polygon', got {valid_spaces_geom.geom_type}")
        return valid_spaces_geom

    @property
    def valid_spaces_polygon(self) -> Polygon:
        """
        Convert the valid spaces boolean array to a polygon.

        Returns
        -------
            Polygon: The valid spaces polygon.
        """
        if self._valid_spaces_polygon is None:
            self._valid_spaces_polygon = self._array_to_polygon(self.valid_spaces)
        return self._valid_spaces_polygon

    def max_transpose(self, func: Callable[[np.ndarray], Any] | None = None) -> tuple[Polygon, Affine, Any | None]:
        """
        Calculate the maximum transpose of the watershed mask over the data array.

        This method initializes the max transpose array, max shift, and stats collection as None.
        It iterates over the list of shift values, applies the shifts to the watershed mask,
        calculates the stats, and if the stats collection has a greater mean than the max stats,
        it overwrites the max stats, max shift, and max transpose array.

        Args:
            func (Callable[[np.ndarray], Any] | None): A callable to apply to the data array.

        Returns
        -------
            tuple[Polygon, Affine, Any | None]: The resulting polygon, affine transformation, and results.
        """
        original_window_row_slice, original_window_col_slice = self.watershed_window.toslices()
        max_mean = None
        max_shift = None
        results = None
        for x_delta, y_delta in self.valid_shifts:
            adjusted_row_start = original_window_row_slice.start + y_delta
            adjusted_row_stop = original_window_row_slice.stop + y_delta
            adjusted_col_start = original_window_col_slice.start + x_delta
            adjusted_col_stop = original_window_col_slice.stop + x_delta
            data_clipped = self.np_data_array[
                adjusted_row_start:adjusted_row_stop, adjusted_col_start:adjusted_col_stop
            ]
            data_clipped_masked = np.ma.masked_array(data_clipped, ~self.watershed_mask_clipped)
            mean = np.nanmean(data_clipped_masked)
            if max_mean is None or mean > max_mean:
                max_mean = mean
                max_shift = (float(x_delta * self.x_cellsize), float(y_delta * self.y_cellsize))
                if func:
                    results = func(data_clipped_masked)
        poly = self._array_to_polygon(self.watershed_mask)
        poly = translate(poly, *max_shift)
        aff = Affine.translation(*max_shift)
        return poly, aff, results
