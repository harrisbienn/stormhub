"""Tiny synthetic NetCDF4 files exercise actual packed NWM channel decoding."""

from datetime import timedelta

import pytest

h5netcdf = pytest.importorskip("h5netcdf")
np = pytest.importorskip("numpy")

from stormhub.met.nwm_channel import decode, selections
from stormhub.met.precipitation_features import PrecipitationFeatureError, utc
from test_nwm_channel import REQUEST


def native(path, request=REQUEST):
    """Create two real packed NetCDF4 channels with known flows and clocks."""
    selected = selections(request)[0]
    initialization = utc(request["cycle"])
    if request["product"] == "analysis_assim":
        initialization -= timedelta(hours=3)
    with h5netcdf.File(path, "w") as f:
        f.dimensions = {"feature_id": 2, "time": 1, "reference_time": 1}
        f.attrs.update(
            {
                "NWM_version_number": "v3.0",
                "model_output_type": "channel_rt",
                "model_configuration": "short_range"
                if request["product"] == "short_range"
                else "analysis_and_assimilation",
                "model_initialization_time": initialization.strftime("%Y-%m-%d_%H:%M:%S"),
                "model_output_valid_time": utc(selected["valid_time"]).strftime("%Y-%m-%d_%H:%M:%S"),
                "code_version": "synthetic",
            }
        )
        for name, t in [("reference_time", initialization), ("time", utc(selected["valid_time"]))]:
            v = f.create_variable(name, (name,), dtype="i8", data=[int(t.timestamp() / 60)])
            v.attrs["units"] = "minutes since 1970-01-01 00:00:00 UTC"
        ids = f.create_variable("feature_id", ("feature_id",), dtype="i8", data=[15223060, 99])
        ids.attrs["comment"] = "NHDPlusv2 ComIDs within CONUS, arbitrary Reach IDs outside of CONUS"
        flow = f.create_variable("streamflow", ("feature_id",), dtype="i4", data=[1250, 400], fillvalue=-999900)
        flow.attrs.update(
            {
                "long_name": "River Flow",
                "units": "m3 s-1",
                "scale_factor": 0.01,
                "add_offset": 0.0,
                "valid_range": np.array([0, 5000000], dtype="i4"),
            }
        )
    return selected


@pytest.mark.parametrize("product,hours", [("short_range", 2), ("analysis_assim", 0)])
def test_actual_packing_and_analysis_clock(tmp_path, product, hours):
    """Known packed values decode once, preserving the three-hour analysis origin."""
    request = {**REQUEST, "product": product, "hours": hours}
    path = tmp_path / "tiny.nc"
    selected = native(path, request)
    result = decode(path, request, selected)
    assert result["flow_m3_s"] == {"15223060": 12.5}
    assert utc(result["model_initialization"]).hour == (15 if product == "analysis_assim" else 18)


@pytest.mark.parametrize("fault", ["units", "stage", "network", "missing", "duplicate", "reach", "clock", "version"])
def test_native_rejections(tmp_path, fault):
    """Metadata, missingness and selected ID errors cannot yield usable flows."""
    path = tmp_path / "tiny.nc"
    selected = native(path)
    with h5netcdf.File(path, "a") as f:
        if fault == "units":
            f.variables["streamflow"].attrs["units"] = "ft3 s-1"
        if fault == "stage":
            f.variables["streamflow"].attrs["long_name"] = "Water level"
        if fault == "network":
            f.variables["feature_id"].attrs["comment"] = "NHDPlusHR"
        if fault == "missing":
            f.variables["streamflow"][0] = -999900
        if fault == "duplicate":
            f.variables["feature_id"][1] = 15223060
        if fault == "reach":
            f.variables["feature_id"][0] = 98
        if fault == "clock":
            f.variables["time"][0] += 1
        if fault == "version":
            f.attrs["NWM_version_number"] = "v2.1"
    with pytest.raises(PrecipitationFeatureError):
        decode(path, REQUEST, selected)
