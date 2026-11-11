"""Exercise refreshed vector and Zarr dependencies with tiny offline fixtures."""

import json

import pytest

fiona = pytest.importorskip("fiona")
gpd = pytest.importorskip("geopandas")
fsspec = pytest.importorskip("fsspec")
s3fs = pytest.importorskip("s3fs")
np = pytest.importorskip("numpy")
xr = pytest.importorskip("xarray")
pytest.importorskip("zarr")
pytest.importorskip("dask.array")
geometry = pytest.importorskip("shapely.geometry")


def _rainfall_dataset():
    """Return two hourly fields that distinguish zero, missingness and cell order."""
    values = np.array([[[0, 1], [2, np.nan]], [[4, 5], [6, 7]]], dtype="float32")
    return xr.Dataset(
        {"APCP_surface": (("time", "latitude", "longitude"), values, {"units": "mm"})},
        coords={
            "time": np.array(["2020-01-01T01:00:00", "2020-01-01T02:00:00"], dtype="datetime64[ns]"),
            "latitude": [1.0, 0.0],
            "longitude": [0.0, 1.0],
        },
        attrs={"source": "synthetic offline fixture"},
    )


def _write_consolidated_v2(dataset, mapper):
    """Write the format consumed by the existing consolidated Zarr reader."""
    dataset.to_zarr(
        mapper,
        mode="w",
        consolidated=True,
        zarr_version=2,
        encoding={"APCP_surface": {"chunks": (1, 2, 2)}},
    )
    assert json.loads(mapper[".zgroup"])["zarr_format"] == 2
    assert ".zmetadata" in mapper


def test_fiona_geojson_roundtrip_preserves_geometry_and_crs(tmp_path):
    """Preserve polygon holes, attributes and known projected coordinates through Fiona."""
    polygon = geometry.Polygon(
        [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
        holes=[[(0.25, 0.25), (0.25, 0.75), (0.75, 0.75), (0.75, 0.25), (0.25, 0.25)]],
    )
    path = tmp_path / "domain.geojson"
    schema = {"geometry": "Polygon", "properties": {"feature_id": "int", "label": "str"}}
    with fiona.open(path, "w", driver="GeoJSON", crs="EPSG:4326", schema=schema) as output:
        output.write(
            {"geometry": geometry.mapping(polygon), "properties": {"feature_id": 7, "label": "synthetic"}}
        )
    original = path.read_bytes()

    frame = gpd.read_file(path, engine="fiona")

    assert path.read_bytes() == original
    assert frame.crs.to_epsg() == 4326
    assert frame["feature_id"].tolist() == [7]
    assert frame["label"].tolist() == ["synthetic"]
    actual = frame.geometry.iloc[0]
    assert actual.is_valid and actual.equals(polygon)
    assert len(actual.interiors) == 1
    assert actual.area == pytest.approx(0.75)
    projected = frame.to_crs("EPSG:3857")
    assert projected.geometry.iloc[0].bounds == pytest.approx(
        (0.0, 0.0, 111319.49079327357, 111325.1428663851), rel=0, abs=1e-6
    )
    assert projected.to_crs("EPSG:4326").geometry.iloc[0].equals_exact(polygon, tolerance=1e-10)


def test_fsspec_consolidated_zarr_preserves_hourly_values(tmp_path):
    """Reopen real consolidated Zarr through a fresh mapper without losing time or nodata."""
    source = _rainfall_dataset()
    root = str(tmp_path / "rainfall.zarr")
    _write_consolidated_v2(source, fsspec.get_mapper(root, create=True, auto_mkdir=True))

    with xr.open_zarr(fsspec.get_mapper(root), consolidated=True, chunks={}) as reopened:
        assert reopened["APCP_surface"].chunks is not None
        assert reopened["APCP_surface"].dtype == np.dtype("float32")
        xr.testing.assert_identical(reopened.load(), source)


def test_s3map_wrapper_combines_local_zarr_stores_without_s3_client(tmp_path, monkeypatch):
    """Exercise the S3Map interface with a local filesystem, not S3 transport or credentials."""

    def forbidden_client(*args, **kwargs):
        """Fail before an accidental default S3 client can be created or recovered."""
        pytest.fail("The offline mapper fixture must never create or recover an S3 client")

    monkeypatch.setattr(s3fs.S3FileSystem, "__init__", forbidden_client)
    monkeypatch.setattr(s3fs.S3FileSystem, "current", forbidden_client)
    filesystem = fsspec.filesystem("file", auto_mkdir=True)
    source = _rainfall_dataset()
    roots = []
    for index in range(2):
        root = str(tmp_path / f"hour-{index}.zarr")
        mapper = s3fs.S3Map(root=root, s3=filesystem, check=False, create=True)
        _write_consolidated_v2(source.isel(time=slice(index, index + 1)), mapper)
        roots.append(root)

    # Match StormHub's multifile reader while keeping the underlying filesystem local.
    maps = [s3fs.S3Map(root=root, s3=filesystem, check=False) for root in roots]
    with xr.open_mfdataset(maps, engine="zarr", chunks="auto", consolidated=True) as reopened:
        assert reopened["APCP_surface"].chunks is not None
        xr.testing.assert_identical(reopened.load(), source)
