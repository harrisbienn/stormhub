"""Small real NetCDF fixtures exercise state metadata, packing and spatial support."""

from datetime import timedelta

import pytest

h5netcdf = pytest.importorskip("h5netcdf")
np = pytest.importorskip("numpy")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from stormhub.met.nwm_state import decode  # noqa: E402
from stormhub.met.precipitation_features import PrecipitationFeatureError, utc  # noqa: E402

REQUEST = {
    "cycle": "2026-11-04T10:00:00Z",
    "model_version": "v3.1",
    "product": "land",
    "lake_ids": [],
    "base_url": "https://example.test",
    "retrieval_revision": "test",
}


def native(path, product="land"):
    """Write a native-shaped file with two unequal fractional-cell contributions."""
    with h5netcdf.File(path, "w") as ds:
        ds.dimensions = {"time": 1, "reference_time": 1, "x": 2, "y": 2, "feature_id": 2}
        initial = utc(REQUEST["cycle"]) - timedelta(hours=3)
        ds.attrs.update(
            NWM_version_number="v3.1",
            model_output_type=product,
            model_configuration="analysis_and_assimilation",
            proj4="EPSG:5070",
            model_initialization_time=initial.strftime("%Y-%m-%d_%H:%M:%S"),
            model_output_valid_time=utc(REQUEST["cycle"]).strftime("%Y-%m-%d_%H:%M:%S"),
        )
        for name, time in [("time", utc(REQUEST["cycle"])), ("reference_time", initial)]:
            v = ds.create_variable(name, (name,), dtype="i8", data=[int(time.timestamp() / 60)])
            v.attrs["units"] = "minutes since 1970-01-01 00:00:00 UTC"
        for name in ["x", "y"]:
            v = ds.create_variable(name, (name,), dtype="f8", data=[500, 1500])
            v.attrs["units"] = "m"
        v = ds.create_variable(
            "SOILSAT_TOP", ("time", "y", "x"), dtype="i4", data=[[[200, 800], [500, 500]]], fillvalue=-9999
        )
        v.attrs.update(units="1", scale_factor=0.001, valid_range=np.array([0, 1000]), grid_mapping="crs")
        ids = ds.create_variable("feature_id", ("feature_id",), dtype="i8", data=[42, 43])
        ids.attrs["comment"] = "ComID from NHDPlusV2 waterbody layer"
        for name, units, values in [
            ("inflow", "m3 s-1", [125, 300]),
            ("outflow", "m3 s-1", [100, 200]),
            ("water_sfc_elev", "m", [20, 30]),
        ]:
            v = ds.create_variable(name, ("feature_id",), dtype="i4", data=values, fillvalue=-9999)
            v.attrs.update(units=units, scale_factor=0.1)


@pytest.fixture
def geometry():
    """Create a region extending halfway across the second grid cell."""
    from shapely.geometry import box, mapping

    return {
        "crs": "EPSG:5070",
        "partition": None,
        "regions": [{"id": "basin", "kind": "basin", "geometry": mapping(box(0, 0, 1500, 1000))}],
    }


def test_fractional_wetness_and_missing_area(tmp_path, geometry):
    """Known weighted mean retains explicit missing area rather than dry zeros."""
    path = tmp_path / "land.nc"
    native(path)
    result = decode(path, REQUEST, geometry=geometry)
    assert result["regions"][0]["value"] == pytest.approx(0.4)
    assert result["regions"][0]["missing_fraction"] == pytest.approx(0)
    with h5netcdf.File(path, "a") as ds:
        ds.variables["SOILSAT_TOP"][0, 0, 1] = -9999
    region = decode(path, REQUEST, geometry=geometry)["regions"][0]
    assert region["value"] == pytest.approx(0.2)
    assert region["missing_fraction"] == pytest.approx(1 / 3)


@pytest.mark.parametrize("fault", ["clock", "version", "units", "axis", "value", "packing"])
def test_native_rejections(tmp_path, geometry, fault):
    """Metadata mismatches and invalid packed values fail before export."""
    path = tmp_path / "land.nc"
    native(path)
    with h5netcdf.File(path, "a") as ds:
        if fault == "clock":
            ds.variables["time"][0] += 60
        if fault == "version":
            ds.attrs["NWM_version_number"] = "v3.0"
        if fault == "units":
            ds.variables["SOILSAT_TOP"].attrs["units"] = "mm"
        if fault == "axis":
            ds.variables["x"][1] = 1700
        if fault == "value":
            ds.variables["SOILSAT_TOP"][0, 0, 0] = 2000
        if fault == "packing":
            ds.variables["SOILSAT_TOP"].attrs["scale_factor"] = float("nan")
    with pytest.raises(PrecipitationFeatureError):
        decode(path, REQUEST, geometry=geometry)


def test_reservoir_selected_identity_and_missing_datum(tmp_path):
    """Waterbody values remain separate from channel IDs and unreviewed elevations."""
    path = tmp_path / "lake.nc"
    native(path, "reservoir")
    request = {**REQUEST, "product": "reservoir", "lake_ids": [42]}
    lake = decode(path, request)["lakes"][0]
    assert lake["quantities"]["inflow"]["value"] == pytest.approx(12.5)
    assert lake["vertical_datum"] == "unknown"
    with pytest.raises(PrecipitationFeatureError, match="absent"):
        decode(path, {**request, "lake_ids": [99]})


def test_coordinate_quantization_is_bounded_and_all_missing_is_null(tmp_path, geometry):
    """Native sub-metre quantization is retained; missing cells never become dry."""
    path = tmp_path / "quantized.nc"
    native(path)
    with h5netcdf.File(path, "a") as ds:
        ds.variables["x"][1] += 0.125
        ds.variables["SOILSAT_TOP"][0, 0, :] = -9999
    result = decode(path, REQUEST, geometry=geometry)
    assert result["regions"][0]["value"] is None
    assert result["regions"][0]["missing_fraction"] == 1
    assert len(result["coordinate_sha256"]) == 64
