"""Portable domain and coverage contracts; no native runtime or network."""

import copy
import json
from pathlib import Path

import pytest

from stormhub.met.model_domain_precipitation import CONTRACT, POLICY, new_domain, validate_domain, validate_export
from stormhub.met.precipitation_features import PrecipitationFeatureError, digest


def _rehash(value):
    value["export_id"] = (
        "model-domain-precipitation-" + digest({k: v for k, v in value.items() if k != "export_id"})[:24]
    )
    return value


@pytest.fixture
def envelope():
    """Construct semantic evidence only; native tests independently prove bytes."""
    source = json.loads((Path(__file__).parent / "fixtures/forecast_precipitation/hrrr-export-v1.json").read_bytes())
    domain = new_domain(grid=source["target_grid"], required_cells=[0], model_domain_sha256="a" * 64)
    value = {
        "contract_version": CONTRACT,
        "domain": domain,
        "source": source,
        "normalization": dict(POLICY),
        "seam": {
            "policy": POLICY["seam"],
            "forecast_start": source["valid_start"],
            "observed_end": None,
            "observed_values_included": False,
        },
        "producer": {
            "name": "stormhub.model-domain-precipitation",
            "version": "1.0.0",
            "revision": "a" * 40,
            "dirty": False,
            "code_sha256": "b" * 64,
            "dependencies": dict.fromkeys(("rasterio", "numpy", "pyproj", "shapely"), "fixture"),
            "gdal": "fixture",
        },
        "forecast_eligible": False,
        "intervals": [
            {
                **{k: row[k] for k in ("row", "start", "end", "values_sha256")},
                "required_cell_count": 1,
                "missing_required_cells": 0,
                "outside_domain_cells": 1,
                "area_m2": 1_000_000,
                "max_coverage_fraction_error": 0,
                "source_common_footprint_volume_m3": 0,
                "target_volume_m3": 0,
                "absolute_error_m3": 0,
                "tolerance_m3": 1e-6,
            }
            for row in source["intervals"]
        ],
    }
    return _rehash(value)


def test_semantic_envelope_does_not_claim_native_admission(envelope):
    """Separate semantic validation from the retained native proof boundary."""
    validate_export(envelope)
    assert not envelope["forecast_eligible"]
    assert not envelope["seam"]["observed_values_included"]


@pytest.mark.parametrize("cells", [[], [0, 0], [1, 0], [-1], [2], [True], [0.0], ["0"]])
def test_domain_requires_explicit_unique_integer_mask(envelope, cells):
    """Reject ambiguous or missing cells instead of inferring a smaller domain."""
    with pytest.raises(PrecipitationFeatureError):
        new_domain(grid=envelope["domain"]["grid"], required_cells=cells, model_domain_sha256="a" * 64)


def test_domain_budgets_and_evidence_identity(envelope):
    """Large masks fail without silently cropping; domain evidence affects identity."""
    grid = copy.deepcopy(envelope["domain"]["grid"])
    grid["shape"] = [501, 501]
    with pytest.raises(PrecipitationFeatureError, match="limits"):
        new_domain(grid=grid, required_cells=[0], model_domain_sha256="a" * 64)
    grid["shape"] = [250, 250]
    with pytest.raises(PrecipitationFeatureError, match="limits"):
        new_domain(grid=grid, required_cells=list(range(50001)), model_domain_sha256="a" * 64)
    changed = new_domain(grid=envelope["domain"]["grid"], required_cells=[0], model_domain_sha256="b" * 64)
    assert changed["domain_id"] != envelope["domain"]["domain_id"]
    changed["required_cells"] = [1]
    with pytest.raises(PrecipitationFeatureError, match="identity"):
        validate_domain(changed)


@pytest.mark.parametrize("change", ["area-bool", "observed-zero", "count-bool"])
def test_rehashed_scalar_type_confusion_refuses(envelope, change):
    """Boolean and numeric equality cannot replace distinct wire types."""
    if change == "observed-zero":
        envelope["seam"]["observed_values_included"] = 0
    elif change == "count-bool":
        envelope["intervals"][0]["required_cell_count"] = True
    else:
        envelope["intervals"][0]["area_m2"] = True
    with pytest.raises(PrecipitationFeatureError):
        validate_export(_rehash(envelope))


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "swap",
        "cell",
        "count",
        "area",
        "volume",
        "tolerance",
        "coverage",
        "mask",
        "seam",
        "observed",
        "policy",
        "eligible",
        "producer",
    ],
)
def test_rehashed_invalid_coverage_refuses(envelope, change):
    """Consistent outer hashes cannot legitimize invalid time, area or volume proof."""
    if change == "missing":
        envelope["intervals"].pop()
    elif change == "extra":
        envelope["intervals"].append(envelope["intervals"][0])
    elif change == "swap":
        envelope["intervals"].reverse()
    elif change == "cell":
        envelope["intervals"][0]["missing_required_cells"] = 1
    elif change == "count":
        envelope["intervals"][0]["required_cell_count"] = 0
    elif change == "area":
        envelope["intervals"][0]["area_m2"] -= 1
    elif change == "volume":
        envelope["intervals"][0]["target_volume_m3"] = 1
    elif change == "tolerance":
        envelope["intervals"][0]["tolerance_m3"] = 1
    elif change == "coverage":
        envelope["intervals"][0]["max_coverage_fraction_error"] = 0.01
    elif change == "mask":
        envelope["intervals"][0]["outside_domain_cells"] = 0
    elif change == "seam":
        envelope["seam"]["observed_end"] = "2025-10-25T19:00:00Z"
    elif change == "observed":
        envelope["seam"]["observed_values_included"] = True
    elif change == "policy":
        envelope["normalization"]["missing_required"] = "zero"
    elif change == "eligible":
        envelope["forecast_eligible"] = True
    elif change == "producer":
        envelope["producer"]["revision"] = "main"
    with pytest.raises(PrecipitationFeatureError):
        validate_export(_rehash(envelope))
