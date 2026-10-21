"""Conservative rainfall remapping shared by projected HRRR and geographic MRMS grids."""

from __future__ import annotations

import math

from .precipitation_features import PrecipitationFeatureError, digest, validate_grid


def source_grid(dataset) -> dict:
    """Bind the native raster coordinate system and north-to-south transform."""
    transform = dataset.transform
    if (
        dataset.count != 1
        or dataset.crs is None
        or not (dataset.crs.is_projected or dataset.crs.is_geographic)
        or transform.b != 0
        or transform.d != 0
        or transform.a <= 0
        or transform.e >= 0
    ):
        raise PrecipitationFeatureError("expected one georeferenced north-to-south raster")
    return {"crs": dataset.crs.to_wkt(), "shape": [dataset.height, dataset.width], "transform": list(transform)[:6]}


def conservative_weights(dataset, grid: dict, indices: list[int]) -> dict:
    """Intersect native cells with selected target cells in the target metre CRS.

    Edges are densified into four segments before projection. Conservation is
    measured over this projected, piecewise-linear common footprint. This is
    a first-order remap, not a claim of geodesic or subcell rainfall accuracy.
    """
    import numpy as np
    import shapely
    from pyproj import CRS, Transformer
    from shapely.geometry import box

    validate_grid(grid)
    target = CRS.from_user_input(grid["crs"])
    if not target.is_projected or any(a.unit_conversion_factor != 1 for a in target.axis_info):
        raise PrecipitationFeatureError("target requires a projected metre CRS")
    rows, cols = grid["shape"]
    if rows * cols > 250_000 or not indices or len(indices) > 50_000:
        raise PrecipitationFeatureError("target exceeds bounded research subset limits")
    if indices != sorted(set(indices)) or any(type(i) is not int or not 0 <= i < rows * cols for i in indices):
        raise PrecipitationFeatureError("target cell indices must be sorted, unique and in bounds")
    x0, y0 = grid["origin"]
    size = grid["cell_size_meters"]
    left = x0 + min(i % cols for i in indices) * size
    right = x0 + (max(i % cols for i in indices) + 1) * size
    bottom = y0 + min(i // cols for i in indices) * size
    top = y0 + (max(i // cols for i in indices) + 1) * size
    inverse = Transformer.from_crs(target, dataset.crs, always_xy=True)
    bounds = inverse.transform_bounds(left, bottom, right, top, densify_pts=21)
    transform = dataset.transform
    c0 = max(0, math.floor((bounds[0] - transform.c) / transform.a) - 2)
    c1 = min(dataset.width, math.ceil((bounds[2] - transform.c) / transform.a) + 2)
    r0 = max(0, math.floor((bounds[3] - transform.f) / transform.e) - 2)
    r1 = min(dataset.height, math.ceil((bounds[1] - transform.f) / transform.e) + 2)
    if c1 <= c0 or r1 <= r0 or (r1 - r0) * (c1 - c0) > 100_000:
        raise PrecipitationFeatureError("native subset is empty or exceeds 100000 cells")
    project = Transformer.from_crs(dataset.crs, target, always_xy=True)
    weights = {i: [] for i in indices}
    targets = {
        i: box(x0 + i % cols * size, y0 + i // cols * size, x0 + (i % cols + 1) * size, y0 + (i // cols + 1) * size)
        for i in indices
    }
    # Shared edge vertices ensure adjacent native cells form the same partition.
    offsets = [(i / 4, 0) for i in range(4)] + [(1, i / 4) for i in range(4)]
    offsets += [(1 - i / 4, 1) for i in range(4)] + [(0, 1 - i / 4) for i in range(4)]
    for row in range(r0, r1):
        for col in range(c0, c1):
            xy = [
                (transform.c + transform.a * (col + dx), transform.f + transform.e * (row + dy)) for dx, dy in offsets
            ]
            px, py = project.transform([p[0] for p in xy], [p[1] for p in xy])
            polygon = shapely.polygons(np.column_stack((px, py)))
            if polygon.is_empty or not polygon.is_valid:
                raise PrecipitationFeatureError("invalid projected native cell")
            a, b, c, d = polygon.bounds
            for tr in range(max(0, math.floor((b - y0) / size)), min(rows, math.ceil((d - y0) / size))):
                for tc in range(max(0, math.floor((a - x0) / size)), min(cols, math.ceil((c - x0) / size))):
                    index = tr * cols + tc
                    if index not in weights:
                        continue
                    area = polygon.intersection(targets[index]).area
                    if area > 0:
                        weights[index].append([(row - r0) * (c1 - c0) + col - c0, area])
    errors = [abs(math.fsum(a for _, a in pairs) / size**2 - 1) for pairs in weights.values()]
    if max(errors) > 1e-7:
        raise PrecipitationFeatureError("native cells do not completely partition every selected target cell")
    return {
        "method": "projected-cell-intersection-v1",
        "edge_segments": 4,
        "native_grid": source_grid(dataset),
        "target_grid_sha256": digest(grid),
        "window": [c0, r0, c1 - c0, r1 - r0],
        "target_cells": [{"index": i, "sources": pairs} for i, pairs in weights.items()],
        "max_coverage_fraction_error": max(errors),
        "coverage_tolerance": 1e-7,
    }


def remap_interval(dataset, weights: dict, grid: dict) -> tuple[list, dict]:
    """Remap one interval, rejecting missing/negative consumed source values."""
    import numpy as np
    from rasterio.windows import Window

    if source_grid(dataset) != weights["native_grid"]:
        raise PrecipitationFeatureError("native grid changed between hourly messages")
    depths = dataset.read(1, window=Window(*weights["window"]), masked=True).flatten()
    used = sorted({i for cell in weights["target_cells"] for i, _ in cell["sources"]})
    values = depths[used]
    if np.ma.getmaskarray(values).any() or not np.isfinite(values.data).all() or (values.data < 0).any():
        raise PrecipitationFeatureError("missing, nonfinite or negative precipitation in consumed native cells")
    output = [None] * (grid["shape"][0] * grid["shape"][1])
    source_areas = {}
    target_volumes = []
    for cell in weights["target_cells"]:
        volume = math.fsum(float(depths[i]) * area / 1000 for i, area in cell["sources"])
        output[cell["index"]] = volume * 1000 / grid["cell_size_meters"] ** 2
        target_volumes.append(output[cell["index"]] * grid["cell_size_meters"] ** 2 / 1000)
        for i, area in cell["sources"]:
            source_areas.setdefault(i, []).append(area)
    source_volume = math.fsum(float(depths[i]) * math.fsum(areas) / 1000 for i, areas in source_areas.items())
    target_volume = math.fsum(target_volumes)
    error = abs(target_volume - source_volume)
    tolerance = max(1e-6, source_volume * 1e-10)
    if error > tolerance:
        raise PrecipitationFeatureError("remapping fails common-footprint volume accounting")
    return output, {
        "source_common_footprint_volume_m3": source_volume,
        "target_volume_m3": target_volume,
        "absolute_error_m3": error,
        "tolerance_m3": tolerance,
    }
