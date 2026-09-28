"""Regression tests for thumbnails produced by background workers."""

from types import SimpleNamespace

import numpy as np
import xarray as xr
from matplotlib import pyplot as plt
from PIL import Image
from shapely.geometry import box

from stormhub.met.aorc.aorc import AORCItem


def test_thumbnail_renders_without_gui_manager(tmp_path, monkeypatch):
    """Write a real PNG without requesting a desktop window or closing other figures."""
    item = object.__new__(AORCItem)
    item.item_id = "1"
    item.local_directory = str(tmp_path)
    item.assets = {}
    item._transposed_watershed = box(1, 1, 2, 2)
    item.watershed_geometry = box(0, 0, 1, 1)
    item._transpose = SimpleNamespace(valid_spaces_polygon=box(0, 0, 3, 3))
    item._sum_aorc = xr.Dataset(
        {"APCP_surface": (("latitude", "longitude"), np.ones((4, 4)))},
        coords={"latitude": np.arange(4), "longitude": np.arange(4)},
    )

    def reject_gui(*args, **kwargs):
        raise AssertionError("Thumbnail requested a GUI window or changed another figure")

    monkeypatch.setattr(plt, "new_figure_manager", reject_gui)
    monkeypatch.setattr(plt, "close", reject_gui)
    figure = item.aorc_thumbnail(scale_max=1, return_fig=True)
    assert figure is not None
    assert figure.canvas.manager is None
    assert item.assets["thumbnail"].href == "1.thumbnail.png"
    with Image.open(tmp_path / "1.thumbnail.png") as image:
        image.verify()
    item.aorc_thumbnail(scale_max=1)
