"""Complete, authenticated model-domain rainfall over retained forecast sources.

Domain declarations are supplied by the model owner. This boundary proves the
declared cell/time matrix, not that the declaration represents an approved model.
Native precipitation decoding stays here; DSS and model preparation do not.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

from . import forecast_precipitation
from .hrrr_acquisition import verify_asset, write_immutable
from .hrrr_normalization import normalize, validate_hour_tags
from .precipitation_features import (
    SUPPORT_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    exact,
    number,
    sha256,
    utc,
    validate_grid,
)
from .precipitation_remapping import conservative_weights, remap_interval

CONTRACT = "stormhub/model-domain-precipitation/1.0.0"
DOMAIN_CONTRACT = "stormhub/model-forcing-domain/1.0.0"
POLICY = {
    "contract_version": "stormhub/model-domain-normalization/1.0.0",
    "remapping": "projected-cell-intersection-v1",
    "edge_segments": 4,
    "coverage_fraction_tolerance": 1e-7,
    "depth_absolute_tolerance_mm": 1e-9,
    "depth_relative_tolerance": 1e-10,
    "volume_absolute_tolerance_m3": 1e-6,
    "volume_relative_tolerance": 1e-10,
    "accumulation": "source-interval-depth-no-differencing-v1",
    "interval_bounds": "start-exclusive-end-inclusive-v1",
    "missing_required": "refuse-v1",
    "outside_domain": "null-not-consumed-v1",
    "seam": "observed-end-equals-forecast-start-no-overlap-v1",
}
_HRRR = ("noaa", "hrrr", "hrrr-v4", "deterministic", "conus-hourly-surface-apcp", "0.1.0")
_PRODUCT_FIELDS = ("provider", "model", "model_version", "member", "product", "adapter_version")


def new_domain(*, grid: dict, required_cells: list[int], model_domain_sha256: str) -> dict[str, Any]:
    """Bind the complete caller-declared model mask to its model-owner evidence."""
    value = {
        "contract_version": DOMAIN_CONTRACT,
        "purpose": "hydrologic-forcing",
        "grid": copy.deepcopy(grid),
        "required_cells": list(required_cells),
        "model_domain_sha256": model_domain_sha256,
        "mask_policy": "explicit-full-required-cells/1.0.0",
    }
    value["domain_id"] = "model-forcing-domain-" + digest(value)[:24]
    validate_domain(value)
    return value


def validate_domain(value: Mapping[str, Any]) -> None:
    """Require a bounded, explicit mask; never derive it from available rainfall."""
    exact(
        value,
        ("contract_version", "purpose", "grid", "required_cells", "model_domain_sha256", "mask_policy", "domain_id"),
        "model forcing domain",
    )
    if (
        value["contract_version"] != DOMAIN_CONTRACT
        or value["purpose"] != "hydrologic-forcing"
        or value["mask_policy"] != "explicit-full-required-cells/1.0.0"
    ):
        raise PrecipitationFeatureError("unsupported model forcing domain policy")
    validate_grid(value["grid"])
    sha256(value["model_domain_sha256"], "model-owner domain evidence")
    count = math.prod(value["grid"]["shape"])
    cells = value["required_cells"]
    if count > 250_000 or not isinstance(cells, list) or not cells or len(cells) > 50_000:
        raise PrecipitationFeatureError("model domain exceeds 250000 raster or 50000 required cell limits")
    if any(type(cell) is not int or not 0 <= cell < count for cell in cells) or cells != sorted(set(cells)):
        raise PrecipitationFeatureError("required cells must be sorted, unique and in bounds")
    if (
        value["domain_id"]
        != "model-forcing-domain-" + digest({k: v for k, v in value.items() if k != "domain_id"})[:24]
    ):
        raise PrecipitationFeatureError("model forcing domain identity differs")


def _support(domain: Mapping[str, Any]) -> dict:
    grid = domain["grid"]
    area = grid["cell_size_meters"] ** 2
    return {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "partition": None,
        "regions": [
            {
                "id": "model-domain",
                "kind": "domain",
                "geometry_sha256": digest(domain),
                "area_m2": area * len(domain["required_cells"]),
                "cells": [{"index": cell, "area_m2": area} for cell in domain["required_cells"]],
            }
        ],
    }


def _producer(revision: str, dirty: bool) -> dict:
    import rasterio

    return {
        "name": "stormhub.model-domain-precipitation",
        "version": "1.0.0",
        "revision": revision,
        "dirty": dirty,
        "code_sha256": digest(
            {
                name: (Path(__file__).parent / name).read_text(encoding="utf-8")
                for name in (
                    "model_domain_precipitation.py",
                    "precipitation_remapping.py",
                    "hrrr_normalization.py",
                    "forecast_precipitation.py",
                )
            }
        ),
        "dependencies": {name: version(name) for name in ("rasterio", "numpy", "pyproj", "shapely")},
        "gdal": rasterio.__gdal_version__,
    }


def _validate_producer(value: Mapping[str, Any]) -> None:
    exact(
        value, ("name", "version", "revision", "dirty", "code_sha256", "dependencies", "gdal"), "model-domain producer"
    )
    if (
        value["name"] != "stormhub.model-domain-precipitation"
        or value["version"] != "1.0.0"
        or not isinstance(value["revision"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["revision"])
        or type(value["dirty"]) is not bool
    ):
        raise PrecipitationFeatureError("invalid model-domain producer")
    sha256(value["code_sha256"], "model-domain producer code")
    dependencies = value["dependencies"]
    if (
        not isinstance(dependencies, Mapping)
        or set(dependencies) != {"rasterio", "numpy", "pyproj", "shapely"}
        or any(not isinstance(v, str) or not v.strip() for v in [*dependencies.values(), value["gdal"]])
    ):
        raise PrecipitationFeatureError("model-domain producer requires scientific dependency identities")


def _seam(source: Mapping[str, Any], observed_end: str | None) -> dict:
    if observed_end is not None and utc(observed_end) != utc(source["valid_start"]):
        raise PrecipitationFeatureError("observed/forecast seam must equal forecast start")
    return {
        "policy": POLICY["seam"],
        "forecast_start": source["valid_start"],
        "observed_end": observed_end,
        "observed_values_included": False,
    }


def _hrrr_evidence(source: Mapping[str, Any], domain: Mapping[str, Any], root: Path, raw: Path) -> list[dict]:
    import rasterio

    normalized = forecast_precipitation._read_asset(root, source["normalized"])
    support = forecast_precipitation._read_asset(root, source["support"])
    retained_weights = forecast_precipitation._read_asset(root, source["weights"])
    if source["target_grid"] != domain["grid"] or support != _support(domain):
        raise PrecipitationFeatureError("source grid/support does not equal the complete declared model domain")
    grid, cells = domain["grid"], domain["required_cells"]
    required = set(cells)
    weights = None
    evidence = []
    for interval, values in zip(source["intervals"], normalized["values"]):
        if any(value is None for i, value in enumerate(values) if i in required) or any(
            value is not None for i, value in enumerate(values) if i not in required
        ):
            raise PrecipitationFeatureError("required rainfall is missing or outside-domain mask is not null")
        path = verify_asset(raw, interval["raw"]["message"])
        with rasterio.open(path) as dataset:
            metadata = validate_hour_tags(dataset.tags(1), source["initialization"], interval["row"] + 1)
            if utc(metadata["start"]) != utc(interval["start"]) or utc(metadata["end"]) != utc(interval["end"]):
                raise PrecipitationFeatureError("native accumulation endpoints differ from the export")
            if weights is None:
                weights = conservative_weights(dataset, grid, cells)
                if retained_weights != weights:
                    raise PrecipitationFeatureError(
                        "retained remapping matrix differs from complete native intersections"
                    )
            actual, audit = remap_interval(dataset, weights, grid)
        # Recheck retained bytes after the native read as well as before it.
        verify_asset(raw, interval["raw"]["message"])
        if any(
            not math.isclose(
                values[i],
                actual[i],
                rel_tol=POLICY["depth_relative_tolerance"],
                abs_tol=POLICY["depth_absolute_tolerance_mm"],
            )
            for i in cells
        ):
            raise PrecipitationFeatureError("normalized rainfall differs from retained native source")
        volume = math.fsum(values[i] * grid["cell_size_meters"] ** 2 / 1000 for i in cells)
        error = abs(volume - audit["source_common_footprint_volume_m3"])
        tolerance = max(
            POLICY["volume_absolute_tolerance_m3"],
            audit["source_common_footprint_volume_m3"] * POLICY["volume_relative_tolerance"],
        )
        if error > tolerance:
            raise PrecipitationFeatureError("normalized volume fails complete-domain conservation")
        evidence.append(
            {
                "row": interval["row"],
                "start": interval["start"],
                "end": interval["end"],
                "values_sha256": interval["values_sha256"],
                "required_cell_count": len(cells),
                "missing_required_cells": 0,
                "outside_domain_cells": len(values) - len(cells),
                "area_m2": len(cells) * grid["cell_size_meters"] ** 2,
                "max_coverage_fraction_error": weights["max_coverage_fraction_error"],
                "source_common_footprint_volume_m3": audit["source_common_footprint_volume_m3"],
                "target_volume_m3": volume,
                "absolute_error_m3": error,
                "tolerance_m3": tolerance,
            }
        )
    return evidence


def _evidence(source: Mapping[str, Any], domain: Mapping[str, Any], root: Path, raw: Path) -> list[dict]:
    try:
        forecast_precipitation.verify_export(source, bundle_root=root, raw_root=raw)
        key = tuple(source[field] for field in _PRODUCT_FIELDS)
        if key not in _NATIVE_VERIFIERS:
            raise PrecipitationFeatureError("forecast product has no model-domain native verifier")
        return _NATIVE_VERIFIERS[key](source, domain, root, raw)
    except OSError as error:
        raise PrecipitationFeatureError("retained model-domain source is unavailable or unreadable") from error


def from_forecast(
    source: Mapping[str, Any],
    *,
    domain: Mapping[str, Any],
    bundle_root: str | Path,
    raw_root: str | Path,
    revision: str,
    dirty: bool = False,
    observed_end: str | None = None,
) -> dict[str, Any]:
    """Prove a complete domain from an existing owner export and retained raw bytes.

    ``observed_end`` declares only the adjoining observation cutoff, not observed
    rainfall availability or matching coverage. The consumer must authenticate that
    separate prefix. No acquisition, event selection or interpolation occurs here.
    """
    validate_domain(domain)
    forecast_precipitation.validate_export(source)
    producer = _producer(revision, dirty)
    _validate_producer(producer)
    seam = _seam(source, observed_end)
    evidence = _evidence(source, domain, Path(bundle_root), Path(raw_root))
    value = {
        "contract_version": CONTRACT,
        "domain": copy.deepcopy(domain),
        "source": copy.deepcopy(source),
        "normalization": dict(POLICY),
        "seam": seam,
        "intervals": evidence,
        "producer": producer,
        "forecast_eligible": False,
    }
    value["export_id"] = "model-domain-precipitation-" + digest(value)[:24]
    validate_export(value)
    return value


def normalize_hrrr(
    acquisition: dict,
    raw_root: str | Path,
    *,
    domain: Mapping[str, Any],
    output: str | Path,
    revision: str,
    dirty: bool = False,
    observed_end: str | None = None,
) -> dict[str, Any]:
    """Normalize retained HRRR over every declared model cell and write its proof."""
    validate_domain(domain)
    _validate_producer(_producer(revision, dirty))
    _seam({"valid_start": acquisition["request"]["initialization"]}, observed_end)
    root = Path(output)
    forcing = normalize(
        acquisition,
        Path(raw_root),
        grid=domain["grid"],
        support=_support(domain),
        output=root,
        revision=revision,
        dirty=dirty,
    )
    source = forecast_precipitation.from_hrrr(root / (forcing["dataset_id"] + ".json"), raw_root=raw_root)
    value = from_forecast(
        source,
        domain=domain,
        bundle_root=root,
        raw_root=raw_root,
        revision=revision,
        dirty=dirty,
        observed_end=observed_end,
    )
    write_immutable(root / (value["export_id"] + ".json"), canonical_bytes(value))
    return value


def validate_export(value: Mapping[str, Any]) -> None:
    """Validate portable domain/interval identities; retained proof needs verification."""
    exact(
        value,
        (
            "contract_version",
            "domain",
            "source",
            "normalization",
            "seam",
            "intervals",
            "producer",
            "forecast_eligible",
            "export_id",
        ),
        "model-domain precipitation",
    )
    if (
        value["contract_version"] != CONTRACT
        or value["normalization"] != POLICY
        or value["forecast_eligible"] is not False
    ):
        raise PrecipitationFeatureError("unsupported model-domain normalization or applicability")
    validate_domain(value["domain"])
    forecast_precipitation.validate_export(value["source"])
    _validate_producer(value["producer"])
    domain, source = value["domain"], value["source"]
    if source["target_grid"] != domain["grid"]:
        raise PrecipitationFeatureError("source and model domain grids differ")
    seam = exact(value["seam"], ("policy", "forecast_start", "observed_end", "observed_values_included"), "seam")
    if seam["observed_values_included"] is not False or seam != _seam(source, seam["observed_end"]):
        raise PrecipitationFeatureError("unsupported observed/forecast seam")
    intervals = value["intervals"]
    if not isinstance(intervals, list) or len(intervals) != len(source["intervals"]):
        raise PrecipitationFeatureError("model-domain interval matrix is incomplete")
    area = len(domain["required_cells"]) * domain["grid"]["cell_size_meters"] ** 2
    outside = math.prod(domain["grid"]["shape"]) - len(domain["required_cells"])
    for interval, original in zip(intervals, source["intervals"]):
        exact(
            interval,
            (
                "row",
                "start",
                "end",
                "values_sha256",
                "required_cell_count",
                "missing_required_cells",
                "outside_domain_cells",
                "area_m2",
                "max_coverage_fraction_error",
                "source_common_footprint_volume_m3",
                "target_volume_m3",
                "absolute_error_m3",
                "tolerance_m3",
            ),
            "model-domain interval",
        )
        number(interval["area_m2"], "model-domain interval area", positive=True)
        if (
            any(
                type(interval[k]) is not int
                for k in ("row", "required_cell_count", "missing_required_cells", "outside_domain_cells")
            )
            or any(interval[k] != original[k] for k in ("row", "start", "end", "values_sha256"))
            or interval["required_cell_count"] != len(domain["required_cells"])
            or interval["missing_required_cells"] != 0
            or interval["outside_domain_cells"] != outside
            or original["missing_cells"] != outside
            or interval["area_m2"] != area
        ):
            raise PrecipitationFeatureError("complete-domain interval coverage differs")
        for key in (
            "max_coverage_fraction_error",
            "source_common_footprint_volume_m3",
            "target_volume_m3",
            "absolute_error_m3",
            "tolerance_m3",
        ):
            number(interval[key], key)
        tolerance = max(
            POLICY["volume_absolute_tolerance_m3"],
            interval["source_common_footprint_volume_m3"] * POLICY["volume_relative_tolerance"],
        )
        if (
            interval["max_coverage_fraction_error"] > POLICY["coverage_fraction_tolerance"]
            or interval["tolerance_m3"] != tolerance
            or interval["absolute_error_m3"]
            != abs(interval["target_volume_m3"] - interval["source_common_footprint_volume_m3"])
            or interval["absolute_error_m3"] > tolerance
        ):
            raise PrecipitationFeatureError("complete-domain conservation exceeds policy tolerance")
    if (
        value["export_id"]
        != "model-domain-precipitation-" + digest({k: v for k, v in value.items() if k != "export_id"})[:24]
    ):
        raise PrecipitationFeatureError("model-domain precipitation identity differs")


def verify_export(
    value: Mapping[str, Any], *, expected_domain: Mapping[str, Any], bundle_root: str | Path, raw_root: str | Path
) -> None:
    """Authenticate the caller's expected domain and replay retained native rainfall.

    The separately supplied expected declaration prevents a self-consistent but
    narrowed source mask from silently becoming the model's complete domain.
    """
    validate_export(value)
    validate_domain(expected_domain)
    if value["domain"] != expected_domain:
        raise PrecipitationFeatureError("export does not match the expected model domain")
    evidence = _evidence(value["source"], expected_domain, Path(bundle_root), Path(raw_root))
    if evidence != value["intervals"]:
        raise PrecipitationFeatureError("model-domain evidence differs from retained native rainfall")


def write_export(
    value: Mapping[str, Any],
    directory: str | Path,
    *,
    expected_domain: Mapping[str, Any],
    bundle_root: str | Path,
    raw_root: str | Path,
) -> Path:
    """Verify all retained evidence before immutable content-addressed publication."""
    verify_export(value, expected_domain=expected_domain, bundle_root=bundle_root, raw_root=raw_root)
    path = Path(directory) / (value["export_id"] + ".json")
    write_immutable(path, canonical_bytes(value))
    return path


_NATIVE_VERIFIERS = {_HRRR: _hrrr_evidence}
